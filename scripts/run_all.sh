#!/usr/bin/env bash
# Re-run every experiment behind a number in the paper, WITHOUT touching the shipped results/.
# Thin wrapper over scripts/pipeline.py (stages, images, architectures and commands are in pipeline.json).
#
# All stages run in a scratch copy of this repository, $OUT/work, whose results/ starts as a copy of the
# shipped results/; a stage that writes results/<x>.json overwrites only the copy, and later stages read the
# fresh files. When the stages are done, the figures are redrawn from them ($OUT/stage_figures/). Every output a stage wrote is copied to $OUT/outputs/ and marked
# equal/different from the shipped file after normalisation (provenance, timestamps and durations dropped).
#
#   bash scripts/run_all.sh --list                      # every stage: image, arch the paper's run used, outputs
#   bash scripts/run_all.sh                             # every paper stage this host can run (hours; REPRODUCE.md)
#   bash scripts/run_all.sh vert flourine               # whole systems (names as in `--list`)
#   bash scripts/run_all.sh vert.diff syzygy.empty_deflate
#   STEPS="syzygy_empty vert_scalar" bash scripts/run_all.sh     # old step names still work
#   OUT=/tmp/rerun DOCKER_CPUS=4 bash scripts/run_all.sh syzygy
#   bash scripts/run_all.sh --in-place syzygy.empty_deflate      # overwrite the shipped results/ (not the default)
#   bash scripts/run_all.sh --verify                    # cheap deterministic stages only, vs shipped hashes
#   bash scripts/run_all.sh --spotcheck vert.diff       # small fixed subset, per-case verdicts vs the shipped rows
#
# Runner: on a host, each docker stage runs in its pinned image (docker/Dockerfile*; build them with
# scripts/provision.sh images, plus docker build -t crust-kani -f docker/Dockerfile.kani docker/). Inside the
# reviewer image crust-artifact (or RUNNER=local) stages run directly with the toolchain the stage's image pins.
# Third-party artifacts are read from artifacts/ (fetch: scripts/provision.sh artifacts). A stage whose artifact is
# missing, which needs another architecture (EMULATE=1 tries Docker emulation), a network (NETWORK=1 allows it) or
# an unknown command is skipped and says why. Further options: python3 scripts/pipeline.py run --help.
set -uo pipefail
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
P="python3 $R/scripts/pipeline.py"

# old step names (this script's earlier step table) -> stage ids
map_step(){ case "$1" in
  vert_scalar) echo vert.diff;; vert_legacy_grid) echo vert.diff_legacy_grid;; vert_grid_compare) echo vert.grid_compare;;
  vert_arrays) echo vert.arrays;; vert_x86_spread) echo vert.diff_x86_spread;; vert_x86_legacy) echo vert.diff_x86_legacy;;
  vert_oracle) echo vert.oracle_check;; vert_kani) echo vert.kani_vacuity;; vert_kani_unwind_on) echo vert.kani_unwinding_on;;
  vert_bolero) echo vert.bolero_vacuity;; vert_mutation) echo vert.mutation_legacy;;
  flourine_scalar) echo flourine.diff;; flourine_struct) echo flourine.diff_struct;;
  rustassure_scalar) echo rustassure.diff;; rustassure_libcsv) echo rustassure.libcsv_e2e;; rustassure_u8c) echo rustassure.u8c_e2e;;
  rustassure_compose) echo rustassure.composability;;
  c2sr_v4) echo c2saferrust.diff;; c2sr_baseline) echo c2saferrust.diff_c2rust_baseline;; c2sr_acceptance) echo c2saferrust.acceptance;;
  c2sr_ablation) echo c2saferrust.ablation;;
  syzygy_empty) echo syzygy.empty_deflate;; rustmap_bzip2) echo rustmap.bzip2_check;; rustmap_rosetta) echo rustmap.rosetta;;
  rustine_build_186) echo rustine.build_186;; rustine_build_nightly) echo rustine.build_nightly;;
  *) echo "$1";; esac; }

OPTS=(); ARGS=(); MODE=run
while [ $# -gt 0 ]; do case "$1" in
  --list) exec $P list;;
  --verify) MODE=verify;;
  --spotcheck) MODE=spotcheck;;
  --in-place) OPTS+=(--in-place);;
  -h|--help) sed -n 2,31p "${BASH_SOURCE[0]}"; exit 0;;
  *) ARGS+=("$1");; esac; shift; done

OUT="${OUT:-$R/out/rerun-$(date -u +%Y%m%dT%H%M%SZ)}"
[ -n "${DOCKER_CPUS:-}" ] && OPTS+=(--cpus "$DOCKER_CPUS")
[ -n "${RUNNER:-}" ] && OPTS+=(--runner "$RUNNER")
[ -n "${EMULATE:-}" ] && OPTS+=(--emulate)
[ -n "${NETWORK:-}" ] && OPTS+=(--network)

case "$MODE" in
  verify) exec $P verify --out "$OUT";;
  spotcheck) exec $P spotcheck "${ARGS[@]}" "${OPTS[@]}" --out "$OUT";;
esac

SEL=""
for s in ${STEPS:-}; do SEL="${SEL:+$SEL,}$(map_step "$s")"; done
for a in "${ARGS[@]+"${ARGS[@]}"}"; do SEL="${SEL:+$SEL,}$(map_step "$a")"; done
if [ -z "$SEL" ]; then
  # every system, in pipeline.json order, without the cross-system ledger/figures stages that run at the end
  SEL="$($P list | awk '{print $1}' | grep -E '^[a-z0-9]+\.' | cut -d. -f1 | grep -v '^cross$' | awk '!s[$0]++' | paste -sd, -)"
fi
$P run "$SEL" --out "$OUT" ${OPTS[@]+"${OPTS[@]}"}
rc=$?
# figures redrawn from the fresh results (same scratch copy)
$P run cross.figures --out "$OUT" ${OPTS[@]+"${OPTS[@]}"} >/dev/null 2>&1 || true
$P run cross.figure_overlap --out "$OUT" ${OPTS[@]+"${OPTS[@]}"} >/dev/null 2>&1 || true
echo "ALL DONE -> $OUT (shipped results/ untouched unless --in-place); summary: $OUT/run_summary.json"
[ "$rc" = 0 ] && exit 0 || exit 1
