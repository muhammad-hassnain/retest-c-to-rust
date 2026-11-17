#!/usr/bin/env bash
# Re-verify EVERY system's headline result this cycle (not just VERT). Host-side: each harness
# runs in its own pinned image. Each writes results/<system>*.json IN PLACE (it updates the shipped
# results); to re-run without touching results/, use scripts/run_all.sh, which works in a scratch copy.
# (VERT + Syzygy + RustMap-Rosetta + RustAssure-composability were already re-run this cycle.)
#   bash systems/reverify_all.sh            # from anywhere; the repo root is found from this file
set -uo pipefail
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
run(){ echo ""; echo "### $1"; shift; docker run --rm -v "$R:/work" -w /work "$@" 2>&1 | tail -4; }

run "Flourine scalar"        crust-stable  python3 systems/flourine/run.py
run "Flourine struct (OPL)"  crust-stable  python3 systems/flourine/run_struct.py
run "RustAssure per-function" crust-stable python3 systems/rustassure/run.py
run "RustAssure libcsv e2e"   crust-heldout python3 systems/rustassure/libcsv_e2e/run.py
run "RustAssure u8c e2e"      crust-heldout python3 systems/rustassure/u8c_e2e/run.py
run "RustAssure composability" crust-stable python3 systems/rustassure/signature_consistency.py
run "Rustine compile-rate"    crust-stable  bash systems/rustine/compile_rate.sh
run "C2SaferRust coreutils (difftest + mutation)" -e UTILS="cat head tail uniq" -e NCASES=200 crust-heldout ./harness/runall.sh
echo ""; echo "REVERIFY_ALL_DONE"
