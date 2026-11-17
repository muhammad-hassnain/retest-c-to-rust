"""Determinism check for pipeline.sh `check`: compare the first and the re-run outputs (reads files only).

  python3 -I systems/adatrans/compare_runs.py <work dir>
Compares byte for byte: the input tree (inputs/ vs inputs_recheck/), cases_run1.jsonl (diff stage),
cases_run1_ub.jsonl (UB-rule stage), diff_run1.json (analyze stage). For the case files it also compares
with the duration-derived field removed (DURATION_FIELDS: whether the 10 s CPU run of the Rust timed out
before its 60 s re-run), and lists the rows that differ. Writes <work>/build/determinism.json.
"""
import hashlib
import json
import os
import sys

DURATION_FIELDS = ("rust_rerun_long_timeout",)


def h(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def tree(d):
    m = hashlib.sha256()
    for dp, dn, fn in sorted(os.walk(d)):
        dn.sort()
        for f in sorted(fn):
            p = os.path.join(dp, f)
            m.update(os.path.relpath(p, d).encode() + b"\0" + h(p).encode() + b"\n")
    return m.hexdigest()


def rows(p, drop=()):
    out = []
    for x in open(p):
        r = json.loads(x)
        for k in drop:
            r.pop(k, None)
        out.append(r)
    return out


def main():
    W = sys.argv[1]
    B = os.path.join(W, "build")
    pairs = {"inputs (tree)": (tree(os.path.join(W, "inputs")), tree(os.path.join(W, "inputs_recheck"))),
             "cases_run1.jsonl (diff stage)": (h(f"{B}/cases_run1.jsonl"), h(f"{B}/cases_run1_recheck.jsonl")),
             "cases_run1_ub.jsonl (UB-rule stage)": (h(f"{B}/cases_run1_ub.jsonl"), h(f"{B}/cases_run1_recheck_ub.jsonl")),
             "diff_run1.json (analyze stage)": (h(f"{B}/diff_run1.json"), h(f"{B}/diff_run1_recheck.json"))}
    res = {k: {"first": a, "second": b, "identical": a == b} for k, (a, b) in pairs.items()}
    for name, (f1, f2) in {"cases_run1.jsonl (diff stage)": ("cases_run1.jsonl", "cases_run1_recheck.jsonl"),
                           "cases_run1_ub.jsonl (UB-rule stage)": ("cases_run1_ub.jsonl", "cases_run1_recheck_ub.jsonl")}.items():
        A, Bv = rows(f"{B}/{f1}"), rows(f"{B}/{f2}")
        diff = [(a["inst"], a["src"], a["idx"], sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k)))
                for a, b in zip(A, Bv) if a != b]
        A2, B2 = rows(f"{B}/{f1}", DURATION_FIELDS), rows(f"{B}/{f2}", DURATION_FIELDS)
        res[name].update({"rows": len(A), "rows_differing": len(diff) + abs(len(A) - len(Bv)),
                          "differences": diff[:50],
                          "identical_ignoring_duration_fields": A2 == B2,
                          "duration_fields_ignored": list(DURATION_FIELDS)})
    json.dump(res, open(f"{B}/determinism.json", "w"), indent=1, sort_keys=True)
    print(json.dumps({k: {kk: v[kk] for kk in ("identical", "identical_ignoring_duration_fields", "rows_differing") if kk in v}
                      for k, v in res.items()}, indent=1))


if __name__ == "__main__":
    main()
