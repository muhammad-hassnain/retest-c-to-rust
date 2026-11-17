#!/bin/bash
# VERT scalar re-test on the opt-in v2 input grid (systems/enumerate_inputs.py grid="v2": per-argument
# v1 grid + powers of two +-1 + range ends, no one-argument truncation). Same harness, image and
# arch as stage vert.diff; only --grid v2 and --jobs differ. Writes a NEW file; never vert_diff.json.
#   bash systems/vert/run_grid2.sh [CPUS=6] [OUT=results/vert_diff_grid2.json]
set -euo pipefail
CPUS=${1:-6}; OUT=${2:-results/vert_diff_grid2.json}; IMG=crust-stable
REPO=$(cd "$(dirname "$0")/../.." && pwd)
cd "$REPO"
CMD="python3 systems/vert/run.py -n 80 --grid v2 --jobs $CPUS --out $OUT"
T0=$(date -u +%Y-%m-%dT%H:%M:%SZ); S0=$(date +%s)
docker run --rm --cpus "$CPUS" -v "$REPO:/work" -w /work "$IMG" $CMD
S1=$(date +%s); T1=$(date -u +%Y-%m-%dT%H:%M:%SZ)
VERS=$(docker run --rm "$IMG" sh -c 'rustc --version; gcc --version | head -1; python3 --version; uname -m')
GIT_REV=$(git rev-parse HEAD) IMG_ID=$(docker image inspect "$IMG" --format '{{.Id}}') \
DOCKER_CMD="docker run --rm --cpus $CPUS -v \$PWD:/work -w /work $IMG $CMD" \
T0=$T0 T1=$T1 WALL=$((S1 - S0)) VERS="$VERS" HOST_DESC="$(uname -sm), docker --cpus $CPUS" \
python3 - "$OUT" <<'PY'
import json, os, sys
p = sys.argv[1]; d = json.load(open(p)); e = os.environ
v = e["VERS"].splitlines()
d = {"provenance": {
        "script": "systems/vert/run.py (driver systems/vert/run_grid2.sh)",
        "command": e["DOCKER_CMD"], "image": "crust-stable " + e["IMG_ID"],
        "git_rev": e["GIT_REV"],
        "git_dirty_note": "uncommitted working tree (parallel agents); grid v2 lives in the uncommitted systems/enumerate_inputs.py",
        "host": e["HOST_DESC"], "arch": v[3],
        "toolchains": {"rustc": v[0], "gcc": v[1], "python": v[2]},
        "date_start_utc": e["T0"], "date_end_utc": e["T1"], "wall_clock_seconds": int(e["WALL"])},
     **d}
json.dump(d, open(p, "w"), indent=2)
print("provenance added; wall-clock", e["WALL"], "s")
PY
