#!/usr/bin/env bash
# Fetch Rustine at a pinned commit (not vendored). Upstream: github.com/Intelligent-CAT-Lab/Rustine
# Note: the paper's tool SOURCE is withheld "until acceptance"; the translated artifacts are shipped.
set -euo pipefail
PIN="774ff51e48d4d3a6a73e4864689a042fc1028fc0"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST="${HERE}/artifacts/rustine"
if [ -d "${DEST}/.git" ]; then
  echo "[fetch] rustine present; HEAD=$(git -C "${DEST}" rev-parse HEAD)"
else
  git clone https://github.com/Intelligent-CAT-Lab/Rustine.git "${DEST}"
  git -C "${DEST}" checkout -q "${PIN}"
fi
echo "[fetch] rustine @ $(git -C "${DEST}" rev-parse HEAD)"
[ "$(git -C "${DEST}" rev-parse HEAD)" = "${PIN}" ] || { echo "[fetch] ERROR: HEAD != pinned commit ${PIN}" >&2; exit 1; }   # the commit hash pins the content
