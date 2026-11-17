#!/bin/bash
# Same container setup as run_in_container.sh (ubuntu:22.04; cargo-bolero from VERT's fork
# YoshikiTakashima/bolero@1d02eb8, branch kani-unwind-0; nightly-2023-07-29 per
# benchmark/rust-toolchain.toml), then check_current.py on EXACTLY the current 25 VERT scalar wrong
# answers (results/vert_diff.json value_bugs), split over 3 parallel workers (one part file each),
# then merged into results/vert_bolero_vacuity_current.json. Run from the repo root:
#   docker run --rm --cpus=3 -e CARGO_BUILD_JOBS=3 -v "$PWD:/w" -w /w ubuntu:22.04 \
#     bash systems/vert/bolero_vacuity/run_in_container_current.sh
set -e
apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq build-essential clang curl python3 ca-certificates pkg-config libssl-dev git >/dev/null
curl -sSf https://sh.rustup.rs | sh -s -- -y -q >/dev/null
. "$HOME/.cargo/env"
rustup toolchain install nightly-2023-07-29 --profile minimal
cargo install cargo-bolero --git https://github.com/YoshikiTakashima/bolero.git --rev 1d02eb853af3dad36eba5f03a9a8c41e5488cfef --locked \
  || cargo install cargo-bolero --git https://github.com/YoshikiTakashima/bolero.git --rev 1d02eb853af3dad36eba5f03a9a8c41e5488cfef -q
cargo bolero --version || true
OUTDIR=${OUTDIR:-results/vert_bolero_current_work}
export BOLERO_LOGDIR=results/vert_bolero_logs_current
mkdir -p "$OUTDIR" "$BOLERO_LOGDIR"
B=artifacts/vert/benchmark/c_transcoder
# toolchain facts for provenance
python3 - "$OUTDIR/container_toolchains.json" <<'EOF'
import json, subprocess, sys
def sh(c): return subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()
json.dump({"os_release": sh(". /etc/os-release; echo $PRETTY_NAME"),
           "rustc_project_toolchain": sh("rustc +nightly-2023-07-29 --version"),
           "cargo_project_toolchain": sh("cargo +nightly-2023-07-29 --version"),
           "rustup_default_rustc": sh("rustc --version"),
           "cargo_bolero": sh("cargo bolero --version") + " (git YoshikiTakashima/bolero rev 1d02eb853af3dad36eba5f03a9a8c41e5488cfef)",
           "clang": sh("clang --version | head -1"), "gcc": sh("gcc --version | head -1"),
           "python3": sh("python3 --version"), "nproc_in_container": sh("nproc")},
          open(sys.argv[1], "w"), indent=1)
EOF
# FUNCS_OVERRIDE (opt-in, space-separated): run only these functions (used to add the grid-v2 defects)
if [ -n "${FUNCS_OVERRIDE:-}" ]; then read -ra FUNCS <<< "$FUNCS_OVERRIDE"
else mapfile -t FUNCS < <(python3 -c 'import json; [print(v["func"]) for v in json.load(open("results/vert_diff.json"))["value_bugs"]]'); fi
echo "functions: ${#FUNCS[@]}"
W0=(); W1=(); W2=()
for i in "${!FUNCS[@]}"; do case $((i % 3)) in 0) W0+=("${FUNCS[$i]}");; 1) W1+=("${FUNCS[$i]}");; 2) W2+=("${FUNCS[$i]}");; esac; done
python3 systems/vert/bolero_vacuity/check_current.py $B "$OUTDIR/part0.json" "${W0[@]}" > "$OUTDIR/worker0.out" 2>&1 &
python3 systems/vert/bolero_vacuity/check_current.py $B "$OUTDIR/part1.json" "${W1[@]}" > "$OUTDIR/worker1.out" 2>&1 &
python3 systems/vert/bolero_vacuity/check_current.py $B "$OUTDIR/part2.json" "${W2[@]}" > "$OUTDIR/worker2.out" 2>&1 &
wait
echo "workers done"
