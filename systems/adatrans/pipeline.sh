#!/usr/bin/env bash
# AdaTrans re-grade: one deterministic driver, from a clean checkout to results/adatrans_summary.json.
#
#   systems/adatrans/pipeline.sh [--force] <stage>... | all
#
# Stages (each skips when its output exists; --force re-runs it):
#   fetch      repo at the pinned commit (fetch.sh) + Zenodo fuzz bundle (archive sha256 and extracted-tree
#              sha256 both pinned)
#   vendor     the artifact's four crates, offline, at the versions pinned in systems/adatrans/Cargo.lock
#   extract    final Rust per instance per run (extract_final.py)
#   build      C (7 builds per program) + Rust (cargo debug, rustc 1.86.0)   (run_diff.py build)
#   gen        our inputs (gen_inputs.py; deterministic grid + op budget, no clock, no seed but k)
#   diff       C vs Rust on our inputs and the artifact's own inputs        (run_diff.py run --no-ub)
#   ub         the UB rule on every divergent case                           (run_diff.py ub)
#   analyze    per-translation aggregation                                  (analyze.py)
#   confirm    one-mechanism patches for the triaged run1 defects            (confirm.py)
#   heldout    overlap of our inputs with the artifact's check inputs        (heldout.py)
#   mutate     mutation analysis on agreeing run1 translations (optional)    (mutate.py)
#   check      determinism: gen, diff, ub, analyze re-run into *_recheck, compared by compare_runs.py
#   summarize  results/adatrans_*.json                                       (summarize.py; runs twice and compares)
#   all        every stage above in order
#
# Everything that executes third-party code (the C, the Rust, the artifact's fuzz scripts) runs in Docker
# with --network none. Environment: ADATRANS_WORK (default artifacts/adatrans_work), ADATRANS_FUZZ
# (default artifacts/adatrans_fuzz_outputs), ADATRANS_RUNS (default "run1"), ADATRANS_JOBS (default 3,
# also the --cpus limit), ADATRANS_IMAGE (default crust-heldout-plus186), CARGO_REGISTRY (a cargo
# registry cache holding the pinned crate versions; default ~/.cargo/registry), ADATRANS_GIT_REV.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SYS="$REPO/systems/adatrans"
WORK="${ADATRANS_WORK:-$REPO/artifacts/adatrans_work}"
FUZZ="${ADATRANS_FUZZ:-$REPO/artifacts/adatrans_fuzz_outputs}"
RUNS="${ADATRANS_RUNS:-run1}"
JOBS="${ADATRANS_JOBS:-3}"
IMAGE="${ADATRANS_IMAGE:-crust-heldout-plus186}"
IMAGE_ID_PIN="sha256:db473e6761446908cc684aef7d5e5ef3326b7f022f9fd0b00f3f20e7a5660503"
CARGO_REGISTRY="${CARGO_REGISTRY:-$HOME/.cargo/registry}"
TC="1.86.0"
ZENODO_URL="https://zenodo.org/api/records/22258998/files/adatrans-artifacts.tar.zst/content"
ZENODO_SHA256="03110aba3b469b37c56daf931dd6bb0e2ead37ac8462900da39c5e795493d6fe"
FUZZ_TREE_SHA256="8203f3ffbdad7b46f3126fb3ea13e6ca0d3da7d6d530e8f813cc625a637170c9"   # 208 files
export ADATRANS_GIT_REV="${ADATRANS_GIT_REV:-$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo '?')}"

FORCE=0
STAGES=()
for a in "$@"; do
  case "$a" in
    --force) FORCE=1 ;;
    all) STAGES+=(fetch vendor extract build gen diff ub analyze confirm heldout mutate check summarize) ;;
    *) STAGES+=("$a") ;;
  esac
done
[ ${#STAGES[@]} -gt 0 ] || { sed -n 2,30p "${BASH_SOURCE[0]}"; exit 2; }
mkdir -p "$WORK/build" "$WORK/inputs" "$REPO/results"
log() { echo "[pipeline $(date -u +%H:%M:%S)] $*" >&2; }
skip() {  # skip <output>...: true when every output exists and --force was not given
  [ "$FORCE" = 1 ] && return 1
  for f in "$@"; do [ -e "$f" ] || return 1; done
  log "skip (exists): $*"; return 0
}
tree_sha() { (cd "$1" && find . -type f | LC_ALL=C sort | xargs sha256sum | sha256sum | cut -d' ' -f1); }

dk() {  # run a command in the pinned image
  local iid; iid=$(docker image inspect "$IMAGE" --format '{{.Id}}')
  [ "$iid" = "$IMAGE_ID_PIN" ] || log "WARNING: image $IMAGE is $iid, pinned $IMAGE_ID_PIN"
  docker run --rm --network none --hostname adatrans-pipeline --cpus "$JOBS" -u "$(id -u):$(id -g)" \
    -e CARGO_HOME=/b/cargo_home -e ADATRANS_GIT_REV -e ADATRANS_IMAGE_ID="$iid" -e ADATRANS_JOBS="$JOBS" \
    -v "$REPO:/work:ro" -v "$REPO/results:/work/results" -v "$WORK/build:/b" -v "$WORK/inputs:/inputs" \
    -v "$FUZZ:/fuzz:ro" -w /work "$IMAGE" "$@"
}

st_fetch() {
  if ! skip "$REPO/artifacts/adatrans/.git"; then bash "$SYS/fetch.sh"; fi
  bash "$SYS/fetch.sh" >/dev/null   # always re-verifies the pinned commit
  if [ -d "$FUZZ" ] && [ "$FORCE" = 0 ]; then
    [ "$(tree_sha "$FUZZ")" = "$FUZZ_TREE_SHA256" ] || { log "ERROR: $FUZZ tree digest mismatch"; exit 1; }
    log "fuzz bundle present, tree sha256 verified"
  else
    local tmp; tmp=$(mktemp -d "$REPO/artifacts/.zenodo.XXXX")
    log "downloading Zenodo bundle (744 MB) -> extracting dataset_fuzz_outputs only"
    curl -sSL --retry 3 "$ZENODO_URL" | tee >(sha256sum | cut -d' ' -f1 > "$tmp/archive.sha256") \
      | zstd -dc | tar -xf - -C "$tmp" --wildcards '*dataset_fuzz_outputs*'
    sleep 1
    [ "$(cat "$tmp/archive.sha256")" = "$ZENODO_SHA256" ] || { log "ERROR: archive sha256 mismatch"; exit 1; }
    rm -rf "$FUZZ"; mv "$tmp/adatrans-artifacts/dataset_fuzz_outputs" "$FUZZ"; rm -rf "$tmp"
    [ "$(tree_sha "$FUZZ")" = "$FUZZ_TREE_SHA256" ] || { log "ERROR: extracted tree digest mismatch"; exit 1; }
  fi
}

st_vendor() {
  skip "$WORK/build/vendor/.ok" && return 0
  mkdir -p "$WORK/build/vendor_tpl/src" "$WORK/build/cargo_home/registry"
  cp "$SYS/Cargo.lock" "$WORK/build/vendor_tpl/Cargo.lock"
  printf '[package]\nname = "solution"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\nlibc = "0.2"\nrand = "0.8"\nregex = "1"\nlazy_static = "1.4"\n' > "$WORK/build/vendor_tpl/Cargo.toml"
  echo 'fn main() {}' > "$WORK/build/vendor_tpl/src/main.rs"
  local iid; iid=$(docker image inspect "$IMAGE" --format '{{.Id}}')
  docker run --rm --network none --cpus 1 -u "$(id -u):$(id -g)" \
    -v "$CARGO_REGISTRY/index:/ro_index:ro" -v "$CARGO_REGISTRY/cache:/ro_cache:ro" -v "$WORK/build:/b" "$IMAGE" bash -c '
      set -e; export CARGO_HOME=/b/cargo_vendor_home; mkdir -p $CARGO_HOME/registry
      ln -sfn /ro_index $CARGO_HOME/registry/index; ln -sfn /ro_cache $CARGO_HOME/registry/cache
      cd /b/vendor_tpl && rm -rf /b/vendor && cargo +1.86.0 vendor --locked --offline /b/vendor >/dev/null'
  cmp "$SYS/Cargo.lock" "$WORK/build/vendor_tpl/Cargo.lock" || { log "ERROR: Cargo.lock changed"; exit 1; }
  printf '[source.crates-io]\nreplace-with = "vendored-sources"\n\n[source.vendored-sources]\ndirectory = "/b/vendor"\n\n[net]\noffline = true\n' > "$WORK/build/cargo_home/config.toml"
  touch "$WORK/build/vendor/.ok"
}

st_extract() { skip "$WORK/build/extracted/manifest.json" || dk python3 -I systems/adatrans/extract_final.py --out /b/extracted; }
st_build() { skip "$WORK/build/build_$TC.json" || dk python3 -I systems/adatrans/run_diff.py build --runs run1 run2 run3; }
st_gen() { skip "$WORK/inputs/inputs_manifest.json" || dk python3 -I systems/adatrans/gen_inputs.py --out /inputs --jobs "$JOBS"; }

st_diff() {  # $1 = tag ("" or _recheck)
  for r in $RUNS; do
    skip "$WORK/build/cases_${r}${1:-}.jsonl" || dk python3 -I systems/adatrans/run_diff.py run --runs "$r" --no-ub --tag "${1:-}" --jobs "$JOBS"
  done
}
st_ub() {
  for r in $RUNS; do
    skip "$WORK/build/cases_${r}${1:-}_ub.jsonl" || dk python3 -I systems/adatrans/run_diff.py ub --runs "$r" --tag "${1:-}" --jobs "$JOBS"
  done
}
st_analyze() {
  for r in $RUNS; do
    if ! skip "$WORK/build/diff_${r}${1:-}.json"; then
      dk python3 -I systems/adatrans/analyze.py --cases "/b/cases_${r}${1:-}_ub.jsonl" --manifest /b/extracted/manifest.json \
         --run "$r" --out "/b/diff_${r}${1:-}.json" > "$WORK/build/analyze_${r}${1:-}.log"
      python3 -I -c 'import json,sys; json.dump(json.load(open(sys.argv[1]))["agreeing_on_all_ours"], open(sys.argv[2], "w"))' \
         "$WORK/build/diff_${r}${1:-}.json" "$WORK/build/agreeing_${r}${1:-}.json"
    fi
  done
}
st_confirm() { skip "$WORK/build/confirm_run1.json" || dk python3 -I systems/adatrans/confirm.py --run run1 --defects systems/adatrans/patches_run1.json --cases /b/cases_run1_ub.jsonl --jobs "$JOBS"; }
st_heldout() { skip "$WORK/build/heldout.json" || dk python3 -I systems/adatrans/heldout.py --out /b/heldout.json; }
st_mutate() { skip "$WORK/build/mutation_run1.json" || dk python3 -I systems/adatrans/mutate.py --run run1 --agreeing /b/agreeing_run1.json --jobs "$JOBS"; }

st_check() {  # determinism: regenerate inputs and re-run diff/ub/analyze for run1, compare byte for byte
  skip "$WORK/build/determinism.json" && return 0
  local R="$RUNS"; RUNS="run1"
  mkdir -p "$WORK/inputs_recheck"
  if ! skip "$WORK/inputs_recheck/inputs_manifest.json"; then
    docker run --rm --network none --cpus "$JOBS" -u "$(id -u):$(id -g)" -v "$REPO:/work:ro" -v "$WORK/inputs_recheck:/out" \
      -w /work "$IMAGE" python3 -I systems/adatrans/gen_inputs.py --out /out --jobs "$JOBS"
  fi
  st_diff _recheck; st_ub _recheck; st_analyze _recheck
  RUNS="$R"
  python3 -I "$SYS/compare_runs.py" "$WORK"
}

st_summarize() {
  skip "$REPO/results/adatrans_summary.json" && return 0
  dk python3 -I systems/adatrans/summarize.py --work /b --inputs-manifest /inputs/inputs_manifest.json --results /work/results --runs $RUNS
}

for s in "${STAGES[@]}"; do
  log "stage $s"
  "st_$s"
done
log "done: ${STAGES[*]}"
