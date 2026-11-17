"""Issue 4 (arXiv): FLOURINE rebuilt in the profile its own check uses, compared with the shipped run.

FLOURINE's check (oracle.py:196, Differential_Tester/Makefile:49-57) runs `cargo bolero test`, which
builds with cargo profile "fuzz" (cargo-bolero 0.10 default `--profile fuzz`); FLOURINE's Cargo.toml
template defines it as inherits = "dev", opt-level = 3, incremental = false, codegen-units = 1
(Differential_Tester/instrumentor/src/template.rs:37-41), so debug assertions and overflow checks are
on; cargo-bolero also adds -Cdebug-assertions -Coverflow_checks to RUSTFLAGS. Our drivers build with
rustc -O (overflow wraps). This script compares

  fuzz profile:    harness/oracle_ub_flourine_current.py --profile fuzz  (phase rust + phase c, same
                   grid, same rustc 1.86.0 image, same C oracle and UB rule as the shipped run)
  release profile: results/flourine_diff_current.json, results/flourine_struct_current.json (shipped)

per translation (every success- and fail-labelled file both drivers reach), and maps the changes onto
the FLOURINE rows of results/defect_ledger.json. A divergence counts only on an input whose C is
UB-clean under the shipped rule (harness/oracle_ub_flourine_current.rule_clean).

  python3 -I harness/arxiv_flourine_devprofile.py --scalar <fuzz scalar json> --struct <fuzz struct json> \
      [--out results/arxiv_flourine_devprofile.json]
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "scripts"))
from provenance import stamp  # noqa: E402

# benchmark-prefix of each FLOURINE ledger row, per driver (struct tags carry no "opl/" prefix)
LEDGER_PREFIX = {
    "FLOURINE-operator_list-1": ("scalar", "opl/opl_emu_registers_operator_list/"),
    "FLOURINE-lfo_pm_depth-1": ("struct", "opl_emu_registers_lfo_pm_depth/"),
    "FLOURINE-clip-1": ("scalar", "libopenaptx/clip/"),
    "FLOURINE-opl_emu_clamp-1": ("scalar", "opl/opl_emu_clamp/"),
}

CHECK_PROFILE_EVIDENCE = {
    "flourine_cargo_template": "artifacts/flourine_dl/flourine-artifact/Differential_Tester/instrumentor/src/template.rs:37-41: "
                               "[profile.fuzz] inherits = \"dev\", opt-level = 3, incremental = false, codegen-units = 1",
    "flourine_check_command": "artifacts/flourine_dl/flourine-artifact/oracle.py:169-201: RUSTFLAGS=\"-L {fuzz_target}\"; "
                              "cargo bolero test --manifest-path {fuzz_target}/Cargo.toml --features fuzzing <test> "
                              "--target-dir {fuzz_target}/target/__fuzz__ --sanitizer NONE --engine-args=\"-rss_limit_mb=8096 -max_len=32768\" "
                              "(no --profile, no --release); same in Differential_Tester/Makefile:49-57 (max_len=131072)",
    "cargo_bolero_defaults": "cargo-bolero 0.10.0 (github.com/camshaft/bolero commit 36d41b8fea, 'chore: release 0.10.0', "
                             "2023-09-28; same file in 0.10.1, f401669697) bin/cargo-bolero/src/project.rs:26-30 "
                             "`--profile` default_value = \"fuzz\"; :93 passes `--profile fuzz` to cargo; :139-146 RUSTFLAGS "
                             "\"--cfg fuzzing -Cdebug-assertions -Ctarget-cpu=native -Cdebuginfo=2 -Coverflow_checks -Clink-dead-code\" "
                             "+ -Cpasses=sancov-module + libfuzzer coverage flags + the caller's RUSTFLAGS",
    "cargo_bolero_version_used_by_flourine": "not recorded in the artifact (the template pins the bolero crate 0.10.0); "
                                             "cargo-bolero 0.9.0 (cargo-bolero/src/project.rs) builds --release instead of a profile "
                                             "but adds the same -Cdebug-assertions -Coverflow_checks, so overflow checks are on in either",
    "dev_profile_defaults": "cargo's dev profile: debug-assertions = true, overflow-checks = true, panic = unwind",
    "effective": "opt-level 3, debug-assertions on, overflow-checks on, codegen-units 1, debuginfo 2, panic = unwind",
}

# Hand-read mechanisms of the success files whose verdict changes (source lines checked; panic sites from
# rebuilding each file with the fuzz-profile flags and a plain main, crust-syzygy, rustc 1.86.0).
TR = "artifacts/flourine_dl/flourine-artifact/translations_only/success/c/opl"
BM = "artifacts/flourine_dl/flourine-artifact/bms/c/opl"
MECHANISMS = {
    "opl_emu_registers_lfo_pm_depth/claude3/BaseRepair/opl_emu_registers_lfo_pm_depth.rs":
        f"{TR}/opl_emu_registers_lfo_pm_depth/claude3/BaseRepair/opl_emu_registers_lfo_pm_depth.rs:18 "
        "`(1u32.shr(length as u32)) - 1` with length 1 is 0 - 1: wraps to 0xFFFFFFFF under rustc -O (wrong value on 4 "
        "of 8 inputs), panics 'attempt to subtract with overflow' under overflow checks (on every call, all 8 inputs). "
        f"C {BM}/opl_emu_registers_lfo_pm_depth/opl_emu_registers_lfo_pm_depth.c:35 `(1 << length) - 1`.",
    "opl_emu_registers_lfo_am_depth/claude3/CAPR/opl_emu_registers_lfo_am_depth.rs":
        f"{TR}/opl_emu_registers_lfo_am_depth/claude3/CAPR/opl_emu_registers_lfo_am_depth.rs:18 same helper as lfo_pm_depth, "
        "`(1u32.shr(length as u32)) - 1` = 0 - 1 for length 1. The accessor reads bit 7 of a u8 "
        f"({BM}/opl_emu_registers_lfo_am_depth/opl_emu_registers_lfo_am_depth.c:41, start 7, count 1), so the "
        "wrapped all-ones mask still gives the right value under rustc -O (agrees on 8 of 8); under overflow checks "
        "every call panics.",
    "opl/opl_emu_opl_compute_phase_step/gpt4/BaseRepair/opl_emu_opl_compute_phase_step.rs":
        f"{TR}/opl_emu_opl_compute_phase_step/gpt4/BaseRepair/opl_emu_opl_compute_phase_step.rs:8 "
        "`(lfo_raw_pm as u32) * opl_emu_bitfield(block_freq, 7, 3)` and :11 `phase_step * multiple` are u32 "
        "multiplications; the C (opl_emu_opl_compute_phase_step.c, `lfo_raw_pm * opl_emu_bitfield(...)` int32*uint32 "
        "-> unsigned, and `phase_step * multiple` uint32) wraps, which C defines. rustc -O wraps too (agrees on 2000 of "
        "2000); overflow checks panic 'attempt to multiply with overflow' (line 8:34 for a negative lfo_raw_pm, e.g. "
        "(16383, 1, -1); line 11:5 e.g. (1048575, 1048575, 65)).",
    "opl/opl_emu_opl_compute_phase_step/gpt4/CAPR/opl_emu_opl_compute_phase_step.rs":
        "byte-identical to the gpt4/BaseRepair file (same sha256 3451bf1e664f in results/flourine_shipped_vs_log.json); same mechanism.",
}


def load(p):
    with open(p) as f:
        return json.load(f)


def key(r):
    return r["tag"]


def ub_counts(r):
    u = r.get("ub_rule") or {}
    return {k: u.get(k, 0) for k in ("value_inputs", "value_inputs_ub_clean", "value_inputs_normal_ub_clean",
                                     "crash_inputs", "crash_inputs_ub_clean", "hang_inputs")}


def examples(r, kind, n=3):
    """Smallest-magnitude stored divergent inputs of one kind (from the first 20 the run keeps)."""
    xs = [d for d in (r.get("divergent_inputs_first") or []) if d["kind"] == kind]

    def mag(d):
        return sum(abs(x) for x in d["in"]) if isinstance(d["in"], list) else 0
    xs.sort(key=lambda d: (not d.get("rule_clean", False), mag(d)))
    return [{"in": d["in"], "c": d["c"], "rust": d["rust"], "ub_clean": d.get("rule_clean"),
             "degenerate_min_gt_max": d.get("degenerate_min_gt_max"),
             "c_ub_flags": sorted(d.get("flags", {}))} for d in xs[:n]]


def compare(rel_rows, fz_rows, driver):
    rel = {key(r): r for r in rel_rows}
    fz = {key(r): r for r in fz_rows}
    rows, changes = [], []
    for t in sorted(set(rel) | set(fz)):
        a, b = rel.get(t, {}), fz.get(t, {})
        row = {"driver": driver, "tag": t, "label": a.get("label") or b.get("label"),
               "release": {"status": a.get("status"), "verdict": a.get("verdict"), "tally": a.get("tally"),
                           "ub_rule": ub_counts(a) if a.get("ub_rule") else None},
               "fuzz": {"status": b.get("status"), "verdict": b.get("verdict"), "tally": b.get("tally"),
                        "ub_rule": ub_counts(b) if b.get("ub_rule") else None}}
        row["verdict_changed"] = (a.get("status"), a.get("verdict")) != (b.get("status"), b.get("verdict"))
        row["tally_changed"] = a.get("tally") != b.get("tally")
        rows.append(row)
        if row["verdict_changed"] or row["tally_changed"]:
            ch = dict(row)
            ch["fuzz_examples"] = {"crash": examples(b, "crash"), "value": examples(b, "value")}
            ch["release_examples"] = {"value": examples(a, "value")}
            ch["fuzz_divergent_inputs_stored"] = len(b.get("divergent_inputs_first") or [])
            changes.append(ch)
    return rows, changes


def severity_under(files_rows, degenerate):
    """Ledger-style classification of one defect row from its files' UB-rule counts."""
    val = sum((r["ub_rule"] or {}).get("value_inputs_normal_ub_clean" if not degenerate else "value_inputs_ub_clean", 0)
              for r in files_rows)
    crash = sum((r["ub_rule"] or {}).get("crash_inputs_ub_clean", 0) for r in files_rows)
    if val > 0:
        return "S1", val, crash
    if crash > 0:
        return "crash", val, crash
    return "none", val, crash


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scalar", required=True, help="fuzz-profile scalar output of oracle_ub_flourine_current.py")
    ap.add_argument("--struct", required=True, help="fuzz-profile struct output of oracle_ub_flourine_current.py")
    ap.add_argument("--rel-scalar", default=os.path.join(REPO, "results", "flourine_diff_current.json"))
    ap.add_argument("--rel-struct", default=os.path.join(REPO, "results", "flourine_struct_current.json"))
    ap.add_argument("--ledger", default=os.path.join(REPO, "results", "defect_ledger.json"))
    ap.add_argument("--out", default=os.path.join(REPO, "results", "arxiv_flourine_devprofile.json"))
    ap.add_argument("--shipped-vs-log", default=os.path.join(REPO, "results", "flourine_shipped_vs_log.json"))
    ap.add_argument("--probe", help="JSON from the full-grid crash-input listing of opl_emu_opl_compute_phase_step")
    ap.add_argument("--probe-command", default=None)
    ap.add_argument("--run-script", help="the shell script that ran both phases (embedded verbatim)")
    a = ap.parse_args()
    fs, ft = load(a.scalar), load(a.struct)
    rs, rt = load(a.rel_scalar), load(a.rel_struct)
    led = load(a.ledger)
    svl = {r["translation"]: r for r in load(a.shipped_vs_log)["rows"]}

    s_rows, s_changes = compare(rs["per_translation"], fs["per_translation"], "scalar (systems/flourine/run.py)")
    t_rows, t_changes = compare(rt["per_translation"], ft["per_translation"], "struct (systems/flourine/run_struct.py)")
    for ch in s_changes + t_changes:
        full = ch["tag"] if ch["driver"].startswith("scalar") else "opl/" + ch["tag"]
        if ch["label"] == "success" and full in svl:
            v = svl[full]
            ch["flourine_log_record"] = {k: v.get(k) for k in ("table_final_status", "table_status_detail", "shipped_sha256",
                                                               "shipped_equals", "first_verification", "final_verdict",
                                                               "accepted_candidate_file", "log_record_for_shipped_content")}
            ch["flourine_log_record"]["source"] = "results/flourine_shipped_vs_log.json"
        if ch["tag"] in MECHANISMS:
            ch["mechanism"] = MECHANISMS[ch["tag"]]
    per = {r["tag"]: r for r in s_rows}
    perT = {r["tag"]: r for r in t_rows}

    # ---- ledger rows
    ledger_rows = []
    covered = set()
    for row in [r for r in led["rows"] if r["system"] == "Flourine"]:
        drv, pre = LEDGER_PREFIX[row["defect_id"]]
        src = per if drv == "scalar" else perT
        units = [u.split(":", 1)[1] for u in row["translation_units"]]          # the files the row counts
        units = [u[len("opl/"):] if drv == "struct" else u for u in units]     # struct tags have no "opl/"
        files = [src[u] for u in units]
        assert all(f["tag"].startswith(pre) and f["label"] == "success" for f in files), row["defect_id"]
        covered |= {r["tag"] for r in files}
        deg = bool(row.get("degenerate_input_only"))
        sev_rel = severity_under([{"ub_rule": r["release"]["ub_rule"]} for r in files], deg)
        sev_fz = severity_under([{"ub_rule": r["fuzz"]["ub_rule"]} for r in files], deg)
        ledger_rows.append({
            "defect_id": row["defect_id"], "ledger_severity": row["severity"], "count_status": row.get("count_status"),
            "degenerate_input_only": deg,
            "files": {r["tag"]: {"release": r["release"], "fuzz": r["fuzz"]} for r in files},
            "release_profile": {"class": sev_rel[0], "ub_clean_value_inputs": sev_rel[1], "ub_clean_crash_inputs": sev_rel[2]},
            "fuzz_profile": {"class": sev_fz[0], "ub_clean_value_inputs": sev_fz[1], "ub_clean_crash_inputs": sev_fz[2]},
            "class_changes": sev_rel[0] != sev_fz[0],
        })

    # ---- success translations outside the ledger rows that diverge in the fuzz profile on a UB-clean input
    new_candidates = []
    for src in (per, perT):
        for t, r in src.items():
            if r["label"] != "success" or t in covered:
                continue
            u = r["fuzz"]["ub_rule"] or {}
            if r["fuzz"]["verdict"] not in (None, "agree"):
                new_candidates.append({"tag": t, "driver": r["driver"], "release_verdict": r["release"]["verdict"],
                                       "fuzz_verdict": r["fuzz"]["verdict"], "fuzz_tally": r["fuzz"]["tally"],
                                       "fuzz_ub_rule": u,
                                       "counts_under_ub_rule": (u.get("value_inputs_ub_clean", 0) + u.get("crash_inputs_ub_clean", 0)) > 0})
            elif r["release"]["status"] == "tested" and r["fuzz"]["status"] != "tested":
                new_candidates.append({"tag": t, "driver": r["driver"], "release_status": r["release"]["status"],
                                       "fuzz_status": r["fuzz"]["status"], "note": "tested in release, not in fuzz profile"})

    # ---- totals
    tot = led["totals"]
    paper = {"defects": sum(v["defects_counted"]["total"] for v in tot.values()),
             "wrong_output_S1": sum(v["defects_counted"]["S1"] for v in tot.values()),
             "crash": sum(v["defects_counted"]["crash"] for v in tot.values())}
    moved = [lr for lr in ledger_rows if lr["count_status"] == "counted" and lr["class_changes"]]
    d_s1 = -sum(1 for lr in moved if lr["release_profile"]["class"] == "S1" and lr["fuzz_profile"]["class"] == "crash") \
        + sum(1 for lr in moved if lr["release_profile"]["class"] == "crash" and lr["fuzz_profile"]["class"] == "S1")
    lost = sum(1 for lr in ledger_rows if lr["count_status"] == "counted" and lr["fuzz_profile"]["class"] == "none")
    new_counting = [c for c in new_candidates if c.get("counts_under_ub_rule")]
    paper["translations_with_counted_defect"] = sum(v["translations_with_counted_defect"] for v in tot.values())
    after = {"defects": paper["defects"] - lost, "wrong_output_S1": paper["wrong_output_S1"] + d_s1,
             "crash": paper["crash"] - d_s1, "translations_with_counted_defect": paper["translations_with_counted_defect"]}
    by_bench = {}
    for c in new_counting:
        bench = c["tag"].split("/")[1] if c["driver"].startswith("scalar") else c["tag"].split("/")[0]
        u = c["fuzz_ub_rule"]
        by_bench.setdefault(bench, {"files": [], "class": "S1" if u.get("value_inputs_normal_ub_clean", 0) > 0 else "crash"})
        by_bench[bench]["files"].append(c["tag"])
    n_s1 = sum(1 for b in by_bench.values() if b["class"] == "S1")
    n_cr = sum(1 for b in by_bench.values() if b["class"] == "crash")
    effect = {
        "ledger_totals_now": paper,
        "after_reclassifying_existing_flourine_rows": after,
        "existing_counted_rows_that_change_class": [lr["defect_id"] for lr in moved],
        "existing_counted_rows_that_no_longer_diverge": lost,
        "new_success_translation_divergences_counting_under_ub_rule": len(new_counting),
        "new_candidate_defects_one_per_benchmark": by_bench,
        "if_new_candidates_counted_one_defect_per_benchmark": {
            "defects": after["defects"] + len(by_bench), "wrong_output_S1": after["wrong_output_S1"] + n_s1,
            "crash": after["crash"] + n_cr,
            "translations_with_counted_defect": after["translations_with_counted_defect"] + len(new_counting)},
        "note": ("the new candidates are not in the ledger; counting them needs ledger rows (mechanism grouping, "
                 "precondition and class review). The grouping above (one defect per benchmark, as the existing "
                 "FLOURINE rows) is an assumption, not a ledger decision."
                 if new_counting else "no success translation outside the ledger rows diverges on a UB-clean input"),
    }

    def summ(rows, fz_sum, rel_sum):
        succ = [r for r in rows if r["label"] == "success"]
        return {"release_summary": rel_sum, "fuzz_summary": fz_sum,
                "success_verdict_changes": sum(r["verdict_changed"] for r in succ),
                "fail_verdict_changes": sum(r["verdict_changed"] for r in rows if r["label"] == "fail"),
                "translations_with_tally_change": sum(r["tally_changed"] for r in rows)}

    out = {
        "provenance": {
            "script": "harness/arxiv_flourine_devprofile.py",
            "command": " ".join(["python3", "-I"] + sys.argv),
            "inputs": {"fuzz_scalar": a.scalar, "fuzz_struct": a.struct, "release_scalar": os.path.relpath(a.rel_scalar, REPO),
                       "release_struct": os.path.relpath(a.rel_struct, REPO), "ledger": os.path.relpath(a.ledger, REPO)},
            "fuzz_run": {k: fs["provenance"].get(k) for k in ("script", "command", "git_rev", "git_worktree_note", "host", "arch",
                                                              "kernel", "docker_image", "docker_image_id", "toolchains", "date",
                                                              "phases", "rust_profile", "rust_phase_wall_seconds",
                                                              "phase_c_wall_seconds", "artifact_source_digests",
                                                              "harness_script_digests", "ub_rule", "sanitizer_cflags")},
            "fuzz_run_shipped_script_runs": fs["provenance"].get("shipped_script_runs"),
            "release_run": {k: rs["provenance"].get(k) for k in ("command", "git_rev", "host", "date", "phases",
                                                                 "artifact_source_digests")},
            "same_artifact_sources_as_release_run": fs["provenance"].get("artifact_source_digests") == rs["provenance"].get("artifact_source_digests"),
            "same_rust_image_as_release_run": fs["provenance"].get("phases", {}).get("rust") == rs["provenance"].get("phases", {}).get("rust"),
            "same_c_image_as_release_run": fs["provenance"].get("phases", {}).get("c") == rs["provenance"].get("phases", {}).get("c"),
            "_stamp": stamp(),
        },
        "flourine_check_profile": CHECK_PROFILE_EVIDENCE,
        "rebuild": {"rustc_flags_fuzz": (fs["provenance"].get("rust_profile") or {}).get("rustc_flags"),
                    "rustc_flags_release": ["-O"],
                    "left_out": "-Ctarget-cpu=native, -Clink-dead-code, --cfg fuzzing, sancov/libfuzzer instrumentation, -L "
                                "(no translation uses cfg(fuzzing), cfg(debug_assertions) or debug_assert)",
                    "inputs": "unchanged: enumerate_cases(cap=2000) per scalar benchmark; run_struct feed (8 seeds x offset grid)",
                    "ub_rule": "unchanged: harness/oracle_ub_flourine_current.rule_clean (clang-14 and gcc-11 ASan+UBSan+"
                               "float-cast-overflow without -fwrapv; -O0/-O2 vs -O1; -funsigned-char/-fsigned-char)"},
        "scalar": summ(s_rows, fs["summary"], rs["summary"]) | {"iso_c_ub_rule_release": rs["iso_c_ub_rule"],
                                                               "iso_c_ub_rule_fuzz": fs["iso_c_ub_rule"]},
        "struct": summ(t_rows, ft["summary"], rt["summary"]) | {"iso_c_ub_rule_release": rt["iso_c_ub_rule"],
                                                               "iso_c_ub_rule_fuzz": ft["iso_c_ub_rule"]},
        "ledger_rows": ledger_rows,
        "success_translations_outside_ledger_rows_that_change": new_candidates,
        "effect_on_counts": effect,
        "changes": s_changes + t_changes,
    }
    if a.probe:
        out["compute_phase_step_full_grid_crash_inputs"] = {
            "command": a.probe_command, "note": "all crash inputs of the two gpt4 files over the run.py grid (the run stores "
            "only the first 20), smallest first; probes_outside_grid are extra inputs within OPL register ranges, not "
            "part of the counted grid", **load(a.probe)}
    if a.run_script:
        with open(a.run_script) as f:
            out["provenance"]["run_script"] = {"path": "~/work/arxiv-flprofile/" + os.path.basename(a.run_script),
                                               "text": f.read()}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
        f.write("\n")
    print(json.dumps({"scalar": {k: out["scalar"][k] for k in ("success_verdict_changes", "fail_verdict_changes",
                                                               "translations_with_tally_change")},
                      "struct": {k: out["struct"][k] for k in ("success_verdict_changes", "fail_verdict_changes",
                                                               "translations_with_tally_change")},
                      "ledger": [{k: lr[k] for k in ("defect_id", "count_status", "release_profile", "fuzz_profile")}
                                 for lr in ledger_rows],
                      "new_candidates": len(new_candidates), "effect": effect}, indent=1))


if __name__ == "__main__":
    main()
