#!/usr/bin/env bash
# Fetch ACTOR (UW; Schesch and Ernst, IEEE S&P 2026) at pinned commits (not vendored).
#   ACTOR    github.com/UW-HARVEST/ACTOR (MIT)                         pin 1b1e70a (2026-08-19)
#   results  github.com/UW-HARVEST/harvest-agentic-translate-results   pin e31aec03 (the submodule
#            commit that ACTOR 1b1e70a records; no license file upstream)
# Why 1b1e70a: it is the LAST ACTOR commit whose generated tables/numbers.tex reproduces the
# paper's TRACTOR headline (ActorKiroTests 325/338, ActorClaudeTests 319/338, ActorCodexTests
# 244/338; true from 2026-07-07 to 2026-08-19). From 3a70820 (2026-08-21) on, the results were
# re-run with other models (HEAD 54daf8b: Kiro 332/338 with claude-opus-5); those are not the
# translations the paper reports.
# The results repository is ~9 GB at HEAD (an agent-invocation cache); we fetch the one commit
# blob-less and check out only Test-Corpus/kiro/ (the ACTOR (Kiro) translations, without agent
# logs) plus the README.
# The test-corpus submodule (github.com/UW-HARVEST/Test-Corpus, pin 6fefd73) is NOT public:
# `git ls-remote` answers "Repository not found" (checked 2026-11-13). It holds the TRACTOR test
# vectors; see heldout.py for what the released results record of them.
set -euo pipefail
ACTOR_PIN="1b1e70ac84e10ca73efa7b8a8b9e5d6f6ab8b885"
RESULTS_PIN="e31aec03eb21dde31c399b64d49b806c6470bdf4"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST="${HERE}/artifacts/actoruw"
mkdir -p "${DEST}"
if [ ! -d "${DEST}/ACTOR/.git" ]; then
  git clone -q https://github.com/UW-HARVEST/ACTOR.git "${DEST}/ACTOR"
fi
git -C "${DEST}/ACTOR" checkout -q "${ACTOR_PIN}"
[ "$(git -C "${DEST}/ACTOR" rev-parse HEAD)" = "${ACTOR_PIN}" ] || { echo "[fetch] ACTOR != pin" >&2; exit 1; }
[ "$(git -C "${DEST}/ACTOR" ls-tree HEAD results | awk '{print $3}')" = "${RESULTS_PIN}" ] || {
  echo "[fetch] ACTOR ${ACTOR_PIN} does not record results ${RESULTS_PIN}" >&2; exit 1; }
if [ ! -d "${DEST}/results/.git" ]; then
  git init -q "${DEST}/results"
  git -C "${DEST}/results" remote add origin https://github.com/UW-HARVEST/harvest-agentic-translate-results.git
  git -C "${DEST}/results" fetch -q --depth 1 --filter=blob:none origin "${RESULTS_PIN}"
  git -C "${DEST}/results" sparse-checkout init --no-cone
  printf '%s\n' '/README.md' '/Test-Corpus/kiro/' '!/Test-Corpus/kiro/*/*/*/logs/' \
    '/Test-Corpus/kiro-translate/*/summary.json' > "${DEST}/results/.git/info/sparse-checkout"
  git -C "${DEST}/results" checkout -q FETCH_HEAD
fi
[ "$(git -C "${DEST}/results" rev-parse HEAD)" = "${RESULTS_PIN}" ] || { echo "[fetch] results != pin" >&2; exit 1; }
echo "[fetch] ACTOR @ ${ACTOR_PIN}; results @ ${RESULTS_PIN} (sparse: Test-Corpus/kiro)"
# Source digest of what we grade (sha256 over relpath+sha256 of every source file; see
# harness/oracle_ub_common.py tree_digest):
python3 -I "${HERE}/harness/oracle_ub_common.py" digest "${DEST}/results/Test-Corpus/kiro"
