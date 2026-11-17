#!/usr/bin/env bash
# Run one ACTOR re-grade step inside Docker on the x86 box (all third-party code runs only here).
#   image:     crust-heldout:latest (Ubuntu 22.04; gcc 11.4, clang 14; sha256:d57dd28c18ee...)
#   Rust:      the host's rustup toolchain nightly-2026-01-10 (rustc 1.94.0-nightly a3f2d5abe),
#              mounted read-only at /rust. ACTOR pins channel 1.94.0 (rust-toolchain.toml); the
#              1.94.0 stable release is not on this box, so the
#              1.94.0 pre-release nightly is the closest available toolchain (recorded as a deviation).
#              Override with RUST_TC=<toolchain dir name> (e.g. stable-x86_64-unknown-linux-gnu,
#              rustc 1.99.1) for the toolchain cross-check.
#   crates:    the host's ~/.cargo/registry mounted read-only (index + .crate cache); --offline.
#   network:   none. CPUs: 3 (CPUS=<n> to lower it while another step runs).
# Env: CPUS (default 3), MEM (default 6g), ACTORUW_HOST_W (default ~/work/regrade-actoruw), RUST_TC,
#      RUST_TC_DIR (optional host path of the toolchain dir mounted at /rust instead of
#      ~/.rustup/toolchains/$RUST_TC; arXiv issue 34: ACTOR's pinned 1.94.0 stable, installed outside
#      ~/.rustup, for arxiv_pinned_toolchain.py). Unset (the default), nothing changes.
# Usage: run_in_container.sh <python script under scripts/> [args...]
set -euo pipefail
W="${ACTORUW_HOST_W:-${HOME}/work/regrade-actoruw}"   # ACTORUW_HOST_W: another work root (e.g. ~/work/actor-mut)
TC="${RUST_TC:-nightly-2026-01-10-x86_64-unknown-linux-gnu}"
TCDIR="${RUST_TC_DIR:-${HOME}/.rustup/toolchains/${TC}}"
mkdir -p "${W}/cargo_home/registry/src" "${W}/out" "${W}/build"
[ -L "${W}/cargo_home/registry/index" ] || ln -s /cargo_ro/index "${W}/cargo_home/registry/index"
[ -L "${W}/cargo_home/registry/cache" ] || ln -s /cargo_ro/cache "${W}/cargo_home/registry/cache"
exec docker run --rm --network none --cpus "${CPUS:-3}" --memory "${MEM:-6g}" \
  --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -e CARGO_HOME=/w/cargo_home -e CARGO_NET_OFFLINE=true \
  -e PATH=/rust/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  -e ACTORUW_W=/w -e ACTORUW_TC="${TC}" \
  -e OUB_IMAGE="crust-heldout:latest" \
  -e OUB_IMAGE_ID="$(docker image inspect crust-heldout:latest --format '{{.Id}}')" \
  -e OUB_HOST="$(uname -s)" -e OUB_GIT_REV="${OUB_GIT_REV:-?}" \
  -e OUB_COMMAND="systems/actoruw/run_in_container.sh $*" \
  -v "${W}:/w" \
  -v "${TCDIR}:/rust:ro" \
  -v "${HOME}/.cargo/registry:/cargo_ro:ro" \
  -w /w/scripts crust-heldout:latest python3 -I "$@"
