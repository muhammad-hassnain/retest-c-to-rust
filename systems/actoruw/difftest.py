"""Differential test: C oracle vs ACTOR's Rust, every TRACTOR executable case that builds.

Channels: stdout (bytes), exit code, stderr (bytes). Both binaries run with argv[0] = "driver", so
a program-name prefix cannot differ. Each case runs in a fresh scratch cwd holding its input files.
A divergent case is re-run once on both sides; a case whose C output is not reproducible is marked
c_nondeterministic (it then goes through the UB rule like any other divergence).

Usage (in container): python3 -I difftest.py [--only battery/case] [--build /w/out/build.json]
Writes /w/out/difftest.json.
"""
import argparse
import base64
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import gen_inputs  # noqa: E402

TIMEOUT = 5


OUT_CAP = 16 << 20     # RLIMIT_FSIZE on the captured stdout/stderr files (both sides alike)


def _limits():
    import resource
    resource.setrlimit(resource.RLIMIT_FSIZE, (OUT_CAP, OUT_CAP))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def run_one(binpath, c, timeout=TIMEOUT, env_extra=None):
    """Run one input. stdout/stderr go to files capped at OUT_CAP bytes by RLIMIT_FSIZE (a program
    that writes more is stopped by SIGXFSZ on both sides alike), so an unbounded printer costs
    neither memory nor the full timeout."""
    d = tempfile.mkdtemp(prefix="adt_", dir="/tmp")
    try:
        for name, data in c["files"].items():
            with open(os.path.join(d, name), "wb") as f:
                f.write(base64.b64decode(data))
        for dd in c["dirs"]:
            os.makedirs(os.path.join(d, dd), exist_ok=True)
        env = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "HOME": d}
        if env_extra:
            env.update(env_extra)
        io = tempfile.mkdtemp(prefix="adtio_", dir="/tmp")
        try:
            with open(os.path.join(io, "o"), "wb") as fo, open(os.path.join(io, "e"), "wb") as fe:
                try:
                    p = subprocess.run(["driver"] + c["argv"], executable=binpath, cwd=d, env=env,
                                       input=base64.b64decode(c["stdin_b64"]), stdout=fo, stderr=fe,
                                       timeout=timeout, preexec_fn=_limits)
                    rc, to = p.returncode, False
                except subprocess.TimeoutExpired:
                    rc, to = None, True
            with open(os.path.join(io, "o"), "rb") as f:
                out = f.read()
            with open(os.path.join(io, "e"), "rb") as f:
                err = f.read()
            return {"stdout": out, "stderr": err, "rc": rc, "timeout": to}
        finally:
            shutil.rmtree(io, ignore_errors=True)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def same(a, b):
    return a["stdout"] == b["stdout"] and a["rc"] == b["rc"] and a["stderr"] == b["stderr"] \
        and a["timeout"] == b["timeout"]


def classify(cr, rr):
    """Severity of one divergent case (before the UB rule)."""
    if rr["timeout"] and not cr["timeout"]:
        return "S3_hang"
    if cr["timeout"]:
        return "c_timeout"
    rust_panic = rr["rc"] == 101 and b"panicked" in rr["stderr"]
    rust_signal = rr["rc"] is not None and rr["rc"] < 0
    if rust_panic or rust_signal:
        return "rust_crash" if not (cr["rc"] is not None and cr["rc"] < 0) else "both_crash"
    if cr["stdout"] != rr["stdout"] or cr["rc"] != rr["rc"]:
        return "S1"
    return "S2"


def enc(r):
    def t(b):
        return base64.b64encode(b[:4096]).decode()
    return {"stdout_b64": t(r["stdout"]), "stderr_b64": t(r["stderr"]), "rc": r["rc"],
            "timeout": r["timeout"], "stdout_len": len(r["stdout"]), "stderr_len": len(r["stderr"])}


def test_program(rec):
    b, c = rec["battery"], rec["case"]
    ph, src = common.phase_dir(b, c)
    sh, cases = gen_inputs.generate(os.path.join(src, "c_src"))
    cbin = os.path.join(common.W, "build", b, c, "c", "driver")
    rbin = os.path.join(common.W, rec["rust_bin"])
    out = {"battery": b, "case": c, "shape": sh, "n_inputs": len(cases), "input_ids": [x["id"] for x in cases],
           "divergent": [], "counts": {}}
    for x in cases:
        cr = run_one(cbin, x)
        rr = run_one(rbin, x)
        if same(cr, rr):
            out["counts"]["agree"] = out["counts"].get("agree", 0) + 1
            continue
        k = classify(cr, rr)
        if k == "c_timeout":                 # no oracle on this input: not exercised
            out["counts"][k] = out["counts"].get(k, 0) + 1
            out.setdefault("c_timeout_ids", []).append(x["id"])
            continue
        cr2 = run_one(cbin, x)
        rr2 = run_one(rbin, x)
        out["counts"][k] = out["counts"].get(k, 0) + 1
        out["divergent"].append({"input": x, "class": k, "c": enc(cr), "rust": enc(rr),
                                 "c_reproducible": same(cr, cr2), "rust_reproducible": same(rr, rr2),
                                 "channels": [ch for ch in ("stdout", "rc", "stderr", "timeout")
                                              if cr[ch] != rr[ch]]})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None)
    ap.add_argument("--build", default=os.path.join(common.W, "out", "build.json"))
    ap.add_argument("--out", default=os.path.join(common.W, "out", "difftest.json"))
    ap.add_argument("-j", type=int, default=3)
    a = ap.parse_args()
    import json
    bj = json.load(open(a.build))
    recs = [r for r in bj["cases"] if r["status"] == "ok"]
    if a.only:
        recs = [r for r in recs if f"{r['battery']}/{r['case']}" == a.only]
    def one(r):
        o = test_program(r)
        print(f"{o['battery']}/{o['case']}: n={o['n_inputs']} {o['counts']}", flush=True)
        return o
    with ThreadPoolExecutor(a.j) as ex:
        res = list(ex.map(one, recs))
    common.dump(a.out, {"generator": "systems/actoruw/gen_inputs.py", "timeout_s": TIMEOUT,
                        "toolchains": bj["toolchains"], "programs": res,
                        "not_run": [{"battery": r["battery"], "case": r["case"], "status": r["status"]}
                                    for r in bj["cases"] if r["status"] != "ok"]})


if __name__ == "__main__":
    main()
