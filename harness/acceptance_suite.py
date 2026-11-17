"""C2SaferRust's acceptance suite, replicated exactly as translate.py runs it (audit 5).

translate.py (run from the artifact root, README line 46) selects the tests with
`select_tests` (231-248): every `*.sh` in tests/<util>/ is run as
`subprocess.run(f"{test_dir/test} {code_dir}/target/debug", shell=True, timeout=25)` -- the
script is executed directly via its shebang, from the artifact root, and only exit code 0
counts as a pass (a skip, 77, is not selected). The selected set on the STARTING translation
(`rust/`) is the acceptance set; the translation loop (line 712) re-runs it after each rewrite.

For each util we run that exact procedure on `rust/` (starting translation) and on
`rust_WIP/` (C2SaferRust's output) and report which acceptance tests the output fails.

  docker run --rm -v "$PWD:/work" -w /work crust-heldout python3 harness/acceptance_suite.py
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ROOT = os.environ.get("C2SR_ROOT", os.path.join(REPO, "artifacts", "c2saferrust"))
UTILS = ["cat", "head", "tail", "uniq", "split", "truncate", "pwd"]
RUNS = int(os.environ.get("RUNS", "3"))


def build(code_dir, util):
    p = subprocess.run(["cargo", "build", "--bin", util], cwd=os.path.join(ROOT, code_dir),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode == 0


def passes(test_rel, code_dir):
    cmd = f"{test_rel} {code_dir}/target/debug"
    try:
        r = subprocess.run(cmd, shell=True, cwd=ROOT, timeout=25,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def main():
    out = {"procedure": "translate.py select_tests, replicated: shebang exec from artifact root, "
                        "timeout 25 s, pass = exit 0", "runs_per_test": RUNS, "per_util": {}}
    tot_sel, tot_fail = 0, []
    for u in UTILS:
        tdir = os.path.join("coreutils", "tests", u)
        if not os.path.isdir(os.path.join(ROOT, tdir)):
            continue
        base, wip = f"coreutils/src/{u}/rust", f"coreutils/src/{u}/rust_WIP"
        if not (build(base, u) and build(wip, u)):
            out["per_util"][u] = {"error": "build failed"}; continue
        tests = sorted(t for t in os.listdir(os.path.join(ROOT, tdir)) if t.endswith(".sh"))
        rows = {}
        for t in tests:
            rel = os.path.join(tdir, t)
            b = [passes(rel, base) for _ in range(RUNS)]
            w = [passes(rel, wip) for _ in range(RUNS)]
            rows[t] = {"start_pass": sum(b), "wip_pass": sum(w)}
            print(u, t, "start", sum(b), "/", RUNS, "wip", sum(w), "/", RUNS, flush=True)
        selected = [t for t, r in rows.items() if r["start_pass"] == RUNS]
        fails = [t for t in selected if rows[t]["wip_pass"] < RUNS]
        out["per_util"][u] = {"sh_tests": len(tests), "selected_on_start": len(selected),
                              "wip_fails_selected": fails, "rows": rows}
        tot_sel += len(selected); tot_fail += [f"{u}/{t}" for t in fails]
    out["totals"] = {"acceptance_set": tot_sel, "wip_fails": tot_fail}
    json.dump(out, open(os.path.join(REPO, "results", "c2saferrust_acceptance_suite.json"), "w"), indent=1)
    print(json.dumps(out["totals"]))


if __name__ == "__main__":
    sys.exit(main())
