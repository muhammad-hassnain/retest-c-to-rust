"""Toolchain cross-check (S3 rule): is a counted divergence a property of the translation, or only
of the Rust toolchain we built it with? Rebuild every translation that has a counted (UB-clean)
divergence with a second toolchain and re-run exactly the counted inputs; a case is
toolchain-stable when the Rust output under the second toolchain equals the output under the
primary one (the ACTOR pin 1.94.0 is approximated by rustc 1.94.0-nightly 2026-01-09; the second
toolchain is rustc 1.99.1 stable, mounted by run_in_container.sh with RUST_TC=stable-...).

Usage (in container, RUST_TC=stable-x86_64-unknown-linux-gnu):
  python3 -I tc_crosscheck.py --kind exe|lib   -> /w/out/tc_crosscheck_<kind>.json
"""
import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import difftest  # noqa: E402


def sh(argv, cwd=None, env=None):
    p = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["exe", "lib"], required=True)
    a = ap.parse_args()
    out_d = os.path.join(common.W, "out")
    sfx = "" if a.kind == "exe" else "lib_"
    dt = json.load(open(os.path.join(out_d, "difftest.json" if a.kind == "exe" else "lib_difftest.json")))
    ub = json.load(open(os.path.join(out_d, f"ub_filter{'' if a.kind == 'exe' else '_lib'}.json")))
    counted = {}
    for p in ub["programs"]:
        for v in p["verdicts"]:
            if v["counted"]:
                counted.setdefault((p["battery"], p["case"]), set()).add(v["id"])
    _, rv = sh(["rustc", "--version"])
    res = {"toolchain": rv.strip(), "programs": []}
    for p in dt["programs"]:
        key = (p["battery"], p["case"])
        if key not in counted:
            continue
        d = os.path.join(common.W, "build", *key)
        tdir = os.path.join(d, "target_tc2")
        crate = os.path.join(d, "rust_tc2")
        if os.path.isdir(crate):
            shutil.rmtree(crate)
        shutil.copytree(os.path.join(d, "rust"), crate, ignore=shutil.ignore_patterns("target"))
        env = dict(os.environ)
        env["CARGO_TARGET_DIR"] = tdir
        if a.kind == "exe":
            _, feats = common.case_config(key[1])
            rc, log = sh(["cargo", "build", "--release", "--offline", "--bins"] + feats, cwd=crate, env=env)
            bj = {(r["battery"], r["case"]): r for r in json.load(open(os.path.join(out_d, "build.json")))["cases"]}
            rbin = os.path.join(tdir, "release", os.path.basename(bj[key]["rust_bin"]))
        else:
            rc, log = sh(["cargo", "build", "--release", "--offline", "--lib"], cwd=crate, env=env)
            lb = {(r["battery"], r["case"]): r for r in json.load(open(os.path.join(out_d, "lib_build.json")))["cases"]}
            so = os.path.join(tdir, "release", os.path.basename(lb[key]["rust_so"]))
            link = lb[key]["rust_link"]["cmd"].split()
            rbin = os.path.join(d, "rust_driver_tc2")
            link[link.index("-o") + 1] = rbin
            link = [so if x.endswith(".so") else x for x in link]
            link = ["-Wl,-rpath," + os.path.dirname(so) if x.startswith("-Wl,-rpath,") else x for x in link]
            if rc == 0:
                rc, log2 = sh(link)
        row = {"battery": key[0], "case": key[1], "build_rc": rc, "cases": []}
        if rc == 0:
            for dv in p["divergent"]:
                if dv["input"]["id"] not in counted[key]:
                    continue
                r2 = difftest.enc(difftest.run_one(rbin, dv["input"]))
                # a Rust panic message names the thread with its OS id ("thread 'main' (18851)"),
                # which differs from run to run: normalised before comparing stderr
                n = lambda b64: re.sub(rb"thread '([^']*)' \(\d+\)", rb"thread '\1'", base64.b64decode(b64))
                same = all(r2[k] == dv["rust"][k] for k in ("stdout_b64", "rc", "timeout")) and \
                    n(r2["stderr_b64"]) == n(dv["rust"]["stderr_b64"])
                row["cases"].append({"id": dv["input"]["id"], "same_rust_output": same,
                                     **({} if same else {"tc2_output": r2})})
        row["all_stable"] = rc == 0 and all(c["same_rust_output"] for c in row["cases"])
        print(f"{key[0]}/{key[1]}: build={rc} stable={row['all_stable']} n={len(row['cases'])}", flush=True)
        res["programs"].append(row)
    common.dump(os.path.join(out_d, f"tc_crosscheck_{a.kind}.json"), res)


if __name__ == "__main__":
    main()
