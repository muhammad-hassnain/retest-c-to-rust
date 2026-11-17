"""M3 items 4 and 5: re-check every VERT wrong answer (scalar run.py + array/float run_arrays.py)
for C undefined / implementation-defined behaviour that the arm64 primary's filters cannot see.

The primary filters (gcc ASan+UBSan, gcc -O0/-O1 cross-check) miss float-to-int conversion UB:
GCC's UBSan has no -fsanitize=float-cast-overflow. For each wrong answer we

  1. rebuild the C exactly as the primary harness does (run.build_c / run_arrays.build_c, gcc 11
     -O1 -fwrapv) and the Rust (run.build_rust / run_arrays.build_rust, rustc 1.86.0 -O), run the
     primary's own input grid, and collect EVERY input where both return a value and the values
     differ (the primary's "value" divergences, before any filter);
  2. run each diverging input through
       gcc  -O0 -fsanitize=address,undefined                          (the primary's filter)
       clang-14 -O0 and -O1 -fsanitize=address,undefined,float-cast-overflow
       clang-14 -O1 -fsanitize=memory (uninitialised reads; recorded as unavailable if MSan cannot run)
     all with -fno-sanitize-recover=all, and compare the value under gcc -O0 vs gcc -O1 (the
     primary's cross-check) and under clang-14 -O0 / -O1 (-fwrapv) with gcc's;
  3. call an input CLEAN if every sanitizer is silent, gcc -O0 == gcc -O1, and (informational)
     clang agrees with gcc. A function keeps its wrong-answer verdict on this evidence iff it has
     at least one clean diverging input.

Item 5 (implementation-defined char signedness): the WHOLE primary harness for the function
(run.test_function / run_arrays.test_one: C build, gcc sanitizer filter, -O0/-O1 filter) is
re-run with a `gcc` wrapper on PATH that appends -fsigned-char or -funsigned-char, and the verdict
compared with the default build (arm64 gcc default: unsigned char). A static scan also records which
functions mention `char` at all.

Run in crust-vert-toolchain with RUSTUP_TOOLCHAIN=1.86.0 (see toolchain_ub_run.sh).
"""
import argparse
import datetime
import json
import multiprocessing as mp
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import run as srun                     # noqa: E402
import run_arrays as arun              # noqa: E402
from enumerate_inputs import enumerate_cases   # noqa: E402
from batch_diff import eval_cases, run_batch, run_one, SAN_RE, INFRA_RE   # noqa: E402

CLANG = "clang-14"
MAX_CHECK = 160          # diverging inputs re-checked per function (smallest magnitude first)
GRID = "v1"              # scalar input grid (enumerate_inputs grid=); --grid v2 for the v2 re-test
WIDE_INT = False         # opt-in --wide-int: run.test_function(..., wide_int=True) for long/long long returns
SAN_TIMEOUT = 10


def sh(argv):
    p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return p.returncode, p.stderr.decode("utf8", "replace")


def compile_variants(cfile, d):
    """Build the extra C variants from the SAME driver source the primary compiled."""
    base = ["-w", cfile, "-lm"]
    v = {
        "gcc_san_O0": ["gcc", "-O0", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"],
        "clang_san_O0": [CLANG, "-O0", "-fsanitize=address,undefined,float-cast-overflow",
                         "-fno-sanitize-recover=all"],
        "clang_san_O1": [CLANG, "-O1", "-fsanitize=address,undefined,float-cast-overflow",
                         "-fno-sanitize-recover=all"],
        "clang_msan_O1": [CLANG, "-O1", "-fsanitize=memory", "-fno-sanitize-recover=all"],
        "gcc_O0_wrapv": ["gcc", "-O0", "-fwrapv"],
        "clang_O0_wrapv": [CLANG, "-O0", "-fwrapv"],
        "clang_O1_wrapv": [CLANG, "-O1", "-fwrapv"],
    }
    out, errs = {}, {}
    for k, cmd in v.items():
        b = os.path.join(d, "v_" + k)
        rc, err = sh(cmd + base[:1] + [cfile, "-o", b, "-lm"])
        if rc == 0 and os.path.isfile(b):
            out[k] = b
        else:
            errs[k] = err.strip().splitlines()[-1:] if err.strip() else [f"rc={rc}"]
    return out, errs


def san_check(binp, row):
    """'clean' | 'ub' | 'timeout' | 'infra' (sanitizer runtime failed to start), with message."""
    for _ in range(6):
        line, rc, to, err = run_one(binp, row, timeout=SAN_TIMEOUT)
        if to:
            return "timeout", None, None
        if INFRA_RE.search(err) or (rc < 0 and not SAN_RE.search(err)):
            continue
        if rc != 0 or SAN_RE.search(err):
            m = re.search(r"runtime error: [^\n]*|ERROR: (AddressSanitizer|MemorySanitizer): [^\n]*|"
                          r"WARNING: MemorySanitizer: [^\n]*", err)
            return "ub", (m.group(0)[:200] if m else err.strip().splitlines()[-1][:200] if err.strip() else f"rc={rc}"), line
        return "clean", None, line
    return "infra", None, None


def prepare(kind, func, d):
    if kind == "scalar":
        rust, argtypes = srun.extract_llm_rust(func)
        csrc = srun.c_source(func)
        nargs = len(argtypes)
        binc = srun.build_c(func, csrc, nargs, d)
        binr = srun.build_rust(rust, nargs, d)
        cfile = os.path.join(d, "prog_normal.c")
        cases = enumerate_cases(["i32"] * nargs, cap=80, max_abs=512, nonneg=True, legacy=False, grid=GRID)
        eq = (lambda a, b: a == b)
        is_float = srun.c_uses_float(csrc)
        return binc, binr, cfile, cases, eq, is_float, csrc
    rust, _ = arun.extract(func)
    sig = arun.parse_fgold(rust)
    comps = [arun.comp(t) for t in sig["params"]]
    csrc = open(os.path.join(arun.TC, func, f"{func}.c"), errors="replace").read()
    binc = arun.build_c(csrc, sig, comps, d)
    binr = arun.build_rust(rust, sig, comps, d)
    cfile = os.path.join(d, "p_normal.c")
    cases = arun.gen_cases(comps, 240)
    rf = sig["ret"] in arun.FLOAT_SCALARS
    eq = (lambda a, b: arun.vals_equal(a, b, rf))
    is_float = bool(re.search(r"\b(float|double)\b", arun.strip_scaffold(csrc))) or rf
    return binc, binr, cfile, cases, eq, is_float, csrc


def lines_for(binp, cases):
    """Aligned per-case output of binp (batch, with eval_cases' per-input fallback)."""
    return [c for (_, c, _) in eval_cases(binp, binp, cases)]


def ub_recheck(kind, func):
    with tempfile.TemporaryDirectory() as d:
        binc, binr, cfile, cases, eq, is_float, csrc = prepare(kind, func, d)
        if not binc or not binr:
            return {"func": func, "harness": kind, "status": "build-fail",
                    "c_built": bool(binc), "rust_built": bool(binr)}
        var, var_err = compile_variants(cfile, d)
        trip = eval_cases(binc, binr, cases)
        div = []
        for i, (row, cl, rl) in enumerate(trip):
            if cl in (None, "__PARSE__", "__TIMEOUT__") or rl in (None, "__PARSE__", "__TIMEOUT__", "__PANIC__"):
                continue
            if not eq(cl, rl):
                div.append((i, row, cl, rl))
        div.sort(key=lambda t: sum(abs(x) for x in t[1]))
        plain = {k: lines_for(var[k], cases) for k in ("gcc_O0_wrapv", "clang_O0_wrapv", "clang_O1_wrapv") if k in var}
        checked = []
        for i, row, cl, rl in div[:MAX_CHECK]:
            rec = {"in": row, "c": cl, "rust": rl}
            for k in ("gcc_san_O0", "clang_san_O0", "clang_san_O1", "clang_msan_O1"):
                if k in var:
                    st, msg, line = san_check(var[k], row)
                    rec[k] = st if msg is None else f"{st}: {msg}"
                    if st == "clean" and line is not None and not eq(line, cl):
                        rec[k + "_value"] = line          # sanitizer build returned another value
                else:
                    rec[k] = "unavailable"
            for k, ls in plain.items():
                v = ls[i] if i < len(ls) else None
                rec[k] = v
            rec["gcc_O0_eq_O1"] = (plain.get("gcc_O0_wrapv", [None] * len(cases))[i] == cl)
            rec["clang_agrees_with_gcc"] = all(
                (plain[k][i] is not None and eq(plain[k][i], cl)) for k in ("clang_O0_wrapv", "clang_O1_wrapv") if k in plain)
            msan_ok = rec["clang_msan_O1"] in ("clean", "unavailable")
            # audit 8 uniform rule: a sanitizer run that timed out / failed to start is not "clean"
            # (san_check), and one that returned another value than the C (`*_value`) disagrees
            rec["clean"] = (rec["gcc_san_O0"] == "clean" and rec["clang_san_O0"] == "clean"
                            and rec["clang_san_O1"] == "clean" and msan_ok and rec["gcc_O0_eq_O1"]
                            and not any(k.endswith("_value") for k in rec))
            checked.append(rec)
        clean = [r for r in checked if r["clean"]]
        clean_and_clang_agrees = [r for r in clean if r["clang_agrees_with_gcc"]]
        float_cast = [r for r in checked if "float-cast-overflow" in str(r.get("clang_san_O0", "")) + str(r.get("clang_san_O1", ""))
                      or "is outside the range of representable values" in str(r.get("clang_san_O0", "")) + str(r.get("clang_san_O1", ""))]
        return {
            "func": func, "harness": kind, "status": "checked", "c_uses_float": is_float,
            "grid_cases": len(cases), "value_divergences": len(div), "rechecked": len(checked),
            "clean_divergences": len(clean),
            "clean_divergences_clang_agrees": len(clean_and_clang_agrees),
            "float_cast_ub_inputs": len(float_cast),
            "gcc_san_ub_inputs": sum(1 for r in checked if str(r["gcc_san_O0"]).startswith("ub")),
            "clang_san_ub_inputs": sum(1 for r in checked if str(r["clang_san_O0"]).startswith("ub")
                                       or str(r["clang_san_O1"]).startswith("ub")),
            "msan_ub_inputs": sum(1 for r in checked if str(r["clang_msan_O1"]).startswith("ub")),
            "msan_status": ("unavailable: " + "; ".join(var_err.get("clang_msan_O1", []))) if "clang_msan_O1" not in var
                           else ("infra" if any(r["clang_msan_O1"] == "infra" for r in checked) else "ran"),
            "gcc_O0_ne_O1_inputs": sum(1 for r in checked if not r["gcc_O0_eq_O1"]),
            "clang_disagrees_inputs": sum(1 for r in checked if not r["clang_agrees_with_gcc"]),
            "variant_build_errors": var_err,
            "smallest_clean": clean[0] if clean else None,
            "smallest_clean_clang_agrees": clean_and_clang_agrees[0] if clean_and_clang_agrees else None,
            "non_clean_examples": [r for r in checked if not r["clean"]][:3],
            "all_checked": checked,
        }


def make_gcc_wrapper(root, flag):
    d = os.path.join(root, flag.lstrip("-"))
    os.makedirs(d, exist_ok=True)
    real = shutil.which("gcc")
    p = os.path.join(d, "gcc")
    with open(p, "w") as f:
        f.write(f'#!/bin/sh\nexec {real} {flag} "$@"\n')
    os.chmod(p, 0o755)
    return d


def harness_verdict(kind, func, path_prefix):
    old = os.environ["PATH"]
    if path_prefix:
        os.environ["PATH"] = path_prefix + ":" + old
    try:
        if kind == "scalar":
            r = srun.test_function(func, 80, legacy=False, nonneg=True, max_abs=512, grid=GRID,
                                   **({"wide_int": True} if WIDE_INT else {}))
            if r["status"] != "tested":
                return r["status"], r
            v = ("value_bug" if r["value_div"] > 0 else "rust_panic" if r["rust_panic_c_ok"] > 0
                 else "rust_hang" if r.get("rust_hang", 0) > 0 else "ub_only" if r["ub_div"] > 0
                 else "timeout_only" if r["timeout_div"] > 0 else "agree")
            return v, {k: r.get(k) for k in ("value_div", "ub_div", "timeout_div", "rust_hang", "value_samples")}
        r = arun.test_one(func, 240)
        if r["status"] != "tested":
            return r["status"], r
        return {"value": "value_bug", "rust_crash": "rust_panic"}.get(r["kind"], r["kind"]), \
            {k: r.get(k) for k in ("value", "ub", "timeout", "crash", "samples")}
    finally:
        os.environ["PATH"] = old


def char_check(kind, func, wrappers):
    out = {}
    for label, prefix in (("default", None), ("fsigned_char", wrappers["-fsigned-char"]),
                          ("funsigned_char", wrappers["-funsigned-char"])):
        v, detail = harness_verdict(kind, func, prefix)
        out[label] = {"verdict": v, "detail": detail}
    out["verdict_changes"] = len({out[k]["verdict"] for k in ("default", "fsigned_char", "funsigned_char")}) > 1
    return out


def job(args):
    kind, func, wrappers = args
    r = ub_recheck(kind, func)
    r["char_signedness"] = char_check(kind, func, wrappers)
    # audit 8 uniform rule: the char-signedness runs are whole-harness re-runs, so a change of the
    # verdict or of any count under -fsigned-char / -funsigned-char cannot be pinned to one input;
    # then no divergence of the function counts as UB-clean.
    cs = r["char_signedness"]
    cnt = lambda d: json.dumps({k: v for k, v in (d.get("detail") or {}).items() if "samples" not in k}, sort_keys=True)
    r["char_signedness_disagrees"] = bool(cs["verdict_changes"]) or len(
        {cnt(cs[k]) for k in ("default", "fsigned_char", "funsigned_char")}) > 1
    if r["char_signedness_disagrees"] and r.get("status") == "checked":
        r["clean_before_char_rule"] = {k: r[k] for k in ("clean_divergences", "clean_divergences_clang_agrees")}
        r["clean_divergences"] = r["clean_divergences_clang_agrees"] = 0
        r["smallest_clean"] = r["smallest_clean_clang_agrees"] = None
    c_scaf = srun.strip_scaffold(srun.c_source(func)) if kind == "scalar" else \
        arun.strip_scaffold(open(os.path.join(arun.TC, func, f"{func}.c"), errors="replace").read())
    r["c_mentions_char"] = bool(re.search(r"\bchar\b", c_scaf))
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scalar-results", default=os.path.join(REPO, "results", "vert_diff.json"))
    ap.add_argument("--array-results", default=os.path.join(REPO, "results", "vert_arrays.json"))
    ap.add_argument("--out", default=os.path.join(REPO, "results", "vert_wrong_answer_ub_recheck.json"))
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--only", default="")
    ap.add_argument("--grid", choices=("v1", "v2"), default="v1",
                    help="scalar input grid (default v1 = the primary's); v2 = enumerate_inputs grid='v2'")
    ap.add_argument("--wide-int", action="store_true",
                    help="arXiv: the char-signedness harness re-runs call run.test_function(..., wide_int=True), "
                         "for the functions whose C returns long / long long (default off)")
    ap.add_argument("--merge-crosscheck", default=None, metavar="LABEL=PATH",
                    help="do not run; embed the summary of another platform's run of this script "
                         "(e.g. x86_64=/scratch/x.json) into --out under 'crosschecks'")
    args = ap.parse_args()
    if args.merge_crosscheck:
        label, path = args.merge_crosscheck.split("=", 1)
        main_res = json.load(open(args.out))
        other = json.load(open(path))
        main_res.setdefault("crosschecks", {})[label] = {"provenance": other["provenance"],
                                                         "summary": other["summary"]}
        json.dump(main_res, open(args.out, "w"), indent=2)
        print("merged", label)
        return
    global GRID, WIDE_INT
    GRID = args.grid          # set before the fork-based Pool so workers inherit it
    WIDE_INT = args.wide_int
    assert shutil.which("clang") is None and shutil.which(CLANG), "need clang-14 but no `clang` alias"
    sc = json.load(open(args.scalar_results))["summary"]["value_bug_functions"]
    ar = json.load(open(args.array_results))["summary"]["value_bug_functions"]
    todo = [("scalar", f) for f in sc] + [("arrays", f) for f in ar]
    if args.only:
        keep = set(args.only.split(","))
        todo = [t for t in todo if t[1] in keep]
    wroot = tempfile.mkdtemp(prefix="gccwrap_")
    wrappers = {fl: make_gcc_wrapper(wroot, fl) for fl in ("-fsigned-char", "-funsigned-char")}
    t0 = datetime.datetime.now(datetime.timezone.utc)
    with mp.Pool(args.jobs) as pool:
        res = pool.map(job, [(k, f, wrappers) for k, f in todo], chunksize=1)
    t1 = datetime.datetime.now(datetime.timezone.utc)

    def ver(cmd):
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout.decode().strip()
    # gcc's default char signedness on this platform, measured
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "c.c"), "w").write('#include <stdio.h>\nint main(void){char c=(char)200;printf("%d\\n",c<0);return 0;}\n')
        subprocess.run(["gcc", os.path.join(d, "c.c"), "-o", os.path.join(d, "c")])
        default_char_signed = subprocess.run([os.path.join(d, "c")], stdout=subprocess.PIPE).stdout.decode().strip() == "1"

    no_clean = [r["func"] for r in res if r.get("status") == "checked" and r["clean_divergences"] == 0]
    no_clean_agree = [r["func"] for r in res if r.get("status") == "checked" and r["clean_divergences_clang_agrees"] == 0]
    summary = {
        "functions": len(res),
        "scalar": len(sc), "arrays": len(ar),
        "build_fail": [r["func"] for r in res if r.get("status") != "checked"],
        "with_clean_diverging_input": sum(1 for r in res if r.get("status") == "checked" and r["clean_divergences"] > 0),
        "without_clean_diverging_input": no_clean,
        "without_clean_input_where_clang_also_agrees": no_clean_agree,
        "functions_with_float_cast_ub_on_some_diverging_input": [r["func"] for r in res if r.get("float_cast_ub_inputs", 0) > 0],
        "functions_with_any_clang_san_ub": [r["func"] for r in res if r.get("clang_san_ub_inputs", 0) > 0],
        "functions_with_msan_ub": [r["func"] for r in res if r.get("msan_ub_inputs", 0) > 0],
        "functions_where_gcc_O0_ne_O1_on_some_diverging_input": [r["func"] for r in res if r.get("gcc_O0_ne_O1_inputs", 0) > 0],
        "msan_status": sorted({r.get("msan_status", "") for r in res}),
        "char_signedness_default_is_signed": default_char_signed,
        "char_signedness_verdict_changes": [r["func"] for r in res if r["char_signedness"]["verdict_changes"]],
        "functions_mentioning_char": [r["func"] for r in res if r["c_mentions_char"]],
        "per_function": {r["func"]: {
            "harness": r["harness"],
            "value_divergences": r.get("value_divergences"),
            "rechecked": r.get("rechecked"),
            "clean": r.get("clean_divergences"),
            "clean_and_clang_agrees": r.get("clean_divergences_clang_agrees"),
            "float_cast_ub": r.get("float_cast_ub_inputs"),
            "clang_san_ub": r.get("clang_san_ub_inputs"),
            "gcc_san_ub": r.get("gcc_san_ub_inputs"),
            "msan_ub": r.get("msan_ub_inputs"),
            "gcc_O0_ne_O1": r.get("gcc_O0_ne_O1_inputs"),
            "smallest_clean": ({k: r["smallest_clean"][k] for k in ("in", "c", "rust")} if r.get("smallest_clean") else None),
            "char_verdicts": {k: r["char_signedness"][k]["verdict"] for k in ("default", "fsigned_char", "funsigned_char")},
        } for r in res},
    }
    out = {
        "provenance": {
            "script": "systems/vert/toolchain_ub_recheck.py",
            "command": "RUSTUP_TOOLCHAIN=%s python3 systems/vert/toolchain_ub_recheck.py %s" % (
                os.environ.get("RUSTUP_TOOLCHAIN"), " ".join(sys.argv[1:])),
            "git_rev": os.environ.get("GIT_REV"), "git_dirty_note": os.environ.get("GIT_DIRTY"),
            "host": os.environ.get("HOST_DESC"), "arch": platform.machine(),
            "image": os.environ.get("IMAGE_DESC"),
            "toolchains": {"rustc": ver(["rustc", "-vV"]), "gcc": ver(["gcc", "--version"]).splitlines()[0],
                           "clang": ver([CLANG, "--version"]).splitlines()[0]},
            "inputs": {"scalar_wrong_answers_from": os.path.relpath(args.scalar_results, REPO),
                       "array_wrong_answers_from": os.path.relpath(args.array_results, REPO)},
            "date_start_utc": t0.isoformat(), "date_end_utc": t1.isoformat(),
            "clean_definition": "gcc -O0 ASan+UBSan clean AND clang-14 -O0 and -O1 ASan+UBSan(+float-cast-overflow) "
                                "clean AND clang-14 MSan clean (when MSan runs) AND gcc -O0 -fwrapv output == gcc -O1 "
                                "-fwrapv output (the primary's cross-check); 'clang_agrees' additionally requires "
                                "clang-14 -O0/-O1 -fwrapv to print the same value as gcc",
            "max_inputs_rechecked_per_function": MAX_CHECK,
            "scalar_grid": GRID,
            **({"wide_int": True} if WIDE_INT else {}),
        },
        "summary": summary,
        "functions": res,
    }
    json.dump(out, open(args.out, "w"), indent=2)
    print(json.dumps({k: v for k, v in summary.items() if k != "per_function"}, indent=1))


if __name__ == "__main__":
    main()
