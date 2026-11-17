#!/usr/bin/env bash
# Strengthened held-out coverage measurement: richer deterministic grid (v4 edge-case
# corpus + more numeric boundaries + flag triples + a C.UTF-8 locale axis), then
# re-measure the ROR mutation kill rate to see whether the stronger generator detects
# more injected faults than the v3 grid did.
#
#   docker run --rm -v "$PWD:/work" -w /work crust-heldout ./harness/measure_coverage.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
export C2SR_ROOT="${C2SR_ROOT:-${ROOT}/artifacts/c2saferrust}"
RESULTS="${ROOT}/results"; mkdir -p "${RESULTS}"
cd "${HERE}"

UTILS="${UTILS:-cat head tail uniq}"
DIFF_CAP="${DIFF_CAP:-3000}"
MUT_CAP="${MUT_CAP:-500}"
MAX_MUT="${MAX_MUT:-15}"

echo "grid=v4  diff_cap=${DIFF_CAP}  mut_cap=${MUT_CAP}  max_mutants=${MAX_MUT}"
tot_killed=0; tot_compiled=0
for util in ${UTILS}; do
  echo "### ${util} ###"
  python3 gen_inputs.py --util "${util}" -n "${DIFF_CAP}" -o "${RESULTS}/${util}_cases_v4.json"
  python3 difftest.py --cases "${RESULTS}/${util}_cases_v4.json" --out "${RESULTS}/${util}_diff_v4.json"
  python3 mutate.py --util "${util}" -n "${MUT_CAP}" --max-mutants "${MAX_MUT}" \
      --out "${RESULTS}/${util}_mut_v4.json"
done

echo ""
echo "============ STRENGTHENED-GRID SUMMARY ============"
printf "%-8s %-14s %-10s %-s\n" "util" "stdout/exit" "stderr" "ror killed/compiled"
for util in ${UTILS}; do
  python3 - "$util" "${RESULTS}/${util}_diff_v4.json" "${RESULTS}/${util}_mut_v4.json" <<'PY'
import json, sys
util, dj, mj = sys.argv[1:4]
d = json.load(open(dj))["summary"]
m = json.load(open(mj))
rs = m["ror_summary"]
# count stdout/exit vs stderr divergences from the per-case records if present
print(f"{util:<8} {str(d['mismatches'])+' mism':<14} "
      f"{str(d.get('stderr_divergences','?')):<10} "
      f"{rs['killed']}/{rs['compiled']}  (sanity {'OK' if m['sanity_ok'] else 'FAIL'})")
PY
done
