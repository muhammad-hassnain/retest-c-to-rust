#!/usr/bin/env bash
# Fetch Flourine's artifact tarball (not vendored). It is NOT on GitHub; the paper distributes a
# CloudFront tarball (Eniser et al., arXiv 2405.11514). No commit to pin; we pin the URL and the
# tarball's sha256 (the tarball our results were produced from, fetched 2026-10-25, 54,106,065 bytes),
# and verify the expected layout after extraction. A tarball whose checksum differs is rejected.
set -euo pipefail
URL="https://d34gtk3knhjgeg.cloudfront.net/artifact.tar.gz"
SHA256="87e570d4ee6dabd6905d6573f8da10d1645051e521d5ee9d6a19704cd87f0a32"
sha256(){ if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
verify(){ local got; got="$(sha256 "$1")"
  if [ "$got" != "${SHA256}" ]; then echo "[fetch] ERROR: $1 sha256 ${got} != pinned ${SHA256}" >&2; exit 1; fi
  echo "[fetch] sha256 OK (${SHA256})"; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST="${HERE}/artifacts/flourine_dl"
mkdir -p "${DEST}"
if [ -d "${DEST}/flourine-artifact/bms" ]; then
  echo "[fetch] flourine present at ${DEST}/flourine-artifact"
else
  echo "[fetch] downloading ${URL}"
  curl -fsSL -o "${DEST}/artifact.tar.gz" "${URL}"
  verify "${DEST}/artifact.tar.gz"
  tar xzf "${DEST}/artifact.tar.gz" -C "${DEST}"
fi
if [ -f "${DEST}/artifact.tar.gz" ]; then verify "${DEST}/artifact.tar.gz"; fi
echo "[fetch] C benchmarks: $(find "${DEST}/flourine-artifact/bms/c" -name '*.c' 2>/dev/null | wc -l | tr -d ' ')"
