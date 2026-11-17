"""M7 stretch: power of Flourine's SCALAR harness (systems/flourine/run.py), measured the same way
as harness/mutate_full.py measures the coreutils harness.

  * Case set and comparison: exactly run.py's -- the 18 scalar benchmarks it selects, cases from
    enumerate_inputs.enumerate_cases(ptypes, cap=2000) (run.py's default), the C built by
    run.build_c (gcc -O1 -fwrapv), the Rust built by run.build_rust (rustc -O, panic caught per
    case), compared by batch_diff.tally with no sanitizer (as run.py calls it).
  * Mutated translations: every Flourine `success` translation that run.py builds and that AGREES
    with the C on that grid (tally value == 0 and rust_crash == 0, run.py's "success_agree").
    The step re-derives run.py's success counts and checks them against results/flourine_diff.json.
  * Operators and lexing: mutate_full.enumerate_mutants (rel / arith / const, identical rules),
    over every function in the translation file except `main` and test functions.
  * Site rule: for each agreeing translation and operator, the candidates sorted by
    (offset, replacement) and a seeded sample of at most K (random.Random(f"{SEED}:{tag}:{op}")).
  * Verdict (run.py's own classification): killed_value (tally value > 0, a wrong answer, S1),
    killed_crash (rust_panic > 0), hang (only rust_hang > 0), survived, noncompile (excluded).
    tce = the -O binary is byte-identical to the unmutated build (trivially equivalent).

  docker run --rm --network none --cpus 8 -v $WORK:/w -w /w/repo crust-stable \
      python3 harness/mutate_full_flourine.py --out results/flourine_mutation.json
"""
import argparse
import glob
import hashlib
import importlib.util
import json
import multiprocessing as mp
import os
import random
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "systems"))
import mutate_full as MF  # noqa: E402  (lexer / operators / wilson)

_spec = importlib.util.spec_from_file_location(
    "flourine_run", os.path.join(REPO, "systems", "flourine", "run.py"))
FR = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(FR)
from enumerate_inputs import enumerate_cases  # noqa: E402
from batch_diff import tally  # noqa: E402

SEED = MF.SEED + ":flourine"
FL = os.environ.get("FLOURINE_ROOT", FR.FL)
BMS = os.path.join(FL, "bms", "c")
SCRATCH = os.environ.get("MFULL_FL_SCRATCH", "/tmp/mfull_fl")


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def benchmarks(cap):
    """Mirror run.py main(): yield (name, cfile, sig, cases, [(verdict, rsfile, sig2)])."""
    for cfile in sorted(glob.glob(os.path.join(BMS, "*", "*", "*.c"))):
        name = os.path.basename(cfile)[:-2]
        proj = cfile.split("/bms/c/")[1].split("/")[0]
        anyrs = glob.glob(os.path.join(FL, "translations_only", "*", "c", proj, name, "*", "*",
                                       name + ".rs"))
        if not anyrs:
            continue
        sig = FR.parse_rust_fn(open(anyrs[0], errors="replace").read(), name)
        if not FR.ok_sig(sig):
            continue
        cases = enumerate_cases(sig["ptypes"], cap=cap)
        trs = []
        for rs in glob.glob(os.path.join(FL, "translations_only", "success", "c", proj, name,
                                         "*", "*", name + ".rs")):
            s2 = FR.parse_rust_fn(open(rs, errors="replace").read(), name)
            if FR.ok_sig(s2):
                trs.append((rs, s2))
        yield name, cfile, sig, cases, trs


# ------------------------------------------------------------------------------------ workers
_W = {}


def _init(q):
    _W["id"] = q.get()
    _W["dir"] = os.path.join(SCRATCH, f"w{_W['id']}")
    os.makedirs(_W["dir"], exist_ok=True)
    _W["cbin"] = {}


def _cbin(name, cfile, sig):
    if name not in _W["cbin"]:
        d = os.path.join(_W["dir"], "c_" + name)
        os.makedirs(d, exist_ok=True)
        b = FR.build_c(name, cfile, sig, d)
        _W["cbin"][name] = b
    return _W["cbin"][name]


def _build(name, src_text, sig):
    """Build a translation text with run.build_rust in a FIXED per-worker dir (so byte-identical
    binaries mean identical code). Returns path of a copy of the binary, or None."""
    d = os.path.join(_W["dir"], "r")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    rs = os.path.join(d, name + ".rs")
    open(rs, "w").write(src_text)
    b = FR.build_rust(name, rs, sig, d)
    return b


def _job(job):
    kind = job["kind"]
    name, cfile, sig = job["name"], job["cfile"], job["sig"]
    cases = enumerate_cases(sig["ptypes"], cap=job["cap"])
    binc = _cbin(name, cfile, sig)
    if binc is None:
        return dict(job, status="c_build_fail")
    src = open(job["rs"], errors="replace").read()
    t0 = time.time()
    if kind == "base":
        b = _build(name, src, job["sig2"])
        if not b:
            return dict(job, status="build_fail")
        t = tally(binc, b, cases)
        return dict(job, status="ok", tally={k: v for k, v in t.items()},
                    bin_sha=sha(b), seconds=round(time.time() - t0, 2))
    m = job["mutant"]
    b = _build(name, MF.apply_mutant(src, m), job["sig2"])
    if not b:
        return dict(job, status="noncompile", seconds=round(time.time() - t0, 2))
    tce = sha(b) == job["base_sha"]
    t = tally(binc, b, cases)
    if t["value"] > 0:
        st = "killed_value"
    elif t["rust_panic"] > 0:
        st = "killed_crash"
    elif t.get("rust_hang", 0) > 0:
        st = "hang"
    else:
        st = "survived"
    return dict(job, status=st, tce=tce,
                tally={k: t[k] for k in ("value", "rust_panic", "ub", "timeout", "agree")
                       } | {"rust_hang": t.get("rust_hang", 0)},
                value_samples=t["value_samples"][:1], seconds=round(time.time() - t0, 2))


def _pool(jobs, n):
    q = mp.Queue()
    for k in range(n):
        q.put(k)
    with mp.Pool(n, initializer=_init, initargs=(q,)) as p:
        return list(p.imap(_job, jobs, chunksize=4))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=int, default=2000, help="run.py default")
    ap.add_argument("-k", type=int, default=8, help="max mutants per (translation, operator)")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-agree", type=int, default=0, help="testing only: mutate first N")
    args = ap.parse_args()
    t_start = time.time()

    bms = list(benchmarks(args.cap))
    base_jobs = []
    for name, cfile, sig, cases, trs in bms:
        for rs, s2 in sorted(trs):
            base_jobs.append({"kind": "base", "name": name, "cfile": cfile, "sig": sig,
                              "sig2": s2, "rs": rs, "cap": args.cap,
                              "tag": rs.split("/translations_only/success/c/")[1]})
    base = _pool(base_jobs, args.jobs)
    built = [b for b in base if b["status"] == "ok"]
    agree = [b for b in built if b["tally"]["value"] == 0 and b["tally"]["rust_panic"] == 0]
    ref = json.load(open(os.path.join(REPO, "results", "flourine_diff.json")))["summary"]
    check = {"benchmarks_scalar": [len(bms), ref["benchmarks_scalar"]],
             "success_tested": [len(built), ref["success_tested"]],
             "success_agree": [len(agree), ref["success_agree"]],
             "success_value_bug": [sum(b["tally"]["value"] > 0 for b in built),
                                   ref["success_value_bug"]]}
    print("[fl] baseline vs results/flourine_diff.json:", json.dumps(check), flush=True)

    mjobs, cand_counts = [], {}
    for b in (agree[:args.max_agree] if args.max_agree else agree):
        src = open(b["rs"], errors="replace").read()
        muts, _spans = MF.enumerate_mutants(src)
        muts = [m for m in muts if m["fn"] != "main" and not m["fn"].startswith("test")]
        for op in ("rel", "arith", "const"):
            pool = sorted([m for m in muts if m["op"] == op],
                          key=lambda m: (m["offset"], m["new"]))
            cand_counts[op] = cand_counts.get(op, 0) + len(pool)
            rng = random.Random(f"{SEED}:{b['tag']}:{op}")
            pick = sorted(rng.sample(pool, min(args.k, len(pool))),
                          key=lambda m: (m["offset"], m["new"]))
            for j, m in enumerate(pick):
                mjobs.append({"kind": "mut", "name": b["name"], "cfile": b["cfile"],
                              "sig": b["sig"], "sig2": b["sig2"], "rs": b["rs"], "cap": args.cap,
                              "tag": b["tag"], "base_sha": b["bin_sha"],
                              "mutant": {k: m[k] for k in ("op", "subop", "offset", "old", "new",
                                                           "line", "col", "fn", "context")},
                              "id": f"{b['tag']}#{op}{j}"})
    print(f"[fl] {len(agree)} agreeing translations, {len(mjobs)} mutants", flush=True)
    res = _pool(mjobs, args.jobs)

    def rate(rows):
        n = len(rows)
        k = sum(r["status"] in ("killed_value", "killed_crash") for r in rows)
        kh = sum(r["status"] == "hang" for r in rows)
        return {"compiled": n, "killed_value": sum(r["status"] == "killed_value" for r in rows),
                "killed_crash": sum(r["status"] == "killed_crash" for r in rows),
                "killed_value_or_crash": k,
                "kill_rate": round(k / n, 4) if n else None, "ci95_wilson": MF.wilson(k, n),
                "hang_only": kh, "survived": sum(r["status"] == "survived" for r in rows),
                "survived_tce": sum(r["status"] == "survived" and r.get("tce") for r in rows)}

    comp = [r for r in res if r["status"] != "noncompile"]
    names = sorted({r["name"] for r in comp})
    per_tr = {}
    for r in comp:
        per_tr.setdefault(r["tag"], []).append(r)
    summary = {
        "baseline_check_vs_flourine_diff_json": check,
        "agreeing_translations": len(agree),
        "agreeing_translations_per_benchmark": {n: sum(1 for b in agree if b["name"] == n)
                                                for n in sorted({b["name"] for b in agree})},
        "candidates_in_agreeing_translations": cand_counts,
        "mutants": len(res), "noncompile": len(res) - len(comp),
        "overall": rate(comp),
        "per_operator": {op: rate([r for r in comp if r["mutant"]["op"] == op])
                         for op in ("rel", "arith", "const")},
        "per_suboperator": {s: rate([r for r in comp if r["mutant"]["subop"] == s])
                            for s in sorted({r["mutant"]["subop"] for r in comp})},
        "per_benchmark": {n: rate([r for r in comp if r["name"] == n]) for n in names},
        "translations_with_all_compiled_mutants_killed": sum(
            1 for rows in per_tr.values() if all(r["status"] in ("killed_value", "killed_crash")
                                                 for r in rows)),
        "translations_with_a_compiled_mutant": len(per_tr),
        "seconds": round(time.time() - t_start, 1),
    }
    sh = lambda c: subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()
    import platform
    prov = {"script": "harness/mutate_full_flourine.py (driver: harness/mutate_full_run.sh flourine)",
            "command": " ".join(["python3"] + sys.argv),
            "git_rev": os.environ.get("GIT_REV", "unknown"),
            "git_note": "study repo HEAD on the Mac; mutate_full* scripts are new, uncommitted",
            "host": os.environ.get("MFULL_HOST", platform.system()), "arch": platform.machine(),
            "image": os.environ.get("MFULL_IMAGE"), "image_id": os.environ.get("MFULL_IMAGE_ID"),
            "toolchains": {"rustc": sh("rustc --version"), "gcc": sh("gcc --version | head -1"),
                           "python": sys.version.split()[0]},
            "flourine_artifact": "artifacts/flourine_dl/flourine-artifact (bms/c + "
                                 "translations_only: 7958 files, sha256-of-sha256s 09224bde091cebc4 "
                                 "on both the Mac and the x86 box)",
            "seed": SEED, "k_per_translation_per_op": args.k, "cap": args.cap,
            "date": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    out = {"provenance": prov, "summary": summary,
           "baseline": [{k: b.get(k) for k in ("tag", "name", "status", "tally", "bin_sha")}
                        for b in base],
           "mutants": [{k: r.get(k) for k in ("id", "tag", "name", "mutant", "status", "tce",
                                              "tally", "value_samples", "seconds")}
                       for r in res]}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    json.dump(out, open(args.out, "w"), indent=1)
    print(json.dumps({k: summary[k] for k in ("overall", "per_operator", "noncompile")}, indent=1))


if __name__ == "__main__":
    main()
