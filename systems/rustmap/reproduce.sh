#!/usr/bin/env bash
# SUPERSEDED (audit 4): this script builds `--release` with a 10 s timeout, which is NOT how
# RustMap builds/checks bzip2 (README 4.1.1 uses a debug `cargo build`; 4.1.3 grades with
# bzip2recover -> bzip2 -d -> diff). Its "hangs / invalid output (S3)" result is withdrawn; use
# systems/rustmap/check_bzip2.sh (-> results/rustmap_bzip2_check.json) instead. Kept for the record.
#
# Reproduce the RustMap bzip2 finding: built from the released source, the translated
# bzip2 (a) never terminates (hangs) in file mode, and (b) produces an INVALID/incomplete
# .bz2 stream -- the original C's output decompresses directly with `bzip2 -d`, the
# translation's does not (which is why RustMap's own verification, README section 4.1.3,
# runs `bzip2recover` first to salvage blocks before decompressing).
#
# Run in the amd64 image (the translation hardcodes `*const i8`, i.e. x86_64 c_char):
#   docker run --rm --platform linux/amd64 -v "$PWD:/work" -w /work crust-syzygy \
#       systems/rustmap/reproduce.sh
set -uo pipefail
ART=/work/artifacts/rustmap
C="$ART/c-code/bzip2-1.0.8/bzip2"
R="$ART/rust-code/bzip2_rs_gpt/target/release/bzip2_rs_gpt"

echo "== build oracle (C bzip2) =="
( cd "$ART/c-code/bzip2-1.0.8" && make bzip2 >/dev/null 2>&1 && echo "  C ok" )
echo "== build SUT (translated Rust bzip2, release) =="
( cd "$ART/rust-code/bzip2_rs_gpt" && cargo build --release >/dev/null 2>&1 && echo "  Rust ok" )

# Deterministic input corpus: fixed sizes x a reproducible LCG byte stream (no $RANDOM / urandom),
# so the result is the same on every machine and every run.
SIZES=(20 64 128 512 1000 1500 2048 2600 3000 20)
hang=0; invalid=0; n=${#SIZES[@]}
for i in $(seq 0 $((n-1))); do
  sz=${SIZES[$i]}
  python3 - "$sz" "$i" > /tmp/src <<'PY'
import sys
n=int(sys.argv[1]); seed=(int(sys.argv[2])*2654435761 + 12345) & 0xffffffff
b=bytearray()
for _ in range(n):
    seed=(1103515245*seed + 12345) & 0xffffffff
    b.append((seed>>16) & 0xff)
sys.stdout.buffer.write(bytes(b))
PY
  rm -rf /tmp/c /tmp/r; mkdir -p /tmp/c /tmp/r; cp /tmp/src /tmp/c/data; cp /tmp/src /tmp/r/data
  ( cd /tmp/c && "$C" --keep --force data >/dev/null 2>&1 )            # oracle: terminates
  ( cd /tmp/r && timeout 10 "$R" --keep --force data >/dev/null 2>&1 ) # SUT: hangs -> rc 124
  [ $? -eq 124 ] && hang=$((hang+1))
  # Is the SUT's .bz2 a valid bzip2 stream? (original C decompresses its own fine)
  if [ -s /tmp/r/data.bz2 ]; then
    "$C" -t /tmp/r/data.bz2 >/dev/null 2>&1 || invalid=$((invalid+1))
  else
    invalid=$((invalid+1))   # empty output counts as invalid
  fi
done
echo "== result over $n deterministic inputs =="
echo "  translated bzip2 hangs (never exits):      $hang / $n"
echo "  translated bzip2 output INVALID/empty:     $invalid / $n"
echo "  (original C bzip2: terminates and produces a valid stream on every input)"
