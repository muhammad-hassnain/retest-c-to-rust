#!/usr/bin/env bash
# Run a PtrTrans quadtree harness step inside Docker (all third-party code is built and run only
# in the container; no network; 2 CPUs).
#
#   bash systems/ptrtrans/quadtree_e2e/run_in_docker.sh <image> <script.py> [args...]
#
# Expects: the study repo (or a copy) at $REPO, the fetched artifact at $ART (read-only mount),
# a scratch build dir at $BUILD. Used on the x86-64 box under ~/work/regrade-ptrtrans/.
set -euo pipefail
IMG="$1"; shift
SCRIPT="$1"; shift
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
ART="${ART:-$REPO/artifacts/ptrtrans}"
BUILD="${BUILD:-$HOME/work/regrade-ptrtrans/build}"
mkdir -p "$BUILD"
IID=$(docker image inspect "$IMG" --format '{{.Id}}')
export OUB_GIT_REV="${OUB_GIT_REV:-$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo '?')}"
export OUB_HOST="${OUB_HOST:-$(uname -s) ($(uname -m), $(nproc) cores)}"
export PT_COMMIT="${PT_COMMIT:-b20d5bb68a598a739f79d52114651e37cc6999cb}"
docker run --rm --network none --cpus 2 --memory 4g --memory-swap 4g --ulimit core=0 \
  -e OUB_GIT_REV -e OUB_HOST -e PT_COMMIT -e OUB_IMAGE="$IMG" -e OUB_IMAGE_ID="$IID" \
  -e OUB_WORKTREE_NOTE="study repo subset (harness/oracle_ub_common.py, systems/ptrtrans/) copied to the run host; uncommitted" \
  -e OUB_COMMAND="bash systems/ptrtrans/quadtree_e2e/run_in_docker.sh $IMG $SCRIPT $* (image $IID)" \
  -v "$REPO:/work" -v "$ART:/art:ro" -v "$BUILD:/oub" -w /work "$IMG" \
  bash -c "python3 $SCRIPT $*; rc=\$?; chown -R $(id -u):$(id -g) /oub /work/results 2>/dev/null; exit \$rc"
