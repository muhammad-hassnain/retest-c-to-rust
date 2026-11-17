#!/bin/bash
# M3 items 4+5 driver: run systems/vert/toolchain_ub_recheck.py on every VERT wrong answer.
#   bash systems/vert/toolchain_ub_run.sh IMAGE OUT_JSON CPUS [extra args]
# macOS arm64 (primary platform):  IMAGE=crust-vert-toolchain (toolchain_image.sh), OUT=results/vert_wrong_answer_ub_recheck.json
# Linux x86-64 cross-check:        IMAGE=crust-vert-toolchain-x86 (same recipe FROM crust-rustmap-toolchains), OUT in scratch
# Rust side built with 1.86.0, the primary's toolchain (the pinned nightly gives identical verdicts and
# samples on all 176 scalar functions; see results/vert_diff_pinned_nightly.json).
set -euo pipefail
IMG=$1; OUT=$2; CPUS=$3; shift 3
REPO=$(cd "$(dirname "$0")/../.." && pwd)
ART=$(cd "$REPO/artifacts" && pwd -P)
OUTDIR=$(cd "$(dirname "$OUT")" && pwd)
GIT_REV=${GIT_REV:-$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)}
HOST_DESC="$(uname -sm), docker --cpus $CPUS"
IMAGE_DESC="$IMG $(docker image inspect "$IMG" --format '{{.Id}}')"
docker run --rm --cpus "$CPUS" -v "$REPO:/work" -v "$ART:/work/artifacts:ro" -v "$OUTDIR:/out" -w /work \
  -e RUSTUP_TOOLCHAIN=1.86.0 -e GIT_REV="$GIT_REV" -e HOST_DESC="$HOST_DESC" -e IMAGE_DESC="$IMAGE_DESC" \
  "$IMG" python3 systems/vert/toolchain_ub_recheck.py --jobs "$CPUS" --out "/out/$(basename "$OUT")" "$@"
