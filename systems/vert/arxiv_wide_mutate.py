"""arXiv (issues.md 47): mutation adequacy of the VERT scalar harness on the newly tested wide-return functions.

run.py --wide-int also tests the VERT functions whose C f_gold returns long / long long
(results/arxiv_vert_diff_grid2_wide.json, grid v2). This script runs the scalar mutation analysis of
systems/vert/mutate_primary.py on the ones among them that the harness calls "agree", with mutate_primary.py
itself unchanged (same lexer, operators ror/aor/const/f32 + bnd, cap 0, seed 20261113, grid v2, kill rule,
early-exit tally and its --verify-every check, equivalence phases A/B/C and budgets, summary code). Two
substitutions are made here, at import time, so that mutate_primary.py and every shipped result stay as they are:

  * population: mutate_primary.population() reads results/arxiv_vert_diff_grid2_wide.json instead of
    results/vert_diff_grid2.json, with the same rule (status "tested" and value_div = rust_panic_c_ok =
    rust_hang = ub_div = timeout_div = 0);
  * wide_int: run.test_function is called with wide_int=True, so the C return-type gate admits long / long long.
    The only call in mutate_primary.py is the control (job_control: the unmutated translation through
    run.test_function end to end). Nothing else there gates on the return type: build_oracles calls run.build_c,
    whose driver prints (long long) f_gold(...) for any return type, and the Rust driver prints the i32 result
    widened to i64, exactly as run.test_function does.

--verify-every counts mutants in this population's order (the 3 functions), so the verified subset is not the
one a combined 118-function run would pick; it only checks the early-exit tally against the full tally and
never changes a verdict.

Run (arm64 Mac, Docker, repo root; SCRATCH = a scratch directory):
  docker run --rm --cpus 5 --memory 4g -v "$PWD:/work" -v SCRATCH:/scratch -w /work \\
      -e MUTPRIM_WORK=/scratch/mutprim_wide -e GIT_REV=$(git rev-parse HEAD) -e HOST_DESC="macOS arm64, docker --cpus 5" \\
      -e IMAGE_ID=$(docker image inspect crust-stable -f '{{.Id}}') crust-stable \\
      python3 systems/vert/arxiv_wide_mutate.py --grid v2 --jobs 5 --verify-every 25 --equiv --budgets 30,10,10 \\
      --out results/arxiv_vert_wide_mutation_primary.json
"""
import functools
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import run  # noqa: E402
import mutate_primary as mp  # noqa: E402

WIDE_POP = "arxiv_vert_diff_grid2_wide.json"
DEFAULT_OUT = os.path.join(mp.REPO, "results", "arxiv_vert_wide_mutation_primary.json")
PROTECTED = {os.path.join(mp.REPO, "results", f) for f in
             ("vert_mutation_primary.json", "vert_mutation_primary_grid2.json", "vert_arrays_mutation_primary.json")}

# 1. population: the grid-v2 wide-int run, same rule as mutate_primary.population()
mp.POP_FILE = {**mp.POP_FILE, "v2": WIDE_POP}
# 2. wide_int=True wherever mutate_primary calls run.test_function (the control)
_test_function = run.test_function
run.test_function = functools.partial(_test_function, wide_int=True)

_provenance = mp.provenance


def provenance(argv):
    p = _provenance(argv)
    p["script"] = "systems/vert/arxiv_wide_mutate.py (systems/vert/mutate_primary.py, unchanged, with the two substitutions below)"
    p["substitutions"] = {
        "population_file": "results/" + WIDE_POP + " (instead of results/vert_diff_grid2.json; same selection rule)",
        "run.test_function": "called with wide_int=True (the control); the C return-type gate admits long / long long",
    }
    p["file_sha256"]["systems/vert/arxiv_wide_mutate.py"] = mp.sha(os.path.abspath(__file__))
    return p


mp.provenance = provenance


def main():
    argv = sys.argv[1:]
    if "--arrays" in argv or "--merge" in argv:
        raise SystemExit("arxiv_wide_mutate.py: scalar harness only (no --arrays / --merge)")
    if "--grid" not in argv:
        argv += ["--grid", "v2"]
    elif argv[argv.index("--grid") + 1] != "v2":
        raise SystemExit("arxiv_wide_mutate.py: the wide-int population is the grid-v2 run (--grid v2)")
    if "--out" not in argv:
        argv += ["--out", DEFAULT_OUT]
    out = os.path.abspath(argv[argv.index("--out") + 1])
    if out in PROTECTED:
        raise SystemExit(f"arxiv_wide_mutate.py: refusing to overwrite {out}")
    sys.argv = [sys.argv[0]] + argv
    mp.main()


if __name__ == "__main__":
    main()
