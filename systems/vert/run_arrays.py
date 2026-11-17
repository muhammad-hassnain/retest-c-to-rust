"""VERT breadth extension: array- and float-argument functions.

run.py drives only the all-scalar-i32 subset (239 funcs). This covers the next two buckets:
  * 177 functions with fixed `[i32; N]` array args (+ scalars)  -- where real logic bugs hide,
  * 11 functions with f32/f64 scalar args,
by parsing each translation's `fn f_gold(...)` signature (the source of truth for param types,
array sizes, and return type) and driving the SAME held-out inputs into the original C (gcc
oracle) and the accepted Rust. Arrays are passed as contiguous `int[N]` to C (its f_gold takes
`int arr[]`) and as `[i32; N]` to Rust; scalars are kept in-domain (0..N, so length-like args
don't push either side out of bounds). Int returns compared exact; float returns with a small
relative tolerance (so last-bit rounding isn't flagged, but a wrong formula is). UB-crashes in C
and Rust panics are separated from real value divergences, same discipline as run.py.

  docker run --rm -v "$PWD:/work" -w /work crust-stable python3 systems/vert/run_arrays.py
"""
import argparse, json, math, os, re, subprocess, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
VERT = os.path.join(REPO, "artifacts", "vert")
TC = os.path.join(VERT, "benchmark", "c_transcoder")
LLM_MARK = "////// LLM Output //////"
INT_SCALARS = {"i8", "i16", "i32", "i64", "u8", "u16", "u32", "u64", "isize", "usize"}
FLOAT_SCALARS = {"f32", "f64"}


def accepted_functions():
    out = []
    for line in open(os.path.join(VERT, "c_results.csv")):
        m = re.match(r"\s*([A-Za-z0-9_]+),\s*compile=(\d)", line)
        if m and m.group(2) == "1":
            out.append(m.group(1))
    return out


def extract(func):
    p = os.path.join(TC, func, "out-rwasm-bolero", "src", "main.rs")
    if not os.path.isfile(p):
        return None, None
    text = open(p, errors="replace").read()
    parts = text.split(LLM_MARK)
    if len(parts) < 3:
        return None, None
    return parts[1].strip(), text


def parse_fgold(rust):
    """Parse `fn f_gold(p: T, ...) -> R`. Returns (params:[type], ret) or None."""
    m = re.search(r"\bfn\s+f_gold\s*\(([^)]*)\)\s*(?:->\s*([^\{]+?))?\s*\{", rust, re.S)
    if not m:
        return None
    params = []
    if m.group(1).strip():
        for p in m.group(1).split(","):
            if ":" not in p:
                return None
            params.append(re.sub(r"\s+", "", p.split(":", 1)[1]))
    ret = re.sub(r"\s+", "", (m.group(2) or "()"))
    return {"params": params, "ret": ret}


def comp(t):
    """Classify a param type -> ('scal',t) | ('arr',elem,N) | None (unsupported)."""
    if t in INT_SCALARS or t in FLOAT_SCALARS:
        return ("scal", t)
    m = re.match(r"\[([a-z0-9]+);(\d+)\]$", t)
    if m and m.group(1) in INT_SCALARS:
        return ("arr", m.group(1), int(m.group(2)))
    return None


def supported(sig):
    if not sig or not sig["params"]:
        return False
    comps = [comp(t) for t in sig["params"]]
    if any(c is None for c in comps):
        return False
    ret = sig["ret"]
    if ret not in INT_SCALARS and ret not in FLOAT_SCALARS:
        return False
    # must have at least one array or one float (scalar-int-only is run.py's job)
    return any(c[0] == "arr" for c in comps) or any(t in FLOAT_SCALARS for t in sig["params"]) \
        or ret in FLOAT_SCALARS


def strip_scaffold(csrc):
    """Keep includes + helpers + f_gold; drop the test scaffold (f_filled stub + main).
    Cut at the START of the line that begins the f_filled definition or main() -- backing up
    over the return type (e.g. `double f_filled`), so no dangling type specifier is left."""
    cut = len(csrc)
    for m in re.finditer(r"\bf_filled\b|\b(?:int|void)\s+main\b", csrc):
        i = csrc.rfind("\n", 0, m.start())
        cut = min(cut, i + 1 if i != -1 else m.start())
    return csrc[:cut]


# ---- deterministic input generation -------------------------------------------------
def array_patterns(N):
    """A fixed, structured set of length-N int arrays (no randomness)."""
    pats = [[0] * N, [1] * N, [-1] * N, list(range(N)), list(range(N, 0, -1)),
            [2] * N, [1 if i % 2 == 0 else 0 for i in range(N)], [5 - i for i in range(N)],
            [i * 2 for i in range(N)], [N - i - 1 for i in range(N)], [3, 1, 4, 1, 5, 9, 2, 6][:N] + [0] * max(0, N - 8),
            [-(i + 1) for i in range(N)], [100, 1, 50, 2, 25, 3][:N] + [0] * max(0, N - 6),
            [7] * N, [i % 3 for i in range(N)]]
    # pad short patterns to length N, trim long
    return [(p + [0] * N)[:N] for p in pats]


def gen_cases(comps, cap):
    arr_sizes = [c[2] for c in comps if c[0] == "arr"]
    maxN = max(arr_sizes) if arr_sizes else 4
    # scalars kept in [1, N]: a length-like scalar of 0 means "empty array", where the C
    # reads arr[-1] (UB, returns garbage silently) while Rust panics on 0usize-1 -- a
    # degenerate C-UB-vs-Rust-panic divergence, not a translation bug, so we exclude it.
    scal_vals = sorted(set([maxN, 1, max(1, maxN // 2), 2]))
    # per-array pattern list
    arr_lists = [array_patterns(c[2]) if c[0] == "arr" else None for c in comps]
    rows = []
    # iterate: array patterns in lockstep (index p) x scalar value choices
    nA = max((len(a) for a in arr_lists if a), default=1)
    for p in range(nA):
        for sv in scal_vals:
            row = []
            for i, c in enumerate(comps):
                if c[0] == "arr":
                    row.extend(arr_lists[i][p % len(arr_lists[i])])
                else:
                    row.append(sv)
            rows.append(row)
            if len(rows) >= cap:
                return rows
    return rows


# ---- drivers ------------------------------------------------------------------------
def c_scalar_type(t):
    return "double" if t in FLOAT_SCALARS else "long long"

def c_cast_type(t):
    return {"f32": "float", "f64": "double"}.get(t, "int" if t in ("i8", "i16", "i32", "u8", "u16", "u32") else "long long")


def build_c(csrc, sig, comps, d, mode="normal"):
    csrc = strip_scaffold(csrc)
    decls, scans, addrs, callargs = [], [], [], []
    ai = si = 0
    for c in comps:
        if c[0] == "arr":
            N = c[2]
            decls.append(f"int arr{ai}[{N}];")
            for k in range(N):
                scans.append("%d"); addrs.append(f"&arr{ai}[{k}]")
            callargs.append(f"arr{ai}")
            ai += 1
        else:
            t = c[1]
            decls.append(f"{c_scalar_type(t)} s{si};")
            scans.append("%lf" if t in FLOAT_SCALARS else "%lld"); addrs.append(f"&s{si}")
            callargs.append(f"({c_cast_type(t)})s{si}")
            si += 1
    total = len(scans)
    ret_is_float = sig["ret"] in FLOAT_SCALARS
    call = "f_gold(" + ", ".join(callargs) + ")"
    pr = ('    printf("%.9g\\n",(double)(' + call + '));') if ret_is_float \
        else ('    printf("%lld\\n",(long long)(' + call + '));')
    main = f"""
#include <stdlib.h>
#include <stdio.h>
int main(void){{
  char line[16384];
  while(fgets(line,sizeof line,stdin)){{
    {' '.join(decls)}
    if(sscanf(line,"{' '.join(scans)}",{', '.join(addrs)})!={total}){{printf("__PARSE__\\n");continue;}}
{pr}
    fflush(stdout);
  }}
  return 0;
}}
"""
    cfile = os.path.join(d, "p_" + mode + ".c"); open(cfile, "w").write(csrc + "\n" + main)
    binc = os.path.join(d, "cb_" + mode)
    if mode == "san":
        # UB oracle: trap OOB reads/writes AND signed overflow (no -fwrapv) -> a divergence that
        # only appears where the original C executes UB is reclassified as C-UB, not a bug.
        flags = ["gcc", "-O0", "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                 "-w", cfile, "-o", binc, "-lm"]
    elif mode == "alt":
        # opt-level cross-check (-O0 vs the -O1 primary): differs on uninitialized/VLA reads, same
        # on any well-defined INTEGER program. Not used for float returns (FP is legitimately
        # opt-dependent via contraction/rounding).
        flags = ["gcc", "-O0", "-fwrapv", "-w", cfile, "-o", binc, "-lm"]
    else:
        flags = ["gcc", "-O1", "-fwrapv", "-w", cfile, "-o", binc, "-lm"]
    r = subprocess.run(flags, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return binc if (r.returncode == 0 and os.path.isfile(binc)) else None


def build_rust(rust, sig, comps, d):
    # parse tokens; build arrays + scalars; call f_gold in order.
    reads, callargs = [], []
    tok = 0; ai = si = 0
    for c in comps:
        if c[0] == "arr":
            N = c[2]
            elems = ", ".join(f"t[{tok+k}] as i32" for k in range(N))
            reads.append(f"let arr{ai}:[i32;{N}]=[{elems}];")
            callargs.append(f"arr{ai}")
            tok += N; ai += 1
        else:
            t = c[1]
            if t in FLOAT_SCALARS:
                reads.append(f"let s{si}:{t}=tf[{tok}] as {t};")
            else:
                reads.append(f"let s{si}:{t}=t[{tok}] as {t};")
            callargs.append(f"s{si}")
            tok += 1; si += 1
    total = tok
    ret_is_float = sig["ret"] in FLOAT_SCALARS
    printexpr = 'println!("{:.9}", r as f64);' if ret_is_float else 'println!("{}", r as i64);'
    main = f"""
use std::io::BufRead;
fn main(){{ std::panic::set_hook(Box::new(|_|{{}}));
 for line in std::io::stdin().lock().lines(){{
   let line=match line{{Ok(l)=>l,Err(_)=>break}};
   let raw:Vec<&str>=line.split_whitespace().collect();
   if raw.len()<{total}{{println!("__PARSE__");continue;}}
   let t:Vec<i64>=raw.iter().map(|x|x.parse::<f64>().unwrap_or(0.0) as i64).collect();
   let tf:Vec<f64>=raw.iter().map(|x|x.parse::<f64>().unwrap_or(0.0)).collect();
   let _=&t; let _=&tf;
   let res=std::panic::catch_unwind(std::panic::AssertUnwindSafe(||{{
     {' '.join(reads)}
     let r=f_gold({', '.join(callargs)});
     r
   }}));
   match res {{ Ok(r)=>{{ {printexpr} }}, Err(_)=>println!("__PANIC__") }}
 }}
}}
"""
    rfile = os.path.join(d, "p.rs"); open(rfile, "w").write(rust + "\n" + main)
    binr = os.path.join(d, "rb")
    r = subprocess.run(["rustc", "-O", "-A", "warnings", rfile, "-o", binr],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return binr if (r.returncode == 0 and os.path.isfile(binr)) else None


def run_batch(binp, rows, timeout=25):
    inp = ("\n".join(" ".join(str(x) for x in r) for r in rows) + "\n").encode()
    try:
        p = subprocess.run([binp], input=inp, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout)
        return p.stdout.decode("latin1").splitlines()
    except subprocess.TimeoutExpired:
        return []


SAN_RE = re.compile(r"runtime error|AddressSanitizer|UndefinedBehaviorSanitizer|Sanitizer")
INFRA_RE = re.compile(r"out of application range|can not mmap|cannot mmap|Shadow memory range interleaves|"
                      r"FATAL: (Code|MemorySanitizer|AddressSanitizer)")
REL_TOL = 1e-6      # floating point: equal within a relative tolerance (the paper's rule)


def vals_equal(a, b, ret_is_float):
    """Integers: exact. Floating point (audit 8): NaN equals NaN (any sign / payload spelling),
    an infinity equals the same-signed infinity, otherwise |a-b| <= REL_TOL * max(|a|, |b|) -- a
    purely relative tolerance (the earlier max(1, |a|) made it absolute 1e-6 for |a| < 1, and
    inf - inf = NaN made inf != inf)."""
    if not ret_is_float:
        return a == b
    try:
        fa, fb = float(a), float(b)
    except (ValueError, TypeError):
        return False
    if math.isnan(fa) or math.isnan(fb):
        return math.isnan(fa) and math.isnan(fb)
    if math.isinf(fa) or math.isinf(fb):
        return fa == fb
    return math.isclose(fa, fb, rel_tol=REL_TOL, abs_tol=0.0)


def run_one(binp, row, timeout=3):
    """Run a binary on ONE input line. Returns (first_line|None, rc, timed_out, stderr)."""
    inp = (" ".join(str(x) for x in row) + "\n").encode()
    try:
        p = subprocess.run([binp], input=inp, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout)
        out = p.stdout.decode("latin1").splitlines()
        return (out[0] if out else None), p.returncode, False, p.stderr.decode("latin1", "replace")
    except subprocess.TimeoutExpired:
        return None, -1, True, ""


def eval_rows(binc, binr, rows):
    """Aligned per-input (C lines, Rust lines), as systems/batch_diff.eval_cases (audit 8): one batch
    per binary; if either comes back short (a crash or hang truncates a batch), re-run in chunks of
    32 and, for a chunk that is still short, one input per process, so every input gets its own
    verdict. A per-input hang is "__TIMEOUT__" (before: a Rust batch hang made every input a crash,
    and a crash truncated the remaining rows)."""
    cl, rl = run_batch(binc, rows), run_batch(binr, rows)
    if len(cl) == len(rows) and len(rl) == len(rows):
        return cl, rl
    C, R = [], []
    for k in range(0, len(rows), 32):
        chunk = rows[k:k + 32]
        c, r = run_batch(binc, chunk, timeout=6), run_batch(binr, chunk, timeout=6)
        if len(c) == len(chunk) and len(r) == len(chunk):
            C += c; R += r
            continue
        for row in chunk:
            a, _, ato, _ = run_one(binc, row)
            b, _, bto, _ = run_one(binr, row)
            C.append("__TIMEOUT__" if ato else a)
            R.append("__TIMEOUT__" if bto else b)
    return C, R


def run_alt(binc_alt, rows):
    """Per-input aligned output of the -O0 cross-check build (same fallback as eval_rows)."""
    al = run_batch(binc_alt, rows)
    if len(al) == len(rows):
        return al
    out = []
    for row in rows:
        a, _, to, _ = run_one(binc_alt, row)
        out.append("__TIMEOUT__" if to else a)
    return out


def san_run(binp, row, timeout=3, retries=6):
    """(verdict, first line) of one sanitizer run; verdict 'ub' | 'clean' | 'timeout' | 'infra'
    (a start-up crash with no report, retried; as batch_diff.san_run)."""
    for _ in range(retries):
        line, rc, to, err = run_one(binp, row, timeout=timeout)
        if to:
            return "timeout", None
        if INFRA_RE.search(err) or (rc < 0 and not SAN_RE.search(err)):
            continue
        return ("ub" if (rc != 0 or SAN_RE.search(err)) else "clean"), line
    return "infra", None


def classify(rows, cl, rl, ret_is_float, binc_san, binc_alt=None):
    """Two-pass: (1) batch to find candidate divergences fast; (2) per-input filter through a
    sanitizer-instrumented C oracle so any divergence that only appears where the ORIGINAL C
    executes undefined behaviour (OOB read/write, etc.) is reclassified as `ub` -- not a
    translation bug -- and genuine timeouts become their own verdict. Rust panics are kept
    SEPARATE from wrong answers. Samples are the SMALLEST-magnitude diverging inputs."""
    agree = ub = 0
    # opt-level cross-check in one batch: -O0 vs the -O1 primary disagreement -> UB (uninitialized/
    # VLA read). Skip for float returns (FP is legitimately opt-dependent).
    altl = run_alt(binc_alt, rows) if (binc_alt is not None and not ret_is_float) else None
    cands = []           # (row, c, rust, tag) tag in {value, crash, hang, c_missing}
    for i, row in enumerate(rows):
        a = cl[i] if i < len(cl) else None
        b = rl[i] if i < len(rl) else None
        if a == "__PARSE__":
            continue
        if altl is not None and a is not None and a != "__TIMEOUT__":
            alt = altl[i] if i < len(altl) else None
            if alt != "__PARSE__" and alt != a:
                ub += 1; continue            # -O0 differs (or crashes / hangs): opt-dependent C -> UB
        if a is None or a == "__TIMEOUT__":
            cands.append((row, a, b, "c_missing")); continue
        if b == "__TIMEOUT__":
            cands.append((row, a, b, "hang")); continue
        if b is None or b == "__PANIC__" or b == "__PARSE__":
            cands.append((row, a, b, "crash")); continue
        if vals_equal(a, b, ret_is_float):
            agree += 1
        else:
            cands.append((row, a, b, "value"))
    value = crash = timeout = hang = san_unresolved = 0
    vs, cs = [], []
    for row, a, b, tag in cands:
        if binc_san is not None:
            sv, sline = san_run(binc_san, row)
            if sv == "timeout":
                timeout += 1; continue                       # sanitizer run hangs: not UB-clean
            if sv == "ub":
                ub += 1; continue                            # C executes OOB/overflow UB here
            if sv == "infra" or (tag != "c_missing" and not vals_equal(sline, a, ret_is_float)):
                san_unresolved += 1; continue                # audit 8: failed / disagreeing run
        if tag == "c_missing":
            timeout += 1; continue                           # C gave no line (crash / hang)
        if tag == "hang":
            hang += 1; continue                              # Rust hangs: reported, not a panic
        if tag == "crash":
            crash += 1; cs.append((row, a))
        else:
            value += 1; vs.append((row, a, b))
    vs.sort(key=lambda t: sum(abs(x) for x in t[0]))
    cs.sort(key=lambda t: sum(abs(x) for x in t[0]))
    return {"agree": agree, "value": value, "crash": crash, "ub": ub, "timeout": timeout,
            "rust_hang": hang, "san_unresolved": san_unresolved,
            "samples": [{"in": r, "c": a, "rust": b} for r, a, b in vs[:4]],
            "crash_samples": [{"in": r, "c": a} for r, a in cs[:4]]}


def test_one(func, cap):
    rust, _ = extract(func)
    if rust is None:
        return {"func": func, "status": "no-artifact"}
    sig = parse_fgold(rust)
    if not sig or not supported(sig):
        return {"func": func, "status": "unsupported", "sig": sig["params"] if sig else None}
    comps = [comp(t) for t in sig["params"]]
    csrc_path = os.path.join(TC, func, f"{func}.c")
    if not os.path.isfile(csrc_path):
        return {"func": func, "status": "no-c"}
    csrc = open(csrc_path, errors="replace").read()
    # EXCLUDE side-effecting functions: if f_gold (or a helper it uses) prints to stdout, our
    # return-value batch comparison misaligns (the printed text interleaves with the return
    # line). These need a full-output harness, not a scalar-return one, so we skip rather than
    # miscount them as value bugs.
    if re.search(r"\b(printf|puts|putchar|fprintf|fputs|puts)\b", strip_scaffold(csrc)):
        return {"func": func, "status": "side_effecting"}
    with tempfile.TemporaryDirectory() as d:
        binc = build_c(csrc, sig, comps, d)
        if not binc:
            return {"func": func, "status": "c-build-fail"}
        binr = build_rust(rust, sig, comps, d)
        if not binr:
            return {"func": func, "status": "rust-build-fail"}
        binc_san = build_c(csrc, sig, comps, d, mode="san")   # OOB + overflow UB oracle
        # opt-level cross-check, but not when the C computes in float/double (-O0 vs -O1 can differ
        # by FP contraction -- benign, not UB). classify() also skips it for float returns.
        uses_fp = bool(re.search(r"\b(float|double)\b", strip_scaffold(csrc)))
        binc_alt = None if uses_fp else build_c(csrc, sig, comps, d, mode="alt")   # -O0 cross-check
        rows = gen_cases(comps, cap)
        cl, rl = eval_rows(binc, binr, rows)
        r = classify(rows, cl, rl, sig["ret"] in FLOAT_SCALARS, binc_san, binc_alt)
        kind = ("value" if r["value"] > 0 else "rust_crash" if r["crash"] > 0
                else "rust_hang" if r["rust_hang"] > 0 else "ub_only" if r["ub"] > 0
                else "timeout_only" if r["timeout"] > 0 or r["san_unresolved"] > 0 else "agree")
        return {"func": func, "status": "tested", "kind": kind, "san": binc_san is not None,
                "ret": sig["ret"], "params": sig["params"], **r}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=int, default=240)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=os.path.join(REPO, "results", "vert_arrays.json"))
    args = ap.parse_args()
    funcs = accepted_functions()
    if args.limit:
        funcs = funcs[:args.limit]
    agg = {"tested": 0, "agree": 0, "value_bug": 0, "rust_panic": 0, "rust_hang": 0, "ub_only": 0,
           "timeout_only": 0, "san_unavailable": 0,
           "unsupported": 0, "no-artifact": 0, "no-c": 0, "c-build-fail": 0, "rust-build-fail": 0}
    value_bugs, crash_bugs, results = [], [], []
    for i, f in enumerate(funcs):
        r = test_one(f, args.cap)
        st = r["status"]
        if st == "tested":
            agg["tested"] += 1
            if r.get("san") is False:
                agg["san_unavailable"] += 1
            k = r["kind"]
            if k == "value":
                agg["value_bug"] += 1; value_bugs.append(r)
            elif k == "rust_crash":
                agg["rust_panic"] += 1; crash_bugs.append(r)
            elif k == "rust_hang":
                agg["rust_hang"] += 1
            elif k == "ub_only":
                agg["ub_only"] += 1
            elif k == "timeout_only":
                agg["timeout_only"] += 1
            else:
                agg["agree"] += 1
        else:
            agg[st] = agg.get(st, 0) + 1
        results.append(r)
        if (i + 1) % 100 == 0:
            print(f"  ...{i+1}/{len(funcs)} tested={agg['tested']} value={agg['value_bug']} "
                  f"panic={agg['rust_panic']}", flush=True)
    summary = {"system": "VERT", "scope": "array + float functions (breadth extension)",
               "headline": "value_bug = genuine wrong-answer on inputs the original C handles "
                           "WITHOUT undefined behaviour (sanitizer-filtered); rust_panic (Rust "
                           "memory-safety panic where sanitizer-clean C returns) and ub_only "
                           "(divergence only where C executes UB) are reported SEPARATELY and are "
                           "not counted as wrong answers",
               "functions_considered": len(funcs), **agg,
               "value_bug_functions": [d["func"] for d in value_bugs],
               "rust_panic_functions": [d["func"] for d in crash_bugs]}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump({"summary": summary, "value_bugs": value_bugs, "rust_crash_bugs": crash_bugs,
               "all": results}, open(args.out, "w"), indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
