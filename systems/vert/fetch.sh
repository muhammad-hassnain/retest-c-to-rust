#!/usr/bin/env bash
# Fetch VERT at a pinned commit (not vendored). Upstream: github.com/YoshikiTakashima/vert (MIT)
set -euo pipefail
PIN="3270ceccb63a28acd611c8fa6105ed37dc76de07"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST="${HERE}/artifacts/vert"
if [ -d "${DEST}/.git" ]; then
  echo "[fetch] vert present; HEAD=$(git -C "${DEST}" rev-parse HEAD)"
else
  git clone https://github.com/YoshikiTakashima/vert.git "${DEST}"
  git -C "${DEST}" checkout -q "${PIN}"
fi
echo "[fetch] vert @ $(git -C "${DEST}" rev-parse HEAD)"
[ "$(git -C "${DEST}" rev-parse HEAD)" = "${PIN}" ] || { echo "[fetch] ERROR: HEAD != pinned commit ${PIN}" >&2; exit 1; }   # the commit hash pins the content
