"""arXiv (issues.md 47, wide-int VERT functions): VERT's bounded-Kani command with unwinding checks ON
for the defective wide-int functions VERT records as bounded_kani=1.

Same procedure as systems/vert/kani_vacuity/unwinding_on.py (which wrote results/vert_kani_unwinding_on.json
for the int-return wrong answers): copy the function's shipped out-rwasm-mutated harness, rename the crate
to a short unique name (u<i>; Kani 0.55 ICEs on VERT's long names), and run in its src/
    cargo kani --default-unwind 10          (VERT's command, evaluation.py:76, minus --no-unwinding-checks)
with the same timeout (900 s), verdict parsing, failed-check parsing and equivalence-assertion parsing.
The only differences: the function list is the wide-int value-bug set of results/arxiv_vert_diff_grid2_wide.json
intersected with the bounded_kani=1 rows of results/vert_kani_vacuity.json (unwinding_on.py intersected
with results/vert_diff.json), and the output paths and provenance block.

Run from the repo root in the crust-kani image (Kani 0.55.0 preinstalled, no network needed), e.g.
  docker run --rm --cpus 4 -v "$PWD:/w" -w /w -e IMAGE_DESC="crust-kani <id>" -e HOST_DESC="<OS name>" \
      crust-kani python3 -I systems/vert/arxiv_wide_kani_unwinding_on.py
Writes results/arxiv_vert_wide_kani_unwinding_on.json; full logs in results/arxiv_vert_wide_kani_logs_unwind_on/.
"""
import datetime
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time

REPO = os.getcwd()
BENCH = os.path.join(REPO, "artifacts/vert/benchmark/c_transcoder")
OUT = os.path.join(REPO, os.environ.get("OUT", "results/arxiv_vert_wide_kani_unwinding_on.json"))
LOGS_REL = os.environ.get("LOGS", "results/arxiv_vert_wide_kani_logs_unwind_on")
LOGS = os.path.join(REPO, LOGS_REL)
vac = json.load(open(os.path.join(REPO, "results/vert_kani_vacuity.json")))
wide = json.load(open(os.path.join(REPO, "results/arxiv_vert_diff_grid2_wide.json")))
wrong = set(wide["summary"]["value_bug_functions"])
funcs = [r["function"] for r in vac["rows"] if r["function"] in wrong]   # wide wrong answers VERT records as bounded_kani=1
os.makedirs(LOGS, exist_ok=True)
tgt = tempfile.mkdtemp(prefix="kani_target_")
CMD = ["cargo", "kani", "--default-unwind", "10"]


def ver(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
    except OSError as e:
        return f"unavailable: {e}"


t_start = datetime.datetime.now(datetime.timezone.utc)
rows = []
for i, f in enumerate(funcs):
    work = tempfile.mkdtemp(prefix="kani_")
    shutil.copytree(os.path.join(BENCH, f, "out-rwasm-mutated"), os.path.join(work, "h"))
    ct = os.path.join(work, "h", "Cargo.toml")
    txt = open(ct).read()
    txt = re.sub(r'(\[package\][^\[]*?\bname\s*=\s*)"[^"]*"', r'\1"u%d"' % i, txt, count=1, flags=re.S)
    open(ct, "w").write(txt)
    t0 = time.time()
    try:
        p = subprocess.run(CMD, cwd=os.path.join(work, "h", "src"), capture_output=True, text=True,
                           timeout=900, env={**os.environ, "CARGO_TARGET_DIR": tgt})
        log, timeout = p.stdout + p.stderr, False
    except subprocess.TimeoutExpired:
        log, timeout = "", True
    verdict = ("TIMEOUT" if timeout else "FAILED" if "VERIFICATION:- FAILED" in log
               else "SUCCESSFUL" if "VERIFICATION:- SUCCESSFUL" in log else "NO_VERDICT")
    failed = re.findall(r"Failed Checks: (.+)", log)
    m = re.search(r'Status: (\w+)\n\s*- Description: "assertion failed: result == result_prime"', log)
    open(os.path.join(LOGS, f + ".log"), "w").write(log)
    rows.append({"function": f, "verdict": verdict, "failed_checks": failed,
                 "unwinding_assertion_failed": any("unwinding assertion" in x for x in failed),
                 "equivalence_assertion": m.group(1) if m else None,
                 "seconds": round(time.time() - t0, 1)})
    print(i + 1, len(funcs), f, verdict, failed[:2], flush=True)
    shutil.rmtree(work, ignore_errors=True)
t_end = datetime.datetime.now(datetime.timezone.utc)
summ = {"kani_version": subprocess.run(["cargo", "kani", "--version"], capture_output=True, text=True).stdout.strip(),
        "command": " ".join(CMD) + "  (VERT's command without --no-unwinding-checks)",
        "functions": len(rows),
        "verdict_counts": {v: sum(r["verdict"] == v for r in rows) for v in sorted({r["verdict"] for r in rows})},
        "failed_on_unwinding_assertion": sum(r["unwinding_assertion_failed"] for r in rows)}
prov = {
    "script": "systems/vert/arxiv_wide_kani_unwinding_on.py (procedure of systems/vert/kani_vacuity/unwinding_on.py)",
    "command": os.environ.get("RUN_COMMAND", "python3 -I systems/vert/arxiv_wide_kani_unwinding_on.py"),
    "function_set": "results/arxiv_vert_diff_grid2_wide.json summary.value_bug_functions intersected with the "
                    "bounded_kani=1 rows of results/vert_kani_vacuity.json",
    "image": os.environ.get("IMAGE_DESC"),
    "host": os.environ.get("HOST_DESC"),
    "arch": platform.machine(),
    "git_rev": os.environ.get("GIT_REV"),
    "toolchains": {"cargo_kani": summ["kani_version"], "kani_rustc": ver(["bash", "-c", "ls -d ${RUSTUP_HOME:-$HOME/.rustup}/toolchains/* 2>/dev/null | xargs -n1 basename"]),
                   "rustc_default": ver(["rustc", "--version"]), "python": platform.python_version()},
    "date_start_utc": t_start.isoformat(timespec="seconds"), "date_end_utc": t_end.isoformat(timespec="seconds"),
    "logs": LOGS_REL + "/<F>.log",
}
json.dump({"provenance": prov, "summary": summ, "rows": rows}, open(OUT, "w"), indent=2)
print(json.dumps(summ, indent=2))
