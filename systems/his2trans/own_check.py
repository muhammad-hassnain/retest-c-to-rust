"""Re-run His2Trans's own check (the RQ2 Rust tests) as the artifact documents it, inside Docker.

The artifact's reproduce.sh runs scripts/run_archived_oss8_metrics.py, which (1) links the archived
crates into a run layout, (2) runs scripts/run_oss_rq2_rust_tests.py (adapts each
tests/source_rq2_tests/<p>/c2r/test.rs to the crate's API, injects it as `mod test_c2r`, runs
`cargo test` per test), then incremental-compile, unsafe and clippy metrics. We run steps (1)-(2)
only (the acceptance verdict), offline, with the same vendored dependencies as run.py.
Writes results/his2trans_own_check.json: per-project verdicts, the adapted test sources actually run
(the system's own inputs), and a comparison with the shipped results/oss8_rq2.json.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run as R  # noqa: E402

ARCH = os.path.join(R.ART, R.OSS8)
PROJECTS = "urlparser,avl,ht,qsort,buffer,rgba,quadtree,genann"


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "/b/own_check2"
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(os.path.join(out_dir, "results"))
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    prep = ("import sys; sys.path.insert(0, %r); import run_archived_oss8_metrics as m; from pathlib import Path; "
            "print(m._prepare_archived_run_layout(Path(%r), Path(%r)))") % (os.path.join(ARCH, "scripts"), ARCH, out_dir)
    run_root = subprocess.run([sys.executable, "-c", prep], cwd=R.ART, env=env, stdout=subprocess.PIPE,
                              text=True, check=True).stdout.strip().splitlines()[-1]
    cmd = [sys.executable, os.path.join(ARCH, "scripts", "run_oss_rq2_rust_tests.py"), "--run-dir", run_root,
           "--projects", PROJECTS, "--tests-dir", os.path.join(ARCH, "tests", "source_rq2_tests"), "--timeout", "240",
           "--work-dir", os.path.join(out_dir, "tmp", "rq2_work"), "--output", os.path.join(out_dir, "results", "oss8_rq2.json")]
    p = subprocess.run(cmd, cwd=R.ART, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    ours = json.load(open(os.path.join(out_dir, "results", "oss8_rq2.json")))
    shipped = json.load(open(os.path.join(ARCH, "results", "oss8_rq2.json")))

    def per(d):
        rs = d["projects"].values() if isinstance(d["projects"], dict) else d["projects"]
        return {r["project"]: {"passed": r["tests_passed"], "total": r["total_tests"],
                               "tests": r.get("function_test_results"), "adapter_actions": r.get("adapter_actions")}
                for r in rs}
    po, ps = per(ours), per(shipped)
    adapted = {}
    for proj in PROJECTS.split(","):
        f = os.path.join(out_dir, "tmp", "rq2_work", proj, "src", "test_c2r.rs")
        if os.path.exists(f):
            s = open(f).read()
            adapted[proj] = {"sha256": hashlib.sha256(s.encode()).hexdigest(), "source": s}
    res = {
        "command_run": " ".join(cmd), "exit_code": p.returncode, "log_tail": p.stdout[-1500:],
        "per_project": {k: {"rerun": po.get(k), "shipped": ps.get(k),
                            "same_verdict": (po.get(k) or {}).get("passed") == (ps.get(k) or {}).get("passed")
                            and (po.get(k) or {}).get("total") == (ps.get(k) or {}).get("total")}
                        for k in PROJECTS.split(",")},
        "rerun_summary": ours.get("summary"), "shipped_summary": shipped.get("summary"),
        "adapted_tests_run": adapted,
        "provenance": R.ou.provenance(__file__, {"artifact_script": OSS8_SCRIPT_NOTE}),
    }
    R.ou.dump(os.path.join(R.REPO, "results", "his2trans_own_check.json"), res)
    print(json.dumps(res["per_project"], indent=1)[:3000])


OSS8_SCRIPT_NOTE = ("data/paper_artifacts/deepseek_v4_pro_oss8_0613_rq2_100pct_minimal/scripts/run_oss_rq2_rust_tests.py "
                    "(steps 1-2 of reproduce.sh)")

if __name__ == "__main__":
    main()
