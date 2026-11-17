#!/usr/bin/env bash
# End-to-end: for each coreutils util, generate held-out inputs, run the
# original-C-vs-translated-Rust differential test, and run the harness
# mutation-kill sanity check. Meant to run inside the pinned Linux container.
#
#   docker run --rm -v "$PWD:/work" -w /work crust-heldout ./harness/runall.sh
#
# Env:
#   UTILS   space-separated list (default: pwd cat head tail uniq)
#   NCASES  held-out cases per util for the diff pass (default: 200)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
export C2SR_ROOT="${C2SR_ROOT:-${ROOT}/artifacts/c2saferrust}"
RESULTS="${ROOT}/results"
mkdir -p "${RESULTS}"

UTILS="${UTILS:-pwd cat head tail uniq}"
NCASES="${NCASES:-200}"
cd "${HERE}"

if [ ! -d "${C2SR_ROOT}/coreutils/src" ]; then
  echo "ERROR: artifact not found at ${C2SR_ROOT}"
  echo "Run ./scripts/fetch_artifact.sh first."
  exit 2
fi

echo "=================================================================="
echo " held-out differential testing  (C2SaferRust @ pinned commit)"
echo " utils: ${UTILS}   cases/util: ${NCASES}"
echo "=================================================================="

summary_rows=()
for util in ${UTILS}; do
  echo ""
  echo "### ${util} ########################################################"
  python3 gen_inputs.py --util "${util}" -n "${NCASES}" \
      -o "${RESULTS}/${util}_cases.json"

  echo "--- held-out differential test ---"
  python3 difftest.py --cases "${RESULTS}/${util}_cases.json" \
      --out "${RESULTS}/${util}_diff.json"
  diff_rc=$?

  echo "--- harness mutation-kill sanity check ---"
  python3 mutate.py --util "${util}" -n 60 \
      --out "${RESULTS}/${util}_mut.json"
  mut_rc=$?

  summary_rows+=("${util}|${diff_rc}|${mut_rc}")
done

echo ""
echo "=================================================================="
echo " SUMMARY"
echo "=================================================================="
printf "%-8s %-28s %-10s %-s\n" "util" "held-out (pass/assessable)" "mism" "harness-red?"
for util in ${UTILS}; do
  dj="${RESULTS}/${util}_diff.json"
  mj="${RESULTS}/${util}_mut.json"
  python3 - "$util" "$dj" "$mj" <<'PY'
import json, sys
util, dj, mj = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    d = json.load(open(dj))["summary"]
    agree, assess, mism = d["agree"], d["assessable"], d["mismatches"]
    rate = f"{agree}/{assess}"
except Exception as e:
    rate, mism = f"ERR({e})", "?"
try:
    m = json.load(open(mj))
    red = "YES" if m.get("sanity_ok") else "NO(!!)"
    red += f" ror {m['ror_summary']['killed']}/{m['ror_summary']['compiled']}"
except Exception as e:
    red = f"ERR({e})"
print(f"{util:<8} {str(rate):<28} {str(mism):<10} {red}")
PY
done
echo ""
echo "Full results in ${RESULTS}/*.json"
