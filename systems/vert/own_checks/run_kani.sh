#!/bin/bash
# docker run --rm -v "$PWD:/w" -w /w ubuntu:22.04@sha256:0a9887931b0e38af2e3487ed39e29436b0535c4f3f52e57745baaf79d1898956 bash systems/vert/own_checks/run_kani.sh FUNC...
# Kani as pinned by VERT's readme.md: kani-verifier 0.55.0.
set -e
apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq build-essential curl python3 python3-pip ca-certificates pkg-config >/dev/null
curl -sSf https://sh.rustup.rs | sh -s -- -y -q >/dev/null
. "$HOME/.cargo/env"
cargo install --locked kani-verifier@0.55.0 -q
cargo kani setup >/dev/null
python3 systems/vert/own_checks/check.py "${VERT_ROOT:-$PWD/artifacts/vert}" results_kani.json kani_correct "$@"
