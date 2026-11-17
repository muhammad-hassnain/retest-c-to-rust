#!/usr/bin/env bash
# RustMap bzip2, graded the way RustMap's own README grades it (§4.1.1 debug `cargo build`;
# §4.1.3 bzip2recover -> bzip2 -d -> diff against the input), but on held-out inputs:
# deterministic LCG binary streams of fixed sizes, plus printable-text controls shaped like the
# authors' random_N_chars.txt files. Records whether the Rust compressor terminates, whether its
# output passes RustMap's own check, and whether the C's output passes plain `bzip2 -d`.
#
#   docker run --rm --platform linux/amd64 -v "$PWD:/work" -w /work crust-syzygy \
#       bash systems/rustmap/check_bzip2.sh > results/rustmap_bzip2_check.json
set -uo pipefail
ART=${ART:-/work/artifacts/rustmap}
W=$(mktemp -d)
cp -r "$ART/rust-code/bzip2_rs_gpt" "$W/rs"; rm -rf "$W/rs/target"
cp -r "$ART/c-code/bzip2-1.0.8" "$W/c"
( cd "$W/c" && make bzip2 bzip2recover >/dev/null 2>&1 ) || { echo '{"error":"C build failed"}'; exit 2; }
( cd "$W/rs" && cargo build >/dev/null 2>&1 ) || { echo '{"error":"Rust build failed"}'; exit 2; }
C="$W/c/bzip2"; REC="$W/c/bzip2recover"; R="$W/rs/target/debug/bzip2_rs_gpt"
TC=$(rustc --version)

gen() {  # kind size seed
python3 - "$1" "$2" "$3" <<'PY'
import sys
kind, n, seed = sys.argv[1], int(sys.argv[2]), (int(sys.argv[3])*2654435761 + 12345) & 0xffffffff
b = bytearray()
for _ in range(n):
    seed = (1103515245*seed + 12345) & 0xffffffff
    v = (seed >> 16) & 0xff
    b.append(v if kind == "binary" else 33 + v % 94)
sys.stdout.buffer.write(bytes(b))
PY
}

echo "{\"toolchain\": \"$TC\", \"build\": \"cargo build (debug), as README 4.1.1\", \"rows\": ["
first=1
i=0
for kind in binary text; do
  for sz in 1 10 20 64 100 128 512 1000 2048 5000; do
    i=$((i+1)); d="$W/case$i"; mkdir -p "$d/c" "$d/r"
    gen "$kind" "$sz" "$i" > "$d/data"; cp "$d/data" "$d/c/data"; cp "$d/data" "$d/r/data"
    ( cd "$d/c" && "$C" --keep --force data >/dev/null 2>&1 )
    c_ok=$( (cd "$d/c" && "$C" -dc data.bz2 2>/dev/null | cmp -s - data) && echo true || echo false )
    ( cd "$d/r" && timeout 60 "$R" --keep --force data >/dev/null 2>&1 ); rrc=$?
    # RustMap's own check, verbatim shape: bzip2recover, bzip2 -d on rec00001, diff against input
    own="false"
    if [ -f "$d/r/data.bz2" ]; then
      ( cd "$d/r" && "$REC" data.bz2 >/dev/null 2>&1 && "$C" -d rec00001data.bz2 >/dev/null 2>&1 ) \
        && [ -f "$d/r/rec00001data" ] && cmp -s "$d/r/rec00001data" "$d/data" && own="true"
    fi
    [ $first -eq 1 ] || echo ","; first=0
    printf '{"kind":"%s","size":%d,"c_roundtrip_ok":%s,"rust_rc":%d,"rust_passes_rustmap_own_check":%s}' \
      "$kind" "$sz" "$c_ok" "$rrc" "$own"
  done
done
echo "]}"
rm -rf "$W"
