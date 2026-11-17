"""arXiv (issues.md 47): systems/vert/oracle_check.py's check for the VERT wide-int wrong answers.

oracle_check.py (results/vert_oracle_confirmation.json) copies VERT's own out-rwasm-bolero project unchanged
except that the bolero test is replaced by a `main` that runs VERT's LLM translation `f_gold` and VERT's rWasm
oracle `f_gold_wasm_thread_unsafe()` (the C compiled to WebAssembly and back to Rust) on our counterexample
inputs, built with cargo --release. This script does exactly that (it imports oracle_check.make_main) for the
wide-int value bugs of results/arxiv_vert_diff_grid2_wide.json (run.py --wide-int), on
  * the function's value_samples in that file (oracle_check.py's input set), and
  * the smallest UB-clean diverging input on which clang agrees (smallest_clean_clang_agrees) of
    results/arxiv_vert_wide_ub_recheck.json, our exposing input under the full UB rule, if not already there.
Per input it records whether VERT's oracle prints the original C's value (gcc, (long long) f_gold), the Rust's
value, both or neither. Both f_gold and f_gold_wasm_thread_unsafe return i32 in these projects (checked per
function: rust_signature / oracle_wrapper_signature), so the oracle's value is what VERT's harness compares.
VERT built the oracle from <F>_towasm_mutated.c, whose f_gold declaration is recorded next to the original's
(VERT's tool changed some return types to int; on wasm32 `long` is 32 bits).

  docker run --rm --cpus 4 -v "$PWD:/work" -w /work crust-stable python3 -I systems/vert/arxiv_wide_oracle_check.py
Writes results/arxiv_vert_wide_oracle_confirmation.json.
"""
import datetime
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
from oracle_check import make_main, BENCH  # noqa: E402  (systems/vert/oracle_check.py, unchanged)

SCALAR = "results/arxiv_vert_diff_grid2_wide.json"
UB = "results/arxiv_vert_wide_ub_recheck.json"
OUT = os.path.join(REPO, "results", "arxiv_vert_wide_oracle_confirmation.json")
I32 = (-2 ** 31, 2 ** 31 - 1)


def decl(path):
    s = open(path, errors="replace").read()
    m = re.search(r"^[^\n;{}]*\bf_gold\s*\([^)]*\)\s*\{", s, re.M)
    return m.group(0).rstrip("{").strip() if m else None


def ver(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True).stdout.splitlines()[0]
    except Exception:  # noqa: BLE001
        return None


def main():
    t0 = datetime.datetime.now(datetime.timezone.utc)
    d = json.load(open(os.path.join(REPO, SCALAR)))
    ub = {r["func"]: r for r in json.load(open(os.path.join(REPO, UB)))["functions"]}
    tgt = tempfile.mkdtemp(prefix="vert_oracle_target_")
    out = []
    for r in d["value_bugs"]:
        f = r["func"]
        proj = os.path.join(BENCH, f, "out-rwasm-bolero")
        src = open(os.path.join(proj, "src", "main.rs")).read()
        m = re.search(r"with_type::<\((.*?)\)>", src)
        nparams = len([t for t in m.group(1).split(",") if t.strip()])
        samples = [dict(s, source="value_samples") for s in r["value_samples"]]
        sc = (ub.get(f) or {}).get("smallest_clean_clang_agrees")
        if sc and sc["in"] not in [s["in"] for s in samples]:
            samples.append({"in": sc["in"], "c": sc["c"], "rust": sc["rust"], "source": "ub_recheck.smallest_clean_clang_agrees"})
        rows = [s["in"] for s in samples]
        llm = src.split("////// LLM Output //////")[1]
        rsig = re.search(r"fn f_gold\s*\([^)]*\)\s*->\s*[^{]+", llm)
        osig = re.search(r"fn f_gold_wasm_thread_unsafe\s*\([^)]*\)\s*->\s*[^{]+", src)
        work = tempfile.mkdtemp(prefix="vert_oracle_")
        shutil.copytree(proj, os.path.join(work, "p"), ignore=shutil.ignore_patterns("target"))
        with open(os.path.join(work, "p", "src", "main.rs"), "w") as fh:
            fh.write(make_main(src, rows, nparams))
        b = subprocess.run(["cargo", "build", "--release", "-q"], cwd=os.path.join(work, "p"),
                           capture_output=True, text=True, env={**os.environ, "CARGO_TARGET_DIR": tgt})
        row = {"function": f, "c_return": r.get("c_return"),
               "c_f_gold_original": decl(os.path.join(BENCH, f, f"{f}.c")),
               "c_f_gold_compiled_to_wasm_by_vert": decl(os.path.join(BENCH, f, f"{f}_towasm_mutated.c")),
               "rust_signature": rsig.group(0).strip() if rsig else None,
               "oracle_wrapper_signature": osig.group(0).strip() if osig else None,
               "build_ok": b.returncode == 0, "inputs": []}
        if b.returncode == 0:
            name = re.search(r'name\s*=\s*"([^"]+)"', open(os.path.join(work, "p", "Cargo.toml")).read()).group(1)
            p = subprocess.run([os.path.join(tgt, "release", name)], capture_output=True, text=True, timeout=120)
            for s, line in zip(samples, p.stdout.strip().splitlines()):
                _, llm_v, oracle = line.split("\t")
                om, orr = oracle == s["c"], oracle == s["rust"]
                row["inputs"].append({"input": s["in"], "source": s["source"], "c_gcc": s["c"], "rust_our_harness": s["rust"],
                                      "c_value_fits_i32": I32[0] <= int(s["c"]) <= I32[1],
                                      "vert_llm_f_gold": llm_v, "vert_rwasm_oracle": oracle,
                                      "oracle_matches_c": om, "oracle_matches_our_rust": orr,
                                      "oracle_agrees_with": "both" if om and orr else "c" if om else "rust" if orr else "neither",
                                      "c_value_mod_2_32_as_i32": ((int(s["c"]) + 2 ** 31) % 2 ** 32) - 2 ** 31,
                                      "oracle_equals_c_mod_2_32": oracle != "panic" and int(oracle) == ((int(s["c"]) + 2 ** 31) % 2 ** 32) - 2 ** 31,
                                      "llm_matches_our_rust": llm_v == s["rust"],
                                      "llm_differs_from_vert_oracle": llm_v != oracle})
        else:
            row["build_error"] = b.stderr[-800:]
        ins = row["inputs"]
        reads = sorted(set(re.findall(r"TaggedVal::from\(unsafe \{(PARAM\d+)\}\)", src)))
        row["oracle_reads_params"] = reads
        # static reading of the rWasm oracle: the _start-like function stores the wasm f_gold result into the
        # i32 static RESULT via try_as_i32()?; if the wasm f_gold returns i64 that conversion yields None and
        # f_gold_wasm_thread_unsafe's _start().unwrap() panics on every input
        st = re.search(r"fn func_\d+\(&mut self, \) -> Option<i32> \{(?:(?!\nfn ).)*?self\.func_(\d+)\((?:(?!\nfn ).)*?RESULT = retval", src, re.S)
        callee = st.group(1) if st else None
        cm = re.search(r"fn func_%s\(&mut self,[^)]*\) -> Option<([^>]+)>" % callee, src) if callee else None
        row["oracle_wasm_f_gold_returns"] = cm.group(1) if cm else None
        row["oracle_result_static"] = (re.search(r"static mut RESULT\s*:\s*([^=]+?)\s*=", src) or [None, None])[1]
        row["oracle_panics_on_every_input_by_construction"] = (row["oracle_wasm_f_gold_returns"] == "i64"
                                                              and row["oracle_result_static"] == "i32")
        row["declared_params"] = nparams
        row["oracle_ignores_params"] = len(reads) < nparams
        # oracle_check.py's rule, unchanged
        row["confirmed"] = any(x["oracle_matches_c"] and x["llm_differs_from_vert_oracle"] for x in ins)
        exp = [x for x in ins if x["source"] == "ub_recheck.smallest_clean_clang_agrees"] or \
              [x for x in ins if x["input"] == ((ub.get(f) or {}).get("smallest_clean_clang_agrees") or {}).get("in")]
        row["on_our_exposing_input"] = exp[0] if exp else None
        row["oracle_agrees_with_on_exposing_input"] = exp[0]["oracle_agrees_with"] if exp else None
        out.append(row)
        print(f, "build" if row["build_ok"] else "BUILD-FAIL", "confirmed" if row["confirmed"] else "NOT-confirmed",
              row["oracle_agrees_with_on_exposing_input"], flush=True)
        shutil.rmtree(work, ignore_errors=True)
    t1 = datetime.datetime.now(datetime.timezone.utc)
    cnt = {}
    for r in out:
        k = r["oracle_agrees_with_on_exposing_input"]
        cnt[str(k)] = cnt.get(str(k), 0) + 1
    summ = {"functions": len(out), "built": sum(r["build_ok"] for r in out), "confirmed": sum(r["confirmed"] for r in out),
            "single_param_functions": sum(r["declared_params"] == 1 for r in out),
            "single_param_confirmed": sum(r["confirmed"] for r in out if r["declared_params"] == 1),
            "oracle_ignores_params": [r["function"] for r in out if r["oracle_ignores_params"]],
            "oracle_panics_on_every_input_by_construction": [r["function"] for r in out if r["oracle_panics_on_every_input_by_construction"]],
            "oracle_agrees_with_on_exposing_input": cnt,
            "functions_by_oracle_agreement_on_exposing_input": {
                k: [r["function"] for r in out if str(r["oracle_agrees_with_on_exposing_input"]) == k] for k in sorted(cnt)},
            "rule": "confirmed = on at least one input, VERT's oracle equals the original C's answer and VERT's LLM output differs (oracle_check.py's rule)",
            "method": "VERT's own out-rwasm-bolero project, unchanged except the bolero test replaced by a main printing "
                      "f_gold (VERT's LLM translation) and f_gold_wasm_thread_unsafe (VERT's rWasm oracle of the same C) "
                      "on our counterexample inputs; built with cargo --release (overflow wraps, as in our harness); "
                      "systems/vert/oracle_check.make_main"}
    prov = {"script": "systems/vert/arxiv_wide_oracle_check.py (systems/vert/oracle_check.py's procedure; make_main imported unchanged)",
            "command": os.environ.get("RUN_COMMAND", "docker run --rm -v \"$PWD:/work\" -w /work crust-stable python3 -I systems/vert/arxiv_wide_oracle_check.py"),
            "inputs_from": [SCALAR, UB],
            "git_rev": os.environ.get("GIT_REV"), "git_dirty_note": os.environ.get("GIT_DIRTY"),
            "host": os.environ.get("HOST_DESC"), "arch": platform.machine(), "image": os.environ.get("IMAGE_DESC"),
            "toolchains": {"rustc": ver(["rustc", "--version"]), "cargo": ver(["cargo", "--version"]),
                           "gcc": ver(["gcc", "--version"]), "python": platform.python_version(),
                           "note": "no rust-toolchain.toml is copied into the project (as oracle_check.py), so the image's default rustc builds it"},
            "date_start_utc": t0.isoformat(), "date_end_utc": t1.isoformat()}
    json.dump({"provenance": prov, "summary": summ, "rows": out}, open(OUT, "w"), indent=1)
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()
