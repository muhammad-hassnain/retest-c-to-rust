"""Check the `tce` (trivially-equivalent) flag of harness/mutate_full.py from scratch.

mutate_full.py marks a mutant `tce` when the incrementally rebuilt debug binary is byte-identical
to that worker's unmutated build. An identical binary could also mean cargo skipped the rebuild,
so this re-builds every tce mutant NON-incrementally (CARGO_INCREMENTAL=0, its own target dir,
bin crate fully recompiled each time) and compares against a fresh unmutated build. As a control
it also rebuilds the first 3 compiled non-tce mutants per util, which must differ.

  (in the crust-heldout container, as mutate_full_run.sh) python3 mutate_full_tce_check.py
Writes $MFULL_WORK/build/tce_check.json.
"""
import json
import os
import shutil

import mutate_full as MF


def main():
    sites = json.load(open(os.path.join(MF.BUILD, "sites.json")))
    byid = {m["id"]: m for u in MF.UTILS for m in sites["mutants"][u]}
    rows = [json.loads(ln) for ln in open(os.path.join(MF.BUILD, "mutants.jsonl"))]
    out = {"tce": [], "controls": []}
    for util in MF.UTILS:
        d = os.path.join(MF.BUILD, "tce", util)
        shutil.rmtree(d, ignore_errors=True)
        shutil.copytree(os.path.join(MF.TREES, util, MF.VARIANT), os.path.join(d, "rust"))
        src_dir, tgt = os.path.join(d, "rust"), os.path.join(d, "target")
        path = os.path.join(src_dir, "src", f"{util}.rs")
        orig = open(path).read()
        os.environ["CARGO_INCREMENTAL"] = "0"
        ok, b, log = MF.cargo_build(src_dir, util, tgt)
        assert ok, log[-2000:]
        base = MF.sha256_file(b)
        tce = [r for r in rows if r["id"] in byid and byid[r["id"]]["util"] == util
               and r.get("tce")]
        ctl = [r for r in rows if r["id"] in byid and byid[r["id"]]["util"] == util
               and r.get("status") != "noncompile" and not r.get("tce")
               and byid[r["id"]]["op"] not in ("sanity",)][:3]
        for kind, group in (("tce", tce), ("controls", ctl)):
            for r in group:
                m = byid[r["id"]]
                try:
                    open(path, "w").write(MF.apply_mutant(orig, m))
                    ok, b, log = MF.cargo_build(src_dir, util, tgt)
                    same = ok and MF.sha256_file(b) == base
                finally:
                    open(path, "w").write(orig)
                out[kind].append({"id": r["id"], "built": ok, "identical_to_baseline": same})
                print(kind, r["id"], ok, same, flush=True)
    out["tce_confirmed"] = sum(x["identical_to_baseline"] for x in out["tce"])
    out["tce_total"] = len(out["tce"])
    out["controls_differ"] = sum(not x["identical_to_baseline"] for x in out["controls"])
    out["controls_total"] = len(out["controls"])
    json.dump(out, open(os.path.join(MF.BUILD, "tce_check.json"), "w"), indent=1)
    print(json.dumps({k: out[k] for k in ("tce_confirmed", "tce_total", "controls_differ",
                                          "controls_total")}))


if __name__ == "__main__":
    main()
