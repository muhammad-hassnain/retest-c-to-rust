#!/usr/bin/env bash
# Fetch RustMap at a pinned commit (not vendored).
# Upstream: https://github.com/Cxm211/RustMap  (Apache-2.0)
set -euo pipefail
PIN="cb1df95fb9f4950f0ae1a43863c0051ff991891d"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST="${HERE}/artifacts/rustmap"
if [ -d "${DEST}/.git" ]; then
  echo "[fetch] rustmap present; HEAD=$(git -C "${DEST}" rev-parse HEAD)"
else
  git clone https://github.com/Cxm211/RustMap.git "${DEST}"
  git -C "${DEST}" checkout -q "${PIN}"
fi
echo "[fetch] rustmap @ $(git -C "${DEST}" rev-parse HEAD)"
[ "$(git -C "${DEST}" rev-parse HEAD)" = "${PIN}" ] || { echo "[fetch] ERROR: HEAD != pinned commit ${PIN}" >&2; exit 1; }   # the commit hash pins the content
