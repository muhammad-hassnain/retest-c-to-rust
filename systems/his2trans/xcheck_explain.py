"""Explain the cases where the toolchain/profile cross-check (xcheck.py) saw different raw output:
run the C reference and the two Rust builds on each case and compare them with run.py's tolerant
comparison (the study's float rule: NaN == NaN), and count NaN bit patterns.

  python3 systems/his2trans/xcheck_explain.py <case-id>... --bin c=<path> --bin rel=<path> --bin dbg=<path> --out <json>
"""
import argparse
import collections
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run as R  # noqa: E402
import gen_inputs  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="+")
    ap.add_argument("--bin", action="append", required=True, help="name=path")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    bins = dict(b.split("=", 1) for b in a.bin)
    cases = {c["id"]: c for c in gen_inputs.all_cases()}
    res = []
    for cid in a.ids:
        data = ("\n".join(cases[cid]["lines"]) + "\n").encode()
        outs = {n: R.run_bin(b, data, False) for n, b in bins.items()}
        names = list(bins)
        row = {"id": cid, "rc": {n: o["rc"] for n, o in outs.items()},
               "nan_tokens": {n: dict(collections.Counter(
                   t.decode() for t in o["stdout"].split() if t.startswith((b"d:7ff", b"d:fff")))) for n, o in outs.items()},
               "pairs": {}}
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                x, y = outs[names[i]], outs[names[j]]
                row["pairs"][f"{names[i]}-{names[j]}"] = {
                    "raw_stdout_equal": x["stdout"] == y["stdout"],
                    "tolerant_first_diff_line": R.first_diff(x["stdout"], y["stdout"]),
                    "stderr_equal": x["stderr"] == y["stderr"]}
        res.append(row)
        print(row, flush=True)
    R.ou.dump(a.out, {"cases": res, "provenance": R.ou.provenance(__file__, {"bins": bins})})


if __name__ == "__main__":
    main()
