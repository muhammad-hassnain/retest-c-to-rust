#!/bin/bash
# M3 item 3 (RustAssure): its README (artifacts/rustassure/README.md:38-41) documents rustc 1.64.0;
# our harness used 1.86.0 (systems/rustassure/run.py, u8c_e2e) and nightly-2022-08-08 (libcsv_e2e).
# Re-run all three harnesses UNCHANGED under 1.64.0 and, as same-session controls, under the
# primary's toolchains, in a PRIVATE COPY of the repo (u8c_e2e/libcsv_e2e write results.json and
# build files next to themselves, so they must not run in the shared tree).
#
#   bash systems/vert/toolchain_rustassure.sh BASE_IMAGE COPY_REPO SCRATCH CPUS GIT_REV
# libcsv's crate depends on libc =0.2.126; cargo 1.64 cannot use the sparse crates.io index, so the
# dependency is vendored once with cargo 1.86 (`cargo vendor`, into the private copy) and every
# toolchain then builds it --offline from that vendor directory.
set -euo pipefail
BASE=$1; COPY=$2; SCR=$3; CPUS=$4; GIT_REV=$5
docker build -t crust-rustassure-toolchains - <<DOCKERFILE
FROM $BASE
RUN rustup toolchain install 1.64.0 --profile minimal && rustup toolchain install nightly-2022-08-08 --profile minimal \
    && rustup default 1.86.0 && rustup toolchain list && ! command -v clang
DOCKERFILE
IMAGE_DESC="crust-rustassure-toolchains $(docker image inspect crust-rustassure-toolchains --format '{{.Id}}') FROM $BASE"
mkdir -p "$SCR/rustassure"
ART=$(readlink -f "$COPY/artifacts")
docker run --rm --cpus "$CPUS" -v "$COPY:/work" -v "$ART:/work/artifacts:ro" -v "$SCR:/scratch" -w /work \
  -e GIT_REV="$GIT_REV" -e IMAGE_DESC="$IMAGE_DESC" -e PERFN_TCS="${PERFN_TCS-1.86.0 1.64.0}" -e SKIP_LIBCSV="${SKIP_LIBCSV:-}" crust-rustassure-toolchains bash -c '
set -u
O=/scratch/rustassure
for TC in ${PERFN_TCS-1.86.0 1.64.0}; do
  RUSTUP_TOOLCHAIN=$TC python3 systems/rustassure/run.py --out $O/rustassure_diff_$TC.json > $O/perfn_$TC.log 2>&1; echo "perfn $TC rc=$?"
  rm -f /tmp/u8c_sut /tmp/u8c_oracle
  RUSTUP_TOOLCHAIN=$TC python3 systems/rustassure/u8c_e2e/run.py > $O/u8c_$TC.log 2>&1; rc=$?; echo "u8c $TC rc=$rc"
  [ $rc -eq 0 ] && cp systems/rustassure/u8c_e2e/results.json $O/u8c_$TC.json
done
[ -n "${SKIP_LIBCSV:-}" ] || { cd systems/rustassure/libcsv_e2e/sut_crate && rm -rf target Cargo.lock vendor .cargo
python3 ../extract_sut.py ../csv_sut.rs >/dev/null 2>&1; mkdir -p src; cp ../csv_sut.rs src/main.rs
mkdir -p .cargo && RUSTUP_TOOLCHAIN=1.86.0 cargo vendor vendor >/dev/null && rm -f Cargo.lock
printf "[source.crates-io]\nreplace-with = \"vendored-sources\"\n[source.vendored-sources]\ndirectory = \"vendor\"\n" > .cargo/config.toml
cd /work
for TC in nightly-2022-08-08 1.64.0; do
  rm -rf systems/rustassure/libcsv_e2e/sut_crate/target /tmp/csv_oracle
  RUSTUP_TOOLCHAIN=$TC CARGO_NET_OFFLINE=true python3 systems/rustassure/libcsv_e2e/run.py > $O/libcsv_$TC.log 2>&1; rc=$?; echo "libcsv $TC rc=$rc"
  [ $rc -eq 0 ] && cp systems/rustassure/libcsv_e2e/results.json $O/libcsv_$TC.json
done; }
# u8c is char-signedness sensitive (x86 gcc: signed char; arm64 gcc: unsigned char): re-run the
# 1.86.0 u8c harness with a gcc wrapper that forces each signedness (the Rust side is unchanged).
for FL in -funsigned-char -fsigned-char; do
  mkdir -p /tmp/w$FL && printf "#!/bin/sh\nexec /usr/bin/gcc $FL \"\$@\"\n" > /tmp/w$FL/gcc && chmod +x /tmp/w$FL/gcc
  rm -f /tmp/u8c_sut /tmp/u8c_oracle
  PATH=/tmp/w$FL:$PATH RUSTUP_TOOLCHAIN=1.86.0 python3 systems/rustassure/u8c_e2e/run.py > $O/u8c_1.86.0$FL.log 2>&1; rc=$?; echo "u8c 1.86.0 $FL rc=$rc"
  [ $rc -eq 0 ] && cp systems/rustassure/u8c_e2e/results.json $O/u8c_1.86.0$FL.json
done
for TC in 1.86.0 1.64.0 nightly-2022-08-08; do echo "$TC: $(rustc +$TC -V)"; done > $O/versions.txt
echo "libcsv SUT binary (last build, 1.64.0 unless SKIP_LIBCSV) embeds: $(strings systems/rustassure/libcsv_e2e/sut_crate/target/release/csv_sut 2>/dev/null | grep -o "rustc version [0-9.a-z-]*" | sort -u | tr "\n" " ")" >> $O/versions.txt
echo "$IMAGE_DESC" >> $O/versions.txt; echo "git_rev $GIT_REV (private copy, artifacts mounted read-only)" >> $O/versions.txt
'
