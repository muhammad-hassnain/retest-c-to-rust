"""arXiv (issues.md 47): held-out / own-inputs measurement of the VERT wide-int wrong answers on grid v2.

Same measurement as systems/vert/heldout_grid2.py (results/heldout_vert_grid2.json): harness/heldout_vert.py
scalar_function and harness/heldout_overlap.py vert_defects, imported unchanged, with OUR input grid
enumerate_cases(..., grid="v2"). The function set is the wide-int run's (results/arxiv_vert_diff_grid2_wide.json,
run.py --wide-int): every value_bug except those the full UB rule excludes
(results/arxiv_vert_wide_ub_recheck.json summary.without_clean_input_where_clang_also_agrees), plus Rust hangs.
heldout_vert.scalar_function builds the C with run.build_c, whose driver prints (long long) f_gold(...), and
the Rust with run.build_rust (i32 widened to i64), i.e. the --wide-int comparison; it does not look at the
return type.

VERT's recorded inputs are those its released commands run: none for the bolero replay (no corpus is
shipped for these functions: vert_inputs.bolero_corpus_files), and for bounded Kani the oracle-side tuples
{(n, stub[1:]) : n in the kani::assume range} (n in {0, 1}) plus the Rust-side static point.

Run in the image that produced results/arxiv_vert_diff_grid2_wide.json (arm64, crust-stable):
  docker run --rm --cpus 4 -v "$PWD:/work" -w /work -e HOST_DESC=... -e IMAGE_DESC=... -e GIT_REV=... \
      crust-stable python3 -I systems/vert/arxiv_wide_heldout.py
Writes results/arxiv_heldout_vert_wide.json.
"""
import datetime
import functools
import json
import os
import platform
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "harness"))
import heldout_vert as hv  # noqa: E402  (inserts systems/ and systems/vert/ on sys.path)
import heldout_overlap as hov  # noqa: E402
from enumerate_inputs import enumerate_cases  # noqa: E402

hv.enumerate_cases = functools.partial(enumerate_cases, grid="v2")
SCALAR = "results/arxiv_vert_diff_grid2_wide.json"
UB = "results/arxiv_vert_wide_ub_recheck.json"
OUT = os.path.join(REPO, "results", "arxiv_heldout_vert_wide.json")


def main():
    t0 = datetime.datetime.now(datetime.timezone.utc)
    vd = json.load(open(os.path.join(REPO, SCALAR)))
    ubx = set(json.load(open(os.path.join(REPO, UB)))["summary"]["without_clean_input_where_clang_also_agrees"])
    scalar = [(r, "wrong_answer") for r in vd["value_bugs"] if r["func"] not in ubx] + \
             [(r, "rust_hang") for r in vd["all"] if r.get("status") == "tested" and r.get("value_div", 0) == 0
              and r.get("rust_panic_c_ok", 0) == 0 and r.get("rust_hang", 0) > 0]
    jobs = [(r["func"], {"counted_as": kind, "source": SCALAR, "value_div": r["value_div"],
                         "rust_hang": r.get("rust_hang", 0)}) for r, kind in scalar]
    from concurrent.futures import ProcessPoolExecutor
    with ProcessPoolExecutor(max_workers=int(os.environ.get("JOBS", "4"))) as ex:
        futs = [ex.submit(hv.scalar_function, f, e) for f, e in jobs]
        rows = []
        for (f, e), fu in zip(jobs, futs):
            res = fu.result()
            res["c_return"] = next(r.get("c_return") for r in vd["all"] if r["func"] == f)
            rows.append(res)
            print(f, e["counted_as"], res["reproduced"], "kani oracle-side diverges:",
                  res["our_comparison_on_vert_inputs"].get("kani_oracle_side_diverges"), flush=True)
    t1 = datetime.datetime.now(datetime.timezone.utc)

    def ver(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True).stdout.splitlines()[0]
        except Exception:  # noqa: BLE001
            return None
    prov = {
        "script": "systems/vert/arxiv_wide_heldout.py (harness/heldout_vert.scalar_function and harness/heldout_overlap.vert_defects, unchanged, with grid='v2'; as systems/vert/heldout_grid2.py)",
        "command": os.environ.get("RUN_COMMAND", "docker run --rm -v \"$PWD:/work\" -w /work crust-stable python3 -I systems/vert/arxiv_wide_heldout.py"),
        "grid": "v2",
        "comparison": "C driver prints (long long) f_gold(...) (run.build_c); VERT's Rust returns i32, printed widened to i64 (run.build_rust): the run.py --wide-int comparison",
        "excluded_by_full_ub_rule": sorted(ubx),
        "vert_recorded_inputs": "bolero: the documented replay (cargo bolero reduce) runs only a shipped corpus, and none is shipped for these functions (vert_inputs.bolero_corpus_files); bounded Kani: the C copy (rWasm oracle) gets n in the kani::assume range, {0, 1}, with the C stub's other literals, and the Rust the static PARAM values (vert_inputs.kani_*)",
        "git_rev": os.environ.get("GIT_REV"),
        "git_dirty_note": os.environ.get("GIT_DIRTY"),
        "host": os.environ.get("HOST_DESC"),
        "arch": platform.machine(),
        "toolchains": {"gcc": ver(["gcc", "--version"]), "rustc": ver(["rustc", "--version"]),
                       "clang": ver(["clang", "--version"]), "python": platform.python_version()},
        "image": os.environ.get("IMAGE_DESC"),
        "date": t1.isoformat(),
        "date_start_utc": t0.isoformat(), "date_end_utc": t1.isoformat(),
        "inputs_read": ["artifacts/vert/benchmark/c_transcoder/<F>/{out-rwasm-mutated,out-rwasm-bolero}/src/main.rs",
                        "artifacts/vert/benchmark/c_transcoder/<F>/<F>_towasm_mutated.c",
                        "artifacts/vert/benchmark/c_transcoder/<F>/<F>.c",
                        SCALAR, UB],
    }
    out = {"provenance": prov, "functions": rows, "counted_defects": hov.vert_defects({"functions": rows})}
    rep = [r for r in rows if r["reproduced"]["value"] != r["reproduced"]["expected_value"]
           or r["reproduced"]["rust_hang"] != r["reproduced"]["expected_rust_hang"]]
    out["reproduction_check"] = {"all_reproduced": not rep, "mismatches": [{"function": r["function"], **r["reproduced"]} for r in rep]}
    json.dump(out, open(os.environ.get("OUT", OUT), "w"), indent=1)
    print("reproduced:", not rep, "rows:", len(rows))


if __name__ == "__main__":
    main()
