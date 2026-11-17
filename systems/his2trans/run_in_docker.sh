#!/usr/bin/env bash
# Run a His2Trans re-grade step inside the pinned image, never on the host (the artifact is untrusted).
#   bash systems/his2trans/run_in_docker.sh <python-script-relative-to-repo> [args...]
# Layout expected (x86-64 box, ~/work/regrade-his2trans):
#   $WORK/study     copy of this repo's systems/his2trans, systems/enumerate_inputs.py,
#                   harness/oracle_ub_common.py, results/   (mounted rw at /work)
#   $WORK/artifact  the pinned His2Trans checkout (systems/his2trans/fetch.sh)    (ro at /art)
#   $WORK/vendor    cc/find-msvc-tools/libc/shlex .crate sources, sha256 = Cargo.lock checksums (ro)
#   $WORK/build     scratch                                                        (rw at /b)
# Image: crust-heldout-plus186 (Ubuntu 22.04, gcc 11.4, clang 14, rustup toolchains nightly-2022-08-08
# and 1.86.0; 1.86.0 is selected unless TOOLCHAIN is set). No network inside the container.
set -euo pipefail
WORK="${WORK:-$HOME/work/regrade-his2trans}"
IMG="${IMG:-crust-heldout-plus186}"
TOOLCHAIN="${TOOLCHAIN:-1.86.0}"   # rustup toolchain selected inside the image
IID=$(docker image inspect "$IMG" --format '{{.Id}}')
COMMIT=$(git -C "$WORK/artifact" rev-parse HEAD)
docker run --rm --network none --cpus 3 --ulimit core=0 \
  -e OUB_GIT_REV="${OUB_GIT_REV:-?}" -e OUB_HOST="${OUB_HOST:-$(uname -s) ($(uname -m), $(nproc) cores)}" \
  -e OUB_IMAGE="$IMG" -e OUB_IMAGE_ID="$IID" -e H2T_COMMIT="$COMMIT" -e H2T_ART=/art \
  -e OUB_COMMAND="bash systems/his2trans/run_in_docker.sh $* (docker image $IMG $IID)" \
  -e OUB_WORKTREE_NOTE="study files copied to the x86 box; systems/his2trans uncommitted" \
  -e CARGO_HOME=/tmp/cargohome -e PYTHONDONTWRITEBYTECODE=1 -e TOOLCHAIN="$TOOLCHAIN" \
  -v "$WORK/study:/work" -v "$WORK/artifact:/art:ro" -v "$WORK/vendor:/vendor:ro" \
  -v "$WORK/cargo_cfg:/cfg:ro" -v "$WORK/build:/b" -w /work "$IMG" \
  bash -c 'mkdir -p $CARGO_HOME && cp /cfg/config.toml $CARGO_HOME/config.toml && rustup default "$TOOLCHAIN" >/dev/null 2>&1 && python3 "$@"' _ "$@"
