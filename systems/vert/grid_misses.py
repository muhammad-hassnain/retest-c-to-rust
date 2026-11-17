"""Grid misses: VERT translations the harness calls "agree" that are wrong on an in-domain input.

systems/vert/mutate_primary.py (equivalence phase, results/vert_mutation_primary.json and
results/vert_arrays_mutation_primary.json) found five such functions: the unmutated translation
differs from the original C on an input inside the harness's own domain that the harness's input
grid never generates. Before these are cited as evidence that the defect counts are lower bounds,
each needs a witness that passes the FULL undefined-behaviour rule used for every other wrong
answer (harness/oracle_ub_common.py, systems/vert/toolchain_ub_recheck.py):

  * clang-14 ASan + UBSan + float-cast-overflow, -O0 and -O1 (oracle_ub_common flags, -fno-sanitize-recover=all)
  * gcc-11   ASan + UBSan + float-cast-overflow, -O0 (same flags)
  * clang-14 MemorySanitizer -O1 (recorded; required clean when it runs)
  * the value agrees across gcc and clang at -O0, -O1, -O2, each with and without -fwrapv
  * the value agrees under -fsigned-char and -funsigned-char (gcc and clang, -O1 -fwrapv)
  * and the input really diverges under the primary harness's own classifier (batch_diff.tally for
    scalars, run_arrays.classify for array/float functions).

Search (deterministic): the function's in-domain set -- scalars: [0,512]^k (run.py's domain);
array/float functions: arrays with elements in [-40,40] and int scalars in [1,N] (run_arrays.py's
own scalar range) -- enumerated in increasing L1 norm (sum |x_i|), ties in generation order
(coordinates left to right, values by |v| then sign). Inputs where the primary oracle (gcc -O1
-fwrapv) and the Rust differ are checked in that order; the first that passes every check is the
SMALLEST passing witness. Up to MAX_CHECKS diverging inputs are checked per function.

  docker run --rm --cpus 3 -v $PWD:/work -w /work -e RUSTUP_TOOLCHAIN=1.86.0 crust-vert-toolchain \
      python3 systems/vert/grid_misses.py --jobs 3
"""
import argparse
import datetime
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "harness"))
import run as srun                      # noqa: E402
import run_arrays as arun               # noqa: E402
from batch_diff import tally            # noqa: E402
import oracle_ub_common as oub          # noqa: E402

CLANG = "clang-14"
MAX_CHECKS = 40
TC = os.path.join(REPO, "artifacts", "vert", "benchmark", "c_transcoder")

FUNCS = {
    "SUM_SERIES_555555_N_TERMS": dict(
        kind="scalar",
        c_pat=r"pow \( 10, n \)", rs_pat=r"10_f32\.powf",
        mechanism="C computes 0.6172*(pow(10,n)-1) - 0.55*n in double and converts the in-range result "
                  "to int on return; the Rust evaluates the same formula in f32 (10_f32.powf), whose 24-bit "
                  "significand cannot represent the intermediate values once they exceed 2^24, so the "
                  "truncated result differs.",
        source="results/vert_mutation_primary.json per_function[].original_extended_domain"),
    "NUMBER_WAYS_NODE_MAKE_LOOP_SIZE_K_UNDIRECTED_COMPLETE_CONNECTED_GRAPH_N_NODES": dict(
        kind="scalar",
        c_pat=r"pow \( n - 1, k \)", rs_pat=r"\(n - 1\.0\)\.powf\(k\)",
        mechanism="C computes (pow(n-1,k) + p*(n-1)) / n in double; the Rust computes it in f32 "
                  "((n - 1.0).powf(k) ... / n), which rounds (n-1)^k once it exceeds 2^24, so the "
                  "truncated quotient differs.",
        source="results/vert_mutation_primary.json per_function[].original_extended_domain"),
    "FIND_WHETHER_AN_ARRAY_IS_SUBSET_OF_ANOTHER_ARRAY_SET_1": dict(
        kind="arrays",
        c_pat=r"if \( j == m \) return 0;", rs_pat=r"for j in 0\.\.m",
        rs_pat2=r"if j == m",
        mechanism="The Rust declares `let mut j = 0;` and then loops with `for j in 0..m`, which binds a NEW "
                  "loop-scoped j that shadows the outer one; after the inner loop `if j == m` tests the "
                  "outer j, which is always 0, so the 'element not found' exit (return 0) fires only when "
                  "m == 0. The C uses one j for the loop and the test. The harness's array patterns are "
                  "generated in lockstep, so arr2 equals arr1 on every row and the exit is never needed.",
        source="results/vert_arrays_mutation_primary.json per_function[].original_extended_domain"),
    "FIND_HARMONIC_MEAN_USING_ARITHMETIC_MEAN_GEOMETRIC_MEAN": dict(
        kind="arrays",
        c_pat=r"AM = \( a \+ b \) / 2;", rs_pat=r"let am = \(a as f32 \+ b as f32\) / 2\.0;",
        mechanism="C computes AM = (a + b) / 2 with int a, b: integer division, then assigns it to a "
                  "double; the Rust computes (a as f32 + b as f32) / 2.0, a float division. They differ "
                  "whenever a + b is odd. The harness's 3 rows set a == b, so a + b is always even.",
        source="results/vert_arrays_mutation_primary.json per_function[].original_extended_domain"),
    "PAIR_WITH_GIVEN_PRODUCT_SET_1_FIND_IF_ANY_PAIR_EXISTS": dict(
        kind="arrays",
        c_pat=r"i \+\+ \) if \( arr \[ i \] \* arr \[ j \] == x \) return true;", rs_pat=r"let mut j = i \+ 1;",
        c_pat2=r"i \+\+ \) for \( int j = i \+ 1;",
        mechanism="The C's inner loop is `for (int j = i + 1; i < n; i ++)`: it tests and increments i, "
                  "not j, so it compares arr[i]*arr[j0] for i = i0..n-1 with j0 = i0+1 fixed, which "
                  "includes arr[1]*arr[1]. The Rust iterates the proper pairs j = i+1..n-1. Here the C "
                  "(the oracle) is the non-standard program and the Rust is the textbook algorithm; it is "
                  "counted only because the original C is the oracle.",
        c_is_nonstandard=True,
        source="results/vert_arrays_mutation_primary.json per_function[].original_extended_domain"),
}


def find_line(path, pat):
    rx = re.compile(pat)
    for i, l in enumerate(open(path, errors="replace"), 1):
        if rx.search(l):
            return {"file": os.path.relpath(path, REPO), "line": i, "text": l.strip()}
    return {"file": os.path.relpath(path, REPO), "line": None, "text": None, "pattern": pat}


def l1_vectors(bounds, s):
    """All integer vectors v with lo_i <= v_i <= hi_i and sum |v_i| == s, deterministic order."""
    n = len(bounds)

    def vals(lo, hi, budget, exact):
        cand = [0] + [x for k in range(1, budget + 1) for x in (k, -k)]
        out = [v for v in cand if lo <= v <= hi and abs(v) <= budget]
        return [v for v in out if abs(v) == budget] if exact else out

    def rec(i, budget):
        lo, hi = bounds[i]
        if i == n - 1:
            for v in vals(lo, hi, budget, True):
                yield (v,)
            return
        for v in vals(lo, hi, budget, False):
            for rest in rec(i + 1, budget - abs(v)):
                yield (v,) + rest
    yield from rec(0, s)


def build(cc, flags, cfile, out):
    p = subprocess.run([cc] + flags + ["-w", cfile, "-o", out, "-lm"], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT)
    return out if p.returncode == 0 and os.path.isfile(out) else ("BUILD_FAIL: " + p.stdout.decode()[-300:])


def run_in(binp, row, timeout=20):
    out, err, rc, to = oub.run_proc([binp], (" ".join(str(x) for x in row) + "\n").encode(), timeout=timeout)
    lines = out.decode("latin1").splitlines()
    return (lines[0] if lines else None), rc, to


def prepare(func, spec, d):
    if spec["kind"] == "scalar":
        rust, argtypes = srun.extract_llm_rust(func)
        csrc = srun.c_source(func)
        nargs = len(argtypes)
        binc = srun.build_c(func, csrc, nargs, d)
        binr = srun.build_rust(rust, nargs, d)
        orc = dict(binc=binc, binc_san=srun.build_c(func, csrc, nargs, d, mode="san"),
                   binc_msan=srun.build_c(func, csrc, nargs, d, mode="msan"), binc_alt=None)
        if orc["binc_msan"] is None and not srun.c_uses_float(csrc):
            orc["binc_alt"] = srun.build_c(func, csrc, nargs, d, mode="alt")
        cfile = os.path.join(d, "prog_normal.c")
        bounds = [(0, 512)] * nargs
        eq = (lambda a, b: a == b)

        def primary(row):
            t = tally(binc, binr, [list(row)], binc_san=orc["binc_san"], binc_alt=orc["binc_alt"],
                      binc_msan=orc["binc_msan"])
            return ("value" if t["value"] else "panic" if t["rust_panic"] else "ub" if t["ub"]
                    else "hang" if t.get("rust_hang") else "timeout" if t["timeout"] else "agree")
        return binc, binr, cfile, bounds, eq, primary, {"harness": "run.py (scalar)", "nargs": nargs}
    rust, _ = arun.extract(func)
    sig = arun.parse_fgold(rust)
    comps = [arun.comp(t) for t in sig["params"]]
    csrc = open(os.path.join(TC, func, f"{func}.c"), errors="replace").read()
    binc = arun.build_c(csrc, sig, comps, d)
    binr = arun.build_rust(rust, sig, comps, d)
    binc_san = arun.build_c(csrc, sig, comps, d, mode="san")
    uses_fp = bool(re.search(r"\b(float|double)\b", arun.strip_scaffold(csrc)))
    binc_alt = None if uses_fp else arun.build_c(csrc, sig, comps, d, mode="alt")
    cfile = os.path.join(d, "p_normal.c")
    maxN = max([c[2] for c in comps if c[0] == "arr"] or [4])
    bounds = []
    for c in comps:
        if c[0] == "arr":
            bounds.extend([(-40, 40)] * c[2])
        else:
            bounds.append((1, maxN))
    rf = sig["ret"] in arun.FLOAT_SCALARS
    eq = (lambda a, b: arun.vals_equal(a, b, rf))

    def primary(row):
        rows = [list(row)]
        r = arun.classify(rows, arun.run_batch(binc, rows), arun.run_batch(binr, rows), rf, binc_san, binc_alt)
        return ("value" if r["value"] else "panic" if r["crash"] else "ub" if r["ub"]
                else "timeout" if r["timeout"] else "agree")
    return binc, binr, cfile, bounds, eq, primary, {"harness": "run_arrays.py", "params": sig["params"],
                                                    "ret": sig["ret"], "float_return": rf}


def variants(cfile, d):
    v, errs = {}, {}
    plan = {
        "clang_asan_ubsan_fco_O0": (CLANG, oub.san_cflags("clang") + oub.san_ldflags("clang")),
        "clang_asan_ubsan_fco_O1": (CLANG, [f if f != "-O0" else "-O1" for f in oub.san_cflags("clang")] + oub.san_ldflags("clang")),
        "gcc_asan_ubsan_fco_O0": ("gcc", oub.san_cflags("gcc") + oub.san_ldflags("gcc")),
        "clang_msan_O1": (CLANG, ["-O1", "-fsanitize=memory", "-fno-sanitize-recover=all"]),
    }
    for cc, tag in (("gcc", "gcc"), (CLANG, "clang")):
        for o in ("-O0", "-O1", "-O2"):
            plan[f"{tag}{o}_wrapv"] = (cc, [o, "-fwrapv"])
            plan[f"{tag}{o}"] = (cc, [o])
        plan[f"{tag}-O1_wrapv_signed_char"] = (cc, ["-O1", "-fwrapv", "-fsigned-char"])
        plan[f"{tag}-O1_wrapv_unsigned_char"] = (cc, ["-O1", "-fwrapv", "-funsigned-char"])
    for k, (cc, fl) in plan.items():
        r = build(cc, fl, cfile, os.path.join(d, "v_" + k))
        if r.startswith("BUILD_FAIL"):
            errs[k] = r
        else:
            v[k] = r
    return v, errs


def full_check(row, c_out, var, eq):
    rec = {}
    ok = True
    for k in ("clang_asan_ubsan_fco_O0", "clang_asan_ubsan_fco_O1", "gcc_asan_ubsan_fco_O0", "clang_msan_O1"):
        if k not in var:
            rec[k] = "unavailable"
            ok = ok and k == "clang_msan_O1"
            continue
        r = oub.run_san([var[k]], (" ".join(str(x) for x in row) + "\n").encode())
        if r["infra"]:
            rec[k] = "infra"
            ok = ok and k == "clang_msan_O1"     # MSan start-up failure is recorded, not UB
            continue
        if r["timed_out"]:
            rec[k] = "timeout"; ok = False; continue
        if r["report"] or r["rc"] != 0:
            rec[k] = "UB: " + str(r["report"] or f"rc={r['rc']}"); ok = False; continue
        line = r["stdout"].decode("latin1").splitlines()
        val = line[0] if line else None
        rec[k] = "clean"
        rec[k + "_value"] = val
        if val is None or not eq(val, c_out):
            ok = False
    values = {}
    for k in sorted(var):
        if "san" in k:
            continue
        val, rc, to = run_in(var[k], row)
        values[k] = val
        if to or val is None or not eq(val, c_out):
            ok = False
    rec["values"] = values
    rec["all_values_agree_with_oracle"] = all(v is not None and eq(v, c_out) for v in values.values())
    rec["passes"] = ok
    return rec


def job(func):
    spec = FUNCS[func]
    with tempfile.TemporaryDirectory() as d:
        binc, binr, cfile, bounds, eq, primary, meta = prepare(func, spec, d)
        if not binc or not binr:
            return {"func": func, "status": "build_fail"}
        var, var_err = variants(cfile, d)
        checked, witness, scanned = [], None, 0
        s = sum(max(0, lo) for lo, hi in bounds)          # smallest reachable L1 norm
        smax = sum(max(abs(lo), abs(hi)) for lo, hi in bounds)
        while s <= smax and witness is None and len(checked) < MAX_CHECKS:
            rows = [list(v) for v in l1_vectors(bounds, s)]
            for k in range(0, len(rows), 4096):
                chunk = rows[k:k + 4096]
                cl = srun_batch(binc, chunk)
                rl = srun_batch(binr, chunk)
                scanned += len(chunk)
                for row, c, r in zip(chunk, cl, rl):
                    if c in (None, "__PARSE__") or c == r or (r not in (None, "__PANIC__", "__PARSE__") and eq(c, r)):
                        continue
                    if len(checked) >= MAX_CHECKS:
                        break
                    rec = {"in": row, "l1": s, "c_oracle_gcc_O1_wrapv": c, "rust": r}
                    rec.update(full_check(row, c, var, eq))
                    rec["primary_harness_verdict"] = primary(row)
                    rec["passes"] = rec["passes"] and rec["primary_harness_verdict"] in ("value", "panic")
                    checked.append(rec)
                    if rec["passes"]:
                        witness = rec
                        break
                if witness is not None or len(checked) >= MAX_CHECKS:
                    break
            s += 1
        cpath = os.path.join(TC, func, f"{func}.c")
        rpath = os.path.join(TC, func, "out-rwasm-bolero", "src", "main.rs")
        where = {"c": find_line(cpath, spec["c_pat"]), "rust": find_line(rpath, spec["rs_pat"])}
        if spec.get("c_pat2"):
            where["c_2"] = find_line(cpath, spec["c_pat2"])
        if spec.get("rs_pat2"):
            where["rust_2"] = find_line(rpath, spec["rs_pat2"])
        return {"func": func, "status": "checked", **meta, "mechanism": spec["mechanism"],
                "mechanism_location": where, "c_is_nonstandard": spec.get("c_is_nonstandard", False),
                "found_by": spec["source"], "domain_bounds": bounds[:4] + (["..."] if len(bounds) > 4 else []),
                "inputs_scanned": scanned, "diverging_inputs_checked": len(checked),
                "smallest_passing_witness": witness,
                "failed_checks_before_witness": [c for c in checked if not c["passes"]][:5],
                "variant_build_errors": var_err}


def srun_batch(binp, rows):
    inp = ("\n".join(" ".join(str(x) for x in r) for r in rows) + "\n").encode()
    try:
        p = subprocess.run([binp], input=inp, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        out = p.stdout.decode("latin1").splitlines()
    except subprocess.TimeoutExpired:
        out = []
    if len(out) != len(rows):          # crash/hang somewhere: fall back to one input per process
        out = [run_in(binp, r, timeout=5)[0] for r in rows]
    return out


def positive_controls_ok():
    with tempfile.TemporaryDirectory() as d:
        r = {}
        for name, (src, argv) in oub.CONTROLS.items():
            c = os.path.join(d, name + ".c")
            open(c, "w").write(src)
            for cc in (CLANG, "gcc"):
                tag = "clang" if cc == CLANG else "gcc"
                b = os.path.join(d, f"{name}_{tag}")
                p = subprocess.run([cc] + oub.san_cflags(tag) + [c, "-o", b] + oub.san_ldflags(tag),
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                if p.returncode:
                    r[f"{name}/{tag}"] = "build_fail"; continue
                rr = oub.run_san([b] + argv)
                r[f"{name}/{tag}"] = "fired" if rr["report"] else ("infra" if rr["infra"] else "clean")
        r["all_as_expected"] = all(v == ("clean" if k.startswith("clean/") else "fired") for k, v in r.items())
        return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--out", default=os.path.join(REPO, "results", "vert_grid_misses.json"))
    args = ap.parse_args()
    t0 = datetime.datetime.now(datetime.timezone.utc)
    with ProcessPoolExecutor(max_workers=args.jobs) as ex:
        res = list(ex.map(job, list(FUNCS)))
    controls = positive_controls_ok()

    def ver(cmd):
        try:
            return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True).stdout.strip().splitlines()[0]
        except OSError:
            return "ABSENT"
    out = {
        "provenance": {
            "script": "systems/vert/grid_misses.py",
            "command": os.environ.get("DOCKER_CMD", "") or ("python3 " + " ".join(sys.argv)),
            "git_rev": os.environ.get("GIT_REV", "unknown"), "git_dirty_note": os.environ.get("GIT_DIRTY", ""),
            "host": os.environ.get("HOST_DESC", platform.system()), "arch": platform.machine(),
            "image": os.environ.get("IMAGE_DESC", "?"),
            "toolchains": {"rustc": ver(["rustc", "--version"]), "gcc": ver(["gcc", "--version"]),
                           "clang": ver([CLANG, "--version"]), "python": platform.python_version()},
            "sanitizer_flags": {"clang": oub.san_cflags("clang") + oub.san_ldflags("clang"),
                                "gcc": oub.san_cflags("gcc") + oub.san_ldflags("gcc"),
                                "env": oub.SAN_ENV, "from": "harness/oracle_ub_common.py"},
            "date_start_utc": t0.isoformat(timespec="seconds"),
            "date_end_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        },
        "rule": __doc__,
        "sanitizer_positive_controls": controls,
        "summary": {
            "functions": len(res),
            "with_passing_witness": [r["func"] for r in res if r.get("smallest_passing_witness")],
            "without_passing_witness": [r["func"] for r in res if not r.get("smallest_passing_witness")],
            "witnesses": {r["func"]: ({k: r["smallest_passing_witness"][k] for k in
                                       ("in", "c_oracle_gcc_O1_wrapv", "rust", "primary_harness_verdict")}
                                      if r.get("smallest_passing_witness") else None) for r in res},
        },
        "functions": res,
    }
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=1)
    print(json.dumps(out["summary"], indent=1))
    print(json.dumps(controls))


if __name__ == "__main__":
    main()
