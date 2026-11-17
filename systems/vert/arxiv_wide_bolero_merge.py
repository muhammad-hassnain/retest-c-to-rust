"""arXiv (issues.md 47): assemble VERT's documented bolero step for the wide-int wrong answers.

The runs are systems/vert/bolero_vacuity/run_in_container_current.sh + check_current.py, unchanged (same
container setup, three arms vert_reduce / planted_reduce / fuzz_30s, commands, env, 600 s timeout, plant(),
VERT's verdict rule), started with FUNCS_OVERRIDE=<the wide-int value bugs> and OUTDIR=<work dir>. This script
does what `check_current.py --merge` does (same summary fields and formulas, same output layout as
results/vert_bolero_vacuity_current.json), except that the expected row set is the wide-int run's value bugs
(results/arxiv_vert_diff_grid2_wide.json, run.py --wide-int) minus those the full UB rule excludes
(results/arxiv_vert_wide_ub_recheck.json); check_current.py --merge asserts the int-return set instead.

  python3 -I systems/vert/arxiv_wide_bolero_merge.py <benchmark_dir> <out.json> <provenance.json> <part.json> [...]
(run from the repo root with the full artifacts/vert/benchmark/c_transcoder as <benchmark_dir>)
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(HERE, "bolero_vacuity"))
from check_current import ARMS, corpus_files  # noqa: E402  (unchanged)

SCALAR = "results/arxiv_vert_diff_grid2_wide.json"
UB = "results/arxiv_vert_wide_ub_recheck.json"


def main():
    bench, out_path, prov_path, parts = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
    rows = []
    for p in parts:
        d = json.load(open(p))
        assert d["partial"] is False, p
        rows += d["rows"]
    rows.sort(key=lambda r: r["function"])
    vd = json.load(open(os.path.join(REPO, SCALAR)))
    ubx = set(json.load(open(os.path.join(REPO, UB)))["summary"]["without_clean_input_where_clang_also_agrees"])
    cur = [v["func"] for v in vd["value_bugs"] if v["func"] not in ubx]
    fs = [r["function"] for r in rows]
    assert sorted(fs) == sorted(cur) and len(fs) == len(set(fs)) == len(cur), f"row set != wide set {len(cur)}"

    def cnt(arm, v): return sum(1 for r in rows if r["arms"].get(arm, {}).get("vert_verdict") == v)
    summ = {arm: {v: cnt(arm, v) for v in ("pass", "fail", "compile_error", "timeout")} for arm in ARMS}
    summ["functions"] = len(rows)
    summ["procedure"] = "VERT ARTIFACT_README 'How to Run Validation Directly' (PBT): mkdir -p src/__fuzz__; RUSTFLAGS='-C overflow-checks=false' cargo bolero reduce bolero_wasm_eq; pass iff no 'Test Failure'"
    oba = {}
    for arm in ARMS:
        c = {}
        for r in rows:
            o = r["arms"][arm].get("outcome", "skipped")
            c[o] = c.get(o, 0) + 1
        oba[arm] = c
    summ["outcomes_by_arm"] = oba
    summ["vert_reduce_aborted_before_any_input"] = sum(
        r["arms"]["vert_reduce"]["outcome"] == "aborted_empty_corpus"
        and not r["arms"]["vert_reduce"]["evidence"]["libfuzzer_started"]
        and not r["arms"]["vert_reduce"]["evidence"]["merge_inner_total_files"] for r in rows)
    summ["vert_reduce_no_input_evidence_any_outcome"] = sum(
        not r["arms"]["vert_reduce"]["evidence"]["libfuzzer_started"]
        and not r["arms"]["vert_reduce"]["evidence"]["merge_inner_total_files"] for r in rows)
    summ["functions_with_shipped_corpus"] = sum(r["shipped_corpus_files"] > 0 for r in rows)
    B = bench
    names = sorted(x for x in os.listdir(B) if os.path.isdir(os.path.join(B, x)))
    summ["benchmarks"] = len(names)
    summ["benchmarks_shipping_a_corpus"] = sum(corpus_files(os.path.join(B, x, "out-rwasm-bolero")) > 0 for x in names)
    csvr = {}
    for line in open(os.path.join(B, "..", "..", "c_results.csv")):
        p = [x.strip() for x in line.split(",")]
        if p and p[0]:
            csvr[p[0]] = p
    summ["vert_pbt_pass_total"] = sum("bolero=1" in v for v in csvr.values())
    summ["vert_pbt_pass_without_shipped_corpus"] = sum(
        "bolero=1" in v and corpus_files(os.path.join(B, k, "out-rwasm-bolero")) == 0 for k, v in csvr.items())
    summ["current_set_recorded_bolero_1_by_vert"] = sum("bolero=1" in csvr.get(f, []) for f in cur)
    summ["functions_note"] = (f"exactly the {len(cur)} VERT wide-int wrong answers on grid v2 ({SCALAR} value_bugs, run.py --wide-int"
                              + (f", minus {sorted(ubx)} excluded by the full UB rule in {UB})" if ubx else f"; none excluded by the full UB rule in {UB})"))
    summ["grid"] = "v2"
    summ["vs_previous_run"] = ("same scripts and arms as results/vert_bolero_vacuity_current.json (the int-return counted set); "
                               "this file covers only the wide-int functions (the run copy held only their artifact directories; "
                               "benchmarks / corpus / pbt totals above are computed at merge time on the full <benchmark_dir>)")
    prov = json.load(open(prov_path))
    res = {"provenance": prov, "summary": summ, "rows": rows}
    json.dump(res, open(out_path, "w"), indent=1)
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()
