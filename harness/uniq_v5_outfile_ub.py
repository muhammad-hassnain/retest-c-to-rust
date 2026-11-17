"""Oracle-UB rule (harness/oracle_ub_c2saferrust.py) applied to the OUTPUT-FILE channel of the
uniq v5 cases whose output file differs (results/uniq_diff_v5.json, file_tree_match false).

oracle_ub_c2saferrust.py runs every build in one shared dir and compares stdout/exit only, so for
`uniq IN OUT` it never compared OUT. Here each build (ref, sut, o1, o2, the three sanitizer builds)
runs in its OWN fresh dir and the left-behind file tree is compared too. Same builds, same rule:
a case is original-C UB if a sanitizer reports on the C, or -O1/-O2 differs from -O0 in stdout,
exit or output tree; the *_excluding_null_plus_zero verdict ignores only clang's NULL+0 report.

  docker run --rm -v $W:/work -w /work crust-heldout python3 harness/uniq_v5_outfile_ub.py
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import oracle_ub_common as ou  # noqa: E402
import oracle_ub_c2saferrust as O  # noqa: E402
from common import Case, controlled_env  # noqa: E402
from difftest import _snapshot_tree, _tree_diff  # noqa: E402

REPO = ou.REPO
RUN = ["ref", "sut", "o1", "o2", "san_clang", "san_gcc", "san_clang_nz"]


def run_one(case, binp, name, timeout=15):
    env = controlled_env()
    env.update(case.env or {})
    with tempfile.TemporaryDirectory() as wd:
        O.materialize(case, wd)
        argv = [binp] + list(case.argv)
        if name.startswith("san_"):
            r = ou.run_san(argv, case.stdin(), cwd=wd, env=env, timeout=timeout * 4,
                           halt=not name.endswith("_nz"))
        else:
            o, e, rc, to = ou.run_proc(argv, case.stdin(), cwd=wd, env=env, timeout=timeout)
            r = {"stdout": o, "stderr": e, "rc": rc, "timed_out": to, "report": None, "reports": []}
        r["tree"] = _snapshot_tree(wd)
    return r


def main():
    t0 = time.time()
    build = "/tmp/uniq_v5_ub"
    cases = {c["id"]: Case.from_json(c) for c in
             json.load(open(os.path.join(REPO, "results", "uniq_cases_v5.json")))["cases"]}
    v5 = json.load(open(os.path.join(REPO, "results", "uniq_diff_v5.json")))["verdicts"]
    ids = [v["id"] for v in v5 if v.get("file_tree_match") is False]
    csrc = os.path.join(build, "uniq", "c_src")
    ou.copy_sources(os.path.join(O.SRC, "uniq", "c"), csrc)
    bins = {}
    for name in RUN:
        if name == "sut":
            b = O.build_sut("uniq", build)
        else:
            b = O.build_c_variant("uniq", csrc, os.path.join(build, "uniq", "c_bin"), name)
        if not b["ok"]:
            raise SystemExit(f"build failed: {name}")
        bins[name] = b["bin"]
    rows = []
    for i in ids:
        c = cases[i]
        res = {n: run_one(c, bins[n], n) for n in RUN}
        ref, sut = res["ref"], res["sut"]
        out_name = c.argv[-1]
        row = {"id": i, "argv": c.argv, "env": c.env,
               "ref_exit": ref["rc"], "sut_exit": sut["rc"],
               "ref_outfile_hex": ref["tree"].get(out_name, b"").hex(),
               "sut_outfile_hex": sut["tree"].get(out_name, b"").hex(),
               "ref_vs_sut_tree_diffs": _tree_diff(ref["tree"], sut["tree"])}
        opt = []
        for o in ("o1", "o2"):
            d = [k for k, same in (("stdout", res[o]["stdout"] == ref["stdout"]), ("exit", res[o]["rc"] == ref["rc"]),
                                   ("output_tree", res[o]["tree"] == ref["tree"])) if not same]
            if d:
                opt.append(f"opt-level: -{o.upper()} differs from -O0 in " + ",".join(d))
        ub = [f"{s}: {res[s]['report']}" for s in O.SANS if res[s]["report"]]
        ub_nz = [f"san_gcc: {res['san_gcc']['report']}"] if res["san_gcc"]["report"] else []
        ub_nz += [f"san_clang_nz: {x}" for x in ou.other_than_null_zero(res["san_clang_nz"]["reports"])]
        row["san_output_tree_same_as_ref"] = {s: res[s]["tree"] == ref["tree"] for s in O.SANS}
        row["c_ub_evidence"] = ub + opt
        row["c_ub_evidence_excluding_null_plus_zero"] = ub_nz + opt
        row["final"] = "original_C_UB" if row["c_ub_evidence"] else "counted"
        row["final_excluding_null_plus_zero"] = "original_C_UB" if row["c_ub_evidence_excluding_null_plus_zero"] else "counted"
        rows.append(row)
    out = {"provenance": ou.provenance(__file__, {"wall_seconds": round(time.time() - t0),
                                                   "inputs": ["results/uniq_cases_v5.json", "results/uniq_diff_v5.json"]}),
           "n_cases": len(rows),
           "summary": {k: {v: sum(1 for r in rows if r[k] == v) for v in ("counted", "original_C_UB")}
                       for k in ("final", "final_excluding_null_plus_zero")},
           "rows": rows}
    ou.dump(os.path.join(REPO, "results", "uniq_v5_outfile_ub.json"), out)
    print(json.dumps(out["summary"]))


if __name__ == "__main__":
    main()
