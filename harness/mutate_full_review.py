"""Hand review of a seeded sample of REACHED survivors from harness/mutate_full.py.

`list` draws, per util, a seeded sample of K survivors whose mutated token the full grid executes
(survivor_class reached-not-detected: not tce, coverage count > 0), excluding any already killed
by the uncapped grid (mutate_full_extgrid.py; those are non-equivalent by witness), and prints
each with source context so a person can judge it. Judgments are recorded by hand in
harness/mutate_full_review.json as {id: {"verdict": "equivalent" | "non-equivalent" | "undecided",
"reason": "..."}}; `mutate_full.py summarize` counts them into results/cli_mutation_full.json.

  python3 harness/mutate_full_review.py list --build DIR --src-root ARTIFACT_SRC [-k 8]
"""
import argparse
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mutate_full import SEED, UTILS  # noqa: E402

K_DEFAULT = 6


def sample(build, k=K_DEFAULT):
    sites = json.load(open(os.path.join(build, "sites.json")))
    byid = {m["id"]: m for u in UTILS for m in sites["mutants"][u]}
    rows = {}
    for ln in open(os.path.join(build, "mutants.jsonl")):
        r = json.loads(ln)
        rows[r["id"]] = r
    ext = {}
    ep = os.path.join(build, "extgrid.json")
    if os.path.exists(ep):
        for u, e in json.load(open(ep))["utils"].items():
            for x in e["results"]:
                ext[x["id"]] = x["status"]
    out = {}
    for u in UTILS:
        pool = sorted(i for i, r in rows.items() if i in byid and byid[i]["util"] == u
                      and byid[i]["op"] in ("rel", "arith", "const")
                      and r["status"] == "survived" and not r.get("tce")
                      and (byid[i]["site_count"] or 0) > 0
                      and ext.get(i) not in ("killed_s1", "killed_crash"))
        rng = random.Random(f"{SEED}:review:{u}")
        out[u] = sorted(rng.sample(pool, min(k, len(pool))))
    return out, byid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["list"])
    ap.add_argument("--build", required=True)
    ap.add_argument("--src-root", required=True, help=".../coreutils/src")
    ap.add_argument("-k", type=int, default=K_DEFAULT)
    a = ap.parse_args()
    sel, byid = sample(a.build, a.k)
    for u, ids in sel.items():
        lines = open(os.path.join(a.src_root, u, "rust", "src", f"{u}.rs")).read().split("\n")
        for i in ids:
            m = byid[i]
            print(f"=== {i}  fn={m['fn']}  {m['old']} -> {m['new']}  line {m['line']}:{m['col']}  "
                  f"site_count={m['site_count']} ({m['site_count_method']}) fn_count={m['fn_count']}")
            lo, hi = max(0, m["line"] - 7), min(len(lines), m["line"] + 5)
            for n in range(lo, hi):
                mark = ">>" if n + 1 == m["line"] else "  "
                print(f"{mark}{n + 1:5d} {lines[n]}")


if __name__ == "__main__":
    main()
