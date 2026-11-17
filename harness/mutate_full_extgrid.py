"""Are the full-grid survivors of harness/mutate_full.py killable by MORE of the same generator?

The v4 grid caps cat and uniq at 3000 cases (harness/measure_coverage.sh DIFF_CAP=3000); the
uncapped generator (gen_inputs.generate(util, 10**6)) yields cat 7800 and uniq 5148 cases, whose
first 3000 are exactly results/<util>_cases_v4.json (checked below). head (1404) and tail (1170)
are already uncapped, so they have no extension.

For cat and uniq this runs every compiled `survived` mutant (tce ones excluded) on the EXTRA
cases (index >= 3000) on which the unmutated C2Rust baseline agrees with the C on stdout/exit,
using the same comparison (mutate_full.compare), and records whether an S1/crash appears. A kill
here is a witness that the survivor is NOT equivalent and was missed only because of the cap.

  (in the crust-heldout container) python3 mutate_full_extgrid.py --jobs 8
Writes $MFULL_WORK/build/extgrid.json.
"""
import argparse
import json
import multiprocessing as mp
import os
import shutil
import time

import gen_inputs
import mutate_full as MF

UTILS_EXT = ["cat", "uniq"]
CAP = 3000


def extra_cases(util):
    full = gen_inputs.generate(util, 10 ** 6)
    v4 = MF.load_cases(util)
    same_prefix = [c.to_json() for c in full[:CAP]] == [c.to_json() for c in v4]
    return full, same_prefix


_W = {}


def _init(q):
    _W["id"] = q.get()
    _W["dir"] = os.path.join(MF.BUILD, "wx", str(_W["id"]))


def _base_chunk(job):
    util, idxs = job
    full, _ = extra_cases(util)
    return [(i, MF.compare(full[i], MF.oracle_bin(util), MF.base_bin(util))) for i in idxs]


def _mut(job):
    util, m, keep = job
    d = os.path.join(_W["dir"], util)
    src_dir, tgt = os.path.join(d, "rust"), os.path.join(d, "target")
    if not os.path.isdir(src_dir):
        shutil.copytree(os.path.join(MF.TREES, util, MF.VARIANT), src_dir)
    path = os.path.join(src_dir, "src", f"{util}.rs")
    orig = open(path).read()
    t0 = time.time()
    try:
        open(path, "w").write(MF.apply_mutant(orig, m))
        ok, b, log = MF.cargo_build(src_dir, util, tgt)
        if not ok:
            return {"id": m["id"], "status": "noncompile"}
        full, _ = extra_cases(util)
        timeouts = 0
        for i in keep:
            v = MF.compare(full[i], MF.oracle_bin(util), b, MF.TIMEOUT)
            if v["timed_out"]:
                timeouts += 1
                if timeouts >= MF.HANG_CAP:
                    return {"id": m["id"], "status": "hang", "case": v["id"],
                            "seconds": round(time.time() - t0, 1)}
                continue
            if v["mismatch"]:
                crash = (v["sut_exit"] < 0 and not v["_sut_capped"]) or v["sut_exit"] == 101
                return {"id": m["id"], "status": "killed_crash" if crash else "killed_s1",
                        "case": v["id"], "argv": v["argv"],
                        "seconds": round(time.time() - t0, 1)}
        return {"id": m["id"], "status": "survived", "seconds": round(time.time() - t0, 1)}
    finally:
        open(path, "w").write(orig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=8)
    args = ap.parse_args()
    sites = json.load(open(os.path.join(MF.BUILD, "sites.json")))
    byid = {m["id"]: m for u in MF.UTILS for m in sites["mutants"][u]}
    rows = {json.loads(ln)["id"]: json.loads(ln)
            for ln in open(os.path.join(MF.BUILD, "mutants.jsonl"))}
    report = {"cap": CAP, "utils": {}}
    for util in UTILS_EXT:
        full, same = extra_cases(util)
        idxs = list(range(CAP, len(full)))
        with mp.Pool(args.jobs) as pool:
            parts = pool.map(_base_chunk, [(util, idxs[k::args.jobs]) for k in range(args.jobs)])
        vs = sorted([x for p in parts for x in p], key=lambda t: t[0])
        keep = [i for i, v in vs if not v["mismatch"] and v["assessable"]]
        surv = [byid[i] for i, r in rows.items() if i in byid and byid[i]["util"] == util
                and byid[i]["op"] in ("rel", "arith", "const") and r["status"] == "survived"
                and not r.get("tce")]
        q = mp.Queue()
        for k in range(args.jobs):
            q.put(k)
        with mp.Pool(args.jobs, initializer=_init, initargs=(q,)) as pool:
            res = pool.map(_mut, [(util, m, keep) for m in surv], chunksize=1)
        report["utils"][util] = {
            "uncapped_cases": len(full), "first_3000_identical_to_v4_file": same,
            "extra_cases": len(idxs), "extra_cases_baseline_agrees": len(keep),
            "extra_cases_baseline_mismatch": len(idxs) - len(keep),
            "survivors_tested": len(surv),
            "killed_by_extra_cases": sum(r["status"] in ("killed_s1", "killed_crash")
                                         for r in res),
            "hang_on_extra_cases": sum(r["status"] == "hang" for r in res),
            "results": res}
        print(util, json.dumps({k: v for k, v in report["utils"][util].items()
                                if k != "results"}), flush=True)
    json.dump(report, open(os.path.join(MF.BUILD, "extgrid.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
