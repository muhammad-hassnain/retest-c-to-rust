"""30 s of real fuzzing with VERT's own bolero harness on VERT's REAL shipped translations
(arXiv re-run).

check_current.py's `fuzz_30s` arm ran `plant()` first (f_gold body -> `return 12345`), so it measured
whether 30 s of fuzzing rejects a planted constant. This script runs the same command on the
UNMODIFIED project:

  * VERT's out-rwasm-bolero project is copied (target/ left out) and benchmark/rust-toolchain.toml
    (nightly-2023-07-29) is put next to it, exactly as check_current.py does. main.rs, Cargo.toml and
    Cargo.lock are NOT edited (sha256 of the copy == sha256 of the artifact file, recorded per row);
  * RUSTFLAGS='-C overflow-checks=false' cargo bolero test bolero_wasm_eq -T 30s
    (the command and env of check_current.py's fuzz_30s arm; cargo-bolero from VERT's fork, installed
    by run_in_container_fuzz_real.sh the way run_in_container_current.sh installs it);
  * the harness compares, for each fuzzer-generated argument tuple, the LLM's Rust f_gold(PARAM1, ...)
    with the rWasm build of the C (which reads PARAM1 only) via assert_eq!(result, result_prime).

Verdicts recorded per attempt (none changes the run):
  * readme_verdict  : VERT ARTIFACT_README rule: fail iff the output contains "Test Failure";
  * torust_verdict  : VERT torust.py rule: "could not compile" -> compile_error; "Test Failure" or
                      exit code != 0 -> fail; subprocess timeout -> pass; else pass;
  * outcome         : check_current.py's labels (test_failure / clean_exit / compile_error / timeout /
                      exit_<rc>_no_test_failure_text), plus `startup_crash` when the fuzzing binary never
                      reached libFuzzer (no "INFO: Seed:") and the run ended non-zero without
                      "Test Failure" (cargo prints "process exited with no status code" for a signal).
Bookkeeping added:
  * the input and the panic message bolero prints after "Test Failure" are parsed (left/right of the
    assert, or the panic text and its src/main.rs line, which is classified as LLM Rust / rWasm
    oracle / the assertion by the line's position in main.rs);
  * start-up crash retry: an attempt whose outcome is `startup_crash` is re-run (the build is reused)
    up to RETRIES more times (env RETRIES, default 4). Every attempt and its full log are kept;
    `first_attempt` and `final` are reported separately;
  * crate-rename diagnostic (NOT counted): a project that fails to compile with "File name too long"
    (rustc object file name > 255 bytes, it repeats the crate name) is re-run once on a copy whose
    Cargo.toml [package] name is shortened to 'nwh', the edit rename_diag_current.sh makes.

  python3 check_fuzz_real.py <benchmark_dir> <part.json> FUNC [FUNC ...]
Logs: $BOLERO_LOGDIR/<FUNC>.fuzz_30s_real.a<k>.log (and .rename_diag.a<k>.log).
"""
import hashlib, json, os, re, shutil, subprocess, sys, tempfile, time

LOGDIR = os.environ.get("BOLERO_LOGDIR")
RETRIES = int(os.environ.get("RETRIES", 4))
TIMEOUT = int(os.environ.get("FUZZ_TIMEOUT", 1200))
CMD = "cargo bolero test bolero_wasm_eq -T 30s"
ENV = {"RUSTFLAGS": "-C overflow-checks=false"}
SHARED_TARGET = os.environ.get("SHARED_TARGET")   # sets CARGO_TARGET_DIR; NOTE: cargo-bolero 0.9 builds under
# <project>/target/fuzz/build_<hash> regardless (see the paths in the logs), so this has no effect on the build


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def parse_failure(out):
    """The block bolero prints after 'Test Failure': input text, error text, and fields from them."""
    i = out.find("======================== Test Failure ========================")
    if i < 0:
        return None
    blk = out[i:]
    j = blk.find("==============================================================", 10)
    blk = blk[:j] if j > 0 else blk[:4000]
    m = re.search(r"Input:\s*\n(.*?)\n\s*\nError:\s*\n(.*)", blk, re.S)
    if not m:
        return {"raw": blk[:3000]}
    inp, err = m.group(1).strip(), m.group(2).strip()
    r = {"input_text": inp, "input": [int(x) for x in re.findall(r"-?\d+", inp)], "error_text": err[:2000]}
    lr = re.search(r"left: `([^`]*)`,\s*\n\s*right: `([^`]*)`", err)
    if lr:
        r["left_rust"], r["right_oracle"] = lr.group(1), lr.group(2)
    loc = re.search(r"src/main\.rs:(\d+):(\d+)", err)
    if loc:
        r["panic_line"] = int(loc.group(1))
    pm = re.search(r"panicked at '(.*?)'(?:, src/|$)", err, re.S)
    if pm:
        r["panic_message"] = pm.group(1)[:600]
    return r


def regions(main_rs):
    """Line ranges in main.rs: the LLM's Rust (between the two LLM Output markers), the bolero test."""
    lines = open(main_rs).read().split("\n")
    marks = [k + 1 for k, l in enumerate(lines) if "////// LLM Output //////" in l]
    test = next((k + 1 for k, l in enumerate(lines) if "fn bolero_wasm_eq" in l), None)
    wasm_fn = next((k + 1 for k, l in enumerate(lines) if re.search(r"fn f_gold_wasm_thread_unsafe", l)), None)
    return {"llm_output": [marks[0], marks[-1]] if marks else None, "bolero_test_fn": test,
            "wasm_wrapper_fn": wasm_fn, "lines": len(lines)}


def failure_kind(f, reg):
    if f is None:
        return None
    if "left_rust" in f:
        return "assertion_mismatch"
    ln = f.get("panic_line")
    if ln is None:
        return "panic_unlocated"
    a, b = reg["llm_output"] or (0, -1)
    if a <= ln <= b:
        return "panic_in_llm_rust"
    if reg["bolero_test_fn"] and ln >= reg["bolero_test_fn"]:
        return "panic_in_harness"
    return "panic_in_rwasm_oracle"


def classify(rc, to, out):
    if to:
        return "timeout"
    if "could not compile" in out:
        return "compile_error"
    if "Test Failure" in out:
        return "test_failure"
    if rc == 0:
        return "clean_exit"
    if "INFO: Seed:" not in out:
        return "startup_crash"
    return f"exit_{rc}_no_test_failure_text"


def run_once(p, tgt, logname):
    env = {**os.environ, **ENV, "CARGO_TARGET_DIR": tgt}
    t = time.time()
    try:
        pr = subprocess.run(CMD, shell=True, cwd=p, env=env, capture_output=True, text=True, timeout=TIMEOUT)
        out, rc, to = pr.stdout + pr.stderr, pr.returncode, False
    except subprocess.TimeoutExpired as e:
        dec = lambda x: x.decode(errors="replace") if isinstance(x, bytes) else (x or "")
        out, rc, to = dec(e.stdout) + dec(e.stderr), None, True
    secs = round(time.time() - t, 1)
    if LOGDIR:
        open(os.path.join(LOGDIR, logname), "w").write(out)
    oc = classify(rc, to, out)
    execs = re.findall(r"^#(\d+)\s", out, re.M)
    done = re.search(r"Done (\d+) runs in (\d+) second", out)
    seed = re.search(r"INFO: Seed: (\d+)", out)
    built = re.findall(r"Finished release \[optimized\] target\(s\) in ([^\n]+)", out)
    return {"cmd": CMD, "env": ENV, "rc": rc, "timeout": to, "secs": secs, "outcome": oc,
            "readme_verdict": "fail" if "Test Failure" in out else "pass",
            "torust_verdict": ("pass" if to else "compile_error" if "could not compile" in out else
                               "fail" if ("Test Failure" in out or rc != 0) else "pass"),
            "evidence": {"libfuzzer_started": "INFO: Seed:" in out, "running_1_test": "running 1 test" in out,
                         "libfuzzer_seed": int(seed.group(1)) if seed else None,
                         "last_exec_count_printed": int(execs[-1]) if execs else None,
                         "libfuzzer_done": [int(done.group(1)), int(done.group(2))] if done else None,
                         "cargo_finished": built,
                         "signal": [f"{a} {b}" for a, b in re.findall(r"\(signal: (\d+), ([A-Z]+)", out)],
                         "no_status_code": "process exited with no status code" in out,
                         "exited_with_status": re.findall(r"process exited with status (\d+)", out),
                         "file_name_too_long": "File name too long" in out,
                         "deadly_signal": "libFuzzer: deadly signal" in out,
                         "asan_report": bool(re.search(r"ERROR: AddressSanitizer", out))},
            "failure": parse_failure(out), "log": logname if LOGDIR else None, "tail": out[-1200:]}


def prepare(bench, f, rename=False):
    proj = os.path.join(bench, f, "out-rwasm-bolero")
    work = tempfile.mkdtemp(prefix="fzr_")
    p = os.path.join(work, "p")
    shutil.copytree(proj, p, ignore=shutil.ignore_patterns("target"))
    shutil.copy(os.path.join(bench, "..", "rust-toolchain.toml"), p)
    files = {}
    for rel in ("src/main.rs", "Cargo.toml", "Cargo.lock", "src/guest_mem_wrapper.rs"):
        a, b = os.path.join(proj, rel), os.path.join(p, rel)
        if os.path.exists(a):
            files[rel] = {"artifact_sha256": sha(a), "copy_sha256": sha(b)}
    if rename:
        c = os.path.join(p, "Cargo.toml"); t = open(c).read()
        t2 = re.sub(r'(\[package\][^\[]*?\bname\s*=\s*)"[^"]*"', r'\1"nwh"', t, count=1, flags=re.S)
        assert t2 != t
        open(c, "w").write(t2)
        files["Cargo.toml"]["copy_sha256_after_rename"] = sha(c)
    return work, p, files


def run_with_retries(p, tgt, stem):
    atts = []
    for k in range(1 + RETRIES):
        a = run_once(p, tgt, f"{stem}.a{k}.log")
        atts.append(a)
        if a["outcome"] != "startup_crash":
            break
    return atts


def main():
    bench, out_path, funcs = sys.argv[1], sys.argv[2], sys.argv[3:]
    tgt_root = SHARED_TARGET or tempfile.mkdtemp(prefix="fzr_tgt_")
    rows = []
    for f in funcs:
        tgt = tgt_root if SHARED_TARGET else os.path.join(tgt_root, f)
        work, p, files = prepare(bench, f)
        reg = regions(os.path.join(p, "src", "main.rs"))
        row = {"function": f, "files": files,
               "unmodified": all(v["artifact_sha256"] == v["copy_sha256"] for v in files.values()),
               "main_rs_regions": reg,
               "harness_line": next((l.strip() for l in open(os.path.join(p, "src", "main.rs"))
                                     if "bolero::check!()" in l), None)}
        atts = run_with_retries(p, tgt, f"{f}.fuzz_30s_real")
        for a in atts:
            a["failure_kind"] = failure_kind(a["failure"], reg)
        row["attempts"] = atts
        row["first_attempt"] = {k: atts[0][k] for k in ("outcome", "readme_verdict", "torust_verdict", "failure_kind")}
        row["final"] = {k: atts[-1][k] for k in ("outcome", "readme_verdict", "torust_verdict", "failure_kind", "failure")}
        row["final"]["attempt_index"] = len(atts) - 1
        print(f, [a["outcome"] for a in atts], atts[-1]["secs"], (atts[-1]["failure"] or {}).get("input_text"), flush=True)
        shutil.rmtree(work, ignore_errors=True)
        if atts[-1]["outcome"] == "compile_error" and atts[-1]["evidence"]["file_name_too_long"]:
            work, p, files2 = prepare(bench, f, rename=True)
            d = run_with_retries(p, tgt, f"{f}.rename_diag")
            for a in d:
                a["failure_kind"] = failure_kind(a["failure"], reg)
            row["diagnostic_crate_rename"] = {
                "note": "NOT counted. Same command on a copy whose Cargo.toml [package] name is 'nwh' "
                        "(rename_diag_current.sh's edit); main.rs untouched.", "files": files2, "attempts": d,
                "final": {k: d[-1][k] for k in ("outcome", "readme_verdict", "torust_verdict", "failure_kind", "failure")}}
            print(f, "rename_diag", [a["outcome"] for a in d], (d[-1]["failure"] or {}).get("input_text"), flush=True)
            shutil.rmtree(work, ignore_errors=True)
        rows.append(row)
        json.dump({"partial": True, "rows": rows}, open(out_path, "w"), indent=1)
    json.dump({"partial": False, "rows": rows}, open(out_path, "w"), indent=1)


if __name__ == "__main__":
    main()
