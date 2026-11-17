# ACTOR (UW): re-grade notes

**System.** ACTOR, agentic C-to-Rust translation (Benedikt Schesch and Michael D. Ernst, "Agentic C
to Rust Translation", IEEE Security & Privacy 24(5), 2026). Code: github.com/UW-HARVEST/ACTOR (MIT).
This is a different system from *ACToR* by NUS and Amazon (arXiv 2510.03879).

**Re-graded here: yes**, on a rule-defined subset (below). All third-party code was built and run only
inside Docker (`crust-heldout` image, `--network none`) on the x86 box.

## Artifact and version

* `systems/actoruw/fetch.sh` pins ACTOR at **1b1e70a** (2026-08-19) and its `results` submodule
  (harvest-agentic-translate-results) at **e31aec03**. 1b1e70a is the last ACTOR commit whose
  generated `tables/numbers.tex` gives the paper's TRACTOR headline (Kiro 325/338, Claude Code
  319/338, Codex 244/338). From 2026-08-21 on, the results were re-run with other models (HEAD
  54daf8b, 2026-09-10: Kiro 332/338 with `claude-opus-5`). We grade the translations behind the
  paper's numbers.
* The results repository is about 9 GB at HEAD (an agent-invocation cache). We fetch the one
  commit without blobs and check out only `Test-Corpus/kiro/`, without agent logs.
* Source digest of `Test-Corpus/kiro` (without the agent's `c_src/build*/` CMake products):
  `c035d74f...c780` (30,700 files). It is the same on the Mac and on the x86 copy.
* We grade **ACTOR (Kiro)**, the paper's headline configuration. For each case we use the directory
  the scorer uses: `verified/` if present, otherwise `translated/`. Two cases, `macrodepth_mul_4`
  and `macrodepth_sub_6`, have only `translated/`.

## Does the re-grade rule hold?

**Yes.** Each case directory ships the original C (`c_src/`, with its CMakeLists) next to ACTOR's
own Rust crate. We checked that:

* all 61 executable translations build, both C and Rust;
* 88 of the 89 eligible library translations build, both C and Rust;
* both sides run.

**Toolchain.** ACTOR pins Rust `channel = "1.94.0"` in `rust-toolchain.toml`, and the scorer builds
under that root. The 1.94.0 stable release was not available offline.
We therefore built with the host's `nightly-2026-01-10` (rustc 1.94.0-nightly a3f2d5abe), mounted
read-only. Crates were resolved `--offline` from the host crate cache, because no `Cargo.lock`
ships; the resolved versions are recorded per crate.

As a cross-check, the 40 translations that had a counted divergence when this was run (37
executables, 3 libraries) were rebuilt with rustc 1.99.1; all reproduce every counted Rust output
(panic thread ids normalised). So no finding depends on the toolchain.

**The benchmark's own test vectors are not in the artifact.** They live in the `test-corpus`
submodule, `github.com/UW-HARVEST/Test-Corpus` @6fefd73. On 2026-11-13, `git ls-remote` answered
"Repository not found", so the submodule is not public. The paper cites the upstream DARPA/MIT LL
public corpus; we did not download it.

## Method (same as the other systems)

* **Oracle.** The C, built with gcc from its own CMakeLists recipe (sources, include directories,
  options, `CMAKE_C_FLAGS`; no `-O`, which is CMake's default build type). The three `macrodepth`
  cases are configured as their names say (`OP`/`REPEAT`), and their Rust with the matching Cargo
  features.
* **Executables (61).** Compared on stdout, exit code and stderr, byte for byte. Both sides run
  with `argv[0]="driver"`, a fresh working directory per case, a 5 s timeout, and output capped at
  16 MiB by `RLIMIT_FSIZE`.
  * Inputs come from `gen_inputs.py`, by program shape read statically from the C:
    * a fixed stdin corpus: the text corpus of `harness/gen_inputs.py`, plus a numeric corpus from
      the per-argument integer grid of `systems/enumerate_inputs.py` (singletons, and
      R_d-spread tuples of 2/3/4/6 values and of the C's widest scanf arity);
    * an argv grid (arities 1–4 over int/float/text tokens, plus the option literals in the C)
      when the C indexes `argv`;
    * file, missing-file and directory sources when the C opens files.
  * 9,130 inputs in all, 105 to 414 per program.
* **Libraries.** `lib_harness.py` grades a library when every function its public header declares
  takes scalar or `const char*` arguments and returns a scalar or void. 89 of 149 libraries qualify.
  * One generated C caller per library is linked once with the C sources (the oracle) and once
    against ACTOR's cdylib, so the Rust is tested as the drop-in C-ABI replacement TRACTOR requires.
  * There is one call per process.
  * Inputs are per-argument grids, at most 80 per function, with an R_d spread for several
    arguments (VERT's rule).
  * Float returns are compared with a relative tolerance of 1e-6.
  * 6,363 calls in all.
* **UB rule.** `ub_filter.py`, using `harness/oracle_ub_common.py`. A divergence counts only if the
  C on that input passes all of:
  * clang-14 ASan+UBSan+float-cast-overflow;
  * gcc-11 ASan+UBSan;
  * identical output at gcc -O1 and -O2;
  * identical output with `-funsigned-char` and with `-fsigned-char`;
  * a reproducible oracle.

  The positive controls fire as expected. A supplementary run with
  `detect_stack_use_after_return=1` excluded one more library case (`015_return_stack_buffer_lib`,
  which returns a dangling stack pointer). The common configuration cannot see that UB.

## Coverage

**149 of the 211 Kiro translations were re-graded:** all 61 executables and 88 libraries.

The other 62 are not graded:

* 60 libraries whose interfaces take pointers or structs. A generated caller cannot drive these
  without per-library harness code.
* `call_predict_lib`: the C library does not define the function its header declares,
  `get_predict_func`, so no oracle links.
* P01 SPHINCS+: one translation in 128 configurations, KAT-driven. Not attempted.

## Results (counted = UB-clean, toolchain-stable; `results/defect_ledger.json`)

* **36 of the 149 graded translations contain a counted defect:** 35 of the 61 executables and
  1 of the 88 libraries.
* **57 counted defects:** 45 wrong outputs and 12 crashes (Rust panics).
* **26 of the 36 were accepted** by the system's own check (recorded `passed: true`); 10 were
  rejected. Of all graded translations, 138 are accepted and 11 rejected.
* **Not counted (4):** `027_ctype_ascii_lib` prints the value `isdigit()` returns, which ISO C leaves
  unspecified; `underhanded-c-luggage` depends on uninitialised stack contents in the C; `strcmp`'s
  replay inputs pass 999999999999999 to `atoi` (UB in ISO C), which excludes its PARSE_NUM row (its
  NUL_BYTE and WS_SET defects count); `char-to-bool`'s mechanism is not confirmed in the source.
* `memmove`'s PARSE_NUM defect is counted on the second annotator's three hand-written inputs, not on
  our generated grid (disclosed in the paper's Threats).

Counted defects by mechanism:

| Mechanism | Defects | Kind | What happens |
|---|---|---|---|
| PARSE_NUM | 28 | wrong output | Rust `str::parse` rejects what C's scanf/strtol/atoi/atof accept. Examples: the numeric prefix of "3.14159" or "1e10"; " 12"; "" for strtol; "-1" for `%u`; hex floats; the first number of a line or of all of stdin. |
| UTF8_PANIC | 10 | crash | `read_to_string(..).unwrap()` panics on non-UTF-8 stdin. |
| LINE_SPLIT | 7 | wrong output | C `fgets(buf, N)` splits lines longer than N−1 bytes; the Rust reads whole lines. |
| UTF8_EOF | 4 | wrong output | Non-UTF-8 input is treated as end of input. |
| TOKEN_PANIC | 2 | crash | Missing or malformed tokens make the Rust panic, where the C keeps defaults: `043_iso646_and_digraphs`, and `001_perlin_noise` (P00), which panics on every input with fewer than 12 numbers. |
| ASSERT_DROPPED | 1 | wrong output | `hashmap-tree` drops the C's side-effecting `assert(tree_add_node(..))` calls. |
| BREAK_LOOP | 1 | wrong output | `mutable-duplication-dag`: C's `break` in a switch becomes a Rust `break` out of the menu loop. |
| NUL_BYTE | 1 | wrong output | `strcmp` |
| WS_SET | 1 | wrong output | `strcmp` |
| NAN_FMT | 1 | wrong output | `007_errno-pow` prints `NaN` where C prints `nan`. |
| SHIFT_SIGN | 1 | wrong output | Library `checkshift_lib`: the Rust uses a logical `>>` where the C shifts a negative int arithmetically. Implementation-defined in ISO C. |

Every defect has a confirmed `file:line` in `defects.py` (`LOCATIONS`) and appears in
`results/actoruw_summary.json` with examples.

## Held out

**Executables.** The released results record, in `<battery>/test.log` (runtests `--verbose`), each
executable vector's full argv and a stdin preview, cut at about 1,000 bytes. That is 1,164 vectors,
22 of them truncated.

* Only 34 of our 9,130 inputs (0.37%) are among them.
* **47 of the 56 counted executable defects diverge only on inputs outside that set** (the paper's
  "only on inputs the check never ran"). The other 9, in 6 translations the system rejected, also
  diverge on a recorded vector.
* We also replayed the 1,129 non-truncated system inputs through our comparison:
  * There are 57 UB-clean divergences, all in 9 translations the system **rejected**.
  * There are 0 divergences in accepted translations, so on the system's own inputs our C oracle
    agrees with the outputs the benchmark expected.
  * `underhanded-c-luggage` diverges only on the system's inputs, not on ours. Our generator does
    not produce its record format, which is a known gap in our grid.
  * `macrodepth_mul_4` and `macrodepth_sub_6` agree with the C on all 12 system inputs when they
    are built in the configuration under test. The system rejected them because its scorer built
    the default Cargo features, `add`/`5`.

**Libraries.** Not measurable. Library vectors are JSON files in the non-public submodule, and the
log records only that they ran.

**Survey.** The survey codes Held-out as Yes (with the prompt-tuning caveat). This measurement
agrees for executables.

## Harness power

`mutate.py` (model: `systems/adatrans/mutate.py`) seeds one fault per mutant into the Rust source
(`src/**/*.rs`) of every translation that agrees with the C on all our inputs: 19 executables and 75
libraries (one with only C-timeout inputs). Operators: relational (`<`/`<=`, `>`/`>=`, `==`/`!=`),
arithmetic (`+`/`-`, `*`->`+`, `+=`/`-=`), and integer constants +-1. Two sites per class per
translation, drawn with a fixed seed. Each mutant is rebuilt as in the re-test and re-run on the same
inputs against the same C binaries. Kill rule as in `results/harness_power.json`: wrong answer (stdout
or exit code; for libraries, the float tolerance on `#ret`) or crash. Stderr-only differences and hangs
do not count. A mutant with a hang and no kill is undetermined. Non-compiling mutants are excluded.

Run on the x86 box (`~/work/actor-mut`, image `crust-heldout`, 1412 s), 2026-11-13:

* Mutated: 84 translations (19 executables, 65 libraries). 9 libraries have no site, and
  `021_complex_goto_lib` is excluded because its unmutated rebuild hangs on one input.
* Results: 402 mutants, 9 stillborn, 393 compiled. 183 killed (156 wrong answer, 27 crash), 206
  survived, 4 undetermined. Kill rate 46.6% (Wilson 95% CI 41.7 to 51.5%).
  * Executables: 31/82 (37.8%).
  * Libraries: 152/311 (48.9%).
* 67 survivors are byte-identical to the unmutated build, so they are equivalent. Equivalent mutants are
  not removed, so the rate is a lower bound.

Per-mutant records: `results/actoruw_mutation.json`. Summary: `results/actoruw_summary.json`
`harness_power`.

An earlier run of a different `mutate.py` (4 or 3 random sites per operator) counted stderr-only
differences as kills and excluded byte-identical mutants. Its outputs never reached `results/` and
are superseded.

## Files

* Scripts:
  * `systems/actoruw/*.py` and `*.sh`: `run_in_container.sh` wraps every step.
  * `defects.py`: the mechanism rules and source locations.
  * `build_summary.py`: assembles `results/actoruw_summary.json`.
* Results: `results/actoruw_*.json`. Each file has a `provenance` object.

## arXiv version (2026-11-16)

- Harness power with kills counted only on inputs where the C has no UB: 166 of 393 (42.2%), replacing the
  46.6% above (`results/arxiv_mutation_ubfilter_actor.json`; 3 recorded kills did not reproduce because
  the C overflows a signed integer there).
