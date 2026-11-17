"""Supplementary UB check (stricter than the common rule): re-run every COUNTED case's C under the
clang ASan build of ub_filter.py with ASAN_OPTIONS detect_stack_use_after_return=1, which the
common sanitizer configuration (harness/oracle_ub_common.py) leaves off. A report here means the
C reads a dangling stack frame on that input (UB the common filter cannot see); such a case is
excluded and listed.

Usage (in container): python3 -I suar_check.py --kind exe|lib  -> /w/out/suar_<kind>.json
"""
import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import oracle_ub_common as ou  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["exe", "lib"], required=True)
    a = ap.parse_args()
    od = os.path.join(common.W, "out")
    dt = json.load(open(os.path.join(od, "difftest.json" if a.kind == "exe" else "lib_difftest.json")))
    ub = json.load(open(os.path.join(od, "ub_filter.json" if a.kind == "exe" else "ub_filter_lib.json")))
    counted = {(p["battery"], p["case"], v["id"]) for p in ub["programs"] for v in p["verdicts"] if v["counted"]}
    env = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
           "ASAN_OPTIONS": "detect_leaks=0:detect_stack_use_after_return=1:exitcode=86",
           "UBSAN_OPTIONS": ou.SAN_ENV["UBSAN_OPTIONS"]}
    res = {"asan_options": env["ASAN_OPTIONS"], "cases": [], "fired": []}
    for p in dt["programs"]:
        binp = os.path.join(common.W, "build", p["battery"], p["case"], "ubvar", "san_clang")
        for dv in p["divergent"]:
            if (p["battery"], p["case"], dv["input"]["id"]) not in counted:
                continue
            inp = dv["input"]
            wd = tempfile.mkdtemp(dir="/tmp")
            try:
                for fn, data in inp["files"].items():
                    open(os.path.join(wd, fn), "wb").write(base64.b64decode(data))
                for dd in inp["dirs"]:
                    os.makedirs(os.path.join(wd, dd), exist_ok=True)
                e = dict(env, HOME=wd)
                try:
                    r = subprocess.run(["driver"] + inp["argv"], executable=binp, input=base64.b64decode(inp["stdin_b64"]),
                                       cwd=wd, env=e, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=40)
                    rep = ou.san_report(r.stderr)
                except subprocess.TimeoutExpired:
                    rep = None
            finally:
                shutil.rmtree(wd, ignore_errors=True)
            row = {"translation": f"{p['battery']}/{p['case']}", "id": inp["id"], "report": rep}
            res["cases"].append(row)
            if rep:
                res["fired"].append(row)
    res["n_cases"] = len(res["cases"])
    res["n_fired"] = len(res["fired"])
    common.dump(os.path.join(od, f"suar_{a.kind}.json"), res)
    print(f"{a.kind}: counted cases {res['n_cases']}, stack-use-after-return reports {res['n_fired']}")
    for r in res["fired"]:
        print("  ", r["translation"], r["id"], (r["report"] or "")[:120])


if __name__ == "__main__":
    main()
