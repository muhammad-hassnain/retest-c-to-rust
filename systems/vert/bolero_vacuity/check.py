"""Does VERT's property-based test (PBT, Table 1 "PBT" column) ever execute the equivalence check?

VERT's ARTIFACT_README ("How to Run Validation Directly") prescribes, per benchmark:
    mkdir -p src/__fuzz__
    RUSTFLAGS="-C overflow-checks=false" cargo bolero reduce bolero_wasm_eq
and "if [output] doesn't match 'Test Failure', count it as success" (evaluation.py:57-69 does the same,
with a 180 s timeout). `cargo bolero reduce` minimises an existing corpus; it does not generate inputs.

For each selected function we run three arms on a fresh copy of VERT's out-rwasm-bolero project:
  vert_reduce   : VERT's exact procedure on VERT's shipped translation.
  planted_reduce: VERT's exact procedure after replacing the LLM translation's f_gold body with
                  `return 12345` (wrong on every input) -- a mutation-adequacy check of the procedure.
  fuzz_30s      : the same planted-wrong project under real fuzzing, `cargo bolero test -T 30s`
                  (control: shows the harness itself CAN detect a mismatch when inputs are generated).
Verdict per arm uses VERT's own rule: pass = no 'Test Failure' in stdout+stderr.

  python3 systems/vert/bolero_vacuity/check.py <benchmark_dir> <out.json> FUNC [FUNC ...]
"""
import json, os, re, shutil, subprocess, sys, tempfile, time

def run(cmd, cwd, env_extra, timeout):
    env = {**os.environ, **env_extra}
    t = time.time()
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        out, rc, to = p.stdout + p.stderr, p.returncode, False
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")) + \
              ((e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or ""))
        rc, to = None, True
    return {"cmd": cmd, "rc": rc, "timeout": to, "secs": round(time.time() - t, 1),
            "test_failure": "Test Failure" in out, "could_not_compile": "could not compile" in out,
            "vert_verdict": "timeout" if to else ("compile_error" if "could not compile" in out else
                                                  ("fail" if "Test Failure" in out else "pass")),
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

def main():
    bench, out_path, funcs = sys.argv[1], sys.argv[2], sys.argv[3:]
    tgt = tempfile.mkdtemp(prefix="bolero_tgt_")
    rows = []
    for f in funcs:
        proj = os.path.join(bench, f, "out-rwasm-bolero")
        row = {"function": f, "arms": {}}
        for arm in ("vert_reduce", "planted_reduce", "fuzz_30s"):
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
                r = run("cargo bolero test bolero_wasm_eq -T 30s", p, env, 600)
            else:
                os.makedirs(os.path.join(p, "src", "__fuzz__"), exist_ok=True)
                r = run("cargo bolero reduce bolero_wasm_eq", p, env, 600)
            row["arms"][arm] = r
            print(f, arm, r["vert_verdict"], r["secs"], flush=True)
            shutil.rmtree(work, ignore_errors=True)
        rows.append(row)
        json.dump({"partial": True, "rows": rows}, open(out_path, "w"), indent=1)
    def cnt(arm, v): return sum(1 for r in rows if r["arms"].get(arm, {}).get("vert_verdict") == v)
    summ = {arm: {v: cnt(arm, v) for v in ("pass", "fail", "compile_error", "timeout")}
            for arm in ("vert_reduce", "planted_reduce", "fuzz_30s")}
    summ["functions"] = len(rows)
    summ["procedure"] = "VERT ARTIFACT_README 'How to Run Validation Directly' (PBT): mkdir -p src/__fuzz__; RUSTFLAGS='-C overflow-checks=false' cargo bolero reduce bolero_wasm_eq; pass iff no 'Test Failure'"
    json.dump({"summary": summ, "rows": rows}, open(out_path, "w"), indent=1)
    print(json.dumps(summ))

if __name__ == "__main__":
    main()
