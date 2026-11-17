"""Per-function held-out differential tester for VERT's TransCoder translations.

VERT (arXiv 2404.18852) translates small single-function C programs (`int f_gold(...)`)
to Rust. The accepted Rust is embedded in each function's
`out-rwasm-bolero/src/main.rs`, between two `////// LLM Output //////` markers; the bolero
harness right after it records the argument types via `with_type::<(...)>()`.

What we add that VERT's own evaluation does not: VERT checks the translated Rust against an
**rWasm** oracle (the C compiled to WebAssembly, "equivalent by construction"). We instead use
the **original C compiled with gcc** as the oracle, and test on fresh held-out integer inputs.

Scope (v1): functions whose arguments are all scalar `i32` and that return `i32` — the common
TransCoder shape. Array/float/pointer signatures are counted as "unsupported" (a documented gap).
Both sides are built with overflow checks OFF (rustc -O, gcc 2's-complement), so we compare the
translation's LOGIC rather than debug-mode overflow panics.

Run in the stable image:
  docker run --rm -v "$PWD:/work" -w /work crust-stable python3 systems/vert/run.py -n 80
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
VERT = os.path.join(REPO, "artifacts", "vert")
TC = os.path.join(VERT, "benchmark", "c_transcoder")

import sys as _sys  # noqa: E402
_sys.path.insert(0, os.path.dirname(HERE))   # systems/
from enumerate_inputs import enumerate_cases  # noqa: E402  (deterministic, systematic grid)
from batch_diff import tally  # noqa: E402  (shared differential; sanitizer-filtered UB)

LLM_MARK = "////// LLM Output //////"


def accepted_functions():
    """Return function names VERT recorded as compile=1 (its own accepted translations)."""
    names = []
    path = os.path.join(VERT, "c_results.csv")
    with open(path) as f:
        for line in f:
            m = re.match(r"\s*([A-Za-z0-9_]+),\s*compile=(\d)", line)
            if m and m.group(2) == "1":
                names.append(m.group(1))
    return names


def extract_llm_rust(func):
    """Return the translated Rust (between the two LLM-Output markers), or None."""
    p = os.path.join(TC, func, "out-rwasm-bolero", "src", "main.rs")
    if not os.path.isfile(p):
        return None, None
    text = open(p, errors="replace").read()
    parts = text.split(LLM_MARK)
    if len(parts) < 3:
        return None, None
    rust = parts[1].strip()
    # arg types from the bolero harness: with_type::<(...)>()
    mt = re.search(r"with_type::<\(([^)]*)\)>", text)
    argtypes = None
    if mt:
        inner = mt.group(1).strip()
        argtypes = [t.strip() for t in inner.split(",") if t.strip()] if inner else []
    return rust, argtypes


def c_source(func):
    p = os.path.join(TC, func, f"{func}.c")
    return open(p, errors="replace").read() if os.path.isfile(p) else None


def supported(argtypes):
    return argtypes is not None and len(argtypes) >= 1 and all(t == "i32" for t in argtypes)


def c_return_type(csrc):
    """Return the ORIGINAL C f_gold return type, e.g. 'int', 'long int', 'double'."""
    m = re.search(r"\b([A-Za-z_][\w ]*?)\s+f_gold\s*\(", csrc)
    return m.group(1).strip() if m else None


def c_uses_float(csrc):
    """True if f_gold's C computes in float/double (so -O0 vs -O1 may differ legitimately)."""
    return bool(re.search(r"\b(float|double)\b", strip_scaffold(csrc)))


def strip_scaffold(csrc):
    """TransCoder C files ship their own test main + an empty f_filled stub after f_gold.
    Keep everything up to (not including) f_filled / main so only includes+helpers+f_gold remain."""
    cut = len(csrc)
    for marker in ("int f_filled", "f_filled (", "f_filled(", "\nint main", "\n int main"):
        i = csrc.find(marker)
        if i != -1:
            # cut at the START of that line, so a return type such as `bool ` before
            # `f_filled (` is not left dangling (audit 4: this broke 27 bool functions' C build)
            cut = min(cut, csrc.rfind("\n", 0, i) + 1)
    return csrc[:cut]


def build_c(func, csrc, nargs, d, mode="normal"):
    csrc = strip_scaffold(csrc)
    # BATCH driver: read one case per stdin line (space-separated ints), print one result line.
    fmt = " ".join(["%d"] * nargs)
    addrs = ", ".join(f"&a[{i}]" for i in range(nargs))
    call = ", ".join(f"a[{i}]" for i in range(nargs))
    main = f"""
#include <stdlib.h>
#include <stdio.h>
int main(void) {{
    int a[{max(nargs, 1)}];
    char line[8192];
    while (fgets(line, sizeof line, stdin)) {{
        if (sscanf(line, "{fmt}", {addrs}) != {nargs}) {{ printf("__PARSE__\\n"); continue; }}
        printf("%lld\\n", (long long)(f_gold({call})));
        fflush(stdout);
    }}
    return 0;
}}
"""
    src = csrc + "\n" + main
    cfile = os.path.join(d, "prog_" + mode + ".c")
    with open(cfile, "w") as f:
        f.write(src)
    binc = os.path.join(d, "c_bin_" + mode)
    if mode == "san":
        # UB oracle: trap OOB reads/writes AND signed overflow (no -fwrapv) so a divergence that
        # only appears where the ORIGINAL C executes UB is reclassified as C-UB, not a bug.
        flags = ["gcc", "-O0", "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                 "-w", cfile, "-o", binc, "-lm"]
    elif mode == "msan":
        # Rigorous uninitialized-read oracle (clang MemorySanitizer). Precisely flags use of
        # uninitialized/VLA memory in the ORIGINAL C -- the gold standard the -O0/-O1 cross-check
        # only approximates. Returns None where clang is unavailable, so callers fall back.
        if not shutil.which("clang"):
            return None
        flags = ["clang", "-O1", "-fsanitize=memory", "-fno-sanitize-recover=all",
                 "-w", cfile, "-o", binc, "-lm"]
    elif mode == "alt":
        # opt-level cross-check (fallback when clang/MSan is absent): -O0 differs from the -O1
        # primary on uninitialized/VLA reads while a well-defined integer program is -O-invariant.
        flags = ["gcc", "-O0", "-fwrapv", "-w", cfile, "-o", binc, "-lm"]
    else:  # normal primary oracle
        flags = ["gcc", "-O1", "-fwrapv", "-w", cfile, "-o", binc, "-lm"]
    r = subprocess.run(flags, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return binc if (r.returncode == 0 and os.path.isfile(binc)) else None


def build_rust(rust, nargs, d):
    # BATCH driver: read one case per stdin line; catch_unwind so a panic doesn't kill the batch.
    call = ", ".join(f"v[{i}]" for i in range(nargs))
    main = f"""
use std::io::BufRead;
fn main() {{
    std::panic::set_hook(Box::new(|_| {{}}));
    let stdin = std::io::stdin();
    for line in stdin.lock().lines() {{
        let line = match line {{ Ok(l) => l, Err(_) => break }};
        let v: Vec<i32> = line.split_whitespace()
            .map(|s| s.parse::<i64>().unwrap_or(0) as i32).collect();
        if v.len() < {nargs} {{ println!("__PARSE__"); continue; }}
        let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| f_gold({call})));
        match r {{ Ok(x) => println!("{{}}", x as i64), Err(_) => println!("__PANIC__") }}
    }}
}}
"""
    src = rust + "\n" + main
    rfile = os.path.join(d, "prog.rs")
    with open(rfile, "w") as f:
        f.write(src)
    binr = os.path.join(d, "r_bin")
    r = subprocess.run(["rustc", "-O", "-A", "warnings", rfile, "-o", binr],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return binr if (r.returncode == 0 and os.path.isfile(binr)) else None


def test_function(func, n, legacy=False, nonneg=True, max_abs=512, grid="v1", wide_int=False):
    rust, argtypes = extract_llm_rust(func)
    csrc = c_source(func)
    if rust is None or csrc is None:
        return {"func": func, "status": "no-artifact"}
    if not supported(argtypes):
        return {"func": func, "status": "unsupported", "argtypes": argtypes}
    # Exclude side-effecting f_gold (prints to stdout) -- its text would misalign our
    # return-value batch comparison. (0/215 scalar-i32 functions actually do this today, but
    # the guard keeps the harness robust; it mirrors run_arrays.py.)
    if re.search(r"\b(printf|puts|putchar|fprintf|fputs)\b", strip_scaffold(csrc)):
        return {"func": func, "status": "side_effecting"}
    # Gate on the ORIGINAL C return type: VERT's accepted Rust narrows every signature to `-> i32`,
    # but some C f_gold return `long`/`long long` (or `double`). For those the i32 translation is a
    # separate return-WIDTH fidelity issue, and printing a 64-bit return via the driver would be a
    # format mismatch -- so we exclude them as a documented gap rather than count a truncation
    # artifact as a logic bug. (The 174 remaining are genuine int-return functions.)
    # arXiv (issues.md 47): --wide-int tests the C functions that return a wider SIGNED integer type
    # (long, long long). The C driver prints (long long) f_gold(...), so the C value is printed in full;
    # VERT's Rust returns i32 and its driver prints it widened to i64, so a value the i32 cannot hold
    # shows as a divergence. float/double and unsigned int returns stay excluded; unsigned long long is admitted
    # (one function, SUM_SUBSETS_SET_FORMED_FIRST_N_NATURAL_NUMBERS, whose Rust does not build).
    crt = c_return_type(csrc)
    wide_ok = wide_int and crt is not None and re.fullmatch(r"(signed |unsigned )?long( long)?( int)?", crt) is not None
    if crt is None or (re.search(r"\b(long|double|float|unsigned)\b", crt) and not wide_ok):
        return {"func": func, "status": "non_int_return", "c_return": crt}
    nargs = len(argtypes)
    with tempfile.TemporaryDirectory() as d:
        binc = build_c(func, csrc, nargs, d)
        binr = build_rust(rust, nargs, d)
        if not binc:
            return {"func": func, "status": "c-build-fail"}
        if not binr:
            return {"func": func, "status": "rust-build-fail"}
        binc_san = build_c(func, csrc, nargs, d, mode="san")    # OOB + overflow UB oracle (gcc)
        binc_msan = build_c(func, csrc, nargs, d, mode="msan")  # uninitialized-read UB oracle (clang MSan; None if no clang)
        # -O0/-O1 cross-check is the FALLBACK uninitialized detector when MSan is unavailable; skip
        # it for float-internal functions (FP is legitimately opt-dependent).
        binc_alt = None
        if binc_msan is None and not c_uses_float(csrc):
            binc_alt = build_c(func, csrc, nargs, d, mode="alt")
        # deterministic, systematic grid; magnitude capped so loop-heavy competitive functions stay
        # fast. Divergences are filtered per-input: sanitizer (OOB/overflow) + MSan or the -O0/-O1
        # cross-check (uninitialized/VLA reads), so silent C UB isn't counted as a bug.
        cases = enumerate_cases(["i32"] * nargs, cap=n, max_abs=max_abs, nonneg=nonneg, legacy=legacy, grid=grid)
        t = tally(binc, binr, cases, binc_san=binc_san, binc_alt=binc_alt, binc_msan=binc_msan)
        return {"func": func, "status": "tested", "nargs": nargs, "cases": len(cases), **({"c_return": crt} if wide_ok else {}),
                "san": binc_san is not None, "msan": binc_msan is not None, "alt": binc_alt is not None,
                "value_div": t["value"], "rust_panic_c_ok": t["rust_panic"],
                "ub_div": t["ub"], "timeout_div": t["timeout"], "san_infra": t.get("san_infra", 0), "rust_hang": t.get("rust_hang", 0),
                "value_samples": t["value_samples"], "rust_panic_samples": t["crash_samples"]}


def _job(a):
    func, n, kw = a
    return test_function(func, n, **kw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=80, help="held-out inputs per function (grid cap)")
    ap.add_argument("--limit", type=int, default=0, help="max functions (0 = all)")
    ap.add_argument("--full", action="store_true",
                    help="full structured grid incl. negatives and up to +-8192 (default: "
                         "in-domain non-negative, |x|<=512 -- the defensible headline)")
    ap.add_argument("--max-abs", type=int, default=0,
                    help="override the grid magnitude cap |x| (0 = use the --full/default preset); "
                         "used for the grid sensitivity sweep")
    ap.add_argument("--signed", action="store_true",
                    help="include negative inputs (override the default non-negative domain); "
                         "used for the grid sensitivity sweep")
    ap.add_argument("--legacy-grid", action="store_true",
                    help="v1 grid: first n tuples of the product (truncates multi-argument grids); "
                         "default spreads n tuples evenly over the product")
    ap.add_argument("--grid", choices=("v1", "v2"), default="v1",
                    help="v1 (default, every shipped result) or v2 (opt-in: adds powers of two +-1 and "
                         "the range ends per argument, no one-argument truncation; enumerate_inputs.py)")
    ap.add_argument("--jobs", type=int, default=1,
                    help="functions tested in parallel (default 1 = the original sequential loop)")
    ap.add_argument("--wide-int", action="store_true",
                    help="arXiv (issues.md 47): also test functions whose C returns long / long long")
    ap.add_argument("--only", default="",
                    help="comma-separated function names (default: every compile=1 function)")
    ap.add_argument("--out", default=os.path.join(REPO, "results", "vert_diff.json"))
    args = ap.parse_args()
    # Presets: default = nonneg, |x|<=512 ; --full = signed, |x|<=8192. --max-abs/--signed override
    # either preset independently so the grid can be swept (sensitivity analysis).
    nonneg = not (args.full or args.signed)
    max_abs = args.max_abs if args.max_abs > 0 else (8192 if args.full else 512)

    funcs = accepted_functions()
    if args.only:
        want = [x for x in args.only.split(",") if x]
        missing = [x for x in want if x not in funcs]
        assert not missing, f"not compile=1 in c_results.csv: {missing}"
        funcs = [f for f in funcs if f in want]
    if args.limit:
        funcs = funcs[:args.limit]

    results = []
    agg = {"tested": 0, "agree": 0, "value_bug": 0, "rust_panic": 0, "ub_only": 0,
           "timeout_only": 0, "san_unavailable": 0,
           "unsupported": 0, "side_effecting": 0, "c-build-fail": 0, "rust-build-fail": 0,
           "no-artifact": 0}
    value_bugs, rust_panic_fns = [], []
    kw = dict(legacy=args.legacy_grid, nonneg=nonneg, max_abs=max_abs, grid=args.grid, wide_int=args.wide_int)
    if args.jobs > 1:
        import multiprocessing as mp
        pool = mp.Pool(args.jobs)
        it = pool.imap(_job, [(f, args.n, kw) for f in funcs], chunksize=1)   # input order kept
    else:
        it = (test_function(f, args.n, **kw) for f in funcs)
    for i, (func, r) in enumerate(zip(funcs, it)):
        st = r["status"]
        if st == "tested":
            agg["tested"] += 1
            if r.get("san") is False:
                agg["san_unavailable"] += 1
            if r["value_div"] > 0:               # genuine wrong answer on sanitizer-clean input
                agg["value_bug"] += 1
                value_bugs.append(r)
            elif r["rust_panic_c_ok"] > 0:       # Rust panics where sanitizer-clean C returns
                agg["rust_panic"] += 1
                rust_panic_fns.append(r)
            elif r.get("rust_hang", 0) > 0:      # Rust never terminates where C returns (reported separately)
                agg["rust_hang"] = agg.get("rust_hang", 0) + 1
            elif r["ub_div"] > 0:                # differs only where C executes UB (not a bug)
                agg["ub_only"] += 1
            elif r["timeout_div"] > 0:
                agg["timeout_only"] += 1
            else:
                agg["agree"] += 1
        else:
            agg[st] = agg.get(st, 0) + 1
        results.append(r)
        if (i + 1) % 50 == 0:
            print(f"  ...{i+1}/{len(funcs)} (tested={agg['tested']} "
                  f"value_bug={agg['value_bug']} rust_panic={agg['rust_panic']})",
                  flush=True)
    if args.jobs > 1:
        pool.close()
        pool.join()

    summary = {"system": "VERT", "benchmark": "c_transcoder",
               "domain": ("in-domain (non-negative, |x|<=512)" if nonneg
                          else "full structured grid (incl. negatives, up to +-8192)"),
               "headline": "value_bug = genuine wrong-answer on inputs the ORIGINAL C handles "
                           "without UB (ASan/UBSan-filtered); rust_panic and ub_only are reported "
                           "separately and are NOT counted as wrong answers",
               "grid": ("legacy (truncated product)" if args.legacy_grid else
                        "spread (R_d low-discrepancy over the product; 1-arg unchanged)" if args.grid == "v1" else
                        "v2: per-arg v1 grid + powers of two +-1 + range ends; 1-arg untruncated; "
                        "multi-arg spread (R_d) over the v2 grids"),
               "functions_considered": len(funcs), **agg,
               "value_bug_functions": [d["func"] for d in value_bugs],
               "rust_panic_functions": [d["func"] for d in rust_panic_fns]}
    with_out = {"summary": summary, "value_bugs": value_bugs,
                "rust_panic_fns": rust_panic_fns, "all": results}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(with_out, f, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
