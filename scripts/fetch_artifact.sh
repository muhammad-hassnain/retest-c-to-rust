#!/usr/bin/env bash
# Fetch the C2SaferRust published artifact at a PINNED commit.
# The artifact is never vendored into this repo (see .gitignore); it is fetched
# reproducibly here so a clean clone + this script reproduces the exact inputs.
#
# Upstream: https://github.com/vikramnitin9/c2saferrust  (MIT, Vikram Nitin)
set -euo pipefail

REPO_URL="https://github.com/vikramnitin9/c2saferrust.git"
PINNED_COMMIT="e8d5c3a18b91523f0ca6e52117cbd2f288d21c31"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${HERE}/artifacts/c2saferrust"

mkdir -p "${HERE}/artifacts"

if [ -d "${DEST}/.git" ]; then
  echo "[fetch] artifact already present at ${DEST}"
  have="$(git -C "${DEST}" rev-parse HEAD 2>/dev/null || echo none)"
  if [ "${have}" != "${PINNED_COMMIT}" ]; then
    echo "[fetch] WARNING: present commit ${have} != pinned ${PINNED_COMMIT}"
    echo "[fetch] fetching pinned commit..."
    git -C "${DEST}" fetch --depth 1 origin "${PINNED_COMMIT}"
    git -C "${DEST}" checkout -q "${PINNED_COMMIT}"
  fi
else
  echo "[fetch] cloning ${REPO_URL} @ ${PINNED_COMMIT} -> ${DEST}"
  git clone "${REPO_URL}" "${DEST}"
  git -C "${DEST}" checkout -q "${PINNED_COMMIT}"
fi

echo "[fetch] HEAD = $(git -C "${DEST}" rev-parse HEAD)"
[ "$(git -C "${DEST}" rev-parse HEAD)" = "${PINNED_COMMIT}" ] || { echo "[fetch] ERROR: HEAD != pinned commit ${PINNED_COMMIT}" >&2; exit 1; }   # the commit hash pins the content
echo "[fetch] utilities available:"
ls "${DEST}/coreutils/src" 2>/dev/null | sed 's/^/        - /'
echo "[fetch] done."
