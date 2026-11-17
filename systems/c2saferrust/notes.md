> **Study log (historical).** Notes recorded while the study ran; some numbers and framing are
> superseded. Current, checked results: `results/defect_ledger.json` and `README.md`.

# C2SaferRust

**What it is:** A tool that takes old C programs and rewrites them in Rust, using an AI model
to make the Rust safer. The programs it converted are 7 small Unix command-line tools
(`cat`, `head`, `tail`, `uniq`, `pwd`, `split`, `truncate`).

**Did they share enough to test it?** Yes. They put both the original C and the finished Rust
online, so we could build both and run them. One catch we only noticed in audit 4: each tool's
folder holds **two** Rust versions. `rust/` is the plain, automatic C2Rust translation the tool
*starts* from; the tool copies it to `rust_WIP/` and does all its AI rewriting there
(`translate.py:528-537`), and its own results script grades `rust_WIP/` (`collect_results.py:90-91`).
So **`rust_WIP/` is C2SaferRust's output**. All our earlier results were on `rust/` by mistake; we now
keep those only as the "C2Rust baseline" and test `rust_WIP/` by default (`C2SR_VARIANT`, see
`harness/common.py`).

**What we did:** We ran the original C program and the AI's Rust version side by side on the
same fresh, made-up inputs (ones the tool had never seen while it was being built) — a fixed,
repeatable grid of 8,574 cases for `cat`/`head`/`tail`/`uniq` — and checked whether they gave the
exact same answers.

**What we found (C2SaferRust's output, `rust_WIP/`, on x86-64):**
- **1,651 of the 8,574 cases give different normal output or exit code** (cat 144, head 566, tail
  371, uniq 570). Of those, **611 are wrong answers** and the rest are crashes we count separately
  (**960 segfaults, 80 panics**). Checked by hand: `cat -A` prints bytes ≥0x80 wrongly (the C shows
  `M-^@`…`M-^?`); `head in1 in2` prints both file headers before any content; `head <a directory>`
  says success (exit 0) where the C fails (exit 1); `head -q`, `uniq -i`, `uniq <a directory>` and
  `tail -n -1` segfault.
- The plain C2Rust starting version (`rust/`) matches the C's normal output and exit code on **all**
  8,574 cases, so these wrong answers and crashes were **introduced by the AI rewriting**.
- Two problems come from C2Rust and are in **both** versions: error messages are **printed twice**
  (`cat`/`head`/`tail` on a missing file, a directory, or a good file plus a missing one; `uniq -c -D`'s
  "meaningless" complaint), and `head -n -N` leaves a shared file position in the wrong place
  (for N = 5000, 50000, 69000 the C leaves N lines for the next reader, the Rust 0; re-checked on `rust_WIP`). On the
  baseline the doubled messages showed up in 2,774 of the 8,574 cases (277 of 800 on a smaller
  200-per-tool grid). On `rust_WIP` 2,844 cases differ only in error messages — the doubling plus some
  messages with a missing or garbled file name.
- **Its own tests barely notice.** The tool's acceptance tests are the shell-script (`.sh`) tests that
  already pass on the starting version (`translate.py` `select_tests`, lines 231-248). Copying
  `select_tests` exactly ([`../../harness/acceptance_suite.py`](../../harness/acceptance_suite.py): run
  each script by its shebang from the artifact root, 25 s timeout, pass = exit 0, 3 runs →
  `results/c2saferrust_acceptance_suite.json`), that is **27** of the 67 `.sh` tests in our setup (cat 3,
  head 2, tail 4, uniq 2, split 8, truncate 7, pwd 1; the authors' setup may pick a slightly different
  set). `rust_WIP` fails only **2** of those 27, in 3 of 3 runs (the starting version passes both 3 of
  3): `truncate/truncate-parameters.sh` and `tail/big-4gb.sh` — `tail -c1` on a 4 GiB sparse file
  prints `abcdefgh…` from the start of the file instead of `1` (checked by hand; the C and the starting
  version print `1`). (Our older `ablation.py` run — bash, from the test folder — is a different
  procedure.) (An earlier "12 of 38 tests fail" count was wrong: all 12 also fail on
  the starting version, so the tool would never have picked them — including `head-pos.sh`, so the
  `head -n -N` bug is not on an input from its own tests.) It never runs the Perl tests, yet one
  (`tests/uniq/uniq.pl`, test `112`, `-D -c`) checks exactly the `uniq` message the Rust prints twice.
  Of all the `.sh` tests (not only the 27 selected), the ones that capture error output are: `cat` 0 of 4, `head` 1 of 3, `tail` 16
  of 33 (mostly follow-mode tests that mix error output into normal output), `uniq` 0 of 2, `split`
  3 of 14 ([`stderr_coverage.py`](stderr_coverage.py) → `results/c2saferrust_stderr_coverage.json`).
- How good is our test at catching mistakes? We planted 15 small bugs per tool in `rust_WIP` and it
  caught **12 of 60 (20%)** (cat 5, head 1, tail 3, uniq 3), on the first 500 generated inputs per
  tool (`mutate.py -n 500`, not the full 8,574 grid), scoring only inputs where the unplanted
  Rust already matches the C; a deliberately-broken copy was caught every time.

**One-line takeaway:** The AI-rewritten Rust gives wrong answers or crashes on fresh inputs where the
plain C2Rust version it started from was right — and the tool's own acceptance tests flag just two
failures.

**Coverage-guided fuzzing (AFL++) — C2Rust baseline (`rust/`) only, not yet re-run on `rust_WIP`:** on top of the fixed deterministic grid, we also let a real
fuzzer (AFL++) generate stdin bytes by watching which code paths they reach
([`fuzz.sh`](fuzz.sh)), then replayed everything it found through both the C and the Rust with the
same flags ([`fuzz_replay.py`](fuzz_replay.py)). This targets the *output-formatting* paths that
read stdin — exactly the byte-exact behaviour a translation is most likely to get subtly wrong.
- **`cat -A -n`** (show-all + line-numbering, the richest stdin formatter): 343,023 executions in
  90s, 63 fuzzer-discovered inputs covering control characters, high bytes and binary.
  **0 stdout/exit divergences, 0 stderr divergences.** The Rust reproduces the C byte-for-byte on
  every input the fuzzer found — matching the baseline grid's result that the baseline `cat`'s stdout
  formatting is faithful (on `rust_WIP` the grid finds `cat -A` wrong on bytes ≥0x80). (The error-message-duplication defect above is on the
  *file-argument error path*, which stdin-only fuzzing never reaches — so the two methods are
  complementary, not contradictory.)
- **`head -c 15`** (byte-count from stdin): 286,164 executions, **0 divergences**. Note the corpus
  stayed tiny (3 inputs): `head -c N` reads N bytes and exits, so there is almost no branch
  surface for coverage-guided search to expand into — an honest limit of fuzzing an early-exit
  utility on stdin, not a strong equivalence claim.
- **`tail -n 5`** (last-N lines): 190,651 executions, 8 inputs, **0 divergences**.
- **`uniq -c`** (count consecutive duplicate lines): 322,310 executions, 39 inputs, **0 divergences**.

So for the baseline's stdout path the fuzzer agrees with the baseline grid on **all four** stdin→stdout
utils (faithful), while the baseline's defect lives on the error path the grid already isolated. The
pipeline is parameterised (`UTIL=… FLAGS=…`).

**File-writing utils — `split` and `truncate` (C2Rust baseline `rust/` only, not yet re-run on `rust_WIP`):** these don't print their main output; `split`
writes `xaa`, `xab`, … and `truncate` resizes a file in place. So we run the original C and the
Rust in *separate* folders and compare the **files they leave behind**, byte-for-byte. Across 440
made-up cases (every sensible flag combination × a fixed mix of inputs × sizes like "round up to a
multiple" and "shrink only") the Rust produced **identical files every time** — and we proved the
check actually works by deliberately breaking a copy (making `split` use longer names, making
`truncate` set a different size) and confirming it was caught.

**How to reproduce:** [`fuzz.sh`](fuzz.sh) (coverage-guided, needs the `crust-afl-nightly`
image = [`../../docker/Dockerfile.afl-nightly`](../../docker/Dockerfile.afl-nightly)) and the
deterministic harness in [`../../harness`](../../harness); results in [`../../results`](../../results) (the
study's working log, `docs/findings.md`, is not part of this repository).

## arXiv version (2026-11-16)

- `head` drops a final partial buffer when reading a pipe. The C2Rust code C2SaferRust rewrites declares
  aarch64's `struct stat`, so on x86-64 (our runs) regular files take the pipe path too; the routing is
  inherited from C2Rust, and it is how its acceptance test's file loses 4,095 bytes (on aarch64 the
  routing is correct and only the pipe case still loses bytes; `results/arxiv_c2saferrust_head_aarch64.json`).
- On aarch64 `rust_WIP`'s `head -n -N` inverts the C's `0 <= elseek` test and exits 1 on files over 4096
  bytes (head-pos.sh then fails 3 of 3). It is hidden on x86-64, so it is noted, not counted.
- Harness power under the UB-clean kill rule: 378 of 1892 planted bugs caught (20.0%); on C2SaferRust's own
  output 206 of 1074 (19.2%); survivors 935 (the paper's "619 of the 935"). This replaces the
  earlier 22% (21.5% of 1074; 892 survivors).
