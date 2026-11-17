#!/usr/bin/env bash
# Full SACTOR re-grade, in order, on the x86-64 box (all third-party code inside Docker):
#   bash systems/sactor/fetch.sh                       # on the Mac: pinned clones into artifacts/sactor
#   copy this repository (at least harness/ and systems/sactor/) to the box
#   ssh <box> 'cd ~/work/regrade-sactor/repo && bash systems/sactor/run_all.sh'
# then copy ~/work/regrade-sactor/repo/results/sactor_*.json back to results/ and run
#   python3 systems/sactor/build_summary.py
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
export SACTOR_GIT_REV="${SACTOR_GIT_REV:-$(cat GIT_REV 2>/dev/null || echo '?')}"
R=systems/sactor/run_docker.sh
bash $R python3 systems/sactor/libogg/run.py --out /work/results/sactor_libogg.json
bash $R python3 systems/sactor/libogg/heldout.py --out /work/results/sactor_libogg_heldout.json
bash $R python3 systems/sactor/crust/run.py --out /work/results/sactor_crust_owntests.json
bash $R python3 systems/sactor/crust/grid.py --out /work/results/sactor_crust_grid.json
bash $R python3 systems/sactor/libogg/mutate.py --n 40 --out /work/results/sactor_libogg_mutation.json
bash $R python3 systems/sactor/crust/mutate.py --samples "${CRUST_MUT_SAMPLES:-morton,NandC,murmurhash_c,utf8,geofence}" --n 12 \
  --out /work/results/sactor_crust_mutation.json
# completeness record of the 18 partial CRUST-Bench sets (no Rust is built for them)
bash $R python3 systems/sactor/crust/run.py --out /work/results/sactor_crust_partial.json \
  --only 42-Kocaeli-Printf,Phills_DHT,SimpleXML,aes128-SIMD,amp,approxidate,bhshell,bostree,cfsm,clog,csyncmers,dict,emlang,expr,inversion_list,jccc,razz_simulation,totp
# if the host OOM-kills rustc during mutation (seen at load ~77), re-run only the build_fail mutants:
#   bash $R python3 systems/sactor/libogg/mutate.py --n 40 --retry-build-fail --out /work/results/sactor_libogg_mutation.json
