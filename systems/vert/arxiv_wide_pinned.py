"""arXiv (issues.md 47): VERT's pinned compiler (nightly-2023-07-29) on the wide-int functions.

Same measurement as systems/vert/arxiv_pinned_grid2.py (results/arxiv_vert_pinned_grid2.json), scalar part,
for the 16 functions whose C f_gold returns long / long long (results/arxiv_vert_diff_grid2_wide.json, run.py
--wide-int): each run is systems/vert/toolchain_pinned.py --harness scalar --grid v2 --wide-int --only <16>
(which calls run.test_function(..., grid="v2", wide_int=True) with run.py main()'s other defaults), started
as a subprocess with RUSTUP_TOOLCHAIN set, first under 1.86.0 (the primary's rustc, same-session control),
then under nightly-2023-07-29 (VERT's artifacts/vert/benchmark/rust-toolchain.toml) with the control
embedded. The result file embeds both runs' rows, provenance, verdicts, the verdict diffs (vs the baseline
file and vs the control), per-function count differences, and for each of the 8 wide value bugs (planned
ledger rows VERT-<F>-1, S1) whether the wrong answer still shows under each toolchain.

Run inside crust-vert-toolchain (no `clang` on PATH, as the primary arm64 run):
  docker run --rm --cpus 4 -v $PWD:/work -v SCRATCH:/scratch -w /work \
      -e GIT_REV=... -e IMAGE_DESC="crust-vert-toolchain <id>" -e HOST_DESC="<OS name>" crust-vert-toolchain \
      python3 -I systems/vert/arxiv_wide_pinned.py --scratch /scratch --jobs 4 --out results/arxiv_vert_wide_pinned.json
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "scripts"))

PINNED = "nightly-2023-07-29"
CONTROL = "1.86.0"
BASE = "results/arxiv_vert_diff_grid2_wide.json"
COUNT_KEYS = ("status", "cases", "value_div", "rust_panic_c_ok", "ub_div", "timeout_div", "rust_hang", "san_infra")


def run_pinned(tc, funcs, out, jobs, extra=None, embed=None):
    cmd = [sys.executable, "-I", "systems/vert/toolchain_pinned.py", "--harness", "scalar", "--out", out,
           "--baseline", BASE, "--jobs", str(jobs), "--grid", "v2", "--wide-int", "--only", ",".join(funcs)]
    if extra:
        cmd += ["--extra-baseline", extra]
    if embed:
        cmd += ["--embed-control", embed]
    env = dict(os.environ, RUSTUP_TOOLCHAIN=tc)
    t0 = datetime.datetime.now(datetime.timezone.utc)
    print("RUN", tc, flush=True)
    subprocess.run(cmd, cwd=REPO, env=env, check=True)
    t1 = datetime.datetime.now(datetime.timezone.utc)
    return {"toolchain": tc, "command": "RUSTUP_TOOLCHAIN=%s python3 -I %s" % (tc, " ".join(cmd[2:])),
            "start_utc": t0.isoformat(), "end_utc": t1.isoformat(),
            "wall_clock_seconds": round((t1 - t0).total_seconds())}


def shows_s1(row):
    return bool(row) and row.get("status") == "tested" and row.get("value_div", 0) > 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", required=True)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--out", required=True)
    ap.add_argument("--skip-runs", action="store_true", help="re-assemble from existing scratch files")
    a = ap.parse_args()
    os.makedirs(a.scratch, exist_ok=True)
    S = lambda n: os.path.join(a.scratch, n)  # noqa: E731
    base = json.load(open(os.path.join(REPO, BASE)))
    funcs = [r["func"] for r in base["all"]]                  # the 16 wide-int functions run.py --wide-int considered
    wrong = base["summary"]["value_bug_functions"]
    files = {CONTROL: S("wide_scalar_control_1.86.0.json"), PINNED: S("wide_scalar_nightly.json")}
    if not a.skip_runs:
        runs = {CONTROL: run_pinned(CONTROL, funcs, files[CONTROL], a.jobs)}
        runs[PINNED] = run_pinned(PINNED, funcs, files[PINNED], a.jobs,
                                  extra="control_1.86.0=" + files[CONTROL], embed=files[CONTROL])
        json.dump(runs, open(S("wide_runs.json"), "w"), indent=1)
    else:
        runs = json.load(open(S("wide_runs.json")))
    nig, ctl = json.load(open(files[PINNED])), json.load(open(files[CONTROL]))
    nrows = {r["func"]: r for r in nig["all"]}
    crows = {r["func"]: r for r in ctl["all"]}
    brows = {r["func"]: r for r in base["all"]}
    tc_n, tc_c = nig["toolchain_comparison"], ctl["toolchain_comparison"]
    count_changes = []
    for f in funcs:
        trip = {lab: {k: rr.get(f, {}).get(k) for k in COUNT_KEYS}
                for lab, rr in (("baseline", brows), ("control_1.86.0", crows), ("nightly", nrows))}
        if not (trip["baseline"] == trip["control_1.86.0"] == trip["nightly"]):
            count_changes.append({"func": f, **trip})
    planned = []
    for f in wrong:
        planned.append({
            "defect_id": f"VERT-{f}-1", "func": f, "severity": "S1", "count_status": "planned (not yet in results/defect_ledger.json)",
            "shows_in_baseline": shows_s1(brows.get(f)), "shows_under_control_1.86.0": shows_s1(crows.get(f)),
            "shows_under_nightly": shows_s1(nrows.get(f)),
            "verdict_control_1.86.0": tc_c["verdicts"].get(f), "verdict_nightly": tc_n["verdicts"].get(f),
            "counts": {lab: {k: rr.get(f, {}).get(k) for k in COUNT_KEYS}
                       for lab, rr in (("baseline", brows), ("control_1.86.0", crows), ("nightly", nrows))},
            "value_samples_nightly": (nrows.get(f, {}).get("value_samples") or [])[:3],
            "value_samples_equal_control": (nrows.get(f, {}).get("value_samples") == crows.get(f, {}).get("value_samples")),
        })
    tested = sorted(f for f, r in brows.items() if r.get("status") == "tested")
    out = {"about": "arXiv issues.md 47. VERT's pinned rustc (nightly-2023-07-29, artifacts/vert/benchmark/rust-toolchain.toml) "
                    "vs the primary's 1.86.0, same session, same image, on grid v2, for the 16 functions whose C f_gold "
                    "returns long / long long (run.py --wide-int; baseline %s). %d of them build and are tested in the "
                    "baseline; %d have value divergences there." % (BASE, len(tested), len(wrong)),
           "script": "systems/vert/arxiv_wide_pinned.py (wraps systems/vert/toolchain_pinned.py --wide-int --only, which "
                     "calls systems/vert/run.py test_function(..., wide_int=True) unchanged)",
           "scalar": {
               "baseline": BASE,
               "grid": "v2 (systems/enumerate_inputs.py grid='v2', cap 80, non-negative, |x| <= 512)",
               "functions_run": len(tc_n["verdicts"]), "tested_in_baseline": len(tested),
               "compile_nightly": tc_n["compile"], "compile_control_1.86.0": tc_c["compile"],
               "verdict_diffs_nightly": tc_n["diffs"],
               "verdict_diff_control_vs_baseline": tc_c["diffs"]["primary"],
               "verdicts_nightly": tc_n["verdicts"], "verdicts_control_1.86.0": tc_c["verdicts"],
               "per_function_count_differences": count_changes,
               "planned_ledger_rows": planned,
               "rows_nightly": nig["all"], "rows_control_1.86.0": ctl["all"],
               "provenance_nightly": nig.get("provenance"), "provenance_control_1.86.0": ctl.get("provenance"),
               "runs": runs,
           }}
    sc = out["scalar"]
    nb = sc["verdict_diffs_nightly"]["primary"]["verdict_changes"]
    cb = sc["verdict_diff_control_vs_baseline"]["verdict_changes"]
    out["result"] = {
        "verdict_changes_nightly_vs_baseline": sc["verdict_diffs_nightly"]["primary"]["n_verdict_changes"],
        "verdict_changes_nightly_vs_control": sc["verdict_diffs_nightly"]["control_1.86.0"]["n_verdict_changes"],
        "verdict_changes_control_vs_baseline": sc["verdict_diff_control_vs_baseline"]["n_verdict_changes"],
        "nightly_vs_baseline_changes_equal_control_vs_baseline": (
            sorted(json.dumps(c, sort_keys=True) for c in nb) == sorted(json.dumps(c, sort_keys=True) for c in cb)),
        "functions_with_nightly_counts_different_from_control": [
            c["func"] for c in count_changes if c["nightly"] != c["control_1.86.0"]],
        "functions_with_control_counts_different_from_baseline": [
            c["func"] for c in count_changes if c["control_1.86.0"] != c["baseline"]],
        "rust_build_fail_nightly": tc_n["compile"]["rust_build_fail_functions"],
        "rust_build_fail_control_1.86.0": tc_c["compile"]["rust_build_fail_functions"],
        "planned_rows": len(planned),
        "planned_rows_shown_under_nightly": sum(x["shows_under_nightly"] for x in planned),
        "planned_rows_shown_under_control": sum(x["shows_under_control_1.86.0"] for x in planned),
    }
    import provenance as prov            # scripts/provenance.py (stamp only)
    out["_provenance"] = prov.stamp(image=os.environ.get("IMAGE_DESC"), step="arxiv_wide_pinned")
    if out["_provenance"].get("git_commit") is None:
        out["_provenance"]["git_commit"] = os.environ.get("GIT_REV")
    out["_provenance"]["host"] = os.environ.get("HOST_DESC")
    out["_provenance"]["note"] = ("stamp() taken in the assembly step (its rustc is the container default); "
                                  "the toolchain of each run is in scalar.provenance_*.toolchains")
    with open(os.path.join(REPO, a.out) if not os.path.isabs(a.out) else a.out, "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps(out["result"], indent=1))


if __name__ == "__main__":
    main()
