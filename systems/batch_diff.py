"""Shared batched differential execution for function-level systems (VERT/Flourine/RustAssure).

Both the C oracle and the Rust SUT are compiled as drivers that read one test case per stdin line
and print one result line. We run each binary ONCE over all cases (fast), then classify each case:
  agree | value (both ran, different answer) | rust_panic (C ran, Rust panicked)
  | ub (C executes undefined behaviour on this input) | timeout | skip (parse marker)

Audit 8 uniform rule: a candidate whose sanitizer run fails to start, times out or prints another
value than the oracle is `san_unresolved` (not counted, not UB); the -O0 cross-check is per input.
Undefined behaviour is detected PROPERLY when a sanitizer-instrumented oracle `binc_san` is passed:
every candidate divergence is re-run on that input through the sanitized C, and any case where the
C trips ASan/UBSan is reclassified `ub` -- so a divergence that only exists where the original C
reads/writes out of bounds (silent UB) is NOT counted as a translation bug, and a Rust panic on such
an input is not called "wrong". Without a sanitizer we fall back to crash-only UB detection (the C
produced no line). `rust_panic` (Rust panics where sanitizer-clean C returns) is kept SEPARATE from
`value` (wrong answer). Samples are the SMALLEST-magnitude diverging inputs, so the evidence matches
the claim rather than reflecting grid order.
"""
import re
import subprocess

SAN_RE = re.compile(r"runtime error|AddressSanitizer|UndefinedBehaviorSanitizer|Sanitizer")


def run_batch(binp, cases, timeout=12):
    inp = ("\n".join(" ".join(str(x) for x in row) for row in cases) + "\n").encode()
    try:
        p = subprocess.run([binp], input=inp, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout)
        return p.returncode, p.stdout.decode("latin1").splitlines()
    except subprocess.TimeoutExpired:
        return -1, []


def run_one(binp, row, timeout=2):
    """Run a binary on ONE input line. Returns (first_line|None, rc, timed_out, stderr)."""
    inp = (" ".join(str(x) for x in row) + "\n").encode()
    try:
        p = subprocess.run([binp], input=inp, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout)
        out = p.stdout.decode("latin1").splitlines()
        return (out[0] if out else None), p.returncode, False, p.stderr.decode("latin1", "replace")
    except subprocess.TimeoutExpired:
        return None, -1, True, ""



# A sanitizer runtime that fails to START (e.g. clang-14 MemorySanitizer under a kernel with high
# mmap ASLR entropy: "FATAL: Code 0x... is out of application range", or a bare SIGSEGV before
# main) says nothing about the C program. Audit 3 found such start-up crashes being counted as
# original-C UB. Retry them; if they persist, report "infra" (counted, NOT treated as UB).
INFRA_RE = re.compile(r"out of application range|can not mmap|cannot mmap|Shadow memory range interleaves|"
                      r"FATAL: (Code|MemorySanitizer|AddressSanitizer)")


def san_run(binp, row, timeout=4, retries=12):
    """(verdict, first_line) for one sanitizer run of the C on one input; verdict is
    'ub' | 'clean' | 'timeout' | 'infra'."""
    for _ in range(retries):
        line, rc, to, err = run_one(binp, row, timeout=timeout)
        if to:
            return "timeout", None
        if INFRA_RE.search(err) or (rc < 0 and not SAN_RE.search(err)):
            continue                        # sanitizer never got to run the program; retry
        return ("ub" if (rc != 0 or SAN_RE.search(err)) else "clean"), line
    return "infra", None


def san_verdict(binp, row, timeout=4, retries=12):
    """'ub' | 'clean' | 'timeout' | 'infra' for one sanitizer run of the C on one input."""
    return san_run(binp, row, timeout, retries)[0]

def eval_cases(binc, binr, cases):
    """Return [(row, c_line|None, r_line|None)] aligned to cases."""
    _, cl = run_batch(binc, cases)
    _, rl = run_batch(binr, cases)
    if len(cl) == len(cases) and len(rl) == len(cases):
        return list(zip(cases, cl, rl))
    out = []
    for k in range(0, len(cases), 32):
        chunk = cases[k:k + 32]
        _, c = run_batch(binc, chunk, timeout=3)
        _, r = run_batch(binr, chunk, timeout=3)
        if len(c) == len(chunk) and len(r) == len(chunk):
            out.extend(zip(chunk, c, r)); continue
        # A crash or hang on one input truncates the rest of a batch. Re-run this chunk one input
        # per process so every input gets its own verdict (audit 4: whole chunks were lost and a
        # Rust hang was recorded as a panic). A per-input hang is marked "__TIMEOUT__".
        for row in chunk:
            cl, _, cto, _ = run_one(binc, row, timeout=3)
            rl, _, rto, _ = run_one(binr, row, timeout=3)
            out.append((row, "__TIMEOUT__" if cto else cl, "__TIMEOUT__" if rto else rl))
    return out


def lines_aligned(binp, cases):
    """Per-case output of ONE binary, aligned to cases (eval_cases' chunk / per-input fallback)."""
    return [c for (_, c, _) in eval_cases(binp, binp, cases)]


def _magnitude(row):
    return sum(abs(x) for x in row)


def tally(binc, binr, cases, binc_san=None, binc_alt=None, binc_msan=None):
    """Run + classify; return counts and up to 3 SMALLEST value/crash samples.

    Every candidate divergence is filtered so it isn't miscounted as a translation bug:
      * binc_san (ASan+UBSan, incl. signed-integer-overflow): if the C trips it on this input, the
        divergence only exists where the ORIGINAL C executes undefined behaviour (OOB read/write,
        signed overflow, ...) -> `ub`. A hang -> `timeout`.
      * binc_msan (clang MemorySanitizer, when available): precisely detects use of UNINITIALIZED /
        VLA memory the ORIGINAL C reads on this input -> `ub`. This is the rigorous version of:
      * binc_alt (the SAME C at a different optimization level): a well-defined C program is
        `-O`-invariant, so binc(-O1) != binc_alt(-O0) on an input means opt-dependent output, i.e.
        uninitialized/VLA UB. Used as a fallback where clang/MSan is unavailable.
    `rust_panic` (Rust panics on sanitizer-clean, well-defined C) is kept SEPARATE from `value`."""
    out = {"cases": len(cases), "value": 0, "rust_panic": 0, "ub": 0, "timeout": 0, "agree": 0,
           "value_samples": [], "crash_samples": []}
    # Opt-level cross-check, done in ONE batch (cheap): a well-defined C program is -O-invariant,
    # so any case where the -O1 and -O0 oracles disagree is UB (uninitialized/VLA read) the
    # sanitizer can't see -> exclude it up front.
    # per-input aligned (audit 8): a crash or hang of the -O0 build on one input no longer
    # truncates the batch, so every input is compared with its own -O0 line
    altl = lines_aligned(binc_alt, cases) if binc_alt is not None else None
    cands = []   # (row, cl, rl, tag)
    for i, (row, cl, rl) in enumerate(eval_cases(binc, binr, cases)):
        if cl == "__PARSE__" or rl == "__PARSE__":
            continue
        if cl is not None and cl != "__TIMEOUT__" and cl == rl:
            out["agree"] += 1; continue        # agreement needs no UB filtering (audit 5)
        if altl is not None and cl is not None and cl != "__TIMEOUT__":
            alt = altl[i] if i < len(altl) else None
            if alt != "__PARSE__" and alt != cl:
                out["ub"] += 1; continue       # optimization-dependent output (or -O0 crash/hang) -> UB
        if cl is None or cl == "__TIMEOUT__":
            cands.append((row, None, rl, "c_missing")); continue
        if rl == "__TIMEOUT__":
            cands.append((row, cl, rl, "hang")); continue   # sanitizer-filtered like the rest (audit 5)
        if rl is None or rl == "__PANIC__":
            cands.append((row, cl, rl, "crash")); continue
        cands.append((row, cl, rl, "value"))
    vs, cs = [], []
    out["san_unresolved"] = 0
    for row, cl, rl, tag in cands:
        # Audit 8 uniform rule: a candidate counts only if every sanitizer run of the C on this input
        # ran to completion and agrees with the primary oracle. A sanitizer timeout or start-up
        # failure ("infra"), or a sanitizer build that prints another value, leaves the input NOT
        # UB-clean: `san_unresolved`, neither counted as a translation bug nor called UB. (Before,
        # a timeout or infra run left the divergence counted.)
        verdicts = []
        for sbin in (binc_san, binc_msan):
            if sbin is not None:
                v, sline = san_run(sbin, row)
                if v == "infra":
                    out["san_infra"] = out.get("san_infra", 0) + 1
                verdicts.append((v, sline))
                if v == "ub":
                    break
        if any(v == "ub" for v, _ in verdicts):
            out["ub"] += 1; continue          # the C executes UB (ASan/UBSan, or MSan uninitialized read)
        if any(v in ("infra", "timeout") or (tag != "c_missing" and sl != cl) for v, sl in verdicts):
            out["san_unresolved"] += 1; continue
        if tag == "c_missing":
            # no sanitizer, or sanitizer clean but C gave no batch line -> UB/timeout, not a bug
            out["ub" if binc_san is None else "timeout"] += 1; continue
        if tag == "hang":
            out["rust_hang"] = out.get("rust_hang", 0) + 1; continue   # reported, not a panic
        if tag == "crash":
            out["rust_panic"] += 1; cs.append((row, cl))
        else:
            out["value"] += 1; vs.append((row, cl, rl))
    vs.sort(key=lambda t: _magnitude(t[0]))
    cs.sort(key=lambda t: _magnitude(t[0]))
    out["value_samples"] = [{"in": r, "c": cl, "rust": rl} for r, cl, rl in vs[:3]]
    out["crash_samples"] = [{"in": r, "c": cl} for r, cl in cs[:3]]
    # back-compat alias
    out["rust_crash"] = out["rust_panic"]
    return out
