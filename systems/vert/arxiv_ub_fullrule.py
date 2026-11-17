"""arXiv: the FULL undefined-behaviour rule applied to VERT.

Issue 7. VERT's shipped UB re-checks (toolchain_ub_recheck.py, toolchain_crash_recheck.py) are weaker
than the rule every other system uses (harness/oracle_ub_common.py): gcc runs without
float-cast-overflow, and no gcc sanitizer and not every opt level is covered. Mode `ledger` re-checks
EVERY VERT row of results/defect_ledger.json (counted and not counted) on every exposing input.

Issue 10. Mode `offscript` checks the three array/float translations whose failing inputs come from
the mutation analysis (results/vert_grid_misses.json, results/vert_arrays_mutation_primary.json),
not from the fixed input scripts.

FULL RULE, per input, on the primary harness's own C driver source (run.build_c / run_arrays.build_c,
the file the primary oracle gcc -O1 -fwrapv is compiled from). The input is UB-clean iff
  (i)   ASan + UBSan + float-cast-overflow with oracle_ub_common's flags (-g -fno-omit-frame-pointer
        -fno-sanitize-recover=all, no -fwrapv; clang non-PIE), under gcc-11 AND clang-14, each at
        -O0, -O1 AND -O2 (6 builds): no sanitizer report, the run completes (no time-out, no
        sanitizer start-up failure), exits 0 and prints the reference value;
  (ii)  gcc-11 and clang-14 at -O0, -O1 and -O2 with -fwrapv (the harness's own flags; 6 builds)
        print the reference value, exit 0, no time-out;
  (iii) gcc-11 and clang-14 -O1 -fwrapv with -fsigned-char and with -funsigned-char (4 builds)
        print the reference value, exit 0, no time-out.
Reference = the primary oracle build (gcc -O1 -fwrapv) on the same input. "Same value" is the
harness's own equality (exact for integer returns; run_arrays.vals_equal, relative 1e-6, for float
returns). This is oracle_ub_common.not_clean_reasons applied to every run: a report is UB; a failed
start, time-out or different output is "not clean".
Recorded but NOT part of the rule: clang-14 MemorySanitizer -O1 (the shipped VERT rule also required
it), clang-14 -O0 sanitizers with float-divide-by-zero added (the shipped crash rule had it), and
gcc/clang -O0/-O1/-O2 without -fwrapv.

Exposing inputs of a ledger row (mode `ledger`): (a) every input of the primary grid that shows the
row's outcome under the primary oracle -- scalar rows: grid v2 (enumerate_cases cap 80, |x| <= 512,
non-negative; results/vert_diff_grid2.json is what the ledger reads), array/float rows:
run_arrays.gen_cases(comps, 240); outcome S1 = both return a value and the values differ, crash = the
C returns a value and the Rust panics / prints nothing, hang = the C returns a value and the Rust
times out (3 s per input, batch_diff.eval_cases); plus (b) every input recorded for the row in the
shipped UB files (vert_grid2_ub_recheck.json, vert_wrong_answer_ub_recheck.json,
vert_crash_ub_recheck.json), re-run and kept only if it still shows the outcome. No cap.

  docker run --rm --cpus 4 -v $PWD:/work -v $(cd artifacts && pwd -P):/work/artifacts:ro -w /work \
      -e RUSTUP_TOOLCHAIN=1.86.0 crust-vert-toolchain \
      python3 -I systems/vert/arxiv_ub_fullrule.py ledger --jobs 4 --out results/arxiv_vert_ub_fullrule.json
  (same with `offscript --out results/arxiv_vert_offscript.json`)
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
sys.path.insert(0, os.path.join(REPO, "harness"))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import toolchain_ub_recheck as ub      # noqa: E402  (prepare: the primary's builds, cases and equality)
import run_arrays as arun              # noqa: E402
import oracle_ub_common as oub         # noqa: E402
from batch_diff import eval_cases      # noqa: E402

CLANG = "clang-14"
OPTS = ("-O0", "-O1", "-O2")
SAN_TIMEOUT = 30
VAL_TIMEOUT = 15
LEDGER = os.path.join(REPO, "results", "defect_ledger.json")
REC_SCALAR = os.path.join(REPO, "results", "vert_grid2_ub_recheck.json")
# arXiv (issues.md 47): shipped-style recheck of the scalar functions whose C returns long / long long; read only if present
REC_SCALAR_WIDE = os.path.join(REPO, "results", "arxiv_vert_wide_ub_recheck.json")
REC_ARRAYS = os.path.join(REPO, "results", "vert_wrong_answer_ub_recheck.json")
REC_CRASH = os.path.join(REPO, "results", "vert_crash_ub_recheck.json")
GRID_MISSES = os.path.join(REPO, "results", "vert_grid_misses.json")
ARR_MUT = os.path.join(REPO, "results", "vert_arrays_mutation_primary.json")
OFFSCRIPT = ["FIND_WHETHER_AN_ARRAY_IS_SUBSET_OF_ANOTHER_ARRAY_SET_1",
             "FIND_HARMONIC_MEAN_USING_ARITHMETIC_MEAN_GEOMETRIC_MEAN",
             "PAIR_WITH_GIVEN_PRODUCT_SET_1_FIND_IF_ANY_PAIR_EXISTS"]
# One sentence per off-script defect, read from the sources; each location is checked by its pattern
# (file, pattern) at run time and the matching line number recorded. The example input is the
# smallest witness of results/vert_grid_misses.json, re-run here.
OFFSCRIPT_DEFECTS = {
    "FIND_WHETHER_AN_ARRAY_IS_SUBSET_OF_ANOTHER_ARRAY_SET_1": dict(
        example=[0] * 10 + [1] + [0] * 9 + [1, 1],
        example_call="f_gold(arr1 = [0,0,0,0,0,0,0,0,0,0], arr2 = [1,0,0,0,0,0,0,0,0,0], m = 1, n = 1)",
        defect=("On f_gold(arr1 = ten 0s, arr2 = [1, 0, ..., 0], m = 1, n = 1) the C returns 0 and the Rust returns 1: "
                "the Rust's inner `for j in 0..m` binds a new loop-scoped j that shadows the outer `let mut j = 0`, so "
                "the `if j == m { return 0; }` after the loop tests the outer j, which is always 0, and the "
                "element-not-found exit fires only when m == 0 (the C uses one j for the loop and the test)."),
        c=(r"if \( j == m \) return 0;", r"for \( j = 0;"), rust=(r"for j in 0\.\.m", r"if j == m")),
    "FIND_HARMONIC_MEAN_USING_ARITHMETIC_MEAN_GEOMETRIC_MEAN": dict(
        example=[1, 2], example_call="f_gold(1, 2)",
        defect=("On f_gold(1, 2) the C returns 2 and the Rust returns 1.333333254: the C computes AM = (a + b) / 2 "
                "in int (integer division, AM = 1) before assigning it to a double, while the Rust computes "
                "(a as f32 + b as f32) / 2.0 in floating point (AM = 1.5), so HM = GM*GM/AM differs whenever a + b is odd."),
        c=(r"AM = \( a \+ b \) / 2;",), rust=(r"let am = \(a as f32 \+ b as f32\) / 2\.0;",)),
    "PAIR_WITH_GIVEN_PRODUCT_SET_1_FIND_IF_ANY_PAIR_EXISTS": dict(
        example=[0, 1, 2, 1], example_call="f_gold(arr = [0, 1], n = 2, x = 1)",
        defect=("On f_gold(arr = [0, 1], n = 2, x = 1) the C returns 1 and the Rust returns 0: the C's inner loop "
                "`for (int j = i + 1; i < n; i ++)` tests and increments i, not j, so it also compares "
                "arr[1] * arr[1] = 1, while the Rust iterates only the proper pairs j = i+1..n-1 "
                "(arr[0] * arr[1] = 0); here the C is the non-standard program and the Rust the textbook algorithm."),
        c=(r"i \+\+ \) for \( int j = i \+ 1;", r"i \+\+ \) if \( arr \[ i \] \* arr \[ j \] == x \) return true;"),
        rust=(r"let mut j = i \+ 1;", r"while j < n")),
}


def find_line(path, pat):
    rx = re.compile(pat)
    for i, line in enumerate(open(path, errors="replace"), 1):
        if rx.search(line):
            return {"file": os.path.relpath(path, REPO), "line": i, "text": line.strip()}
    return {"file": os.path.relpath(path, REPO), "line": None, "pattern": pat}


def _o(flags, o):
    return [o if f == "-O0" else f for f in flags]


def plan():
    """name -> (compiler, flags, role); role in {san, val, char, info_san, info_val}."""
    p = {}
    for cc, tag in (("gcc", "gcc"), (CLANG, "clang")):
        for o in OPTS:
            p[f"san_{tag}{o}"] = (cc, _o(oub.san_cflags(tag), o) + oub.san_ldflags(tag), "san")
        for o in OPTS:
            p[f"val_{tag}{o}_wrapv"] = (cc, [o, "-fwrapv"], "val")
        p[f"char_{tag}-O1_wrapv_signed"] = (cc, ["-O1", "-fwrapv", "-fsigned-char"], "char")
        p[f"char_{tag}-O1_wrapv_unsigned"] = (cc, ["-O1", "-fwrapv", "-funsigned-char"], "char")
        for o in OPTS:
            p[f"info_{tag}{o}_nowrapv"] = (cc, [o], "info_val")
    p["info_clang_msan-O1"] = (CLANG, ["-O1", "-g", "-fsanitize=memory", "-fno-sanitize-recover=all"], "info_san")
    fdz = [f.replace("float-cast-overflow", "float-cast-overflow,float-divide-by-zero") for f in oub.san_cflags("clang")]
    fdz_ld = [f.replace("float-cast-overflow", "float-cast-overflow,float-divide-by-zero") for f in oub.san_ldflags("clang")]
    p["info_clang_san_fdz-O0"] = (CLANG, fdz + fdz_ld, "info_san")
    return p


PLAN = plan()
RULE_CHECKS = [k for k, v in PLAN.items() if v[2] in ("san", "val", "char")]


def build_variants(cfile, d):
    out, errs = {}, {}
    for k, (cc, flags, _) in PLAN.items():
        b = os.path.join(d, "x_" + re.sub(r"[^A-Za-z0-9_]", "_", k))
        p = subprocess.run([cc] + flags + ["-w", cfile, "-o", b, "-lm"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if p.returncode == 0 and os.path.isfile(b):
            out[k] = b
        else:
            errs[k] = (p.stdout.decode("utf8", "replace").strip().splitlines() or [f"rc={p.returncode}"])[-1][:300]
    return out, errs


def first_line(b):
    s = (b or b"").decode("latin1").splitlines()
    return s[0] if s else None


def check_input(var, row, ref, eq):
    """Apply the full rule to one input. Returns (clean, fails{check: reason}, info{check: status})."""
    stdin = (" ".join(str(x) for x in row) + "\n").encode()
    fails, info = {}, {}
    for k, (cc, flags, role) in PLAN.items():
        if k not in var:
            if role in ("san", "val", "char"):
                fails[k] = "build failed"
            else:
                info[k] = "unavailable (build failed)"
            continue
        if role in ("san", "info_san"):
            r = oub.run_san([var[k]], stdin, timeout=SAN_TIMEOUT)
            msan = re.search(rb"WARNING: MemorySanitizer: [^\n]*", r["stderr"] or b"")
            if r["report"]:
                st = "ub: " + r["report"]
            elif msan and not r["infra"]:      # oracle_ub_common.SAN_RE does not match MSan's report line
                st = "ub: " + msan.group(0).decode("utf8", "replace")[:200]
            elif r["infra"]:
                st = "infra: sanitizer runtime did not start"
            elif r["timed_out"]:
                st = "timeout"
            else:
                v = first_line(r["stdout"])
                st = "clean" if (r["rc"] == 0 and v is not None and eq(v, ref)) else f"differs: rc={r['rc']} value={v}"
        else:
            out, err, rc, to = oub.run_proc([var[k]], stdin, timeout=VAL_TIMEOUT)
            v = first_line(out)
            st = ("timeout" if to else "same" if (rc == 0 and v is not None and eq(v, ref))
                  else f"differs: rc={rc} value={v}")
        if role in ("san", "val", "char"):
            if st not in ("clean", "same"):
                fails[k] = st
        else:
            info[k] = st
    return not fails, fails, info


def valid(x):
    return x is not None and x not in ("__PARSE__", "__TIMEOUT__", "__PANIC__")


def outcome(sev, kind, cl, rl, eq):
    if not valid(cl):
        return False
    if sev == "S1":
        return valid(rl) and not eq(cl, rl)
    if sev == "crash":
        return rl is None or rl == "__PANIC__" or (kind == "arrays" and rl == "__PARSE__")
    if sev == "hang":
        return rl == "__TIMEOUT__"
    return False


def l1(row):
    return sum(abs(x) for x in row)


def reason_kind(s):
    """A failure reason without input-specific numbers, addresses, pids or paths (for counting)."""
    if s.startswith("ub:"):
        m = re.search(r"(AddressSanitizer|MemorySanitizer): ([\w-]+)", s)
        if m and "runtime error:" not in s:
            return f"ub: {m.group(1)} {m.group(2)}"
        t = s.split("runtime error:")[-1].strip()
        t = re.sub(r"-?\d+(\.\d+)?(e[+-]?\d+)?", "N", t)
        return "ub: " + t[:90]
    return s.split(":")[0]


def summarize(recs):
    clean = [r for r in recs if r["clean"]]
    fb = {}
    for r in recs:
        for k in r["fails"]:
            fb[k] = fb.get(k, 0) + 1
    by_reason = {}
    for r in recs:
        for k, s in r["fails"].items():
            kind = reason_kind(s)
            by_reason.setdefault(k, {})
            by_reason[k][kind] = by_reason[k].get(kind, 0) + 1
    msan = {}
    for r in recs:
        s = r["info"].get("info_clang_msan-O1", "?")
        s = s.split(":")[0] if not s.startswith("clean") else "clean"
        msan[s] = msan.get(s, 0) + 1
    clean_msan = [r for r in clean if r["info"].get("info_clang_msan-O1") == "clean"]
    clean_all_info = [r for r in clean if all(v in ("clean", "same") for v in r["info"].values())]
    sm = min(clean, key=lambda r: (l1(r["in"]), r["in"])) if clean else None
    return {
        "exposing_inputs_distinct": len({json.dumps(r["in"]) for r in recs}),
        "clean_full_rule": len(clean),
        "clean_full_rule_distinct": len({json.dumps(r["in"]) for r in clean}),
        "clean_full_rule_and_msan_clean": len(clean_msan),
        "clean_full_rule_and_every_recorded_extra_check": len(clean_all_info),
        "failing_by_check": dict(sorted(fb.items())),
        "failing_by_check_and_reason": by_reason,
        "msan_status_counts": msan,
        "smallest_clean": ({"in": sm["in"], "c": sm["c"], "rust": sm["rust"]} if sm else None),
    }


def ledger_job(args):
    kind, func, rows_spec = args
    t0 = datetime.datetime.now(datetime.timezone.utc)
    with tempfile.TemporaryDirectory() as d:
        binc, binr, cfile, cases, eq, is_float, _ = ub.prepare(kind, func, d)
        if not binc or not binr:
            return {"func": func, "harness": kind, "status": "build-fail", "c_built": bool(binc), "rust_built": bool(binr)}
        var, errs = build_variants(cfile, d)
        trip = eval_cases(binc, binr, cases)
        out_rows = []
        for spec in rows_spec:
            sev = spec["severity"]
            grid_exp = [(row, cl, rl) for row, cl, rl in trip if outcome(sev, kind, cl, rl, eq)]
            seen = {json.dumps(r) for r, _, _ in grid_exp}
            extra = [r for r in spec["recorded"] if json.dumps(r) not in seen]
            extra_trip = eval_cases(binc, binr, extra) if extra else []
            extra_exp = [(row, cl, rl) for row, cl, rl in extra_trip if outcome(sev, kind, cl, rl, eq)]
            extra_not = [{"in": row, "c": cl, "rust": rl} for row, cl, rl in extra_trip if not outcome(sev, kind, cl, rl, eq)]
            rec_set = {json.dumps(r) for r in spec["recorded"]}
            recs = []
            for row, cl, rl in grid_exp + extra_exp:
                clean, fails, info = check_input(var, row, cl, eq)
                recs.append({"in": row, "c": cl, "rust": rl, "on_primary_grid": json.dumps(row) not in {json.dumps(r) for r in extra},
                             "recorded_in_shipped_file": json.dumps(row) in rec_set,
                             "clean": clean, "fails": fails, "info": info})
            recs.sort(key=lambda r: (l1(r["in"]), r["in"]))
            s = summarize(recs)
            out_rows.append({
                "defect_id": spec["defect_id"], "function": func, "harness": kind, "severity": sev,
                "ledger_count_status": spec["count_status"], "ledger_ub_rule": spec["ledger_ub_rule"],
                "exposing_inputs": len(recs),
                "exposing_on_primary_grid": len(grid_exp),
                "recorded_inputs_in_shipped_file": len(spec["recorded"]),
                "recorded_source": spec["recorded_source"],
                "recorded_not_on_primary_grid_still_exposing": len(extra_exp),
                "recorded_no_longer_exposing": extra_not,
                **s,
                "has_ub_clean_exposing_input": s["clean_full_rule"] > 0,
                "inputs": recs,
            })
    return {"func": func, "harness": kind, "status": "checked", "grid_cases": len(cases), "c_uses_float": is_float,
            "variant_build_errors": errs, "rows": out_rows,
            "secs": round((datetime.datetime.now(datetime.timezone.utc) - t0).total_seconds(), 1)}


def offscript_job(args):
    func, inputs = args
    with tempfile.TemporaryDirectory() as d:
        binc, binr, cfile, cases, eq, is_float, csrc = ub.prepare("arrays", func, d)
        if not binc or not binr:
            return {"func": func, "status": "build-fail"}
        var, errs = build_variants(cfile, d)
        rust, _ = arun.extract(func)
        sig = arun.parse_fgold(rust)
        comps = [arun.comp(t) for t in sig["params"]]
        rf = sig["ret"] in arun.FLOAT_SCALARS
        csrc_full = open(os.path.join(arun.TC, func, f"{func}.c"), errors="replace").read()
        binc_san = arun.build_c(csrc_full, sig, comps, d, mode="san")
        # as run_arrays.test_one: the -O0 cross-check build only when the C has no float/double
        uses_fp = bool(re.search(r"\b(float|double)\b", arun.strip_scaffold(csrc_full)))
        binc_alt = None if uses_fp else arun.build_c(csrc_full, sig, comps, d, mode="alt")
        rows = [x["in"] for x in inputs]
        trip = eval_cases(binc, binr, rows)
        recs = []
        for (row, cl, rl), src in zip(trip, inputs):
            clean, fails, info = check_input(var, row, cl, eq)
            # the primary harness's own classifier (run_arrays.classify) on this one input
            cls = arun.classify([row], arun.run_batch(binc, [row]), arun.run_batch(binr, [row]), rf, binc_san, binc_alt)
            verdict = ("value" if cls["value"] else "crash" if cls["crash"] else "ub" if cls["ub"]
                       else "timeout" if cls["timeout"] else "hang" if cls["rust_hang"] else
                       "san_unresolved" if cls["san_unresolved"] else "agree")
            recs.append({"in": row, "source": src["source"], "c": cl, "rust": rl,
                         "rust_differs": valid(cl) and valid(rl) and not eq(cl, rl),
                         "primary_classifier_verdict": verdict,
                         "clean": clean, "fails": fails, "info": info})
        cases_grid = cases
        grid_trip = eval_cases(binc, binr, cases_grid)
        grid_div = [r for r, c, x in grid_trip if valid(c) and valid(x) and not eq(c, x)]
        dd = OFFSCRIPT_DEFECTS[func]
        cpath = os.path.join(arun.TC, func, f"{func}.c")
        rpath = os.path.join(arun.TC, func, "out-rwasm-bolero", "src", "main.rs")
        ex = next((r for r in recs if r["in"] == dd["example"]), None)
        defect = {"one_sentence": dd["defect"], "example_call": dd["example_call"], "example_input_row": dd["example"],
                  "example_rerun": ({k: ex[k] for k in ("c", "rust", "rust_differs", "primary_classifier_verdict", "clean")} if ex else None),
                  "c_location": [find_line(cpath, p) for p in dd["c"]],
                  "rust_location": [find_line(rpath, p) for p in dd["rust"]]}
        return {"func": func, "status": "checked", "params": sig["params"], "ret": sig["ret"], "defect": defect,
                "fixed_script_grid_cases": len(cases_grid), "fixed_script_grid_value_divergences": len(grid_div),
                "variant_build_errors": errs, "inputs": recs,
                "summary": {"inputs": len(recs), "rust_differs": sum(r["rust_differs"] for r in recs),
                            "ub_clean_full_rule": sum(r["clean"] for r in recs),
                            "differs_and_ub_clean": sum(r["clean"] and r["rust_differs"] for r in recs),
                            "differs_and_ub_clean_and_msan_clean": sum(r["clean"] and r["rust_differs"] and r["info"].get("info_clang_msan-O1") == "clean" for r in recs)}}


def positive_controls():
    """Each sanitizer configuration of the rule on oracle_ub_common.CONTROLS (one UB each; 'clean' none)."""
    res = {}
    with tempfile.TemporaryDirectory() as d:
        for k, (cc, flags, role) in PLAN.items():
            if role not in ("san", "info_san") or "msan" in k:
                continue
            r = {}
            for name, (src, argv) in oub.CONTROLS.items():
                c = os.path.join(d, name + ".c")
                open(c, "w").write(src)
                b = os.path.join(d, name + "_" + re.sub(r"[^A-Za-z0-9_]", "_", k))
                p = subprocess.run([cc] + flags + [c, "-o", b], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                if p.returncode:
                    r[name] = "build_fail"
                    continue
                rr = oub.run_san([b] + argv)
                r[name] = "fired" if rr["report"] else ("infra" if rr["infra"] else "clean")
            r["all_as_expected"] = all(v == ("clean" if n == "clean" else "fired") for n, v in r.items())
            res[k] = r
        # MSan: does the runtime start, and does it fire on an uninitialised read?
        c = os.path.join(d, "msan.c")
        open(c, "w").write("#include <stdlib.h>\nint main(int c,char**v){int a[4];a[0]=atoi(v[1]);"
                           "if(a[atoi(v[1])])return 1;return 0;}\n")
        b = os.path.join(d, "msan")
        cc, flags, _ = PLAN["info_clang_msan-O1"]
        p = subprocess.run([cc] + flags + [c, "-o", b], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if p.returncode:
            res["info_clang_msan-O1"] = {"uninit_read": "build_fail"}
        else:
            e = dict(os.environ)
            fire = oub.run_san([b, "2"])
            ok = oub.run_san([b, "0"])
            res["info_clang_msan-O1"] = {
                "uninit_read": "fired" if (fire["rc"] not in (0,) and b"MemorySanitizer" in fire["stderr"]) else ("infra" if fire["infra"] else "clean"),
                "clean": "fired" if b"MemorySanitizer" in ok["stderr"] else ("infra" if ok["infra"] else "clean")}
    return res


def tool(cmd):
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True).stdout.strip().splitlines()[0]
    except (OSError, IndexError):
        return "ABSENT"


def provenance_block(mode, args, t0):
    import provenance as prov          # scripts/provenance.py (stamp only; never --index)
    rule = __doc__.split("FULL RULE")[1].split("Exposing inputs")[0].strip()
    return {
        "script": "systems/vert/arxiv_ub_fullrule.py", "mode": mode,
        "command": os.environ.get("RUN_COMMAND") or ("python3 -I systems/vert/arxiv_ub_fullrule.py " + " ".join(sys.argv[1:])),
        "git_rev": os.environ.get("GIT_REV") or prov.stamp().get("git_commit"),
        "git_dirty_note": os.environ.get("GIT_DIRTY"),
        "host": os.environ.get("HOST_DESC"), "arch": platform.machine(), "kernel": platform.release(),
        "image": os.environ.get("IMAGE_DESC"),
        "toolchains": {"rustc": tool(["rustc", "--version"]), "gcc": tool(["gcc", "--version"]),
                       "clang": tool([CLANG, "--version"]), "python": platform.python_version()},
        "builds": {k: [v[0]] + v[1] + ["-w", "<driver.c>", "-o", "<bin>", "-lm"] for k, v in PLAN.items()},
        "rule_checks": RULE_CHECKS,
        "recorded_not_rule": [k for k in PLAN if k not in RULE_CHECKS],
        "sanitizer_env": oub.SAN_ENV, "san_timeout_s": SAN_TIMEOUT, "value_timeout_s": VAL_TIMEOUT,
        "full_rule": rule,
        "date_start_utc": t0.isoformat(timespec="seconds"),
        "date_end_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }


def ledger_specs():
    L = json.load(open(LEDGER))
    rs = json.load(open(REC_SCALAR))
    ra = json.load(open(REC_ARRAYS))
    rc = json.load(open(REC_CRASH))
    sc_rec = {f["func"]: f for f in rs["functions"]}
    sc_src = {f: "results/vert_grid2_ub_recheck.json" for f in sc_rec}
    if os.path.isfile(REC_SCALAR_WIDE):
        for f in json.load(open(REC_SCALAR_WIDE))["functions"]:
            if f["func"] not in sc_rec:
                sc_rec[f["func"]], sc_src[f["func"]] = f, "results/arxiv_vert_wide_ub_recheck.json"
    ar_rec = {f["func"]: f for f in ra["functions"] if f["harness"] == "arrays"}
    cr_rec = {f["defect_id"]: f for f in rc["functions"]}
    by_func = {}
    for r in L["rows"]:
        if r["system"] != "VERT":
            continue
        func = r["translation"].split()[0]
        kind = "scalar" if r["exposure_breakdown"]["family"] == "scalar" else "arrays"
        sev = r["severity"]
        if sev == "crash":
            x = cr_rec.get(r["defect_id"])
            src = "results/vert_crash_ub_recheck.json" if x else None
        elif sev == "S1":
            x = (sc_rec if kind == "scalar" else ar_rec).get(func)
            src = (sc_src[func] if kind == "scalar" else "results/vert_wrong_answer_ub_recheck.json") if x else None
        else:
            x, src = None, None
        recorded = [c["in"] for c in (x or {}).get("all_checked", [])]
        u = r.get("ub_rule") or {}
        by_func.setdefault((kind, func), []).append({
            "defect_id": r["defect_id"], "severity": sev, "count_status": r["count_status"],
            "ledger_ub_rule": {"status": u.get("status"), "clean_inputs": u.get("clean_inputs"), "of": u.get("of")},
            "recorded": recorded, "recorded_source": src})
    return by_func


def offscript_specs():
    gm = {f["func"]: f for f in json.load(open(GRID_MISSES))["functions"]}
    mut = {f["func"]: f for f in json.load(open(ARR_MUT))["per_function"]}
    specs = []
    for func in OFFSCRIPT:
        inputs, seen = [], set()

        def add(row, source):
            if row is not None and json.dumps(row) not in seen:
                seen.add(json.dumps(row))
                inputs.append({"in": row, "source": source})
        w = (mut.get(func, {}).get("original_extended_domain") or {}).get("witness") or {}
        add(w.get("in"), "results/vert_arrays_mutation_primary.json per_function[].original_extended_domain.witness")
        g = gm.get(func, {})
        add((g.get("smallest_passing_witness") or {}).get("in"), "results/vert_grid_misses.json smallest_passing_witness")
        for c in g.get("failed_checks_before_witness", []):
            add(c["in"], "results/vert_grid_misses.json failed_checks_before_witness")
        specs.append((func, inputs))
    return specs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["ledger", "offscript"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--only", default="", help="comma-separated function names (testing)")
    a = ap.parse_args()
    assert shutil.which("clang") is None and shutil.which(CLANG), "need clang-14 and no `clang` alias (as the primary)"
    ub.GRID = "v2"            # scalar rows: the grid the ledger reads (results/vert_diff_grid2.json); set before forking
    t0 = datetime.datetime.now(datetime.timezone.utc)
    controls = positive_controls()
    if a.mode == "ledger":
        specs = ledger_specs()
        jobs = [(k, f, rows) for (k, f), rows in specs.items() if not a.only or f in a.only.split(",")]
        jobs.sort(key=lambda j: -sum(len(r["recorded"]) + 100 for r in j[2]))    # long jobs first
        res = []
        with mp.Pool(a.jobs) as pool:
            for r in pool.imap_unordered(ledger_job, jobs, chunksize=1):
                res.append(r)
                print(f"[{len(res)}/{len(jobs)}] {r['func']} {r.get('secs')}s", flush=True)
        res.sort(key=lambda r: r["func"])
        rows = [x for r in res for x in r.get("rows", [])]
        rows.sort(key=lambda x: x["defect_id"])
        counted = [x for x in rows if x["ledger_count_status"] == "counted"]
        summary = {
            "vert_rows_checked": len(rows),
            "functions": len(res),
            "build_fail": [r["func"] for r in res if r.get("status") != "checked"],
            "ledger_counted_rows": len(counted),
            "counted_rows_with_ub_clean_exposing_input": sum(1 for x in counted if x["has_ub_clean_exposing_input"]),
            "counted_rows_without_ub_clean_exposing_input": [x["defect_id"] for x in counted if not x["has_ub_clean_exposing_input"]],
            "counted_rows_without_input_clean_under_rule_and_msan": [x["defect_id"] for x in counted if x["clean_full_rule_and_msan_clean"] == 0],
            "not_counted_rows": {x["defect_id"]: {"ledger_count_status": x["ledger_count_status"],
                                                  "exposing_inputs": x["exposing_inputs"], "clean_full_rule": x["clean_full_rule"]}
                                 for x in rows if x["ledger_count_status"] != "counted"},
            "exposing_inputs_total": sum(x["exposing_inputs"] for x in rows),
            "per_row": {x["defect_id"]: {
                "severity": x["severity"], "ledger_count_status": x["ledger_count_status"],
                "ledger_ub_rule": x["ledger_ub_rule"],
                "exposing_inputs": x["exposing_inputs"], "clean_full_rule": x["clean_full_rule"],
                "clean_full_rule_and_msan_clean": x["clean_full_rule_and_msan_clean"],
                "has_ub_clean_exposing_input": x["has_ub_clean_exposing_input"],
                "failing_by_check": x["failing_by_check"], "smallest_clean": x["smallest_clean"],
                "recorded_inputs_in_shipped_file": x["recorded_inputs_in_shipped_file"],
                "recorded_no_longer_exposing": len(x["recorded_no_longer_exposing"]),
            } for x in rows},
        }
        out = {"provenance": provenance_block("ledger", a, t0), "sanitizer_positive_controls": controls,
               "summary": summary, "functions": [{k: v for k, v in r.items() if k != "rows"} for r in res], "rows": rows}
    else:
        specs = offscript_specs()
        with mp.Pool(min(a.jobs, len(specs))) as pool:
            res = pool.map(offscript_job, specs, chunksize=1)
        out = {"provenance": provenance_block("offscript", a, t0), "sanitizer_positive_controls": controls,
               "summary": {r["func"]: {**(r.get("summary") or {}), "defect": (r.get("defect") or {}).get("one_sentence"),
                                       "example_rerun": (r.get("defect") or {}).get("example_rerun")} for r in res},
               "note": ("Not candidates in results/defect_ledger.json: the failing inputs come from the mutation analysis's "
                        "extended-domain search, not from the fixed input scripts (fixed_script_grid_value_divergences = 0 "
                        "for all three). For an appendix of defects found outside the fixed scripts; not counted."),
               "functions": res}
    try:
        import provenance as prov
        out["_provenance"] = prov.stamp(image=os.environ.get("IMAGE_DESC"), step=f"arxiv_ub_fullrule {a.mode}")
    except Exception as e:      # noqa: BLE001
        out["_provenance"] = {"error": str(e)}
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
        f.write("\n")
    print(json.dumps(out["summary"] if a.mode == "offscript" else {k: v for k, v in out["summary"].items() if k != "per_row"}, indent=1))
    print(json.dumps({k: v.get("all_as_expected", v) for k, v in controls.items()}))


if __name__ == "__main__":
    main()
