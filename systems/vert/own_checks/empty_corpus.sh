#!/bin/bash
# VERT's latest GitHub pipeline (generate_utils.py at 3270cec, 2025-05-20) creates
# src/__fuzz__/bolero_wasm_eq/corpus/empty before `cargo bolero reduce`. Does that replay an input that
# can expose a wrong translation? Run on VERT's MAXIMUM_GAMES_PLAYED_WINNER (wrong at n=0) and on a planted
# translation returning 12345 for every input. Same tooling as run_bolero.sh.
set -e
apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq build-essential clang curl python3 ca-certificates pkg-config libssl-dev git >/dev/null
curl -sSf https://sh.rustup.rs | sh -s -- -y -q >/dev/null
. "$HOME/.cargo/env"
rustup toolchain install nightly-2023-07-29 --profile minimal >/dev/null 2>&1
cargo install cargo-bolero --git https://github.com/YoshikiTakashima/bolero.git --rev 1d02eb853af3dad36eba5f03a9a8c41e5488cfef --locked -q 2>/dev/null
B=${VERT_ROOT:-artifacts/vert}/benchmark
for f in MAXIMUM_GAMES_PLAYED_WINNER HEXAGONAL_NUMBER; do
 for arm in vert planted; do
  d=/tmp/e/$f-$arm; rm -rf $d; mkdir -p /tmp/e; cp -r $B/c_transcoder/$f/out-rwasm-bolero $d; cp $B/rust-toolchain.toml $d/
  if [ $arm = planted ]; then python3 - "$d/src/main.rs" <<'PY'
import re,sys
p=sys.argv[1]; s=open(p).read(); a=s.index("////// LLM Output //////"); b=s.index("////// LLM Output //////",a+10)
llm=s[a:b]; m=re.search(r"fn f_gold\s*\([^)]*\)\s*->\s*i32\s*\{",llm); i=m.end(); depth=1
while depth: depth+={"{":1,"}":-1}.get(llm[i],0); i+=1
open(p,"w").write(s[:a]+llm[:m.end()]+"\n    return 12345;\n"+llm[i-1:]+s[b:])
PY
  fi
  mkdir -p $d/src/__fuzz__/bolero_wasm_eq/corpus && touch $d/src/__fuzz__/bolero_wasm_eq/corpus/empty
  echo "##### $f $arm: corpus/empty + cargo bolero reduce"
  (cd $d && RUSTFLAGS="-C overflow-checks=false" cargo bolero reduce bolero_wasm_eq 2>&1 | grep -v "^warning\|^ *|\|^ *=\|^ *-->\|^$\|Compiling\|Downloaded\|^help\|^[0-9]* [-+|]" | tail -15; echo "rc=${PIPESTATUS[0]}")
 done
done
