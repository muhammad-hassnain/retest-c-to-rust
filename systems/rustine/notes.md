> **Study log (historical).** Notes recorded while the study ran; some numbers and framing are
> superseded. Current, checked results: `results/defect_ledger.json` and `README.md`.

# Rustine

> **Status: examined, not re-graded.** We also examined Rustine, but its translation tool and its
> equivalence check are withheld (its README says the source code "will be released upon acceptance")
> and its shipped C does not build into a runnable oracle, so we could only build its raw output; we do
> not re-grade it. Everything below is supporting material for that decision, not a paper finding.

**What it is:** A tool that turns whole C programs into Rust. It ships 24 real programs
(avl-tree, bzip2, qsort, zopfli, lodepng, …), and for each it includes the tool's own
**auto-translation** (`translate/`), plus for some programs an **`automatic_debug/`** stage and/or a
**`manual_debug/`** version.
(The paper's tool *source* is held back "until acceptance"; only the outputs are shared.)

**Did they share enough to test it?** Only partly — the auto-translation, the debug stages, and the
original C are all there, but the tool and its equivalence check are not, and the C does not build into
a runnable oracle (see the wall below). So we could build the raw output, not re-grade it.

**What we did:** Two honest, quick measures of the **raw tool output** (`translate/`):
1. **Does it even compile?** (`cargo build` each program and stage from a fresh copy, under two
   toolchains.)
2. **If it compiles, does it run without crashing?** (run each program's built-in test/example,
   count Rust panics/crashes.)

**What we found:**
- **How many build depends on the Rust version *and* the day you build, so we draw no build-rate
  conclusion.** Rustine ships **no `Cargo.lock`** for `translate/`, `automatic_debug/` or
  `manual_debug/` (the lockfiles once in our copy were left there by our own earlier
  `compile_rate.sh`), so each build pulls whatever dependency versions are current. Building each
  stage from a fresh copy (x86_64, untracked lockfiles removed):

  | stage | rustc 1.86 | Rustine's pinned `nightly-2025-05-25` |
  |---|:---:|:---:|
  | `translate/` (raw) | 19 / 24 | 9 / 24 |
  | `automatic_debug/` | 5 / 6 | 2 / 6 |
  | `manual_debug/` | 10 / 14 | 4 / 14 |

  Under the pinned nightly most failures come from dependency versions resolved today (e.g. a
  dependency using the unstable feature `stdarch_x86_avx512`). Under 1.86 the 5 raw failures are `ht` (a genuine type error, E0308),
  `genann` (package-name mismatch: unresolved import `translate`), `grabc` and `xzoom` (a dependency
  needs a newer rustc) and `robotfindskitten` (needs the ncurses system library). An earlier
  "14 of 24 compile / 42% don't" count came from building in place with stale `target/` folders; it
  is **withdrawn**. (Rustine's paper abstract says it produces "fully compilable Rust code for all" 23 of
  its programs, with 87% of assertions passing.)
- **Runtime (scoped honestly):** the one program with a deterministic **built-in self-test** is
  avl-tree, and the raw translation **fails** it (re-checked on a fresh copy with rustc 1.86) — its
  own `unit_test_create` prints `FAILED` then **panics** (`unwrap()` on a `None` value, at
  `src/avl_bf.rs:583`, exit 101) in the translated core. That is a real, verified runtime defect.
  The `manual_debug` version's `avl_test` prints `ALL TESTS PASSED`. The other compiled binaries we only ran on **empty stdin**; several error/panic
  there, but we **don't** compare them to the C and many legitimately need arguments/a file, so an
  empty-stdin panic is **not** a correctness verdict. We therefore do **not** claim a runtime
  pass-rate like "only 21% work" — that earlier phrasing over-counted empty-stdin panics.

**One-line takeaway:** Whether Rustine's raw output builds depends on the toolchain and the
dependency resolution date (19/24 vs 9/24),
so no build-rate claim; the one raw translation with a built-in self-test fails it, and its
`manual_debug` version passes.

**Honesty notes:** "runs without crashing" is weaker than "behaviourally matches the original C."
Rustine does document its own automated check — its README lists `evaluate` ("Compare translated
Rust code with C source") and `validate` ("Verify semantic equivalence between C and Rust code")
commands — but the tool source is held back, so we could not run or inspect them. We grade only the
raw outputs and did **not** compare them to the C, so Rustine is **examined, not re-graded** (builds + one self-test only)
(no severity class: the avl-tree panic is a self-test failure, not a comparison with the C).

**We tried to close that to a real equivalence test, and hit a wall in the artifact.** Each program
*does* ship original C — in `preprocess/`, `refactor/` and `decompose/` variants, often with a paired
`test.c` / `test.rs` driver — so in principle we could build the C, build the Rust, run both tests and
diff. In practice the shipped C is **fragmented and multi-file with external header dependencies**
(e.g. `refactor/libcsv.c` needs `csv.h`, which isn't present; others pull in decomposed fragments),
and each program has its own bespoke test driver and build shape. There is no uniform way to build the
original C into a runnable oracle across the 24 programs from the artifact as shipped. So a clean
original-C-vs-Rust differential isn't reachable here, and the faithful finding is the
**toolchain- and date-dependent build matrix** (raw 19/24 under rustc 1.86, 9/24 under the pinned nightly) plus
the one raw translation whose deterministic built-in self-test fails. (We do not claim a runtime pass-rate; an earlier "~21%
work" over-counted empty-stdin panics as defects.)

**How to reproduce:** [`build_matrix.py`](build_matrix.py) (fresh-copy build of every stage →
`results/rustine_build_1.86.json`, `results/rustine_build_nightly-2025-05-25.json`; it drops untracked
lockfiles, records the arch, and replaces `compile_rate.sh`, which built in place with stale `target/`
dirs and left `Cargo.lock` files behind) and
[`runtime_check.sh`](runtime_check.sh); fetch with [`fetch.sh`](fetch.sh); stable Docker image.
