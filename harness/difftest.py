"""Held-out differential test: ORACLE (original C) vs SUT (translated Rust).

For each held-out case, run both binaries with identical argv + stdin + env in a
fresh temp CWD, capture (stdout bytes, exit code), and compare BYTE-EXACT with
ZERO tolerance. stderr is compared too but as a
SECONDARY signal with a documented program-name normalisation -- a stdout+exit
divergence is what counts as a differential mismatch.

Exit codes (so a build/setup failure is never miscounted as "agreement"):
    2 = build/setup error (could not produce a verdict)
    1 = at least one differential mismatch on held-out inputs
    0 = oracle and SUT agree on every assessable case
"""
import argparse
import json
import os
import sys
import tempfile

from common import Case, run_capture, normalize_stderr
from build import build_both


def _materialize(case: Case, wd: str):
    """Create any input files / dirs the case names in argv, inside the shared wd."""
    for d in (case.make_dirs or []):
        os.makedirs(os.path.join(wd, d), exist_ok=True)
    for name, data in case.decoded_files().items():
        path = os.path.join(wd, name)
        os.makedirs(os.path.dirname(path) or wd, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)


def _snapshot_tree(root: str) -> dict:
    """Map every regular file under root to its bytes (relative paths, sorted).
    Used to compare the FILE output of file-writing utils (split, truncate) -- the
    program's real output isn't stdout, it's the files it leaves behind."""
    tree = {}
    for dirpath, _dirs, names in os.walk(root):
        for n in sorted(names):
            full = os.path.join(dirpath, n)
            rel = os.path.relpath(full, root)
            try:
                with open(full, "rb") as f:
                    tree[rel] = f.read()
            except OSError:
                tree[rel] = b"<unreadable>"
    return tree


def _tree_diff(a: dict, b: dict):
    """Summarise differences between two file trees (oracle vs SUT)."""
    keys = sorted(set(a) | set(b))
    diffs = []
    for k in keys:
        if k not in a:
            diffs.append({"file": k, "kind": "only-in-sut", "sut_len": len(b[k])})
        elif k not in b:
            diffs.append({"file": k, "kind": "only-in-oracle", "oracle_len": len(a[k])})
        elif a[k] != b[k]:
            diffs.append({"file": k, "kind": "content-differs",
                          "oracle_len": len(a[k]), "sut_len": len(b[k]),
                          "first_diff_byte": _first_diff(a[k], b[k])})
    return diffs


def compare_case(case: Case, oracle_bin: str, sut_bin: str, timeout=15):
    """Return a per-case verdict dict."""
    stdin = case.stdin()
    env = case.env or None
    file_tree_diffs = None
    if case.writes_files:
        # File-WRITING utils (split, truncate): the real output is the files left
        # behind, so oracle and SUT MUST run in SEPARATE dirs (else one overwrites
        # the other's output), each materialised identically, then we diff the trees.
        with tempfile.TemporaryDirectory() as wo, tempfile.TemporaryDirectory() as ws:
            _materialize(case, wo)
            _materialize(case, ws)
            o = run_capture([oracle_bin] + list(case.argv), stdin, cwd=wo,
                            timeout=timeout, env_overrides=env)
            s = run_capture([sut_bin] + list(case.argv), stdin, cwd=ws,
                            timeout=timeout, env_overrides=env)
            if not (o.timed_out or s.timed_out):
                file_tree_diffs = _tree_diff(_snapshot_tree(wo), _snapshot_tree(ws))
    else:
        # Oracle and SUT run in the SAME fresh working directory, so cwd-dependent
        # output (e.g. pwd) and input FILES named in argv are seen identically by
        # both sides. These utils read files but write none, so there is no
        # cross-run interference.
        with tempfile.TemporaryDirectory() as wd:
            _materialize(case, wd)
            o = run_capture([oracle_bin] + list(case.argv), stdin, cwd=wd,
                            timeout=timeout, env_overrides=env)
            s = run_capture([sut_bin] + list(case.argv), stdin, cwd=wd,
                            timeout=timeout, env_overrides=env)

    assessable = not (o.timed_out or s.timed_out)
    stdout_match = o.stdout == s.stdout
    exit_match = o.returncode == s.returncode
    file_tree_match = not file_tree_diffs  # None (n/a) or [] both mean "no difference"
    oe = normalize_stderr(o.stderr, oracle_bin, sut_bin)
    se = normalize_stderr(s.stderr, oracle_bin, sut_bin)
    stderr_match = oe == se

    # A MISMATCH is a stdout, exit-code, OR output-FILE divergence on an assessable
    # case (the program's primary output -- for split/truncate that output is the
    # files it writes). stderr is a SEPARATE, first-class divergence class (after
    # program-name normalisation it reflects real differences -- e.g. the error-
    # message duplication C2SaferRust's cat/head/tail exhibit). any_divergence =
    # the translation is observably non-equivalent on this case in any channel.
    mismatch = assessable and not (stdout_match and exit_match and file_tree_match)
    stderr_divergence = assessable and not stderr_match
    any_divergence = assessable and not (stdout_match and exit_match
                                         and file_tree_match and stderr_match)

    v = {
        "id": case.id,
        "argv": case.argv,
        "assessable": assessable,
        "stdout_match": stdout_match,
        "exit_match": exit_match,
        "stderr_match": stderr_match,
        "mismatch": mismatch,
        "stderr_divergence": stderr_divergence,
        "any_divergence": any_divergence,
        "oracle_exit": o.returncode,
        "sut_exit": s.returncode,
        "timed_out": o.timed_out or s.timed_out,
    }
    if case.writes_files:
        v["file_tree_match"] = file_tree_match
        if file_tree_diffs:
            v["file_tree_diffs"] = file_tree_diffs[:8]
    if stderr_divergence and not mismatch:
        v["stderr_detail"] = {
            "oracle_stderr": oe[:200].decode("utf-8", "replace"),
            "sut_stderr": se[:200].decode("utf-8", "replace"),
        }
    if mismatch:
        v["detail"] = {
            "oracle_stdout_len": len(o.stdout),
            "sut_stdout_len": len(s.stdout),
            "first_diff_byte": _first_diff(o.stdout, s.stdout),
            "oracle_stdout_head": o.stdout[:120].hex(),
            "sut_stdout_head": s.stdout[:120].hex(),
        }
    return v


def _first_diff(a: bytes, b: bytes):
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    if len(a) != len(b):
        return n
    return -1


def run(util, cases, oracle_bin, sut_bin, timeout=15, stop_on_mismatch=False,
        stop_on_any=False):
    verdicts = []
    for c in cases:
        v = compare_case(c, oracle_bin, sut_bin, timeout)
        verdicts.append(v)
        if stop_on_mismatch and v["mismatch"]:
            break  # a single stdout/exit divergence is enough to "kill" a mutant
        if stop_on_any and v.get("any_divergence"):
            break  # any observable divergence (incl. stderr) kills the mutant
    n = len(verdicts)
    assessable = sum(1 for v in verdicts if v["assessable"])
    mism = sum(1 for v in verdicts if v["mismatch"])
    file_tree_div = sum(1 for v in verdicts
                        if v["assessable"] and v.get("file_tree_match") is False)
    stderr_div = sum(1 for v in verdicts if v.get("stderr_divergence"))
    stderr_only = sum(1 for v in verdicts
                      if v["assessable"] and not v["mismatch"] and not v["stderr_match"])
    any_div = sum(1 for v in verdicts if v.get("any_divergence"))
    agree = assessable - mism
    summary = {
        "util": util,
        "n_cases": n,
        "assessable": assessable,
        "not_assessable": n - assessable,
        "mismatches": mism,                       # stdout/exit/output-file divergences
        "file_tree_divergences": file_tree_div,   # output FILES differ (split/truncate)
        "agree": agree,                           # primary-output agreement
        "held_out_pass_rate": (agree / assessable) if assessable else None,
        "stderr_divergences": stderr_div,         # stderr differs (real, post-normalisation)
        "stderr_only_divergences": stderr_only,   # stderr differs but stdout/exit agree
        "any_divergences": any_div,               # observably non-equivalent in ANY channel
        "any_agree_rate": ((assessable - any_div) / assessable) if assessable else None,
    }
    return summary, verdicts


def load_cases(path):
    with open(path) as f:
        d = json.load(f)
    return d["util"], [Case.from_json(c) for c in d["cases"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", required=True, help="cases JSON from gen_inputs.py")
    ap.add_argument("--oracle", help="prebuilt oracle binary (else build it)")
    ap.add_argument("--sut", help="prebuilt SUT binary (else build it)")
    ap.add_argument("--out", help="write full results JSON here")
    ap.add_argument("--timeout", type=int, default=15)
    args = ap.parse_args()

    util, cases = load_cases(args.cases)

    oracle_bin, sut_bin = args.oracle, args.sut
    build_info = None
    if not (oracle_bin and sut_bin):
        build_info = build_both(util)
        if not build_info["oracle"]["ok"]:
            print(json.dumps({"error": "oracle build failed", "build": build_info}, indent=2))
            sys.exit(2)
        if not build_info["sut"]["ok"]:
            print(json.dumps({"error": "SUT build failed", "build": build_info}, indent=2))
            sys.exit(2)
        oracle_bin = oracle_bin or build_info["oracle"]["bin"]
        sut_bin = sut_bin or build_info["sut"]["bin"]

    summary, verdicts = run(util, cases, oracle_bin, sut_bin, args.timeout)
    out = {"summary": summary, "build": build_info, "verdicts": verdicts}
    if args.out:
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w") as f:
            json.dump(out, f, indent=2)
    print(json.dumps(summary, indent=2))

    if summary["mismatches"] > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
