"""M3: re-run VERT's scalar (run.py) or array/float (run_arrays.py) harness under a chosen Rust
toolchain, unchanged otherwise, and diff per-function verdicts against a baseline result file.

Why: the paper says the Rust is built "under its pinned Rust toolchain", but VERT pins
nightly-2023-07-29 (artifacts/vert/benchmark/rust-toolchain.toml) while the primary arm64 run
(results/vert_diff.json, results/vert_arrays.json) used crust-stable's rustc 1.86.0.

Nothing in run.py / run_arrays.py is modified. We call their own per-function test
(run.test_function / run_arrays.test_one) with the exact arguments their main() uses by default,
in a process pool, then feed the cached results back through their own main() (monkeypatched
test function) so aggregation and the output layout are byte-for-byte theirs. The toolchain is
chosen with RUSTUP_TOOLCHAIN, which the rustup proxy `rustc` honours ahead of any
rust-toolchain file. The UB filters are the primary's: gcc ASan+UBSan and, because `clang` is
NOT on PATH in crust-vert-toolchain (asserted below), the gcc -O0/-O1 cross-check rather than MSan.

Run inside crust-vert-toolchain (see toolchain_image.sh / toolchain_run.sh), e.g.
  RUSTUP_TOOLCHAIN=nightly-2023-07-29 python3 systems/vert/toolchain_pinned.py --harness scalar \
      --out results/vert_diff_pinned_nightly.json --baseline results/vert_diff.json --jobs 3
"""
import argparse
import datetime
import json
import multiprocessing as mp
import os
import platform
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import run as scalar_run          # noqa: E402  systems/vert/run.py
import run_arrays                 # noqa: E402  systems/vert/run_arrays.py

# defaults of run.py main(): -n 80, nonneg (no --full/--signed), max_abs 512, spread grid
SCALAR_KW = dict(n=80, legacy=False, nonneg=True, max_abs=512, grid="v1")   # grid: opt-in --grid v2
ARRAY_CAP = 240                   # run_arrays.py main() default --cap


def _scalar_one(func):
    return func, scalar_run.test_function(func, SCALAR_KW["n"], legacy=SCALAR_KW["legacy"],
                                          nonneg=SCALAR_KW["nonneg"], max_abs=SCALAR_KW["max_abs"],
                                          grid=SCALAR_KW["grid"],
                                          **({"wide_int": True} if SCALAR_KW.get("wide_int") else {}))


def _array_one(func):
    return func, run_arrays.test_one(func, ARRAY_CAP)


def scalar_verdict(r):
    """Exactly run.py main()'s if/elif ladder."""
    if r["status"] != "tested":
        return r["status"]
    if r["value_div"] > 0:
        return "value_bug"
    if r["rust_panic_c_ok"] > 0:
        return "rust_panic"
    if r.get("rust_hang", 0) > 0:
        return "rust_hang"
    if r["ub_div"] > 0:
        return "ub_only"
    if r["timeout_div"] > 0:
        return "timeout_only"
    return "agree"


def array_verdict(r):
    if r["status"] != "tested":
        return r["status"]
    return {"value": "value_bug", "rust_crash": "rust_panic"}.get(r["kind"], r["kind"])


def rust_build_error(harness, func):
    """Re-build one function's Rust driver exactly as the harness does and return the first
    error line(s) from rustc (the harness itself discards stderr)."""
    with tempfile.TemporaryDirectory() as d:
        if harness == "scalar":
            rust, argtypes = scalar_run.extract_llm_rust(func)
            if rust is None or not scalar_run.supported(argtypes):
                return None
            scalar_run.build_rust(rust, len(argtypes), d)
            rfile = os.path.join(d, "prog.rs")
        else:
            rust, _ = run_arrays.extract(func)
            sig = run_arrays.parse_fgold(rust) if rust else None
            if not sig or not run_arrays.supported(sig):
                return None
            comps = [run_arrays.comp(t) for t in sig["params"]]
            run_arrays.build_rust(rust, sig, comps, d)
            rfile = os.path.join(d, "p.rs")
        p = subprocess.run(["rustc", "-O", "-A", "warnings", rfile, "-o", os.path.join(d, "x")],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode == 0:
            return {"rc": 0}
        lines = [l for l in p.stderr.decode("utf8", "replace").splitlines() if l.startswith("error")]
        return {"rc": p.returncode, "errors": lines[:4]}


def tool_versions():
    def out(cmd):
        try:
            return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
                                  ).stdout.decode().strip()
        except OSError as e:
            return f"unavailable: {e}"
    return {"rustc": out(["rustc", "-vV"]), "gcc": out(["gcc", "--version"]).splitlines()[0],
            "RUSTUP_TOOLCHAIN": os.environ.get("RUSTUP_TOOLCHAIN"),
            "clang_on_PATH": shutil.which("clang")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--harness", choices=["scalar", "arrays"], required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--baseline", required=True, help="result file to diff verdicts against")
    ap.add_argument("--extra-baseline", action="append", default=[],
                    help="label=path of further result files to diff against (e.g. a same-session control)")
    ap.add_argument("--embed-control", default=None,
                    help="path of a same-session control run (this script, other toolchain) whose "
                         "summary, provenance and verdicts are embedded in the output")
    ap.add_argument("--expect-clang", action="store_true",
                    help="x86-64 MSan configuration (crust-heldout): `clang` IS on PATH, so run.py uses "
                         "clang MSan instead of the -O0/-O1 cross-check, as the x86 re-runs did")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--grid", choices=("v1", "v2"), default="v1",
                    help="scalar input grid (systems/enumerate_inputs.py); default v1 = the shipped result")
    ap.add_argument("--wide-int", action="store_true",
                    help="arXiv: pass wide_int=True to run.test_function (functions whose C returns long / "
                         "long long); only with --only (run.py main()'s aggregation is not used then)")
    ap.add_argument("--only", default="",
                    help="comma-separated functions: run only these and skip run.py main()'s aggregation "
                         "(the output then has 'all' and 'value_bugs' for these functions only)")
    args = ap.parse_args()
    SCALAR_KW["grid"] = args.grid
    if args.wide_int:
        assert args.harness == "scalar" and args.only, "--wide-int needs --harness scalar and --only"
        SCALAR_KW["wide_int"] = True

    if args.expect_clang:
        assert shutil.which("clang"), "--expect-clang but no clang on PATH"
    else:
        assert shutil.which("clang") is None, \
            "clang on PATH would switch run.py to MSan; the primary arm64 run had no clang"
    tc = os.environ.get("RUSTUP_TOOLCHAIN")
    assert tc, "set RUSTUP_TOOLCHAIN"
    vers = tool_versions()

    mod = scalar_run if args.harness == "scalar" else run_arrays
    funcs = mod.accepted_functions()
    if args.limit:
        funcs = funcs[:args.limit]
    only = [x for x in args.only.split(",") if x]
    if only:
        assert set(only) <= set(funcs), sorted(set(only) - set(funcs))
        funcs = [f for f in funcs if f in only]
    worker = _scalar_one if args.harness == "scalar" else _array_one
    t0 = datetime.datetime.now(datetime.timezone.utc)
    with mp.Pool(args.jobs) as pool:
        cache = dict(pool.map(worker, funcs, chunksize=1))
    t1 = datetime.datetime.now(datetime.timezone.utc)

    # Feed the cached per-function results back through the harness's OWN main() so the
    # aggregation and output layout are exactly the primary's.
    tmp_out = args.out + ".harness_main.tmp.json"
    if only:
        allr = [cache[f] for f in funcs]
        k = "value_div" if args.harness == "scalar" else "value"
        json.dump({"summary": {"only": funcs, "grid": args.grid if args.harness == "scalar" else None,
                               "note": "--only run: run.py main()'s aggregation skipped; rows are test_function's own"},
                   "all": allr, "value_bugs": [r for r in allr if r.get("status") == "tested" and r.get(k, 0) > 0]},
                  open(tmp_out, "w"))
    elif args.harness == "scalar":
        def cached(func, n, legacy=False, nonneg=True, max_abs=512, grid="v1"):
            assert (n, legacy, nonneg, max_abs, grid) == (SCALAR_KW["n"], SCALAR_KW["legacy"],
                                                          SCALAR_KW["nonneg"], SCALAR_KW["max_abs"], SCALAR_KW["grid"])
            return cache[func]
        scalar_run.test_function = cached
        argv = ["run.py", "--out", tmp_out, "--grid", args.grid] + (["--limit", str(args.limit)] if args.limit else [])
        sys.argv = argv
        scalar_run.main()
    else:
        def cached_a(func, cap):
            assert cap == ARRAY_CAP
            return cache[func]
        run_arrays.test_one = cached_a
        sys.argv = ["run_arrays.py", "--out", tmp_out] + (["--limit", str(args.limit)] if args.limit else [])
        run_arrays.main()
    res = json.load(open(tmp_out))
    os.remove(tmp_out)

    verdict = scalar_verdict if args.harness == "scalar" else array_verdict
    mine = {r["func"]: verdict(r) for r in res["all"]}

    def diff_against(path):
        base = json.load(open(path))
        theirs = {r["func"]: verdict(r) for r in base["all"]}
        changed = []
        for f in funcs:
            a, b = theirs.get(f), mine.get(f)
            if a != b:
                changed.append({"func": f, "baseline": a, "this_run": b})
        # value-bug sample inputs also compared for functions that are value_bug in both
        same_bug_diff_samples = []
        bmap = {r["func"]: r for r in base["all"]}
        for r in res["all"]:
            f = r["func"]
            if mine.get(f) == "value_bug" and theirs.get(f) == "value_bug":
                k = "value_samples" if args.harness == "scalar" else "samples"
                if [s["in"] for s in r.get(k, [])] != [s["in"] for s in bmap[f].get(k, [])]:
                    same_bug_diff_samples.append(f)
        return {"baseline_file": os.path.relpath(path, REPO),
                "baseline_provenance": base.get("provenance"),
                "functions_compared": len(funcs), "verdict_changes": changed,
                "n_verdict_changes": len(changed),
                "value_bug_in_both_but_different_smallest_samples": same_bug_diff_samples}

    diffs = {"primary": diff_against(args.baseline)}
    for spec in args.extra_baseline:
        label, path = spec.split("=", 1)
        diffs[label] = diff_against(path)

    build_fail = sorted(f for f, v in mine.items() if v == "rust-build-fail")
    build_err = {f: rust_build_error(args.harness, f) for f in build_fail}

    res["provenance"] = {
        "script": "systems/vert/toolchain_pinned.py",
        "wraps": "systems/vert/run.py" if args.harness == "scalar" else "systems/vert/run_arrays.py",
        "command": "RUSTUP_TOOLCHAIN=%s python3 %s" % (tc, " ".join(
            ["systems/vert/toolchain_pinned.py"] + [a for a in sys_argv_orig])),
        "harness_args": SCALAR_KW if args.harness == "scalar" else {"cap": ARRAY_CAP},
        "git_rev": os.environ.get("GIT_REV"),
        "git_dirty_note": os.environ.get("GIT_DIRTY"),
        "host": os.environ.get("HOST_DESC"),
        "arch": platform.machine(),
        "image": os.environ.get("IMAGE_DESC"),
        "toolchains": vers,
        "jobs": args.jobs,
        "date_start_utc": t0.isoformat(), "date_end_utc": t1.isoformat(),
    }
    res["toolchain_comparison"] = {
        "toolchain": tc,
        "compile": {"rust_build_ok_among_in_scope": sum(1 for v in mine.values()
                                                         if v not in ("unsupported", "no-artifact",
                                                                      "no-c", "non_int_return",
                                                                      "side_effecting",
                                                                      "c-build-fail",
                                                                      "rust-build-fail")),
                    "rust_build_fail": len(build_fail),
                    "rust_build_fail_functions": build_fail,
                    "rust_build_errors": build_err},
        "verdicts": mine,
        "diffs": diffs,
    }
    if args.embed_control:
        ctl = json.load(open(args.embed_control))
        res["control_same_session"] = {
            "summary": ctl["summary"], "provenance": ctl.get("provenance"),
            "compile": ctl["toolchain_comparison"]["compile"],
            "verdicts": ctl["toolchain_comparison"]["verdicts"],
            "diff_vs_primary": ctl["toolchain_comparison"]["diffs"]["primary"],
            "value_samples": {r["func"]: r.get("value_samples", r.get("samples"))
                              for r in ctl["all"] if r.get("status") == "tested"}}
    with open(args.out, "w") as f:
        json.dump(res, f, indent=2)
    print(json.dumps({"summary": res["summary"],
                      "n_changes": {k: v["n_verdict_changes"] for k, v in diffs.items()}}, indent=1))


sys_argv_orig = sys.argv[1:]

if __name__ == "__main__":
    main()
