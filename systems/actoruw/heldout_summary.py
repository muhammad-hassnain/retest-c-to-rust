"""Summarise the held-out measurement (pure JSON; runs on any host).

For executables: overlap of our inputs with the recorded system inputs, per translation and in
total, and for every counted exe defect whether it diverges on at least one input OUTSIDE the
recorded set (i.e. neither an exact match nor a possible match with a truncated record). For
libraries: the system's inputs are not recorded (only the vector count per case), so held-out is
"not measurable". Also summarises the replay of the system's own inputs through our comparison
(heldout.json) and, if present, the UB filter over the replay divergences.

Usage: python3 -I heldout_summary.py <out_dir>   (reads heldout.json, difftest.json, ub_filter.json,
       [ub_filter_replay.json]; writes heldout_summary.json and heldout_replay_difftest.json)
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import defects as D  # noqa: E402


def main():
    od = sys.argv[1]
    ho = json.load(open(os.path.join(od, "heldout.json")))
    dt = json.load(open(os.path.join(od, "difftest.json")))
    ub = json.load(open(os.path.join(od, "ub_filter.json")))
    counted = {(p["battery"], p["case"], v["id"]) for p in ub["programs"] for v in p["verdicts"] if v["counted"]}
    hp = {(p["battery"], p["case"]): p for p in ho["programs"]}
    S = {"definition": "an input is held out if it is not among the inputs the system's own check ran "
                       "(the TRACTOR vectors, recorded in results/Test-Corpus/kiro/<battery>/test.log)",
         "exe": {}, "lib": {}}
    tot_ours = sum(p["our_inputs"] for p in ho["programs"])
    tot_exact = sum(p["our_inputs_exactly_recorded"] for p in ho["programs"])
    tot_poss = sum(p["our_inputs_possibly_recorded_truncated"] for p in ho["programs"])
    S["exe"]["recorded_vectors"] = sum(p["recorded_vectors"] for p in ho["programs"])
    S["exe"]["recorded_vectors_truncated_stdin"] = sum(p["recorded_truncated"] for p in ho["programs"])
    S["exe"]["our_inputs"] = tot_ours
    S["exe"]["our_inputs_exactly_recorded"] = tot_exact
    S["exe"]["our_inputs_possibly_recorded_truncated"] = tot_poss
    S["exe"]["overlap_fraction_upper_bound"] = (tot_exact + tot_poss) / tot_ours if tot_ours else None
    S["exe"]["per_translation"] = {f"{b}/{c}": {"recorded": p["recorded_vectors"], "ours": p["our_inputs"],
                                                "exact": p["our_inputs_exactly_recorded"],
                                                "possible": p["our_inputs_possibly_recorded_truncated"]}
                                   for (b, c), p in sorted(hp.items())}
    # per defect: diverges outside the recorded set?
    per_def = {}
    for p in dt["programs"]:
        h = hp.get((p["battery"], p["case"]))
        inside = set(h["exact_ids"]) | set(h["possible_ids"]) if h else set()
        for dv in p["divergent"]:
            if (p["battery"], p["case"], dv["input"]["id"]) not in counted:
                continue
            m = D.mechanism(p, dv)
            k = f"{p['battery']}/{p['case']}::{m}"
            d = per_def.setdefault(k, {"cases": 0, "cases_outside_recorded_set": 0, "cases_inside_recorded_set": 0})
            d["cases"] += 1
            if dv["input"]["id"] in inside:
                d["cases_inside_recorded_set"] += 1
            else:
                d["cases_outside_recorded_set"] += 1
    for k, d in per_def.items():
        d["diverges_on_held_out_input"] = d["cases_outside_recorded_set"] > 0
    S["exe"]["defects"] = per_def
    S["exe"]["defects_diverging_on_held_out_input"] = sum(1 for d in per_def.values() if d["diverges_on_held_out_input"])
    S["exe"]["defects_total"] = len(per_def)
    # replay of the system's own inputs
    rep = {"replayed_vectors": sum(p["replay_n"] for p in ho["programs"]),
           "replay_divergent": sum(len(p["replay_divergent"]) for p in ho["programs"]),
           "per_translation_divergent": {f"{p['battery']}/{p['case']}": len(p["replay_divergent"])
                                         for p in ho["programs"] if p["replay_divergent"]}}
    # write a difftest-shaped file of the replay divergences, so the SAME ub_filter.py can check them
    rd = {"programs": [{"battery": p["battery"], "case": p["case"], "divergent": [
        {k: v for k, v in r.items() if k in ("input", "class", "c", "rust", "c_reproducible", "channels")}
        for r in p["replay_divergent"]]} for p in ho["programs"] if p["replay_divergent"]]}
    json.dump(rd, open(os.path.join(od, "heldout_replay_difftest.json"), "w"), indent=1)
    ubr_p = os.path.join(od, "ub_filter_replay.json")
    if os.path.isfile(ubr_p):
        ubr = json.load(open(ubr_p))
        rep["replay_divergent_ub_clean"] = sum(1 for p in ubr["programs"] for v in p["verdicts"] if v["counted"])
        rep["replay_divergent_ub_clean_by_translation"] = {
            f"{p['battery']}/{p['case']}": sum(1 for v in p["verdicts"] if v["counted"]) for p in ubr["programs"]}
    S["exe"]["system_inputs_through_our_comparison"] = rep
    S["lib"] = {"status": "not measurable",
                "why": "library vectors are JSON files in the non-public test-corpus submodule; test.log records only "
                       "that they ran ('[test] Running [runner, lib, -c, testNN.json]'), not their contents",
                "vectors_recorded_without_contents": ho.get("library_vectors_recorded_without_contents")}
    json.dump(S, open(os.path.join(od, "heldout_summary.json"), "w"), indent=1)
    print(json.dumps({k: v for k, v in S["exe"].items() if k not in ("per_translation", "defects")}, indent=1))


if __name__ == "__main__":
    main()
