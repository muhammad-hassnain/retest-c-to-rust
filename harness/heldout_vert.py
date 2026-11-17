"""M2 (held-out measurement), VERT part.

For every VERT function we count (25 scalar wrong answers, 2 scalar Rust hangs, 10 array/float
wrong answers, 2 array panics), this script:

  1. parses VERT's OWN check inputs from the shipped harnesses, per function:
       * bounded Kani (out-rwasm-mutated/src/main.rs): the rWasm oracle draws PARAM1 from the
         `kani::assume((lo..hi).contains(&PARAM1))` range and keeps the remaining arguments at
         the literals of the C stub `f_gold(...)` in <F>_towasm_mutated.c; the Rust translation is
         called with the static initial values `static mut PARAMk = v`. We record both the
         oracle-side tuples {(n, stub[1:]) : lo <= n < hi} and the Rust-side point.
       * bolero (out-rwasm-bolero/src/main.rs): the documented step `cargo bolero reduce` replays
         only a saved corpus (src/__fuzz__/bolero_wasm_eq/corpus); we list it if present.
       * the TransCoder test arrays `param0[] ...` in <F>.c (shipped with the benchmark, not run
         by VERT's evaluation.py; recorded for completeness).
  2. regenerates OUR inputs exactly as systems/vert/run.py / run_arrays.py do, and computes the
     exact (whole-tuple) overlap with each of VERT's sets;
  3. re-runs OUR C-vs-Rust comparison (same builds, same UB filter as systems/batch_diff.py /
     run_arrays.classify) and keeps EVERY diverging input, so we can say whether each counted
     defect diverges outside VERT's input set (and checks the re-run reproduces the counted
     numbers);
  4. runs VERT's own Kani-domain inputs (and the TransCoder arrays) through OUR comparison:
     does the defect show on VERT's own inputs when C and Rust get the same input?

Run in the image that produced results/vert_diff.json (arm64, crust-stable):
  docker run --rm -v "$PWD:/work" -w /work crust-stable python3 -I harness/heldout_vert.py
Writes results/heldout_vert.json.
"""
import datetime
import json
import os
import platform
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "systems"))
sys.path.insert(0, os.path.join(REPO, "systems", "vert"))
import batch_diff as bd  # noqa: E402
import run as vrun  # noqa: E402  (systems/vert/run.py)
import run_arrays as varr  # noqa: E402
from enumerate_inputs import enumerate_cases  # noqa: E402

TC = os.path.join(REPO, "artifacts", "vert", "benchmark", "c_transcoder")
OUT = os.path.join(REPO, "results", "heldout_vert.json")


# ---------------------------------------------------------------- VERT's own inputs
def _ints(s):
    return [int(x) for x in re.findall(r"-?\d+", s)]


def parse_vert_inputs(func):
    base = os.path.join(TC, func)
    mut = open(os.path.join(base, "out-rwasm-mutated", "src", "main.rs"), errors="replace").read()
    bol = open(os.path.join(base, "out-rwasm-bolero", "src", "main.rs"), errors="replace").read()
    stub = open(os.path.join(base, f"{func}_towasm_mutated.c"), errors="replace").read()
    csrc = open(os.path.join(base, f"{func}.c"), errors="replace").read()
    statics = [(int(k), t.strip(), v.strip()) for k, t, v in
               re.findall(r"^static mut PARAM(\d+)\s*:\s*([^=]+?)\s*=\s*(.+?);", mut, re.M)]
    assumes = re.findall(r"kani::assume\(\((-?\d+)\.\.(-?\d+)\)\.contains\(&(PARAM\d+(?:\[\d+\])?)\)\)", mut)
    unwind = re.findall(r"#\[kani::unwind\((\d+)\)\]", mut)
    wt = re.search(r"with_type::<\((.*?)\)>\(\)", bol)
    gen = re.search(r"\.with_generator\((.*?)\)\.cloned", bol)
    calls = re.findall(r"f_gold\s*\(([^;]*)\)\s*;", stub)
    stub_call = calls[-1].strip() if calls else None
    arrays_in_stub = re.findall(r"int\s+(\w+)\s*\[\s*\]\s*=\s*\{([^}]*)\}", stub)
    # constants the oracle's _start body loads before the PARAM1 override (static validation that
    # the non-symbolic arguments are the stub literals)
    m = re.search(r"fn func_\d+\(&mut self, \) -> Option<i32> \{(.*?)PARAM1(?:\[0\])? = kani::any\(\);", mut, re.S)
    start_consts = [int(x) for x in re.findall(r"TaggedVal::from\((-?\d+)i32\)", m.group(1))] if m else None
    corpus_dir = os.path.join(base, "out-rwasm-bolero", "src", "__fuzz__", "bolero_wasm_eq", "corpus")
    corpus = sorted(os.listdir(corpus_dir)) if os.path.isdir(corpus_dir) else []
    # TransCoder test arrays (scalar ones only: `int param0[] = {...};`)
    tparams = {}
    for name, body in re.findall(r"\bint\s+param(\d+)\s*\[\s*\]\s*=\s*\{([^}]*)\}\s*;", csrc):
        tparams[int(name)] = _ints(body)
    loop_breaks_after_first = bool(re.search(r"n_success\s*\+=\s*1;\s*\}\s*break;", csrc))
    return {"statics": statics, "kani_assumes": assumes, "kani_unwind_attr": unwind,
            "bolero_with_type": wt.group(1) if wt else None,
            "bolero_with_generator": gen.group(1) if gen else None,
            "bolero_corpus_files": len(corpus),
            "stub_call": stub_call, "stub_arrays": arrays_in_stub,
            "oracle_start_constants": start_consts,
            "transcoder_params": tparams,
            "transcoder_loop_breaks_after_first": loop_breaks_after_first}


def scalar_kani_sets(func, nargs, vi):
    """Oracle-side tuples and Rust-side point for an all-scalar function."""
    stub = _ints(vi["stub_call"]) if vi["stub_call"] else None
    ok = stub is not None and len(stub) == nargs and len(vi["kani_assumes"]) == 1 \
        and vi["kani_assumes"][0][2] == "PARAM1"
    if not ok:
        return None
    lo, hi = int(vi["kani_assumes"][0][0]), int(vi["kani_assumes"][0][1])
    oracle = [[n] + stub[1:] for n in range(lo, hi)]
    statics = sorted(vi["statics"])
    rust_pt = [int(v) for _k, _t, v in statics]
    # static validation: the oracle body loads the stub literals (in order) before the override
    sc = vi["oracle_start_constants"] or []
    stub_consts_match = sc[-len(stub):] == stub if stub else False
    return {"assume_range": [lo, hi], "oracle_side": oracle, "rust_side": rust_pt,
            "oracle_body_loads_stub_literals": stub_consts_match}


def transcoder_tuples(vi, nargs):
    tp = vi["transcoder_params"]
    if sorted(tp) != list(range(nargs)):
        return None
    n = min(len(v) for v in tp.values())
    return [[tp[k][i] for k in range(nargs)] for i in range(n)]


# ---------------------------------------------------------------- our comparison, keep everything
def tally_all(binc, binr, cases, binc_san=None, binc_alt=None, binc_msan=None):
    """systems/batch_diff.tally, verbatim classification, but returns the verdict of EVERY input."""
    per = []
    altl = bd.run_batch(binc_alt, cases)[1] if binc_alt is not None else None
    cands = []
    for i, (row, cl, rl) in enumerate(bd.eval_cases(binc, binr, cases)):
        if cl == "__PARSE__" or rl == "__PARSE__":
            per.append({"in": row, "v": "skip", "c": cl, "rust": rl}); continue
        if cl is not None and cl != "__TIMEOUT__" and cl == rl:
            per.append({"in": row, "v": "agree", "c": cl, "rust": rl}); continue
        if altl is not None and cl is not None:
            alt = altl[i] if i < len(altl) else None
            if alt is not None and alt != "__PARSE__" and alt != cl:
                per.append({"in": row, "v": "ub", "c": cl, "rust": rl, "why": "opt-level"}); continue
        if cl is None or cl == "__TIMEOUT__":
            cands.append((row, None, rl, "c_missing")); continue
        if rl == "__TIMEOUT__":
            cands.append((row, cl, rl, "hang")); continue
        if rl is None or rl == "__PANIC__":
            cands.append((row, cl, rl, "crash")); continue
        cands.append((row, cl, rl, "value"))
    for row, cl, rl, tag in cands:
        # audit 8 uniform rule (as batch_diff.tally): a sanitizer run that fails to start, times out
        # or prints another value than the oracle leaves the input not UB-clean ("san_unresolved")
        vs = []
        for sbin, why in ((binc_san, "asan/ubsan"), (binc_msan, "msan")):
            if sbin is not None:
                v, sline = bd.san_run(sbin, row)
                vs.append((v, sline, why))
                if v == "ub":
                    break
        if vs and vs[-1][0] == "ub":
            per.append({"in": row, "v": "ub", "c": cl, "rust": rl, "why": vs[-1][2]}); continue
        if any(v in ("infra", "timeout") or (tag != "c_missing" and sl != cl) for v, sl, _ in vs):
            per.append({"in": row, "v": "san_unresolved", "c": cl, "rust": rl}); continue
        if tag == "c_missing":
            per.append({"in": row, "v": "ub" if binc_san is None else "timeout", "c": cl, "rust": rl}); continue
        if tag == "hang":
            per.append({"in": row, "v": "rust_hang", "c": cl, "rust": rl}); continue
        if tag == "crash":
            per.append({"in": row, "v": "rust_panic", "c": cl, "rust": rl}); continue
        per.append({"in": row, "v": "value", "c": cl, "rust": rl})
    return per


def classify_all(rows, cl, rl, ret_is_float, binc_san, binc_alt=None):
    """systems/vert/run_arrays.classify, verbatim classification, verdict of EVERY input."""
    per = []
    altl = varr.run_batch(binc_alt, rows) if (binc_alt is not None and not ret_is_float) else None
    cands = []
    for i, row in enumerate(rows):
        a = cl[i] if i < len(cl) else None
        b = rl[i] if i < len(rl) else None
        if a == "__PARSE__":
            per.append({"in": row, "v": "skip"}); continue
        if altl is not None and a is not None:
            alt = altl[i] if i < len(altl) else None
            if alt is not None and alt != "__PARSE__" and alt != a:
                per.append({"in": row, "v": "ub", "c": a, "rust": b, "why": "opt-level"}); continue
        if a is None:
            cands.append((row, a, b, "c_missing")); continue
        if b is None or b == "__PANIC__" or b == "__PARSE__":
            cands.append((row, a, b, "crash")); continue
        if varr.vals_equal(a, b, ret_is_float):
            per.append({"in": row, "v": "agree", "c": a, "rust": b})
        else:
            cands.append((row, a, b, "value"))
    for row, a, b, tag in cands:
        if binc_san is not None:
            sline, src, sto, sstderr = varr.run_one(binc_san, row)
            if sto:
                per.append({"in": row, "v": "timeout", "c": a, "rust": b}); continue
            if src != 0 or varr.SAN_RE.search(sstderr):
                per.append({"in": row, "v": "ub", "c": a, "rust": b, "why": "asan/ubsan"}); continue
        if tag == "c_missing":
            per.append({"in": row, "v": "timeout", "c": a, "rust": b}); continue
        per.append({"in": row, "v": "rust_panic" if tag == "crash" else "value", "c": a, "rust": b})
    return per


def counts(per):
    c = {}
    for p in per:
        c[p["v"]] = c.get(p["v"], 0) + 1
    return c


def tset(rows):
    return {tuple(r) for r in rows}


# ---------------------------------------------------------------- scalar functions
def scalar_function(func, expected):
    rust, argtypes = vrun.extract_llm_rust(func)
    csrc = vrun.c_source(func)
    nargs = len(argtypes)
    vi = parse_vert_inputs(func)
    ours = enumerate_cases(["i32"] * nargs, cap=80, max_abs=512, nonneg=True, legacy=False)
    ks = scalar_kani_sets(func, nargs, vi)
    tt = transcoder_tuples(vi, nargs)
    with tempfile.TemporaryDirectory() as d:
        binc = vrun.build_c(func, csrc, nargs, d)
        binr = vrun.build_rust(rust, nargs, d)
        binc_san = vrun.build_c(func, csrc, nargs, d, mode="san")
        binc_msan = vrun.build_c(func, csrc, nargs, d, mode="msan")
        binc_alt = None
        if binc_msan is None and not vrun.c_uses_float(csrc):
            binc_alt = vrun.build_c(func, csrc, nargs, d, mode="alt")
        run = lambda rows: tally_all(binc, binr, rows, binc_san=binc_san, binc_alt=binc_alt, binc_msan=binc_msan)  # noqa: E731
        ours_per = run(ours)
        kani_oracle_per = run(ks["oracle_side"]) if ks else None
        kani_rust_per = run([ks["rust_side"]]) if ks else None
        tc_per = run(tt) if tt else None
    div = [p for p in ours_per if p["v"] in ("value", "rust_panic", "rust_hang")]
    vert_set = set()
    if ks:
        vert_set |= tset(ks["oracle_side"]) | {tuple(ks["rust_side"])}
    tc_set = tset(tt) if tt else set()
    our_set = tset(ours)
    return {
        "function": func, "kind": "scalar", "nargs": nargs,
        "counted_as": expected["counted_as"],
        "expected_from": expected["source"],
        "reproduced": {"value": sum(p["v"] == "value" for p in ours_per),
                       "rust_hang": sum(p["v"] == "rust_hang" for p in ours_per),
                       "expected_value": expected["value_div"], "expected_rust_hang": expected["rust_hang"]},
        "vert_inputs": {
            "kani_assume": vi["kani_assumes"], "kani_unwind_attr": vi["kani_unwind_attr"],
            "stub_call": vi["stub_call"], "statics": vi["statics"],
            "oracle_body_loads_stub_literals": ks["oracle_body_loads_stub_literals"] if ks else None,
            "kani_oracle_side": ks["oracle_side"] if ks else None,
            "kani_rust_side": ks["rust_side"] if ks else None,
            "bolero_with_type": vi["bolero_with_type"], "bolero_with_generator": vi["bolero_with_generator"],
            "bolero_corpus_files": vi["bolero_corpus_files"],
            "transcoder_test_tuples": tt, "transcoder_loop_breaks_after_first": vi["transcoder_loop_breaks_after_first"],
        },
        "our_inputs": len(ours),
        "our_grid_verdicts": counts(ours_per),
        "overlap": {
            "ours_vs_kani_oracle_side": sorted(our_set & tset(ks["oracle_side"])) if ks else None,
            "ours_vs_kani_rust_side": sorted(our_set & {tuple(ks["rust_side"])}) if ks else None,
            "ours_vs_bolero_corpus": "no corpus shipped" if vi["bolero_corpus_files"] == 0 else "corpus present (not decoded)",
            "ours_vs_transcoder_tests": sorted(our_set & tc_set),
        },
        "our_divergences": {
            "count_by_class": counts(div),
            "inputs": [p["in"] for p in div],
            "inside_vert_check_set": sorted({tuple(p["in"]) for p in div} & vert_set),
            "outside_vert_check_set": len({tuple(p["in"]) for p in div} - vert_set),
            "inside_transcoder_tests": sorted({tuple(p["in"]) for p in div} & tc_set),
            "smallest": sorted(div, key=lambda p: sum(abs(x) for x in p["in"]))[:3],
        },
        "our_comparison_on_vert_inputs": {
            "kani_oracle_side": kani_oracle_per, "kani_rust_side": kani_rust_per,
            "kani_domain_diverges": (any(p["v"] in ("value", "rust_panic", "rust_hang") for p in kani_oracle_per + kani_rust_per)
                                     if ks else None),
            "kani_oracle_side_diverges": (any(p["v"] in ("value", "rust_panic", "rust_hang") for p in kani_oracle_per)
                                          if ks else None),
            "transcoder_tests": tc_per,
            "transcoder_tests_diverge": (any(p["v"] in ("value", "rust_panic", "rust_hang") for p in tc_per) if tc_per else None),
            "transcoder_first_tuple_diverges": (tc_per[0]["v"] in ("value", "rust_panic", "rust_hang") if tc_per else None),
        },
    }


# ---------------------------------------------------------------- array/float functions
def array_function(func, expected):
    rust, _ = varr.extract(func)
    sig = varr.parse_fgold(rust)
    comps = [varr.comp(t) for t in sig["params"]]
    csrc = open(os.path.join(TC, func, f"{func}.c"), errors="replace").read()
    vi = parse_vert_inputs(func)
    rows = varr.gen_cases(comps, 240)
    # Rust-side Kani point: statics, flattened in parameter order (arrays element-wise)
    rust_pt = []
    for _k, _t, v in sorted(vi["statics"]):
        rust_pt.extend(_ints(v))
    ret_is_float = sig["ret"] in varr.FLOAT_SCALARS
    with tempfile.TemporaryDirectory() as d:
        binc = varr.build_c(csrc, sig, comps, d)
        binr = varr.build_rust(rust, sig, comps, d)
        binc_san = varr.build_c(csrc, sig, comps, d, mode="san")
        uses_fp = bool(re.search(r"\b(float|double)\b", varr.strip_scaffold(csrc)))
        binc_alt = None if uses_fp else varr.build_c(csrc, sig, comps, d, mode="alt")

        def run(rr):
            return classify_all(rr, varr.run_batch(binc, rr), varr.run_batch(binr, rr), ret_is_float, binc_san, binc_alt)
        ours_per = run(rows)
        rust_side_per = run([rust_pt]) if len(rust_pt) == len(rows[0]) else None
    div = [p for p in ours_per if p["v"] in ("value", "rust_panic")]
    has_array = any(c[0] == "arr" for c in comps)
    return {
        "function": func, "kind": "array/float", "params": sig["params"],
        "counted_as": expected["counted_as"], "expected_from": expected["source"],
        "reproduced": {"value": sum(p["v"] == "value" for p in ours_per),
                       "rust_panic": sum(p["v"] == "rust_panic" for p in ours_per),
                       "expected_value": expected["value"], "expected_crash": expected["crash"]},
        "vert_inputs": {
            "kani_assume": vi["kani_assumes"], "stub_call": vi["stub_call"], "stub_arrays": vi["stub_arrays"],
            "statics": vi["statics"], "kani_rust_side_flat": rust_pt,
            "kani_oracle_side": ("not mapped to a C input: the symbolic value overrides the oracle's "
                                 "array-pointer/first operand inside the rWasm call sequence") if has_array
                                else "scalar float function: see assume",
            "bolero_with_type": vi["bolero_with_type"], "bolero_corpus_files": vi["bolero_corpus_files"],
        },
        "our_inputs": len(rows),
        "our_grid_verdicts": counts(ours_per),
        "overlap": {"ours_vs_kani_rust_side": sorted(tset(rows) & {tuple(rust_pt)}),
                    "ours_vs_bolero_corpus": "no corpus shipped" if vi["bolero_corpus_files"] == 0 else "corpus present"},
        "our_divergences": {"count_by_class": counts(div), "inputs": [p["in"] for p in div],
                            "equal_to_kani_rust_side": sum(tuple(p["in"]) == tuple(rust_pt) for p in div),
                            "smallest": sorted(div, key=lambda p: sum(abs(x) for x in p["in"]))[:3]},
        "our_comparison_on_vert_inputs": {"kani_rust_side": rust_side_per},
    }


def main():
    vd = json.load(open(os.path.join(REPO, "results", "vert_diff.json")))
    va = json.load(open(os.path.join(REPO, "results", "vert_arrays.json")))
    vadj = json.load(open(os.path.join(REPO, "results", "vert_arrays_adjusted.json")))
    adj_kind = {r["func"]: r["kind"] for r in vadj["rows"]}
    scalar = [(r, "wrong_answer") for r in vd["value_bugs"]] + \
             [(r, "rust_hang") for r in vd["all"] if r.get("status") == "tested" and r.get("value_div", 0) == 0
              and r.get("rust_panic_c_ok", 0) == 0 and r.get("rust_hang", 0) > 0]
    only = [x for x in os.environ.get("ONLY", "").split(",") if x]
    jobs = []
    for r, kind in scalar:
        if only and r["func"] not in only:
            continue
        exp = {"counted_as": kind, "source": "results/vert_diff.json", "value_div": r["value_div"],
               "rust_hang": r.get("rust_hang", 0)}
        jobs.append((scalar_function, r["func"], exp))
    for r in va["value_bugs"] + va["rust_crash_bugs"]:
        if only and r["func"] not in only:
            continue
        k = adj_kind.get(r["func"], r["kind"])
        kind = {"value": "wrong_answer", "rust_crash": "rust_panic"}.get(k, "not_counted:" + k)
        exp = {"counted_as": kind, "source": "results/vert_arrays.json + vert_arrays_adjusted.json",
               "value": r["value"], "crash": r["crash"]}
        jobs.append((array_function, r["func"], exp))
    from concurrent.futures import ProcessPoolExecutor
    with ProcessPoolExecutor(max_workers=int(os.environ.get("JOBS", "6"))) as ex:
        futs = [ex.submit(fn, f, e) for fn, f, e in jobs]
        rows = []
        for (fn, f, e), fu in zip(jobs, futs):
            res = fu.result()
            rows.append(res)
            print(f, e["counted_as"], res["reproduced"],
                  "kani oracle-side diverges:", res["our_comparison_on_vert_inputs"].get("kani_oracle_side_diverges"),
                  flush=True)

    def ver(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True).stdout.splitlines()[0]
        except Exception:  # noqa: BLE001
            return None
    prov = {
        "script": "harness/heldout_vert.py",
        "command": "docker run --rm -v \"$PWD:/work\" -w /work crust-stable python3 -I harness/heldout_vert.py",
        "git_rev": os.environ.get("GIT_REV"),
        "host": os.environ.get("HOST_DESC", platform.system()),
        "arch": platform.machine(),
        "toolchains": {"gcc": ver(["gcc", "--version"]), "rustc": ver(["rustc", "--version"]),
                       "clang": ver(["clang", "--version"]), "python": platform.python_version()},
        "image": os.environ.get("IMAGE_DESC"),
        "date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "inputs_read": ["artifacts/vert/benchmark/c_transcoder/<F>/{out-rwasm-mutated,out-rwasm-bolero}/src/main.rs",
                        "artifacts/vert/benchmark/c_transcoder/<F>/<F>_towasm_mutated.c",
                        "artifacts/vert/benchmark/c_transcoder/<F>/<F>.c",
                        "results/vert_diff.json", "results/vert_arrays.json", "results/vert_arrays_adjusted.json"],
    }
    json.dump({"provenance": prov, "functions": rows}, open(os.environ.get("OUT", OUT), "w"), indent=1)


if __name__ == "__main__":
    main()
