"""Control for the vacuity finding: run VERT's bounded-Kani harness exactly as check.py does, but with
Kani's unwinding assertions ON (VERT's command minus `--no-unwinding-checks`):
    cargo kani --default-unwind 10
on every scalar wrong answer (results/vert_diff.json) that VERT records as bounded_kani=1. If the vacuous SUCCESSFUL in
results/vert_kani_vacuity.json comes from the cut-off loop, this run reports FAILED with an
"unwinding assertion" as the failed check. Output: results/vert_kani_unwinding_on.json; full logs in
results/kani_logs_unwind_on/. Run from the repo root in the same container as check.py:
  docker run --rm -v "$PWD:/w" -w /w ubuntu:22.04 bash systems/vert/kani_vacuity/run_in_container.sh unwinding_on
"""
import json, os, re, shutil, subprocess, tempfile, time
REPO = os.getcwd()
BENCH = os.path.join(REPO, "artifacts/vert/benchmark/c_transcoder")
OUT = os.path.join(REPO, "results/vert_kani_unwinding_on.json")
vac = json.load(open(os.path.join(REPO, "results/vert_kani_vacuity.json")))
wrong = set(json.load(open(os.path.join(REPO, "results/vert_diff.json")))["summary"]["value_bug_functions"])
funcs = [r["function"] for r in vac["rows"] if r["function"] in wrong]   # scalar wrong answers VERT records as bounded_kani=1
LOGS = os.path.join(REPO, "results", "kani_logs_unwind_on"); os.makedirs(LOGS, exist_ok=True)
tgt = tempfile.mkdtemp(prefix="kani_target_")
CMD = ["cargo", "kani", "--default-unwind", "10"]
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
summ = {"kani_version": subprocess.run(["cargo", "kani", "--version"], capture_output=True, text=True).stdout.strip(),
        "command": " ".join(CMD) + "  (VERT's command without --no-unwinding-checks)",
        "functions": len(rows),
        "verdict_counts": {v: sum(r["verdict"] == v for r in rows) for v in sorted({r["verdict"] for r in rows})},
        "failed_on_unwinding_assertion": sum(r["unwinding_assertion_failed"] for r in rows)}
json.dump({"summary": summ, "rows": rows}, open(OUT, "w"), indent=2)
print(json.dumps(summ, indent=2))
