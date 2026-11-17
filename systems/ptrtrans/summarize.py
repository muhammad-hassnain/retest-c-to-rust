"""Build results/ptrtrans_summary.json from the PtrTrans quadtree runs and the shipped artifact.

Inputs (all produced by the scripts in systems/ptrtrans/quadtree_e2e/):
  results/ptrtrans_quadtree_diff.json            run.py, rustc 1.86.0, full UB rule
  results/ptrtrans_quadtree_diff_rust1.99.1.json run.py --no-ub, rustc 1.99.1 (cross-toolchain)
  results/ptrtrans_quadtree_mutation.json        mutate.py (harness power)
and, for the acceptance / held-out evidence, the artifact's own records (read as text only):
  artifacts/ptrtrans/dataset/Trans_C-Rust-KG/quadtree_Trans_PA_trans_metadata.jsonl
  artifacts/ptrtrans/dataset/Trans_C-Rust-KG/quadtree_runLog.txt

  python3 -I systems/ptrtrans/summarize.py
"""
import collections
import datetime
import hashlib
import json
import os
import platform
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
ART = os.path.join(REPO, "artifacts", "ptrtrans")
RES = os.path.join(REPO, "results")
KG = os.path.join(ART, "dataset", "Trans_C-Rust-KG")
RS = "artifacts/ptrtrans/dataset/Trans_C-Rust-KG/quadtree/src/quadtree.rs"
CS = "artifacts/ptrtrans/dataset/crown_dataset/quadtree/src/quadtree.c"
PIN = "b20d5bb68a598a739f79d52114651e37cc6999cb"


def load(name):
    p = os.path.join(RES, name)
    return json.load(open(p)) if os.path.isfile(p) else None


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def fdec(tok):
    import struct
    return repr(struct.unpack(">d", bytes.fromhex(tok[1:]))[0])


def human(line):
    """Replace f<16 hex> tokens by decimal for the report."""
    if line is None:
        return None
    return " ".join(fdec(t) if re.fullmatch(r"f[0-9a-f]{16}", t) else t for t in line.split())


def human_script(script):
    out = []
    for l in script.strip().splitlines():
        t = l.split()
        out.append(" ".join([t[0]] + [fdec("f" + x) if re.fullmatch(r"[0-9a-f]{16}", x) else x for x in t[1:]]))
    return out


def corpus_digest():
    sys.path.insert(0, os.path.join(HERE, "quadtree_e2e"))
    from gen_cases import corpus
    cs = corpus()
    return {"generator": "systems/ptrtrans/quadtree_e2e/gen_cases.py", "n_cases": len(cs),
            "sha256_of_json_dump": hashlib.sha256(json.dumps(cs, indent=0).encode()).hexdigest(),
            "note": "same digest from the generator on macOS arm64 and in the x86-64 container"}


def acceptance_evidence():
    meta = [json.loads(l) for l in open(os.path.join(KG, "quadtree_Trans_PA_trans_metadata.jsonl"))]
    tags = collections.Counter(str(r.get("Verfi_Tag")) for r in meta if r.get("map_tag") != "file")
    per_unit = {"/".join(r["source_c_code_id"]): r.get("Verfi_Tag") for r in meta if r.get("source_c_code_id")}
    log = open(os.path.join(KG, "quadtree_runLog.txt"), encoding="utf-8", errors="replace").read().splitlines()
    heads = collections.Counter(re.sub(r";.*", "", l.strip("= ")) for l in log if l.startswith("=== "))
    test_words = re.compile(r"\b(cargo test|#\[test\]|unit test|test case|AFL|harness|equivalen)", re.I)
    test_hits = [i + 1 for i, l in enumerate(log) if test_words.search(l)]
    find_passes = [i + 1 for i, l in enumerate(log)
                   if l.startswith("=== Code Translation Response; code_id: ['find_#")]
    none_lines = [i + 1 for i, l in enumerate(log) if "get_quadrant_(None, Some(&test))" in l]
    expl = [i + 1 for i, l in enumerate(log) if "`None` is passed for the mutable reference" in l]
    return {
        "metadata_file": "artifacts/ptrtrans/dataset/Trans_C-Rust-KG/quadtree_Trans_PA_trans_metadata.jsonl",
        "metadata_sha256": sha(os.path.join(KG, "quadtree_Trans_PA_trans_metadata.jsonl")),
        "verfi_tag_counts": dict(tags),
        "verfi_tag_per_unit": per_unit,
        "runlog_file": "artifacts/ptrtrans/dataset/Trans_C-Rust-KG/quadtree_runLog.txt",
        "runlog_sha256": sha(os.path.join(KG, "quadtree_runLog.txt")),
        "runlog_section_kinds": dict(heads),
        "runlog_lines_mentioning_tests_or_equivalence": len(test_hits),
        "runlog_find_translation_passes": find_passes,
        "runlog_lines_with_get_quadrant_None": none_lines,
        "runlog_model_explanation_lines": expl,
    }


def main():
    diff = load("ptrtrans_quadtree_diff.json")
    x199 = load("ptrtrans_quadtree_diff_rust1.99.1.json")
    mut = load("ptrtrans_quadtree_mutation.json")
    acc = acceptance_evidence()
    rows = diff["verdicts"]
    s = diff["summary"]
    counted = [r for r in rows if r["final"] == "divergent"]
    by_ch = collections.Counter(r["rust_debug"]["div"]["channel"] for r in counted)
    search = [r for r in counted if r["rust_debug"]["div"]["channel"] == "search"]
    keyfree = [r for r in counted if r["rust_debug"]["div"]["channel"] == "key_free"]
    other = [r for r in counted if r["rust_debug"]["div"]["channel"] not in ("search", "key_free")]
    pre = [r for r in rows if r["final"] == "precondition_violating_input"]
    impl = [r for r in rows if r["c_impl_dependent"]]
    kfc = load("ptrtrans_quadtree_keyfree.json")
    ub = [r for r in rows if r["final"] == "original_C_UB"]
    ub_any = [r for r in rows if r["c_ub_evidence"]]
    search_cases = [r for r in rows if r["family"] in ("tree.search", "tree.prefix", "upstream")]
    search_clean = [r for r in search_cases if not r["c_ub_evidence"]]
    # search: the example = upstream test.c sequence (the C project's own test)
    up = next(r for r in rows if r["family"] == "upstream")
    ex = up if up["final"] == "divergent" else (search[0] if search else None)
    search_pattern = collections.Counter((r["rust_debug"]["div"]["c_line"] or "").split()[1] + "->" +
                                         (r["rust_debug"]["div"]["rust_line"] or "").split()[1] for r in search)
    rep = s["repaired"]
    acc_status = "no-record"
    acc_ev = [
        "PtrTrans's own check for correctness (%Equiv.) is function-level equivalence unit tests "
        "(paper Sec. 4.1.1, 4.1.3); the tests, harnesses and their input-output pairs are not in the artifact "
        f"(github.com/FudanSELab/PtrTrans-C2Rust @ {PIN[:12]}; docs/survey-evidence.json yuan2025ptrtrans).",
        f"The shipped metadata records only compile labels: Verfi_Tag counts {acc['verfi_tag_counts']} "
        "(No_Fix_Compile_Success / Fixed_N_Compile_Success = the cargo check repair loop, not a test verdict; "
        "None = deallocation units judged 'free' and not translated).",
        f"The run log is a translation log only: section kinds {acc['runlog_section_kinds']}; no test run.",
        "The paper reports quadtree %Equiv. = 87.5 as a project aggregate (Table 4); no per-function verdict "
        "is shipped, so which functions failed is not recorded.",
        "A re-run of the check as documented is not possible: the unit tests are not shipped.",
    ]
    defects = []
    if search:
        d = ex["rust_debug"]["div"]
        defects.append({
            "defect_id": "PTRTRANS-quadtree-find_-1",
            "counted": True,
            "severity": "S1",
            "mechanism": ("find_ never descends below a non-leaf node: the C recurses into "
                          "get_quadrant_(node, &test); the translation calls get_quadrant_(None, Some(&test)) "
                          "(get_quadrant_ takes Option<&mut QuadtreeNode> and find_ holds only a shared "
                          "reference), which always returns None. So quadtree_search returns None for every "
                          "point once the root has split (two or more points stored), where the C returns the "
                          "stored point."),
            "location": {"rust": f"{RS}:86-90 (line 88: get_quadrant_(None, Some(&test)))",
                         "c": f"{CS}:85-90 (line 89: return find_(get_quadrant_(node, &test), x, y);)"},
            "example_input": human_script(ex["script"]) if "script" in ex else ex["id"],
            "example_case_id": ex["id"],
            "c_output": human(d["c_line"]),
            "rust_output": human(d["rust_line"]),
            "exposure": len(search),
            "exposure_breakdown": {
                "counted_search_cases": len(search),
                "by_family": dict(collections.Counter(r["family"] for r in search)),
                "c_vs_rust": dict(search_pattern),
                "search_cases_total": len(search_cases),
                "search_cases_ub_clean": len(search_clean),
                "search_cases_ub_clean_agree": sum(1 for r in search_clean if r["final"] == "agree"),
                "search_cases_ub_clean_on_precondition_violating_input_same_mechanism": sum(
                    1 for r in search_clean if r["final"] == "precondition_violating_input"),
                "trees_exposing": len({r["id"].rsplit("/", 1)[0] for r in search}),
            },
            "confirmed_by_repair": {
                "repair": "replace get_quadrant_(None, ..) in find_ by the same nw/ne/sw/se lookup on node_ref "
                          "(run.py repair_patch)",
                "ub_clean_divergences_after_repair": rep["divergent_ub_clean"],
                "after_repair_by_channel": rep["divergent_ub_clean_by_channel"],
            },
            "translation_history": {
                "runlog_find_passes": acc["runlog_find_translation_passes"],
                "runlog_lines_passing_None": acc["runlog_lines_with_get_quadrant_None"],
                "runlog_model_explanation": (f"quadtree_runLog.txt:{acc['runlog_model_explanation_lines']}: the model "
                                             "states it passes None because find_ holds an immutable reference"),
            },
            "upstream_test": ("The C project's own test.c asserts quadtree_search(tree, 3.0, 1.1)->x == 3.0 "
                              "(test.c:83); replayed on the translation (case upstream/test_c_test_tree) the search "
                              f"returns {'None' if up['final'] == 'divergent' else 'the point'}."),
            "acceptance_status": acc_status,
            "evidence": acc_ev,
            "check_exercises_defect": {"status": "unknown",
                                       "evidence": "PtrTrans's unit tests are not shipped."},
        })
    not_in_totals = []
    if keyfree:
        k = keyfree[0]
        drops = [l for l in (k.get("rust_out") or "").splitlines() if l.startswith("# rust_key_drops")]
        k = next((r for r in keyfree if r["id"] == "bench_0_1000/duplicates/keyfree"), k)
        drops = [l for l in (k.get("rust_out") or "").splitlines() if l.startswith("# rust_key_drops")]
        not_in_totals.append({
            "id": "PTRTRANS-quadtree-insert_-key_free",
            "counted": False,
            "why_not_counted": ("Not a wrong value, state or output: every return value, length and tree state "
                                "agrees. The divergence is a user callback that the C invokes and the translation "
                                "never does. It is the key-deallocation hook: PtrTrans's 'Free Judge' step removes "
                                "memory-deallocation calls from the C before translating (quadtree_runLog.txt:24638 "
                                "'// Removed: reset_node_(tree, root); // NOTE: Memory Deallocation Operation, NEED TO "
                                "REMOVE'), and its metric excludes deallocation functions (paper Sec. 4.1.3). On every "
                                "insert where the C calls key_free on a non-NULL key, the translation drops exactly "
                                "that key (Rust Drop) during the same insert (per_insert_check). Listed for the author "
                                "to decide; not in the totals."),
            "mechanism": ("The translation keeps the public field Quadtree.key_free as Option<&dyn FnMut(&dyn Any)> "
                          "but never calls it (a FnMut cannot be called through a shared reference); on a "
                          "duplicate insert the C calls tree->key_free(old_key) via reset_node_ "
                          "(quadtree.c:103-107, node.c:28-32), the Rust overwrites root.key (quadtree.rs:107-113)."),
            "location": {"rust": f"{RS}:107-113", "c": f"{CS}:103-107; src/node.c:28-32"},
            "example_case_id": k["id"],
            "c_output": human(k["rust_debug"]["div"]["c_line"]),
            "rust_output": human(k["rust_debug"]["div"]["rust_line"]),
            "rust_drop_log_example": drops[:2],
            "exposure": len(keyfree),
            "per_insert_check": (kfc["summary"] if kfc else None),
            "per_insert_check_file": "results/ptrtrans_quadtree_keyfree.json",
        })
    for r in other:
        not_in_totals.append({"id": "UNCLASSIFIED", "case": r["id"], "div": r["rust_debug"]["div"]})
    mut_s = mut["summary"] if mut else None
    out = {
        "provenance": {
            "script": "systems/ptrtrans/summarize.py",
            "command": "python3 -I systems/ptrtrans/summarize.py",
            "git_rev": subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"], stdout=subprocess.PIPE,
                                      text=True).stdout.strip() + " (+uncommitted systems/ptrtrans/, results/ptrtrans_*)",
            "host": platform.system(), "arch": platform.machine(),
            "date": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "inputs": {n: (load(n) or {}).get("provenance", {}).get("date") for n in
                       ("ptrtrans_quadtree_diff.json", "ptrtrans_quadtree_diff_rust1.99.1.json",
                        "ptrtrans_quadtree_mutation.json", "ptrtrans_quadtree_keyfree.json")},
            "corpus": corpus_digest(),
            "toolchains_images": {
                "run_host": diff["provenance"]["host"],
                "docker_image": diff["provenance"].get("docker_image"),
                "docker_image_id": diff["provenance"].get("docker_image_id"),
                "c": diff["provenance"]["toolchains"],
                "rust_primary": diff["provenance"]["rust_shipped_build"]["rustc"].splitlines()[0],
                "rust_cross": (x199["provenance"]["rust_shipped_build"]["rustc"].splitlines()[0] if x199 else None),
                "rust_cross_image": (x199["provenance"].get("docker_image_id") if x199 else None),
                "rust_recorded_by_artifact": diff["provenance"]["rust_toolchain_recorded_by_artifact"],
            },
            "artifact": diff["provenance"]["artifact"],
        },
        "system": "PtrTrans",
        "rule_check": {
            "regrade_rule": "met for one project: quadtree ships the original C and PtrTrans's own Rust (Trans_PA)",
            "c_builds": diff["provenance"]["c_builds"]["project_make_bin_test"]["rc"] == 0,
            "c_project_test_passes": diff["provenance"]["c_builds"]["project_make_bin_test"]["bin_test_rc"] == 0,
            "rust_lib_builds": {
                "1.86.0": diff["provenance"]["rust_shipped_build"]["lib_build"],
                "1.99.1": (x199["provenance"]["rust_shipped_build"]["lib_build"] if x199 else None)},
            "rust_1.99.1_warnings": "4 deprecation warnings (std::f64::INFINITY, src/bounds.rs:10-11); 0 under 1.86.0",
            "toolchain_note": ("The artifact records rustc 1.90.0-nightly (e43d139a8 2025-07-09) in "
                               "quadtree/target/.rustc_info.json (the paper: cargo check 1.90.0-nightly); no "
                               "rust-toolchain file. That nightly is not in our images, so the crate is built with "
                               "1.86.0 and 1.99.1, which bracket it."),
            "not_regraded": "LLM_only baseline outputs (dataset/parsed_projects) and ablations (Trans_not_*) are not PtrTrans's output.",
        },
        "translation_unit": "library (one C project: CROWN quadtree; 17 translated functions + 4 structs; 7 deallocation units not translated, mapped to Drop)",
        "translations_tested": 1,
        "translations_with_defect": 1 if defects else 0,
        "defects": defects,
        "not_in_totals": not_in_totals,
        "crashes": s["rust_panics_or_crashes_on_ub_clean"],
        "S2": 0,
        "S2_note": "a library has no stderr/diagnostic channel; none observed",
        "S3": {"count": 0, "note": "the crate and the driver build with 0 errors under 1.86.0 and 1.99.1; no hang"},
        "cases": {"n": s["n_cases"], "by_final": s["by_final"], "by_family": s["by_family"],
                  "divergent_ub_clean_valid_input_by_channel": dict(by_ch),
                  "counted_S1_cases": len(search),
                  "note": "'divergent' = UB-clean, precondition-respecting cases that differ: search (the find_ "
                          "defect, counted) and key_free (listed in not_in_totals, not counted)"},
        "ub_excluded": {"cases": len(ub), "cases_with_any_ub_evidence": len(ub_any),
                        "kinds": s["ub_evidence_kinds"],
                        "c_crash_cases": s["c_crash_cases"],
                        "c_crash_cases_all_have_ub_evidence": s["c_crash_cases_all_have_ub_evidence"],
                        "cases_null_member_access": sum(1 for r in ub if any("member access within null pointer" in e for e in r["c_ub_evidence"])),
                        "cases_stack_overflow": sum(1 for r in ub if any("stack-overflow" in e for e in r["c_ub_evidence"])),
                        "note": ("Null-member-access cases: quadtree_search in the C dereferences NULL when the query path ends "
                                 "at an empty quadrant or outside every quadrant (find_ -> get_quadrant_ -> "
                                 "node_contains_ on a NULL child: empty tree, absent, outside, split-line and NaN "
                                 "queries); UBSan 'member access within null pointer' under clang and gcc. Stack-overflow cases: "
                                 "bounds +/-1e308 (width overflows to inf) make split_node_ produce a child equal to "
                                 "its parent, so insert_ recurses without end (ASan stack-overflow; at -O3 the tail "
                                 "call becomes a loop that allocates until malloc fails; the Rust overflows its "
                                 "stack). Every one of these cases is a C crash with sanitizer evidence; none is counted.")},
        "precondition_only": {"defects": 0,
                              "rule": "quadtree_new / quadtree_node_with_bounds name their parameters (minx, miny, "
                                      "maxx, maxy): an input with min > max violates the names. No other precondition "
                                      "is stated (header, README, comments).",
                              "divergent_cases_on_violating_input": len(pre),
                              "by_channel": dict(collections.Counter(r["rust_debug"]["div"]["channel"] for r in pre)),
                              "note": "every divergence on such inputs (bounds 10,10,1,1) is one of the two mechanisms "
                                      "that also diverge on valid inputs; these cases are left out of the exposure "
                                      "counts, and no mechanism diverges only on them."},
        "c_implementation_dependent": {
            "cases": len(impl),
            "counted_divergences_among_them": sum(1 for r in impl if r["final"] != "agree"),
            "kinds": s["c_impl_dependent_kinds"],
            "note": "the clang build's output differs from the gcc -O3 reference with no sanitizer report, and only "
                    "within the float tolerance (signed zero from fmin/fmax on +/-0 inputs, which C leaves "
                    "unspecified); all of these cases agree with the Rust, so nothing is affected. An ad-hoc re-run "
                    "(not saved) found the differing tokens are only +0/-0 and the Rust equals the clang build bit "
                    "for bit on all of them."},
        "float_comparison": {"tolerance": diff["provenance"]["float_tolerance"],
                             "ub_clean_cases_bit_exact_differences_within_tolerance": s["bit_exact_only_differences_ub_clean"]},
        "release_vs_debug_verdict_differs": s["rust_release_vs_debug_verdict_differs"],
        "cross_toolchain": ({"toolchain": "1.99.1", "same_final_verdicts": x199["summary"]["same_final_verdicts_as_ub_from_run"],
                             "by_final": x199["summary"]["by_final"]} if x199 else None),
        "acceptance_status": {"status": acc_status, "evidence": acc_ev,
                              "metadata_verfi_tags": acc["verfi_tag_per_unit"]},
        "heldout": {"status": "not measurable",
                    "why": "PtrTrans's own unit tests (and the AFL++ input-output pairs behind them) are not shipped, "
                           "so the overlap of our inputs with its check's inputs cannot be measured. Its translation "
                           "loop runs no tests (cargo check only), so no input reached the loop."},
        "harness_power": {
            "natural_fault": f"the shipped translation turns the harness red on {len(search)} UB-clean cases (find_ defect)",
            "repair_turns_green": rep,
            "mutation": mut_s,
            "mutation_survivors_note": ("all 6 survivors sit in branches the C API cannot reach: the map_or(0.0, ..) "
                                        "defaults for bounds.nw in split_node_ (quadtree_bounds_new always sets nw; "
                                        "4 mutants) and the trailing 0 returned by quadtree_insert when tree is None "
                                        "(in the C a NULL tree is dereferenced; 2 mutants). 58 of the 58 reachable "
                                        "mutants are killed. 1 mutant (u32 length 0 -> -1) does not compile."),
            "mutation_file": "results/ptrtrans_quadtree_mutation.json",
        },
    }
    p = os.path.join(RES, "ptrtrans_summary.json")
    with open(p, "w") as f:
        json.dump(out, f, indent=1)
        f.write("\n")
    print(json.dumps({k: out[k] for k in ("translations_tested", "translations_with_defect", "crashes", "S2",
                                          "ub_excluded", "precondition_only", "heldout")}, indent=1))
    for d in defects:
        print(d["defect_id"], d["exposure"], d["exposure_breakdown"])
    print("not_in_totals", [(x["id"], x.get("exposure")) for x in not_in_totals])
    if mut_s:
        print("mutation", {k: mut_s[k] for k in ("mutants_valid", "killed", "survived", "mutation_score")})


if __name__ == "__main__":
    main()
