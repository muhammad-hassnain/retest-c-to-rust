#!/usr/bin/env python3
"""Re-run VERT's bounded-Kani check on chosen functions and record what Kani actually compared.

For each function it copies VERT's shipped harness (out-rwasm-mutated), gives the crate a short
unique name (Kani 0.55 fails with ENAMETOOLONG on VERT's long names; a shared name would reuse a
stale model), runs Kani, and records:
  verdict               SUCCESSFUL / FAILED / COMPILE_ERROR / TIMEOUT / NO_VERDICT
  equivalence           status of `assert!(result == result_prime)`: SUCCESS / FAILURE /
                        UNREACHABLE / UNDETERMINED / None (not in the report)
  failed_checks         description of every check with Status: FAILURE
  counterexample        for a FAILURE, the concrete inputs Kani reports (--concrete-playback=print)
  compared_on_failure   True iff the equivalence assertion itself FAILED, i.e. the check compared
                        the two sides on an input and saw them differ

Modes (both use VERT's unwind bound 10, evaluation.py:76):
  vert       cargo kani --no-unwinding-checks --default-unwind 10   (VERT's exact command)
  unwind-on  cargo kani --default-unwind 10                         (unwinding assertions back on)

Function sets:
  --funcs A,B,...        explicit list
  --ledger-vert          every VERT function with a counted defect in results/defect_ledger.json
  --recorded-passes      every function VERT records as bounded_kani=1 (c_results.csv)

Run from the repo root inside the pinned image with Kani 0.55.0 (docker/Dockerfile.kani):
  docker run --rm -v "$PWD:/w" -w /w crust-kani \
      python3 systems/vert/kani_verify.py --ledger-vert --mode vert --mode unwind-on --jobs 3 \
      --out results/vert_kani_verify.json
"""
import argparse, concurrent.futures as cf, datetime, json, os, platform, re, shutil, subprocess, tempfile, time

REPO = os.getcwd()
BENCH = os.path.join(REPO, "artifacts/vert/benchmark/c_transcoder")
MODES = {"vert": ["--no-unwinding-checks", "--default-unwind", "10"],
         "unwind-on": ["--default-unwind", "10"]}
EQ = "assertion failed: result == result_prime"


def records():
    out = {}
    for line in open(os.path.join(REPO, "artifacts/vert/c_results.csv")):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 5:
            out[parts[0]] = {k: v for k, v in (p.split("=") for p in parts[1:5])}
    return out


def ledger_functions():
    L = json.load(open(os.path.join(REPO, "results/defect_ledger.json")))
    fs = set()
    for r in L["rows"]:
        if r["system"] == "VERT" and r.get("count_status") == "counted":
            fs.add(r["translation"].split()[0])
    return sorted(fs)


def parse(log):
    checks = re.findall(r'Status: (\w+)\n\s*- Description: "([^"]*)"', log)
    eq = next((s for s, d in checks if d == EQ), None)
    failed = [d for s, d in checks if s == "FAILURE"]
    if "VERIFICATION:- FAILED" in log:
        verdict = "FAILED"
    elif "VERIFICATION:- SUCCESSFUL" in log:
        verdict = "SUCCESSFUL"
    elif re.search(r"^error(\[E\d+\])?:", log, re.M):
        verdict = "COMPILE_ERROR"
    else:
        verdict = "NO_VERDICT"
    return verdict, eq, failed


def playback_values(log):
    """Kani's concrete playback prints, per nondet value, a comment with the decoded value followed by
    its bytes: `// 1` / `vec![1, 0, 0, 0],`. Return the decoded comments in order."""
    vals = re.findall(r"//\s*(-?[0-9][0-9_.eE+-]*|true|false)\s*\n\s*vec!\[", log)
    return vals or None


# Opt-in KANI_CARGO_PRECISE="crate@version[,...]": VERT's harnesses ship no Cargo.lock, so cargo resolves the
# newest semver-compatible dependencies; pinning a crate back (cargo update --precise, run before Kani) keeps
# the resolution buildable by Kani's toolchain (e.g. unicode-segmentation 1.13.3 needs rustc 1.85; Kani 0.55 is 1.83).
PRECISE = [x for x in os.environ.get("KANI_CARGO_PRECISE", "").split(",") if x]


def kani(hdir, args, tgt, timeout):
    t0 = time.time()
    try:
        p = subprocess.run(["cargo", "kani", *args], cwd=os.path.join(hdir, "src"), capture_output=True,
                           text=True, timeout=timeout, env={**os.environ, "CARGO_TARGET_DIR": tgt})
        return p.stdout + p.stderr, False, round(time.time() - t0, 1)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        return out + err, True, round(time.time() - t0, 1)


def run_one(job):
    idx, func, mode, playback, timeout, logdir, tgt = job
    work = tempfile.mkdtemp(prefix="kv_")
    try:
        h = os.path.join(work, "h")
        shutil.copytree(os.path.join(BENCH, func, "out-rwasm-mutated"), h)
        ct = os.path.join(h, "Cargo.toml")
        txt = open(ct).read()
        txt = re.sub(r'(\[package\][^\[]*?\bname\s*=\s*)"[^"]*"', r'\1"kv%d"' % idx, txt, count=1, flags=re.S)
        open(ct, "w").write(txt)
        pre = ""
        for spec in PRECISE:
            name, ver = spec.split("@")
            q = subprocess.run(["cargo", "update", "-p", name, "--precise", ver], cwd=h, capture_output=True, text=True)
            pre += f"$ cargo update -p {name} --precise {ver}  (rc {q.returncode})\n" + q.stdout + q.stderr
        log, to, secs = kani(h, MODES[mode], tgt, timeout)
        log = pre + log
        verdict, eq, failed = ("TIMEOUT", None, []) if to else parse(log)
        row = {"function": func, "mode": mode, "command": "cargo kani " + " ".join(MODES[mode]),
               "verdict": verdict, "equivalence": eq, "failed_checks": failed,
               "compared_on_failure": eq == "FAILURE", "seconds": secs}
        if PRECISE:
            row["cargo_precise"] = PRECISE
        lp = os.path.join(logdir, f"{func}.{mode}.log")
        open(lp, "w").write(log)
        row["log"] = os.path.relpath(lp, REPO)
        if playback and verdict == "FAILED":
            plog, pto, _ = kani(h, MODES[mode] + ["-Z", "concrete-playback", "--concrete-playback=print"], tgt, timeout)
            open(lp.replace(".log", ".playback.log"), "w").write(plog)
            row["counterexample"] = None if pto else playback_values(plog)
        return row
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--funcs", default="")
    ap.add_argument("--ledger-vert", action="store_true")
    ap.add_argument("--recorded-passes", action="store_true")
    ap.add_argument("--mode", action="append", choices=list(MODES))
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--no-playback", action="store_true")
    ap.add_argument("--out", default="results/vert_kani_verify.json")
    a = ap.parse_args()
    rec = records()
    funcs = [f for f in a.funcs.split(",") if f]
    if a.ledger_vert:
        funcs += ledger_functions()
    if a.recorded_passes:
        funcs += [f for f, r in rec.items() if r.get("bounded_kani") == "1"]
    funcs = sorted(set(funcs))
    modes = a.mode or ["vert"]
    ver = subprocess.run(["cargo", "kani", "--version"], capture_output=True, text=True).stdout.strip()
    logdir = os.path.join(REPO, "results", "kani_logs_verify"); os.makedirs(logdir, exist_ok=True)
    tgts = [tempfile.mkdtemp(prefix=f"kv_target{w}_") for w in range(a.jobs)]  # one target dir per worker
    jobs = [(i, f, m, not a.no_playback, a.timeout, logdir, tgts[i % a.jobs])
            for i, (f, m) in enumerate((f, m) for m in modes for f in funcs)]
    rows = []
    with cf.ThreadPoolExecutor(a.jobs) as ex:
        for row in ex.map(run_one, jobs):
            row["vert_record"] = rec.get(row["function"])
            rows.append(row)
            print(row["mode"], row["function"], row["verdict"], row["equivalence"], row.get("counterexample"), flush=True)
    summary = {}
    for m in modes:
        rs = [r for r in rows if r["mode"] == m]
        summary[m] = {"functions": len(rs),
                      "verdict": {v: sum(r["verdict"] == v for r in rs) for v in sorted({r["verdict"] for r in rs})},
                      "equivalence": {str(e): sum(r["equivalence"] == e for r in rs) for e in sorted({str(r["equivalence"]) for r in rs})},
                      "compared_and_failed": sorted(r["function"] for r in rs if r["compared_on_failure"])}
    try:
        rev = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=REPO).stdout.strip() or None
    except OSError:
        rev = os.environ.get("GIT_REV")
    out = {"provenance": {"script": "systems/vert/kani_verify.py", "command": " ".join(os.sys.argv),
                          "kani": ver, "host": platform.system(), "arch": platform.machine(),
                          "date": datetime.datetime.now().isoformat(timespec="seconds"), "git_rev": rev},
           "summary": summary, "rows": sorted(rows, key=lambda r: (r["mode"], r["function"]))}
    json.dump(out, open(os.path.join(REPO, a.out), "w"), indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
