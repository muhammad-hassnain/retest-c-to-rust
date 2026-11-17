"""Deterministic input generator for the AdaTrans (LeetCode stdin/stdout) programs.

Program shape: each of the 104 programs is a LeetCode solution function wrapped in a main() that reads
the function's arguments from stdin (scanf) and prints the result. The input FORMAT and the argument
CONSTRAINTS of each problem are encoded in the artifact's per-problem script
dataset/C_CPP/C/constraints/weekly_contest_<c>_p<p>_fuzz.py, which draws every argument with Python's
`random` module (seeded with time.time(), so not reproducible). We keep the script's format and
constraint bounds but replace its random source with a deterministic GRID source, as in the method's
per-argument grids for functions:

  * randint(a, b) draws from the grid G(a, b) = {a, a+1, a+2, b-2, b-1, b, 0, +-1, +-2^k, +-2^k+-1
    (in range), and an even 9-point sweep of [a, b]}, or (half the time, when [a, b] is wider than
    2|G|) from an even spread over all of [a, b], so rejection loops (e.g. "distinct values") end;
  * the choice among those values for call number c of input k is the Kronecker low-discrepancy
    sequence frac(k * sqrt(p_c)) (p_c = the c-th prime), so every argument sweeps its whole range
    and the arguments are spread over their product (as the VERT multi-argument grid);
  * input 0 takes every argument at its minimum (min mode), input 1 at its maximum (max mode);
  * DETERMINISTIC cost model (no wall clock): the script's AST is rewritten so that every loop
    iteration costs 1 op, every `x in container` costs len(container) ops, and every random draw costs
    1 op. A case that exceeds OPS_BUDGET ops, or on which the script's own generator raises, is
    skipped and recorded, and generation resumes at the next index (at most 4*80 indices). Some
    scripts are quadratic (e.g. 418_2 `[a, b] in invocations`); the op budget, not the machine's
    speed, decides which of their cases are kept. A 300 s wall-clock safety net exists only to stop a
    runaway script; if it ever fires the manifest says so (`wallclock_safety_net_fired`), because the
    input set would then depend on the machine. It did not fire in our runs (see manifest);
  * choice/choices/sample/shuffle/random/uniform/randrange/getrandbits map onto the same source.
At most 80 inputs per program (the cap of the method), duplicates removed. No model is queried.

The fuzz script is third-party code: this generator EXECUTES it (with `random`, `subprocess` and file
writes replaced), so it must run inside Docker:
  docker run --rm --network none -v <repo>:/work:ro -v <out>:/out -w /work crust-heldout-plus186 \
     python3 -I systems/adatrans/gen_inputs.py --out /out
Writes <out>/<inst>/NNN.in and <out>/inputs_manifest.json (sha256 of every input).
"""
import argparse
import builtins
import glob
import hashlib
import io
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONS = os.path.join(REPO, "artifacts", "adatrans", "dataset", "C_CPP", "C", "constraints")
N_INPUTS = 80
OPS_BUDGET = 20_000_000      # per case, deterministic (see cost model above)
SAFETY_NET_S = 300           # wall clock, per case; must never fire


def _primes(n):
    out, x = [], 2
    while len(out) < n:
        if all(x % p for p in out if p * p <= x):
            out.append(x)
        x += 1
    return out


PRIMES = _primes(3000)
ALPHA = [math.sqrt(p) % 1.0 for p in PRIMES]


def splitmix(k, c):
    z = (k * 0x9E3779B97F4A7C15 + c * 0xBF58476D1CE4E5B9 + 0x94D049BB133111EB) & (2**64 - 1)
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & (2**64 - 1)
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & (2**64 - 1)
    return (z ^ (z >> 31)) / 2**64


_GRID_CACHE = {}


def grid(a, b):
    key = (a, b)
    g = _GRID_CACHE.get(key)
    if g is not None:
        return g
    s = {a, b}
    for d in (1, 2):
        s.add(a + d)
        s.add(b - d)
    for v in (0, 1, -1, 2, -2):
        s.add(v)
    k = 0
    while 2**k <= max(abs(a), abs(b)) + 1 and k < 64:
        p = 2**k
        for v in (p - 1, p, p + 1, -p + 1, -p, -p - 1):
            s.add(v)
        k += 1
    for i in range(9):
        s.add(a + (b - a) * i // 8)
    g = sorted(v for v in s if a <= v <= b)
    _GRID_CACHE[key] = g
    return g


class AbortMode(BaseException):
    pass


class WallClock(BaseException):
    pass


_OPS = [0]


def _charge(n):
    _OPS[0] += n
    if _OPS[0] > OPS_BUDGET:
        raise AbortMode()


def _tick():
    _charge(1)


def _in_chk(x, c, neg):
    if isinstance(c, (list, tuple, str, bytes)):
        _charge(len(c))
    else:
        _charge(1)
    return (x not in c) if neg else (x in c)


class _Cost(__import__("ast").NodeTransformer):
    """Charge loop iterations and container membership tests to the op budget."""

    def _loop(self, node):
        import ast
        self.generic_visit(node)
        node.body.insert(0, ast.Expr(ast.Call(ast.Name("_adt_tick", ast.Load()), [], [])))
        return node

    visit_For = visit_While = _loop

    def visit_Compare(self, node):
        import ast
        self.generic_visit(node)
        if len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn)):
            return ast.Call(ast.Name("_adt_in_chk", ast.Load()),
                            [node.left, node.comparators[0], ast.Constant(isinstance(node.ops[0], ast.NotIn))], [])
        return node


def instrument(src, path):
    import ast
    tree = _Cost().visit(ast.parse(src, path))
    ast.fix_missing_locations(tree)
    return compile(tree, path, "exec")


class GridRandom(types.ModuleType):
    """Drop-in for the `random` module functions the 104 fuzz scripts use."""

    def __init__(self):
        super().__init__("random")
        self.k = 0
        self.c = 0
        self.mode = "kron"  # min | max | kron
        self.calls = 0

    def new_case(self, k, mode):
        self.k, self.c, self.mode, self.calls = k, 0, mode, 0

    def _u(self):
        c = self.c
        self.c += 1
        _charge(1)
        if c < len(ALPHA):
            return (self.k * ALPHA[c]) % 1.0
        return splitmix(self.k, c)

    # --- the API
    def seed(self, *a, **kw):
        return None

    def randint(self, a, b):
        a, b = int(a), int(b)
        if b < a:
            raise ValueError("empty range for randint(%d, %d)" % (a, b))
        if self.mode == "min":
            _charge(1)
            return a
        if self.mode == "max":
            _charge(1)
            return b
        u = self._u()
        g = grid(a, b)
        n = b - a + 1
        if n <= 2 * len(g):
            return a + min(n - 1, int(u * n))
        if u < 0.5:
            return g[min(len(g) - 1, int(2 * u * len(g)))]
        return a + min(n - 1, int((2 * u - 1) * n))

    def randrange(self, start, stop=None, step=1):
        if stop is None:
            start, stop = 0, start
        n = (stop - start + (step - 1 if step > 0 else step + 1)) // step
        if n <= 0:
            raise ValueError("empty range for randrange")
        return start + step * self.randint(0, n - 1)

    def getrandbits(self, n):
        return self.randint(0, 2**n - 1)

    def choice(self, seq):
        seq = list(seq) if not hasattr(seq, "__getitem__") else seq
        if len(seq) == 0:
            raise IndexError("Cannot choose from an empty sequence")
        return seq[self.randint(0, len(seq) - 1)]

    def choices(self, population, weights=None, *, cum_weights=None, k=1):
        return [self.choice(population) for _ in range(k)]

    def random(self):
        if self.mode == "min":
            return 0.0
        if self.mode == "max":
            return 0.9999999
        return self._u()

    def uniform(self, a, b):
        return a + (b - a) * self.random()

    def shuffle(self, x):
        if self.mode == "min":
            return
        if self.mode == "max":
            x.reverse()
            return
        for i in range(len(x) - 1, 0, -1):
            j = self.randint(0, i)
            x[i], x[j] = x[j], x[i]

    def sample(self, population, k, *, counts=None):
        n = len(population)
        if k > n or k < 0:
            raise ValueError("Sample larger than population or is negative")
        if self.mode == "min":
            return [population[i] for i in range(k)]
        if self.mode == "max":
            return [population[n - 1 - i] for i in range(k)]
        seen, out, tries = set(), [], 0
        while len(out) < k:
            i = self.randint(0, n - 1)
            tries += 1
            if i in seen:
                if tries > 50 * (k + 1):  # fall back to the next unused index
                    i = next(j for j in range(n) if j not in seen)
                else:
                    continue
            seen.add(i)
            out.append(population[i])
        return out


class Captured(Exception):
    pass


def run_script(path, n_inputs, modes, k_base=0, case_seconds=SAFETY_NET_S):
    """Execute one fuzz script with the grid source; return the list of stdin strings it would feed
    the C program (input k = k_base + position). modes: per input index, 'min' / 'max' / 'kron'.
    A case whose generation raises or exceeds case_seconds aborts the run; the caller restarts at
    the next index."""
    src = open(path).read()
    src2, nsub = re.subn(r"(?m)^test_cases\s*=\s*\d+", f"test_cases = {n_inputs}", src)
    assert nsub == 1, path
    gr = GridRandom()
    captured = []

    def start_case():
        k = k_base + len(captured)
        gr.new_case(k, modes[k] if k < len(modes) else "kron")
        _OPS[0] = 0
        signal.setitimer(signal.ITIMER_REAL, case_seconds)

    def is_compile(argv):
        a0 = os.path.basename(str(argv[0] if isinstance(argv, (list, tuple)) else str(argv).split()[0]))
        return a0 in ("gcc", "g++", "cc", "clang", "clang++") or a0.startswith("gcc")

    def fake_run(argv, *a, **kw):
        text = kw.get("text") or kw.get("universal_newlines") or kw.get("encoding")
        if is_compile(argv):
            return subprocess.CompletedProcess(argv, 0, "" if text else b"", "" if text else b"")
        inp = kw.get("input")
        if inp is None and a:
            inp = a[0]
        captured.append(inp.decode() if isinstance(inp, bytes) else (inp or ""))
        start_case()
        out = "0\n"
        return subprocess.CompletedProcess(argv, 0, out if text else out.encode(), "" if text else b"")

    class FakePopen:
        def __init__(self, argv, *a, **kw):
            self.argv, self.text, self.returncode = argv, kw.get("text"), 0

        def communicate(self, input=None, timeout=None):
            captured.append(input.decode() if isinstance(input, bytes) else (input or ""))
            start_case()
            return ("0\n", "") if self.text else (b"0\n", b"")

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    real_open = builtins.open

    def fake_open(file, mode="r", *a, **kw):
        if any(m in mode for m in "wax+"):
            return io.StringIO() if "b" not in mode else io.BytesIO()
        return real_open(file, mode, *a, **kw)

    fake_sub = types.ModuleType("subprocess")
    for k in dir(subprocess):
        setattr(fake_sub, k, getattr(subprocess, k))
    fake_sub.run = fake_run
    fake_sub.Popen = FakePopen
    fake_os = types.ModuleType("os")
    for k in dir(os):
        setattr(fake_os, k, getattr(os, k))
    fake_os.remove = lambda *a, **kw: None
    fake_os.system = lambda *a, **kw: 0
    fake_os.makedirs = lambda *a, **kw: None
    fake_os.mkdir = lambda *a, **kw: None
    fake_os.chmod = lambda *a, **kw: None
    fake_os.path = os.path

    saved = {m: sys.modules.get(m) for m in ("random", "subprocess", "os")}
    sys.modules["random"] = gr
    sys.modules["subprocess"] = fake_sub
    sys.modules["os"] = fake_os
    g = {"__name__": "__main__", "__file__": path, "_adt_tick": _tick, "_adt_in_chk": _in_chk,
         "__builtins__": dict(builtins.__dict__, open=fake_open, print=lambda *a, **kw: None)}
    start_case()
    err = None
    try:
        exec(instrument(src2, path), g)
    except AbortMode:
        err = f"op budget exceeded ({OPS_BUDGET} ops)"
    except WallClock:
        err = "WALLCLOCK safety net fired"
    except Exception as e:  # the script's own generator failed on this case
        err = f"{type(e).__name__}: {e}"[:200]
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for m, v in saved.items():
            if v is None:
                sys.modules.pop(m, None)
            else:
                sys.modules[m] = v
    return captured, err


def _alarm(signum, frame):
    raise WallClock()


def gen_one(path):
    """Restart the script after a failed case, skipping that case index, until N_INPUTS unique inputs
    or 4*N_INPUTS case indices."""
    import time as _t
    info = {"script": os.path.relpath(path, REPO), "skipped_cases": [], "error": None}
    signal.signal(signal.SIGALRM, _alarm)
    out, seen, k_base, t0 = [], set(), 0, _t.time()
    modes = ["min", "max"]
    while len(out) < N_INPUTS and k_base < 4 * N_INPUTS:
        cap, err = run_script(path, N_INPUTS - len(out), modes, k_base)
        for s_ in cap:
            if s_ not in seen and len(out) < N_INPUTS:
                seen.add(s_)
                out.append(s_)
        if err is None:
            if len(cap) == 0:
                break
            k_base += len(cap)
            continue
        info["skipped_cases"].append({"k": k_base + len(cap), "why": err})
        k_base += len(cap) + 1
    info["k_used"] = k_base
    info["wallclock_safety_net_fired"] = any("WALLCLOCK" in x["why"] for x in info["skipped_cases"])
    info["unique"] = len(out)
    info["modes"] = [m for i, m in enumerate(modes) if i not in {x["k"] for x in info["skipped_cases"]}]
    return out, info


def _gen_worker(t):
    return gen_one(t[1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--jobs", type=int, default=3)
    a = ap.parse_args()
    man = {"generator": "systems/adatrans/gen_inputs.py", "n_inputs_cap": N_INPUTS, "programs": {}}
    os.chdir(tempfile.mkdtemp())
    todo = []
    for path in sorted(glob.glob(os.path.join(CONS, "weekly_contest_*_fuzz.py"))):
        m = re.search(r"weekly_contest_(\d+)_p(\d+)_fuzz", path)
        inst = f"C_{m.group(1)}_{m.group(2)}"
        if a.only and inst not in a.only:
            continue
        todo.append((inst, path))
    import multiprocessing as mp
    with mp.get_context("fork").Pool(a.jobs, maxtasksperchild=1) as pool:
        res = pool.map(_gen_worker, todo, chunksize=1)
    for (inst, path), (inputs, info) in zip(todo, res):
        d = os.path.join(a.out, inst)
        os.makedirs(d, exist_ok=True)
        hs = []
        for i, s in enumerate(inputs):
            b = s.encode()
            with builtins.open(os.path.join(d, f"{i:03d}.in"), "wb") as f:
                f.write(b)
            hs.append(hashlib.sha256(b).hexdigest())
        info["sha256"] = hs
        info["n"] = len(inputs)
        man["programs"][inst] = info
        print(inst, info["n"], info["modes"], len(info["skipped_cases"]), flush=True)
    with builtins.open(os.path.join(a.out, "inputs_manifest.json"), "w") as f:
        json.dump(man, f, indent=1)


if __name__ == "__main__":
    main()
