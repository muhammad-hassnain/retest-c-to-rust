"""For each VERT function with bounded_kani=1 in artifacts/vert/c_results.csv, run VERT's bounded-Kani
command (`cargo kani --no-unwinding-checks --default-unwind 10`, evaluation.py:76) on a copy of its
shipped out-rwasm-mutated harness and parse (a) the overall verdict and (b) the status of the
equivalence assertion `result == result_prime`. A SUCCESSFUL verdict with that assertion UNREACHABLE
is a vacuous pass: the check never compared the two sides on any input."""
import json, os, re, shutil, subprocess, tempfile, time
REPO = os.getcwd()
BENCH = os.path.join(REPO, "artifacts/vert/benchmark/c_transcoder")
OUT = os.path.join(REPO, "results/vert_kani_vacuity.json")
funcs = []
for line in open(os.path.join(REPO, "artifacts/vert/c_results.csv")):
    parts = [p.strip() for p in line.split(",")]
    if len(parts) >= 4 and parts[3] == "bounded_kani=1":
        funcs.append(parts[0])
vb = set(json.load(open(os.path.join(REPO, "results/vert_diff.json")))["summary"]["value_bug_functions"])
funcs.sort(key=lambda f: f not in vb)  # the wrong-answer functions first
# ONLY=<comma list> re-runs a subset and merges it into the existing result file (audit 4: the
# NO_VERDICT runs were Kani ICEs "File name too long" caused by the 60-100 char crate name).
ONLY = [x for x in os.environ.get("ONLY", "").split(",") if x]
if ONLY:
    funcs = [f for f in funcs if f in ONLY]
LOGS = os.path.join(REPO, "results", "kani_logs"); os.makedirs(LOGS, exist_ok=True)
tgt = tempfile.mkdtemp(prefix="kani_target_")  # shared target dir caches the heavy wasm deps
rows = []
for i, f in enumerate(funcs):
    work = tempfile.mkdtemp(prefix="kani_")
    shutil.copytree(os.path.join(BENCH, f, "out-rwasm-mutated"), os.path.join(work, "h"))
    # Rename the crate to a short name: Kani 0.55 writes artifacts named after the crate and
    # ICEs with ENAMETOOLONG on VERT's long benchmark names. The name has no semantic effect, but
    # it MUST be unique per function: with a shared CARGO_TARGET_DIR one fixed name made every
    # run reuse the first function's model (audit 5).
    ct = os.path.join(work, "h", "Cargo.toml")
    txt = open(ct).read()
    txt = re.sub(r'(\[package\][^\[]*?\bname\s*=\s*)"[^"]*"', r'\1"k%d"' % i, txt, count=1, flags=re.S)
    open(ct, "w").write(txt)
    t0 = time.time()
    try:
        p = subprocess.run(["cargo", "kani", "--no-unwinding-checks", "--default-unwind", "10"],
                           cwd=os.path.join(work, "h", "src"), capture_output=True, text=True,
                           timeout=900, env={**os.environ, "CARGO_TARGET_DIR": tgt})
        log = p.stdout + p.stderr
        timeout = False
    except subprocess.TimeoutExpired:
        log, timeout = "", True
    verdict = ("TIMEOUT" if timeout else "FAILED" if "VERIFICATION:- FAILED" in log
               else "SUCCESSFUL" if "VERIFICATION:- SUCCESSFUL" in log
               else "COMPILE_ERROR" if re.search(r"^error(\[E\d+\])?:", log, re.M) else "NO_VERDICT")
    # status of the equivalence assertion: Kani prints "Status: X" then "Description: ..."
    m = re.search(r'Status: (\w+)\n\s*- Description: "assertion failed: result == result_prime"', log)
    assert_status = m.group(1) if m else None
    vert_would_say = "succesful" if "VERIFICATION:- FAILED" not in log and not timeout else "failed/timeout"
    open(os.path.join(LOGS, f + ".log"), "w").write(log)  # full log
    rows.append({"crate_renamed": True, "function": f, "in_scalar_wrong_answers": f in vb, "verdict": verdict,
                 "equivalence_assertion": assert_status, "vert_evaluator_would_print": vert_would_say,
                 "seconds": round(time.time() - t0, 1)})
    print(i + 1, len(funcs), f, verdict, assert_status, flush=True)
    shutil.rmtree(work, ignore_errors=True)
    if not ONLY:
        json.dump({"partial": True, "rows": rows}, open(OUT, "w"), indent=2)
if ONLY:
    old = json.load(open(OUT))
    new = {r["function"]: r for r in rows}
    rows = [new.get(r["function"], r) for r in old["rows"]]
summ = {
    "kani_version": subprocess.run(["cargo", "kani", "--version"], capture_output=True, text=True).stdout.strip(),
    "command": "cargo kani --no-unwinding-checks --default-unwind 10 (VERT evaluation.py:76)",
    "functions_bounded_kani_1": len(rows),
    "verdict_counts": {}, "assertion_status_counts": {},
    "vacuous_success": sum(r["verdict"] == "SUCCESSFUL" and r["equivalence_assertion"] == "UNREACHABLE" for r in rows),
    "non_vacuous_success": sum(r["verdict"] == "SUCCESSFUL" and r["equivalence_assertion"] == "SUCCESS" for r in rows),
    "of_the_scalar_wrong_answers": {"bounded_kani_1": sum(r["in_scalar_wrong_answers"] for r in rows),
                        "vacuous_success": sum(r["in_scalar_wrong_answers"] and r["verdict"] == "SUCCESSFUL" and r["equivalence_assertion"] == "UNREACHABLE" for r in rows)},
}
for r in rows:
    summ["verdict_counts"][r["verdict"]] = summ["verdict_counts"].get(r["verdict"], 0) + 1
    k = str(r["equivalence_assertion"])
    summ["assertion_status_counts"][k] = summ["assertion_status_counts"].get(k, 0) + 1
json.dump({"summary": summ, "rows": rows}, open(OUT, "w"), indent=2)
print(json.dumps(summ, indent=2))
