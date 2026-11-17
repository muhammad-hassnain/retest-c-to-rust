#!/usr/bin/env bash
# Fetch PtrTrans at a pinned commit (not vendored). Upstream: github.com/FudanSELab/PtrTrans-C2Rust
# NOTE: default branch "master"; no license declared upstream (GitHub license null, no LICENSE file).
# Only dataset/crown_dataset/quadtree (original C) and dataset/Trans_C-Rust-KG/quadtree (PtrTrans's
# own Rust, Trans_PA) are used. The artifact is untrusted: nothing in it is built or run on the host.
set -euo pipefail
PIN="b20d5bb68a598a739f79d52114651e37cc6999cb"   # "update readme", 2026-04-12 (repo HEAD on 2026-11-13)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST="${HERE}/artifacts/ptrtrans"
if [ -d "${DEST}/.git" ]; then
  echo "[fetch] ptrtrans present; HEAD=$(git -C "${DEST}" rev-parse HEAD)"
else
  git clone --no-checkout https://github.com/FudanSELab/PtrTrans-C2Rust.git "${DEST}"
  git -C "${DEST}" -c core.hooksPath=/dev/null checkout -q "${PIN}"
fi
echo "[fetch] ptrtrans @ $(git -C "${DEST}" rev-parse HEAD)"
[ "$(git -C "${DEST}" rev-parse HEAD)" = "${PIN}" ] || { echo "[fetch] ERROR: HEAD != pinned commit ${PIN}" >&2; exit 1; }
# Content digests of the two trees this study uses (sha256 over sorted "relpath sha256" lines).
for d in dataset/crown_dataset/quadtree dataset/Trans_C-Rust-KG/quadtree; do
  dig=$(cd "${DEST}/${d}" && find . -type f -not -path './target/*' -print0 | LC_ALL=C sort -z \
        | xargs -0 shasum -a 256 | shasum -a 256 | cut -d' ' -f1)
  echo "[fetch] ${d} tree-sha256 (excluding target/) = ${dig}"
done
