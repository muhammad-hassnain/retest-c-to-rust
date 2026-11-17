"""Follow-up: VERT's exact bounded-Kani command on counted VERT defects whose c_results.csv
row has bounded_kani=0 (so nothing shows whether Kani ever reached the comparison).

Same procedure as systems/vert/kani_vacuity/check.py: copy the shipped out-rwasm-mutated harness,
give the crate a unique short name (Kani 0.55 ICEs on VERT's long crate names), run
`cargo kani --no-unwinding-checks --default-unwind 10` (VERT evaluation.py:76) in src/, parse the
verdict and the status of the equivalence assertion (`assert_eq!(result, result_prime)` ->
"assertion failed: result == result_prime"). Added: every check with Status FAILURE (description +
location), and, for a FAILED verdict, a second run of the same command plus
`-Z concrete-playback --concrete-playback=print` to obtain Kani's concrete trace (the values its
kani::any() calls returned), recorded verbatim.

  python3 systems/vert/toolchain_kani_remaining.py OUT.json LOGDIR WORKER_COUNT FUNC [FUNC ...]
Run inside crust-kani (docker/Dockerfile.kani: Kani 0.55.0).
"""
import datetime, json, multiprocessing as mp, os, platform, re, shutil, subprocess, sys, tempfile, time

REPO = os.getcwd()
BENCH = os.path.join(REPO, "artifacts/vert/benchmark/c_transcoder")
CMD = ["cargo", "kani", "--no-unwinding-checks", "--default-unwind", "10"]
EQ = "assertion failed: result == result_prime"
TIMEOUT = int(os.environ.get("KANI_TIMEOUT", "900"))   # check.py's 900 s; VERT's own evaluator uses 180 s (evaluation.py:13)
VERT_TIMEOUT = 180


def checks(log):
    out = []
    for m in re.finditer(r"Check \d+: (\S+)\n\s*- Status: (\w+)\n\s*- Description: \"([^\"]*)\"\n\s*- Location: ([^\n]*)", log):
        out.append({"name": m.group(1), "status": m.group(2), "description": m.group(3), "location": m.group(4)})
    return out


def run_kani(f, idx, logdir, extra=()):
    work = tempfile.mkdtemp(prefix="kani_")
    shutil.copytree(os.path.join(BENCH, f, "out-rwasm-mutated"), os.path.join(work, "h"), ignore=shutil.ignore_patterns("target"))
    ct = os.path.join(work, "h", "Cargo.toml")
    txt = open(ct).read()
    txt = re.sub(r'(\[package\][^\[]*?\bname\s*=\s*)"[^"]*"', r'\1"kr%d"' % idx, txt, count=1, flags=re.S)
    open(ct, "w").write(txt)
    tgt = os.path.join(tempfile.gettempdir(), f"kani_tgt_{os.getpid()}")
    t0 = time.time()
    try:
        p = subprocess.run(CMD + list(extra), cwd=os.path.join(work, "h", "src"), capture_output=True, text=True,
                           timeout=TIMEOUT, env={**os.environ, "CARGO_TARGET_DIR": tgt})
        log, to = p.stdout + p.stderr, False
    except subprocess.TimeoutExpired:
        log, to = "", True
    tag = "playback" if extra else "vert_cmd"
    open(os.path.join(logdir, f"{f}.{tag}.log"), "w").write(log)
    shutil.rmtree(work, ignore_errors=True)
    return log, to, round(time.time() - t0, 1)


def one(args):
    f, idx, logdir = args
    log, to, secs = run_kani(f, idx, logdir)
    oom = "CBMC failed with status 137" in log or "CBMC appears to have run out of memory" in log
    verdict = ("TIMEOUT" if to else "OUT_OF_MEMORY" if oom else "FAILED" if "VERIFICATION:- FAILED" in log
               else "SUCCESSFUL" if "VERIFICATION:- SUCCESSFUL" in log
               else "COMPILE_ERROR" if re.search(r"^error(\[E\d+\])?:", log, re.M) else "NO_VERDICT")
    cs = checks(log)
    eq = [c for c in cs if c["description"] == EQ]
    summ = re.search(r"SUMMARY:\s*\n\s*\*\* (\d+) of (\d+) failed(?: \((\d+) unreachable\))?", log)
    row = {"function": f, "verdict": verdict, "seconds": secs,
           "equivalence_assertion": eq[0]["status"] if eq else None,
           "equivalence_assertion_location": eq[0]["location"] if eq else None,
           "equivalence_assertion_reachable": (None if not eq else eq[0]["status"] != "UNREACHABLE"),
           "summary_line": summ.group(0).split("** ")[-1] if summ else None,
           "failed_checks": [c for c in cs if c["status"] == "FAILURE"],
           "n_checks": len(cs),
           "compile_errors": re.findall(r"^error(?:\[E\d+\])?: [^\n]*", log, re.M)[:4],
           "vert_evaluator_would_print": ("Kani timeout" if (to or secs > VERT_TIMEOUT) else
                                          "Kani failed" if "VERIFICATION:- FAILED" in log else "Kani succesful"),
           "exceeds_vert_180s_timeout": bool(to or secs > VERT_TIMEOUT),
           "cbmc_out_of_memory": oom,
           "log": f"{f}.vert_cmd.log"}
    if verdict == "FAILED":
        plog, pto, psecs = run_kani(f, idx + 100, logdir, extra=("-Z", "concrete-playback", "--concrete-playback=print"))
        m = re.findall(r"(#\[test\]\s*fn kani_concrete_playback[^\n]*\n(?:.*\n){0,40}?\s*\}\n)", plog)
        vecs = re.findall(r"//\s*(-?[\w.]+)\n\s*vec!\[([^\]]*)\]", plog)
        row["concrete_playback"] = {"seconds": psecs, "timeout": pto,
                                    "same_verdict": "VERIFICATION:- FAILED" in plog,
                                    "tests": m[:2], "values_commented": vecs[:12],
                                    "log": f"{f}.playback.log"}
    print(f, verdict, row["equivalence_assertion"], [c["description"] for c in row["failed_checks"]][:3], flush=True)
    return row


def main():
    out, logdir, workers, funcs = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4:]
    os.makedirs(logdir, exist_ok=True)
    t0 = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with mp.Pool(workers) as pool:
        rows = pool.map(one, [(f, i, logdir) for i, f in enumerate(funcs)], chunksize=1)
    csv = {}
    for line in open(os.path.join(REPO, "artifacts/vert/c_results.csv")):
        p = [x.strip() for x in line.split(",")]
        if p and p[0]:
            csv[p[0]] = p[1:]
    for r in rows:
        r["vert_c_results_csv"] = csv.get(r["function"])
    sh = lambda c: subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()  # noqa: E731
    res = {"provenance": {"script": "systems/vert/toolchain_kani_remaining.py", "command": " ".join(sys.argv),
                          "kani": sh("cargo kani --version"), "vert_command": " ".join(CMD) + " (evaluation.py:76)",
                          "arch": platform.machine(), "image": os.environ.get("IMAGE_DESC"),
                          "git_rev": os.environ.get("GIT_REV"), "host": os.environ.get("HOST_DESC"),
                          "date_start_utc": t0, "date_end_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()},
           "rows": rows}
    json.dump(res, open(out, "w"), indent=2)


if __name__ == "__main__":
    main()
