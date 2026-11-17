"""Do the two Kani result files use exactly {current 25 scalar wrong answers} ∩ {bounded_kani=1 in VERT's
c_results.csv}? Re-derives every verdict from the saved full logs (same regexes as check.py /
unwinding_on.py) and fingerprints each log against its own function's harness source, so a log
produced from another function's model (the audit-5 stale-model bug) would be caught. Also adds the
"diverges outside {0,1}" check from systems/vert/zero_one_current.py's per-input output.

  python3 systems/vert/kani_vacuity/set_check_current.py <zero_one.json> <zero_one_provenance.json> OUT.json
Run from the repo root. Reads only; writes OUT.json.
"""
import datetime, json, os, platform, re, subprocess, sys

ZO, ZOPROV, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
B = "artifacts/vert/benchmark/c_transcoder"
J = lambda p: json.load(open(p))
vd = J("results/vert_diff.json")
cur = [v["func"] for v in vd["value_bugs"]]
assert len(cur) == 25 and set(cur) == set(vd["summary"]["value_bug_functions"])
csv = {}
for line in open("artifacts/vert/c_results.csv"):
    p = [x.strip() for x in line.split(",")]
    if p and p[0]:
        csv[p[0]] = p
bk = {f for f, p in csv.items() if len(p) >= 4 and p[3] == "bounded_kani=1"}
expected = sorted(set(cur) & bk)

kv, ku = J("results/vert_kani_vacuity.json"), J("results/vert_kani_unwinding_on.json")
kv_rows = {r["function"]: r for r in kv["rows"]}
ku_rows = {r["function"]: r for r in ku["rows"]}
kv_flagged = {f for f, r in kv_rows.items() if r["in_scalar_wrong_answers"]}


def src_lines(f):
    return open(os.path.join(B, f, "out-rwasm-mutated", "src", "main.rs"), errors="replace").read().split("\n")


def fgold_span(lines):
    s = next(i for i, l in enumerate(lines) if re.match(r"\s*fn f_gold\s*\(", l))
    e = next(i for i in range(s + 1, len(lines)) if re.match(r"\s*(fn |#\[kani::proof\])", lines[i]))
    return s + 1, e  # 1-based first line, last line


def parse_bounded(f):
    # the committed logs are extracts: a '# extract ...' header line, then verdict/summary/assertion/errors
    log = open(f"results/kani_logs/{f}.log").read()
    body = "\n".join(l for l in log.split("\n") if not l.startswith("# extract")).strip()
    verdict = ("FAILED" if "VERIFICATION:- FAILED" in log else "SUCCESSFUL" if "VERIFICATION:- SUCCESSFUL" in log
               else "COMPILE_ERROR" if re.search(r"^error(\[E\d+\])?:", log, re.M) else ("TIMEOUT_OR_EMPTY" if not body else "NO_VERDICT"))
    m = re.search(r'Status: (\w+)\n\s*- Description: "assertion failed: result == result_prime"\n\s*- Location: src/main.rs:(\d+):\d+', log)
    st, line = (m.group(1), int(m.group(2))) if m else (None, None)
    fp = None
    if line:
        L = src_lines(f)
        fp = "result_prime" in L[line - 1] and "assert" in L[line - 1]
    return {"verdict": verdict, "equivalence_assertion": st, "assert_line": line, "assert_line_matches_own_source": fp}


def parse_unwind(f):
    # committed logs are extracts (first 40 + last 120 lines); the per-check list for the equivalence
    # assertion is usually cut, so its status is derived from the SUMMARY line when every check that did
    # not fail is undetermined ("1 of N failed (N-1 undetermined)" with the one failure the unwinding check)
    log = open(f"results/kani_logs_unwind_on/{f}.log").read()
    verdict = "FAILED" if "VERIFICATION:- FAILED" in log else "SUCCESSFUL" if "VERIFICATION:- SUCCESSFUL" in log else "NO_VERDICT"
    failed = re.findall(r"Failed Checks: (.+)\n\s*File: \"([^\"]+)\", line (\d+), in (\S+)", log)
    m = re.search(r'Status: (\w+)\n\s*- Description: "assertion failed: result == result_prime"', log)
    L = src_lines(f)
    s, e = fgold_span(L)
    fails = []
    for c, path, n, fn in failed:
        own = path == "src/main.rs"
        fails.append({"check": c, "file": path if own else re.sub(r".*/library/", "<rust std>/library/", path), "line": int(n), "in_function": fn,
                      "line_in_own_f_gold": own and fn == "f_gold" and s <= int(n) <= e,
                      "source_line": L[int(n) - 1].strip()[:120] if own else None})
    sm = re.search(r"\*\* (\d+) of (\d+) failed \((\d+) undetermined\)", log)
    derived = None
    if sm:
        nf, nt, nu = map(int, sm.groups())
        if nf == len(failed) and nf + nu == nt and all("unwinding assertion" in x[0] for x in failed):
            derived = "UNDETERMINED"
    return {"verdict": verdict, "failed_checks": fails,
            "equivalence_assertion_in_extract": m.group(1) if m else None,
            "equivalence_assertion_derived_from_summary": derived,
            "summary_line": sm.group(0) if sm else None}


per14 = []
for f in expected:
    b, u = parse_bounded(f), parse_unwind(f)
    jb, ju = kv_rows.get(f), ku_rows.get(f)
    per14.append({"function": f, "bounded_kani_no_unwinding_checks": b, "unwinding_on": u,
                  "json_agrees_bounded": bool(jb) and jb["verdict"] == b["verdict"] and jb["equivalence_assertion"] == b["equivalence_assertion"],
                  "json_agrees_unwinding_on": bool(ju) and ju["verdict"] == u["verdict"]
                  and ju["failed_checks"] == [x["check"] for x in u["failed_checks"]]
                  and ju["equivalence_assertion"] == (u["equivalence_assertion_in_extract"] or u["equivalence_assertion_derived_from_summary"]),
                  "vacuous_pass": b["verdict"] == "SUCCESSFUL" and b["equivalence_assertion"] == "UNREACHABLE",
                  "fails_on_unwinding_assertion": u["verdict"] == "FAILED" and any("unwinding assertion" in x["check"] for x in u["failed_checks"]),
                  "equivalence_assertion_unwinding_on_json": (ju or {}).get("equivalence_assertion")})

# all 90: re-derive from logs, compare with the JSON, fingerprint
all90 = {}
for f in sorted(bk):
    b = parse_bounded(f)
    j = kv_rows[f]
    all90[f] = {**b, "json_verdict": j["verdict"], "json_assertion": j["equivalence_assertion"],
                "json_agrees": (j["verdict"] == b["verdict"] or (j["verdict"] == "TIMEOUT" and b["verdict"] == "TIMEOUT_OR_EMPTY"))
                and j["equivalence_assertion"] == b["equivalence_assertion"]}
vc = {}
for r in all90.values():
    vc[r["json_verdict"]] = vc.get(r["json_verdict"], 0) + 1

# ---- {0,1} domain ----
zo = J(ZO)
zrows = {r["function"]: r for r in zo["rows"]}
assert sorted(zrows) == sorted(cur)
out01 = lambda x: x not in (0, 1)
dom = []
for f in cur:
    r = zrows[f]
    conf = [x["in"] for x in r["per_input"] if x["primary_like"] == "value" and x["msan_like"] == "value"]
    prim = [x["in"] for x in r["per_input"] if x["primary_like"] == "value"]
    inside_grid = [x["in"] for x in r["per_input"] if all(not out01(a) for a in x["in"])]
    samples = [s["in"] for s in next(v for v in vd["value_bugs"] if v["func"] == f)["value_samples"]]
    pick = lambda L, pred: next((x for x in sorted(L, key=lambda t: (sum(abs(a) for a in t), t)) if pred(x)), None)
    dom.append({
        "function": f, "nargs": r["nargs"], "bounded_kani_1": f in bk, "grid_inputs": r["cases"],
        "value_div_vert_diff_json": r["vert_diff_value_div"],
        "value_div_rerun_primary_like": len(prim), "value_div_rerun_confirmed_both_filters": len(conf),
        "grid_inputs_inside_01k": len(inside_grid),
        "divergent_inputs_inside_01k": [x for x in conf if all(not out01(a) for a in x)],
        "diverges_with_PARAM1_outside_01": any(out01(x[0]) for x in conf),
        "diverges_with_some_arg_outside_01": any(any(out01(a) for a in x) for x in conf),
        "diverges_with_all_args_outside_01": any(all(out01(a) for a in x) for x in conf),
        "example_PARAM1_outside_01": pick(conf, lambda x: out01(x[0])),
        "example_all_args_outside_01": pick(conf, lambda x: all(out01(a) for a in x)),
        "vert_diff_samples_show_PARAM1_outside_01": any(out01(x[0]) for x in samples),
        "vert_diff_samples_show_some_arg_outside_01": any(any(out01(a) for a in x) for x in samples),
        "divergent_inputs_confirmed": conf})


def agg(rows):
    return {"functions": len(rows),
            "diverges_with_PARAM1_outside_01": sum(d["diverges_with_PARAM1_outside_01"] for d in rows),
            "diverges_with_some_arg_outside_01": sum(d["diverges_with_some_arg_outside_01"] for d in rows),
            "diverges_with_all_args_outside_01": sum(d["diverges_with_all_args_outside_01"] for d in rows),
            "diverges_somewhere_inside_01k": sum(bool(d["divergent_inputs_inside_01k"]) for d in rows),
            "vert_diff_samples_alone_show_PARAM1_outside_01": sum(d["vert_diff_samples_show_PARAM1_outside_01"] for d in rows),
            "rerun_value_div_equals_vert_diff_primary_like": sum(d["value_div_rerun_primary_like"] == d["value_div_vert_diff_json"] for d in rows),
            "rerun_value_div_equals_vert_diff_confirmed": sum(d["value_div_rerun_confirmed_both_filters"] == d["value_div_vert_diff_json"] for d in rows)}


git = lambda *a: subprocess.run(["git", *a], capture_output=True, text=True).stdout.strip()
res = {
    "provenance": {
        "script": "systems/vert/kani_vacuity/set_check_current.py (+ systems/vert/zero_one_current.py for the {0,1} section)",
        "command": f"python3 systems/vert/kani_vacuity/set_check_current.py {ZO} {ZOPROV} {OUT}",
        "git_rev": git("rev-parse", "HEAD"), "git_dirty_files": subprocess.run(["git", "status", "--short"], capture_output=True, text=True).stdout.rstrip("\n").split("\n"),
        "host": platform.system(), "arch": platform.machine(), "python": platform.python_version(),
        "date": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "inputs": ["results/vert_diff.json", "artifacts/vert/c_results.csv", "results/vert_kani_vacuity.json",
                   "results/vert_kani_unwinding_on.json", "results/kani_logs/", "results/kani_logs_unwind_on/"],
        "kani": {"bounded": kv["summary"]["kani_version"] + " (from results/vert_kani_vacuity.json; no Kani re-run was needed)",
                 "unwinding_on": ku["summary"]["kani_version"]},
        "zero_one_rerun": J(ZOPROV)},
    "set_comparison": {
        "current_wrong_answers": len(cur),
        "c_results_bounded_kani_1": len(bk),
        "expected_set_current_and_bounded_kani_1": expected, "expected_size": len(expected),
        "vert_kani_vacuity_json": {"rows": len(kv_rows), "rows_equal_all_bounded_kani_1": set(kv_rows) == bk,
                                   "missing_from_expected": sorted(set(expected) - set(kv_rows)),
                                   "flag_in_scalar_wrong_answers_count": len(kv_flagged),
                                   "flag_missing": sorted(set(expected) - kv_flagged), "flag_extra": sorted(kv_flagged - set(expected)),
                                   "summary_of_the_scalar_wrong_answers_stale": kv["summary"]["of_the_scalar_wrong_answers"],
                                   "note": "the per-function verdicts for all 14 are present; only the in_scalar_wrong_answers flag and the 'of_the_scalar_wrong_answers' summary (12) predate the current set (2 functions newly wrong on the spread grid)"},
        "vert_kani_unwinding_on_json": {"rows": len(ku_rows), "missing": sorted(set(expected) - set(ku_rows)),
                                        "extra": sorted(set(ku_rows) - set(expected)), "exact_match": set(ku_rows) == set(expected)},
        "rerun_needed": sorted((set(expected) - set(kv_rows)) | (set(expected) - set(ku_rows)) | (set(ku_rows) - set(expected))),
        "rerun_verdicts": "none: no function missing or extra in either file's rows"},
    "current_14": {
        "vacuous_pass": sum(r["vacuous_pass"] for r in per14),
        "fails_on_unwinding_assertion_when_on": sum(r["fails_on_unwinding_assertion"] for r in per14),
        "equivalence_assertion_status_unwinding_on_json": sorted({str(r["equivalence_assertion_unwinding_on_json"]) for r in per14}),
        "equivalence_assertion_undetermined_derived_from_log_summary": sum(r["unwinding_on"]["equivalence_assertion_derived_from_summary"] == "UNDETERMINED" for r in per14),
        "unwind_log_failing_loop_in_rust_std_vec": sum(any(x["file"].startswith("<rust std>") for x in r["unwinding_on"]["failed_checks"]) for r in per14),
        "unwind_std_vec_note": "the 4 failures in <rust std>/library/alloc/src/vec/mod.rs:2708 are in functions whose f_gold allocates vec![_; n+1] or vec![_; n+2]; at the harness's n=12 that fill loop runs 13-14 times, beyond unwind 10",
        "logs_agree_with_json": sum(r["json_agrees_bounded"] and r["json_agrees_unwinding_on"] for r in per14),
        "bounded_log_assert_line_matches_own_source": sum(bool(r["bounded_kani_no_unwinding_checks"]["assert_line_matches_own_source"]) for r in per14),
        "unwind_log_failing_loop_in_own_f_gold": sum(all(x["line_in_own_f_gold"] for x in r["unwinding_on"]["failed_checks"]) and bool(r["unwinding_on"]["failed_checks"]) for r in per14),
        "rows": per14},
    "all_90": {
        "verdict_counts_json": vc,
        "vacuous_success": sum(r["json_verdict"] == "SUCCESSFUL" and r["json_assertion"] == "UNREACHABLE" for r in all90.values()),
        "logs_agree_with_json": sum(r["json_agrees"] for r in all90.values()),
        "assert_line_fingerprint_checked": sum(r["assert_line"] is not None for r in all90.values()),
        "assert_line_matches_own_source": sum(bool(r["assert_line_matches_own_source"]) for r in all90.values()),
        "note": "independent of the wrong-answer set: 81 of 90 is a statement about all bounded_kani=1 functions",
        "rows": all90},
    "zero_one_domain": {
        "claim": "Each counted wrong answer also diverges outside {0,1}, the harness's only inputs.",
        "harness_inputs": "VERT's Kani harness draws only PARAM1, kani::assume((0..2).contains(&PARAM1)); other parameters are not symbolic",
        "method": "per-input re-run of systems/vert/run.py's classifier (batch_diff.tally, one input at a time) on the current spread grid (n=80, non-negative, |x|<=512); a divergence counts only if it is a value divergence under BOTH UB filters (ASan/UBSan + -O0/-O1, and ASan/UBSan + MSan)",
        "all_25": agg(dom), "bounded_kani_14": agg([d for d in dom if d["bounded_kani_1"]]),
        "rows": dom},
}
json.dump(res, open(OUT, "w"), indent=1)
print(json.dumps({k: v for k, v in res["set_comparison"].items()}, indent=1))
print(json.dumps({k: v for k, v in res["current_14"].items() if k != "rows"}, indent=1))
print(json.dumps({k: v for k, v in res["all_90"].items() if k != "rows"}, indent=1))
print(json.dumps({k: v for k, v in res["zero_one_domain"].items() if k != "rows"}, indent=1))
