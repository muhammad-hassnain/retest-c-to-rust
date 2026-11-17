# His2Trans

**Paper:** "His2Trans: A Knowledge-Guided Agentic Framework for Project-Level C-to-Rust Migration"
(Wang, Liu, Ou, Chen, Li, Wang, Zheng; arXiv 2603.02617, v4 read). Artifact: the public mirror
github.com/SYSUSELab/His2Trans of the paper's anonymous.4open.science/r/His2Trans-F1F0 link,
pinned at commit `3a0ec5544c8e6788280b3520105a77432ed8a730` (2026-07-06, "Sanitize archived
provenance paths"); `git archive` of that commit has sha256
`9e30d1f99ec8c6a7bf1537d82e7863e2d523b311bfb6905b7848e208a078b183`, and the archive's own
`SHA256SUMS` (183 files) verifies. Fetch: [`fetch.sh`](fetch.sh) (LFS disabled: the only LFS file
is the RAG knowledge base, not needed).

**What it is:** a pipeline that builds a Rust scaffold for a C project, has an LLM translate each
function body with retrieved "historical migration knowledge", repairs each function until
`cargo check` passes, and then lets an agent refine the whole crate.

**Its loop has no input-driven step (`n/l`), confirmed in the code.** Per-function repair is
compile-only (framework/incremental_translate.py, `cargo check` compile gate); the pipeline's
"step 9" is a project-level *compile* test (framework/batch_test_staged.sh:2385-2392,
auto_test_rust.py:254-266, example command `["cargo", "build"]`); the refinement agent's prompt
forbids the tests ("Do not read or run held-out Rust tests during repair",
scripts/agentic_repair/post_repair_agent.py:3446; forbidden list at 3648). So the evaluation tests
are held out *by construction*; what we measure below is whether those tests, and our inputs, see
the defects.

**Its correctness check:** "Test Pass Rate measures behavior exercised by the available project
tests" (Sec. III.D). For the eight open-source projects (OSS8) the tests are Rust test harnesses
adapted from the original projects' tests: 52 tests in all, 1-14 per project, shipped in
`data/paper_artifacts/deepseek_v4_pro_oss8_0613_rq2_100pct_minimal/tests/source_rq2_tests/<p>/c2r/test.rs`
and adapted to each crate's API by `scripts/run_oss_rq2_rust_tests.py`. Shipped verdict
(`results/oss8_rq2.json`): 52/52. **We re-ran that check in Docker exactly as `reproduce.sh` runs
it (steps 1-2): 52/52 pass** ([`own_check.py`](own_check.py), `results/his2trans_own_check.json`).
So every crate is **accepted**.

**Re-grade rule: holds for OSS8.** The archive ships the original C
(`data/rq2/deepseek/c_source/c_source/<p>/src`) and His2Trans's own final Rust crates
(`.../projects/<p>`), which all 8 build (offline, vendored deps checked against `Cargo.lock`) and
run under rustc 1.86.0 in Docker; the C builds with gcc 11.4 and clang 14 (the paper used
"Clang 14, Rust nightly"; no nightly date is pinned). rustc 1.86.0 reproduces the shipped
rustc warning counts exactly (avl 9, genann 1, others 0). OHOS10 is out of scope (C shipped for
only 5 of 10 modules; its gtest bridge needs the full OpenHarmony tree).

## How we tested (same method as the other systems)

These are libraries, so for each crate we wrote a **function-level differential driver** pair
([`drivers/`](drivers/)): a C driver linked with the original C and a Rust module compiled into a
scratch copy of the released crate (only `fn main` is replaced; for `ht` one line exposes the
private `hash_key`). Both read the same call script and print every observable result (return
values, output strings, struct fields through the public API, library stdout, stderr). Inputs come
from [`gen_inputs.py`](gen_inputs.py): per-argument grids (<= 80 per API function, R_d spread for
several arguments, reusing `systems/enumerate_inputs.py`), every ordered pair of mutating
operations for stateful APIs (buffer, quadtree, avl), and the call sequences of the system's own
RQ2 tests (class `own`). No model at test time. UB rule (`harness/oracle_ub_common.py`): a
divergence counts only if the C has no report under clang ASan+UBSan(+float-cast-overflow) and gcc
ASan+UBSan, gives the same output at -O0/-O1/-O2, and does not depend on char signedness.

Interface notes (the translations changed several C signatures): qsort takes `usize` bounds
(we only use 0 <= low, high < n); rgba_to_string returns a `String` (we give the C a 512-byte
buffer); urlparser takes `&str` (ASCII inputs only); buffer takes byte slices and keeps the length
parameters as `_len`; genann uses `Vec` fields (run/train are tested with explicitly set weights so
they are compared independently of the random initialisation).

## What we found (5,126 cases, 8 crates)

| crate | cases | agree | counted divergent | C-UB excluded | defects |
|---|---:|---:|---:|---:|---|
| qsort | 166 | 166 | 0 | 0 | - |
| ht | 75 | 75 | 0 | 0 | - |
| rgba | 328 | 318 | 10 | 0 | H2T-RGBA-1 |
| genann | 627 | 548 | 73 | 6 | H2T-GEN-1 (not counted) |
| buffer | 1302 | 1107 | 170 | 25 | H2T-BUF-1..6 |
| quadtree | 404 | 350 | 0 | 54 | - |
| urlparser | 1022 | 466 | 0 | 556 | - (the C is UB on every input of 9 of its 13 functions) |
| avl | 1202 | 1202 | 0 | 0 | - |

**7 counted defects in 2 of 8 crates** (buffer six, rgba one), all wrong outputs. H2T-GEN-1 is
listed below but not counted (see its entry). Re-running the Rust side under rustc 1.99.1 and under
the debug profile changes no verdict.
Each defect is confirmed by fix-and-rerun ([`attribute.py`](attribute.py)): a patch that makes only
that mechanism behave like the C turns exactly its cases green. With all patches applied, no counted
divergence remains and no agreeing case breaks.

- **H2T-BUF-1** `buffer_append_n` ignores its length argument (`_len`) and appends the whole string
  (src_buffer.rs:154-173; C uses strncat, buffer.c:195-210). `buffer_append_n(b, "hello", 1)`: C "h",
  Rust "hello". 78 cases.
- **H2T-BUF-2** `buffer_indexof(b, "")` returns -1; the C returns 0 (src_buffer.rs:238-241). 5 cases.
- **H2T-BUF-3** `buffer_new_with_string_length` ignores its length argument, so `buffer_size`
  differs (src_buffer.rs:65-68; C buffer.c:64). 20 cases.
- **H2T-BUF-4** `buffer_prepend` moves prev+1 bytes where the C moves len+1 (src_buffer.rs:187-189;
  C buffer.c:232). The C behaviour is an upstream bug that the translation silently fixes; it is still a
  divergence from the C ("hello" prepend "0": C "0helo", Rust "0hello"). After `buffer_trim_left`
  the same code panics ("dest is out of bounds", src_buffer.rs:189) on an input where the C is clean.
  The defect counts once, as a wrong output. 54 cases.
- **H2T-BUF-5** `buffer_resize` to a smaller size does not write the terminating NUL at n
  (src_buffer.rs:100-107; C buffer.c:139), so the string is one byte longer than the C's. 14 cases.
- **H2T-BUF-6** after `buffer_trim_left`, an append that grows the buffer writes at a stale offset:
  prev is computed before `buffer_resize` resets `data_offset` (src_buffer.rs:160-171). 2 cases.
- **H2T-GEN-1** `genann_randomize` uses a private LCG instead of the C library's `rand()`
  (src_genann.rs:161-167; C genann.h:39). Every initial weight differs from the C's, and `srand` has
  no effect: 4 seeds give 4 different C weight vectors and 1 Rust vector. 73 cases. They include 6 of
  the system's own test inputs, which pass because those tests check only shapes and ranges. **Not
  counted:** ISO C leaves the sequence `rand()` returns unspecified, so the C fixes no expected value.
- **H2T-RGBA-1** `rgba_to_string` casts channels with a saturating `as u8` where the C uses `(int)`
  (src_rgba.rs:199-208; C rgba.c:252-264). Only channel values outside [0,1] expose it
  (r=1.5: C "#17e7f3f", Rust "#ff7f3f"). 10 cases.
- 3 buffer cases expose two of these defects at once (they need two patches) and are counted for both.

**Undefined behaviour in the C oracle** excluded 641 divergent cases. clang and gcc sanitizers both fired on
every one of them:
- urlparser's `url.h` writes into 1-byte heap buffers (`malloc(sizeof(char))`, then `sscanf` into it) and calls
  `strdup(NULL)`. Only `url_is_protocol`, `url_is_ssh`, `url_get_protocol` and the NULL-returning inputs
  of `url_get_auth`/`url_parse` are testable, and all of them agree.
- quadtree's C search dereferences NULL when a point is not in a leaf; the Rust returns NULL.
- buffer's C is UB on three inputs: slice with out-of-range bounds; trim_right on an empty string; append after trim_left, because its
  capacity check ignores the trimmed offset.
- genann's cached sigmoid converts huge doubles to int.
- rgba's C shifts 255 << 24 in `int` (UB) on every colour with red >= 128. Those cases agree anyway.

One Rust panic (`buffer_append_n` after trim_left) occurs only on an input where the C overflows its
heap buffer, so it is not counted.

**Held out:** the own inputs are recorded (the RQ2 test call sequences). All 7 counted defects diverge
only on inputs outside that set. Only H2T-GEN-1 (not counted) is also triggered by the own inputs, and
those tests do not check the values. Running the own inputs through our comparison, all of them agree except those 6 genann cases and
1 urlparser case whose C is UB.

**Harness power:** one injected fault per harness turns each of the 8 harnesses red. A first
genann choice, the sigmoid clamp 45 -> 44, survived because it is equivalent in double precision; it is
recorded. Mutation analysis (relational, arithmetic and constant +1, at most 40 per crate): the harness
kills 60 of 113 compiled mutants (53%; `results/his2trans_power.json`, `results/harness_power.json`).

Files: `results/his2trans_{diff,attribution,own_check,power,xcheck,xcheck_explain,summary}.json`
(+ the three raw `his2trans_xcheck_*` dumps).

## arXiv version (2026-11-16)

- H2T-BUF-3 (`buffer_new_with_string_length` ignores its length) is not counted: the length argument the C
  stores contradicts the string passed, a precondition the function name states (rule ii). The count is
  6 defects in 2 crates (rgba 1, buffer five, not six); H2T-GEN-1 stays excluded (unspecified value).
  The 7 counted defects in 2 crates above are superseded.
- For `buffer`, His2Trans runs 14 tests of its own instead of the project's, none of which calls
  `buffer_append_n`. Where the C His2Trans ships is itself wrong (`buffer_prepend`), the Rust is correct
  and the divergence is still counted (class: the C bug is not copied).
