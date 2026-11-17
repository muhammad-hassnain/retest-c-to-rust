"""arXiv re-run: ACTOR mutation kills under the UB rule.

systems/actoruw/mutate.py (results/actoruw_mutation.json) counts a mutant as killed at the first input
where, after one confirming re-run, the mutant gives a wrong answer or a crash (judge), whether or not
the C is UB-clean on that input. The defect count applies the UB rule of ub_filter.py (clang-14 and
gcc-11 ASan+UBSan+float-cast-overflow; gcc -O1/-O2, -funsigned-char/-fsigned-char identical to the
oracle; audit 8 failure semantics) to every divergence. This script applies the same rule to the kills:

  1. For every killed mutant, the C variants of its translation are built exactly as ub_filter.py
     builds them (build_variants, the case's own CMake recipe; lib: lib_harness.c_oracle_cmd) and the
     input that killed it (by_input) goes through ub_filter.check_case (with run_variant_capped below in
     place of ub_filter.run_variant: same runs, output capped as difftest.run_one caps it). UB-clean: the
     kill stands.
  2. Otherwise the mutant is regenerated (the plan of mutate.main, same seed; checked field by field
     against the recorded mutants), rebuilt with mutate.build, and re-run over all inputs in order with
     mutate.evaluate's loop (same timeouts, hang rule and confirming re-run). A confirmed wrong answer or
     crash kills it only if check_case finds the C UB-clean on that input; otherwise the scan continues.
     The first candidate must be the recorded by_input (reproduction check). To bound memory, C outputs
     are held for one translation at a time, and for executables a C result over 64 KiB is held as
     sha256 digests (the mutant's result on that input is digested the same way; judge's comparisons
     are byte equalities and a `panicked` substring test, both preserved).

Usage (in container, see harness/arxiv_mutub_run.sh actor):
  python3 -I arxiv_mutate_ubfilter.py -j 12 --results /w/repo/results/actoruw_mutation.json --out /w/out/actor.json
"""
import argparse
import base64
import collections
import hashlib
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import difftest  # noqa: E402
import mutate as M  # noqa: E402
import ub_filter as UF  # noqa: E402
import oracle_ub_common as ou  # noqa: E402


def plan():
    """mutate.main's plan, verbatim: (targets, jobs) with jobs in the order of the recorded mutants."""
    tg = M.targets()
    jobs = []
    for kind, p in tg:
        crate = os.path.join(common.W, "build", p["battery"], p["case"], "rust")
        files = sorted(os.path.relpath(os.path.join(dp, f), crate)
                       for dp, _, fs in os.walk(os.path.join(crate, "src")) for f in fs if f.endswith(".rs"))
        pool = {c: [] for c in M.CLASSES}
        for rel in files:
            src = open(os.path.join(crate, rel)).read()
            for cls, lst in M.sites(src).items():
                pool[cls] += [(rel, pos, old, new, src.count("\n", 0, pos) + 1) for pos, old, new in lst]
        for cls in M.CLASSES:
            lst = sorted(pool[cls])
            rnd = random.Random(f"{M.SEED}:{kind}:{p['battery']}/{p['case']}:{cls}")
            for rel, pos, old, new, line in sorted(rnd.sample(lst, min(M.PER_CLASS, len(lst)))):
                jobs.append((kind, p, {"kind": kind, "translation": f"{p['battery']}/{p['case']}", "class": cls,
                                       "file": rel, "line": line, "pos": pos, "old_tok": old, "new_tok": new,
                                       "old": old.strip(), "new": new.strip()}))
    return tg, jobs


def run_variant_capped(name, binp, inp, timeout=10):
    """ub_filter.run_variant with difftest.run_one's output cap: stdout/stderr go to files limited to
    difftest.OUT_CAP bytes by RLIMIT_FSIZE (a variant that writes more is stopped by SIGXFSZ, as the C
    oracle of the re-test is), instead of pipes held in memory. A variant that loops while printing
    (e.g. a build whose behaviour depends on the optimisation level) otherwise filled 20 GB here.
    Everything else (scratch cwd, env, argv[0], sanitizer env, timeouts, infra retries) is unchanged."""
    wd = tempfile.mkdtemp(prefix="aub_", dir="/tmp")
    io = tempfile.mkdtemp(prefix="aubio_", dir="/tmp")
    try:
        for fn, data in inp["files"].items():
            with open(os.path.join(wd, fn), "wb") as f:
                f.write(base64.b64decode(data))
        for dd in inp["dirs"]:
            os.makedirs(os.path.join(wd, dd), exist_ok=True)
        env = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "HOME": wd}
        stdin = base64.b64decode(inp["stdin_b64"])
        argv = ["driver"] + inp["argv"]
        op, ep = os.path.join(io, "o"), os.path.join(io, "e")

        def run(e, tmo):
            with open(op, "wb") as fo, open(ep, "wb") as fe:
                try:
                    p = subprocess.run(argv, executable=binp, input=stdin, cwd=wd, env=e, stdout=fo, stderr=fe,
                                       timeout=tmo, preexec_fn=difftest._limits)
                    rc, to = p.returncode, False
                except subprocess.TimeoutExpired:
                    rc, to = None, True
            with open(op, "rb") as f:
                o = f.read()
            with open(ep, "rb") as f:
                er = f.read()
            return o, er, rc, to
        if UF.VARIANTS[name][3]:
            e = dict(env)
            e.update(ou.SAN_ENV)
            last = None
            for _ in range(6):
                o, er, rc, to = run(e, timeout * 4)
                last = (o, er, rc, to)
                if not to and ou.is_startup_infra(rc if rc is not None else 0, er):
                    continue
                return {"stdout": o, "stderr": er, "rc": rc, "timed_out": to,
                        "report": ou.san_report(er), "reports": ou.san_reports_all(er), "infra": False}
            o, er, rc, to = last
            return {"stdout": o, "stderr": er, "rc": rc, "timed_out": to, "report": None, "reports": [],
                    "infra": True}
        o, er, rc, to = run(env, timeout)
        return {"stdout": o, "stderr": er, "rc": rc, "timed_out": to}
    finally:
        shutil.rmtree(wd, ignore_errors=True)
        shutil.rmtree(io, ignore_errors=True)


UF.run_variant = run_variant_capped   # check_case looks it up in ub_filter's globals

_VAR, _VLOCK, _UB = {}, collections.defaultdict(threading.Lock), {}


def variants(kind, p):
    key = f"{p['battery']}/{p['case']}"
    with _VLOCK[key]:
        if key not in _VAR:
            bins, logs = UF.build_variants(p["battery"], p["case"], kind)
            _VAR[key] = (bins, logs, [n for n in UF.VARIANTS if n not in bins])
    return _VAR[key]


def ub_verdict(kind, p, x, cr):
    """ub_filter.check_case on one input (cached per translation and input)."""
    key = (f"{p['battery']}/{p['case']}", x["id"])
    if key in _UB:
        return _UB[key]
    bins, _, missing = variants(kind, p)
    if missing:
        v = {"id": x["id"], "counted": False, "ub_reasons": [f"variant did not build: {n}" for n in missing]}
    else:
        div = {"input": x, "class": "mutant_kill_candidate", "c_reproducible": True,
               "c": {"stdout_b64": base64.b64encode((cr["stdout"] or b"")[:4096]).decode(), "rc": cr["rc"]}}
        v = UF.check_case(bins, div)
    _UB[key] = v
    return v


BIG = 1 << 16


def _digest(r):
    """Same equality and the same `b"panicked" in stderr` test as the raw record, in a few bytes."""
    st = r["stderr"] or b""
    return dict(r, stdout=hashlib.sha256(r["stdout"] or b"").hexdigest().encode(),
                stderr=hashlib.sha256(st).hexdigest().encode() + (b"|panicked" if b"panicked" in st else b""),
                digested=True)


def compact(kind, cr):
    """Executables only (judge compares bytes for equality): a C result with more than 64 KiB of output is
    kept as digests, and the mutant's result on that input is digested the same way before judge."""
    if kind == "exe" and len(cr["stdout"] or b"") + len(cr["stderr"] or b"") > BIG:
        return _digest(cr)
    return cr


def evaluate_ubfiltered(kind, p, cbin, ins, cres, mbin):
    """mutate.evaluate, except that a confirmed wrong answer / crash kills only on a UB-clean input."""
    hangs, s2, n, ub_skipped, first_cand = 0, 0, 0, [], None
    for x, cr in zip(ins, cres):
        n += 1
        rr = difftest.run_one(mbin, x, timeout=difftest.TIMEOUT if hangs < 3 else 1)
        v = M.judge(kind, cr, _digest(rr) if cr.get("digested") else rr)
        if v == "hang":
            hangs += 1
        elif v == "stderr_only":
            s2 += 1
        elif v in ("wrong_answer", "crash"):
            c_fresh = difftest.run_one(cbin, x)
            v2 = M.judge(kind, c_fresh, difftest.run_one(mbin, x))   # confirm (flaky C/Rust)
            if v2 in ("wrong_answer", "crash"):
                if first_cand is None:
                    first_cand = x["id"]
                u = ub_verdict(kind, p, x, c_fresh)
                if u["counted"]:
                    return {"status": "killed", "kill_kind": v, "by_input": x["id"], "inputs_run": n,
                            "hangs_before": hangs, "stderr_only_before": s2, "ub_kills_skipped": ub_skipped,
                            "first_candidate_input": first_cand}
                ub_skipped.append({"input": x["id"], "kind": v, "ub_reasons": u["ub_reasons"][:3]})
    st = "undetermined" if hangs else "survived"
    return {"status": st, "inputs_run": n, "hang_inputs": hangs, "stderr_only_inputs": s2,
            "ub_kills_skipped": ub_skipped, "first_candidate_input": first_cand}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-j", type=int, default=8)
    ap.add_argument("--results", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    t0 = time.time()
    orig = json.load(open(a.results))
    om = orig["mutants"]
    tg, jobs = plan()
    assert len(jobs) == len(om), (len(jobs), len(om))
    for (kind, p, m), r in zip(jobs, om):
        for k in ("kind", "translation", "class", "file", "line", "old", "new"):
            assert m[k] == r[k], (k, m, r)
    print(f"plan reproduces the {len(om)} recorded mutants", flush=True)
    killed = [i for i, r in enumerate(om) if r["status"] == "killed"]
    # Memory: C outputs (up to 16 MB per stream and input) are held for ONE translation at a time.
    # step 1: UB verdict of the C on each recorded killing input (the C is run on that input only)
    trans = sorted({om[i]["translation"] for i in killed})
    tmap = {f"{p['battery']}/{p['case']}": (kind, p) for kind, p in tg}
    with ThreadPoolExecutor(a.j) as ex:
        list(ex.map(lambda t: variants(*tmap[t]), trans))
    ins_by_t = {}
    for t in trans:
        ins_by_t[t] = {x["id"]: x for x in M.program_inputs(*tmap[t])}

    def step1(i):
        kind, p, _ = jobs[i]
        key = f"{p['battery']}/{p['case']}"
        x = ins_by_t[key][om[i]["by_input"]]
        cr = difftest.run_one(M.cbin_of(kind, p), x)
        return i, ub_verdict(kind, p, x, cr)
    with ThreadPoolExecutor(a.j) as ex:
        s1 = dict(ex.map(step1, killed))
    ins_by_t.clear()
    need = [i for i in killed if not s1[i]["counted"]]
    print(f"killed {len(killed)}; recorded killing input not UB-clean: {len(need)}", flush=True)

    # step 2: re-scan those mutants, one translation at a time
    def work(args):
        i, cbin, ins, cres = args
        kind, p, m = jobs[i]
        key = f"{p['battery']}/{p['case']}"
        wdir = os.path.join(common.W, "mutub", kind, f"w{threading.get_ident()}", f"{p['battery']}_{p['case']}")
        os.makedirs(wdir, exist_ok=True)
        mbin, art, rc = M.build(kind, p, m, wdir)
        if rc != 0 or not mbin or not os.path.isfile(mbin):
            return i, {"status": "stillborn_now"}
        r = evaluate_ubfiltered(kind, p, cbin, ins, cres, mbin)
        r["first_candidate_is_recorded_by_input"] = r.get("first_candidate_input") == om[i]["by_input"]
        print(f"{r['status']:12s} {key} {m['class']} {m['file']}:{m['line']} {m['old']}->{m['new']}", flush=True)
        return i, r
    s2 = {}
    for t in sorted({om[i]["translation"] for i in need}):
        kind, p = tmap[t]
        ins, cbin = M.program_inputs(kind, p), M.cbin_of(kind, p)
        with ThreadPoolExecutor(a.j) as ex:
            cres = list(ex.map(lambda x: compact(kind, difftest.run_one(cbin, x)), ins))
            s2.update(ex.map(work, [(i, cbin, ins, cres) for i in need if om[i]["translation"] == t]))
        del cres

    rows = []
    for i, r in enumerate(om):
        row = {k: r.get(k) for k in ("kind", "translation", "class", "file", "line", "old", "new", "status",
                                     "kill_kind", "by_input")}
        row["orig_status"] = row.pop("status")
        if r["status"] == "killed":
            u = s1[i]
            row["orig_kill_input_ub_clean"] = u["counted"]
            row["orig_kill_input_ub_reasons"] = u["ub_reasons"][:3]
            row["orig_kill_input_ref_matches_difftest_oracle"] = u.get("ref_matches_difftest_oracle")
            if u["counted"]:
                row["new_status"], row["basis"] = "killed", "recorded killing input is UB-clean"
            else:
                row["rescan"] = s2[i]
                row["new_status"], row["basis"] = s2[i]["status"], "re-scanned; kills only on UB-clean inputs"
        else:
            row["new_status"], row["basis"] = r["status"], "not killed in the original run; a UB filter cannot add a kill"
        rows.append(row)
    counted = [r for r in rows if r["orig_status"] not in ("stillborn", "baseline_excluded")]
    k_old = sum(r["orig_status"] == "killed" for r in counted)
    k_new = sum(r["new_status"] == "killed" for r in counted)
    resc = [r for r in rows if "rescan" in r]
    by_kind = {k: {"compiled": sum(r["kind"] == k for r in counted),
                   "killed_old": sum(r["kind"] == k and r["orig_status"] == "killed" for r in counted),
                   "killed_new": sum(r["kind"] == k and r["new_status"] == "killed" for r in counted)}
               for k in ("exe", "lib")}
    summary = {"compiled": len(counted), "killed_old": k_old, "killed_new": k_new,
               "rate_old": round(k_old / len(counted), 4), "rate_new": round(k_new / len(counted), 4),
               "undetermined_old": sum(r["orig_status"] == "undetermined" for r in counted),
               "undetermined_new": sum(r["new_status"] == "undetermined" for r in counted),
               "by_kind": by_kind,
               "killed_inputs_checked": len(killed),
               "killed_input_not_ub_clean": len(need),
               "killed_input_ub_reason_kinds": dict(collections.Counter(
                   x.split(":")[0] for i in need for x in s1[i]["ub_reasons"])),
               "translations_with_variants_built": len(_VAR),
               "variants_missing": {k: v[2] for k, v in _VAR.items() if v[2]},
               "rescanned": len(resc),
               "rescanned_new_status": dict(collections.Counter(r["new_status"] for r in resc)),
               "rescanned_first_candidate_is_recorded_by_input": sum(
                   1 for r in resc if r["rescan"].get("first_candidate_is_recorded_by_input")),
               "seconds": round(time.time() - t0)}
    sys.path.insert(0, "/w/repo/scripts")
    from provenance import stamp  # noqa: E402
    sh = lambda c: subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()
    prov = {"script": "systems/actoruw/arxiv_mutate_ubfilter.py",
            "command": os.environ.get("ARXIV_COMMAND") or " ".join(["python3"] + sys.argv),
            "git_commit_of_study_repo": os.environ.get("CRUST_ARTIFACT_COMMIT"),
            "git_note": "study repo working tree (Mac) at that commit plus uncommitted files, this script new; rsynced "
                        "to the run host without .git; scripts/ = systems/actoruw/*.py + harness/oracle_ub_common.py",
            "host": "Linux x86_64 box (20 cores)", "arch": platform.machine(), "kernel": platform.release(),
            "image": os.environ.get("ARXIV_IMAGE"), "image_id": os.environ.get("ARXIV_IMAGE_ID"),
            "cpus": f"docker --cpus {a.j}", "toolchains": ou.toolchains() | {"rustc_actor": sh("rustc -V")},
            "work_dir": "build/ and out/{difftest,lib_difftest,lib_scan,build,lib_build}.json copied from the original "
                        "run's work dir (~/work/actor-mut); artifact/ mounted read-only from there",
            "inputs": {"results/actoruw_mutation.json": hashlib.sha256(open(a.results, "rb").read()).hexdigest()},
            "ub_variants": {k: {"cc": v[0], "cflags": v[1], "ldflags": v[2]} for k, v in UF.VARIANTS.items()},
            "positive_controls": ou.positive_controls(os.path.join(common.W, "build", "_ub_controls_arxiv")),
            "date": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    out = {"_provenance": stamp(image=os.environ.get("ARXIV_IMAGE")), "provenance": prov,
           "rule": __doc__.strip().split("\n\n")[1], "summary": summary, "mutants": rows}
    common.dump(a.out, out)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
