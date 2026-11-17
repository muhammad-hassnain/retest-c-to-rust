#!/bin/bash
# Build the image used by the M3 toolchain checks (systems/vert/toolchain_*.py).
#
#   bash systems/vert/toolchain_image.sh
#
# crust-vert-toolchain = crust-stable (ubuntu 22.04, gcc 11.4, rustc 1.86.0, native arch)
#   + rustup toolchain nightly-2023-07-29 (the pin in artifacts/vert/benchmark/rust-toolchain.toml)
#   + clang-14 / llvm-14 installed WITHOUT the unversioned `clang` alias.
# The missing alias matters: systems/vert/run.py switches its uninitialized-read filter from the
# gcc -O0/-O1 cross-check to clang MSan when `clang` is on PATH. The primary arm64 run used
# crust-stable (no clang), so the pinned-nightly re-run must not see `clang` either.
# toolchain_pinned.py asserts shutil.which("clang") is None before it runs.
set -euo pipefail
BASE_ID=$(docker image inspect crust-stable --format '{{.Id}}')
echo "base crust-stable = $BASE_ID"
docker build -t crust-vert-toolchain - <<'DOCKERFILE'
FROM crust-stable
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends clang-14 llvm-14 \
    && rm -rf /var/lib/apt/lists/* \
    && ! command -v clang
RUN rustup toolchain install nightly-2023-07-29 --profile minimal \
    && rustup default 1.86.0 \
    && rustc +nightly-2023-07-29 -vV && rustc +1.86.0 -vV && clang-14 --version
DOCKERFILE
docker image inspect crust-vert-toolchain --format '{{.Id}}'
