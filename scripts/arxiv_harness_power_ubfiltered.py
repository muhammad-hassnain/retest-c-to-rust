"""arXiv re-run: harness power with every kill subject to the UB rule.

Reads results/harness_power.json (the shipped rows, scripts/plot_results.py) and the per-system re-scans
  results/arxiv_mutation_ubfilter_c2saferrust.json  harness/arxiv_mutate_full_ubfilter.py (rust, rust_WIP)
  results/arxiv_mutation_ubfilter_flourine.json     harness/arxiv_mutate_full_flourine_ubfilter.py
  results/arxiv_mutation_ubfilter_actor.json        systems/actoruw/arxiv_mutate_ubfilter.py
  results/arxiv_mutation_ubfilter_sactor.json       systems/sactor/crust/arxiv_grid_ub_all_inputs.py
and checks SACTOR's libogg kills against the per-case UB verdicts of results/sactor_libogg.json.
Writes results/arxiv_harness_power_ubfiltered.json: the rows of harness_power.json with `killed`,
`rate` under the UB rule, plus killed_old / killed_new / rate_old, whether the original scan filtered
kills by the UB rule (with file:line evidence), and a note per row. Nothing else is rewritten.

--vert (added later; the default output above is unchanged): also read
  results/arxiv_mutation_ubfilter_vert.json         systems/vert/arxiv_mutate_ubfilter.py (full rule on VERT's kills)
take the two VERT rows from it, add a `vert_section`, and write results/arxiv_harness_power_ubfiltered_vert.json
instead (every other row as in the default output).

  python3 -I scripts/arxiv_harness_power_ubfiltered.py
  python3 -I scripts/arxiv_harness_power_ubfiltered.py --vert
"""
import argparse
import copy
import datetime
import hashlib
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
from provenance import stamp  # noqa: E402

R = lambda p: os.path.join(REPO, p)  # noqa: E731
INPUTS = ["results/harness_power.json", "results/arxiv_mutation_ubfilter_c2saferrust.json",
          "results/arxiv_mutation_ubfilter_flourine.json", "results/arxiv_mutation_ubfilter_actor.json",
          "results/arxiv_mutation_ubfilter_sactor.json", "results/sactor_libogg.json",
          "results/sactor_libogg_mutation.json", "results/sactor_crust_mutation.json"]

# Does the scan that produced each row drop a kill on an input where the C is not UB-clean?
AUDIT = {
    "VERT scalar": {
        "ub_filtered": "yes (VERT harness rule, weaker than the full rule)",
        "evidence": ["systems/vert/mutate_primary.py:611-666 (tally_first_kill: a candidate whose gcc ASan+UBSan -O0 "
                     "run reports, or whose -O0 output differs from -O1, is counted `ub` and the scan continues; "
                     "only a UB-clean value/panic stops it)",
                     "systems/vert/mutate_primary.py:558-568 (classify: ub_only is not a kill)",
                     "systems/vert/run.py:130-146 (san = gcc -O0 -fsanitize=address,undefined; alt = gcc -O0 -fwrapv; "
                     "clang MSan when clang exists)"],
        "rescanned": False,
        "why_not_rescanned": "filters in code; its rule lacks clang ASan/UBSan, float-cast-overflow under gcc, -O2 "
                             "and char signedness (issues.md item 7); kills were not re-checked under the full rule here"},
    "VERT array/float": {
        "ub_filtered": "yes (VERT harness rule, weaker than the full rule)",
        "evidence": ["systems/vert/mutate_primary.py:1053-1093 (ra_classify_first_kill: -O0 != -O1 -> ub; gcc ASan+UBSan "
                     "report or nonzero exit -> ub; scan continues)"],
        "rescanned": False,
        "why_not_rescanned": "as VERT scalar"},
    "Fluorine scalar": {
        "ub_filtered": "no",
        "evidence": ["harness/mutate_full_flourine.py:133-136 (tally(binc, b, cases) with no sanitizer or -O0 build: "
                     "every value difference is a kill)"],
        "rescanned": True},
    "C2SaferRust coreutils": {
        "ub_filtered": "no",
        "evidence": ["harness/mutate_full.py:726-746 (_run_mutant: the scan stops at the first case with a stdout/exit "
                     "mismatch; no UB check)"],
        "rescanned": True},
    "SACTOR, pooled": {
        "ub_filtered": "no (in code); by data every kill is on a UB-clean input",
        "evidence": ["systems/sactor/libogg/mutate.py:144-151 (any (stdout, stderr, exit) difference on a case outside "
                     "the unmutated translation's divergences kills; no UB check)",
                     "systems/sactor/crust/mutate.py:86-92 (grid: whole-run output of one function differs from the C; "
                     "no UB check)"],
        "rescanned": False,
        "why_not_rescanned": "not needed: libogg killing cases checked against the per-case UB verdicts of "
                             "results/sactor_libogg.json; every input of the CRUST grid functions involved checked "
                             "under the full rule (results/arxiv_mutation_ubfilter_sactor.json)"},
    "His2Trans": {
        "ub_filtered": "yes",
        "evidence": ["systems/his2trans/power.py:101-103 (clean_agree_cases: only cases where the main run's "
                     "c_ub_evidence is empty are run; the main run applies the full rule to every case, "
                     "systems/his2trans/run.py:52-62, 220-234)"],
        "rescanned": False},
    "AdaTrans": {
        "ub_filtered": "yes",
        "evidence": ["systems/adatrans/mutate.py:139-143 (eval_mutant: a divergence kills only if run_diff.ub_check "
                     "finds the C clean; otherwise the scan continues)",
                     "systems/adatrans/run_diff.py:208-241 (ub_check: repeat run, -O1/-O2, uchar/schar, clang and "
                     "gcc ASan+UBSan)"],
        "rescanned": False},
    "PtrTrans": {
        "ub_filtered": "yes",
        "evidence": ["systems/ptrtrans/quadtree_e2e/mutate.py:148-149 (only cases with empty c_ub_evidence in "
                     "results/ptrtrans_quadtree_diff.json are run; run.py:60 UB_VARIANTS o0/o1/o2/san_clang/san_gcc/"
                     "uchar/schar)"],
        "rescanned": False},
    "ACTOR": {
        "ub_filtered": "no",
        "evidence": ["systems/actoruw/mutate.py:266-283 (evaluate: the first confirmed wrong answer or crash kills; no "
                     "UB check)"],
        "rescanned": True},
}


def sha(p):
    with open(R(p), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def sactor_check():
    base = json.load(open(R("results/sactor_libogg.json")))
    mut = json.load(open(R("results/sactor_libogg_mutation.json")))
    cm = json.load(open(R("results/sactor_crust_mutation.json")))
    grid = json.load(open(R("results/arxiv_mutation_ubfilter_sactor.json")))
    ub_ids = {r["id"] for f in base["files"].values() for r in f["rows"] if r["c_ub"]}
    known = {r["id"] for f in base["files"].values() for r in f["rows"]}
    lib = {}
    for t, v in mut["targets"].items():
        ks = [m for m in v["mutants"] if m["status"] == "killed"]
        lib[t] = {"valid": v["valid"], "killed": len(ks),
                  "killed_first_case_ub_clean": sum(1 for m in ks if m["killed_by"] and m["killed_by"][0] in known
                                                    and m["killed_by"][0] not in ub_ids),
                  "killed_by_cases_not_in_base_run": sorted({c for m in ks for c in m["killed_by"] if c not in known})}
    grid_kills = {s: sum(1 for m in d["mutants"] if m.get("killed_by_grid")) for s, d in cm["samples"].items()}
    grid_samples_checked = {s: grid["summary"][s] for s in grid["summary"]}
    ok_lib = all(x["killed_first_case_ub_clean"] == x["killed"] for x in lib.values())
    ok_grid = all((s in grid_samples_checked and grid_samples_checked[s]["all_inputs_ub_clean"]) for s, k in
                  grid_kills.items() if k)
    lk = sum(x["killed"] for x in lib.values())
    gk = sum(grid_kills.values())
    return {"libogg": lib, "libogg_ub_cases_in_base_run": sorted(ub_ids), "crust_grid_kills_per_sample": grid_kills,
            "crust_grid_samples_checked": grid_samples_checked,
            "all_libogg_kills_on_ub_clean_case": ok_lib, "all_crust_grid_kills_on_samples_with_every_input_ub_clean": ok_grid,
            "killed_new": lk + gk if ok_lib and ok_grid else None}


VERT_FILE = "results/arxiv_mutation_ubfilter_vert.json"


def vert_audit():
    """AUDIT with the two VERT rows re-checked under the full rule (--vert); the default AUDIT is untouched."""
    a = copy.deepcopy(AUDIT)
    for lab in ("VERT scalar", "VERT array/float"):
        a[lab]["rescanned"] = True
        a[lab].pop("why_not_rescanned", None)
        a[lab]["rescanned_how"] = (
            "systems/vert/arxiv_mutate_ubfilter.py (results/arxiv_mutation_ubfilter_vert.json): the recorded kill input "
            "of every counted kill checked under the full rule (systems/vert/arxiv_ub_fullrule.py check_input); a mutant "
            "whose kill input is not UB-clean regenerated, rebuilt and re-scanned over the same grid in the same order, "
            "a kill counting only on a UB-clean input")
    return a


def vert_note(lab, s):
    rs = s["rescanned_new_outcome"]
    chk = s["kill_inputs_not_ub_clean_by_failing_check"]
    funcs = s["functions_with_a_kill_input_not_ub_clean"]
    why = sorted({k for v in s["distinct_not_ub_clean_inputs_by_check_and_reason"].values() for k in v})
    bad = (f"{s['kill_input_not_ub_clean']} not UB-clean"
           + (f" ({', '.join(f'{f} x{n}' for f, n in funcs.items())}; failing checks: "
              f"{', '.join(sorted(chk))}; reports: {'; '.join(why)})" if s["kill_input_not_ub_clean"] else ""))
    return (f"full rule (issues.md item 7) applied to the recorded kill input of all {s['kill_inputs_checked']} counted kills "
            f"({s['distinct_kill_inputs_checked']} distinct function/input pairs; primary C value reproduced on "
            f"{s['kill_c_value_reproduced']}): {bad}. Re-scanned over the same grid: {s['rescanned']} "
            f"(new outcomes {rs}; first kill the shipped scan would count = the recorded input in "
            f"{s['rescanned_first_shipped_kill_is_recorded_input']}). Killed {s['killed_old']} -> {s['killed_new']}; "
            f"undetermined {s['undetermined_old']} -> {s['undetermined_new']}.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vert", action="store_true",
                    help=f"take the VERT rows from {VERT_FILE} and write results/arxiv_harness_power_ubfiltered_vert.json")
    args = ap.parse_args()
    audit = vert_audit() if args.vert else AUDIT
    vt = json.load(open(R(VERT_FILE)))["summary"] if args.vert else None
    hp = json.load(open(R("results/harness_power.json")))
    c2 = json.load(open(R("results/arxiv_mutation_ubfilter_c2saferrust.json")))
    fl = json.load(open(R("results/arxiv_mutation_ubfilter_flourine.json")))
    ac = json.load(open(R("results/arxiv_mutation_ubfilter_actor.json")))
    sa = sactor_check()
    rows = []
    for r in hp["rows"]:
        lab = r["label"]
        a = audit[lab]
        row = dict(r)
        row["killed_old"], row["rate_old"] = r["killed"], r["rate"]
        row["ub_filtered_in_original_scan"] = a["ub_filtered"]
        row["ub_filter_evidence"] = a["evidence"]
        if lab == "C2SaferRust coreutils":
            s, w = c2["runs"]["rust"]["summary"], c2["runs"]["rust_WIP"]["summary"]
            assert (s["killed_old"], s["compiled"]) == (r["killed"], r["built"]), (s, r)
            kn = s["killed_new"]
            u = s["per_util"]["uniq"]
            row["note_ubfilter"] = (
                f"re-scanned: {s['rescanned']} killed mutants (all uniq) first differed on a case where the C is not "
                f"UB-clean (clang's NULL+0 at linebuffer.c:65, which the defect count treats as UB); re-run over the "
                f"{u['ub_clean_cases']} UB-clean uniq cases of {u['grid_cases']}, {s['rescanned_new_status'].get('killed_s1', 0) + s['rescanned_new_status'].get('killed_crash', 0)} "
                f"are still killed; uniq {u['killed_old']} -> {u['killed_new']}, cat/head/tail unchanged (every case "
                f"UB-clean). C2SaferRust's own output (rust_WIP, cases where it agrees): {w['killed_old']}/{w['compiled']} -> "
                f"{w['killed_new']}/{w['compiled']} ({w['rate_new']:.2%}). If NULL+0 alone were not UB, every kill would "
                f"stand ({s['killed_if_null_plus_zero_alone_is_not_ub']}/{s['compiled']}).")
            row["rust_WIP"] = {"killed_old": w["killed_old"], "killed_new": w["killed_new"], "built": w["compiled"],
                               "rate_old": w["rate_old"], "rate_new": w["rate_new"]}
            row["killed_if_null_plus_zero_alone_is_not_ub"] = s["killed_if_null_plus_zero_alone_is_not_ub"]
        elif lab == "Fluorine scalar":
            s = fl["summary"]
            assert (s["killed_old"], s["compiled"]) == (r["killed"], r["built"]), (s, r)
            kn = s["killed_new"]
            row["note_ubfilter"] = (
                f"re-run: all {s['compiled']} compiled mutants rebuilt and re-run ({s['reproduced_all_compiled']} reproduce "
                f"the recorded tally); the full UB rule checked on every grid input of the 18 benchmarks; every killed "
                f"mutant has a value difference on a UB-clean input ({s['killed_new']} of {s['killed_old']}; strict view "
                f"{s['killed_new_strict']}). Not part of the UB rule: also dropping inputs with min > max, as the defect "
                f"count does, leaves {s['killed_new_uniform_and_not_min_gt_max']}.")
            row["killed_if_min_gt_max_inputs_also_dropped"] = s["killed_new_uniform_and_not_min_gt_max"]
        elif lab == "ACTOR":
            s = ac["summary"]
            assert (s["killed_old"], s["compiled"]) == (r["killed"], r["built"]), (s, r)
            kn = s["killed_new"]
            row["undetermined"] = s["undetermined_new"]
            row["note_ubfilter"] = (
                f"the C was checked under the full UB rule on the recorded killing input of all {s['killed_inputs_checked']} "
                f"killed mutants: {s['killed_input_not_ub_clean']} not UB-clean; those re-scanned (kills only on UB-clean "
                f"inputs): {s['rescanned_new_status']}. Killed {s['killed_old']} -> {s['killed_new']}; undetermined "
                f"{s['undetermined_old']} -> {s['undetermined_new']}. In {s['rescanned'] - s['rescanned_first_candidate_is_recorded_by_input']} "
                f"of the {s['rescanned']} re-scans the recorded kill did not recur (the mutant agreed with the C on every "
                f"input; the recorded input is one where the C has signed overflow); they count as not killed.")
        elif lab == "SACTOR, pooled":
            assert sa["killed_new"] is not None, sa
            kn = sa["killed_new"]
            assert kn == r["killed"], (kn, r)
            row["note_ubfilter"] = (
                "no re-run needed: every libogg kill's first killing case is UB-clean in results/sactor_libogg.json (the "
                "one case there with C UB, " + ", ".join(sa["libogg_ub_cases_in_base_run"]) + ", kills no mutant); every "
                "input of the CRUST grid functions with a grid kill (NandC, geofence) is UB-clean under the full rule.")
            row["sactor_check"] = sa
        elif lab.startswith("VERT") and args.vert:
            s = vt[lab]
            assert (s["killed_old"], s["built"]) == (r["killed"], r["built"]), (s, r)
            kn = s["killed_new"]
            row["undetermined"] = s["undetermined_new"]
            row["note_ubfilter"] = vert_note(lab, s)
        elif lab.startswith("VERT"):
            kn = r["killed"]
            row["note_ubfilter"] = ("the original scan drops kills on inputs where the C is not UB-clean under the VERT "
                                    "harness's own rule (gcc ASan+UBSan -O0, -O0/-O1 cross-check, clang MSan where "
                                    "present); not re-checked here under the full rule, which adds clang ASan/UBSan, "
                                    "float-cast-overflow, -O2 and char signedness (issues.md item 7); count unchanged")
        else:
            kn = r["killed"]
            row["note_ubfilter"] = "the original scan already drops kills on inputs where the C is not UB-clean; unchanged"
        row["killed"], row["killed_new"] = kn, kn
        row["rate"] = round(kn / r["built"], 4)
        rows.append(row)
    rates_old = [r["rate_old"] for r in rows]
    rates_new = [r["rate"] for r in rows]
    lo = min(rows, key=lambda r: r["rate"])
    hi = max(rows, key=lambda r: r["rate"])
    out = {
        "_provenance": stamp(),
        "provenance": {"script": "scripts/arxiv_harness_power_ubfiltered.py",
                       "command": "python3 -I scripts/arxiv_harness_power_ubfiltered.py" + (" --vert" if args.vert else ""),
                       "inputs": {p: sha(p) for p in INPUTS + ([VERT_FILE] if args.vert else [])},
                       "generated_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "runs": "the four arxiv_mutation_ubfilter_*.json files each carry the provenance of their run "
                               "(x86_64 box, Docker, harness/arxiv_mutub_run.sh)"},
        "kill_rule": hp["kill_rule"] + "; and (UB rule) a kill counts only on an input where the C is UB-clean under "
                                       "the rule the defect count uses",
        "label_marks": hp["label_marks"],
        "range_old": {"min": min(rates_old), "max": max(rates_old)},
        "range_new": {"min": lo["rate"], "min_row": lo["label"], "max": hi["rate"], "max_row": hi["label"],
                      "as_percent": f"{lo['rate'] * 100:.0f}-{hi['rate'] * 100:.0f}%"},
        "ub_filter_audit": [{"row": lab, "ub_filtered_in_original_scan": a["ub_filtered"], "evidence": a["evidence"],
                             "rescanned_here": a["rescanned"], **({"why_not_rescanned": a["why_not_rescanned"]}
                                                                  if "why_not_rescanned" in a else {})}
                            for lab, a in audit.items()],
        "rows": rows,
    }
    if args.vert:
        out["provenance"]["runs"] += ("; results/arxiv_mutation_ubfilter_vert.json carries the provenance of the VERT "
                                      "runs (arm64 Mac and x86_64 box, Docker, crust-vert-toolchain[-x86])")
        out["vert_section"] = {
            "source": VERT_FILE,
            "rule": "full UB rule of systems/vert/arxiv_ub_fullrule.py (results/arxiv_vert_ub_fullrule.json): ASan+UBSan+"
                    "float-cast-overflow under gcc-11 and clang-14 at -O0/-O1/-O2; gcc/clang -O0/-O1/-O2 -fwrapv and "
                    "-fsigned-char/-funsigned-char builds print the primary oracle's value",
            "rows": {lab: {**{k: vt[lab][k] for k in ("built", "killed_old", "killed_new", "rate_old", "rate_new",
                                                      "undetermined_old", "undetermined_new", "kill_inputs_checked",
                                                      "distinct_kill_inputs_checked", "kill_input_not_ub_clean",
                                                      "functions_with_a_kill_input_not_ub_clean",
                                                      "kill_inputs_not_ub_clean_by_failing_check",
                                                      "distinct_not_ub_clean_inputs_by_check_and_reason", "rescanned",
                                                      "rescanned_new_outcome",
                                                      "rescanned_first_shipped_kill_is_recorded_input")}}
                     for lab in ("VERT scalar", "VERT array/float")},
            "range_changes": out["range_new"]["as_percent"] != json.load(open(R("results/arxiv_harness_power_ubfiltered.json")))["range_new"]["as_percent"],
        }
    p = R("results/arxiv_harness_power_ubfiltered_vert.json" if args.vert else "results/arxiv_harness_power_ubfiltered.json")
    with open(p, "w") as f:
        json.dump(out, f, indent=1)
    for r in rows:
        print(f"{r['label']:24s} {r['killed_old']:5d} -> {r['killed_new']:5d} / {r['built']:5d}   "
              f"{r['rate_old']:.4f} -> {r['rate']:.4f}")
    print("range", out["range_old"], "->", out["range_new"])


if __name__ == "__main__":
    main()
