"""arXiv re-run: FLOURINE scalar mutation kills under the UB rule.

harness/mutate_full_flourine.py (results/flourine_mutation.json) calls batch_diff.tally with no
sanitizer, so a mutant counts as killed on any input where its output differs from the C's
(gcc -O1 -fwrapv), whether or not the C is UB-clean there. The defect count applies the full UB
rule to every divergence (harness/oracle_ub_flourine*.py). This script applies the same rule to the
mutation kills.

  Phase c (crust-heldout: gcc 11.4 + clang 14). For each of the 18 scalar benchmarks of
    mutate_full_flourine.benchmarks (run.py's selection, enumerate_cases(cap=2000)), the harness's
    own C driver (run.build_c writes p.c) is built in every variant of oracle_ub_flourine.CC_VARIANTS
    and run over EVERY grid input (oracle_ub_flourine.c_side: resumable sanitizer batches), giving
    per-input flags (oracle_ub_flourine.per_input_flags). An input is UB-clean under the uniform rule
    of oracle_ub_flourine_current.rule_clean (audit 8; copied below): no clang-14 or gcc-11
    ASan+UBSan+float-cast-overflow report (built without -fwrapv), both sanitizer runs reached the
    input and print the oracle's line, gcc -O0 and -O2 (-fwrapv) print the -O1 line, and
    -funsigned-char / -fsigned-char print the same line. The strict view of
    oracle_ub_flourine.summarize_inputs (any flag at all, adding -fwrapv dependence, sanitizers with
    -fwrapv and bit-field signedness; what scripts/build_defect_ledger.py reads) is recorded too.
  Phase m (audit3-stable186: rustc 1.86.0 + gcc 11.4, the image of the original run). Every mutant
    of results/flourine_mutation.json is rebuilt exactly as mutate_full_flourine._job builds it
    (mutate_full.apply_mutant, run.build_rust = rustc -O, fixed per-worker dir) and run over the grid
    once (batch_diff.eval_cases against run.build_c's oracle). From the per-input outcomes:
      reproduced  the counts batch_diff.tally gives with no sanitizer (the original verdict), which
                  must equal the recorded tally;
      new verdict killed_value if some UB-clean input gives a different value, else killed_crash if
                  some UB-clean input gives a Rust panic, else hang / ub_only / survived.
    The unmutated translations are re-run too (they must agree with the C on every input).
  Also recorded, not part of the UB rule: the verdict when inputs with min > max (C parameters named
  *min*/*max*, oracle_ub_flourine_current.degenerate_fn) are not counted either, as the defect count
  does for FLOURINE.

  python3 -I harness/arxiv_mutate_full_flourine_ubfilter.py --phase c --jobs 12 --work /w/work --out /w/out/flourine.json
  python3 -I harness/arxiv_mutate_full_flourine_ubfilter.py --phase m --jobs 12 --work /w/work --out /w/out/flourine.json

Optional flags (phase m; the defaults reproduce the run above):
  --profile fuzz  builds every mutant and every unmutated translation with run.RUSTC_PROFILE_FLAGS["fuzz"]
    (FLOURINE's own check profile: opt-level 3, debug assertions and overflow checks on)
    instead of rustc -O, keeps each translation's per-input outcomes, and writes a different summary
    (phase_m_profile): the kill counts under the UB rule, with and without the min > max inputs, for each
    way of treating an unmutated translation that itself diverges in that profile.
  --cap-output-bytes N  runs every C and Rust binary with stdout/stderr written to files under
    RLIMIT_FSIZE = N (a run that writes more is killed by SIGXFSZ) instead of in-memory pipes.
  --reuse-raw  phase m with --profile: re-summarize the per-job results saved by an earlier run.
"""
import argparse
import collections
import hashlib
import json
import multiprocessing as mp
import os
import platform
import re
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mutate_full as MF  # noqa: E402
import mutate_full_flourine as MFF  # noqa: E402  (benchmarks, _build, FR = systems/flourine/run.py)
from batch_diff import eval_cases  # noqa: E402

REPO = MF.REPO
FR = MFF.FR


def rule_clean(flags):
    """oracle_ub_flourine_current.rule_clean (audit 8 uniform rule), copied."""
    bad = ("san_clang", "san_gcc", "san_clang_not_clean", "san_gcc_not_clean", "san_clang_differs", "san_gcc_differs",
           "alt_o0_differs", "alt_o2_differs", "uchar_differs", "schar_differs")
    return not any(b in flags for b in bad)


def c_param_names(cfile, name):
    """oracle_ub_flourine_current.c_param_names, copied."""
    src = open(cfile, errors="replace").read()
    m = re.search(r"\b" + re.escape(name) + r"\s*\(([^)]*)\)\s*\{", src)
    if not m:
        return []
    return [re.split(r"[\s\*]+", p.strip())[-1] for p in m.group(1).split(",") if p.strip()]


def degenerate_fn(params):
    """oracle_ub_flourine_current.degenerate_fn, copied: index pair (min, max) if the C parameters are
    named like a range, else None."""
    lo = [i for i, p in enumerate(params) if "min" in p.lower()]
    hi = [i for i, p in enumerate(params) if "max" in p.lower()]
    return (lo[0], hi[0]) if lo and hi else None


def sha(b):
    return hashlib.sha256(b).hexdigest()


def sha_file(p):
    with open(p, "rb") as f:
        return sha(f.read())


def lines_of(cases):
    return [" ".join(str(x) for x in row) for row in cases]


# ------------------------------------------------------------------------------------ phase c
def _c_job(args):
    import oracle_ub_flourine as OUF  # needs clang: phase c only
    name, cfile, sig, cases, d = args
    if os.path.isdir(d):
        shutil.rmtree(d)
    os.makedirs(d)
    t0 = time.time()
    FR.build_c(name, cfile, sig, d)                  # writes d/p.c (+ its own -O1 -fwrapv build d/cb)
    pc = os.path.join(d, "p.c")
    bins = OUF.build_variants(pc, d)
    lines = lines_of(cases)
    c = OUF.c_side(bins, lines)
    ref = c["ref"]["outs"]
    flags = [OUF.per_input_flags(i, c, ref[i]) for i in range(len(lines))]
    deg = degenerate_fn(c_param_names(cfile, name))
    return {"name": name, "cfile": os.path.relpath(cfile, MFF.FL), "ptypes": sig["ptypes"], "inputs": len(lines),
            "lines_sha256": sha("\n".join(lines).encode()), "ref_outs_sha256": sha("\n".join(map(str, ref)).encode()),
            "ref_outs": ref, "pc_sha256": sha_file(pc),
            "clean_uniform": [i for i in range(len(lines)) if rule_clean(flags[i])],
            "clean_strict": [i for i in range(len(lines)) if not flags[i]],
            "degenerate_min_gt_max_params": deg,
            "degenerate_inputs": [i for i, row in enumerate(cases) if deg and row[deg[0]] > row[deg[1]]],
            "not_clean_flags": {str(i): flags[i] for i in range(len(lines)) if flags[i]},
            "sanitizer_restarts": {s: sum(1 for x in c[s]["status"] if x == "report") for s in OUF.SANS},
            "seconds": round(time.time() - t0, 1)}


def phase_c(a):
    bms = list(MFF.benchmarks(2000))
    jobs = [(name, cfile, sig, cases, os.path.join(a.work, "c", name)) for name, cfile, sig, cases, _ in bms]
    with mp.Pool(a.jobs) as p:
        res = []
        for r in p.imap_unordered(_c_job, jobs):
            print(f"[c] {r['name']}: {r['inputs']} inputs, uniform-clean {len(r['clean_uniform'])}, strict-clean "
                  f"{len(r['clean_strict'])}, {r['seconds']}s", flush=True)
            res.append(r)
    res.sort(key=lambda r: r["name"])
    import oracle_ub_common as ou
    import oracle_ub_flourine as OUF
    out = {"phase_c": {"benchmarks": res,
                       "c_builds": {k: [v[0]] + v[1] for k, v in OUF.CC_VARIANTS.items()},
                       "toolchains": ou.toolchains(), "image": os.environ.get("ARXIV_IMAGE"),
                       "image_id": os.environ.get("ARXIV_IMAGE_ID"),
                       "positive_controls": ou.positive_controls(os.path.join(a.work, "controls")),
                       "date": time.strftime("%Y-%m-%dT%H:%M:%S%z")}}
    os.makedirs(a.work, exist_ok=True)
    with open(os.path.join(a.work, "phase_c.json"), "w") as f:
        json.dump(out, f)


# ------------------------------------------------------------------------------------ phase m
_W = {}

# --cap-output-bytes: batch_diff.run_batch / run_one with the output written to files under RLIMIT_FSIZE
BD = sys.modules[eval_cases.__module__]
_CAP = {"bytes": 0, "hits": 0}


def _run_capped(binp, inp, timeout):
    """subprocess.run([binp], input=inp, timeout=timeout) with stdout/stderr in files capped at _CAP bytes
    each (Popen restores SIGXFSZ to its default, so a run that writes more is killed). Returns
    (returncode, stdout bytes, stderr bytes); raises TimeoutExpired after killing the child, as run does."""
    cap = _CAP["bytes"]
    with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
        p = subprocess.Popen([binp], stdin=subprocess.PIPE, stdout=fo, stderr=fe,
                             preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (cap, cap)))
        try:
            p.communicate(inp, timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            raise
        if p.returncode == -signal.SIGXFSZ:
            _CAP["hits"] += 1
        fo.seek(0)
        fe.seek(0)
        return p.returncode, fo.read(), fe.read()


def run_batch_capped(binp, cases, timeout=12):
    """batch_diff.run_batch with the output cap."""
    inp = ("\n".join(" ".join(str(x) for x in row) for row in cases) + "\n").encode()
    try:
        rc, out, _ = _run_capped(binp, inp, timeout)
        return rc, out.decode("latin1").splitlines()
    except subprocess.TimeoutExpired:
        return -1, []


def run_one_capped(binp, row, timeout=2):
    """batch_diff.run_one with the output cap."""
    inp = (" ".join(str(x) for x in row) + "\n").encode()
    try:
        rc, out, err = _run_capped(binp, inp, timeout)
        lines = out.decode("latin1").splitlines()
        return (lines[0] if lines else None), rc, False, err.decode("latin1", "replace")
    except subprocess.TimeoutExpired:
        return None, -1, True, ""


def _init(q, cinfo, opts=None):
    MFF._init(q)
    _W.update(cinfo=cinfo)
    if opts:                                    # only with --profile / --cap-output-bytes
        _W.update(opts)
        if opts.get("profile"):
            FR.RUST_PROFILE = opts["profile"]
        if opts.get("cap_output_bytes"):
            _CAP["bytes"] = opts["cap_output_bytes"]
            BD.run_batch, BD.run_one = run_batch_capped, run_one_capped


KCODE = {"agree": "a", "value": "v", "crash": "c", "hang": "h", "c_missing": "m", "skip": "s"}


def outcomes(binc, binr, cases):
    """Per-input outcome exactly as batch_diff.tally classifies with no sanitizer and no -O0 build:
    skip (__PARSE__), agree, c_missing (counted as ub), hang, crash (rust_panic), value."""
    out = []
    for row, cl, rl in eval_cases(binc, binr, cases):
        if cl == "__PARSE__" or rl == "__PARSE__":
            out.append(("skip", cl, rl))
        elif cl is not None and cl != "__TIMEOUT__" and cl == rl:
            out.append(("agree", cl, rl))
        elif cl is None or cl == "__TIMEOUT__":
            out.append(("c_missing", cl, rl))
        elif rl == "__TIMEOUT__":
            out.append(("hang", cl, rl))
        elif rl is None or rl == "__PANIC__":
            out.append(("crash", cl, rl))
        else:
            out.append(("value", cl, rl))
    return out


def _m_job(job):
    name, cfile, sig, sig2, rs, mut = job["name"], job["cfile"], job["sig"], job["sig2"], job["rs"], job["mutant"]
    ci = _W["cinfo"][name]
    cases = MFF.enumerate_cases(sig["ptypes"], cap=2000)
    binc = MFF._cbin(name, cfile, sig)
    src = open(rs, errors="replace").read()
    t0 = time.time()
    hits0 = _CAP["hits"]
    b = MFF._build(name, MF.apply_mutant(src, mut) if mut else src, sig2)
    rec = {"id": job["id"], "tag": job["tag"], "name": name}
    if not b:
        rec.update(status="noncompile", seconds=round(time.time() - t0, 2))
        return rec
    rec["bin_sha"] = sha_file(b)
    oc = outcomes(binc, b, cases)
    kinds = [k for k, _, _ in oc]
    if _W.get("profile"):                       # --profile: keep the per-input outcomes for phase_m_profile
        rec["kinds"] = "".join(KCODE[k] for k in kinds)
        rec["rust_profile"] = FR.RUST_PROFILE
    if _W.get("cap_output_bytes"):
        rec["output_cap_hits"] = _CAP["hits"] - hits0
    c_lines = [cl for _, cl, _ in oc]
    rec["c_outs_equal_phase_c_ref"] = [x if x is not None else None for x in c_lines] == \
        [x if x is not None else None for x in ci["ref_outs"]]
    tal = {"value": kinds.count("value"), "rust_panic": kinds.count("crash"), "ub": kinds.count("c_missing"),
           "timeout": 0, "agree": kinds.count("agree"), "rust_hang": kinds.count("hang")}
    rec["tally_no_sanitizer"] = tal
    rec["status_reproduced"] = ("killed_value" if tal["value"] else "killed_crash" if tal["rust_panic"]
                                else "hang" if tal["rust_hang"] else "survived")
    cu, cs, dg = set(ci["clean_uniform"]), set(ci["clean_strict"]), set(ci["degenerate_inputs"])

    def verdict(ok):
        v = [i for i, k in enumerate(kinds) if k == "value" and ok(i)]
        c = [i for i, k in enumerate(kinds) if k == "crash" and ok(i)]
        h = [i for i, k in enumerate(kinds) if k == "hang" and ok(i)]
        st = ("killed_value" if v else "killed_crash" if c else "hang" if h
              else "ub_only" if (tal["value"] or tal["rust_panic"]) else "survived")
        return st, len(v), len(c), (cases[v[0]] if v else cases[c[0]] if c else None)

    for key, ok in (("uniform", lambda i: i in cu), ("strict", lambda i: i in cs),
                    ("uniform_and_not_min_gt_max", lambda i: i in cu and i not in dg)):
        st, nv, nc, first = verdict(ok)
        rec[f"status_{key}"] = st
        rec[f"value_inputs_{key}"] = nv
        rec[f"crash_inputs_{key}"] = nc
        rec[f"first_killing_input_{key}"] = first
    rec["value_inputs_not_ub_clean_uniform"] = sum(1 for i, k in enumerate(kinds) if k == "value" and i not in cu)
    rec["seconds"] = round(time.time() - t0, 2)
    return rec


def c_side_summary(b):
    """Compact per-benchmark record of phase c: the inputs that are NOT UB-clean, flag counts, examples."""
    n = b["inputs"]
    cu, cs = set(b["clean_uniform"]), set(b["clean_strict"])
    fl = b["not_clean_flags"]
    kinds = collections.Counter(k for f in fl.values() for k in f)
    reps = collections.Counter(re.sub(r"\d+", "N", v.split("runtime error:")[-1].strip())[:100]
                               for f in fl.values() for k, v in f.items() if k in ("san_clang", "san_gcc"))
    return {k: b[k] for k in ("name", "cfile", "ptypes", "inputs", "lines_sha256", "ref_outs_sha256", "pc_sha256",
                              "degenerate_min_gt_max_params", "sanitizer_restarts", "seconds")} | {
        "ub_clean_inputs_uniform": len(cu), "ub_clean_inputs_strict": len(cs),
        "not_ub_clean_inputs_uniform": [i for i in range(n) if i not in cu],
        "not_ub_clean_inputs_strict_only": [i for i in range(n) if i in cu and i not in cs],
        "min_gt_max_inputs": len(b["degenerate_inputs"]),
        "flag_counts": dict(kinds), "sanitizer_report_kinds": dict(reps),
        "flag_examples": dict(list(fl.items())[:5])}


def phase_m(a):
    pc = json.load(open(os.path.join(a.work, "phase_c.json")))["phase_c"]
    cinfo = {b["name"]: b for b in pc["benchmarks"]}
    orig = json.load(open(a.results))
    bms = {name: (cfile, sig, cases) for name, cfile, sig, cases, _ in MFF.benchmarks(2000)}
    # the grid must be the one phase c checked
    grid_check = {n: sha("\n".join(lines_of(bms[n][2])).encode()) == cinfo[n]["lines_sha256"] for n in bms}
    assert all(grid_check.values()), grid_check
    jobs = []
    for b in orig["baseline"]:
        if b["status"] != "ok":
            continue
        name = b["name"]
        rs = os.path.join(MFF.FL, "translations_only", "success", "c", b["tag"])
        sig2 = FR.parse_rust_fn(open(rs, errors="replace").read(), name)
        jobs.append({"id": "BASE:" + b["tag"], "tag": b["tag"], "name": name, "cfile": bms[name][0], "sig": bms[name][1],
                     "sig2": sig2, "rs": rs, "mutant": None, "orig": b})
    for m in orig["mutants"]:
        name = m["name"]
        rs = os.path.join(MFF.FL, "translations_only", "success", "c", m["tag"])
        sig2 = FR.parse_rust_fn(open(rs, errors="replace").read(), name)
        jobs.append({"id": m["id"], "tag": m["tag"], "name": name, "cfile": bms[name][0], "sig": bms[name][1],
                     "sig2": sig2, "rs": rs, "mutant": m["mutant"], "orig": m})
    print(f"[m] {len(jobs)} jobs ({len(orig['mutants'])} mutants)", flush=True)
    os.environ["MFULL_FL_SCRATCH"] = os.path.join(a.work, "m_scratch")
    MFF.SCRATCH = os.environ["MFULL_FL_SCRATCH"]
    opts = None
    if a.profile != "release" or a.cap_output_bytes:
        opts = {"profile": a.profile if a.profile != "release" else None, "cap_output_bytes": a.cap_output_bytes}
    raw = os.path.join(a.work, f"m_raw_{a.profile}.json")
    if a.reuse_raw:
        res = json.load(open(raw))["results"]
        print(f"[m] reusing {raw}: {len(res)} job results", flush=True)
    else:
        q = mp.Queue()
        for k in range(a.jobs):
            q.put(k)
        res = {}
        t_m = time.time()
        with mp.Pool(a.jobs, initializer=_init, initargs=(q, cinfo, opts)) as p:
            for k, r in enumerate(p.imap_unordered(_m_job, jobs, chunksize=2)):
                res[r["id"]] = r
                if (k + 1) % 50 == 0:
                    print(f"[m] {k + 1}/{len(jobs)}", flush=True)
        if opts:
            with open(raw, "w") as f:
                json.dump({"profile": a.profile, "cap_output_bytes": a.cap_output_bytes,
                           "wall_seconds": round(time.time() - t_m, 1),
                           "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "results": res}, f)
    if a.profile != "release":
        return phase_m_profile(a, pc, cinfo, orig, jobs, res, grid_check, raw)
    base_rows, rows = [], []
    for j in jobs:
        r, o = res[j["id"]], j["orig"]
        if j["mutant"] is None:
            base_rows.append({"tag": j["tag"], "bin_sha_equal_original": r.get("bin_sha") == o.get("bin_sha"),
                              "tally_no_sanitizer": r.get("tally_no_sanitizer"), "original_tally": {
                                  k: o["tally"].get(k) for k in ("value", "rust_panic", "ub", "timeout", "agree")},
                              "status_uniform": r.get("status_uniform"),
                              "c_outs_equal_phase_c_ref": r.get("c_outs_equal_phase_c_ref")})
            continue
        row = {"id": j["id"], "tag": j["tag"], "name": j["name"], "op": j["mutant"]["op"],
               "subop": j["mutant"]["subop"], "orig_status": o["status"], "orig_tally": o.get("tally"),
               "orig_value_sample": (o.get("value_samples") or [None])[0]}
        row.update({k: v for k, v in r.items() if k not in ("id", "tag", "name")})
        if o["status"] != "noncompile":
            row["reproduced"] = (r.get("status_reproduced") == o["status"] and
                                 all(r["tally_no_sanitizer"][k] == o["tally"][k]
                                     for k in ("value", "rust_panic", "ub", "agree", "rust_hang")))
        rows.append(row)
    comp = [r for r in rows if r["orig_status"] != "noncompile"]
    K = ("killed_value", "killed_crash")

    def count(key):
        return sum(1 for r in comp if r.get(key) in K)
    n = len(comp)
    per_bm = {}
    for nm in sorted({r["name"] for r in comp}):
        rs_ = [r for r in comp if r["name"] == nm]
        per_bm[nm] = {"compiled": len(rs_), "killed_old": sum(r["orig_status"] in K for r in rs_),
                      "killed_new_uniform": sum(r.get("status_uniform") in K for r in rs_),
                      "killed_new_strict": sum(r.get("status_strict") in K for r in rs_),
                      "killed_new_uniform_and_not_min_gt_max": sum(r.get("status_uniform_and_not_min_gt_max") in K
                                                                   for r in rs_),
                      "grid_inputs": cinfo[nm]["inputs"], "ub_clean_inputs_uniform": len(cinfo[nm]["clean_uniform"]),
                      "ub_clean_inputs_strict": len(cinfo[nm]["clean_strict"]),
                      "min_gt_max_inputs": len(cinfo[nm]["degenerate_inputs"])}
    summary = {
        "compiled": n, "killed_old": sum(r["orig_status"] in K for r in comp),
        "killed_new": count("status_uniform"), "rate_old": round(sum(r["orig_status"] in K for r in comp) / n, 4),
        "rate_new": round(count("status_uniform") / n, 4),
        "killed_new_strict": count("status_strict"),
        "killed_new_uniform_and_not_min_gt_max": count("status_uniform_and_not_min_gt_max"),
        "new_status_counts_uniform": dict(collections.Counter(r.get("status_uniform") for r in comp)),
        "reproduced_all_compiled": sum(1 for r in comp if r.get("reproduced")),
        "noncompile_reproduced": sum(1 for r in rows if r["orig_status"] == "noncompile" and r.get("status") == "noncompile"),
        "noncompile_total": sum(1 for r in rows if r["orig_status"] == "noncompile"),
        "mutant_c_outs_equal_phase_c_ref": sum(1 for r in comp if r.get("c_outs_equal_phase_c_ref")),
        "baseline_translations": len(base_rows),
        "baseline_agree_on_every_input": sum(1 for b in base_rows if b["tally_no_sanitizer"] and
                                             b["tally_no_sanitizer"]["value"] == 0 and
                                             b["tally_no_sanitizer"]["rust_panic"] == 0),
        "baseline_bin_sha_equal_original": sum(1 for b in base_rows if b["bin_sha_equal_original"]),
        "per_benchmark": per_bm,
    }
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    from provenance import stamp  # noqa: E402
    sh = lambda c: subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()
    prov = {"script": "harness/arxiv_mutate_full_flourine_ubfilter.py",
            "command": "JOBS=12 bash harness/arxiv_mutub_run.sh flourine_c && JOBS=12 bash harness/arxiv_mutub_run.sh flourine_m",
            "git_commit_of_study_repo": os.environ.get("CRUST_ARTIFACT_COMMIT"),
            "git_note": "study repo working tree (Mac) at that commit plus uncommitted files, this script new; rsynced "
                        "to the run host without .git",
            "host": "Linux x86_64 box (20 cores)", "arch": platform.machine(), "kernel": platform.release(),
            "phase_c": {k: pc[k] for k in ("c_builds", "toolchains", "image", "image_id", "positive_controls", "date")},
            "phase_m": {"image": os.environ.get("ARXIV_IMAGE"), "image_id": os.environ.get("ARXIV_IMAGE_ID"),
                        "toolchains": {"rustc": sh("rustc --version"), "gcc": sh("gcc --version | head -1"),
                                       "python": sys.version.split()[0]},
                        "date": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
            "cpus": f"docker --cpus {a.jobs}",
            "flourine_artifact": "rsync of the Mac artifacts/flourine_dl/flourine-artifact; digest recorded in "
                                 "flourine_artifact_digest",
            "flourine_artifact_digest": os.environ.get("ARXIV_FL_DIGEST"),
            "inputs": {os.path.relpath(a.results, REPO): sha_file(a.results)},
            "grid_check_vs_phase_c": grid_check}
    out = {"_provenance": stamp(image=os.environ.get("ARXIV_IMAGE")), "provenance": prov,
           "rule": __doc__.strip().split("\n\n")[1], "summary": summary,
           "benchmarks_c_side": [c_side_summary(b) for b in pc["benchmarks"]],
           "baseline": base_rows, "mutants": rows}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps({k: v for k, v in summary.items() if k != "per_benchmark"}, indent=1))


# ------------------------------------------------------------------------------------ phase m, --profile
PROFILE_RULE = """\
--profile: every unmutated translation and every mutant is built with run.RUSTC_PROFILE_FLAGS[profile]
and run over the same grid against the same C oracle (run.build_c, gcc -O1 -fwrapv); the C side (UB-clean
and min > max inputs) is phase c's. Per input: agree, value, crash (Rust panic), hang, c_missing, skip, as
batch_diff.tally with no sanitizer. A mutant is killed (killed_value, else killed_crash) if some counted
input gives a different value or a Rust panic. Counted inputs: UB-clean under the uniform rule
(status_uniform*) and, for the second count, not min > max (status_uniform_and_not_min_gt_max*); the
*_baseline_agrees verdicts also skip every input where the unmutated translation does not agree with the
C in this profile. The shipped analysis (harness/mutate_full_flourine.py) mutates only translations whose
unmutated build agrees with the C (no value, no panic) on every grid input, so none of its mutants has a
baseline that diverges; the variants differ in how a translation that diverges in this profile is
treated:
  A  shipped selection, literally: only translations that agree on every grid input in this profile;
  B  shipped selection judged on the inputs each count uses: only translations with no value or panic on
     a counted input (primary);
  C  all translations (the same mutants as the release run); inputs where the unmutated translation
     diverges are skipped;
  D  all translations, no baseline treatment (a mutant's panic counts where the unmutated one panics too).
"""


def phase_m_profile(a, pc, cinfo, orig, jobs, res, grid_check, raw):
    K = ("killed_value", "killed_crash")
    rel = json.load(open(a.release_ubfilter)) if a.release_ubfilter else None
    rel_rows = {r["id"]: r for r in rel["mutants"]} if rel else {}
    dev = {t["tag"]: t for t in json.load(open(a.devprofile_scalar))["per_translation"]} if a.devprofile_scalar else {}
    mutated_tags = {j["tag"] for j in jobs if j["mutant"] is not None}
    grids = {}

    def grid(j):
        if j["name"] not in grids:
            grids[j["name"]] = MFF.enumerate_cases(j["sig"]["ptypes"], cap=2000)
        return grids[j["name"]]

    def ix(s, ch, ok):
        return [i for i, k in enumerate(s) if k == ch and ok(i)]

    def tal(s):
        return {"value": s.count("v"), "rust_panic": s.count("c"), "ub": s.count("m"), "agree": s.count("a"),
                "rust_hang": s.count("h"), "skip": s.count("s")}

    def filters(nm, bk):
        cu, dg = set(cinfo[nm]["clean_uniform"]), set(cinfo[nm]["degenerate_inputs"])
        f = {"uniform": lambda i: i in cu, "uniform_and_not_min_gt_max": lambda i: i in cu and i not in dg}
        if bk is not None:
            f["uniform_baseline_agrees"] = lambda i: i in cu and bk[i] == "a"
            f["uniform_and_not_min_gt_max_baseline_agrees"] = lambda i: i in cu and i not in dg and bk[i] == "a"
        return f

    # unmutated translations
    base, base_rows = {}, []
    for j in jobs:
        if j["mutant"] is not None:
            continue
        r, o, nm = res[j["id"]], j["orig"], j["name"]
        row = {"tag": j["tag"], "name": nm, "mutated": j["tag"] in mutated_tags,
               "release_tally": {k: o["tally"].get(k) for k in ("value", "rust_panic", "ub", "timeout", "agree")},
               "release_agrees_on_every_input": o["tally"]["value"] == 0 and o["tally"]["rust_panic"] == 0}
        bk = r.get("kinds")
        if bk is None:
            row["status"] = r.get("status")
            base_rows.append(row)
            continue
        base[j["tag"]] = bk
        f = filters(nm, None)
        t = tal(bk)
        row.update(status="ok", tally=t, c_outs_equal_phase_c_ref=r.get("c_outs_equal_phase_c_ref"),
                   output_cap_hits=r.get("output_cap_hits"))
        for key in ("uniform", "uniform_and_not_min_gt_max"):
            row[f"value_inputs_{key}"] = len(ix(bk, "v", f[key]))
            row[f"crash_inputs_{key}"] = len(ix(bk, "c", f[key]))
        row["agrees_on_every_input"] = t["value"] == 0 and t["rust_panic"] == 0
        row["agrees_on_uniform_inputs"] = row["value_inputs_uniform"] == 0 and row["crash_inputs_uniform"] == 0
        row["agrees_on_uniform_and_not_min_gt_max_inputs"] = (row["value_inputs_uniform_and_not_min_gt_max"] == 0 and
                                                             row["crash_inputs_uniform_and_not_min_gt_max"] == 0)
        if t["value"] or t["rust_panic"]:
            g = grid(j)
            row["first_divergent_inputs"] = [[g[i], bk[i], i in cinfo[nm]["clean_uniform"]]
                                             for i in [i for i, k in enumerate(bk) if k in "vc"][:3]]
        if j["tag"] in dev:
            dt = dev[j["tag"]]["tally"]
            row["devprofile_run_tally"] = {k: dt.get(k) for k in ("value", "rust_panic", "agree")}
            row["tally_equal_devprofile_run"] = all(dt.get(k) == t[k] for k in ("value", "rust_panic", "agree"))
        base_rows.append(row)
    B = {b["tag"]: b for b in base_rows}

    # mutants
    rows = []
    for j in jobs:
        if j["mutant"] is None:
            continue
        r, o, nm, tag = res[j["id"]], j["orig"], j["name"], j["tag"]
        row = {"id": j["id"], "tag": tag, "name": nm, "op": j["mutant"]["op"], "subop": j["mutant"]["subop"],
               "release_shipped_status": o["status"]}
        if j["id"] in rel_rows:
            rr = rel_rows[j["id"]]
            row["release_status_uniform"] = rr.get("status_uniform")
            row["release_status_uniform_and_not_min_gt_max"] = rr.get("status_uniform_and_not_min_gt_max")
        mk = r.get("kinds")
        if mk is None:
            row.update(status="noncompile", seconds=r.get("seconds"))
            rows.append(row)
            continue
        g, bk = grid(j), base.get(tag)
        t = tal(mk)
        row.update(status="compiled", bin_sha=r.get("bin_sha"), tally=t,
                   status_no_ub_filter=("killed_value" if t["value"] else "killed_crash" if t["rust_panic"]
                                        else "hang" if t["rust_hang"] else "survived"))
        for key, ok in filters(nm, bk).items():
            v, c, h = ix(mk, "v", ok), ix(mk, "c", ok), ix(mk, "h", ok)
            other = "excluded_only" if key.endswith("baseline_agrees") else "ub_only"
            row[f"status_{key}"] = ("killed_value" if v else "killed_crash" if c else "hang" if h
                                    else other if (t["value"] or t["rust_panic"]) else "survived")
            row[f"value_inputs_{key}"] = len(v)
            row[f"crash_inputs_{key}"] = len(c)
            row[f"first_killing_input_{key}"] = g[v[0]] if v else g[c[0]] if c else None
        if bk is None:
            row["status_uniform_baseline_agrees"] = row["status_uniform_and_not_min_gt_max_baseline_agrees"] = \
                "baseline_not_built"
        row.update(c_outs_equal_phase_c_ref=r.get("c_outs_equal_phase_c_ref"), output_cap_hits=r.get("output_cap_hits"),
                   seconds=r.get("seconds"))
        rows.append(row)
    comp = [r for r in rows if r["status"] == "compiled"]

    def agg(sel, key, relkey):
        rs_ = [r for r in comp if sel(r["tag"])]
        n = len(rs_)
        k = sum(r[f"status_{key}"] in K for r in rs_)
        return {"built": n, "translations": len({r["tag"] for r in rs_}), "killed": k,
                "rate": round(k / n, 4) if n else None, "ci95_wilson": MF.wilson(k, n) if n else None,
                "killed_value": sum(r[f"status_{key}"] == "killed_value" for r in rs_),
                "killed_crash": sum(r[f"status_{key}"] == "killed_crash" for r in rs_),
                "status_counts": dict(collections.Counter(r[f"status_{key}"] for r in rs_)),
                "verdict_key": f"status_{key}",
                "release_run_killed_same_mutants": sum(r.get(f"release_status_{relkey}") in K for r in rs_)}

    def variant(sel_u, sel_n, ku, kn):
        return {"ub_clean": agg(sel_u, ku, "uniform"),
                "ub_clean_and_not_min_gt_max": agg(sel_n, kn, "uniform_and_not_min_gt_max")}

    yes = lambda t: True
    sel = lambda f: (lambda t: B.get(t, {}).get(f) is True)
    UB, NB = "uniform_baseline_agrees", "uniform_and_not_min_gt_max_baseline_agrees"
    variants = {
        "A_translation_agrees_on_every_grid_input": variant(sel("agrees_on_every_input"),
                                                            sel("agrees_on_every_input"), UB, NB),
        "B_translation_agrees_on_counted_inputs": variant(sel("agrees_on_uniform_inputs"),
                                                          sel("agrees_on_uniform_and_not_min_gt_max_inputs"), UB, NB),
        "C_all_translations_skip_inputs_where_unmutated_diverges": variant(yes, yes, UB, NB),
        "D_all_translations_no_baseline_treatment": variant(yes, yes, "uniform", "uniform_and_not_min_gt_max"),
    }
    P = variants["B_translation_agrees_on_counted_inputs"]
    pb = {}
    for nm in sorted({r["name"] for r in comp}):
        pb[nm] = {"compiled_all": sum(r["name"] == nm for r in comp)}
        for cnt, s, key in (("ub_clean", sel("agrees_on_uniform_inputs"), UB),
                            ("ub_clean_and_not_min_gt_max", sel("agrees_on_uniform_and_not_min_gt_max_inputs"), NB)):
            rs_ = [r for r in comp if r["name"] == nm and s(r["tag"])]
            pb[nm][cnt] = {"built": len(rs_), "killed": sum(r[f"status_{key}"] in K for r in rs_)}
    noncompile_changed = [r["id"] for r in rows if (r["status"] == "noncompile") != (r["release_shipped_status"] == "noncompile")]
    diverging = [b for b in base_rows if b["mutated"] and b.get("status") == "ok" and not b["agrees_on_every_input"]]
    summary = {
        "rust_profile": {"name": a.profile, "rustc_flags": FR.RUSTC_PROFILE_FLAGS[a.profile]},
        "primary_variant": "B_translation_agrees_on_counted_inputs",
        "built": P["ub_clean"]["built"],
        "killed_ub_clean": P["ub_clean"]["killed"], "rate_ub_clean": P["ub_clean"]["rate"],
        "built_for_second_count": P["ub_clean_and_not_min_gt_max"]["built"],
        "killed_ub_clean_and_not_min_gt_max": P["ub_clean_and_not_min_gt_max"]["killed"],
        "rate_ub_clean_and_not_min_gt_max": P["ub_clean_and_not_min_gt_max"]["rate"],
        "release_run": ({"file": os.path.relpath(a.release_ubfilter, REPO), "compiled": rel["summary"]["compiled"],
                         "killed_new": rel["summary"]["killed_new"],
                         "killed_new_uniform_and_not_min_gt_max": rel["summary"]["killed_new_uniform_and_not_min_gt_max"]}
                        if rel else None),
        "variants": variants,
        "mutants": len(rows), "compiled": len(comp), "noncompile": len(rows) - len(comp),
        "compile_status_differs_from_release": noncompile_changed,
        "unmutated_translations": len(base_rows),
        "unmutated_translations_mutated": sum(b["mutated"] for b in base_rows),
        "mutated_translations_not_agreeing_in_this_profile": [
            {k: b.get(k) for k in ("tag", "tally", "value_inputs_uniform", "crash_inputs_uniform",
                                   "value_inputs_uniform_and_not_min_gt_max", "crash_inputs_uniform_and_not_min_gt_max",
                                   "first_divergent_inputs")} | {"mutants_compiled": sum(r["tag"] == b["tag"] for r in comp)}
            for b in diverging],
        "unmutated_tally_equal_devprofile_run": [sum(1 for b in base_rows if b.get("tally_equal_devprofile_run")),
                                                 sum(1 for b in base_rows if "tally_equal_devprofile_run" in b)],
        "status_change_vs_release_ub_clean": dict(collections.Counter(
            f"{r.get('release_status_uniform')} -> {r['status_uniform']}" for r in comp)),
        "mutant_c_outs_equal_phase_c_ref": sum(1 for r in comp if r.get("c_outs_equal_phase_c_ref")),
        "output_cap_hits": sum((r.get("output_cap_hits") or 0) for r in rows + base_rows),
        "per_benchmark_primary": pb,
    }
    phase_c_check = None
    if rel:
        rc = {b["name"]: b for b in rel["benchmarks_c_side"]}
        phase_c_check = {nm: all(rc[nm][k] == cinfo[nm][k] for k in ("lines_sha256", "ref_outs_sha256", "pc_sha256")) and
                         rc[nm]["ub_clean_inputs_uniform"] == len(cinfo[nm]["clean_uniform"]) and
                         rc[nm]["min_gt_max_inputs"] == len(cinfo[nm]["degenerate_inputs"]) for nm in cinfo}
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    from provenance import stamp  # noqa: E402
    sh = lambda c: subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()
    rawd = json.load(open(raw))
    prov = {"script": "harness/arxiv_mutate_full_flourine_ubfilter.py (--profile)",
            "command": os.environ.get("ARXIV_COMMAND"), "argv": sys.argv,
            "git_commit_of_study_repo": os.environ.get("CRUST_ARTIFACT_COMMIT"),
            "git_note": "study repo working tree (Mac) at that commit plus uncommitted files (this script's --profile, "
                        "--cap-output-bytes and --reuse-raw flags; run.py's --profile flags); rsynced to the run host "
                        "without .git",
            "host": "Linux x86_64 box (20 cores)", "arch": platform.machine(), "kernel": platform.release(),
            "phase_c": {"note": "not re-run: work/phase_c.json is the C side of the release re-scan (C UB flags do not "
                                "depend on the Rust build)",
                        "phase_c_json_sha256": sha_file(os.path.join(a.work, "phase_c.json")),
                        "equal_to_release_rescan_benchmarks_c_side": phase_c_check} |
                       {k: pc[k] for k in ("c_builds", "toolchains", "image", "image_id", "date")},
            "phase_m": {"image": os.environ.get("ARXIV_IMAGE"), "image_id": os.environ.get("ARXIV_IMAGE_ID"),
                        "toolchains": {"rustc": sh("rustc -vV | head -3 | tr '\\n' ' '"),
                                       "gcc": sh("gcc --version | head -1"), "python": sys.version.split()[0]},
                        "rust_profile": a.profile, "rustc_flags": FR.RUSTC_PROFILE_FLAGS[a.profile],
                        "c_build": "run.build_c: gcc -w -O1 -fwrapv",
                        "output_cap_bytes_per_stream": a.cap_output_bytes or None,
                        "run_date": rawd.get("date"), "wall_seconds": rawd.get("wall_seconds"),
                        "summary_date": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
            "cpus": f"docker --cpus {a.jobs}",
            "flourine_artifact_digest": os.environ.get("ARXIV_FL_DIGEST"),
            "inputs": {os.path.relpath(p, REPO): sha_file(p) for p in (a.results, a.release_ubfilter, a.devprofile_scalar) if p},
            "grid_check_vs_phase_c": grid_check}
    out = {"_provenance": stamp(image=os.environ.get("ARXIV_IMAGE")), "provenance": prov,
           "rule": PROFILE_RULE, "summary": summary, "unmutated": base_rows, "mutants": rows}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps({k: v for k, v in summary.items() if k not in ("per_benchmark_primary", "variants",
                                                                     "mutated_translations_not_agreeing_in_this_profile")},
                     indent=1))
    print(json.dumps({k: {c: {x: v[c][x] for x in ("built", "killed", "rate", "release_run_killed_same_mutants")}
                          for c in v} for k, v in variants.items()}, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=("c", "m"), required=True)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--work", required=True)
    ap.add_argument("--results", default=os.path.join(REPO, "results", "flourine_mutation.json"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--profile", choices=("release", "fuzz"), default="release",
                    help="Rust build profile for phase m (default release = rustc -O, as shipped; fuzz = FLOURINE's "
                         "check profile, systems/flourine/run.py RUSTC_PROFILE_FLAGS)")
    ap.add_argument("--cap-output-bytes", type=int, default=0,
                    help="cap each C/Rust run's stdout and stderr at N bytes (RLIMIT_FSIZE); default 0 = off, as shipped")
    ap.add_argument("--reuse-raw", action="store_true",
                    help="with --profile: re-summarize work/m_raw_<profile>.json instead of re-running")
    ap.add_argument("--release-ubfilter", default=None,
                    help="with --profile: the release re-scan (results/arxiv_mutation_ubfilter_flourine.json) to compare")
    ap.add_argument("--devprofile-scalar", default=None,
                    help="with --profile: results/arxiv_flourine_devprofile_scalar.json, to cross-check unmutated tallies")
    a = ap.parse_args()
    (phase_c if a.phase == "c" else phase_m)(a)


if __name__ == "__main__":
    main()
