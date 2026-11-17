"""M2: measure "held out" instead of asserting it.

For each re-graded system this script records (a) what inputs the system's OWN check ran, as far as
the shipped artifact records them, (b) the exact overlap of those inputs with ours (whole input:
all arguments; for CLIs argv + stdin + contents of argv-named files), (c) for every counted
wrong answer / crash, whether its diverging inputs lie outside the system's input set, and (d)
where the system's inputs are shipped and runnable, whether OUR C-vs-Rust comparison already
diverges on them (a weak check) or only on new inputs.

The runnable parts are measured by helpers that write their own result files:
  harness/heldout_vert.py        -> results/heldout_vert.json        (crust-stable, arm64)
  harness/heldout_c2saferrust.py -> results/heldout_c2saferrust.json (crust-heldout, x86-64)
  harness/heldout_rustassure.py  -> results/heldout_rustassure.json  (crust-heldout, x86-64)
  harness/heldout_rustmap.py     -> results/heldout_rustmap.json     (crust-syzygy, x86-64)
This script reads those, adds the static (file-level) measurements for Flourine, Syzygy and VERT's
benchmark-wide harness facts, and writes results/heldout_overlap.json.

  python3 -I harness/heldout_overlap.py            (host python; reads artifacts/ and results/)
"""
import csv
import datetime
import glob
import hashlib
import json
import os
import platform
import re
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ART = os.path.join(REPO, "artifacts")
RES = os.path.join(REPO, "results")
OUT = os.path.join(RES, "heldout_overlap.json")


def load(name):
    p = os.path.join(RES, name)
    return json.load(open(p)) if os.path.isfile(p) else None


def sha_file(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def git(*a, cwd=REPO):
    try:
        return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True).stdout.strip()
    except Exception:  # noqa: BLE001
        return None


# ----------------------------------------------------------------------------------- VERT
def vert_static():
    tc = os.path.join(ART, "vert", "benchmark", "c_transcoder")
    funcs = sorted(os.listdir(tc))
    assumes, gens, corpora, unwind = {}, [], [], {}
    for f in funcs:
        mut = os.path.join(tc, f, "out-rwasm-mutated", "src", "main.rs")
        bol = os.path.join(tc, f, "out-rwasm-bolero", "src", "main.rs")
        if os.path.isfile(mut):
            s = open(mut, errors="replace").read()
            for a in re.findall(r"kani::assume\((.*?)\);", s):
                assumes[a] = assumes.get(a, 0) + 1
            for u in re.findall(r"#\[kani::unwind\((\d+)\)\]", s):
                unwind[u] = unwind.get(u, 0) + 1
        if os.path.isfile(bol) and ".with_generator(" in open(bol, errors="replace").read():
            gens.append(f)
        cd = os.path.join(tc, f, "out-rwasm-bolero", "src", "__fuzz__", "bolero_wasm_eq", "corpus")
        if os.path.isdir(cd):
            corpora.append({"function": f, "files": len(os.listdir(cd))})
    ev = open(os.path.join(ART, "vert", "evaluation.py")).read()
    return {"benchmarks": len(funcs), "kani_assume_forms": assumes, "kani_unwind_attrs": unwind,
            "bolero_harnesses_with_restricted_generator": gens, "bolero_corpora": corpora,
            "evaluation_commands": re.findall(r'command = "([^"]+)"', ev)}


def vert_block(hv):
    sc = [r for r in hv["functions"] if r["kind"] == "scalar"]
    ar = [r for r in hv["functions"] if r["kind"] != "scalar"]
    wrong = [r for r in sc if r["counted_as"] == "wrong_answer"]
    st = vert_static()
    return {
        "system_check_inputs": {
            "bounded_kani": "per function: rWasm oracle on PARAM1 in {0,1} with the other arguments fixed at the C "
                            "stub's literals; Rust translation on the static initial values (12, 12, ...). "
                            "Parsed from each shipped out-rwasm-mutated/src/main.rs and <F>_towasm_mutated.c.",
            "bolero": "evaluation.py runs `cargo bolero reduce bolero_wasm_eq`, which replays only a saved corpus; "
                      f"{len(st['bolero_corpora'])} of {st['benchmarks']} c_transcoder benchmarks ship one "
                      f"({', '.join(c['function'] for c in st['bolero_corpora'])}); none of the counted functions does",
            "unbounded_kani": "symbolic; no concrete inputs recorded",
            "transcoder_test_arrays": "shipped in <F>.c (10 tuples; the loop breaks after the first); not executed by "
                                      "VERT's evaluation.py; included as recorded inputs",
            "static": st,
        },
        "our_inputs": "80-tuple grid per scalar function (enumerate_cases, nonneg, |x|<=512, spread); 240-row cap for arrays",
        "overlap_scalar_counted": {
            "functions": len(sc),
            "our_inputs_total": sum(r["our_inputs"] for r in sc),
            "in_kani_oracle_side": sum(len(r["overlap"]["ours_vs_kani_oracle_side"] or []) for r in sc),
            "in_kani_rust_side": sum(len(r["overlap"]["ours_vs_kani_rust_side"] or []) for r in sc),
            "in_transcoder_tests": sum(len(r["overlap"]["ours_vs_transcoder_tests"] or []) for r in sc),
            "per_function": {r["function"]: {"kani_oracle_side": r["overlap"]["ours_vs_kani_oracle_side"],
                                             "kani_rust_side": r["overlap"]["ours_vs_kani_rust_side"],
                                             "transcoder_tests": r["overlap"]["ours_vs_transcoder_tests"]} for r in sc},
        },
        "reproduction_check": {
            "scalar_all_reproduced": all(r["reproduced"]["value"] == r["reproduced"]["expected_value"]
                                         and r["reproduced"]["rust_hang"] == r["reproduced"]["expected_rust_hang"] for r in sc),
            "array_all_reproduced": all(r["reproduced"]["value"] == r["reproduced"]["expected_value"]
                                        and r["reproduced"]["rust_panic"] == r["reproduced"]["expected_crash"] for r in ar),
            "mismatches": [{"function": r["function"], **r["reproduced"]} for r in hv["functions"]
                           if (r["kind"] == "scalar" and (r["reproduced"]["value"] != r["reproduced"]["expected_value"]
                                                          or r["reproduced"]["rust_hang"] != r["reproduced"]["expected_rust_hang"]))
                           or (r["kind"] != "scalar" and (r["reproduced"]["value"] != r["reproduced"]["expected_value"]
                                                          or r["reproduced"]["rust_panic"] != r["reproduced"]["expected_crash"]))],
        },
        "own_inputs_through_our_comparison": {
            "scalar_wrong_answers": len(wrong),
            "diverge_on_kani_oracle_side_inputs": sum(bool(r["our_comparison_on_vert_inputs"]["kani_oracle_side_diverges"]) for r in wrong),
            "diverge_on_kani_oracle_side_functions": [r["function"] for r in wrong
                                                      if r["our_comparison_on_vert_inputs"]["kani_oracle_side_diverges"]],
            "diverge_on_kani_rust_side_point": sum(any(p["v"] in ("value", "rust_panic", "rust_hang")
                                                       for p in (r["our_comparison_on_vert_inputs"]["kani_rust_side"] or []))
                                                   for r in wrong),
            "diverge_on_any_kani_input": sum(bool(r["our_comparison_on_vert_inputs"]["kani_domain_diverges"]) for r in wrong),
            "diverge_on_transcoder_tests": sum(bool(r["our_comparison_on_vert_inputs"]["transcoder_tests_diverge"]) for r in wrong),
            "diverge_on_first_transcoder_tuple": sum(bool(r["our_comparison_on_vert_inputs"]["transcoder_first_tuple_diverges"]) for r in wrong),
            "array_counted_diverge_on_kani_rust_side_point": sum(any(p["v"] in ("value", "rust_panic")
                                                                     for p in (r["our_comparison_on_vert_inputs"]["kani_rust_side"] or []))
                                                                 for r in ar if not r["counted_as"].startswith("not_counted")),
        },
    }


def vert_defects(hv):
    out = []
    for r in hv["functions"]:
        div = r["our_divergences"]
        cmp_ = r["our_comparison_on_vert_inputs"]
        if r["kind"] == "scalar":
            vs = [p for p in (cmp_["kani_oracle_side"] or [])]
            out.append({
                "system": "VERT", "defect": r["function"], "counted_as": r["counted_as"],
                "our_diverging_inputs": div["count_by_class"],
                "diverging_inputs_in_system_set": div["inside_vert_check_set"],
                "diverging_inputs_outside_system_set": div["outside_vert_check_set"],
                "all_diverging_inputs_outside": len(div["inside_vert_check_set"]) == 0,
                "smallest_diverging_input": div["smallest"][0] if div["smallest"] else None,
                "system_inputs_kani_oracle_side": r["vert_inputs"]["kani_oracle_side"],
                "our_comparison_on_kani_oracle_side": [{"in": p["in"], "verdict": p["v"], "c": p.get("c"), "rust": p.get("rust")} for p in vs],
                "our_comparison_on_kani_rust_side": [{"in": p["in"], "verdict": p["v"], "c": p.get("c"), "rust": p.get("rust")}
                                                     for p in (cmp_["kani_rust_side"] or [])],
                "visible_on_system_inputs": {"kani_n_in_0_1": cmp_["kani_oracle_side_diverges"],
                                             "kani_rust_point": any(p["v"] in ("value", "rust_panic", "rust_hang") for p in (cmp_["kani_rust_side"] or [])),
                                             "transcoder_tests": cmp_["transcoder_tests_diverge"]},
            })
        else:
            out.append({
                "system": "VERT", "defect": r["function"], "counted_as": r["counted_as"],
                "our_diverging_inputs": div["count_by_class"],
                "diverging_inputs_equal_to_kani_rust_point": div["equal_to_kani_rust_side"],
                "all_diverging_inputs_outside": div["equal_to_kani_rust_side"] == 0,
                "note": "array/float function: Kani oracle-side input is not a C input (see heldout_vert.json); "
                        "bolero ships no corpus; so the recorded set is the Rust-side point only",
                "smallest_diverging_input": div["smallest"][0] if div["smallest"] else None,
                "visible_on_system_inputs": {"kani_rust_point": any(p["v"] in ("value", "rust_panic")
                                                                    for p in (cmp_["kani_rust_side"] or []))},
            })
    return out


# ----------------------------------------------------------------------------------- Flourine
URL_LOGS = "https://d34gtk3knhjgeg.cloudfront.net/translations.tar.gz"


def head_length(url):
    """HTTP HEAD only (no body is downloaded)."""
    try:
        h = subprocess.run(["curl", "-sI", "--max-time", "20", url], capture_output=True, text=True).stdout
        m = re.search(r"content-length:\s*(\d+)", h, re.I)
        return int(m.group(1)) if m else None
    except Exception:  # noqa: BLE001
        return None


def flourine_block():
    fl = os.path.join(ART, "flourine_dl", "flourine-artifact")
    readme = open(os.path.join(fl, "README")).read()
    tarlist = subprocess.run(["tar", "tzf", os.path.join(ART, "flourine_dl", "artifact.tar.gz")],
                             capture_output=True, text=True).stdout.splitlines()
    ans = [p for p in tarlist if p.endswith(".ans")]
    ce = [p for p in tarlist if "counter_examples" in p or "positive_examples" in p]
    fuzz = [p for p in tarlist if "__fuzz__" in p or "/corpus/" in p]
    rows = list(csv.reader(open(os.path.join(fl, "all_statistics", "new_big_table.csv"))))
    hdr = rows[0]
    idx = {h: i for i, h in enumerate(hdr)}

    def status(bench, llm, strat):
        for r in rows[1:]:
            if r[idx["benchmark_name"]] == bench and r[idx["llm"]] == llm and r[idx["feedback_strategy"]] == strat:
                return r[idx["final_status"]], r[idx["status_detail"]]
        return None
    counted = [("opl_emu_registers_operator_list", "claude2", "BaseRepair", "wrong_answer"),
               ("opl_emu_registers_operator_list", "claude2", "CAPR", "wrong_answer"),
               ("opl_emu_registers_lfo_pm_depth", "claude3", "BaseRepair", "wrong_answer"),
               ("clip", "claude2", "BaseRepair", "not_counted:degenerate_range"),
               ("clip", "claude2", "CAPR", "not_counted:degenerate_range"),
               ("opl_emu_clamp", "claude2", "BaseRepair", "not_counted:degenerate_range")]
    orc = open(os.path.join(fl, "oracle.py")).read().splitlines()
    sem = open(os.path.join(fl, "semantics.py")).read().splitlines()
    drv = open(os.path.join(fl, "driver.py")).read().splitlines()

    def find(lines, pat):
        return [i + 1 for i, l in enumerate(lines) if re.search(pat, l)]
    return {
        "system_check_inputs": {
            "fuzzer": "cargo bolero test (libFuzzer engine), oracle.py verify(); inputs generated at run time",
            "oracle_py_lines": {"cargo bolero test": find(orc, r"cargo bolero test"),
                                "writes counter_examples.json only if result_path": find(orc, r"counter_examples\.json"),
                                "soft_verify replays recorded I/O examples": find(orc, r"def soft_verify")},
            "semantics_py_lines": {"requires_verification (fresh fuzz only without examples)": find(sem, r"requires_verification")},
            "driver_py_lines": {"re-verify after repair (fresh fuzz)": find(drv, r"Verifying again")},
            "shipped_in_local_artifact": {"files_in_tarball": len(tarlist), "prog_ans_logs": len(ans),
                                          "counter/positive_examples files": len(ce), "fuzz corpora": len(fuzz)},
            "readme_says_logs_are_separate": "translations.tar.gz" in readme and "wspace/logs/prog_*.ans" in readme,
            "separate_logs_download": {"url": URL_LOGS,
                                       "content_length_bytes_from_HEAD": head_length(URL_LOGS),
                                       "fetched": False,
                                       "why_not": "multi-GB third-party download; not downloaded"},
        },
        "status": "not measurable from the shipped artifact alone; measured from the separately downloaded logs "
                  "(results/heldout_flourine.json) where available",
        "counted_translations_status_in_flourine_table": [
            {"benchmark": b, "llm": l, "strategy": s, "our_count": k, "flourine_status": status(b, l, s)} for b, l, s, k in counted],
        "overlap": None,
    }


# ----------------------------------------------------------------------------------- Syzygy
def syzygy_block():
    sz = os.path.join(ART, "syzygy_zopfli")
    files = sorted(os.path.relpath(p, sz) for p in glob.glob(os.path.join(sz, "**", "*"), recursive=True)
                   if os.path.isfile(p) and "/target/" not in p and "/.git/" not in p)
    re_ = open(os.path.join(sz, "run_equiv.py")).read()
    rs = open(os.path.join(sz, "rust_code", "src", "main.rs"), errors="replace").read()
    cz = open(os.path.join(sz, "c_code", "zopfli.c"), errors="replace").read()
    log = git("log", "--all", "--format=%h %s", cwd=sz)
    gen_like = [f for f in files if re.search(r"gen|fuzz|random|test_", os.path.basename(f), re.I)]
    return {
        "system_check_inputs": {
            "run_equiv.py": {"TEST_DIR": re.search(r'TEST_DIR = "([^"]+)"', re_).group(1),
                             "N_TESTS": int(re.search(r"N_TESTS = (\d+)", re_).group(1)),
                             "compares": "stderr of the C and Rust drivers only (ret1 = o1.stderr.decode())"},
            "rust_cfg_test": {"reads": "tests_folder/test_{i}", "TOTAL_TESTS": int(re.search(r"const TOTAL_TESTS: usize = (\d+);", rs).group(1))},
            "c_driver_length": {
                "single_test_uses_strlen": "size_t insize = strlen(in);" in cz,
                "read_stdin_to_bytes_writes_terminator": bool(re.search(
                    r"buffer\[total_read\]\s*=\s*(0|'\\0')\s*;",
                    cz[cz.index("unsigned char *read_stdin_to_bytes"):cz.index("int main()")]))},
            "files_in_artifact": files,
            "generator_or_test_files_in_artifact": gen_like,
            "git_history": log.splitlines() if log else None,
        },
        "status": "not measurable: neither the test files (/dev/test_folder/test_<i>, tests_folder/test_<i>) "
                  "nor a generator is in the artifact",
        "empty_input_producible_by_shipped_generator": "not measurable (no generator shipped)",
    }


# ----------------------------------------------------------------------------------- per-system blocks
def c2sr_block(hc):
    si = hc["system_inputs"]
    return {
        "system_check_inputs": "every invocation of cat/head/tail/uniq made by tests/<util>/*.sh, traced with a "
                               "logging shim under translate.py's procedure (heldout_c2saferrust.json)",
        "traced": si,
        "our_inputs": hc["our_inputs"],
        "overlap": {"distinct_inputs_in_both": hc["overlap"]["distinct_inputs_in_both"], "items": hc["overlap"]["items"]},
        "counted_defects": {k: hc["counted_defects"][k] for k in ("total", "by_kind", "inside_system_input_set",
                                                                    "outside_system_input_set")},
        "own_inputs_through_our_comparison": {"all_sh_tests": {k: si["all_sh_tests"][k] for k in si["all_sh_tests"]},
                                              "acceptance_set": {k: si["acceptance_set_tests"][k] for k in si["acceptance_set_tests"]}},
    }


def rustassure_block(ha):
    oc = ha["our_comparison_on_rustassure_inputs"]
    return {
        "system_check_inputs": ha["system_check"],
        "our_inputs": ha["our_inputs"],
        "overlap": ha["overlap"],
        "own_inputs_through_our_comparison": {k: oc[k] for k in ("runs", "diverge", "diverge_null_vs_empty", "diverging_tests")},
        "own_check_run_unchanged": {p: {k: v.get(k) for k in ("built", "exit", "headers_reached", "last_header", "reached_test_19", "stderr_tail")}
                                    for p, v in ha.get("own_check_run", {}).items()},
        "counted_defects": {k: ha["counted_defects"][k] for k in ("reproduced", "inside_rustassure_set", "outside_rustassure_set")},
    }


def shipped_bz2_static():
    """Classify every shipped .bz2 under bzip2_testcases by its bytes (no execution)."""
    out = {}
    base = os.path.join(ART, "rustmap", "rust-code", "bzip2_testcases")
    for p in sorted(glob.glob(os.path.join(base, "**", "*.bz2"), recursive=True)):
        b = open(p, "rb").read()
        lz = len(b) - len(b.lstrip(b"\0"))
        kind = ("bzip2 stream at offset 0" if b[:3] == b"BZh" else
                "bzip2 stream after 5000 zero bytes" if lz == 5000 and b[5000:5003] == b"BZh" else
                "not bzip2: macOS Finder metadata ('Bud1' magic)" if b[4:8] == b"Bud1" else "other")
        out[os.path.relpath(p, base)] = {"size": len(b), "leading_zero_bytes": lz, "kind": kind}
    return out


def rustmap_block(hm):
    b = hm["bzip2"]
    return {
        "bzip2": {
            "system_check_inputs": b["system_inputs"],
            "our_inputs": len(b["our_inputs"]),
            "overlap_exact": b["overlap_exact"],
            "own_inputs_through_our_comparison": [
                {k: r[k] for k in ("input", "size", "c_roundtrip_ok", "rust_rc", "rust_panicked", "rust_passes_rustmap_own_check",
                                   "plain_bzip2_d_accepts_rust_output", "rust_bz2_equals_c_bz2", "rust_bz2_leading_zero_bytes")}
                for r in b["our_comparison_on_system_inputs"]],
            "shipped_bz2_files": shipped_bz2_static(),
        },
        "rosetta": hm["rosetta"],
    }


def summary(hv, hc, ha, hm):
    out = {}
    if hv:
        DIV = ("value", "rust_panic", "rust_hang")
        sc = [r for r in hv["functions"] if r["kind"] == "scalar"]
        w = [r for r in sc if r["counted_as"] == "wrong_answer"]
        hang = [r for r in sc if r["counted_as"] == "rust_hang"]
        ar = [r for r in hv["functions"] if r["kind"] != "scalar" and not r["counted_as"].startswith("not_counted")]

        def any_div(lst):
            return any(p["v"] in DIV for p in (lst or []))
        out["VERT_scalar_wrong_answers"] = {
            "functions": len(w),
            "one_argument_functions": sum(r["nargs"] == 1 for r in w),
            "our_inputs": sum(r["our_inputs"] for r in w),
            "our_inputs_in_kani_oracle_side_domain": sum(len(r["overlap"]["ours_vs_kani_oracle_side"]) for r in w),
            "functions_with_overlap_kani_oracle_side": sum(bool(r["overlap"]["ours_vs_kani_oracle_side"]) for r in w),
            "our_inputs_equal_to_kani_rust_side_point": sum(len(r["overlap"]["ours_vs_kani_rust_side"]) for r in w),
            "functions_with_bolero_corpus": sum(r["vert_inputs"]["bolero_corpus_files"] > 0 for r in w),
            "our_inputs_in_transcoder_test_tuples": sum(len(r["overlap"]["ours_vs_transcoder_tests"]) for r in w),
            "functions_diverging_outside_vert_inputs": sum(r["our_divergences"]["outside_vert_check_set"] > 0 for r in w),
            "functions_with_a_diverging_input_inside_vert_inputs": [r["function"] for r in w if r["our_divergences"]["inside_vert_check_set"]],
            "diverge_under_our_comparison_on_kani_oracle_side_inputs": [
                {"function": r["function"], "inputs": [[p["in"], p["v"], p.get("c"), p.get("rust")] for p in r["our_comparison_on_vert_inputs"]["kani_oracle_side"] if p["v"] in DIV]}
                for r in w if any_div(r["our_comparison_on_vert_inputs"]["kani_oracle_side"])],
            "kani_oracle_side_inputs_where_c_has_ub": sum(sum(p["v"] == "ub" for p in r["our_comparison_on_vert_inputs"]["kani_oracle_side"]) for r in w),
            "diverge_on_kani_rust_side_point": sum(any_div(r["our_comparison_on_vert_inputs"]["kani_rust_side"]) for r in w),
            "diverge_on_any_kani_input": sum(any_div(r["our_comparison_on_vert_inputs"]["kani_oracle_side"]) or any_div(r["our_comparison_on_vert_inputs"]["kani_rust_side"]) for r in w),
            "diverge_on_transcoder_test_tuples": sum(any_div(r["our_comparison_on_vert_inputs"]["transcoder_tests"]) for r in w),
            "diverge_on_first_transcoder_tuple": sum(bool(r["our_comparison_on_vert_inputs"]["transcoder_first_tuple_diverges"]) for r in w),
        }
        out["VERT_scalar_rust_hangs"] = [{"function": r["function"], "diverging_inputs": len(r["our_divergences"]["inputs"]),
                                          "inside_vert_inputs": r["our_divergences"]["inside_vert_check_set"],
                                          "outside_vert_inputs": r["our_divergences"]["outside_vert_check_set"]} for r in hang]
        out["VERT_array_counted"] = {
            "functions": len(ar),
            "diverging_inputs_equal_to_kani_rust_side_point": sum(r["our_divergences"]["equal_to_kani_rust_side"] for r in ar),
            "diverge_on_kani_rust_side_point": sum(any(p["v"] in ("value", "rust_panic") for p in (r["our_comparison_on_vert_inputs"]["kani_rust_side"] or [])) for r in ar),
            "functions_with_bolero_corpus": sum(r["vert_inputs"]["bolero_corpus_files"] > 0 for r in ar),
        }
    if hc:
        si = hc["system_inputs"]
        out["C2SaferRust"] = {"sh_tests": si["all_sh_tests"]["tests"], "acceptance_set_tests": si["acceptance_set_tests"]["tests"],
                              "traced_invocations": si["all_sh_tests"]["invocations"],
                              "traced_invocations_acceptance_set": si["acceptance_set_tests"]["invocations"],
                              "distinct_traced_inputs": si["all_sh_tests"]["distinct_inputs"],
                              "our_cases": hc["our_inputs"]["cases"], "overlap": hc["overlap"]["distinct_inputs_in_both"],
                              "counted_divergences": hc["counted_defects"]["total"],
                              "counted_divergences_inside_system_inputs": hc["counted_defects"]["inside_system_input_set"],
                              "replay_mismatch_all_tests": si["all_sh_tests"]["replay_mismatch"],
                              "replay_mismatch_by_kind_all_tests": si["all_sh_tests"]["replay_mismatch_by_kind"],
                              "replay_mismatch_only_in_c2saferrust_output": si["all_sh_tests"]["replay_mismatch_only_in_c2saferrust_output"],
                              "replay_mismatch_acceptance_set": si["acceptance_set_tests"]["replay_mismatch"],
                              "replay_mismatch_acceptance_set_tests": si["acceptance_set_tests"]["tests_with_replay_mismatch"],
                              "test_outcomes_match_acceptance_suite": all(
                                  (t["test_exit_with_shim"] == 0) == (t["acceptance_suite_wip_pass_runs"] == 3) for t in hc["per_test"].values())}
    if ha:
        out["RustAssure_libcsv"] = {"system_inputs": ha["system_check"]["distinct_inputs"],
                                    "system_inputs_with_CSV_EMPTY_IS_NULL": ha["system_check"]["with_CSV_EMPTY_IS_NULL"],
                                    "our_cases": ha["our_inputs"]["cases"], "overlap": ha["overlap"]["exact_input_and_options"],
                                    "overlap_items": ha["overlap"]["items"],
                                    "counted_divergences": ha["counted_defects"]["reproduced"],
                                    "counted_divergences_inside_system_inputs": ha["counted_defects"]["inside_rustassure_set"],
                                    "system_inputs_diverging_under_our_comparison": ha["our_comparison_on_rustassure_inputs"]["diverging_tests"],
                                    "own_composed_test_run": {p: {k: v.get(k) for k in ("exit", "headers_reached", "last_header", "reached_test_19")}
                                                              for p, v in ha.get("own_check_run", {}).items()}}
    if hm:
        b = hm["bzip2"]
        rows = b["our_comparison_on_system_inputs"]
        out["RustMap_bzip2"] = {"system_inputs": len(b["system_inputs"]), "our_inputs": len(b["our_inputs"]),
                                "overlap": len(b["overlap_exact"]),
                                "counted_defects": len(b["counted_defects"]),
                                "counted_defects_inside_system_inputs": sum(r["in_system_input_set"] for r in b["counted_defects"]),
                                "system_inputs_passing_rustmap_own_check": sum(r["rust_passes_rustmap_own_check"] is True for r in rows),
                                "system_inputs_rust_not_finished_60s": sum(r["rust_rc"] == 124 for r in rows),
                                "system_inputs_rust_output_rejected_by_plain_bzip2_d": sum((r["plain_bzip2_d_accepts_rust_output"] or {}).get("rc", 0) != 0 for r in rows),
                                "system_inputs_rust_output_with_5000_leading_zero_bytes": sum(r["rust_bz2_leading_zero_bytes"] == 5000 for r in rows)}
        out["RustMap_rosetta"] = {"runner_exec_lines": [hm["rosetta"]["c_runner_exec_lines"], hm["rosetta"]["rust_runner_exec_lines"]],
                                  "c_programs_indexing_argv": sorted(hm["rosetta"]["c_programs_that_read_stdin_or_argv"]),
                                  "status": "in-distribution: same runs (no arguments, no stdin redirection) as RustMap's scripts"}
    hf = load("heldout_flourine.json")
    if hf:
        rows = hf["translations"]
        sc = [r for r in rows if "shipped_divergent_iso_c_clean" in r]
        out["Flourine"] = {
            "translations_checked": len(rows),
            "shipped_equals_first_candidate_that_flourine_rejected": sum(r["identity"]["shipped_equals_first_candidate_prog_0"]
                                                                        and not r["identity"]["shipped_equals_verified_candidate"] for r in rows),
            "logged_inputs_per_scalar_translation": {r["translation"]: r["logged_inputs"] for r in sc},
            "overlap_ours_logged": sum(len(r["overlap_ours_logged"]) for r in sc),
            "counted_divergences_iso_c_clean": {r["translation"]: r["shipped_divergent_iso_c_clean"] for r in sc},
            "counted_divergences_inside_logged_inputs": sum(len(r["counted_divergences_inside_logged_inputs"]) for r in sc),
            "shipped_diverges_on_logged_inputs_iso_c_clean": {r["translation"]: r["shipped_diverges_on_logged_inputs_iso_c_clean"] for r in sc},
        }
    else:
        out["Flourine"] = "not measurable: fuzz inputs not in the shipped artifact"
    out["Syzygy"] = "not measurable: test inputs and generator not in the shipped artifact"
    return out


def main():
    hv, hc, ha, hm = (load("heldout_vert.json"), load("heldout_c2saferrust.json"),
                      load("heldout_rustassure.json"), load("heldout_rustmap.json"))
    systems, defects = {}, []
    if hv:
        systems["VERT"] = vert_block(hv)
        defects += vert_defects(hv)
    systems["Flourine"] = flourine_block()
    hf = load("heldout_flourine.json")
    if hf:
        systems["Flourine"]["from_logs"] = {
            "archive": hf["provenance"]["logs_archive"],
            "logged_inputs": "only the input/output examples quoted in the counterexample-fix prompts (wspace/logs/prog_*_*.ans); "
                             "the fuzzer's example sets (up to 1,000 per run, oracle.py n_counter_examples) are not saved",
            "per_translation": [{k: r.get(k) for k in (
                "translation", "label", "flourine_log_events", "logged_inputs", "overlap_ours_logged",
                "shipped_divergent_iso_c_clean", "counted_divergences_inside_logged_inputs",
                "counted_divergences_outside_logged_inputs", "shipped_diverges_on_logged_inputs_iso_c_clean",
                "final_verification_coverage_count_on_fn_line")} | {
                "shipped_equals_first_candidate_prog_0": r["identity"]["shipped_equals_first_candidate_prog_0"],
                "shipped_equals_verified_candidate": r["identity"]["shipped_equals_verified_candidate"]}
                for r in hf["translations"]],
        }
        systems["Flourine"]["status"] = "measured from the logs for the 6 listed translations"
    fsl = load("flourine_shipped_vs_log.json")
    if fsl:
        systems["Flourine"]["shipped_vs_log_all_tested_success_files"] = fsl["totals"]
    fl_div = load("flourine_diff.json")["success_divergences"]
    fs_div = load("flourine_struct.json")["success_divergences"]
    hfr = {r["translation"]: r for r in (hf or {}).get("translations", [])}
    for d in fl_div + fs_div:
        t = d["t"]
        k = ("wrong_answer" if "operator_list" in t or "lfo_pm_depth" in t else "not_counted:degenerate_range")
        key = next((x for x in hfr if t.startswith(x) or t.startswith(x.split("/", 1)[1])), None)
        r = hfr.get(key)
        row = {"system": "Flourine", "defect": t, "counted_as": k, "our_diverging_inputs": d["value"],
               "smallest_diverging_input": d["samples"][0] if d["samples"] else None}
        if r and "shipped_divergent_iso_c_clean" in r:
            row.update({"our_diverging_inputs_current_grid_iso_c_clean": r["shipped_divergent_iso_c_clean"],
                        "logged_inputs": r["logged_inputs"],
                        "all_diverging_inputs_outside": not r["counted_divergences_inside_logged_inputs"],
                        "visible_on_system_inputs": r["shipped_diverges_on_logged_inputs_iso_c_clean"] > 0,
                        "shipped_translation_is_flourine_first_rejected_candidate": r["identity"]["shipped_equals_first_candidate_prog_0"]
                        and not r["identity"]["shipped_equals_verified_candidate"]})
        elif r:
            row.update({"all_diverging_inputs_outside": "not measured (struct harness)",
                        "shipped_translation_is_flourine_first_rejected_candidate": r["identity"]["shipped_equals_first_candidate_prog_0"]
                        and not r["identity"]["shipped_equals_verified_candidate"]})
        else:
            row.update({"all_diverging_inputs_outside": "not measurable", "visible_on_system_inputs": "not measurable"})
        defects.append(row)
    if ha:
        systems["RustAssure"] = rustassure_block(ha)
        for r in ha["counted_defects"]["rows"]:
            defects.append({"system": "RustAssure", "defect": f"libcsv {r['input']} / {r['options']}", "counted_as": "wrong_answer",
                            "tags": r["tags"], "all_diverging_inputs_outside": not r["in_rustassure_set"]})
    if hc:
        systems["C2SaferRust"] = c2sr_block(hc)
        cd = hc["counted_defects"]
        defects.append({"system": "C2SaferRust", "defect": "all counted cat/head/tail/uniq divergences (results/<u>_diff_v4.json)",
                        "counted_as": cd["by_kind"], "count": cd["total"],
                        "inside_system_input_set": cd["inside_system_input_set"],
                        "all_diverging_inputs_outside": cd["inside_system_input_set"] == 0,
                        "inside_ids": cd["inside_ids"]})
    systems["Syzygy"] = syzygy_block()
    defects.append({"system": "Syzygy", "defect": "empty input to ZopfliDeflate (results/syzygy_empty_deflate.json)",
                    "counted_as": "wrong_answer", "all_diverging_inputs_outside": "not measurable (inputs not shipped)",
                    "visible_on_system_inputs": "not measurable"})
    if hm:
        systems["RustMap"] = rustmap_block(hm)
        for r in hm["bzip2"]["counted_defects"]:
            defects.append({"system": "RustMap", "defect": f"bzip2 {r['input']}", "counted_as": r["kind"],
                            "all_diverging_inputs_outside": not r["in_system_input_set"]})
        tri = load("rustmap_rosetta_triage.json")
        for name in list(tri["wrong_output_S1"]) + list(tri["rust_panic"]):
            defects.append({"system": "RustMap", "defect": f"Rosetta {name}",
                            "counted_as": "wrong_answer" if name in tri["wrong_output_S1"] else "rust_panic",
                            "all_diverging_inputs_outside": False,
                            "note": "in-distribution by construction: the same run (no arguments, no stdin) RustMap's "
                                    "documented whole-log diff performs"})

    out = {
        "provenance": {
            "script": "harness/heldout_overlap.py",
            "command": "python3 -I harness/heldout_overlap.py",
            "git_rev": git("rev-parse", "HEAD"),
            "git_dirty": bool(git("status", "--porcelain")),
            "host": platform.system(), "arch": platform.machine(), "python": platform.python_version(),
            "date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "helper_results": {n: (load(n) or {}).get("provenance") for n in
                               ("heldout_vert.json", "heldout_c2saferrust.json", "heldout_rustassure.json", "heldout_rustmap.json",
                                "heldout_flourine.json")},
            "artifact_hashes": {"flourine artifact.tar.gz": sha_file(os.path.join(ART, "flourine_dl", "artifact.tar.gz")),
                                "vert c_results.csv": sha_file(os.path.join(ART, "vert", "c_results.csv")),
                                "syzygy git HEAD": git("rev-parse", "HEAD", cwd=os.path.join(ART, "syzygy_zopfli")),
                                "rustassure git HEAD": git("rev-parse", "HEAD", cwd=os.path.join(ART, "rustassure")),
                                "vert git HEAD": git("rev-parse", "HEAD", cwd=os.path.join(ART, "vert"))},
        },
        "definition": "An input is 'in the system's input set' if the system's own shipped check ran exactly that "
                      "input (all arguments equal; for CLIs argv, stdin bytes and the contents of argv-named files "
                      "equal). 'Outside' means not in that set. Where the artifact does not record the inputs its "
                      "check ran, the status is 'not measurable'.",
        "summary": summary(hv, hc, ha, hm),
        "systems": systems,
        "counted_defects": defects,
    }
    dest = os.environ.get("OUT", OUT)
    json.dump(out, open(dest, "w"), indent=1)
    print("wrote", dest, "systems:", list(systems), "defect rows:", len(defects))


if __name__ == "__main__":
    main()
