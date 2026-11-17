"""arXiv (issues.md 47): the full-rule UB filter on the kills of the wide-return mutation run.

systems/vert/arxiv_mutate_ubfilter.py (`run --harness scalar`) checks every counted kill of the scalar mutation scan
under the full UB rule (systems/vert/arxiv_ub_fullrule.py) and re-scans a mutant whose recorded kill input is not
UB-clean. This script runs that code unchanged (arxiv_mutate_ubfilter.run_mode -> job, scalar_scan, part format) on
results/arxiv_vert_wide_mutation_primary.json (systems/vert/arxiv_wide_mutate.py: the 3 functions whose C f_gold
returns long / long long and that agree on grid v2) instead of results/vert_mutation_primary_grid2.json, with two
substitutions made here at import time so that arxiv_mutate_ubfilter.py stays as it is:

  * SCALAR_SRC = results/arxiv_vert_wide_mutation_primary.json;
  * shipped_pins() = [] : the shipped scalar scan was two shards (arm64 shard 0, x86_64 shard 1) and shipped_pins
    reads shard 0's --pin list; the wide run is one unsharded arm64 run, so there is only --shard all (one part).

No wide_int is needed here: the job builds the C oracle with mutate_primary.build_oracles -> run.build_c, whose
driver prints (long long) f_gold(...) for any return type, never calls run.test_function (the only place with the
return-type gate), and the full rule compares the printed values of the C builds with the primary oracle's.
Output: arxiv_mutate_ubfilter.py's part-file format (part, provenance, sanitizer_positive_controls, functions,
_provenance), with provenance.wrapper added.

Run (arm64 Mac, as the shipped scalar shard 0 and the wide mutation run; crust-vert-toolchain has clang-14):
  docker run --rm --cpus 5 --memory 4g -v $PWD:/work -w /work -e RUSTUP_TOOLCHAIN=1.86.0 \\
      -e GIT_REV=$(git rev-parse HEAD) -e HOST_DESC="macOS arm64, docker --cpus 5" -e IMAGE_DESC="crust-vert-toolchain <id>" \\
      crust-vert-toolchain python3 -I systems/vert/arxiv_wide_ubfilter.py --jobs 5 --out results/arxiv_vert_wide_mutation_ubfilter.json
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import arxiv_mutate_ubfilter as amu  # noqa: E402

WIDE_SRC = "results/arxiv_vert_wide_mutation_primary.json"
PROTECTED = {os.path.join(amu.REPO, "results", f) for f in ("arxiv_mutation_ubfilter_vert.json",)}

amu.SCALAR_SRC = WIDE_SRC
amu.shipped_pins = lambda d: []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=5)
    ap.add_argument("--only", default="", help="comma-separated function names (testing)")
    ap.add_argument("--out", default=os.path.join(amu.REPO, "results", "arxiv_vert_wide_mutation_ubfilter.json"))
    a = ap.parse_args()
    if os.path.abspath(a.out) in PROTECTED:
        raise SystemExit(f"arxiv_wide_ubfilter.py: refusing to overwrite {a.out}")
    os.environ.setdefault("RUN_COMMAND", "python3 -I systems/vert/arxiv_wide_ubfilter.py " + " ".join(sys.argv[1:]))
    a.harness, a.shard = "scalar", "all"
    amu.run_mode(a)
    with open(a.out) as f:
        out = json.load(f)
    out["provenance"]["wrapper"] = {
        "script": "systems/vert/arxiv_wide_ubfilter.py",
        "runs": "systems/vert/arxiv_mutate_ubfilter.py run_mode (run --harness scalar --shard all), unchanged",
        "substitutions": {"SCALAR_SRC": WIDE_SRC + " (instead of " + "results/vert_mutation_primary_grid2.json)",
                          "shipped_pins": "[] (one unsharded arm64 run; no shard 0 / shard 1)"},
        "sha256": amu.sha("systems/vert/arxiv_wide_ubfilter.py"),
    }
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
        f.write("\n")


if __name__ == "__main__":
    main()
