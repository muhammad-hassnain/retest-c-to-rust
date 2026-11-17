"""Flourine on the CURRENT grid: re-run the shipped systems/flourine/run.py and run_struct.py over
every translation they cover (success and fail labels), record per-translation verdicts, compare
with results/flourine_diff.json / results/flourine_struct.json and the paper's figures, and apply
the ISO-C UB rule of harness/oracle_ub_flourine.py to every divergence.

Outputs: results/flourine_diff_current.json, results/flourine_struct_current.json.

Phase "rust" (crust-syzygy: rustc 1.86.0 + gcc 11.4.0, the toolchains of the original runs; on the
x86 box the `crust-stable` tag is the nightly-2022 image, so it is NOT used):
  1. the shipped scripts, unchanged:  run.py --out <build>/shipped_run.json  and
     run_struct.py --out <build>/shipped_struct.json   (their "summary" is what we publish);
  2. the same loops re-done per translation with the scripts' own functions (parse_rust_fn,
     ok_sig, enumerate_cases(cap=2000), build_c, build_rust, tally / discover, classify_regfn,
     build_c, build_rust, run_pair), keeping each verdict; their totals must equal (1).
Phase "c" (crust-heldout: clang 14 + gcc 11.4): for every translation that diverges, list its
divergent inputs again (batch_diff.eval_cases against the harness's own -O1 -fwrapv oracle) and
check each input against the ISO-C UB rule:
  UB-clean  <=>  no report from clang-14 or gcc-11 ASan+UBSan+float-cast-overflow built WITHOUT
                 -fwrapv (-O0, -fno-sanitize-recover=all), and both sanitizer runs reached the input
                 (no start-up failure or timeout) and print the oracle's line  AND  gcc -O0 and -O2
                 -fwrapv output == -O1 -fwrapv  AND  -funsigned-char / -fsigned-char output == oracle
                 (audit 8 uniform rule; -fwrapv and bit-field signedness are recorded, not part of it).
A translation is "wrong on a UB-clean input" if at least one value divergence is UB-clean.
"Normal input" (paper: "99 on normal inputs", "3 only on min>max"): for functions whose C
parameters are named *min*/*max*, an input with min > max is degenerate; every other input is normal.

  JOBS=6 bash harness/oracle_ub_run.sh flourine_current

--profile fuzz (phase rust) builds the Rust in FLOURINE's own check profile instead of rustc -O (see
systems/flourine/run.py RUSTC_PROFILE_FLAGS); pass --out-scalar/--out-struct to keep the shipped files.
The arXiv re-run is results/arxiv_flourine_devprofile*.json.
"""
import argparse
import concurrent.futures as cf
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import oracle_ub_common as ou  # noqa: E402
import oracle_ub_flourine as ouf  # noqa: E402  (C variants, resumable sanitizer batches, flags)
from oracle_ub_flourine import fl_scalar, fl_struct, enumerate_cases, eval_cases  # noqa: E402
from batch_diff import tally  # noqa: E402

REPO = ou.REPO
PAPER = {"benchmarks_scalar": 18, "success_tested": 101, "success_agree": 96,
         "success_agree_on_normal_inputs": 99, "success_diverge": 5, "success_diverge_only_min_gt_max": 3,
         "success_wrong_on_normal_inputs": 2, "struct_success_tested": 185, "struct_success_agree": 184}


# ------------------------------------------------------------------ helpers
def c_param_names(cfile, name):
    src = open(cfile, errors="replace").read()
    m = re.search(r"\b" + re.escape(name) + r"\s*\(([^)]*)\)\s*\{", src)
    if not m:
        return []
    return [re.split(r"[\s\*]+", p.strip())[-1] for p in m.group(1).split(",") if p.strip()]


def degenerate_fn(params):
    """index pair (min, max) if the C parameters are named like a range, else None."""
    lo = [i for i, p in enumerate(params) if "min" in p.lower()]
    hi = [i for i, p in enumerate(params) if "max" in p.lower()]
    return (lo[0], hi[0]) if lo and hi else None


def scalar_benchmarks():
    """Exactly run.py main()'s benchmark selection."""
    out = []
    for cfile in sorted(glob.glob(os.path.join(fl_scalar.BMS, "*", "*", "*.c"))):
        name = os.path.basename(cfile)[:-2]
        proj = cfile.split("/bms/c/")[1].split("/")[0]
        anyrs = glob.glob(os.path.join(fl_scalar.FL, "translations_only", "*", "c", proj, name, "*", "*", name + ".rs"))
        if not anyrs:
            continue
        sig = fl_scalar.parse_rust_fn(open(anyrs[0], errors="replace").read(), name)
        if not fl_scalar.ok_sig(sig):
            continue
        trs = []
        for verdict in ("success", "fail"):
            for rs in sorted(glob.glob(os.path.join(fl_scalar.FL, "translations_only", verdict, "c", proj, name,
                                                    "*", "*", name + ".rs"))):
                trs.append((verdict, rs))
        out.append({"name": name, "proj": proj, "cfile": cfile, "sig": sig, "translations": trs})
    return out


# ------------------------------------------------------------------ phase rust
def _scalar_job(args):
    bdir, bi, ti, b, verdict, rs, binc, profile = args
    name = b["name"]
    tag = rs.split("/c/")[1]                          # exactly run.py's tag
    s2 = fl_scalar.parse_rust_fn(open(rs, errors="replace").read(), name)
    rec = {"tag": tag, "label": verdict, "benchmark": name}
    if not fl_scalar.ok_sig(s2):
        rec["status"] = "skipped_signature"          # run.py `continue`s silently
        return rec
    d = os.path.join(bdir, "scalar_rs", f"{bi}_{ti}")
    os.makedirs(d, exist_ok=True)
    binr = fl_scalar.build_rust(name, rs, s2, d, profile=profile)
    if not binr:
        rec["status"] = "rust_build_fail"             # run.py `continue`s silently
        return rec
    t = tally(binc, binr, enumerate_cases(b["sig"]["ptypes"], cap=2000))
    rec.update({"status": "tested", "bin": binr, "tally": {k: t[k] for k in ("cases", "value", "rust_panic", "ub",
                                                                             "timeout", "agree")},
                "value_samples": t["value_samples"]})
    rec["verdict"] = (("value_bug" if t["value"] > 0 else "crash" if t["rust_crash"] > 0 else "agree")
                      if verdict == "success" else ("diverge" if (t["value"] > 0 or t["rust_crash"] > 0) else "agree"))
    return rec


def _struct_job(args):
    bdir, fi, ti, name, cfile, verdict, rs, binc, profile = args
    tag = rs.split("/opl/")[1]
    rec = {"tag": tag, "label": verdict, "fn": name}
    s2 = fl_struct.classify_regfn(open(rs, errors="replace").read(), name)
    if not s2:
        rec["status"] = "skipped_shape"
        return rec
    d = os.path.join(bdir, "struct_rs", f"{fi}_{ti}")
    os.makedirs(d, exist_ok=True)
    binr, st = fl_struct.build_rust(rs, name, s2, d, profile=profile)
    if not binr:
        rec["status"] = st                            # no_construct | build_fail
        return rec
    t = fl_struct.run_pair(binc, binr, s2)
    rec.update({"status": "tested", "bin": binr, "info": s2,
                "tally": {k: t[k] for k in ("agree", "value", "crash")}, "samples": t["samples"]})
    rec["verdict"] = (("value_bug" if t["value"] > 0 else "crash" if t["crash"] > 0 else "agree")
                      if verdict == "success" else ("diverge" if (t["value"] > 0 or t["crash"] > 0) else "agree"))
    return rec


def phase_rust(bdir, jobs, profile="release"):
    t0 = time.time()
    os.makedirs(bdir, exist_ok=True)
    # 1) shipped scripts, unchanged, in the background (one core each); --profile only when not the
    # shipped default, -I only when this process runs isolated
    py = [sys.executable] + (["-I"] if sys.flags.isolated else [])
    prof = [] if profile == "release" else ["--profile", profile]
    shipped = {
        "run.py": subprocess.Popen(py + [os.path.join(REPO, "systems", "flourine", "run.py"),
                                         "--out", os.path.join(bdir, "shipped_run.json")] + prof,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT),
        "run_struct.py": subprocess.Popen(py + [os.path.join(REPO, "systems", "flourine", "run_struct.py"),
                                                "--out", os.path.join(bdir, "shipped_struct.json")] + prof,
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)}
    # 2) per-translation loops with the scripts' own functions
    bms = scalar_benchmarks()
    sjobs = []
    for bi, b in enumerate(bms):
        d = os.path.join(bdir, "scalar_c", b["name"])
        os.makedirs(d, exist_ok=True)
        binc = fl_scalar.build_c(b["name"], b["cfile"], b["sig"], d)
        b["binc"] = binc
        if not binc:
            continue
        for ti, (verdict, rs) in enumerate(b["translations"]):
            sjobs.append((bdir, bi, ti, b, verdict, rs, binc, profile))
    targets = fl_struct.discover()
    tjobs, struct_fns = [], []
    for fi, (name, cfile, verds, info) in enumerate(targets):
        d = os.path.join(bdir, "struct_c", name)
        os.makedirs(d, exist_ok=True)
        binc = fl_struct.build_c(cfile, name, info, d)
        struct_fns.append({"fn": name, "cfile": cfile, "info": info, "c_ok": bool(binc)})
        if not binc:
            continue
        ti = 0
        for v in ("success", "fail"):
            for rs in verds[v]:
                tjobs.append((bdir, fi, ti, name, cfile, v, rs, binc, profile))
                ti += 1
    with cf.ProcessPoolExecutor(max(1, jobs - 2)) as ex:
        scalar = list(ex.map(_scalar_job, sjobs))
        print(f"[{time.time()-t0:.0f}s] scalar per-translation done: {len(scalar)}", flush=True)
        struct = list(ex.map(_struct_job, tjobs))
        print(f"[{time.time()-t0:.0f}s] struct per-translation done: {len(struct)}", flush=True)
    logs = {}
    for k, p in shipped.items():
        out, _ = p.communicate()
        logs[k] = {"rc": p.returncode, "tail": out.decode("utf-8", "replace")[-600:]}
    print(f"[{time.time()-t0:.0f}s] shipped scripts done: {logs}", flush=True)
    info = {"rustc": ou.tool_version(["rustc", "--version"]), "gcc": ou.tool_version(["gcc", "--version"]),
            "image": os.environ.get("OUB_IMAGE"), "image_id": os.environ.get("OUB_IMAGE_ID"),
            "wall_seconds": round(time.time() - t0), "shipped_logs": logs,
            "rust_profile": {"name": profile, "rustc_flags": fl_scalar.RUSTC_PROFILE_FLAGS[profile]},
            "benchmarks": [{k: b[k] for k in ("name", "proj", "cfile", "sig")} | {"c_ok": bool(b.get("binc"))}
                           for b in bms],
            "struct_functions": struct_fns, "scalar": scalar, "struct": struct}
    ou.dump(os.path.join(bdir, "rust_phase.json"), info)


# ------------------------------------------------------------------ phase c
def _c_side_job(args):
    key, pc, d, lines = args
    bins = ouf.build_variants(pc, d)
    return key, bins, ouf.c_side(bins, lines)


def rule_clean(flags):
    """Audit 8 uniform rule: no clang/gcc sanitizer report; both sanitizer runs reached the input
    (no start-up failure, no timeout) and agree with the oracle; -O0 and -O2 agree with -O1; the
    output does not change with -funsigned-char / -fsigned-char."""
    bad = ("san_clang", "san_gcc", "san_clang_not_clean", "san_gcc_not_clean", "san_clang_differs", "san_gcc_differs",
           "alt_o0_differs", "alt_o2_differs", "uchar_differs", "schar_differs")
    return not any(b in flags for b in bad)


def analyse(div, deg):
    """div: per-input dicts (in, kind, flags). Returns counts under the rule."""
    val = [d for d in div if d["kind"] == "value"]
    crash = [d for d in div if d["kind"] == "crash"]
    for d in div:
        d["rule_clean"] = rule_clean(d["flags"])
        d["degenerate_min_gt_max"] = bool(deg and isinstance(d["in"], list) and d["in"][deg[0]] > d["in"][deg[1]])
    a = {"value_inputs": len(val), "value_inputs_ub_clean": sum(d["rule_clean"] for d in val),
         "value_inputs_normal": sum(not d["degenerate_min_gt_max"] for d in val),
         "value_inputs_normal_ub_clean": sum(d["rule_clean"] and not d["degenerate_min_gt_max"] for d in val),
         "crash_inputs": len(crash), "crash_inputs_ub_clean": sum(d["rule_clean"] for d in crash),
         "hang_inputs": sum(1 for d in div if d["kind"] == "hang"),
         "value_inputs_san_report": sum(1 for d in val if any(s in d["flags"] for s in ("san_clang", "san_gcc"))),
         "value_inputs_opt_level_dependent": sum(1 for d in val if "alt_o0_differs" in d["flags"]),
         "value_inputs_O2_dependent": sum(1 for d in val if "alt_o2_differs" in d["flags"]),
         "value_inputs_fwrapv_dependent": sum(1 for d in val if "nowrap_differs" in d["flags"]),
         "value_inputs_char_signedness_dependent": sum(1 for d in val if "uchar_differs" in d["flags"] or "schar_differs" in d["flags"]),
         "value_inputs_bitfield_signedness_dependent": sum(1 for d in val if "ubitfield_differs" in d["flags"])}
    kinds = {}
    for d in val + crash:
        for s in ("san_clang", "san_gcc"):
            if s in d["flags"]:
                k = re.sub(r"\d+", "N", d["flags"][s].split("runtime error:")[-1].strip())[:100]
                kinds[k] = kinds.get(k, 0) + 1
    a["report_kinds"] = kinds
    a["smallest_ub_clean_value_inputs"] = [
        {k: d[k] for k in ("in", "c", "rust")} for d in
        sorted((d for d in val if d["rule_clean"]),
               key=lambda d: sum(abs(x) for x in d["in"]) if isinstance(d["in"], list) else 0)[:3]]
    return a


def phase_c(bdir, jobs, out_scalar, out_struct):
    t0 = time.time()
    rp = json.load(open(os.path.join(bdir, "rust_phase.json")))
    shipped_run = json.load(open(os.path.join(bdir, "shipped_run.json")))
    shipped_struct = json.load(open(os.path.join(bdir, "shipped_struct.json")))
    old_run = json.load(open(os.path.join(REPO, "results", "flourine_diff.json")))
    old_struct = json.load(open(os.path.join(REPO, "results", "flourine_struct.json")))

    # ---- C side over the whole grid, for every benchmark/function that has a divergent translation
    bm = {b["name"]: b for b in rp["benchmarks"]}
    need_s = sorted({r["benchmark"] for r in rp["scalar"] if r.get("verdict") in ("value_bug", "crash", "diverge")})
    need_t = sorted({r["fn"] for r in rp["struct"] if r.get("verdict") in ("value_bug", "crash", "diverge")})
    sfn = {f["fn"]: f for f in rp["struct_functions"]}
    cjobs, grids = [], {}
    for name in need_s:
        b = bm[name]
        d = os.path.join(bdir, "c_scalar", name)
        if os.path.isdir(d):
            shutil.rmtree(d)
        os.makedirs(d)
        fl_scalar.build_c(name, b["cfile"], b["sig"], d)                # writes d/p.c
        cases = enumerate_cases(b["sig"]["ptypes"], cap=2000)
        grids[("s", name)] = cases
        cjobs.append((("s", name), os.path.join(d, "p.c"), d, [" ".join(str(x) for x in r) for r in cases]))
    for name in need_t:
        f = sfn[name]
        d = os.path.join(bdir, "c_struct", name)
        if os.path.isdir(d):
            shutil.rmtree(d)
        os.makedirs(d)
        fl_struct.build_c(f["cfile"], name, f["info"], d)
        lines = [l for l in fl_struct.feed(f["info"]).split("\n") if l.strip()]
        grids[("t", name)] = lines
        cjobs.append((("t", name), os.path.join(d, "p.c"), d, lines))
    cside = {}
    with cf.ProcessPoolExecutor(jobs) as ex:
        for key, bins, c in ex.map(_c_side_job, cjobs):
            cside[key] = (bins, c)
            print(f"[{time.time()-t0:.0f}s] C side {key}", flush=True)

    # ---- per divergent translation: divergent inputs + flags
    def scalar_div(r):
        bins, c = cside[("s", r["benchmark"])]
        cases = grids[("s", r["benchmark"])]
        div = []
        for j, (row, cl, rl) in enumerate(eval_cases(bins["ref"], r["bin"], cases)):
            if cl == "__PARSE__" or rl == "__PARSE__":
                continue
            if cl is not None and cl != "__TIMEOUT__" and cl == rl:
                continue
            if cl is None or cl == "__TIMEOUT__":
                kind = "c_missing"
            else:
                kind = "crash" if (rl is None or rl == "__PANIC__") else ("hang" if rl == "__TIMEOUT__" else "value")
            div.append({"in": row, "c": cl, "rust": rl, "kind": kind, "flags": ouf.per_input_flags(j, c, cl)})
        return div

    def struct_div(r):
        bins, c = cside[("t", r["fn"])]
        lines = grids[("t", r["fn"])]
        ro, _, _ = ouf.batch_lines(r["bin"], lines)
        div = []
        for i, l in enumerate(lines):
            cl = c["ref"]["outs"][i]
            rl = ro[i] if i < len(ro) else None
            if cl is None or cl == rl or cl == "__PARSE__" or rl == "__PARSE__":
                continue
            kind = "crash" if (rl is None or rl == "__PANIC__") else "value"
            div.append({"in": l, "c": cl, "rust": rl, "kind": kind, "flags": ouf.per_input_flags(i, c, cl)})
        return div

    def work(item):
        kind, r = item
        if kind == "s":
            div = scalar_div(r)
            deg = degenerate_fn(c_param_names(bm[r["benchmark"]]["cfile"], r["benchmark"]))
        else:
            div = struct_div(r)
            deg = None
        a = analyse(div, deg)
        exp_v = r["tally"]["value"]
        a["consistent_with_rust_phase_tally"] = (a["value_inputs"] == exp_v)
        return r["tag"], a, div

    items = [("s", r) for r in rp["scalar"] if r.get("verdict") in ("value_bug", "crash", "diverge")] + \
            [("t", r) for r in rp["struct"] if r.get("verdict") in ("value_bug", "crash", "diverge")]
    ana = {}
    with cf.ThreadPoolExecutor(jobs) as ex:
        for tag, a, div in ex.map(work, items):
            ana[tag] = (a, div)
    print(f"[{time.time()-t0:.0f}s] analysed {len(ana)} divergent translations", flush=True)

    controls = ou.positive_controls(os.path.join(bdir, "controls"))
    common_prov = {
        "phases": {"rust": f"{rp['image']} {rp['image_id']} ({rp['rustc']}; {rp['gcc']})",
                   "c": f"{os.environ.get('OUB_IMAGE')} {os.environ.get('OUB_IMAGE_ID')}"},
        "rust_phase_wall_seconds": rp["wall_seconds"], "shipped_script_runs": rp["shipped_logs"],
        **({"rust_profile": rp["rust_profile"]} if rp.get("rust_profile", {}).get("name", "release") != "release" else {}),
        "artifact_source_digests": {"bms/c": ou.tree_digest(os.path.join(fl_scalar.FL, "bms", "c")),
                                    "translations_only": ou.tree_digest(os.path.join(fl_scalar.FL, "translations_only"))},
        "harness_script_digests": {p: ou.tree_digest(os.path.join(REPO, p))["sha256"] for p in
                                   ("systems/flourine/run.py", "systems/flourine/run_struct.py",
                                    "systems/enumerate_inputs.py", "systems/batch_diff.py")},
        "ub_rule": "UB-clean input = no clang-14/gcc-11 ASan+UBSan+float-cast-overflow report on the C built without "
                   "-fwrapv (-O0, no recovery) AND gcc -O0 -fwrapv output == harness oracle (-O1 -fwrapv)",
        "sanitizer_positive_controls": controls,
        "phase_c_wall_seconds": round(time.time() - t0)}

    # ================= scalar file =================
    per = []
    for r in rp["scalar"]:
        row = {k: r.get(k) for k in ("tag", "label", "benchmark", "status", "verdict", "tally", "value_samples")}
        if r["tag"] in ana:
            a, div = ana[r["tag"]]
            row["ub_rule"] = a
            row["wrong_on_ub_clean_input"] = a["value_inputs_ub_clean"] > 0
            row["wrong_on_ub_clean_normal_input"] = a["value_inputs_normal_ub_clean"] > 0
            row["divergent_inputs_first"] = div[:20]
        per.append(row)
    tested = [r for r in per if r["status"] == "tested"]
    succ = [r for r in tested if r["label"] == "success"]
    fail = [r for r in tested if r["label"] == "fail"]
    sdiv = [r for r in succ if r["verdict"] != "agree"]
    agg = {"benchmarks_scalar": len(rp["benchmarks"]),
           "success_tested": len(succ), "success_agree": sum(r["verdict"] == "agree" for r in succ),
           "success_value_bug": sum(r["verdict"] == "value_bug" for r in succ),
           "success_crash": sum(r["verdict"] == "crash" for r in succ),
           "fail_tested": len(fail), "fail_diverge": sum(r["verdict"] == "diverge" for r in fail),
           "fail_agree": sum(r["verdict"] == "agree" for r in fail),
           "build_fail": sum(1 for b in rp["benchmarks"] if not b["c_ok"])}
    s_sum = shipped_run["summary"]
    paper_now = {
        "benchmarks_scalar": agg["benchmarks_scalar"], "success_tested": agg["success_tested"],
        "success_agree": agg["success_agree"],
        "success_diverge": len(sdiv),
        "success_diverge_only_min_gt_max": sum(1 for r in sdiv if r["ub_rule"]["value_inputs"] > 0
                                               and r["ub_rule"]["value_inputs_normal"] == 0
                                               and r["ub_rule"]["crash_inputs"] == 0),
        "success_wrong_on_normal_inputs": sum(1 for r in sdiv if r["ub_rule"]["value_inputs_normal"] > 0),
    }
    paper_now["success_agree_on_normal_inputs"] = paper_now["success_agree"] + paper_now["success_diverge_only_min_gt_max"]
    ub_rule = {
        "success_wrong_on_ub_clean_input": sum(1 for r in sdiv if r.get("wrong_on_ub_clean_input")),
        "success_wrong_on_ub_clean_normal_input": sum(1 for r in sdiv if r.get("wrong_on_ub_clean_normal_input")),
        "success_crash_on_ub_clean_input": sum(1 for r in sdiv if r["ub_rule"]["crash_inputs_ub_clean"] > 0),
        "success_divergent_translations_with_any_ub_input": sorted(
            r["tag"] for r in sdiv if r["ub_rule"]["value_inputs"] + r["ub_rule"]["crash_inputs"]
            > r["ub_rule"]["value_inputs_ub_clean"] + r["ub_rule"]["crash_inputs_ub_clean"]),
        "fail_diverge": agg["fail_diverge"],
        "fail_diverge_on_ub_clean_input": sum(1 for r in fail if r["verdict"] == "diverge" and (
            r["ub_rule"]["value_inputs_ub_clean"] + r["ub_rule"]["crash_inputs_ub_clean"]) > 0),
        "fail_diverge_only_on_ub_inputs": sorted(r["tag"] for r in fail if r["verdict"] == "diverge" and (
            r["ub_rule"]["value_inputs_ub_clean"] + r["ub_rule"]["crash_inputs_ub_clean"]) == 0),
        "translations_whose_divergence_list_matched_the_rust_phase": sum(
            1 for a, _ in ana.values() if a["consistent_with_rust_phase_tally"]),
        "divergent_translations_analysed": len(ana),
    }
    old_sd = {s["t"]: s["value"] for s in old_run["success_divergences"]}
    new_sd = {s["t"]: s["value"] for s in shipped_run["success_divergences"]}
    comp = {
        "old_file": "results/flourine_diff.json",
        "summary_old": old_run["summary"], "summary_new": s_sum,
        "summary_fields_changed": {k: [old_run["summary"].get(k), s_sum.get(k)] for k in s_sum
                                   if old_run["summary"].get(k) != s_sum.get(k)},
        "success_divergences_old_vs_new_value_inputs": {t: [old_sd.get(t), new_sd.get(t)] for t in sorted(set(old_sd) | set(new_sd))},
        "note": "the old file stores per-translation results only for success translations that diverge; "
                "per-translation verdicts for all others are new in this file",
        "paper_figures": {k: {"paper": PAPER[k], "now": paper_now[k], "same": PAPER[k] == paper_now[k]} for k in paper_now},
    }
    consistency = {"per_translation_totals_equal_shipped_summary":
                   all(agg[k] == s_sum.get(k) for k in agg), "per_translation_totals": agg}
    ou.dump(out_scalar, {
        "provenance": ou.provenance(__file__, {**common_prov,
                                               "shipped_command": "python3 systems/flourine/run.py --out <tmp>  (in the rust-phase image)"}),
        "summary": s_sum,
        "success_divergences": shipped_run["success_divergences"],
        "consistency": consistency,
        "comparison_with_old": comp,
        "iso_c_ub_rule": ub_rule,
        "per_translation": per,
    })

    # ================= struct file =================
    perT = []
    for r in rp["struct"]:
        row = {k: r.get(k) for k in ("tag", "label", "fn", "status", "verdict", "tally", "samples")}
        if r["tag"] in ana:
            a, div = ana[r["tag"]]
            row["ub_rule"] = a
            row["wrong_on_ub_clean_input"] = a["value_inputs_ub_clean"] > 0
            row["divergent_inputs_first"] = div[:20]
        perT.append(row)
    testedT = [r for r in perT if r["status"] == "tested"]
    sT = [r for r in testedT if r["label"] == "success"]
    fT = [r for r in testedT if r["label"] == "fail"]
    aggT = {"drivable_functions": len(rp["struct_functions"]),
            "success_tested": len(sT), "success_agree": sum(r["verdict"] == "agree" for r in sT),
            "success_value_bug": sum(r["verdict"] == "value_bug" for r in sT),
            "success_crash": sum(r["verdict"] == "crash" for r in sT),
            "fail_tested": len(fT), "fail_agree": sum(r["verdict"] == "agree" for r in fT),
            "fail_diverge": sum(r["verdict"] == "diverge" for r in fT),
            "build_fail": sum(1 for f in rp["struct_functions"] if not f["c_ok"]),
            "no_construct": sum(1 for r in perT if r["status"] == "no_construct"),
            "rust_build_fail": sum(1 for r in perT if r["status"] == "build_fail")}
    t_sum = shipped_struct["summary"]
    old_t = {s["t"]: s["value"] for s in old_struct["success_divergences"]}
    new_t = {s["t"]: s["value"] for s in shipped_struct["success_divergences"]}
    ub_ruleT = {
        "success_wrong_on_ub_clean_input": sum(1 for r in sT if r.get("wrong_on_ub_clean_input")),
        "success_value_bug": aggT["success_value_bug"],
        "success_crash_on_ub_clean_input": sum(1 for r in sT if r.get("ub_rule", {}).get("crash_inputs_ub_clean", 0) > 0),
        "fail_diverge": aggT["fail_diverge"],
        "fail_diverge_on_ub_clean_input": sum(1 for r in fT if r["verdict"] == "diverge" and (
            r["ub_rule"]["value_inputs_ub_clean"] + r["ub_rule"]["crash_inputs_ub_clean"]) > 0),
        "fail_diverge_only_on_ub_inputs": sorted(r["tag"] for r in fT if r["verdict"] == "diverge" and (
            r["ub_rule"]["value_inputs_ub_clean"] + r["ub_rule"]["crash_inputs_ub_clean"]) == 0),
    }
    compT = {"old_file": "results/flourine_struct.json", "summary_old": old_struct["summary"], "summary_new": t_sum,
             "summary_fields_changed": {k: [old_struct["summary"].get(k), t_sum.get(k)] for k in t_sum
                                        if old_struct["summary"].get(k) != t_sum.get(k)},
             "success_divergences_old_vs_new": {t: [old_t.get(t), new_t.get(t)] for t in sorted(set(old_t) | set(new_t))},
             "paper_figures": {"struct_success_tested": {"paper": 185, "now": aggT["success_tested"]},
                               "struct_success_agree": {"paper": 184, "now": aggT["success_agree"]}},
             "stale": old_struct["summary"] != t_sum or old_t != new_t}
    ou.dump(out_struct, {
        "provenance": ou.provenance(__file__, {**common_prov,
                                               "shipped_command": "python3 systems/flourine/run_struct.py --out <tmp>  (in the rust-phase image)"}),
        "summary": t_sum,
        "function_list": shipped_struct.get("function_list"),
        "success_divergences": shipped_struct["success_divergences"],
        "consistency": {"per_translation_totals_equal_shipped_summary": all(aggT[k] == t_sum.get(k) for k in aggT),
                        "per_translation_totals": aggT},
        "comparison_with_old": compT,
        "iso_c_ub_rule": ub_ruleT,
        "per_translation": perT,
    })
    print(json.dumps({"scalar": {"summary_new": s_sum, "fields_changed": comp["summary_fields_changed"],
                                 "paper": comp["paper_figures"], "ub_rule": ub_rule, "consistency": consistency["per_translation_totals_equal_shipped_summary"]},
                      "struct": {"fields_changed": compT["summary_fields_changed"], "stale": compT["stale"],
                                 "ub_rule": ub_ruleT}}, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["rust", "c"], required=True)
    ap.add_argument("--build", default="/tmp/oub_flourine_current")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--out-scalar", default=os.path.join(REPO, "results", "flourine_diff_current.json"))
    ap.add_argument("--out-struct", default=os.path.join(REPO, "results", "flourine_struct_current.json"))
    ap.add_argument("--profile", choices=sorted(fl_scalar.RUSTC_PROFILE_FLAGS), default="release",
                    help="Rust build profile for phase rust (default release = rustc -O, as shipped; "
                         "fuzz = FLOURINE's own check profile, overflow checks on)")
    args = ap.parse_args()
    if args.phase == "rust":
        phase_rust(args.build, args.jobs, args.profile)
    else:
        phase_c(args.build, args.jobs, args.out_scalar, args.out_struct)


if __name__ == "__main__":
    main()
