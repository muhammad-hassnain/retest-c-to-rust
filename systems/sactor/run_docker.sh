#!/usr/bin/env bash
# Run a command for the SACTOR re-grade inside Docker on the x86-64 box. Third-party code (SACTOR's
# Rust, libogg, CRUST-Bench C) is only ever built and run inside this container: --network none,
# artifacts mounted read-only, at most 3 CPUs.
#
#   bash systems/sactor/run_docker.sh python3 systems/sactor/libogg/run.py ...
#
# Layout on the host (WORK=~/work/regrade-sactor):
#   $WORK/repo        copy of the study repo files this needs (harness/oracle_ub_common.py, systems/sactor)
#   $WORK/artifacts   pinned clones (systems/sactor/fetch.sh with SACTOR_ART=$WORK/artifacts/sactor), read-only
#   $WORK/vendor      libc-0.2.191 unpacked from the host cargo cache (crates.io checksum 65cd87c9...)
#   $WORK/cargo_home  config.toml: crates-io -> /vendor, offline
#   $WORK/build, $WORK/out  scratch
#
# Optional: SACTOR_EXTRA_TC=<name>=<host toolchain dir> also mounts that toolchain read-only at
# /opt/tc/<name> (arXiv issue 34: SACTOR's pinned nightly-2024-12-18, installed outside ~/.rustup;
# used by systems/sactor/arxiv_pinned_toolchain.py). Unset (the default), nothing changes.
set -euo pipefail
WORK="${WORK:-$HOME/work/regrade-sactor}"
IMG="${SACTOR_IMAGE:-crust-heldout}"
IID=$(docker image inspect "$IMG" --format '{{.Id}}')
TC="$HOME/.rustup/toolchains"
EXTRA_TC=()
if [ -n "${SACTOR_EXTRA_TC:-}" ]; then
  EXTRA_TC=(-v "${SACTOR_EXTRA_TC#*=}:/opt/tc/${SACTOR_EXTRA_TC%%=*}:ro")
fi
exec docker run --rm --network none --cpus "${CPUS:-3}" --ulimit core=0 \
  -e SACTOR_GIT_REV="${SACTOR_GIT_REV:-?}" -e SACTOR_HOST="$(uname -s) ($(uname -m), $(nproc) cores)" \
  -e SACTOR_IMAGE="$IMG" -e SACTOR_IMAGE_ID="$IID" -e CARGO_HOME=/cargo_home -e SACTOR_ART=/art/sactor \
  -e SACTOR_COMMAND="bash systems/sactor/run_docker.sh $*" \
  -v "$WORK/repo:/work" -v "$WORK/artifacts:/art:ro" -v "$WORK/vendor:/vendor:ro" \
  -v "$WORK/cargo_home:/cargo_home" -v "$WORK/build:/build" -v "$WORK/out:/out" \
  -v "$TC/nightly-2024-09-03-x86_64-unknown-linux-gnu:/opt/tc/nightly-2024-09-03:ro" \
  -v "$TC/nightly-2025-05-10-x86_64-unknown-linux-gnu:/opt/tc/nightly-2025-05-10:ro" \
  ${EXTRA_TC[@]+"${EXTRA_TC[@]}"} \
  -w /work "$IMG" "$@"
