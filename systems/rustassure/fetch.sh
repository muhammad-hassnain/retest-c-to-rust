#!/usr/bin/env bash
# Fetch RustAssure at a pinned commit (not vendored). Upstream: github.com/davsec-lab/rustassure
# NOTE: default branch is "configurations"; no license declared upstream.
set -euo pipefail
PIN="39618406bd0c6acb1c5ca2cd697c3781a8e4c296"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST="${HERE}/artifacts/rustassure"
if [ -d "${DEST}/.git" ]; then
  echo "[fetch] rustassure present; HEAD=$(git -C "${DEST}" rev-parse HEAD)"
else
  git clone --branch configurations https://github.com/davsec-lab/rustassure.git "${DEST}"
  git -C "${DEST}" checkout -q "${PIN}"
fi
echo "[fetch] rustassure @ $(git -C "${DEST}" rev-parse HEAD)"
[ "$(git -C "${DEST}" rev-parse HEAD)" = "${PIN}" ] || { echo "[fetch] ERROR: HEAD != pinned commit ${PIN}" >&2; exit 1; }   # the commit hash pins the content
