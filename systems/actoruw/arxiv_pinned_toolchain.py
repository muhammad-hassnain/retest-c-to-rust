"""arXiv version, issues.md item 34: ACTOR's counted defects rebuilt with the toolchain ACTOR pins.

ACTOR pins channel = "1.94.0" (stable) in rust-toolchain.toml (ACTOR @1b1e70a). The shipped re-grade
built with rustc 1.94.0-nightly (nightly-2026-01-10) and cross-checked with 1.99.1 (tc_crosscheck.py).

For every translation with a counted ACTOR defect (results/defect_ledger.json), the crate the shipped
build compiled (build/<battery>/<case>/rust, with the Cargo.lock that build resolved) is copied to
rust_pin/ and built as build.py and lib_harness.py build it: cargo build --release --offline, with
--bins and the case's Cargo features for an executable, --lib for a library (then linked to the same
generated C caller, as tc_crosscheck.py does). The Rust toolchain is the one mounted at /rust
(run_in_container.sh with RUST_TC_DIR = the pinned 1.94.0 toolchain).

Each exposing input is then run as the shipped comparison runs it: difftest.run_one on the C oracle the
shipped run built (build/<b>/<c>/c/driver, or c_driver for a library; gcc, not rebuilt) and on the new
Rust, compared with difftest.same (executables: stdout, exit code, stderr and timeout, byte for byte)
or lib_harness.same_lib (libraries: relative 1e-6 on a float #ret), and classified with
difftest.classify. The exposing inputs are the defect's case_ids in results/actoruw_summary.json (all
counted by the shipped UB rule, which depends on the C only; the C is unchanged) and, for memmove's
second-annotator row, the three inputs of second_rater_memmove.py. A defect's verdict is unchanged
when every exposing input still diverges from the C with the same class.

Usage (in the container; inputs copied to /w/in):
  RUST_TC=1.94.0-x86_64-unknown-linux-gnu RUST_TC_DIR=<host toolchain dir> \\
    bash systems/actoruw/run_in_container.sh arxiv_pinned_toolchain.py -j 4 --out /w/out/arxiv_actoruw_pinned_toolchain.json
"""
import argparse
import base64
import collections
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import difftest  # noqa: E402
import lib_harness  # noqa: E402
import second_rater_memmove as srm  # noqa: E402

IN = os.path.join(common.W, "in")
PANIC_TID = re.compile(rb"thread '([^']*)' \(\d+\)")


def sh(argv, cwd=None, env=None):
    p = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def sha(p):
    if not os.path.isfile(p):
        return None
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def data(name):
    d = json.load(open(os.path.join(IN, name)))
    return d.get("data", d)


def norm_stderr(b):
    # a Rust panic message names the thread with its OS id ("thread 'main' (18851)"), which differs
    # from run to run (as tc_crosscheck.py)
    return PANIC_TID.sub(rb"thread '\1'", b)


def build_pinned(t, kind, brec):
    b, c = t.split("/")
    d = os.path.join(common.W, "build", b, c)
    crate, tdir = os.path.join(d, "rust_pin"), os.path.join(d, "target_pin")
    if os.path.isdir(crate):
        shutil.rmtree(crate)
    shutil.copytree(os.path.join(d, "rust"), crate, ignore=shutil.ignore_patterns("target"))
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = tdir
    rec = {"cargo_lock_sha256": sha(os.path.join(crate, "Cargo.lock"))}
    if kind == "exe":
        _, feats = common.case_config(c)
        cmd = ["cargo", "build", "--release", "--offline", "--bins"] + feats
        rc, log = sh(cmd, cwd=crate, env=env)
        rbin = os.path.join(tdir, "release", os.path.basename(brec["rust_bin"]))
        cbin = os.path.join(d, "c", "driver")
        rec["c_sha256_matches_shipped_build"] = sha(cbin) == brec.get("c_sha256")
    else:
        cmd = ["cargo", "build", "--release", "--offline", "--lib"]
        rc, log = sh(cmd, cwd=crate, env=env)
        so = os.path.join(tdir, "release", os.path.basename(brec["rust_so"]))
        rbin = os.path.join(d, "rust_driver_pin")
        link = brec["rust_link"]["cmd"].split()
        link[link.index("-o") + 1] = rbin
        link = [so if x.endswith(".so") else x for x in link]
        link = ["-Wl,-rpath," + os.path.dirname(so) if x.startswith("-Wl,-rpath,") else x for x in link]
        if rc == 0:
            rc2, log2 = sh(link)
            rec["link"] = {"cmd": " ".join(link), "rc": rc2, **({} if rc2 == 0 else {"log_tail": log2[-1500:]})}
            rc = rc if rc2 == 0 else rc2
        cbin = os.path.join(d, "c_driver")
    rec.update({"cmd": " ".join(cmd), "rc": rc, "ok": rc == 0 and os.path.isfile(rbin),
                "rust_sha256": sha(rbin), "c_oracle_sha256": sha(cbin)})
    if rc != 0:
        rec["log_tail"] = log[-3000:]
        rec["errors"] = [ln for ln in log.splitlines() if ln.startswith("error")][:20]
    return rec, cbin, rbin


def same_rust(enc_new, ref):
    if any(enc_new[k] != ref[k] for k in ("stdout_b64", "rc", "timeout")):
        return False
    if "stdout_len" in ref and enc_new["stdout_len"] != ref["stdout_len"]:
        return False
    return norm_stderr(base64.b64decode(enc_new["stderr_b64"])) == norm_stderr(base64.b64decode(ref["stderr_b64"]))


def run_input(kind, cbin, rbin, x, shipped):
    cr, rr = difftest.run_one(cbin, x), difftest.run_one(rbin, x)
    agree = lib_harness.same_lib(cr, rr) if kind == "lib" else difftest.same(cr, rr)
    cls = None if agree else difftest.classify(cr, rr)
    ce, re_ = difftest.enc(cr), difftest.enc(rr)
    row = {"id": x["id"], "shipped_class": shipped["class"], "pinned_class": cls, "diverges": not agree,
           "same_class": cls == shipped["class"]}
    if "rust" in shipped and "stdout_b64" in shipped["rust"]:
        row["rust_output_same_as_shipped"] = same_rust(re_, shipped["rust"])
        row["c_output_same_as_shipped"] = all(ce[k] == shipped["c"][k] for k in ("stdout_b64", "stderr_b64", "rc", "timeout"))
    else:   # second_rater_memmove.json records decoded outputs
        dec = lambda r: {"exit": r["rc"], "stdout": r["stdout"].decode("utf-8", "replace"), "stderr": r["stderr"].decode("utf-8", "replace")}
        row["rust_output_same_as_shipped"] = dec(rr) == shipped["rust"]
        row["c_output_same_as_shipped"] = dec(cr) == shipped["c"]
    if not row["rust_output_same_as_shipped"] or not row["same_class"]:
        row["pinned_rust"] = re_
        row["pinned_c"] = ce
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-j", type=int, default=3)
    ap.add_argument("--out", default=os.path.join(common.W, "out", "arxiv_actoruw_pinned_toolchain.json"))
    a = ap.parse_args()
    t0 = time.time()
    ledger = data("defect_ledger.json")
    summ = data("actoruw_summary.json")
    dt = data("actoruw_difftest.json")
    ldt = data("actoruw_lib_difftest.json")
    bj = {f"{r['battery']}/{r['case']}": r for r in data("actoruw_build.json")["cases"]}
    lbj = {f"{r['battery']}/{r['case']}": r for r in data("actoruw_lib_build.json")["cases"]}
    sr = data("actoruw_second_rater_memmove.json")
    dv_by = {}
    for prog in dt["programs"] + ldt["programs"]:
        for dv in prog["divergent"]:
            dv_by[(f"{prog['battery']}/{prog['case']}", dv["input"]["id"])] = dv
    case_ids = {(x["translation"], x["mechanism"]): x["case_ids"] for x in summ["defects"]}
    defects = []
    for r in ledger["rows"]:
        if r["system"] != "ACTOR" or r["count_status"] != "counted":
            continue
        t = r["translation"].split(" (")[0]
        m = r["defect_id"].split("::")[-1]
        kind = "lib" if t.endswith("_lib") else "exe"
        if m.endswith("-own"):
            if t != f"{srm.BATTERY}/{srm.CASE}":
                raise SystemExit(f"no input source for {r['defect_id']}")
            ref = {x["id"]: x for x in sr["inputs"]}
            ins = []
            for iid, stdin in srm.INPUTS:
                x = {"id": iid, "argv": [], "stdin_b64": base64.b64encode(stdin).decode(), "files": {}, "dirs": [],
                     "note": "second annotator (docs/reviews/second-rater/ratings.json, case 11)"}
                ins.append((x, ref[iid]))
            src = "results/actoruw_second_rater_memmove.json (second_rater_memmove.INPUTS)"
        else:
            ins = [(dv_by[(t, i)]["input"], dv_by[(t, i)]) for i in case_ids[(t, m)]]
            src = "results/actoruw_summary.json case_ids"
        defects.append({"defect_id": r["defect_id"], "translation": t, "kind": kind, "severity": r["severity"],
                        "ledger_exposure_count": r["exposure_count"], "input_source": src, "_ins": ins})
    trans = sorted({(d["translation"], d["kind"]) for d in defects})

    def one(tk):
        t, kind = tk
        brec, cbin, rbin = build_pinned(t, kind, bj[t] if kind == "exe" else lbj[t])
        rows = {}
        if brec["ok"]:
            for d in defects:
                if d["translation"] != t:
                    continue
                for x, shipped in d["_ins"]:
                    if x["id"] not in rows:
                        rows[x["id"]] = run_input(kind, cbin, rbin, x, shipped)
        print(f"{t}: build={brec['rc']} inputs={len(rows)} diverge={sum(r['diverges'] for r in rows.values())} "
              f"same_class={sum(r['same_class'] for r in rows.values())}", flush=True)
        return t, brec, rows

    with ThreadPoolExecutor(a.j) as ex:
        res = {t: (b, rows) for t, b, rows in ex.map(one, trans)}
    out_defects = []
    for d in defects:
        b, rows = res[d["translation"]]
        ir = [rows[x["id"]] for x, _ in d["_ins"]] if b["ok"] else []
        dd = {k: v for k, v in d.items() if k != "_ins"}
        dd.update({"build_ok": b["ok"], "inputs_rerun": len(ir),
                   "still_diverging": sum(r["diverges"] for r in ir),
                   "same_class": sum(r["same_class"] for r in ir),
                   "rust_output_same_as_shipped": sum(r["rust_output_same_as_shipped"] for r in ir),
                   "c_output_same_as_shipped": sum(r["c_output_same_as_shipped"] for r in ir),
                   "verdict_unchanged": b["ok"] and bool(ir) and all(r["diverges"] and r["same_class"] for r in ir),
                   "inputs": ir})
        out_defects.append(dd)
    sys.path.insert(0, os.path.join(common.W, "scripts"))
    os.environ.setdefault("CRUST_ARTIFACT_COMMIT", os.environ.get("OUB_GIT_REV", "?"))
    from provenance import stamp  # noqa: E402
    rv = sh(["rustc", "-vV"])[1].strip()
    cv = sh(["cargo", "--version"])[1].strip()
    ins_files = ["defect_ledger.json", "actoruw_summary.json", "actoruw_difftest.json", "actoruw_lib_difftest.json",
                 "actoruw_build.json", "actoruw_lib_build.json", "actoruw_second_rater_memmove.json"]
    out = {
        "_provenance": stamp(image=os.environ.get("OUB_IMAGE")),
        "provenance": {
            "script": "systems/actoruw/arxiv_pinned_toolchain.py",
            "command": os.environ.get("OUB_COMMAND") or " ".join(["python3", "-I"] + sys.argv),
            "git_commit_of_study_repo": os.environ.get("OUB_GIT_REV", "?"),
            "git_note": "scripts/ = systems/actoruw/*.py + systems/enumerate_inputs.py + harness/oracle_ub_common.py + "
                        "scripts/provenance.py, copied from the study repo working tree at that commit plus uncommitted "
                        "changes (this script new; run_in_container.sh RUST_TC_DIR option new); in/ = the shipped "
                        "results files named in inputs",
            "host": os.environ.get("OUB_HOST", platform.system()), "arch": platform.machine(), "kernel": platform.release(),
            "image": os.environ.get("OUB_IMAGE"), "image_id": os.environ.get("OUB_IMAGE_ID"),
            "toolchain_dir_name": os.environ.get("ACTORUW_TC"),
            "rustc_pinned": rv, "cargo_pinned": cv,
            "toolchain_install": "rustup 1.29.1 of the crust-heldout image, `rustup toolchain install 1.94.0 --profile "
                                 "minimal` with RUSTUP_HOME outside ~/.rustup (network on for that step only); the "
                                 "toolchain dir is mounted read-only at /rust (run_in_container.sh RUST_TC_DIR)",
            "pinned_by_artifact": "ACTOR @1b1e70a rust-toolchain.toml: channel = \"1.94.0\", components = [\"rust-src\", "
                                  "\"rustfmt\", \"clippy\"] (installed with --profile minimal: rustc, cargo, rust-std; "
                                  "rust-src, rustfmt and clippy serve ACTOR's own compile-fail tests and lint gates, "
                                  "not a crate build)",
            "shipped_toolchain": dt.get("toolchains"),
            "work_dir": "build/ copied from the shipped run's work dir (~/work/regrade-actoruw) without target/, "
                        "target_tc2/, rust_tc2/ (C oracles and the crates with their resolved Cargo.lock); artifact/ "
                        "copied from there; crates resolved --offline from the host cargo cache (read-only)",
            "inputs": {f"results/{n}": sha(os.path.join(IN, n)) for n in ins_files},
            "seconds": round(time.time() - t0, 1),
            "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
        "rule": next(x for x in __doc__.split("\n\n") if x.startswith("Each exposing input")),
        "summary": {
            "pinned_toolchain": rv.splitlines()[0] if rv else None,
            "counted_defects": len(out_defects),
            "translations_rebuilt": {"exe": sum(1 for _, k in trans if k == "exe"), "lib": sum(1 for _, k in trans if k == "lib")},
            "build_failures": sorted(t for t, (b, _) in res.items() if not b["ok"]),
            "inputs_rerun": sum(d["inputs_rerun"] for d in out_defects),
            "defects_verdict_unchanged": sum(d["verdict_unchanged"] for d in out_defects),
            "defects_verdict_changed": sorted(d["defect_id"] for d in out_defects if not d["verdict_unchanged"]),
            "inputs_rust_output_same_as_shipped": sum(d["rust_output_same_as_shipped"] for d in out_defects),
            "inputs_c_output_same_as_shipped": sum(d["c_output_same_as_shipped"] for d in out_defects),
            "pinned_class_counts": dict(sorted(collections.Counter(
                str(r["pinned_class"]) for d in out_defects for r in d["inputs"]).items())),
        },
        "translations": {t: b for t, (b, _) in sorted(res.items())},
        "defects": out_defects,
    }
    common.dump(a.out, out)
    print(json.dumps(out["summary"], indent=1))


if __name__ == "__main__":
    main()
