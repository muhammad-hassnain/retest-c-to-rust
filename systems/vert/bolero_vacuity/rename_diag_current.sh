#!/bin/bash
# Diagnostic only (not part of the counted arms): NUMBER_WHICH_HAS_THE_MAXIMUM_NUMBER_OF_DISTINCT_PRIME_FACTORS_IN_RANGE_M_TO_N
# fails to build under check.py with "File name too long": rustc names the object file
# <crate>-<hash>.<crate>.<hash>-cgu.0.rcgu.o and the crate name is 107 characters, so the file name
# exceeds the 255-byte limit. Same container setup as run_in_container_current.sh; the project is
# copied and ONLY the [package] name in Cargo.toml is shortened (as kani_vacuity/check.py does for
# Kani), then check_current.py runs the same three arms. Run from the repo root:
#   docker run --rm --cpus=... -v "$PWD:/w" -w /w ubuntu:22.04 bash systems/vert/bolero_vacuity/rename_diag_current.sh
set -e
apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq build-essential clang curl python3 ca-certificates pkg-config libssl-dev git >/dev/null
curl -sSf https://sh.rustup.rs | sh -s -- -y -q >/dev/null
. "$HOME/.cargo/env"
rustup toolchain install nightly-2023-07-29 --profile minimal
cargo install cargo-bolero --git https://github.com/YoshikiTakashima/bolero.git --rev 1d02eb853af3dad36eba5f03a9a8c41e5488cfef --locked \
  || cargo install cargo-bolero --git https://github.com/YoshikiTakashima/bolero.git --rev 1d02eb853af3dad36eba5f03a9a8c41e5488cfef -q
# RENAME_FUNC (opt-in): another function with the same "File name too long" compile error
F=${RENAME_FUNC:-NUMBER_WHICH_HAS_THE_MAXIMUM_NUMBER_OF_DISTINCT_PRIME_FACTORS_IN_RANGE_M_TO_N}
OUTDIR=${OUTDIR:-results/vert_bolero_current_work}
export BOLERO_LOGDIR=results/vert_bolero_logs_current/rename_diag
mkdir -p "$OUTDIR" "$BOLERO_LOGDIR" /tmp/bench/c_transcoder/$F
cp -r artifacts/vert/benchmark/c_transcoder/$F/out-rwasm-bolero /tmp/bench/c_transcoder/$F/
cp artifacts/vert/benchmark/rust-toolchain.toml /tmp/bench/
python3 - /tmp/bench/c_transcoder/$F/out-rwasm-bolero/Cargo.toml <<'EOF'
import re, sys
p = sys.argv[1]; t = open(p).read()
t2 = re.sub(r'(\[package\][^\[]*?\bname\s*=\s*)"[^"]*"', r'\1"nwh"', t, count=1, flags=re.S)
assert t2 != t
open(p, "w").write(t2)
EOF
diff -r artifacts/vert/benchmark/c_transcoder/$F/out-rwasm-bolero /tmp/bench/c_transcoder/$F/out-rwasm-bolero || true
python3 systems/vert/bolero_vacuity/check_current.py /tmp/bench/c_transcoder "$OUTDIR/rename_diag.json" $F
