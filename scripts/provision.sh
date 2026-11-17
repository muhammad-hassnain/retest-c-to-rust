#!/usr/bin/env bash
# Systematic provisioning for a fresh machine (written for an x86_64 Linux desktop).
# Builds the pinned Docker images and fetches every upstream artifact, so the full
# validation can run reproducibly. Idempotent: skips images/artifacts already present.
#
#   bash scripts/provision.sh            # build everything + fetch all artifacts
#   bash scripts/provision.sh images     # only build images
#   bash scripts/provision.sh artifacts  # only fetch artifacts
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
LOG(){ echo "[provision] $*"; }

build_image(){  # name  dockerfile
  local name="$1" df="$2"
  if docker image inspect "$name" >/dev/null 2>&1; then LOG "image $name present, skip"; return 0; fi
  LOG "building $name from $df ..."
  docker build -t "$name" -f "$df" docker/ && LOG "built $name" || { LOG "FAILED $name"; return 1; }
}

do_images(){
  # order matters: afl images are FROM the base images
  build_image crust-stable       docker/Dockerfile.stable
  build_image crust-heldout      docker/Dockerfile
  build_image crust-syzygy       docker/Dockerfile.syzygy
  build_image crust-afl          docker/Dockerfile.afl
  build_image crust-afl-nightly  docker/Dockerfile.afl-nightly
  LOG "images:"; docker images --format '  {{.Repository}}:{{.Tag}} ({{.Size}})' | grep -i crust || true
}

do_artifacts(){
  LOG "fetching C2SaferRust ...";  bash scripts/fetch_artifact.sh      >/dev/null 2>&1 && LOG "  c2saferrust ok" || LOG "  c2saferrust FAILED"
  for s in vert rustmap rustine syzygy flourine rustassure actoruw adatrans his2trans ptrtrans sactor; do
    LOG "fetching $s ...";  bash "systems/$s/fetch.sh"                 >/dev/null 2>&1 && LOG "  $s ok" || LOG "  $s FAILED"
  done
  LOG "artifacts under artifacts/:"; ls -1 artifacts/ 2>/dev/null | sed 's/^/  /'
}

case "${1:-all}" in
  images)    do_images ;;
  artifacts) do_artifacts ;;
  all)       do_images; do_artifacts ;;
  *) echo "usage: $0 [all|images|artifacts]"; exit 2 ;;
esac
LOG "done."
