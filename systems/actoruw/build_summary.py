"""Assemble results/actoruw_*.json from the run outputs (pure JSON processing; runs on any host).

Inputs: a directory holding the container outputs (build.json, difftest.json, ub_filter.json,
lib_scan.json, lib_build.json, lib_difftest.json, ub_filter_lib.json, heldout.json,
tc_crosscheck_{exe,lib}.json; results/actoruw_mutation.json, results/harness_power.json) and the artifact tree (for result.json /
summary.json records). Writes results/actoruw_summary.json and copies each input to
results/actoruw_<name>.json with a provenance object.

Usage: python3 -I systems/actoruw/build_summary.py <out_dir> <kiro_dir> <results_dir> <provenance.json>
"""
import base64
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import defects as D  # noqa: E402

SEV_ORDER = ["rust_crash", "S1", "S2", "S3_hang"]


def B(s):
    return base64.b64decode(s)


KILL_RULE_TEXT = ("a mutant is killed if OUR harness, on OUR inputs, reports a wrong answer (value, stdout, exit code or "
                  "output file) or a crash; not counted: stderr-only differences, hangs/timeouts, kills by the system's own test")


def load(d, n):
    p = os.path.join(d, n)
    return json.load(open(p)) if os.path.isfile(p) else None


def recorded(kiro, translation):
    b, c = translation.split("/")
    for ph in ("verified", "translated"):
        p = os.path.join(kiro, b, c, ph, "result.json")
        if os.path.isfile(p):
            r = json.load(open(p))
            return ph, os.path.relpath(p, os.path.dirname(os.path.dirname(kiro))), r
    return None, None, None


def acceptance(kiro, translation):
    ph, path, r = recorded(kiro, translation)
    if r is None:
        return "no record", {"note": "no result.json in verified/ or translated/"}
    ev = {"record": f"results/{path}", "passed": r.get("passed"), "vectors_failed": r.get("vectors_failed"),
          "phase": ph}
    if r.get("error"):
        ev["error"] = r["error"]
    if r.get("failures"):
        ev["first_failures"] = [{k: f.get(k) for k in ("vector", "reason", "expected_rc", "actual_rc")}
                                | {"diff_head": (f.get("diff") or "")[:240]} for f in r["failures"][:2]]
        ev["n_failures"] = len(r["failures"])
    return ("accepted" if r.get("passed") is True else "rejected"), ev


def defect_rows(dt, ub, kind, suar=None):
    """Counted case -> defect, using the hand-confirmed mechanism rules of defects.py. Cases where
    the supplementary stack-use-after-return check fired (suar_check.py) are excluded as C UB."""
    fired = {(r["translation"], r["id"]) for r in (suar or {}).get("fired", [])}
    counted = {}
    for p in ub["programs"]:
        for v in p["verdicts"]:
            if (f"{p['battery']}/{p['case']}", v["id"]) in fired:
                continue
            counted[(p["battery"], p["case"], v["id"])] = v
    rows = {}
    for p in dt["programs"]:
        for dv in p["divergent"]:
            v = counted.get((p["battery"], p["case"], dv["input"]["id"]))
            if not v or not v["counted"]:
                continue
            m = D.mechanism(p, dv) if kind == "exe" else D.lib_mechanism(p, dv)
            key = (p["battery"], p["case"], m)
            r = rows.setdefault(key, {"translation": f"{p['battery']}/{p['case']}", "kind": kind, "mechanism": m,
                                      "location": D.LOCATIONS.get((p["case"], m), "UNCONFIRMED"),
                                      "classes": {}, "exposure_cases": 0, "case_ids": [], "examples": []})
            r["exposure_cases"] += 1
            r["case_ids"].append(dv["input"]["id"])
            r["classes"][dv["class"]] = r["classes"].get(dv["class"], 0) + 1
            if len(r["examples"]) < 2:
                r["examples"].append({
                    "input": {"argv": dv["input"]["argv"], "stdin": B(dv["input"]["stdin_b64"])[:200].decode("latin-1"),
                              **({"function": dv.get("function")} if kind == "lib" else {})},
                    "class": dv["class"], "channels": dv["channels"],
                    "c": {"exit": dv["c"]["rc"], "stdout": B(dv["c"]["stdout_b64"])[:400].decode("utf-8", "replace"),
                          "stderr": B(dv["c"]["stderr_b64"])[:200].decode("utf-8", "replace")},
                    "rust": {"exit": dv["rust"]["rc"], "stdout": B(dv["rust"]["stdout_b64"])[:400].decode("utf-8", "replace"),
                             "stderr": B(dv["rust"]["stderr_b64"])[:300].decode("utf-8", "replace")}})
    for r in rows.values():
        cl = r["classes"]
        r["severity"] = ("crash" if cl.get("rust_crash") else "S1" if cl.get("S1") else "S2" if cl.get("S2")
                         else "S3" if cl.get("S3_hang") else "?")
    return list(rows.values())


def ub_stats(dt, ub):
    tot = sum(len(p["divergent"]) for p in dt["programs"])
    exc = [(p["battery"], p["case"], v) for p in ub["programs"] for v in p["verdicts"] if not v["counted"]]
    reasons = {}
    for _, _, v in exc:
        for r in v["ub_reasons"]:
            k = r.split(":")[0]
            reasons[k] = reasons.get(k, 0) + 1
    return {"divergent_cases": tot, "ub_excluded_cases": len(exc),
            "ub_excluded_by_check (a case can fail several)": reasons,
            "translations_with_only_ub_divergences": sorted({f"{b}/{c}" for b, c, _ in exc} -
                                                             {f"{p['battery']}/{p['case']}" for p in ub["programs"]
                                                              if any(v["counted"] for v in p["verdicts"])})}


def main():
    out_d, kiro, res_d, prov_p = sys.argv[1:5]
    prov = json.load(open(prov_p))
    S = {"system": "ACTOR (UW; Schesch and Ernst, IEEE S&P 2026)", "provenance": prov}
    bj, dt, ub = load(out_d, "build.json"), load(out_d, "difftest.json"), load(out_d, "ub_filter.json")
    ls, lb, ld, lu = (load(out_d, n) for n in ("lib_scan.json", "lib_build.json", "lib_difftest.json", "ub_filter_lib.json"))
    ho = load(out_d, "heldout.json")
    rows = defect_rows(dt, ub, "exe", load(out_d, "suar_exe.json"))
    if ld and lu:
        rows += defect_rows(ld, lu, "lib", load(out_d, "suar_lib.json"))
    hs = load(out_d, "heldout_summary.json") or {}
    for r in rows:
        r["acceptance_status"], r["acceptance_evidence"] = acceptance(kiro, r["translation"])
        if r["kind"] == "exe":
            h = hs.get("exe", {}).get("defects", {}).get(f"{r['translation']}::{r['mechanism']}")
            r["heldout"] = ({"status": "measured", **h} if h else {"status": "?"})
        else:
            r["heldout"] = {"status": "not measurable", "why": "library vectors not recorded in the artifact"}
        r["implementation_defined"] = r["mechanism"] in ("CTYPE_VALUE", "SHIFT_SIGN")
        r["toolchain_stable_rustc_1_99_1"] = True if r["translation"].split("/")[1] in {
            p["case"] for k in ("exe", "lib") for p in (load(out_d, f"tc_crosscheck_{k}.json") or {}).get("programs", [])
            if p["all_stable"]} else None
    exe_tested = [f"{p['battery']}/{p['case']}" for p in dt["programs"]]
    lib_tested = [f"{p['battery']}/{p['case']}" for p in (ld or {}).get("programs", [])]
    S["scope"] = {
        "artifact": "UW-HARVEST/ACTOR @1b1e70a, results @e31aec03 (paper's TRACTOR numbers: Kiro 325/338)",
        "graded": "ACTOR (Kiro) translations of the TRACTOR Test-Corpus; graded dir = verified/ else translated/",
        "translations_in_artifact": 211,
        "subset_rule": ("(a) every executable translation (61: B01_synthetic 43, B02_organic 2, B02_synthetic 15, P00 1); "
                        "(b) every library translation whose public header (c_src/include/*.h) declares only functions with "
                        "scalar or const char* parameters and scalar/void returns (89 of 149); "
                        "not graded: 60 libraries with pointer/struct interfaces, and P01 SPHINCS+ (1 translation, 128 configurations)."),
        "exe_built_and_graded": len(exe_tested),
        "lib_eligible": sum(1 for r in (ls or {}).get("cases", []) if r["eligible"]),
        "lib_built_and_graded": len(lib_tested),
        "lib_not_graded_build": [f"{r['battery']}/{r['case']}: {r['status']}" for r in (lb or {}).get("cases", []) if r["status"] != "ok"],
    }
    trans_def = sorted({r["translation"] for r in rows})
    acc_all = {}
    for t in exe_tested + lib_tested:
        a, _ = acceptance(kiro, t)
        acc_all.setdefault(a, []).append(t)
    S["graded_translations_by_acceptance"] = {a: len(v) for a, v in acc_all.items()}
    S["graded_translations_by_acceptance_detail"] = {
        "executables": {a: sum(1 for t in v if t in exe_tested) for a, v in acc_all.items()},
        "libraries": {a: sum(1 for t in v if t in lib_tested) for a, v in acc_all.items()},
        "rejected_list": sorted(acc_all.get("rejected", []))}
    S["translations_tested"] = len(exe_tested) + len(lib_tested)
    S["translations_tested_detail"] = {"executables": len(exe_tested), "libraries": len(lib_tested)}
    S["translations_with_defect"] = len(trans_def)
    S["translations_with_defect_detail"] = {
        "executables": len({r["translation"] for r in rows if r["kind"] == "exe"}),
        "libraries": len({r["translation"] for r in rows if r["kind"] == "lib"})}
    S["defects_total"] = len(rows)
    S["defects_by_severity"] = {k: sum(1 for r in rows if r["severity"] == k) for k in ("S1", "crash", "S2", "S3")}
    S["defects_by_mechanism"] = {}
    for r in rows:
        S["defects_by_mechanism"][r["mechanism"]] = S["defects_by_mechanism"].get(r["mechanism"], 0) + 1
    S["translations_with_defect_by_acceptance"] = {}
    seen = {}
    for r in rows:
        seen[r["translation"]] = r["acceptance_status"]
    for t, a in seen.items():
        S["translations_with_defect_by_acceptance"][a] = S["translations_with_defect_by_acceptance"].get(a, 0) + 1
    S["translations_with_S1_or_crash_defect"] = len({r["translation"] for r in rows if r["severity"] in ("S1", "crash")})
    S["crashes"] = {"defects": [f"{r['translation']}::{r['mechanism']}" for r in rows if r["severity"] == "crash"],
                    "cases": sum(r["classes"].get("rust_crash", 0) for r in rows)}
    S["S2"] = {"defects_S2_only": [f"{r['translation']}::{r['mechanism']}" for r in rows if r["severity"] == "S2"],
               "cases": sum(r["classes"].get("S2", 0) for r in rows)}
    S["ub_excluded"] = {"executables": ub_stats(dt, ub), **({"libraries": ub_stats(ld, lu)} if ld and lu else {}),
                        "supplementary_stack_use_after_return": {
                            k: {"counted_cases_checked": (load(out_d, f"suar_{k}.json") or {}).get("n_cases"),
                                "fired_and_excluded": (load(out_d, f"suar_{k}.json") or {}).get("fired")}
                            for k in ("exe", "lib")}}
    S["cases"] = {"exe_inputs_run": sum(p["n_inputs"] for p in dt["programs"]),
                  "lib_inputs_run": sum(p["n_inputs"] for p in (ld or {}).get("programs", [])),
                  "exe_c_timeouts_not_exercised": sum(p["counts"].get("c_timeout", 0) for p in dt["programs"])}
    S["defects"] = sorted(rows, key=lambda r: (r["kind"], r["translation"], r["mechanism"]))
    S["heldout"] = load(out_d, "heldout_summary.json") or {"status": "see heldout.json"}
    # mutation_run1: systems/actoruw/mutate.py's out/mutation.json, copied to results/actoruw_mutation.json
    # (top-level layout kept, host provenance added); the kill_rule block is the results/harness_power.json row
    mu = load(res_d, "actoruw_mutation.json")
    hp = (load(res_d, "harness_power.json") or {}).get("rows", [])
    row = next((r for r in hp if r["label"] == "ACTOR"), None)
    S["harness_power"] = {"mutation_run1": None if mu is None else {
        k: mu[k] for k in ("translations_agreeing", "translations_mutated", "total", "by_class", "by_kind", "kill_rate",
                           "wilson95", "killed_by_kind", "survived_binary_identical", "per_class_sites", "seed",
                           "baseline_excluded")}
        | {"seconds": mu["provenance"]["seconds"], "source": "results/actoruw_mutation.json (systems/actoruw/mutate.py)"},
        "kill_rule": None if row is None else {"rule": KILL_RULE_TEXT, **{k: row[k] for k in (
            "killed", "built", "rate", "undetermined", "rule_separable", "mutated_program", "note")},
            "source": "results/harness_power.json (scripts/plot_results.py, from the per-mutant records); this is the figure's number"}}
    S["toolchain_crosscheck"] = {k: load(out_d, f"tc_crosscheck_{k}.json") for k in ("exe", "lib")}
    S["date"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    os.makedirs(res_d, exist_ok=True)
    with open(os.path.join(res_d, "actoruw_summary.json"), "w") as f:
        json.dump(S, f, indent=1)
        f.write("\n")
    for n in ("build", "difftest", "ub_filter", "lib_scan", "lib_build", "lib_difftest", "ub_filter_lib", "heldout",
              "heldout_summary", "heldout_replay_difftest", "ub_filter_replay", "tc_crosscheck_exe", "tc_crosscheck_lib",
              "suar_exe", "suar_lib", "power_injection"):
        obj = load(out_d, f"{n}.json")
        if obj is None:
            continue
        with open(os.path.join(res_d, f"actoruw_{n}.json"), "w") as f:
            json.dump({"provenance": prov | {"script_of_this_file": prov.get("scripts", {}).get(n)}, "data": obj}, f)
            f.write("\n")
    print(json.dumps({k: S[k] for k in ("translations_tested", "translations_with_defect", "defects_total",
                                         "defects_by_severity", "translations_with_defect_by_acceptance")}, indent=1))


if __name__ == "__main__":
    main()
