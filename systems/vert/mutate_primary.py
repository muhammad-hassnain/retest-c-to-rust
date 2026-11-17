"""Mutation adequacy of the PRIMARY VERT scalar harness (run.py, spread grid, ASan/UBSan + -O0/-O1).

Why this exists. The older measure (mutate.py -> results/vert_mutation.json, 161/173) ran a
different harness from the one behind the headline (x86 + MSan, legacy truncated grid, the first 40
functions in harness order, ROR/AOR only). This script measures the power of the harness that
produced results/vert_diff.json, on the population that harness calls "agree":

  * Population: every function with status "tested" in results/vert_diff.json and no divergence of
    any kind (value 0, panic 0, hang 0, ub 0, timeout 0) -- the 117 null results.
  * Harness: exactly run.py's: build_c (gcc -O1 -fwrapv), build_c(mode=san) (gcc -O0 ASan+UBSan),
    build_c(mode=alt) (gcc -O0 -fwrapv) when MSan is unavailable and the C has no float, rustc -O,
    enumerate_cases(i32 x nargs, cap=80, max_abs=512, nonneg=True, spread grid), batch_diff.tally.
    The unmutated translation is re-run through the same path first (control: it must agree).
  * Kill rule (task): a mutant is KILLED iff tally reports value > 0 (wrong answer on an input
    the C runs UB-free) or rust_panic > 0 (sanitizer-clean Rust panic). Rust hangs, C-UB-only and
    C-timeout-only outcomes are recorded but are NOT kills. Mutants that fail to compile are
    recorded separately and excluded from the rate.

Mutation sites (deterministic). The translated Rust is lexed (comments/strings skipped); sites are
taken from the body of f_gold and of every helper function f_gold reaches through direct calls
(VERT's boilerplate min/max/cmpfunc/len/sort helpers are mutated only when f_gold actually calls
them). Signatures are never mutated. Operators:
  ror   relational replacement, the same mapping as mutate.py: == -> !=, != -> ==, <= -> >,
        >= -> <, < -> >=, > -> <= (generic angle brackets are excluded).
  aor   arithmetic replacement, the same mapping as mutate.py: + -> -, - -> +, * -> /, / -> *,
        on BINARY operators only (unary minus / deref excluded), plus the compound forms
        += -> -=, -= -> +=, *= -> /=, /= -> *= (mutate.py's " + " text match skipped these).
  const integer literal c -> c+1 and c -> c-1 (boundary shift); a result below 0 is written "(-1)".
        Float literals and tuple indices (x.0) are excluded.
  f32   every maximal integer arithmetic chain (operands joined by + - * / %, at one bracket level,
        not itself the parenthesised operand of an enclosing chain) is evaluated in f32 and
        converted back: every operand becomes `(operand as f32)` (parenthesised sub-chains are
        converted recursively) and the chain becomes __vf(orig, f32expr), which returns the f32
        value cast back to orig's integer type. __vf is implemented for integer types only, so a
        chain that is really float typed fails to compile and is excluded. Chains with a float
        literal or an `as f32/f64` at their top level are skipped as float chains.
  bnd   (extra, reported separately; not in the headline) relational/range boundary:
        < <-> <=, > <-> >=, .. <-> ..=
If an operator yields more than --cap mutants for one function, a fixed seeded sample of --cap is
taken (random.Random(f"{seed}:{func}:{op}").sample over the site-ordered list); the rule and the
counts before/after sampling are recorded per function.

Equivalence analysis (--equiv). Every compiled, not-killed mutant is re-run against the C on a much
larger domain, in three phases with per-phase time budgets, smallest inputs first:
  A  in-domain (the harness's own domain, non-negative, each arg <= 512): exhaustive for 1 and 2
     arguments ([0,512]^k in max-norm shells); for k >= 3 exhaustive shells of [0,512]^k until the
     budget runs out.
  B  negative inputs: [-512,512]^k tuples with at least one negative component, shell order.
  C  large magnitudes: the full-range i32 grid (enumerate_inputs.grid_for over [-2^31, 2^31-1]),
     spread product capped at 4096 tuples.
A candidate divergence (smallest first, up to 64 per phase) counts only if the C is clean under the
ASan/UBSan build and -O0 == -O1 on that input, exactly the primary harness's filter. Outcome per
survivor: inequivalent_in_domain (grid missed a fault inside the domain it samples),
inequivalent_out_of_domain, rust_hang_only, or no_divergence_found (with the exact coverage).

Run (arm64 Mac, Docker, at most 6 CPUs):
  docker run --rm --cpus 6 -v "$PWD:/work" -w /work -e GIT_REV=$(git rev-parse HEAD) \
      -e HOST_DESC="$(uname -s) $(uname -m)" -e IMAGE_ID=$(docker image inspect crust-stable -f '{{.Id}}') \
      crust-stable python3 systems/vert/mutate_primary.py --jobs 6 --verify-every 25 --equiv --budgets 30,10,10
  ... and the same with --arrays --budgets 30 for the array/float harness (see ARRAYS_NOTE).
"""
import argparse
import datetime
import hashlib
import json
import math
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from itertools import product

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import run  # noqa: E402  (the primary harness: extract/build/gates)
from batch_diff import tally, run_batch, run_one, san_verdict  # noqa: E402
from enumerate_inputs import enumerate_cases, grid_for, _spread  # noqa: E402

REPO = run.REPO
N_GRID = 80          # run.py default -n
MAX_ABS = 512        # run.py default domain
NONNEG = True
# Input grid (opt-in --grid v2; default v1 = every shipped result). Read from the environment so
# ProcessPool workers see the same value whether they fork or spawn.
GRID = os.environ.get("MUTPRIM_GRID", "v1")

# ----------------------------------------------------------------------------------------------
# Lexer
# ----------------------------------------------------------------------------------------------
PUNCTS = ["..=", "...", "<<=", ">>=", "::", "->", "=>", "==", "!=", "<=", ">=", "&&", "||",
          "+=", "-=", "*=", "/=", "%=", "^=", "&=", "|=", "<<", ">>", ".."]
KEYWORDS = {"as", "break", "const", "continue", "crate", "else", "enum", "extern", "false", "fn",
            "for", "if", "impl", "in", "let", "loop", "match", "mod", "move", "mut", "pub", "ref",
            "return", "self", "Self", "static", "struct", "super", "trait", "true", "type",
            "unsafe", "use", "where", "while"}
GENERIC_TYPES = {"Vec", "VecDeque", "Option", "Box", "HashMap", "HashSet", "BTreeMap", "BTreeSet",
                 "BinaryHeap", "Result", "Rc", "RefCell", "Cell", "Reverse", "Wrapping"}
INT_RE = re.compile(r"(0x[0-9a-fA-F_]+|0o[0-7_]+|0b[01_]+|[0-9][0-9_]*)"
                    r"(i8|i16|i32|i64|i128|isize|u8|u16|u32|u64|u128|usize)?$")


class Tok:
    __slots__ = ("kind", "text", "s", "e", "generic")

    def __init__(self, kind, text, s, e):
        self.kind, self.text, self.s, self.e, self.generic = kind, text, s, e, False

    def __repr__(self):
        return f"{self.kind}:{self.text}"


def lex(src):
    toks, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c.isspace():
            i += 1; continue
        if src.startswith("//", i):
            j = src.find("\n", i); i = n if j == -1 else j; continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2); i = n if j == -1 else j + 2; continue
        if c == '"':
            j = i + 1
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            toks.append(Tok("str", src[i:j + 1], i, j + 1)); i = j + 1; continue
        if c == "'":
            m = re.match(r"'(\\.|[^\\'])'", src[i:])
            if m:
                toks.append(Tok("char", m.group(0), i, i + m.end())); i += m.end(); continue
            m = re.match(r"'[A-Za-z_]\w*", src[i:])
            toks.append(Tok("lifetime", m.group(0), i, i + m.end())); i += m.end(); continue
        if c.isalpha() or c == "_":
            m = re.match(r"[A-Za-z_]\w*", src[i:])
            toks.append(Tok("ident", m.group(0), i, i + m.end())); i += m.end(); continue
        if c.isdigit():
            m = re.match(r"0x[0-9a-fA-F_]+|0o[0-7_]+|0b[01_]+", src[i:])
            if m:
                j = i + m.end()
            else:
                m = re.match(r"[0-9][0-9_]*", src[i:]); j = i + m.end()
                isfloat = False
                # fractional part: '.' not followed by '.' or an identifier (method/range)
                if j < n and src[j] == "." and not (j + 1 < n and (src[j + 1] == "." or src[j + 1].isalpha() or src[j + 1] == "_")):
                    isfloat = True; j += 1
                    m2 = re.match(r"[0-9][0-9_]*", src[j:])
                    if m2:
                        j += m2.end()
                m3 = re.match(r"[eE][+-]?[0-9_]+", src[j:])
                if m3:
                    isfloat = True; j += m3.end()
                ms = re.match(r"(f32|f64)", src[j:])
                if ms:
                    isfloat = True; j += ms.end()
                if isfloat:
                    toks.append(Tok("float", src[i:j], i, j)); i = j; continue
            ms = re.match(r"(i8|i16|i32|i64|i128|isize|u8|u16|u32|u64|u128|usize)\b", src[j:])
            if ms:
                j += ms.end()
            toks.append(Tok("int", src[i:j], i, j)); i = j; continue
        for p in PUNCTS:
            if src.startswith(p, i):
                toks.append(Tok("punct", p, i, i + len(p))); i += len(p); break
        else:
            toks.append(Tok("punct", c, i, i + 1)); i += 1
    mark_generics(toks)
    return toks


def mark_generics(toks):
    """Mark '<' / '>' tokens that are generic brackets (Vec<i32>, ::<T>), not comparisons."""
    depth = 0
    for k, t in enumerate(toks):
        if t.kind != "punct":
            continue
        if t.text == "<":
            prev = toks[k - 1] if k else None
            if prev is not None and (prev.text == "::" or (prev.kind == "ident" and prev.text in GENERIC_TYPES)):
                t.generic = True; depth += 1
        elif t.text == ">" and depth > 0:
            t.generic = True; depth -= 1
        elif t.text == ">>" and depth > 0:
            t.generic = True; depth = max(0, depth - 2)
        elif t.text == ">=" and depth > 0:   # Vec<i32>= is not real Rust; be safe
            t.generic = True; depth -= 1


def is_operand_end(t):
    """Token after which a +,-,*,& is BINARY (an expression just ended)."""
    if t is None:
        return False
    if t.kind in ("int", "float", "str", "char"):
        return True
    if t.kind == "ident":
        return t.text not in KEYWORDS or t.text in ("self", "Self", "true", "false")
    return t.text in (")", "]") or (t.text == ">" and t.generic)


# ----------------------------------------------------------------------------------------------
# Functions and reachability
# ----------------------------------------------------------------------------------------------
def match_close(toks, k):
    """Index of the bracket closing toks[k] ('(', '[', '{')."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    o, c = toks[k].text, pairs[toks[k].text]
    d = 0
    for j in range(k, len(toks)):
        if toks[j].kind != "punct":
            continue
        if toks[j].text == o:
            d += 1
        elif toks[j].text == c:
            d -= 1
            if d == 0:
                return j
    raise ValueError("unbalanced")


def functions(toks):
    """{name: (body_open_idx, body_close_idx)} for top-level `fn name(...) ... { body }`."""
    out = {}
    k = 0
    while k < len(toks):
        t = toks[k]
        if t.kind == "ident" and t.text == "fn" and k + 1 < len(toks) and toks[k + 1].kind == "ident":
            name = toks[k + 1].text
            j = k + 2
            while j < len(toks) and toks[j].text != "{":
                if toks[j].text == "(":
                    j = match_close(toks, j)
                j += 1
            if j >= len(toks):
                break
            e = match_close(toks, j)
            out.setdefault(name, (j, e))
            k = e + 1
            continue
        k += 1
    return out


def reachable(toks, fns, root="f_gold"):
    seen, todo = set(), [root]
    while todo:
        f = todo.pop()
        if f in seen or f not in fns:
            continue
        seen.add(f)
        s, e = fns[f]
        for k in range(s + 1, e):
            t = toks[k]
            if t.kind == "ident" and t.text in fns and k + 1 < len(toks) and toks[k + 1].text == "(":
                prev = toks[k - 1]
                if prev.text not in (".", "::", "fn"):
                    todo.append(t.text)
    return sorted(seen)


# ----------------------------------------------------------------------------------------------
# Site enumeration
# ----------------------------------------------------------------------------------------------
ROR = {"==": "!=", "!=": "==", "<=": ">", ">=": "<", "<": ">=", ">": "<="}
AOR = {"+": "-", "-": "+", "*": "/", "/": "*", "+=": "-=", "-=": "+=", "*=": "/=", "/=": "*="}
BND = {"<": "<=", "<=": "<", ">": ">=", ">=": ">", "..": "..=", "..=": ".."}


def line_of(src, pos):
    return src.count("\n", 0, pos) + 1


def body_ranges(toks, fns, names):
    return [(n, fns[n][0], fns[n][1]) for n in names]


def sites(src, toks, ranges):
    """Return list of mutant dicts {op, sub, fn, line, s, e, repl} (text splice), deterministic order."""
    out = []
    for fname, bs, be in ranges:
        for k in range(bs + 1, be):
            t = toks[k]
            prev = toks[k - 1]
            if t.kind == "punct" and not t.generic:
                if t.text in ROR:
                    out.append(dict(op="ror", sub=f"{t.text}->{ROR[t.text]}", fn=fname, tok=k,
                                    s=t.s, e=t.e, repl=ROR[t.text]))
                if t.text in BND:
                    out.append(dict(op="bnd", sub=f"{t.text}->{BND[t.text]}", fn=fname, tok=k,
                                    s=t.s, e=t.e, repl=BND[t.text]))
                if t.text in AOR:
                    if t.text in ("+", "-", "*", "/") and not is_operand_end(prev):
                        pass   # unary minus / deref / leading sign: not a binary arithmetic operator
                    else:
                        out.append(dict(op="aor", sub=f"{t.text}->{AOR[t.text]}", fn=fname, tok=k,
                                        s=t.s, e=t.e, repl=AOR[t.text]))
            if t.kind == "int" and prev.text != ".":
                m = INT_RE.match(t.text)
                if not m:
                    continue
                v = int(m.group(1).replace("_", ""), 0)
                suf = m.group(2) or ""
                for d, lab in ((+1, "c+1"), (-1, "c-1")):
                    nv = v + d
                    repl = f"{nv}{suf}" if nv >= 0 else f"(-{-nv}{suf})"
                    out.append(dict(op="const", sub=lab, fn=fname, tok=k, s=t.s, e=t.e, repl=repl))
    out.extend(f32_sites(src, toks, ranges))
    for m in out:
        m["line"] = line_of(src, m["s"])
        m["orig"] = src[m["s"]:m["e"]]
    out.sort(key=lambda m: (m["s"], m["op"], m["sub"]))
    return out


# --- f32 chains -------------------------------------------------------------------------------
ARITH = {"+", "-", "*", "/", "%"}
CHAIN_DELIMS = {";", ",", "=", "+=", "-=", "*=", "/=", "%=", "^=", "&=", "|=", "<<=", ">>=",
                "==", "!=", "<", ">", "<=", ">=", "&&", "||", "..", "..=", "...", "=>", "->", ":",
                "<<", ">>", "|", "^", "?", "{", "}", "@", "#"}
CHAIN_KW = {"if", "else", "while", "for", "in", "let", "return", "match", "loop", "break",
            "continue", "mut", "fn", "use", "const", "static"}


def items_at_level(toks, a, b):
    """Split toks[a:b] into level items: ('tok', k) or ('grp', open_k, close_k)."""
    items, k = [], a
    while k < b:
        t = toks[k]
        if t.kind == "punct" and t.text in ("(", "[", "{"):
            e = match_close(toks, k)
            items.append(("grp", k, e)); k = e + 1
        else:
            items.append(("tok", k)); k += 1
    return items


def segments(toks, items):
    """Split level items into delimiter-free segments (lists of items)."""
    segs, cur = [], []
    for it in items:
        delim = False
        if it[0] == "tok":
            t = toks[it[1]]
            if t.kind == "punct" and (t.text in CHAIN_DELIMS or t.generic):
                delim = True
            elif t.kind == "punct" and t.text == "&":
                prev = toks[it[1] - 1]
                delim = is_operand_end(prev)          # binary & is a delimiter; reference & is not
            elif t.kind == "ident" and t.text in CHAIN_KW:
                delim = True
            elif t.kind == "punct" and t.text == "!" and False:
                delim = True
        else:
            if toks[it[1]].text == "{":
                delim = True
        if delim:
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(it)
    if cur:
        segs.append(cur)
    return segs


def split_operands(toks, seg):
    """Split a segment at BINARY arithmetic operators -> (operands, ops) or None if no operator."""
    operands, ops, cur = [], [], []
    for idx, it in enumerate(seg):
        if it[0] == "tok":
            t = toks[it[1]]
            if t.kind == "punct" and t.text in ARITH and cur:
                last = cur[-1]
                last_tok = toks[last[2]] if last[0] == "grp" else toks[last[1]]
                if is_operand_end(last_tok) or (last[0] == "grp" and last_tok.text in (")", "]")):
                    operands.append(cur); ops.append(t.text); cur = []
                    continue
        cur.append(it)
    operands.append(cur)
    if not ops or any(not o for o in operands):
        return None
    return operands, ops


def item_span(toks, it):
    return (toks[it[1]].s, toks[it[2]].e) if it[0] == "grp" else (toks[it[1]].s, toks[it[1]].e)


def is_float_operand(toks, op_items):
    for it in op_items:
        if it[0] == "tok":
            t = toks[it[1]]
            if t.kind == "float":
                return True
    # an `as f32/f64` at the operand's top level (last cast wins)
    casts = [toks[it[1] + 1].text for it in op_items
             if it[0] == "tok" and toks[it[1]].text == "as" and it[1] + 1 < len(toks)]
    return bool(casts) and casts[-1] in ("f32", "f64")


def chain_of_group(src, toks, open_k, close_k):
    """If the content of a '(' group is exactly one arithmetic chain, return (operands, ops)."""
    items = items_at_level(toks, open_k + 1, close_k)
    segs = segments(toks, items)
    if len(segs) != 1 or len(segs[0]) != len(items):
        return None
    return split_operands(toks, segs[0])


def convert_chain(src, toks, operands, ops, covered):
    parts = []
    for op_items, op in zip(operands, ops + [None]):
        if len(op_items) == 1 and op_items[0][0] == "grp" and toks[op_items[0][1]].text == "(":
            g = op_items[0]
            sub = chain_of_group(src, toks, g[1], g[2])
            if sub is not None:
                covered.add((g[1], g[2]))
                parts.append("(" + convert_chain(src, toks, sub[0], sub[1], covered) + ")")
            else:
                s, e = item_span(toks, g)
                parts.append(f"(({src[s:e]}) as f32)")
        else:
            s = item_span(toks, op_items[0])[0]
            e = item_span(toks, op_items[-1])[1]
            parts.append(f"(({src[s:e]}) as f32)")
        if op is not None:
            parts.append(f" {op} ")
    return "".join(parts)


def f32_sites(src, toks, ranges):
    found, covered = [], set()

    def walk(a, b, fname):
        items = items_at_level(toks, a, b)
        for seg in segments(toks, items):
            r = split_operands(toks, seg)
            if r is not None:
                operands, ops = r
                s = item_span(toks, seg[0])[0]
                e = item_span(toks, seg[-1])[1]
                if not any(is_float_operand(toks, o) for o in operands):
                    cov = set()
                    f32e = convert_chain(src, toks, operands, ops, cov)
                    found.append(dict(op="f32", sub="chain", fn=fname, tok=seg[0][1], s=s, e=e,
                                      repl=f"__vf({src[s:e]}, {f32e})", covers=cov,
                                      span_tok=(seg[0][1], seg[-1][-1] if seg[-1][0] == "grp" else seg[-1][1])))
        for it in items:
            if it[0] == "grp":
                walk(it[1] + 1, it[2], fname)

    for fname, bs, be in ranges:
        walk(bs + 1, be, fname)
    # drop chains that are the parenthesised operand of an enclosing chain (covered by recursion)
    allcov = set()
    for f in found:
        allcov |= f["covers"]
    out = []
    for f in found:
        grp = (f["span_tok"][0] - 1, f["span_tok"][1] + 1)
        if grp in allcov:
            continue
        f.pop("covers"); f.pop("span_tok")
        out.append(f)
    return out


VF_PRELUDE = """
#[allow(dead_code, non_camel_case_types)]
trait __ViaF32 { fn __from_f32(v: f32) -> Self; }
macro_rules! __via_f32_impl { ($($t:ty),*) => { $(impl __ViaF32 for $t { fn __from_f32(v: f32) -> Self { v as $t } })* } }
__via_f32_impl!(i8, i16, i32, i64, i128, isize, u8, u16, u32, u64, u128, usize);
#[allow(dead_code)]
fn __vf<T: __ViaF32>(_orig: T, v: f32) -> T { T::__from_f32(v) }
"""


def apply(src, m):
    out = src[:m["s"]] + m["repl"] + src[m["e"]:]
    if m["op"] == "f32":
        out += "\n" + VF_PRELUDE
    return out


def mutants_for(func, cap, seed):
    rust, argtypes = run.extract_llm_rust(func)
    toks = lex(rust)
    fns = functions(toks)
    names = reachable(toks, fns)
    ms = sites(rust, toks, body_ranges(toks, fns, names))
    by_op, sampled = {}, {}
    for m in ms:
        by_op.setdefault(m["op"], []).append(m)
    chosen = []
    for op in sorted(by_op):
        lst = by_op[op]
        if cap and len(lst) > cap:
            idx = sorted(random.Random(f"{seed}:{func}:{op}").sample(range(len(lst)), cap))
            sampled[op] = {"sites": len(lst), "sampled": cap}
            lst = [lst[i] for i in idx]
        else:
            sampled[op] = {"sites": len(lst), "sampled": len(lst)}
        chosen.extend(lst)
    chosen.sort(key=lambda m: (m["s"], m["op"], m["sub"]))
    seen, uniq = set(), []
    for i, m in enumerate(chosen):
        msrc = apply(rust, m)
        if msrc in seen or msrc == rust:
            continue
        seen.add(msrc)
        m = dict(m)
        m["id"] = f"{m['op']}:{m['fn']}:L{m['line']}:{m['sub']}@{m['s']}"
        m["src"] = msrc
        uniq.append(m)
    return rust, argtypes, names, uniq, sampled


# ----------------------------------------------------------------------------------------------
# Running mutants through the primary harness
# ----------------------------------------------------------------------------------------------
POP_FILE = {"v1": "vert_diff.json", "v2": "vert_diff_grid2.json"}   # population: the run on the same grid


def population():
    d = json.load(open(os.path.join(REPO, "results", POP_FILE[GRID])))
    return [r["func"] for r in d["all"] if r.get("status") == "tested" and r["value_div"] == 0
            and r["rust_panic_c_ok"] == 0 and r.get("rust_hang", 0) == 0 and r["ub_div"] == 0
            and r["timeout_div"] == 0]


def build_oracles(func, d):
    """Exactly run.test_function's C builds."""
    rust, argtypes = run.extract_llm_rust(func)
    csrc = run.c_source(func)
    nargs = len(argtypes)
    binc = run.build_c(func, csrc, nargs, d)
    binc_san = run.build_c(func, csrc, nargs, d, mode="san")
    binc_msan = run.build_c(func, csrc, nargs, d, mode="msan")
    binc_alt = None
    if binc_msan is None and not run.c_uses_float(csrc):
        binc_alt = run.build_c(func, csrc, nargs, d, mode="alt")
    od = os.path.join(d, "orig"); os.makedirs(od, exist_ok=True)
    binr_orig = run.build_rust(rust, nargs, od)      # the unmutated translation
    return dict(binc=binc, binc_san=binc_san, binc_msan=binc_msan, binc_alt=binc_alt, nargs=nargs,
                binr_orig=binr_orig)


def grid(nargs):
    return enumerate_cases(["i32"] * nargs, cap=N_GRID, max_abs=MAX_ABS, nonneg=NONNEG, legacy=False, grid=GRID)


def classify(t):
    if t["value"] > 0:
        return "killed_value"
    if t["rust_panic"] > 0:
        return "killed_panic"
    if t.get("rust_hang", 0) > 0:
        return "rust_hang"
    if t["ub"] > 0:
        return "ub_only"
    if t["timeout"] > 0:
        return "timeout_only"
    return "survived"


FAST_RUST_TIMEOUT = 0.25   # per-input Rust timeout in the per-input fallback (harness: 3 s)
SLOW_RECHECK = 4           # slow inputs re-run with the harness's own 3 s timeout


def eval_cases_fast(binc, binr, cases):
    """batch_diff.eval_cases with ONE change, for mutants that hang: in the per-input fallback the
    Rust side first gets FAST_RUST_TIMEOUT instead of 3 s; the first SLOW_RECHECK inputs that exceed
    it (grid order) are re-run with the harness's 3 s timeout, the rest are recorded as hangs
    without the re-run (counted in `unrechecked_slow`). Batch (12 s) and chunk (3 s) stages and the
    C side are unchanged. A deterministic program that answers within FAST_RUST_TIMEOUT gives the
    same answer within 3 s, so every kill found this way is a kill of the real harness; the only
    possible deviation is a missed kill on an unrechecked slow input (0.25-3 s), so the decision is
    exact when unrechecked_slow == 0 and otherwise a lower bound."""
    _, cl = run_batch(binc, cases)
    _, rl = run_batch(binr, cases)
    stats = {"unrechecked_slow": 0, "rechecked_slow": 0, "per_input_fallback": False}
    if len(cl) == len(cases) and len(rl) == len(cases):
        return list(zip(cases, cl, rl)), stats
    out = []
    for k in range(0, len(cases), 32):
        chunk = cases[k:k + 32]
        _, c = run_batch(binc, chunk, timeout=3)
        _, r = run_batch(binr, chunk, timeout=3)
        if len(c) == len(chunk) and len(r) == len(chunk):
            out.extend(zip(chunk, c, r)); continue
        stats["per_input_fallback"] = True
        for row in chunk:
            c1, _, cto, _ = run_one(binc, row, timeout=3)
            r1, _, rto, _ = run_one(binr, row, timeout=FAST_RUST_TIMEOUT)
            if rto:
                if stats["rechecked_slow"] < SLOW_RECHECK:
                    stats["rechecked_slow"] += 1
                    r1, _, rto, _ = run_one(binr, row, timeout=3)
                else:
                    stats["unrechecked_slow"] += 1
            out.append((row, "__TIMEOUT__" if cto else c1, "__TIMEOUT__" if rto else r1))
    return out, stats


def tally_first_kill(binc, binr, cases, binc_san=None, binc_alt=None, binc_msan=None):
    """batch_diff.tally with ONE change: it stops at the first candidate that tally would count as
    `value` or `rust_panic` (a kill). Same eval_cases, same -O0/-O1 pre-filter, same candidate
    tags, same san/msan verdicts, same order. If no kill exists it processes every candidate, so
    the non-kill counts (ub, timeout, rust_hang) equal tally's. Each ASan run costs ~0.7 s of
    start-up here, and a killed mutant typically has dozens of candidates, so this is what makes
    2,000+ mutants feasible. Agreement with the full tally is re-checked on a deterministic
    subset (--verify-every) and recorded in the result file."""
    out = {"cases": len(cases), "value": 0, "rust_panic": 0, "ub": 0, "timeout": 0, "agree": 0,
           "value_samples": [], "crash_samples": [], "early_exit": False}
    altl = run_batch(binc_alt, cases)[1] if binc_alt is not None else None
    cands = []
    evald, st = eval_cases_fast(binc, binr, cases)
    out.update(st)
    for i, (row, cl, rl) in enumerate(evald):
        if cl == "__PARSE__" or rl == "__PARSE__":
            continue
        if cl is not None and cl != "__TIMEOUT__" and cl == rl:
            out["agree"] += 1; continue
        if altl is not None and cl is not None:
            alt = altl[i] if i < len(altl) else None
            if alt is not None and alt != "__PARSE__" and alt != cl:
                out["ub"] += 1; continue
        if cl is None or cl == "__TIMEOUT__":
            cands.append((row, None, rl, "c_missing")); continue
        if rl == "__TIMEOUT__":
            cands.append((row, cl, rl, "hang")); continue
        if rl is None or rl == "__PANIC__":
            cands.append((row, cl, rl, "crash")); continue
        cands.append((row, cl, rl, "value"))
    out["candidates"] = len(cands)
    for row, cl, rl, tag in cands:
        if binc_san is not None:
            v = san_verdict(binc_san, row)
            if v == "infra":
                out["san_infra"] = out.get("san_infra", 0) + 1
            if v == "ub":
                out["ub"] += 1; continue
        if binc_msan is not None:
            v = san_verdict(binc_msan, row)
            if v == "infra":
                out["san_infra"] = out.get("san_infra", 0) + 1
            if v == "ub":
                out["ub"] += 1; continue
        if tag == "c_missing":
            out["ub" if binc_san is None else "timeout"] += 1; continue
        if tag == "hang":
            out["rust_hang"] = out.get("rust_hang", 0) + 1; continue
        if tag == "crash":
            out["rust_panic"] += 1; out["crash_samples"].append({"in": row, "c": cl})
        else:
            out["value"] += 1; out["value_samples"].append({"in": row, "c": cl, "rust": rl})
        out["early_exit"] = True
        break
    return out


def bin_name(mid):
    return re.sub(r"[^A-Za-z0-9_.@-]", "_", mid)


def job_mutant(args):
    func, mid, msrc, orc, workdir, verify = args
    t0 = time.time()
    md = tempfile.mkdtemp(dir=workdir)
    try:
        binr = run.build_rust(msrc, orc["nargs"], md)
        if not binr:
            return dict(func=func, id=mid, outcome="compile_fail", secs=round(time.time() - t0, 2))
        cases = grid(orc["nargs"])
        t = tally_first_kill(orc["binc"], binr, cases, binc_san=orc["binc_san"], binc_alt=orc["binc_alt"],
                             binc_msan=orc["binc_msan"])
        res = dict(func=func, id=mid, outcome=classify(t), value=t["value"], rust_panic=t["rust_panic"],
                   rust_hang=t.get("rust_hang", 0), ub=t["ub"], timeout=t["timeout"],
                   agree=t["agree"], cases=t["cases"], candidates=t["candidates"],
                   early_exit=t["early_exit"], value_samples=t["value_samples"],
                   crash_samples=t["crash_samples"], unrechecked_slow=t["unrechecked_slow"],
                   rechecked_slow=t["rechecked_slow"], per_input_fallback=t["per_input_fallback"])
        if verify:
            full = tally(orc["binc"], binr, cases, binc_san=orc["binc_san"], binc_alt=orc["binc_alt"],
                         binc_msan=orc["binc_msan"])
            res["verify_full_tally"] = {"outcome": classify(full), "value": full["value"],
                                        "rust_panic": full["rust_panic"], "rust_hang": full.get("rust_hang", 0),
                                        "ub": full["ub"], "timeout": full["timeout"],
                                        "same_kill_decision": (classify(full) in ("killed_value", "killed_panic")) ==
                                                              (res["outcome"] in ("killed_value", "killed_panic"))}
        keep = os.path.join(workdir, "bins", func, bin_name(mid))
        os.makedirs(os.path.dirname(keep), exist_ok=True)
        shutil.copy(binr, keep)
        res["bin"] = keep
        res["secs"] = round(time.time() - t0, 2)
        return res
    finally:
        shutil.rmtree(md, ignore_errors=True)


# ----------------------------------------------------------------------------------------------
# Equivalence analysis of survivors
# ----------------------------------------------------------------------------------------------
def shells(lo, hi, k, neg_only=False):
    """Yield (k-tuple, r) over [lo,hi]^k in increasing max-norm shells r = 0, 1, ... (deterministic).
    Shell r is partitioned by the FIRST coordinate whose |value| == r, so each tuple appears once."""
    rmax = max(abs(lo), abs(hi))
    for r in range(0, rmax + 1):
        vals = [v for v in range(max(lo, -r), min(hi, r) + 1)]
        ring = [v for v in vals if abs(v) == r]
        inner = [v for v in vals if abs(v) < r]
        if not ring:
            continue
        for i in range(k):
            for pre in product(inner, repeat=i):
                for x in ring:
                    for post in product(vals, repeat=k - i - 1):
                        tup = pre + (x,) + post
                        if neg_only and not any(v < 0 for v in tup):
                            continue
                        yield tup, r


def run_chunk(binp, rows, timeout):
    inp = ("\n".join(" ".join(str(x) for x in r) for r in rows) + "\n").encode()
    try:
        p = subprocess.run([binp], input=inp, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return p.stdout.decode("latin1").splitlines(), False
    except subprocess.TimeoutExpired:
        return None, True


def ub_clean(orc, row):
    """Primary-harness filter for ONE input: True iff the C is UB-clean (ASan/UBSan, -O0==-O1)."""
    if orc["binc_san"] is not None:
        v = san_verdict(orc["binc_san"], row)
        if v == "ub":
            return False, "san_ub"
        if v == "infra":
            return False, "san_infra"
    if orc["binc_alt"] is not None:
        a, _, to, _ = run_one(orc["binc_alt"], row, timeout=5)
        c, _, to2, _ = run_one(orc["binc"], row, timeout=5)
        if to or to2:
            return False, "alt_timeout"
        if a != c:
            return False, "opt_dependent"
    return True, "clean"


def _outs(binp, buf, left, deadline=None):
    """Outputs of one binary on a chunk; per-input fallback if the batch crashed/hung (1 s per
    input; once `deadline` passes, the remaining inputs are marked __SKIPPED__ and not counted)."""
    out, to = run_chunk(binp, buf, timeout=left)
    if not to and out is not None and len(out) == len(buf):
        return out, False
    res = []
    for row in buf:
        if deadline is not None and time.time() > deadline:
            res.append("__SKIPPED__"); continue
        o, _, to1, _ = run_one(binp, row, timeout=1)
        res.append("__TIMEOUT__" if to1 else o)
    return res, True


def search(orc, binr, gen, budget, binr_orig=None, chunk=2048, max_checks=24):
    """Stream inputs from gen (smallest first) through the C oracle, the mutant `binr` and, when
    given, the ORIGINAL translation `binr_orig`. Returns the first KILL WITNESS: an input where
    the original agrees with the C, the mutant does not, and the C is UB-clean under the primary
    harness's filter (ASan/UBSan + -O0/-O1) -- i.e. an input that, had the grid contained it, the
    harness would have used to kill the mutant. Also records the first input where the mutant's
    output differs from the original's at all (diff_vs_orig: evidence of non-equivalence, whatever
    the C does there), and how many inputs the ORIGINAL itself diverges from the C on.
    With binr_orig=None the original is taken to equal the C (used to check the original itself)."""
    t0 = time.time()
    checked, ub_div, checks, orig_div = 0, 0, 0, 0
    hang_witness, diff_vs_orig = None, None
    last_r = 0
    complete = True
    fallback = 0
    it = iter(gen)
    while True:
        buf = []
        for tup, r in it:
            buf.append(list(tup)); last_r = r
            if len(buf) >= chunk:
                break
        if not buf:
            break
        if time.time() - t0 > budget:
            complete = False
            break
        left = max(3.0, budget - (time.time() - t0))
        dl = t0 + budget + 10
        cl, f1 = _outs(orc["binc"], buf, left, dl)
        ml, f2 = _outs(binr, buf, left, dl)
        ol, f3 = _outs(binr_orig, buf, left, dl) if binr_orig is not None else (cl, False)
        fallback += int(f1 or f2 or f3)
        for row, c, m, o in zip(buf, cl, ml, ol):
            if "__SKIPPED__" in (c, m, o):
                complete = False
                continue
            checked += 1
            if m != o and diff_vs_orig is None and binr_orig is not None:
                diff_vs_orig = {"in": row, "orig": o, "mutant": m, "c": c}
            if c is None or c == "__TIMEOUT__" or c == "__PARSE__":
                continue        # C crashed/hung: not a valid oracle value here
            if m == c:
                continue
            if o != c:
                orig_div += 1   # the ORIGINAL translation also differs from the C here
                continue
            if checks >= max_checks or time.time() - t0 > budget + 30:
                complete = False
                continue
            checks += 1
            ok, why = ub_clean(orc, row)
            if not ok:
                ub_div += 1
                continue
            kind = "hang" if m == "__TIMEOUT__" else ("panic" if (m is None or m == "__PANIC__") else "value")
            w = {"in": row, "c": c, "rust": m, "kind": kind}
            if kind == "hang":
                if hang_witness is None:
                    hang_witness = w
                continue
            return dict(witness=w, diff_vs_orig=diff_vs_orig, checked=checked, complete=False,
                        ub_divergences=ub_div, orig_divergences=orig_div,
                        secs=round(time.time() - t0, 1), max_norm_reached=last_r, fallback_chunks=fallback)
        if time.time() - t0 > budget:
            complete = False
            break
    return dict(witness=None, hang_witness=hang_witness, diff_vs_orig=diff_vs_orig, checked=checked,
                complete=complete, ub_divergences=ub_div, orig_divergences=orig_div,
                secs=round(time.time() - t0, 1), max_norm_reached=last_r, fallback_chunks=fallback)


def large_grid(nargs):
    g = grid_for(-2**31, 2**31 - 1, 64 if nargs == 1 else max(4, int(round(4096 ** (1.0 / nargs)))))
    rows = [[v] for v in g] if nargs == 1 else _spread([g] * nargs, 4096)
    rows.sort(key=lambda r: (max(abs(x) for x in r), r))
    return [(tuple(r), max(abs(x) for x in r)) for r in rows]


def confirm(orc, binr, row):
    """Re-run ONE witness input through the primary harness's own tally."""
    t = tally(orc["binc"], binr, [list(row)], binc_san=orc["binc_san"], binc_alt=orc["binc_alt"],
              binc_msan=orc["binc_msan"])
    return classify(t)


def job_control(args):
    func, = args
    r = run.test_function(func, N_GRID, grid=GRID)   # the unmodified primary harness, end to end
    st = "survived" if (r.get("status") == "tested" and r["value_div"] == 0 and r["rust_panic_c_ok"] == 0
                        and r.get("rust_hang", 0) == 0 and r["ub_div"] == 0 and r["timeout_div"] == 0) else "not_agree"
    return dict(func=func, id="control", outcome=st, **{k: r.get(k) for k in
                ("status", "cases", "value_div", "rust_panic_c_ok", "rust_hang", "ub_div", "timeout_div", "san", "msan", "alt")})


def job_equiv(args):
    func, mid, binr, orc, budgets = args
    k = orc["nargs"]
    bo = orc["binr_orig"]
    res = {"func": func, "id": mid}
    phases = [("A_in_domain", lambda: shells(0, MAX_ABS, k), budgets[0], 2048),
              ("B_negative", lambda: shells(-MAX_ABS, MAX_ABS, k, neg_only=True), budgets[1], 2048),
              ("C_large", lambda: large_grid(k), budgets[2], 256)]
    for name, gen, bud, ch in phases:
        r = search(orc, binr, gen(), bud, binr_orig=bo, chunk=ch)
        res[name] = r
        if r["witness"] is not None:
            r["witness_confirmed_by_tally"] = confirm(orc, binr, r["witness"]["in"])
            r["orig_on_witness_by_tally"] = confirm(orc, bo, r["witness"]["in"])
            res["verdict"] = "killable_in_domain" if name == "A_in_domain" else "killable_out_of_domain"
            return res
    ph = [res[n] for n, *_ in phases]
    if any(p.get("hang_witness") for p in ph):
        res["verdict"] = "rust_hang_only"
    elif any(p.get("diff_vs_orig") for p in ph):
        res["verdict"] = "differs_from_original_only_where_C_has_UB_or_original_also_diverges"
    else:
        res["verdict"] = "no_difference_found"
    res["A_exhaustive"] = bool(res["A_in_domain"]["complete"])
    return res


def job_orig_check(args):
    """The ORIGINAL translation against the C on the same extended domain (phases A, B, C)."""
    func, orc, budgets = args
    k = orc["nargs"]
    res = {"func": func}
    for name, gen, bud, ch in [("A_in_domain", shells(0, MAX_ABS, k), budgets[0], 2048),
                               ("B_negative", shells(-MAX_ABS, MAX_ABS, k, neg_only=True), budgets[1], 2048),
                               ("C_large", large_grid(k), budgets[2], 256)]:
        r = search(orc, orc["binr_orig"], gen, bud, binr_orig=None, chunk=ch)
        if r["witness"] is not None:
            r["witness_confirmed_by_tally"] = confirm(orc, orc["binr_orig"], r["witness"]["in"])
        res[name] = r
    return res


# ----------------------------------------------------------------------------------------------
# Statistics
# ----------------------------------------------------------------------------------------------
def wilson(k, n, z=1.959963984540054):
    if n == 0:
        return None
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def cluster_bootstrap(per_func, B=10000, seed=12345):
    """95% percentile CI of the pooled kill rate, resampling FUNCTIONS (mutants are clustered)."""
    pairs = [(k, n) for k, n in per_func if n > 0]
    if not pairs:
        return None
    rng = random.Random(seed)
    stats = []
    m = len(pairs)
    for _ in range(B):
        kk = nn = 0
        for _ in range(m):
            k, n = pairs[rng.randrange(m)]
            kk += k; nn += n
        stats.append(kk / nn)
    stats.sort()
    return [round(stats[int(0.025 * B)], 4), round(stats[int(0.975 * B) - 1], 4)]


def rate(k, n):
    return {"killed": k, "assessable": n, "rate": (round(k / n, 4) if n else None), "wilson95": wilson(k, n)}


# ----------------------------------------------------------------------------------------------
def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def provenance(argv):
    def cmd(c):
        try:
            return subprocess.run(c, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True).stdout.strip()
        except Exception as e:  # noqa
            return f"unavailable ({e})"
    files = ["systems/vert/mutate_primary.py", "systems/vert/run.py", "systems/batch_diff.py",
             "systems/enumerate_inputs.py", "results/" + POP_FILE[GRID]]
    return {
        "script": "systems/vert/mutate_primary.py",
        "grid": GRID,
        "command": "python3 " + " ".join(argv),
        "docker": os.environ.get("DOCKER_CMD", "docker run --rm --cpus 6 -v $PWD:/work -w /work crust-stable ..."),
        "git_rev": os.environ.get("GIT_REV", "unknown"),
        "git_dirty_note": os.environ.get("GIT_DIRTY", ""),
        "host": os.environ.get("HOST_DESC", platform.system()),
        "arch": platform.machine(),
        "container_kernel": platform.release(),
        "image": {"name": "crust-stable", "id": os.environ.get("IMAGE_ID", "unknown")},
        "toolchains": {"rustc": cmd(["rustc", "--version"]), "gcc": cmd(["gcc", "--version"]).splitlines()[0],
                       "clang": shutil.which("clang") or "absent (so MSan unavailable; -O0/-O1 cross-check used, as in vert_diff.json)",
                       "python": platform.python_version()},
        "file_sha256": {f: sha(os.path.join(REPO, f)) for f in files if os.path.exists(os.path.join(REPO, f))},
        "date": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }



ARRAYS_NOTE = """
--arrays mode (results/vert_arrays_mutation_primary.json): the same mutants on the VERT array/float
harness (run_arrays.py). Population: results/vert_arrays_adjusted.json rows with kind == agree.
Harness path: run_arrays.build_c (normal, san, alt unless the C uses float), run_arrays.build_rust,
run_arrays.gen_cases(comps, 240), run_arrays.run_batch, run_arrays.classify (early exit at the first
kill, re-checked against the full classify on every --verify-every'th mutant). Kill = classify value
> 0 or crash > 0. run_arrays.run_batch returns [] when the Rust batch times out (25 s), which
classify counts as a crash on every input; such kills are flagged rust_batch_timeout and the rate is
also reported without them. Equivalence of survivors: arrays with every element in [-40, 40], int
scalars in the harness's own [1, N], float scalars on a 0.25 grid over [-16, 16] when the C parameter is float/double and on the integers
in [-16, 16] when the C parameter is an integer (the driver converts to the C int); exhaustive when
the product is <= 200,000 tuples, else a seeded sample of 200,000 plus the harness rows; a
divergence counts only if the C is ASan/UBSan-clean there and (integer return, non-float C)
-O0 == -O1, using run_arrays.vals_equal's float tolerance.
"""

# ==============================================================================================
# --arrays: the same measurement for the VERT array/float harness (run_arrays.py)
# ==============================================================================================
# Population: results/vert_arrays_adjusted.json rows with kind == "agree" (81: the 82 raw agreeing
# functions minus MAXIMUM_PRODUCT_SUBARRAY_ADDED_NEGATIVE_PRODUCT_CASE, which has 0 assessable
# inputs). Harness: run_arrays.test_one's path exactly (build_c normal/san, alt unless the C uses
# float, gen_cases(comps, 240), run_batch, classify). Kill rule: classify reports value > 0 or
# crash > 0 (run_arrays' "rust_crash", i.e. its rust_panic). Note that run_arrays.run_batch returns
# [] when the Rust batch times out (25 s), so classify counts a Rust HANG as a crash on every input;
# we follow the harness but record such mutants (rust_batch_timeout) and report the rate both ways.
# Equivalence of survivors: a larger in-domain set (arrays with every element in [-40, 40],
# int scalars in the harness's own [1, N] range, float scalars on a 0.25 grid over [-16, 16], or on the
# integers in [-16, 16] where the C parameter is an integer),
# exhaustive when the product is <= 200,000 tuples, else a seeded sample of 200,000 plus the
# harness's own rows; a divergence counts only if the C is ASan/UBSan-clean on it and (integer
# returns, non-float C) -O0 == -O1, with run_arrays.vals_equal's float tolerance.
import run_arrays as ra  # noqa: E402

A_CAP = 240


def arrays_population():
    d = json.load(open(os.path.join(REPO, "results", "vert_arrays_adjusted.json")))
    return [r["func"] for r in d["rows"] if r["kind"] == "agree"]


def arrays_ctx(func, d):
    rust, _ = ra.extract(func)
    sig = ra.parse_fgold(rust)
    comps = [ra.comp(t) for t in sig["params"]]
    csrc = open(os.path.join(ra.TC, func, f"{func}.c"), errors="replace").read()
    binc = ra.build_c(csrc, sig, comps, d)
    binc_san = ra.build_c(csrc, sig, comps, d, mode="san")
    uses_fp = bool(re.search(r"\b(float|double)\b", ra.strip_scaffold(csrc)))
    binc_alt = None if uses_fp else ra.build_c(csrc, sig, comps, d, mode="alt")
    rows = ra.gen_cases(comps, A_CAP)
    od = os.path.join(d, "orig"); os.makedirs(od, exist_ok=True)
    binr_orig = ra.build_rust(rust, sig, comps, od)     # the unmutated translation
    # C parameter types, positionally matched to the Rust signature: a Rust f32 parameter whose C
    # parameter is an integer only gets integer values in the extended domain (the driver converts
    # the value to the C int; a fractional value is outside the C function's domain).
    m = re.search(r"\bf_gold\s*\(([^)]*)\)", ra.strip_scaffold(csrc))
    cparams = [x.strip() for x in m.group(1).split(",")] if m else []
    c_float = [bool(re.search(r"\b(float|double)\b", cp)) for cp in cparams] if len(cparams) == len(comps) \
        else [False] * len(comps)
    return dict(sig=sig, comps=comps, binc=binc, binc_san=binc_san, binc_alt=binc_alt, rows=rows,
                ret_is_float=sig["ret"] in ra.FLOAT_SCALARS, binr_orig=binr_orig, c_float=c_float,
                c_uses_fp=uses_fp, cparams=cparams)


def ra_run_batch_flag(binp, rows, timeout=25):
    """run_arrays.run_batch, plus a flag saying whether it timed out (same return value)."""
    inp = ("\n".join(" ".join(str(x) for x in r) for r in rows) + "\n").encode()
    try:
        p = subprocess.run([binp], input=inp, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return p.stdout.decode("latin1").splitlines(), False
    except subprocess.TimeoutExpired:
        return [], True


def ra_classify_first_kill(rows, cl, rl, ret_is_float, binc_san, binc_alt=None):
    """run_arrays.classify with ONE change: stop at the first value/crash candidate it would count."""
    agree = ub = 0
    altl = ra.run_batch(binc_alt, rows) if (binc_alt is not None and not ret_is_float) else None
    cands = []
    for i, row in enumerate(rows):
        a = cl[i] if i < len(cl) else None
        b = rl[i] if i < len(rl) else None
        if a == "__PARSE__":
            continue
        if altl is not None and a is not None:
            alt = altl[i] if i < len(altl) else None
            if alt is not None and alt != "__PARSE__" and alt != a:
                ub += 1; continue
        if a is None:
            cands.append((row, a, b, "c_missing")); continue
        if b is None or b == "__PANIC__" or b == "__PARSE__":
            cands.append((row, a, b, "crash")); continue
        if ra.vals_equal(a, b, ret_is_float):
            agree += 1
        else:
            cands.append((row, a, b, "value"))
    value = crash = timeout = 0
    sample = None
    for row, a, b, tag in cands:
        if binc_san is not None:
            sline, src, sto, sstderr = ra.run_one(binc_san, row)
            if sto:
                timeout += 1; continue
            if src != 0 or ra.SAN_RE.search(sstderr):
                ub += 1; continue
        if tag == "c_missing":
            timeout += 1; continue
        if tag == "crash":
            crash += 1
        else:
            value += 1
        sample = {"in": row, "c": a, "rust": b}
        break
    return {"agree": agree, "value": value, "crash": crash, "ub": ub, "timeout": timeout,
            "candidates": len(cands), "sample": sample}


def ra_kind(r):
    return ("killed_value" if r["value"] > 0 else "killed_panic" if r["crash"] > 0
            else "ub_only" if r["ub"] > 0 else "timeout_only" if r["timeout"] > 0 else "survived")


def job_arrays_control(args):
    func, = args
    r = ra.test_one(func, A_CAP)
    return dict(func=func, id="control", outcome=("survived" if r.get("kind") == "agree" else "not_agree"),
                kind=r.get("kind"), status=r.get("status"), agree=r.get("agree"), value=r.get("value"),
                crash=r.get("crash"), ub=r.get("ub"), timeout=r.get("timeout"))


def job_arrays_mutant(args):
    func, mid, msrc, ctx, workdir, verify = args
    t0 = time.time()
    md = tempfile.mkdtemp(dir=workdir)
    try:
        binr = ra.build_rust(msrc, ctx["sig"], ctx["comps"], md)
        if not binr:
            return dict(func=func, id=mid, outcome="compile_fail", secs=round(time.time() - t0, 2))
        rows = ctx["rows"]
        cl = ra.run_batch(ctx["binc"], rows)
        rl, rto = ra_run_batch_flag(binr, rows)
        r = ra_classify_first_kill(rows, cl, rl, ctx["ret_is_float"], ctx["binc_san"], ctx["binc_alt"])
        res = dict(func=func, id=mid, outcome=ra_kind(r), rust_batch_timeout=rto, **r)
        if verify:
            full = ra.classify(rows, cl, rl, ctx["ret_is_float"], ctx["binc_san"], ctx["binc_alt"])
            fk = "killed" if (full["value"] > 0 or full["crash"] > 0) else "not"
            res["verify_full_classify"] = {"value": full["value"], "crash": full["crash"], "ub": full["ub"],
                                           "timeout": full["timeout"],
                                           "same_kill_decision": (fk == "killed") == (res["outcome"] in ("killed_value", "killed_panic"))}
        keep = os.path.join(workdir, "bins", func, bin_name(mid))
        os.makedirs(os.path.dirname(keep), exist_ok=True)
        shutil.copy(binr, keep)
        res["bin"] = keep
        res["secs"] = round(time.time() - t0, 2)
        return res
    finally:
        shutil.rmtree(md, ignore_errors=True)


def arrays_domain(ctx, cap=200000, seed=20261113):
    axes = []
    for i, c in enumerate(ctx["comps"]):
        if c[0] == "arr":
            axes.extend([list(range(-40, 41))] * c[2])
        elif c[1] in ra.FLOAT_SCALARS and ctx["c_float"][i]:
            axes.append([x / 4 for x in range(-64, 65)])
        elif c[1] in ra.FLOAT_SCALARS:
            axes.append([float(x) for x in range(-16, 17)])   # C takes an int here
        else:
            maxN = max([cc[2] for cc in ctx["comps"] if cc[0] == "arr"] or [4])
            axes.append(list(range(1, maxN + 1)))
    total = 1
    for a in axes:
        total *= len(a)
    if total <= cap:
        return [list(t) for t in product(*axes)], True, total
    rng = random.Random(seed)
    rows = [list(r) for r in ctx["rows"]]
    seen = {tuple(r) for r in rows}
    while len(rows) < cap:
        t = tuple(rng.choice(a) for a in axes)
        if t not in seen:
            seen.add(t); rows.append(list(t))
    return rows, False, total


def arrays_search(ctx, binr, budget, binr_orig):
    """As search() for the scalar harness: first input where the original agrees with the C, the
    mutant does not, and the C is clean (ASan/UBSan; -O0 == -O1 for integer returns of non-float C).
    binr_orig=None checks the original itself against the C."""
    t0 = time.time()
    rows, exhaustive, total = arrays_domain(ctx)
    fl = ctx["ret_is_float"]
    checked = ub_div = checks = orig_div = 0
    witness = diff_vs_orig = None
    stopped = False
    for k in range(0, len(rows), 4096):
        if time.time() - t0 > budget:
            stopped = True; break
        chunk = rows[k:k + 4096]
        left = max(3.0, budget - (time.time() - t0))
        cl, cto = run_chunk(ctx["binc"], chunk, timeout=left)
        ml, mto = run_chunk(binr, chunk, timeout=left)
        ol, oto = run_chunk(binr_orig, chunk, timeout=left) if binr_orig is not None else (cl, False)
        if cto or mto or oto or any(x is None or len(x) != len(chunk) for x in (cl, ml, ol)):
            stopped = True; break    # a crash/hang truncates the batch; stop (coverage recorded)
        for row, a, m, o in zip(chunk, cl, ml, ol):
            checked += 1
            same_mo = (m == o) or (m not in (None, "__PANIC__", "__PARSE__") and o not in (None, "__PANIC__", "__PARSE__") and ra.vals_equal(o, m, fl))
            if same_mo:
                continue
            if diff_vs_orig is None and binr_orig is not None:
                diff_vs_orig = {"in": row, "orig": o, "mutant": m, "c": a}
            if a == "__PARSE__":
                continue
            if m not in (None, "__PANIC__", "__PARSE__") and ra.vals_equal(a, m, fl):
                continue
            if not (o not in (None, "__PANIC__", "__PARSE__") and ra.vals_equal(a, o, fl)):
                orig_div += 1; continue
            if checks >= 24:
                continue
            checks += 1
            ok = True
            if ctx["binc_san"] is not None:
                _, src, sto, err = ra.run_one(ctx["binc_san"], row)
                if sto or src != 0 or ra.SAN_RE.search(err):
                    ok = False
            if ok and ctx["binc_alt"] is not None and not fl:
                alt, _, _, _ = ra.run_one(ctx["binc_alt"], row)
                if alt != a:
                    ok = False
            if not ok:
                ub_div += 1; continue
            witness = {"in": row, "c": a, "rust": m, "kind": "panic" if m in (None, "__PANIC__") else "value"}
            break
        if witness:
            break
    complete = (not stopped) and witness is None and checked == len(rows) and checks < 24
    return dict(witness=witness, diff_vs_orig=diff_vs_orig, checked=checked, domain_size=total,
                domain_rows=len(rows), exhaustive=bool(exhaustive and complete), complete=complete,
                ub_divergences=ub_div, orig_divergences=orig_div, secs=round(time.time() - t0, 1))


def arrays_confirm(ctx, binr, row):
    """Re-run ONE witness input through run_arrays' own batch + classify."""
    rows = [list(row)]
    r = ra.classify(rows, ra.run_batch(ctx["binc"], rows), ra.run_batch(binr, rows), ctx["ret_is_float"],
                    ctx["binc_san"], ctx["binc_alt"])
    return ("killed_value" if r["value"] > 0 else "killed_panic" if r["crash"] > 0
            else "ub_only" if r["ub"] > 0 else "timeout_only" if r["timeout"] > 0 else "agree")


def job_arrays_equiv(args):
    func, mid, binr, ctx, budget = args
    r = arrays_search(ctx, binr, budget, ctx["binr_orig"])
    if r["witness"]:
        r["witness_confirmed_by_classify"] = arrays_confirm(ctx, binr, r["witness"]["in"])
        r["orig_on_witness_by_classify"] = arrays_confirm(ctx, ctx["binr_orig"], r["witness"]["in"])
        v = "killable_in_domain"
    elif r["diff_vs_orig"]:
        v = "differs_from_original_only_where_C_has_UB_or_original_also_diverges"
    else:
        v = "no_difference_found"
    return dict(func=func, id=mid, verdict=v, **r)


def job_arrays_orig_check(args):
    func, ctx, budget = args
    r = arrays_search(ctx, ctx["binr_orig"], budget, None)
    if r["witness"]:
        r["witness_confirmed_by_classify"] = arrays_confirm(ctx, ctx["binr_orig"], r["witness"]["in"])
    return dict(func=func, **r)


def main_arrays(args):
    funcs = arrays_population()
    pop_n = len(funcs)
    if args.funcs:
        want = set(args.funcs.split(","))
        funcs = [f for f in funcs if f in want]
    if args.limit:
        funcs = funcs[:args.limit]
    plan = {}
    for f in funcs:
        _rust, _t, names, muts, sampled = mutants_for(f, args.cap, args.seed)
        plan[f] = dict(reached=names, mutants=muts, sampling=sampled)
    if args.count_only:
        from collections import Counter
        c = Counter(m["op"] for p in plan.values() for m in p["mutants"])
        print(json.dumps({"functions": len(plan), "mutants": sum(c.values()), "by_op": c}, indent=1))
        return
    work = os.environ.get("MUTPRIM_WORK") or tempfile.mkdtemp(prefix="mutprim_arr_")
    t_start = time.time()
    ctxs = {}
    for f in funcs:
        d = os.path.join(work, "c", f); os.makedirs(d, exist_ok=True)
        ctxs[f] = arrays_ctx(f, d)
    jobs, gi = [], 0
    for f in funcs:
        for m in plan[f]["mutants"]:
            verify = bool(args.verify_every) and gi % args.verify_every == 0
            jobs.append((f, m["id"], m["src"], ctxs[f], work, verify)); gi += 1
    results = {}
    with ProcessPoolExecutor(max_workers=args.jobs) as ex:
        futs = [ex.submit(job_arrays_control, (f,)) for f in funcs]
        futs += [ex.submit(job_arrays_mutant, j) for j in jobs]
        for i, fu in enumerate(as_completed(futs)):
            r = fu.result()
            results[(r["func"], r["id"])] = r
            if (i + 1) % 100 == 0:
                print(f"  arrays {i + 1}/{len(futs)} ({time.time() - t_start:.0f}s)", flush=True)
    t_mut = time.time() - t_start
    equiv = {}
    if args.equiv:
        budget = float(args.budgets.split(",")[0])
        ejobs = [(f, m["id"], results[(f, m["id"])]["bin"], ctxs[f], budget) for f in funcs
                 if results[(f, "control")]["outcome"] == "survived" for m in plan[f]["mutants"]
                 if results[(f, m["id"])]["outcome"] in ("survived", "ub_only", "timeout_only")]
        print(f"  arrays equivalence analysis: {len(ejobs)} not-killed mutants", flush=True)
        orig_ext = {}
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            ofuts = [ex.submit(job_arrays_orig_check, (f, ctxs[f], budget)) for f in funcs]
            for fu in as_completed([ex.submit(job_arrays_equiv, j) for j in ejobs]):
                r = fu.result()
                equiv[(r["func"], r["id"])] = r
            for fu in ofuts:
                r = fu.result()
                orig_ext[r["func"]] = r
    t_all = time.time() - t_start
    HEAD_OPS = ("ror", "aor", "const", "f32")
    ctrl_ok = [f for f in funcs if results[(f, "control")]["outcome"] == "survived"]
    per_function, all_mut = [], []
    for f in funcs:
        rows = []
        for m in plan[f]["mutants"]:
            r = results[(f, m["id"])]
            row = {k: m[k] for k in ("id", "op", "sub", "fn", "line", "orig", "repl")}
            row.update({k: r[k] for k in ("outcome", "value", "crash", "ub", "timeout", "agree", "candidates",
                                          "sample", "rust_batch_timeout", "verify_full_classify", "secs") if k in r})
            if (f, m["id"]) in equiv:
                row["equiv"] = equiv[(f, m["id"])]
            rows.append(row)
            all_mut.append((f, row))
        ctx = ctxs[f]
        per_function.append({"func": f, "params": ctx["sig"]["params"], "ret": ctx["sig"]["ret"],
                             "c_params": ctx["cparams"], "c_uses_float": ctx["c_uses_fp"],
                             "harness_inputs": len(ctx["rows"]), "reached_functions": plan[f]["reached"],
                             "original_extended_domain": (orig_ext.get(f) if args.equiv else None),
                             "sampling": plan[f]["sampling"], "control": results[(f, "control")], "mutants": rows})

    def summarize(sel):
        from collections import Counter
        ass = [(f, r) for f, r in sel if r["outcome"] != "compile_fail"]
        kil = [(f, r) for f, r in ass if r["outcome"] in ("killed_value", "killed_panic")]
        kil_nohang = [(f, r) for f, r in kil if not r.get("rust_batch_timeout")]
        pf = {}
        for f, r in ass:
            a = pf.setdefault(f, [0, 0]); a[1] += 1
            if r["outcome"] in ("killed_value", "killed_panic"):
                a[0] += 1
        return {"mutants": len(sel), "compile_fail": len(sel) - len(ass), **rate(len(kil), len(ass)),
                "cluster_bootstrap95_by_function": cluster_bootstrap(list(pf.values())),
                "outcomes": dict(Counter(r["outcome"] for _, r in sel)),
                "killed_only_by_rust_batch_timeout": len(kil) - len(kil_nohang),
                "hang_kills_not_counted": rate(len(kil_nohang), len(ass))}

    sel_all = [(f, r) for f, r in all_mut if f in ctrl_ok]
    head = [(f, r) for f, r in sel_all if r["op"] in HEAD_OPS]
    pfr = []
    for pf in per_function:
        if pf["func"] not in ctrl_ok:
            continue
        hs = [r for r in pf["mutants"] if r["op"] in HEAD_OPS and r["outcome"] != "compile_fail"]
        k = sum(1 for r in hs if r["outcome"] in ("killed_value", "killed_panic"))
        pfr.append({"func": pf["func"], "params": pf["params"], "harness_inputs": pf["harness_inputs"], **rate(k, len(hs))})
    from collections import Counter
    ver = Counter((r["outcome"], r["equiv"]["verdict"], r["equiv"]["exhaustive"]) for f, r in head if "equiv" in r)
    oe = {f: orig_ext[f] for f in ctrl_ok if args.equiv and f in orig_ext}
    orig_summary = {"rule": "the unmutated translation vs the C on the same extended in-domain set",
                    "functions": len(oe),
                    "diverge": sorted(f for f, r in oe.items() if r["witness"]),
                    "clean_exhaustive": sum(1 for r in oe.values() if r["exhaustive"]),
                    "clean_sampled_or_partial": sum(1 for r in oe.values() if not r["witness"] and not r["exhaustive"])}
    ver_op = {}
    for f, r in head:
        if "equiv" in r:
            ver_op.setdefault(r["op"], Counter())[r["equiv"]["verdict"]] += 1
    rates_ = [p["rate"] for p in pfr if p["rate"] is not None]
    summary = {
        "population": {"definition": "results/vert_arrays_adjusted.json rows with kind == agree",
                       "functions": pop_n, "run": len(funcs), "control_agree": len(ctrl_ok),
                       "control_not_agree": [results[(f, "control")] for f in funcs if f not in ctrl_ok]},
        "headline_ops": list(HEAD_OPS),
        "overall_headline": summarize(head),
        "by_operator": {op: summarize([(f, r) for f, r in sel_all if r["op"] == op]) for op in ("ror", "aor", "const", "f32", "bnd")},
        "overall_including_bnd": summarize(sel_all),
        "by_input_count": {str(n): summarize([(f, r) for f, r in head if len(ctxs[f]["rows"]) == n])
                           for n in sorted({len(ctxs[f]["rows"]) for f in ctrl_ok})},
        "per_function_distribution": {
            "functions_with_assessable_mutants": len(rates_),
            "functions_with_no_assessable_mutant": [p["func"] for p in pfr if p["rate"] is None],
            "min": min(rates_) if rates_ else None,
            "median": sorted(rates_)[len(rates_) // 2] if rates_ else None,
            "functions_at_100pct": sum(1 for x in rates_ if x == 1.0),
            "functions_at_0pct": [p["func"] for p in pfr if p["rate"] == 0.0],
        },
        "survivor_equivalence_headline": {
            "by_outcome_verdict_exhaustive": {f"{a}|{b}|exhaustive={c}": n for (a, b, c), n in sorted(ver.items())},
            "by_operator": {op: dict(c) for op, c in ver_op.items()},
            "budget_seconds": args.budgets.split(",")[0]},
        "original_translation_extended_domain": orig_summary,
        "early_exit_verification": {
            "verified": sum(1 for _, r in all_mut if "verify_full_classify" in r),
            "same_kill_decision": sum(1 for _, r in all_mut if r.get("verify_full_classify", {}).get("same_kill_decision")),
            "disagreements": [r["id"] for _, r in all_mut if "verify_full_classify" in r and not r["verify_full_classify"]["same_kill_decision"]]},
        "seconds": {"mutation": round(t_mut), "total": round(t_all)},
    }
    out = {"provenance": provenance(sys.argv), "rules": __doc__ + ARRAYS_NOTE, "summary": summary,
           "per_function_rates": pfr, "per_function": per_function}
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=1)
    print(json.dumps(summary, indent=1)[:5000])
    if not os.environ.get("MUTPRIM_WORK"):
        shutil.rmtree(work, ignore_errors=True)


def finish(args, funcs, per_function, pop_n, t_mut, t_all, prov):
    """Aggregate per-function rows into the summary and write args.out (shared by a normal run and
    --merge, so a merged sharded run is summarised by exactly the same code)."""
    HEAD_OPS = ("ror", "aor", "const", "f32")
    controls = {pf["func"]: pf["control"] for pf in per_function}
    nargs_of = {pf["func"]: pf["nargs"] for pf in per_function}
    all_mut = [(pf["func"], r) for pf in per_function for r in pf["mutants"]]
    orig_ext = {pf["func"]: pf["original_extended_domain"] for pf in per_function
                if pf.get("original_extended_domain")}
    def summarize(sel):
        sel = [(f, r) for f, r in sel]
        tot = len(sel)
        cf = sum(1 for _, r in sel if r["outcome"] == "compile_fail")
        ass = [(f, r) for f, r in sel if r["outcome"] != "compile_fail"]
        kil = sum(1 for _, r in ass if r["outcome"] in ("killed_value", "killed_panic"))
        from collections import Counter
        oc = Counter(r["outcome"] for _, r in sel)
        pf = {}
        for f, r in ass:
            a = pf.setdefault(f, [0, 0]); a[1] += 1
            if r["outcome"] in ("killed_value", "killed_panic"):
                a[0] += 1
        lb = sum(1 for _, r in ass if r["outcome"] not in ("killed_value", "killed_panic") and r.get("unrechecked_slow", 0) > 0)
        out = {"mutants": tot, "compile_fail": cf, **rate(kil, len(ass)),
               "not_killed_with_unrechecked_slow_inputs": lb,
               "cluster_bootstrap95_by_function": cluster_bootstrap(list(pf.values())),
               "outcomes": dict(oc),
               "hang_counted_as_detected": rate(kil + oc.get("rust_hang", 0), len(ass))}
        return out

    ctrl_ok = [f for f in funcs if controls[f]["outcome"] == "survived"]
    sel_all = [(f, r) for f, r in all_mut if f in ctrl_ok]
    head = [(f, r) for f, r in sel_all if r["op"] in HEAD_OPS]
    summary = {
        "population": {"definition": "results/" + POP_FILE[GRID] + ": status tested and value_div=rust_panic=rust_hang=ub_div=timeout_div=0",
                       "grid": GRID,
                       "functions": pop_n, "run": len(funcs),
                       "control_agree": len(ctrl_ok),
                       "control_not_agree": [{"func": f, **controls[f]} for f in funcs if f not in ctrl_ok]},
        "headline_ops": list(HEAD_OPS),
        "overall_headline": summarize(head),
        "by_operator": {op: summarize([(f, r) for f, r in sel_all if r["op"] == op]) for op in ("ror", "aor", "const", "f32", "bnd")},
        "aor_binary_only_legacy_comparable": summarize([(f, r) for f, r in sel_all if r["op"] == "aor" and "=" not in r["sub"]]),
        "ror_plus_binary_aor_legacy_operator_set": summarize([(f, r) for f, r in sel_all if r["op"] == "ror" or (r["op"] == "aor" and "=" not in r["sub"])]),
        "headline_by_nargs": {str(n): summarize([(f, r) for f, r in head if nargs_of[f] == n])
                              for n in sorted({nargs_of[f] for f in ctrl_ok})},
        "f_gold_only_headline": summarize([(f, r) for f, r in head if r["fn"] == "f_gold"]),
        "overall_including_bnd": summarize(sel_all),
        "seconds": {"mutation": round(t_mut), "total": round(t_all)},
        "early_exit_verification": {
            "rule": "every --verify-every'th mutant (global order) also run through the full batch_diff.tally",
            "verified": sum(1 for _, r in all_mut if "verify_full_tally" in r),
            "same_kill_decision": sum(1 for _, r in all_mut if r.get("verify_full_tally", {}).get("same_kill_decision")),
            "disagreements": [r["id"] for _, r in all_mut if "verify_full_tally" in r and not r["verify_full_tally"]["same_kill_decision"]]},
    }
    # per-function kill rates (headline ops)
    pfr = []
    for pf in per_function:
        if pf["func"] not in ctrl_ok:
            continue
        hs = [r for r in pf["mutants"] if r["op"] in HEAD_OPS and r["outcome"] != "compile_fail"]
        k = sum(1 for r in hs if r["outcome"] in ("killed_value", "killed_panic"))
        pfr.append({"func": pf["func"], "nargs": pf["nargs"], **rate(k, len(hs))})
        pf["headline_rate"] = rate(k, len(hs))
    rates = [p["rate"] for p in pfr if p["rate"] is not None]
    summary["per_function_distribution"] = {
        "functions_with_assessable_mutants": len(rates),
        "functions_with_no_assessable_mutant": [p["func"] for p in pfr if p["rate"] is None],
        "min": min(rates) if rates else None,
        "median": (sorted(rates)[len(rates) // 2] if rates else None),
        "mean_of_function_rates": (round(sum(rates) / len(rates), 4) if rates else None),
        "functions_at_100pct": sum(1 for x in rates if x == 1.0),
        "functions_below_50pct": [p["func"] for p in pfr if p["rate"] is not None and p["rate"] < 0.5],
        "lowest10": sorted(pfr, key=lambda p: (p["rate"] if p["rate"] is not None else 2, p["func"]))[:10],
    }
    if args.equiv:
        from collections import Counter
        ver = Counter()
        ver_op = {}
        for f, r in head:
            if "equiv" in r:
                v = r["equiv"]["verdict"]
                if v == "no_difference_found":
                    v += "|A_exhaustive" if r["equiv"].get("A_exhaustive") else "|A_partial"
                ver[(r["outcome"], v)] += 1
                ver_op.setdefault(r["op"], Counter())[v] += 1
        summary["survivor_equivalence_headline"] = {
            "by_outcome_and_verdict": {f"{a}|{b}": c for (a, b), c in sorted(ver.items())},
            "by_operator": {op: dict(c) for op, c in ver_op.items()},
            "budgets_seconds_ABC": args.budgets,
        }
        oe = {f: orig_ext[f] for f in ctrl_ok if f in orig_ext}
        summary["original_translation_extended_domain"] = {
            "rule": "the unmutated translation vs the C on phases A (in-domain, exhaustive for 1-2 args), B (negatives), C (large); first UB-clean divergence per phase",
            "functions": len(oe),
            "diverge_in_domain_A": sorted(f for f, r in oe.items() if r["A_in_domain"]["witness"]),
            "diverge_negative_B": sorted(f for f, r in oe.items() if r["B_negative"]["witness"]),
            "diverge_large_C": sorted(f for f, r in oe.items() if r["C_large"]["witness"]),
            "A_exhaustive_clean": sum(1 for r in oe.values() if r["A_in_domain"]["complete"] and not r["A_in_domain"]["witness"]),
        }
    out = {"provenance": prov, "rules": __doc__, "summary": summary,
           "per_function_rates": pfr, "per_function": per_function}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=1)
    print(json.dumps(summary, indent=1)[:6000])


def shard_select(funcs, spec, pins=()):
    """--shard I[,J...]/N: deterministic split of the functions.
    Without --pin: functions sorted by name; the k-th (0-based) goes to shard k % N.
    With --pin F,...: the pinned functions are shard 0 (and only they are); the others, sorted by
    name, go round-robin over shards 1..N-1 (k-th -> 1 + k % (N-1)); N must be >= 2.
    Returns the selected functions in the original (population) order."""
    idx, n = spec.split("/")
    n = int(n); want = {int(x) for x in idx.split(",")}
    if n < 1 or not want or not all(0 <= w < n for w in want) or (pins and n < 2):
        raise SystemExit(f"bad --shard {spec!r}")
    pins = set(pins)
    if pins:
        rest = sorted(f for f in funcs if f not in pins)
        shard = {f: 1 + k % (n - 1) for k, f in enumerate(rest)}
        shard.update({f: 0 for f in funcs if f in pins})
    else:
        shard = {f: k % n for k, f in enumerate(sorted(funcs))}
    return [f for f in funcs if shard[f] in want]


def merge_shards(args):
    """--merge: combine shard outputs (same grid, population, seed, cap, budgets) into one file whose
    summary is recomputed by finish(); each shard's provenance is kept under provenance.shards."""
    parts = [json.load(open(p)) for p in args.merge]
    grids = {pt["provenance"].get("grid", "v1") for pt in parts}
    if len(grids) != 1:
        raise SystemExit(f"shards disagree on the grid: {grids}")
    global GRID
    GRID = grids.pop()
    funcs = population()
    if args.funcs:
        want = set(args.funcs.split(","))
        funcs = [f for f in funcs if f in want]
    by_f = {}
    for p, pt in zip(args.merge, parts):
        for pf in pt["per_function"]:
            if pf["func"] in by_f:
                raise SystemExit(f"{pf['func']} is in two shards")
            by_f[pf["func"]] = pf
    missing = [f for f in funcs if f not in by_f]
    extra = sorted(set(by_f) - set(funcs))
    if missing or extra:
        raise SystemExit(f"shards do not cover the population: missing {missing}, extra {extra}")
    per_function = [by_f[f] for f in funcs]
    budgets = {json.dumps(pt["summary"].get("survivor_equivalence_headline", {}).get("budgets_seconds_ABC"))
               for pt in parts}
    if len(budgets) != 1:
        raise SystemExit(f"shards disagree on --budgets: {budgets}")
    args.budgets = json.loads(budgets.pop()) or args.budgets
    args.equiv = any(pf.get("original_extended_domain") for pf in per_function)
    secs = [pt["summary"]["seconds"] for pt in parts]
    shards = []
    for p, pt in zip(args.merge, parts):
        pv = dict(pt["provenance"])
        pv.pop("file_sha256", None)
        shards.append({"file": os.path.relpath(p, REPO) if os.path.isabs(p) else p,
                       "functions": len(pt["per_function"]),
                       "mutants": sum(len(pf["mutants"]) for pf in pt["per_function"]),
                       "seconds": pt["summary"]["seconds"], **pv})
    prov = provenance(sys.argv)
    prov["merged_from_shards"] = shards
    prov["seconds_note"] = ("summary.seconds = max over shards (they ran in parallel on different "
                            "hosts); per-shard seconds under provenance.merged_from_shards")
    finish(args, funcs, per_function, len(population()),
           max(s_["mutation"] for s_ in secs), max(s_["total"] for s_ in secs), prov)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--cap", type=int, default=0, help="max mutants per operator per function (0 = all sites)")
    ap.add_argument("--seed", type=int, default=20261113)
    ap.add_argument("--funcs", default="", help="comma-separated subset (debug)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--count-only", action="store_true")
    ap.add_argument("--equiv", action="store_true", help="run equivalence analysis of survivors")
    ap.add_argument("--budgets", default="40,15,15", help="seconds per survivor for phases A,B,C")
    ap.add_argument("--verify-every", type=int, default=40,
                    help="also run the FULL batch_diff.tally on every Nth mutant and compare the kill decision")
    ap.add_argument("--resume", action="store_true", help="reuse $MUTPRIM_WORK/checkpoint.json (mutation phase)")
    ap.add_argument("--arrays", action="store_true", help="measure the array/float harness (run_arrays.py) instead")
    ap.add_argument("--out", default=None)
    ap.add_argument("--shard", default="",
                    help="I[,J..]/N: run only the functions whose rank in name order is I (or J..) mod N "
                         "(default: all). Combine shard outputs with --merge")
    ap.add_argument("--pin", default="",
                    help="with --shard: comma-separated functions that form shard 0 (e.g. the ones whose verdicts "
                         "must come from one host); the rest go round-robin over shards 1..N-1")
    ap.add_argument("--merge", nargs="+", default=None, metavar="SHARD.json",
                    help="merge shard outputs into --out (summary recomputed); runs nothing")
    ap.add_argument("--grid", choices=("v1", "v2"), default=None,
                    help="scalar input grid (enumerate_inputs.py); v2 also takes the population from "
                         "results/vert_diff_grid2.json. Default: $MUTPRIM_GRID or v1 (every shipped result)")
    args = ap.parse_args()
    global GRID
    if args.grid:
        GRID = args.grid
        os.environ["MUTPRIM_GRID"] = GRID
    if args.arrays and GRID != "v1":
        ap.error("--grid applies to the scalar harness only")
    if args.merge:
        if not args.out:
            ap.error("--merge needs --out")
        return merge_shards(args)
    if args.arrays and args.shard:
        ap.error("--shard applies to the scalar harness only")
    if args.arrays:
        args.out = args.out or os.path.join(REPO, "results", "vert_arrays_mutation_primary.json")
        return main_arrays(args)
    args.out = args.out or os.path.join(REPO, "results", "vert_mutation_primary.json")

    pop_all = population()
    pop_n = len(pop_all)
    funcs = list(pop_all)
    if args.funcs:
        want = set(args.funcs.split(","))
        funcs = [f for f in funcs if f in want]
    if args.limit:
        funcs = funcs[:args.limit]

    plan = {}
    for f in pop_all:   # the whole population, so --verify-every picks the same mutants under any subset
        rust, argtypes, names, muts, sampled = mutants_for(f, args.cap, args.seed)
        plan[f] = dict(nargs=len(argtypes), reached=names, mutants=muts, sampling=sampled)
    if args.count_only:
        plan = {f: plan[f] for f in funcs}
        from collections import Counter
        c = Counter(m["op"] for p in plan.values() for m in p["mutants"])
        print(json.dumps({"functions": len(plan), "mutants": sum(c.values()), "by_op": c}, indent=1))
        for f, p in plan.items():
            print(f, p["reached"], {op: v["sites"] for op, v in p["sampling"].items()})
        return
    # --verify-every counts mutants in the global (whole-population) order, so a sharded or --funcs
    # run verifies exactly the mutants an unsharded run would.
    verify_set, gi = set(), 0
    for f in pop_all:
        for m in plan[f]["mutants"]:
            if args.verify_every and gi % args.verify_every == 0:
                verify_set.add((f, m["id"]))
            gi += 1
    if args.shard:
        pins = [x for x in args.pin.split(",") if x]
        unknown = sorted(set(pins) - set(pop_all))
        if unknown:
            raise SystemExit(f"--pin: not in the population: {unknown}")
        funcs = shard_select(funcs, args.shard, pins)
        print(f"  shard {args.shard}: {len(funcs)} functions, "
              f"{sum(len(plan[f]['mutants']) for f in funcs)} mutants", flush=True)

    # MUTPRIM_WORK (optional): a persistent work dir (binaries + checkpoint) so the equivalence
    # phase can be resumed with --resume without re-running the mutation phase.
    work = os.environ.get("MUTPRIM_WORK") or tempfile.mkdtemp(prefix="mutprim_")
    ckpt = os.path.join(work, "checkpoint.json")
    t_start = time.time()
    orcs, controls = {}, {}
    for f in funcs:
        d = os.path.join(work, "c", f); os.makedirs(d, exist_ok=True)
        orcs[f] = build_oracles(f, d)
    jobs = []
    for f in funcs:
        for m in plan[f]["mutants"]:
            jobs.append((f, m["id"], m["src"], orcs[f], work, (f, m["id"]) in verify_set))
    results = {}
    jl = os.path.join(work, "results.jsonl")   # every finished job, appended as it completes
    if os.path.exists(jl):
        for line in open(jl):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            results[(r["func"], r["id"])] = r
        jobs = [j for j in jobs if (j[0], j[1]) not in results]
        print(f"  resuming: {len(results)} finished jobs loaded from results.jsonl", flush=True)
    t_mut_prev = None
    if args.resume and os.path.exists(ckpt):
        ck = json.load(open(ckpt))
        if isinstance(ck, dict):
            t_mut_prev = ck.get("t_mut")
            ck = ck["results"]
        for r in ck:
            results[(r["func"], r["id"])] = r
        jobs = []
    with ProcessPoolExecutor(max_workers=args.jobs) as ex:
        # control: the unmutated translation through run.test_function itself (must agree)
        futs = [ex.submit(job_control, (f,)) for f in funcs if (f, "control") not in results]
        futs += [ex.submit(job_mutant, j) for j in jobs]
        done = 0
        with open(jl, "a") as jfh:
            for fu in as_completed(futs):
                r = fu.result()
                results[(r["func"], r["id"])] = r
                jfh.write(json.dumps(r) + "\n"); jfh.flush()
                done += 1
                if done % 100 == 0:
                    print(f"  {done}/{len(futs)} ({time.time() - t_start:.0f}s)", flush=True)
    t_mut = time.time() - t_start
    if t_mut_prev is not None:
        t_mut = t_mut_prev
    else:
        with open(ckpt, "w") as fh:
            json.dump({"t_mut": t_mut, "results": list(results.values())}, fh)
    for f in funcs:
        controls[f] = results[(f, "control")]

    equiv = {}
    if args.equiv:
        budgets = [float(x) for x in args.budgets.split(",")]
        ejobs = []
        for f in funcs:
            if controls[f]["outcome"] != "survived":
                continue
            for m in plan[f]["mutants"]:
                r = results[(f, m["id"])]
                if r["outcome"] in ("survived", "ub_only", "timeout_only", "rust_hang"):
                    ejobs.append((f, m["id"], r["bin"], orcs[f], budgets))
        print(f"  equivalence analysis: {len(ejobs)} not-killed mutants", flush=True)
        orig_ext = {}
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            futs = [ex.submit(job_equiv, j) for j in ejobs]
            ofuts = [ex.submit(job_orig_check, (f, orcs[f], budgets)) for f in funcs]
            for i, fu in enumerate(as_completed(futs)):
                r = fu.result()
                equiv[(r["func"], r["id"])] = r
                if (i + 1) % 50 == 0:
                    print(f"  equiv {i + 1}/{len(ejobs)} ({time.time() - t_start:.0f}s)", flush=True)
            for fu in ofuts:
                r = fu.result()
                orig_ext[r["func"]] = r
    t_all = time.time() - t_start

    # ---- aggregate ----
    HEAD_OPS = ("ror", "aor", "const", "f32")
    per_function, all_mut = [], []
    for f in funcs:
        ctrl = controls[f]
        rows = []
        for m in plan[f]["mutants"]:
            r = results[(f, m["id"])]
            row = {k: m[k] for k in ("id", "op", "sub", "fn", "line", "orig", "repl")}
            row.update({k: r.get(k) for k in ("outcome", "value", "rust_panic", "rust_hang", "ub",
                                              "timeout", "agree", "candidates", "early_exit", "value_samples",
                                              "crash_samples", "unrechecked_slow", "rechecked_slow",
                                              "per_input_fallback", "verify_full_tally", "secs") if k in r})
            if (f, m["id"]) in equiv:
                row["equiv"] = equiv[(f, m["id"])]
            rows.append(row)
            all_mut.append((f, row))
        per_function.append({"func": f, "nargs": plan[f]["nargs"], "reached_functions": plan[f]["reached"],
                             "sampling": plan[f]["sampling"], "control": ctrl,
                             "original_extended_domain": (orig_ext.get(f) if args.equiv else None),
                             "mutants": rows})

    finish(args, funcs, per_function, pop_n, t_mut, t_all, provenance(sys.argv))
    if not os.environ.get("MUTPRIM_WORK"):
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
