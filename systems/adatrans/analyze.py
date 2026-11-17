"""Aggregate run_diff.py case records into per-translation results (reads JSON only).

  python3 -I systems/adatrans/analyze.py --cases <cases_run1_1.86.0.jsonl> --manifest <extracted/manifest.json> \
      --run run1 --out <diff_run1.json>

A case is COUNTED when it diverges (kind S1 / S2 / panic / crash / hang) and the C passes the UB rule
(c_ub_clean). whitespace_only = stdout equal after whitespace folding (cosmetic, not counted; the
artifact's own check folds whitespace too). Divergent cases whose C fails the UB rule are ub_excluded.
"""
import argparse
import collections
import json

COUNTED = ("S1", "S2", "panic", "crash", "hang")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--run", default="run1")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    man = json.load(open(a.manifest))["runs"][a.run]
    per = collections.OrderedDict()
    for line in open(a.cases):
        r = json.loads(line)
        t = per.setdefault(r["inst"], {"cases": collections.Counter(), "kinds": collections.Counter(),
                                        "counted": collections.Counter(), "ub_excluded": collections.Counter(),
                                        "own_check": {"examples": [0, 0], "fuzz": [0, 0]},
                                        "c_matches_recorded": {"examples": [0, 0], "fuzz": [0, 0]},
                                        "divergent": []})
        t["cases"][r["src"]] += 1
        t["kinds"][f'{r["src"]}:{r["kind"]}'] += 1
        if "rust_passes_own_check" in r:
            oc = t["own_check"][r["src"]]
            oc[0] += bool(r["rust_passes_own_check"])
            oc[1] += 1
            cm = t["c_matches_recorded"][r["src"]]
            cm[0] += bool(r["c_matches_recorded"])
            cm[1] += 1
        if r["kind"] in COUNTED or r["kind"] == "whitespace_only":
            if r["kind"] in COUNTED:
                if r.get("c_ub_clean"):
                    t["counted"][f'{r["src"]}:{r["kind"]}'] += 1
                else:
                    t["ub_excluded"][f'{r["src"]}:{r["kind"]}'] += 1
            t["divergent"].append({k: r.get(k) for k in ("src", "idx", "kind", "channels", "c_ub_clean",
                                                          "c_ub_evidence", "c", "rust", "input_text", "input_sha",
                                                          "input_len")})
    out = {"run": a.run, "translations": {}}
    agg = collections.Counter()
    for inst, t in per.items():
        v = man[inst]
        counted_total = sum(t["counted"].values())
        rec = {
            "loop_status": v["loop_status"], "fuzz_status": v["fuzz_status"], "solved": v["solved"],
            "code_sha256": v["code_sha256"],
            "cases": dict(t["cases"]), "kinds": dict(t["kinds"]),
            "counted": dict(t["counted"]), "ub_excluded": dict(t["ub_excluded"]),
            "counted_on_ours": sum(n for k, n in t["counted"].items() if k.startswith("ours:")),
            "counted_on_system_inputs": sum(n for k, n in t["counted"].items() if not k.startswith("ours:")),
            "own_check_rerun": {k: {"pass": x[0], "of": x[1]} for k, x in t["own_check"].items()},
            "c_matches_recorded_expected": {k: {"match": x[0], "of": x[1]} for k, x in t["c_matches_recorded"].items()},
            "rust_build_fail": t["kinds"].get("ours:rust_build_fail", 0) > 0,
            "divergent_examples": sorted(t["divergent"], key=lambda d: (d["src"] != "ours", not d.get("c_ub_clean"), d.get("input_len") or 0))[:12],
            "divergent_total": len(t["divergent"]),
        }
        out["translations"][inst] = rec
        agg["translations"] += 1
        agg["rust_build_fail"] += rec["rust_build_fail"]
        agg["with_counted_divergence"] += counted_total > 0
        agg["with_counted_divergence_on_ours"] += rec["counted_on_ours"] > 0
        agg["with_whitespace_only"] += any(k.endswith("whitespace_only") for k in t["kinds"])
        agg["all_agree_on_ours"] = agg["all_agree_on_ours"] + (
            not rec["rust_build_fail"] and set(k for k in t["kinds"] if k.startswith("ours:")) <= {"ours:agree", "ours:c_timeout"})
        for k, n in t["counted"].items():
            agg["counted_cases:" + k] += n
        for k, n in t["ub_excluded"].items():
            agg["ub_excluded_cases:" + k] += n
        for k, n in t["kinds"].items():
            agg["kind:" + k] += n
    out["aggregate"] = dict(sorted(agg.items()))
    out["agreeing_on_all_ours"] = sorted(i for i, r in out["translations"].items()
                                         if not r["rust_build_fail"] and set(k for k in r["kinds"] if k.startswith("ours:")) <= {"ours:agree"})
    with open(a.out, "w") as fh:
        json.dump(out, fh, indent=1)
    print(json.dumps(out["aggregate"], indent=1))
    for inst, r in out["translations"].items():
        if r["counted"] or r["ub_excluded"] or r["rust_build_fail"] or any("whitespace" in k for k in r["kinds"]):
            print(inst, r["loop_status"], r["fuzz_status"], "counted", r["counted"], "ubx", r["ub_excluded"],
                  "build_fail" if r["rust_build_fail"] else "", {k: v for k, v in r["kinds"].items() if "whitespace" in k or "c_timeout" in k})


if __name__ == "__main__":
    main()
