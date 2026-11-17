#!/usr/bin/env bash
# Driver for harness/mutate_full.py on the x86_64 Linux box (M7: CLI harness power, full grid).
#
# Layout (WORK, default ~/work/cli-mutation; nothing outside it is written):
#   WORK/repo            rsync of the study repo working tree (artifacts/ excluded)
#   WORK/trees/<util>/{c,rust}   private copies of artifacts/c2saferrust/coreutils/src/<util>/*
#                        (checksummed equal to the Mac copies; target/ and objects excluded)
#   WORK/vendor/libc-0.2.158     the crate the translations' Cargo.lock pins (sha256 d8adc4bb...
#                        equals the lockfile checksum), so cargo builds offline (--network none)
#   WORK/cargo_home/config.toml  source replacement -> /w/vendor, net.offline = true
#   WORK/build           baselines, coverage, per-worker trees, mutants.jsonl
#
# Every step runs in the crust-heldout image (ubuntu 22.04, gcc 11.4.0, rustc nightly-2022-08-08)
# capped at 8 CPUs, except `llvm-cov export` (LLVM 21 on the host; see mutate_full_cov.py).
# RUSTUP_TOOLCHAIN pins the image's only toolchain (nightly-2022-08-08, the one the trees'
# rust-toolchain.toml names); without it rustup tries to download the extra components
# (rustc-dev, miri) that file lists, which the build does not use.
#
#   harness/mutate_full_run.sh prov|prepare|baseline|covrun|covexport|sites|run|tcecheck|extgrid|summarize
#   harness/mutate_full_run.sh flourine     (stretch: Flourine scalar harness, K mutants per op)
#   (GIT_REV=<study repo HEAD on the Mac> for prov; the Linux copy has no .git)
set -euo pipefail
WORK="${WORK:-$HOME/work/cli-mutation}"
IMG="${IMG:-crust-heldout}"
JOBS="${JOBS:-8}"
N="${N:-40}"
LLVM="${LLVM:-/usr/lib/llvm-21/bin}"
VARIANT="${VARIANT:-rust}"          # rust (C2Rust baseline, default) | rust_WIP (C2SaferRust output)
# rust_WIP is summarised into its build dir and folded into results/cli_mutation_full.json under
# the key "rust_WIP" by the (later) rust summarize, so it is reported separately in the same file.
if [ "${VARIANT}" = rust ]; then B=build; OUT=/w/repo/results/cli_mutation_full.json
  STEPS="prov prepare baseline covrun covexport sites run tcecheck extgrid summarize (after the rust_WIP summarize)"
else B="build_${VARIANT}"; OUT="/w/${B}/cli_mutation_full_${VARIANT}.json"
  STEPS="prov prepare baseline covrun covexport sites run tcecheck summarize (no extgrid, no hand review)"; fi

dock() {
  docker run --rm --network none --cpus "${JOBS}" -u "$(id -u):$(id -g)" \
    -e HOME=/tmp -e CARGO_HOME=/w/cargo_home -e MFULL_WORK=/w \
    -e RUSTUP_TOOLCHAIN=nightly-2022-08-08 -e MFULL_VARIANT="${VARIANT}" \
    -v "${WORK}:/w" -w /w/repo/harness "${IMG}" "$@"
}

step="$1"; shift || true
case "${step}" in
  prepare)   dock python3 mutate_full.py prepare ;;
  baseline)  dock python3 mutate_full.py baseline --jobs "${JOBS}" ;;
  covrun)    dock python3 mutate_full.py covrun --jobs "${JOBS}" ;;
  covexport)
    for u in cat head tail uniq; do
      d="${WORK}/${B}/covraw/${u}"
      python3 "${WORK}/repo/harness/mutate_full_cov.py" "${WORK}/${B}/cov_${u}.proftext" "${d}"/*.profraw
      "${LLVM}/llvm-profdata" merge -o "${WORK}/${B}/cov_${u}.profdata" "${WORK}/${B}/cov_${u}.proftext"
      "${LLVM}/llvm-cov" export -instr-profile="${WORK}/${B}/cov_${u}.profdata" \
        -path-equivalence=/w,"${WORK}" "${WORK}/${B}/cov/${u}/debug/${u}" > "${WORK}/${B}/cov_${u}.json"
      "${LLVM}/llvm-cov" report -instr-profile="${WORK}/${B}/cov_${u}.profdata" \
        -path-equivalence=/w,"${WORK}" "${WORK}/${B}/cov/${u}/debug/${u}" \
        | grep -E "(^${u}\.rs|^Filename|^TOTAL)" > "${WORK}/${B}/cov_${u}_report.txt" || true
    done ;;
  sites)     dock python3 mutate_full.py sites --cov-json "/w/${B}/cov_{util}.json" -n "${N}" ;;
  run)       dock python3 mutate_full.py run --jobs "${JOBS}" "$@" ;;
  tcecheck)  dock python3 mutate_full_tce_check.py ;;
  extgrid)   dock python3 mutate_full_extgrid.py --jobs "${JOBS}" ;;
  prov)
    mkdir -p "${WORK}/${B}"
    tc="$(dock bash -c 'rustc -vV; cargo --version; gcc --version | head -1; python3 --version' | tr '\n' ';')"
    python3 - "${WORK}/${B}/provenance_host.json" "${tc}" <<PY
import json, sys, subprocess, platform, time
out, tc = sys.argv[1], sys.argv[2]
sh = lambda c: subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()
json.dump({
  "command": "WORK=${WORK} JOBS=${JOBS} N=${N} VARIANT=${VARIANT} harness/mutate_full_run.sh <step>, one call per step: ${STEPS}",
  "git_rev": "${GIT_REV:-unknown}",
  "git_note": "study repo HEAD on the Mac; the mutate_full* scripts are new, uncommitted files rsynced to the Linux work copy",
  "host": platform.system(), "arch": platform.machine(), "kernel": platform.release(),
  "image": "${IMG}", "image_id": sh("docker image inspect ${IMG} --format '{{.Id}}'"),
  "image_toolchains": tc,
  "rustup_toolchain_env": "RUSTUP_TOOLCHAIN=nightly-2022-08-08",
  "libc_crate": "libc 0.2.158 vendored, sha256 d8adc4bb1803a324070e64a98ae98f38934d91957a99cfb3a43dcbc01bc56439 (= Cargo.lock checksum)",
  "llvm_for_coverage": sh("${LLVM}/llvm-cov --version | grep -i version | head -1"),
  "cpus": "docker --cpus ${JOBS}",
  "date": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, open(out, "w"), indent=1)
PY
    cat "${WORK}/${B}/provenance_host.json" ;;
  flourine)
    FLIMG="${FLIMG:-audit3-stable186}"   # ubuntu 22.04, gcc 11.4, rustc 1.86.0 (= the Mac crust-stable toolchain); recipe: docker/Dockerfile.audit3-stable186
    FLROOT="${FLROOT:-$(cd "$(dirname "$0")/.." && pwd)/artifacts/flourine_dl/flourine-artifact}"
    docker run --rm --network none --cpus "${JOBS}" -u "$(id -u):$(id -g)" -e HOME=/tmp \
      -e MFULL_WORK=/w -e MFULL_FL_SCRATCH=/tmp/fl -e FLOURINE_ROOT=/fl \
      -e GIT_REV="${GIT_REV:-unknown}" -e MFULL_IMAGE="${FLIMG}" \
      -e MFULL_IMAGE_ID="$(docker image inspect "${FLIMG}" --format '{{.Id}}')" -e MFULL_HOST="$(uname -s) $(uname -m)" \
      -v "${FLROOT}:/fl:ro" -v "${WORK}:/w" -w /w/repo "${FLIMG}" \
      python3 harness/mutate_full_flourine.py --jobs "${JOBS}" -k "${K:-8}" \
      --out /w/repo/results/flourine_mutation.json "$@" ;;
  summarize) dock python3 mutate_full.py summarize --out "${OUT}" "$@" ;;
  *) echo "unknown step ${step}"; exit 2 ;;
esac
