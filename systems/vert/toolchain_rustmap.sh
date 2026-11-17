#!/bin/bash
# M3 item 3: rebuild RustMap's Rosetta translations under several toolchains
# (harness/toolchain_audit.py rustmap). Builds image crust-rustmap-toolchains FROM a base that has
# gcc 11.4 + rustc 1.86.0 and no `clang` alias, adds the extra toolchains, and runs the rebuild.
#
#   bash systems/vert/toolchain_rustmap.sh BASE_IMAGE SCRATCH_DIR OUT_JSON CPUS GIT_REV
#   e.g. Mac:   bash systems/vert/toolchain_rustmap.sh crust-vert-toolchain $SCR $SCR/rustmap_mac.json 3 <rev>
#        Linux: bash systems/vert/toolchain_rustmap.sh crust-syzygy ~/work/toolchain/scratch .../rustmap_linux.json 4 <rev>
# Toolchains: 1.86.0 (our primary), nightly-2023-07-29 (VERT's pin, a 1.73 nightly), and stable
# 1.69.0 / 1.73.0 / 1.74.0 as candidates for the unrecorded toolchain behind RustMap's own run log.
set -euo pipefail
BASE=$1; SCR=$2; OUT=$3; CPUS=$4; GIT_REV=$5
REPO=$(cd "$(dirname "$0")/../.." && pwd)
TCS=${TCS:-"1.86.0,nightly-2023-07-29,1.69.0,1.73.0,1.74.0"}   # override with env TCS=...
docker build -t crust-rustmap-toolchains - <<DOCKERFILE
FROM $BASE
RUN for t in $(echo "$TCS" | tr , " "); do rustup toolchain install \$t --profile minimal; done \
    && rustup default 1.86.0 && rustup toolchain list && ! command -v clang
DOCKERFILE
mkdir -p "$SCR"
IMAGE_DESC="crust-rustmap-toolchains $(docker image inspect crust-rustmap-toolchains --format '{{.Id}}') FROM $BASE $(docker image inspect $BASE --format '{{.Id}}')"
HOST_DESC="$(uname -sm), docker --cpus $CPUS"
docker run --rm --cpus "$CPUS" -v "$REPO:/work" -v "$(readlink -f "$REPO/artifacts" 2>/dev/null || echo "$REPO/artifacts"):/work/artifacts:ro" \
  -v "$SCR:/scratch" -w /work -e GIT_REV="$GIT_REV" -e HOST_DESC="$HOST_DESC" -e IMAGE_DESC="$IMAGE_DESC" \
  crust-rustmap-toolchains python3 harness/toolchain_audit.py rustmap --toolchains "$TCS" \
  --out "/scratch/$(basename "$OUT")" --scratch /scratch
