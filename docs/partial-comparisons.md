# Checks that ran an exposing input but compared only part of the output

Generated from `results/defect_ledger.json` (field `check_exercises_defect`, status `exercised-passed`).
For each defect below, the system's own check ran an input that exposes the defect, yet the check passed,
because it compared only part of what the program produces. The paper summarizes these three cases in one
sentence (Introduction, RQ2 answer, and Section V); this file keeps the full detail.

| System | Defect | What the check ran | What it compared | What it missed |
|---|---|---|---|---|
| C2SaferRust | `C2SR-head-5` | `head-write-error.sh` (in the acceptance set): `head --lines=-1` on a 10 MiB file and on a pipe | exit status and error message only; stdout is sent to `/dev/full` | `head` drops the bytes of a final partial buffer (4,095 bytes of this file) |
| SACTOR | `SACTOR-C1` (Holdem-Odds `NewCard`) | SACTOR's own CuTest suite, which calls `NewCard` 56 times, 4 of them on exposing inputs | the process exit code only | the test prints its failures but `all_tests` returns 0 |
| RustMap | `RUSTMAP-bzip2-BZ-4` | RustMap's bzip2 check on its own test files | the output after `bzip2recover` salvages it, decompressed with the C | every Rust output starts with 5,000 zero bytes, which plain `bzip2 -d` rejects |

## Evidence, verbatim from the ledger

### C2SR-head-5 (C2SaferRust)

- Mechanism: head -n -N, final partial buffer (elide_tail_lines_pipe): the rewritten loop advances p by one byte per counted line (`p = &p[1..]`) instead of to the byte after the next line end (C: p = memchr(...) + 1), so only one byte per line of the last partial buffer is written. It shows when at least one full buffer was written first (desired_pos has wrapped, so C2SR-head-3's checked add does not fire), as on the piped input of head-write-error.sh (yes | head --lines=-1). On x86-64 regular files take this pipe path too (current_pos -1), a routing inherited from the C2Rust starting tree, not introduced by the rewrite: its struct stat has the aarch64 layout, so st_mode reads st_nlink and usable_st_size is false (results/arxiv_c2saferrust_head_aarch64.json). Through that routing the block also truncates regular files: head --lines=-1 on the 10 MiB bigseek file drops 4,095 bytes. The faulty block is the rewrite's under either routing (the C2Rust baseline agrees with the C under both).
- Acceptance: `accepted-recorded`
- Check ran an exposing input, and passed: head-write-error.sh (in the acceptance set) runs head --lines=-1 on the 10 MiB file and on a pipe, but sends stdout to /dev/full and checks only the exit status and error message, so the truncated output passes.

### SACTOR-C1 (SACTOR)

- Mechanism: NewCard: NewCard defines a nested stub `fn CardIsValid(c: *const Card) -> bool { true }` that shadows the real CardIsValid (combined.rs:132). Every card is reported valid, so NewCard(InvalidRank, Spade), NewCard(Ace+1, ..) and NewCard(.., InvalidSuit) return the invalid card instead of INVALID_CARD.
- Acceptance: `accepted-recorded`
- Check ran an exposing input, and passed: It diverges on SACTOR's own test, but SACTOR's check reads only the exit code, and CuTest's all_tests.c returns 0. We traced the own test with -Wl,--wrap: it makes 56 distinct NewCard calls. 4 of the 48 diverging NewCard grid inputs are among them (the test's own out-of-range checks, e.g. NewCard(14,4) = Ace+1). The other 44 are outside SACTOR's inputs. For NewCardFromChars the own test makes 2845 distinct calls; the trace kept 200, so membership of the 9 diverging inputs is not established. SACTOR's check reads only the test's exit code; CuTest returns 0 although it prints the failure.

### RUSTMAP-bzip2-BZ-4 (RustMap)

- Mechanism: In BZ2_bzWrite the loop sets avail_in = len where C sets avail_out = BZ_MAX_UNUSED (5000). avail_out stays at its default 0, so after each BZ_RUN call the 'avail_out < 5000' branch writes n = 5000 - 0 = 5000 bytes of the untouched, zero-initialised output buffer. The real stream is written later by bzWriteClose64, which does reset avail_out = 5000.
- Acceptance: `accepted-rerun`
- Check ran an exposing input, and passed: RustMap's check runs bzip2recover before bzip2 -d (README 4.1.3), which skips the zero prefix, so the check passes by design.

