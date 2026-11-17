#!/bin/bash
# Re-run VERT's own bounded-Kani check on every function VERT records as bounded_kani=1, with the
# Kani version VERT pins (0.55.0) and VERT's exact command, and record whether the equivalence
# assertion (result == result_prime) is actually reached. Run from the repo root, in the pinned
# image that has Kani 0.55.0 preinstalled (docker build -t crust-kani -f docker/Dockerfile.kani docker/):
#   docker run --rm -v "$PWD:/w" -w /w crust-kani bash systems/vert/kani_vacuity/run_in_container.sh [unwinding_on]
# In a plain ubuntu:22.04 container it falls back to installing rustup and Kani 0.55.0 at run time.
set -e
if ! command -v cargo-kani >/dev/null 2>&1; then
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq build-essential curl python3 python3-pip ca-certificates pkg-config >/dev/null
  curl -sSf https://sh.rustup.rs | sh -s -- -y -q >/dev/null
  . "$HOME/.cargo/env"
  cargo install --locked kani-verifier@0.55.0 -q
  cargo kani setup >/dev/null
fi
cargo kani --version | grep -q '0\.55\.0' || { echo "need Kani 0.55.0, have: $(cargo kani --version)" >&2; exit 1; }
python3 systems/vert/kani_vacuity/${1:-check}.py
