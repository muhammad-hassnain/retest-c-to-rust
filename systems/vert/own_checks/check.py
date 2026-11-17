"""Re-run VERT's own checks on VERT's shipped translations, three ways, one function at a time.

Arms (each on a fresh copy of VERT's project; VERT's translation and oracle are never edited):
  vert_script : VERT's own evaluation.py, unmodified, run from a VERT checkout
                (`python3 evaluation.py c FUNC bolero`). Records what VERT's script prints.
  fuzz_60s    : real fuzzing of VERT's own bolero harness: `cargo bolero test bolero_wasm_eq -T 60s`.
                Only change: the assert gets a message so a failure prints its input
                (assert_eq!(result, result_prime) -> assert_eq!(result, result_prime, "VERT_INPUT={:?}", (args))).
  kani_correct: VERT's bolero project plus one added Kani proof in which BOTH sides get the SAME
                symbolic input n in [LO, HI], with Kani's unwinding checks ON (VERT's harness instead
                runs the translation at 12 and the oracle at n in {0,1}, with unwinding checks off).
                Single-parameter functions only (VERT's oracle ignores later parameters).
Verdicts:
  fuzz_60s     caught = 'VERT_INPUT=' printed (the equivalence assertion failed); input/left/right parsed.
  kani_correct counterexample = VERIFICATION:- FAILED with a failed assertion that is the equivalence
               check (not an unwinding assertion); the concrete input comes from --concrete-playback=print.

  python3 check.py VERT_ROOT OUT.json ARM FUNC [FUNC ...]     (ARM: vert_script | fuzz_60s | kani_correct)
"""
import json, os, re, shutil, subprocess, sys, tempfile, time

LO, HI, UNWIND = int(os.environ.get("KLO", 0)), int(os.environ.get("KHI", 8)), int(os.environ.get("KUNWIND", 20))


def sh(cmd, cwd, env=None, timeout=900):
    t = time.time()
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, env={**os.environ, **(env or {})}, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout + p.stderr, round(time.time() - t, 1), False
    except subprocess.TimeoutExpired as e:
        o = (e.stdout or b"") + (e.stderr or b"")
        return None, o.decode(errors="replace") if isinstance(o, bytes) else o, round(time.time() - t, 1), True


def closure_args(src):
    m = re.search(r"for_each\(\|\(?([^|)]*)\)?\|", src)
    return [a.strip() for a in m.group(1).split(",") if a.strip()]


def vert_script(root, f):
    rc, out, secs, to = sh(f"python3 evaluation.py c {f} bolero", root, timeout=600)
    verdict = next((v for v in ("Bolero pass", "Bolero failed", "Compilation problem", "Bolero timeout") if v in out), "none")
    return {"cmd": f"cd VERT_ROOT && python3 evaluation.py c {f} bolero", "printed_verdict": verdict, "secs": secs,
            "tail": out[-1200:]}


def fuzz(root, f):
    w = tempfile.mkdtemp(prefix="fz_"); p = os.path.join(w, "p")
    shutil.copytree(os.path.join(root, "benchmark/c_transcoder", f, "out-rwasm-bolero"), p, ignore=shutil.ignore_patterns("target"))
    shutil.copy(os.path.join(root, "benchmark/rust-toolchain.toml"), p)
    m = os.path.join(p, "src/main.rs"); s = open(m).read()
    args = closure_args(s)
    s2 = s.replace("assert_eq!(result, result_prime);",
                   'assert_eq!(result, result_prime, "VERT_INPUT={:?}", (' + ",".join(args) + ",));", 1)
    assert s2 != s
    open(m, "w").write(s2)
    rc, out, secs, to = sh("cargo bolero test bolero_wasm_eq -T 60s", p, {"RUSTFLAGS": "-C overflow-checks=false"}, timeout=900)
    r = {"cmd": "cd out-rwasm-bolero && RUSTFLAGS='-C overflow-checks=false' cargo bolero test bolero_wasm_eq -T 60s",
         "edit": "assert message only: VERT_INPUT={:?}", "rc": rc, "secs": secs, "timeout": to,
         "caught": "VERT_INPUT=" in out}
    mi = re.search(r"left: `?([^`\n]*)`?,?\s*\n\s*right: `?([^`\n]*)`?:? ?VERT_INPUT=\(([^)]*)\)", out)
    if mi:
        r.update({"llm_result": mi.group(1).strip(), "oracle_result": mi.group(2).strip(),
                  "input": [x.strip() for x in mi.group(3).split(",") if x.strip()]})
    elif r["caught"]:
        r["raw"] = out[out.index("VERT_INPUT=") - 300: out.index("VERT_INPUT=") + 100]
    r["tail"] = out[-1500:]
    shutil.rmtree(w, ignore_errors=True)
    return r


def kani(root, f):
    w = tempfile.mkdtemp(prefix="kn_"); p = os.path.join(w, "p")
    shutil.copytree(os.path.join(root, "benchmark/c_transcoder", f, "out-rwasm-bolero"), p, ignore=shutil.ignore_patterns("target"))
    m = os.path.join(p, "src/main.rs"); s = open(m).read()
    t = re.search(r"with_type::<\((.*?)\)>", s).group(1)
    types = [x.strip() for x in t.split(",") if x.strip()]
    if types != ["i32"]:
        return {"skipped": f"parameter types {types}: only single-i32 functions (VERT's oracle ignores later parameters)"}
    # unique crate name, as in kani_vacuity/check.py (avoids stale models across functions)
    c = os.path.join(p, "Cargo.toml"); ct = open(c).read()
    open(c, "w").write(re.sub(r'name\s*=\s*"[^"]+"', 'name = "kc_' + re.sub(r"\W", "_", f.lower())[:40] + '"', ct, count=1))
    # cargo kani builds without dev-dependencies, so VERT's bolero test (which imports bolero) is
    # dropped from this copy; the translation and the oracle above it are untouched.
    s = s[:s.index("use bolero::check;")]
    s += f"""
#[cfg(kani)]
#[kani::proof]
#[kani::unwind({UNWIND})]
fn kani_eq_correct() {{
    let n: i32 = kani::any();
    kani::assume(n >= {LO} && n <= {HI});
    unsafe {{ PARAM1 = n; }}
    let result = f_gold(n.into());
    let result_prime = f_gold_wasm_thread_unsafe();
    assert!(result == result_prime, "equivalence");
}}
"""
    open(m, "w").write(s)
    cmd = "cargo kani --harness kani_eq_correct --output-format terse -Z concrete-playback --concrete-playback=print"
    rc, out, secs, to = sh(cmd, p, timeout=int(os.environ.get("KTIMEOUT", 1800)))
    os.makedirs("kani_logs", exist_ok=True)
    open(os.path.join("kani_logs", f + ".log"), "w").write(out)
    failed = "VERIFICATION:- FAILED" in out
    eq_fail = bool(re.search(r"Failed Checks: equivalence", out))
    unwind_fail = bool(re.search(r"Failed Checks: unwinding assertion", out))
    r = {"cmd": f"cd out-rwasm-bolero && (append kani_eq_correct, see check.py) && {cmd}", "range": [LO, HI], "unwind": UNWIND,
         "unwinding_checks": "on (Kani default)", "secs": secs, "timeout": to,
         "verdict": "timeout" if to else ("counterexample" if eq_fail else ("inconclusive_unwind" if unwind_fail else
                    ("verified_in_range" if "VERIFICATION:- SUCCESSFUL" in out else ("failed_other" if failed else "error"))))}
    vals = re.findall(r"vec!\[([^\]]*)\]", out)
    if vals:
        b = [int(x) for x in vals[0].split(",") if x.strip()]
        if len(b) == 4:
            r["counterexample_n"] = int.from_bytes(bytes(b), "little", signed=True)
    r["failed_checks"] = sorted(set(re.findall(r"Failed Checks: ([^\n]*)", out)))[:20]
    errs = re.findall(r"error(?:\[E\d+\])?: [^\n]*", out)
    r["errors"] = errs[:5]
    r["tail"] = out[-2500:]
    shutil.rmtree(w, ignore_errors=True)
    return r


def main():
    root, outp, arm, funcs = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
    rows = json.load(open(outp))["rows"] if os.path.exists(outp) else []
    done = {r["function"] for r in rows}
    fn = {"vert_script": vert_script, "fuzz_60s": fuzz, "kani_correct": kani}[arm]
    for f in funcs:
        if f in done:
            continue
        r = {"function": f, arm: fn(root, f)}
        rows.append(r)
        x = r[arm]; print(f, arm, {k: x.get(k) for k in ("printed_verdict", "caught", "input", "llm_result", "oracle_result", "verdict", "counterexample_n", "skipped") if k in x}, flush=True)
        json.dump({"arm": arm, "rows": rows}, open(outp, "w"), indent=1)


if __name__ == "__main__":
    main()
