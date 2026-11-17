"""C2SaferRust: re-run the counted crash defects (and the overflow-adjacent S1 head-5) under the
OTHER cargo profile.

Our harness builds the SUT with `cargo build --bin <util>` = DEBUG (harness/build.py:50-51, binary
at target/debug/<util>), the same profile C2SaferRust's own loop uses (translate.py:184 `cargo
build`, :236 and :711 `target/debug`). So the counted results are DEBUG results (overflow checks
ON, which is what produces C2SR-head-3's 'attempt to add with overflow' panic). This script builds
the same rust_WIP crates in --release (overflow checks OFF, wraps) in a scratch copy and replays
the exposing cases against the unchanged C oracle with the harness's own compare_case.

Exposing cases = the cases of results/<util>_cases_v4.json whose SHIPPED (debug) verdict in
results/<util>_diff_v4.json is the crash of the row (sut_exit 101 = panic for head-3; -11 = SIGSEGV
for head-4, tail-2, tail-3, selected by argv). Plus the ledger example input and, for head-5, the
10 MiB bigseek file the ledger names.

Runs inside crust-heldout (rustc nightly-2022-08-08), --network none:
  docker run --rm --network none --cpus 2 -v $W:/w -v $REPO:/repo:ro -v $OUT:/out \
     -e CARGO_HOME=/cargo_home -e RUSTUP_TOOLCHAIN=nightly-2022-08-08 crust-heldout \
     python3 /repo/systems/profile_check/c2sr_profile.py /out/c2sr.json
"""
import json, os, shutil, subprocess, sys, tempfile

W = os.environ.get("PC_W", "/w")             # scratch: c2sr/<util>/{c,rust_WIP}
REPO = os.environ.get("PC_REPO", "/repo")    # read-only copy of repo files (harness/, results/)
sys.path.insert(0, os.path.join(REPO, "harness"))
from common import Case, run_capture  # noqa: E402
import difftest  # noqa: E402


def sh(argv, cwd):
    p = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout[-1500:]


def build(util):
    out = {}
    cdir = os.path.join(W, "c2sr", util, "c")
    rc, log = sh(["make"], cdir)
    out["oracle"] = {"rc": rc, "bin": os.path.join(cdir, util)}
    rdir = os.path.join(W, "c2sr", util, "rust_WIP")
    for prof in ("debug", "release"):
        rc, log = sh(["cargo", "build", "--offline", "--bin", util] + (["--release"] if prof == "release" else []), rdir)
        b = os.path.join(rdir, "target", prof, util)
        out[prof] = {"rc": rc, "bin": b if os.path.isfile(b) else None, "log_tail": log[-300:]}
    return out


def klass(v):
    if v["timed_out"]:
        return "timeout"
    if v["sut_exit"] == 101:
        return "crash (panic, exit 101)"
    if v["sut_exit"] < 0:
        return "crash (signal %d)" % -v["sut_exit"]
    if v["mismatch"]:
        return "wrong answer"
    return "agrees"


def replay(util, bins, cases, label):
    rows = []
    for c in cases:
        r = {"id": c.id, "argv": c.argv}
        for prof in ("debug", "release"):
            v = difftest.compare_case(c, bins["oracle"]["bin"], bins[prof]["bin"])
            r[prof] = {"outcome": klass(v), "oracle_exit": v["oracle_exit"], "sut_exit": v["sut_exit"],
                       "stdout_match": v["stdout_match"], "stderr_match": v["stderr_match"],
                       "detail": v.get("detail")}
        rows.append(r)
    summ = {}
    for prof in ("debug", "release"):
        s = {}
        for r in rows:
            s[r[prof]["outcome"]] = s.get(r[prof]["outcome"], 0) + 1
        summ[prof] = s
    return {"label": label, "n": len(rows), "outcome_counts": summ,
            "diverges_under_release": sum(1 for r in rows if r["release"]["outcome"] != "agrees"),
            "diverges_under_debug": sum(1 for r in rows if r["debug"]["outcome"] != "agrees"),
            "samples": rows[:3] + [r for r in rows if r["release"]["outcome"] == "agrees"][:3]}


def main():
    outp = sys.argv[1]
    res = {"builds": {}, "rows": {}}
    for util in ("head", "tail"):
        res["builds"][util] = build(util)
    for util, defects in (("head", {"C2SR-head-3": lambda v: v["sut_exit"] == 101,
                                     "C2SR-head-4": lambda v: v["sut_exit"] == -11}),
                          ("tail", {"C2SR-tail-2": lambda v: v["sut_exit"] == -11,
                                    "C2SR-tail-3": lambda v: v["sut_exit"] == -11})):
        cj = json.load(open(os.path.join(REPO, "results", f"{util}_cases_v4.json")))
        cases = {c["id"]: Case.from_json(c) for c in cj["cases"]}
        dj = json.load(open(os.path.join(REPO, "results", f"{util}_diff_v4.json")))
        for did, pred in defects.items():
            ids = [v["id"] for v in dj["verdicts"] if v["mismatch"] and pred(v)]
            if util == "head" and did == "C2SR-head-3":
                ids = [i for i in ids if any(a in ("-1", "-2") for a in cases[i].argv)]
            if util == "head" and did == "C2SR-head-4":
                ids = [i for i in ids if any(a in ("-q", "-z") for a in cases[i].argv)]
            res["rows"][did] = replay(util, res["builds"][util], [cases[i] for i in ids], f"{did} exposing cases from shipped debug verdicts")
    # ledger example inputs and head-5 (own-input bigseek)
    hb = res["builds"]["head"]
    ex = Case(id="ex-head3", util="head", argv=["-n", "-1"], stdin_b64=__import__("base64").b64encode(b"a\nb\nc\n").decode())
    res["rows"]["C2SR-head-3"]["ledger_example"] = replay("head", hb, [ex], "head -n -1, stdin a\\nb\\nc\\n")
    big = Case(id="bigseek", util="head", argv=["--lines=-1", "bigseek"], stdin_b64="",
               files={"bigseek": __import__("base64").b64encode(b"y\n" * (5 * 1024 * 1024)).decode()})
    res["rows"]["C2SR-head-5"] = replay("head", hb, [big], "head --lines=-1 on 10 MiB file of 'y\\n' (head-write-error.sh bigseek)")
    # head-5 on a stdin pipe larger than one buffer (the form the mechanism text describes)
    pipe = Case(id="pipe-big", util="head", argv=["--lines=-1"], stdin_b64=__import__("base64").b64encode(b"y\n" * 40000).decode())
    res["rows"]["C2SR-head-5"]["pipe_80KB"] = replay("head", hb, [pipe], "head --lines=-1, 80000 B on stdin")
    json.dump(res, open(outp, "w"), indent=1)
    for k, v in res["rows"].items():
        print(k, v["n"], v["outcome_counts"])


if __name__ == "__main__":
    main()
