"""VERT: re-run every counted VERT defect under the OTHER cargo/rustc profile.

Our harness builds the translation with `rustc -O` (release: opt-level 3, overflow checks OFF;
systems/vert/run.py:176, systems/vert/run_arrays.py:241). This script re-uses those harness modules
unchanged, but rewrites the rustc command line so the Rust side is built as DEBUG
(`-C opt-level=0 -C overflow-checks=on -C debug-assertions=on`, what plain `cargo build` does).
The C side is untouched (gcc -O1 -fwrapv oracle).

Per counted VERT row it records: the ledger example input under C / Rust-release / Rust-debug, the
whole harness grid (same inputs as the shipped result) under both profiles, the harness's own
UB-filtered counts under debug (run.test_function / run_arrays.test_one), and how many grid inputs
have a different Rust outcome between profiles (= overflow-sensitive).

Opt-in (defaults unchanged): VERT_GRID=v2 uses input grid v2 (systems/enumerate_inputs.py) for the scalar
grid and the harness re-run; ONLY=F1,F2 restricts to those functions (used for the grid-v2 defects);
VERT_WIDE_INT=1 lets the harness re-run drive functions whose C returns long / long long (arXiv, issues.md 47).

Run in the stable image (arm64):
  docker run --rm --cpus 2 -v $PWD:/work:ro -v $PWD/results:/out -w /work crust-stable \
     python3 systems/profile_check/vert_profile.py /out/profile_check_vert.json
"""
import json, os, re, subprocess, sys, tempfile, types

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "systems", "vert"))
sys.path.insert(0, os.path.join(REPO, "systems"))
import run as R          # scalar harness
import run_arrays as A   # array/float harness

PROFILE = {"p": "release"}
GRID = os.environ.get("VERT_GRID", "v1")
# arXiv (issues.md 47): VERT_WIDE_INT=1 also drives the scalar functions whose C returns long / long long
WIDE = os.environ.get("VERT_WIDE_INT") == "1"
_real = subprocess.run
DEBUG_FLAGS = ["-C", "opt-level=0", "-C", "overflow-checks=on", "-C", "debug-assertions=on"]


def _shim_run(args, *a, **k):
    if isinstance(args, list) and args and args[0] == "rustc" and PROFILE["p"] == "debug":
        args = [x for x in args if x != "-O"]
        args = args[:1] + DEBUG_FLAGS + args[1:]
    return _real(args, *a, **k)


shim = types.SimpleNamespace(**{k: getattr(subprocess, k) for k in dir(subprocess) if not k.startswith("__")})
shim.run = _shim_run
R.subprocess = shim
A.subprocess = shim


def run_lines(binp, rows, timeout=60):
    inp = ("\n".join(" ".join(str(x) for x in r) for r in rows) + "\n").encode()
    try:
        p = _real([binp], input=inp, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        o = p.stdout.decode("latin1").splitlines()
    except subprocess.TimeoutExpired as e:
        o = (e.stdout or b"").decode("latin1").splitlines()   # hang: keep what was printed
    return o + [None] * (len(rows) - len(o))


def parse_example(s):
    m = re.search(r"f_gold\((.*)\)", s)
    return [int(x) for x in m.group(1).replace(" ", "").split(",")]


def outcome(c, r, isfloat=False):
    if r in (None, "__PANIC__", "__PARSE__"):
        return "crash"
    if (A.vals_equal(c, r, isfloat) if isfloat else c == r):
        return "agrees"
    return "wrong answer"


def one_func(func, arrays, example):
    out = {"func": func, "harness": "run_arrays.py" if arrays else "run.py"}
    with tempfile.TemporaryDirectory() as d:
        if arrays:
            rust, _ = A.extract(func)
            sig = A.parse_fgold(rust)
            comps = [A.comp(t) for t in sig["params"]]
            csrc = open(os.path.join(A.TC, func, func + ".c"), errors="replace").read()
            binc = A.build_c(csrc, sig, comps, d)
            isf = sig["ret"] in A.FLOAT_SCALARS
            rows = A.gen_cases(comps, 240)
            builder = lambda dd: A.build_rust(rust, sig, comps, dd)
        else:
            rust, argtypes = R.extract_llm_rust(func)
            csrc = R.c_source(func)
            nargs = len(argtypes)
            binc = R.build_c(func, csrc, nargs, d)
            isf = False
            rows = R.enumerate_cases(["i32"] * nargs, cap=80, max_abs=512, nonneg=True, legacy=False, grid=GRID)
            builder = lambda dd: R.build_rust(rust, nargs, dd)
        bins = {}
        for prof in ("release", "debug"):
            PROFILE["p"] = prof
            dd = os.path.join(d, prof); os.makedirs(dd)
            bins[prof] = builder(dd)
        PROFILE["p"] = "release"
        if not (binc and bins["release"] and bins["debug"]):
            out["error"] = "build failed"; return out
        ex_rows = [example] if example else []
        allrows = ex_rows + [list(r) for r in rows]
        cl = run_lines(binc, allrows)
        rl = {p: run_lines(bins[p], allrows) for p in bins}
        if ex_rows:
            c, rr, rd = cl[0], rl["release"][0], rl["debug"][0]
            out["example"] = {"input": example, "c": c, "rust_release": rr, "rust_debug": rd,
                              "release_outcome": outcome(c, rr, isf), "debug_outcome": outcome(c, rd, isf)}
        off = len(ex_rows)
        n = len(rows); sens = 0; dbg_div = rel_div = 0
        dbg_only_examples = []
        for i in range(n):
            c, rr, rd = cl[off + i], rl["release"][off + i], rl["debug"][off + i]
            if c == "__PARSE__":
                continue
            orr, ord_ = outcome(c, rr, isf), outcome(c, rd, isf)
            rel_div += orr != "agrees"; dbg_div += ord_ != "agrees"
            if rr != rd:
                sens += 1
                if len(dbg_only_examples) < 4:
                    dbg_only_examples.append({"in": allrows[off + i], "c": c, "release": rr, "debug": rd})
        out["grid"] = {"cases": n, "raw_diverge_release": int(rel_div), "raw_diverge_debug": int(dbg_div),
                       "rust_release_vs_debug_differ": sens, "profile_sensitive_samples": dbg_only_examples,
                       "note": "raw = before the harness UB filter"}
    # harness's own filtered classification under DEBUG
    PROFILE["p"] = "debug"
    try:
        h = A.test_one(func, 240) if arrays else R.test_function(func, 80, grid=GRID, **({"wide_int": True} if WIDE else {}))
    finally:
        PROFILE["p"] = "release"
    keep = ("status", "kind", "value", "crash", "ub", "agree", "value_div", "rust_panic_c_ok", "ub_div")
    out["harness_debug_filtered"] = {k: h[k] for k in keep if k in h}
    out["harness_debug_filtered"]["samples"] = h.get("samples") or h.get("value_samples") or []
    out["harness_debug_filtered"]["crash_samples"] = h.get("crash_samples") or h.get("rust_panic_samples") or []
    return out


def main():
    outp = sys.argv[1]
    led = json.load(open(os.path.join(REPO, "results", "defect_ledger.json")))
    rows = [r for r in led["rows"] if r["system"] == "VERT" and r.get("count_status") == "counted"]
    only = [x for x in os.environ.get("ONLY", "").split(",") if x]
    if only:
        rows = [r for r in rows if r["translation"].split(" (")[0] in only]
    res = []
    for r in rows:
        func = r["translation"].split(" (")[0]
        arrays = "vert_arrays" in r["exposure_breakdown"]["source"]
        ex = parse_example(r["example_input"])
        try:
            o = one_func(func, arrays, ex)
        except Exception as e:  # keep going; recorded
            o = {"func": func, "error": repr(e)}
        o.update({"defect_id": r["defect_id"], "severity": r["severity"], "ledger_c": r["c_output"], "input_grid": GRID,
                  "ledger_rust": r["rust_output"], "ledger_exposure_count": r["exposure_count"]})
        res.append(o)
        json.dump(res, open(outp, "w"), indent=1)
        print(r["defect_id"], json.dumps(o.get("example")), json.dumps(o.get("grid", {}).get("rust_release_vs_debug_differ")), flush=True)
    json.dump(res, open(outp, "w"), indent=1)


if __name__ == "__main__":
    main()
