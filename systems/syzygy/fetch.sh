#!/usr/bin/env bash
# Fetch Syzygy's released Zopfli case study at a pinned commit (not vendored).
# Upstream: https://github.com/syzygy-project/Syzygy_Zopfli  (MIT)
set -euo pipefail
PIN="e0e7666d5c5b6872894ba0909700a7cbaafcf4ec"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST="${HERE}/artifacts/syzygy_zopfli"
if [ -d "${DEST}/.git" ]; then
  echo "[fetch] syzygy present; HEAD=$(git -C "${DEST}" rev-parse HEAD)"
else
  git clone https://github.com/syzygy-project/Syzygy_Zopfli.git "${DEST}"
  git -C "${DEST}" checkout -q "${PIN}"
fi
echo "[fetch] syzygy @ $(git -C "${DEST}" rev-parse HEAD)"
[ "$(git -C "${DEST}" rev-parse HEAD)" = "${PIN}" ] || { echo "[fetch] ERROR: HEAD != pinned commit ${PIN}" >&2; exit 1; }   # the commit hash pins the content
