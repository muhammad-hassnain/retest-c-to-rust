"""Assemble results/profile_check.json from the per-system outputs of the profile_check scripts.

  python3 systems/profile_check/assemble.py DIR_WITH_PER_SYSTEM_JSON results/profile_check.json
  python3 systems/profile_check/assemble.py --add-adatrans results/profile_check.json ADATRANS_JSON OUT.json
  python3 systems/profile_check/assemble.py --add-flourine-fuzz results/profile_check.json results/arxiv_flourine_devprofile.json OUT.json

DIR holds vert.json, c2sr.json, rustmap.json, actor.json, sactor.json, flourine.json (the files the
per-system scripts write; the box ones are copied back from ~/work/profile-check/out/), and optionally
adatrans.json (systems/profile_check/adatrans_profile.sh, copied back from ~/work/adatrans-profile/out/).
--add-adatrans keeps every non-AdaTrans row, not_checked and the earlier provenance of an existing
profile_check.json verbatim, (re)builds the AdaTrans rows with the same block as the full run, and
recomputes the summary with the same formulas.
Per-defect verdict `diverges_under_other_profile` is decided on the ledger example input; the
exposing-input counts record whether every exposing input still diverges.
"""
import hashlib, json, os, sys, time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
led = {r["defect_id"]: r for r in json.load(open(os.path.join(REPO, "results", "defect_ledger.json")))["rows"]}
entries, not_checked = [], []


def add(sysname, did, counted, other, ex_out_c, ex_out_o, diverges, n_chk, n_div, evidence, note=None, example=None):
    r = led.get(did, {})
    e = {"system": sysname, "defect_id": did, "severity": r.get("severity"), "counted_profile": counted,
         "other_profile": other, "example_input": example if example is not None else r.get("example_input"),
         "outcome_under_counted_profile": ex_out_c, "outcome_under_other_profile": ex_out_o,
         "diverges_under_other_profile": diverges, "exposing_inputs_checked": n_chk,
         "exposing_inputs_diverging_under_other_profile": n_div, "evidence": evidence}
    if note:
        e["note"] = note
    entries.append(e)


# ---------------------------------------------------------------- AdaTrans (counted = debug, other = release)
ADATRANS_PROF = {"own_pipeline": "debug (its check runs `cargo build --quiet`, then target/debug/solution)",
                 "own_cite": "systems/adatrans/run_diff.py:12-15 (artifact src/runner.py check)",
                 "our_harness": "debug (cargo build, rustc 1.86.0)", "harness_cite": "systems/adatrans/run_diff.py:124-132",
                 "agreement": "same"}


def add_adatrans(at):
    for did, r in sorted(at["rows"].items()):
        ex = r["example"]
        o = lambda p: ex[p]["kind"] + f" (exit {ex[p]['rc']}" + (f", stdout {ex[p]['stdout_head'].strip()!r}" if ex[p]["kind"] == "S1" else
                                                                 f": {ex[p]['stderr_head'].strip().splitlines()[1] if len(ex[p]['stderr_head'].strip().splitlines()) > 1 else ''}") + ")"
        add("AdaTrans", did, "debug (cargo build; target/debug, as AdaTrans's check)", "release (cargo build --release)",
            o("debug"), o("release"), "yes" if ex["release"]["kind"] not in ("agree", "whitespace_only") else "no",
            r["n_cases"], r["diverging"]["release"],
            f"{r['rustc']}; pipeline extract (main.rs sha256 {r['rs_sha256'][:16]}) rebuilt in a scratch workspace, crust-heldout-plus186, offline vendored crates; "
            f"unchanged C oracle (pipeline c/{r['inst']}/ref, sha256 {r['c_ref_sha256'][:16]}, C stdout {ex['c']['stdout_head'].strip()!r}); "
            f"every UB-clean exposing case of the pipeline (cases_run1_ub.jsonl, kind panic, c_ub_clean): {r['by_src']}; "
            f"outcomes under debug {r['outcomes']['debug']}, under release {r['outcomes']['release']}",
            note=("integer overflow: release wraps the i32 sum instead of panicking and prints a wrong value on all %d (%d negative)"
                  % (r["n_cases"], sum(1 for c in r["cases"] if c["release"]["stdout_head"].startswith("-"))))
            if r["outcomes"]["release"].get("S1") else "bounds / unwrap panic, not overflow: identical in both profiles")


def summarize(entries, not_checked):
    crash_rows = [e for e in entries if e["severity"] == "crash"]
    nodiv = [e for e in entries if e["diverges_under_other_profile"] != "yes"]
    partial = [e["defect_id"] for e in entries if e["exposing_inputs_checked"] and 0 <= (e["exposing_inputs_diverging_under_other_profile"] or 0) < (e["exposing_inputs_checked"] or 0)
               and e["diverges_under_other_profile"] == "yes"]
    all_counted_crash = [r["defect_id"] for r in led.values() if r.get("count_status") == "counted" and r["severity"] == "crash"]
    return {"defects_checked": len(entries),
            "counted_crash_rows_in_ledger": len(all_counted_crash),
            "counted_crash_rows_checked": len(crash_rows),
            "crash_rows_not_checked": sorted(set(all_counted_crash) - {e["defect_id"] for e in crash_rows}),
            "diverge_under_other_profile": sum(1 for e in entries if e["diverges_under_other_profile"] == "yes"),
            "do_not_diverge_under_other_profile": [e["defect_id"] for e in nodiv],
            "yes_but_some_exposing_inputs_agree": partial,
            "not_checked_actor_rows": len(not_checked)}


# ---------------------------------------------------------------- VERT (counted = release, other = debug)
def add_vert(objs):
    for o in objs:
        did = o["defect_id"]
        if "error" in o:
            add("VERT", did, "release (rustc -O)", "debug", None, None, "not-run", 0, 0, "harness error: " + o["error"]); continue
        ex, h, g = o["example"], o["harness_debug_filtered"], o["grid"]
        crash_row = o["severity"] == "crash"
        ndbg = (h.get("crash", 0) if o["harness"] == "run_arrays.py" else h.get("rust_panic_c_ok", 0)) if crash_row else \
               (h.get("value", 0) if o["harness"] == "run_arrays.py" else h.get("value_div", 0))
        # a defect also still diverges when the row's class turns into the other divergent class under debug
        ndbg_any = h.get("value", h.get("value_div", 0)) + h.get("crash", h.get("rust_panic_c_ok", 0))
        div = "yes" if ex["debug_outcome"] != "agrees" else "no"
        add("VERT", did, "release (rustc -O)", "debug (rustc, overflow-checks on)",
            ex["release_outcome"] + " (rerun: " + str(ex["rust_release"]) + ")",
            ex["debug_outcome"] + " (" + str(ex["rust_debug"]) + ")", div,
            o["ledger_exposure_count"], ndbg_any,
            f"{o['harness']} grid rerun; harness UB-filtered counts under debug: {({k: v for k, v in h.items() if k in ('value','crash','value_div','rust_panic_c_ok','ub','ub_div')})}; "
            f"raw grid (pre-UB-filter) release vs debug Rust outputs differ on {g['rust_release_vs_debug_differ']} of {g['cases']} inputs",
            note=("profile-sensitive: release and debug Rust outputs differ on %d grid inputs (overflow)" % g["rust_release_vs_debug_differ"]) if g["rust_release_vs_debug_differ"] else None,
            example=o["example"]["input"])
        entries[-1]["c_output"] = ex["c"]
        entries[-1]["profile_sensitive_samples"] = g["profile_sensitive_samples"]


if sys.argv[1] == "--add-adatrans":
    EXIST, ATJ, OUT = sys.argv[2], sys.argv[3], sys.argv[4]
    res = json.load(open(EXIST))
    entries[:] = [e for e in res["defects"] if e["system"] != "AdaTrans"]
    old_summary = summarize(entries, res["not_checked"])
    add_adatrans(json.load(open(ATJ)))
    res["defects"] = entries
    res["profiles_by_system"]["AdaTrans"] = ADATRANS_PROF
    res["summary"] = summarize(entries, res["not_checked"])
    res["provenance"]["adatrans_additions"] = {
        "rows": sorted(e["defect_id"] for e in entries if e["system"] == "AdaTrans"),
        "command": "bash systems/profile_check/adatrans_profile.sh ~/work/adatrans-profile/repo ~/work/regrade-adatrans/pipe "
                   "~/work/regrade-adatrans/zenodo/adatrans-artifacts/dataset_fuzz_outputs ~/work/adatrans-profile; then "
                   "python3 systems/profile_check/assemble.py --add-adatrans results/profile_check.json adatrans.json results/profile_check.json",
        "how_merged": "entries built with assemble.py's AdaTrans block (add_adatrans); summary recomputed with assemble.py's formulas (summarize); every other row, not_checked and earlier provenance unchanged",
        "summary_before": old_summary,
        "machine": "Linux x86-64 box, image crust-heldout-plus186 (sha256:db473e67..., rustc/cargo 1.86.0 as the AdaTrans re-test), --network none, one container per defect in parallel (--cpus 4 each); a sequential re-run gave byte-identical per-defect results",
        "runtime": "12 s wall (parallel; 25 s sequential)",
        "date": time.strftime("%Y-%m-%d %H:%M %Z"),
        "scripts": {f: hashlib.sha256(open(os.path.join(REPO, "systems", "profile_check", f), "rb").read()).hexdigest()[:16]
                    for f in ("adatrans_profile.py", "adatrans_profile.sh", "assemble.py")},
        "ledger": "results/defect_ledger.json sha256 " + hashlib.sha256(open(os.path.join(REPO, "results", "defect_ledger.json"), "rb").read()).hexdigest()[:16]}
    json.dump(res, open(OUT, "w"), indent=1, default=str)
    print(json.dumps(res["summary"], indent=1))
    sys.exit(0)

# ---------------------------------------------------------------- FLOURINE in its own profile (arXiv)
# The arXiv ledger counts FLOURINE in the profile its own check builds the Rust in (cargo-bolero's fuzz profile:
# opt-level 3, overflow checks on), so the counted profile is fuzz and the other profile is release (rustc -O,
# the submitted version's build). Both runs are in results/arxiv_flourine_devprofile.json (same grid, C oracle, UB rule).
FLOURINE_FUZZ_PROF = {"own_pipeline": "fuzz profile: its check runs `cargo bolero test` (oracle.py:169-201), which builds with cargo-bolero's default "
                                      "--profile fuzz, defined in FLOURINE's Cargo.toml template as inherits = \"dev\", opt-level = 3 (overflow checks on)",
                      "own_cite": "artifacts/flourine_dl/flourine-artifact/oracle.py:169-201; Differential_Tester/instrumentor/src/template.rs:37-41 "
                                  "(results/arxiv_flourine_devprofile.json flourine_check_profile)",
                      "our_harness": "fuzz profile (rustc -C opt-level=3 -C debug-assertions=on -C overflow-checks=on; systems/flourine/run.py, run_struct.py --profile fuzz); "
                                     "the submitted version used release (rustc -O)",
                      "harness_cite": "systems/flourine/run.py RUSTC_PROFILE_FLAGS; harness/oracle_ub_flourine_current.py --profile",
                      "agreement": "same (arXiv version)"}


def add_flourine_fuzz(dp):
    """One entry per counted FLOURINE ledger row: counted = fuzz profile, other = release, from the two runs of
    results/arxiv_flourine_devprofile.json (per-file tallies and UB-clean counts under the same rule)."""
    per = {}
    for lr in dp["ledger_rows"]:
        for t, f in lr["files"].items():
            per["opl/" + t if t.startswith("opl_emu_registers_") else t] = {"release": f["release"], "fuzz": f["fuzz"]}
    for c in dp["success_translations_outside_ledger_rows_that_change"]:
        t = "opl/" + c["tag"] if c["tag"].startswith("opl_emu_registers_") else c["tag"]
        ch = next(x for x in dp["changes"] if x["tag"] == c["tag"])
        per[t] = {"release": ch["release"], "fuzz": ch["fuzz"]}
    clean = lambda v: ((v or {}).get("ub_rule") or {}).get("value_inputs_ub_clean", 0) + ((v or {}).get("ub_rule") or {}).get("crash_inputs_ub_clean", 0)
    outc = lambda v: ("wrong answer" if ((v or {}).get("ub_rule") or {}).get("value_inputs_ub_clean") else
                      "crash" if ((v or {}).get("ub_rule") or {}).get("crash_inputs_ub_clean") else "agrees")
    for did, r in sorted(led.items()):
        if r["system"] != "Flourine" or r.get("count_status") != "counted":
            continue
        files = list(r["exposure_breakdown"]["diverging_inputs_per_file"])
        miss = [t for t in files if t not in per]
        if miss:
            add("Flourine", did, "fuzz", "release", None, None, "not-run", 0, 0, f"no release/fuzz record for {miss}")
            continue
        n_c = sum(clean(per[t]["fuzz"]) for t in files)
        n_o = sum(clean(per[t]["release"]) for t in files)
        tal = {t: {"fuzz": per[t]["fuzz"]["tally"], "release": per[t]["release"]["tally"]} for t in files}
        add("Flourine", did, "fuzz (FLOURINE's check profile: opt-level 3, overflow checks on)", "release (rustc -O; the submitted version's build)",
            f"{outc(per[files[0]]['fuzz'])} ({r.get('rust_output')})", outc(per[files[0]]["release"]) + (f" ({n_o} UB-clean diverging inputs)" if n_o else " on every input"),
            "yes" if n_o else "no", n_c, n_o,
            "results/arxiv_flourine_devprofile.json (fuzz run results/arxiv_flourine_devprofile_scalar.json / _struct.json; release run "
            f"results/flourine_diff_current.json / flourine_struct_current.json): per-file tallies {tal}",
            note=("wrong answer under release (the wrap), panic under the fuzz profile (overflow checks)" if n_o and outc(per[files[0]]["fuzz"]) == "crash" else
                  "explicit wrapping_shl / wrapping_add: identical in both profiles" if n_o else
                  "overflow checks turn a wrap that C defines into a panic; under release the wrap gives the C's value"))


if sys.argv[1] == "--add-flourine-fuzz":
    EXIST, DPJ, OUT = sys.argv[2], sys.argv[3], sys.argv[4]
    res = json.load(open(EXIST))
    old_fl = [e for e in res["defects"] if e["system"] == "Flourine"]
    entries[:] = [e for e in res["defects"] if e["system"] != "Flourine"]
    old_summary = res["summary"]
    add_flourine_fuzz(json.load(open(DPJ)))
    res["defects"] = entries
    res["profiles_by_system"]["Flourine"] = FLOURINE_FUZZ_PROF
    res["summary"] = summarize(entries, res["not_checked"])
    res["provenance"]["flourine_fuzz_profile_update"] = {
        "why": "arXiv version: FLOURINE counted in its own check's build profile (fuzz), so the other profile is release",
        "rows": sorted(e["defect_id"] for e in entries if e["system"] == "Flourine"),
        "replaced_rows": [{"defect_id": e["defect_id"], "counted_profile": e["counted_profile"], "other_profile": e["other_profile"],
                           "diverges_under_other_profile": e["diverges_under_other_profile"]} for e in old_fl],
        "command": "python3 -I systems/profile_check/assemble.py --add-flourine-fuzz results/profile_check.json results/arxiv_flourine_devprofile.json results/profile_check.json",
        "inputs": {"results/arxiv_flourine_devprofile.json": hashlib.sha256(open(DPJ, "rb").read()).hexdigest()[:16]},
        "how_merged": "FLOURINE entries rebuilt with assemble.py's add_flourine_fuzz from the release and fuzz runs of results/arxiv_flourine_devprofile.json "
                      "(no new run); summary recomputed with assemble.py's formulas (summarize); every other row, not_checked and earlier provenance unchanged",
        "machine": "the two runs: Linux x86_64 box, crust-syzygy (rustc 1.86.0) for the Rust phase and crust-heldout for the C phase (results/arxiv_flourine_devprofile.json provenance); this step reads JSON only",
        "summary_before": old_summary,
        "date": time.strftime("%Y-%m-%d %H:%M %Z"),
        "scripts": {"assemble.py": hashlib.sha256(open(os.path.join(REPO, "systems", "profile_check", "assemble.py"), "rb").read()).hexdigest()[:16]},
        "ledger": "results/defect_ledger.json sha256 " + hashlib.sha256(open(os.path.join(REPO, "results", "defect_ledger.json"), "rb").read()).hexdigest()[:16]}
    json.dump(res, open(OUT, "w"), indent=1, default=str)
    print(json.dumps(res["summary"], indent=1))
    sys.exit(0)

if sys.argv[1] == "--add-vert":
    # arXiv (issues.md 47): add the VERT rows of a vert_profile.py run restricted with ONLY= (the functions whose C returns
    # long / long long) to an existing profile_check.json; every other row, not_checked and earlier provenance verbatim.
    EXIST, VJ, OUT = sys.argv[2], sys.argv[3], sys.argv[4]
    res = json.load(open(EXIST))
    new = json.load(open(VJ))
    ids = {o["defect_id"] for o in new}
    entries[:] = [e for e in res["defects"] if e["defect_id"] not in ids]
    old_summary = res["summary"]
    add_vert(new)
    res["defects"] = entries
    res["summary"] = summarize(entries, res["not_checked"])
    res["provenance"].setdefault("vert_wide_update", {}).update({
        "why": "arXiv version (issues.md 47): VERT's scalar scope extended to the functions whose C returns long / long long; their counted rows are re-run the other way",
        "rows": sorted(ids),
        "command": "python3 -I systems/profile_check/assemble.py --add-vert results/profile_check.json results/profile_check_vert_wide.json results/profile_check.json",
        "run": "docker run --rm --cpus 3 -v $PWD:/work:ro -v $PWD/results:/out -w /work -e VERT_GRID=v2 -e VERT_WIDE_INT=1 -e ONLY=<the 8 functions> crust-stable python3 systems/profile_check/vert_profile.py /out/profile_check_vert_wide.json (macOS arm64, Docker)",
        "inputs": {"results/profile_check_vert_wide.json": hashlib.sha256(open(VJ, "rb").read()).hexdigest()[:16]},
        "how_merged": "rows built with assemble.py's add_vert (the same block as the full run); summary recomputed with summarize",
        "summary_before": old_summary,
        "date": time.strftime("%Y-%m-%d %H:%M %Z"),
        "scripts": {"assemble.py": hashlib.sha256(open(os.path.join(REPO, "systems", "profile_check", "assemble.py"), "rb").read()).hexdigest()[:16],
                    "vert_profile.py": hashlib.sha256(open(os.path.join(REPO, "systems", "profile_check", "vert_profile.py"), "rb").read()).hexdigest()[:16]},
        "ledger": "results/defect_ledger.json sha256 " + hashlib.sha256(open(os.path.join(REPO, "results", "defect_ledger.json"), "rb").read()).hexdigest()[:16]})
    json.dump(res, open(OUT, "w"), indent=1, default=str)
    print(json.dumps(res["summary"], indent=1))
    sys.exit(0)

D, OUT = sys.argv[1], sys.argv[2]
ld = lambda n: json.load(open(os.path.join(D, n)))


# ---------------------------------------------------------------- VERT (counted = release, other = debug)
add_vert(ld("vert.json"))

# ---------------------------------------------------------------- C2SaferRust (counted = debug, other = release)
c = ld("c2sr.json")
for did in ("C2SR-head-3", "C2SR-head-4", "C2SR-tail-2", "C2SR-tail-3", "C2SR-head-5"):
    r = c["rows"][did]
    dv = r["diverges_under_release"]
    ex = r.get("ledger_example")
    exo = ex["samples"][0]["release"]["outcome"] if ex else r["samples"][0]["release"]["outcome"]
    ex_c = ex["samples"][0]["debug"]["outcome"] if ex else r["samples"][0]["debug"]["outcome"]
    agree = [s["argv"] for s in r["samples"] if s["release"]["outcome"] == "agrees"]
    add("C2SaferRust", did, "debug (cargo build; target/debug)", "release (cargo build --release)", ex_c, exo,
        "yes" if exo != "agrees" else "no", r["n"], dv,
        f"rust_WIP rebuilt in scratch (nightly-2022-08-08); outcome counts debug {r['outcome_counts']['debug']} release {r['outcome_counts']['release']}",
        note=(f"{r['n']-dv} of {r['n']} exposing cases agree with the C under release; e.g. argv {agree[:2]}" if dv < r["n"] else None))

# ---------------------------------------------------------------- RustMap (counted = debug, other = release)
rm = ld("rustmap.json")
ros = rm["rosetta"]
for t, did in (("matrix_chain_multiplication", "RUSTMAP-rosetta-matrix_chain_multiplication-1"),
               ("hailstone_sequence", "RUSTMAP-rosetta-hailstone_sequence-1"),
               ("box_the_compass", "RUSTMAP-rosetta-box_the_compass-1"),
               ("roman_numerals_decode", "RUSTMAP-rosetta-roman_numerals_decode-1")):
    r = ros[t]
    add("RustMap", did, "debug (plain rustc, as 125-rosetta-rs.sh:16)", "release (rustc -O)", r["debug"]["outcome"] + f" (exit {r['debug']['exit']})",
        r["release"]["outcome"] + f" (exit {r['release']['exit']})", "yes" if r["release"]["outcome"] != "agrees" else "no", 1,
        1 if r["release"]["outcome"] != "agrees" else 0,
        f"stdout under release: {r['release']['stdout_head'][:160]!r}; C: {r['c_stdout_head'][:80]!r}",
        note="release wraps instead of panicking (cost values 45/38108/... vs C 48/38120/...)" if t == "matrix_chain_multiplication" else None)
bz = rm.get("bzip2", {})
if "rows" in bz:
    rows = bz["rows"]
    n_div = sum(1 for v in rows.values() if v["release"]["outcome"] != "no crash")
    add("RustMap", "RUSTMAP-bzip2-BZ-3", "debug (cargo build; README 4.1.1)", "release (cargo build --release, panic=abort in Cargo.toml)",
        "crash (index out of bounds, exit 101)", "; ".join(f"{k}: {v['release']['outcome']}" for k, v in rows.items()),
        "yes" if n_div else "no", len(rows), n_div, "two 1-byte inputs named by the ledger; " + bz["deviation"],
        note="not an overflow defect (index out of bounds); under release the compressor hangs (20 s timeout) instead of panicking")

# ---------------------------------------------------------------- SACTOR (counted = debug primary, other = release)
sa = ld("sactor.json")
fc = sa["fs_chown"]
nd = {k: v["diverging_inputs"] for k, v in fc["per_build"].items()}
nc = {k: v["crash_signal_or_abort"] for k, v in fc["per_build"].items()}
ub_clean = sum(1 for v in fc["c_ub"].values() if not v["c_ub"])
add("SACTOR", "SACTOR-C3", "debug (cargo build; both nightlies)", "release (cargo build --release; both nightlies)",
    "crash (abort) on 74 of 80", "crash (abort) on %s of 80" % sorted(set(nc[k] for k in nc if k.endswith("release")))[0],
    "yes", 74, min(v for k, v in nd.items() if k.endswith("release")),
    f"per-build diverging inputs {nd}; crash/abort {nc}; UB-clean in C: {ub_clean}", note="panic in extern C / bad pointer; not arithmetic overflow")
L1 = sa["L1"]["per_build"]
add("SACTOR", "SACTOR-L1", "debug (cargo build; both nightlies)", "release (cargo build --release; both nightlies)",
    "wrong answer on 8 of 442", "wrong answer on %d of 442" % min(v["diverging_sequences_total"] for k, v in L1.items() if k.endswith("release")),
    "yes", 8, min(v["of_the_8_still_diverging"] for k, v in L1.items() if k.endswith("release")),
    "the 8 sequences bw0220 bw0232 bw0237 bw0240 bw0266 bw0276 bw0300 bw0320 rebuilt/rerun on every build: " + json.dumps({k: v["of_the_8_still_diverging"] for k, v in L1.items()}),
    note="`<< 32` is guarded by `if bits > 0`, so the shift never overflows in either profile; the divergence is the missing mask")

# ---------------------------------------------------------------- ACTOR (counted = release, other = debug)
ac = ld("actor.json")
for key, res in ac.items():
    if "error" in res:
        for did in [x for x in led if x.startswith("ACTOR-" + key)]:
            add("ACTOR", did, "release", "debug", None, None, "not-run", 0, 0, "build failed: " + res["error"]); continue
    for r in res.get("rows", []):
        ex = r["example"]
        div = sum(v for k, v in r["their_outcome_under_debug"].items() if k != "agrees")
        add("ACTOR", r["defect_id"], "release (cargo build --release --offline)", "debug (cargo build)",
            ex["release_outcome"], ex["debug_outcome"], "yes" if ex["debug_outcome"] != "agrees" else "no",
            r["grid_release_exposing_inputs"], div,
            f"gen_inputs grid ({res['n_inputs']} inputs): outcomes under release {res['grid_outcomes']['release']}; under debug {res['grid_outcomes']['debug']}; exposing inputs' outcome under debug {r['their_outcome_under_debug']}",
            note=("debug panics on %s additional grid inputs (arithmetic overflow)" % (res['grid_outcomes']['debug'].get('crash', 0) - res['grid_outcomes']['release'].get('crash', 0))) if res['grid_outcomes']['debug'].get('crash', 0) > res['grid_outcomes']['release'].get('crash', 0) else None)
done_actor = {e["defect_id"] for e in entries if e["system"] == "ACTOR"}
for did, r in led.items():
    if r["system"] == "ACTOR" and r.get("count_status") == "counted" and did not in done_actor:
        not_checked.append({"defect_id": did, "severity": r["severity"], "reason": "S1 whose mechanism (%s) does not involve integer overflow or wrapping; not required by the task" % did.split("::")[-1]})

# ---------------------------------------------------------------- Flourine (counted = release, other = debug)
fl = ld("flourine.json")
for did, row in fl["rows"].items():
    add("Flourine", did, "release (rustc -O; systems/flourine/run.py:97, run_struct.py:247)", "debug (rustc)",
        row["release"]["outcome"] + " (" + row["release"]["stdout"] + ")",
        row["debug"]["outcome"] + " (" + (row["debug"]["stdout"] or row["debug"]["stderr"][:60]) + ")",
        "yes" if row["debug"]["outcome"] != "agrees" else "no", 1, 1 if row["debug"]["outcome"] != "agrees" else 0,
        f"{fl['rustc']}; shipped translation + 1-line main; C value {row['c_value']}",
        note="`(1u32 >> length) - 1` underflows: wraps under release, panics under debug" if "lfo" in did else "explicit wrapping_shl / wrapping_add: identical in both profiles")

if os.path.isfile(os.path.join(D, "adatrans.json")):
    add_adatrans(ld("adatrans.json"))

sysprof = {
    "VERT": {"own_pipeline": "bolero (cargo-bolero fuzz profile = release, README tells users RUSTFLAGS=-C overflow-checks=false) and Kani (always checks overflow)",
             "own_cite": "artifacts/vert/evaluation.py:57 (cargo bolero reduce), :79 and :100 (cargo kani); artifacts/vert/ARTIFACT_README:152 (overflow-checks=false); results/vert_bolero_debug.log (`Finished release [optimized]`)",
             "our_harness": "release: rustc -O, overflow checks off", "harness_cite": "systems/vert/run.py:14-15 (docstring), :176; systems/vert/run_arrays.py:241",
             "agreement": "same (release-like) for bolero; Kani is profile-independent and does check overflow"},
    "C2SaferRust": {"own_pipeline": "debug (cargo build; binary target/debug)", "own_cite": "artifacts/c2saferrust/translate.py:184, :236, :711",
                    "our_harness": "debug", "harness_cite": "harness/build.py:50-51", "agreement": "same"},
    "RustMap": {"own_pipeline": "debug (plain `rustc file` for Rosetta; `cargo build` for bzip2)",
                "own_cite": "artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/125-rosetta-rs.sh:15; artifacts/rustmap/README.md:213-215 (section 4.1.1)",
                "our_harness": "debug", "harness_cite": "systems/rustmap/run_rosetta.py:96-98; systems/rustmap/check_bzip2.sh:16-17", "agreement": "same"},
    "SACTOR": {"own_pipeline": "debug (cargo build of the build_attempt crate)", "own_cite": "systems/sactor/sactor_common.py:222-233 (mirrors SACTOR's build_attempt); systems/sactor/crust/grid.py:19",
               "our_harness": "debug primary, release and a second nightly as cross-checks in the same run", "harness_cite": "systems/sactor/crust/grid.py:426; systems/sactor/libogg/run.py:13, :164", "agreement": "same (release also recorded)"},
    "ACTOR": {"own_pipeline": "release (cargo build --release for the translated crate and for test vectors; cargo check in verify is profile-free)",
              "own_cite": "artifacts/actoruw/ACTOR/prompts/claude/ablations/translate-and-verify-executable.md:14,19; artifacts/actoruw/ACTOR/prompts/claude/translate-library.md:30; artifacts/actoruw/ACTOR/tools/src/translate.rs:1630-1631; tools/src/oracle/gtest.rs:38,59; tools/src/verify.rs:753",
              "our_harness": "release", "harness_cite": "systems/actoruw/build.py:6, :77; tc_crosscheck.py:59; mutate.py:112", "agreement": "same"},
    "His2Trans": {"own_pipeline": "debug (`cargo test` per test, `cargo build`)", "own_cite": "systems/his2trans/own_check.py:6; systems/his2trans/xcheck.py:10-11; artifacts/his2trans/README.md:215",
                  "our_harness": "release (the crate's own release profile: opt-level 3, lto)", "harness_cite": "systems/his2trans/run.py:13, :132, :323",
                  "agreement": "DIFFERENT: counted rows are release, the system's own check is debug. A debug cross-check already exists (results/his2trans_xcheck_186_debug.json). No counted crash row; no counted S1 row involves overflow (H2T-RGBA-1 is a saturating `as u8` cast, profile-independent), so nothing was re-run here"},
    "PtrTrans": {"own_pipeline": "debug (its cargo check loop)", "own_cite": "systems/ptrtrans/quadtree_e2e/run.py:16-17",
                 "our_harness": "debug primary, --release cross-check", "harness_cite": "systems/ptrtrans/quadtree_e2e/run.py:16-17, :108",
                 "agreement": "same; no counted crash or overflow row, nothing re-run"},
    "Flourine": {"own_pipeline": "not examined", "our_harness": "release (rustc -O)", "harness_cite": "systems/flourine/run.py:97; systems/flourine/run_struct.py:247"},
}
if any(e["system"] == "AdaTrans" for e in entries):
    sysprof["AdaTrans"] = ADATRANS_PROF

res = {
    "provenance": {
        "generated": time.strftime("%Y-%m-%d %H:%M %Z"),
        "question": "does each counted crash / overflow-related wrong answer still diverge from the C under the OTHER cargo profile?",
        "scripts": {f: hashlib.sha256(open(os.path.join(REPO, "systems", "profile_check", f), "rb").read()).hexdigest()[:16]
                    for f in sorted(os.listdir(os.path.join(REPO, "systems", "profile_check"))) if f.endswith(".py")},
        "ledger": "results/defect_ledger.json sha256 " + hashlib.sha256(open(os.path.join(REPO, "results", "defect_ledger.json"), "rb").read()).hexdigest()[:16],
        "machines": {"VERT, Flourine": "this Mac, Docker arm64, image crust-stable (rustc 1.86.0, gcc 11.4), --cpus 2",
                     "C2SaferRust": "Linux x86-64 box, image crust-heldout (rustc nightly-2022-08-08, as the harness), --network none, --cpus 2, libc 0.2.158 vendored from cli-mutation/vendor",
                     "RustMap": "Linux x86-64 box, image crust-heldout-plus186 (rustc 1.86.0), --network none, --cpus 2; bzip2 crate rebuilt from the host cargo source cache with unused deps rustix/filetime/tempfile/rand removed from the scratch Cargo.toml",
                     "ACTOR": "Linux x86-64 box, image crust-heldout, toolchain nightly-2026-01-10 (rustc 1.94.0-nightly) as the harness, offline",
                     "SACTOR": "Linux x86-64 box, image crust-heldout, nightly-2024-09-03 and nightly-2025-05-10, offline, libc 0.2.191 vendored"},
        "scratch": "~/work/profile-check/ on the box; copies of translations only, artifacts untouched",
        "method": "the translation is rebuilt in the other profile in a scratch copy and the unchanged C oracle is run on the ledger example input and on the harness's own exposing input set; verdict = example input",
        "limits": ["the C oracle is not re-sanitized for the grid inputs; verdicts rest on the ledger example (UB-clean per the ledger) plus the harness's own UB filter where it was re-run (VERT, SACTOR)",
                   "ACTOR/RustMap/C2SaferRust exposing sets are the harness's own generated inputs, not new inputs",
                   "ACTOR S1 rows other than 019/033/042/qmath/macrodepth were not re-run (mechanisms PARSE_NUM, LINE_SPLIT, UTF8_EOF, NUL_BYTE, WS_SET, ASSERT_DROPPED, BREAK_LOOP, NAN_FMT do not involve overflow)",
                   "ACTOR checkshift_lib (SHIFT_SIGN, library harness) not re-run: right shift of a negative int, profile-independent in Rust",
                   "RustMap bzip2 BZ-1 (sign-extending cast), BZ-4, C2SR-head-5 mechanisms are not overflow; head-5 was run, BZ-1/BZ-4 not"]},
    "profiles_by_system": sysprof,
    "summary": summarize(entries, not_checked),
    "defects": entries,
    "not_checked": not_checked,
}
json.dump(res, open(OUT, "w"), indent=1, default=str)
print(json.dumps(res["summary"], indent=1))
