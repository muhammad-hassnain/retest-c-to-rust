"""Held-out measurement for AdaTrans: overlap of our inputs with the inputs AdaTrans's own checks ran.

AdaTrans's checks (src/runner.py) read their inputs from the fuzz-output bundle (Zenodo 10.5281/
zenodo.22258998, adatrans-artifacts.tar.zst, dataset_fuzz_outputs/weekly_contest_<c>_p<p>/):
  examples  the 2-3 LeetCode examples, run by the generate-validate-repair LOOP (TestRunner.check)
  outputs   the fuzz oracle (10-200 cases), run once on the final program (validate_against_fuzz)
Both are parsed exactly as src/runner.py::_load_test_cases_from_file (run_diff.parse_cases), which
strips each input; the check feeds that stripped string to the binary.

Two notions of "same input": exact bytes after the same strip(), and token-equal (whitespace folded;
every program reads its input with scanf, which skips whitespace, so token-equal inputs are the
same input to the program).
Reads data files only.
  python3 -I systems/adatrans/heldout.py --inputs /inputs --fuzz /fuzz --out /b/heldout.json
"""
import argparse
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_diff as rd  # noqa: E402


def h(b):
    return hashlib.sha256(b).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", default="/inputs")
    ap.add_argument("--fuzz", default="/fuzz")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    per = {}
    tot = {"ours": 0, "examples": 0, "fuzz": 0, "ours_in_check_exact": 0, "ours_in_check_tokens": 0,
           "programs_with_check_inputs": 0}
    for i in rd.insts():
        inst = "C_" + i
        c, p = i.split("_")
        d = os.path.join(a.inputs, inst)
        names = sorted(os.listdir(d)) if os.path.isdir(d) else []
        ours = [open(os.path.join(d, f), "rb").read() for f in names]
        fd = os.path.join(a.fuzz, f"weekly_contest_{c}_p{p}")
        ex = [x["input"].encode() for x in rd.parse_cases(os.path.join(fd, "examples"))]
        fz = [x["input"].encode() for x in rd.parse_cases(os.path.join(fd, "outputs"))]
        chk_exact = {h(x) for x in ex + fz}
        chk_tok = {h(rd.norm(x)) for x in ex + fz}
        ie = sum(1 for x in ours if h(x.strip()) in chk_exact)
        it = sum(1 for x in ours if h(rd.norm(x)) in chk_tok)
        per[inst] = {"ours": len(ours), "examples": len(ex), "fuzz": len(fz),
                     "fuzz_unique_tokens": len({h(rd.norm(x)) for x in fz}),
                     "ours_in_check_exact": ie, "ours_in_check_tokens": it,
                     "ours_in_check_files": [f for f, x in zip(names, ours) if h(rd.norm(x)) in chk_tok],
                     "examples_in_fuzz_tokens": sum(1 for x in ex if h(rd.norm(x)) in {h(rd.norm(y)) for y in fz}),
                     "fuzz_files_present": os.path.isfile(os.path.join(fd, "outputs")),
                     "examples_file_present": os.path.isfile(os.path.join(fd, "examples"))}
        for k in ("ours", "examples", "fuzz", "ours_in_check_exact", "ours_in_check_tokens"):
            tot[k] += per[inst][k]
        tot["programs_with_check_inputs"] += bool(ex or fz)
    tot["examples_also_in_fuzz_tokens"] = sum(v["examples_in_fuzz_tokens"] for v in per.values())
    out = {"totals": tot, "per_program": per}
    with open(a.out, "w") as fh:
        json.dump(out, fh, indent=1)
    print(json.dumps(tot, indent=1))


if __name__ == "__main__":
    main()
