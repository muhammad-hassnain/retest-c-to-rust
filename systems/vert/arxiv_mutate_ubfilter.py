"""arXiv re-run: VERT's mutation kills under the FULL UB rule.

The VERT mutation scans (systems/vert/mutate_primary.py; results/vert_mutation_primary_grid2.json for the
scalar harness, results/vert_arrays_mutation_primary.json for the array/float harness) already drop a kill
when the C is not UB-clean, but under the VERT harness's own, weaker rule: gcc -O0 ASan+UBSan (no
float-cast-overflow, no clang, no -O2) and the gcc -O0 vs -O1 -fwrapv cross-check (scalar: when the C has
no float; arrays: when the C has no float and the return is an integer); tally_first_kill
(mutate_primary.py:611-666) and ra_classify_first_kill (:1053-1093). The defect count uses the FULL rule
(systems/vert/arxiv_ub_fullrule.py, results/arxiv_vert_ub_fullrule.json): ASan+UBSan+float-cast-overflow
under gcc-11 and clang-14 at -O0/-O1/-O2, gcc/clang -O0/-O1/-O2 -fwrapv builds and -fsigned-char /
-funsigned-char builds all printing the primary oracle's value. This script applies that rule to the kills:

  1. For every killed mutant of the headline operators (ror, aor, const, f32) that the harness-power row
     counts (scalar: killed_value / killed_panic; arrays: the same minus the 30 kills that are only a Rust
     batch time-out, which results/harness_power.json does not count), the recorded kill input
     (value_samples / crash_samples for scalar, sample for arrays) is re-run through the primary C oracle
     (its value must equal the recorded one) and checked with arxiv_ub_fullrule.check_input on the
     harness's own C driver source (run.build_c / run_arrays.build_c), the reference being the primary
     oracle's (gcc -O1 -fwrapv) value on that input. UB-clean: the kill stands.
  2. Otherwise the mutant is regenerated (mutate_primary.mutants_for, cap 0, seed 20261113, as the shipped
     runs; checked against the recorded op/sub/line/orig/repl) and rebuilt (run.build_rust /
     run_arrays.build_rust), and the shipped scan is re-run over the same grid in the same order
     (scalar: grid v2, mutate_primary.eval_cases_fast and tally_first_kill's candidate loop; arrays:
     gen_cases(comps, 240), run_batch and ra_classify_first_kill's candidate loop), with ONE change: a
     candidate that the shipped scan would count as a kill counts only if the full rule finds the C
     UB-clean on that input; otherwise it is counted `ub_fullrule` and the scan continues. The first
     candidate the shipped scan would count must be the recorded kill input (reproduction check).
     New outcome as mutate_primary.classify / ra_kind (a full-rule rejection counts as ub).
The shipped harness filter is unchanged and still applied first; the full rule contains it (its gcc -O0
ASan+UBSan build adds float-cast-overflow, its gcc -O0 -fwrapv build must match the oracle), so a
candidate the shipped scan skipped as UB is not UB-clean under the full rule either.

Where: as the shipped runs (provenance of the two result files). Scalar shard 0 (the 30 pinned functions)
and the array/float harness ran on the arm64 Mac (crust-stable); scalar shard 1 (the other 85) on the
x86_64 box (crust-stable-amd64). The full rule needs clang-14, so this runs in crust-vert-toolchain
(arm64) / crust-vert-toolchain-x86 (x86_64): same gcc 11.4.0 and rustc 1.86.0 as crust-stable, plus
clang-14; no `clang` alias, so MSan stays off in the harness filter as in the shipped runs.

  # arm64 Mac (repo root); SCRATCH = a scratch directory:
  docker run --rm --cpus 6 --memory 6g -v $PWD:/work -v $(cd artifacts && pwd -P):/work/artifacts:ro -v SCRATCH:/scratch \
      -w /work -e RUSTUP_TOOLCHAIN=1.86.0 crust-vert-toolchain \
      python3 -I systems/vert/arxiv_mutate_ubfilter.py run --harness scalar --shard 0 --jobs 6 --out /scratch/scalarshard0.json
  (the same with `run --harness arrays --jobs 6 --out /scratch/arrays.json`, and, as a cross-architecture check that
  the counts do not use, `run --harness scalar --shard 1 --jobs 6 --out /scratch/scalarshard1_arm64.json`)
  # x86_64 box (~/work/arxiv-vertmut/repo = the files of this tree the run needs, rsynced; see the part's git_note):
  docker run --rm --cpus 6 --memory 12g -v ~/work/arxiv-vertmut/repo:/work:ro -v ~/work/arxiv-vertmut/out:/out -w /work \
      -e RUSTUP_TOOLCHAIN=1.86.0 crust-vert-toolchain-x86 \
      python3 -I systems/vert/arxiv_mutate_ubfilter.py run --harness scalar --shard 1 --jobs 6 --out /out/scalarshard1.json
  # merge (scalarshard1.json copied into SCRATCH):
  python3 -I systems/vert/arxiv_mutate_ubfilter.py merge SCRATCH/scalarshard0.json SCRATCH/scalarshard1.json \
      SCRATCH/arrays.json --cross-arch SCRATCH/scalarshard1_arm64.json --out results/arxiv_mutation_ubfilter_vert.json
"""
import argparse
import collections
import datetime
import hashlib
import json
import multiprocessing as mp_
import os
import platform
import shutil
import sys
import tempfile
import time

os.environ["MUTPRIM_GRID"] = "v2"          # the scalar row's grid (vert_mutation_primary_grid2.json); before import
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "harness"))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import mutate_primary as mp              # noqa: E402  (the shipped scan: mutants, oracles, grid, fast eval)
import arxiv_ub_fullrule as fr           # noqa: E402  (the full rule: build_variants, check_input)
import run                               # noqa: E402
import run_arrays as ra                  # noqa: E402
from batch_diff import run_batch, run_one, san_verdict   # noqa: E402

mp.GRID = "v2"
SCALAR_SRC = "results/vert_mutation_primary_grid2.json"
ARRAYS_SRC = "results/vert_arrays_mutation_primary.json"
HEAD_OPS = ("ror", "aor", "const", "f32")
KILLED = ("killed_value", "killed_panic")
SEED = 20261113


def sha(p):
    with open(os.path.join(REPO, p), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def shipped_pins(d):
    """The 30 functions of the shipped scalar shard 0 (arm64), from its recorded --pin list."""
    sh0 = d["provenance"]["merged_from_shards"][0]
    assert "shard0" in sh0["file"] and sh0["arch"] == "aarch64", sh0["file"]
    return sh0["command"].split("--pin ")[1].split()[0].split(",")


def counted_kill(harness, m):
    if m["op"] not in HEAD_OPS or m["outcome"] not in KILLED:
        return False
    return not (harness == "arrays" and m.get("rust_batch_timeout"))


def kill_sample(harness, m):
    if harness == "scalar":
        s = m["value_samples"] + m["crash_samples"]
        assert len(s) == 1, (m["id"], s)
        return s[0]["in"], s[0]["c"], s[0].get("rust", "__PANIC__/no line")
    return m["sample"]["in"], m["sample"]["c"], m["sample"]["rust"]


class Full:
    """The full rule on the ORIGINAL C of one function, cached per input."""

    def __init__(self, var, eq):
        self.var, self.eq, self.cache = var, eq, {}

    def __call__(self, row, ref):
        k = json.dumps([row, ref])
        if k not in self.cache:
            clean, fails, info = fr.check_input(self.var, row, ref, self.eq)
            self.cache[k] = {"in": row, "ref": ref, "clean": clean, "fails": fails, "info": info}
        return self.cache[k]


# ------------------------------------------------------------------------------------------------
# scalar: tally_first_kill (mutate_primary.py:611-666) with the full-rule gate
# ------------------------------------------------------------------------------------------------
def scalar_scan(orc, binr, cases, full):
    binc, binc_san, binc_alt, binc_msan = orc["binc"], orc["binc_san"], orc["binc_alt"], orc["binc_msan"]
    out = {"cases": len(cases), "value": 0, "rust_panic": 0, "rust_hang": 0, "ub": 0, "ub_fullrule": 0,
           "timeout": 0, "agree": 0, "value_samples": [], "crash_samples": [], "early_exit": False,
           "first_shipped_kill": None, "rejected_by_full_rule": []}
    altl = run_batch(binc_alt, cases)[1] if binc_alt is not None else None
    cands = []
    evald, st = mp.eval_cases_fast(binc, binr, cases)
    out.update(st)
    for i, (row, cl, rl) in enumerate(evald):
        if cl == "__PARSE__" or rl == "__PARSE__":
            continue
        if cl is not None and cl != "__TIMEOUT__" and cl == rl:
            out["agree"] += 1; continue
        if altl is not None and cl is not None:
            alt = altl[i] if i < len(altl) else None
            if alt is not None and alt != "__PARSE__" and alt != cl:
                out["ub"] += 1; continue
        if cl is None or cl == "__TIMEOUT__":
            cands.append((row, None, rl, "c_missing")); continue
        if rl == "__TIMEOUT__":
            cands.append((row, cl, rl, "hang")); continue
        if rl is None or rl == "__PANIC__":
            cands.append((row, cl, rl, "crash")); continue
        cands.append((row, cl, rl, "value"))
    out["candidates"] = len(cands)
    for row, cl, rl, tag in cands:
        if binc_san is not None:
            v = san_verdict(binc_san, row)
            if v == "infra":
                out["san_infra"] = out.get("san_infra", 0) + 1
            if v == "ub":
                out["ub"] += 1; continue
        if binc_msan is not None:
            v = san_verdict(binc_msan, row)
            if v == "infra":
                out["san_infra"] = out.get("san_infra", 0) + 1
            if v == "ub":
                out["ub"] += 1; continue
        if tag == "c_missing":
            out["ub" if binc_san is None else "timeout"] += 1; continue
        if tag == "hang":
            out["rust_hang"] += 1; continue
        # the shipped scan stops here with a kill; the full rule decides whether it counts
        if out["first_shipped_kill"] is None:
            out["first_shipped_kill"] = {"in": row, "c": cl, "rust": rl, "tag": tag}
        f = full(row, cl)
        if not f["clean"]:
            out["ub_fullrule"] += 1
            out["rejected_by_full_rule"].append({"in": row, "tag": tag, "failing_checks": sorted(f["fails"])})
            continue
        if tag == "crash":
            out["rust_panic"] += 1; out["crash_samples"].append({"in": row, "c": cl})
        else:
            out["value"] += 1; out["value_samples"].append({"in": row, "c": cl, "rust": rl})
        out["early_exit"] = True
        break
    return out


def scalar_outcome(t):
    return mp.classify({**t, "ub": t["ub"] + t["ub_fullrule"]})


# ------------------------------------------------------------------------------------------------
# arrays: ra_classify_first_kill (mutate_primary.py:1053-1093) with the full-rule gate
# ------------------------------------------------------------------------------------------------
def arrays_scan(ctx, binr, full):
    rows = ctx["rows"]
    cl = ra.run_batch(ctx["binc"], rows)
    rl, rto = mp.ra_run_batch_flag(binr, rows)
    ret_is_float, binc_san, binc_alt = ctx["ret_is_float"], ctx["binc_san"], ctx["binc_alt"]
    agree = ub = 0
    altl = ra.run_batch(binc_alt, rows) if (binc_alt is not None and not ret_is_float) else None
    cands = []
    for i, row in enumerate(rows):
        a = cl[i] if i < len(cl) else None
        b = rl[i] if i < len(rl) else None
        if a == "__PARSE__":
            continue
        if altl is not None and a is not None:
            alt = altl[i] if i < len(altl) else None
            if alt is not None and alt != "__PARSE__" and alt != a:
                ub += 1; continue
        if a is None:
            cands.append((row, a, b, "c_missing")); continue
        if b is None or b == "__PANIC__" or b == "__PARSE__":
            cands.append((row, a, b, "crash")); continue
        if ra.vals_equal(a, b, ret_is_float):
            agree += 1
        else:
            cands.append((row, a, b, "value"))
    value = crash = timeout = ub_full = 0
    sample = first = None
    rejected = []
    for row, a, b, tag in cands:
        if binc_san is not None:
            sline, src, sto, sstderr = ra.run_one(binc_san, row)
            if sto:
                timeout += 1; continue
            if src != 0 or ra.SAN_RE.search(sstderr):
                ub += 1; continue
        if tag == "c_missing":
            timeout += 1; continue
        if first is None:
            first = {"in": row, "c": a, "rust": b, "tag": tag}
        f = full(row, a)
        if not f["clean"]:
            ub_full += 1
            rejected.append({"in": row, "tag": tag, "failing_checks": sorted(f["fails"])})
            continue
        if tag == "crash":
            crash += 1
        else:
            value += 1
        sample = {"in": row, "c": a, "rust": b}
        break
    return {"agree": agree, "value": value, "crash": crash, "ub": ub, "ub_fullrule": ub_full, "timeout": timeout,
            "candidates": len(cands), "sample": sample, "rust_batch_timeout": rto,
            "first_shipped_kill": first, "rejected_by_full_rule": rejected}


def arrays_outcome(t):
    return mp.ra_kind({**t, "ub": t["ub"] + t["ub_fullrule"]})


# ------------------------------------------------------------------------------------------------
# one job = one function
# ------------------------------------------------------------------------------------------------
def regenerate(func, recorded_ids):
    _rust, _t, _names, muts, sampled = mp.mutants_for(func, 0, SEED)
    return {m["id"]: m for m in muts if m["id"] in recorded_ids}, sampled


def job(args):
    harness, func, pf = args
    t0 = time.time()
    muts = [m for m in pf["mutants"] if counted_kill(harness, m)]
    res = {"func": func, "harness": harness, "killed_counted_old": len(muts), "mutants": [], "full_rule_checks": []}
    if not muts:
        res["secs"] = round(time.time() - t0, 1)
        return res
    d = tempfile.mkdtemp(prefix="vmub_")
    try:
        if harness == "scalar":
            orc = mp.build_oracles(func, d)
            cfile = os.path.join(d, "prog_normal.c")
            binc = orc["binc"]
            eq = (lambda a, b: a == b)
            cases = mp.grid(orc["nargs"])
            res["grid_cases"] = len(cases)
            res["harness_builds"] = {k: bool(orc[k]) for k in ("binc", "binc_san", "binc_alt", "binc_msan")}
            ref_of = (lambda row: run_one(binc, row, timeout=3)[0])
        else:
            ctx = mp.arrays_ctx(func, d)
            cfile = os.path.join(d, "p_normal.c")
            binc = ctx["binc"]
            rf = ctx["ret_is_float"]
            eq = (lambda a, b: ra.vals_equal(a, b, rf))
            res["grid_cases"] = len(ctx["rows"])
            res["grid_cases_shipped"] = pf.get("harness_inputs")
            res["harness_builds"] = {k: bool(ctx[k]) for k in ("binc", "binc_san", "binc_alt")}
            ref_of = (lambda row: ra.run_one(binc, row, timeout=3)[0])
        var, errs = fr.build_variants(cfile, d)
        res["variant_build_errors"] = errs
        full = Full(var, eq)
        need = []
        for m in muts:
            kin, kc, kr = kill_sample(harness, m)
            ref = ref_of(kin)
            f = full(kin, ref)
            rec = {"id": m["id"], "op": m["op"], "old_outcome": m["outcome"], "kill_input": kin,
                   "kill_c_recorded": kc, "kill_rust_recorded": kr, "kill_c_rerun": ref,
                   "kill_c_reproduces": ref is not None and eq(ref, kc),
                   "kill_input_ub_clean": f["clean"], "kill_input_failing_checks": sorted(f["fails"])}
            if harness == "scalar":
                rec["old_unrechecked_slow"] = m.get("unrechecked_slow", 0)
            if f["clean"] and rec["kill_c_reproduces"]:
                rec.update(rescanned=False, new_outcome=m["outcome"], new_killed=True, new_kill_input=kin,
                           basis="recorded kill input is UB-clean under the full rule; kill stands")
            else:
                need.append((m, rec))
            res["mutants"].append(rec)
        if need:
            regen, sampled = regenerate(func, {m["id"] for m, _ in need})
            res["sampling_regenerated"] = sampled
            for m, rec in need:
                g = regen.get(m["id"])
                same = g is not None and all(g[k] == m[k] for k in ("op", "sub", "line", "orig", "repl"))
                rec["rescanned"] = True
                rec["mutant_regenerated_matches_record"] = same
                if not same:
                    rec.update(new_outcome="not_rescanned_mutant_mismatch", new_killed=None,
                               basis="the regenerated mutant does not match the recorded one")
                    continue
                md = tempfile.mkdtemp(dir=d)
                if harness == "scalar":
                    binr = run.build_rust(g["src"], orc["nargs"], md)
                else:
                    binr = ra.build_rust(g["src"], ctx["sig"], ctx["comps"], md)
                if not binr:
                    rec.update(new_outcome="rebuild_failed", new_killed=None, basis="the regenerated mutant did not build")
                    continue
                if harness == "scalar":
                    t = scalar_scan(orc, binr, cases, full)
                    new = scalar_outcome(t)
                    s = (t["value_samples"] + t["crash_samples"])
                    rec["rescan"] = {k: t.get(k) for k in ("cases", "candidates", "agree", "value", "rust_panic",
                                                          "rust_hang", "ub", "ub_fullrule", "timeout", "san_infra",
                                                          "unrechecked_slow", "rechecked_slow", "per_input_fallback",
                                                          "first_shipped_kill", "rejected_by_full_rule")}
                    rec["new_unrechecked_slow"] = t["unrechecked_slow"]
                    rec["new_undetermined"] = new not in KILLED and t["unrechecked_slow"] > 0
                else:
                    t = arrays_scan(ctx, binr, full)
                    new = arrays_outcome(t)
                    s = [t["sample"]] if t["sample"] else []
                    rec["rescan"] = {k: t.get(k) for k in ("candidates", "agree", "value", "crash", "ub", "ub_fullrule",
                                                          "timeout", "rust_batch_timeout", "first_shipped_kill",
                                                          "rejected_by_full_rule")}
                    rec["new_undetermined"] = new not in KILLED and bool(t["rust_batch_timeout"])
                fk = t["first_shipped_kill"]
                rec["rescan_first_shipped_kill_is_recorded_input"] = bool(fk) and fk["in"] == rec["kill_input"]
                rec["new_outcome"] = new
                rec["new_killed"] = new in KILLED
                rec["new_kill_input"] = s[0]["in"] if s else None
                rec["basis"] = ("re-scanned over the same grid in the same order; killed on a later input that is "
                                "UB-clean under the full rule" if new in KILLED else
                                "re-scanned over the same grid in the same order; no candidate is UB-clean under "
                                "the full rule")
                shutil.rmtree(md, ignore_errors=True)
        res["full_rule_checks"] = sorted(full.cache.values(), key=lambda r: (fr.l1(r["in"]), r["in"]))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    res["secs"] = round(time.time() - t0, 1)
    return res


def tool(cmd):
    return fr.tool(cmd)


def run_mode(a):
    assert shutil.which("clang") is None and shutil.which(fr.CLANG), "need clang-14 and no `clang` alias (as the primary)"
    t0 = datetime.datetime.now(datetime.timezone.utc)
    src = SCALAR_SRC if a.harness == "scalar" else ARRAYS_SRC
    d = json.load(open(os.path.join(REPO, src)))
    pfs = d["per_function"]
    if a.harness == "scalar":
        pins = set(shipped_pins(d))
        if a.shard in ("0", "1"):
            pfs = [pf for pf in pfs if (pf["func"] in pins) == (a.shard == "0")]
    if a.only:
        pfs = [pf for pf in pfs if pf["func"] in a.only.split(",")]
    jobs = [(a.harness, pf["func"], pf) for pf in pfs]
    jobs.sort(key=lambda j: -sum(1 for m in j[2]["mutants"] if counted_kill(a.harness, m)))
    controls = fr.positive_controls()
    res = []
    with mp_.Pool(a.jobs) as pool:
        for r in pool.imap_unordered(job, jobs, chunksize=1):
            res.append(r)
            nr = sum(1 for m in r["mutants"] if m.get("rescanned"))
            print(f"[{len(res)}/{len(jobs)}] {r['func']} killed={r['killed_counted_old']} rescanned={nr} {r['secs']}s",
                  flush=True)
    res.sort(key=lambda r: r["func"])
    out = {"part": {"harness": a.harness, "shard": a.shard if a.harness == "scalar" else None,
                    "functions": [r["func"] for r in res]},
           "provenance": {
               "script": "systems/vert/arxiv_mutate_ubfilter.py",
               "command": os.environ.get("RUN_COMMAND") or ("python3 -I systems/vert/arxiv_mutate_ubfilter.py " + " ".join(sys.argv[1:])),
               "git_rev": os.environ.get("GIT_REV"), "git_note": os.environ.get("GIT_NOTE"),
               "host": os.environ.get("HOST_DESC"), "arch": platform.machine(), "kernel": platform.release(),
               "image": os.environ.get("IMAGE_DESC"),
               "toolchains": {"rustc": tool(["rustc", "--version"]), "gcc": tool(["gcc", "--version"]),
                              "clang": tool([fr.CLANG, "--version"]), "python": platform.python_version()},
               "inputs": {src: sha(src)},
               "file_sha256": {p: sha(p) for p in ("systems/vert/arxiv_mutate_ubfilter.py", "systems/vert/arxiv_ub_fullrule.py",
                                                   "systems/vert/mutate_primary.py", "systems/vert/run.py",
                                                   "systems/vert/run_arrays.py", "systems/batch_diff.py",
                                                   "systems/enumerate_inputs.py", "harness/oracle_ub_common.py")},
               "full_rule_builds": {k: [v[0]] + v[1] + ["-w", "<driver.c>", "-o", "<bin>", "-lm"] for k, v in fr.PLAN.items()},
               "rule_checks": fr.RULE_CHECKS,
               "recorded_not_rule": [k for k in fr.PLAN if k not in fr.RULE_CHECKS],
               "sanitizer_env": fr.oub.SAN_ENV, "san_timeout_s": fr.SAN_TIMEOUT, "value_timeout_s": fr.VAL_TIMEOUT,
               "jobs": a.jobs,
               "date_start_utc": t0.isoformat(timespec="seconds"),
               "date_end_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")},
           "sanitizer_positive_controls": controls,
           "functions": res}
    try:
        import provenance as prov
        out["_provenance"] = prov.stamp(image=os.environ.get("IMAGE_DESC"), step=f"arxiv_mutate_ubfilter {a.harness} {a.shard}")
    except Exception as e:      # noqa: BLE001
        out["_provenance"] = {"error": str(e)}
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
        f.write("\n")
    print(json.dumps(part_summary(res), indent=1))


def part_summary(res):
    ms = [m for r in res for m in r["mutants"]]
    bad = [m for m in ms if not m["kill_input_ub_clean"]]
    by_check = collections.Counter(c for m in bad for c in m["kill_input_failing_checks"])
    return {"killed_old": len(ms), "killed_new": sum(1 for m in ms if m.get("new_killed")),
            "kill_input_not_ub_clean": len(bad),
            "kill_c_not_reproduced": sum(1 for m in ms if not m["kill_c_reproduces"]),
            "rescanned": sum(1 for m in ms if m.get("rescanned")),
            "rescanned_new_outcome": dict(collections.Counter(m["new_outcome"] for m in ms if m.get("rescanned"))),
            "not_ub_clean_by_check": dict(sorted(by_check.items()))}


# ------------------------------------------------------------------------------------------------
# merge
# ------------------------------------------------------------------------------------------------
def harness_summary(harness, src_doc, res, hp_row):
    """Old and new counts for one harness-power row."""
    ms = [m for r in res for m in r["mutants"]]
    built = sum(1 for pf in src_doc["per_function"] for m in pf["mutants"]
                if m["op"] in HEAD_OPS and m["outcome"] != "compile_fail")
    killed_old = sum(1 for pf in src_doc["per_function"] for m in pf["mutants"] if counted_kill(harness, m))
    assert len(ms) == killed_old, (harness, len(ms), killed_old)
    assert (killed_old, built) == (hp_row["killed"], hp_row["built"]), (killed_old, built, hp_row)
    killed_new = sum(1 for m in ms if m.get("new_killed"))
    bad = [m for m in ms if not m["kill_input_ub_clean"]]
    bad_inputs = {(r["func"], json.dumps(c["in"])) for r in res for c in r["full_rule_checks"] if not c["clean"]
                  and any(json.dumps(m["kill_input"]) == json.dumps(c["in"]) and not m["kill_input_ub_clean"] for m in r["mutants"])}
    reasons = collections.defaultdict(collections.Counter)
    for r in res:
        for c in r["full_rule_checks"]:
            if (r["func"], json.dumps(c["in"])) in bad_inputs:
                for k, s in c["fails"].items():
                    reasons[k][fr.reason_kind(s)] += 1
    # which C files: functions whose kill inputs were flagged
    funcs_bad = collections.Counter(r["func"] for r in res for m in r["mutants"] if not m["kill_input_ub_clean"])
    newly_undet = sum(1 for m in ms if m.get("rescanned") and m.get("new_undetermined"))
    s = {
        "built": built, "killed_old": killed_old, "killed_new": killed_new,
        "rate_old": round(killed_old / built, 4), "rate_new": round(killed_new / built, 4),
        "kill_inputs_checked": len(ms),
        "distinct_kill_inputs_checked": len({(r["func"], json.dumps(m["kill_input"])) for r in res for m in r["mutants"]}),
        "kill_c_value_reproduced": sum(1 for m in ms if m["kill_c_reproduces"]),
        "kill_input_not_ub_clean": len(bad),
        "distinct_kill_inputs_not_ub_clean": len(bad_inputs),
        "functions_with_a_kill_input_not_ub_clean": dict(sorted(funcs_bad.items())),
        "kill_inputs_not_ub_clean_by_failing_check": dict(sorted(collections.Counter(
            c for m in bad for c in m["kill_input_failing_checks"]).items())),
        "distinct_not_ub_clean_inputs_by_check_and_reason": {k: dict(v) for k, v in sorted(reasons.items())},
        "rescanned": sum(1 for m in ms if m.get("rescanned")),
        "rescanned_new_outcome": dict(sorted(collections.Counter(m["new_outcome"] for m in ms if m.get("rescanned")).items())),
        "rescanned_first_shipped_kill_is_recorded_input": sum(1 for m in ms if m.get("rescan_first_shipped_kill_is_recorded_input")),
        "rescanned_mutant_regenerated_matches_record": sum(1 for m in ms if m.get("mutant_regenerated_matches_record")),
        "newly_undetermined": newly_undet,
        "undetermined_old": hp_row["undetermined"],
        "undetermined_new": hp_row["undetermined"] + newly_undet,
        "full_rule_inputs_checked": sum(len(r["full_rule_checks"]) for r in res),
        "variant_build_errors": {r["func"]: r["variant_build_errors"] for r in res if r.get("variant_build_errors")},
    }
    if harness == "arrays":
        s["grid_size_mismatch"] = [r["func"] for r in res if r.get("grid_cases") is not None
                                   and r.get("grid_cases") != r.get("grid_cases_shipped")]
    return s


def cross_arch(path, scalar_res):
    """Compare a second run of some scalar functions on another architecture (not used for the counts)."""
    other = json.load(open(path))
    mine = {r["func"]: r for r in scalar_res}
    diffs, n_m, n_in = [], 0, 0
    for r in other["functions"]:
        b = mine[r["func"]]
        ma = {m["id"]: m for m in b["mutants"]}
        for m in r["mutants"]:
            n_m += 1
            x = ma[m["id"]]
            keys = ("kill_c_rerun", "kill_input_ub_clean", "kill_input_failing_checks", "new_outcome", "new_kill_input")
            if any(m.get(k) != x.get(k) for k in keys):
                diffs.append({"func": r["func"], "id": m["id"], "this_arch": {k: x.get(k) for k in keys},
                              "other_arch": {k: m.get(k) for k in keys}})
        fa = {json.dumps([c["in"], c["ref"]]): c for c in b["full_rule_checks"]}
        for c in r["full_rule_checks"]:
            n_in += 1
            y = fa.get(json.dumps([c["in"], c["ref"]]))
            if y is None or y["clean"] != c["clean"] or sorted(y["fails"]) != sorted(c["fails"]):
                diffs.append({"func": r["func"], "in": c["in"], "this_arch": y and {"clean": y["clean"], "fails": sorted(y["fails"])},
                              "other_arch": {"clean": c["clean"], "fails": sorted(c["fails"])}})
    return {"other_file": os.path.basename(path), "other_arch": other["provenance"]["arch"],
            "other_image": other["provenance"]["image"],
            "other_part": {**other["part"], "functions": len(other["part"]["functions"])},
            "sha256": hashlib.sha256(open(path, "rb").read()).hexdigest(),
            "mutants_compared": n_m, "full_rule_inputs_compared": n_in, "differences": diffs,
            "note": "a re-run of the same functions on the other architecture, to see whether the verdicts depend on it; "
                    "the counts use the run on the architecture of the shipped scan"}


def merge_mode(a):
    parts = [json.load(open(p)) for p in a.parts]
    hp = {r["label"]: r for r in json.load(open(os.path.join(REPO, "results", "harness_power.json")))["rows"]}
    sc = json.load(open(os.path.join(REPO, SCALAR_SRC)))
    ar = json.load(open(os.path.join(REPO, ARRAYS_SRC)))
    by = {"scalar": [], "arrays": []}
    for p in parts:
        by[p["part"]["harness"]].extend(p["functions"])
    for h, doc in (("scalar", sc), ("arrays", ar)):
        got = sorted(r["func"] for r in by[h])
        want = sorted(pf["func"] for pf in doc["per_function"])
        assert got == want, (h, sorted(set(want) - set(got)), sorted(set(got) - set(want)))
    pins = set(shipped_pins(sc))
    summary = {"VERT scalar": harness_summary("scalar", sc, by["scalar"], hp["VERT scalar"]),
               "VERT array/float": harness_summary("arrays", ar, by["arrays"], hp["VERT array/float"])}
    mutants = []
    for h in ("scalar", "arrays"):
        for r in sorted(by[h], key=lambda r: r["func"]):
            where = ("x86_64" if (h == "scalar" and r["func"] not in pins) else "aarch64")
            for m in r["mutants"]:
                mutants.append({"harness": h, "func": r["func"], "run_arch": where, **m})
    out = {
        "provenance": {
            "script": "systems/vert/arxiv_mutate_ubfilter.py",
            "command": "python3 -I systems/vert/arxiv_mutate_ubfilter.py merge " + " ".join(os.path.basename(p) for p in a.parts)
                       + " --out " + os.path.relpath(a.out, REPO),
            "parts": [{"part": p["part"]["harness"] + (f" shard {p['part']['shard']}" if p["part"]["shard"] else ""),
                       "file": os.path.basename(path), "functions": len(p["functions"]),
                       "sha256": hashlib.sha256(open(path, "rb").read()).hexdigest(),
                       **{k: v for k, v in p["provenance"].items() if k not in ("full_rule_builds",)},
                       "_provenance": p.get("_provenance"),
                       "sanitizer_positive_controls_all_as_expected": all(
                           v.get("all_as_expected", True) for v in p["sanitizer_positive_controls"].values())}
                      for path, p in zip(a.parts, parts)],
            "where_note": ("as the shipped runs: scalar shard 0 (30 pinned functions) and arrays on the arm64 Mac, "
                           "scalar shard 1 (85 functions) on the x86_64 box (results/vert_mutation_primary_grid2.json "
                           "provenance.merged_from_shards; results/vert_arrays_mutation_primary.json provenance)"),
            "full_rule_builds": parts[0]["provenance"]["full_rule_builds"],
            "inputs": {SCALAR_SRC: sha(SCALAR_SRC), ARRAYS_SRC: sha(ARRAYS_SRC),
                       "results/harness_power.json": sha("results/harness_power.json")},
            "generated_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "rule": __doc__.split("Where:")[0].strip(),
        "summary": summary,
        "sanitizer_positive_controls": {p["part"]["harness"] + (p["part"]["shard"] or ""): p["sanitizer_positive_controls"]
                                        for p in parts},
        "functions": [{k: v for k, v in r.items() if k != "mutants"} for h in ("scalar", "arrays")
                      for r in sorted(by[h], key=lambda r: r["func"])],
        "mutants": mutants,
    }
    if a.cross_arch:
        out["cross_arch_check"] = cross_arch(a.cross_arch, by["scalar"])
    import provenance as prov
    out["_provenance"] = prov.stamp(step="arxiv_mutate_ubfilter merge")
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
        f.write("\n")
    print(json.dumps(summary, indent=1))


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="mode", required=True)
    r = sp.add_parser("run")
    r.add_argument("--harness", choices=["scalar", "arrays"], required=True)
    r.add_argument("--shard", choices=["0", "1", "all"], default="all",
                   help="scalar: the shipped shard (0 = the 30 pinned functions run on arm64, 1 = the 85 run on x86_64)")
    r.add_argument("--jobs", type=int, default=6)
    r.add_argument("--only", default="", help="comma-separated function names (testing)")
    r.add_argument("--out", required=True)
    m = sp.add_parser("merge")
    m.add_argument("parts", nargs="+")
    m.add_argument("--out", required=True)
    m.add_argument("--cross-arch", default="", help="a scalar part run on the other architecture (compared, not counted)")
    a = ap.parse_args()
    if a.mode == "run":
        run_mode(a)
    else:
        merge_mode(a)


if __name__ == "__main__":
    main()
