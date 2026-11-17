#!/usr/bin/env python3
"""SACTOR CRUST-Bench re-grade: SACTOR's Rust for the CRUST-Bench samples in its fork
(github.com/qsdrqs/CRUST-bench @ 69970f9c, datasets/CRUST_bench/CBench/<sample>/result/) against
the original C, with the oracle-UB rule of harness/oracle_ub_common.py.

Run inside the container (systems/sactor/run_docker.sh):
    python3 systems/sactor/crust/run.py --out /work/results/sactor_crust_owntests.json

Per sample:
  * inventory: which translations ship (combined.rs per style, item counts, SACTOR's own
    {unidiomatic,idiomatic}_failure_info.json verdicts);
  * C oracle: the sample's own build_lib_and_tests.sh (lib.a + test<k>.o, exactly what SACTOR's
    translate.py links against), built under every oracle-UB variant through a gcc shim
    (ccwrap.sh): ref, o0, o1, uchar, schar, san_gcc, san_clang, san_clang_nz;
  * Rust: every unidiomatic combined.rs, built as SACTOR's E2E check builds it (build_attempt
    cdylib, every C function exposed #[no_mangle] extern "C", cargo build = debug profile), also
    --release and with the second bracketing nightly; linked with the same test<k>.o objects;
  * cases: the sample's own test commands (test_cmd.json; '%t' or 'sactor run-tests ...
    test_samples.json %t N --feed-as-args', replayed exactly), run on the C and on the Rust;
    stdout, stderr and exit code compared byte for byte (every binary is named run_test and run
    as ./run_test from a fresh copy of the sample directory, so program names never differ).
  * acceptance (SACTOR's own check re-run): '%t' -> exit 0; run-tests -> normalised
    stdout+stderr equals the shipped expected output.
"""
import argparse
import concurrent.futures as cf
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import sactor_common as sc  # noqa: E402
ou = sc.ou

CB = os.path.join(sc.ART, "CRUST-bench", "datasets", "CRUST_bench", "CBench")
C_VARIANTS = ["ref", "o0", "o1", "uchar", "schar", "san_gcc", "san_clang", "san_clang_nz"]
UB_CHECKS = ["o0", "o1", "uchar", "schar", "san_gcc", "san_clang"]
SKIP_COPY = {"result", ".git"}


def samples():
    return sorted(d for d in os.listdir(CB) if os.path.isdir(os.path.join(CB, d, "result"))
                  or os.path.isfile(os.path.join(CB, d, "build_lib_and_tests.sh")))


def copy_sample(s, dst):
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(os.path.join(CB, s), dst, ignore=lambda d, n: [x for x in n if x in SKIP_COPY] if d == os.path.join(CB, s) else [],
                    symlinks=True)
    for dp, dns, fns in os.walk(dst):
        os.chmod(dp, 0o755)
        for f in fns:
            p = os.path.join(dp, f)
            if not os.path.islink(p):
                os.chmod(p, 0o755 if os.access(p, os.X_OK) or f.endswith(".sh") else 0o644)


def test_objects(s):
    t = open(os.path.join(CB, s, "translate.py")).read()
    m = re.search(r"executable_object\s*=\s*(\[[^\]]*\]|'[^']*'|\"[^\"]*\")", t)
    if not m:
        return ["test1.o"]
    return re.findall(r"['\"]([^'\"]+\.o)['\"]", m.group(1))


def link_libs(s):
    t = open(os.path.join(CB, s, "build_run_tests.sh")).read()
    libs = []
    for x in re.findall(r"(?<!\S)-l[A-Za-z0-9_]+", t):
        if x not in libs:
            libs.append(x)
    return libs


def c_function_defs(lib_c, cwd):
    """Names of all functions defined (with a body) in lib.c itself, static or not (clang AST)."""
    p = subprocess.run(["clang", "-Xclang", "-ast-dump=json", "-fsyntax-only", "-w", lib_c], cwd=cwd,
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        ast = json.loads(p.stdout)
    except ValueError:
        return set()
    cur, out = None, set()
    for n in ast.get("inner", []):
        loc = n.get("loc", {})
        if loc.get("file"):
            cur = loc["file"]
        if "includedFrom" in loc or not (cur or "").endswith("lib.c"):
            continue
        if n.get("kind") == "FunctionDecl" and any(c.get("kind") == "CompoundStmt" for c in n.get("inner", [])):
            out.add(n["name"])
    return out


def inventory(s):
    inv = {}
    for style in ("unidiomatic", "idiomatic"):
        tdir = os.path.join(CB, s, "result", f"translated_code_{style}")
        d = {"combined_rs": os.path.isfile(os.path.join(tdir, "combined.rs")),
             "functions": len(glob.glob(os.path.join(tdir, "functions", "*.rs")))}
        fi = os.path.join(CB, s, "result", f"{style}_failure_info.json")
        if os.path.isfile(fi):
            j = json.load(open(fi))
            fn = {k: v for k, v in j.items() if v.get("type") == "function"}
            d["failure_info_functions"] = len(fn)
            d["failure_info_success"] = sum(1 for v in fn.values() if v.get("status") == "success")
            d["failure_info_failed"] = sorted(k for k, v in fn.items() if v.get("status") != "success")
        inv[style] = d
    inv["test_harness_functions"] = len(glob.glob(os.path.join(CB, s, "result", "test_harness", "functions", "*.rs")))
    return inv


def build_c(s, bdir, variant):
    d = os.path.join(bdir, f"c_{variant}")
    copy_sample(s, d)
    env = dict(os.environ)
    env["PATH"] = os.path.join(bdir, "..", "wrap") + ":" + env["PATH"]
    env["CRUST_VARIANT"] = variant
    rc, log = sc.sh(["bash", "build_lib_and_tests.sh"], cwd=d, env=env, timeout=900)
    bins, fails = {}, {}
    if not os.path.isfile(os.path.join(d, "lib.a")):
        return d, bins, {"build_lib_and_tests.sh": log[-2000:]}
    for t in test_objects(s):
        if not os.path.isfile(os.path.join(d, t)):
            fails[t] = "missing after build: " + log[-800:]
            continue
        exe_dir = os.path.join(bdir, "bin", variant, t[:-2])
        os.makedirs(exe_dir, exist_ok=True)
        exe = os.path.join(exe_dir, "run_test")
        rc2, log2 = sc.sh(["gcc", "-o", exe, os.path.join(d, t), os.path.join(d, "lib.a")] + link_libs(s) + ["-lm"],
                          env=env)
        if rc2 != 0:
            fails[t] = log2[-1500:]
        else:
            bins[t] = exe
    return d, bins, fails


def build_rust(s, bdir, cdir_ref, style, tc, prof):
    tdir = os.path.join(CB, s, "result", f"translated_code_{style}")
    if os.path.isfile(os.path.join(tdir, "combined.rs")):
        code = open(os.path.join(tdir, "combined.rs")).read()
    else:   # complete per-function set without combined.rs: combine as SACTOR's Combiner does
        code = sc.assemble(tdir)
    names = {os.path.basename(f)[:-3] for f in glob.glob(os.path.join(tdir, "functions", "*.rs"))}
    code = sc.expose_to_c(code, names)
    crate = os.path.join(bdir, "crates", f"{style}_{tc}_{prof}")
    ok, log, so = sc.build_crate(code, crate, tc, prof)
    info = {"ok": ok}
    bins = {}
    if not ok:
        errs = [ln for ln in log.splitlines() if ln.startswith("error")]
        info.update({"n_errors": len(errs), "errors": errs[:10], "log_tail": log[-2000:]})
        return info, bins
    libdir = os.path.dirname(so)
    for t in test_objects(s):
        exe_dir = os.path.join(bdir, "bin", f"rust_{style}_{tc}_{prof}", t[:-2])
        os.makedirs(exe_dir, exist_ok=True)
        exe = os.path.join(exe_dir, "run_test")
        rc, lg = sc.sh(["gcc", "-o", exe, os.path.join(cdir_ref, t), "-L" + libdir, "-lbuild_attempt", "-lm",
                        "-Wl,-rpath," + libdir])
        if rc != 0:
            info.setdefault("link_fail", {})[t] = lg[-1500:]
        else:
            bins[t] = exe
    return info, bins


def own_cases(s):
    """SACTOR's own test commands for the sample, as (case_id, kind, argv_tail, expected)."""
    cmds = json.load(open(os.path.join(CB, s, "test_cmd.json")))
    out = []
    for i, c in enumerate(cmds):
        cmd = c["command"] if isinstance(c["command"], str) else " ".join(c["command"])
        toks = cmd.split()
        if toks == ["%t"]:
            out.append({"id": f"own{i}", "kind": "exit0", "args": [], "expected": None})
        elif toks[:2] == ["sactor", "run-tests"]:
            samples_path = toks[toks.index("--type") + 2] if "--type" in toks else toks[2]
            idx = int(toks[toks.index("%t") + 1])
            ts = json.load(open(os.path.join(CB, s, samples_path)))
            out.append({"id": f"own{i}", "kind": "run-tests", "args": ts[idx]["input"].split(),
                        "expected": ts[idx]["output"], "sample_index": idx, "n_samples_in_file": len(ts)})
        else:
            out.append({"id": f"own{i}", "kind": "unknown", "cmd": cmd})
    return out


SHIM = "/build/crust/detshim.so"
LIBASAN = None


def shim_env(san_gcc=False):
    """LD_PRELOAD for the determinism shim (detshim.c). gcc's ASan runtime is a shared library that
    must come first in the preload list, so it is preloaded explicitly ahead of the shim."""
    global LIBASAN
    if san_gcc:
        if LIBASAN is None:
            LIBASAN = subprocess.run(["gcc", "-print-file-name=libasan.so"], stdout=subprocess.PIPE, text=True).stdout.strip()
        return {"LD_PRELOAD": LIBASAN + ":" + SHIM}
    return {"LD_PRELOAD": SHIM}


def run_bin(s, exe, args, san=None, nz=False, shim=False, variant=""):
    with tempfile.TemporaryDirectory(dir="/build/tmp") as td:
        wd = os.path.join(td, "w")
        copy_sample(s, wd)
        shutil.copy(exe, os.path.join(wd, "run_test"))
        argv = ["./run_test"] + args
        if san:
            e = dict(os.environ)
            if shim:
                e.update(shim_env(san_gcc=(variant == "san_gcc")))
            r = ou.run_san(argv, b"", cwd=wd, env=e, timeout=120, halt=not nz)
            return {"stdout": r["stdout"], "stderr": r["stderr"], "rc": r["rc"], "to": r["timed_out"],
                    "report": r["report"], "reports": r["reports"], "infra": r["infra"]}
        e = dict(os.environ)
        if shim:
            e.update(shim_env())
        e["LC_ALL"] = "C"
        e["LANG"] = "C"
        o, er, rc, to = ou.run_proc(argv, b"", cwd=wd, env=e, timeout=120)
        return {"stdout": o, "stderr": er, "rc": rc, "to": to, "report": None, "reports": [], "infra": False}


def norm(b):
    return "\n".join(x.strip() for x in b.decode("utf-8", "replace").splitlines())


def first_diff(a, b):
    al, bl = a.split(b"\n"), b.split(b"\n")
    for i in range(max(len(al), len(bl))):
        x = al[i] if i < len(al) else b"<EOF>"
        y = bl[i] if i < len(bl) else b"<EOF>"
        if x != y:
            return {"line": i + 1, "c": x.decode("utf-8", "replace")[:300], "rust": y.decode("utf-8", "replace")[:300]}
    return None


def do_sample(s, args):
    t0 = time.time()
    bdir = os.path.join(args.build, s)
    os.makedirs(bdir, exist_ok=True)
    res = {"sample": s, "inventory": inventory(s), "test_objects": test_objects(s), "link_libs": link_libs(s)}
    cdirs, cbins, cfails = {}, {}, {}
    for v in C_VARIANTS:
        cdirs[v], cbins[v], f = build_c(s, bdir, v)
        if f:
            cfails[v] = f
    res["c_build_failures"] = cfails
    res["c_builds"] = {v: sorted(cbins[v]) for v in C_VARIANTS}
    rbins = {}
    res["rust_builds"] = {}
    cdefs = c_function_defs(os.path.join(cdirs["ref"], "lib.c"), cdirs["ref"])
    res["c_function_definitions"] = len(cdefs)
    for style in ("unidiomatic", "idiomatic"):
        inv = res["inventory"][style]
        tdir_ = os.path.join(CB, s, "result", f"translated_code_{style}", "functions")
        have = {f[:-3] for f in os.listdir(tdir_)} if os.path.isdir(tdir_) else set()
        inv["c_functions_without_rust"] = sorted(cdefs - have)
        # complete = a Rust file for every function DEFINED in lib.c (SACTOR's failure_info lists
        # only the functions a run reached, so it is not a completeness record)
        complete = style == "unidiomatic" and bool(cdefs) and not (cdefs - have)
        inv["rust_source"] = "combined.rs" if inv["combined_rs"] else ("assembled from the complete per-function set" if complete else None)
        if not inv["combined_rs"] and not complete:
            continue
        for tc in sc.TOOLCHAINS:
            for prof in ("debug", "release"):
                key = f"{style}:{tc}:{prof}"
                if style == "idiomatic":
                    # idiomatic: Rust-native API; SACTOR skips E2E for library code. Build check only.
                    tdir = os.path.join(CB, s, "result", "translated_code_idiomatic")
                    ok, log, so = sc.build_crate(open(os.path.join(tdir, "combined.rs")).read(),
                                                 os.path.join(bdir, "crates", f"idiomatic_{tc}_{prof}"), tc, prof)
                    errs = [ln for ln in log.splitlines() if ln.startswith("error")]
                    res["rust_builds"][key] = {"ok": ok, **({} if ok else {"errors": errs[:10], "log_tail": log[-1500:]})}
                    continue
                info, b = build_rust(s, bdir, cdirs["ref"], style, tc, prof)
                res["rust_builds"][key] = info
                for t, exe in b.items():
                    rbins.setdefault(key, {})[t] = exe
    cases = own_cases(s)
    rows = []
    for c in cases:
        if c["kind"] == "unknown":
            rows.append({"case": c, "skipped": "unknown test command"})
            continue
        for t in test_objects(s):
            if t not in cbins["ref"]:
                rows.append({"case": c["id"], "obj": t, "skipped": "C ref did not build"})
                continue
            ref = run_bin(s, cbins["ref"][t], c["args"])
            ref2 = run_bin(s, cbins["ref"][t], c["args"])
            det_plain = (ref["stdout"], ref["stderr"], ref["rc"]) == (ref2["stdout"], ref2["stderr"], ref2["rc"])
            shim = not det_plain
            if shim:   # pin clock and pid (detshim.c) for this case, for the C and every Rust build
                ref = run_bin(s, cbins["ref"][t], c["args"], shim=True)
                ref2 = run_bin(s, cbins["ref"][t], c["args"], shim=True)
            row = {"case": c["id"], "kind": c["kind"], "obj": t, "args": c["args"],
                   "c_exit": ref["rc"], "c_timed_out": ref["to"],
                   "c_deterministic_plain": det_plain, "determinism_shim": shim,
                   "c_deterministic": (ref["stdout"], ref["stderr"], ref["rc"]) == (ref2["stdout"], ref2["stderr"], ref2["rc"]),
                   "c_stdout_tail": ref["stdout"].decode("utf-8", "replace")[-300:],
                   "c_stderr_tail": ref["stderr"].decode("utf-8", "replace")[-300:]}
            if c["kind"] == "run-tests":
                row["c_matches_expected"] = norm(ref["stdout"] + ref["stderr"]) == c["expected"]
            ub, san_nr = [], []
            nzr = None
            for v in UB_CHECKS + ["san_clang_nz"]:
                if t not in cbins[v]:
                    ub.append(f"{v}: did not build")
                    continue
                r = run_bin(s, cbins[v][t], c["args"], san=v.startswith("san_"), nz=v.endswith("_nz"), shim=shim, variant=v)
                if v == "san_clang_nz":
                    nzr = r
                    continue
                if v.startswith("san_"):
                    if r["report"]:
                        ub.append(f"{v}: {r['report']}")
                    elif r["infra"]:
                        ub.append(f"{v}: infra")
                    elif (r["stdout"], r["rc"]) != (ref["stdout"], ref["rc"]):
                        san_nr.append(v)   # audit 8 uniform rule: disagreement (or timeout) -> not UB-clean
                        ub.append(f"{v}: output differs from ref without a sanitizer report (not UB-clean)")
                elif (r["stdout"], r["rc"], r["stderr"]) != (ref["stdout"], ref["rc"], ref["stderr"]):
                    ub.append(f"{v}: output differs from ref")
            row["c_ub"] = ub
            row["san_output_differs_without_report"] = san_nr
            row["c_ub_excluding_null_plus_zero"] = ([u for u in ub if not u.startswith("san_clang:")]
                                                   + ([f"san_clang_nz: {x}" for x in ou.other_than_null_zero(nzr["reports"])] if nzr else []))
            row["rust"] = {}
            for key, bins in rbins.items():
                if t not in bins:
                    row["rust"][key] = {"missing": True}
                    continue
                r = run_bin(s, bins[t], c["args"], shim=shim)
                own = (r["rc"] == 0 and not r["to"]) if c["kind"] == "exit0" else (norm(r["stdout"] + r["stderr"]) == c["expected"])
                same = (r["stdout"], r["stderr"], r["rc"]) == (ref["stdout"], ref["stderr"], ref["rc"])
                d = {"agree": same, "sactor_check_pass": own, "exit": r["rc"], "timed_out": r["to"]}
                if not same:
                    d.update({"stdout_differs": r["stdout"] != ref["stdout"], "stderr_differs": r["stderr"] != ref["stderr"],
                              "first_diff_stdout": first_diff(ref["stdout"], r["stdout"]),
                              "first_diff_stderr": first_diff(ref["stderr"], r["stderr"]),
                              "rust_stderr_tail": r["stderr"].decode("utf-8", "replace")[-500:]})
                row["rust"][key] = d
            rows.append(row)
    res["cases"] = rows
    res["elapsed_s"] = round(time.time() - t0, 1)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="/build/crust")
    ap.add_argument("--out", default="/work/results/sactor_crust_owntests.json")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--only", default=None)
    args = ap.parse_args()
    os.makedirs(os.path.join(args.build, "wrap"), exist_ok=True)
    os.makedirs("/build/tmp", exist_ok=True)
    shutil.copy(os.path.join(HERE, "ccwrap.sh"), os.path.join(args.build, "wrap", "gcc"))
    rc, log = sc.sh(["/usr/bin/gcc", "-shared", "-fPIC", "-O2", "-o", SHIM, os.path.join(HERE, "detshim.c")])
    if rc != 0:
        raise SystemExit("detshim build failed: " + log)
    os.chmod(os.path.join(args.build, "wrap", "gcc"), 0o755)
    ss = samples() if not args.only else args.only.split(",")
    t0 = time.time()
    out = {"system": "SACTOR", "target": "CRUST-Bench samples (qsdrqs/CRUST-bench fork, 69970f9c)",
           "provenance": sc.provenance(__file__, os.environ.get("SACTOR_COMMAND", " ".join(sys.argv)),
                                       {"c_variants": C_VARIANTS, "ccwrap": "systems/sactor/crust/ccwrap.sh"}),
           "samples": {}}
    with cf.ThreadPoolExecutor(args.jobs) as ex:
        futs = {ex.submit(do_sample, s, args): s for s in ss}
        for fu in cf.as_completed(futs):
            s = futs[fu]
            try:
                out["samples"][s] = fu.result()
            except Exception as e:  # report, never hide
                out["samples"][s] = {"sample": s, "error": repr(e)}
            r = out["samples"][s]
            print(s, r.get("error") or {k: v.get("ok") for k, v in r.get("rust_builds", {}).items() if k.endswith("debug")},
                  [(c.get("case"), c.get("obj"), c.get("c_ub"), {k: (v.get("agree"), v.get("sactor_check_pass")) for k, v in c.get("rust", {}).items() if k.endswith(sc.PRIMARY_TC + ":debug")}) for c in r.get("cases", [])],
                  flush=True)
    out["elapsed_s"] = round(time.time() - t0, 1)
    sc.dump(args.out, out)


if __name__ == "__main__":
    main()
