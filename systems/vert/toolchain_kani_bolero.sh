#!/bin/bash
# Follow-up driver (Linux x86-64, private repo copy, <= 3 cores).
#   bash systems/vert/toolchain_kani_bolero.sh SCRATCH GIT_REV [kani|bolero|all]
# kani:   VERT's bounded-Kani command (crust-kani, Kani 0.55.0) on the 8 counted defects with bounded_kani=0
# bolero: VERT's documented bolero replay (check_current.py vert_reduce arm; ubuntu:22.04 + nightly-2023-07-29 +
#         cargo-bolero YoshikiTakashima/bolero@1d02eb8, the setup of bolero_vacuity/run_in_container_current.sh)
#         on the 12 array/float defective functions in results/defect_ledger.json
set -euo pipefail
SCR=$1; GIT_REV=$2; WHAT=${3:-all}
REPO=$(cd "$(dirname "$0")/../.." && pwd)
ART=$(readlink -f "$REPO/artifacts")
mkdir -p "$SCR/kani_logs" "$SCR/bolero_logs" "$SCR/bolero_parts"
KANI_FUNCS="CEILING_IN_A_SORTED_ARRAY FIND_SMALLEST_VALUE_REPRESENTED_SUM_SUBSET_GIVEN_ARRAY COUNT_DISTINCT_NON_NEGATIVE_PAIRS_X_Y_SATISFY_INEQUALITY_XX_YY_N_2_1 COUNT_STRINGS_CAN_FORMED_USING_B_C_GIVEN_CONSTRAINTS_1 HEIGHT_COMPLETE_BINARY_TREE_HEAP_N_NODES MAXIMUM_GAMES_PLAYED_WINNER NTH_PENTAGONAL_NUMBER PROGRAM_TO_CHECK_IF_A_GIVEN_NUMBER_IS_LUCKY_ALL_DIGITS_ARE_DIFFERENT"
if [ "$WHAT" = kani ] || [ "$WHAT" = all ]; then
  docker run --rm --cpus "${KANI_CPUS:-3}" -v "$REPO:/w" -v "$ART:/w/artifacts:ro" -v "$SCR:/scratch" -w /w \
    -e GIT_REV="$GIT_REV" -e HOST_DESC="$(uname -sm), docker --cpus ${KANI_CPUS:-3}" -e KANI_TIMEOUT="${KANI_TIMEOUT:-900}" \
    -e IMAGE_DESC="crust-kani $(docker image inspect crust-kani --format '{{.Id}}')" \
    crust-kani python3 systems/vert/toolchain_kani_remaining.py /scratch/kani_remaining.json /scratch/kani_logs "${KANI_WORKERS:-3}" $KANI_FUNCS
fi
if [ "$WHAT" = bolero ] || [ "$WHAT" = all ]; then
  FUNCS=$(cd "$REPO" && python3 -c '
import json
fs=[]
for x in json.load(open("results/defect_ledger.json"))["rows"]:
    if x["system"]=="VERT" and (x.get("exposure_breakdown") or {}).get("family")=="array/float":
        f=x["translation"].split(" ")[0]
        fs.append(f) if f not in fs else None
print(" ".join(fs))')
  docker run --rm --name "${BOLERO_NAME:-toolchain_bolero}" --cpus "${BOLERO_CPUS:-3}" -e CARGO_BUILD_JOBS=3 -v "$REPO:/w" -v "$ART:/w/artifacts:ro" -v "$SCR:/scratch" -w /w \
    -e BOLERO_LOGDIR=/scratch/bolero_logs -e FUNCS="$FUNCS" \
    ubuntu:22.04@sha256:0a9887931b0e38af2e3487ed39e29436b0535c4f3f52e57745baaf79d1898956 bash -c '
set -e
apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq build-essential clang curl python3 ca-certificates pkg-config libssl-dev git >/dev/null
curl -sSf https://sh.rustup.rs | sh -s -- -y -q >/dev/null
. "$HOME/.cargo/env"
rustup toolchain install nightly-2023-07-29 --profile minimal
cargo install cargo-bolero --git https://github.com/YoshikiTakashima/bolero.git --rev 1d02eb853af3dad36eba5f03a9a8c41e5488cfef --locked \
  || cargo install cargo-bolero --git https://github.com/YoshikiTakashima/bolero.git --rev 1d02eb853af3dad36eba5f03a9a8c41e5488cfef -q
python3 - /scratch/bolero_parts/container_toolchains.json <<"PY"
import json, subprocess, sys
def sh(c): return subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()
json.dump({"os_release": sh(". /etc/os-release; echo $PRETTY_NAME"),
           "rustc_project_toolchain": sh("rustc +nightly-2023-07-29 --version"),
           "cargo_bolero": sh("cargo bolero --version") + " (git YoshikiTakashima/bolero rev 1d02eb853af3dad36eba5f03a9a8c41e5488cfef)",
           "clang": sh("clang --version | head -1"), "gcc": sh("gcc --version | head -1")}, open(sys.argv[1], "w"), indent=1)
PY
read -r -a F <<< "$FUNCS"
W0=(); W1=(); W2=()
for i in "${!F[@]}"; do case $((i % 3)) in 0) W0+=("${F[$i]}");; 1) W1+=("${F[$i]}");; 2) W2+=("${F[$i]}");; esac; done
B=artifacts/vert/benchmark/c_transcoder
python3 systems/vert/toolchain_bolero_remaining.py $B /scratch/bolero_parts/part0.json "${W0[@]}" > /scratch/bolero_parts/worker0.out 2>&1 &
python3 systems/vert/toolchain_bolero_remaining.py $B /scratch/bolero_parts/part1.json "${W1[@]}" > /scratch/bolero_parts/worker1.out 2>&1 &
python3 systems/vert/toolchain_bolero_remaining.py $B /scratch/bolero_parts/part2.json "${W2[@]}" > /scratch/bolero_parts/worker2.out 2>&1 &
wait; echo workers done'
fi
