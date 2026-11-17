#!/usr/bin/env bash
# Fetch His2Trans at a pinned commit (not vendored). Upstream: github.com/SYSUSELab/His2Trans
# (public mirror of the paper's anonymous.4open.science/r/His2Trans-F1F0 artifact).
# Untrusted: nothing under artifacts/his2trans is built or run on the host; see run_in_docker.sh.
set -euo pipefail
PIN="3a0ec5544c8e6788280b3520105a77432ed8a730"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST="${HERE}/artifacts/his2trans"
if [ -d "${DEST}/.git" ]; then
  echo "[fetch] his2trans present; HEAD=$(git -C "${DEST}" rev-parse HEAD)"
else
  # The only LFS-tracked file (framework/workspace/rag/knowledge_base.json, the RAG knowledge base)
  # is not needed for re-grading, so LFS smudging is disabled and it stays a pointer file.
  NOLFS=(-c filter.lfs.smudge= -c filter.lfs.process= -c filter.lfs.required=false -c core.hooksPath=/dev/null)
  git "${NOLFS[@]}" clone --no-checkout https://github.com/SYSUSELab/His2Trans.git "${DEST}"
  git -C "${DEST}" "${NOLFS[@]}" checkout -f -q "${PIN}"
fi
echo "[fetch] his2trans @ $(git -C "${DEST}" rev-parse HEAD)"
[ "$(git -C "${DEST}" rev-parse HEAD)" = "${PIN}" ] || { echo "[fetch] ERROR: HEAD != pinned commit ${PIN}" >&2; exit 1; }
