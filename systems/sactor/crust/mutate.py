#!/usr/bin/env python3
"""Harness power for the two CRUST-Bench harnesses (own-test comparison, crust/run.py; per-function
grids, crust/grid.py): seed one operator-replacement fault at a time (relational / arithmetic, the
VERT scalar operator set, libogg/mutate.py's site rule) into an agreeing SACTOR unidiomatic
translation, rebuild it exactly as crust/run.py does, and re-run
  (a) the sample's own test, compared byte for byte with the C (crust/run.py), and
  (b) the per-function grid of crust/grid.py, compared with the C.
A mutant is killed by a harness if that harness reports a divergence the unmutated translation
does not have.

    python3 systems/sactor/crust/mutate.py --samples morton,avalanche,libbase122 --n 12
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import importlib.util  # noqa: E402
import sactor_common as sc  # noqa: E402
import run as crun  # noqa: E402
import grid as cgrid  # noqa: E402
_spec = importlib.util.spec_from_file_location("ogg_mutate", os.path.join(os.path.dirname(HERE), "libogg", "mutate.py"))
_om = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_om)
sites, SWAP = _om.sites, _om.SWAP   # libogg/mutate.py's site rule and operator set

ou = sc.ou


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="/build/crust")
    ap.add_argument("--samples", default="morton,avalanche,libbase122")
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--out", default="/work/results/sactor_crust_mutation.json")
    args = ap.parse_args()
    out = {"provenance": sc.provenance(__file__, os.environ.get("SACTOR_COMMAND", " ".join(sys.argv)),
                                       {"toolchain": sc.PRIMARY_TC + " debug", "operators": SWAP}), "samples": {}}
    for s in args.samples.split(","):
        bdir = os.path.join(args.build, s)
        tdir = os.path.join(crun.CB, s, "result", "translated_code_unidiomatic")
        names = {f[:-3] for f in os.listdir(os.path.join(tdir, "functions"))}
        code = sc.expose_to_c(open(os.path.join(tdir, "combined.rs")).read(), names)
        ss = sites(code)
        stride = max(1, len(ss) // args.n)
        chosen = ss[::stride][:args.n]
        cref = os.path.join(bdir, "c_ref")
        gdir = os.path.join(bdir, "grid")
        drv = os.path.join(gdir, "grid_driver.c")
        grid_ref = os.path.join(gdir, "c_ref")
        nfn = len(open(drv).read().split("if (sactor_F == ")) - 1 if os.path.isfile(drv) else 0
        own = [c for c in crun.own_cases(s) if c["kind"] != "unknown"]
        objs = crun.test_objects(s)
        ref_own = {(c["id"], t): crun.run_bin(s, os.path.join(bdir, "bin", "ref", t[:-2], "run_test"), c["args"])
                   for c in own for t in objs}
        ref_grid = {fi: cgrid.run1(grid_ref, [str(fi)]) for fi in range(nfn)}
        res = []
        for k, (off, tok) in enumerate(chosen):
            mcode = code[:off] + SWAP[tok] + code[off + len(tok):]
            fn_name = re.findall(r"fn\s+(\w+)", code[:off])[-1]
            rec = {"site": k, "line": code.count("\n", 0, off) + 1, "fn": fn_name, "op": f"{tok} -> {SWAP[tok]}"}
            crate = os.path.join(bdir, "mut_crate")
            ok, log, so = sc.build_crate(mcode, crate, sc.PRIMARY_TC, "debug")
            if not ok:
                rec["status"] = "build_fail"
                rec["errors"] = [ln for ln in log.splitlines() if ln.startswith("error")][:3]
                res.append(rec)
                continue
            libdir = os.path.dirname(so)
            killed_own = False
            for c in own:
                for t in objs:
                    exe = os.path.join(bdir, "mut_bin", t[:-2], "run_test")
                    os.makedirs(os.path.dirname(exe), exist_ok=True)
                    sc.sh(["gcc", "-o", exe, os.path.join(cref, t), "-L" + libdir, "-lbuild_attempt", "-lm", "-Wl,-rpath," + libdir])
                    r = crun.run_bin(s, exe, c["args"])
                    a = ref_own[(c["id"], t)]
                    if (r["stdout"], r["stderr"], r["rc"]) != (a["stdout"], a["stderr"], a["rc"]):
                        killed_own = True
            killed_grid = False
            if nfn:
                gexe = os.path.join(bdir, "mut_bin", "grid")
                sc.sh(["gcc", "-w", "-I", gdir, "-o", gexe, drv, "-L" + libdir, "-lbuild_attempt", "-lm", "-Wl,-rpath," + libdir])
                for fi in range(nfn):
                    r = cgrid.run1(gexe, [str(fi)])
                    if (r[0], r[1], r[2]) != ref_grid[fi][:3]:
                        killed_grid = True
                        break
            rec.update({"status": "built", "killed_by_own_test": killed_own, "killed_by_grid": killed_grid})
            res.append(rec)
            print(s, rec, flush=True)
        valid = [r for r in res if r["status"] == "built"]
        out["samples"][s] = {"candidate_sites": len(ss), "selected": len(chosen), "valid": len(valid),
                             "build_fail": len(res) - len(valid), "gridded_functions": nfn,
                             "killed_own_test": sum(r["killed_by_own_test"] for r in valid),
                             "killed_grid": sum(r["killed_by_grid"] for r in valid),
                             "killed_either": sum(r["killed_by_own_test"] or r["killed_by_grid"] for r in valid),
                             "mutants": res}
    sc.dump(args.out, out)
    print(json.dumps({s: {k: v for k, v in d.items() if k != "mutants"} for s, d in out["samples"].items()}, indent=1))


if __name__ == "__main__":
    main()
