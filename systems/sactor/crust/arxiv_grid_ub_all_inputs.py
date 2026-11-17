#!/usr/bin/env python3
"""arXiv re-run: the UB rule on EVERY input of the CRUST per-function grids that
SACTOR's mutation analysis uses (systems/sactor/crust/mutate.py, results/sactor_crust_mutation.json).

mutate.py counts a grid kill when the mutant's whole-run output for one function differs from the C's
(killed_by_grid is a boolean; the killing input is not recorded) and does not ask whether the C is
UB-clean on the inputs that differ. grid.py ran its UB checks only on inputs where an unmutated
translation diverged, so for the agreeing functions no per-input UB verdict exists. This script runs
grid.py's per-input UB check (same C builds: c_<variant> of crun.UB_CHECKS linked from the same
grid_driver.c; the same comparison: a sanitizer report, a sanitizer start-up failure, or a sanitizer
build whose stdout/exit differs from the -O2 ref, or an o0/o1/uchar/schar build whose stdout, stderr or
exit differs) on every input of every gridded function of the given samples, plus the ref run itself
(a crash or timeout of the ref leaves the input not UB-clean). If every input is UB-clean, every
grid kill of those samples is on a UB-clean input.

  python3 -I systems/sactor/crust/arxiv_grid_ub_all_inputs.py --samples NandC,geofence --out /out/sactor_grid_ub.json
"""
import argparse
import json
import os
import platform
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import grid as cgrid  # noqa: E402
import run as crun  # noqa: E402

ou = cgrid.ou
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))


def check_input(bins, fi, ii):
    a = cgrid.run1(bins["ref"], [str(fi), str(ii)])
    u = []
    if a[3] or a[2] < 0:
        u.append(f"ref: C crashed or timed out (rc={a[2]}, timed_out={a[3]})")
    for v in crun.UB_CHECKS:
        if v not in bins:
            u.append(f"{v}: no build")
            continue
        r = cgrid.run1(bins[v], [str(fi), str(ii)], san=v.startswith("san_"))
        if v.startswith("san_"):
            if r[4]:
                u.append(f"{v}: {r[4]}")
            elif r[6]:
                u.append(f"{v}: infra")
            elif (r[0], r[2]) != (a[0], a[2]):
                u.append(f"{v}: output differs from ref without a sanitizer report (not UB-clean)")
        elif (r[0], r[1], r[2]) != (a[0], a[1], a[2]):
            u.append(f"{v}: output differs from ref")
    return {"input": ii, "ref_rc": a[2], "ref_out": a[0].decode("utf-8", "replace")[:120], "c_ub": u}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="/build/crust")
    ap.add_argument("--samples", required=True)
    ap.add_argument("--grid", default=os.path.join(REPO, "results", "sactor_crust_grid.json"))
    ap.add_argument("--mutation", default=os.path.join(REPO, "results", "sactor_crust_mutation.json"))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    t0 = time.time()
    g = json.load(open(a.grid))["samples"]
    mu = json.load(open(a.mutation))["samples"]
    out = {"samples": {}}
    for s in a.samples.split(","):
        gdir = os.path.join(a.build, s, "grid")
        bins = {v: os.path.join(gdir, f"c_{v}") for v in crun.C_VARIANTS if os.path.isfile(os.path.join(gdir, f"c_{v}"))}
        fns = g[s]["functions_gridded"]
        rec = {"functions": {}, "c_bins": sorted(bins),
               "grid_kills_in_mutation_file": sum(1 for m in mu[s]["mutants"] if m.get("killed_by_grid")),
               "built_mutants": sum(1 for m in mu[s]["mutants"] if m.get("status") == "built")}
        for fi, fn in enumerate(fns):
            n = g[s]["per_function"][fn]["inputs"]
            # the whole-run ref output must equal grid.py's recorded run (exit code; same driver)
            whole = cgrid.run1(bins["ref"], [str(fi)])
            rows = [check_input(bins, fi, ii) for ii in range(n)]
            rec["functions"][fn] = {"index": fi, "inputs": n, "whole_run_ref_exit": whole[2],
                                    "whole_run_ref_exit_recorded": g[s]["per_function"][fn]["c_exit"],
                                    "ub_clean_inputs": sum(1 for r in rows if not r["c_ub"]),
                                    "not_ub_clean": [r for r in rows if r["c_ub"]]}
            print(s, fn, n, rec["functions"][fn]["ub_clean_inputs"], flush=True)
        rec["all_inputs_ub_clean"] = all(f["ub_clean_inputs"] == f["inputs"] for f in rec["functions"].values())
        out["samples"][s] = rec
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    from provenance import stamp  # noqa: E402
    out["summary"] = {s: {"all_inputs_ub_clean": r["all_inputs_ub_clean"],
                          "inputs": sum(f["inputs"] for f in r["functions"].values()),
                          "ub_clean_inputs": sum(f["ub_clean_inputs"] for f in r["functions"].values()),
                          "grid_kills_in_mutation_file": r["grid_kills_in_mutation_file"]}
                      for s, r in out["samples"].items()}
    out["provenance"] = {"script": "systems/sactor/crust/arxiv_grid_ub_all_inputs.py",
                         "command": os.environ.get("ARXIV_COMMAND") or " ".join(["python3"] + sys.argv),
                         "git_commit_of_study_repo": os.environ.get("CRUST_ARTIFACT_COMMIT"),
                         "host": "Linux x86_64 box (20 cores)", "arch": platform.machine(),
                         "image": os.environ.get("ARXIV_IMAGE"), "image_id": os.environ.get("ARXIV_IMAGE_ID"),
                         "c_builds": "build/crust/<sample>/grid/c_<variant> copied from the original re-grade's "
                                     "work dir (~/work/regrade-sactor), built there by grid.py",
                         "toolchains": ou.toolchains(), "ub_checks": crun.UB_CHECKS,
                         "seconds": round(time.time() - t0, 1), "date": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    out["_provenance"] = stamp(image=os.environ.get("ARXIV_IMAGE"))
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps(out["summary"], indent=1))


if __name__ == "__main__":
    main()
