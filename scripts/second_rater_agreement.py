#!/usr/bin/env python3
"""Compare the blind second annotator's labels with the first annotator's key.

Inputs:  results/second_rater_key.json (the first annotator's labels for the 12 rated items, as they
         stood when the sample was drawn, i.e. before the memmove change), and
         docs/reviews/second-rater/ratings.json + cases-spec.json (the second annotator's answers).
Output:  results/second_rater_agreement.json (ratings copied in, since docs/reviews is not in the
         anonymized artifact).

  python3 -I scripts/second_rater_agreement.py                  # recompute from results/second_rater_key.json
  python3 -I scripts/second_rater_agreement.py --key KEY.json   # first run: copy the 12 rated rows of the full key
"""
import json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REVIEW = "docs/reviews/second-rater"
KEY_OUT = "results/second_rater_key.json"
OUT = "results/second_rater_agreement.json"
CLS = {"outright_error": "outright", "edge_input_robustness": "edge", "c_bug_not_copied": "c_bug"}


def load(rel):
    with open(os.path.join(ROOT, rel)) as f:
        return json.load(f)


def dump(rel, obj):
    with open(os.path.join(ROOT, rel), "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")


def main():
    spec = load(f"{REVIEW}/cases-spec.json")
    ratings = load(f"{REVIEW}/ratings.json")
    items = sorted(c["id"] for c in spec)
    if "--key" in sys.argv:
        full = {r["item"]: r for r in json.load(open(sys.argv[sys.argv.index("--key") + 1]))}
        dump(KEY_OUT, {"source": "first annotator's blinded item->defect key (labels before the memmove change); "
                                 "revealed after rating was complete",
                       "rows": [{k: full[i][k] for k in ("item", "defect_id", "count_status", "class")} for i in items]})
    key = {r["item"]: r for r in load(KEY_OUT)["rows"]}
    assert sorted(key) == items == sorted(ratings), (sorted(key), items, sorted(ratings))
    rows = []
    for i in items:
        k, r = key[i], ratings[i]
        first_counted = k["count_status"] == "counted"
        second_counted = r["counted"] == "yes"
        both = first_counted and second_counted
        rows.append({"item": i, "case": r["case"], "defect_id": k["defect_id"],
                     "first": {"count_status": k["count_status"], "counted": first_counted, "class": CLS.get(k["class"])},
                     "second": {"counted": second_counted, "class": r["cls"] or None, "rule": r["rule"] or None, "cause": r["cause"]},
                     "count_agrees": first_counted == second_counted,
                     "class_agrees": (CLS.get(k["class"]) == r["cls"]) if both else None})
    counted = [x for x in rows if x["first"]["counted"]]
    both = [x for x in rows if x["class_agrees"] is not None]
    out = {
        "inputs": [KEY_OUT, f"{REVIEW}/ratings.json", f"{REVIEW}/cases-spec.json"],
        "class_map": CLS,
        "summary": {
            "cases": len(rows),
            "first_counted": len(counted),
            "first_excluded": len(rows) - len(counted),
            "count_agree": sum(x["count_agrees"] for x in rows),
            "count_differ_items": [x["item"] for x in rows if not x["count_agrees"]],
            "both_counted": len(both),
            "class_agree": sum(x["class_agrees"] for x in both),
            "class_differ_items": [x["item"] for x in both if not x["class_agrees"]],
        },
        # Reviewed, not computed: whether the second annotator's stated cause is the first annotator's mechanism.
        "same_cause_reviewed": {
            "items": [x["item"] for x in counted],
            "same_cause": len(counted),
            "evidence": f"{REVIEW} (the verifier confirmed each stated cause by running the exposing input it names "
                        "against the C and the Rust); not string-matched",
        },
        "rows": rows,
        "ratings": ratings,
    }
    dump(OUT, out)
    print(json.dumps(out["summary"]))


if __name__ == "__main__":
    main()
