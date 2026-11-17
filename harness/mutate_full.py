"""M7: power of the command-line (coreutils) differential harness, measured on the FULL v4 grid.

What is measured
----------------
The headline C2SaferRust numbers come from the deterministic v4 grid in
results/{cat,head,tail,uniq}_cases_v4.json (3000 + 1404 + 1170 + 3000 = 8574 cases) and the
comparison in harness/difftest.compare_case (byte-exact stdout, exit code, program-name-normalised
stderr; 15 s timeout). This script seeds ONE operator fault at a time into a translation that
agrees with the C on that whole grid, rebuilds, and re-runs exactly that case set through exactly
that comparison. Every kill is therefore caused by the seeded fault.

  * Mutated tree: the C2Rust starting translation (coreutils/src/<util>/rust), which agrees with
    the C on stdout/exit for all 8574 cases (results/*_diff_v4_c2rust_baseline.json). The
    `baseline` step re-runs it here and checks, case by case, that every verdict matches that file.
  * Mutated file: the utility's own translated source, src/<util>.rs (the gnulib helper modules
    are not mutated; same scope as harness/mutate.py).
  * Operators (one fault per mutant):
      rel   relational: negation (== <-> !=, < -> >=, <= -> >, > -> <=, >= -> <) and boundary
            shift (< <-> <=, > <-> >=); binary operators written with spaces on both sides
            (rustfmt style), so generics, ->, => and shifts are never touched.
      arith arithmetic: binary + <-> -, * <-> /, % -> /; compound += <-> -=, *= <-> /=;
            C2Rust's method forms wrapping_add <-> wrapping_sub, wrapping_mul <-> wrapping_div,
            wrapping_rem -> wrapping_div.
      const integer literal +1 and -1 (decimal, hex or octal, suffix kept; 0-1 is written (-1)),
            except C2Rust relooper labels (literals assigned to current_block_N, and the same
            values in match arms), which are not constants of the C program.
    Tokens inside string/char literals, comments and attributes are never mutated.
  * Site rule (deterministic): candidates are (site, replacement) pairs inside the BODY of a
    function of <util>.rs that the unmodified grid executes (function entry count > 0 in
    region coverage of the baseline on the full grid, see mutate_full_cov.py). Statics/consts
    are excluded. For each (util, operator) the candidates are sorted by (byte offset,
    replacement) and a sample of size N is drawn with random.Random(f"{SEED}:{util}:{op}").
  * Verdict per mutant (cases run in grid order; stop at the first S1/crash):
      killed_s1     first stdout/exit divergence with a normal exit
      killed_crash  first stdout/exit divergence where the SUT died on a signal or a Rust panic
                    (exit 101; the C never exits 101 on this grid)
      killed_s2     no S1/crash on the whole grid, but stderr differs from the C on a case where
                    the unmutated baseline's stderr agreed with the C (reported separately)
      hang          SUT timed out (15 s) on >= HANG_CAP cases and no S1/crash was found; after
                    the third timeout the scan continues with a 1 s timeout for at most 60 s
                    (scan_complete records whether it reached the end of the grid)
      survived      none of the above on all 8574 cases
      noncompile    cargo build failed (excluded from rates; listed)
  * Survivor classes: tce (binary byte-identical to the unmutated build: trivially equivalent;
    re-checked non-incrementally by mutate_full_tce_check.py), unreached (coverage count 0 at
    the mutated token on the full grid), reached-not-detected (executed, no divergence).
    mutate_full_extgrid.py re-runs cat/uniq survivors on the cases beyond the 3000-case cap;
    mutate_full_review.py draws a seeded sample of reached survivors for hand judgment
    (mutate_full_review.json), which summarize counts.
  * Also run, not counted in the rates: the 4 sanity mutants (must be killed) and a bridge set,
    the exact 15 relational sites per util that harness/mutate.py picks (find_ror_sites), so the
    old first-500-case figures can be compared on the full grid.
  * The run used N = 200 per (util, operator) (arith pools are smaller, so arith is a census).
  * MFULL_VARIANT=rust_WIP mutates C2SaferRust's output instead, using only the cases where the
    unmutated rust_WIP agrees with the C on stdout/exit; it is reported separately.

Commands (run inside the crust-heldout image; see harness/mutate_full_run.sh)
  prepare | baseline | covrun | sites --cov-json F -n N | run | summarize --out F
"""
import argparse
import hashlib
import json
import math
import multiprocessing as mp
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import common  # noqa: E402
import difftest  # noqa: E402
from common import RunResult, controlled_env  # noqa: E402

REPO = os.path.dirname(HERE)
UTILS = ["cat", "head", "tail", "uniq"]
SEED = "m7-cli-mutation-2026-11-13"
TIMEOUT = 15          # same as the headline difftest runs (difftest.py default)
HANG_CAP = 3          # timeouts before a mutant is classed as hanging
HANG_SCAN_TIMEOUT = 1  # after HANG_CAP timeouts, keep scanning for an S1/crash with this timeout
HANG_SCAN_BUDGET = 60   # ... for at most this many seconds (then scan_complete = False)
OUT_CAP = 64 << 20    # per-stream capture cap; the C's largest output on the grid is < 64 KiB

WORK = os.environ.get("MFULL_WORK", "/w")             # Linux work dir mounted in the container
TREES = os.path.join(WORK, "trees")                    # private copies of the artifact trees
# Which tree is mutated. `rust` (default) = the C2Rust starting translation, which agrees with
# the C on all 8574 cases. `rust_WIP` = C2SaferRust's output (reported separately): it diverges
# on 1651 cases, so for it every step only uses the cases where the UNMUTATED rust_WIP agrees
# with the C on stdout/exit (as harness/mutate.py's pre-filter does).
VARIANT = os.environ.get("MFULL_VARIANT", "rust")
BUILD = os.path.join(WORK, "build" if VARIANT == "rust" else f"build_{VARIANT}")
REF_DIFF = "_diff_v4_c2rust_baseline.json" if VARIANT == "rust" else "_diff_v4.json"


# ------------------------------------------------------------------ capped, recording capture
_REC = []


def run_capture_capped(argv, stdin_bytes=b"", cwd=None, timeout=15, env_overrides=None):
    """Drop-in for common.run_capture: same env, same pipes, same timeout semantics
    (returncode -1, timed_out=True), but each stream is capped at OUT_CAP bytes so a mutant that
    loops while printing cannot exhaust memory (on overflow the process is killed; the captured
    prefix already differs from the C's output, so the verdict is unchanged). Records each result
    in _REC so the caller can also see the raw stderr."""
    env = controlled_env()
    if env_overrides:
        env.update(env_overrides)
    p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, cwd=cwd, env=env)
    bufs = {"out": [], "err": []}
    over = {"flag": False}

    def reader(stream, key):
        n = 0
        while True:
            chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
            if not chunk:
                break
            if n < OUT_CAP:
                bufs[key].append(chunk[:OUT_CAP - n])
            n += len(chunk)
            if n >= OUT_CAP:
                over["flag"] = True
                try:
                    p.kill()
                except OSError:
                    pass
                break

    def writer():
        try:
            if stdin_bytes:
                p.stdin.write(stdin_bytes)
        except (BrokenPipeError, OSError):
            pass
        try:
            p.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    ts = [threading.Thread(target=reader, args=(p.stdout, "out"), daemon=True),
          threading.Thread(target=reader, args=(p.stderr, "err"), daemon=True),
          threading.Thread(target=writer, daemon=True)]
    for t in ts:
        t.start()
    timed_out = False
    try:
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        p.kill()
        p.wait()
    for t in ts:
        t.join(5)
    out, err = b"".join(bufs["out"]), b"".join(bufs["err"])
    rr = RunResult(out, err, -1 if timed_out else p.returncode, timed_out)
    _REC.append((rr, over["flag"]))
    return rr


difftest.run_capture = run_capture_capped  # compare_case looks the name up in difftest


def compare(case, oracle_bin, sut_bin, timeout=TIMEOUT):
    """difftest.compare_case verbatim, plus the SUT's normalised stderr hash and capture flags."""
    _REC.clear()
    v = difftest.compare_case(case, oracle_bin, sut_bin, timeout)
    o, s = _REC[0][0], _REC[1][0]
    se = common.normalize_stderr(s.stderr, oracle_bin, sut_bin)
    v["_sut_stderr_sha"] = hashlib.sha256(se).hexdigest()[:16]
    v["_sut_capped"] = _REC[1][1]
    v["_sut_panic"] = b"panicked at" in s.stderr
    return v


# ------------------------------------------------------------------------------ paths/builds
def cases_path(util):
    return os.path.join(REPO, "results", f"{util}_cases_v4.json")


def load_cases(util):
    u, cases = difftest.load_cases(cases_path(util))
    assert u == util
    return cases


def oracle_bin(util):
    return os.path.join(TREES, util, "c", util)


def cargo_build(src_dir, util, target_dir, rustflags=None):
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = target_dir
    if rustflags:
        env["RUSTFLAGS"] = rustflags
    else:
        env.pop("RUSTFLAGS", None)
    p = subprocess.run(["cargo", "build", "--offline", "--bin", util], cwd=src_dir, env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    b = os.path.join(target_dir, "debug", util)
    return p.returncode == 0 and os.path.isfile(b), b, p.stdout


def base_bin(util):
    return os.path.join(BUILD, "base", util, "debug", util)


def cov_bin(util):
    return os.path.join(BUILD, "cov", util, "debug", util)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cmd_prepare(args):
    info = {}
    for util in UTILS:
        cdir = os.path.join(TREES, util, "c")
        p = subprocess.run(["make"], cwd=cdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True)
        assert p.returncode == 0 and os.path.isfile(oracle_bin(util)), p.stdout[-2000:]
        rdir = os.path.join(TREES, util, VARIANT)
        ok, b, log = cargo_build(rdir, util, os.path.join(BUILD, "base", util))
        assert ok, log[-3000:]
        ok, cb, log = cargo_build(rdir, util, os.path.join(BUILD, "cov", util),
                                  rustflags="-C instrument-coverage")
        assert ok, log[-3000:]
        info[util] = {"oracle_sha256": sha256_file(oracle_bin(util)),
                      "baseline_sha256": sha256_file(b), "cov_sha256": sha256_file(cb),
                      "src_sha256": sha256_file(os.path.join(rdir, "src", f"{util}.rs"))}
    os.makedirs(BUILD, exist_ok=True)
    with open(os.path.join(BUILD, "prepare.json"), "w") as f:
        json.dump(info, f, indent=1)
    print(json.dumps(info, indent=1))


# ------------------------------------------------------------- baseline (validation) & coverage
def _grid_chunk(job):
    util, idxs, sut, extra_env = job
    cases = load_cases(util)
    ob = oracle_bin(util)
    out = []
    for i in idxs:
        c = cases[i]
        if extra_env:
            c.env = dict(c.env or {}, **extra_env)
        v = compare(c, ob, sut)
        out.append((i, v))
    return out


def run_grid_parallel(util, sut, jobs, extra_env=None):
    n = len(load_cases(util))
    idxs = list(range(n))
    chunks = [idxs[k::jobs] for k in range(jobs)]
    with mp.Pool(jobs) as pool:
        res = pool.map(_grid_chunk, [(util, ch, sut, extra_env) for ch in chunks])
    verdicts = [None] * n
    for part in res:
        for i, v in part:
            verdicts[i] = v
    return verdicts


def cmd_baseline(args):
    """Re-run the unmutated C2Rust baseline on the full grid with this script's capture and
    check every per-case verdict against results/<util>_diff_v4_c2rust_baseline.json
    (results/<util>_diff_v4.json for MFULL_VARIANT=rust_WIP)."""
    report = {}
    for util in UTILS:
        t0 = time.time()
        vs = run_grid_parallel(util, base_bin(util), args.jobs)
        dt = time.time() - t0
        ref = json.load(open(os.path.join(REPO, "results",
                                          f"{util}{REF_DIFF}")))["verdicts"]
        keys = ("assessable", "stdout_match", "exit_match", "stderr_match", "mismatch",
                "oracle_exit", "sut_exit")
        diffs = [v["id"] for v, r in zip(vs, ref)
                 if v["id"] != r["id"] or any(v[k] != r[k] for k in keys)]
        report[util] = {
            "n_cases": len(vs), "seconds": round(dt, 1),
            "mismatches": sum(v["mismatch"] for v in vs),
            "stderr_divergences": sum(v["stderr_divergence"] for v in vs),
            "timeouts": sum(v["timed_out"] for v in vs),
            "verdicts_differing_from_headline_baseline_file": len(diffs),
            "first_differing_ids": diffs[:10],
        }
        per_case = [{"id": v["id"], "stderr_match": v["stderr_match"], "mismatch": v["mismatch"],
                     "sut_stderr_sha": v["_sut_stderr_sha"]} for v in vs]
        with open(os.path.join(BUILD, f"baseline_{util}.json"), "w") as f:
            json.dump(per_case, f)
        print(util, json.dumps(report[util]), flush=True)
    with open(os.path.join(BUILD, "baseline_report.json"), "w") as f:
        json.dump(report, f, indent=1)


def cmd_covrun(args):
    """Run the coverage-instrumented baseline over the full grid. LLVM_PROFILE_FILE is added to
    the per-case env of BOTH sides (the C ignores it), one raw profile per process."""
    report = {}
    for util in UTILS:
        d = os.path.join(BUILD, "covraw", util)
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        t0 = time.time()
        vs = run_grid_parallel(util, cov_bin(util), args.jobs,
                               extra_env={"LLVM_PROFILE_FILE": os.path.join(d, "p-%p.profraw")})
        report[util] = {"n_cases": len(vs), "seconds": round(time.time() - t0, 1),
                        "mismatches": sum(v["mismatch"] for v in vs),
                        "profraw_files": len(os.listdir(d))}
        print(util, json.dumps(report[util]), flush=True)
    with open(os.path.join(BUILD, "covrun_report.json"), "w") as f:
        json.dump(report, f, indent=1)


# ---------------------------------------------------------------------------------- lexer/sites
MULTI = ["<<=", ">>=", "...", "..=", "::", "->", "=>", "==", "!=", "<=", ">=", "&&", "||",
         "+=", "-=", "*=", "/=", "%=", "^=", "&=", "|=", "<<", ">>", ".."]
NUM_RE = re.compile(r"(0x[0-9a-fA-F_]+|0o[0-7_]+|0b[01_]+|[0-9][0-9_]*)"
                    r"((?:i|u)(?:8|16|32|64|128|size))?")


_STR_RE = re.compile(r'(b|br|r)?(#*)"')
_LIFE_RE = re.compile(r"'[A-Za-z_][A-Za-z0-9_]*")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMTOK_RE = re.compile(r"[0-9][0-9a-zA-Z_]*(\.[0-9][0-9a-zA-Z_]*)?")
_INTTOK_RE = re.compile(r"[0-9a-zA-Z_]*")


def lex(src):
    """Minimal Rust lexer: yields (kind, text, start). Skips comments, string/char literals,
    attributes. kind in {ident, num, punct, lifetime}."""
    toks = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c.isspace():
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if src.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif src.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            continue
        if c == "#" and (src.startswith("#[", i) or src.startswith("#![", i)):
            j = src.index("[", i)
            depth, i = 1, j + 1
            while i < n and depth:
                if src[i] == "[":
                    depth += 1
                elif src[i] == "]":
                    depth -= 1
                elif src[i] == '"':
                    i = _skip_str(src, i)
                    continue
                i += 1
            continue
        m = _STR_RE.match(src, i)
        if m and (m.group(1) or c == '"'):
            if m.group(1) and "r" in m.group(1):
                hashes = m.group(2)
                j = src.index('"' + hashes, i + len(m.group(0)))
                i = j + 1 + len(hashes)
            else:
                i = _skip_str(src, i + len(m.group(0)) - 1)
            continue
        if c == "'" or (c == "b" and src.startswith("b'", i)):
            k = i + (1 if c == "'" else 2)
            if src[k] == "\\":
                j = k + 1
                while src[j] != "'":
                    j += 1
                i = j + 1
                continue
            if k + 1 < n and src[k + 1] == "'":
                i = k + 2
                continue
            m = _LIFE_RE.match(src, i)
            toks.append(("lifetime", m.group(0), i))
            i += len(m.group(0))
            continue
        if c.isalpha() or c == "_":
            m = _IDENT_RE.match(src, i)
            toks.append(("ident", m.group(0), i))
            i += len(m.group(0))
            continue
        if c.isdigit():
            m = _NUMTOK_RE.match(src, i)
            t = m.group(0)
            if m.group(1) and not (len(toks) and toks[-1][1] == "."):
                toks.append(("float", t, i))
            else:
                t = _INTTOK_RE.match(src, i).group(0)
                toks.append(("num", t, i))
            i += len(t)
            continue
        for op in MULTI:
            if src.startswith(op, i):
                toks.append(("punct", op, i))
                i += len(op)
                break
        else:
            toks.append(("punct", c, i))
            i += 1
    return toks


def _skip_str(src, i):
    """src[i] == '"'; return index after the closing quote."""
    j = i + 1
    while True:
        if src[j] == "\\":
            j += 2
            continue
        if src[j] == '"':
            return j + 1
        j += 1


def fn_spans(src, toks):
    """[(name, sig_start, body_open, body_close)] for every fn with a body."""
    spans = []
    for k, (kind, text, pos) in enumerate(toks):
        if kind == "ident" and text == "fn" and k + 1 < len(toks) and toks[k + 1][0] == "ident":
            name = toks[k + 1][1]
            depth = 0
            j = k + 2
            while j < len(toks):
                t = toks[j][1]
                if t in "([":
                    depth += 1
                elif t in ")]":
                    depth -= 1
                elif depth == 0 and t in ("{", ";"):
                    break
                j += 1
            if j >= len(toks) or toks[j][1] == ";":
                continue
            open_pos = toks[j][2]
            d = 0
            for jj in range(j, len(toks)):
                if toks[jj][1] == "{":
                    d += 1
                elif toks[jj][1] == "}":
                    d -= 1
                    if d == 0:
                        spans.append((name, pos, open_pos, toks[jj][2]))
                        break
    return spans


REL_NEG = {"==": "!=", "!=": "==", "<": ">=", "<=": ">", ">": "<=", ">=": "<"}
REL_BND = {"<": "<=", "<=": "<", ">": ">=", ">=": ">"}
ARITH_BIN = {"+": "-", "-": "+", "*": "/", "/": "*", "%": "/"}
ARITH_CMP = {"+=": "-=", "-=": "+=", "*=": "/=", "/=": "*="}
ARITH_METH = {"wrapping_add": "wrapping_sub", "wrapping_sub": "wrapping_add",
              "wrapping_mul": "wrapping_div", "wrapping_div": "wrapping_mul",
              "wrapping_rem": "wrapping_div"}


def _spaced(src, pos, length):
    return pos > 0 and src[pos - 1] in " \t\n" and pos + length < len(src) and \
        src[pos + length] in " \t\n"


def _const_variants(text):
    m = NUM_RE.fullmatch(text)
    if not m:
        return []
    body, suffix = m.group(1), m.group(2) or ""
    b = body.replace("_", "")
    if b.startswith("0x"):
        val, fmt = int(b, 16), (lambda v: "0x" + (format(v, "X") if any(
            ch.isupper() for ch in body[2:]) else format(v, "x")))
    elif b.startswith("0o"):
        val, fmt = int(b[2:], 8), (lambda v: "0o" + format(v, "o"))
    elif b.startswith("0b"):
        val, fmt = int(b[2:], 2), (lambda v: "0b" + format(v, "b"))
    else:
        val, fmt = int(b), str
    out = [("const+1", fmt(val + 1) + suffix)]
    out.append(("const-1", fmt(val - 1) + suffix if val > 0 else "(-1" + suffix + ")"))
    return out


def enumerate_mutants(src):
    toks = lex(src)
    spans = fn_spans(src, toks)

    def fn_at(pos):
        best = None
        for name, s, o, c in spans:
            if o < pos < c and (best is None or o > best[2]):
                best = (name, s, o, c)
        return best

    # C2Rust's relooper encodes goto targets as u64 labels (`current_block_N = 1075..;` and
    # `1075.. => {` arms). They are not constants of the C program: never mutate them.
    labels = set()
    for k in range(2, len(toks)):
        if toks[k][0] == "num" and toks[k - 1][1] == "=" and \
                toks[k - 2][1].startswith("current_block"):
            labels.add(toks[k][1])
    muts = []
    for k, (kind, text, pos) in enumerate(toks):
        f = fn_at(pos)
        if f is None:
            continue
        cands = []
        if kind == "punct" and text in REL_NEG and _spaced(src, pos, len(text)):
            cands.append(("rel", "rel-negate", REL_NEG[text]))
            if text in REL_BND:
                cands.append(("rel", "rel-boundary", REL_BND[text]))
        elif kind == "punct" and text in ARITH_BIN and _spaced(src, pos, len(text)):
            cands.append(("arith", "arith-binary", ARITH_BIN[text]))
        elif kind == "punct" and text in ARITH_CMP and _spaced(src, pos, len(text)):
            cands.append(("arith", "arith-compound", ARITH_CMP[text]))
        elif kind == "ident" and text in ARITH_METH and k > 0 and toks[k - 1][1] == ".":
            cands.append(("arith", "arith-method", ARITH_METH[text]))
        elif kind == "num" and not (k > 0 and toks[k - 1][1] == ".") and text not in labels:
            for sub, new in _const_variants(text):
                cands.append(("const", sub, new))
        for op, sub, new in cands:
            line = src.count("\n", 0, pos) + 1
            col = pos - (src.rfind("\n", 0, pos) + 1) + 1
            muts.append({"op": op, "subop": sub, "offset": pos, "old": text, "new": new,
                         "line": line, "col": col, "fn": f[0],
                         "context": src[src.rfind("\n", 0, pos) + 1:src.find("\n", pos)].strip()[:160]})
    return muts, spans


def apply_mutant(src, m):
    pos, old = m["offset"], m["old"]
    assert src[pos:pos + len(old)] == old, (m, src[pos:pos + len(old)])
    return src[:pos] + m["new"] + src[pos + len(old):]


# -------------------------------------------------------------------- coverage -> reachability
def load_cov(cov_json_path, util):
    """From an `llvm-cov export` JSON (LLVM 21 reading the LLVM-14 binary), return
    (fn_entry_counts by start line, code regions [(l0,c0,l1,c1,count)] for src/<util>.rs)."""
    d = json.load(open(cov_json_path))
    fname_suffix = f"/src/{util}.rs"
    regions, fn_by_line = [], {}
    for fn in d["data"][0]["functions"]:
        files = fn["filenames"]
        if not files or not files[0].endswith(fname_suffix):
            continue
        rs = [r for r in fn["regions"] if r[5] == 0 and r[7] == 0]  # file 0, code regions
        if not rs:
            continue
        first = min(rs, key=lambda r: (r[0], r[1]))
        fn_by_line[first[0]] = max(fn_by_line.get(first[0], 0), fn["count"])
        regions += [(r[0], r[1], r[2], r[3], r[4]) for r in rs]
    return fn_by_line, regions


def site_count(regions, line, col, length):
    """(count, method). Count of the innermost code region containing the token
    [col, col+length) ("containing"). rustc leaves some operator tokens in gaps between regions
    (e.g. the `- 1` after a block expression `{ .. }) - 1`); then the larger count of the nearest
    region ending before the token and the nearest region starting after it ("adjacent"; a lower
    bound on how often the operator ran)."""
    best = None
    for (l0, c0, l1, c1, cnt) in regions:
        if (l0, c0) <= (line, col) and (line, col + length) <= (l1, c1):
            size = (l1 - l0) * 100000 + (c1 - c0)
            if best is None or size < best[0]:
                best = (size, cnt)
    if best is not None:
        return best[1], "containing"
    before = [r for r in regions if (r[2], r[3]) <= (line, col)]
    after = [r for r in regions if (r[0], r[1]) >= (line, col + length)]
    cands = []
    if before:
        cands.append(max(before, key=lambda r: (r[2], r[3]))[4])
    if after:
        cands.append(min(after, key=lambda r: (r[0], r[1]))[4])
    return (max(cands), "adjacent") if cands else (None, "none")


def cmd_sites(args):
    allsel = {}
    meta = {}
    for util in UTILS:
        src = open(os.path.join(TREES, util, VARIANT, "src", f"{util}.rs")).read()
        muts, spans = enumerate_mutants(src)
        fn_by_line, regions = load_cov(args.cov_json.replace("{util}", util), util)
        # entry count of each fn span: the coverage function whose first region starts on a line
        # inside the span's signature..body-open range
        span_count = {}
        for name, s, o, c in spans:
            l_s = src.count("\n", 0, s) + 1
            l_c = src.count("\n", 0, c) + 1
            cnts = [cnt for ln, cnt in fn_by_line.items() if l_s <= ln <= l_c]
            span_count[(name, s)] = max(cnts) if cnts else None
        fn_count = {name: span_count[(name, s)] for name, s, o, c in spans}
        for m in muts:
            m["fn_count"] = fn_count.get(m["fn"])
            m["site_count"], m["site_count_method"] = site_count(regions, m["line"], m["col"],
                                                                 len(m["old"]))
        executed = [m for m in muts if (m["fn_count"] or 0) > 0]
        sel = []
        meta[util] = {"candidates_all": {}, "candidates_in_executed_fns": {}, "selected": {},
                      "functions_total": len(spans),
                      "functions_executed": sum(1 for v in fn_count.values() if (v or 0) > 0),
                      "functions_unmatched_in_coverage": sorted(
                          k for k, v in fn_count.items() if v is None)}
        for op in ("rel", "arith", "const"):
            pool = sorted([m for m in executed if m["op"] == op],
                          key=lambda m: (m["offset"], m["new"]))
            rng = random.Random(f"{SEED}:{util}:{op}")
            k = min(args.n, len(pool))
            pick = sorted(rng.sample(pool, k), key=lambda m: (m["offset"], m["new"]))
            meta[util]["candidates_all"][op] = sum(1 for m in muts if m["op"] == op)
            meta[util]["candidates_in_executed_fns"][op] = len(pool)
            meta[util]["selected"][op] = k
            sel += pick
        for j, m in enumerate(sel):
            m["util"] = util
            m["id"] = f"{util}-{m['op']}-{j:03d}"
        # the harness sanity mutant (stray stdout bytes at main_0 entry, as in mutate.py): must
        # be killed, else the harness cannot go red; not counted in any rate
        sel.append({"util": util, "id": f"{util}-sanity", "op": "sanity", "subop": "sanity",
                    "fn": "main_0", "site_count": None, "site_count_method": "n/a"})
        # bridge to the old figure: the SAME 15 relational sites per util that harness/mutate.py
        # picks (find_ror_sites(src, 15): every relational op written with spaces, evenly spaced
        # over the file, negation replacement) -- results/*_mut_v4_c2rust_baseline.json scored these
        # on the first 500 cases; here they run on the full grid. Not counted in the main rates.
        import mutate  # harness/mutate.py
        for j, (idx, old, new) in enumerate(mutate.find_ror_sites(src, 15)):
            line = src.count("\n", 0, idx) + 1
            col = idx - (src.rfind("\n", 0, idx) + 1) + 1
            fm = [sp for sp in spans if sp[2] < idx < sp[3]]
            m = {"util": util, "id": f"{util}-legacy-{j:02d}", "op": "legacy-ror",
                 "subop": "legacy-ror", "offset": idx, "old": old, "new": new, "line": line,
                 "col": col + 1, "fn": fm[-1][0] if fm else None,
                 "context": src[src.rfind("\n", 0, idx) + 1:src.find("\n", idx)].strip()[:160]}
            m["fn_count"] = fn_count.get(m["fn"])
            m["site_count"], m["site_count_method"] = site_count(regions, line, col + 1,
                                                                 len(old.strip()))
            sel.append(m)
        allsel[util] = sel
    out = {"seed": SEED, "n_per_util_per_op": args.n, "meta": meta, "mutants": allsel}
    with open(os.path.join(BUILD, "sites.json"), "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps(meta, indent=1))


# ------------------------------------------------------------------------------ mutant runner
_WORKER = {}


def _worker_init(q):
    wid = q.get()
    _WORKER["id"] = wid
    _WORKER["dir"] = os.path.join(BUILD, "w", str(wid))
    _WORKER["base_sha"] = {}


def _worker_tree(util):
    d = os.path.join(_WORKER["dir"], util)
    src_dir = os.path.join(d, "rust")
    tgt = os.path.join(d, "target")
    if not os.path.isdir(src_dir):
        shutil.copytree(os.path.join(TREES, util, VARIANT), src_dir)
    if util not in _WORKER["base_sha"]:
        ok, b, log = cargo_build(src_dir, util, tgt)
        assert ok, log[-2000:]
        _WORKER["base_sha"][util] = sha256_file(b)
    return src_dir, tgt


def _run_mutant(m):
    util = m["util"]
    src_dir, tgt = _worker_tree(util)
    path = os.path.join(src_dir, "src", f"{util}.rs")
    orig = open(path).read()
    res = {"id": m["id"], "worker": _WORKER["id"]}
    t0 = time.time()
    try:
        if m["op"] == "sanity":
            import mutate  # harness/mutate.py: inject_sanity
            msrc = mutate.inject_sanity(orig)
            if msrc is None:
                res.update(status="noncompile", error="sanity injection point (main_0) not found")
                return res
        else:
            msrc = apply_mutant(orig, m)
        with open(path, "w") as f:
            f.write(msrc)
        ok, b, log = cargo_build(src_dir, util, tgt)
        res["build_seconds"] = round(time.time() - t0, 1)
        if not ok:
            errs = [ln for ln in log.splitlines() if ln.startswith("error")]
            res.update(status="noncompile", error=(errs[0] if errs else log[-300:])[:300])
            return res
        res["tce"] = sha256_file(b) == _WORKER["base_sha"][util]
        cases = load_cases(util)
        base = json.load(open(os.path.join(BUILD, f"baseline_{util}.json")))
        ob = oracle_bin(util)
        timeouts, n_run, new_s2, new_s2_ex, changed_s2 = 0, 0, 0, [], 0
        status, first = "survived", None
        tmo = TIMEOUT
        hang_t0 = None
        for c, bv in zip(cases, base):
            if bv.get("mismatch"):
                continue  # rust_WIP only: the unmutated tree already diverges here
            v = compare(c, ob, b, tmo)
            n_run += 1
            if v["timed_out"]:
                timeouts += 1
                if timeouts >= HANG_CAP and hang_t0 is None:
                    hang_t0, tmo = time.time(), HANG_SCAN_TIMEOUT
                if hang_t0 is not None and time.time() - hang_t0 > HANG_SCAN_BUDGET:
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
            if not v["stderr_match"] and bv["stderr_match"]:
                new_s2 += 1
                if len(new_s2_ex) < 3:
                    new_s2_ex.append(v["id"])
            if v["_sut_stderr_sha"] != bv["sut_stderr_sha"]:
                changed_s2 += 1
            if hang_t0 is not None and time.time() - hang_t0 > HANG_SCAN_BUDGET:
                break
        hang = timeouts >= HANG_CAP
        n_elig = sum(1 for bv in base if not bv.get("mismatch"))
        scan_complete = n_run == n_elig or status != "survived"
        if status == "survived":
            if hang:
                status = "hang"
            elif new_s2:
                status = "killed_s2"
        res.update(status=status, first_kill=first, cases_run=n_run, timeouts=timeouts,
                   hang=hang, scan_complete=scan_complete,
                   new_stderr_divergence_cases=new_s2,
                   new_stderr_divergence_examples=new_s2_ex,
                   stderr_changed_vs_baseline_cases=changed_s2)
        return res
    finally:
        with open(path, "w") as f:
            f.write(orig)
        res["seconds"] = round(time.time() - t0, 1)


def cmd_run(args):
    sites = json.load(open(os.path.join(BUILD, "sites.json")))
    log_path = os.path.join(BUILD, "mutants.jsonl")
    done = set()
    if os.path.exists(log_path):
        for ln in open(log_path):
            done.add(json.loads(ln)["id"])
    todo = [m for u in (args.utils or UTILS) for m in sites["mutants"][u] if m["id"] not in done]
    # interleave utils so all workers stay busy; deterministic order
    todo.sort(key=lambda m: (-1 if m["op"] == "sanity" else int(m["id"].rsplit("-", 1)[1]) +
                             (10000 if m["op"] == "legacy-ror" else 0),
                             m["util"], m["op"]))
    print(f"[run] {len(todo)} mutants to run, {len(done)} already done", flush=True)
    q = mp.Queue()
    for k in range(args.jobs):
        q.put(k)
    with mp.Pool(args.jobs, initializer=_worker_init, initargs=(q,)) as pool, \
            open(log_path, "a") as log:
        for k, r in enumerate(pool.imap_unordered(_run_mutant, todo)):
            log.write(json.dumps(r) + "\n")
            log.flush()
            print(f"[{k + 1}/{len(todo)}] {r['id']} {r['status']} {r.get('seconds')}s",
                  flush=True)


# ------------------------------------------------------------------------------- summarize
def wilson(k, n, z=1.959964):
    if n == 0:
        return None
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def _rate(rows):
    n = len(rows)
    k1 = sum(r["status"] in ("killed_s1", "killed_crash") for r in rows)
    k2 = sum(r["status"] == "killed_s2" for r in rows)
    kh = sum(r["status"] == "hang" for r in rows)
    return {"compiled": n,
            "killed_s1": sum(r["status"] == "killed_s1" for r in rows),
            "killed_crash": sum(r["status"] == "killed_crash" for r in rows),
            "killed_s1_or_crash": k1,
            "kill_rate_s1_or_crash": round(k1 / n, 4) if n else None,
            "ci95_wilson_s1_or_crash": wilson(k1, n),
            "killed_s2_only": k2,
            "hang_only": kh,
            "kill_rate_incl_s2_and_hang": round((k1 + k2 + kh) / n, 4) if n else None,
            "ci95_wilson_incl_s2_and_hang": wilson(k1 + k2 + kh, n),
            "survived": sum(r["status"] == "survived" for r in rows)}


def cmd_summarize(args):
    sites = json.load(open(os.path.join(BUILD, "sites.json")))
    byid = {m["id"]: m for u in UTILS for m in sites["mutants"][u]}
    rows = {}
    for ln in open(os.path.join(BUILD, "mutants.jsonl")):
        r = json.loads(ln)
        rows[r["id"]] = dict(byid[r["id"]], **r)
    sanity = {u: rows.get(f"{u}-sanity", {}).get("status") for u in UTILS}
    allm = [rows[i] for i in byid if i in rows and byid[i]["op"] not in ("sanity", "legacy-ror")]
    legacy = [rows[i] for i in byid if i in rows and byid[i]["op"] == "legacy-ror"]
    missing = [i for i in byid if i not in rows]
    comp = [r for r in allm if r["status"] != "noncompile"]
    ext_path = os.path.join(BUILD, "extgrid.json")
    ext = json.load(open(ext_path)) if os.path.exists(ext_path) else None
    ext_kill = {}
    if ext:
        for u, e in ext["utils"].items():
            for x in e["results"]:
                ext_kill[x["id"]] = x["status"]
    for r in comp:
        if r["id"] in ext_kill:
            r["uncapped_grid_extra_cases"] = ext_kill[r["id"]]
        if r["status"] == "survived":
            if r.get("tce"):
                r["survivor_class"] = "tce-equivalent"
            elif r["site_count"] == 0:
                r["survivor_class"] = "unreached"
            elif r["site_count"] is None:
                r["survivor_class"] = "reach-unknown"
            else:
                r["survivor_class"] = "reached-not-detected"
    summ = {"overall": _rate(comp),
            "per_util": {u: _rate([r for r in comp if r["util"] == u]) for u in UTILS},
            "per_operator": {op: _rate([r for r in comp if r["op"] == op])
                             for op in ("rel", "arith", "const")},
            "per_suboperator": {s: _rate([r for r in comp if r["subop"] == s])
                                for s in sorted({r["subop"] for r in comp})},
            "per_util_operator": {f"{u}/{op}": _rate([r for r in comp
                                                      if r["util"] == u and r["op"] == op])
                                  for u in UTILS for op in ("rel", "arith", "const")}}
    surv = [r for r in comp if r["status"] == "survived"]
    classes = {}
    for r in surv:
        classes[r["survivor_class"]] = classes.get(r["survivor_class"], 0) + 1
    summ["survivor_classes"] = classes
    summ["survivors_stderr_changed_without_new_divergence"] = sum(
        1 for r in surv if r.get("stderr_changed_vs_baseline_cases", 0) > 0
        and r.get("new_stderr_divergence_cases", 0) == 0)
    summ["survivor_classes_per_util"] = {
        u: {c: sum(1 for r in surv if r["util"] == u and r["survivor_class"] == c)
            for c in sorted(classes)} for u in UTILS}
    summ["noncompile"] = {"total": len(allm) - len(comp),
                          "per_util": {u: sum(1 for r in allm if r["util"] == u and
                                              r["status"] == "noncompile") for u in UTILS},
                          "per_operator": {op: sum(1 for r in allm if r["op"] == op and
                                                   r["status"] == "noncompile")
                                           for op in ("rel", "arith", "const")}}
    # equivalence-adjusted denominators: drop trivially-equivalent (tce) survivors; then also
    # unreached survivors. Every killed mutant has coverage count > 0 (checked below).
    non_tce = [r for r in comp if not (r["status"] == "survived" and r.get("tce"))]
    summ["excluding_tce_equivalent"] = _rate(non_tce)
    summ["reached_and_not_tce"] = _rate([r for r in non_tce if (r["site_count"] or 0) > 0])
    summ["killed_with_coverage_count_0"] = sum(
        1 for r in comp if r["status"] in ("killed_s1", "killed_crash", "killed_s2", "hang")
        and r["site_count"] == 0)
    summ["outcome_partition"] = {
        "killed_s1": summ["overall"]["killed_s1"], "killed_crash": summ["overall"]["killed_crash"],
        "killed_s2_only": summ["overall"]["killed_s2_only"], "hang_only": summ["overall"]["hang_only"],
        **{"survived_" + c: n for c, n in sorted(classes.items())}, "total_compiled": len(comp)}
    # reached-only kill rate: mutants whose token the grid executes
    reached = [r for r in comp if (r["site_count"] or 0) > 0]
    summ["reached_sites_only_containing_region"] = _rate(
        [r for r in reached if r["site_count_method"] == "containing"])
    summ["reached_sites_only"] = _rate(reached)
    summ["hang_detail"] = [{k: r.get(k) for k in ("id", "timeouts", "cases_run",
                                                   "scan_complete")}
                           for r in comp if r.get("hang")]
    summ["sanity_mutants"] = sanity
    tce_path = os.path.join(BUILD, "tce_check.json")
    if os.path.exists(tce_path):
        t = json.load(open(tce_path))
        summ["tce_recheck_noincremental"] = {k: t[k] for k in (
            "tce_confirmed", "tce_total", "controls_differ", "controls_total")}
    if ext:
        summ["survivors_on_uncapped_grid"] = {
            u: {k: v for k, v in e.items() if k != "results"} for u, e in ext["utils"].items()}
        summ["survivors_on_uncapped_grid"]["by_survivor_class"] = {
            c: {"tested": sum(1 for r in surv if r["survivor_class"] == c and r["id"] in ext_kill),
                "killed": sum(1 for r in surv if r["survivor_class"] == c and
                              ext_kill.get(r["id"]) in ("killed_s1", "killed_crash"))}
            for c in sorted(classes)}
    rev_path = os.path.join(HERE, "mutate_full_review.json")
    if VARIANT == "rust" and os.path.exists(rev_path):
        import mutate_full_review as MR
        rev = json.load(open(rev_path))
        sel, _ = MR.sample(BUILD)
        sampled = [i for u in UTILS for i in sel[u]]
        for r in comp:
            if r["id"] in rev:
                r["review"] = rev[r["id"]]
        vcount = {}
        for i in sampled:
            v = rev.get(i, {}).get("verdict", "not-reviewed")
            vcount[v] = vcount.get(v, 0) + 1
        pool = [r for r in surv if r["survivor_class"] == "reached-not-detected"
                and r.get("uncapped_grid_extra_cases") not in ("killed_s1", "killed_crash")]
        summ["hand_review_of_reached_survivors"] = {
            "rule": "per util, seeded sample (random.Random(SEED:review:<util>)) of up to "
                    f"{MR.K_DEFAULT} reached-not-detected survivors not killed by the uncapped "
                    "grid; judged by hand from source (harness/mutate_full_review.json)",
            "pool_size": len(pool), "sampled": len(sampled), "verdicts": vcount,
            "categories": {c: sum(1 for i in sampled if rev.get(i, {}).get("category") == c)
                           for c in sorted({rev[i].get("category") for i in sampled
                                            if i in rev})},
            "per_util": {u: {v: sum(1 for i in sel[u] if rev.get(i, {}).get("verdict",
                                                                           "not-reviewed") == v)
                             for v in sorted(vcount)} for u in UTILS}}
        h = summ["hand_review_of_reached_survivors"]
        n_ne = vcount.get("non-equivalent", 0)
        n_nf = vcount.get("equivalent", 0) + vcount.get("repairs-defect", 0)  # not a fault vs C
        h["non_equivalent_fraction"] = round(n_ne / len(sampled), 4) if sampled else None
        h["non_equivalent_fraction_ci95_wilson"] = wilson(n_ne, len(sampled))
        # Extrapolation (an estimate, not a measurement): remove the sampled not-a-fault share of
        # the reached-not-detected survivors from the reached, non-tce denominator.
        rn = summ["reached_and_not_tce"]
        k = rn["killed_s1_or_crash"]
        lo_hi = wilson(n_nf, len(sampled)) or [0, 0]
        est = lambda frac: round(k / (rn["compiled"] - len(pool) * frac), 4)
        h["estimated_kill_rate_reached_nontce_excluding_not_a_fault"] = {
            "formula": "killed / (reached_and_not_tce.compiled - pool_size * f), f = sampled "
                       "share judged equivalent or repairs-defect",
            "f": round(n_nf / len(sampled), 4) if sampled else None,
            "point": est(n_nf / len(sampled)) if sampled else None,
            "range_from_f_ci95": [est(lo_hi[0]), est(lo_hi[1])]}
    prov_path = os.path.join(BUILD, "provenance_host.json")
    prov = json.load(open(prov_path)) if os.path.exists(prov_path) else {}
    prov.update({"script": "harness/mutate_full.py (+ mutate_full_cov.py, mutate_full_run.sh, "
                           "mutate_full_tce_check.py, mutate_full_extgrid.py)",
                 "summarized_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    for f in ("prepare.json", "baseline_report.json", "covrun_report.json"):
        fp = os.path.join(BUILD, f)
        if os.path.exists(fp):
            prov[f.replace(".json", "")] = json.load(open(fp))
    prov["selection"] = {"seed": sites["seed"], "n_per_util_per_op": sites["n_per_util_per_op"],
                         "meta": sites["meta"]}
    lc = [r for r in legacy if r["status"] != "noncompile"]
    old = {}
    for u in UTILS:
        f = os.path.join(REPO, "results", f"{u}_mut_v4_c2rust_baseline.json" if VARIANT == "rust"
                         else f"{u}_mut_v4.json")
        if os.path.exists(f):
            old[u] = json.load(open(f))["ror_summary"]
    summ["legacy_60_ror_sites_full_grid"] = {
        "note": f"the 15 sites per util harness/mutate.py selects, same tree ({VARIANT}), "
                "re-run on the full 8574-case grid (rust_WIP: on the cases where the unmutated "
                "tree agrees with the C); old scores were on the first 500 cases",
        "full_grid": _rate(lc),
        "full_grid_per_util": {u: _rate([r for r in lc if r["util"] == u]) for u in UTILS},
        "first_500_cases_old_files": old,
        "noncompile": len(legacy) - len(lc)}
    prov["variant"] = VARIANT
    wip = os.path.join(WORK, "build_rust_WIP", "cli_mutation_full_rust_WIP.json")
    wip_data = json.load(open(wip)) if VARIANT == "rust" and os.path.exists(wip) else None
    out = {"provenance": prov, "summary": summ, "missing_not_run": missing, "mutants": allm,
           "legacy_mutants": legacy}
    if wip_data:
        out["rust_WIP_reported_separately"] = wip_data
    with open(args.out, "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps(summ["overall"], indent=1))
    print(json.dumps(summ["per_util"], indent=1))
    print(json.dumps(summ["per_operator"], indent=1))
    print(json.dumps(classes))


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("prepare")
    b = sp.add_parser("baseline")
    b.add_argument("--jobs", type=int, default=8)
    c = sp.add_parser("covrun")
    c.add_argument("--jobs", type=int, default=8)
    s = sp.add_parser("sites")
    s.add_argument("--cov-json", required=True, help="llvm-cov export JSON; {util} placeholder")
    s.add_argument("-n", type=int, default=40, help="mutants per (util, operator)")
    r = sp.add_parser("run")
    r.add_argument("--jobs", type=int, default=8)
    r.add_argument("--utils", nargs="*")
    z = sp.add_parser("summarize")
    z.add_argument("--out", required=True)
    a = ap.parse_args()
    {"prepare": cmd_prepare, "baseline": cmd_baseline, "covrun": cmd_covrun,
     "sites": cmd_sites, "run": cmd_run, "summarize": cmd_summarize}[a.cmd](a)


if __name__ == "__main__":
    main()
