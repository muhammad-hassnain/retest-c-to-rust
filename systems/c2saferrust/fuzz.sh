#!/usr/bin/env bash
# Coverage-guided fuzzing for a C2SaferRust coreutils CLI (default: cat), fuzzing STDIN with a
# fixed transforming flag set, then differential-replaying the discovered corpus (C vs Rust).
#   docker run --rm -v "$PWD:/work" -w /work -e UTIL=cat -e FLAGS="-A -n" -e FUZZ_SECS=90 \
#       crust-afl-nightly systems/c2saferrust/fuzz.sh
set -uo pipefail
UTIL="${UTIL:-cat}"
FLAGS="${FLAGS:--A -n}"
ART=/work/artifacts/c2saferrust/coreutils/src/$UTIL
OUT=/tmp/aflout; SEEDS=/tmp/seeds

mkdir -p "$SEEDS"
printf 'hello\tworld\n\nfoo' > "$SEEDS/s1"
printf '\x00\x01\x02\x7f\x80\xff\n\ta\n' > "$SEEDS/s2"
head -c 64 /dev/urandom > "$SEEDS/s3" 2>/dev/null

echo "=== build instrumented C $UTIL (afl-cc) ==="
( cd "$ART/c" && afl-cc -O1 -I ./include *.c -o /tmp/${UTIL}_afl -lm ) 2>/tmp/aflcc.log \
    || { echo "AFL-CC FAIL"; tail -8 /tmp/aflcc.log; exit 1; }
echo "=== ensure plain C + Rust $UTIL built (for replay) ==="
( cd "$ART/c" && make >/dev/null 2>&1 )
( cd "$ART/${C2SR_VARIANT:-rust_WIP}" && cargo build --bin "$UTIL" >/tmp/rb.log 2>&1 ) || { echo "RUST FAIL"; tail -6 /tmp/rb.log; exit 1; }
CBIN="$ART/c/$UTIL"; RBIN="$ART/${C2SR_VARIANT:-rust_WIP}/target/debug/$UTIL"
echo "builds ok"

export AFL_SKIP_CPUFREQ=1 AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 AFL_NO_AFFINITY=1 AFL_NO_UI=1
echo "=== coverage-guided fuzzing $UTIL $FLAGS for ${FUZZ_SECS:-90}s (stdin) ==="
timeout "${FUZZ_SECS:-90}" afl-fuzz -i "$SEEDS" -o "$OUT" -- /tmp/${UTIL}_afl $FLAGS < /dev/null > /tmp/afl.log 2>&1
echo "corpus: $(ls "$OUT/default/queue" 2>/dev/null | grep -c id:) inputs"
grep -E "^(execs_done|corpus_count|edges_found|bitmap_cvg)" "$OUT/default/fuzzer_stats" 2>/dev/null

echo "=== differential replay (C vs Rust, $UTIL $FLAGS) ==="
python3 /work/systems/c2saferrust/fuzz_replay.py "$OUT/default/queue" "$CBIN" "$RBIN" "$FLAGS"
