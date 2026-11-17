"""check.py re-run on the CURRENT VERT scalar wrong-answer set (results/vert_diff.json "value_bugs").

Identical procedure to systems/vert/bolero_vacuity/check.py (same three arms, same commands, same env,
same 600 s timeout, same `plant`, same VERT verdict rule). Differences, all bookkeeping only:
  * the full output of every arm is written to $BOLERO_LOGDIR/<FUNC>.<arm>.log (check.py kept a
    1500-char tail only), so "aborted before executing any input" can be read from the whole log;
  * each arm gets an `outcome` label (the labels results/vert_bolero_vacuity.json uses) plus the
    evidence it rests on (test binary started? libFuzzer MERGE-INNER file count? os-error-2 path?);
  * each row records `shipped_corpus_files` (files in the shipped src/__fuzz__/bolero_wasm_eq/corpus);
  * `--merge` combines the per-worker part files into one JSON with summary + provenance
    (run_in_container_current.sh runs 3 workers in parallel, one part file each).

  python3 check_current.py <benchmark_dir> <part.json> FUNC [FUNC ...]
  python3 check_current.py --merge <benchmark_dir> <out.json> <provenance.json> <part.json> [...]
"""
import json, os, re, shutil, subprocess, sys, tempfile, time

LOGDIR = os.environ.get("BOLERO_LOGDIR")
ARMS = ("vert_reduce", "planted_reduce", "fuzz_30s")


def outcome(rc, to, out):
    # same precedence as check.py's vert_verdict, then a finer label for the 'pass' runs
    if to:
        return "timeout"
    if "could not compile" in out:
        return "compile_error"
    if "Test Failure" in out:
        return "test_failure"
    if rc == 0:
        return "clean_exit"
    if re.search(r"No such file or directory \(os error 2\) at path \"[^\"]*/src/__fuzz__/bolero_wasm_eq/", out):
        return "aborted_empty_corpus"
    return f"exit_{rc}_no_test_failure_text"


def run(cmd, cwd, env_extra, timeout, logname):
    env = {**os.environ, **env_extra}
    t = time.time()
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        out, rc, to = p.stdout + p.stderr, p.returncode, False
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")) + \
              ((e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or ""))
        rc, to = None, True
    if LOGDIR:
        open(os.path.join(LOGDIR, logname), "w").write(out)
    m = re.findall(r"MERGE-INNER: (\d+) total files", out)
    sig = re.findall(r"\(signal: (\d+), ([A-Z]+)", out)
    return {"cmd": cmd, "rc": rc, "timeout": to, "secs": round(time.time() - t, 1),
            "test_failure": "Test Failure" in out, "could_not_compile": "could not compile" in out,
            "vert_verdict": "timeout" if to else ("compile_error" if "could not compile" in out else
                                                  ("fail" if "Test Failure" in out else "pass")),
            "outcome": outcome(rc, to, out),
            # evidence for "executed any input": libFuzzer prints INFO: Seed when the test binary starts
            # fuzzing/replaying; MERGE-INNER counts the corpus files a reduce replays
            "evidence": {"libfuzzer_started": "INFO: Seed:" in out,
                         "running_1_test": "running 1 test" in out,
                         "merge_inner_total_files": [int(x) for x in m],
                         "signal": [f"{a} {b}" for a, b in sig],
                         "file_name_too_long": "File name too long" in out},
            "log": logname if LOGDIR else None,
            "tail": out[-1500:]}


def plant(src):
    # replace the body of the LLM's f_gold (between the LLM Output markers) with `return 12345`
    a = src.index("////// LLM Output //////"); b = src.index("////// LLM Output //////", a + 10)
    llm = src[a:b]
    m = re.search(r"fn f_gold\s*\(([^)]*)\)\s*->\s*([^{]+)\{", llm)
    start = m.end(); depth = 1; i = start
    while depth:
        depth += {"{": 1, "}": -1}.get(llm[i], 0); i += 1
    ret = m.group(2).strip()
    body = "12345" if ret.startswith(("i", "u")) else ("12345.0" if ret.startswith("f") else None)
    if body is None:
        return None
    new = llm[:start] + f"\n    return {body} as {ret};\n" + llm[i - 1:]
    return src[:a] + new + src[b:]


def corpus_files(proj):
    c = os.path.join(proj, "src", "__fuzz__", "bolero_wasm_eq", "corpus")
    return len(os.listdir(c)) if os.path.isdir(c) else 0


def main():
    bench, out_path, funcs = sys.argv[1], sys.argv[2], sys.argv[3:]
    tgt = tempfile.mkdtemp(prefix="bolero_tgt_")
    rows = []
    for f in funcs:
        proj = os.path.join(bench, f, "out-rwasm-bolero")
        row = {"function": f, "arms": {}, "shipped_corpus_files": corpus_files(proj)}
        for arm in ARMS:
            work = tempfile.mkdtemp(prefix="bolero_")
            p = os.path.join(work, "p")
            shutil.copytree(proj, p, ignore=shutil.ignore_patterns("target"))
            shutil.copy(os.path.join(bench, "..", "rust-toolchain.toml"), p)
            if arm != "vert_reduce":
                src = open(os.path.join(p, "src", "main.rs")).read()
                s2 = plant(src)
                if s2 is None:
                    row["arms"][arm] = {"skipped": "non-numeric return"}; shutil.rmtree(work); continue
                open(os.path.join(p, "src", "main.rs"), "w").write(s2)
            env = {"RUSTFLAGS": "-C overflow-checks=false", "CARGO_TARGET_DIR": os.path.join(tgt, f)}
            if arm == "fuzz_30s":
                r = run("cargo bolero test bolero_wasm_eq -T 30s", p, env, 600, f"{f}.{arm}.log")
            else:
                os.makedirs(os.path.join(p, "src", "__fuzz__"), exist_ok=True)
                r = run("cargo bolero reduce bolero_wasm_eq", p, env, 600, f"{f}.{arm}.log")
            row["arms"][arm] = r
            print(f, arm, r["vert_verdict"], r["outcome"], r["secs"], flush=True)
            shutil.rmtree(work, ignore_errors=True)
        rows.append(row)
        json.dump({"partial": True, "rows": rows}, open(out_path, "w"), indent=1)
    json.dump({"partial": False, "rows": rows}, open(out_path, "w"), indent=1)


def merge():
    bench, out_path, prov_path, parts = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5:]
    rows = []
    for p in parts:
        d = json.load(open(p))
        assert d["partial"] is False, p
        rows += d["rows"]
    rows.sort(key=lambda r: r["function"])
    # GRID=v2 (opt-in): the current set is the grid-v2 run's wrong answers (results/vert_diff_grid2.json
    # value_bugs) minus those the full UB rule excludes (results/vert_grid2_ub_recheck.json)
    grid = os.environ.get("GRID", "v1")
    if grid == "v2":
        vd = json.load(open(os.path.join(os.getcwd(), "results", "vert_diff_grid2.json")))
        ubx = set(json.load(open(os.path.join(os.getcwd(), "results", "vert_grid2_ub_recheck.json")))["summary"]["without_clean_input_where_clang_also_agrees"])
        cur = [v["func"] for v in vd["value_bugs"] if v["func"] not in ubx]
    else:
        vd = json.load(open(os.path.join(os.getcwd(), "results", "vert_diff.json")))   # run from the repo root
        cur = [v["func"] for v in vd["value_bugs"]]
    fs = [r["function"] for r in rows]
    assert sorted(fs) == sorted(cur) and len(fs) == len(set(fs)) == len(cur), f"row set != current {len(cur)}"

    def cnt(arm, v): return sum(1 for r in rows if r["arms"].get(arm, {}).get("vert_verdict") == v)
    summ = {arm: {v: cnt(arm, v) for v in ("pass", "fail", "compile_error", "timeout")} for arm in ARMS}
    summ["functions"] = len(rows)
    summ["procedure"] = "VERT ARTIFACT_README 'How to Run Validation Directly' (PBT): mkdir -p src/__fuzz__; RUSTFLAGS='-C overflow-checks=false' cargo bolero reduce bolero_wasm_eq; pass iff no 'Test Failure'"
    oba = {}
    for arm in ARMS:
        c = {}
        for r in rows:
            o = r["arms"][arm].get("outcome", "skipped")
            c[o] = c.get(o, 0) + 1
        oba[arm] = c
    summ["outcomes_by_arm"] = oba
    # "aborted before executing any input": the os-error-2 abort AND the test binary never started
    summ["vert_reduce_aborted_before_any_input"] = sum(
        r["arms"]["vert_reduce"]["outcome"] == "aborted_empty_corpus"
        and not r["arms"]["vert_reduce"]["evidence"]["libfuzzer_started"]
        and not r["arms"]["vert_reduce"]["evidence"]["merge_inner_total_files"] for r in rows)
    summ["vert_reduce_no_input_evidence_any_outcome"] = sum(
        not r["arms"]["vert_reduce"]["evidence"]["libfuzzer_started"]
        and not r["arms"]["vert_reduce"]["evidence"]["merge_inner_total_files"] for r in rows)
    summ["functions_with_shipped_corpus"] = sum(r["shipped_corpus_files"] > 0 for r in rows)
    B = bench
    names = sorted(x for x in os.listdir(B) if os.path.isdir(os.path.join(B, x)))
    summ["benchmarks"] = len(names)
    summ["benchmarks_shipping_a_corpus"] = sum(corpus_files(os.path.join(B, x, "out-rwasm-bolero")) > 0 for x in names)
    csvr = {}
    for line in open(os.path.join(B, "..", "..", "c_results.csv")):
        p = [x.strip() for x in line.split(",")]
        if p and p[0]:
            csvr[p[0]] = p
    summ["vert_pbt_pass_total"] = sum("bolero=1" in v for v in csvr.values())
    summ["vert_pbt_pass_without_shipped_corpus"] = sum(
        "bolero=1" in v and corpus_files(os.path.join(B, k, "out-rwasm-bolero")) == 0 for k, v in csvr.items())
    summ["current_set_recorded_bolero_1_by_vert"] = sum("bolero=1" in csvr.get(f, []) for f in cur)
    summ["functions_note"] = ("exactly the 25 VERT scalar wrong answers in results/vert_diff.json value_bugs (current spread grid)" if grid != "v2" else
                              f"exactly the {len(cur)} counted VERT scalar wrong answers on grid v2 (results/vert_diff_grid2.json value_bugs minus "
                              f"{sorted(ubx)}, excluded by the full UB rule in results/vert_grid2_ub_recheck.json)")
    summ["grid"] = grid
    summ["vs_previous_run"] = ("results/vert_bolero_vacuity.json ran 26 functions of an older grid (23 of these 25 + 3 no longer wrong); this file supersedes it for the current set"
                               if grid != "v2" else
                               "the 25 grid-v1 rows are the same part files as before (unchanged); 3 rows added for the defects grid v2 finds (provenance.grid_v2_additions). "
                               "results/vert_bolero_vacuity.json ran 26 functions of an older grid; this file supersedes it for the current set")
    prov = json.load(open(prov_path))
    res = {"provenance": prov, "summary": summ, "rows": rows}
    diag = os.environ.get("RENAME_DIAG")   # optional: rename_diag_current.sh output (not counted above)
    if diag and os.path.exists(diag):
        d = json.load(open(diag))
        res["diagnostic_crate_rename"] = {
            "note": ("NOT counted in the summary. The one compile_error is rustc failing to write an object file whose name repeats the 107-character crate name (\"File name too long\", >255 bytes). Re-run of the same three arms on a copy whose Cargo.toml [package] name is shortened to 'nwh' (nothing else changed); script systems/vert/bolero_vacuity/rename_diag_current.sh, logs results/vert_bolero_logs_current/rename_diag/"
                     if grid != "v2" else
                     "NOT counted in the summary. Each compile_error is rustc failing to write an object file whose name repeats the crate name (\"File name too long\", >255 bytes). Re-run of the same three arms on a copy whose Cargo.toml [package] name is shortened to 'nwh' (nothing else changed); script systems/vert/bolero_vacuity/rename_diag_current.sh (RENAME_FUNC selects the function), logs results/vert_bolero_logs_current/rename_diag/"),
            "rows": d["rows"]}
    json.dump(res, open(out_path, "w"), indent=1)
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    merge() if sys.argv[1] == "--merge" else main()
