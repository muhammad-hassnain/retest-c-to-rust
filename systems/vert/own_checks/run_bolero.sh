#!/bin/bash
# docker run --rm -v "$PWD:/w" -w /w ubuntu:22.04@sha256:0a9887931b0e38af2e3487ed39e29436b0535c4f3f52e57745baaf79d1898956 bash systems/vert/own_checks/run_bolero.sh FUNC...
# Tools as VERT's readme.md "Checking Tools": cargo-bolero from VERT's fork (branch kani-unwind-0, head 1d02eb8);
# projects build with VERT's pinned nightly-2023-07-29 (benchmark/rust-toolchain.toml).
set -e
apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq build-essential clang curl python3 ca-certificates pkg-config libssl-dev git >/dev/null
curl -sSf https://sh.rustup.rs | sh -s -- -y -q >/dev/null
. "$HOME/.cargo/env"
rustup toolchain install nightly-2023-07-29 --profile minimal >/dev/null 2>&1
cargo install cargo-bolero --git https://github.com/YoshikiTakashima/bolero.git --rev 1d02eb853af3dad36eba5f03a9a8c41e5488cfef --locked -q 2>/dev/null
git clone -q https://github.com/YoshikiTakashima/bolero.git /tmp/fork && git -C /tmp/fork log -1 --format='fork commit %H date %cI' 1d02eb853af3dad36eba5f03a9a8c41e5488cfef > results_fork_commit.txt
python3 systems/vert/own_checks/check.py "${VERT_ROOT:-$PWD/artifacts/vert}" results_vert_script.json vert_script "$@"
python3 systems/vert/own_checks/check.py "${VERT_ROOT:-$PWD/artifacts/vert}" results_fuzz.json fuzz_60s "$@"
