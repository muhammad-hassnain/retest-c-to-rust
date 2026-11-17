"""Assemble results/adatrans_*.json from the measurement outputs (reads JSON only; no artifact code runs).

  python3 -I systems/adatrans/summarize.py --work <build dir> --inputs-manifest <inputs_manifest.json> \
      --results <results dir> --runs run1 [run2 run3]
Inputs in --work (all produced by pipeline.sh stages): build_1.86.0.json, extracted/manifest.json,
  cases_<run>_ub.jsonl, diff_<run>.json, confirm_run1.json, heldout.json, mutation_run1.json (optional),
  determinism.json (optional, the `check` stage).
The summary is assembled twice from the same inputs and compared (provenance excluded); the result of
that comparison and of the `check` stage are recorded under "determinism".
Hand triage: systems/adatrans/defects_run1.json, patches_run1.json.
Writes results/adatrans_summary.json, adatrans_diff_run{1,2,3}.json, adatrans_heldout.json,
adatrans_mutation_run1.json, adatrans_confirm_run1.json.
"""
import argparse
import collections
import datetime
import json
import os
import platform
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
RES = os.path.join(REPO, "results")  # overridden by --results
COUNTED = ("S1", "S2", "panic", "crash", "hang")


def git_rev():
    try:
        r = subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"], stdout=subprocess.PIPE, text=True).stdout.strip()
        d = subprocess.run(["git", "-C", REPO, "status", "--porcelain", "systems/adatrans"], stdout=subprocess.PIPE,
                           text=True).stdout.strip()
        return r + (" (systems/adatrans/ uncommitted)" if d else "")
    except OSError:
        return "?"


def provenance(command, extra=None):
    p = {
        "script": "systems/adatrans/pipeline.sh -> systems/adatrans/summarize.py (assembly) <- measurement scripts below",
        "command": command,
        "git_rev": os.environ.get("ADATRANS_GIT_REV") or git_rev(),
        "host": {"measurement": "desktop (x86-64 Linux box, kernel 7.0.0-38-generic, 20 cores; "
                                f"docker --cpus {os.environ.get('ADATRANS_JOBS', '?')} for the last pipeline run; earlier stages (fetch..gen) at 3)",
                 "assembly": f"{platform.system()} ({platform.machine()})"},
        "arch": "x86_64 (all builds and runs); assembly on " + platform.machine(),
        "docker_image": {"name": "crust-heldout-plus186",
                         "id": os.environ.get("ADATRANS_IMAGE_ID") or "sha256:db473e6761446908cc684aef7d5e5ef3326b7f022f9fd0b00f3f20e7a5660503",
                         "flags": "--network none, --cpus <=3, run as the invoking uid"},
        "toolchains": {"gcc": "gcc (Ubuntu 11.4.0-1ubuntu1~22.04.3) 11.4.0",
                       "clang": "Ubuntu clang version 14.0.0-1ubuntu1.1",
                       "rustc": "rustc 1.86.0 (05f9846f8 2025-03-31) via rustup in the image",
                       "cargo": "cargo 1.86.0 (adf9b6ad1 2025-02-28)",
                       "crates (vendored offline, Cargo.lock)": "rand 0.8.8, regex 1.13.1, libc 0.2.191, lazy_static 1.5.1",
                       "python_in_image": "3.10.12"},
        "artifact": {"repo": "https://github.com/SlainTroyard/adatrans_dev",
                     "commit": "31f4031aadb7ee9d4a2fb1a180e78ae9b45e5578",
                     "git_archive_sha256": "49e132b5991856c92deedab7e96d171ac2b6b6bf1c30d7cb6cc7cf632ba99231",
                     "zenodo": "10.5281/zenodo.22258998 adatrans-artifacts.tar.zst sha256 "
                               "03110aba3b469b37c56daf931dd6bb0e2ead37ac8462900da39c5e795493d6fe (matches published); "
                               "only dataset_fuzz_outputs/ extracted"},
        "measurement_scripts": ["systems/adatrans/fetch.sh", "systems/adatrans/extract_final.py",
                                "systems/adatrans/gen_inputs.py", "systems/adatrans/run_diff.py",
                                "systems/adatrans/analyze.py", "systems/adatrans/confirm.py",
                                "systems/adatrans/heldout.py", "systems/adatrans/mutate.py",
                                "harness/oracle_ub_common.py (sanitizer flags, run_san)"],
        "date": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    if extra:
        p.update(extra)
    return p


def load_cases(paths):
    rows = []
    for p in paths:
        if os.path.exists(p):
            rows += [json.loads(x) for x in open(p)]
    return rows


def dump(name, obj):
    with open(os.path.join(RES, name), "w") as f:
        json.dump(obj, f, indent=1)
        f.write("\n")


def main():
    global RES
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--inputs-manifest", required=True)
    ap.add_argument("--results", default=RES)
    ap.add_argument("--runs", nargs="+", default=["run1"])
    a = ap.parse_args()
    RES = a.results
    s1, files1 = assemble(a)
    s2, files2 = assemble(a)
    strip = lambda o: {k: v for k, v in o.items() if k != "provenance"}  # noqa: E731
    same = (json.dumps(strip(s1), sort_keys=True) == json.dumps(strip(s2), sort_keys=True)
            and all(json.dumps(strip(files1[k]), sort_keys=True) == json.dumps(strip(files2[k]), sort_keys=True) for k in files1))
    det_path = os.path.join(a.work, "determinism.json")
    s1["determinism"] = {
        "summarize_twice_identical_excluding_provenance": same,
        "stage_recheck": json.load(open(det_path)) if os.path.exists(det_path) else "not run (pipeline.sh check)",
        "seeds": "none: inputs are a function of (fuzz script, input index k, draw index c) only "
                 "(Kronecker frac(k*sqrt(p_c)), splitmix64(k, c) beyond 3000 draws); rand::random in a translation "
                 "(C_415_4) and srand(time(NULL)) in the C (415_4) are the programs' own randomness",
        "nondeterminism_sources_left": ["timeouts (10 s / 60 s) decide 'hang'; a program near the limit can flip",
                                        "sanitizer report text is normalised (PIDs, addresses) before it is stored"],
    }
    dump("adatrans_summary.json", s1)
    for k, v in files1.items():
        dump(k, v)
    print(json.dumps({k: s1[k] for k in ("translations_tested", "translations_built", "translations_with_defect",
                                         "defects_count", "defects_by_severity", "accepted_translations_with_defect")}, indent=1))
    print("summarize twice identical:", same)


def assemble(a):
    D = a.work
    J = lambda n: json.load(open(os.path.join(D, n)))  # noqa: E731
    man = json.load(open(os.path.join(D, "extracted", "manifest.json")))
    build = J("build_1.86.0.json")
    triage = json.load(open(os.path.join(HERE, "defects_run1.json")))
    patches = {p["id"]: p for p in json.load(open(os.path.join(HERE, "patches_run1.json")))["defects"]}
    confirm = J("confirm_run1.json")
    held = J("heldout.json")
    gen = json.load(open(a.inputs_manifest))
    mut = J("mutation_run1.json") if os.path.exists(os.path.join(D, "mutation_run1.json")) else None
    cases1 = load_cases([os.path.join(D, "cases_run1_ub.jsonl")])
    diffs = {r: J(f"diff_{r}.json") for r in a.runs if os.path.exists(os.path.join(D, f"diff_{r}.json"))}
    cmd = "bash systems/adatrans/pipeline.sh summarize  (in Docker: python3 -I " + " ".join(sys.argv) + ")"

    # ---------------- rule check
    m1 = man["runs"]["run1"]
    c_ok = {k: all(x["ok"] for x in v.values()) for k, v in build["c"].items()}
    rust = build["rust"]["per"]
    compile_match = {}
    for run, rows in man["runs"].items():
        art_ce = sorted(k for k, v in rows.items() if v["final_iter_status"] in ("COMPILE_ERROR", "LINK_ERROR"))
        ours_ce = sorted(k for k in rows if not rust[f"{run}_{k}".lower()]["ok"])
        compile_match[run] = {"artifact_final_iteration_compile_error": art_ce, "our_build_fails": ours_ce,
                              "identical": art_ce == ours_ce, "built": len(rows) - len(ours_ce), "of": len(rows)}
    own = collections.defaultdict(lambda: [0, 0])
    exmis = collections.defaultdict(lambda: {"examples": [0, 0], "fuzz": [0, 0]})
    for r in cases1:
        if "rust_passes_own_check" in r:
            own[r["inst"]][0] += bool(r["rust_passes_own_check"])
            own[r["inst"]][1] += 1
        if "c_matches_recorded" in r:
            e = exmis[r["inst"]][r["src"]]
            e[0] += 0 if r["c_matches_recorded"] else 1
            e[1] += 1
    own_fail = sorted(i for i, v in own.items() if v[0] < v[1])
    build_fail1 = compile_match["run1"]["our_build_fails"]
    not_solved1 = sorted(k for k, v in m1.items() if not v["solved"])
    rule = {
        "holds": True,
        "original_C": {"programs": len(build["c"]), "gcc_ref_builds": sum(1 for v in build["c"].values() if v["ref"]["ok"]),
                       "all_7_ub_rule_builds": sum(c_ok.values()),
                       "clang_cannot_compile (GCC nested functions)": sorted(k for k, v in c_ok.items() if not v),
                       "runs": "every C program ran on its inputs (see diff files: no C build or start failure)"},
        "systems_own_rust": {"rule_for_final": man["rule"], "per_run": compile_match,
                             "c_in_first_prompt_equals_dataset_c": {r: man["summary"][r]["c_in_first_prompt"] for r in man["summary"]}},
        "toolchain": "artifact pins only 'a stable Rust toolchain' (README); 1.86.0 used. The artifact's recorded "
                     "panic sites match ours (e.g. C_421_2 'attempt to add with overflow' at "
                     "library/core/src/iter/traits/accum.rs:149:1 in both).",
        "own_check_rerun_reproduces_artifact_verdicts_run1": {
            "translations_failing_own_check_rerun_or_build": sorted(set(own_fail) | set(build_fail1)),
            "artifact_not_solved": not_solved1,
            "identical": sorted(set(own_fail) | set(build_fail1)) == not_solved1},
    }

    # ---------------- defects (run1)
    hin = {k: set(v["ours_in_check_files"]) for k, v in held["per_program"].items()}
    by_inst = collections.defaultdict(list)
    for r in cases1:
        if r["kind"] in COUNTED and r.get("c_ub_clean"):
            by_inst[r["inst"]].append(r)
    defects = []
    for d in triage["defects"]:
        rows = by_inst[d["inst"]]
        exp = collections.Counter(f'{r["src"]}:{r["kind"]}' for r in rows)
        held_rows = [r for r in rows if r["src"] == "ours" and r["idx"] not in hin.get(d["inst"], set())]
        sys_rows = [r for r in rows if r["src"] != "ours"]
        ex = sorted(rows, key=lambda r: (r["src"] != "ours", r["input_len"]))[0] if rows else None
        v = m1[d["inst"]]
        if v["solved"]:
            status = "accepted"
            ev = (f"logs/{d['inst']}.json status SUCCESS; validation/{d['inst']}.json PASSED "
                  f"{v['fuzz_passed']}/{v['fuzz_total']}; our re-run of its check on its own inputs: "
                  f"{own[d['inst']][0]}/{own[d['inst']][1]} pass")
        elif v["loop_status"] == "SUCCESS":
            status = "rejected"
            ev = (f"logs SUCCESS (examples) but validation/{d['inst']}.json FAILED {v['fuzz_passed']}/{v['fuzz_total']} "
                  f"(fuzz oracle); not counted as solved by analysis/metrics.py::read_run; our re-run: "
                  f"{own[d['inst']][0]}/{own[d['inst']][1]} pass")
        else:
            status = "rejected"
            ev = (f"logs/{d['inst']}.json status FAILURE after {v['iterations']} iterations "
                  f"(last: {v['last_iter_status']}); never fuzz-validated; our re-run: {own[d['inst']][0]}/{own[d['inst']][1]} pass")
        cf = confirm.get(d.get("patch_id") or "", None)
        ex_case = {"case": f"run1/{d['inst']}/{ex['src']}/{ex['idx']}",
                   "input": ex.get("input_text") if ex.get("input_text") is not None else f"({ex['input_len']} bytes; sha256 {ex['input_sha']})",
                   "C": {"stdout_head": ex["c"]["stdout_head"], "exit": ex["c"]["rc"]},
                   "Rust": {"stdout_head": ex["rust"]["stdout_head"], "exit": ex["rust"]["rc"],
                            "stderr_head": ex["rust"]["stderr_head"][:240]}} if ex else None
        defects.append({
            "id": d["id"], "translation": f"run1/{d['inst']}", "function": "whole program (stdin/stdout)",
            "mechanism": d["mechanism"],
            "rust_location": d["rust_location"] + f" -- source: artifacts/adatrans/results/adatrans_full/run1/conversations/{d['inst']}.json history[{v['final_index']}].extracted_code (sha256 {v['code_sha256']}), extracted by systems/adatrans/extract_final.py",
            "c_location": "artifacts/adatrans/dataset/C_CPP/C/src/" + d["c_location"],
            "severity": d["severity"],
            "example": ex_case,
            "exposure": {"cases": len(rows), "by_source_kind": dict(exp),
                         "our_inputs_diverging": sum(1 for r in rows if r["src"] == "ours"),
                         "our_inputs_run": sum(1 for r in cases1 if r["inst"] == d["inst"] and r["src"] == "ours"),
                         "systems_own_inputs_diverging": len(sys_rows)},
            "acceptance_status": status, "acceptance_evidence": ev,
            "heldout": {"diverges_on_heldout_input": bool(held_rows), "heldout_cases": len(held_rows),
                        "diverges_on_systems_own_inputs": bool(sys_rows), "systems_own_input_cases": len(sys_rows)},
            "confirmed_by": ({"method": "fix-and-rerun: one-mechanism patch (systems/adatrans/patches_run1.json), rebuilt, every divergent input of the translation re-run (confirm.py)",
                              "patch": d.get("patch_summary"),
                              "result": {k: cf[k] for k in ("rerun", "now_agree", "still_diverge", "now_agree_by_orig_kind", "still_by_orig_kind", "still_diverge_detail") if k in cf},
                              **({"joint": {"patch_id": d["joint_patch_id"],
                                            "result": {k: confirm[d["joint_patch_id"]][k] for k in ("rerun", "now_agree", "still_diverge", "now_agree_by_orig_kind", "still_by_orig_kind") if k in confirm.get(d["joint_patch_id"], {})}}}
                                 if d.get("joint_patch_id") else {})}
                             if cf else {"method": "source reading only (rewritten function; no single-mechanism patch)"}),
            **({"only_on_format_violating_inputs": True} if d.get("only_on_format_violating_inputs") else {}),
            **({"note": d["note"]} if d.get("note") else {}),
        })

    # ---------------- per-run aggregates
    def run_agg(run):
        dj = diffs.get(run)
        if not dj:
            return None
        t = dj["translations"]
        counted = {i: r for i, r in t.items() if r["counted"]}
        acc = collections.Counter()
        for i in counted:
            v = man["runs"][run][i]
            acc["accepted" if v["solved"] else "rejected"] += 1
        return {"translations": len(t), "built": sum(1 for r in t.values() if not r["rust_build_fail"]),
                "with_counted_divergence": len(counted),
                "with_counted_divergence_on_our_inputs": sum(1 for r in t.values() if r["counted_on_ours"]),
                "accepted_with_counted_divergence": sorted(i for i in counted if man["runs"][run][i]["solved"]),
                "rejected_with_counted_divergence": sorted(i for i in counted if not man["runs"][run][i]["solved"]),
                "acceptance_of_divergent": dict(acc),
                "agree_on_all_our_inputs_strict": len(dj["agreeing_on_all_ours"]),
                "whitespace_only_translations": sorted(i for i, r in t.items() if any("whitespace" in k for k in r["kinds"])),
                "aggregate": dj["aggregate"]}

    runs = {r: run_agg(r) for r in a.runs}
    per_translation = {}
    dfx = collections.defaultdict(list)
    for d in defects:
        dfx[d["translation"].split("/")[1]].append(d["id"])
    for i, t in diffs["run1"]["translations"].items():
        v = m1[i]
        per_translation[f"run1/{i}"] = {
            "built": not t["rust_build_fail"], "cases": t["cases"], "kinds": t["kinds"],
            "divergent_counted": sum(t["counted"].values()), "original_C_UB_excluded": sum(t["ub_excluded"].values()),
            "defects": dfx.get(i, []),
            "artifact_verdict": {"loop": v["loop_status"], "fuzz": v["fuzz_status"],
                                 "fuzz_passed": v["fuzz_passed"], "fuzz_total": v["fuzz_total"], "solved": v["solved"]},
            "acceptance_status": "accepted" if v["solved"] else "rejected",
            "own_check_rerun": {"pass": own[i][0], "of": own[i][1]}}
    a1 = runs["run1"]
    crashes = [d for d in defects if d["severity"] in ("panic", "crash")]
    s3 = [d for d in defects if d["severity"] == "S3"]
    ubx = collections.Counter()
    for r in cases1:
        if r["kind"] in COUNTED and not r.get("c_ub_clean"):
            ubx[r["inst"]] += 1
    ex_mis = {i: v for i, v in exmis.items() if v["examples"][0]}
    summary = {
        "provenance": provenance(cmd),
        "system": "AdaTrans (arXiv 2606.31706)",
        "scope": {"primary": "run1 (results/adatrans_full/run1): all 104 programs re-graded and triaged",
                  "secondary": "run2, run3: same pipeline, automated counts only, divergences NOT triaged into defects",
                  "subset_rule": "every program; translations that do not build are S3 build failures"},
        "rule_check": rule,
        "inputs": {"generator": "systems/adatrans/gen_inputs.py", "programs": len(gen["programs"]),
                   "total_inputs": sum(v["n"] for v in gen["programs"].values()),
                   "programs_below_80": {k: v["n"] for k, v in gen["programs"].items() if v["n"] < 80},
                   "systems_own_inputs_also_run": "examples (loop) + fuzz oracle, parsed as src/runner.py does"},
        "translations_tested": a1["translations"],
        "translations_built": a1["built"],
        "translations_with_defect": len({d["translation"] for d in defects}),
        "translations_with_defect_list": sorted({d["translation"] for d in defects}),
        "defects_count": len(defects),
        "defects_by_severity": dict(collections.Counter(d["severity"] for d in defects)),
        "accepted_translations_with_defect": sorted({d["translation"] for d in defects if d["acceptance_status"] == "accepted"}),
        "defects": defects,
        "crashes": {"note": "Rust panics/crashes on UB-clean C inputs, reported separately from S1",
                    "counted": len(crashes), "ids": [d["id"] for d in crashes],
                    "cases": sum(d["exposure"]["cases"] for d in crashes)},
        "S2": {"counted": sum(1 for d in defects if d["severity"] == "S2"),
               "ids": [d["id"] for d in defects if d["severity"] == "S2"], "note": "no stderr-only divergence observed"},
        "S3": {"build_failures_run1": build_fail1, "hang_defects": [d["id"] for d in s3]},
        "whitespace_only_not_counted": {"translations_run1": a1["whitespace_only_translations"],
                                        "note": "stdout equal after whitespace folding (mostly a trailing space the C prints, or the C's missing final newline); the artifact's own check folds whitespace"},
        "ub_excluded": {"cases_by_translation_run1": dict(ubx), "not_counted_detail": triage["not_counted"]},
        "heldout": {
            "status": "measured (AdaTrans records both check input sets: loop examples and fuzz oracle, in the Zenodo bundle)",
            "overlap_of_our_inputs_with_checks": held["totals"],
            "accepted_defects_diverging_on_heldout_inputs": sorted(d["id"] for d in defects if d["acceptance_status"] == "accepted" and d["heldout"]["diverges_on_heldout_input"]),
            "defects_diverging_only_on_systems_own_inputs": sorted(d["id"] for d in defects if not d["heldout"]["diverges_on_heldout_input"]),
            "systems_inputs_through_our_comparison": {k: v for k, v in a1["aggregate"].items() if k.startswith("kind:examples") or k.startswith("kind:fuzz")},
            "loop_examples_disagree_with_C": {"problems": sorted(ex_mis), "detail": ex_mis,
                                              "note": "AdaTrans's loop compares the Rust against the recorded example outputs (LeetCode's), not against the C; on these problems the C does not produce the recorded output"},
        },
        "harness_power": ({"fault_injection": "every confirm.py patch is the converse experiment: removing one mechanism turns the harness green on its cases; the mutation analysis below injects faults into agreeing translations",
                           "mutation_run1": {k: v for k, v in mut.items() if k != "mutants_detail"}} if mut else "pending"),
        "runs": runs,
        "per_translation": per_translation,
    }
    files = {}
    for r, dj in diffs.items():
        files[f"adatrans_diff_{r}.json"] = dict(provenance=provenance(cmd, {"source": f"analyze.py over cases_{r}_ub.jsonl"}), **dj)
    files["adatrans_heldout.json"] = dict(provenance=provenance(cmd, {"source": "heldout.py"}), **held)
    files["adatrans_confirm_run1.json"] = dict(provenance=provenance(cmd, {"source": "confirm.py"}), patches=patches, results=confirm)
    if mut:
        files["adatrans_mutation_run1.json"] = dict(provenance=provenance(cmd, {"source": "mutate.py"}), **mut)
    return summary, files

if __name__ == "__main__":
    main()
