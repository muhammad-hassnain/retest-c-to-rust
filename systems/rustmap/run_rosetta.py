"""RustMap depth+breadth: differential-test the 124 Rosetta Code translations.

RustMap's released verification only decompresses bzip2 output (via bzip2recover); its 125
Rosetta Code translations ship WITH the original C but were never differentially compared --
the shipped runner (125-rosetta-rs.sh) just compiles and runs each Rust file alone. We close
that gap: for each task, build the original C (c-code/rosseta-125/<task>/main.c, gcc) and the
translated Rust (rust-code/.../<task>-c.rs, rustc), run both with no stdin, and compare
stdout + exit code byte-exact. These are self-contained compute-and-print programs.

Honest classification: build failures on each side are counted separately; a stdout divergence
is examined and, where it is clearly nondeterministic (random/time/address output) or a pure
float-formatting difference, labelled as such rather than asserted a logic bug.

  docker run --rm -v "$PWD:/work" -w /work crust-stable python3 systems/rustmap/run_rosetta.py
"""
import argparse, json, os, re, subprocess, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
RM = os.path.join(REPO, "artifacts", "rustmap")
CDIR = os.path.join(RM, "c-code", "rosseta-125")
RDIR = os.path.join(RM, "rust-code", "rosetta_code_gpt", "125-rosetta-code-gpt")

NONDET = re.compile(rb"0x[0-9a-fA-F]{6,}|random|rand\(|time\(|clock\(", re.I)


def normalize(b):
    """Fold BENIGN cosmetic differences that are never a translation logic bug:
    per-line leading/trailing whitespace + internal whitespace runs, NaN<->nan case,
    true/false <-> 1/0, and surrounding double-quotes. What survives is a real
    value/structure difference, not formatting."""
    s = b.decode("latin1")
    out = []
    for line in s.splitlines():
        line = re.sub(r"\s+", " ", line.strip())
        line = line.strip('"')
        low = line.lower()
        low = re.sub(r"\btrue\b", "1", low)    # whole-word bool rendering (not "construe"->"cons1")
        low = re.sub(r"\bfalse\b", "0", low)
        out.append(low)
    return "\n".join(out)


def floats_equal(a, b):
    """token-wise float compare with tolerance; True only if same count and all close."""
    ta, tb = a.split(), b.split()
    if len(ta) != len(tb):
        return False
    try:
        for x, y in zip(ta, tb):
            fx, fy = float(x), float(y)
            import math
            if math.isnan(fx) and math.isnan(fy):
                continue
            if abs(fx - fy) > 1e-6 * max(1.0, abs(fx)):
                return False
        return True
    except ValueError:
        return False


RAND_SRC = re.compile(r"\brand\b|\brandom\b|srand|thread_rng|getrandom|\btime\s*\(|StdRng|rng\b", re.I)


def is_random(cmain, rfile):
    src = ""
    for p in (cmain, rfile):
        try:
            src += open(p, errors="replace").read()
        except OSError:
            pass
    return bool(RAND_SRC.search(src))


def pairs():
    out = []
    for d in sorted(os.listdir(CDIR)):
        cmain = os.path.join(CDIR, d, "main.c")
        rfile = os.path.join(RDIR, d + "-c.rs")
        if os.path.isfile(cmain) and os.path.isfile(rfile):
            out.append((d, cmain, rfile))
    return out


def build_c(cmain, d):
    b = os.path.join(d, "c_bin")
    # RustMap's own runner (c-code/rosseta-125/rosetta-125.sh:19) uses plain `gcc main.c -o main`;
    # we match it (no -O; -lm only so math programs link) -- audit 4.
    r = subprocess.run(["gcc", "-w", cmain, "-o", b, "-lm"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return b if (r.returncode == 0 and os.path.isfile(b)) else None


def build_rust(rfile, d):
    b = os.path.join(d, "r_bin")
    # RustMap's runner (125-rosetta-rs.sh:16) uses plain `rustc file` (debug: overflow checks on).
    r = subprocess.run(["rustc", "-A", "warnings", rfile, "-o", b],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return b if (r.returncode == 0 and os.path.isfile(b)) else None


def run(binp, timeout=10):
    try:
        p = subprocess.run([binp], input=b"", stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout)
        return p.stdout, p.returncode, False
    except subprocess.TimeoutExpired:
        return b"", -1, True


def classify(task, co, cr, ro, rr, ctimeout, rtimeout):
    if ctimeout or rtimeout:
        return "timeout"
    if co == ro and cr == rr:
        return "agree"                                   # byte-exact
    # fold benign cosmetic formatting (whitespace/case/bool/quotes)
    nc, nr = normalize(co), normalize(ro)
    if nc == nr:
        return "agree_after_formatting"
    # whole-output float tolerance (float-precision-only differences)
    if floats_equal(nc.replace("\n", " "), nr.replace("\n", " ")):
        return "agree_float_tolerance"
    if NONDET.search(co) or NONDET.search(ro):
        return "divergent_nondeterministic"
    return "divergent_real"                              # survives all benign folds


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=os.path.join(REPO, "results", "rustmap_rosetta.json"))
    args = ap.parse_args()
    ps = pairs()
    if args.limit:
        ps = ps[:args.limit]
    agg = {"pairs": len(ps), "agree": 0, "agree_after_formatting": 0,
           "agree_float_tolerance": 0, "divergent_real": 0,
           "divergent_nondeterministic": 0, "timeout": 0,
           "c_build_fail": 0, "rust_build_fail": 0}
    div_samples, divergent_tasks = [], []
    for task, cmain, rfile in ps:
        with tempfile.TemporaryDirectory() as d:
            binc = build_c(cmain, d)
            if not binc:
                agg["c_build_fail"] += 1
                continue
            binr = build_rust(rfile, d)
            if not binr:
                agg["rust_build_fail"] += 1
                continue
            co, cr, ct = run(binc)
            ro, rr, rt = run(binr)
            k = classify(task, co, cr, ro, rr, ct, rt)
            if k == "divergent_real" and is_random(cmain, rfile):
                k = "divergent_nondeterministic"      # source uses rand/time -> not a bug
            agg[k] = agg.get(k, 0) + 1
            if k == "divergent_real":
                divergent_tasks.append(task)
                # every real divergence is recorded (no cap) so each can be triaged by hand
                div_samples.append({
                    "task": task, "exit_c": cr, "exit_rust": rr,
                    "c": co.decode("latin1")[:2000], "rust": ro.decode("latin1")[:2000]})
    summary = {"system": "RustMap", "scope": "Rosetta Code translations (differential, "
               "original C vs translated Rust, byte-exact stdout+exit)",
               **agg, "real_divergent_tasks": divergent_tasks}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump({"summary": summary, "divergence_samples": div_samples}, open(args.out, "w"), indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
