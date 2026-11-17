#!/usr/bin/env python3
"""arXiv version, issues.md item 34: SACTOR's counted defects rebuilt with the toolchain SACTOR pins.

SACTOR pins nightly-2024-12-18 (rust-toolchain.toml in sactor and sactor-datasets). The shipped
re-grade built every translation with nightly-2024-09-03 and nightly-2025-05-10 (notes.md,
Deviations). This script re-runs, with the pinned toolchain as the only Rust toolchain, the three
shipped stages that expose the five counted SACTOR defects (results/defect_ledger.json):

  libogg  libogg/run.py: all 442 bitwise and 343 framing call sequences    (L1, L2)
  crust   crust/run.py --only Holdem-Odds,c-string,fs_c: SACTOR's own tests (C1)
  grid    crust/grid.py --only Holdem-Odds,c-string,fs_c: per-function grids (C1, C2, C3)

The stage scripts run unchanged. This wrapper imports sactor_common, sets TOOLCHAINS to the pinned
toolchain alone (mounted at /opt/tc/nightly-2024-12-18 by run_docker.sh with SACTOR_EXTRA_TC) and runs
the stage script as __main__, so the builds (SACTOR's build_attempt crate, debug and release), the
inputs, the C variants, the comparison and the UB rule are the shipped ones. `compare` then puts each
defect translation's UB-clean divergences under the pinned builds next to the shipped ones (same
profile, primary nightly-2024-09-03, and the second nightly-2025-05-10). A defect's verdict is
unchanged when, in both profiles, the pinned build diverges from the C on exactly the shipped UB-clean
cases with the same outcome (wrong output vs crash).

Usage (inside the container: SACTOR_EXTRA_TC=nightly-2024-12-18=<host dir> bash systems/sactor/run_docker.sh ...):
  python3 -I systems/sactor/arxiv_pinned_toolchain.py all --out /out/arxiv_sactor_pinned_toolchain.json
  (= stage libogg, stage crust, stage grid into /out/pin_*.json, then compare)
"""
import argparse
import collections
import json
import os
import platform
import runpy
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sactor_common as sc  # noqa: E402

PINNED = sc.SACTOR_PINNED_TC
PINNED_DIR = f"/opt/tc/{PINNED}"
SAMPLES = "Holdem-Odds,c-string,fs_c"
STAGES = {
    "libogg": (os.path.join(HERE, "libogg", "run.py"), []),
    "crust": (os.path.join(HERE, "crust", "run.py"), ["--only", SAMPLES]),
    "grid": (os.path.join(HERE, "crust", "grid.py"), ["--only", SAMPLES]),
}
SHIPPED = {"libogg": "sactor_libogg.json", "crust": "sactor_crust_owntests.json", "grid": "sactor_crust_grid.json"}
# defect -> where the shipped run exposed it (results/defect_ledger.json example_input / exposure_breakdown)
DEFECTS = {
    "SACTOR-L1": {"stage": "libogg", "file": "bitwise", "model": "gpt-4o"},
    "SACTOR-L2": {"stage": "libogg", "file": "framing", "model": "gpt-4o"},
    "SACTOR-C1": {"stage": "crust", "sample": "Holdem-Odds"},
    "SACTOR-C2": {"stage": "crust", "sample": "c-string"},
    "SACTOR-C3": {"stage": "crust", "sample": "fs_c"},
}
SHIPPED_TCS = ["nightly-2024-09-03", "nightly-2025-05-10"]
PROFILES = ["debug", "release"]


def stage(name, rest):
    """Run one shipped stage script with TOOLCHAINS = {pinned} (in this process)."""
    if not os.path.isfile(os.path.join(PINNED_DIR, "bin", "rustc")):
        raise SystemExit(f"{PINNED_DIR}/bin/rustc missing: run_docker.sh needs SACTOR_EXTRA_TC={PINNED}=<dir>")
    sc.TOOLCHAINS = {PINNED: PINNED_DIR}
    sc.PRIMARY_TC = PINNED
    orig = sc.provenance

    def prov(script, command, extra=None):
        p = orig(script, command, extra)
        p["toolchains"]["rust_pinned_by_artifact"] = f"{PINNED} (used: the only toolchain of this run)"
        p["arxiv_wrapper"] = ("systems/sactor/arxiv_pinned_toolchain.py stage " + name +
                              f": sactor_common.TOOLCHAINS = {{{PINNED!r}: {PINNED_DIR!r}}}; stage script unchanged")
        return p
    sc.provenance = prov
    script, fixed = STAGES[name]
    sys.argv = [script] + fixed + rest
    runpy.run_path(script, run_name="__main__")


def outcome(d):
    """Outcome of one divergence record: 'crash' (signal, timeout or Rust panic exit 101) or 'wrong'."""
    ex = d.get("exit")
    if d.get("timed_out") or (isinstance(ex, int) and (ex < 0 or ex == 101)):
        return "crash"
    return "wrong"


def libogg_div(run, file, key):
    """{case id: divergence descriptor} for UB-clean cases where build `key` differs from the C ref."""
    fd = run["files"][file]
    out = {}
    for r in fd["rows"]:
        v = r["rust"].get(key)
        if v is None:
            return None
        if v == "agree" or r["c_ub"]:
            continue
        out[r["id"]] = {"outcome": outcome(v), "exit": v["exit"], "timed_out": v["timed_out"],
                        "stdout_differs": v["stdout_differs"], "stderr_differs": v["stderr_differs"],
                        "first_diff": v["first_diff"]}
    return out


def crust_div(own, grid, sample, tc, prof):
    """UB-clean divergences of one CRUST-Bench translation under one build: own-test cases and grid inputs."""
    out = {}
    so = own["samples"].get(sample, {})
    k = f"unidiomatic:{tc}:{prof}"
    if not so.get("rust_builds", {}).get(k, {}).get("ok"):
        return None
    for c in so.get("cases", []):
        v = c.get("rust", {}).get(k)
        if v is None or c.get("skipped") or c["c_ub"] or v.get("agree"):
            continue
        out[f"own:{c['case']}:{c['obj']}"] = {"outcome": outcome(v), "exit": v["exit"], "timed_out": v["timed_out"],
                                              "sactor_check_pass": v.get("sactor_check_pass"),
                                              "first_diff_stdout": v.get("first_diff_stdout"),
                                              "first_diff_stderr": v.get("first_diff_stderr")}
    sg = grid["samples"].get(sample, {})
    gk = f"rust:{tc}:{prof}"
    for fn, rec in sg.get("per_function", {}).items():
        v = rec["rust"].get(gk)
        if v is None or v == "agree":
            continue
        for dv in v["divergent_inputs"]:
            if dv.get("rust_timed_out"):
                continue   # listed for review by grid.py, never sent to the UB checks
            ub = rec.get("ub", {}).get(str(dv["input"]), rec.get("ub", {}).get(dv["input"], {}))
            if ub.get("c_ub"):
                continue
            rc = dv["rust"][1]
            out[f"grid:{fn}:{dv['input']}"] = {"outcome": "crash" if (rc < 0 or rc == 101) else "wrong",
                                               "c_stdout_rc": dv["c"], "rust_stdout_rc": dv["rust"][:2],
                                               "rust_stderr_tail": dv["rust"][2]}
    return out


def compare_sets(shipped, pinned, exact_keys):
    """Same cases, same outcome per case; also whether the recorded outputs are identical."""
    if shipped is None or pinned is None:
        return {"comparable": False}
    s, p = set(shipped), set(pinned)
    same_outcome = s == p and all(shipped[k]["outcome"] == pinned[k]["outcome"] for k in s)
    diff_out = sorted(k for k in s & p if any(shipped[k].get(x) != pinned[k].get(x) for x in exact_keys))
    fields = collections.Counter(x for k in diff_out for x in exact_keys if shipped[k].get(x) != pinned[k].get(x))
    return {"comparable": True, "shipped_cases": len(s), "pinned_cases": len(p), "only_shipped": sorted(s - p),
            "only_pinned": sorted(p - s), "same_cases_same_outcome": same_outcome,
            "outcomes_pinned": dict(sorted(collections.Counter(pinned[k]["outcome"] for k in p).items())),
            "cases_with_other_recorded_output": diff_out,
            "recorded_fields_that_differ": dict(sorted(fields.items()))}


def compare(a):
    load = lambda p: json.load(open(p))
    pin = {k: load(getattr(a, k)) for k in ("libogg", "crust", "grid")}
    shp = {k: load(os.path.join(a.shipped, SHIPPED[k])) for k in ("libogg", "crust", "grid")}
    ledger = load(a.ledger)
    rows = {r["defect_id"]: r for r in ledger["rows"] if r["system"] == "SACTOR"}
    counted = sorted(d for d, r in rows.items() if r["count_status"] == "counted")
    if counted != sorted(DEFECTS):
        raise SystemExit(f"counted SACTOR defects in the ledger {counted} != {sorted(DEFECTS)}")
    res = {}
    for did, spec in DEFECTS.items():
        r = rows[did]
        rec = {"translation": r["translation"], "severity": r["severity"], "ledger_exposure_count": r["exposure_count"],
               "builds": {}}
        for prof in PROFILES:
            if spec["stage"] == "libogg":
                f, m = spec["file"], spec["model"]
                p = libogg_div(pin["libogg"], f, f"rust:{m}:{PINNED}:{prof}")
                b = pin["libogg"]["translations"][f"{m}/{f}.c/unidiomatic"]["builds"][f"{PINNED}:{prof}"]
                build = {"ok": b["ok"], "sactor_own_test_pass": b.get("sactor_test", {}).get("pass"),
                         **({} if b["ok"] else {"errors": b.get("errors")})}
                vs = {tc: compare_sets(libogg_div(shp["libogg"], f, f"rust:{m}:{tc}:{prof}"), p,
                                       ("exit", "timed_out", "stdout_differs", "stderr_differs", "first_diff"))
                      for tc in SHIPPED_TCS}
            else:
                s = spec["sample"]
                p = crust_div(pin["crust"], pin["grid"], s, PINNED, prof)
                rb = pin["crust"]["samples"][s]["rust_builds"].get(f"unidiomatic:{PINNED}:{prof}", {})
                own = [c.get("rust", {}).get(f"unidiomatic:{PINNED}:{prof}", {}) for c in pin["crust"]["samples"][s]["cases"]]
                build = {"ok": rb.get("ok"), "sactor_own_test_pass": [o.get("sactor_check_pass") for o in own],
                         **({} if rb.get("ok") else {"errors": rb.get("errors"), "log_tail": rb.get("log_tail")})}
                vs = {tc: compare_sets(crust_div(shp["crust"], shp["grid"], s, tc, prof), p,
                                       ("exit", "timed_out", "sactor_check_pass", "first_diff_stdout", "first_diff_stderr",
                                        "c_stdout_rc", "rust_stdout_rc", "rust_stderr_tail"))
                      for tc in SHIPPED_TCS}
            rec["builds"][f"{PINNED}:{prof}"] = {"build": build, "ub_clean_divergent_cases": None if p is None else len(p),
                                                 "vs_shipped": vs}
        primary = [rec["builds"][f"{PINNED}:{prof}"]["vs_shipped"][SHIPPED_TCS[0]] for prof in PROFILES]
        rec["verdict_unchanged"] = all(x.get("comparable") and x["same_cases_same_outcome"] for x in primary)
        rec["recorded_outputs_identical_to_shipped_primary"] = all(
            x.get("comparable") and not x["cases_with_other_recorded_output"] for x in primary)
        res[did] = rec
    # every translation the pinned runs built (incl. ones without a counted defect), for build failures
    builds = {}
    for t, info in pin["libogg"]["translations"].items():
        builds[f"libogg {t}"] = {k: v["ok"] for k, v in info["builds"].items()}
    for s, r in pin["crust"]["samples"].items():
        builds[f"CRUST-Bench {s}"] = {k: v.get("ok") for k, v in r.get("rust_builds", {}).items()}
    shipped_builds = {}
    for t, info in shp["libogg"]["translations"].items():
        shipped_builds[f"libogg {t}"] = {k: v["ok"] for k, v in info["builds"].items()}
    for s in SAMPLES.split(","):
        shipped_builds[f"CRUST-Bench {s}"] = {k: v.get("ok") for k, v in shp["crust"]["samples"][s].get("rust_builds", {}).items()}
    fails = sorted(f"{t} [{k}]" for t, v in builds.items() for k, ok in v.items() if not ok)
    sys.path.insert(0, os.path.join(sc.REPO, "scripts"))
    os.environ.setdefault("CRUST_ARTIFACT_COMMIT", os.environ.get("SACTOR_GIT_REV", "?"))
    from provenance import stamp  # noqa: E402
    out = {
        "_provenance": stamp(image=os.environ.get("SACTOR_IMAGE")),
        "provenance": {
            "script": "systems/sactor/arxiv_pinned_toolchain.py",
            "command": os.environ.get("SACTOR_COMMAND") or " ".join(["python3", "-I"] + sys.argv),
            "toolchain_install": f"rustup 1.29.1 of the crust-heldout image, `rustup toolchain install {PINNED} --profile "
                                 "minimal` with RUSTUP_HOME outside ~/.rustup (network on for that step only); the "
                                 f"toolchain dir is mounted read-only at {PINNED_DIR} (run_docker.sh SACTOR_EXTRA_TC)",
            "git_commit_of_study_repo": os.environ.get("SACTOR_GIT_REV", "?"),
            "git_note": "harness/oracle_ub_common.py, systems/sactor/, scripts/provenance.py and the shipped results files "
                        "named in shipped_inputs copied from the study repo working tree at that commit plus uncommitted "
                        "changes (this script new; run_docker.sh SACTOR_EXTRA_TC option new)",
            "host": os.environ.get("SACTOR_HOST", platform.system()), "arch": platform.machine(),
            "image": os.environ.get("SACTOR_IMAGE"), "image_id": os.environ.get("SACTOR_IMAGE_ID"),
            "rustc_pinned": sc.sh([f"{PINNED_DIR}/bin/rustc", "-vV"])[1].strip(),
            "cargo_pinned": sc.sh([f"{PINNED_DIR}/bin/cargo", "--version"])[1].strip(),
            "pinned_by_artifact": "sactor and sactor-datasets rust-toolchain.toml: channel = \"nightly-2024-12-18\", "
                                  "components = [\"rustfmt\", \"clippy\", \"rust-analyzer\"] (installed with --profile "
                                  "minimal: rustc, cargo, rust-std; the listed components are lint/format/IDE tools "
                                  "and take no part in a build)",
            "stage_outputs": {k: {"path": getattr(a, k), "provenance": pin[k]["provenance"]} for k in pin},
            "shipped_inputs": {k: f"results/{SHIPPED[k]}" for k in SHIPPED} | {"ledger": "results/defect_ledger.json"},
            "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
        "rule": next(x for x in __doc__.split("\n\n") if x.startswith("The stage scripts")),
        "summary": {
            "pinned_toolchain": PINNED,
            "counted_defects": len(DEFECTS),
            "verdict_unchanged": sorted(d for d, r in res.items() if r["verdict_unchanged"]),
            "verdict_changed": sorted(d for d, r in res.items() if not r["verdict_unchanged"]),
            "recorded_outputs_identical_to_shipped_primary": sorted(
                d for d, r in res.items() if r["recorded_outputs_identical_to_shipped_primary"]),
            "pinned_build_failures": fails,
            "shipped_build_failures": sorted(f"{t} [{k}]" for t, v in shipped_builds.items() for k, ok in v.items() if not ok),
        },
        "defects": res,
        "builds_pinned": builds,
    }
    sc.dump(a.out, out)
    print(json.dumps(out["summary"], indent=1))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("stage")
    s.add_argument("name", choices=sorted(STAGES))
    s.add_argument("rest", nargs=argparse.REMAINDER, help="arguments for the stage script (after --)")
    for name in ("compare", "all"):
        c = sub.add_parser(name)
        c.add_argument("--libogg", default="/out/pin_libogg.json")
        c.add_argument("--crust", default="/out/pin_crust.json")
        c.add_argument("--grid", default="/out/pin_grid.json")
        c.add_argument("--shipped", default=os.path.join(sc.REPO, "results"))
        c.add_argument("--ledger", default=os.path.join(sc.REPO, "results", "defect_ledger.json"))
        c.add_argument("--out", default="/out/arxiv_sactor_pinned_toolchain.json")
    a = ap.parse_args()
    if a.cmd == "stage":
        stage(a.name, [x for x in a.rest if x != "--"])
    elif a.cmd == "compare":
        compare(a)
    else:
        for name in ("libogg", "crust", "grid"):
            rc = subprocess.call([sys.executable, "-I", os.path.abspath(__file__), "stage", name, "--",
                                  "--out", getattr(a, name)])
            if rc != 0:
                raise SystemExit(f"stage {name} failed: exit {rc}")
        compare(a)


if __name__ == "__main__":
    main()
