"""Oracle-UB validation for C2SaferRust coreutils (cat, head, tail, uniq): is the original C
well-defined on every case we count?

Re-runs the full held-out grid (results/<u>_cases_v4.json, 8574 cases) with:
  ref        the oracle exactly as the shipped Makefile builds it (gcc -I./include -g, i.e. -O0)
  sut        C2SaferRust's output (rust_WIP), cargo build (pinned nightly-2022-08-08)
  san_clang  clang-14 ASan+UBSan+float-cast-overflow -O0, -fno-sanitize-recover=all (non-PIE)
  san_gcc    gcc-11, same sanitizer set
  san_clang_nz  clang as san_clang, but NULL+0 (pointer-overflow) recoverable: reports, continues
  o1         gcc -O1 (the VERT harness's optimization-level cross-check: must equal ref)
  o2         gcc -O2 (also enables -fstrict-aliasing; must equal ref)
  uchar      gcc -funsigned-char (implementation-defined toggle)
  schar      gcc -fsigned-char   (x86-64 default; must equal ref)
  ubitfield  gcc -funsigned-bitfields (implementation-defined: signedness of plain int bit-fields)

Per case: the fresh ref-vs-sut class (S1 / crash_segv / crash_panic / S2 / agree, same rule as
difftest.py) is checked against results/<u>_diff_v4.json, then any case
whose C trips a sanitizer, or whose C output changes between -O0 and -O1, is reclassified as
original-C UB (not counted). Char signedness: we record whether the C output changes under
-funsigned-char and whether the verdict against the Rust would change.

Every source tree is COPIED to a scratch build dir first; the artifact tree is never written.

  docker run --rm --ulimit core=0 -v <repo>:/work -v <artifacts>:<artifacts>:ro -w /work \
      crust-heldout python3 harness/oracle_ub_c2saferrust.py --jobs 8
"""
import argparse
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import oracle_ub_common as ou  # noqa: E402
from common import Case, controlled_env, normalize_stderr  # noqa: E402

REPO = ou.REPO
SRC = os.path.join(REPO, "artifacts", "c2saferrust", "coreutils", "src")
UTILS = ["cat", "head", "tail", "uniq"]
SANS = ("san_clang", "san_gcc", "san_clang_nz")

VARIANTS = {
    # name: (cc, cflags, ldflags, is_sanitizer)
    "ref": ("gcc", ["-I./include", "-g"], [], False),
    "o1": ("gcc", ["-I./include", "-g", "-O1"], [], False),
    "o2": ("gcc", ["-I./include", "-g", "-O2"], [], False),
    "san_clang": ("clang", ["-I./include", "-w"] + ou.san_cflags("clang"), ou.san_ldflags("clang"), True),
    "san_gcc": ("gcc", ["-I./include", "-w"] + ou.san_cflags("gcc"), ou.san_ldflags("gcc"), True),
    # clang again, but with the NULL+0 pointer-overflow check made recoverable, so the run goes on
    # past gnulib linebuffer.c:65 and every OTHER report is still seen (see oracle_ub_common)
    "san_clang_nz": ("clang", ["-I./include", "-w"] + ou.san_cflags("clang") + ou.NZ_RECOVER,
                     ou.san_ldflags("clang"), True),
    "uchar": ("gcc", ["-I./include", "-g", "-funsigned-char"], [], False),
    "schar": ("gcc", ["-I./include", "-g", "-fsigned-char"], [], False),
    "ubitfield": ("gcc", ["-I./include", "-g", "-funsigned-bitfields"], [], False),
}


def sh(argv, cwd=None):
    p = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout


def build_c_variant(util, srcdir, outroot, name):
    cc, cflags, ldflags, _ = VARIANTS[name]
    vdir = os.path.join(outroot, name)
    os.makedirs(vdir, exist_ok=True)
    srcs = sorted(f for f in os.listdir(srcdir) if f.endswith(".c"))

    def comp(f):
        o = os.path.join(vdir, f[:-2] + ".o")
        rc, log = sh([cc] + cflags + ["-c", f, "-o", o], cwd=srcdir)
        return f, rc, log
    objs = []
    with cf.ThreadPoolExecutor(4) as ex:
        for f, rc, log in ex.map(comp, srcs):
            if rc != 0:
                return {"ok": False, "why": f"compile {f}", "log": log[-1500:]}
            objs.append(os.path.join(vdir, f[:-2] + ".o"))
    binp = os.path.join(vdir, util)          # argv[0] basename = util, like the Makefile build
    rc, log = sh([cc] + objs + ldflags + ["-o", binp])
    if rc != 0 or not os.path.isfile(binp):
        return {"ok": False, "why": "link", "log": log[-1500:]}
    return {"ok": True, "bin": binp, "cc": cc, "cflags": cflags, "ldflags": ldflags}


def build_sut(util, build):
    rdir = os.path.join(build, util, "rust_WIP")
    ou.copy_sources(os.path.join(SRC, util, "rust_WIP"), rdir)
    rc, log = sh(["cargo", "build", "--bin", util], cwd=rdir)
    binp = os.path.join(rdir, "target", "debug", util)
    return {"ok": rc == 0 and os.path.isfile(binp), "bin": binp, "log_tail": log[-800:]}


def materialize(case, wd):
    for d in (case.make_dirs or []):
        os.makedirs(os.path.join(wd, d), exist_ok=True)
    for name, data in case.decoded_files().items():
        path = os.path.join(wd, name)
        os.makedirs(os.path.dirname(path) or wd, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)


def norm_err(err, binp):
    err = err.replace(binp.encode(), b"PROG")
    return err.replace(os.path.basename(binp).encode() + b":", b"PROG:")


def classify(c_out, c_rc, c_err_n, r_out, r_rc, r_err_n):
    mismatch = (c_out != r_out) or (c_rc != r_rc)
    if mismatch:
        if r_rc in (-11, 139):
            return "crash_segv"
        if r_rc == 101:
            return "crash_panic"
        return "S1"
    if c_err_n != r_err_n:
        return "S2"
    return "agree"


def orig_class(v):
    if v["mismatch"]:
        e = v["sut_exit"]
        return "crash_segv" if e in (-11, 139) else ("crash_panic" if e == 101 else "S1")
    return "S2" if v.get("stderr_divergence") else "agree"


def run_case(args):
    case_d, bins, timeout = args
    case = Case.from_json(case_d)
    stdin = case.stdin()
    env = controlled_env()
    env.update(case.env or {})
    res = {}
    with tempfile.TemporaryDirectory() as wd:
        materialize(case, wd)
        # ref then sut first, in the same dir, exactly as difftest.compare_case does
        for name in ["ref", "sut"] + [k for k in bins if k not in ("ref", "sut")]:
            argv = [bins[name]] + list(case.argv)
            if name.startswith("san_"):
                r = ou.run_san(argv, stdin, cwd=wd, env=env, timeout=timeout * 4,
                               halt=not name.endswith("_nz"))
            else:
                o, e, rc, to = ou.run_proc(argv, stdin, cwd=wd, env=env, timeout=timeout)
                r = {"stdout": o, "stderr": e, "rc": rc, "timed_out": to, "report": None, "infra": False}
            res[name] = r
    ref, sut = res["ref"], res["sut"]
    ref_en = norm_err(ref["stderr"], bins["ref"])
    out = {"id": case.id, "argv": case.argv,
           "timed_out": ref["timed_out"] or sut["timed_out"],
           "ref_exit": ref["rc"], "sut_exit": sut["rc"]}
    # same normalisation as difftest (both full paths, then basenames)
    oe = normalize_stderr(ref["stderr"], bins["ref"], bins["sut"])
    se = normalize_stderr(sut["stderr"], bins["ref"], bins["sut"])
    out["fresh_class"] = ("timeout" if out["timed_out"] else
                          classify(ref["stdout"], ref["rc"], oe, sut["stdout"], sut["rc"], se))
    for name, r in res.items():
        if name in ("ref", "sut"):
            continue
        en = norm_err(r["stderr"], bins[name])
        same = {"stdout": r["stdout"] == ref["stdout"], "exit": r["rc"] == ref["rc"]}
        if not name.startswith("san_"):
            same["stderr"] = en == ref_en      # sanitizer builds: stderr carries the report
        rec = {"same_as_ref": all(same.values()), "exit": r["rc"]}
        if not rec["same_as_ref"]:
            rec["differs_in"] = [k for k, v in same.items() if not v]
        if name.startswith("san_"):
            rec["report"] = r["report"]
            rec["reports"] = r["reports"]
            rec["other_than_null_plus_zero"] = ou.other_than_null_zero(r["reports"])
            rec["infra"] = r["infra"]
            rec["timed_out"] = r["timed_out"]
        if name in ("uchar", "schar", "ubitfield"):
            # verdict of the Rust against THIS C build
            se2 = normalize_stderr(sut["stderr"], bins["ref"], bins["sut"])
            ve = normalize_stderr(r["stderr"], bins[name], bins["sut"])
            rec["class_vs_sut"] = classify(r["stdout"], r["rc"], ve, sut["stdout"], sut["rc"], se2)
        out[name] = rec
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--build", default="/tmp/oub_c2sr")
    ap.add_argument("--utils", default=",".join(UTILS))
    ap.add_argument("--limit", type=int, default=0, help="debug: first N cases per util")
    ap.add_argument("--timeout", type=int, default=15)
    ap.add_argument("--out", default=os.path.join(REPO, "results", "oracle_ub_c2saferrust.json"))
    args = ap.parse_args()
    utils = args.utils.split(",")
    t0 = time.time()

    builds, digests, per_case, per_util = {}, {}, {}, {}
    for u in utils:
        digests[f"{u}/c"] = ou.tree_digest(os.path.join(SRC, u, "c"))
        digests[f"{u}/rust_WIP"] = ou.tree_digest(os.path.join(SRC, u, "rust_WIP"))
        csrc = os.path.join(args.build, u, "c_src")
        ou.copy_sources(os.path.join(SRC, u, "c"), csrc)
        b = {}
        for name in VARIANTS:
            b[name] = build_c_variant(u, csrc, os.path.join(args.build, u, "c_bin"), name)
            if not b[name]["ok"]:
                print(json.dumps({"util": u, "variant": name, **b[name]}, indent=1))
                raise SystemExit(f"C build failed: {u} {name}")
        b["sut"] = build_sut(u, args.build)
        if not b["sut"]["ok"]:
            print(b["sut"]["log_tail"])
            raise SystemExit(f"SUT build failed: {u}")
        builds[u] = b
        print(f"[{time.time()-t0:.0f}s] built {u}", flush=True)

    for u in utils:
        cases = json.load(open(os.path.join(REPO, "results", f"{u}_cases_v4.json")))["cases"]
        v4 = {v["id"]: v for v in json.load(open(os.path.join(REPO, "results", f"{u}_diff_v4.json")))["verdicts"]}
        if args.limit:
            cases = cases[:args.limit]
        bins = {k: v["bin"] for k, v in builds[u].items()}
        rows = []
        with cf.ProcessPoolExecutor(args.jobs) as ex:
            for r in ex.map(run_case, [(c, bins, args.timeout) for c in cases], chunksize=8):
                v = v4[r["id"]]
                r["orig_class"] = orig_class(v)
                r["reproduced"] = (r["orig_class"] == r["fresh_class"]
                                   and r["ref_exit"] == v["oracle_exit"] and r["sut_exit"] == v["sut_exit"])
                ub = []
                for s in SANS:
                    if r[s]["report"]:
                        ub.append(f"{s}: {r[s]['report']}")
                opt = []
                for o in ("o1", "o2"):
                    if not r[o]["same_as_ref"]:
                        opt.append(f"opt-level: -{o.upper()} output differs from -O0 in " + ",".join(r[o]["differs_in"]))
                r["c_ub_evidence"] = ub + opt
                # the same, ignoring only clang's NULL+0 report (gcc run and the *_nz run see past it)
                ub_nz = [f"san_gcc: {r['san_gcc']['report']}"] if r["san_gcc"]["report"] else []
                ub_nz += [f"san_clang_nz: {x}" for x in r["san_clang_nz"]["other_than_null_plus_zero"]]
                r["c_ub_evidence_excluding_null_plus_zero"] = ub_nz + opt
                r["null_plus_zero_only"] = bool(ub) and not ub_nz
                r["san_infra"] = [s for s in SANS if r[s]["infra"]]
                # sanitizer build ran clean but printed something else than the plain oracle
                r["san_output_differs_without_report"] = [
                    s for s in SANS if not r[s]["report"] and not r[s]["same_as_ref"]]
                r["char_signedness_dependent"] = (not r["uchar"]["same_as_ref"]) or (not r["schar"]["same_as_ref"])
                r["bitfield_signedness_dependent"] = not r["ubitfield"]["same_as_ref"]
                # uniform rule (audit 8, ou.not_clean_reasons): a sanitizer run that failed to start,
                # timed out or printed something else, or a char-signedness build that disagrees,
                # leaves the case NOT UB-clean (it was previously treated as clean)
                unres = []
                for s in ("san_clang", "san_gcc"):
                    if not r[s]["report"]:
                        unres += ou.not_clean_reasons(s, r[s], r[s]["same_as_ref"])
                for n in ("uchar", "schar"):
                    if not r[n]["same_as_ref"]:
                        unres.append(f"{n}: output differs from the reference build (char signedness)")
                r["ub_rule_not_clean"] = unres
                counted = r["fresh_class"] in ("S1", "crash_segv", "crash_panic", "S2")
                r["final_class"] = ("original_C_UB" if (counted and r["c_ub_evidence"]) else
                                    "not_ub_clean" if (counted and unres) else r["fresh_class"])
                r["final_class_excluding_null_plus_zero"] = (
                    "original_C_UB" if (counted and r["c_ub_evidence_excluding_null_plus_zero"]) else
                    "not_ub_clean" if (counted and unres) else r["fresh_class"])
                rows.append(r)
        per_case[u] = rows
        print(f"[{time.time()-t0:.0f}s] ran {u}: {len(rows)} cases", flush=True)

    # ---------------- summary ----------------
    def count(rows, key, vals):
        return {v: sum(1 for r in rows if r[key] == v) for v in vals}
    CL = ["S1", "crash_segv", "crash_panic", "S2", "agree", "timeout"]
    allrows = [r for u in utils for r in per_case[u]]
    summary = {"utils": utils, "n_cases": len(allrows)}
    summary["before_v4"] = count(allrows, "orig_class", CL)
    summary["fresh_rerun"] = count(allrows, "fresh_class", CL)
    summary["reproduced_cases"] = sum(r["reproduced"] for r in allrows)
    summary["not_reproduced_ids"] = [r["id"] for r in allrows if not r["reproduced"]][:50]
    summary["after_ub_filter"] = count(allrows, "final_class", CL + ["original_C_UB", "not_ub_clean"])
    summary["after_ub_filter_excluding_null_plus_zero"] = count(
        allrows, "final_class_excluding_null_plus_zero", CL + ["original_C_UB", "not_ub_clean"])
    summary["null_plus_zero_only_cases"] = {
        "counted": sum(1 for r in allrows if r["null_plus_zero_only"] and r["fresh_class"] != "agree"),
        "agreeing": sum(1 for r in allrows if r["null_plus_zero_only"] and r["fresh_class"] == "agree"),
        "locations": sorted({x for r in allrows for x in r["san_clang_nz"]["reports"]
                             if ou.NULL_ZERO_RE.search(x.encode())})}
    summary["reclassified_to_original_C_UB_by_class"] = {
        c: sum(1 for r in allrows if r["fresh_class"] == c and r["final_class"] == "original_C_UB")
        for c in ["S1", "crash_segv", "crash_panic", "S2"]}
    summary["per_util"] = {u: {"fresh": count(per_case[u], "fresh_class", CL),
                               "after": count(per_case[u], "final_class", CL + ["original_C_UB", "not_ub_clean"])}
                           for u in utils}
    summary["sanitizer_reports_any_case"] = {
        s: sum(1 for r in allrows if r[s]["report"]) for s in SANS}
    summary["reports_other_than_null_plus_zero_any_case"] = sum(
        1 for r in allrows if r["san_gcc"]["report"] or r["san_clang_nz"]["other_than_null_plus_zero"])
    summary["sanitizer_reports_on_agreeing_cases"] = sum(
        1 for r in allrows if r["fresh_class"] == "agree" and any(r[s]["report"] for s in SANS))
    summary["opt_level_differs_any_case"] = {o: sum(1 for r in allrows if not r[o]["same_as_ref"]) for o in ("o1", "o2")}
    summary["bitfield_signedness"] = {
        "any_case_where_C_output_changes_with_-funsigned-bitfields": sum(1 for r in allrows if r["bitfield_signedness_dependent"]),
        "counted_cases_whose_class_changes": sum(1 for r in allrows if r["fresh_class"] != "agree"
                                                 and r["ubitfield"]["class_vs_sut"] != r["fresh_class"])}
    summary["san_infra_cases"] = sum(1 for r in allrows if r["san_infra"])
    summary["san_output_differs_without_report"] = sum(1 for r in allrows if r["san_output_differs_without_report"])
    counted = [r for r in allrows if r["fresh_class"] in ("S1", "crash_segv", "crash_panic", "S2")]
    summary["char_signedness"] = {
        "counted_cases_where_C_output_changes_with_-funsigned-char": sum(1 for r in counted if not r["uchar"]["same_as_ref"]),
        "counted_cases_where_C_output_changes_with_-fsigned-char": sum(1 for r in counted if not r["schar"]["same_as_ref"]),
        "counted_cases_whose_class_changes_under_-funsigned-char": sum(
            1 for r in counted if r["uchar"]["class_vs_sut"] != r["fresh_class"]),
        "class_transitions_under_-funsigned-char": {},
        "any_case_where_C_output_changes_with_-funsigned-char": sum(1 for r in allrows if not r["uchar"]["same_as_ref"]),
    }
    tr = summary["char_signedness"]["class_transitions_under_-funsigned-char"]
    for r in allrows:
        if r["uchar"]["class_vs_sut"] != r["fresh_class"]:
            k = f"{r['fresh_class']}->{r['uchar']['class_vs_sut']}"
            tr[k] = tr.get(k, 0) + 1
    # compact per-case verdicts: every counted case, plus any agreeing case with an anomaly
    keep = [r for r in allrows if r["fresh_class"] != "agree" or r["orig_class"] != "agree"
            or r["c_ub_evidence"] or r["san_output_differs_without_report"] or r["char_signedness_dependent"]
            or r["bitfield_signedness_dependent"]
            or r["san_infra"] or r["ub_rule_not_clean"]]
    out = {
        "provenance": ou.provenance(__file__, {
            "artifact_source_digests": digests,
            "builds": {u: {k: {kk: vv for kk, vv in v.items() if kk != "log_tail"} for k, v in b.items()}
                       for u, b in builds.items()},
            "inputs": [f"results/{u}_cases_v4.json" for u in utils],
            "compared_against": [f"results/{u}_diff_v4.json" for u in utils],
            "wall_seconds": round(time.time() - t0),
            "sanitizer_positive_controls": ou.positive_controls(os.path.join(args.build, "controls")),
        }),
        "classification_rule": "S1 = stdout or exit differs and Rust exit not in {-11,139,101}; crash_segv = "
                               "Rust exit -11/139; crash_panic = Rust exit 101; S2 = only normalised stderr "
                               "differs (difftest.py). A counted case becomes original_C_UB "
                               "if clang or gcc ASan/UBSan reports on the C, or gcc -O1 output differs from -O0 "
                               "(final_class); a counted case whose clang/gcc sanitizer run failed to start, timed out or printed a "
                               "different stdout/exit, or whose -funsigned-char/-fsigned-char build differs, is not_ub_clean "
                               "(audit 8 uniform rule). final_class_excluding_null_plus_zero ignores only clang's "
                               "'applying zero offset to null pointer' report (NULL+0: undefined in C11, defined "
                               "in C2y/N3322 and C++; gcc does not flag it).",
        "summary": summary,
        "verdicts": keep,
        "verdicts_note": f"{len(keep)} of {len(allrows)} cases listed: every case counted before or after, and "
                         "every agreeing case with a sanitizer report, opt-level or char-signedness change. "
                         "Agreeing cases with no anomaly are omitted.",
    }
    ou.dump(args.out, out)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
