# SACTOR

**What it is.** SACTOR is an LLM translator from C to Rust. It works in two stages: an "unidiomatic" Rust that keeps the C ABI, then an "idiomatic" Rust. Each translated item is checked by linking it into the original C build (FFI) and running the program's end-to-end tests. A stage is accepted when all E2E tests pass. For libraries, the check is a test program's exit code or its output.

**What it ships (rule R).**
- The 200 headline TransCoder-IR / CodeNet translations are **not shipped**, so that part fails R.
- It ships libogg (C plus gpt-4o/gpt-5 Rust for bitwise.c and framing.c, both stages) in github.com/qsdrqs/sactor-datasets.
- It ships 49 CRUST-Bench samples in the fork github.com/qsdrqs/CRUST-bench @ 69970f9c.

**What we re-grade.** Every shipped translation that is a complete Rust translation of its C unit, exposes the C ABI (unidiomatic), and builds. Idiomatic sets have a Rust-native API that no C caller can link; SACTOR itself skips their E2E test. We record whether they build, but they are not tested against the C. Partial unidiomatic sets have no combined.rs and are not a library. Both kinds are out of scope.

**How.** Everything third-party is built and run in Docker (`run_docker.sh`: crust-heldout image, `--network none`, 3 CPUs). Full order: `run_all.sh`; summary: `build_summary.py` produces results/sactor_summary.json.
- `fetch.sh` makes the pinned clones.
- `libogg/` holds library-level differential drivers (same C driver linked to the C or to the Rust cdylib), call-sequence generators, the UB filter, held-out tracing with `--wrap`, and mutation analysis.
- `crust/` re-runs SACTOR's own tests against C and Rust (`run.py`), runs per-function argument grids (`grid.py`), and does mutation analysis (`mutate.py`).
- Defects confirmed in source are listed in `defects.json`.

**Deviations.** SACTOR pins nightly-2024-12-18. It was not available offline, so every Rust build uses nightly-2024-09-03 and nightly-2025-05-10, in debug and release. libc crate: 0.2.191 (offline cache). libogg's C is built with configure's CFLAGS and a configure-equivalent config_types.h, because autotools and cmake are absent from the image.

**What we found** (results/sactor_summary.json).
- **Coverage.** We re-graded 35 of 86 shipped translation sets. 34 were tested against the C: libogg gpt-4o bitwise, gpt-4o framing, gpt-5 framing, and 31 CRUST-Bench libraries. 1 does not build: libogg gpt-5 bitwise.
- **5 translations have a counted defect**, confirmed in source (`defects.json`): 4 wrong outputs and 1 crash.
  - **L1** libogg gpt-4o bitwise, oggpackB_write: a zero-bit write ORs the value's top bits into the buffer (wrong output).
  - **L2** libogg gpt-4o framing, ogg_stream_flush_i: the first page is empty when the first packet is at least 65025 bytes (wrong output).
  - **C1** CRUST Holdem-Odds, NewCard: a nested stub `CardIsValid() -> true`. SACTOR's own test prints the failure but exits 0 (wrong output).
  - **C2** CRUST c-string, string_t_is_space_char: the NUL terminator counts as whitespace (wrong output).
  - **C3** CRUST fs_c, fs_chown/fs_error: a Rust `&str` is exported where the C has `const char*`, so the function crashes (crash).
- **Acceptance.** All five runtime defects are in translations SACTOR's check accepts.
- **Held out.** L1, L2, C2 and C3 diverge only on inputs SACTOR's tests never ran (traced with `-Wl,--wrap`). C1 diverges inside and outside them.
- **Not counted.** Build failure: L3, libogg gpt-5 bitwise, whose combined crate fails E0530 because `let mask` shadows the static (it is not tested, so it is not in the 34). Precondition-only: lib2bit byte2base; c-aces randrange and randinverse. Float tolerance: c-aces normal_rand, which uses a different PI. C UB: 2DPartInt, Holdem indexOf, murmurhash, c-aces xgcd, Linear-Algebra roundn.
