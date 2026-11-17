#!/bin/bash
# docker run --rm -v "$PWD:/w" -w /w ubuntu:22.04 bash systems/vert/bolero_vacuity/run_in_container.sh OUT.json FUNC...
# Tooling exactly as VERT's readme.md "Checking Tools": cargo-bolero from VERT's fork
# (YoshikiTakashima/bolero, branch kani-unwind-0, pinned here to its head 1d02eb8); projects build with
# the toolchain VERT pins in benchmark/rust-toolchain.toml (nightly-2023-07-29).
set -e
apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq build-essential clang curl python3 ca-certificates pkg-config libssl-dev git >/dev/null
curl -sSf https://sh.rustup.rs | sh -s -- -y -q >/dev/null
. "$HOME/.cargo/env"
rustup toolchain install nightly-2023-07-29 --profile minimal
cargo install cargo-bolero --git https://github.com/YoshikiTakashima/bolero.git --rev 1d02eb853af3dad36eba5f03a9a8c41e5488cfef --locked \
  || cargo install cargo-bolero --git https://github.com/YoshikiTakashima/bolero.git --rev 1d02eb853af3dad36eba5f03a9a8c41e5488cfef -q
cargo bolero --version || true
OUT=$1; shift
python3 systems/vert/bolero_vacuity/check.py artifacts/vert/benchmark/c_transcoder "$OUT" "$@"
