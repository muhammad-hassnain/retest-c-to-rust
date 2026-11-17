"""arXiv issue 39: VERT's pinned compiler (nightly-2023-07-29) on the grid the defect count uses.

The shipped pinned-compiler check (results/vert_diff_pinned_nightly.json, stage
vert.toolchain_pinned_scalar) re-ran the scalar harness on grid v1 (results/vert_diff.json) and, in
its grid_v2_new_defects section, only the 4 functions that newly diverge on grid v2. The counted
scalar defects come from grid v2 (results/vert_diff_grid2.json). This script re-runs the WHOLE scalar
harness on grid v2 (every accepted function, so every counted scalar defect is included) and the
whole array/float harness (whose input generator, run_arrays.gen_cases, has not changed since the
shipped pinned run; the evaluation did change in audit 8, and the ledger reads the audit-8 file
results/vert_arrays_fixed.json), each under nightly-2023-07-29 and, as a same-session control,
under 1.86.0 (the primary's rustc). The harness itself is not modified: each run is
systems/vert/toolchain_pinned.py (which calls run.test_function / run_arrays.test_one with the
arguments their main() uses, then their own main() for aggregation), started as a subprocess with
RUSTUP_TOOLCHAIN set. The per-run outputs go to --scratch; the result file embeds their summaries,
provenance, per-function verdicts and counts, the verdict diffs, and a per-row check for every VERT
row of results/defect_ledger.json (does the row's outcome still show under each toolchain?).

Run inside crust-vert-toolchain (no clang on PATH, as the primary arm64 run), e.g.
  docker run --rm --cpus 4 -v $PWD:/work -v SCRATCH:/scratch -w /work \
      -e GIT_REV=$(git rev-parse HEAD) -e IMAGE_DESC="crust-vert-toolchain <id>" crust-vert-toolchain \
      python3 -I systems/vert/arxiv_pinned_grid2.py --scratch /scratch --jobs 4 \
      --out results/arxiv_vert_pinned_grid2.json
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
SCALAR_BASE = "results/vert_diff_grid2.json"
ARRAY_BASE = "results/vert_arrays_fixed.json"
COUNT_KEYS_SCALAR = ("status", "cases", "value_div", "rust_panic_c_ok", "ub_div", "timeout_div",
                     "rust_hang", "san_infra")
COUNT_KEYS_ARRAY = ("status", "kind", "agree", "value", "crash", "ub", "timeout", "rust_hang",
                    "san_unresolved")


def run_pinned(tc, harness, out, baseline, jobs, extra=None, embed=None):
    cmd = [sys.executable, "-I", "systems/vert/toolchain_pinned.py", "--harness", harness,
           "--out", out, "--baseline", baseline, "--jobs", str(jobs)]
    if harness == "scalar":
        cmd += ["--grid", "v2"]
    if extra:
        cmd += ["--extra-baseline", extra]
    if embed:
        cmd += ["--embed-control", embed]
    env = dict(os.environ, RUSTUP_TOOLCHAIN=tc)
    t0 = datetime.datetime.now(datetime.timezone.utc)
    print("RUN", tc, " ".join(cmd[2:]), flush=True)
    subprocess.run(cmd, cwd=REPO, env=env, check=True)
    t1 = datetime.datetime.now(datetime.timezone.utc)
    return {"toolchain": tc, "command": "RUSTUP_TOOLCHAIN=%s python3 -I %s" % (tc, " ".join(cmd[2:])),
            "start_utc": t0.isoformat(), "end_utc": t1.isoformat(),
            "wall_clock_seconds": round((t1 - t0).total_seconds())}


def rows_by_func(path):
    return {r["func"]: r for r in json.load(open(os.path.join(REPO, path)))["all"]}


def shows(row, family, severity):
    """Does this harness row still show the ledger row's outcome?"""
    if row is None or row.get("status") != "tested":
        return False
    if family == "scalar":
        return {"S1": row.get("value_div", 0) > 0, "crash": row.get("rust_panic_c_ok", 0) > 0,
                "hang": row.get("rust_hang", 0) > 0}[severity]
    return {"S1": row.get("value", 0) > 0, "crash": row.get("crash", 0) > 0,
            "hang": row.get("rust_hang", 0) > 0}[severity]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", required=True)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--out", required=True)
    ap.add_argument("--skip-runs", action="store_true", help="re-assemble from existing scratch files")
    a = ap.parse_args()
    os.makedirs(a.scratch, exist_ok=True)
    S = lambda n: os.path.join(a.scratch, n)
    files = {("scalar", CONTROL): S("scalar_control_1.86.0.json"), ("scalar", PINNED): S("scalar_nightly.json"),
             ("arrays", CONTROL): S("arrays_control_1.86.0.json"), ("arrays", PINNED): S("arrays_nightly.json")}
    runs = {}
    if not a.skip_runs:
        for h, base in (("scalar", SCALAR_BASE), ("arrays", ARRAY_BASE)):
            runs[(h, CONTROL)] = run_pinned(CONTROL, h, files[(h, CONTROL)], base, a.jobs)
            runs[(h, PINNED)] = run_pinned(PINNED, h, files[(h, PINNED)], base, a.jobs,
                                           extra="control_1.86.0=" + files[(h, CONTROL)],
                                           embed=files[(h, CONTROL)])
        json.dump({"%s|%s" % k: v for k, v in runs.items()}, open(S("runs.json"), "w"), indent=1)
    else:
        runs = {tuple(k.split("|")): v for k, v in json.load(open(S("runs.json"))).items()}

    ledger = json.load(open(os.path.join(REPO, "results", "defect_ledger.json")))
    vrows = [r for r in ledger["rows"] if r["system"] == "VERT"]

    out = {"about": "arXiv issue 39. VERT's pinned rustc (nightly-2023-07-29, artifacts/vert/benchmark/"
                    "rust-toolchain.toml) vs the primary's 1.86.0, same session, same image, on the grid "
                    "the defect count uses: scalar grid v2 (baseline %s) and the array/float generator "
                    "(baseline %s). Whole harnesses, every accepted function." % (SCALAR_BASE, ARRAY_BASE),
           "script": "systems/vert/arxiv_pinned_grid2.py (wraps systems/vert/toolchain_pinned.py, which wraps "
                     "systems/vert/run.py and systems/vert/run_arrays.py unchanged)"}
    for h, base, keys in (("scalar", SCALAR_BASE, COUNT_KEYS_SCALAR), ("arrays", ARRAY_BASE, COUNT_KEYS_ARRAY)):
        nig = json.load(open(files[(h, PINNED)]))
        ctl = json.load(open(files[(h, CONTROL)]))
        nrows = {r["func"]: r for r in nig["all"]}
        crows = {r["func"]: r for r in ctl["all"]}
        brows = rows_by_func(base)
        tc_n, tc_c = nig["toolchain_comparison"], ctl["toolchain_comparison"]
        tested = sorted(f for f, r in brows.items() if r.get("status") == "tested")
        count_changes = []
        for f in sorted(set(nrows) | set(crows) | set(brows)):
            trip = {lab: {k: rr.get(f, {}).get(k) for k in keys}
                    for lab, rr in (("baseline", brows), ("control_1.86.0", crows), ("nightly", nrows))}
            if not (trip["baseline"] == trip["control_1.86.0"] == trip["nightly"]):
                count_changes.append({"func": f, **trip})
        fam = "scalar" if h == "scalar" else "array/float"
        ledger_check = []
        for r in vrows:
            if r["exposure_breakdown"].get("family") != fam:
                continue
            f = r["translation"].split(" ")[0]
            ledger_check.append({
                "defect_id": r["defect_id"], "func": f, "severity": r["severity"],
                "count_status": r.get("count_status"),
                "shows_in_baseline": shows(brows.get(f), "scalar" if h == "scalar" else "arrays", r["severity"]),
                "shows_under_control_1.86.0": shows(crows.get(f), "scalar" if h == "scalar" else "arrays", r["severity"]),
                "shows_under_nightly": shows(nrows.get(f), "scalar" if h == "scalar" else "arrays", r["severity"]),
                "verdict_control_1.86.0": tc_c["verdicts"].get(f),
                "verdict_nightly": tc_n["verdicts"].get(f),
                "counts": {lab: {k: rr.get(f, {}).get(k) for k in keys}
                           for lab, rr in (("baseline", brows), ("control_1.86.0", crows), ("nightly", nrows))},
                "smallest_samples_nightly": (nrows.get(f, {}).get("value_samples") or nrows.get(f, {}).get("samples") or [])[:2]
                if r["severity"] == "S1" else (nrows.get(f, {}).get("rust_panic_samples") or nrows.get(f, {}).get("crash_samples") or [])[:2],
            })
        counted = [x for x in ledger_check if x["count_status"] == "counted"]
        out[h] = {
            "baseline": base,
            "grid": "v2 (systems/enumerate_inputs.py grid='v2', cap 80, non-negative, |x| <= 512)" if h == "scalar"
                    else "run_arrays.gen_cases(comps, 240) (unchanged since the shipped pinned run)",
            "functions_run": len(nig["toolchain_comparison"]["verdicts"]),
            "tested_in_baseline": len(tested),
            "summary_nightly": nig["summary"], "summary_control_1.86.0": ctl["summary"],
            "compile_nightly": tc_n["compile"], "compile_control_1.86.0": tc_c["compile"],
            "verdict_diffs_nightly": tc_n["diffs"],          # vs baseline and vs the same-session control
            "verdict_diff_control_vs_baseline": tc_c["diffs"]["primary"],
            "verdicts_nightly": tc_n["verdicts"], "verdicts_control_1.86.0": tc_c["verdicts"],
            "per_function_count_differences": count_changes,
            "ledger_rows": ledger_check,
            "ledger_rows_counted": len(counted),
            "ledger_rows_counted_still_shown_under_nightly": sum(x["shows_under_nightly"] for x in counted),
            "ledger_rows_counted_still_shown_under_control": sum(x["shows_under_control_1.86.0"] for x in counted),
            "provenance_nightly": nig.get("provenance"), "provenance_control_1.86.0": ctl.get("provenance"),
            "runs": {tc: runs.get((h, tc)) for tc in (CONTROL, PINNED)},
        }
    sc, ar = out["scalar"], out["arrays"]
    out["result"] = {
        "scalar_verdict_changes_nightly_vs_baseline": sc["verdict_diffs_nightly"]["primary"]["n_verdict_changes"],
        "scalar_verdict_changes_nightly_vs_control": sc["verdict_diffs_nightly"]["control_1.86.0"]["n_verdict_changes"],
        "scalar_counted_rows": sc["ledger_rows_counted"],
        "scalar_counted_rows_shown_under_nightly": sc["ledger_rows_counted_still_shown_under_nightly"],
        "array_verdict_changes_nightly_vs_baseline": ar["verdict_diffs_nightly"]["primary"]["n_verdict_changes"],
        "array_verdict_changes_nightly_vs_control": ar["verdict_diffs_nightly"]["control_1.86.0"]["n_verdict_changes"],
        "array_counted_rows": ar["ledger_rows_counted"],
        "array_counted_rows_shown_under_nightly": ar["ledger_rows_counted_still_shown_under_nightly"],
    }
    for h, x in (("scalar", sc), ("array", ar)):
        nb = x["verdict_diffs_nightly"]["primary"]["verdict_changes"]
        cb = x["verdict_diff_control_vs_baseline"]["verdict_changes"]
        out["result"][f"{h}_nightly_vs_baseline_changes_equal_control_vs_baseline"] = (
            sorted(json.dumps(c, sort_keys=True) for c in nb) == sorted(json.dumps(c, sort_keys=True) for c in cb))
        out["result"][f"{h}_functions_with_nightly_counts_different_from_control"] = [
            c["func"] for c in x["per_function_count_differences"] if c["nightly"] != c["control_1.86.0"]]
    out["result"]["note"] = ("A verdict or count that differs from the baseline file identically under BOTH toolchains is a "
                             "harness-version difference (the baselines predate the audit-8 harness changes), not a "
                             "compiler effect; the compiler effect is nightly vs the same-session 1.86.0 control.")
    import provenance as prov            # scripts/provenance.py (stamp only)
    out["_provenance"] = prov.stamp(image=os.environ.get("IMAGE_DESC"), step="arxiv_pinned_grid2")
    if out["_provenance"].get("git_commit") is None:
        out["_provenance"]["git_commit"] = os.environ.get("GIT_REV")
    out["_provenance"]["note"] = ("stamp() taken in the assembly step (its rustc is the container default); "
                                  "the toolchain of each run is in <harness>.provenance_*.toolchains")
    with open(os.path.join(REPO, a.out) if not os.path.isabs(a.out) else a.out, "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps(out["result"], indent=1))


if __name__ == "__main__":
    main()
