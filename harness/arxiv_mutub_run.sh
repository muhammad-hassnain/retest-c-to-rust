#!/usr/bin/env bash
# Driver for the arXiv re-run of the mutation kills under the UB rule on the x86_64 box.
# Everything is written under ARXIV_W (default ~/work/arxiv-mutub); the original work dirs are only read.
#
# Layout:
#   $ARXIV_W/repo        rsync of the study repo working tree (artifacts/, out/, .git excluded); mounted read-only
#   $ARXIV_W/c2sr        trees/, vendor/, cargo_home/config.toml copied from ~/work/cli-mutation;
#                        build{,_rust_WIP}/{prepare,sites,baseline_*}.json copied from the original run; out/
#   $ARXIV_W/flourine    flourine-artifact/ (rsync of the Mac artifacts/flourine_dl/flourine-artifact), out/
#   $ARXIV_W/actor       build/ and out/*.json copied from ~/work/actor-mut; scripts/ = systems/actoruw/*.py +
#                        systems/enumerate_inputs.py +
#                        harness/oracle_ub_common.py; artifact/ mounted read-only from ~/work/actor-mut/artifact
#   $ARXIV_W/sactor      build/crust/<sample>/grid copied from ~/work/regrade-sactor; out/
#
#   bash harness/arxiv_mutub_run.sh c2sr <rust|rust_WIP>
#   bash harness/arxiv_mutub_run.sh flourine_c        # C side: full UB rule on every grid input (crust-heldout)
#   bash harness/arxiv_mutub_run.sh flourine_m        # mutants rebuilt and re-run (audit3-stable186, rustc 1.86.0;
#                                                     # build it with docker/Dockerfile.audit3-stable186)
#   ARXIV_W=~/work/arxiv-flmut JOBS=10 DOCKER_MEMORY=16g bash harness/arxiv_mutub_run.sh flourine_m --profile fuzz \
#     --cap-output-bytes 16777216 --release-ubfilter ... --out ...   # FLOURINE's fuzz profile
#   bash harness/arxiv_mutub_run.sh actor
#   bash harness/arxiv_mutub_run.sh sactor_grid
set -euo pipefail
W="${ARXIV_W:-$HOME/work/arxiv-mutub}"
JOBS="${JOBS:-12}"
export CRUST_ARTIFACT_COMMIT="${CRUST_ARTIFACT_COMMIT:-unknown}"
iid() { docker image inspect "$1" --format '{{.Id}}'; }
step="$1"; shift || true
case "$step" in
  c2sr)
    V="${1:-rust}"; IMG=crust-heldout
    docker run --rm --network none --cpus "$JOBS" -u "$(id -u):$(id -g)" \
      -e HOME=/tmp -e CARGO_HOME=/w/cargo_home -e MFULL_WORK=/w -e RUSTUP_TOOLCHAIN=nightly-2022-08-08 \
      -e MFULL_VARIANT="$V" -e CRUST_ARTIFACT_COMMIT -e ARXIV_IMAGE="$IMG" -e ARXIV_IMAGE_ID="$(iid $IMG)" \
      -e ARXIV_COMMAND="JOBS=$JOBS bash harness/arxiv_mutub_run.sh c2sr $V" \
      -v "$W/c2sr:/w" -v "$W/repo:/w/repo:ro" -w /w/repo "$IMG" \
      python3 -I harness/arxiv_mutate_full_ubfilter.py --jobs "$JOBS" --controls --out "/w/out/c2sr_${V}.json" ;;
  flourine_c|flourine_m)
    if [ "$step" = flourine_c ]; then IMG=crust-heldout; PH=c; else IMG=audit3-stable186; PH=m; fi
    # optional: DOCKER_MEMORY (e.g. 16g) adds --memory; extra arguments go to the script (e.g. --profile fuzz)
    docker run --rm --network none --cpus "$JOBS" ${DOCKER_MEMORY:+--memory "$DOCKER_MEMORY"} \
      -u "$(id -u):$(id -g)" --ulimit core=0 \
      -e HOME=/tmp -e MFULL_WORK=/tmp/mf -e FLOURINE_ROOT=/fl -e CRUST_ARTIFACT_COMMIT -e ARXIV_FL_DIGEST \
      -e ARXIV_IMAGE="$IMG" -e ARXIV_IMAGE_ID="$(iid $IMG)" \
      -e ARXIV_COMMAND="JOBS=$JOBS${DOCKER_MEMORY:+ DOCKER_MEMORY=$DOCKER_MEMORY} bash harness/arxiv_mutub_run.sh $step${*:+ $*}" \
      -v "$W/flourine/flourine-artifact:/fl:ro" -v "$W/flourine:/w" -v "$W/repo:/w/repo:ro" -w /w/repo "$IMG" \
      python3 -I harness/arxiv_mutate_full_flourine_ubfilter.py --phase "$PH" --jobs "$JOBS" \
      --work /w/work --out /w/out/flourine.json "$@" ;;
  actor)
    IMG=crust-heldout; TC="nightly-2026-01-10-x86_64-unknown-linux-gnu"
    mkdir -p "$W/actor/cargo_home/registry/src" "$W/actor/artifact"
    [ -L "$W/actor/cargo_home/registry/index" ] || ln -s /cargo_ro/index "$W/actor/cargo_home/registry/index"
    [ -L "$W/actor/cargo_home/registry/cache" ] || ln -s /cargo_ro/cache "$W/actor/cargo_home/registry/cache"
    docker run --rm --network none --cpus "$JOBS" --memory 20g --user "$(id -u):$(id -g)" -e HOME=/tmp \
      -e CARGO_HOME=/w/cargo_home -e CARGO_NET_OFFLINE=true \
      -e PATH=/rust/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
      -e ACTORUW_W=/w -e ACTORUW_TC="$TC" -e CRUST_ARTIFACT_COMMIT \
      -e ARXIV_IMAGE="$IMG" -e ARXIV_IMAGE_ID="$(iid $IMG)" \
      -e ARXIV_COMMAND="JOBS=$JOBS bash harness/arxiv_mutub_run.sh actor" \
      -v "$W/actor:/w" -v "$HOME/work/actor-mut/artifact:/w/artifact:ro" -v "$W/repo:/w/repo:ro" \
      -v "$HOME/.rustup/toolchains/$TC:/rust:ro" -v "$HOME/.cargo/registry:/cargo_ro:ro" \
      -w /w/scripts "$IMG" python3 -I arxiv_mutate_ubfilter.py -j "$JOBS" \
      --results /w/repo/results/actoruw_mutation.json --out /w/out/actor.json "$@" ;;
  sactor_grid)
    IMG=crust-heldout; mkdir -p "$W/sactor/build/tmp"
    docker run --rm --network none --cpus "${CPUS:-4}" --ulimit core=0 -u "$(id -u):$(id -g)" -e HOME=/tmp \
      -e CRUST_ARTIFACT_COMMIT -e ARXIV_IMAGE="$IMG" -e ARXIV_IMAGE_ID="$(iid $IMG)" \
      -e ARXIV_COMMAND="bash harness/arxiv_mutub_run.sh sactor_grid" \
      -v "$W/sactor/build:/build" -v "$W/sactor/out:/out" -v "$W/repo:/work:ro" -w /work "$IMG" \
      python3 -I systems/sactor/crust/arxiv_grid_ub_all_inputs.py --samples NandC,geofence --out /out/sactor_grid_ub.json "$@" ;;
  *) echo "unknown step $step"; exit 2 ;;
esac
