"""Held-out / own-inputs measurement of the VERT scalar defects on input grid v2.

Same measurement as harness/heldout_vert.py (scalar part) and harness/heldout_overlap.py
(vert_defects), imported unchanged; the one difference is OUR input grid: enumerate_cases(...,
grid="v2") instead of the v1 grid, and the function set is the grid-v2 run's
(results/vert_diff_grid2.json): every value_bug except those the full UB rule excludes
(results/vert_grid2_ub_recheck.json summary.without_clean_input_where_clang_also_agrees), plus the
Rust hangs. VERT's own inputs (Kani PARAM1 in {0,1} with the stub's other literals, the Kani Rust-side
point, the shipped TransCoder test tuples) do not depend on our grid.

Run in the image that produced results/vert_diff_grid2.json (arm64, crust-stable):
  docker run --rm -v "$PWD:/work" -w /work crust-stable python3 -I systems/vert/heldout_grid2.py
Writes results/heldout_vert_grid2.json (read by scripts/build_defect_ledger.py for the VERT scalar rows).
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
OUT = os.path.join(REPO, "results", "heldout_vert_grid2.json")


def main():
    vd = json.load(open(os.path.join(REPO, "results", "vert_diff_grid2.json")))
    ubx = set(json.load(open(os.path.join(REPO, "results", "vert_grid2_ub_recheck.json")))["summary"]
              ["without_clean_input_where_clang_also_agrees"])
    scalar = [(r, "wrong_answer") for r in vd["value_bugs"] if r["func"] not in ubx] + \
             [(r, "rust_hang") for r in vd["all"] if r.get("status") == "tested" and r.get("value_div", 0) == 0
              and r.get("rust_panic_c_ok", 0) == 0 and r.get("rust_hang", 0) > 0]
    only = [x for x in os.environ.get("ONLY", "").split(",") if x]
    jobs = []
    for r, kind in scalar:
        if only and r["func"] not in only:
            continue
        jobs.append((r["func"], {"counted_as": kind, "source": "results/vert_diff_grid2.json",
                                 "value_div": r["value_div"], "rust_hang": r.get("rust_hang", 0)}))
    from concurrent.futures import ProcessPoolExecutor
    with ProcessPoolExecutor(max_workers=int(os.environ.get("JOBS", "6"))) as ex:
        futs = [ex.submit(hv.scalar_function, f, e) for f, e in jobs]
        rows = []
        for (f, e), fu in zip(jobs, futs):
            res = fu.result()
            rows.append(res)
            print(f, e["counted_as"], res["reproduced"], "kani oracle-side diverges:",
                  res["our_comparison_on_vert_inputs"].get("kani_oracle_side_diverges"), flush=True)

    def ver(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True).stdout.splitlines()[0]
        except Exception:  # noqa: BLE001
            return None
    prov = {
        "script": "systems/vert/heldout_grid2.py (harness/heldout_vert.scalar_function and harness/heldout_overlap.vert_defects, unchanged, with grid='v2')",
        "command": "docker run --rm -v \"$PWD:/work\" -w /work crust-stable python3 -I systems/vert/heldout_grid2.py",
        "grid": "v2",
        "excluded_by_full_ub_rule": sorted(ubx),
        "git_rev": os.environ.get("GIT_REV"),
        "host": os.environ.get("HOST_DESC", platform.system()),
        "arch": platform.machine(),
        "toolchains": {"gcc": ver(["gcc", "--version"]), "rustc": ver(["rustc", "--version"]),
                       "clang": ver(["clang", "--version"]), "python": platform.python_version()},
        "image": os.environ.get("IMAGE_DESC"),
        "date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "inputs_read": ["artifacts/vert/benchmark/c_transcoder/<F>/{out-rwasm-mutated,out-rwasm-bolero}/src/main.rs",
                        "artifacts/vert/benchmark/c_transcoder/<F>/<F>_towasm_mutated.c",
                        "artifacts/vert/benchmark/c_transcoder/<F>/<F>.c",
                        "results/vert_diff_grid2.json", "results/vert_grid2_ub_recheck.json"],
    }
    out = {"provenance": prov, "functions": rows, "counted_defects": hov.vert_defects({"functions": rows})}
    rep = [r for r in rows if r["reproduced"]["value"] != r["reproduced"]["expected_value"]
           or r["reproduced"]["rust_hang"] != r["reproduced"]["expected_rust_hang"]]
    out["reproduction_check"] = {"all_reproduced": not rep, "mismatches": [{"function": r["function"], **r["reproduced"]} for r in rep]}
    json.dump(out, open(os.environ.get("OUT", OUT), "w"), indent=1)
    print("reproduced:", not rep, "rows:", len(rows))


if __name__ == "__main__":
    main()
