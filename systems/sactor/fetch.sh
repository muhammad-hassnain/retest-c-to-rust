#!/usr/bin/env bash
# Fetch SACTOR's public repositories at pinned commits (not vendored). Untrusted: nothing under
# artifacts/sactor is built or run on the host; all builds/runs happen in Docker (see run_docker.sh).
#   sactor           github.com/qsdrqs/sactor           (tool; Apache-2.0)
#   sactor-datasets  github.com/qsdrqs/sactor-datasets  (libogg C + SACTOR's libogg Rust; no license file)
#   CRUST-bench      github.com/qsdrqs/CRUST-bench      (fork with SACTOR results, 69970f9c; GPL-3.0)
# Prints, per repo, HEAD and sha256 of `git archive --format=tar HEAD` (content digest).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST_ROOT="${SACTOR_ART:-${HERE}/artifacts/sactor}"
mkdir -p "${DEST_ROOT}"
NOHOOK=(-c core.hooksPath=/dev/null -c filter.lfs.smudge= -c filter.lfs.process= -c filter.lfs.required=false)
fetch() {  # name url pin
  local name="$1" url="$2" pin="$3" d="${DEST_ROOT}/$1"
  if [ ! -d "$d/.git" ]; then
    git "${NOHOOK[@]}" clone --no-checkout "$url" "$d"
  fi
  git -C "$d" "${NOHOOK[@]}" cat-file -e "${pin}^{commit}" 2>/dev/null || git -C "$d" "${NOHOOK[@]}" fetch -q origin "$pin"
  git -C "$d" "${NOHOOK[@]}" checkout -f -q "$pin"
  [ "$(git -C "$d" rev-parse HEAD)" = "$pin" ] || { echo "[fetch] ERROR: $name HEAD != $pin" >&2; exit 1; }
  local sha; sha=$(git -C "$d" archive --format=tar HEAD | shasum -a 256 2>/dev/null | cut -d' ' -f1 || git -C "$d" archive --format=tar HEAD | sha256sum | cut -d' ' -f1)
  echo "[fetch] $name @ $pin  git-archive-sha256=$sha  date=$(git -C "$d" log -1 --format=%cI)"
}
fetch sactor          https://github.com/qsdrqs/sactor.git          577c3d2b8074d5fed7c47b988c0acd175e498f98
fetch sactor-datasets https://github.com/qsdrqs/sactor-datasets.git 1060b3b27d151e2d4919dd2db69abcfa91153399
fetch CRUST-bench     https://github.com/qsdrqs/CRUST-bench.git     69970f9c9a583290f943145eada5e8948b1b91ff
