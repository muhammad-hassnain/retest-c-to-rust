"""Shared helpers for the ACTOR (UW; Schesch and Ernst, IEEE S&P 2026) re-grade.

Artifact: github.com/UW-HARVEST/ACTOR at 1b1e70a (the last commit whose generated tables give the
paper's TRACTOR numbers, Kiro 325/338, Claude Code 319/338, Codex 244/338), with its `results`
submodule github.com/UW-HARVEST/harvest-agentic-translate-results at e31aec03. We grade the
ACTOR (Kiro) translations of the TRACTOR Test-Corpus: results/Test-Corpus/kiro/<battery>/<case>/.

The scorer reads <case>/verified/ if present, else <case>/translated/ (ACTOR README, "scoring reads
`verified/` if present, else `translated/`"); we grade the same directory. Each phase dir ships
the C it was given (c_src/) next to the Rust (Cargo.toml, src/).

Everything here is pure Python (no third-party imports) so it runs inside the crust-heldout image.
"""
import json
import os
import re
import shlex

HERE = os.path.dirname(os.path.abspath(__file__))
W = os.environ.get("ACTORUW_W", "/w")                      # work root inside the container
KIRO = os.environ.get("ACTORUW_KIRO", os.path.join(W, "artifact/Test-Corpus/kiro"))
BATTERIES = ["B01_organic", "B01_synthetic", "B02_organic", "B02_synthetic",
             "P00_perlin_noise", "P01_sphincs_plus"]


def phase_dir(battery, case):
    d = os.path.join(KIRO, battery, case)
    for ph in ("verified", "translated"):
        if os.path.isfile(os.path.join(d, ph, "Cargo.toml")):
            return ph, os.path.join(d, ph)
    return None, None


def all_cases():
    out = []
    for b in BATTERIES:
        bd = os.path.join(KIRO, b)
        if not os.path.isdir(bd):
            continue
        for c in sorted(os.listdir(bd)):
            if os.path.isdir(os.path.join(bd, c)):
                out.append((b, c))
    return out


def is_lib(case):
    return case.endswith("_lib")


def exe_cases():
    """Program (executable) translations: every non-_lib case outside P01 (P01's 128 cases are
    configurations of ONE library translation)."""
    return [(b, c) for (b, c) in all_cases() if not is_lib(c) and b != "P01_sphincs_plus"]


def recorded_result(battery, case):
    """The system's own recorded verdict (result.json in the graded phase dir)."""
    ph, d = phase_dir(battery, case)
    if not d:
        return None
    p = os.path.join(d, "result.json")
    if not os.path.isfile(p):
        return None
    with open(p) as f:
        return json.load(f)


# ------------------------------------------------------------------ CMake (subset) -> gcc recipe

_MACRODEPTH = re.compile(r"^macrodepth_(add|sub|mul)_(\d+)$")


def case_config(case):
    """The configuration under test. Only the macrodepth cases are configured: the case name
    encodes the CMake cache variables OP and REPEAT that the benchmark builds the C with (their
    CMakeLists default is add/5 for all three). Returns (c_overrides, cargo_feature_args)."""
    m = _MACRODEPTH.match(case)
    if m:
        op, rep = m.group(1), m.group(2)
        return {"OP": op, "REPEAT": rep}, ["--no-default-features", "--features", f"op_{op},repeat_{rep}"]
    return {}, []


def parse_cmake(cmake_path, overrides=None):
    """Very small CMake subset used by every TRACTOR executable case: set(VAR "v" CACHE ...),
    set(CMAKE_C_FLAGS "..."), add_executable(name srcs...), target_include_directories,
    target_compile_options, target_link_libraries. Anything else is reported, not guessed."""
    txt = open(cmake_path).read()
    txt = re.sub(r"#[^\n]*", "", txt)
    vars_ = {}
    recipe = {"sources": [], "includes": [], "cflags": [], "libs": [], "unparsed": []}
    for m in re.finditer(r"(\w+)\s*\(([^)]*)\)", txt, re.S):
        cmd, args = m.group(1).lower(), m.group(2)
        toks = shlex.split(args.replace("\n", " "))

        def sub(s):
            return re.sub(r"\$\{(\w+)\}", lambda mm: vars_.get(mm.group(1), ""), s)
        if cmd == "set":
            name = toks[0]
            val = toks[1] if len(toks) > 1 else ""
            if name in (overrides or {}):
                val = overrides[name]
            vars_[name] = sub(val)
        elif cmd == "add_executable":
            recipe["name"] = toks[0]
            recipe["sources"] += [sub(t) for t in toks[1:]]
        elif cmd == "add_library":
            recipe["name"] = toks[0]
            recipe["sources"] += [sub(t) for t in toks[1:] if t not in ("SHARED", "STATIC", "MODULE", "OBJECT")]
        elif cmd in ("cmake_path", "install"):
            pass
        elif cmd in ("target_include_directories", "include_directories"):
            recipe["includes"] += [sub(t) for t in toks if t not in ("PUBLIC", "PRIVATE", "INTERFACE")
                                   and (cmd == "include_directories" or t != toks[0])]
        elif cmd in ("target_compile_options", "add_compile_options"):
            recipe["cflags"] += [sub(t) for t in toks if t not in ("PUBLIC", "PRIVATE", "INTERFACE")
                                 and (cmd == "add_compile_options" or t != toks[0])]
        elif cmd == "target_link_libraries":
            recipe["libs"] += ["-l" + sub(t) for t in toks[1:] if t not in ("PUBLIC", "PRIVATE", "INTERFACE")]
        elif cmd in ("cmake_minimum_required", "project"):
            pass
        else:
            recipe["unparsed"].append(cmd)
    if vars_.get("CMAKE_C_FLAGS"):
        recipe["cflags"] = shlex.split(vars_["CMAKE_C_FLAGS"]) + recipe["cflags"]
    recipe["vars"] = vars_
    return recipe


def cargo_bin_name(cargo_toml_path):
    txt = open(cargo_toml_path).read()
    m = re.search(r"\[\[bin\]\][^\[]*?name\s*=\s*\"([^\"]+)\"", txt, re.S)
    if m:
        return m.group(1)
    m = re.search(r"\[package\][^\[]*?name\s*=\s*\"([^\"]+)\"", txt, re.S)
    return m.group(1) if m else None


def dump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
        f.write("\n")
    os.replace(tmp, path)
