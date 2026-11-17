"""Provenance check for every VERT scalar wrong answer, inside VERT's OWN project.

VERT's out-rwasm-bolero/src/main.rs contains, side by side, (a) the LLM translation `f_gold`
(between the `////// LLM Output //////` markers written by torust.py) and (b) VERT's rWasm
oracle `f_gold_wasm_thread_unsafe()`, mechanically compiled from the same benchmark's C. VERT's
own bolero test asserts they are equal. We copy the project unchanged except that the bolero test
is replaced by a `main` that runs both on our counterexample inputs and prints them.

This shows, with no harness or C build of ours involved, that (1) VERT's oracle reproduces the
original C's answer (so the file belongs to that C function) and (2) VERT's accepted translation
disagrees with VERT's own oracle on those inputs.

  python3 -I systems/vert/oracle_check.py      # writes results/vert_oracle_confirmation.json
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
BENCH = os.path.join(REPO, "artifacts", "vert", "benchmark", "c_transcoder")
OUT = os.path.join(REPO, "results", "vert_oracle_confirmation.json")


def make_main(src, rows, nparams):
    # rWasm's own `fn main` (unused here) is renamed so ours can be the binary entry point.
    src = re.sub(r"^fn main\(\) \{", "fn _rwasm_main() {", src, count=1, flags=re.M)
    i = src.index("use bolero::check;")
    sets = "\n".join(f"            PARAM{k + 1} = row[{k}];" for k in range(nparams))
    args = ",".join(f"unsafe{{PARAM{k + 1}}}.into()" for k in range(nparams))
    table = ", ".join("[" + ", ".join(str(x) for x in r) + "]" for r in rows)
    return src[:i] + f"""
fn main() {{
    std::panic::set_hook(Box::new(|_| {{}}));
    let rows: Vec<[i32; {nparams}]> = vec![{table}];
    for row in rows.iter() {{
        unsafe {{
{sets}
        }}
        let llm = std::panic::catch_unwind(|| f_gold({args}));
        let oracle = std::panic::catch_unwind(|| f_gold_wasm_thread_unsafe());
        let show = |r: std::thread::Result<i32>| match r {{ Ok(v) => v.to_string(), Err(_) => "panic".to_string() }};
        println!("{{:?}}\\t{{}}\\t{{}}", row, show(llm), show(oracle));
    }}
}}
"""


def main():
    d = json.load(open(os.path.join(REPO, "results", "vert_diff.json")))
    tgt = tempfile.mkdtemp(prefix="vert_oracle_target_")
    out = []
    for r in d["value_bugs"]:
        f = r["func"]
        proj = os.path.join(BENCH, f, "out-rwasm-bolero")
        src = open(os.path.join(proj, "src", "main.rs")).read()
        m = re.search(r"with_type::<\((.*?)\)>", src)
        nparams = len([t for t in m.group(1).split(",") if t.strip()])
        samples = r["value_samples"]
        rows = [s["in"] for s in samples]
        work = tempfile.mkdtemp(prefix="vert_oracle_")
        shutil.copytree(proj, os.path.join(work, "p"), ignore=shutil.ignore_patterns("target"))
        with open(os.path.join(work, "p", "src", "main.rs"), "w") as fh:
            fh.write(make_main(src, rows, nparams))
        b = subprocess.run(["cargo", "build", "--release", "-q"], cwd=os.path.join(work, "p"),
                           capture_output=True, text=True, env={**os.environ, "CARGO_TARGET_DIR": tgt})
        row = {"function": f, "build_ok": b.returncode == 0, "inputs": []}
        if b.returncode == 0:
            name = re.search(r'name\s*=\s*"([^"]+)"', open(os.path.join(work, "p", "Cargo.toml")).read()).group(1)
            p = subprocess.run([os.path.join(tgt, "release", name)], capture_output=True, text=True, timeout=120)
            for s, line in zip(samples, p.stdout.strip().splitlines()):
                _, llm, oracle = line.split("\t")
                row["inputs"].append({"input": s["in"], "c_gcc": s["c"], "rust_our_harness": s["rust"],
                                      "vert_llm_f_gold": llm, "vert_rwasm_oracle": oracle,
                                      "oracle_matches_c": oracle == s["c"], "llm_matches_our_rust": llm == s["rust"],
                                      "llm_differs_from_vert_oracle": llm != oracle})
        else:
            row["build_error"] = b.stderr[-800:]
        ins = row["inputs"]
        # Which PARAMs does VERT's rWasm oracle actually read? (audit: for multi-argument functions it
        # reads only PARAM1, so it cannot serve as an oracle for those.)
        reads = sorted(set(re.findall(r"TaggedVal::from\(unsafe \{(PARAM\d+)\}\)", src)))
        row["oracle_reads_params"] = reads
        row["declared_params"] = nparams
        row["oracle_ignores_params"] = len(reads) < nparams
        # confirmed: on at least one input VERT's own oracle reproduces the C and the LLM output differs
        row["confirmed"] = any(x["oracle_matches_c"] and x["llm_differs_from_vert_oracle"] for x in ins)
        out.append(row)
        print(f, "build" if row["build_ok"] else "BUILD-FAIL", "confirmed" if row["confirmed"] else "NOT-confirmed", flush=True)
        shutil.rmtree(work, ignore_errors=True)
        json.dump({"partial": True, "rows": out}, open(OUT, "w"), indent=1)
    summ = {"functions": len(out), "built": sum(r["build_ok"] for r in out), "confirmed": sum(r["confirmed"] for r in out),
            "single_param_functions": sum(r["declared_params"] == 1 for r in out),
            "single_param_confirmed": sum(r["confirmed"] for r in out if r["declared_params"] == 1),
            "oracle_ignores_params": [r["function"] for r in out if r["oracle_ignores_params"]],
            "rule": "confirmed = on at least one input, VERT's oracle equals the original C's answer and VERT's LLM output differs",
            "method": "VERT's own out-rwasm-bolero project, unchanged except the bolero test replaced by a main printing "
                      "f_gold (VERT's LLM translation) and f_gold_wasm_thread_unsafe (VERT's rWasm oracle of the same C) "
                      "on our counterexample inputs; built with cargo --release (overflow wraps, as in our harness)"}
    json.dump({"summary": summ, "rows": out}, open(OUT, "w"), indent=1)
    print(json.dumps(summ))


if __name__ == "__main__":
    main()
