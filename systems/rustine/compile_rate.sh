#!/usr/bin/env bash
# Measure how many of Rustine's AUTO translations (translate/) compile, vs the
# human-finished versions (manual_debug/). Rustine's auto output is its tool's raw
# result; manual_debug is after a person fixed it. The gap is a clean, honest metric.
# Run in the stable image:
#   docker run --rm -v "$PWD:/work" -w /work crust-stable systems/rustine/compile_rate.sh
set -uo pipefail
ART=/work/artifacts/rustine/artifacts
auto_ok=0; auto_fail=0; man_ok=0; man_fail=0
echo "prog                      auto(translate)  human(manual_debug)"
for d in "$ART"/*/; do
  prog=$(basename "$d")
  a="skip"; m="skip"
  if [ -f "$d/translate/Cargo.toml" ]; then
    if ( cd "$d/translate" && cargo build --bins >/tmp/a.log 2>&1 ); then a="BUILD"; auto_ok=$((auto_ok+1)); else a="FAIL"; auto_fail=$((auto_fail+1)); fi
  fi
  if [ -f "$d/manual_debug/Cargo.toml" ]; then
    if ( cd "$d/manual_debug" && cargo build --bins >/tmp/m.log 2>&1 ); then m="BUILD"; man_ok=$((man_ok+1)); else m="FAIL"; man_fail=$((man_fail+1)); fi
  fi
  printf "%-25s %-16s %s\n" "$prog" "$a" "$m"
done
echo ""
echo "AUTO translate/ : $auto_ok build, $auto_fail fail"
echo "HUMAN manual_debug/ : $man_ok build, $man_fail fail"
