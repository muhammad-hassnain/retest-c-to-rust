"""Second-annotator inputs for ACTOR B02_synthetic/memmove (PARSE_NUM).

The inputs come from a blind second annotator's rating (docs/reviews/second-rater/ratings.json,
case 11 / item10: "a final byte of 5x makes it error out where the C reads the 5"). None of them
holds a negative number, so the contested-conversion rule (a negative number read by scanf %u) does
not apply. Each input is run the way the other ACTOR stages run it, on the binaries those stages built
(read-only): difftest.run_one on the C oracle (build.py, gcc) and ACTOR's Rust (cargo --release),
difftest.classify for the class, ub_filter.check_case on the ub_filter.py variants for the common UB
rule, and heldout.parse_log on the battery's test.log for membership in ACTOR's recorded inputs.

Usage (in the crust-heldout container, ACTORUW_W = a work root holding build/ and artifact/ from
actoruw.build and actoruw.ub):  python3 -I second_rater_memmove.py --out <file.json>
"""
import argparse
import base64
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import difftest  # noqa: E402
import heldout  # noqa: E402
import oracle_ub_common as ou  # noqa: E402
import ub_filter as uf  # noqa: E402

BATTERY, CASE = "B02_synthetic", "memmove"
INPUTS = [("sr_5x_last", b"0\n0\n0\n1\n5x\n"),       # the annotator's example: final byte token 5x
          ("sr_5x_second", b"0\n0\n0\n2\n1 5x\n"),   # 5x as the second of two bytes
          ("sr_5x_rotate", b"1\n1\n0\n3\n1 2 5x\n")]  # flags=1 (rotate) so the buffer is processed


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    bdir = os.path.join(common.W, "build", BATTERY, CASE)
    cbin, rbin = os.path.join(bdir, "c", "driver"), os.path.join(bdir, "target", "release", "driver")
    bins = {n: os.path.join(bdir, "ubvar", n) for n in uf.VARIANTS}
    rec = heldout.parse_log(os.path.join(common.KIRO, BATTERY, "test.log"))[0].get(CASE, [])
    exact = {(tuple(r["argv"]), r["stdin"]) for r in rec if not r["truncated"] and r["stdin"] is not None}
    trunc = [r for r in rec if r["truncated"]]
    rows = []
    for iid, stdin in INPUTS:
        x = {"id": iid, "argv": [], "stdin_b64": base64.b64encode(stdin).decode(), "files": {}, "dirs": [],
             "note": "second annotator (docs/reviews/second-rater/ratings.json, case 11)"}
        cr, rr = difftest.run_one(cbin, x), difftest.run_one(rbin, x)
        div = {"input": x, "class": difftest.classify(cr, rr), "c": difftest.enc(cr),
               "c_reproducible": difftest.same(cr, difftest.run_one(cbin, x))}
        v = uf.check_case(bins, div)
        key = (tuple(x["argv"]), stdin)
        rows.append({
            "id": iid, "argv": x["argv"], "stdin": stdin.decode(),
            "agree": difftest.same(cr, rr), "class": div["class"],
            "c": {"exit": cr["rc"], "stdout": cr["stdout"].decode("utf-8", "replace"), "stderr": cr["stderr"].decode("utf-8", "replace")},
            "rust": {"exit": rr["rc"], "stdout": rr["stdout"].decode("utf-8", "replace"), "stderr": rr["stderr"].decode("utf-8", "replace")},
            "c_reproducible": div["c_reproducible"],
            "ub_filter": {"counted": v["counted"], "ub_reasons": v["ub_reasons"], "variants": v["variants"]},
            "in_recorded_set": ("exact" if key in exact else
                                "possible_truncated" if any(tuple(r["argv"]) == key[0] and stdin.startswith(r["stdin"]) and len(stdin) > len(r["stdin"]) for r in trunc)
                                else "no")})
    out = {"system": "ACTOR", "translation": f"{BATTERY}/{CASE}", "mechanism": "PARSE_NUM",
           "source": "blind second annotator's rating: docs/reviews/second-rater/ratings.json, case 11 (item10); inputs built from its cause",
           "provenance": ou.provenance(__file__, {
               "script": "systems/actoruw/second_rater_memmove.py",
               "git_worktree_note": "systems/actoruw/*.py, systems/enumerate_inputs.py and harness/oracle_ub_common.py copied from the "
                                    "study repo working tree to the run host; binaries reused read-only from the actoruw work root",
               "binaries": {"c_oracle": os.path.relpath(cbin, common.W), "rust": os.path.relpath(rbin, common.W),
                            "ub_variants": sorted(bins), "built_by": "actoruw.build (c, rust) and actoruw.ub (ubvar); reused read-only",
                            "sha256": {k: sha(p) for k, p in [("c_oracle", cbin), ("rust", rbin)] + sorted(bins.items())}},
               "recorded_inputs": {"log": f"{BATTERY}/test.log", "vectors": len(rec), "truncated": len(trunc)}}),
           "inputs": rows,
           "summary": {"inputs": len(rows), "divergent": sum(not r["agree"] for r in rows),
                       "divergent_ub_clean": sum((not r["agree"]) and r["ub_filter"]["counted"] for r in rows)}}
    ou.dump(a.out, out)
    print(json.dumps(out["summary"]))


if __name__ == "__main__":
    main()
