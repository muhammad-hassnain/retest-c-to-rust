#!/usr/bin/env python3
"""Held-out generated inputs for the SACTOR CRUST-Bench re-grade: per-function argument grids for
every exported library function whose parameters are all scalars or C strings (method.tex,
"Inputs": per-argument grids -- small values, type boundaries, powers of two +-1, an even sweep --
at most 80 inputs per function, multi-argument inputs spread over the product with a Halton
low-discrepancy sequence). Functions taking structs or other pointers are NOT exercised here (their
only inputs are the sample's own tests).

Run after crust/run.py (reuses its C builds and Rust crates), inside the container:
    python3 systems/sactor/crust/grid.py --out /work/results/sactor_crust_grid.json

Per sample with a building unidiomatic combined.rs:
  1. prototypes: clang -Xclang -ast-dump=json on lib.c (the single TU SACTOR translated); keep
     non-static functions with a body that lib.a exports (nm) and whose types are all supported;
  2. a generated C driver (same source for every build) calls fn #F on input #I and prints the
     return value ('%lld', '%llu', '%.17g', or the returned string) -- one process per function
     over all its inputs, inputs separated by fflush'ed lines;
  3. linked against the C lib.a of every oracle-UB variant (crust/run.py's ccwrap builds) and
     against SACTOR's Rust cdylib (primary nightly, debug = SACTOR's own build; plus release and
     the second nightly);
  4. a function whose Rust output differs from the C is re-run one input per process on every C
     variant, and an input counts only if the C passes every UB check on it;
  5. held-out: the sample's own test objects are linked against the C with -Wl,--wrap for the
     same functions; the argument tuples they pass are logged; we report how many of our
     inputs, and how many exposing inputs, are among them.
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import sactor_common as sc  # noqa: E402
import run as crun  # noqa: E402

ou = sc.ou
MAXIN = 80

INT_T = {  # canonical C type -> (printf cast, printf fmt, grid kind, bits, signed)
    "int": ("long long", "%lld", "int", 32, True), "signed int": ("long long", "%lld", "int", 32, True),
    "unsigned int": ("unsigned long long", "%llu", "int", 32, False),
    "short": ("long long", "%lld", "int", 16, True), "unsigned short": ("unsigned long long", "%llu", "int", 16, False),
    "long": ("long long", "%lld", "int", 64, True), "unsigned long": ("unsigned long long", "%llu", "int", 64, False),
    "long long": ("long long", "%lld", "int", 64, True), "unsigned long long": ("unsigned long long", "%llu", "int", 64, False),
    "char": ("long long", "%lld", "char", 8, True), "signed char": ("long long", "%lld", "char", 8, True),
    "unsigned char": ("unsigned long long", "%llu", "char", 8, False),
    "_Bool": ("long long", "%lld", "bool", 1, False),
    "double": ("double", "%.17g", "float", 64, True), "float": ("double", "%.9g", "float", 32, True),
}
STR_T = {"const char *", "char *"}
STRINGS = ["", "a", "hello", "Hello, World!", "123", "-42", "0", "  padded  ", "a,b,c", "%s%d",
           "tab\there", "line\nbreak", "\xc3\xa9t\xc3\xa9", "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
           "3.14159", "true", "x", "the quick brown fox jumps over the lazy dog", "\x01\x7f"]


def halton(i, base):
    f, r = 1.0, 0.0
    while i > 0:
        f /= base
        r += f * (i % base)
        i //= base
    return r


def int_grid(bits, signed):
    g = [0, 1, 3, 7, 10, 2, 4, 5, 8, 15, 16, 17, 31, 32, 33, 63, 64, 65, 100, 127, 128, 255, 256, 511, 512]
    hi = (1 << (bits - 1)) - 1 if signed else (1 << bits) - 1
    g += [hi, hi - 1, (hi + 1) // 2, 1000, 4096, 65535, 65536, 1 << 31, (1 << 31) - 1, (1 << 32) - 1]
    if signed:
        lo = -(1 << (bits - 1))
        g += [-1, -2, -3, -7, -8, -16, -100, -128, -129, -256, -1000, lo, lo + 1, -(1 << 31)]
    g += [i * 37 for i in range(1, 30)]
    lo = -(1 << (bits - 1)) if signed else 0
    out = []
    for x in g:
        if lo <= x <= hi and x not in out:
            out.append(x)
    return out


def grid_for(ctype):
    if ctype in STR_T:
        return [("s", i) for i in range(len(STRINGS))]
    cast, fmt, kind, bits, signed = INT_T[ctype]
    if kind == "bool":
        return [("i", 0), ("i", 1)]
    if kind == "char":
        g = [0, 97, 65, 48, 32, 10, 122, 90, 57, 127, 1, 126, 33, 64] + ([-1, -128, -127] if signed else [128, 255, 200])
        return [("i", x) for x in g]
    if kind == "float":
        g = [0.0, 1.0, -1.0, 0.5, 2.0, 3.0, -0.5, 10.0, 100.0, 1e-9, 1e9, -1e9, 3.141592653589793, 1e300, -1e300,
             1e-300, 0.1, 7.25, 1234.5678, -0.0, 0.333, 2.5, 1e15]
        if bits == 32:
            g = [x for x in g if abs(x) < 1e38 and (x == 0 or abs(x) > 1e-38)]   # double->float must stay in range
        return [("f", x) for x in g]
    return [("i", x) for x in int_grid(bits, signed)]


def grid_for_cls(c):
    if c[0] == "enum":
        vals = sorted(set(c[2]))
        g = vals + [max(vals) + 1, min(vals) - 1 if min(vals) > 0 else max(vals) + 2, 100]
        out = []
        for x in g:
            if ("e", x) not in out:
                out.append(("e", x))
        return out
    return grid_for(c[1])


def inputs_for(params, pcls=None):
    grids = [grid_for_cls(c) for c in pcls] if pcls else [grid_for(p) for p in params]
    if not grids:
        return [[]]
    if len(grids) == 1:
        return [[v] for v in grids[0][:MAXIN]]
    primes = [2, 3, 5, 7, 11, 13, 17, 19]
    total = 1
    for g in grids:
        total *= len(g)
    out = []
    seen = set()
    i = 0
    while len(out) < min(MAXIN, total) and i < 5000:
        tup = tuple(g[int(halton(i, primes[k % len(primes)]) * len(g)) % len(g)] for k, g in enumerate(grids))
        i += 1
        if tup in seen:
            continue
        seen.add(tup)
        out.append(list(tup))
    return out


TYPEDEFS = {"uint8_t": "unsigned char", "int8_t": "signed char", "uint16_t": "unsigned short", "int16_t": "short",
            "uint32_t": "unsigned int", "int32_t": "int", "uint64_t": "unsigned long", "int64_t": "long",
            "size_t": "unsigned long", "ssize_t": "long", "bool": "_Bool", "uintptr_t": "unsigned long",
            "intptr_t": "long", "uint_fast32_t": "unsigned long", "uint_least32_t": "unsigned int",
            "unsigned": "unsigned int", "signed": "int", "long int": "long", "unsigned long int": "unsigned long",
            "long long int": "long long", "unsigned long long int": "unsigned long long", "short int": "short",
            "unsigned short int": "unsigned short", "const unsigned char *": "const unsigned char *"}


def canon(qt):
    """Canonical C spelling of a scalar type (x86-64 Linux glibc typedefs resolved)."""
    t = re.sub(r"\s+", " ", qt.replace("__attribute__((__const__))", "")).strip()
    t = t.replace("const char*", "const char *").replace("char*", "char *")
    if t.startswith("const ") and t[6:] in INT_T:   # by-value const scalar
        t = t[6:]
    if t.startswith("const ") and TYPEDEFS.get(t[6:]) in INT_T:
        t = TYPEDEFS[t[6:]]
    return TYPEDEFS.get(t, t)


def c_str(s):
    """C string literal for s (bytes outside printable ASCII as octal escapes)."""
    out = []
    for b in s.encode("utf-8"):
        c = chr(b)
        if c in '"\\':
            out.append("\\" + c)
        elif 32 <= b < 127:
            out.append(c)
        else:
            out.append("\\%03o" % b)
    return '"' + "".join(out) + '"'


def _walk(n):
    yield n
    for c in n.get("inner", []) or []:
        yield from _walk(c)


def type_env(ast):
    """Typedef / enum / struct declarations of the TU, for enum and struct-by-value support."""
    env = {"typedef": {}, "by_id": {}, "enum_by_name": {}, "rec_by_name": {}}
    for n in ast.get("inner", []):
        k = n.get("kind")
        if k in ("TypedefDecl", "EnumDecl", "RecordDecl"):
            env["by_id"][n["id"]] = n
        if k == "TypedefDecl":
            env["typedef"][n["name"]] = n
            env["by_id"][n["id"]] = n
        elif k == "EnumDecl" and n.get("name"):
            env["enum_by_name"][n["name"]] = n
        elif k == "RecordDecl" and n.get("name") and n.get("completeDefinition"):
            env["rec_by_name"][f"{n.get('tagUsed', 'struct')} {n['name']}"] = n
    return env


def enum_values(en):
    vals, nxt = [], 0
    for c in en.get("inner", []) or []:
        if c.get("kind") != "EnumConstantDecl":
            continue
        v = None
        for x in _walk(c):
            if x is not c and "value" in x:
                try:
                    v = int(x["value"])
                except (TypeError, ValueError):
                    v = None
                break
        if v is None:
            v = nxt
        vals.append(v)
        nxt = v + 1
    return vals


def classify(spelled, desugared, env, depth=0):
    """('scalar', canon) | ('enum', spelled, values) | ('struct', spelled, [(field, cls)]) | None"""
    c = canon(desugared or spelled)
    if c in INT_T or c in STR_T:
        return ("scalar", c)
    c2 = canon(spelled)
    if c2 in INT_T or c2 in STR_T:
        return ("scalar", c2)
    if env is None or depth > 3:
        return None
    decl = None
    td = env["typedef"].get(spelled)
    if td is not None:
        d2 = td["type"].get("desugaredQualType", td["type"].get("qualType"))
        if canon(d2) in INT_T:
            return ("scalar", canon(d2))
        for x in _walk(td):
            if isinstance(x.get("decl"), dict) and x["decl"].get("kind") in ("EnumDecl", "RecordDecl"):
                decl = env["by_id"].get(x["decl"]["id"])
                if decl is None:   # anonymous decl nested in the typedef
                    for y in _walk(td):
                        if y.get("id") == x["decl"]["id"]:
                            decl = y
                break
            if x.get("kind") in ("EnumDecl", "RecordDecl"):
                decl = x
                break
    elif spelled.startswith("enum "):
        decl = env["enum_by_name"].get(spelled[5:])
    elif spelled.startswith("struct "):
        decl = env["rec_by_name"].get(spelled)
    if decl is None:
        return None
    if decl.get("kind") == "EnumDecl":
        v = enum_values(decl)
        return ("enum", spelled, v) if v else None
    if decl.get("kind") == "RecordDecl" and decl.get("tagUsed") == "struct":
        fields = []
        for f in decl.get("inner", []) or []:
            if f.get("kind") != "FieldDecl":
                continue
            if not f.get("name"):
                return None
            t = f["type"]
            fc = classify(t["qualType"], t.get("desugaredQualType"), env, depth + 1)
            if fc is None or fc[0] == "struct" or (fc[0] == "scalar" and fc[1] in STR_T):
                return None
            fields.append((f["name"], fc))
        return ("struct", spelled, fields) if fields else None
    return None


def prototypes(lib_c, workdir):
    p = subprocess.run(["clang", "-Xclang", "-ast-dump=json", "-fsyntax-only", "-w", lib_c], cwd=workdir,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0 and not p.stdout:
        return None, p.stderr.decode()[-1000:]
    ast = json.loads(p.stdout)
    env = type_env(ast)
    fns = []
    for n in ast.get("inner", []):
        if n.get("kind") != "FunctionDecl" or n.get("storageClass") == "static":
            continue
        if not any(c.get("kind") == "CompoundStmt" for c in n.get("inner", [])):
            continue
        loc = n.get("loc", {})
        if "includedFrom" in loc or (loc.get("file") and not loc["file"].endswith("lib.c")):
            continue
        params = [c for c in n.get("inner", []) if c.get("kind") == "ParmVarDecl"]
        ptypes = [canon(c["type"].get("desugaredQualType", c["type"]["qualType"])) for c in params]
        ft = n["type"].get("desugaredQualType", n["type"]["qualType"])
        ret = canon(ft.split("(")[0])
        spelled_ret = n["type"]["qualType"].split("(")[0].strip()
        pcls = [classify(c["type"]["qualType"], c["type"].get("desugaredQualType"), env) for c in params]
        rcls = None if spelled_ret == "void" else classify(spelled_ret, None, env)
        fns.append({"name": n["name"], "ret": ret, "params": ptypes,
                    "spelled_params": [c["type"]["qualType"] for c in params], "spelled_ret": spelled_ret,
                    "pcls": pcls, "rcls": rcls,
                    "variadic": bool(n.get("variadic")) or "..." in ft})
    return fns, None


def supported(f, typed=False):
    """Scalar mode: scalar/string params, scalar/string return. Typed mode (the sample's header
    section is included in the driver): params may also be enums, and the return may be an enum
    or a struct whose fields are all scalars or enums (printed field by field)."""
    if f["variadic"] or f["name"] == "main":
        return False
    if not typed:
        if not all(p in INT_T or p in STR_T for p in f["params"]):
            return False
        if f["ret"] == "void":
            return False   # nothing observable from scalar arguments alone
        return f["ret"] in INT_T or f["ret"] in STR_T
    if any(c is None or c[0] == "struct" for c in f["pcls"]):
        return False
    return f["rcls"] is not None


def c_lit(v, ctype):
    kind, x = v
    if kind == "e":
        return f"({ctype})({x})"
    if kind == "s":
        return "S[%d]" % x
    if kind == "f":
        return repr(float(x)) if x == x else "0.0"
    if ctype in STR_T:
        return "0"
    if x < 0:
        return f"({ctype})(-{-x}LL)" if x != -(1 << 63) else f"({ctype})(-9223372036854775807LL-1)"
    return f"({ctype}){x}ULL"


def ret_printer(f, ii, call, typed):
    if typed:
        rc = f["rcls"]
        if rc[0] == "scalar" and rc[1] in STR_T:
            return f'{{ const char *r = {call}; printf("{ii} %s\\n", r ? r : "(NULL)"); }}'
        if rc[0] == "scalar":
            cast, fmt = INT_T[rc[1]][0], INT_T[rc[1]][1]
            return f'printf("{ii} {fmt}\\n", ({cast}){call});'
        if rc[0] == "enum":
            return f'printf("{ii} %lld\\n", (long long){call});'
        parts, vals = [], []
        for fname, fc in rc[2]:
            if fc[0] == "enum":
                parts.append(f"{fname}=%lld")
                vals.append(f"(long long)r.{fname}")
            else:
                cast, fmt = INT_T[fc[1]][0], INT_T[fc[1]][1]
                parts.append(f"{fname}={fmt}")
                vals.append(f"({cast})r.{fname}")
        return f'{{ {f["spelled_ret"]} r = {call}; printf("{ii} {" ".join(parts)}\\n", {", ".join(vals)}); }}'
    if f["ret"] in STR_T:
        return f'{{ const char *r = {call}; printf("{ii} %s\\n", r ? r : "(NULL)"); }}'
    cast, fmt = INT_T[f["ret"]][0], INT_T[f["ret"]][1]
    return f'printf("{ii} {fmt}\\n", ({cast}){call});'


def gen_driver(fns, path, header=None):
    """header: path of the sample's header section (typed mode) or None (scalar mode)."""
    typed = header is not None
    L = ["#include <stdio.h>", "#include <stdlib.h>", "#include <string.h>"]
    if typed:
        L.append(f'#include "{header}"')
    L.append("static const char *SACTOR_GRID_S[] = {" + ", ".join(c_str(x) for x in STRINGS) + "};")
    for f in fns:
        if typed:
            L.append(f"{f['spelled_ret']} {f['name']}({', '.join(f['spelled_params']) or 'void'});")
        else:
            L.append(f"{f['ret']} {f['name']}({', '.join(f['params']) or 'void'});")
    L.append("int main(int sactor_argc, char **sactor_argv) {")
    L.append("  int sactor_F = atoi(sactor_argv[1]); int sactor_I = sactor_argc > 2 ? atoi(sactor_argv[2]) : -1;")
    L.append("  setvbuf(stdout, NULL, _IOLBF, 0);")
    for fi, f in enumerate(fns):
        L.append(f"  if (sactor_F == {fi}) {{")
        ptypes = f["spelled_params"] if typed else f["params"]
        for ii, inp in enumerate(f["inputs"]):
            args = ", ".join(c_lit(v, t).replace("S[", "SACTOR_GRID_S[") for v, t in zip(inp, ptypes))
            L.append(f"    if (sactor_I < 0 || sactor_I == {ii}) {ret_printer(f, ii, f['name'] + '(' + args + ')', typed)}")
        L.append("  }")
    L.append('  printf("end\\n");\n  return 0;\n}')
    with open(path, "w") as fh:
        fh.write("\n".join(L) + "\n")


def header_section(lib_c, out):
    """The header part of SACTOR's single-TU lib.c (everything before '---- Sources ----')."""
    t = open(lib_c, errors="replace").read()
    k = t.find("/* ---- Sources ---- */")
    if k < 0:
        return None
    with open(out, "w") as fh:
        fh.write(t[:k])
    return out


def exported(lib_a):
    p = subprocess.run(["nm", "-g", "--defined-only", lib_a], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return {ln.split()[-1] for ln in p.stdout.splitlines() if len(ln.split()) == 3 and ln.split()[1] in "TtWw"}


def run1(exe, argv, san=None, nz=False, timeout=20):
    with __import__("tempfile").TemporaryDirectory(dir="/build/tmp") as td:
        if san:
            r = ou.run_san([exe] + argv, b"", cwd=td, timeout=timeout * 3, halt=not nz)
            return r["stdout"], r["stderr"], r["rc"], r["timed_out"], r["report"], r["reports"], r["infra"]
        o, e, rc, to = ou.run_proc([exe] + argv, b"", cwd=td, timeout=timeout)
        return o, e, rc, to, None, [], False


def lines_by_input(out):
    d = {}
    for ln in out.split(b"\n"):
        m = re.match(rb"^(\d+) (.*)$", ln)
        if m:
            d[int(m.group(1))] = m.group(2)
    return d


def do_sample(s, args):
    bdir = os.path.join(args.build, s)
    res = {"sample": s}
    cref = os.path.join(bdir, "c_ref")
    if not os.path.isfile(os.path.join(cref, "lib.a")):
        return {**res, "skipped": "no C lib.a"}
    crates = {}
    for tc in sc.TOOLCHAINS:
        for prof in ("debug", "release"):
            so = os.path.join(bdir, "crates", f"unidiomatic_{tc}_{prof}", "target", prof, "libbuild_attempt.so")
            if os.path.isfile(so):
                crates[f"rust:{tc}:{prof}"] = so
    if not crates:
        return {**res, "skipped": "no building unidiomatic Rust"}
    fns, err = prototypes(os.path.join(cref, "lib.c"), cref)
    if fns is None:
        return {**res, "skipped": "clang AST failed: " + err}
    exp = exported(os.path.join(cref, "lib.a"))
    allf = [f for f in fns if f["name"] in exp]
    gdir = os.path.join(bdir, "grid")
    os.makedirs(gdir, exist_ok=True)
    drv = os.path.join(gdir, "grid_driver.c")
    libs = crun.link_libs(s) + ["-lm"]
    env = dict(os.environ)
    env["PATH"] = os.path.join(args.build, "wrap") + ":" + env["PATH"]
    hdr = header_section(os.path.join(cref, "lib.c"), os.path.join(gdir, "lib_headers.h"))
    res["functions_exported"] = len(allf)
    sel, bins, typed = [], {}, False
    # typed mode first (header section included: enums, struct returns), scalar mode as fallback
    for mode in (["typed"] if hdr else []) + ["scalar"]:
        typed = mode == "typed"
        sel = [f for f in allf if supported(f, typed)]
        if not sel:
            continue
        for f in sel:
            f["inputs"] = inputs_for(f["params"], f["pcls"] if typed else None)
        gen_driver(sel, drv, header=os.path.basename(hdr) if typed else None)
        bins = {}
        fail = None
        for v in crun.C_VARIANTS:
            lib_a = os.path.join(bdir, f"c_{v}", "lib.a")
            if not os.path.isfile(lib_a):
                continue
            env["CRUST_VARIANT"] = v
            exe = os.path.join(gdir, f"c_{v}")
            rc, log = sc.sh(["gcc", "-w", "-I", gdir, "-o", exe, drv, lib_a] + libs, env=env)
            if rc == 0:
                bins[v] = exe
            elif v == "ref":
                fail = log[-1500:]
                break
        if fail is None:
            break
        res.setdefault("driver_mode_failures", {})[mode] = fail
        sel, bins = [], {}
    res["driver_mode"] = ("typed" if typed else "scalar") if sel else None
    res["functions_gridded"] = [f["name"] for f in sel]
    res["functions_not_gridded"] = {f["name"]: f"{f.get('spelled_ret', f['ret'])} ({', '.join(f.get('spelled_params', f['params']))})"
                                    for f in allf if f not in sel}
    if not sel:
        return res
    for key, so in crates.items():
        exe = os.path.join(gdir, key.replace(":", "_"))
        rc, log = sc.sh(["gcc", "-w", "-I", gdir, "-o", exe, drv, "-L" + os.path.dirname(so), "-lbuild_attempt", "-lm",
                         "-Wl,-rpath," + os.path.dirname(so)])
        if rc == 0:
            bins[key] = exe
        else:
            res.setdefault("rust_link_fail", {})[key] = log[-800:]
    res["n_inputs"] = sum(len(f["inputs"]) for f in sel)
    per_fn = {}
    PER_INPUT_TIMEOUT = 3
    for fi, f in enumerate(sel):
        ro, re_, rrc, rto, *_ = run1(bins["ref"], [str(fi)])
        ro2, re2, rrc2, *_ = run1(bins["ref"], [str(fi)])
        rec = {"inputs": len(f["inputs"]), "c_exit": rrc, "c_whole_run_timed_out": rto,
               "c_deterministic": (ro, re_, rrc) == (ro2, re2, rrc2), "rust": {}}
        cache = {}

        def c_one(ii):
            if ii not in cache:
                cache[ii] = run1(bins["ref"], [str(fi), str(ii)], timeout=PER_INPUT_TIMEOUT)
            return cache[ii]
        whole_ok = not rto and rrc >= 0 and rec["c_deterministic"]
        for key in [k for k in bins if k.startswith("rust:")]:
            if whole_ok:
                o, e, rc, to, *_ = run1(bins[key], [str(fi)])
                if (o, e, rc) == (ro, re_, rrc):
                    rec["rust"][key] = "agree"
                    continue
            # one input per process: the C first; inputs on which the C hangs or crashes are
            # excluded (no oracle); the Rust is run only on the rest
            div, c_hang = [], []
            for ii in range(len(f["inputs"])):
                a = c_one(ii)
                if a[3] or a[2] < 0:
                    c_hang.append(ii)
                    continue
                b = run1(bins[key], [str(fi), str(ii)], timeout=PER_INPUT_TIMEOUT)
                if b[3]:   # slower than the -O2 C is not a hang: retry with a 60 s budget
                    b = run1(bins[key], [str(fi), str(ii)], timeout=60)
                if (a[0], a[1], a[2]) != (b[0], b[1], b[2]):
                    div.append({"input": ii, "args": [STRINGS[x[1]] if x[0] == "s" else x[1] for x in f["inputs"][ii]],
                                "c": [a[0].decode("utf-8", "replace")[:200], a[2]],
                                "rust": [b[0].decode("utf-8", "replace")[:200], b[2], b[1].decode("utf-8", "replace")[-300:]],
                                "rust_timed_out": b[3]})
            if not div and not c_hang:
                rec["rust"][key] = "agree"
            else:
                rec["rust"][key] = {"divergent_inputs": div, "c_hang_or_crash_inputs_excluded": c_hang}
        # UB checks for every input that diverges in any Rust build
        # inputs whose only divergence is that the Rust did not finish within 60 s although the -O2 C
        # did (a slow debug build or a hang) are listed for manual review, not sent to the UB checks
        slow = sorted({d["input"] for v in rec["rust"].values() if v != "agree" for d in v["divergent_inputs"]
                       if d.get("rust_timed_out")})
        div_inputs = sorted({d["input"] for v in rec["rust"].values() if v != "agree" for d in v["divergent_inputs"]
                             if not d.get("rust_timed_out")})
        rec["rust_timeout_inputs"] = slow
        ub = {}
        for ii in div_inputs:
            a = run1(bins["ref"], [str(fi), str(ii)])
            u, san_nr = [], []
            for v in crun.UB_CHECKS:
                if v not in bins:
                    u.append(f"{v}: no build")
                    continue
                r = run1(bins[v], [str(fi), str(ii)], san=v.startswith("san_"))
                if v.startswith("san_"):
                    if r[4]:
                        u.append(f"{v}: {r[4]}")
                    elif r[6]:
                        u.append(f"{v}: infra")
                    elif (r[0], r[2]) != (a[0], a[2]):
                        san_nr.append(v)   # audit 8 uniform rule: disagreement (or timeout) -> not UB-clean
                        u.append(f"{v}: output differs from ref without a sanitizer report (not UB-clean)")
                elif (r[0], r[1], r[2]) != (a[0], a[1], a[2]):
                    u.append(f"{v}: output differs from ref")
            nz = []
            if "san_clang_nz" in bins:
                r = run1(bins["san_clang_nz"], [str(fi), str(ii)], san=True, nz=True)
                nz = ou.other_than_null_zero(r[5])
            ub[ii] = {"c_ub": u, "c_ub_excluding_null_plus_zero": [x for x in u if not x.startswith("san_clang:")] + nz,
                      "san_output_differs_without_report": san_nr}
        rec["ub"] = ub
        per_fn[f["name"]] = rec
    res["per_function"] = per_fn
    # held-out: argument tuples the sample's own tests pass to these functions
    res["own_test_calls"] = own_test_calls(s, bdir, sel, gdir, typed)
    return res


def own_test_calls(s, bdir, sel, gdir, typed=False):
    """Link each own test object against the C with --wrap on every gridded function; log args."""
    W = ["#include <stdio.h>", "#include <stdlib.h>"] + (['#include "lib_headers.h"'] if typed else []) + [
        "static FILE *T; static FILE *tf(void){ if(!T){const char*p=getenv(\"SACTOR_TRACE\"); T=fopen(p?p:\"/dev/null\",\"a\");} return T; }"]
    for f in sel:
        pt = f["spelled_params"] if typed else f["params"]
        rt = f["spelled_ret"] if typed else f["ret"]
        ps = [f"{t} a{i}" for i, t in enumerate(pt)]
        W.append(f"{rt} __real_{f['name']}({', '.join(pt) or 'void'});")
        fmt = []
        vals = []
        for i, t in enumerate(f["params"]):
            cls = f["pcls"][i] if typed else ("scalar", t)
            if cls[0] == "scalar" and cls[1] in STR_T:
                fmt.append("s:%s")
                vals.append(f"a{i} ? a{i} : \"(NULL)\"")
            elif cls[0] == "enum":
                fmt.append("%lld")
                vals.append(f"(long long)a{i}")
            else:
                c, fm = INT_T[cls[1]][0], INT_T[cls[1]][1]
                fmt.append(fm)
                vals.append(f"({c})a{i}")
        args = ", ".join(f"a{i}" for i in range(len(f["params"])))
        W.append(f"{rt} __wrap_{f['name']}({', '.join(ps) or 'void'}) {{ fprintf(tf(), \"{f['name']}|{'|'.join(fmt)}\\n\"{''.join(', ' + v for v in vals)}); fflush(tf()); return __real_{f['name']}({args}); }}")
    wrapc = os.path.join(gdir, "wrap_own.c")
    open(wrapc, "w").write("\n".join(W) + "\n")
    cref = os.path.join(bdir, "c_ref")
    out = {"calls": {}, "objects": {}}
    for t in crun.test_objects(s):
        obj = os.path.join(cref, t)
        if not os.path.isfile(obj):
            continue
        exe = os.path.join(gdir, f"own_traced_{t[:-2]}")
        rc, log = sc.sh(["gcc", "-w", "-I", gdir, "-o", exe, obj, wrapc, os.path.join(cref, "lib.a")] + crun.link_libs(s) + ["-lm"]
                        + [f"-Wl,--wrap={f['name']}" for f in sel])
        if rc != 0:
            out["objects"][t] = "trace link failed: " + log[-600:]
            continue
        trace = os.path.join(gdir, f"own_{t[:-2]}.trace")
        if os.path.exists(trace):
            os.remove(trace)
        for c in crun.own_cases(s):
            if c["kind"] == "unknown":
                continue
            import shutil
            import tempfile
            with tempfile.TemporaryDirectory(dir="/build/tmp") as td:
                wd = os.path.join(td, "w")
                crun.copy_sample(s, wd)
                shutil.copy(exe, os.path.join(wd, "run_test"))
                e = dict(os.environ, SACTOR_TRACE=trace, LC_ALL="C", LANG="C")
                ou.run_proc(["./run_test"] + c["args"], b"", cwd=wd, env=e, timeout=120)
        lines = open(trace, errors="replace").read().splitlines() if os.path.exists(trace) else []
        out["objects"][t] = len(lines)
        for ln in lines:
            fn = ln.split("|")[0]
            out["calls"].setdefault(fn, set()).add(ln)
    # compare with our inputs (same formatting)
    ours = {}
    for f in sel:
        S = set()
        for inp in f["inputs"]:
            parts = []
            for v, t in zip(inp, f["params"]):
                if v[0] == "e":
                    parts.append(str(v[1]))
                elif t in STR_T:
                    parts.append("s:" + STRINGS[v[1]])
                elif v[0] == "f":
                    parts.append(("%.17g" if t == "double" else "%.9g") % (v[1] if t == "double" else float(__import__("struct").unpack("f", __import__("struct").pack("f", v[1]))[0])))
                else:
                    x = v[1]
                    bits = INT_T[t][3]
                    signed = INT_T[t][4]
                    x &= (1 << bits) - 1 if bits < 64 else (1 << 64) - 1
                    if signed and x >= 1 << (bits - 1):
                        x -= 1 << bits
                    parts.append(str(x))
            S.add(f["name"] + "|" + "|".join(parts))
        ours[f["name"]] = S
    summ = {}
    for f in sel:
        own = out["calls"].get(f["name"], set())
        summ[f["name"]] = {"own_distinct_calls": len(own), "our_inputs": len(ours[f["name"]]),
                           "our_inputs_in_own": len(ours[f["name"]] & own)}
    out["per_function"] = summ
    out["our_input_signatures"] = {k: sorted(v) for k, v in ours.items()}
    out["calls"] = {k: sorted(v)[:200] for k, v in out["calls"].items()}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="/build/crust")
    ap.add_argument("--out", default="/work/results/sactor_crust_grid.json")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--only", default=None)
    args = ap.parse_args()
    os.makedirs("/build/tmp", exist_ok=True)
    ss = crun.samples() if not args.only else args.only.split(",")
    out = {"system": "SACTOR", "target": "CRUST-Bench samples: per-function grids (held-out generated inputs)",
           "provenance": sc.provenance(__file__, os.environ.get("SACTOR_COMMAND", " ".join(sys.argv)),
                                       {"strings": STRINGS, "max_inputs_per_function": MAXIN}),
           "samples": {}}
    t0 = time.time()
    with cf.ThreadPoolExecutor(args.jobs) as ex:
        futs = {ex.submit(do_sample, s, args): s for s in ss}
        for fu in cf.as_completed(futs):
            s = futs[fu]
            try:
                out["samples"][s] = fu.result()
            except Exception as e:
                import traceback
                out["samples"][s] = {"sample": s, "error": repr(e), "tb": traceback.format_exc()[-1500:]}
            r = out["samples"][s]
            print(s, r.get("skipped") or r.get("error") or (len(r.get("functions_gridded", [])), {fn: {k: (v if v == "agree" else len(v["divergent_inputs"])) for k, v in rec["rust"].items()} for fn, rec in r.get("per_function", {}).items() if any(v != "agree" for v in rec["rust"].values())}), flush=True)
    out["elapsed_s"] = round(time.time() - t0, 1)
    sc.dump(args.out, out)


if __name__ == "__main__":
    main()
