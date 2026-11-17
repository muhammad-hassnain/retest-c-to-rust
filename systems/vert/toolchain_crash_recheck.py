"""Follow-up: apply the FULL C-UB rule to VERT's crash defects, and supplement
the wrong-answer recheck with the same full rule.

FULL RULE -- an input is C-clean iff, on that input, the ORIGINAL C (the primary harness's own driver
source, run.build_c / run_arrays.build_c):
  1. gcc  -O0 -fsanitize=address,undefined                                    reports nothing
  2. clang-14 -O0 and -O2 -fsanitize=address,undefined,float-cast-overflow,float-divide-by-zero
     reports nothing                                  (-fno-sanitize-recover=all everywhere)
  3. clang-14 -O1 -fsanitize=memory reports nothing (recorded 'unavailable' if MSan cannot start)
  4. prints the same value under gcc -O0/-O1/-O2 and clang-14 -O0/-O1/-O2 (all -fwrapv, as the primary)
  5. prints the same value under gcc -O1 -fwrapv -fsigned-char and -funsigned-char
and the translation still diverges there (Rust panics / wrong value) -- the Rust binary is the
primary's (rustc 1.86.0 -O).

Modes
  crash      the 4 VERT crash defects of results/defect_ledger.json: every crash-exposing input of
             the primary grid (Rust panics or dies where the C prints a value) is checked; plus the
             whole harness verdict re-run under -fsigned-char / -funsigned-char gcc wrappers.
             Writes results/vert_crash_ub_recheck.json.
  wrong      for every function in results/vert_wrong_answer_ub_recheck.json, walk its CLEAN
             diverging inputs (smallest first) and record the first that passes the FULL rule
             (adds -O2, float-divide-by-zero and per-input char signedness to that file's rule).
             Written into that file under "full_rule_supplement".

Run in crust-vert-toolchain with RUSTUP_TOOLCHAIN=1.86.0 (systems/vert/toolchain_crash_run.sh).
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
sys.path.insert(0, os.path.dirname(HERE))
import toolchain_ub_recheck as ub      # noqa: E402  (prepare, san_check, lines_for, gcc wrappers)
import run as srun                     # noqa: E402
import run_arrays as arun              # noqa: E402
from batch_diff import eval_cases      # noqa: E402

CLANG = ub.CLANG
CLANG_UB = "address,undefined,float-cast-overflow,float-divide-by-zero"
CRASH_TARGETS = [
    ("arrays", "CEILING_IN_A_SORTED_ARRAY", "VERT-CEILING_IN_A_SORTED_ARRAY-1"),
    ("arrays", "SPACE_OPTIMIZED_DP_SOLUTION_0_1_KNAPSACK_PROBLEM_1", "VERT-SPACE_OPTIMIZED_DP_SOLUTION_0_1_KNAPSACK_PROBLEM_1-1"),
    ("arrays", "HOW_TO_CHECK_IF_A_GIVEN_ARRAY_REPRESENTS_A_BINARY_HEAP_1", "VERT-HOW_TO_CHECK_IF_A_GIVEN_ARRAY_REPRESENTS_A_BINARY_HEAP_1-2"),
    ("scalar", "MAXIMUM_NUMBER_SEGMENTS_LENGTHS_B_C", "VERT-MAXIMUM_NUMBER_SEGMENTS_LENGTHS_B_C-2"),
]
SAN_VARIANTS = {
    "gcc_san_O0": ["gcc", "-O0", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"],
    "clang_fullsan_O0": [CLANG, "-O0", f"-fsanitize={CLANG_UB}", "-fno-sanitize-recover=all"],
    "clang_fullsan_O2": [CLANG, "-O2", f"-fsanitize={CLANG_UB}", "-fno-sanitize-recover=all"],
    "clang_msan_O1": [CLANG, "-O1", "-fsanitize=memory", "-fno-sanitize-recover=all"],
}
VALUE_VARIANTS = {
    "gcc_O0": ["gcc", "-O0", "-fwrapv"], "gcc_O2": ["gcc", "-O2", "-fwrapv"],
    "clang_O0": [CLANG, "-O0", "-fwrapv"], "clang_O1": [CLANG, "-O1", "-fwrapv"], "clang_O2": [CLANG, "-O2", "-fwrapv"],
    "gcc_O1_fsigned_char": ["gcc", "-O1", "-fwrapv", "-fsigned-char"],
    "gcc_O1_funsigned_char": ["gcc", "-O1", "-fwrapv", "-funsigned-char"],
}


def build_all(cfile, d):
    out, errs = {}, {}
    for k, cmd in list(SAN_VARIANTS.items()) + list(VALUE_VARIANTS.items()):
        b = os.path.join(d, "f_" + k)
        p = subprocess.run(cmd + ["-w", cfile, "-o", b, "-lm"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode == 0 and os.path.isfile(b):
            out[k] = b
        else:
            errs[k] = (p.stderr.decode("utf8", "replace").strip().splitlines() or [f"rc={p.returncode}"])[-1:]
    return out, errs


def full_rule(var, values, i, row, c_line, eq):
    """Apply the FULL rule to one input. `values[k]` are batch outputs of the value variants."""
    rec = {"in": row, "c": c_line}
    for k in SAN_VARIANTS:
        if k in var:
            st, msg, line = ub.san_check(var[k], row)
            rec[k] = st if msg is None else f"{st}: {msg}"
            if st == "clean" and line is not None and not eq(line, c_line):
                rec[k + "_value"] = line
        else:
            rec[k] = "unavailable"
    vals = {k: (values[k][i] if k in values and i < len(values[k]) else None) for k in VALUE_VARIANTS}
    rec["values"] = vals
    rec["opt_levels_agree"] = all(vals[k] is not None and eq(vals[k], c_line)
                                  for k in ("gcc_O0", "gcc_O2", "clang_O0", "clang_O1", "clang_O2"))
    rec["char_signedness_agree"] = all(vals[k] is not None and eq(vals[k], c_line)
                                       for k in ("gcc_O1_fsigned_char", "gcc_O1_funsigned_char"))
    san_ok = all(rec[k] == "clean" for k in ("gcc_san_O0", "clang_fullsan_O0", "clang_fullsan_O2")) \
        and rec["clang_msan_O1"] in ("clean", "unavailable") \
        and not any(k.endswith("_value") for k in rec)
    rec["passes_full_rule"] = bool(san_ok and rec["opt_levels_agree"] and rec["char_signedness_agree"])
    return rec


def harness_counts(kind, func, prefix):
    old = os.environ["PATH"]
    if prefix:
        os.environ["PATH"] = prefix + ":" + old
    try:
        if kind == "scalar":
            r = srun.test_function(func, 80, legacy=False, nonneg=True, max_abs=512)
            return {"status": r["status"], "value": r.get("value_div"), "rust_panic": r.get("rust_panic_c_ok"),
                    "ub": r.get("ub_div"), "timeout": r.get("timeout_div"), "rust_hang": r.get("rust_hang")}
        r = arun.test_one(func, 240)
        return {"status": r["status"], "kind": r.get("kind"), "value": r.get("value"), "rust_panic": r.get("crash"),
                "ub": r.get("ub"), "timeout": r.get("timeout")}
    finally:
        os.environ["PATH"] = old


def crash_job(args):
    kind, func, defect_id, wrappers = args
    with tempfile.TemporaryDirectory() as d:
        binc, binr, cfile, cases, eq, is_float, _ = ub.prepare(kind, func, d)
        if not binc or not binr:
            return {"func": func, "defect_id": defect_id, "status": "build-fail"}
        var, errs = build_all(cfile, d)
        values = {k: ub.lines_for(var[k], cases) for k in VALUE_VARIANTS if k in var}
        trip = eval_cases(binc, binr, cases)
        exposing = []
        for i, (row, cl, rl) in enumerate(trip):
            if cl in (None, "__PARSE__", "__TIMEOUT__"):
                continue
            if rl is None or rl == "__PANIC__":
                exposing.append((i, row, cl, rl))
        exposing.sort(key=lambda t: sum(abs(x) for x in t[1]))
        recs = []
        for i, row, cl, rl in exposing:
            rec = full_rule(var, values, i, row, cl, eq)
            rec["rust"] = "panic" if rl == "__PANIC__" else "no output (process died)"
            recs.append(rec)
        passing = [r for r in recs if r["passes_full_rule"]]
        char = {lab: harness_counts(kind, func, pre) for lab, pre in
                (("default", None), ("fsigned_char", wrappers["-fsigned-char"]),
                 ("funsigned_char", wrappers["-funsigned-char"]))}
        return {"func": func, "defect_id": defect_id, "harness": kind, "status": "checked",
                "grid_cases": len(cases), "crash_exposing_inputs": len(recs),
                "passing_full_rule": len(passing),
                "failing_by_check": {
                    "gcc_san": sum(1 for r in recs if str(r["gcc_san_O0"]).startswith("ub")),
                    "clang_fullsan": sum(1 for r in recs if str(r["clang_fullsan_O0"]).startswith("ub")
                                         or str(r["clang_fullsan_O2"]).startswith("ub")),
                    "msan": sum(1 for r in recs if str(r["clang_msan_O1"]).startswith("ub")),
                    "msan_infra": sum(1 for r in recs if r["clang_msan_O1"] == "infra"),
                    "opt_levels_disagree": sum(1 for r in recs if not r["opt_levels_agree"]),
                    "char_signedness_disagree": sum(1 for r in recs if not r["char_signedness_agree"])},
                "variant_build_errors": errs,
                "smallest_passing": passing[0] if passing else None,
                "non_passing_examples": [r for r in recs if not r["passes_full_rule"]][:3],
                "harness_counts_by_char_signedness": char,
                "char_signedness_changes_harness_counts": len({json.dumps(v, sort_keys=True) for v in char.values()}) > 1,
                "all_checked": recs}


def wrong_job(args):
    kind, func, clean_inputs = args
    with tempfile.TemporaryDirectory() as d:
        binc, binr, cfile, cases, eq, is_float, _ = ub.prepare(kind, func, d)
        if not binc or not binr:
            return {"func": func, "status": "build-fail"}
        var, errs = build_all(cfile, d)
        values = {k: ub.lines_for(var[k], cases) for k in VALUE_VARIANTS if k in var}
        rl_all = ub.lines_for(binr, cases)
        index = {json.dumps(r): i for i, r in enumerate(cases)}
        tried = []
        for inp in clean_inputs:
            i = index.get(json.dumps(inp["in"]))
            if i is None:
                continue
            rec = full_rule(var, values, i, inp["in"], inp["c"], eq)
            rec["rust"] = rl_all[i] if i < len(rl_all) else None
            rec["still_diverges"] = rec["rust"] is not None and not eq(rec["rust"], inp["c"])
            rec["passes_full_rule"] = rec["passes_full_rule"] and rec["still_diverges"]
            tried.append(rec)
            if rec["passes_full_rule"]:
                break
        ok = [r for r in tried if r["passes_full_rule"]]
        return {"func": func, "harness": kind, "status": "checked", "clean_inputs_available": len(clean_inputs),
                "tried": len(tried), "first_passing": ok[0] if ok else None, "variant_build_errors": errs,
                "tried_records": tried}


def provenance(cmd_mode):
    def ver(c):
        return subprocess.run(c, stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout.decode().strip()
    return {"script": "systems/vert/toolchain_crash_recheck.py", "mode": cmd_mode,
            "command": "RUSTUP_TOOLCHAIN=%s python3 systems/vert/toolchain_crash_recheck.py %s" % (
                os.environ.get("RUSTUP_TOOLCHAIN"), " ".join(sys.argv[1:])),
            "git_rev": os.environ.get("GIT_REV"), "host": os.environ.get("HOST_DESC"),
            "arch": platform.machine(), "image": os.environ.get("IMAGE_DESC"),
            "toolchains": {"rustc": ver(["rustc", "-V"]), "gcc": ver(["gcc", "--version"]).splitlines()[0],
                           "clang": ver([CLANG, "--version"]).splitlines()[0]},
            "full_rule": __doc__.split("FULL RULE")[1].split("Modes")[0].strip(),
            "date_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["crash", "wrong"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--max-tries", type=int, default=12)
    a = ap.parse_args()
    assert shutil.which("clang") is None and shutil.which(CLANG)
    t0 = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if a.mode == "crash":
        wroot = tempfile.mkdtemp(prefix="gccwrap_")
        wrappers = {fl: ub.make_gcc_wrapper(wroot, fl) for fl in ("-fsigned-char", "-funsigned-char")}
        with mp.Pool(a.jobs) as pool:
            res = pool.map(crash_job, [(k, f, did, wrappers) for k, f, did in CRASH_TARGETS], chunksize=1)
        summary = {
            "defects": len(res),
            "with_passing_exposing_input": [r["defect_id"] for r in res if r.get("passing_full_rule", 0) > 0],
            "without_passing_exposing_input": [r["defect_id"] for r in res if r.get("passing_full_rule", 0) == 0],
            "char_signedness_changes_harness_counts": [r["defect_id"] for r in res if r.get("char_signedness_changes_harness_counts")],
            "per_defect": {r["defect_id"]: {
                "func": r["func"], "harness": r.get("harness"), "crash_exposing_inputs": r.get("crash_exposing_inputs"),
                "passing_full_rule": r.get("passing_full_rule"), "failing_by_check": r.get("failing_by_check"),
                "smallest_passing": ({k: r["smallest_passing"][k] for k in ("in", "c", "rust")}
                                     if r.get("smallest_passing") else None),
                "harness_rust_panic_by_char": {k: v.get("rust_panic") for k, v in r.get("harness_counts_by_char_signedness", {}).items()},
            } for r in res}}
        out = {"provenance": {**provenance("crash"), "date_start_utc": t0}, "summary": summary, "functions": res}
        json.dump(out, open(a.out, "w"), indent=2)
        print(json.dumps(summary, indent=1))
        return
    # mode wrong: supplement the wrong-answer file in place
    base = json.load(open(a.out))
    jobs = []
    for r in base["functions"]:
        clean = [c for c in r.get("all_checked", []) if c.get("clean")][:a.max_tries]
        jobs.append((r["harness"], r["func"], clean))
    with mp.Pool(a.jobs) as pool:
        res = pool.map(wrong_job, jobs, chunksize=1)
    sup = {"provenance": {**provenance("wrong"), "date_start_utc": t0,
                          "note": "walks each function's clean inputs (rule of this file) smallest first, at most "
                                  f"{a.max_tries}, and stops at the first that passes the FULL rule"},
           "summary": {"functions": len(res),
                       "with_input_passing_full_rule": sum(1 for r in res if r.get("first_passing")),
                       "without_input_passing_full_rule": [r["func"] for r in res if not r.get("first_passing")],
                       "first_passing": {r["func"]: ({k: r["first_passing"][k] for k in ("in", "c", "rust")}
                                                     if r.get("first_passing") else None) for r in res}},
           "functions": res}
    base["full_rule_supplement"] = sup
    json.dump(base, open(a.out, "w"), indent=2)
    print(json.dumps(sup["summary"], indent=1))


if __name__ == "__main__":
    main()
