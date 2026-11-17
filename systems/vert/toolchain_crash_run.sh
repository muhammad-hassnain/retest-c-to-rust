#!/bin/bash
# Driver for systems/vert/toolchain_crash_recheck.py (FULL C-UB rule on VERT crash defects / wrong answers).
#   bash systems/vert/toolchain_crash_run.sh IMAGE MODE OUT_JSON CPUS
#   arm64 (primary): IMAGE=crust-vert-toolchain
#     MODE=crash OUT=results/vert_crash_ub_recheck.json ; MODE=wrong OUT=results/vert_wrong_answer_ub_recheck.json
set -euo pipefail
IMG=$1; MODE=$2; OUT=$3; CPUS=$4
REPO=$(cd "$(dirname "$0")/../.." && pwd)
ART=$(cd "$REPO/artifacts" && pwd -P)
OUTDIR=$(cd "$(dirname "$OUT")" && pwd)
GIT_REV=${GIT_REV:-$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)}
docker run --rm --cpus "$CPUS" -v "$REPO:/work" -v "$ART:/work/artifacts:ro" -v "$OUTDIR:/out" -w /work \
  -e RUSTUP_TOOLCHAIN=1.86.0 -e GIT_REV="$GIT_REV" -e HOST_DESC="$(uname -sm), docker --cpus $CPUS" \
  -e IMAGE_DESC="$IMG $(docker image inspect "$IMG" --format '{{.Id}}')" \
  "$IMG" python3 systems/vert/toolchain_crash_recheck.py "$MODE" --jobs "$CPUS" --out "/out/$(basename "$OUT")"
