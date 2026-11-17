"""Deterministic input generator for the TRACTOR executable cases (ACTOR re-grade).

By program SHAPE, read statically from the case's C source (never from the benchmark's tests and
never from a model):
  * every program gets the fixed STDIN corpus (text corpus of harness/gen_inputs.py, plus a numeric
    corpus: every value of the integer grid alone, and spread tuples of 2, 3, 4 and 6 values, and
    of k values when the widest scanf format in the C converts k fields (k not in 2,3,4,6),
    newline- and space-separated, plus float and malformed tokens), with argv empty;
  * a program whose C indexes argv also gets the ARGV grid: arities 1-4 spread over a token grid
    (the integer grid of systems/enumerate_inputs.py over int32, float tokens, text tokens), the
    option strings that appear as literals in its C source (each alone and in pairs), and, if the
    C opens files, the input sources named file / missing file / directory. A program that reads
    stdin AND argv gets each argv vector with three stdin bodies (empty, text, numbers).
Integer grid: grid_for(INT32_MIN, INT32_MAX, 24) -- small values, boundaries, powers of two +-1 and
an even sweep (the method's per-argument grid); tuples use the same R_d low-discrepancy spread.
No randomness: the same C source always yields the same inputs.
"""
import base64
import glob
import hashlib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # repo: systems/
from enumerate_inputs import grid_for, _spread  # noqa: E402

I32 = (-2**31, 2**31 - 1)
INT_GRID = grid_for(I32[0], I32[1], 24)
FLOAT_TOK = ["0.5", "-2.5", "3.14159", "1e10", "-0", "1e308", "nan", "inf"]
TEXT_TOK = ["abc", "hello", "Hello, World!", "héllo wörld", "", "a" * 300, " 12", "0x1F",
            "12abc", "-"]


def _text():
    return b"alpha\nbeta\n\ngamma\n  spaced \ndelta"


def _text_nl():
    return b"one\ntwo\nthree\n"


STDIN_TEXT = [
    b"", _text(), _text_nl(),
    bytes(range(0, 256, 7)) + b"\x00\x00\xff\xfe\n\x01",                # binary
    "café\nMoscowМосква\n日本語\n\U0001F600\nstraße\n".encode(),
    b"a\x00b\x00c\x00",                                                  # NUL-delimited
    ("\n".join(f"line{i}" for i in range(500)) + "\n").encode(),       # larger
    b"alpha\nbeta\nlast line without newline",
    b"A" * 9000 + b"\n" + b"tail\n",
    b"\n\n\n\n\n", b"a\rb\rc\r", b"   \n\t\n \t \n   spaced\n", b"x",
]


def numeric_stdin(k_scanf=0):
    out = [f"{v}\n".encode() for v in INT_GRID]
    sizes = [(2, 12), (3, 12), (4, 8), (6, 6)]
    if k_scanf > 1 and k_scanf not in (2, 3, 4, 6):
        sizes.append((k_scanf, 12))     # as many numbers as the C's widest scanf format converts
    for k, cap in sizes:
        for t in _spread([INT_GRID] * k, cap):
            out.append(("\n".join(str(v) for v in t) + "\n").encode())
        for t in _spread([INT_GRID] * k, max(2, cap // 3)):
            out.append((" ".join(str(v) for v in t) + "\n").encode())
    out += [f"{f}\n".encode() for f in FLOAT_TOK]
    out += [b"1 abc\n", b"abc 1\n", b"5\n1 2 3 4 5\n", b"3\n-1\n0\n1\n"]
    return out


def c_sources(csrc):
    files = [p for p in glob.glob(os.path.join(csrc, "**", "*.[ch]"), recursive=True)
             if "/build" not in p]
    return {p: open(p, encoding="utf-8", errors="replace").read() for p in sorted(files)}


def shape(csrc):
    srcs = c_sources(csrc)
    allc = "\n".join(srcs.values())
    nocomment = re.sub(r"/\*.*?\*/|//[^\n]*", "", allc, flags=re.S)
    flags = sorted(set(re.findall(r"\"(-{1,2}[A-Za-z][\w-]*)\"", nocomment)))
    return {
        "uses_argv": bool(re.search(r"\bargv\s*\[", nocomment)),
        "reads_stdin": bool(re.search(r"\b(scanf|fgets|getchar|getline|fread|fgetc|getc)\s*\(|\bstdin\b",
                                      nocomment)),
        "opens_files": bool(re.search(r"\bfopen\s*\(|\bopen\s*\(", nocomment)),
        "option_literals": flags,
        "scanf_conversions": max([len(CONV_RE.findall(f)) for f in SCANF_RE.findall(nocomment)] or [0]),
    }


# scanf("<format>" ...): the conversions in its format literal (adjacent literals joined)
SCANF_RE = re.compile(r'\bscanf\s*\(\s*((?:"(?:[^"\\]|\\.)*"\s*)+)')
CONV_RE = re.compile(r"%\*?[0-9]*(?:hh|h|ll|l|L|z|j|t)?[diouxXeEfgGaAcsp\[]")


def b64(b):
    return base64.b64encode(b).decode()


def case(argv, stdin=b"", files=None, dirs=None, note=""):
    c = {"argv": argv, "stdin_b64": b64(stdin), "files": {k: b64(v) for k, v in (files or {}).items()},
         "dirs": dirs or [], "note": note}
    h = hashlib.sha256(repr((argv, stdin, sorted((files or {}).items()), dirs or [])).encode()).hexdigest()
    c["id"] = h[:16]
    return c


def argv_grid(sh):
    toks_int = [str(v) for v in INT_GRID]
    toks = toks_int + FLOAT_TOK + TEXT_TOK
    vecs = []
    for k, cap in ((1, len(toks)), (2, 16), (3, 16), (4, 16)):
        if k == 1:
            vecs += [[t] for t in toks]
        else:
            vecs += [list(t) for t in _spread([toks_int] * k, cap // 2)]
            vecs += [list(t) for t in _spread([toks] * k, cap // 2)]
    fl = sh["option_literals"]
    vecs += [[f] for f in fl]
    vecs += [[a, b] for i, a in enumerate(fl) for b in fl[i + 1:]]
    vecs += [[f, "7"] for f in fl] + [[f, "-3", "12"] for f in fl]
    seen, out = set(), []
    for v in vecs:
        if tuple(v) not in seen:
            seen.add(tuple(v))
            out.append(v)
    return out


def generate(csrc):
    sh = shape(csrc)
    cases = []
    for s in STDIN_TEXT:
        cases.append(case([], s, note="stdin-text"))
    for s in numeric_stdin(sh["scanf_conversions"]):
        cases.append(case([], s, note="stdin-num"))
    if sh["uses_argv"]:
        bodies = [b""] if not sh["reads_stdin"] else [b"", _text_nl(), b"3\n5\n7\n"]
        for v in argv_grid(sh):
            for s in bodies:
                cases.append(case(v, s, note="argv"))
        if sh["opens_files"]:
            cases.append(case(["in1.txt"], files={"in1.txt": _text_nl()}, note="file"))
            cases.append(case(["nope_does_not_exist.txt"], note="missing-file"))
            cases.append(case(["adir"], dirs=["adir"], note="is-a-directory"))
    seen, out = set(), []
    for c in cases:
        if c["id"] not in seen:
            seen.add(c["id"])
            out.append(c)
    return sh, out


if __name__ == "__main__":
    sh, cs = generate(sys.argv[1])
    print(sh, len(cs))
