#!/bin/bash
# Full-log rerun of single arms, for diagnosing results_bolero.json rows (same tooling as run_in_container.sh).
set -e
apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq build-essential clang curl python3 ca-certificates pkg-config libssl-dev git >/dev/null
curl -sSf https://sh.rustup.rs | sh -s -- -y -q >/dev/null
. "$HOME/.cargo/env"
rustup toolchain install nightly-2023-07-29 --profile minimal >/dev/null 2>&1
cargo install cargo-bolero --git https://github.com/YoshikiTakashima/bolero.git --rev 1d02eb853af3dad36eba5f03a9a8c41e5488cfef --locked -q 2>/dev/null
B=artifacts/vert/benchmark/c_transcoder
for f in SUM_OF_ALL_PROPER_DIVISORS_OF_A_NATURAL_NUMBER MAXIMUM_GAMES_PLAYED_WINNER; do
  rm -rf /tmp/d/$f; mkdir -p /tmp/d; cp -r $B/$f/out-rwasm-bolero /tmp/d/$f; cp artifacts/vert/benchmark/rust-toolchain.toml /tmp/d/$f/
  cd /tmp/d/$f; mkdir -p src/__fuzz__
  echo "##### $f vert_reduce"; ls -R src/__fuzz__ | head
  RUSTFLAGS="-C overflow-checks=false" cargo bolero reduce bolero_wasm_eq 2>&1 | grep -v "^warning\|^ *|\|^ *=\|^ *-->\|^$\|Compiling\|Downloaded" | tail -25; echo "rc=${PIPESTATUS[0]}"
  echo "##### $f fuzz 20s on VERT's own translation"
  RUSTFLAGS="-C overflow-checks=false" cargo bolero test bolero_wasm_eq -T 20s 2>&1 | grep -v "^warning\|^ *|\|^ *=\|^ *-->\|^$\|Compiling\|Downloaded" | tail -40; echo "rc=${PIPESTATUS[0]}"
  cd /w
done
