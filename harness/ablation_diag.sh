#!/usr/bin/env bash
# Diagnose WHY a C2SaferRust translation fails one of its own repair tests: build C + Rust, run the
# named test against each, and print the test's own diagnostic output for the Rust run (the coreutils
# tests print what diverged). Used to classify ablation failures as real behavioral bugs vs
# environment/flaky (timing, /proc, /sys).
#
#   docker run --rm -v "$PWD:/work" -w /work -e C2SR_ROOT=/work/artifacts/c2saferrust \
#     -e UTIL=split -e TEST=b-chunk.sh crust-heldout bash harness/ablation_diag.sh
set -uo pipefail
A="${C2SR_ROOT:-/work/artifacts/c2saferrust}/coreutils"
U="${UTIL:?set UTIL}"; T="${TEST:?set TEST}"
(cd "$A/src/$U/c" && make >/dev/null 2>&1) || { echo "C build failed"; exit 2; }
(cd "$A/src/$U/${C2SR_VARIANT:-rust_WIP}" && cargo build --bin "$U" >/dev/null 2>&1) || { echo "Rust build failed"; exit 2; }
CD=/tmp/diag_c_$U; RD=/tmp/diag_r_$U; rm -rf "$CD" "$RD"; mkdir -p "$CD" "$RD"
cp "$A/src/$U/c/$U" "$CD/$U"; cp "$A/src/$U/${C2SR_VARIANT:-rust_WIP}/target/debug/$U" "$RD/$U"
cd "$A/tests/$U"
bash "$T" "$CD" >/tmp/diag_c.out 2>&1; cr=$?
bash "$T" "$RD" >/tmp/diag_r.out 2>&1; rr=$?
echo "TEST=$U/$T   C rc=$cr   RUST rc=$rr"
echo "----- Rust run output (tail) -----"
tail -20 /tmp/diag_r.out
