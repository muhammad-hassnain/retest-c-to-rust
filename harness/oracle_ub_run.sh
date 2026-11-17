#!/usr/bin/env bash
# Driver for the oracle-UB validation (audit M3). Runs on the x86-64 Linux box, from a COPY of the
# study repo whose artifacts/ is a symlink to the read-only artifact tree.
#
#   bash harness/oracle_ub_run.sh <c2saferrust|rustassure|rustmap_bzip2|rustmap_rosetta|flourine|flourine_current|pending> [args...]
#
# C oracles are always built and run in crust-heldout (Ubuntu 22.04: gcc 11.4, clang 14, and the
# nightly-2022-08-08 Rust that C2SaferRust and the libcsv crate need). Systems whose Rust needs a
# recent toolchain (RustMap, Flourine) first build the Rust in crust-syzygy (rustc 1.86.0, the
# version of the original runs) -- phase "rust" -- then run everything in crust-heldout -- phase "c".
set -euo pipefail
SYS="$1"; shift || true
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ART="$(readlink -f "$REPO/artifacts")"
JOBS="${JOBS:-8}"
export OUB_GIT_REV="${OUB_GIT_REV:-$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo '?')}"
export OUB_HOST="${OUB_HOST:-$(uname -s) ($(uname -m), $(nproc) cores)}"
SHARED="${OUB_SHARED:-$HOME/work/oracle-ub/build}"   # scratch build dir shared by the two phases
mkdir -p "$SHARED"

run_in() {  # image, then the command inside /work
  local img="$1"; shift
  local iid; iid=$(docker image inspect "$img" --format '{{.Id}}')
  docker run --rm --ulimit core=0 --cpus "$JOBS" \
    -e OUB_GIT_REV -e OUB_HOST -e OUB_IMAGE="$img" -e OUB_IMAGE_ID="$iid" \
    -e OUB_COMMAND="bash harness/oracle_ub_run.sh $SYS $* (docker image $img $iid)" \
    -e OUB_RUST_IMAGE="${OUB_RUST_IMAGE:-}" -e OUB_RUST_IMAGE_ID="${OUB_RUST_IMAGE_ID:-}" \
    -v "$REPO:/work" -v "$ART:$ART:ro" -v "$SHARED:/oub" -w /work "$img" "$@"
}

case "$SYS" in
  c2saferrust)
    run_in crust-heldout python3 harness/oracle_ub_c2saferrust.py --jobs "$JOBS" --build /oub/c2sr "$@" ;;
  rustassure)
    run_in crust-heldout python3 harness/oracle_ub_rustassure.py --build /oub/libcsv "$@" ;;
  rustmap_bzip2|rustmap_rosetta|flourine)
    export OUB_RUST_IMAGE=crust-syzygy
    OUB_RUST_IMAGE_ID=$(docker image inspect crust-syzygy --format '{{.Id}}'); export OUB_RUST_IMAGE_ID
    run_in crust-syzygy python3 "harness/oracle_ub_${SYS}.py" --phase rust --build "/oub/$SYS" "$@"
    run_in crust-heldout python3 "harness/oracle_ub_${SYS}.py" --phase c --build "/oub/$SYS" "$@" ;;
  flourine_current)
    export OUB_RUST_IMAGE=crust-syzygy
    OUB_RUST_IMAGE_ID=$(docker image inspect crust-syzygy --format '{{.Id}}'); export OUB_RUST_IMAGE_ID
    run_in crust-syzygy python3 harness/oracle_ub_flourine_current.py --phase rust --jobs "$JOBS" --build /oub/flourine_current "$@"
    run_in crust-heldout python3 harness/oracle_ub_flourine_current.py --phase c --jobs "$JOBS" --build /oub/flourine_current "$@" ;;
  pending)
    # inputs staged in the shared build dir: a clean copy of the Mac artifacts/syzygy_zopfli at
    # $SHARED/syzygy_src, and the head-5 capture blobs at $SHARED/head5_blobs (both digest-checked)
    export OUB_RUST_IMAGE=crust-syzygy
    OUB_RUST_IMAGE_ID=$(docker image inspect crust-syzygy --format '{{.Id}}'); export OUB_RUST_IMAGE_ID
    run_in crust-syzygy python3 harness/oracle_ub_pending.py --phase rust --build /oub/pending "$@"
    run_in crust-heldout python3 harness/oracle_ub_pending.py --phase c --build /oub/pending "$@" ;;
  *) echo "unknown system $SYS"; exit 2 ;;
esac
