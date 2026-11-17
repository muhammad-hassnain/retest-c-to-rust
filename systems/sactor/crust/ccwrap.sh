#!/bin/bash
# gcc shim used to build CRUST-Bench C oracles with SACTOR's own build scripts
# (build_lib_and_tests.sh) under the oracle-UB variants of harness/oracle_ub_common.py.
# Placed first on PATH as "gcc"; CRUST_VARIANT selects the variant. Flags are APPENDED, so the
# last -O wins and the script's own flags are otherwise kept.
V="${CRUST_VARIANT:-ref}"
REAL=/usr/bin/gcc
SAN="-O0 -g -fno-omit-frame-pointer -fsanitize=address,undefined,float-cast-overflow -fno-sanitize-recover=all"
# A partial (relocatable) link (gcc -r, used by some samples to merge test objects) only merges
# already-built objects: pass it through unchanged, or the sanitizer runtime gets linked in twice.
for a in "$@"; do
  if [ "$a" = "-r" ]; then
    if [[ "$V" == san_clang* ]]; then exec clang "$@"; else exec $REAL "$@"; fi
  fi
done
args=()
for a in "$@"; do
  if [[ "$V" == san_clang* ]]; then
    case "$a" in -fPIE|-fpie|-pie) continue;; esac
  fi
  args+=("$a")
done
case "$V" in
  ref)          exec $REAL "${args[@]}" ;;
  o0)           exec $REAL "${args[@]}" -O0 ;;
  o1)           exec $REAL "${args[@]}" -O1 ;;
  uchar)        exec $REAL "${args[@]}" -funsigned-char ;;
  schar)        exec $REAL "${args[@]}" -fsigned-char ;;
  san_gcc)      exec $REAL "${args[@]}" -w $SAN ;;
  san_clang)    exec clang "${args[@]}" -w -Wno-unused-command-line-argument $SAN -fno-pie -no-pie ;;
  san_clang_nz) exec clang "${args[@]}" -w -Wno-unused-command-line-argument $SAN -fsanitize-recover=pointer-overflow -fno-pie -no-pie ;;
  *) echo "ccwrap: unknown variant $V" >&2; exit 2 ;;
esac
