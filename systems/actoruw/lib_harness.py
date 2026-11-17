"""Function-grid harness for TRACTOR LIBRARY translations (ACTOR re-grade), run inside Docker.

Subset rule (stated before any library was run): a library translation is graded when EVERY
function its public header(s) (c_src/include/*.h) declares has only
  * scalar parameters: integer types (incl. char, bool, size_t, <stdint.h> types, typedefs of
    these, enums) or float/double, or `const char *` (a read-only NUL-terminated string), and
  * a scalar or void return.
These are the libraries whose C interface a generated caller can drive with the same per-argument
grids the method uses for functions (VERT); others (pointers to buffers/structs, struct returns)
are not graded and are listed with the reason.

Per library we generate ONE C caller, driver_main.c, that includes the header and, given
`<fn-index> <args...>` in argv, calls that function once and prints "#ret=<value>" (integers in
decimal, floats as %a). The SAME caller is linked
  * with the C library's own sources (CMake recipe)                       -> the oracle, and
  * against ACTOR's cdylib (cargo build --release --lib), -Wl,-rpath       -> the system under test,
so the Rust is exercised exactly as a drop-in C-ABI replacement, which is what TRACTOR requires.
One call per process (fresh static state, as one benchmark vector per run). Channels: stdout
(function output followed by the #ret line), exit code, stderr. Float returns are compared with a
relative tolerance of 1e-6 (both NaN = equal); everything else byte for byte.

Inputs per function (deterministic, no model): one argument -> its grid (<= 80 values); several
arguments -> 80 tuples spread over the product of the per-argument grids by the R_d
low-discrepancy sequence (systems/enumerate_inputs.py, the same code as VERT; per-argument grid
size round(80^(1/n))). Integer grid = grid_for over the type's range clamped to +-2^31 (small
values, boundaries, powers of two +-1, an even sweep); float grid and string corpus fixed below.

Usage: python3 -I lib_harness.py scan|build|test [--only battery/case]
"""
import argparse
import base64
import glob
import json
import math
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import difftest  # noqa: E402
import gen_inputs  # noqa: E402  (also puts systems/ on sys.path for enumerate_inputs)
from enumerate_inputs import grid_for, _spread  # noqa: E402

CAP = 80
MAX_ABS = 2**31
INT_KINDS = {
    # canonical C spelling -> (lo, hi, printf-cast)
    "i8": (-128, 127), "u8": (0, 255), "i16": (-32768, 32767), "u16": (0, 65535),
    "i32": (-2**31, 2**31 - 1), "u32": (0, 2**32 - 1), "i64": (-2**63, 2**63 - 1), "u64": (0, 2**64 - 1),
    "bool": (0, 1),
}
BASE = {
    "char": "i8", "signed char": "i8", "unsigned char": "u8", "short": "i16", "short int": "i16",
    "signed short": "i16", "unsigned short": "u16", "unsigned short int": "u16", "int": "i32",
    "signed": "i32", "signed int": "i32", "unsigned": "u32", "unsigned int": "u32", "long": "i64",
    "long int": "i64", "signed long": "i64", "unsigned long": "u64", "unsigned long int": "u64",
    "long long": "i64", "long long int": "i64", "unsigned long long": "u64",
    "unsigned long long int": "u64", "size_t": "u64", "ssize_t": "i64", "int8_t": "i8", "uint8_t": "u8",
    "int16_t": "i16", "uint16_t": "u16", "int32_t": "i32", "uint32_t": "u32", "int64_t": "i64",
    "uint64_t": "u64", "bool": "bool", "_Bool": "bool", "float": "f32", "double": "f64",
    "float_t": "f32", "double_t": "f64", "void": "void", "intptr_t": "i64", "uintptr_t": "u64",
    "wchar_t": "i32",
}
FLOATS = [0.0, -0.0, 1.0, -1.0, 0.5, 2.0, 3.0, -2.5, 10.0, 100.0, -100.0, 3.14159, 1e-7, 1e-30,
          1e10, -1e10, 65504.0, 1e38, -1e38, 3.4e38, 1e300, float("inf"), float("-inf"), float("nan"),
          1.401298464324817e-45, 255.0, 256.0, 0.1, 7.0, -7.0, 1000.0, 0.25]
STRS = ["", "a", "abc", "hello", "Hello, World!", "  spaced ", "12345", "-42", "abc123", "0",
        "héllo wörld", "a" * 300, "a,b;c", "\t\n", "ABCxyz", "!@#$%^&*()", "aaaa", "the quick brown fox"]


def strip_c(txt):
    txt = re.sub(r"/\*.*?\*/|//[^\n]*", "", txt, flags=re.S)
    txt = re.sub(r"\\\n", " ", txt)
    return re.sub(r"^\s*(#|%:).*$", "", txt, flags=re.M)


def norm_type(t):
    t = re.sub(r"\b(const|volatile|extern|static|inline|register|restrict|__restrict)\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s*\*\s*", "*", t)
    return t


def typedefs(txt):
    td = {}
    for m in re.finditer(r"typedef\s+enum\b[^;{]*\{[^}]*\}\s*(\w+)\s*;", txt, re.S):
        td[m.group(1)] = "i32"
    for m in re.finditer(r"\benum\s+(\w+)\s*\{", txt):
        td["enum " + m.group(1)] = "i32"
    for m in re.finditer(r"typedef\s+([\w\s]+?)\s+(\w+)\s*;", txt):
        base = norm_type(m.group(1))
        if base in BASE:
            td[m.group(2)] = BASE[base]
        elif base in td:
            td[m.group(2)] = td[base]
    return td


def kind_of(t, td, param=True):
    t = norm_type(t)
    if param and t in ("char*",) and True:
        return None
    if t in BASE:
        return BASE[t]
    if t in td:
        return td[t]
    return None


def parse_header(txt):
    """[(name, ret_kind, [(ptype_raw, kind)], raw)] for every prototype; kind None = ineligible."""
    s = strip_c(txt)
    td = typedefs(s)
    body = re.sub(r"\{[^{}]*\}", ";", s)          # drop struct/enum bodies so they don't look like protos
    body = re.sub(r"\{[^{}]*\}", ";", body)
    out = []
    for m in re.finditer(r"([A-Za-z_][\w\s\*]*?)\b([A-Za-z_]\w*)\s*\(([^;{}()]*)\)\s*;", body):
        ret, name, args = m.group(1), m.group(2), m.group(3)
        if re.search(r"\btypedef\b", ret) or name in ("if", "while", "for", "return", "sizeof"):
            continue
        rk = kind_of(ret, td, param=False)
        params = []
        a = args.strip()
        if a and a != "void":
            for p in a.split(","):
                p = p.strip()
                if p == "...":
                    params.append((p, None))
                    continue
                raw = norm_type(re.sub(r"\[\s*\w*\s*\]", "*", p))
                if re.match(r"^(const\s+)?char\s*\*\s*\w*$", re.sub(r"\s+", " ", p.strip())) and "const" in p:
                    params.append((p, "str"))
                    continue
                mm = re.match(r"^(.*?)(\w+)$", raw)
                ty = raw if (not mm or mm.group(1).strip() == "") else mm.group(1).strip()
                if mm and mm.group(1).strip() == "":
                    ty = raw          # unnamed parameter
                k = kind_of(ty, td)
                if k in (None, "void"):
                    k = None
                params.append((p, k))
        out.append({"name": name, "ret_raw": ret.strip(), "ret": rk, "params": params,
                    "eligible": rk is not None and all(k is not None for _, k in params)})
    return out


def lib_cases():
    return [(b, c) for (b, c) in common.all_cases() if common.is_lib(c) and b != "P01_sphincs_plus"]


def scan_case(b, c):
    ph, src = common.phase_dir(b, c)
    rec = {"battery": b, "case": c, "phase": ph}
    if not src:
        rec.update(eligible=False, reason="no crate")
        return rec
    hdrs = sorted(glob.glob(os.path.join(src, "c_src", "include", "*.h")))
    fns = []
    for h in hdrs:
        for f in parse_header(open(h, errors="replace").read()):
            f["header"] = os.path.basename(h)
            fns.append(f)
    rec["headers"] = [os.path.basename(h) for h in hdrs]
    rec["functions"] = fns
    if not hdrs:
        rec.update(eligible=False, reason="no public header in c_src/include")
    elif not fns:
        rec.update(eligible=False, reason="no function prototype found in the public header")
    elif not all(f["eligible"] for f in fns):
        bad = [f["name"] for f in fns if not f["eligible"]]
        rec.update(eligible=False, reason="non-scalar parameter or return in: " + ", ".join(bad[:6]))
    else:
        rec.update(eligible=True, reason="")
    return rec


# ------------------------------------------------------------------------------ inputs

def fmt_val(k, v):
    if k in ("f32", "f64"):
        return repr(float(v)) if not (isinstance(v, float) and (math.isnan(v) or math.isinf(v))) else \
            ("nan" if math.isnan(v) else ("inf" if v > 0 else "-inf"))
    if k == "str":
        return v
    return str(int(v))


def per_arg_values(k, size):
    if k in INT_KINDS:
        lo, hi = INT_KINDS[k]
        lo, hi = max(lo, -MAX_ABS), min(hi, MAX_ABS)
        if hi - lo + 1 <= size:
            return list(range(lo, hi + 1))
        return grid_for(lo, hi, size)
    pool = FLOATS if k in ("f32", "f64") else STRS
    if len(pool) <= size:
        return list(pool)
    step = len(pool) / size
    return [pool[int(i * step)] for i in range(size)]


def inputs_for(fn):
    ks = [k for _, k in fn["params"]]
    if not ks:
        return [[]]
    if len(ks) == 1:
        vals = per_arg_values(ks[0], CAP)
        return [[fmt_val(ks[0], v)] for v in vals[:CAP]]
    per = max(2, int(round(CAP ** (1.0 / len(ks)))))
    grids = [per_arg_values(k, per) for k in ks]
    return [[fmt_val(k, v) for k, v in zip(ks, t)] for t in _spread(grids, CAP)]


# ------------------------------------------------------------------------------ caller

CT = {"i8": "signed char", "u8": "unsigned char", "i16": "short", "u16": "unsigned short", "i32": "int",
      "u32": "unsigned int", "i64": "long long", "u64": "unsigned long long", "bool": "_Bool",
      "f32": "float", "f64": "double", "str": "const char *"}


def caller_source(rec):
    lines = ["#include <stdio.h>", "#include <stdlib.h>", "#include <string.h>", "#include <stdint.h>",
             "#include <stdbool.h>"]
    for h in rec["headers"]:
        lines.append(f'#include "{h}"')
    lines += ["static long long I(const char *s){return strtoll(s,0,10);}",
              "static unsigned long long U(const char *s){return strtoull(s,0,10);}",
              "static double D(const char *s){return strtod(s,0);}",
              "int main(int argc, char **argv){", "  if (argc < 2) return 99;", "  int f = atoi(argv[1]);",
              "  switch (f) {"]
    for i, fn in enumerate(rec["functions"]):
        args = []
        for j, (_, k) in enumerate(fn["params"]):
            a = f"argv[{j + 2}]"
            if k == "str":
                args.append(a)
            elif k in ("f32", "f64"):
                args.append(f"({CT[k]})D({a})")
            elif k.startswith("u") or k == "bool":
                args.append(f"({CT[k]})U({a})")
            else:
                args.append(f"({CT[k]})I({a})")
        call = f"{fn['name']}({', '.join(args)})"
        n = len(fn["params"])
        lines.append(f"  case {i}: {{ if (argc != {n + 2}) return 98;")
        r = fn["ret"]
        if r == "void":
            lines.append(f"    {call}; fflush(stdout); printf(\"\\n#ret=void\\n\"); break; }}")
        elif r in ("f32", "f64"):
            lines.append(f"    double r = (double){call}; fflush(stdout); printf(\"\\n#ret=%a\\n\", r); break; }}")
        elif r.startswith("u") or r == "bool":
            lines.append(f"    unsigned long long r = (unsigned long long){call}; fflush(stdout); "
                         f"printf(\"\\n#ret=%llu\\n\", r); break; }}")
        else:
            lines.append(f"    long long r = (long long){call}; fflush(stdout); printf(\"\\n#ret=%lld\\n\", r); break; }}")
    lines += ["  default: return 97;", "  }", "  fflush(stdout);", "  return 0;", "}", ""]
    return "\n".join(lines)


def sh(argv, cwd=None, env=None, timeout=900):
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as e:
        return -1, "TIMEOUT"


def lib_recipe(src):
    csrc = os.path.join(src, "c_src")
    r = common.parse_cmake(os.path.join(csrc, "CMakeLists.txt"))
    if not r["sources"]:
        r["sources"] = sorted(os.path.relpath(p, csrc) for p in glob.glob(os.path.join(csrc, "src", "*.c")))
        r["sources_note"] = "no add_library sources parsed; used src/*.c"
    if "include" not in r["includes"] and os.path.isdir(os.path.join(csrc, "include")):
        r["includes"].append("include")
    r["libs"] = [l for l in r["libs"] if l != "-l"]
    return csrc, r


def c_oracle_cmd(cc, rec, src, out, extra=()):
    csrc, r = lib_recipe(src)
    caller = os.path.join(common.W, "build", rec["battery"], rec["case"], "driver_main.c")
    inc = ["-I" + os.path.join(csrc, i) for i in r["includes"]]
    return [cc] + list(extra) + r["cflags"] + inc + [caller] + [os.path.join(csrc, s) for s in r["sources"]] + \
        ["-o", out] + r["libs"] + ["-lm"]


def build_case(rec):
    b, c = rec["battery"], rec["case"]
    ph, src = common.phase_dir(b, c)
    d = os.path.join(common.W, "build", b, c)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "driver_main.c"), "w") as f:
        f.write(caller_source(rec))
    out = {"battery": b, "case": c, "phase": ph}
    cbin = os.path.join(d, "c_driver")
    cmd = c_oracle_cmd("gcc", rec, src, cbin)
    rc, log = sh(cmd)
    out["c_build"] = {"cmd": " ".join(cmd), "rc": rc, "log_tail": log[-1500:]}
    out["c_ok"] = rc == 0
    rdir = os.path.join(d, "rust")
    if os.path.isdir(rdir):
        shutil.rmtree(rdir)
    shutil.copytree(src, rdir, ignore=shutil.ignore_patterns("target", "c_src", "c_libs", "logs", "build*", "*.o", "*.so"))
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = os.path.join(d, "target")
    cmd = ["cargo", "build", "--release", "--offline", "--lib"]
    rc, log = sh(cmd, cwd=rdir, env=env)
    out["rust_build"] = {"cmd": " ".join(cmd), "rc": rc, "log_tail": log[-3000:]}
    sos = sorted(glob.glob(os.path.join(d, "target", "release", "lib*.so")))
    so = sos[0] if len(sos) == 1 else os.path.join(d, "target", "release", "NO_UNIQUE_CDYLIB.so")
    out["cdylibs_found"] = [os.path.basename(x) for x in sos]
    out["rust_so"] = so
    out["rust_lib_ok"] = rc == 0 and os.path.isfile(so)
    if out["rust_lib_ok"]:
        csrc, r = lib_recipe(src)
        rbin = os.path.join(d, "rust_driver")
        cmd = ["gcc", os.path.join(d, "driver_main.c")] + ["-I" + os.path.join(csrc, i) for i in r["includes"]] + \
            ["-o", rbin, so, "-Wl,-rpath," + os.path.dirname(so), "-lm"]
        rc2, log2 = sh(cmd)
        out["rust_link"] = {"cmd": " ".join(cmd), "rc": rc2, "log_tail": log2[-1500:]}
        out["rust_ok"] = rc2 == 0
    else:
        out["rust_ok"] = False
    out["status"] = "ok" if out["c_ok"] and out["rust_ok"] else (
        "c_build_fail" if not out["c_ok"] else ("rust_build_fail" if not out["rust_lib_ok"] else "rust_link_fail"))
    return out


def same_lib(a, b):
    if a["rc"] != b["rc"] or a["stderr"] != b["stderr"] or a["timeout"] != b["timeout"]:
        return False
    if a["stdout"] == b["stdout"]:
        return True
    # float return: relative tolerance on the #ret line only
    ra, rb = a["stdout"].rsplit(b"\n#ret=", 1), b["stdout"].rsplit(b"\n#ret=", 1)
    if len(ra) != 2 or len(rb) != 2 or ra[0] != rb[0]:
        return False
    try:
        x, y = float.fromhex(ra[1].strip().decode()), float.fromhex(rb[1].strip().decode())
    except ValueError:
        return False
    if math.isnan(x) and math.isnan(y):
        return True
    return x == y or abs(x - y) <= 1e-6 * max(abs(x), abs(y))


def test_case(args):
    rec, brec = args
    d = os.path.join(common.W, "build", rec["battery"], rec["case"])
    cbin, rbin = os.path.join(d, "c_driver"), os.path.join(d, "rust_driver")
    res = {"battery": rec["battery"], "case": rec["case"], "functions": [], "divergent": [], "counts": {},
           "input_ids": []}
    for i, fn in enumerate(rec["functions"]):
        ins = inputs_for(fn)
        res["functions"].append({"name": fn["name"], "n_inputs": len(ins)})
        for a in ins:
            x = gen_inputs.case([str(i)] + a, b"", note=f"fn:{fn['name']}")
            res["input_ids"].append(x["id"])
            cr = difftest.run_one(cbin, x)
            rr = difftest.run_one(rbin, x)
            if same_lib(cr, rr):
                res["counts"]["agree"] = res["counts"].get("agree", 0) + 1
                continue
            k = difftest.classify(cr, rr)
            res["counts"][k] = res["counts"].get(k, 0) + 1
            res["divergent"].append({"input": x, "function": fn["name"], "class": k, "c": difftest.enc(cr),
                                     "rust": difftest.enc(rr),
                                     "c_reproducible": difftest.same(cr, difftest.run_one(cbin, x)),
                                     "rust_reproducible": difftest.same(rr, difftest.run_one(rbin, x)),
                                     "channels": [ch for ch in ("stdout", "rc", "stderr", "timeout") if cr[ch] != rr[ch]]})
    res["n_inputs"] = len(res["input_ids"])
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["scan", "build", "test"])
    ap.add_argument("--only", default=None)
    ap.add_argument("-j", type=int, default=3)
    a = ap.parse_args()
    outd = os.path.join(common.W, "out")
    if a.step == "scan":
        recs = [scan_case(b, c) for b, c in lib_cases()]
        common.dump(os.path.join(outd, "lib_scan.json"), {"rule": __doc__.split("\n\n")[1], "cases": recs})
        el = [r for r in recs if r["eligible"]]
        print(f"libraries {len(recs)}; eligible {len(el)}")
        for r in recs:
            print(f"  {r['battery']}/{r['case']}: {'ELIGIBLE' if r['eligible'] else 'skip: ' + r['reason']}")
        return
    scan = json.load(open(os.path.join(outd, "lib_scan.json")))["cases"]
    recs = [r for r in scan if r["eligible"] and (not a.only or f"{r['battery']}/{r['case']}" == a.only)]
    if a.step == "build":
        with ThreadPoolExecutor(a.j) as ex:
            res = list(ex.map(build_case, recs))
        for r in res:
            print(f"{r['battery']}/{r['case']}: {r['status']}", flush=True)
        vers = {t[0]: sh(t)[1].strip().splitlines()[0] for t in (["gcc", "--version"], ["rustc", "--version"], ["cargo", "--version"])}
        common.dump(os.path.join(outd, "lib_build.json"), {"toolchains": vers, "cases": res})
        return
    bj = {(r["battery"], r["case"]): r for r in json.load(open(os.path.join(outd, "lib_build.json")))["cases"]}
    todo = [(r, bj[(r["battery"], r["case"])]) for r in recs if bj.get((r["battery"], r["case"]), {}).get("status") == "ok"]
    with ThreadPoolExecutor(a.j) as ex:
        res = list(ex.map(test_case, todo))
    for r in res:
        print(f"{r['battery']}/{r['case']}: n={r['n_inputs']} {r['counts']}", flush=True)
    common.dump(os.path.join(outd, "lib_difftest.json"), {"programs": res, "tolerance": "rel 1e-6 on float #ret only"})


if __name__ == "__main__":
    main()
