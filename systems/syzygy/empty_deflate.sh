#!/usr/bin/env bash
# UB-clean check of Syzygy's Zopfli on EMPTY input, at the library level.
# Why: the shipped C test driver (read_stdin_to_bytes + strlen) never NUL-terminates its buffer,
# so it is UB on every input (audit 4). Here we call ZopfliDeflate / zopfli_deflate directly with
# insize = 0 (the driver's options: verbose off, 15 iterations, blocksplitting 1, max 15) for each
# btype, build the C under ASan+UBSan, print the output bytes, and check each with zlib's raw
# inflate. Primary output = the compressed bytes.
#
#   docker run --rm --platform linux/amd64 -v "$PWD:/work" -w /work crust-syzygy \
#       bash systems/syzygy/empty_deflate.sh > results/syzygy_empty_deflate.json
#
# --profile debug|release: the Rust build profile. Default debug (`cargo build`, as shipped);
# release (`cargo build --release`) is the profile Syzygy's own runner uses (README.md:7,
# run_equiv.py:16 run ./rust_code/target/release/testing_equiv).
set -euo pipefail
ART=${ART:-/work/artifacts/syzygy_zopfli}
PROFILE=debug
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="${2:-}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$PROFILE" in
  debug) CARGO_PROFILE_ARGS=() ;;
  release) CARGO_PROFILE_ARGS=(--release) ;;
  *) echo "--profile must be debug or release" >&2; exit 2 ;;
esac
W=$(mktemp -d)

# ---- C side: original zopfli.c with its driver main renamed away ----
cat > "$W/h.c" <<'EOF'
#include <stdio.h>
#include <stdlib.h>
#include "zopfli.h"
void ZopfliDeflate(const ZopfliOptions *options, int btype, int final,
                   const unsigned char *in, size_t insize,
                   unsigned char *bp, unsigned char **out, size_t *outsize);
int main(void) {
  for (int bt = 0; bt <= 2; bt++) {
    ZopfliOptions o; o.verbose = 0; o.verbose_more = 0; o.numiterations = 15;
    o.blocksplitting = 1; o.blocksplittinglast = 0; o.blocksplittingmax = 15;
    unsigned char *out = 0; size_t outsize = 0; unsigned char bp = 0;
    static const unsigned char empty[1] = {0};
    ZopfliDeflate(&o, bt, 1, empty, 0, &bp, &out, &outsize);
    printf("%d ", bt);
    for (size_t i = 0; i < outsize; i++) printf("%02x", out[i]);
    printf("\n");
    free(out);
  }
  return 0;
}
EOF
gcc -O0 -g -fsanitize=address,undefined -fno-sanitize-recover=all -w -I"$ART/c_code" \
    -Dmain=syzygy_driver_main "$ART/c_code/zopfli.c" -c -o "$W/z.o"
gcc -O0 -g -fsanitize=address,undefined -fno-sanitize-recover=all -w -I"$ART/c_code" \
    "$W/h.c" "$W/z.o" -o "$W/c_h" -lm
C_OUT=$("$W/c_h" 2>"$W/c_err") || { echo "{\"error\":\"C harness failed\",\"stderr\":\"$(head -c 300 "$W/c_err")\"}"; exit 1; }

# ---- Rust side: the shipped crate with its driver main renamed and a harness main appended ----
cp -r "$ART/rust_code" "$W/rs"; rm -rf "$W/rs/target"
sed -i 's/^fn main() {/fn syzygy_driver_main() {/' "$W/rs/src/main.rs"
cat >> "$W/rs/src/main.rs" <<'EOF'

fn main() {
    for bt in 0..=2 {
        let o = ZopfliOptions { verbose: false, verbose_more: false, numiterations: 15,
                                blocksplitting: 1, blocksplittinglast: 0, blocksplittingmax: 15 };
        let mut out: Vec<u8> = Vec::new(); let mut outsize: usize = 0; let mut bp: u8 = 0;
        zopfli_deflate(&o, bt, 1, &[], 0, &mut bp, &mut out, &mut outsize);
        let hex: String = out[..outsize.min(out.len())].iter().map(|b| format!("{:02x}", b)).collect();
        println!("{} {}", bt, hex);
    }
}
EOF
( cd "$W/rs" && cargo build -q ${CARGO_PROFILE_ARGS[@]+"${CARGO_PROFILE_ARGS[@]}"} 2>/dev/null ) || { echo '{"error":"Rust harness build failed"}'; exit 1; }
R_OUT=$("$W/rs/target/$PROFILE/$(sed -n 's/^name *= *"\(.*\)"/\1/p' "$W/rs/Cargo.toml" | head -1)")

python3 - "$C_OUT" "$R_OUT" "$(rustc --version)" "$(gcc --version | head -1)" "$PROFILE" <<'PY'
import json, sys, zlib
def parse(s):
    d = {}
    for line in s.strip().splitlines():
        parts = line.split()
        d[int(parts[0])] = bytes.fromhex(parts[1]) if len(parts) > 1 else b""
    return d
def inflate(b):
    try:
        z = zlib.decompressobj(-15); r = z.decompress(b)
        return {"ok": z.eof and r == b"", "output": r.hex(), "eof": z.eof}
    except zlib.error as e:
        return {"ok": False, "error": str(e)}
c, r = parse(sys.argv[1]), parse(sys.argv[2])
rows = [{"btype": bt, "c_hex": c[bt].hex(), "rust_hex": r[bt].hex(),
         "c_inflate": inflate(c[bt]), "rust_inflate": inflate(r[bt]), "equal": c[bt] == r[bt]}
        for bt in sorted(c)]
out = {"input": "empty (insize=0)", "c_build": "gcc -O0 -fsanitize=address,undefined (clean)",
       "rustc": sys.argv[3], "gcc": sys.argv[4], "rows": rows}
if sys.argv[5] != "debug":                      # the shipped (debug) output stays byte-identical
    out = {"input": out["input"], "c_build": out["c_build"], "rustc": out["rustc"],
           "rust_profile": sys.argv[5] + " (cargo build --" + sys.argv[5] + ")", "gcc": out["gcc"], "rows": rows}
print(json.dumps(out, indent=1))
PY
rm -rf "$W"
