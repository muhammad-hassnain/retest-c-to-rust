#!/usr/bin/env bash
# AdaTrans profile check driver (Linux x86-64 box): one container per counted crash defect, all three
# in parallel, then one merge into $OUT/adatrans.json (input to systems/profile_check/assemble.py).
#
#   bash systems/profile_check/adatrans_profile.sh REPO_COPY PIPE_DIR FUZZ_DIR WORKDIR
#     REPO_COPY  a copy of this repo (needs systems/adatrans/run_diff.py, harness/oracle_ub_common.py, this dir)
#     PIPE_DIR   ADATRANS_WORK of the AdaTrans pipeline run (build/ and inputs/; mounted read-only)
#     FUZZ_DIR   the Zenodo fuzz bundle (dataset_fuzz_outputs; read-only)
#     WORKDIR    scratch/<inst> and out/ are created here
set -euo pipefail
REPO_COPY=$1; PIPE=$2; FUZZ=$3; W=$4
IMAGE=crust-heldout-plus186
IMAGE_ID_PIN="sha256:db473e6761446908cc684aef7d5e5ef3326b7f022f9fd0b00f3f20e7a5660503"   # = systems/adatrans/pipeline.sh
iid=$(docker image inspect "$IMAGE" --format '{{.Id}}')
[ "$iid" = "$IMAGE_ID_PIN" ] || { echo "image $IMAGE is $iid, pinned $IMAGE_ID_PIN" >&2; exit 1; }
mkdir -p "$W/out"
t0=$(date +%s)
pids=()
for inst in C_421_2 C_430_2 C_432_4; do
  rm -rf "$W/scratch/$inst"; mkdir -p "$W/scratch/$inst"
  docker run --rm --network none --cpus 4 -u "$(id -u):$(id -g)" \
    -v "$REPO_COPY:/work:ro" -v "$PIPE/build:/b:ro" -v "$PIPE/inputs:/inputs:ro" -v "$FUZZ:/fuzz:ro" \
    -v "$W/scratch/$inst:/s" -v "$W/out:/out" -w /work "$IMAGE" \
    python3 -I systems/profile_check/adatrans_profile.py run "$inst" "/out/adatrans_$inst.json" \
    > "$W/out/adatrans_$inst.log" 2>&1 &
  pids+=($!)
done
rc=0
for p in "${pids[@]}"; do wait "$p" || rc=1; done
cat "$W"/out/adatrans_C_*.log
[ "$rc" = 0 ] || { echo "a defect run failed" >&2; exit 1; }
python3 -I "$REPO_COPY/systems/profile_check/adatrans_profile.py" merge "$W/out/adatrans.json" "$W"/out/adatrans_C_*.json
echo "wall seconds: $(( $(date +%s) - t0 ))"
