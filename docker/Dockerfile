# Pinned Linux build environment for held-out differential testing of C2SaferRust (crust-heldout).
#
# Why a container: the C2SaferRust artifacts were built and tested on Linux/glibc.
# The extracted gnulib-heavy C sources do NOT build on macOS (missing <uchar.h> and
# other glibc headers). Rather than patch the oracle's build env -- which would
# compromise the "original C, faithfully compiled" guarantee -- we reproduce the
# original environment.
#
# Arch note: we build for the container's native arch. The oracle-vs-SUT comparison
# is internally consistent (both sides share the same arch/toolchain), so the
# held-out *divergence* result is valid on any arch. Faithfully reproducing the
# authors' *absolute* pass-rates would additionally pin x86_64 (--platform linux/amd64).
#
# Base image pinned by digest: the multi-arch manifest list (OCI index) of ubuntu:22.04 that the
# x86_64 images were built from (2026-10-19); it covers amd64 and arm64. The arm64 images on the Mac
# were built from a later ubuntu:22.04 index (sha256:076e7bf8...); docker/manifests/ lists the
# packages actually installed in the images the results came from.
#
# apt is pinned to the Ubuntu archive as it was at UBUNTU_SNAPSHOT (snapshot.ubuntu.com) on amd64.
# snapshot.ubuntu.com serves no arm64 (ports) snapshots, so arm64 builds use the live archive.
# The first update runs without TLS peer verification only because ca-certificates is not yet
# installed; apt still verifies the archive's GPG-signed Release files.
FROM ubuntu:22.04@sha256:0a9887931b0e38af2e3487ed39e29436b0535c4f3f52e57745baaf79d1898956

ARG UBUNTU_SNAPSHOT=20261019T210000Z
ENV DEBIAN_FRONTEND=noninteractive
RUN set -eu; \
    if [ "$(dpkg --print-architecture)" = amd64 ] && [ -n "${UBUNTU_SNAPSHOT}" ]; then \
      sed -i -E "s#http://(archive|security)\.ubuntu\.com/ubuntu/?#https://snapshot.ubuntu.com/ubuntu/${UBUNTU_SNAPSHOT}/#" /etc/apt/sources.list; \
      apt-get -o Acquire::https::Verify-Peer=false update; \
      apt-get -o Acquire::https::Verify-Peer=false install -y --no-install-recommends ca-certificates; \
    else apt-get update; fi; \
    apt-get install -y --no-install-recommends \
        build-essential gcc make clang \
        curl ca-certificates git pkg-config \
        libc6-dev python3; \
    rm -rf /var/lib/apt/lists/*

# Rust toolchain pinned to the translation's own rust-toolchain.toml.
# rustup itself pinned (1.29.1, the version in the x86_64 images), checked by sha256, with self-update
# off (otherwise the first toolchain install updates rustup); toolchains are pinned by version and
# verified by rustup against the channel manifest.
ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo PATH=/opt/cargo/bin:$PATH
RUN set -eu; \
    case "$(dpkg --print-architecture)" in \
      amd64) T=x86_64-unknown-linux-gnu;  SHA=dda7234360b7f578ca8b0ddcb80145646fa61a67c1720a5abc7051b35c9fcb71 ;; \
      arm64) T=aarch64-unknown-linux-gnu; SHA=15f6e4ce9f583b929c996c91562bad6d4454f3281de858b02cdfdef615fac433 ;; \
      *) echo "unsupported architecture" >&2; exit 1 ;; \
    esac; \
    curl --proto '=https' --tlsv1.2 -sSfL -o /tmp/rustup-init "https://static.rust-lang.org/rustup/archive/1.29.1/${T}/rustup-init"; \
    echo "${SHA}  /tmp/rustup-init" | sha256sum -c -; \
    chmod +x /tmp/rustup-init; \
    /tmp/rustup-init -y --no-modify-path --profile minimal --default-toolchain none; \
    rm /tmp/rustup-init; \
    rustup set auto-self-update disable; \
    rustup toolchain install nightly-2022-08-08 --profile minimal --component rust-src; \
    rustup default nightly-2022-08-08; \
    rustc --version && cargo --version && gcc --version | head -1

WORKDIR /work
CMD ["bash"]
