#!/usr/bin/env bash
# Second half of the final run (after run_all.sh's first four steps), with the corrected combiner
# (sactor_common.assemble: flattened `use` paths and dedup_items, as SACTOR's Combiner does):
# the three CRUST-Bench samples whose complete unidiomatic set ships without combined.rs, the
# libogg run (gpt-5 bitwise.c is such a set), then both mutation analyses.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
export SACTOR_GIT_REV="${SACTOR_GIT_REV:-$(cat GIT_REV 2>/dev/null || echo '?')}"
R=systems/sactor/run_docker.sh
bash $R python3 systems/sactor/crust/run.py --only amp,cfsm,inversion_list --out /work/results/sactor_crust_owntests_assembled.json
bash $R python3 systems/sactor/crust/grid.py --only amp,cfsm,inversion_list --out /work/results/sactor_crust_grid_assembled.json
bash $R python3 systems/sactor/libogg/run.py --out /work/results/sactor_libogg.json
bash $R python3 systems/sactor/libogg/heldout.py --out /work/results/sactor_libogg_heldout.json
bash $R python3 systems/sactor/libogg/mutate.py --n 40 --out /work/results/sactor_libogg_mutation.json
bash $R python3 systems/sactor/crust/mutate.py --samples "${CRUST_MUT_SAMPLES:-morton,NandC,murmurhash_c,utf8,geofence}" --n 12 \
  --out /work/results/sactor_crust_mutation.json
