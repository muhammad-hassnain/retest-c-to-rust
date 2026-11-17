"""arXiv (issues.md 47): the VERT scalar harness-power numbers if the wide-return functions join the mutation pool.

Inputs (nothing here is re-run; every number is computed by the existing scripts' own code, imported):
  results/vert_mutation_primary_grid2.json      the shipped scalar mutation scan (115 agreeing functions, grid v2)
  results/arxiv_vert_wide_mutation_primary.json the same scan on the 3 agreeing long / long long functions
                                                (systems/vert/arxiv_wide_mutate.py)
  results/arxiv_mutation_ubfilter_vert.json     the full-rule UB filter on the shipped kills (systems/vert/arxiv_mutate_ubfilter.py)
  results/arxiv_vert_wide_mutation_ubfilter.json the same filter on the wide kills (systems/vert/arxiv_wide_ubfilter.py)
  results/harness_power.json at commit a6e32c1   the harness_power.json that arxiv_mutate_ubfilter.py merge and
                                                scripts/arxiv_harness_power_ubfiltered.py --vert read (its sha256 is
                                                in both files' provenance; the current file was regenerated since,
                                                with the same VERT scalar row)

Formulas (imported, applied to the combined pool = the 115 shipped functions' per_function records + the 3 new ones):
  * mutation summary: systems/vert/mutate_primary.py finish() (the code that wrote both mutation files);
  * paper numbers: scripts/claims_ledger.py _c_vmut() (claim vert-mut-scalar: kill rate, mutants, functions, rate
    among mutants not shown equivalent, f32 rate), fed the combined summary through its JSON cache;
  * harness-power row: scripts/plot_results.py harness_power() (the VERT scalar row of results/harness_power.json),
    fed the combined mutation file through J_opt;
  * UB-filtered counts: systems/vert/arxiv_mutate_ubfilter.py harness_summary() on the shipped + wide full-rule
    records; UB-filtered row: scripts/arxiv_harness_power_ubfiltered.py main() --vert, run with its path function
    redirected to the combined inputs and a temporary output (results/arxiv_harness_power_ubfiltered_vert.json is
    not touched).
Each formula is first run on the shipped inputs alone and must reproduce the shipped files (reproduces_shipped).

  python3 systems/vert/arxiv_wide_combined.py   # writes results/arxiv_vert_wide_mutation_combined.json
  (not -I: scripts/plot_results.py imports matplotlib, which may live in the user site-packages)
"""
import contextlib
import copy
import datetime
import hashlib
import io
import json
import os
import platform
import subprocess
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
for p in (HERE, os.path.dirname(HERE), os.path.join(REPO, "scripts")):
    sys.path.insert(0, p)
os.environ["MUTPRIM_GRID"] = "v2"
import mutate_primary as mp  # noqa: E402
import arxiv_mutate_ubfilter as amu  # noqa: E402
import claims_ledger as CL  # noqa: E402
import plot_results as PR  # noqa: E402
import arxiv_harness_power_ubfiltered as AHP  # noqa: E402
from provenance import stamp  # noqa: E402

OLD_PRIMARY = "results/vert_mutation_primary_grid2.json"
NEW_PRIMARY = "results/arxiv_vert_wide_mutation_primary.json"
OLD_UBF = "results/arxiv_mutation_ubfilter_vert.json"
NEW_UBF = "results/arxiv_vert_wide_mutation_ubfilter.json"
HP = "results/harness_power.json"
HP_REV = "a6e32c1"
AHP_OUT = "results/arxiv_harness_power_ubfiltered_vert.json"
OUT = "results/arxiv_vert_wide_mutation_combined.json"
PAPER_LABELS = ["kill_rate_pct", "mutants", "functions", "kill_rate_among_not_shown_equivalent_pct", "f32_kill_rate_pct",
                "undetermined_gt_0", "harness_power_row_matches_summary"]


def J(p):
    with open(os.path.join(REPO, p)) as f:
        return json.load(f)


def sha_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha(p):
    with open(os.path.join(REPO, p), "rb") as f:
        return sha_bytes(f.read())


def git_blob(rev, p):
    return subprocess.run(["git", "-C", REPO, "show", f"{rev}:{p}"], stdout=subprocess.PIPE, check=True).stdout


# ------------------------------------------------------------------------------------------------ mutate_primary.finish
def finish_summary(per_function, pop_n, seconds, budgets):
    """mutate_primary.finish() on these per-function records; returns the document it writes."""
    mp.GRID = "v2"
    with tempfile.TemporaryDirectory() as d:
        args = types.SimpleNamespace(equiv=True, budgets=budgets, out=os.path.join(d, "m.json"))
        with contextlib.redirect_stdout(io.StringIO()):
            mp.finish(args, [pf["func"] for pf in per_function], copy.deepcopy(per_function), pop_n,
                      seconds["mutation"], seconds["total"], {"note": "in memory (systems/vert/arxiv_wide_combined.py)"})
        with open(args.out) as f:
            return json.load(f)


# ------------------------------------------------------------------------------------------------ claims_ledger._c_vmut
def paper_numbers(primary_doc, hp_doc):
    CL._CACHE.clear()
    CL._CACHE[OLD_PRIMARY] = primary_doc
    CL._CACHE[HP] = hp_doc
    try:
        r = CL._c_vmut()
    finally:
        CL._CACHE.clear()
    return {"values": dict(zip(PAPER_LABELS, r["actual"])), "detail": r.get("detail"),
            "matches_paper_claim": r["ok"], "paper_claim_values": dict(zip(PAPER_LABELS, r["expected"]))}


# ------------------------------------------------------------------------------------------------ plot_results.harness_power
def hp_row(primary_doc):
    jopt = PR.J_opt
    PR.J_opt = lambda p: copy.deepcopy(primary_doc) if p == OLD_PRIMARY else jopt(p)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            PR.harness_power()
    finally:
        PR.J_opt = jopt
    return copy.deepcopy(next(r for r in PR.HARNESS_ROWS if r["label"] == "VERT scalar"))


# ------------------------------------------------------------------------------------------------ arxiv_mutate_ubfilter
def shipped_scalar_res(ubf):
    """The per-function records of the merged file (functions[] + mutants[]) as the part files hold them."""
    by = {}
    for m in ubf["mutants"]:
        if m["harness"] == "scalar":
            by.setdefault(m["func"], []).append({k: v for k, v in m.items() if k not in ("harness", "func", "run_arch")})
    return [{**r, "mutants": by.get(r["func"], [])} for r in ubf["functions"] if r["harness"] == "scalar"]


# ------------------------------------------------------------------------------------------------ arxiv_harness_power_ubfiltered
def ahp_run(hp_doc, ubf_doc):
    """scripts/arxiv_harness_power_ubfiltered.py --vert on these two inputs; output to a temporary file."""
    with tempfile.TemporaryDirectory() as d:
        paths = {HP: os.path.join(d, "harness_power.json"), "results/arxiv_mutation_ubfilter_vert.json": os.path.join(d, "ubf.json"),
                 AHP_OUT: os.path.join(d, "out.json")}
        for k, v in ((HP, hp_doc), ("results/arxiv_mutation_ubfilter_vert.json", ubf_doc)):
            with open(paths[k], "w") as f:
                json.dump(v, f, indent=1)
        r0, argv = AHP.R, sys.argv
        AHP.R = lambda p: paths.get(p, r0(p))
        sys.argv = ["scripts/arxiv_harness_power_ubfiltered.py", "--vert"]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                AHP.main()
        finally:
            AHP.R, sys.argv = r0, argv
        with open(paths[AHP_OUT]) as f:
            return json.load(f)


def ahp_view(doc):
    rows = {r["label"]: r for r in doc["rows"]}
    v = rows["VERT scalar"]
    return {"killed": v["killed"], "built": v["built"], "rate": v["rate"], "undetermined": v["undetermined"],
            "killed_before_ub_filter": v["killed_old"], "rate_before_ub_filter": v["rate_old"],
            "note_ubfilter": v["note_ubfilter"], "all_rows_range_new": doc["range_new"]}


def strip_volatile(doc):
    doc = copy.deepcopy(doc)
    doc.pop("_provenance", None)
    doc.pop("provenance", None)
    return doc


def main():
    old, new = J(OLD_PRIMARY), J(NEW_PRIMARY)
    ubf_old, ubf_new = J(OLD_UBF), J(NEW_UBF)
    hp_bytes = git_blob(HP_REV, HP)
    hp_old = json.loads(hp_bytes)
    assert sha_bytes(hp_bytes) == ubf_old["provenance"]["inputs"][HP] == J(AHP_OUT)["provenance"]["inputs"][HP]

    # same scan settings on both sides
    budgets = old["summary"]["survivor_equivalence_headline"]["budgets_seconds_ABC"]
    assert budgets == new["summary"]["survivor_equivalence_headline"]["budgets_seconds_ABC"] == "30,10,10"
    assert old["provenance"]["grid"] == new["provenance"]["grid"] == "v2"
    assert old["summary"]["headline_ops"] == new["summary"]["headline_ops"]
    cmds = [s["command"] for s in old["provenance"]["merged_from_shards"]] + [new["provenance"]["command"]]
    assert all("--cap" not in c and "--seed" not in c and "--budgets 30,10,10" in c and "--equiv" in c for c in cmds), cmds
    assert not {pf["func"] for pf in old["per_function"]} & {pf["func"] for pf in new["per_function"]}
    assert new["summary"]["population"]["control_not_agree"] == []

    # 1. mutation summary (mutate_primary.finish)
    re_old = finish_summary(old["per_function"], old["summary"]["population"]["functions"], old["summary"]["seconds"], budgets)
    finish_ok = re_old["summary"] == old["summary"] and re_old["per_function_rates"] == old["per_function_rates"]
    assert finish_ok, "mutate_primary.finish does not reproduce the shipped summary"
    pop_n = old["summary"]["population"]["functions"] + new["summary"]["population"]["functions"]
    secs = {k: old["summary"]["seconds"][k] + new["summary"]["seconds"][k] for k in ("mutation", "total")}
    comb = finish_summary(old["per_function"] + new["per_function"], pop_n, secs, budgets)
    comb["summary"]["population"]["definition"] = (
        "results/vert_diff_grid2.json (115) and results/arxiv_vert_diff_grid2_wide.json (3): status tested and "
        "value_div=rust_panic=rust_hang=ub_div=timeout_div=0")
    comb["summary"]["seconds_note"] = "sum of the two runs' seconds (not a wall-clock time)"

    # 2. harness-power row (plot_results.harness_power)
    row_old, row_new = hp_row(old), hp_row(comb)
    hp_rows_old = {r["label"]: r for r in hp_old["rows"]}
    assert row_old == hp_rows_old["VERT scalar"] == {r["label"]: r for r in J(HP)["rows"]}["VERT scalar"]
    hp_comb = copy.deepcopy(hp_old)
    hp_comb["rows"] = [row_new if r["label"] == "VERT scalar" else r for r in hp_comb["rows"]]

    # 3. paper numbers (claims_ledger._c_vmut)
    paper_old = paper_numbers(old, {"rows": [row_old]})
    assert paper_old["matches_paper_claim"], paper_old
    paper_new = paper_numbers(comb, {"rows": [row_new]})

    # 4. UB-filtered counts (arxiv_mutate_ubfilter.harness_summary) and row (arxiv_harness_power_ubfiltered --vert)
    res_old = shipped_scalar_res(ubf_old)
    s_old = amu.harness_summary("scalar", old, res_old, hp_rows_old["VERT scalar"])
    assert s_old == ubf_old["summary"]["VERT scalar"], "harness_summary does not reproduce the shipped VERT scalar summary"
    assert ubf_new["part"]["harness"] == "scalar" and sorted(ubf_new["part"]["functions"]) == sorted(pf["func"] for pf in new["per_function"])
    s_new = amu.harness_summary("scalar", comb, res_old + ubf_new["functions"], row_new)
    ahp_old = ahp_run(hp_old, {"summary": ubf_old["summary"]})
    shipped_ahp = J(AHP_OUT)
    ahp_ok = strip_volatile(ahp_old) == strip_volatile(shipped_ahp)
    assert ahp_ok, "arxiv_harness_power_ubfiltered --vert does not reproduce the shipped file"
    ahp_new = ahp_run(hp_comb, {"summary": {"VERT scalar": s_new, "VERT array/float": ubf_old["summary"]["VERT array/float"]}})

    # per added function
    added = []
    ub_by = {r["func"]: r for r in ubf_new["functions"]}
    diff_wide = {r["func"]: r for r in J("results/arxiv_vert_diff_grid2_wide.json")["all"]}
    for pf in new["per_function"]:
        hs = [m for m in pf["mutants"] if m["op"] in new["summary"]["headline_ops"]]
        u = ub_by[pf["func"]]
        added.append({
            "func": pf["func"], "c_return": diff_wide[pf["func"]].get("c_return"), "nargs": pf["nargs"],
            "why_in_pool": "status tested and value_div = rust_panic_c_ok = rust_hang = ub_div = timeout_div = 0 in "
                           "results/arxiv_vert_diff_grid2_wide.json; control re-run: " + pf["control"]["outcome"],
            "headline_mutants": len(hs), "compile_fail": sum(m["outcome"] == "compile_fail" for m in hs),
            "killed": pf["headline_rate"]["killed"], "assessable": pf["headline_rate"]["assessable"],
            "by_operator": {op: {"mutants": sum(m["op"] == op for m in hs),
                                 "killed": sum(m["op"] == op and m["outcome"] in ("killed_value", "killed_panic") for m in hs)}
                            for op in new["summary"]["headline_ops"] if any(m["op"] == op for m in hs)},
            "not_killed": [{"id": m["id"], "outcome": m["outcome"], "equivalence_verdict": (m.get("equiv") or {}).get("verdict"),
                            "A_exhaustive": (m.get("equiv") or {}).get("A_exhaustive")}
                           for m in hs if m["outcome"] not in ("killed_value", "killed_panic")],
            "bnd_mutants_not_in_headline": sum(m["op"] == "bnd" for m in pf["mutants"]),
            "ub_filter": {"kills_checked": len(u["mutants"]), "kill_inputs_ub_clean": sum(m["kill_input_ub_clean"] for m in u["mutants"]),
                          "killed_after_full_rule": sum(bool(m.get("new_killed")) for m in u["mutants"])},
        })
    not_in_pool = [{"func": f, "status": r["status"], **({k: r[k] for k in ("value_div", "rust_panic_c_ok", "rust_hang", "ub_div", "timeout_div")}
                                                         if r["status"] == "tested" else {})}
                   for f, r in diff_wide.items() if f not in {a["func"] for a in added}]

    sv = {"old": ahp_view(ahp_old), "combined": ahp_view(ahp_new)}
    out = {
        "provenance": {
            "script": "systems/vert/arxiv_wide_combined.py",
            "command": "python3 systems/vert/arxiv_wide_combined.py",
            "inputs_sha256": {OLD_PRIMARY: sha(OLD_PRIMARY), NEW_PRIMARY: sha(NEW_PRIMARY), OLD_UBF: sha(OLD_UBF), NEW_UBF: sha(NEW_UBF),
                              f"{HP}@{HP_REV}": sha_bytes(hp_bytes), HP + " (current)": sha(HP),
                              "results/arxiv_vert_diff_grid2_wide.json": sha("results/arxiv_vert_diff_grid2_wide.json"),
                              AHP_OUT + " (reproduction check only)": sha(AHP_OUT)},
            "formula_code_sha256": {p: sha(p) for p in ("systems/vert/mutate_primary.py", "systems/vert/arxiv_mutate_ubfilter.py",
                                                        "scripts/claims_ledger.py", "scripts/plot_results.py",
                                                        "scripts/arxiv_harness_power_ubfiltered.py", "systems/vert/arxiv_wide_combined.py")},
            "formulas": {
                "mutation_summary": "systems/vert/mutate_primary.py finish()",
                "paper_numbers": "scripts/claims_ledger.py _c_vmut() (claim vert-mut-scalar)",
                "harness_power_row": "scripts/plot_results.py harness_power() (VERT scalar row)",
                "ub_filtered_counts": "systems/vert/arxiv_mutate_ubfilter.py harness_summary('scalar', ...)",
                "ub_filtered_row": "scripts/arxiv_harness_power_ubfiltered.py main() --vert (VERT scalar row)"},
            "os": f"{platform.system()} {platform.machine()}",
            "generated_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "pool": {
            "rule": "mutate_primary.population(): status tested and value_div = rust_panic_c_ok = rust_hang = ub_div = timeout_div = 0 "
                    "on the grid-v2 run (shipped: results/vert_diff_grid2.json; wide: results/arxiv_vert_diff_grid2_wide.json, run.py --wide-int)",
            "shipped_functions": old["summary"]["population"]["functions"],
            "added_functions": [a["func"] for a in added],
            "combined_functions": pop_n,
            "wide_tested_not_in_pool": not_in_pool,
        },
        "added": added,
        "added_totals": {k: new["summary"]["overall_headline"][k] for k in ("mutants", "compile_fail", "killed", "assessable", "rate", "outcomes")},
        "paper_numbers": {"claim": "vert-mut-scalar", "old": paper_old["values"], "combined": paper_new["values"],
                          "old_detail": paper_old["detail"], "combined_detail": paper_new["detail"],
                          "combined_matches_current_paper_text": paper_new["matches_paper_claim"]},
        "harness_power_row": {"old": row_old, "combined": row_new,
                              "combined_note": "the source/mutated_program text is plot_results.py's; the combined row is computed from "
                                               "the 115 shipped + 3 wide per-function records"},
        "ub_filtered": {"counts_old": s_old, "counts_combined": s_new, "row_old": sv["old"], "row_combined": sv["combined"]},
        "reproduces_shipped": {
            "mutate_primary.finish on the shipped records == results/vert_mutation_primary_grid2.json summary": finish_ok,
            "claims_ledger._c_vmut on the shipped file == the paper's values": paper_old["matches_paper_claim"],
            "plot_results.harness_power VERT scalar row == results/harness_power.json (current and a6e32c1)": True,
            "arxiv_mutate_ubfilter.harness_summary on the shipped records == results/arxiv_mutation_ubfilter_vert.json summary": True,
            "arxiv_harness_power_ubfiltered --vert on the shipped inputs == results/arxiv_harness_power_ubfiltered_vert.json (without provenance)": ahp_ok},
        "combined_mutation_summary": comb["summary"],
        "_provenance": stamp(argv=["systems/vert/arxiv_wide_combined.py"], step="arxiv_wide_combined"),
    }
    with open(os.path.join(REPO, OUT), "w") as f:
        json.dump(out, f, indent=1)
        f.write("\n")
    for k in ("old", "combined"):
        print(k, out["paper_numbers"][k], "| UB-filtered", {x: sv[k][x] for x in ("killed", "built", "rate", "undetermined")})


if __name__ == "__main__":
    main()
