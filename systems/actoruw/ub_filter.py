"""UB rule for the ACTOR re-grade (same rule and sanitizer configuration as every other system:
harness/oracle_ub_common.py). A divergence counts only on an input where the C passes ALL of:

  san_clang  clang-14 ASan + UBSan + float-cast-overflow, -O0 -g, -fno-sanitize-recover=all, non-PIE
  san_gcc    gcc-11, the same sanitizer set
  o1, o2     gcc -O1 / -O2: stdout, exit code and stderr byte-identical to the oracle (gcc, no -O)
  uchar      gcc -funsigned-char: identical to the oracle (no dependence on char signedness)
  schar      gcc -fsigned-char (x86-64 default): identical to the oracle

Every variant gets the case's own CMake recipe (sources, include dirs, compile options,
CMAKE_C_FLAGS, configuration). Audit 8 uniform rule (harness/oracle_ub_common.not_clean_reasons):
a sanitizer run that fails to start (a start-up crash with no report, retried), times out, or prints
another stdout / exit code than the oracle leaves the case NOT counted, as does any o1/o2/uchar/schar
build that times out or disagrees.

Usage (in container): python3 -I ub_filter.py [--difftest /w/out/difftest.json]
Writes /w/out/ub_filter.json.
"""
import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import oracle_ub_common as ou  # noqa: E402
from build import c_cmd  # noqa: E402

VARIANTS = {
    "ref": ("gcc", [], [], False),
    "o1": ("gcc", ["-O1"], [], False),
    "o2": ("gcc", ["-O2"], [], False),
    "uchar": ("gcc", ["-funsigned-char"], [], False),
    "schar": ("gcc", ["-fsigned-char"], [], False),
    "san_clang": ("clang", ["-w"] + ou.san_cflags("clang"), ou.san_ldflags("clang"), True),
    "san_gcc": ("gcc", ["-w"] + ou.san_cflags("gcc"), ou.san_ldflags("gcc"), True),
}


def build_variants(battery, case, kind="exe"):
    ph, src = common.phase_dir(battery, case)
    over, _ = common.case_config(case)
    csrc = os.path.join(src, "c_src")
    recipe = common.parse_cmake(os.path.join(csrc, "CMakeLists.txt"), over)
    d = os.path.join(common.W, "build", battery, case, "ubvar")
    os.makedirs(d, exist_ok=True)
    bins, logs = {}, {}
    for name, (cc, cfl, ldf, _s) in VARIANTS.items():
        out = os.path.join(d, name)
        if kind == "lib":
            import lib_harness
            cmd = lib_harness.c_oracle_cmd(cc, {"battery": battery, "case": case}, src, out, extra=cfl) + ldf
        else:
            cmd = c_cmd(cc, recipe, csrc, out, extra=cfl) + ldf
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        logs[name] = {"cmd": " ".join(cmd), "rc": p.returncode}
        if p.returncode == 0:
            bins[name] = out
    return bins, logs


def run_variant(name, binp, inp, timeout=10):
    wd = tempfile.mkdtemp(prefix="aub_", dir="/tmp")
    try:
        for fn, data in inp["files"].items():
            with open(os.path.join(wd, fn), "wb") as f:
                f.write(base64.b64decode(data))
        for dd in inp["dirs"]:
            os.makedirs(os.path.join(wd, dd), exist_ok=True)
        env = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "HOME": wd}
        stdin = base64.b64decode(inp["stdin_b64"])
        # argv[0] = "driver" for every variant, as in difftest.py
        argv = ["driver"] + inp["argv"]
        if VARIANTS[name][3]:
            e = dict(env)
            e.update(ou.SAN_ENV)
            last = None
            for _ in range(6):
                try:
                    p = subprocess.run(argv, executable=binp, input=stdin, cwd=wd, env=e,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout * 4)
                    o, er, rc, to = p.stdout, p.stderr, p.returncode, False
                except subprocess.TimeoutExpired as ex:
                    o, er, rc, to = ex.stdout or b"", ex.stderr or b"", None, True
                last = (o, er, rc, to)
                if not to and ou.is_startup_infra(rc if rc is not None else 0, er):
                    continue
                return {"stdout": o, "stderr": er, "rc": rc, "timed_out": to,
                        "report": ou.san_report(er), "reports": ou.san_reports_all(er), "infra": False}
            o, er, rc, to = last
            return {"stdout": o, "stderr": er, "rc": rc, "timed_out": to, "report": None, "reports": [],
                    "infra": True}
        try:
            p = subprocess.run(argv, executable=binp, input=stdin, cwd=wd, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
            return {"stdout": p.stdout, "stderr": p.stderr, "rc": p.returncode, "timed_out": False}
        except subprocess.TimeoutExpired as ex:
            return {"stdout": ex.stdout or b"", "stderr": ex.stderr or b"", "rc": None, "timed_out": True}
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def check_case(bins, div):
    inp = div["input"]
    res = {n: run_variant(n, b, inp) for n, b in bins.items()}
    ref = res["ref"]
    v = {"id": inp["id"], "class": div["class"], "variants": {}}
    reasons = []
    for n, r in res.items():
        if n == "ref":
            continue
        rec = {"rc": r["rc"], "timed_out": r["timed_out"]}
        if VARIANTS[n][3]:
            rec.update({"report": r["report"], "reports": r["reports"][:5], "infra": r["infra"]})
            rec["same_stdout_rc_as_ref"] = r["stdout"] == ref["stdout"] and r["rc"] == ref["rc"]
            reasons += [x[:200] for x in ou.not_clean_reasons(n, r, rec["same_stdout_rc_as_ref"])]
        else:
            same = r["stdout"] == ref["stdout"] and r["rc"] == ref["rc"] and r["stderr"] == ref["stderr"]
            rec["same_as_ref"] = same
            if r["timed_out"]:
                reasons.append(f"{n}: timed out")
            elif not same:
                reasons.append(f"{n}: output differs from the oracle")
        v["variants"][n] = rec
    # the oracle must also be reproducible (difftest re-ran it once)
    if not div.get("c_reproducible", True):
        reasons.append("oracle: output not reproducible across two runs")
    v["ref_matches_difftest_oracle"] = (base64.b64encode(ref["stdout"][:4096]).decode() == div["c"]["stdout_b64"]
                                        and ref["rc"] == div["c"]["rc"])
    v["ub_reasons"] = reasons
    v["counted"] = not reasons
    return v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--difftest", default=os.path.join(common.W, "out", "difftest.json"))
    ap.add_argument("--out", default=os.path.join(common.W, "out", "ub_filter.json"))
    ap.add_argument("-j", type=int, default=3)
    ap.add_argument("--kind", choices=["exe", "lib"], default="exe")
    a = ap.parse_args()
    dt = json.load(open(a.difftest))
    progs = [p for p in dt["programs"] if p["divergent"]]
    out = {"rule": __doc__.strip().split("\n\n")[0], "variants": {k: {"cc": v[0], "cflags": v[1], "ldflags": v[2]}
                                                                    for k, v in VARIANTS.items()},
           "positive_controls": ou.positive_controls(os.path.join(common.W, "build", "_ub_controls")),
           "programs": []}
    for p in progs:
        bins, logs = build_variants(p["battery"], p["case"], a.kind)
        missing = [n for n in VARIANTS if n not in bins]
        with ThreadPoolExecutor(a.j) as ex:
            verdicts = list(ex.map(lambda dv: check_case(bins, dv), p["divergent"])) if not missing else []
        n_c = sum(1 for v in verdicts if v["counted"])
        print(f"{p['battery']}/{p['case']}: divergent={len(p['divergent'])} counted={n_c} missing={missing}",
              flush=True)
        out["programs"].append({"battery": p["battery"], "case": p["case"], "build": logs,
                                "variants_missing": missing, "verdicts": verdicts})
    out["toolchains"] = ou.toolchains()
    common.dump(a.out, out)


if __name__ == "__main__":
    main()
