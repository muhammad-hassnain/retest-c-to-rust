"""His2Trans re-grade: function-level differential test of the released OSS8 crates against the
original C, with the study's UB rule (harness/oracle_ub_common.py).

For each project P (a "translation" = one released crate):
  C side   drivers/P_driver.c + the original C (data/rq2/deepseek/c_source/c_source/P/src), built as
           ref (gcc -O2), o0, o1 (gcc -O0/-O1), san_clang (clang-14 ASan+UBSan+float-cast-overflow),
           san_gcc (gcc-11, same set), uchar/schar (gcc -O2 -funsigned-char / -fsigned-char).
  Rust     a scratch copy of the released crate
           (data/paper_artifacts/deepseek_v4_pro_oss8_0613_rq2_100pct_minimal/projects/P) with
           src/regrade_driver.rs = drivers/common.rs + drivers/P_driver.rs and `fn main` replaced by a
           call into it; nothing else in the crate is changed (ht: one pub wrapper line appended to
           src_ht.rs, because the only computation, hash_key, is file-local in C and private in Rust).
           cargo build --release --frozen (the crate's own release profile), offline, vendored deps
           checked against the crate's Cargo.lock.
Every case (gen_inputs.py) is run on all eight binaries. Channels compared: stdout (doubles with the
VERT relative tolerance), exit code, stderr. A divergence is counted only if the C passes the UB rule
on that case (no sanitizer report under clang or gcc, -O0/-O1 identical to -O2, char signedness
irrelevant); otherwise it is original-C UB. Audit 8 uniform rule: a sanitizer run that fails to start,
times out, or prints another stdout / exit code than the ref also leaves the case not counted.

Run inside the crust-heldout-plus186 image (gcc 11.4, clang 14, rustc 1.86.0), see run_in_docker.sh.
"""
import argparse
import collections
import concurrent.futures as cf
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "harness"))
sys.path.insert(0, HERE)
import oracle_ub_common as ou  # noqa: E402
import gen_inputs  # noqa: E402

ART = os.environ.get("H2T_ART", os.path.join(REPO, "artifacts", "his2trans"))
OSS8 = "data/paper_artifacts/deepseek_v4_pro_oss8_0613_rq2_100pct_minimal"
CSRC = "data/rq2/deepseek/c_source/c_source"
DRV = os.path.join(HERE, "drivers")

C_FILES = {
    "qsort": ["qsort.c"], "ht": [], "rgba": ["rgba.c"], "genann": ["genann.c"], "buffer": ["buffer.c"],
    "quadtree": ["quadtree.c", "node.c", "bounds.c", "point.c"], "urlparser": [], "avl": [],
}

VARIANTS = {
    "ref": ("gcc", ["-O2"], []),
    "o0": ("gcc", ["-O0"], []),
    "o1": ("gcc", ["-O1"], []),
    "san_clang": ("clang", ou.san_cflags("clang"), ou.san_ldflags("clang")),
    "san_gcc": ("gcc", ou.san_cflags("gcc"), ou.san_ldflags("gcc")),
    "uchar": ("gcc", ["-O2", "-funsigned-char"], []),
    "schar": ("gcc", ["-O2", "-fsigned-char"], []),
}
SAN = ("san_clang", "san_gcc")
ALT = ("o0", "o1", "uchar", "schar")


def sh(argv, cwd=None, env=None, timeout=1800):
    p = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=timeout)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def build_c(project, bdir, variants=VARIANTS):
    src = os.path.join(ART, CSRC, project, "src")
    out = {}
    cdir = os.path.join(bdir, "c", project)
    os.makedirs(cdir, exist_ok=True)
    for name, (cc, cflags, ldflags) in variants.items():
        b = os.path.join(cdir, f"{project}_{name}")
        argv = ([cc, "-w"] + cflags + ["-I", src, "-I", DRV, os.path.join(DRV, f"{project}_driver.c")]
                + [os.path.join(src, f) for f in C_FILES[project]] + ["-o", b] + ldflags + ["-lm"])
        rc, log = sh(argv)
        if rc != 0:
            raise SystemExit(f"C build {project}/{name} failed:\n{' '.join(argv)}\n{log[-3000:]}")
        out[name] = b
    return out


def prepare_crate(project, dest, mutate=None):
    """Scratch copy of the released crate + our driver module. `mutate` = (relpath, old, new) applies
    one textual fault (harness-power check); None for the real run."""
    src = os.path.join(ART, OSS8, "projects", project)
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("target"))
    with open(os.path.join(DRV, "common.rs")) as f:
        common = f.read()
    with open(os.path.join(DRV, f"{project}_driver.rs")) as f:
        drv = f.read()
    with open(os.path.join(dest, "src", "regrade_driver.rs"), "w") as f:
        f.write(common + "\n// ---- project driver ----\n" + drv)
    mp = os.path.join(dest, "src", "main.rs")
    with open(mp) as f:
        m = f.read()
    i = m.index("fn main()")
    m = m[:i] + "pub mod regrade_driver;\nfn main() {\n    regrade_driver::main();\n}\n"
    with open(mp, "w") as f:
        f.write(m)
    if project == "ht":
        with open(os.path.join(dest, "src", "src_ht.rs"), "a") as f:
            f.write("\n// regrade: expose the file-local hash_key to the differential driver\n"
                    "pub fn regrade_hash_key(k: &core::ffi::CStr) -> u64 { hash_key(k) }\n")
    edits = mutate if isinstance(mutate, list) else ([mutate] if mutate else [])
    for mutate in edits:
        rel, old, new = mutate[:3]
        p = os.path.join(dest, rel)
        with open(p) as f:
            s = f.read()
        if len(mutate) > 3:                      # exact offset (mutation analysis)
            off = mutate[3]
            if s[off:off + len(old)] != old:
                raise ValueError(f"mutation anchor mismatch in {rel} at {off}: {old!r}")
            s = s[:off] + new + s[off + len(old):]
        else:
            if s.count(old) != 1:
                raise ValueError(f"mutation anchor not unique in {rel}: {old!r} ({s.count(old)})")
            s = s.replace(old, new, 1)
        with open(p, "w") as f:
            f.write(s)


def build_rust(project, bdir, mutate=None, tag="sut"):
    dest = os.path.join(bdir, "rust", f"{project}_{tag}")
    prepare_crate(project, dest, mutate)
    rc, log = sh(["cargo", "build", "--release", "--frozen", "-j", "3"], cwd=dest)
    b = os.path.join(dest, "target", "release", project)
    if rc != 0 or not os.path.isfile(b):
        return None, log[-4000:]
    return b, log[-2000:]


# ------------------------------------------------------------------ comparison
TOK_D = re.compile(rb"^d:([0-9a-f]{16})$")


def _dbl(h):
    import struct
    return struct.unpack(">d", bytes.fromhex(h.decode()))[0]


def vals_equal(a, b):
    """The VERT float rule (systems/vert/run_arrays.py): NaN==NaN, else |a-b| <= 1e-6*max(1,|a|)."""
    if math.isnan(a) and math.isnan(b):
        return True
    if math.isinf(a) or math.isinf(b):
        return a == b
    return abs(a - b) <= 1e-6 * max(1.0, abs(a))


def lines_equal(x, y):
    if x == y:
        return True
    tx, ty = x.split(b" "), y.split(b" ")
    if len(tx) != len(ty):
        return False
    for a, b in zip(tx, ty):
        if a == b:
            continue
        ma, mb = TOK_D.match(a), TOK_D.match(b)
        if not (ma and mb and vals_equal(_dbl(ma.group(1)), _dbl(mb.group(1)))):
            return False
    return True


def first_diff(o1, o2):
    """Index of the first differing stdout line (tolerant), or None if equal."""
    l1, l2 = o1.split(b"\n"), o2.split(b"\n")
    for i in range(max(len(l1), len(l2))):
        a = l1[i] if i < len(l1) else None
        b = l2[i] if i < len(l2) else None
        if a is None or b is None or not lines_equal(a, b):
            return i
    return None


def norm_err(e, paths):
    for p in paths:
        e = e.replace(p.encode(), b"<prog>")
    return e


def run_bin(b, data, san):
    if san:
        r = ou.run_san([b], data, timeout=60)
        return r
    o, e, rc, to = ou.run_proc([b], data, timeout=20)
    return {"stdout": o, "stderr": e, "rc": rc, "timed_out": to, "report": None, "reports": [], "infra": False}


def excerpt(out, i, n=400):
    ls = out.split(b"\n")
    return ls[i].decode("latin1")[:n] if i is not None and i < len(ls) else None


def run_case(c, bins, sut):
    data = ("\n".join(c["lines"]) + "\n").encode()
    res = {k: run_bin(b, data, k in SAN) for k, b in bins.items()}
    res["rust"] = run_bin(sut, data, False)
    ref, rs = res["ref"], res["rust"]
    paths = list(bins.values()) + [sut]
    tags = []
    fd = first_diff(ref["stdout"], rs["stdout"])
    if fd is not None:
        tags.append("stdout")
    if ref["rc"] != rs["rc"]:
        tags.append(f"exit({ref['rc']}vs{rs['rc']})")
    if norm_err(ref["stderr"], paths) != norm_err(rs["stderr"], paths):
        tags.append("stderr")
    panic = rs["rc"] == 101 and b"panicked" in rs["stderr"]
    rcrash = rs["rc"] < 0 and not rs["timed_out"]
    ub = []
    for s in SAN:
        if res[s]["report"]:
            ub.append(f"{s}: {res[s]['report']}")
        elif res[s]["infra"]:
            ub.append(f"{s}: infra (start-up crash, retried)")
        elif res[s]["timed_out"]:                       # audit 8 uniform rule: not UB-clean
            ub.append(f"{s}: timed out (not UB-clean)")
        elif first_diff(res[s]["stdout"], ref["stdout"]) is not None or res[s]["rc"] != ref["rc"]:
            ub.append(f"{s}: output differs from gcc -O2 without a sanitizer report (not UB-clean)")
    for a in ALT:
        r = res[a]
        if first_diff(r["stdout"], ref["stdout"]) is not None or r["rc"] != ref["rc"]:
            ub.append(f"{a}: output differs from gcc -O2")
    if ref["rc"] < 0 or ref["timed_out"]:
        ub.append(f"ref: C crashed/timed out (rc={ref['rc']}, timeout={ref['timed_out']})")
    div = bool(tags) or rs["timed_out"]
    if not div:
        verdict = "agree"
    elif ub:
        verdict = "original_C_UB"
    else:
        verdict = "divergent"
    sev = None
    if verdict == "divergent":
        if panic or rcrash:
            sev = "crash"
        elif rs["timed_out"]:
            sev = "S3"
        elif "stdout" in tags or any(t.startswith("exit") for t in tags):
            sev = "S1"
        else:
            sev = "S2"
    first_op = None
    if fd is not None and fd < len(c["lines"]):
        first_op = c["lines"][fd].split()[0]
    elif fd is not None:
        first_op = "<after last call>"
    row = {"id": c["id"], "project": c["project"], "func": c["func"], "cls": c["cls"], "verdict": verdict,
           "severity": sev, "tags": tags, "first_diff_line": fd, "first_diff_op": first_op,
           "c_rc": ref["rc"], "rust_rc": rs["rc"], "rust_panic": panic, "rust_crash": rcrash,
           "rust_timeout": rs["timed_out"], "c_ub_evidence": ub}
    if div:
        row["lines"] = [ln if len(ln) <= 600 else ln[:600] + "...(truncated)" for ln in c["lines"]]
        row["c_line"] = excerpt(ref["stdout"], fd)
        row["rust_line"] = excerpt(rs["stdout"], fd)
        row["c_stderr"] = ref["stderr"].decode("latin1")[-400:]
        row["rust_stderr"] = rs["stderr"].decode("latin1")[-400:]
    return row


def run_project(project, bdir, jobs, cases=None, sut=None, bins=None):
    bins = bins or build_c(project, bdir)
    if sut is None:
        sut, log = build_rust(project, bdir)
        if sut is None:
            return {"project": project, "rust_build": "FAILED", "log": log}, []
    cases = cases if cases is not None else gen_inputs.GENERATORS[project]()
    with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
        rows = list(ex.map(lambda c: run_case(c, bins, sut), cases))
    return {"project": project, "rust_build": "ok"}, rows


def summarize(rows):
    s = collections.Counter(r["verdict"] for r in rows)
    out = {"cases": len(rows), "agree": s["agree"], "divergent_counted": s["divergent"],
           "original_C_UB": s["original_C_UB"],
           "c_ub_any_case": sum(1 for r in rows if r["c_ub_evidence"]),
           "by_severity": dict(collections.Counter(r["severity"] for r in rows if r["severity"])),
           "by_cls": {}, "clusters": {}}
    for cls in sorted({r["cls"] for r in rows}):
        rr = [r for r in rows if r["cls"] == cls]
        out["by_cls"][cls] = dict(collections.Counter(r["verdict"] for r in rr))
    cl = collections.defaultdict(lambda: collections.Counter())
    for r in rows:
        if r["verdict"] in ("divergent", "original_C_UB"):
            cl[f"{r['first_diff_op']}"][r["verdict"] + ("/" + r["severity"] if r["severity"] else "")] += 1
    out["clusters"] = {k: dict(v) for k, v in sorted(cl.items())}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--projects", default="qsort,ht,rgba,genann,buffer,quadtree,urlparser")
    ap.add_argument("--build", default="/b/regrade")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--out", default=os.path.join(REPO, "results", "his2trans_diff.json"))
    args = ap.parse_args()
    t0 = time.time()
    projects = [p for p in args.projects.split(",") if p]
    per, allrows = {}, []
    for p in projects:
        tp = time.time()
        meta, rows = run_project(p, args.build, args.jobs)
        meta["summary"] = summarize(rows) if rows else None
        meta["wall_seconds"] = round(time.time() - tp)
        per[p] = meta
        allrows.extend(rows)
        print(p, json.dumps(meta["summary"], indent=None)[:1500], flush=True)
    out = {
        "provenance": ou.provenance(__file__, {
            "artifact": {"repo": "https://github.com/SYSUSELab/His2Trans", "commit": os.environ.get("H2T_COMMIT", "?"),
                         "crates": OSS8 + "/projects/<p>", "c_source": CSRC + "/<p>/src",
                         "source_digests": {p: {"crate": ou.tree_digest(os.path.join(ART, OSS8, "projects", p)),
                                                "c": ou.tree_digest(os.path.join(ART, CSRC, p, "src"))}
                                            for p in projects}},
            "driver_digests": {f: ou.tree_digest(os.path.join(DRV, f)) for f in sorted(os.listdir(DRV))},
            "generator_digest": ou.tree_digest(os.path.join(HERE, "gen_inputs.py")),
            "c_builds": {k: {"cc": v[0], "flags": v[1] + v[2] + ["-lm"]} for k, v in VARIANTS.items()},
            "rust_build": "cargo build --release --frozen (crate's own release profile: opt-level 3, lto); "
                          "rustc/cargo as in toolchains; deps vendored from the local cargo cache, sha256 = Cargo.lock checksums",
            "float_rule": "NaN==NaN, else |a-b| <= 1e-6*max(1,|a|) (systems/vert/run_arrays.py vals_equal)",
            "wall_seconds": round(time.time() - t0),
            "sanitizer_positive_controls": ou.positive_controls(os.path.join(args.build, "controls")),
        }),
        "per_project": per,
        "verdicts": allrows,
    }
    ou.dump(args.out, out)


if __name__ == "__main__":
    main()
