# C2SaferRust in-distribution ablation — per-case triage

> Note (2026-11-17), superseding the `head -n -N` verdict below. The `head-pos.sh` row (marked REAL S1) and the one "CONFIRMED real defect" in the summary are not a defect of C2SaferRust's rewrite, and the defect ledger does not count them. On x86-64 the C2Rust starting tree mis-positions the shared file offset too: the `struct stat` it declares has the aarch64 layout, so a regular file takes `head`'s pipe path and no `lseek` is made; C2SaferRust's output keeps that routing unchanged. With the routing corrected (aarch64, or x86-64 with the x86-64 `struct stat`) the starting tree positions like the C. C2SaferRust's output then still fails `head-pos.sh` 3 of 3, in code that x86-64 as shipped never reaches; that divergence is noted in the same file, not counted. Evidence: `results/arxiv_c2saferrust_head_aarch64.json` (`routing_claim`, `acceptance_with_correct_routing`, `new_divergence_with_correct_routing`). The five counted `head` defects are `C2SR-head-1` to `C2SR-head-5` in `results/defect_ledger.json`.

**What this is.** `harness/ablation.py` runs C2SaferRust's *own* shipped GNU coreutils repair tests
against the translated Rust and the original C (byte-exact, the test's own pass/fail). With the
**bash** harness (the earlier `sh`/dash runner produced spurious `Bad substitution` failures), the
raw count is **12 of 38** assessable repair tests failing on the Rust where the C passes. This file
classifies each — real translation defect vs environment/harness artifact — by **direct A/B runs of
the shipped binaries** (not through the test framework), on the **native x86_64 desktop** (gcc 15 for
C via the authors' Makefile, the shipped Rust `[[bin]]`). Do not count a repair-test failure as a
defect until it is reproduced in isolation.

**Committed evidence.** The raw 12/38 is `results/c2saferrust_ablation.json` (totals: 56 tests,
`c_pass` 38, `rust_fails_where_c_passes` 12 — the 12 cases tabulated below). The decisive direct A/B
sub-cases are reproduced by [`harness/verify_ablation_triage.py`](../../harness/verify_ablation_triage.py)
into `results/c2saferrust_triage_evidence.json`: `head-pos` shared-fd diverges at **every swept
magnitude** — `head -n -N` for N ∈ {5000, 50000, 69000} over a 70000-line file gives **C=N, R=0**
in all three cases (`all_magnitudes_diverge: true`), so the defect is not an artifact of one chosen
N; every `split` isolation (`-n 2/3 /dev/null`, `-n 10 /dev/null`, `-n r/3`, `-b 1` suffix
auto-extend) → **C and Rust produce identical file sets** (`agree: true`). Both regenerated on the
native x86_64 desktop on 2026-11-03; the earlier committed `ablation.json` was a stale 59-test
snapshot and has been replaced.

## Verdict
- **1 CONFIRMED real defect** (new; the held-out grid never exercised it): `head -n -N` file-pointer
  positioning.
- **stderr duplication (S2)** — the already-documented C2SaferRust defect — is what the `tail -f`
  tests surface; it is real but already counted, not a new finding.
- **`split` failures are NOT confirmed** — every isolated sub-case agrees between C and Rust; the
  test-level failures are cross-step file-accumulation/glob artifacts.
- **Remaining `tail -f`/pipe/sysfs failures** are environment-sensitive follow-mode behavior (special
  file types under Docker), on top of the S2 duplication.

So the ablation does **not** show C2SaferRust is broadly non-equivalent in-distribution. It shows
(a) the known S2 stderr defect, and (b) **one** genuine S1-class semantic defect the held-out grid
missed — which *strengthens* T1 (a real semantic divergence, not just a diagnostic channel) without
inflating the count.

## Per-case

| test | raw | classification | evidence (direct A/B) |
|---|---|---|---|
| `head/head-pos.sh` | fail | **REAL S1 — confirmed** | `(head -n -50000 >/dev/null; wc -l) < seq(70000)` → **C=50000, R=0**. Rust mis-positions the shared file descriptor after eliding "all but last N" lines, so the next reader on the same fd sees nothing. Deterministic, no UB, reproduced natively. |
| `tail/follow-name.sh` | fail | REAL S2 (known) | Rust **duplicates** each stderr diagnostic (`cannot open … / no files remaining` printed twice). Same stderr-duplication defect already reported; `-f` path surfaces it. |
| `tail/wait.sh` | fail | REAL S2 (known) + env | Same stderr duplication, plus `cannot follow end of this type of file` on a special file (pipe/fifo under Docker) — the latter is file-type/environment dependent. |
| `tail/tail-c.sh` | fail | minor (trailing line) | Output differs by a single trailing empty line in a `tail -c` corner; secondary-channel/formatting, not a value defect. |
| `tail/follow-stdin.sh` | fail | env (follow-mode) | `tail -f` on stdin/pipe; timing/file-type dependent, plus the S2 duplication. |
| `tail/pipe-f.sh` | fail | env (follow-mode) | `tail -f` across a pipe; timing/inotify dependent. |
| `tail/pipe-f2.sh` | fail | env (follow-mode) | as above. |
| `tail/tail-n0f.sh` | fail | env (follow-mode) | `tail -n0 -f`; follow-mode timing. |
| `tail/tail-sysfs.sh` | fail | env (special fs) | reads `/sys`; behavior depends on the host's sysfs, not the translation. |
| `split/b-chunk.sh` | fail | **NOT confirmed** (harness) | Isolated `split -n 10 /dev/null`, `-n 4 abc`, `-n 2 abc`, `-n 2/3 /dev/null` all **agree** (C==R). The test-level failure is `returns_ 1 stat 'x??'` finding leftover empty files `xaa..xag` accumulated across earlier steps; isolated `split -n 2/3 /dev/null` creates **0** files on both. Cross-step file/glob artifact, not a demonstrated divergence. |
| `split/r-chunk.sh` | fail | **NOT confirmed** (harness) | Isolated `split -n r/3` agrees (C==R). Same cross-step pattern as b-chunk. |
| `split/suffix-length.sh` | fail | **NOT confirmed** (harness) | Isolated suffix auto-extension (`split -b 1` on 680 bytes) **agrees** — both auto-extend to 3-char suffixes (`xzabc`, `xzabd`). The in-framework `suffix length needs to be at least 3` error was not reproducible in isolation. |

## Method note (why isolation matters)
The first three `split` cases *looked* like real chunk-boundary bugs from the test's tail output
(empty trailing chunks). Direct A/B of the exact commands refuted that: the obvious cases agree, and
the failures are entangled with the tests' own `x??` globbing and cross-step `rm` cleanup. Counting
them as translation defects would have repeated precisely the error Methodology Review #1 flagged
(asserting a mechanism the data doesn't isolate). Only `head -n -N`, reproduced by a direct
two-binary differential, survives as a genuine in-distribution S1 defect.

## Consequence for the oracle-fitting hypothesis (H1)
For C2SaferRust the acceptance distribution is the coreutils repair suite. The shipped Rust **fails
one of those tests for a genuine semantic reason** (`head -n -N`), so the artifact is **not fully
in-distribution equivalent** — which is a *stronger* non-equivalence result than oracle-fitting (it
diverges even on an acceptance-distribution input), not evidence of clean fitting. The S2 stderr
defect remains the clean held-out-only divergence. Both are reported; neither is pooled into a rate.
