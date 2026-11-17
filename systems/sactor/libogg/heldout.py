#!/usr/bin/env python3
"""Held-out measurement for the SACTOR libogg re-grade (run after run.py, same build dir).

1. What SACTOR's own check ran: test_bitwise.o / test_framing.o (the _V_SELFTEST mains SACTOR
   links with each translation) are linked against the ORIGINAL C with -Wl,--wrap on the API
   (trace_own.c) and run; every call into the library is logged with its arguments and with the
   trigger condition of each counted defect.
2. What our harness ran: our driver, linked the same way, on every generated case.
3. Overlap: (a) per case, whether the identical call sequence occurs in SACTOR's tests; (b) per
   distinct call (function + scalar arguments), the share of ours that SACTOR's tests also make;
   (c) per defect, how many SACTOR-test calls meet its trigger condition, and how many of our
   exposing cases contain a trigger call.
4. SACTOR's own inputs through our comparison: the selftest binaries run.py built (C and every
   Rust build) are run again and their stdout, stderr and exit code compared with the C.

    python3 systems/sactor/libogg/heldout.py --build /build/libogg --out /work/results/sactor_libogg_heldout.json
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import sactor_common as sc  # noqa: E402
import gen_cases  # noqa: E402
import run as ogg_run  # noqa: E402

ou = sc.ou
WRAP = {
    "bitwise": ["oggpack_write", "oggpackB_write", "oggpack_read", "oggpackB_read", "oggpack_look", "oggpackB_look",
                "oggpack_writecopy", "oggpackB_writecopy", "oggpack_writetrunc", "oggpackB_writetrunc"],
    "framing": ["ogg_stream_packetin", "ogg_stream_iovecin", "ogg_stream_pageout", "ogg_stream_flush",
                "ogg_stream_pageout_fill", "ogg_stream_flush_fill"],
}
COND = {"bitwise": "COND_B0", "framing": "COND_F1"}


def build_traced(bdir, src, file, main_obj, name):
    exe = os.path.join(bdir, f"traced_{name}_{file}")
    flags = ["-O2", "-g", "-DHAVE_CONFIG_H", "-ffast-math", "-fsigned-char"] + ogg_run.inc(src)
    argv = (["gcc", "-o", exe, main_obj, os.path.join(HERE, "trace_own.c"), os.path.join(src, "src", f"{file}.c")]
            + flags + [f"-DTRACE_{file.upper()}"] + [f"-Wl,--wrap={f}" for f in WRAP[file]] + ["-lm"])
    rc, log = sc.sh(argv)
    if rc != 0:
        raise SystemExit("traced build failed:\n" + log)
    return exe


def run_traced(exe, trace, stdin=b""):
    if os.path.exists(trace):
        os.remove(trace)
    env = dict(os.environ)
    env["SACTOR_TRACE"] = trace
    out, err, rc, to = ou.run_proc([exe], stdin, env=env, timeout=300)
    lines = open(trace).read().splitlines() if os.path.exists(trace) else []
    return lines, rc


def sig(line):
    """Call signature without the state annotations (endbit / lacing_fill / bos_pending / COND)."""
    return re.sub(r"\s+(endbit|lacing_fill|bos_pending)=\S+|\s+COND_\w+", "", line).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="/build/libogg")
    ap.add_argument("--out", default="/work/results/sactor_libogg_heldout.json")
    args = ap.parse_args()
    src = os.path.join(args.build, "ogg")
    cases = {"bitwise": gen_cases.bitwise_cases(), "framing": gen_cases.framing_cases()}
    libogg = json.load(open(os.path.join(os.path.dirname(args.out), "sactor_libogg.json")))
    out = {"provenance": sc.provenance(__file__, os.environ.get("SACTOR_COMMAND", " ".join(sys.argv))), "files": {}}
    for file in ("bitwise", "framing"):
        fdir = os.path.join(args.build, file)
        tdir = os.path.join(fdir, "trace")
        os.makedirs(tdir, exist_ok=True)
        own_exe = build_traced(fdir, src, file, os.path.join(fdir, f"test_{file}.o"), "own")
        own_lines, own_rc = run_traced(own_exe, os.path.join(tdir, "own.trace"))
        own_sigs = {sig(x) for x in own_lines}
        own_cond = sum(1 for x in own_lines if COND[file] in x)
        drv_exe = build_traced(fdir, src, file, os.path.join(fdir, f"drv_{file}.o"), "ours")
        our_sigs = set()
        per_case = {}
        identical = 0
        own_seq = [sig(x) for x in own_lines]
        for c in cases[file]:
            lines, rc = run_traced(drv_exe, os.path.join(tdir, "case.trace"), c["script"].encode())
            s = [sig(x) for x in lines]
            our_sigs |= set(s)
            per_case[c["id"]] = {"calls": len(s), "calls_not_in_own": sum(1 for x in s if x not in own_sigs),
                                 "cond_calls": sum(1 for x in lines if COND[file] in x)}
            if s and s == own_seq:
                identical += 1
        # exposing cases of each Rust build (from run.py), by UB-clean divergence
        rows = libogg["files"][file]["rows"]
        exposing = sorted({r["id"] for r in rows if not r["c_ub"] for rn, v in r["rust"].items() if v != "agree"})
        out["files"][file] = {
            "own_test": {"exit_on_C": own_rc, "api_calls_logged": len(own_lines), "distinct_call_signatures": len(own_sigs),
                         "calls_meeting_defect_trigger": own_cond, "trigger": COND[file]},
            "ours": {"cases": len(cases[file]), "distinct_call_signatures": len(our_sigs),
                     "distinct_signatures_also_in_own": len(our_sigs & own_sigs),
                     "cases_identical_to_own_sequence": identical,
                     "cases_with_every_call_in_own": sum(1 for v in per_case.values() if v["calls"] and v["calls_not_in_own"] == 0),
                     "cases_with_trigger_call": sum(1 for v in per_case.values() if v["cond_calls"]),
                     },
            "exposing_cases": {cid: per_case[cid] for cid in exposing},
            "exposing_cases_with_trigger_call": sum(1 for cid in exposing if per_case[cid]["cond_calls"]),
        }
        # 4. SACTOR's own inputs (the selftest) through our comparison
        cexe = os.path.join(fdir, f"sactortest_C_{file}")
        e = dict(os.environ, LC_ALL="C", LANG="C")
        co, ce, crc, cto = ou.run_proc([cexe], b"", env=e, timeout=300)
        cmp = {}
        for fn in sorted(os.listdir(fdir)):
            if fn.startswith("sactortest_") and not fn.startswith("sactortest_C_"):
                ro, re_, rrc, rto = ou.run_proc([os.path.join(fdir, fn)], b"", env=e, timeout=300)
                cmp[fn] = {"exit": rrc, "agree_with_C": (ro, re_, rrc) == (co, ce, crc),
                           "stdout_same": ro == co, "stderr_same": re_ == ce}
        out["files"][file]["own_inputs_through_our_comparison"] = {"C_exit": crc, "C_stdout_bytes": len(co), "rust": cmp}
    sc.dump(args.out, out)
    print(json.dumps({f: {k: v for k, v in d.items() if k != "exposing_cases"} for f, d in out["files"].items()}, indent=1))


if __name__ == "__main__":
    main()
