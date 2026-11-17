# AdaTrans re-grade (held-out differential testing)

Status: re-graded with the same method as the other systems in the paper. Every number
comes from ONE deterministic driver, `pipeline.sh` (stages fetch, vendor, extract, build, gen, diff,
ub, analyze, confirm, heldout, mutate, check, summarize; `pipeline.sh all`; each stage skips when its
output exists, `--force` re-runs). `results/adatrans_summary.json` is the single source.

Reproduce (x86-64 Linux with Docker, the `crust-heldout-plus186` image, and a cargo registry cache
holding the crate versions in `Cargo.lock`):

    ADATRANS_JOBS=3 bash systems/adatrans/pipeline.sh all

## Artifact

* Repo: https://github.com/SlainTroyard/adatrans_dev @ `31f4031aadb7ee9d4a2fb1a180e78ae9b45e5578`
  (single commit, 2026-09-20), fetched by `fetch.sh` into `artifacts/adatrans/` (gitignored).
  `git archive` tree sha256 `49e132b5991856c92deedab7e96d171ac2b6b6bf1c30d7cb6cc7cf632ba99231`.
* Zenodo 10.5281/zenodo.22258998, `adatrans-artifacts.tar.zst` (743,741,968 bytes), streamed on the
  x86 box; sha256 `03110aba3b469b37c56daf931dd6bb0e2ead37ac8462900da39c5e795493d6fe` and md5
  `f9cae5925e309586a225d814857e4753`, both equal to the values published in `bundles/MANIFEST.md`
  and on Zenodo. Only `dataset_fuzz_outputs/` (104 problem dirs, each `examples` + `outputs`) was
  extracted: these are the inputs of AdaTrans's own two checks. Nothing else was downloaded except
  that archive and the repo.
* All third-party code (the C, the Rust, the artifact's fuzz scripts) was built and run only inside
  Docker (`crust-heldout-plus186`: Ubuntu 22.04, gcc 11.4.0, clang 14.0.0, rustup toolchain
  1.86.0), `--network none`, on the x86-64 Linux box, at most 3 cores.

## What is a translation, and which one is "final"

104 C programs (`dataset/C_CPP/C/src/weekly_contest_{413..438}_p{1..4}.c`, LeetCode solution +
scanf/printf main). AdaTrans's Rust per program per run is in
`results/adatrans_full/run{1,2,3}/conversations/C_<c>_<p>.json`.

Final = the artifact's own definition (`analysis/metrics.py::read_run`): the last non-empty
`extracted_code` of the conversation, "the program actually delivered for that instance". For a
loop SUCCESS that is the SUCCESS iteration's code (`src/core.py::optimize` returns it); for a loop
FAILURE it is the last attempt (counted by the artifact in its 104-program denominators).
`extract_final.py` writes them and checks that the C in each conversation's first prompt is the
dataset C byte for byte (104/104 in every run), so the C we use as oracle is the C AdaTrans
translated.

Run rule: **run1 is the primary re-grade** (the run the README's reproduce command writes, and the
run the paper's sensitivity tables use as "Default"). run2 and run3 are built (compile check below)
but not re-graded in the results; `ADATRANS_RUNS="run1 run2 run3"` runs them through the same
pipeline (their defects would still need triage).

## Build (validated against the artifact's own verdicts)

* C: `gcc -w <c> -lm` (the artifact's fuzz scripts compile `gcc <c> -o solution`), plus the UB-rule
  builds of `harness/oracle_ub_common.py` (clang/gcc ASan+UBSan+float-cast-overflow, -O1, -O2,
  -funsigned-char, -fsigned-char). All 104 build with gcc. Three (C_424_4, C_428_2, C_430_3) use GCC
  nested functions, which clang cannot compile, so the clang sanitizer leg of the UB rule cannot
  run for them; we treat that leg as failed (divergences on those programs are not counted).
* Rust: exactly the artifact's project (`src/runner.py::_init_cargo_project`: edition 2021; libc
  0.2, rand 0.8, regex 1, lazy_static 1.4), `cargo build` debug profile (the artifact's check runs
  `target/debug/solution`), so integer overflow panics as it did in the artifact's runs. The
  artifact pins only "a stable Rust toolchain"; we use 1.86.0. Crates resolved offline from the box's
  cargo cache and vendored (rand 0.8.8, regex 1.13.1, libc 0.2.191, lazy_static 1.5.1).
* Check: our build fails for exactly the translations whose final iteration the artifact records as
  COMPILE_ERROR: run1 4/4 (C_416_2, C_419_2, C_433_2, C_437_3), run2 4/4, run3 6/6; the other
  298 build. So the toolchain reproduces the artifact's own compile verdicts.

## Inputs (gen_inputs.py)

Shape: function-like programs reading their arguments from stdin. The per-problem input format and
constraint bounds come from the artifact's own `constraints/*_fuzz.py` scripts, whose `random`
source we replace with a deterministic grid source (per-argument grid: min, min+1, min+2, max-2..max,
0, +-1, +-2^k, +-2^k+-1, a 9-point even sweep, half the time an even spread over the full range so
rejection loops end; values for call c of input k chosen by the Kronecker sequence frac(k*sqrt(p_c));
input 0 = every draw at its minimum, input 1 = every draw at its maximum). At most 80 inputs per
program, duplicates removed. The script is executed inside Docker with subprocess/file writes
stubbed; the stdin it would have fed the C is the input. No model is queried.
Determinism: there is no seed; input k is a pure function of (script, k, draw index). A case is
skipped only by deterministic rules: the script raises, or it exceeds a budget of 20,000,000 ops
under a cost model applied by rewriting the script's AST (1 op per loop iteration, len(container)
ops per `in` test, 1 op per random draw). Some scripts are quadratic (418_2 tests
`[a, b] in invocations`; 416_1 `word in message`; 424_2/424_3 loop over every query range), so
the budget, not the machine, decides which of their large cases are dropped; those programs get
fewer than 80 inputs (see the manifest). A 300 s wall-clock safety net only stops a runaway
script and is reported if it fires (`wallclock_safety_net_fired`).
Limit: the generated inputs inherit each script's ranges, which are sometimes narrower than the
LeetCode constraints (e.g. 413_1 draws rows '1'..'7', not '8').

## Comparison and UB rule (run_diff.py run / run_diff.py ub)

Every observable channel: stdout (bytes), exit code, stderr. `whitespace_only` (stdout equal after
folding whitespace, the artifact's own `_normalize`) is reported but not counted (cosmetic, as the
Rosetta fold). Timeouts are CPU-time limits (RLIMIT_CPU), not wall clock, so a verdict does not
depend on the load of the shared machine: C and Rust 10 s of CPU (the artifact's fuzz check uses 10 s
wall); every Rust timeout is re-run at 60 s of CPU before it is called a hang.
The output of a timed-out run is discarded (it is partial). A divergence is counted only if the C on
that input passes ALL of: clang-14 ASan+UBSan+float-cast-overflow, gcc-11 ASan+UBSan+float-cast-
overflow (both -O0, -fno-sanitize-recover=all), identical stdout+exit at -O1 and -O2, identical under
-funsigned-char and -fsigned-char, identical on a repeat run. Sanitizer report text is stored with
PIDs and addresses normalised. Per-case rows from the parallel workers are merged in sorted order
(instance, source, index), so the case files are byte-stable.

## Results (run1; `results/adatrans_summary.json`)

Coverage: run1, 104/104 programs re-graded (100 translations build). run2/run3 translations are
built (the compile verdicts above) but not re-graded in the pipeline output (`ADATRANS_RUNS` selects
runs; not run for lack of time).

* 10 translations with a counted defect, 10 defects: 7 wrong outputs (C_425_4, C_426_1, C_438_3,
  C_417_2, C_434_4, C_415_4, C_434_2) and 3 crashes (C_432_4, C_421_2, C_430_2). Not counted: one hang
  (C_416_4) and 4 build failures (C_416_2, C_419_2, C_433_2, C_437_3). Mechanism, file:line, example,
  exposure and confirmation per defect are in the summary (`defects`) and in `defects_run1.json`.
* Confirmation: 10 patches (`patches_run1.json`, `confirm.py`) each make every counted divergent case
  of their translation agree with the C; C_415_4's patch is its loop fix plus a rolling-hash speed-up
  (needed only to finish within confirm.py's 10 s CPU limit). C_434_2 (a rewritten function) is
  confirmed by reading. C_415_4's quadratic hashing makes it slow (AdaTrans's own fuzz check timed it
  out at 10 s wall) but it finishes within our 60 s CPU hang limit, so it is not a hang defect.
* Acceptance of the 10 defective translations: accepted 2 (C_432_4, C_425_4), rejected 8 (C_434_2 by
  the loop, the others by the fuzz oracle). The uncounted hang C_416_4 is rejected by the fuzz oracle too. Our re-run of AdaTrans's check reproduces all 16 of its run1 "not solved" verdicts.
* Held out: 8,279 of our 8,310 inputs are not among the 7,477 check inputs. C_432_4 (accepted, panic on
  n=1) diverges only on a held-out input; C_425_4 (accepted) only on AdaTrans's own examples, whose
  header gives the edge count where the C reads the node count.
* Not counted (UB rule): C_416_3, C_433_3, C_421_2 (2 cases), C_418_3, C_420_4; and C_424_4's 3 panics,
  whose C clang cannot compile (nested functions) although every other check passes.
* Harness power: 413 mutants (2 sites per class: relational, arithmetic, constant ±1; deterministic site
  choice) on the 76 agreeing translations, 328 killed / 82 survived / 3 stillborn (kill rate 0.80, raw:
  equivalent mutants not removed). Under the figure's one kill rule (wrong answer or crash; hangs not
  counted) 311 of 410 (75.9%): 17 mutants stopped at a hang, and mutate.py stops at the first
  non-agreeing input, so whether a later input gives a wrong answer is unknown (results/harness_power.json).
* Loop oracle: on 7 problems the C does not reproduce AdaTrans's recorded example outputs; 4 defects
  (C_417_2, C_425_4, C_434_4, C_438_3) pass those examples while the C fails them, and 2 faithful
  translations (C_417_3, C_420_1) were rejected by the loop because of them.

Determinism (`check` stage, compare_runs.py; recorded under `determinism` in the summary): the inputs
were regenerated and the diff, UB-rule and analyze stages re-run on the same machine. Inputs: identical
(8,310 files). diff_run1.json (all verdicts and counts): identical. The two case files (15,787 rows)
differ in 5 rows, all C_415_4, and only in `rust_rerun_long_timeout` (whether the Rust's first 10 s
CPU run timed out before its 60 s re-run; it needs about 10 s of CPU on those inputs); with that
duration field removed they are identical. summarize.py assembles the summary twice per run and
compares (identical, provenance excluded).

Hand-triage data (not regenerated by the pipeline): defects_run1.json (mechanism, locations) and
patches_run1.json (the exact source edits confirm.py applies, written once from the line ranges in
defects_run1.json).

## arXiv version (2026-11-16)

- C_425_4 is not counted: it diverges only on AdaTrans's own examples, whose header gives the edge count
  where the C reads the node count, so the exposing inputs violate the C's stated input format (rule ii).
  Now 9 defects in 9 translations, of which AdaTrans solved 1 (C_432_4, a panic at n=1 that no fuzzing input
  reaches). 3 of the 9 defects follow the recorded outputs, not the C.
- Harness power: at least 75% (311 of 410, 75.85%), a lower bound: mutants that stopped at a hang are not
  counted as killed.
