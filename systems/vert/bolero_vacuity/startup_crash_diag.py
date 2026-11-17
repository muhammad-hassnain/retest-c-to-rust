"""Diagnostic (not counted): do cargo-bolero's ASan-instrumented binaries crash at start-up on this host?

Background: in results/vert_bolero_logs_current/*.fuzz_30s.log, 12 runs end with exit 1 and no
"Test Failure". In all 12 the test binary printed nothing: 6 die in cargo-bolero's target-selection
step (cargo test ... -- --nocapture --exact with CARGO_BOLERO_SELECT=one, which only prints the target
name and runs none of VERT's code; "signal: 11, SIGSEGV", then "process exited with status 101") and 6
when the fuzzing binary is started ("process exited with no status code" = killed by a signal, before
libFuzzer prints "INFO: Seed"). harness/oracle_ub_common.py records the same symptom for clang-14 ASan
on PIE binaries on this kernel. This script measures the rate directly, with no fuzzing:

  1. the test binaries check_fuzz_real.py built (found under the SHARED_TARGET dirs), each run N times in
     cargo-bolero's selection mode (CARGO_BOLERO_SELECT=one, args: bolero_wasm_eq --nocapture --exact),
     the exact invocation of the selection step; no VERT code runs in this mode;
  2. a two-line Rust program built with the project toolchain (nightly-2023-07-29), with and without
     -Zsanitizer=address (and with -C relocation-model=static / no PIE under ASan), each run N times.

  python3 -I startup_crash_diag.py <out.json> <N> <target_dir> [<target_dir> ...]
"""
import glob, json, os, subprocess, sys, tempfile


def tally(argv, env, n):
    c = {}
    for _ in range(n):
        p = subprocess.run(argv, env={**os.environ, **env}, capture_output=True, timeout=60)
        k = f"rc={p.returncode}" + ("" if p.stdout or p.stderr else " (no output)")
        c[k] = c.get(k, 0) + 1
    return dict(sorted(c.items()))


def main():
    out, n, tdirs = sys.argv[1], int(sys.argv[2]), sys.argv[3:]
    res = {"n_runs_each": n, "vert_test_binaries_selection_mode": {}, "minimal_rust_program": {}}
    exes = []
    for t in tdirs:
        for e in glob.glob(os.path.join(t, "fuzz", "build_*", "*", "release", "deps", "c_transcoder_*")):
            if os.access(e, os.X_OK) and "." not in os.path.basename(e):
                exes.append(e)
    for e in sorted(exes):
        r = tally([e, "bolero_wasm_eq", "--nocapture", "--exact"], {"CARGO_BOLERO_SELECT": "one"}, n)
        res["vert_test_binaries_selection_mode"][os.path.basename(e)] = r
        print(os.path.basename(e)[:60], r, flush=True)
    d = tempfile.mkdtemp()
    src = os.path.join(d, "hello.rs")
    open(src, "w").write('fn main() { let v = vec![1u8; 16]; println!("{}", v[3]); }\n')
    tc = "+nightly-2023-07-29"
    builds = {
        "plain": ["-O"],
        "asan_pie_default": ["-O", "-Zsanitizer=address"],
        "asan_static_relocation_no_pie": ["-O", "-Zsanitizer=address", "-C", "relocation-model=static"],
    }
    for k, fl in builds.items():
        b = os.path.join(d, k)
        p = subprocess.run(["rustc", tc, *fl, "--target", "x86_64-unknown-linux-gnu", src, "-o", b], capture_output=True, text=True)
        if p.returncode:
            res["minimal_rust_program"][k] = {"build_error": p.stderr[-600:]}
            continue
        ftype = subprocess.run(["file", "-b", b], capture_output=True, text=True).stdout.strip() if subprocess.run(
            ["which", "file"], capture_output=True).returncode == 0 else None
        res["minimal_rust_program"][k] = {"rustc_flags": fl, "file": ftype, "outcomes": tally([b], {}, n)}
        print(k, res["minimal_rust_program"][k]["outcomes"], flush=True)
    res["rustc"] = subprocess.run(["rustc", tc, "-vV"], capture_output=True, text=True).stdout
    res["kernel"] = os.uname().release
    res["machine"] = os.uname().machine
    json.dump(res, open(out, "w"), indent=1)


if __name__ == "__main__":
    main()
