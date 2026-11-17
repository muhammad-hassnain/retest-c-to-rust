#!/usr/bin/env bash
# For each Rustine auto-translation (translate/) that COMPILES, build its bins and run each
# with empty stdin + a timeout, recording exit codes. A Rust panic (exit 101) means the
# translation compiles but crashes at runtime -- i.e. "compiles" overstates "works". Many of
# these programs have a built-in self-test/example bin that runs deterministically with no input.
# Run: docker run --rm -v "$PWD:/work" -w /work crust-stable systems/rustine/runtime_check.sh
set -uo pipefail
ART=/work/artifacts/rustine/artifacts
progs_run=0; progs_panic=0; progs_clean=0; progs_needinput=0
echo "prog                      bins  result"
for d in "$ART"/*/; do
  prog=$(basename "$d")
  [ -f "$d/translate/Cargo.toml" ] || continue
  ( cd "$d/translate" && cargo build --bins >/dev/null 2>&1 ) || continue   # only the ones that compile
  bins=$(ls "$d/translate/target/debug/" 2>/dev/null | grep -vE '\.|^build$|^deps$|^incremental$|^examples$' )
  [ -z "$bins" ] && continue
  progs_run=$((progs_run+1))
  panic=0; clean=0; timeout=0; other=0
  for b in $bins; do
    bp="$d/translate/target/debug/$b"
    [ -x "$bp" ] || continue
    timeout 5 "$bp" </dev/null >/dev/null 2>&1; rc=$?
    if [ $rc -eq 101 ]; then panic=$((panic+1));
    elif [ $rc -eq 0 ]; then clean=$((clean+1));
    elif [ $rc -eq 124 ]; then timeout=$((timeout+1));
    else other=$((other+1)); fi
  done
  res="clean=$clean panic=$panic timeout=$timeout other=$other"
  if [ $panic -gt 0 ]; then progs_panic=$((progs_panic+1)); tag="PANIC";
  elif [ $clean -gt 0 ] && [ $timeout -eq 0 ]; then progs_clean=$((progs_clean+1)); tag="runs";
  else progs_needinput=$((progs_needinput+1)); tag="needs-input?"; fi
  printf "%-25s %-5s %-12s %s\n" "$prog" "$(echo $bins|wc -w)" "$tag" "$res"
done
echo ""
echo "compiled programs run: $progs_run | at least one PANIC: $progs_panic | ran clean: $progs_clean | input/other: $progs_needinput"
