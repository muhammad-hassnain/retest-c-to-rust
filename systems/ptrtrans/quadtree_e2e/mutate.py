"""Harness power for the PtrTrans quadtree harness: mutation analysis on the agreeing translation.

The shipped translation is not an agreeing one (find_ never descends; run.py), so the agreeing
baseline is the "repaired" crate of run.py: the shipped crate with only find_'s quadrant lookup
fixed. Every mutant is that crate with ONE operator replaced in a function body of
src/{quadtree,node,bounds,point}.rs:
  relational   < <= > >= == !=  (each to its neighbours: < -> <=, > ; == -> != ...)
  arithmetic   + <-> -, * <-> /, += -> -=
  constant     every numeric literal c -> c-1 and c+1
The mutant is rebuilt (cargo +<toolchain>, debug) and the driver runs every case on which the
baseline agrees with the C and the C is UB-clean (from run.py's results). A mutant is KILLED if any
of those cases now differs from the C (wrong output, panic, crash or a 60 s timeout).

  python3 systems/ptrtrans/quadtree_e2e/mutate.py --art /art --build /oub/mut --toolchain 1.86.0
"""
import argparse
import collections
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(REPO, "harness"))
sys.path.insert(0, HERE)
import oracle_ub_common as ou  # noqa: E402
import run as qrun  # noqa: E402
from gen_cases import corpus  # noqa: E402

FILES = ["src/quadtree.rs", "src/node.rs", "src/bounds.rs", "src/point.rs"]
REL = {" < ": [" <= ", " > "], " <= ": [" < ", " >= "], " > ": [" >= ", " < "], " >= ": [" > ", " <= "],
       " == ": [" != "], " != ": [" == "]}
ARI = {" + ": [" - "], " - ": [" + "], " * ": [" / "], " / ": [" * "], " += ": [" -= "]}
NUM = re.compile(r"(?<![\w.])(\d+\.\d+|\d+)(?![\w.])")


def body_lines(src):
    """Indices of lines inside fn bodies (skip comments, signatures, use/struct lines)."""
    keep = []
    depth = 0
    in_fn = False
    for i, line in enumerate(src.splitlines()):
        s = line.strip()
        if s.startswith("pub fn ") or s.startswith("fn "):
            in_fn = True
        opens, closes = line.count("{"), line.count("}")
        if in_fn and depth >= 1 and not s.startswith("//") and "->" not in line:
            keep.append(i)
        depth += opens - closes
        if in_fn and depth == 0 and closes:
            in_fn = False
    return keep


def mutants(crate):
    out = []
    for rel in FILES:
        src = open(os.path.join(crate, rel)).read()
        lines = src.splitlines(keepends=True)
        for i in body_lines(src):
            line = lines[i]
            code = line.split("//")[0]
            for table, kind in ((REL, "relational"), (ARI, "arithmetic")):
                for op, reps in table.items():
                    start = 0
                    while True:
                        j = code.find(op, start)
                        if j < 0:
                            break
                        # " < " must not be the tail of " <= " etc.; table keys are space-delimited
                        for rep in reps:
                            new = line[:j] + rep + line[j + len(op):]
                            out.append({"file": rel, "line": i + 1, "kind": kind, "from": op.strip(),
                                        "to": rep.strip(), "orig": line.rstrip("\n"), "new": new.rstrip("\n")})
                        start = j + 1
            for m in NUM.finditer(code):
                lit = m.group(1)
                isf = "." in lit
                v = float(lit) if isf else int(lit)
                for dv in (-1, 1):
                    nv = v + dv
                    nlit = (repr(float(nv)) if isf else str(nv))
                    if nv < 0:
                        nlit = f"({nlit})"
                    new = line[:m.start()] + nlit + line[m.end():]
                    out.append({"file": rel, "line": i + 1, "kind": "constant", "from": lit, "to": nlit,
                                "orig": line.rstrip("\n"), "new": new.rstrip("\n")})
    return out


def apply(crate_src, dst, m):
    ou.copy_sources(crate_src, dst)
    p = os.path.join(dst, m["file"])
    lines = open(p).read().splitlines(keepends=True)
    assert lines[m["line"] - 1].rstrip("\n") == m["orig"]
    lines[m["line"] - 1] = m["new"] + "\n"
    open(p, "w").write("".join(lines))


def batch_script(cases):
    return "".join(f"=== {c['id']}\n{c['script']}" for c in cases)


def parse_batch(out):
    res, cur = {}, None
    for line in out.decode("utf-8", "replace").splitlines(keepends=True):
        if line.startswith("=== "):
            cur = line[4:].strip()
            res[cur] = ""
        elif cur is not None:
            res[cur] += line
    return res


def run_batch(binp, cases, timeout=60):
    o, e, rc, to = qrun.run_plain([binp, "--batch"], batch_script(cases).encode(), timeout=timeout)
    return parse_batch(o), rc, to


def build_sut(bdir, crate, toolchain, tag):
    sut = os.path.join(bdir, f"sut_{tag}")
    if os.path.isdir(sut):
        shutil.rmtree(sut)
    os.makedirs(os.path.join(sut, "src"))
    open(os.path.join(sut, "Cargo.toml"), "w").write(qrun.sut_cargo_toml(crate))
    shutil.copy(os.path.join(HERE, "qt_sut.rs"), os.path.join(sut, "src", "main.rs"))
    env = dict(os.environ, CARGO_TARGET_DIR=os.path.join(bdir, "target_mut"))
    rc, log = qrun.sh(["cargo", f"+{toolchain}", "build", "--offline"], cwd=sut, env=env)
    b = os.path.join(bdir, "target_mut", "debug", "qt_sut")
    return (b if rc == 0 and os.path.isfile(b) else None), log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--art", required=True)
    ap.add_argument("--build", default="/tmp/qt_mut")
    ap.add_argument("--toolchain", default="1.86.0")
    ap.add_argument("--diff", default=os.path.join(REPO, "results", "ptrtrans_quadtree_diff.json"))
    ap.add_argument("--out", default=os.path.join(REPO, "results", "ptrtrans_quadtree_mutation.json"))
    args = ap.parse_args()
    t0 = time.time()
    os.makedirs(args.build, exist_ok=True)
    diff = json.load(open(args.diff))
    clean = {r["id"] for r in diff["verdicts"] if not r["c_ub_evidence"]}
    cases = [c for c in corpus() if c["id"] in clean]
    # C reference outputs (Makefile flags)
    cbins, _ = qrun.build_c(args.build, args.art, only=("ref",))
    cref = {}
    for c in cases:
        o, e, rc, to = qrun.run_plain([cbins["ref"]], c["script"].encode())
        cref[c["id"]] = (o, rc)
    # baseline: repaired crate
    rep = qrun.build_rust(args.build, args.art, args.toolchain, repaired=True)
    base_crate = rep["crate_copy"]
    bbin, _ = build_sut(args.build, base_crate, args.toolchain, "base")
    bout, brc, bto = run_batch(bbin, cases)
    agree = [c for c in cases if c["id"] in bout and
             qrun.compare(cref[c["id"]][0], 0, bout[c["id"]].encode(), 0) is None and cref[c["id"]][1] == 0]
    base_disagree = sorted(c["id"] for c in cases if c not in agree)
    ms = mutants(base_crate)
    rows = []
    for k, m in enumerate(ms):
        mdir = os.path.join(args.build, "mutant_crate")
        apply(base_crate, mdir, m)
        mbin, log = build_sut(args.build, mdir, args.toolchain, "mutant")
        row = dict(m, id=k)
        if mbin is None:
            row["status"] = "build_fail"   # not a valid mutant (does not compile); excluded
            row["log"] = "\n".join(l for l in log.splitlines() if l.startswith("error"))[:300]
            rows.append(row)
            continue
        mout, rc, to = run_batch(mbin, agree)
        killed_by = []
        for c in agree:
            got = mout.get(c["id"])
            if got is None:
                killed_by.append((c["id"], "crash" if not to else "timeout"))
            elif qrun.compare(cref[c["id"]][0], 0, got.encode(), 0) is not None:
                killed_by.append((c["id"], "panic" if got.strip().endswith("PANIC") else "wrong_output"))
        row["status"] = "killed" if killed_by else "survived"
        row["batch_rc"], row["batch_timeout"] = rc, to
        row["n_killing_cases"] = len(killed_by)
        row["first_killing_cases"] = killed_by[:3]
        row["kill_kinds"] = dict(collections.Counter(k for _, k in killed_by))
        rows.append(row)
    valid = [r for r in rows if r["status"] != "build_fail"]
    summ = {
        "baseline": "shipped crate + one-line find_ repair (run.py repair_patch)",
        "cases_ub_clean": len(cases), "cases_baseline_agrees": len(agree),
        "cases_baseline_disagrees": len(base_disagree),
        "baseline_disagree_families": dict(collections.Counter(i.split("/")[-1].split("_")[0] if "keyfree" not in i
                                                               else "keyfree" for i in base_disagree)),
        "mutants_generated": len(rows), "mutants_build_fail": len(rows) - len(valid),
        "mutants_valid": len(valid), "killed": sum(1 for r in valid if r["status"] == "killed"),
        "survived": sum(1 for r in valid if r["status"] == "survived"),
        "by_kind": {k: {"valid": sum(1 for r in valid if r["kind"] == k),
                        "killed": sum(1 for r in valid if r["kind"] == k and r["status"] == "killed")}
                    for k in ("relational", "arithmetic", "constant")},
        "survivors": [{"file": r["file"], "line": r["line"], "from": r["from"], "to": r["to"], "orig": r["orig"].strip()}
                      for r in valid if r["status"] == "survived"],
    }
    summ["mutation_score"] = round(summ["killed"] / max(1, summ["mutants_valid"]), 4)
    out = {"provenance": ou.provenance(__file__, {"toolchain": args.toolchain, "wall_seconds": round(time.time() - t0),
                                                  "diff_input": os.path.relpath(args.diff, REPO)}),
           "summary": summ, "mutants": rows}
    ou.dump(args.out, out)
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()
