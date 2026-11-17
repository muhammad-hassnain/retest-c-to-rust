"""arXiv issue 38: which VERT scalar translations diverge from the C ONLY outside the domain we test.

The paper's VERT paragraph says "We do not count 11 further functions that diverge only on negative or
larger inputs, outside the domain we test." The 11 was computed (scripts/claims_ledger.py _c_signed) as
the value_bug_functions of results/vert_diff_fullgrid.json, an archival run whose command is unknown
(pre-round-3 harness: no return-type gate, 33 bool functions "c-build-fail"), minus the counted scalar
functions. This script recomputes the set from the current harness, with a stated definition.

Domain we test (scalar, grid v2; results/vert_diff_grid2.json): every argument in 0..512.

Functions examined
  (a) the 176 int-return scalar functions the primary tests (status "tested" in vert_diff_grid2.json);
  (b) the 16 scalar functions the primary excludes because the C returns a wider INTEGER type
      (long, long long, unsigned long long; status "non_int_return"). The primary's C driver already
      prints (long long) f_gold(...), so their C value is printed in full; VERT's Rust returns i32.
      Each exposing input records whether the C value fits in i32. They are outside the 176 by rule.
  The 6 non_int_return functions that return float/double are not examined here (5 are tested by the
  array/float harness, results/vert_arrays_fixed.json; CALCULATE_AREA_TETRAHEDRON is unsupported there).

Builds: exactly run.test_function's (run.build_c gcc -O1 -fwrapv oracle, gcc -O0 ASan+UBSan filter,
gcc -O0 -fwrapv cross-check when the C uses no float and MSan is unavailable, run.build_rust rustc -O).

Input sets per function (k = number of int arguments)
  W  wide grid: enumerate_cases(k x i32, cap=80, max_abs=8192, nonneg=False, grid="v2"), i.e. run.py's
     --full preset (signed, |x| <= 8192, as the archival file) on grid v2;
  T  tested grid: enumerate_cases(k x i32, cap=80, max_abs=512, nonneg=True, grid="v2") (the primary's),
     run for every function;
  D  dense in-domain set, built only for functions with a divergence on W: k = 1: every n in 0..512;
     k >= 2: T + the in-domain points of W + enumerate_cases(cap=2000, max_abs=512, nonneg=True,
     grid="v2") + the box 0..B per argument with (B+1)^k <= 1000.

Per input, the primary rule (batch_diff.tally's classification, applied per input): agree | value (both
return, values differ) | crash (C returns, Rust panics / prints nothing) | hang (C returns, Rust times
out) | ub (gcc ASan/UBSan report, or -O0 != -O1) | timeout / unresolved. Every value / crash / hang input
(for a function without a scalar ledger row: every one in 0..512, and outside 0..512 smallest first
until 20 pass; functions with a ledger row are not re-judged) is then checked under the FULL rule of systems/vert/arxiv_ub_fullrule.py (gcc-11 and clang-14 ASan+UBSan+
float-cast-overflow at -O0/-O1/-O2; gcc/clang -O0/-O1/-O2 -fwrapv; signed/unsigned char), reference =
the primary oracle's value. "Exposing" below means primary value/crash AND full-rule clean.

Archival check: every sample input results/vert_diff_fullgrid.json records for the functions behind the
paper's old count (its value_bug and rust_panic functions minus the counted scalar functions) is re-run
here: the primary verdict now, and the full rule applied whatever that verdict is, with its reasons.

Classification (a function's divergences = exposing value or crash inputs; hangs are reported apart,
as the ledger lists hangs but does not count them):
  counted_in_ledger       the function has a counted scalar row in results/defect_ledger.json
  ledger_not_counted      it has a scalar ledger row that is not counted (DELANNOY_NUMBER_1, hangs)
  only_outside_domain     not in the ledger; >= 1 exposing input outside 0..512 (on W); 0 exposing
                          inputs in 0..512 (on T, W and D)
  in_domain_off_grid      not in the ledger; >= 1 exposing input in 0..512, none on T (a grid miss)
  in_domain_on_grid       not in the ledger; >= 1 exposing input on T (would be a missed defect)
  no_clean_divergence     primary divergences on W exist but none passes the full rule
  hang_only               only Rust hangs on UB-clean inputs (no value/crash)
  no_divergence           every primary divergence on W is C UB under the primary rule (or none at all)

  docker run --rm --cpus 4 -v $PWD:/work -w /work -e RUSTUP_TOOLCHAIN=1.86.0 crust-vert-toolchain \
      python3 -I systems/vert/arxiv_outside_domain.py --jobs 4 --out results/arxiv_vert_outside_domain.json
"""
import argparse
import datetime
import json
import multiprocessing as mp
import os
import platform
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "harness"))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import run as srun                      # noqa: E402  systems/vert/run.py
import arxiv_ub_fullrule as fr          # noqa: E402  the full UB rule (build_variants, check_input)
from enumerate_inputs import enumerate_cases                    # noqa: E402
from batch_diff import eval_cases, lines_aligned, san_run       # noqa: E402

LO, HI = 0, 512
WIDE = dict(cap=80, max_abs=8192, nonneg=False, grid="v2")
TESTED = dict(cap=80, max_abs=512, nonneg=True, grid="v2")
DENSE_CAP = 2000
OUTSIDE_ENOUGH = 20       # outside-domain inputs: stop the full-rule checks after this many UB-clean value/crash
BOX_MAX = 1000
GRID2 = os.path.join(REPO, "results", "vert_diff_grid2.json")
LEDGER = os.path.join(REPO, "results", "defect_ledger.json")
ARCHIVAL = os.path.join(REPO, "results", "vert_diff_fullgrid.json")
INT32 = (-2 ** 31, 2 ** 31 - 1)


def in_domain(row):
    return all(LO <= x <= HI for x in row)


def key(row):
    return json.dumps(row)


def dense_set(k, T, W):
    if k == 1:
        return [[n] for n in range(LO, HI + 1)]
    out, seen = [], set()

    def add(r):
        if key(r) not in seen:
            seen.add(key(r)); out.append(list(r))
    for r in T:
        add(r)
    for r in W:
        if in_domain(r):
            add(r)
    for r in enumerate_cases(["i32"] * k, cap=DENSE_CAP, max_abs=HI, nonneg=True, grid="v2"):
        add(r)
    b = 0
    while (b + 2) ** k <= BOX_MAX:
        b += 1
    from itertools import product
    for r in product(range(b + 1), repeat=k):
        add(list(r))
    return out


def classify(binc, binr, binc_san, binc_alt, cases):
    """batch_diff.tally's per-input classification, returning every input's verdict."""
    altl = lines_aligned(binc_alt, cases) if binc_alt is not None else None
    recs, cands = [], []
    for i, (row, cl, rl) in enumerate(eval_cases(binc, binr, cases)):
        if cl == "__PARSE__" or rl == "__PARSE__":
            recs.append({"in": row, "v": "parse"}); continue
        if cl is not None and cl != "__TIMEOUT__" and cl == rl:
            recs.append({"in": row, "v": "agree", "c": cl, "rust": rl}); continue
        if altl is not None and cl is not None and cl != "__TIMEOUT__":
            alt = altl[i] if i < len(altl) else None
            if alt != "__PARSE__" and alt != cl:
                recs.append({"in": row, "v": "ub", "why": "-O0 differs", "c": cl, "rust": rl}); continue
        if cl is None or cl == "__TIMEOUT__":
            tag = "c_missing"
        elif rl == "__TIMEOUT__":
            tag = "hang"
        elif rl is None or rl == "__PANIC__":
            tag = "crash"
        else:
            tag = "value"
        cands.append((row, cl, rl, tag))
    for row, cl, rl, tag in cands:
        v, sline = san_run(binc_san, row) if binc_san is not None else ("clean", cl)
        if v == "ub":
            recs.append({"in": row, "v": "ub", "why": "gcc ASan/UBSan", "c": cl, "rust": rl}); continue
        if v in ("infra", "timeout") or (tag != "c_missing" and sline != cl):
            recs.append({"in": row, "v": "unresolved", "c": cl, "rust": rl}); continue
        if tag == "c_missing":
            recs.append({"in": row, "v": "timeout", "c": cl, "rust": rl}); continue
        recs.append({"in": row, "v": tag, "c": cl, "rust": rl})
    return recs


def fits_i32(s):
    try:
        return INT32[0] <= int(s) <= INT32[1]
    except (TypeError, ValueError):
        return None


def job(args):
    func, group, arch_inputs, in_ledger = args
    t0 = datetime.datetime.now(datetime.timezone.utc)
    rust, argtypes = srun.extract_llm_rust(func)
    csrc = srun.c_source(func)
    k = len(argtypes)
    with tempfile.TemporaryDirectory() as d:
        binc = srun.build_c(func, csrc, k, d)
        binr = srun.build_rust(rust, k, d)
        if not binc or not binr:
            return {"func": func, "group": group, "status": "build-fail", "c_built": bool(binc), "rust_built": bool(binr)}
        binc_san = srun.build_c(func, csrc, k, d, mode="san")
        binc_msan = srun.build_c(func, csrc, k, d, mode="msan")
        binc_alt = srun.build_c(func, csrc, k, d, mode="alt") if (binc_msan is None and not srun.c_uses_float(csrc)) else None
        W = enumerate_cases(["i32"] * k, **WIDE)
        T = enumerate_cases(["i32"] * k, **TESTED)
        tset = {key(r) for r in T}
        wset = {key(r) for r in W}
        recs = {key(r["in"]): dict(r, sets=["W"]) for r in classify(binc, binr, binc_san, binc_alt, W)}
        div_w = [r for r in recs.values() if r["v"] in ("value", "crash", "hang")]
        for r in classify(binc, binr, binc_san, binc_alt, [r for r in T if key(r) not in wset]):
            recs[key(r["in"])] = dict(r, sets=[])
        for r in T:
            recs[key(r)]["sets"].append("T")
        D = []
        if div_w and not in_ledger:
            D = dense_set(k, T, W)
            todo = [r for r in D if key(r) not in recs]
            for r in classify(binc, binr, binc_san, binc_alt, todo):
                recs[key(r["in"])] = dict(r, sets=[])
            for r in D:
                if "D" not in recs[key(r)]["sets"]:
                    recs[key(r)]["sets"].append("D")
        for r in recs.values():
            r["on_T"] = key(r["in"]) in tset
            r["in_domain"] = in_domain(r["in"])
        cand_all = [r for r in recs.values() if r["v"] in ("value", "crash", "hang")]
        mag = lambda r: (sum(abs(x) for x in r["in"]), r["in"])
        cand_in = sorted((r for r in cand_all if r["in_domain"]), key=mag)
        cand_out = sorted((r for r in cand_all if not r["in_domain"]), key=mag)
        var_err, var = {}, None
        cand, out_checked = [], 0
        if (cand_all and not in_ledger) or arch_inputs:
            cfile = os.path.join(d, "prog_normal.c")
            var, var_err = fr.build_variants(cfile, d)

        def full(r):
            clean, fails, info = fr.check_input(var, r["in"], r["c"], lambda a, b: a == b)
            r["full_rule_clean"] = clean
            r["full_rule_fails"] = sorted(fails)
            r["msan_info"] = info.get("info_clang_msan-O1")
            cand.append(r)
        if cand_all and not in_ledger:
            for r in cand_in:                      # every in-domain candidate
                full(r)
            found = 0
            for r in cand_out:                     # outside: smallest first, until OUTSIDE_ENOUGH clean value/crash
                if found >= OUTSIDE_ENOUGH:
                    break
                full(r)
                out_checked += 1
                found += bool(r["full_rule_clean"] and r["v"] in ("value", "crash"))
        exp = [r for r in cand if r.get("full_rule_clean")]
        for r in exp:
            r["c_fits_i32"] = fits_i32(r["c"])
        # the archival file's own sample inputs for this function, re-run one by one: primary verdict now,
        # and the full rule applied whatever the primary verdict (with the reasons it fails)
        arch_check = []
        if arch_inputs:
            todo = [x["in"] for x in arch_inputs if key(x["in"]) not in recs]
            for r in classify(binc, binr, binc_san, binc_alt, todo):
                recs[key(r["in"])] = dict(r, sets=[], on_T=key(r["in"]) in tset, in_domain=in_domain(r["in"]))
            for x in arch_inputs:
                r = recs[key(x["in"])]
                clean, fails, info = fr.check_input(var, r["in"], r.get("c"), lambda a, b: a == b)
                arch_check.append({"in": r["in"], "archival_c": x.get("c"), "archival_rust": x.get("rust"),
                                   "c_now": r.get("c"), "rust_now": r.get("rust"), "in_domain": in_domain(r["in"]),
                                   "on_T": key(r["in"]) in tset, "primary_verdict_now": r["v"], "primary_why": r.get("why"),
                                   "full_rule_clean": clean,
                                   "full_rule_fails": {k2: v2[:160] for k2, v2 in sorted(fails.items())},
                                   "msan_info": info.get("info_clang_msan-O1"),
                                   "c_fits_i32": fits_i32(r.get("c"))})

        def pick(rs, n=3):
            rs = sorted(rs, key=lambda r: (sum(abs(x) for x in r["in"]), r["in"]))
            return [{"in": r["in"], "c": r["c"], "rust": r["rust"], "kind": r["v"], "on_T": r["on_T"],
                     **({"c_fits_i32": r["c_fits_i32"]} if group == "wide_int_return" else {})} for r in rs[:n]]
        vc = [r for r in exp if r["v"] in ("value", "crash")]
        out_dom = [r for r in vc if not r["in_domain"]]
        in_dom = [r for r in vc if r["in_domain"]]
        in_dom_T = [r for r in in_dom if r["on_T"]]
        hang = [r for r in exp if r["v"] == "hang"]
        counts = {}
        for r in recs.values():
            counts[r["v"]] = counts.get(r["v"], 0) + 1
        res = {
            "func": func, "group": group, "status": "checked", "nargs": k,
            "c_return": srun.c_return_type(csrc), "c_uses_float": srun.c_uses_float(csrc),
            "sizes": {"W": len(W), "T": len(T), "D": len(D), "distinct_inputs_run": len(recs)},
            "primary_verdict_counts": counts,
            "primary_divergences_on_W": len(div_w),
            "primary_in_domain_value_or_crash": sum(r["v"] in ("value", "crash") for r in cand_in),
            "primary_outside_domain_value_or_crash": sum(r["v"] in ("value", "crash") for r in cand_out),
            "full_rule_scope": ("skipped: the function has a scalar ledger row" if in_ledger else
                                f"every in-domain primary divergence ({len(cand_in)}); outside the domain smallest first "
                                f"until {OUTSIDE_ENOUGH} UB-clean value/crash inputs ({out_checked} of {len(cand_out)} checked)"),
            "primary_divergent_inputs_checked_full_rule": len(cand),
            "full_rule_variant_build_errors": var_err,
            "exposing": {
                "outside_domain": len(out_dom), "in_domain": len(in_dom), "in_domain_on_T": len(in_dom_T),
                "outside_domain_value": sum(r["v"] == "value" for r in out_dom),
                "outside_domain_crash": sum(r["v"] == "crash" for r in out_dom),
                "in_domain_value": sum(r["v"] == "value" for r in in_dom),
                "in_domain_crash": sum(r["v"] == "crash" for r in in_dom),
                "hang_ub_clean": len(hang), "hang_ub_clean_in_domain": sum(r["in_domain"] for r in hang),
            },
            "smallest_outside_domain": pick(out_dom),
            "smallest_in_domain": pick(in_dom),
            "smallest_in_domain_on_T": pick(in_dom_T),
            "smallest_hang": pick(hang),
            "primary_divergences_failing_full_rule": [
                {"in": r["in"], "c": r["c"], "rust": r["rust"], "kind": r["v"], "fails": r["full_rule_fails"][:6]}
                for r in sorted((r for r in cand if not r.get("full_rule_clean")),
                                key=lambda r: (not r["in_domain"], sum(abs(x) for x in r["in"])))[:5]],
            "archival_samples_rechecked": arch_check,
            "secs": round((datetime.datetime.now(datetime.timezone.utc) - t0).total_seconds(), 1),
        }
        if in_ledger:
            res["exposing"] = None                 # not re-judged (full_rule_scope)
        elif group == "wide_int_return":
            res["exposing"]["in_domain_c_fits_i32"] = sum(bool(r.get("c_fits_i32")) for r in in_dom)
            res["exposing"]["outside_domain_c_fits_i32"] = sum(bool(r.get("c_fits_i32")) for r in out_dom)
            res["smallest_in_domain_c_fits_i32"] = pick([r for r in in_dom if r.get("c_fits_i32")])
        return res


def classify_function(r, counted, ledger_other):
    f = r["func"]
    if r["status"] != "checked":
        return "build_fail"
    e = r["exposing"]
    if f in counted:
        return "counted_in_ledger"
    if f in ledger_other:
        return "ledger_not_counted"
    if e["in_domain_on_T"]:
        return "in_domain_on_grid"
    if e["in_domain"]:
        return "in_domain_off_grid"
    if e["outside_domain"]:
        return "only_outside_domain"
    if e["hang_ub_clean"]:
        return "hang_only"
    if r["primary_divergences_on_W"]:
        return "no_clean_divergence"
    return "no_divergence"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    assert shutil.which("clang") is None and shutil.which(fr.CLANG), "need clang-14 and no `clang` alias (as the primary)"
    t0 = datetime.datetime.now(datetime.timezone.utc)
    g2 = json.load(open(GRID2))
    tested = [r["func"] for r in g2["all"] if r["status"] == "tested"]
    wide = [r["func"] for r in g2["all"] if r["status"] == "non_int_return"
            and not any(t in (r.get("c_return") or "") for t in ("float", "double"))]
    floats = [r["func"] for r in g2["all"] if r["status"] == "non_int_return" and r["func"] not in wide]
    L = json.load(open(LEDGER))
    vs = [r for r in L["rows"] if r["system"] == "VERT" and r["exposure_breakdown"].get("family") == "scalar"]
    counted = sorted({r["translation"].split()[0] for r in vs if r["count_status"] == "counted"})
    ledger_other = {r["translation"].split()[0]: r["count_status"] for r in vs if r["count_status"] != "counted"}
    arch = json.load(open(ARCHIVAL))
    arch_vb = set(arch["summary"]["value_bug_functions"])
    arch_11 = sorted(arch_vb - set(counted))
    arch_byf = {x["func"]: x for x in arch["all"]}
    arch_rp = sorted(set(arch["summary"]["rust_panic_functions"]) - set(counted))
    arch_in = {f: (arch_byf[f].get("value_samples") or []) + (arch_byf[f].get("rust_panic_samples") or [])
               for f in arch_11 + arch_rp}
    ledger_funcs = set(counted) | set(ledger_other)
    jobs = [(f, "int_return_tested", arch_in.get(f, []), f in ledger_funcs) for f in tested] + \
           [(f, "wide_int_return", arch_in.get(f, []), f in ledger_funcs) for f in wide]
    if a.only:
        jobs = [j for j in jobs if j[0] in a.only.split(",")]
    controls = fr.positive_controls()
    res = []
    with mp.Pool(a.jobs) as pool:
        for r in pool.imap_unordered(job, jobs, chunksize=1):
            res.append(r)
            print(f"[{len(res)}/{len(jobs)}] {r['func']} {r.get('secs')}s", flush=True)
    res.sort(key=lambda r: (r["group"], r["func"]))
    for r in res:
        r["classification"] = classify_function(r, counted, ledger_other)

    def names(group, cls):
        return [r["func"] for r in res if r["group"] == group and r["classification"] == cls]
    by = {}
    for r in res:
        by.setdefault(r["group"], {}).setdefault(r["classification"], []).append(r["func"])

    # the archival number, recomputed the way scripts/claims_ledger.py _c_signed computes it
    rmap = {r["func"]: r for r in res}
    archival = []
    for f in arch_11 + arch_rp:
        x = arch_byf[f]
        r = rmap.get(f)
        archival.append({
            "func": f,
            "archival_kind": "value_bug" if f in arch_vb else "rust_panic",
            "archival_samples": (x.get("value_samples") or x.get("rust_panic_samples") or [])[:3],
            "archival_sample_in_domain": [in_domain(s["in"]) for s in (x.get("value_samples") or x.get("rust_panic_samples") or [])[:3]],
            "grid2_status": next((y["status"] for y in g2["all"] if y["func"] == f), None),
            "grid2_c_return": next((y.get("c_return") for y in g2["all"] if y["func"] == f), None),
            "ledger": ("counted" if f in counted else ledger_other.get(f)),
            "this_run_classification": r["classification"] if r else ("not examined (float/double return; array/float harness)" if f in floats else None),
            "this_run_exposing": r["exposing"] if r else None,
            "this_run_smallest_in_domain": r.get("smallest_in_domain") if r else None,
            "this_run_smallest_outside_domain": r.get("smallest_outside_domain") if r else None,
            "this_run_in_domain_failing_full_rule": [p for p in (r or {}).get("primary_divergences_failing_full_rule", []) if in_domain(p["in"])],
            "archival_samples_rechecked": (r or {}).get("archival_samples_rechecked"),
        })

    summary = {
        "definition": ("A function diverges only outside the domain we test iff it has no scalar ledger row, at "
                       "least one input outside 0..512 on the wide grid W (signed, |x| <= 8192, grid v2) where "
                       "the C passes the full UB rule and the Rust returns a different value or panics, and no "
                       "such input in 0..512 on T (the tested grid v2), W or the dense in-domain set D (every n "
                       "in 0..512 for one-argument functions). Outside-domain exposing counts are lower bounds: the "
                       "full-rule checks stop after 20 UB-clean inputs. Functions with a scalar ledger row are not re-judged."),
        "int_return_functions_examined": sum(r["group"] == "int_return_tested" for r in res),
        "only_outside_domain_count": len(names("int_return_tested", "only_outside_domain")),
        "only_outside_domain": names("int_return_tested", "only_outside_domain"),
        "in_domain_off_grid": names("int_return_tested", "in_domain_off_grid"),
        "in_domain_on_grid_not_in_ledger": names("int_return_tested", "in_domain_on_grid"),
        "hang_only_not_in_ledger": names("int_return_tested", "hang_only"),
        "no_clean_divergence": names("int_return_tested", "no_clean_divergence"),
        "counted_in_ledger_with_primary_outside_divergence": [r["func"] for r in res if r["classification"] == "counted_in_ledger" and r["primary_outside_domain_value_or_crash"]],
        "by_group_and_class": {g: {c: len(v) for c, v in d.items()} for g, d in by.items()},
        "wide_int_return_functions_examined": sum(r["group"] == "wide_int_return" for r in res),
        "wide_int_return_in_domain_divergence": [r["func"] for r in res if r["group"] == "wide_int_return" and r["status"] == "checked" and r["exposing"]["in_domain"]],
        "wide_int_return_in_domain_divergence_c_fits_i32": [r["func"] for r in res if r["group"] == "wide_int_return" and r["status"] == "checked" and r["exposing"].get("in_domain_c_fits_i32")],
        "wide_int_return_only_outside_domain": names("wide_int_return", "only_outside_domain"),
        "float_double_return_not_examined": floats,
        "archival_count_recomputed": {"how": "scripts/claims_ledger.py _c_signed: value_bug_functions of results/vert_diff_fullgrid.json minus the counted scalar functions",
                                      "count": len(arch_11), "functions": arch_11},
    }
    import provenance as prov
    out = {
        "about": "arXiv issue 38: the paper's '11 further functions that diverge only on negative or larger inputs'.",
        "script": "systems/vert/arxiv_outside_domain.py",
        "command": os.environ.get("RUN_COMMAND") or ("python3 -I systems/vert/arxiv_outside_domain.py " + " ".join(sys.argv[1:])),
        "method": __doc__.split("Domain we test")[1].split("  docker run")[0].strip(),
        "grids": {"W": WIDE, "T": TESTED, "D": {"one_argument": "every n in 0..512",
                                               "several": f"T + in-domain W + enumerate_cases(cap={DENSE_CAP}, max_abs=512, nonneg=True, grid='v2') + box 0..B, (B+1)^k <= {BOX_MAX}"}},
        "summary": summary,
        "archival_11_and_panics": archival,
        "sanitizer_positive_controls": controls,
        "functions": res,
        "provenance": {
            "git_rev": os.environ.get("GIT_REV"), "git_dirty_note": os.environ.get("GIT_DIRTY"),
            "host": os.environ.get("HOST_DESC"), "image": os.environ.get("IMAGE_DESC"),
            "arch": platform.machine(), "toolchains": {"rustc": fr.tool(["rustc", "--version"]), "gcc": fr.tool(["gcc", "--version"]),
                                                      "clang": fr.tool([fr.CLANG, "--version"]), "python": platform.python_version()},
            "full_rule_builds": {k: [v[0]] + v[1] for k, v in fr.PLAN.items()}, "full_rule_checks": fr.RULE_CHECKS,
            "date_start_utc": t0.isoformat(timespec="seconds"),
            "date_end_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        },
    }
    out["_provenance"] = prov.stamp(image=os.environ.get("IMAGE_DESC"), step="arxiv_outside_domain")
    if out["_provenance"].get("git_commit") is None:
        out["_provenance"]["git_commit"] = os.environ.get("GIT_REV")
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
        f.write("\n")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
