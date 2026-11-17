"""Classify the failing inputs of check_fuzz_real.py and assemble results/arxiv_vert_fuzz30_real.json.

For every function whose 30 s fuzz run of VERT's real translation ended in "Test Failure", the input
bolero printed is re-run outside VERT's harness:
  * the LLM's Rust f_gold (rustc -O, systems/vert/run.py build_rust) and the ORIGINAL C f_gold on the
    FULL argument tuple (gcc -O1 -fwrapv, run.py build_c: the study's primary oracle);
  * the full undefined-behaviour rule used for every counted wrong answer (harness/oracle_ub_common.py
    flags; the per-input check of systems/vert/grid_misses.py full_check: clang-14 and gcc-11
    ASan+UBSan+float-cast-overflow -O0 (+clang -O1), clang MSan -O1, gcc/clang -O0/-O1/-O2 with and
    without -fwrapv, -fsigned-char/-funsigned-char; the input is UB-clean only if every run completes,
    reports nothing and prints the reference value);
  * the primary harness's own classifier for that one input (batch_diff.tally via grid_misses.prepare);
  * the C on the arguments VERT's rWasm oracle actually used: PARAM1 plus the constants of the C stub
    the oracle was built from (`f_gold(...)` in <F>_towasm_mutated.c; the oracle reads PARAM1 only),
    to explain the harness's right-hand value;
  * whether the input lies in the domain the study tests ([0, 512] per argument, run.py), and which
    results/defect_ledger.json VERT row(s) exist for the function.

Run in crust-vert-toolchain(-x86) from the repo root (RUSTUP_TOOLCHAIN=1.86.0, as grid_misses.py):
  python3 -I systems/vert/bolero_vacuity/fuzz_real_classify.py <work_dir> <out.json> <provenance.json>
<work_dir> holds part*.json and container_toolchains.json from run_in_container_fuzz_real.sh.
"""
import datetime, glob, json, os, re, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(REPO, "systems", "vert"))
sys.path.insert(0, os.path.join(REPO, "systems"))
sys.path.insert(0, os.path.join(REPO, "harness"))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import grid_misses as gm                 # noqa: E402
import oracle_ub_common as oub           # noqa: E402
from provenance import stamp             # noqa: E402

TC = os.path.join(REPO, "artifacts", "vert", "benchmark", "c_transcoder")
LO, HI = 0, 512


def stub_args(func):
    s = open(os.path.join(TC, func, f"{func}_towasm_mutated.c"), errors="replace").read()
    m = re.findall(r"f_gold\s*\(([^)]*)\)\s*;", s[s.find("int main"):])
    return [int(x) for x in m[-1].split(",")] if m else None


def ledger_rows(func):
    led = json.load(open(os.path.join(REPO, "results", "defect_ledger.json")))
    return [{k: r.get(k) for k in ("defect_id", "severity", "example_input", "c_output", "rust_output", "mechanism")}
            | {"ub_rule_status": (r.get("ub_rule") or {}).get("status")}
            for r in led["rows"] if r.get("system") == "VERT" and r["translation"].split(" ")[0] == func]


def libfuzzer_stop(logpath, nargs):
    """Why libFuzzer stopped when bolero printed no 'Test Failure' (out-of-memory, timeout, ...): its
    ERROR/SUMMARY lines and the offending input bytes. The bytes are also decoded as consecutive
    little-endian i32 values (bolero's byte-slice driver for with_type::<(i32, ...)>; an out-of-memory
    row cross-checks this against the malloc size it reports)."""
    if not os.path.exists(logpath):
        return None
    log = open(logpath, errors="replace").read()
    err = re.findall(r"==\d+== ERROR: libFuzzer: ([^\n]*)", log)
    summ = re.findall(r"SUMMARY: libFuzzer: ([^\n]*)", log)
    alarm = re.findall(r"ALARM: working on the last Unit for (\d+) seconds", log)
    b64 = re.findall(r"Base64: ([A-Za-z0-9+/=]*)", log)
    if not (err or summ):
        return None
    r = {"libfuzzer_error": err, "libfuzzer_summary": summ, "alarm_seconds": alarm or None,
         # bolero prints this when the closure panicked on the unit and it starts shrinking (inside the
         # same libFuzzer unit); 'Test Failure' is printed only after shrinking ends
         "bolero_shrinking_started": "test failed; shrinking input" in log,
         "input_base64": b64[-1] if b64 else None}
    if b64:
        import base64
        raw = base64.b64decode(b64[-1])
        r["input_bytes_hex"] = raw.hex()
        pad = raw + b"\0" * max(0, 4 * nargs - len(raw))
        r["input_decoded_le_i32"] = [int.from_bytes(pad[4 * i:4 * i + 4], "little", signed=True) for i in range(nargs)]
        r["input_bytes_short_padded_with_zeros"] = len(raw) < 4 * nargs
    m = re.search(r"out-of-memory \(malloc\((\d+)\)\)", log)
    if m:
        r["malloc_bytes"] = int(m.group(1))
    return r


def check_input(func, row):
    with tempfile.TemporaryDirectory() as d:
        binc, binr, cfile, bounds, eq, primary, meta = gm.prepare(func, {"kind": "scalar"}, d)
        if not binc or not binr:
            return {"status": "build_fail"}
        nargs = meta["nargs"]
        if len(row) != nargs:
            return {"status": f"input has {len(row)} values, f_gold takes {nargs}"}
        c_val, c_rc, c_to = gm.run_in(binc, row)
        r_val, r_rc, r_to = gm.run_in(binr, row)
        var, var_err = gm.variants(cfile, d)
        fc = gm.full_check(row, c_val, var, eq) if c_val not in (None, "__PARSE__") and not c_to else {"passes": False, "note": "C produced no value"}
        if "note" in fc:
            # the C gave no value (crash or hang): record what the sanitizer builds say on this input
            for k in ("gcc_asan_ubsan_fco_O0", "clang_asan_ubsan_fco_O0"):
                if k in var:
                    s = oub.run_san([var[k]], (" ".join(str(x) for x in row) + "\n").encode())
                    fc[k] = {"rc": s["rc"], "timed_out": s["timed_out"], "infra": s["infra"],
                             "report": s["report"], "reports": s["reports"][:4]}
        res = {"input": row, "in_study_domain": all(LO <= x <= HI for x in row),
               "c_value_full_args_gcc_O1_wrapv": c_val, "c_rc": c_rc, "c_timeout": c_to,
               "rust_value_rustc_O": r_val, "rust_timeout": r_to,
               "rust_differs_from_c": (c_val != r_val) if c_val is not None else None,
               "ub_clean_full_rule": bool(fc.get("passes")), "full_rule_checks": fc,
               "variant_build_errors": var_err,
               "primary_harness_verdict": primary(row)}
        st = stub_args(func)
        if nargs > 1 and st and len(st) == nargs:
            orow = [row[0]] + st[1:]
            ov, _, oto = gm.run_in(binc, orow)
            ofc = gm.full_check(orow, ov, var, eq) if ov not in (None, "__PARSE__") and not oto else {"passes": False}
            res["oracle_args"] = {"stub_call": st, "args_used_by_rwasm_oracle": orow,
                                  "c_value_at_oracle_args": ov, "ub_clean_at_oracle_args": bool(ofc.get("passes")),
                                  "rust_value_at_full_args_equals_c_at_full_args": c_val == r_val}
        return res


def count_distinct_exact(n):
    """COUNT_DISTINCT_NON_NEGATIVE_PAIRS_X_Y_SATISFY_INEQUALITY_XX_YY_N_2_1: the C's loop, and the Rust's
    loop in exact integer arithmetic (its floor(sqrt(n)) start, no f32), to see whether the ledger's
    mechanism alone gives the fuzzer's Rust value."""
    def run(y):
        x = res = 0
        while y != 0:
            res += y; x += 1
            while y != 0 and x * x + (y - 1) * (y - 1) >= n:
                y -= 1
        return res
    yc = 0
    while yc * yc < n:
        yc += 1
    import math
    return {"c": run(yc), "rust_logic_exact_integers": run(math.isqrt(n))}


# Reading of the C and the Rust at the failing input (facts checked against the files named in
# source_location of each ledger row); used only for rows whose input is UB-clean and where the
# Rust differs from the C on the full arguments, or where it does not differ.
MECHANISM_NOTES = {
    "CHECK_IF_A_NUMBER_IS_JUMBLED_OR_NOT": "21 is the ledger row's example input (C 1, Rust 0): the in-loop `num / 10 == 0` guard is missing.",
    "NTH_PENTAGONAL_NUMBER": "3*n*n - n/2 instead of (3*n*n - n)/2 (ledger mechanism): 3*256*256 - 128 = 196480, C (3*256*256 - 256)/2 = 98176.",
    "PROGRAM_TO_CHECK_IF_A_GIVEN_NUMBER_IS_LUCKY_ALL_DIGITS_ARE_DIFFERENT": "f32 digit loop returns 0 for every n > 0 (ledger mechanism); 256 has distinct digits, C 1.",
    "HEIGHT_COMPLETE_BINARY_TREE_HEAP_N_NODES": "floor instead of ceil of log2(N+1) (ledger mechanism): N+1 = 2^24+1 is not a power of two; C ceil(log2(16777217)) - 1 = 24, floor - 1 = 23.",
    "NTH_NON_FIBONACCI_NUMBER": "sign flip `n = n + (curr - prev - 1)` (ledger mechanism): n never decreases, the loop ends when n wraps; here it returns 1664239616 (1647462400 on the in-domain inputs).",
    "COUNT_DISTINCT_NON_NEGATIVE_PAIRS_X_Y_SATISFY_INEQUALITY_XX_YY_N_2_1": "floor(sqrt(n)) start instead of the C's smallest y with y*y >= n (ledger mechanism); the Rust's loop in exact integer arithmetic gives the same value as the fuzzed Rust (see exact_recomputation), so f32 rounding plays no part.",
    "NUMBER_RECTANGLES_NM_GRID": "no divergence: Rust and C agree on (16777216, 0); the rWasm oracle reads PARAM1 only and computed f_gold(16777216, 29) (the C stub's second argument).",
    "NUMBER_WHICH_HAS_THE_MAXIMUM_NUMBER_OF_DISTINCT_PRIME_FACTORS_IN_RANGE_M_TO_N": "no divergence: Rust and C agree on (0, 0); the rWasm oracle reads PARAM1 only and computed f_gold(0, 29) = 6.",
}


def ledger_match(func, blk, ledger):
    c = blk.get("input_check") or {}
    if not c or "ub_clean_full_rule" not in c:
        return None
    ids = {r["severity"]: r["defect_id"] for r in ledger}
    rust_panics = c.get("rust_value_rustc_O") == "__PANIC__"
    m = {"oracle_trapped": blk.get("failure_kind") == "panic_in_rwasm_oracle",
         "note": MECHANISM_NOTES.get(func)}
    if not c["ub_clean_full_rule"]:
        m["status"] = "c_not_ub_clean"
        m["matches_ledger_defect"] = None
        m["why"] = "the input fails the UB rule (sanitizer report, C crash/hang, or opt-level / char-signedness disagreement), so a divergence there is not countable"
    elif not c.get("rust_differs_from_c"):
        m["status"] = "no_divergence_from_c"
        m["matches_ledger_defect"] = None
    elif c.get("in_study_domain"):
        m["status"] = "ledger_defect_in_domain"
        m["matches_ledger_defect"] = ids.get("crash") if rust_panics else ids.get("S1")
    else:
        m["status"] = "ledger_function_outside_domain"
        m["matches_ledger_defect"] = None
        m["same_mechanism_as"] = ids.get("crash") if rust_panics else ids.get("S1")
        m["why"] = "UB-clean divergence outside [0, 512] (the domain the study counts); same function and mechanism as the ledger row named in same_mechanism_as"
    if func == "COUNT_DISTINCT_NON_NEGATIVE_PAIRS_X_Y_SATISFY_INEQUALITY_XX_YY_N_2_1" and c.get("input"):
        m["exact_recomputation"] = count_distinct_exact(c["input"][0])
    return m


def shipped_exit1(logdir, real_rows):
    """The fuzz_30s runs of results/vert_bolero_vacuity_current.json (planted copies) that exited 1
    without 'Test Failure': which cargo-bolero step failed, read from the full log. cargo-bolero 0.9
    (VERT's fork) runs (1) cargo test --no-run (build), (2) the test binary through cargo test with
    CARGO_BOLERO_SELECT=one (it only prints the target; the check!() closure is not run), whose captured
    stderr it prints to stdout on failure followed by 'process exited with status <cargo's code>', and
    (3) the test binary itself under libFuzzer, where a signal gives 'process exited with no status code'."""
    shipped = json.load(open(os.path.join(REPO, "results", "vert_bolero_vacuity_current.json")))
    out = []
    for r in shipped["rows"]:
        a = r["arms"]["fuzz_30s"]
        if a.get("outcome") != "exit_1_no_test_failure_text":
            continue
        log = open(os.path.join(logdir, r["function"] + ".fuzz_30s.log"), errors="replace").read()
        sel = re.search(r"process didn't exit successfully: `[^`]*bolero_wasm_eq --nocapture --exact` \(([^)]*)\)", log)
        step = ("selection step: test binary died (" + sel.group(1) + "); cargo-bolero: " +
                ", ".join(re.findall(r"process exited with [^\n]*", log)) if sel else
                "fuzzing step: test binary killed by a signal ('process exited with no status code')"
                if "process exited with no status code" in log else "other")
        rr = real_rows.get(r["function"])
        out.append({"function": r["function"], "log": "results/vert_bolero_logs_current/" + r["function"] + ".fuzz_30s.log",
                    "failed_step": step, "test_binary_printed_anything": ("running 1 test" in log or "INFO: Seed:" in log),
                    "libfuzzer_started": "INFO: Seed:" in log, "test_failure_text": "Test Failure" in log,
                    "real_translation_run_attempt_outcomes": [x["outcome"] for x in rr["attempts"]] if rr else None})
    return {"count": len(out), "by_step": {k: sum(1 for o in out if o["failed_step"].startswith(k))
                                           for k in ("selection step", "fuzzing step", "other")}, "rows": out}


def main():
    work, out_path, prov_path = sys.argv[1], sys.argv[2], sys.argv[3]
    rows = []
    for p in sorted(glob.glob(os.path.join(work, "part*.json"))):
        d = json.load(open(p))
        assert d["partial"] is False, p
        rows += d["rows"]
    rows.sort(key=lambda r: r["function"])
    expect = [r["function"] for r in json.load(open(os.path.join(REPO, "results", "vert_bolero_vacuity_current.json")))["rows"]]
    fs = [r["function"] for r in rows]
    assert sorted(fs) == sorted(expect) and len(fs) == len(set(fs)) == 28, (len(fs), set(expect) - set(fs))
    logdir = os.environ.get("FUZZ_LOGS", os.path.join(REPO, "results", "arxiv_vert_fuzz30_logs"))
    for r in rows:
        r["ledger_rows"] = ledger_rows(r["function"])
        nargs = len(re.findall(r"PARAM_\d+", (r.get("harness_line") or "").split("for_each")[-1]))
        for a in r["attempts"] + ((r.get("diagnostic_crate_rename") or {}).get("attempts") or []):
            if a["outcome"] not in ("test_failure", "compile_error", "startup_crash") and a.get("log"):
                a["libfuzzer_stop"] = libfuzzer_stop(os.path.join(logdir, a["log"]), nargs)
        r["final"]["libfuzzer_stop"] = r["attempts"][-1].get("libfuzzer_stop")
        for key, blk in (("final", r["final"]), ("rename_diag", (r.get("diagnostic_crate_rename") or {}).get("final"))):
            if not blk or not blk.get("failure") or "input" not in blk["failure"]:
                continue
            fi = blk["failure"]
            chk = check_input(r["function"], fi["input"])
            if "left_rust" in fi and chk.get("rust_value_rustc_O") is not None:
                chk["bolero_left_equals_rust_rerun"] = fi["left_rust"] == chk["rust_value_rustc_O"]
            if "right_oracle" in fi:
                ref = (chk.get("oracle_args") or {}).get("c_value_at_oracle_args", chk.get("c_value_full_args_gcc_O1_wrapv"))
                chk["bolero_right_equals_c_at_oracle_args"] = fi["right_oracle"] == ref
            blk["input_check"] = chk
            blk["ledger_match"] = ledger_match(r["function"], blk, r["ledger_rows"])
            print(r["function"], key, fi["input"], {k: chk.get(k) for k in ("in_study_domain", "c_value_full_args_gcc_O1_wrapv",
                  "rust_value_rustc_O", "ub_clean_full_rule", "primary_harness_verdict")}, flush=True)

    def cnt(xs):
        c = {}
        for x in xs:
            c[x] = c.get(x, 0) + 1
        return dict(sorted(c.items()))
    fails = [r for r in rows if r["final"]["outcome"] == "test_failure"]
    summ = {
        "functions": len(rows),
        "all_projects_unmodified": all(r["unmodified"] for r in rows),
        "first_attempt_outcomes": cnt(r["attempts"][0]["outcome"] for r in rows),
        "final_outcomes": cnt(r["final"]["outcome"] for r in rows),
        "attempts_per_function": cnt(len(r["attempts"]) for r in rows),
        "first_attempt_readme_verdict": cnt(r["first_attempt"]["readme_verdict"] for r in rows),
        "first_attempt_torust_verdict": cnt(r["first_attempt"]["torust_verdict"] for r in rows),
        "final_readme_verdict": cnt(r["final"]["readme_verdict"] for r in rows),
        "final_failure_kinds": cnt(r["final"]["failure_kind"] for r in fails),
        "test_failure_functions": [r["function"] for r in fails],
        "not_failing_functions": {r["function"]: r["final"]["outcome"] for r in rows if r["final"]["outcome"] != "test_failure"},
        "failing_inputs_ub_clean": sum(bool(r["final"].get("input_check", {}).get("ub_clean_full_rule")) for r in fails),
        "failing_inputs_in_study_domain": sum(bool(r["final"].get("input_check", {}).get("in_study_domain")) for r in fails),
        "failing_inputs_rust_differs_from_c_full_args": sum(bool(r["final"].get("input_check", {}).get("rust_differs_from_c")) for r in fails),
        "failing_inputs_by_ledger_match": cnt((r["final"].get("ledger_match") or {}).get("status") for r in fails),
        "failing_inputs_by_ledger_match_functions": {st: [r["function"] for r in fails if (r["final"].get("ledger_match") or {}).get("status") == st]
                                                     for st in sorted({(r["final"].get("ledger_match") or {}).get("status") for r in fails}, key=str)},
        "failing_inputs_where_rwasm_oracle_trapped": [r["function"] for r in fails if r["final"]["failure_kind"] == "panic_in_rwasm_oracle"],
        "first_attempt_startup_crashes": sum(r["attempts"][0]["outcome"] == "startup_crash" for r in rows),
        "startup_crash_attempts_total": sum(a["outcome"] == "startup_crash" for r in rows for a in r["attempts"]),
        "attempts_total": sum(len(r["attempts"]) for r in rows),
        "crate_rename_diagnostic": {r["function"]: (r["diagnostic_crate_rename"]["final"]["outcome"],
                                                    (r["diagnostic_crate_rename"]["final"].get("failure") or {}).get("input_text"))
                                    for r in rows if r.get("diagnostic_crate_rename")},
    }
    prov = json.load(open(prov_path))
    tc = os.path.join(work, "container_toolchains.json")
    if os.path.exists(tc):
        prov["fuzz_container_toolchains"] = json.load(open(tc))
    prov["classify_step"] = {"script": "systems/vert/bolero_vacuity/fuzz_real_classify.py",
                             "argv": sys.argv, "date_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                             "toolchains": oub.toolchains() | {"clang-14": oub.tool_version(["clang-14", "--version"])},
                             "sanitizer_flags": {"clang": oub.san_cflags("clang") + oub.san_ldflags("clang"),
                                                 "gcc": oub.san_cflags("gcc") + oub.san_ldflags("gcc"), "env": oub.SAN_ENV},
                             "sanitizer_positive_controls": gm.positive_controls_ok()}
    res = {"provenance": prov, "summary": summ, "rows": rows}
    # optional: why the shipped planted fuzz_30s runs exited 1 without "Test Failure"
    sl = os.environ.get("SHIPPED_LOGS", os.path.join(REPO, "results", "vert_bolero_logs_current"))
    if os.path.isdir(sl):
        res["shipped_fuzz_30s_exit1_runs"] = shipped_exit1(sl, {r["function"]: r for r in rows})
    sd = os.environ.get("STARTUP_DIAG")
    if sd and os.path.exists(sd):
        res["diagnostic_startup_crash"] = json.load(open(sd))
    res["_provenance"] = stamp(image=os.environ.get("CRUST_IMAGE"), step="vert.arxiv_fuzz30_real (classify)")
    with open(out_path, "w") as f:
        json.dump(res, f, indent=1)
        f.write("\n")
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()
