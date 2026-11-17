#!/usr/bin/env bash
# Coverage-guided differential fuzzing for Syzygy's Zopfli.
#   1. AFL++ fuzzes the instrumented original C (coverage feedback) to discover a diverse corpus.
#   2. We replay that corpus through the original C and the translated Rust and diff their output.
# Run in the AFL image:
#   docker run --rm -v "$PWD:/work" -w /work -e FUZZ_SECS=90 crust-afl systems/syzygy/fuzz.sh
set -uo pipefail
ART=/work/artifacts/syzygy_zopfli
OUT=/tmp/aflout; SEEDS=/tmp/seeds
# arm64 compatibility: zopfli.c hardcodes an x86_64 glibc include path; point it at this arch's.
ARCH_DIR=$(ls -d /usr/include/*-linux-gnu 2>/dev/null | head -1)
[ -n "$ARCH_DIR" ] && ln -sf "$(basename "$ARCH_DIR")" /usr/include/x86_64-linux-gnu 2>/dev/null || true

mkdir -p "$SEEDS"
printf 'hello world\n' > "$SEEDS/s1"
head -c 64 /dev/zero > "$SEEDS/s2"
printf 'aaaaabbbbbcccccdddddeeeee' > "$SEEDS/s3"

echo "=== build: instrumented C (afl-cc), plain C (gcc), Rust (cargo) ==="
afl-cc -O1 "$ART/c_code/zopfli.c" -o /tmp/zopfli_afl -lm 2>/tmp/aflcc.log || { echo "AFL-CC BUILD FAIL"; tail -8 /tmp/aflcc.log; exit 1; }
gcc -O2 "$ART/c_code/zopfli.c" -o /tmp/zopfli_c -lm 2>/dev/null || { echo "GCC BUILD FAIL"; exit 1; }
( cd "$ART/rust_code" && cargo build --release >/tmp/rb.log 2>&1 ) || { echo "RUST BUILD FAIL"; tail -8 /tmp/rb.log; exit 1; }
echo "builds ok"

export AFL_SKIP_CPUFREQ=1 AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 AFL_NO_AFFINITY=1 AFL_NO_UI=1
echo "=== coverage-guided fuzzing for ${FUZZ_SECS:-90}s ==="
timeout "${FUZZ_SECS:-90}" afl-fuzz -i "$SEEDS" -o "$OUT" -- /tmp/zopfli_afl < /dev/null > /tmp/afl.log 2>&1
echo "afl exit (124=timed stop, expected)"
Q="$OUT/default/queue"
echo "corpus discovered: $(ls "$Q" 2>/dev/null | grep -c id: ) inputs"
grep -E "^(execs_done|corpus_count|edges_found|bitmap_cvg)" "$OUT/default/fuzzer_stats" 2>/dev/null

echo "=== differential replay of the fuzzer corpus (C vs translated Rust) ==="
python3 /work/systems/syzygy/fuzz_replay.py "$Q" /tmp/zopfli_c "$ART/rust_code/target/release/testing_equiv"
