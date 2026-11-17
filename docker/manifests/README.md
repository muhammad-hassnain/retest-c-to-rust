Installed packages (`dpkg-query -W`) of the images the shipped results were produced in, captured
2026-11-13 from the images on the two machines:

- `crust-heldout.amd64.dpkg.txt`: crust-heldout on the x86_64 machine (built 2026-10-19 from
  docker/Dockerfile; gcc 11.4, rustc nightly-2022-08-08). On that machine the local tag
  `crust-stable` points at this same image (same image ID), not at a rustc 1.86 build.
- `crust-syzygy.amd64.dpkg.txt`: crust-syzygy on the x86_64 machine (built 2026-10-19 from
  docker/Dockerfile.syzygy; rustc 1.86.0).
- `crust-stable.arm64.dpkg.txt`: crust-stable on the arm64 Mac (built 2026-11-05 from
  docker/Dockerfile.stable; rustc 1.86.0).
- `audit3-stable186.amd64.dpkg.txt`: audit3-stable186 on the x86_64 machine (built 2026-11-05 from
  docker/Dockerfile.stable as of the development repository's commit ec470ae, now
  docker/Dockerfile.audit3-stable186; gcc 11.4,
  rustc 1.86.0), the image of the FLOURINE mutation analyses. Captured 2026-11-17.
- `crust-heldout-arxiv-arm64.arm64.dpkg.txt`: crust-heldout-arxiv-arm64 on the arm64 Mac (built
  2026-11-16 from docker/Dockerfile for arm64; gcc 11.4, clang 14, rustc nightly-2022-08-08), the image
  of the aarch64 runs in results/arxiv_c2saferrust_head_aarch64.json. Captured 2026-11-17.
