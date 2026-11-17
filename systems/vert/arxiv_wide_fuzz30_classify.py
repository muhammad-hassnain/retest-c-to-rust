"""arXiv (issues.md 47): classify the 30 s fuzz runs of VERT's real wide-int translations and assemble
results/arxiv_vert_wide_fuzz30.json, in the format of results/arxiv_vert_fuzz30_real.json.

The fuzz step is systems/vert/bolero_vacuity/run_in_container_fuzz_real.sh + check_fuzz_real.py, unchanged,
started with FUNCS_OVERRIDE=<the wide-int value bugs>, OUTDIR and BOLERO_LOGDIR set. This script does what
fuzz_real_classify.py's main() does (it imports check_input, libfuzzer_stop and ledger_match from it,
unchanged: the failing input re-run on the full arguments, the full UB rule, the primary harness's verdict,
the C at the rWasm oracle's arguments, the study domain 0..512), with two differences:
  * the expected row set is the wide-int value bugs of results/arxiv_vert_diff_grid2_wide.json (run.py
    --wide-int) minus those the full UB rule excludes (results/arxiv_vert_wide_ub_recheck.json), not the 28
    rows of results/vert_bolero_vacuity_current.json;
  * these functions have no rows in results/defect_ledger.json yet, so ledger_rows are the planned rows
    (defect id VERT-<F>-1, severity S1, marked planned).
The C side is run.build_c's driver, which prints (long long) f_gold(...), so "rust_differs_from_c" compares the
full C value with VERT's i32 result widened to i64 (the --wide-int comparison).

Run in crust-vert-toolchain(-x86) from the repo root (RUSTUP_TOOLCHAIN=1.86.0):
  python3 -I systems/vert/arxiv_wide_fuzz30_classify.py <work_dir> <out.json> <provenance.json>
"""
import datetime
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(HERE, "bolero_vacuity"))
import fuzz_real_classify as frc   # noqa: E402  (unchanged; inserts systems/vert, systems, harness, scripts on sys.path)
from provenance import stamp       # noqa: E402

SCALAR = "results/arxiv_vert_diff_grid2_wide.json"
UB = "results/arxiv_vert_wide_ub_recheck.json"


def planned_ledger_rows(func):
    return [{"defect_id": f"VERT-{func}-1", "severity": "S1", "planned": True,
             "note": "planned counted row (wide-int return); not yet in results/defect_ledger.json"}]


def main():
    work, out_path, prov_path = sys.argv[1], sys.argv[2], sys.argv[3]
    rows = []
    for p in sorted(glob.glob(os.path.join(work, "part*.json"))):
        d = json.load(open(p))
        assert d["partial"] is False, p
        rows += d["rows"]
    rows.sort(key=lambda r: r["function"])
    vd = json.load(open(os.path.join(REPO, SCALAR)))
    ubx = set(json.load(open(os.path.join(REPO, UB)))["summary"]["without_clean_input_where_clang_also_agrees"])
    expect = [v["func"] for v in vd["value_bugs"] if v["func"] not in ubx]
    fs = [r["function"] for r in rows]
    assert sorted(fs) == sorted(expect) and len(fs) == len(set(fs)), (len(fs), set(expect) - set(fs))
    logdir = os.environ.get("FUZZ_LOGS", os.path.join(REPO, "results", "arxiv_vert_wide_fuzz30_logs"))
    for r in rows:
        r["ledger_rows"] = planned_ledger_rows(r["function"])
        r["c_return"] = next(v.get("c_return") for v in vd["all"] if v["func"] == r["function"])
        nargs = len(re.findall(r"PARAM_\d+", (r.get("harness_line") or "").split("for_each")[-1]))
        for a in r["attempts"] + ((r.get("diagnostic_crate_rename") or {}).get("attempts") or []):
            if a["outcome"] not in ("test_failure", "compile_error", "startup_crash") and a.get("log"):
                a["libfuzzer_stop"] = frc.libfuzzer_stop(os.path.join(logdir, a["log"]), nargs)
        r["final"]["libfuzzer_stop"] = r["attempts"][-1].get("libfuzzer_stop")
        for key, blk in (("final", r["final"]), ("rename_diag", (r.get("diagnostic_crate_rename") or {}).get("final"))):
            if not blk or not blk.get("failure") or "input" not in blk["failure"]:
                continue
            fi = blk["failure"]
            chk = frc.check_input(r["function"], fi["input"])
            if "left_rust" in fi and chk.get("rust_value_rustc_O") is not None:
                chk["bolero_left_equals_rust_rerun"] = fi["left_rust"] == chk["rust_value_rustc_O"]
            if "right_oracle" in fi:
                ref = (chk.get("oracle_args") or {}).get("c_value_at_oracle_args", chk.get("c_value_full_args_gcc_O1_wrapv"))
                chk["bolero_right_equals_c_at_oracle_args"] = fi["right_oracle"] == ref
            blk["input_check"] = chk
            blk["ledger_match"] = frc.ledger_match(r["function"], blk, r["ledger_rows"])
            print(r["function"], key, fi["input"], {k: chk.get(k) for k in ("in_study_domain", "c_value_full_args_gcc_O1_wrapv",
                  "rust_value_rustc_O", "ub_clean_full_rule", "primary_harness_verdict")}, flush=True)

    def cnt(xs):
        c = {}
        for x in xs:
            c[x] = c.get(x, 0) + 1
        return dict(sorted(c.items(), key=lambda kv: str(kv[0])))
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
        "per_function": {r["function"]: {
            "final_outcome": r["final"]["outcome"], "readme_verdict": r["final"]["readme_verdict"],
            "failure_kind": r["final"]["failure_kind"],
            "failing_input": (r["final"].get("failure") or {}).get("input"),
            "ub_clean_full_rule": (r["final"].get("input_check") or {}).get("ub_clean_full_rule"),
            "rust_differs_from_c": (r["final"].get("input_check") or {}).get("rust_differs_from_c"),
            "in_study_domain": (r["final"].get("input_check") or {}).get("in_study_domain"),
            "ledger_match": (r["final"].get("ledger_match") or {}).get("status")} for r in rows},
    }
    prov = json.load(open(prov_path))
    tc = os.path.join(work, "container_toolchains.json")
    if os.path.exists(tc):
        prov["fuzz_container_toolchains"] = json.load(open(tc))
    oub = frc.oub
    prov["classify_step"] = {"script": "systems/vert/arxiv_wide_fuzz30_classify.py (fuzz_real_classify.check_input / libfuzzer_stop / ledger_match, unchanged)",
                             "argv": sys.argv, "date_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                             "toolchains": oub.toolchains() | {"clang-14": oub.tool_version(["clang-14", "--version"])},
                             "sanitizer_flags": {"clang": oub.san_cflags("clang") + oub.san_ldflags("clang"),
                                                 "gcc": oub.san_cflags("gcc") + oub.san_ldflags("gcc"), "env": oub.SAN_ENV},
                             "sanitizer_positive_controls": frc.gm.positive_controls_ok()}
    res = {"provenance": prov, "summary": summ, "rows": rows}
    res["_provenance"] = stamp(image=os.environ.get("CRUST_IMAGE"), step="vert.arxiv_wide_fuzz30 (classify)")
    with open(out_path, "w") as f:
        json.dump(res, f, indent=1)
        f.write("\n")
    print(json.dumps({k: v for k, v in summ.items() if k != "per_function"}, indent=1))


if __name__ == "__main__":
    main()
