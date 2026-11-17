#!/usr/bin/env python3
"""Harness power for the libogg harness: seed one operator-replacement fault at a time into an
agreeing SACTOR translation, rebuild, re-run the libogg harness (all generated cases, C ref vs the
mutant), and count a mutant as KILLED if any case diverges from the C beyond the divergences the
unmutated translation already has (method.tex, "Harness adequacy"; same operators as the VERT
scalar harness: relational and arithmetic).

Targets (agreeing translations): gpt-5 framing.c (agrees on every case) and gpt-4o bitwise.c
(agrees except the 8 cases of its oggpackB_write defect; a mutant is killed only by a case
outside those 8).
Sites: every relational / arithmetic operator token inside a fn body of the exposed crate
(comments, strings and static tables excluded), selected evenly by a fixed stride.

    python3 systems/sactor/libogg/mutate.py --build /build/libogg --n 40
"""
import argparse
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import sactor_common as sc  # noqa: E402
import gen_cases  # noqa: E402

ou = sc.ou
SWAP = {"<": "<=", "<=": "<", ">": ">=", ">=": ">", "==": "!=", "!=": "==", "+": "-", "-": "+", "*": "/"}
TOK = re.compile(r"(<=|>=|==|!=|<<|>>|->|=>|\+=|-=|\*=|/=|&&|\|\||::|<|>|\+|-|\*)")
TARGETS = [("gpt-5", "framing"), ("gpt-4o", "bitwise")]


def sites(code):
    """(offset, token) for mutable operator tokens inside function bodies, not in comments/strings,
    not in generic brackets (`Vec<u8>`), not unary minus on literals."""
    out = []
    in_fn = False
    depth = 0
    i = 0
    n = len(code)
    line_start = True
    while i < n:
        ch = code[i]
        if code.startswith("//", i):
            j = code.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == '"':
            j = i + 1
            while j < n and code[j] != '"':
                j += 2 if code[j] == "\\" else 1
            i = j + 1
            continue
        if ch == "'" and i + 2 < n and (code[i + 2] == "'" or code[i + 1] == "\\"):
            j = code.find("'", i + 2 if code[i + 1] != "\\" else i + 3)
            i = j + 1
            continue
        if code.startswith("fn ", i) and depth == 0:
            in_fn = True
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                in_fn = False
        if in_fn and depth >= 1:
            m = TOK.match(code, i)
            if m:
                t = m.group(1)
                # rustfmt puts spaces around binary operators; generics (Vec<u8>, ::<T>), unary
                # minus, deref and raw-pointer types (*mut) never have a space on both sides
                if t in SWAP and code[i - 1:i] == " " and code[m.end():m.end() + 1] == " ":
                    out.append((i, t))
                i = m.end()
                continue
        i += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="/build/libogg")
    ap.add_argument("--out", default="/work/results/sactor_libogg_mutation.json")
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--retry-build-fail", action="store_true",
                    help="re-run only the mutants recorded as build_fail in --out (a build can fail for reasons "
                         "outside the mutant, e.g. the host OOM-killing rustc); merge the results into --out")
    args = ap.parse_args()
    prev = json.load(open(args.out)) if args.retry_build_fail else None
    import run as ogg_run
    cases = {"bitwise": gen_cases.bitwise_cases(), "framing": gen_cases.framing_cases()}
    base = json.load(open(os.path.join(os.path.dirname(args.out), "sactor_libogg.json")))
    out = {"provenance": sc.provenance(__file__, os.environ.get("SACTOR_COMMAND", " ".join(sys.argv)),
                                       {"toolchain": sc.PRIMARY_TC + " debug", "operators": SWAP}), "targets": {}}
    if prev is not None:
        out["provenance"]["first_run"] = prev["provenance"]
        out["provenance"]["note"] = ("mutants recorded as build_fail in the first run were rebuilt and re-run "
                                     "(entries marked retried_after_transient_build_failure). During the first run "
                                     "the x86 box was at load ~77 with ~4 GB of 30 GB free (other jobs held ~20 GB), "
                                     "and the same mutants failed in contiguous blocks that differed between two runs.")
    for model, file in TARGETS:
        fdir = os.path.join(args.build, file)
        code = open(os.path.join(fdir, f"lib_{model}_{file}_unidiomatic.rs")).read()
        ss = sites(code)
        stride = max(1, len(ss) // args.n)
        chosen = ss[::stride][:args.n]
        refexe = os.path.join(fdir, f"c_{file}_ref")
        refout = {}
        for c in cases[file]:
            o, e, rc, to = ou.run_proc([refexe], c["script"].encode(), timeout=60)
            refout[c["id"]] = (o, e, rc)
        known = {r["id"] for r in base["files"][file]["rows"]
                 for rn, v in r["rust"].items() if v != "agree" and f":{model}:" in rn}
        drvo = os.path.join(fdir, f"drv_{file}.o")
        crate = os.path.join(args.build, "mut", f"{model}_{file}")
        res = []
        retry = None
        if prev is not None:
            pt = prev["targets"][f"{model}/{file}.c"]
            retry = {m["site"] for m in pt["mutants"] if m["status"] == "build_fail"}
            res = [m for m in pt["mutants"] if m["site"] not in retry]
        for k, (off, tok) in enumerate(chosen):
            if retry is not None and k not in retry:
                continue
            new = SWAP[tok]
            mcode = code[:off] + new + code[off + len(tok):]
            line = code.count("\n", 0, off) + 1
            fn_name = re.findall(r"fn\s+(\w+)", code[:off])[-1]
            ok, log, so = sc.build_crate(mcode, crate, sc.PRIMARY_TC, "debug")
            rec = {"site": k, "line": line, "fn": fn_name, "op": f"{tok} -> {new}"}
            if retry is not None:
                rec["retried_after_transient_build_failure"] = True
            if not ok:
                rec["status"] = "build_fail"   # stillborn: not counted
                rec["errors"] = [ln for ln in log.splitlines() if ln.startswith("error")][:3]
                res.append(rec)
                continue
            exe = os.path.join(args.build, "mut", f"exe_{model}_{file}")
            rc, lg = sc.sh(["gcc", "-o", exe, drvo, "-L" + os.path.dirname(so), "-lbuild_attempt", "-lm",
                            "-Wl,-rpath," + os.path.dirname(so)])
            killed_by = []
            for c in cases[file]:
                if c["id"] in known:
                    continue
                o, e, rc2, to = ou.run_proc([exe], c["script"].encode(), timeout=30)
                if (o, e, rc2) != refout[c["id"]]:
                    killed_by.append(c["id"])
                    if len(killed_by) >= 3:
                        break
            rec["status"] = "killed" if killed_by else "survived"
            rec["killed_by"] = killed_by
            res.append(rec)
            print(model, file, rec, flush=True)
        res.sort(key=lambda r: r["site"])
        valid = [r for r in res if r["status"] != "build_fail"]
        out["targets"][f"{model}/{file}.c"] = {
            "candidate_sites": len(ss), "selected": len(chosen), "stride": stride,
            "build_fail": len(res) - len(valid), "valid": len(valid),
            "killed": sum(1 for r in valid if r["status"] == "killed"),
            "survived": [r for r in valid if r["status"] == "survived"],
            "mutants": res}
    sc.dump(args.out, out)
    print(json.dumps({k: {x: v[x] for x in ("candidate_sites", "selected", "build_fail", "valid", "killed")}
                      for k, v in out["targets"].items()}, indent=1))


if __name__ == "__main__":
    main()
