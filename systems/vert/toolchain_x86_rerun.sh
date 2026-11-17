#!/bin/bash
# Follow-up: which Rust toolchain did the x86-64 VERT re-run (results/vert_diff_x86_spread.json)
# use? On the Linux box crust-stable and crust-heldout are the SAME image (rustc 1.65.0-nightly =
# nightly-2022-08-08, with clang 14), and every x86 result file records msan=True (clang on PATH) for all
# tested functions. Re-run the current scalar harness (run.py, spread grid, MSan path) on x86-64 under
# BOTH candidate toolchains in ONE image (crust-heldout + rustup 1.86.0), and diff each per function
# against vert_diff_x86_spread.json and against each other.
#   bash systems/vert/toolchain_x86_rerun.sh SCRATCH CPUS GIT_REV      (Linux x86-64, private repo copy)
set -euo pipefail
SCR=$1; CPUS=$2; GIT_REV=$3
REPO=$(cd "$(dirname "$0")/../.." && pwd)
docker build -t crust-heldout-plus186 - <<'DOCKERFILE'
FROM crust-heldout
RUN rustup toolchain install 1.86.0 --profile minimal && rustup default nightly-2022-08-08 && rustup toolchain list && command -v clang
DOCKERFILE
IMAGE_DESC="crust-heldout-plus186 $(docker image inspect crust-heldout-plus186 --format '{{.Id}}') FROM crust-heldout $(docker image inspect crust-heldout --format '{{.Id}}')"
ART=$(readlink -f "$REPO/artifacts")
for TC in nightly-2022-08-08 1.86.0; do
  docker run --rm --cpus "$CPUS" -v "$REPO:/work" -v "$ART:/work/artifacts:ro" -v "$SCR:/scratch" -w /work \
    -e RUSTUP_TOOLCHAIN=$TC -e GIT_REV="$GIT_REV" -e HOST_DESC="$(uname -sm), docker --cpus $CPUS" \
    -e IMAGE_DESC="$IMAGE_DESC" crust-heldout-plus186 \
    python3 systems/vert/toolchain_pinned.py --harness scalar --expect-clang --jobs "$CPUS" \
      --out /scratch/vert_diff_x86_msan_$TC.json --baseline results/vert_diff_x86_spread.json \
      $( [ "$TC" = 1.86.0 ] && echo "--extra-baseline nightly-2022-08-08=/scratch/vert_diff_x86_msan_nightly-2022-08-08.json" )
done
