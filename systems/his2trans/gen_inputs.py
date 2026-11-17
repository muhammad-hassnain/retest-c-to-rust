"""Deterministic input generator for the His2Trans re-grade (function-level drivers).

No model and no randomness at test time: every case is a fixed function of this file. Shapes
follow the paper's method (method.tex, "Inputs"):
  * per-argument grids for function arguments (small values, range boundaries, powers of two +-1,
    an even sweep), at most 80 cases per API function; several arguments are spread over the
    product with the low-discrepancy R_d sequence of systems/enumerate_inputs.py (reused, unchanged);
  * for stateful APIs (buffer, quadtree, avl) the "flag pairs" analogue: every ordered pair of
    mutating operations, crossed with a fixed corpus of initial states;
  * class "own": the call sequences of the system's own RQ2 Rust tests, transcribed from
    data/paper_artifacts/.../tests/source_rq2_tests/<project>/c2r/test.rs (as adapted by the
    artifact's run_oss_rq2_rust_tests.py). They are run through the same comparison.

A case = {"id", "project", "func", "cls", "lines"}; "lines" is the driver script (drivers/common.h).
`python3 gen_inputs.py` prints per-project counts.
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE)))          # systems/
from enumerate_inputs import grid_for, _spread  # noqa: E402

CAP = 80


def hx(b):
    if isinstance(b, str):
        b = b.encode()
    return b.hex() if b else "~"


def hd(d):
    return struct.pack(">d", d).hex()


def cap_list(xs, n=CAP):
    """Deterministic thinning to <= n, keeping first and last."""
    if len(xs) <= n:
        return list(xs)
    step = len(xs) / n
    out = [xs[int(i * step)] for i in range(n)]
    if xs[-1] not in out:
        out[-1] = xs[-1]
    return out


def lcg_values(n, seed, lo, hi):
    """Fixed linear congruential sequence (deterministic stand-in for 'random' data)."""
    x, out = seed, []
    for _ in range(n):
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        out.append(lo + x % (hi - lo + 1))
    return out


INT_MIN, INT_MAX = -2**31, 2**31 - 1


def int_grid(lo, hi, n):
    return grid_for(lo, hi, n)


def double_grid(lo=-1e6, hi=1e6):
    vals = [0.0, -0.0, 1.0, -1.0, 0.5, -0.5, 0.25, 0.1, -0.1, 0.001, 1e-9, 2.0, 3.0, 7.0, 10.0, -10.0,
            15.0, -15.0, 15.000001, -15.000001, 14.99, -14.99, 45.0, -45.0, 45.0000001, -45.0000001,
            100.0, -100.0, 1e3, 1e6, -1e6, 1e300, -1e300, 5e-324, float("inf"), float("-inf"),
            float("nan"), 2.0**31, -2.0**31, 2.0**31 - 1, 2.0**53, 0.75, 0.9, 1.5, 4.0, 1023.0,
            1025.0, 63.0, 65.0, 0.3, -0.3, 0.007, 3.14159, -2.71828]
    # even sweep over [-50, 50]
    vals += [-50.0 + 100.0 * i / 25 for i in range(26)]
    seen, out = set(), []
    for v in vals:
        k = hd(v)
        if k not in seen:
            seen.add(k)
            out.append(v)
    return out[:CAP]


STR_CORPUS = [b"", b"a", b"A", b" ", b"\t", b"ab", b"abc", b"hello", b"hello world", b"  hello  ",
              b"  hello", b"hello  ", b"\n\t \r", b"xyz", b"world", b"0", b"123", b"-1", b"%s", b"%d",
              b"a b c d e f", b"\x7f", b"\x80", b"\xff", b"\xc3\xa9t\xc3\xa9", b"\x01\x02\x03",
              b"key=value", b"x" * 63, b"x" * 64, b"x" * 65, b"y" * 1023, b"y" * 1024, b"y" * 1025,
              b"ab" * 100, b"The quick brown fox jumps over the lazy dog", b"\xe4\xb8\xad\xe6\x96\x87",
              b"tab\there", b"line\nbreak", b"\x0b\x0c", b"end "]


def case(cases, project, func, cls, lines):
    cases.append({"id": f"{project}/{func}/{cls}/{len(cases):05d}", "project": project, "func": func,
                  "cls": cls, "lines": lines})


# ---------------------------------------------------------------- qsort
def gen_qsort():
    cases = []
    sizes = [1, 2, 3, 4, 5, 7, 8, 9, 16, 17, 31, 32, 33, 64, 100, 1000]
    pats = ["sorted", "reverse", "equal", "alt", "saw", "extreme", "lcg", "negmix"]

    def arr(n, p):
        if p == "sorted":
            return list(range(n))
        if p == "reverse":
            return list(range(n, 0, -1))
        if p == "equal":
            return [7] * n
        if p == "alt":
            return [(i % 2) * 10 - 5 for i in range(n)]
        if p == "saw":
            return [i % 5 for i in range(n)]
        if p == "extreme":
            base = [INT_MIN, INT_MAX, 0, -1, 1, INT_MIN + 1, INT_MAX - 1]
            return [base[i % len(base)] for i in range(n)]
        if p == "negmix":
            return [(-1) ** i * i for i in range(n)]
        return lcg_values(n, n * 31 + 7, -1000, 999)

    rk = ["full", "inner", "tail", "head", "single", "inverted"]

    def rng(n, k):
        if n == 1 or k == "full":
            return 0, n - 1
        if k == "inner":
            return n // 4, max(n // 4, (3 * n) // 4 - 1)
        if k == "tail":
            return 1, n - 1
        if k == "head":
            return 0, n - 2
        if k == "single":
            return n // 2, n // 2
        return n - 1, 0                       # low > high: a no-op for quickSort
    idx = _spread([list(range(len(sizes))), list(range(len(pats))), list(range(len(rk)))], CAP)
    for func, op in (("quickSort", "qs"), ("partition", "part")):
        for si, pi_, ri in idx:
            n, p = sizes[si], pats[pi_]
            a = arr(n, p)
            lo, hi = rng(n, rk[ri])
            case(cases, "qsort", func, "grid", [f"{op} {n} {' '.join(map(str, a))} {lo} {hi}"])
    own = [[3, 1, 2], [5, 4, 3, 2, 1], [0, -1, 5, -3, 2], [7, 7, 7, 7], [42], [1, 2, 3, 4, 5]]
    for a in own:
        case(cases, "qsort", "quickSort", "own", [f"qs {len(a)} {' '.join(map(str, a))} 0 {len(a) - 1}"])
    return cases


# ---------------------------------------------------------------- ht
def gen_ht():
    cases = []
    strs = [s for s in STR_CORPUS] + [bytes([b]) for b in (1, 31, 32, 64, 127, 128, 129, 191, 192, 254, 255)]
    strs += [b"k%d" % i for i in range(0, 1000, 37)]
    seen, uniq = set(), []
    for s in strs:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    for s in cap_list(uniq):
        case(cases, "ht", "hash_key", "grid", [f"hash {hx(s)}"])
    # own: the RQ2 test only calls the translated (empty) main(); hash_key is never called.
    return cases


# ---------------------------------------------------------------- rgba
NAMED = ["transparent", "aliceblue", "aqua", "black", "blue", "brown", "coral", "crimson", "cyan",
         "darkgray", "fuchsia", "gold", "gray", "green", "grey", "indigo", "lime", "magenta", "maroon",
         "navy", "olive", "orange", "pink", "purple", "red", "silver", "teal", "white", "yellow",
         "yellowgreen"]


def gen_rgba():
    cases = []
    # red channel < 128 where possible: the C computes r << 24 in int, which is UB for r >= 128, so
    # such inputs are excluded by the UB rule; the UB-clean variants below keep hex3, hex6 and the
    # clamp (> 255) paths observable on g/b.
    fs = ([n for n in NAMED[:10]] +
          ["#123", "#7a5", "#0F7", "#456789", "rgb(0,300,0)", "rgb(1,2,999)", "rgba(0,256,1,.5)", "rgb(127,128,129)"] +
          ["#000", "#fff", "#FFF", "#f84", "#7f7f7f", "#800000", "#ff8040", "#6496c8", "#123456", "#abcdef",
           "#ABCDEF", "#gg0000", "#", "#1", "#12", "#1234", "#12345", "#1234567", "#7fffff", "#00ff00",
           "rgb(0,0,0)", "rgb(255, 128, 64)", "rgb(127,127,127)", "rgb( 1 , 2 , 3 )", "rgb(300,0,0)",
           "rgb(1000000000,0,0)", "rgb(1,2)", "rgb(a,b,c)", "rgb(", "rgb", "rgbx", "rgb(0,0,0", "rgb(10 20 30)",
           "rgba(0,0,0,0)", "rgba(255, 128, 64, 0.0)", "rgba(127,0,0,.5)", "rgba(1,2,3,0.25)", "rgba(1,2,3,1)",
           "rgba(1,2,3,.999)", "rgba(1,2,3,0.1234)", "rgba(1,2,3,2)", "rgba(1,2,3,.7)", "rgba(1,2,3,.29)",
           "rgba(1,2,3,.05)", "rgba(1,2,3)", "rgba(1,2,3, )", "rgba", "rgbax", "rgba(", "", " ", "RED",
           "red ", " red", "not-a-color", "Red", "rgba(127,127,127,0.5)", "rgba(0,0,0,0.3)", "rgba(9,9,9,.01)"])
    for s in cap_list(fs):
        case(cases, "rgba", "rgba_from_string", "grid", [f"fs {hx(s)}"])
    u32 = cap_list(int_grid(0, 2**32 - 1, CAP))
    for v in u32:
        case(cases, "rgba", "rgba_new", "grid", [f"new {v}"])
    for v in u32:
        case(cases, "rgba", "rgba_inspect", "grid", [f"insp {v}"])
    ts = []
    for v in int_grid(0, 2**32 - 1, 50):
        r, g, b, a = (v >> 24) / 255, (v >> 16 & 255) / 255, (v >> 8 & 255) / 255, (v & 255) / 255
        ts.append((r, g, b, a))
    for comp in (1.5, -0.5, 2.0, 1.0 / 255 * 0.5, 0.999, 1.0000001, -1.0, 255.0):
        ts.append((comp, 0.5, 0.25, 1.0))
        ts.append((0.2, comp, 0.4, 0.5))
    for a in (0.125, 0.375, 0.005, 0.995, 0.5, 0.0, 0.015, 0.625, 1.0 - 1e-16, -0.0):
        ts.append((0.1, 0.2, 0.3, a))
    for t in cap_list(ts):
        case(cases, "rgba", "rgba_to_string", "grid", ["ts " + " ".join(hd(x) for x in t)])
    # own (RQ2 test.rs)
    for s in ["#ff8040", "#f84", "rgb(255, 128, 64)", "rgba(255, 128, 64, 0.0)", "red", "#6496c8", "#ff0000",
              "#00ff00", "#0000ff", "not-a-color"]:
        case(cases, "rgba", "rgba_from_string", "own", [f"fs {hx(s)}"])
    for v in (0xFF804020, 0xFF8040FF):
        r, g, b, a = (v >> 24) / 255, (v >> 16 & 255) / 255, (v >> 8 & 255) / 255, (v & 255) / 255
        case(cases, "rgba", "rgba_to_string", "own", [f"new {v}", "ts " + " ".join(hd(x) for x in (r, g, b, a))])
    return cases


# ---------------------------------------------------------------- genann
def gen_genann():
    cases = []
    dg = double_grid()
    for func, op in (("genann_act_sigmoid", "sig"), ("genann_act_sigmoid_cached", "sigc"),
                     ("genann_act_threshold", "thr"), ("genann_act_linear", "lin")):
        for d in dg:
            case(cases, "genann", func, "grid", [f"{op} {hd(d)}"])
    shape_grid = [-2, -1, 0, 1, 2, 3, 4, 5, 7, 8, 9, 15, 16, 17, 31, 32, 33, 63, 64]
    shapes = _spread([shape_grid] * 4, CAP)
    for i, hl, h, o in shapes:
        case(cases, "genann", "genann_init", "grid", [f"init {i} {hl} {h} {o}"])
    small = [s for s in shapes if s[0] >= 1 and s[1] >= 0 and s[3] >= 1 and (s[1] == 0 or s[2] >= 1)]
    small = [s for s in small if s[0] * max(s[2], 1) * (s[1] + 1) * s[3] <= 20000]
    for i, hl, h, o in small[:20]:
        case(cases, "genann", "genann_randomize", "grid", [f"randz {i} {hl} {h} {o}"])
    nets = [(1, 0, 0, 1), (2, 0, 0, 1), (2, 1, 2, 1), (2, 1, 3, 1), (3, 2, 4, 2), (4, 3, 5, 2), (1, 1, 1, 1),
            (5, 1, 8, 3), (2, 2, 2, 2), (8, 1, 16, 1)]
    scales = [1.0, 0.1, 5.0, 40.0]
    xs = [0.0, 0.5, 1.0, -1.0, 0.25, 3.0, -2.5, 100.0]
    run_idx = _spread([list(range(len(nets))), [0, 1, 2, 3], [0, 1, 2, 3], list(range(len(scales))),
                       list(range(len(xs)))], CAP)
    for ni, ah, ao, si, xi in run_idx:
        i, hl, h, o = nets[ni]
        x = [xs[(xi + k) % len(xs)] for k in range(i)]
        case(cases, "genann", "genann_run", "grid",
             [f"run {i} {hl} {h} {o} {ah} {ao} {hd(scales[si])} " + " ".join(hd(v) for v in x)])
    lrs = [0.1, 1.0, 3.0, 0.01]
    reps = [1, 2, 10]
    tr_idx = _spread([list(range(len(nets))), [0, 1, 3], [0, 1, 3], list(range(len(scales))),
                      list(range(len(lrs))), list(range(len(reps)))], CAP)
    for ni, ah, ao, si, li, ri in tr_idx:
        i, hl, h, o = nets[ni]
        x = [xs[(si + k) % len(xs)] for k in range(i)]
        t = [[0.0, 1.0, 0.5][(k + li) % 3] for k in range(o)]
        case(cases, "genann", "genann_train", "grid",
             [f"train {i} {hl} {h} {o} {ah} {ao} {hd(scales[si])} {hd(lrs[li])} {reps[ri]} "
              + " ".join(hd(v) for v in x + t)])
    for k, (i, hl, h, o) in enumerate(nets):
        case(cases, "genann", "genann_copy", "grid", [f"copy {i} {hl} {h} {o} {hd(scales[k % 4])}"])
        case(cases, "genann", "genann_write", "grid", [f"write {i} {hl} {h} {o} {hd(scales[k % 4])}"])
    reads = [b"2 1 2 1 " + b" ".join(b"%.3f" % (0.1 * k - 0.3) for k in range(9)),
             b"1 0 0 1 0.5 -0.5", b"2 0 0 1 1 2 3", b"2 0 0 1 1 2", b"", b"1 0 0", b"x", b"1 0 0 1 a b",
             b"3 1 2 2 " + b" ".join(b"%d" % k for k in range(14)), b"1 0 0 1 1e308 -1e308",
             b"1 0 0 1 inf nan", b"  1   0   0   1   7   8  ", b"1 0 0 1 0x10 2", b"4 3 5 2 " + b"0.5 " * 97,
             b"1 0 0 1 1.5e-3 2.5E2", b"2 1 1 1 1 2 3 4 5", b"1 1 1 1 0 0 0 0"]
    for r in reads:
        case(cases, "genann", "genann_read", "grid", [f"read {hx(r)}"])
    # own (RQ2 test.rs)
    for op, v in (("sig", 0.0), ("sig", 10.0), ("sig", -10.0), ("thr", 1.0), ("thr", -1.0), ("thr", 0.0),
                  ("lin", 5.0), ("lin", -3.0), ("lin", 0.0), ("sigc", 0.5), ("sig", 0.5)):
        case(cases, "genann", {"sig": "genann_act_sigmoid", "thr": "genann_act_threshold",
                               "lin": "genann_act_linear", "sigc": "genann_act_sigmoid_cached"}[op], "own",
             [f"{op} {hd(v)}"])
    for s in ((1, 1, 2, 1), (2, 0, 0, 1), (2, 1, 3, 1), (4, 3, 5, 2)):
        case(cases, "genann", "genann_init", "own", ["init %d %d %d %d" % s])
    case(cases, "genann", "genann_copy", "own", [f"copy 2 1 3 1 r".replace(" r", " " + hd(1.0))])
    case(cases, "genann", "genann_randomize", "own", ["randz 2 1 3 1"])
    case(cases, "genann", "genann_run", "own", [f"run 2 1 2 1 0 0 r {hd(0.5)} {hd(0.5)}"])
    # probe: does srand() control genann_init's weights? (C: GENANN_RANDOM() = rand()/RAND_MAX)
    for seed in (1, 2, 42, 12345):
        case(cases, "genann", "genann_init", "probe", [f"seed {seed}", "init 2 1 2 1"])
    return cases


# ---------------------------------------------------------------- buffer
BUF_INIT = [["new"], ["newsz 0"], ["newsz 1"], ["newsz 10"], ["newstr " + hx("hello")],
            ["newstr " + hx("  hello  ")], ["newstr " + hx("")], ["newcopy " + hx("hello world")],
            ["newcopy " + hx("\t x \n")], ["newstr " + hx("y" * 1023)], ["newcopy " + hx("abcdef")],
            ["newstrlen " + hx("hello") + " 5"], ["newstr " + hx("   ")], ["newcopy " + hx(" a")],
            ["newstr " + hx("b ")]]


def buf_args(op):
    s = [hx(x) for x in STR_CORPUS[:24]]
    if op in ("app", "pre", "idx", "eq"):
        return [f"{op} {a}" for a in s]
    if op == "appn":
        return [f"appn {hx(x)} {n}" for x in (b"hello", b"", b"abc", b"world wide") for n in (0, 1, 3, 5, 10)]
    if op == "appf":
        return [f"appf {k} {hx(x)} {v}" for k in (0, 1, 2) for x in (b"", b"ab", b"x" * 70) for v in (0, -1, 2**31 - 1, 42)]
    if op == "resize":
        return [f"resize {n}" for n in (0, 1, 3, 5, 63, 64, 65, 1023, 1024, 1025, 2048, 4097)]
    if op == "fill":
        return [f"fill {c}" for c in (0, 1, 32, 65, 127, 128, 255, 256, -1, 9)]
    if op == "slice":
        return [f"slice {f} {t}" for f in (0, 1, 2, 5) for t in (0, 1, 3, 5, 11, 100, -1, -2, -3)]
    return [op]


BUF_FUNCS = {"new": "buffer_new", "newsz": "buffer_new_with_size", "newstr": "buffer_new_with_string",
             "newstrlen": "buffer_new_with_string_length", "newcopy": "buffer_new_with_copy",
             "app": "buffer_append", "appn": "buffer_append_n", "pre": "buffer_prepend", "appf": "buffer_appendf",
             "resize": "buffer_resize", "compact": "buffer_compact", "tl": "buffer_trim_left",
             "tr": "buffer_trim_right", "trim": "buffer_trim", "fill": "buffer_fill", "clear": "buffer_clear",
             "slice": "buffer_slice", "eq": "buffer_equals", "idx": "buffer_indexof", "print": "buffer_print"}
BUF_MUT = ["app", "appn", "pre", "appf", "resize", "compact", "tl", "tr", "trim", "fill", "clear"]


def gen_buffer():
    cases = []
    # constructors on their own argument grids
    for s in STR_CORPUS:
        case(cases, "buffer", "buffer_new_with_string", "grid", [f"newstr {hx(s)}"])
        case(cases, "buffer", "buffer_new_with_copy", "grid", [f"newcopy {hx(s)}"])
    for n in (0, 1, 2, 7, 63, 64, 65, 1023, 1024, 1025):
        case(cases, "buffer", "buffer_new_with_size", "grid", [f"newsz {n}"])
    case(cases, "buffer", "buffer_new", "grid", ["new"])
    for s in (b"hello", b"", b"abc", b"  x  "):
        for n in (0, 1, 3, 5, 10, 64):
            case(cases, "buffer", "buffer_new_with_string_length", "grid", [f"newstrlen {hx(s)} {n}"])
    # every other API function: (initial state) x (its argument grid), spread to <= 80
    for op in ["app", "appn", "pre", "appf", "resize", "compact", "tl", "tr", "trim", "fill", "clear",
               "slice", "eq", "idx", "print"]:
        args = buf_args(op)
        pairs = (_spread([list(range(len(BUF_INIT))), list(range(len(args)))], CAP) if len(args) > 1
                 else [(i, 0) for i in range(len(BUF_INIT))])
        for ii, ai in pairs:
            init = BUF_INIT[ii]
            case(cases, "buffer", BUF_FUNCS[op], "grid", init + [args[ai]])
    # ordered pairs of mutating operations x initial states (3 per pair)
    for a in BUF_MUT:
        for b in BUF_MUT:
            aa, ba = buf_args(a), buf_args(b)
            for k in range(3):
                init = BUF_INIT[(3 * BUF_MUT.index(a) + BUF_MUT.index(b) + 5 * k) % len(BUF_INIT)]
                case(cases, "buffer", f"pair:{BUF_FUNCS[a]}+{BUF_FUNCS[b]}", "pair",
                     init + [aa[(7 * k + 1) % len(aa)], ba[(5 * k + 2) % len(ba)]])
    # own (RQ2 test.rs; buffer_free is implicit at process exit)
    own = [["new"], ["newsz 64"], ["newstr " + hx("hello")], ["new", "app " + hx("hello")],
           ["new", "app " + hx("hello"), "app " + hx(" world")], ["new", "app " + hx("world"), "pre " + hx("hello ")],
           ["newstr " + hx("hello"), "eq " + hx("hello")], ["newstr " + hx("  hello  "), "trim"],
           ["newstr " + hx("  hello"), "tl"], ["newstr " + hx("hello  "), "tr"], ["newsz 10", "resize 100"],
           ["newstr " + hx("hello world"), "slice 0 5"], ["newstr " + hx("hello world"), "idx " + hx("world")],
           ["newstr " + hx("hello"), "idx " + hx("xyz")]]
    for lines in own:
        case(cases, "buffer", BUF_FUNCS[lines[-1].split()[0]], "own", lines)
    return cases


# ---------------------------------------------------------------- quadtree
def gen_quadtree():
    cases = []
    bvals = [-1.0, 0.0, 1.0, 10.0, 100.0, 1000.0, -1000.0, 0.5, float("inf"), float("-inf"), float("nan"), 1e300]
    for mnx, mny, mxx, mxy in _spread([bvals] * 4, CAP):
        case(cases, "quadtree", "quadtree_new", "grid", [f"new {hd(mnx)} {hd(mny)} {hd(mxx)} {hd(mxy)}"])
    for mnx, mny, mxx, mxy in _spread([bvals] * 4, CAP):
        case(cases, "quadtree", "quadtree_node_with_bounds", "grid",
             [f"nwb {hd(mnx)} {hd(mny)} {hd(mxx)} {hd(mxy)}"])
    pts = [0.0, 1.0, -1.0, 0.5, 10.0, 99.5, 499.5, 500.0, 1000.0, float("nan"), float("inf"), 250.0, 749.5]
    for k in range(40):
        pairs = [(pts[(k * 3 + j) % len(pts)], pts[(k * 7 + 2 * j + 1) % len(pts)]) for j in range(1 + k % 5)]
        case(cases, "quadtree", "quadtree_bounds_extend", "grid",
             ["bounds " + " ".join(f"{hd(x)} {hd(y)}" for x, y in pairs)])
    for x, y in _spread([pts, pts], 40):
        case(cases, "quadtree", "quadtree_point_new", "grid", [f"pnew {hd(x)} {hd(y)}"])
    # insert sequences in a fixed tree. Coordinates: interior points, points on split lines (x or y
    # equal to a quadrant midline), duplicates, outside points, NaN.
    trees = [(-1.0, -1.0, 1000.0, 1000.0), (0.0, 0.0, 100.0, 100.0), (0.0, 0.0, 8.0, 8.0)]
    coords = [10.0, 20.0, 90.0, 50.0, 1.0, 2.0, 99.0, 499.5, 249.25, 0.0, 100.0, 1000.0, -5.0, 3.0, 4.0,
              6.0, 7.0, 2.5, 74.75, 12.375, 24.5]
    for k in range(CAP):
        t = trees[k % 3]
        n = 1 + (k * 7) % 12
        ps = [(coords[(k + 3 * j) % len(coords)], coords[(2 * k + 5 * j + 1) % len(coords)]) for j in range(n)]
        if k % 4 == 0:
            ps.append(ps[0])                                   # duplicate insert
        lines = [f"new {' '.join(hd(v) for v in t)}"]
        for j, (x, y) in enumerate(ps):
            lines.append(f"ins {hd(x)} {hd(y)} {j + 1}")
        lines.append("walk")
        case(cases, "quadtree", "quadtree_insert", "grid", lines)
        # same tree, then search every inserted point (search is only safe on reachable leaves in C)
        case(cases, "quadtree", "quadtree_search", "grid",
             lines[:-1] + [f"srch {hd(x)} {hd(y)}" for (x, y) in ps])
    # own (RQ2 test.rs)
    T = f"new {hd(-1.0)} {hd(-1.0)} {hd(1000.0)} {hd(1000.0)}"
    case(cases, "quadtree", "quadtree_new", "own", [T])
    case(cases, "quadtree", "quadtree_insert", "own", [T, f"ins {hd(10.0)} {hd(20.0)} 4660", f"srch {hd(10.0)} {hd(20.0)}"])
    case(cases, "quadtree", "quadtree_insert", "own", [T, f"ins {hd(1.0)} {hd(2.0)} 4369", f"ins {hd(1.0)} {hd(2.0)} 8738",
                                                        f"srch {hd(1.0)} {hd(2.0)}"])
    case(cases, "quadtree", "quadtree_insert", "own", [T, f"ins {hd(10.0)} {hd(10.0)} 43690", f"ins {hd(90.0)} {hd(10.0)} 48059",
                                                        f"ins {hd(50.0)} {hd(90.0)} 52428", f"srch {hd(10.0)} {hd(10.0)}",
                                                        f"srch {hd(90.0)} {hd(10.0)}", f"srch {hd(50.0)} {hd(90.0)}"])
    return cases


# ---------------------------------------------------------------- urlparser
def gen_urlparser():
    cases = []
    schemes = ["http", "https", "git", "ssh", "ftp", "file", "mailto", "javascript", "doi", "iris.beep",
               "view-source", "aaa", "ymsgr", "HTTP", "htt", "httpx", "not-a-url-scheme", "", "a" * 15, "a" * 16,
               "a" * 40, "irc6", "ws", "wss", "xmpp", "crome-extension", "http ", " http", "ht:tp", "sftp", "svn",
               "tel", "urn", "jdbc", "com-evenbrite-attendee", "soap.beeps"]
    for s in schemes:
        case(cases, "urlparser", "url_is_protocol", "grid", [f"isp {hx(s)}"])
    for s in ["ssh", "git", "http", "", "SSH", "gitx", "sshh", "s", "g", "git "]:
        case(cases, "urlparser", "url_is_ssh", "grid", [f"isssh {hx(s)}"])
    protos = ["http", "https", "git", "ssh", "ftp", "file", "unknownproto", "a" * 20]
    seps = ["://", ":", ":/"]
    auths = ["", "user@", "user:pass@", "@", "git@"]
    hosts = ["example.com", "subdomain.host.com", "localhost", "h", "github.com", "127.0.0.1"]
    ports = ["", ":8080", ":", ":1"]
    paths = ["", "/", "/p/a/t/h", "/index.html", ":jwerle/url.h.git", "/a/b?x=1", "/x|y"]
    queries = ["", "?query=string", "?a=1&b=2", "?"]
    hashes = ["", "#hash", "#", "#a#b"]
    urls = []
    for t in _spread([list(range(len(x))) for x in (protos, seps, auths, hosts, ports, paths, queries, hashes)], CAP):
        p, s, a, h, po, pa, q, ha = (x[i] for x, i in zip((protos, seps, auths, hosts, ports, paths, queries, hashes), t))
        urls.append(p + s + a + h + po + pa + q + ha)
    urls[0:2] = ["http://user:pass@subdomain.host.com:8080/p/a/t/h?query=string#hash",
                 "git://git@github.com:jwerle/url.h.git"]
    for op, fn in (("gp", "url_get_protocol"), ("ga", "url_get_auth"), ("ghn", "url_get_hostname"),
                   ("gh", "url_get_host"), ("gpath", "url_get_path"), ("gpn", "url_get_pathname"),
                   ("gs", "url_get_search"), ("gq", "url_get_query"), ("gha", "url_get_hash"),
                   ("gport", "url_get_port"), ("parse", "url_parse"), ("insp", "url_inspect")):
        for u in urls:
            case(cases, "urlparser", fn, "grid", [f"{op} {hx(u)}"])
    # own (RQ2 test.rs)
    for s in ("http", "https", "file", "not-a-url-scheme"):
        case(cases, "urlparser", "url_is_protocol", "own", [f"isp {hx(s)}"])
    ref = "http://user:pass@subdomain.host.com:8080/p/a/t/h?query=string#hash"
    for op, fn in (("gp", "url_get_protocol"), ("ga", "url_get_auth"), ("ghn", "url_get_hostname"),
                   ("gh", "url_get_host"), ("gpn", "url_get_pathname"), ("gpath", "url_get_path"),
                   ("gs", "url_get_search"), ("gq", "url_get_query"), ("gha", "url_get_hash"),
                   ("gport", "url_get_port"), ("parse", "url_parse")):
        case(cases, "urlparser", fn, "own", [f"{op} {hx(ref)}"])
    case(cases, "urlparser", "url_parse", "own", [f"parse {hx('git://git@github.com:jwerle/url.h.git')}"])
    return cases


# ---------------------------------------------------------------- avl
def avl_keys(shape, n):
    if shape == "asc":
        return list(range(1, n + 1))
    if shape == "desc":
        return list(range(n, 0, -1))
    if shape == "zigzag":
        return [(i // 2 + 1) if i % 2 == 0 else (n - i // 2) for i in range(n)]
    if shape == "dups":
        return [1 + (i % max(1, n // 3)) for i in range(n)]
    if shape == "extreme":
        base = [INT_MIN, INT_MAX, 0, -1, 1, INT_MIN + 1, INT_MAX - 1]
        return [base[i % 7] + (i // 7) * (1 if base[i % 7] < 0 else -1) for i in range(n)]
    return lcg_values(n, 17 * n + 3, -50, 50)


AVL_SHAPES = ["asc", "desc", "zigzag", "dups", "extreme", "lcg"]
AVL_N = [1, 2, 3, 4, 5, 7, 8, 15, 16, 31, 32, 64]
AVL_DEL = ["fwd", "rev", "mid", "alt", "root_first"]


def del_order(n, pat):
    idx = list(range(n))
    if pat == "rev":
        return idx[::-1]
    if pat == "mid":
        return sorted(idx, key=lambda i: abs(i - n // 2))
    if pat == "alt":
        return idx[::2] + idx[1::2]
    if pat == "root_first":
        return [n // 2] + [i for i in idx if i != n // 2]
    return idx


def gen_avl():
    cases = []
    funcs3 = {"insert": "VOS_AVL3_Insert_Or_Find", "delete": "VOS_AVL3_Delete", "find": "VOS_AVL3_Find",
              "fn": "AVL3_Find_Or_Find_Next", "first": "VOS_AVL3_First", "last": "VOS_AVL3_Last",
              "next": "VOS_AVL3_Next", "prev": "VOS_AVL3_Prev"}
    funcs2 = {"insert": "VOS_AVL_Insert_Or_Find", "delete": "VOS_AVL_Delete", "find": "VOS_AVL_Find",
              "fn": "VOS_AVL_Find_Or_Find_Next", "next": "VOS_AVL_Next", "prev": "VOS_AVL_Prev"}
    for api, funcs in (("3", funcs3), ("2", funcs2)):
        for f, fname in funcs.items():
            for si, ni, di in _spread([list(range(len(AVL_SHAPES))), list(range(len(AVL_N))),
                                       list(range(len(AVL_DEL)))], CAP):
                n, keys = AVL_N[ni], avl_keys(AVL_SHAPES[si], AVL_N[ni])
                lines = [f"i{api} {k} {v}" for k, v in enumerate(keys)]
                inserted = []                       # indices actually in the tree (duplicates are refused)
                seen = set()
                for k, v in enumerate(keys):
                    if v not in seen:
                        seen.add(v)
                        inserted.append(k)
                if f == "insert":
                    lines = []
                    for k, v in enumerate(keys):
                        lines += [f"i{api} {k} {v}", f"dump{api}"]
                elif f == "delete":
                    for j in del_order(len(inserted), AVL_DEL[di]):
                        lines += [f"d{api} {inserted[j]}", f"dump{api}"]
                elif f in ("find", "fn"):
                    probes = sorted(set(keys + [min(keys) - 1, max(keys) + 1] + [v + 1 for v in keys[:8]]))
                    probes = [p for p in probes if INT_MIN <= p <= INT_MAX]
                    for v in probes:
                        lines.append(f"f{api} {v}" if f == "find" else f"fn{api} {v} {di % 2}")
                elif f in ("first", "last"):
                    lines += [f"{f}{api}", f"dump{api}"]
                    for j in del_order(len(inserted), AVL_DEL[di])[: max(1, len(inserted) // 2)]:
                        lines += [f"d{api} {inserted[j]}", f"{f}{api}"]
                else:
                    for k in inserted:
                        lines.append(f"{f}{api} {k}")
                lines.append(f"dump{api}")
                case(cases, "avl", fname, "grid", lines)
    # probes on elements that are NOT in the tree (both APIs define this: v2 checks VOS_AVL_IN_TREE,
    # AVL3 Next/Prev walk from the detached node): re-insert an in-tree element (v2), delete twice
    # (v2), next/prev on deleted elements, then dump.
    for si, ni in _spread([list(range(len(AVL_SHAPES))), list(range(len(AVL_N)))], 40):
        n, keys = AVL_N[ni], avl_keys(AVL_SHAPES[si], AVL_N[ni])
        seen, inserted = set(), []
        for k, v in enumerate(keys):
            if v not in seen:
                seen.add(v)
                inserted.append(k)
        gone = inserted[::2]
        for api in ("2", "3"):
            lines = [f"i{api} {k} {v}" for k, v in enumerate(keys)]
            if api == "2":
                lines += [f"i2 {inserted[0]} {keys[inserted[0]]}".replace("i2", "ri2")]
            for k in gone:
                lines.append(f"d{api} {k}")
            if api == "2":
                lines += [f"d2 {k}" for k in gone[:3]]
            lines += [f"next{api} {k}" for k in gone[:4]] + [f"prev{api} {k}" for k in gone[:4]] + [f"dump{api}"]
            case(cases, "avl", "VOS_AVL_*(not in tree)" if api == "2" else "VOS_AVL3_*(detached)", "grid", lines)
    # own (RQ2 test.rs): AVL3 with keys 2,1,3
    case(cases, "avl", "VOS_AVL3_Insert_Or_Find", "own",
         ["i3 1 2", "i3 0 1", "i3 2 3", "f3 2", "next3 0", "next3 1", "dump3"])
    case(cases, "avl", "VOS_AVL3_Delete", "own",
         ["i3 1 2", "i3 0 1", "i3 2 3", "d3 1", "f3 2", "next3 0", "d3 0", "d3 2", "dump3"])
    return cases


GENERATORS = {"qsort": gen_qsort, "ht": gen_ht, "rgba": gen_rgba, "genann": gen_genann, "buffer": gen_buffer,
              "quadtree": gen_quadtree, "urlparser": gen_urlparser, "avl": gen_avl}


def all_cases(projects=None):
    out = []
    for p, g in GENERATORS.items():
        if projects and p not in projects:
            continue
        out.extend(g())
    return out


if __name__ == "__main__":
    import collections
    for p, g in GENERATORS.items():
        cs = g()
        per = collections.Counter((c["cls"], c["func"]) for c in cs)
        print(p, len(cs), "max per (cls,func):", max(per.values()))
