"""arXiv re-run: C2SaferRust mutation kills under the UB rule.

harness/mutate_full.py (results/cli_mutation_full.json) runs a mutant over the v4 grid in grid order
and stops at the FIRST case whose stdout/exit differs from the C (_run_mutant). It does not ask
whether the C is UB-clean on that case. The defect count does: a case counts only if the C passes
the full UB rule (results/oracle_ub_c2saferrust.json: clang-14 and gcc-11 ASan+UBSan+
float-cast-overflow, gcc -O1/-O2 == -O0, -funsigned-char/-fsigned-char == ref, and every run
completed and agreed). This script applies the same rule to the mutation kills:

  * UB verdict per case: results/oracle_ub_c2saferrust.json lists every case that is counted for
    C2SaferRust's output or that shows any anomaly (sanitizer report, opt-level or char-signedness
    change, sanitizer infra/timeout/disagreement); a case not listed ran clean under every check.
    A case is UB-clean iff it is not listed, or it is listed and passes the C-side checks of
    scripts/build_defect_ledger.py:clean_case_c2sr (the `reproduced` flag there is about the Rust
    and is not used here). This counts clang's NULL+0 report as UB, as the ledger does.
  * Re-scan: every killed mutant (killed_s1/killed_crash) whose first-kill case is NOT UB-clean is
    rebuilt exactly as _run_mutant builds it (same per-worker tree, cargo build) and re-run over the
    UB-clean cases only, in grid order, with the same comparison (mutate_full.compare =
    difftest.compare_case), the same 15 s timeout and the same hang rule (HANG_CAP timeouts, then
    1 s timeouts for at most 60 s). It is killed iff some UB-clean case gives a stdout/exit
    difference. Killed mutants whose first-kill case is UB-clean keep their verdict (the scan is in
    grid order, so that case is the first one). Mutants that were not killed cannot become killed.
  * Checks: each re-scanned mutant first re-runs its original first-kill case (must still differ);
    --controls also re-scans the killed mutants of the same utilities whose first kill is UB-clean
    (the re-scan must find the same first-kill case).

Run inside crust-heldout like harness/mutate_full_run.sh (MFULL_WORK, MFULL_VARIANT=rust|rust_WIP):
  python3 -I harness/arxiv_mutate_full_ubfilter.py --jobs 12 --controls --out /w/out/c2sr_<variant>.json
"""
import argparse
import collections
import hashlib
import json
import multiprocessing as mp
import os
import platform
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mutate_full as MF  # noqa: E402

REPO = MF.REPO
KILLED = ("killed_s1", "killed_crash")


def c_clean(v):
    """C-side part of scripts/build_defect_ledger.py:clean_case_c2sr for one listed verdict."""
    if v.get("c_ub_evidence") or v.get("char_signedness_dependent") or v.get("san_infra") or v.get("ub_rule_not_clean"):
        return False
    for k in ("san_clang", "san_gcc"):
        x = v.get(k)
        if isinstance(x, dict) and (x.get("infra") or x.get("timed_out") or x.get("same_as_ref") is False
                                    or x.get("reports")):
            return False
    for k in ("o1", "o2", "uchar", "schar"):
        if k in v and not v[k].get("same_as_ref", True):
            return False
    return v.get("final_class") != "original_C_UB"


def c_clean_ignoring_null_plus_zero(v):
    """Sensitivity view: as c_clean, but clang's NULL+0 report alone is not UB
    (final_class_excluding_null_plus_zero / c_ub_evidence_excluding_null_plus_zero). Where the halting
    clang run stopped at NULL+0 (its only report), the recoverable clang run (san_clang_nz) stands in
    for it: it must complete, agree with the reference and report nothing else."""
    if v.get("c_ub_evidence_excluding_null_plus_zero") or v.get("char_signedness_dependent") or v.get("san_infra") \
            or v.get("ub_rule_not_clean"):
        return False
    sc = v.get("san_clang") or {}
    clang = v.get("san_clang_nz") if (sc.get("reports") and not sc.get("other_than_null_plus_zero")) else sc
    for x in (clang, v.get("san_gcc")):
        if isinstance(x, dict) and (x.get("infra") or x.get("timed_out") or x.get("same_as_ref") is False
                                    or x.get("other_than_null_plus_zero")):
            return False
    if (v.get("san_gcc") or {}).get("reports"):
        return False
    for k in ("o1", "o2", "uchar", "schar"):
        if k in v and not v[k].get("same_as_ref", True):
            return False
    return v.get("final_class_excluding_null_plus_zero") != "original_C_UB"


def sha256_file(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# ------------------------------------------------------------------------------------- worker
_CLEAN = {}


def _init(q, clean):
    MF._worker_init(q)
    _CLEAN.update(clean)


def rescan(m):
    """Rebuild mutant m as mutate_full._run_mutant does; scan only the UB-clean cases."""
    util = m["util"]
    src_dir, tgt = MF._worker_tree(util)
    path = os.path.join(src_dir, "src", f"{util}.rs")
    orig = open(path).read()
    res = {"id": m["id"], "worker": MF._WORKER["id"]}
    t0 = time.time()
    try:
        with open(path, "w") as f:
            f.write(MF.apply_mutant(orig, m))
        ok, b, log = MF.cargo_build(src_dir, util, tgt)
        res["build_seconds"] = round(time.time() - t0, 1)
        if not ok:
            errs = [ln for ln in log.splitlines() if ln.startswith("error")]
            res.update(status="noncompile", error=(errs[0] if errs else log[-300:])[:300])
            return res
        res["tce"] = sha256_file(b) == MF._WORKER["base_sha"][util]
        cases = MF.load_cases(util)
        base = json.load(open(os.path.join(MF.BUILD, f"baseline_{util}.json")))
        ob = MF.oracle_bin(util)
        clean = _CLEAN[util]
        # reproduction check: the original first-kill case must still differ
        fk = m["first_kill"]["case"]
        ci = [k for k, c in enumerate(cases) if c.id == fk][0]
        v0 = MF.compare(cases[ci], ob, b, MF.TIMEOUT)
        res["orig_first_kill_reproduced"] = bool(v0["mismatch"])
        res["orig_first_kill_recheck"] = {"oracle_exit": v0["oracle_exit"], "sut_exit": v0["sut_exit"],
                                          "stdout_match": v0["stdout_match"], "exit_match": v0["exit_match"]}
        # the scan of mutate_full._run_mutant, restricted to UB-clean cases
        timeouts, n_run, n_skip_ub = 0, 0, 0
        status, first = "survived", None
        tmo, hang_t0 = MF.TIMEOUT, None
        for c, bv in zip(cases, base):
            if bv.get("mismatch"):
                continue  # rust_WIP only: the unmutated tree already diverges here
            if c.id not in clean:
                n_skip_ub += 1
                continue
            v = MF.compare(c, ob, b, tmo)
            n_run += 1
            if v["timed_out"]:
                timeouts += 1
                if timeouts >= MF.HANG_CAP and hang_t0 is None:
                    hang_t0, tmo = time.time(), MF.HANG_SCAN_TIMEOUT
                if hang_t0 is not None and time.time() - hang_t0 > MF.HANG_SCAN_BUDGET:
                    break
                continue
            if v["mismatch"]:
                crash = (v["sut_exit"] < 0 and not v["_sut_capped"]) or \
                        (v["sut_exit"] == 101 and v["oracle_exit"] != 101)
                status = "killed_crash" if crash else "killed_s1"
                first = {"case": v["id"], "argv": v["argv"], "oracle_exit": v["oracle_exit"],
                         "sut_exit": v["sut_exit"], "stdout_match": v["stdout_match"],
                         "exit_match": v["exit_match"], "panic": v["_sut_panic"],
                         "output_capped": v["_sut_capped"]}
                break
            if hang_t0 is not None and time.time() - hang_t0 > MF.HANG_SCAN_BUDGET:
                break
        n_elig = sum(1 for c, bv in zip(cases, base) if not bv.get("mismatch") and c.id in clean)
        hang = timeouts >= MF.HANG_CAP
        scan_complete = n_run == n_elig or status != "survived"
        if status == "survived" and hang:
            status = "hang"
        res.update(status=status, first_kill=first, clean_cases_run=n_run, clean_cases_eligible=n_elig,
                   ub_cases_skipped_before_stop=n_skip_ub, timeouts=timeouts, hang=hang,
                   scan_complete=scan_complete)
        return res
    finally:
        with open(path, "w") as f:
            f.write(orig)
        res["seconds"] = round(time.time() - t0, 1)


# ------------------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--results", default=os.path.join(REPO, "results", "cli_mutation_full.json"))
    ap.add_argument("--oracle-ub", default=os.path.join(REPO, "results", "oracle_ub_c2saferrust.json"))
    ap.add_argument("--controls", action="store_true",
                    help="also re-scan killed mutants of the affected utilities whose first kill is UB-clean")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    t_start = time.time()
    variant = MF.VARIANT
    full = json.load(open(a.results))
    data = full if variant == "rust" else full["rust_WIP_reported_separately"]
    mutants = data["mutants"]
    oub = json.load(open(a.oracle_ub))
    V = {v["id"]: v for v in oub["verdicts"]}

    clean, clean_nz, n_cases = {}, {}, {}
    for u in MF.UTILS:
        ids = [c.id for c in MF.load_cases(u)]
        n_cases[u] = len(ids)
        clean[u] = {i for i in ids if i not in V or c_clean(V[i])}
        clean_nz[u] = {i for i in ids if i not in V or c_clean_ignoring_null_plus_zero(V[i])}

    # the C oracle and the trees must be the ones the original run used
    prep = json.load(open(os.path.join(MF.BUILD, "prepare.json")))
    tree_check = {u: {"oracle_sha256_equal": sha256_file(MF.oracle_bin(u)) == prep[u]["oracle_sha256"],
                      "src_sha256_equal": sha256_file(os.path.join(MF.TREES, u, variant, "src", f"{u}.rs"))
                      == prep[u]["src_sha256"]} for u in MF.UTILS}
    print("[tree check]", json.dumps(tree_check), flush=True)
    assert all(all(x.values()) for x in tree_check.values()), tree_check

    comp = [m for m in mutants if m["status"] != "noncompile"]
    killed = [m for m in comp if m["status"] in KILLED]
    need = [m for m in killed if m["first_kill"]["case"] not in clean[m["util"]]]
    utils_aff = sorted({m["util"] for m in need})
    ctrl = [m for m in killed if m["first_kill"]["case"] in clean[m["util"]] and m["util"] in utils_aff] \
        if a.controls else []
    todo = need + ctrl
    print(f"[{variant}] compiled {len(comp)}, killed {len(killed)}, first kill on a non-UB-clean case "
          f"{len(need)} ({collections.Counter(m['util'] for m in need)}), controls {len(ctrl)}", flush=True)

    q = mp.Queue()
    for k in range(a.jobs):
        q.put(k)
    results = {}
    with mp.Pool(a.jobs, initializer=_init, initargs=(q, clean)) as pool:
        for k, r in enumerate(pool.imap_unordered(rescan, todo)):
            results[r["id"]] = r
            print(f"[{k + 1}/{len(todo)}] {r['id']} {r['status']} "
                  f"first={(r.get('first_kill') or {}).get('case')} {r.get('seconds')}s", flush=True)

    rows = []
    for m in comp:
        fk = (m.get("first_kill") or {}).get("case")
        row = {"id": m["id"], "util": m["util"], "op": m["op"], "subop": m["subop"], "line": m.get("line"),
               "old": m.get("old"), "new": m.get("new"), "orig_status": m["status"],
               "orig_first_kill_case": fk if m["status"] in KILLED else None}
        if m["status"] in KILLED:
            row["orig_first_kill_case_ub_clean"] = fk in clean[m["util"]]
            row["orig_first_kill_case_ub_clean_ignoring_null_plus_zero"] = fk in clean_nz[m["util"]]
            if fk in V:
                row["orig_first_kill_case_ub_evidence"] = (V[fk].get("c_ub_evidence") or [])[:3]
        r = results.get(m["id"])
        if r is not None:
            row["rescan"] = r
        if m["status"] not in KILLED:
            row["new_status"], row["basis"] = m["status"], "not killed in the original scan; a UB filter cannot add a kill"
        elif fk in clean[m["util"]]:
            row["new_status"], row["basis"] = m["status"], "original first-kill case is UB-clean"
            if r is not None:
                row["control_same_first_kill"] = (r.get("first_kill") or {}).get("case") == fk
        else:
            row["new_status"] = r["status"] if r else "not_rescanned"
            row["basis"] = "re-scanned over the UB-clean cases only"
        rows.append(row)

    def cnt(rs, key):
        return sum(1 for r in rs if r[key] in KILLED)

    per_util = {}
    for u in MF.UTILS:
        rs = [r for r in rows if r["util"] == u]
        per_util[u] = {"compiled": len(rs), "killed_old": cnt(rs, "orig_status"), "killed_new": cnt(rs, "new_status"),
                       "killed_old_first_kill_not_ub_clean": sum(1 for r in rs if r["orig_status"] in KILLED
                                                                 and not r["orig_first_kill_case_ub_clean"]),
                       "grid_cases": n_cases[u], "ub_clean_cases": len(clean[u]),
                       "ub_clean_cases_ignoring_null_plus_zero": len(clean_nz[u])}
    k_old, k_new, n = cnt(rows, "orig_status"), cnt(rows, "new_status"), len(rows)
    resc = [r for r in rows if r["basis"].startswith("re-scanned")]
    ctrls = [r for r in rows if "control_same_first_kill" in r]
    summary = {
        "variant": variant,
        "compiled": n, "killed_old": k_old, "killed_new": k_new,
        "rate_old": round(k_old / n, 4), "rate_new": round(k_new / n, 4),
        "per_util": per_util,
        "rescanned": len(resc),
        "rescanned_new_status": dict(collections.Counter(r["new_status"] for r in resc)),
        "rescanned_scan_incomplete": sum(1 for r in resc if not r["rescan"].get("scan_complete", True)),
        "rescanned_orig_first_kill_reproduced": sum(1 for r in resc if r["rescan"].get("orig_first_kill_reproduced")),
        "controls": len(ctrls), "controls_same_first_kill": sum(1 for r in ctrls if r["control_same_first_kill"]),
        "killed_if_null_plus_zero_alone_is_not_ub": sum(
            1 for r in rows if r["orig_status"] in KILLED and (r["orig_first_kill_case_ub_clean_ignoring_null_plus_zero"]
                                                               or r["new_status"] in KILLED)),
        "killed_if_null_plus_zero_alone_is_not_ub_note": "every original first-kill case that is UB only through "
            "clang's NULL+0 report is UB-clean under this view, so those kills stand; computed from the records, no re-run",
        "seconds": round(time.time() - t_start, 1),
    }
    sh = lambda c: subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    from provenance import stamp  # noqa: E402
    prov = {
        "script": "harness/arxiv_mutate_full_ubfilter.py",
        "command": os.environ.get("ARXIV_COMMAND") or " ".join(["python3"] + sys.argv),
        "git_commit_of_study_repo": os.environ.get("CRUST_ARTIFACT_COMMIT"),
        "git_note": "study repo working tree (Mac) at that commit plus uncommitted files, this script new; rsynced "
                    "to the run host without .git",
        "host": "Linux x86_64 box (20 cores)", "arch": platform.machine(), "kernel": platform.release(),
        "image": os.environ.get("ARXIV_IMAGE"), "image_id": os.environ.get("ARXIV_IMAGE_ID"),
        "cpus": f"docker --cpus {a.jobs}",
        "toolchains": {"rustc": sh("rustc --version"), "cargo": sh("cargo --version"),
                       "gcc": sh("gcc --version | head -1"), "python": sys.version.split()[0]},
        "variant": variant,
        "inputs": {os.path.relpath(p, REPO): sha256_file(p) for p in (a.results, a.oracle_ub)},
        "mutant_source": "mutants[] of the results file (sites and first-kill cases as recorded); trees, vendored "
                         "libc and baseline_<util>.json copied from the original run's work dir",
        "tree_check_vs_original_prepare_json": tree_check,
        "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    out = {"_provenance": stamp(image=os.environ.get("ARXIV_IMAGE")), "provenance": prov,
           "rule": __doc__.strip().split("\n\n")[1], "summary": summary, "mutants": rows}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
