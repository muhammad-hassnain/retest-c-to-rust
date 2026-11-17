#!/bin/bash
# M3 driver (host side, macOS arm64): run VERT's scalar and array/float harnesses under VERT's
# pinned nightly-2023-07-29 and, as a same-session control, under 1.86.0 (the primary's rustc),
# in crust-vert-toolchain (toolchain_image.sh), limited to 3 CPUs.
#
#   bash systems/vert/toolchain_run.sh SCRATCH_DIR [scalar|arrays|all]
#
# Controls are written to SCRATCH_DIR and embedded (summary, provenance, verdicts) in the final
# results/vert_diff_pinned_nightly.json and results/vert_arrays_pinned_nightly.json.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
SCR=${1:?scratch dir}; WHAT=${2:-all}
mkdir -p "$SCR"
GIT_REV=$(git -C "$REPO" rev-parse HEAD)
GIT_DIRTY="working tree has uncommitted changes: $(git -C "$REPO" status --porcelain | wc -l | tr -d ' ') paths (files outside the harness); harness files used are unmodified: $(git -C "$REPO" status --porcelain systems/vert/run.py systems/vert/run_arrays.py systems/batch_diff.py systems/enumerate_inputs.py | wc -l | tr -d ' ') changed"
HOST_DESC="macOS $(sw_vers -productVersion) $(uname -m), Docker Desktop $(docker version --format '{{.Server.Version}}'), docker --cpus 3"
IMAGE_DESC="crust-vert-toolchain $(docker image inspect crust-vert-toolchain --format '{{.Id}}') FROM crust-stable $(docker image inspect crust-stable --format '{{.Id}}')"
run() {  # $1 toolchain, rest: args
  local tc=$1; shift
  docker run --rm --cpus 3 -v "$REPO:/work" -v "$SCR:/scratch" -w /work \
    -e RUSTUP_TOOLCHAIN="$tc" -e GIT_REV="$GIT_REV" -e GIT_DIRTY="$GIT_DIRTY" \
    -e HOST_DESC="$HOST_DESC" -e IMAGE_DESC="$IMAGE_DESC" \
    crust-vert-toolchain python3 systems/vert/toolchain_pinned.py "$@"
}
if [ "$WHAT" = scalar ] || [ "$WHAT" = all ]; then
  run 1.86.0 --harness scalar --out /scratch/vert_diff_control_1.86.0.json --baseline results/vert_diff.json --jobs 3
  run nightly-2023-07-29 --harness scalar --out results/vert_diff_pinned_nightly.json \
      --baseline results/vert_diff.json --extra-baseline control_1.86.0=/scratch/vert_diff_control_1.86.0.json \
      --embed-control /scratch/vert_diff_control_1.86.0.json --jobs 3
fi
if [ "$WHAT" = arrays ] || [ "$WHAT" = all ]; then
  run 1.86.0 --harness arrays --out /scratch/vert_arrays_control_1.86.0.json --baseline results/vert_arrays.json --jobs 3
  run nightly-2023-07-29 --harness arrays --out results/vert_arrays_pinned_nightly.json \
      --baseline results/vert_arrays.json --extra-baseline control_1.86.0=/scratch/vert_arrays_control_1.86.0.json \
      --embed-control /scratch/vert_arrays_control_1.86.0.json --jobs 3
fi
