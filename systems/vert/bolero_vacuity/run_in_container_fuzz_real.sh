#!/bin/bash
# 30 s fuzzing of VERT's REAL (unmodified) translations with VERT's own bolero harness (arXiv re-run).
# Same container setup as run_in_container_current.sh (ubuntu:22.04; cargo-bolero from VERT's fork
# YoshikiTakashima/bolero@1d02eb8, branch kani-unwind-0; nightly-2023-07-29 per
# benchmark/rust-toolchain.toml), then check_fuzz_real.py on the 28 functions of
# results/vert_bolero_vacuity_current.json (the counted VERT scalar wrong answers on grid v2), split over
# WORKERS parallel workers (one part file each). Setup steps are skipped when already done, so the
# script can be re-run in the same container. Run from the repo root, e.g.:
#   docker run -d --name arxiv_vertfuzz --cpus=4 -e CARGO_BUILD_JOBS=1 -v "$PWD:/w" -w /w \
#     ubuntu:22.04@sha256:0a9887931b0e38af2e3487ed39e29436b0535c4f3f52e57745baaf79d1898956 sleep infinity
#   docker exec arxiv_vertfuzz bash systems/vert/bolero_vacuity/run_in_container_fuzz_real.sh
set -e
if ! command -v clang >/dev/null || ! command -v git >/dev/null; then
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq build-essential clang curl python3 ca-certificates pkg-config libssl-dev git >/dev/null
fi
[ -x "$HOME/.cargo/bin/rustup" ] || curl -sSf https://sh.rustup.rs | sh -s -- -y -q >/dev/null
. "$HOME/.cargo/env"
rustup toolchain list | grep -q nightly-2023-07-29 || rustup toolchain install nightly-2023-07-29 --profile minimal
if ! cargo bolero --version >/dev/null 2>&1; then
  cargo install cargo-bolero --git https://github.com/YoshikiTakashima/bolero.git --rev 1d02eb853af3dad36eba5f03a9a8c41e5488cfef --locked \
    || cargo install cargo-bolero --git https://github.com/YoshikiTakashima/bolero.git --rev 1d02eb853af3dad36eba5f03a9a8c41e5488cfef -q
fi
cargo bolero --version || true
OUTDIR=${OUTDIR:-work/arxiv_vert_fuzz30_work}
export BOLERO_LOGDIR=${BOLERO_LOGDIR:-results/arxiv_vert_fuzz30_logs}
WORKERS=${WORKERS:-4}
mkdir -p "$OUTDIR" "$BOLERO_LOGDIR"
B=artifacts/vert/benchmark/c_transcoder
python3 -I - "$OUTDIR/container_toolchains.json" <<'EOF'
import json, subprocess, sys
def sh(c): return subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()
json.dump({"os_release": sh(". /etc/os-release; echo $PRETTY_NAME"), "uname_m": sh("uname -m"), "kernel": sh("uname -r"),
           "rustc_project_toolchain": sh("rustc +nightly-2023-07-29 --version"),
           "rustc_project_toolchain_llvm": sh("rustc +nightly-2023-07-29 -vV | grep LLVM"),
           "cargo_project_toolchain": sh("cargo +nightly-2023-07-29 --version"),
           "rustup_default_rustc": sh("rustc --version"),
           "cargo_bolero": sh("cargo bolero --version") + " (git YoshikiTakashima/bolero rev 1d02eb853af3dad36eba5f03a9a8c41e5488cfef)",
           "clang": sh("clang --version | head -1"), "gcc": sh("gcc --version | head -1"),
           "python3": sh("python3 --version"), "nproc_in_container": sh("nproc"),
           "cpu_quota": sh("cat /sys/fs/cgroup/cpu.max 2>/dev/null"),
           "cargo_build_jobs": sh("echo ${CARGO_BUILD_JOBS:-unset}"),
           "date_start_utc": sh("date -u +%Y-%m-%dT%H:%M:%SZ")},
          open(sys.argv[1], "w"), indent=1)
EOF
# FUNCS_OVERRIDE (opt-in, space-separated): run only these functions
if [ -n "${FUNCS_OVERRIDE:-}" ]; then read -ra FUNCS <<< "$FUNCS_OVERRIDE"
else mapfile -t FUNCS < <(python3 -I -c 'import json; [print(r["function"]) for r in json.load(open("results/vert_bolero_vacuity_current.json"))["rows"]]'); fi
echo "functions: ${#FUNCS[@]} workers: $WORKERS"
for w in $(seq 0 $((WORKERS - 1))); do
  L=(); for i in "${!FUNCS[@]}"; do if [ $((i % WORKERS)) -eq $w ]; then L+=("${FUNCS[$i]}"); fi; done
  SHARED_TARGET=/tmp/fzr_target_w$w python3 -I systems/vert/bolero_vacuity/check_fuzz_real.py $B "$OUTDIR/part$w.json" "${L[@]}" > "$OUTDIR/worker$w.out" 2>&1 &
done
wait
date -u +%Y-%m-%dT%H:%M:%SZ > "$OUTDIR/date_end_utc.txt"
echo "workers done"
