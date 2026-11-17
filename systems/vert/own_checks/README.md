# VERT's own checks, re-run — how to verify each claim yourself

Everything here uses **VERT's own files** (`artifacts/vert/`, VERT commit `3270cec`) and **VERT's own
tools** at the versions VERT's `readme.md` pins:
- cargo-bolero from VERT's fork (`YoshikiTakashima/bolero`, branch `kani-unwind-0`, head `1d02eb8`);
- Rust `nightly-2023-07-29` (`benchmark/rust-toolchain.toml`);
- Kani `0.55.0`.

We never edit VERT's translation or VERT's oracle. Run all commands from the study repo root.
Docker is used only so the toolchain matches VERT's pins. The base image is pinned by digest (the
`ubuntu:22.04` index that `docker/Dockerfile` uses); inside it the scripts install their tools from the
network: apt packages from the live archive, rustup's current stable plus `nightly-2023-07-29`,
cargo-bolero at the fork commit above, and Kani `0.55.0`.

These scripts are for checking each claim by hand on a few functions; they write `results_*.json` in the
current directory, not under `results/`. The paper's numbers come from the shipped runs listed under each
claim.

## Claim 1 — VERT's oracle reads only the first argument

**What it means.** VERT compares its Rust against an "oracle": a copy of the C compiled through WebAssembly.
For functions with 2+ arguments, the oracle only receives the first argument; the others are frozen
at constants from a C stub.

**Check it by reading VERT's files** (no tools needed):

```bash
F=NUMBER_RECTANGLES_NM_GRID; D=artifacts/vert/benchmark/c_transcoder/$F
grep -n "f_gold(" $D/${F}_towasm_mutated.c | tail -1  # the stub the oracle is built from: f_gold(1,29)
grep -n "unsafe {PARAM" $D/out-rwasm-bolero/src/main.rs   # oracle reads PARAM1 only
grep -n "PARAM2" $D/out-rwasm-bolero/src/main.rs          # PARAM2 is set by the test, never read by the oracle
```

**Where the bug comes from.** VERT's 2023 generator writes only `v0 = PARAM1`:

```bash
git -C artifacts/vert show 3509329d:generate_utils.py | grep -n "v0 = TaggedVal::from(unsafe"
```

**Check it by running** (all 506 benchmarks; builds each oracle and varies arguments 2..n):

```bash
python3 -I systems/vert/oracle_params.py 6       # -> results/vert_oracle_params.json
```

## Claim 2 — VERT's documented fuzz (PBT) run executes no inputs

**What it means.** VERT's ARTIFACT_README says to run `mkdir -p src/__fuzz__` and then
`cargo bolero reduce bolero_wasm_eq`, and to count a run as passing if it does not print
"Test Failure". `reduce` only shrinks an existing set of saved inputs. Only 2 of 506 benchmarks
ship saved inputs, so the run stops before testing anything, and the rule still counts it as passing.

Read the instructions and the script:

```bash
sed -n 140,158p artifacts/vert/ARTIFACT_README
sed -n 55,70p artifacts/vert/evaluation.py
find artifacts/vert/benchmark/c_transcoder -path '*out-rwasm-bolero/src/__fuzz__*' -name corpus   # only 2
```

**Why this is not our setup.** Three checks, each independent of our environment:

1. **Same environment, saved inputs present → it works.** `SUM_OF_ALL_PROPER_DIVISORS_OF_A_NATURAL_NUMBER`
   ships 177 saved inputs. The same command in the same container replays all 177 and exits cleanly
   (`results/vert_bolero_debug.log`).
2. **Same environment, real fuzzing → it works.** `cargo bolero test` generates inputs and fails the
   equivalence assertion on VERT's wrong translations (Claim 3).
3. **No environment at all → VERT's own records contradict a run.** VERT's own translation and VERT's own
   oracle disagree on 160 functions that VERT marks as PBT-passed (Claim 1 data). Any run that tried an
   input would have failed.

Run VERT's own script, unmodified, on one function:

```bash
docker run --rm -v "$PWD:/w" -w /w ubuntu:22.04@sha256:0a9887931b0e38af2e3487ed39e29436b0535c4f3f52e57745baaf79d1898956 bash systems/vert/own_checks/run_bolero.sh HEXAGONAL_NUMBER
# uses artifacts/vert (VERT commit 3270cec) as the VERT checkout; override with -e VERT_ROOT=...
```

## Claim 3 — a real fuzzer catches VERT's wrong translations

Same harness VERT ships. The only edit is a message on the assert, so a failure prints its input.

```bash
docker run --rm -v "$PWD:/w" -w /w ubuntu:22.04@sha256:0a9887931b0e38af2e3487ed39e29436b0535c4f3f52e57745baaf79d1898956 bash systems/vert/own_checks/run_bolero.sh FUNCTION_NAME
```

The script writes each function's result to `results_fuzz.json` (key `fuzz_60s`, 60 s per function) in the
current directory. The paper's numbers come from a separate run: 30 s of real fuzzing of VERT's own,
unmodified translations, with every failing input re-checked under the UB rule
(`results/arxiv_vert_fuzz30_real.json`, pipeline stage `vert.arxiv_fuzz30_real`).

## Claim 4 — Kani, run correctly, finds the bugs

**What is wrong with VERT's Kani harness.** It:
- runs the Rust at the fixed value 12;
- runs the oracle at a symbolic n in {0, 1};
- turns unwinding checks off.

So it never compares the two on the same input. See `artifacts/vert/benchmark/c_transcoder/HEXAGONAL_NUMBER/out-rwasm-mutated/src/main.rs`,
lines 267–269 and 406–408.

**Our corrected harness.** Both sides get the same symbolic n in [0, 8], and unwinding checks are on
(Kani's default). It is added next to VERT's code; nothing of VERT's is changed:

```rust
#[kani::proof] #[kani::unwind(20)]
fn kani_eq_correct() {
    let n: i32 = kani::any(); kani::assume(n >= 0 && n <= 8);
    unsafe { PARAM1 = n; }
    assert!(f_gold(n.into()) == f_gold_wasm_thread_unsafe(), "equivalence");
}
```

```bash
docker run --rm -v "$PWD:/w" -w /w ubuntu:22.04@sha256:0a9887931b0e38af2e3487ed39e29436b0535c4f3f52e57745baaf79d1898956 bash systems/vert/own_checks/run_kani.sh FUNCTION_NAME
```

The result is a counterexample n, or "verified in range" for functions that are correct, written to
`results_kani.json` in the current directory. The paper's Kani numbers come from the shipped runs
`results/vert_kani_vacuity.json`, `results/vert_kani_current_check.json` and
`results/vert_kani_unwinding_on.json`.
