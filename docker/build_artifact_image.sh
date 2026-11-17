#!/usr/bin/env bash
# Build the reviewer image crust-artifact (docker/Dockerfile.artifact) and, optionally, save it.
#
#   bash docker/build_artifact_image.sh                 # context = a clean export of HEAD (git archive)
#   bash docker/build_artifact_image.sh DIR             # context = an already prepared copy of the repo
#   SAVE=crust-artifact.tar.gz bash docker/build_artifact_image.sh [DIR]   # also docker save | gzip, print sha256
#
# The build context never contains artifacts/ (third-party downloads; mount them at /artifacts).
# CPU use is capped with CPUS (default 4) via the classic builder's --cpu-quota.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${1:-}"
if [ -z "$SRC" ]; then
  SRC="$(mktemp -d)/retest-c-to-rust"
  mkdir -p "$SRC" && git -C "$HERE" archive HEAD | tar -x -C "$SRC"
fi
[ -f "$SRC/docker/Dockerfile.artifact" ] || { echo "no docker/Dockerfile.artifact in $SRC" >&2; exit 2; }
[ ! -e "$SRC/artifacts" ] || { echo "$SRC/artifacts exists: the context must not contain third-party downloads" >&2; exit 2; }
COMMIT="${SOURCE_COMMIT:-$(git -C "$HERE" rev-parse HEAD 2>/dev/null || echo unknown)}"
CPUS="${CPUS:-4}"
DOCKER_BUILDKIT=0 docker build --cpu-period 100000 --cpu-quota $((CPUS * 100000)) \
  --build-arg SOURCE_COMMIT="$COMMIT" -t "${TAG:-crust-artifact}" -f "$SRC/docker/Dockerfile.artifact" "$SRC"
docker image inspect "${TAG:-crust-artifact}" --format 'built {{.RepoTags}} {{.Id}} {{.Architecture}} {{.Size}} bytes'
if [ -n "${SAVE:-}" ]; then
  docker save "${TAG:-crust-artifact}" | gzip -6 > "$SAVE"
  ls -l "$SAVE"; sha256sum "$SAVE" 2>/dev/null || shasum -a 256 "$SAVE"
fi
