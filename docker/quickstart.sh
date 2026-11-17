#!/usr/bin/env bash
# Reviewer quick start: re-check the paper's numbers from the shipped results, offline.
#
#   docker run --rm crust-artifact                       # inside the reviewer image (this is its CMD)
#   docker run --rm -v "$PWD/out:/out" crust-artifact    # keep the outputs
#   bash docker/quickstart.sh                            # from a checkout (needs python3; matplotlib for figures)
#
# 1. pipeline verify: re-runs the cheap stages of pipeline.json (tables, figures, code listings,
#    second-annotator agreement, and the defect ledger when artifacts/ is present) and compares each
#    output with the shipped file after normalisation. Stages that need missing artifacts, or a Python
#    module python3 cannot import (matplotlib for the figures, Pygments for the code listings), are skipped.
# 2. the paper's Figs. 1 to 3 (and two extra figures) redrawn from results/ (the plotted numbers are printed).
# 3. the defect explorer (an offline HTML page over every candidate defect) rebuilt from results/.
# 4. the artifact dashboard (headline numbers, per-system results, where things are) rebuilt from results/.
# The shipped results/, explorer and dashboard are never modified; outputs go to $OUT.
set -uo pipefail
R="${CRUST_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$R"
if [ -z "${OUT:-}" ]; then
  if [ -d /out ] && [ -w /out ]; then OUT=/out; else OUT="$R/out"; fi
fi
mkdir -p "$OUT"
rc=0
say(){ printf '\n==== %s\n' "$*"; }

say "environment"
python3 -I scripts/provenance.py --stamp --image "${CRUST_IMAGE:-host}" --step quickstart | tee "$OUT/provenance.json" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("commit", d["git_commit"], "| arch", d["arch"], "|", d["gcc"], "|", d["rustc"], "| python", d["python"])'

say "1. pipeline verify (cheap stages re-run and compared with the shipped files)"
python3 scripts/pipeline.py verify --out "$OUT/pipeline" > "$OUT/verify.txt" 2>&1 || rc=1
grep -vE '^MATCH' "$OUT/verify.txt" || true

say "2. figures from results/"
if python3 -c 'import matplotlib' 2>/dev/null; then
  python3 scripts/plot_results.py --out "$OUT/figures" || rc=1
  python3 scripts/plot_overlap.py --out "$OUT/figures" || rc=1
else
  echo "matplotlib not installed: skipped"
fi

say "3. explorer from results/"
python3 -I scripts/build_explorer.py --out "$OUT/explorer/index.html" || rc=1

say "4. dashboard from results/"
python3 -I scripts/build_dashboard.py --out "$OUT/dashboard.html" || rc=1

say "summary"
echo "outputs in $OUT (shipped results/ untouched)"
[ "$rc" = 0 ] && echo "QUICKSTART OK: every re-run stage matches the shipped files" \
              || echo "QUICKSTART: something failed or differs (see above)"
exit "$rc"
