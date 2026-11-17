"""M2 (held-out measurement), RustAssure / libcsv part.

RustAssure's per-function check runs KLEE with every argument made symbolic; its Symbolizer pass
does not initialise function pointers (src/Symbolizer/Pass/SymbolizerPass.cpp, "we don't
initialize function pointer"), so csv_parse's field callback is never driven by that check and it
records no concrete inputs (no .ktest files are shipped). The only shipped check that exercises the
libcsv COMPOSITION we test is the Rust test program of the composed crate,
src/Link/csv_link/src/main.rs: `main()` calls `test_all_parse()`, a port of libcsv's test_csv.c that
feeds fixed byte strings with fixed option sets to csv_parse/csv_fini and compares the callback
events with hand-written expected events (length and bytes only; cb1 compares
`slice::from_raw_parts(expected_ptr, len)` with the data slice, so a NULL expected field and an
empty field compare equal).

This script
  1. extracts every (input bytes, options, delimiter, quote, space/term functions) that
     test_all_parse() actually runs,
  2. computes the exact overlap with OUR 40 inputs x 9 option sets (gen_corpus.py),
  3. runs RustAssure's own inputs through OUR comparison (systems/rustassure/libcsv_e2e: original
     libcsv C vs the shipped composition, full event stream including NULL-vs-empty),
  4. records, for each of our 12 counted divergences, whether its (input, options) is in
     RustAssure's set.

  docker run --rm -v "$PWD:/work" -w /work crust-heldout python3 -I harness/heldout_rustassure.py
Writes results/heldout_rustassure.json.
"""
import datetime
import hashlib
import json
import os
import platform
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
E2E = os.path.join(REPO, "systems", "rustassure", "libcsv_e2e")
sys.path.insert(0, E2E)
import run as e2e  # noqa: E402  (systems/rustassure/libcsv_e2e/run.py)
from gen_corpus import corpus, OPTIONS  # noqa: E402

MAIN = os.path.join(REPO, "artifacts", "rustassure", "src", "Link", "csv_link", "src", "main.rs")
OUT = os.path.join(REPO, "results", "heldout_rustassure.json")
CONST = {"CSV_STRICT": 1, "CSV_REPALL_NL": 2, "CSV_STRICT_FINI": 4, "CSV_APPEND_NULL": 8,
         "CSV_EMPTY_IS_NULL": 16, "0": 0}


def rust_bytes(lit):
    """Decode the body of a Rust byte-string literal b"..."."""
    out, i = bytearray(), 0
    while i < len(lit):
        ch = lit[i]
        if ch != "\\":
            out += ch.encode("latin1"); i += 1; continue
        nxt = lit[i + 1]
        if nxt == "x":
            out.append(int(lit[i + 2:i + 4], 16)); i += 4
        elif nxt == "\n":          # line continuation: skip newline and leading whitespace
            i += 2
            while i < len(lit) and lit[i] in " \t\r\n":
                i += 1
        else:
            out.append({"n": 10, "r": 13, "t": 9, "\\": 92, '"': 34, "'": 39, "0": 0}[nxt]); i += 2
    return bytes(out)


def parse_tests(src):
    body = re.search(r"fn test_all_parse\(\)\s*\{(.*?)\n\}", src, re.S).group(1)
    called = []
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("//"):
            continue
        m = re.match(r"(test_\w+)\(\);", s)
        if m:
            called.append(m.group(1))
    tests = []
    for name in called:
        m = re.search(r"fn " + name + r"\(\)\s*\{(.*?)\n\}", src, re.S)
        fb = m.group(1)
        dm = re.search(r'let data\s*(?::\s*&\[u8\])?\s*=\s*b"((?:[^"\\]|\\.|\\\n)*)"', fb, re.S)
        tp = re.search(r"test_parser\(\s*test_name\s*,\s*([^,]+?)\s*,\s*data\s*,\s*&\s*mut\s+result\s*,\s*([^,]+?)\s*,"
                       r"\s*([^,]+?)\s*,\s*([^,]+?)\s*,\s*([^)]+?)\s*\)", fb, re.S)
        opts = sum(CONST[t.strip()] for t in tp.group(1).split("|"))
        tests.append({"test": name, "data": rust_bytes(dm.group(1)), "options": opts,
                      "options_expr": tp.group(1).strip(), "delim": tp.group(2).strip(), "quote": tp.group(3).strip(),
                      "space_func": tp.group(4).strip(), "term_func": tp.group(5).strip()})
    return called, tests


def run_own_check():
    """Build and run RustAssure's own composed test program (csv_link main -> test_all_parse) unchanged,
    debug and release, and split its output per test header ("test 19" ...)."""
    import shutil
    import tempfile
    work = tempfile.mkdtemp(prefix="ra_own_")
    crate = os.path.join(work, "csv_link")
    shutil.copytree(os.path.dirname(os.path.dirname(MAIN)), crate, ignore=shutil.ignore_patterns("target"))
    res = {}
    for prof, flags in (("debug", []), ("release", ["--release"])):
        b = subprocess.run(["cargo", "build"] + flags, cwd=crate, capture_output=True, text=True)
        exe = os.path.join(crate, "target", prof, "csv_link_v1")
        if not os.path.isfile(exe):
            res[prof] = {"built": False, "log_tail": b.stderr[-1500:]}; continue
        r = subprocess.run([exe], capture_output=True, text=True, timeout=60)
        per, cur, order = {}, None, []
        for line in r.stdout.splitlines():
            m = re.match(r"test (\w+)\s*$", line.strip())
            if m:
                cur = m.group(1); order.append(cur); per[cur] = {"pass_lines": 0, "fail_lines": 0}; continue
            if cur is None:
                continue
            if re.search(r"error|does not match|unexpected", line):
                per[cur]["fail_lines"] += 1
            elif "pass" in line:
                per[cur]["pass_lines"] += 1
        res[prof] = {"built": True, "exit": r.returncode, "headers_reached": len(order),
                     "last_header": order[-1] if order else None, "reached_test_19": "19" in per,
                     "stderr_tail": r.stderr[-300:], "per_test_header": per,
                     "headers_with_failure_lines": sorted(k for k, v in per.items() if v["fail_lines"])}
    shutil.rmtree(work, ignore_errors=True)
    return res


def main():
    src = open(MAIN, errors="replace").read()
    called, tests = parse_tests(src)
    assert all(t["delim"] == "CSV_COMMA" and t["quote"] == "CSV_QUOTE" for t in tests)
    assert all(t["space_func"] == "noop" and t["term_func"] == "noop" for t in tests)
    # the shipped Rust parser treats space/tab and CR/LF as space/term in addition to the callback
    # (main.rs `is_space(c) != 0 || c == 0x20 || c == 0x09`), so `noop` == libcsv's default; our
    # driver installs exactly that default on both sides.
    sys_set = {(t["data"], t["options"]) for t in tests}
    ours = [(n, d, on, o) for on, o in OPTIONS for n, d in corpus()]
    our_set = {(d, o) for _n, d, _on, o in ours}
    overlap = sorted(sys_set & our_set)
    data_overlap = sorted({t["data"] for t in tests} & {d for _n, d, _on, _o in ours})

    own = run_own_check()
    oracle, sut = e2e.build()
    sys_rows = []
    for t in tests:
        o_out, o_rc = e2e.run_one(oracle, t["options"], t["data"])
        s_out, s_rc = e2e.run_one(sut, t["options"], t["data"])
        tags = e2e.classify(o_out, s_out)
        if o_rc != s_rc:
            tags.add(f"exit({o_rc}vs{s_rc})")
        sys_rows.append({"test": t["test"], "data_hex": t["data"].hex(), "data_repr": repr(t["data"]),
                         "options": t["options"], "options_expr": t["options_expr"],
                         "in_our_set": (t["data"], t["options"]) in our_set,
                         "diverges_under_our_comparison": bool(tags), "tags": sorted(tags),
                         "oracle": o_out.decode("utf-8", "replace").strip().split("\n"),
                         "sut": s_out.decode("utf-8", "replace").strip().split("\n")})
    # our counted divergences (re-run, so the list is exact)
    ours_rows = []
    for n, d, on, o in ours:
        o_out, o_rc = e2e.run_one(oracle, o, d)
        s_out, s_rc = e2e.run_one(sut, o, d)
        tags = e2e.classify(o_out, s_out)
        if o_rc != s_rc:
            tags.add("exit")
        if tags:
            ours_rows.append({"input": n, "options": on, "tags": sorted(tags),
                              "in_rustassure_set": (d, o) in sys_set})

    def ver(cmd, cwd=None):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd).stdout.splitlines()[0]
        except Exception:  # noqa: BLE001
            return None
    out = {
        "provenance": {
            "script": "harness/heldout_rustassure.py",
            "command": "docker run --rm -v \"$PWD:/work\" -w /work crust-heldout python3 -I harness/heldout_rustassure.py",
            "git_rev": os.environ.get("GIT_REV"), "host": os.environ.get("HOST_DESC", platform.system()),
            "arch": platform.machine(), "image": os.environ.get("IMAGE_DESC"),
            "toolchains": {"gcc": ver(["gcc", "--version"]), "rustc": ver(["rustc", "--version"], cwd=e2e.SUT_CRATE)},
            "date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "artifact_file": "artifacts/rustassure/src/Link/csv_link/src/main.rs",
            "artifact_sha256": hashlib.sha256(open(MAIN, "rb").read()).hexdigest(),
        },
        "system_check": {
            "per_function_klee": "symbolic arguments only; function pointers not initialised "
                                 "(SymbolizerPass.cpp lines 396, 429); no concrete inputs or .ktest files shipped",
            "composition_test": "csv_link/src/main.rs test_all_parse(): fixed inputs, events compared by length+bytes",
            "tests_called": len(called), "tests_parsed": len(tests),
            "distinct_inputs": len(sys_set),
            "with_CSV_EMPTY_IS_NULL": sum(bool(t["options"] & 16) for t in tests),
        },
        "own_check_run": own,
        "our_inputs": {"cases": len(ours), "distinct": len(our_set)},
        "overlap": {"exact_input_and_options": len(overlap),
                    "items": [{"data_repr": repr(d), "options": o} for d, o in overlap],
                    "same_bytes_any_options": len(data_overlap),
                    "same_bytes_items": [repr(d) for d in data_overlap]},
        "our_comparison_on_rustassure_inputs": {
            "runs": len(sys_rows),
            "diverge": sum(r["diverges_under_our_comparison"] for r in sys_rows),
            "diverge_null_vs_empty": sum("null_vs_empty_field" in r["tags"] for r in sys_rows),
            "diverging_tests": [r["test"] for r in sys_rows if r["diverges_under_our_comparison"]],
            "rows": sys_rows,
        },
        "counted_defects": {"reproduced": len(ours_rows),
                            "inside_rustassure_set": sum(r["in_rustassure_set"] for r in ours_rows),
                            "outside_rustassure_set": sum(not r["in_rustassure_set"] for r in ours_rows),
                            "rows": ours_rows},
    }
    json.dump(out, open(os.environ.get("OUT", OUT), "w"), indent=1)
    print(json.dumps({k: out[k] for k in ("system_check", "own_check_run", "our_inputs", "overlap", "counted_defects")}, indent=1)[:4000])
    print("diverge on RustAssure inputs:", out["our_comparison_on_rustassure_inputs"]["diverge"],
          out["our_comparison_on_rustassure_inputs"]["diverging_tests"])


if __name__ == "__main__":
    main()
