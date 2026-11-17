#!/usr/bin/env bash
# Fetch AdaTrans at a pinned commit (not vendored).
# Upstream: https://github.com/SlainTroyard/adatrans_dev  (no LICENSE file in the repo)
# The repo has a single commit (created 2026-09-20). The Zenodo bundle (fuzz input/output files,
# 710 MB) is NOT fetched; see systems/adatrans/notes.md for why it is not needed.
set -euo pipefail
PIN="31f4031aadb7ee9d4a2fb1a180e78ae9b45e5578"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST="${HERE}/artifacts/adatrans"
if [ -d "${DEST}/.git" ]; then
  echo "[fetch] adatrans present; HEAD=$(git -C "${DEST}" rev-parse HEAD)"
else
  git clone https://github.com/SlainTroyard/adatrans_dev.git "${DEST}"
  git -C "${DEST}" checkout -q "${PIN}"
fi
echo "[fetch] adatrans @ $(git -C "${DEST}" rev-parse HEAD)"
[ "$(git -C "${DEST}" rev-parse HEAD)" = "${PIN}" ] || { echo "[fetch] ERROR: HEAD != pinned commit ${PIN}" >&2; exit 1; }   # the commit hash pins the content
# Content digest of the checked-out tree (git archive is deterministic for a given commit).
echo "[fetch] tree sha256: $(git -C "${DEST}" archive --format=tar "${PIN}" | shasum -a 256 | cut -d' ' -f1)"
