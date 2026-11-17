#!/usr/bin/env python3
"""Build results/sactor_summary.json from the SACTOR re-grade result files and the curated defect
list (defects.json, each defect confirmed by hand in the Rust source with file:line).

    python3 systems/sactor/build_summary.py   (on any host; reads results/sactor_*.json)
"""
import datetime
import json
import os
import platform
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
RES = os.path.join(REPO, "results")
PRIMARY = "nightly-2024-09-03"


def load(name):
    p = os.path.join(RES, name)
    return json.load(open(p)) if os.path.isfile(p) else None


def main():
    ogg = load("sactor_libogg.json")
    ogg_h = load("sactor_libogg_heldout.json")
    ogg_m = load("sactor_libogg_mutation.json")
    cr = load("sactor_crust_owntests.json")
    cr_extra = load("sactor_crust_owntests_assembled.json")
    gr = load("sactor_crust_grid.json")
    gr_extra = load("sactor_crust_grid_assembled.json")
    cr_m = load("sactor_crust_mutation.json")
    defects = json.load(open(os.path.join(HERE, "defects.json")))
    samples = dict(cr["samples"])
    partial = load("sactor_crust_partial.json")
    if partial:   # completeness evidence (C functions defined in lib.c without a Rust file) for partial sets
        for s_, r_ in partial["samples"].items():
            if s_ in samples and "inventory" in r_:
                for st in ("unidiomatic", "idiomatic"):
                    samples[s_]["inventory"][st]["c_functions_without_rust"] = r_["inventory"][st].get("c_functions_without_rust")
                samples[s_]["c_function_definitions"] = r_.get("c_function_definitions")
    gsamples = dict(gr["samples"]) if gr else {}
    # cr_extra / gr_extra (amp, cfsm, inversion_list assembled anyway) are kept as separate evidence only

    # ---------------------------------------------------------------- inventory (M) and scope (N)
    units = []
    for t, info in ogg["translations"].items():
        model, file, style = t.split("/")
        units.append({"unit": f"libogg {model} {file} {style}", "part": "libogg", "style": style,
                      "rust_source": info["source"], "items": info["items"],
                      "builds": {k: v["ok"] for k, v in info["builds"].items()}})
    for s, r in sorted(samples.items()):
        inv = r["inventory"]
        for style in ("unidiomatic", "idiomatic"):
            if inv[style]["functions"] == 0:
                continue
            b = {k.split(":", 1)[1]: v["ok"] for k, v in r.get("rust_builds", {}).items() if k.startswith(style + ":")}
            units.append({"unit": f"CRUST-Bench {s} {style}", "part": "CRUST-Bench", "style": style,
                          "rust_source": "combined.rs" if inv[style]["combined_rs"] else None,
                          "functions_shipped": inv[style]["functions"],
                          "c_functions_defined": r.get("c_function_definitions"),
                          "c_functions_without_rust": (len(inv[style]["c_functions_without_rust"])
                                                       if inv[style].get("c_functions_without_rust") is not None else None),
                          "failure_info": {k: inv[style].get(k) for k in ("failure_info_functions", "failure_info_success")},
                          "builds": b})
    for u in units:
        built = [v for v in u["builds"].values()]
        if u["part"] == "CRUST-Bench":
            # complete = SACTOR combined the sample (combined.rs). The three samples whose
            # failure_info listed only successes (amp, cfsm, inversion_list) turned out to lack Rust
            # for C functions defined in lib.c (amp_encode; cfsm_event, cfsm_process; 39 of 47), so
            # they are partial sets like the others (results/sactor_crust_owntests_assembled.json).
            complete = u["rust_source"] == "combined.rs"
        else:
            complete = u["rust_source"] is not None and u["style"] == "unidiomatic"
        if u["style"] == "idiomatic":
            u["scope"] = "not differentially tested (Rust-native API, no C ABI)" + ("; builds" if built and all(built) else "")
        elif not complete:
            u["scope"] = "out: incomplete per-function set, no combined.rs"
        elif built and not any(built):
            u["scope"] = "tested: build only (does not build -> S3)"
        elif built and all(built):
            u["scope"] = "tested: differential"
        else:
            u["scope"] = "tested: partial builds"
    tested = [u for u in units if u["scope"].startswith("tested")]

    # ---------------------------------------------------------------- UB exclusions, S2, crashes
    ub = {"libogg_cases_flagged": {f: sum(1 for r in d["rows"] if r["c_ub"]) for f, d in ogg["files"].items()},
          "libogg_divergent_cases_excluded": {f: sum(1 for r in d["rows"] if r["c_ub"] and any(v != "agree" for v in r["rust"].values()))
                                              for f, d in ogg["files"].items()}}
    own_rows = []
    for s, r in samples.items():
        for c in r.get("cases", []):
            if c.get("rust"):           # own-test programs of samples with a building Rust library
                own_rows.append((s, c))
    ub["crust_own_test_programs"] = len(own_rows)
    ub["crust_own_test_cases_flagged"] = sum(1 for s, c in own_rows if c["c_ub"])
    ub["crust_own_test_flagged_list"] = [f"{s}:{c['obj']}: {c['c_ub'][0][:90]}" for s, c in own_rows if c["c_ub"]]
    ub["crust_own_test_divergent_excluded"] = [
        {"sample": s, "obj": c["obj"], "c_ub": c["c_ub"][:3]} for s, c in own_rows
        if c["c_ub"] and any(not v.get("agree", True) for v in c["rust"].values())]
    g_excl, g_div, crashes, s2, hangs, float_tol = [], [], [], [], [], []

    def within_float_tol(d):
        """method.tex: floating-point returns compare with a relative tolerance; same rule as
        systems/vert/run_arrays.py: |a - b| <= 1e-6 * max(1, |a|), exit codes equal."""
        if d["c"][1] != d["rust"][1]:
            return False
        a, b = d["c"][0].split(), d["rust"][0].split()
        if len(a) != len(b) or len(a) < 2:
            return False
        try:
            for x, y in zip(a[1:], b[1:]):
                if x == y:
                    continue
                fx, fy = float(x.split("=")[-1]), float(y.split("=")[-1])
                if "." not in x + y and "e" not in (x + y).lower():
                    return False          # integers must match exactly
                if fx != fx or fy != fy or abs(fx) == float("inf") or abs(fy) == float("inf"):
                    return False          # nan / inf only match exactly (handled by x == y above)
                if abs(fx - fy) > 1e-6 * max(1.0, abs(fx)):
                    return False
            return True
        except ValueError:
            return False
    # map every UB-clean divergence to a curated defect (defects.json "grid_functions"/"libogg_files"/"own_tests")
    fn2def = {}
    for dfc in defects:
        for sf in dfc.get("grid_functions", []):
            fn2def[tuple(sf.split(":"))] = dfc["id"]
    pre_only = {dfc["id"] for dfc in defects if not dfc.get("counted", True)}
    by_def = {}            # defect -> {"primary": n, "per_build": {build: n}}
    unclassified = []

    def note(defect, build, entry, crash):
        e = by_def.setdefault(defect, {"per_build": {}, "crash_per_build": {}})
        e["per_build"][build] = e["per_build"].get(build, 0) + 1
        if crash:
            e["crash_per_build"][build] = e["crash_per_build"].get(build, 0) + 1

    def is_crash(rc, err):
        return rc < 0 or rc in (101, 134, 139) or "panicked" in (err or "")

    for s, r in gsamples.items():
        for fn, rec in r.get("per_function", {}).items():
            for key, v in rec["rust"].items():
                if v == "agree":
                    continue
                for d in v["divergent_inputs"]:
                    u = rec["ub"].get(str(d["input"]), rec["ub"].get(d["input"], {}))
                    entry = {"sample": s, "function": fn, "build": key, "input": d["input"], "args": d["args"],
                             "c": d["c"], "rust": d["rust"][:2]}
                    if d.get("rust_timed_out"):
                        hangs.append(entry)
                        continue
                    if within_float_tol(d):
                        float_tol.append(entry)
                        continue
                    if u.get("c_ub"):
                        g_excl.append({**entry, "c_ub": u["c_ub"][:2]})
                        continue
                    g_div.append(entry)
                    dfc = fn2def.get((s, fn))
                    crash = is_crash(d["rust"][1], d["rust"][2])
                    if dfc is None:
                        unclassified.append(entry)
                        continue
                    note(dfc, key, entry, crash)
                    if dfc in pre_only:
                        continue
                    if crash:
                        crashes.append(entry)
                    elif d["c"][0] == d["rust"][0] and d["c"][1] == d["rust"][1]:
                        s2.append(entry)
    ub["crust_grid_divergent_inputs_excluded"] = len(g_excl)
    ub["crust_grid_excluded_by_function"] = {}
    for e in g_excl:
        k = f"{e['sample']}:{e['function']}"
        ub["crust_grid_excluded_by_function"].setdefault(k, {"inputs_x_builds": 0, "example_c_ub": e["c_ub"]})
        ub["crust_grid_excluded_by_function"][k]["inputs_x_builds"] += 1
    for f, d in ogg["files"].items():
        for r in d["rows"]:
            if r["c_ub"]:
                continue
            for rn, v in r["rust"].items():
                if v == "agree":
                    continue
                dfc = {"bitwise": "SACTOR-L1", "framing": "SACTOR-L2"}[f] if ":gpt-4o:" in rn else None
                crash = is_crash(v["exit"], v["stderr"])
                if dfc is None:
                    unclassified.append({"libogg": f, "case": r["id"], "build": rn})
                    continue
                note(dfc, rn, None, crash)
                if crash:
                    crashes.append({"libogg": f, "case": r["id"], "build": rn, "exit": v["exit"]})
                elif not v["stdout_differs"] and v["exit"] == r["c_exit"]:
                    s2.append({"libogg": f, "case": r["id"], "build": rn})
    for s, c in own_rows:
        if c["c_ub"]:
            continue
        for rn, v in c["rust"].items():
            if v.get("agree", True):
                continue
            dfc = fn2def.get((s, "own_test"))
            crash = is_crash(v["exit"], v.get("rust_stderr_tail", ""))
            if dfc is None:
                unclassified.append({"sample": s, "own_test": c["obj"], "build": rn})
                continue
            note(dfc, rn, None, crash)
            if crash:
                crashes.append({"sample": s, "own_test": c["obj"], "build": rn, "exit": v["exit"]})
            elif not v["stdout_differs"] and v["exit"] == c["c_exit"]:
                s2.append({"sample": s, "own_test": c["obj"], "build": rn})

    # ---------------------------------------------------------------- harness power
    hp = {}
    if ogg_m:
        hp["libogg_mutation"] = {k: {x: v[x] for x in ("candidate_sites", "selected", "build_fail", "valid", "killed")}
                                 for k, v in ogg_m["targets"].items()}
    if cr_m:
        hp["crust_mutation"] = {k: {x: v[x] for x in ("candidate_sites", "selected", "build_fail", "valid",
                                                      "gridded_functions", "killed_own_test", "killed_grid", "killed_either")}
                                for k, v in cr_m["samples"].items()}
    hp["real_defects_turn_harness_red"] = sorted({d["harness"] for d in defects if d.get("harness")})
    hp["survivor_review"] = {   # read by hand in the exposed Rust (line numbers of lib_<model>_<file>_unidiomatic.rs)
        "gpt-4o/bitwise.c": "4 survivors, all equivalent: oggpack_look/oggpackB_look/oggpack_read `bits > 24` -> `>=` "
                            "read one more byte that the mask removes (lines 345, 375, 454); writecopy_helper growth "
                            "test `>=` -> `>` (211) only changes when the buffer grows",
        "gpt-5/framing.c": "15 survivors. 4 provably equivalent: flush_i `lacing_fill > 255` -> `>=` (maxvals "
                           "unchanged, 2477); flush_i granule/pageno header loops `< 14`/`< 22` -> `<=` (the extra "
                           "byte is overwritten by the serial number or the CRC, 2540/2558); ogg_sync_buffer "
                           "`fill > 0` -> `>= 0` (memmove of 0 bytes, 2698). 10 change only internal capacity, "
                           "compaction or overflow guards reachable only with sizes near INT_MAX/LONG_MAX, which "
                           "no API return value shows on our inputs: initial body_storage 2199, _os_body_expand "
                           "guards 2264/2268/2273, _os_lacing_expand 2290 and 2927, ogg_sync_buffer size guard "
                           "2709, pagein compaction 2891/2903, iovecin loop bound 2425 (writes one unused slot). "
                           "1 real gap: flush_i `acc > nfill` -> `>=` (2501); acc == nfill with >= 4 packets done "
                           "is never generated",
        "CRUST geofence": "4 survivors, all boundary comparisons in is_point_in_polygon (`<`/`>` -> `<=`/`>=`, lines 75, 83). "
                          "That function takes a polygon pointer, so only the own test reaches it, and the own test "
                          "does not tell these boundaries apart (not checked whether any is equivalent)",
        "CRUST morton, murmurhash_c": "no relational/arithmetic operator sites (bitwise and wrapping-method code only)",
    }

    # ---------------------------------------------------------------- held-out
    held = {"libogg": {f: {k: v for k, v in d.items() if k not in ("exposing_cases",)} for f, d in ogg_h["files"].items()} if ogg_h else None}
    if gsamples:
        tot_in = tot_own = 0
        per = {}
        for s, r in gsamples.items():
            oc = r.get("own_test_calls", {}).get("per_function", {})
            for fn, v in oc.items():
                tot_in += v["our_inputs"]
                tot_own += v["our_inputs_in_own"]
            if oc:
                per[s] = oc
        held["crust_grid"] = {"our_inputs": tot_in, "our_inputs_also_passed_by_own_tests": tot_own, "per_sample": per}

    with_defect = sorted({d["translation"] for d in defects if d.get("counted", True)})
    rev = subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"], stdout=subprocess.PIPE, text=True).stdout.strip()
    out = {
        "system": "SACTOR",
        "provenance": {
            "script": "systems/sactor/build_summary.py",
            "command": "python3 systems/sactor/build_summary.py",
            "git_rev": rev + " (systems/sactor/ uncommitted)",
            "host": platform.system(), "arch": platform.machine(),
            "inputs": {k: (v or {}).get("provenance", {}).get("date") for k, v in
                       {"sactor_libogg.json": ogg, "sactor_libogg_heldout.json": ogg_h, "sactor_libogg_mutation.json": ogg_m,
                        "sactor_crust_owntests.json": cr, "sactor_crust_owntests_assembled.json": cr_extra,
                        "sactor_crust_grid.json": gr, "sactor_crust_grid_assembled.json": gr_extra,
                        "sactor_crust_mutation.json": cr_m}.items()},
            "toolchains/images": ogg["provenance"]["toolchains"] | {"docker_image": ogg["provenance"]["docker_image"],
                                                                    "docker_image_id": ogg["provenance"]["docker_image_id"]},
            "artifacts": ogg["provenance"]["artifacts"],
            "date": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        },
        "not_shipped": "The 200 headline TransCoder-IR / CodeNet translations are not in any SACTOR repository "
                       "(sactor-datasets .gitignore lists translated_data/ and data/; no .rs outside ogg/): that part "
                       "does not meet the re-grade rule and is not counted below.",
        "translations_shipped": {"total": len(units), "libogg": sum(1 for u in units if u["part"] == "libogg"),
                                 "crust_bench": sum(1 for u in units if u["part"] == "CRUST-Bench")},
        "translations_tested": len(tested),
        "translations_tested_list": [u["unit"] for u in tested],
        "translations_with_defect": len(with_defect),
        "translations_with_defect_list": with_defect,
        "defects": defects,
        "crashes": {"defects": sorted({dfc for dfc, e in by_def.items() if e["crash_per_build"] and dfc not in pre_only}),
                    "count_primary_build": sum(1 for x in crashes if x.get("build", "").endswith(PRIMARY + ":debug")),
                    "count_all_builds": len(crashes), "detail": crashes[:30],
                    "not_counted_precondition_only": {dfc: e["crash_per_build"] for dfc, e in by_def.items() if dfc in pre_only}},
        "S2": {"count_primary_build": sum(1 for x in s2 if x.get("build", "").endswith(PRIMARY + ":debug")),
               "count_all_builds": len(s2), "detail": s2[:30]},
        "exposure_by_defect_and_build": by_def,
        "unclassified_ub_clean_divergences": unclassified,
        "ub_excluded": ub,
        "grid_ub_clean_divergent_inputs": len(g_div),
        "grid_within_float_tolerance": {"count": len(float_tol), "functions": sorted({e["sample"] + ":" + e["function"] for e in float_tol}),
                                        "note": "relative tolerance 1e-6 (systems/vert/run_arrays.py); e.g. c-aces normal_rand uses std::f64::consts::PI where the C #defines PI 3.14159265 (max relative difference 7.5e-9)"},
        "grid_rust_timeouts_c_finished": {"count": len(hangs), "detail": hangs[:40],
                                          "note": "Rust did not finish within 60 s on inputs where the -O2 C finished within 3 s; reviewed by hand (slow debug build vs hang), not sent to the UB checks"},
        "heldout": held,
        "harness_power": hp,
        "units": units,
        "partial_sets_assembled_anyway": ({s_: {"builds": {k: v["ok"] for k, v in r_.get("rust_builds", {}).items()},
                                                "errors_or_link": [v.get("errors", [])[:1] or list(v.get("link_fail", {}).values())[:1]
                                                                   for v in r_.get("rust_builds", {}).values()][:1]}
                                           for s_, r_ in cr_extra["samples"].items()} if cr_extra else None),
    }
    with open(os.path.join(RES, "sactor_summary.json"), "w") as f:
        json.dump(out, f, indent=1)
        f.write("\n")
    print(json.dumps({k: out[k] for k in ("translations_shipped", "translations_tested", "translations_with_defect",
                                          "translations_with_defect_list")}, indent=1))
    print("crashes", len(crashes), "S2", len(s2), "grid UB-clean divergent inputs", len(g_div), "grid excluded", len(g_excl))


if __name__ == "__main__":
    sys.exit(main())
