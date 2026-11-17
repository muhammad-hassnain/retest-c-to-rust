"""PtrTrans quadtree re-grade: library-level differential test of the ORIGINAL CROWN quadtree C
against PtrTrans's own Rust translation (Trans_PA, dataset/Trans_C-Rust-KG/quadtree), with the
study's UB rule applied to every case.

The C is the oracle. Each case (gen_cases.py) is one command script run once on the C driver
(qt_oracle.c + the original src/*.c) and once on the Rust driver (qt_sut.rs + the shipped crate,
unmodified, as a path dependency). Every printed token is compared; floats (IEEE bits) with the
study's relative tolerance |a-b| <= 1e-6*max(1,|a|) (systems/vert/run_arrays.py), NaN == NaN.

UB rule (harness/oracle_ub_common.py, same flags as every other system): a divergence counts only
if the C, on that case, is silent under clang-14 ASan+UBSan+float-cast-overflow and gcc-11 ASan+
UBSan+float-cast-overflow (-O0, -fno-sanitize-recover=all), gives byte-identical output at
-O0/-O1/-O2 and at the Makefile's -O3, and does not change with -funsigned-char / -fsigned-char.

C builds:   ref = gcc -O3 -std=c99 -Wall -g -pedantic (the project's Makefile FLAGS), -lm
Rust build: cargo +<toolchain> build (debug, overflow checks on: the profile PtrTrans's own
            `cargo check` loop used) = primary SUT; --release = cross-check.
"repaired": the shipped crate with ONE change in find_ (get_quadrant_(None, ..) replaced by the
            same nw/ne/sw/se lookup on the node) -- used only to confirm that this one mechanism
            explains every counted divergence, and as the agreeing translation for mutation analysis.

  docker run ... crust-heldout-plus186 python3 systems/ptrtrans/quadtree_e2e/run.py \
      --art /art --build /oub/qt --jobs 2 --toolchain 1.86.0
"""
import argparse
import collections
import concurrent.futures as cf
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(REPO, "harness"))
sys.path.insert(0, HERE)
import oracle_ub_common as ou  # noqa: E402
from gen_cases import corpus  # noqa: E402

C_REL = "dataset/crown_dataset/quadtree"
RS_REL = "dataset/Trans_C-Rust-KG/quadtree"
MAKE_FLAGS = ["-O3", "-std=c99", "-Wall", "-g", "-pedantic"]   # Makefile FLAGS
SRC = ["point.c", "bounds.c", "node.c", "quadtree.c"]

C_VARIANTS = {
    "ref": ("gcc", MAKE_FLAGS, ["-lm"]),
    "o0": ("gcc", ["-O0", "-std=c99"], ["-lm"]),
    "o1": ("gcc", ["-O1", "-std=c99"], ["-lm"]),
    "o2": ("gcc", ["-O2", "-std=c99"], ["-lm"]),
    "san_clang": ("clang", ["-w", "-std=c99"] + ou.san_cflags("clang"), ou.san_ldflags("clang") + ["-lm"]),
    "san_gcc": ("gcc", ["-w", "-std=c99"] + ou.san_cflags("gcc"), ou.san_ldflags("gcc") + ["-lm"]),
    "uchar": ("gcc", MAKE_FLAGS + ["-funsigned-char"], ["-lm"]),
    "schar": ("gcc", MAKE_FLAGS + ["-fsigned-char"], ["-lm"]),
}
UB_VARIANTS = ("o0", "o1", "o2", "san_clang", "san_gcc", "uchar", "schar")

FIND_BUG = "if let Some(next_node) = get_quadrant_(None, Some(&test)) {"
FIND_FIX = ("let q = if node_contains_(node_ref.nw.as_deref(), Some(&test)) { node_ref.nw.as_deref() } "
            "else if node_contains_(node_ref.ne.as_deref(), Some(&test)) { node_ref.ne.as_deref() } "
            "else if node_contains_(node_ref.sw.as_deref(), Some(&test)) { node_ref.sw.as_deref() } "
            "else if node_contains_(node_ref.se.as_deref(), Some(&test)) { node_ref.se.as_deref() } "
            "else { None };\n            if let Some(next_node) = q {")


def sh(argv, cwd=None, env=None):
    p = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout


def sut_cargo_toml(dep_path):
    return ("[package]\nname = \"qt_sut\"\nversion = \"0.1.0\"\nedition = \"2021\"\n\n"
            "[[bin]]\nname = \"qt_sut\"\npath = \"src/main.rs\"\n\n"
            f"[dependencies]\nquadtree = {{ path = \"{dep_path}\" }}\n\n[workspace]\n")


def build_rust(bdir, art, toolchain, repaired=False, crate_src=None, tag=None):
    """Copy the shipped crate (no target/), optionally repair find_, build the driver against it.
    Returns dict with paths and build logs."""
    tag = tag or ("repaired" if repaired else "shipped")
    crate = os.path.join(bdir, f"quadtree_{tag}")
    ou.copy_sources(crate_src or os.path.join(art, RS_REL), crate)
    if os.path.isdir(os.path.join(crate, "target")):
        shutil.rmtree(os.path.join(crate, "target"))
    info = {"crate_copy": crate}
    if repaired:
        f = os.path.join(crate, "src", "quadtree.rs")
        s = open(f).read()
        assert s.count(FIND_BUG) == 1, "find_ pattern not found exactly once"
        open(f, "w").write(s.replace(FIND_BUG, FIND_FIX))
    tc = ["cargo", f"+{toolchain}"]
    env = dict(os.environ, CARGO_TARGET_DIR=os.path.join(bdir, f"target_lib_{tag}_{toolchain}"))
    rc, log = sh(tc + ["build", "--offline"], cwd=crate, env=env)
    info["lib_build"] = {"cmd": " ".join(tc + ["build", "--offline"]) + "  (in the crate copy)", "rc": rc,
                         "warnings": len(re.findall(r"^warning", log, re.M)),
                         "errors": len(re.findall(r"^error", log, re.M)), "log_tail": log[-3000:]}
    sut = os.path.join(bdir, f"sut_{tag}_{toolchain}")
    if os.path.isdir(sut):
        shutil.rmtree(sut)
    os.makedirs(os.path.join(sut, "src"))
    open(os.path.join(sut, "Cargo.toml"), "w").write(sut_cargo_toml(crate))
    shutil.copy(os.path.join(HERE, "qt_sut.rs"), os.path.join(sut, "src", "main.rs"))
    for prof in ("debug", "release"):
        args = tc + ["build", "--offline"] + (["--release"] if prof == "release" else [])
        rc, log = sh(args, cwd=sut)
        b = os.path.join(sut, "target", prof, "qt_sut")
        info[f"sut_{prof}"] = {"cmd": " ".join(args), "rc": rc, "bin": b if os.path.isfile(b) else None,
                               "log_tail": log[-2000:]}
    rc, ver = sh(["rustc", f"+{toolchain}", "-vV"])
    info["rustc"] = ver.strip()
    return info


def build_c(bdir, art, only=None):
    csrc = os.path.join(bdir, "c_src")
    ou.copy_sources(os.path.join(art, C_REL, "src"), csrc)
    bins, logs = {}, {}
    for name, (cc, cflags, ldflags) in C_VARIANTS.items():
        if only and name not in only:
            continue
        b = os.path.join(bdir, f"qt_oracle_{name}")
        argv = [cc] + cflags + ["-I", csrc] + [os.path.join(csrc, s) for s in SRC] + \
               [os.path.join(HERE, "qt_oracle.c"), "-o", b] + ldflags
        rc, log = sh(argv)
        if rc != 0:
            raise SystemExit(f"C build {name} failed:\n{log[-3000:]}")
        bins[name] = b
        logs[name] = {"cc": cc, "flags": cflags + ldflags, "warnings": log.count("warning:")}
    # the project's own build: make (bin/test) in a scratch copy
    proj = os.path.join(bdir, "c_project")
    ou.copy_sources(os.path.join(art, C_REL), proj)
    rc, log = sh(["make", "bin/test"], cwd=proj)
    test_rc, test_out = (None, None)
    if rc == 0:
        test_rc, test_out = sh([os.path.join(proj, "bin", "test")])
    logs["project_make_bin_test"] = {"rc": rc, "log_tail": log[-1500:], "bin_test_rc": test_rc,
                                     "bin_test_out": (test_out or "")[-800:]}
    return bins, logs


# ---------------------------------------------------------------- comparison
FTOK = re.compile(r"^f[0-9a-f]{16}$")


def fval(tok):
    return struct.unpack(">d", bytes.fromhex(tok[1:]))[0]


def feq(a, b):
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    if math.isinf(a) or math.isinf(b):
        return a == b
    return abs(a - b) <= 1e-6 * max(1.0, abs(a))


def norm_lines(out):
    return [l for l in out.decode("utf-8", "replace").splitlines() if not l.startswith("#")]


def compare(c_out, c_rc, r_out, r_rc):
    """None if equal, else dict(channel, c_line, r_line, line_no, bit_exact_only)."""
    cl, rl = norm_lines(c_out), norm_lines(r_out)
    for i in range(max(len(cl), len(rl))):
        a = cl[i] if i < len(cl) else None
        b = rl[i] if i < len(rl) else None
        if a == b:
            continue
        if a is not None and b is not None:
            ta, tb = a.split(), b.split()
            if len(ta) == len(tb) and all(x == y or (FTOK.match(x) and FTOK.match(y) and feq(fval(x), fval(y)))
                                          for x, y in zip(ta, tb)):
                continue   # equal within tolerance (signed zero, last-bit rounding)
        ref = a if a is not None else b
        return {"channel": channel_of(ref), "line_no": i, "c_line": a, "rust_line": b}
    if c_rc != r_rc:
        return {"channel": "exit", "line_no": None, "c_line": f"rc={c_rc}", "rust_line": f"rc={r_rc}"}
    return None


def bit_exact_differs(c_out, r_out):
    return norm_lines(c_out) != norm_lines(r_out)


def channel_of(line):
    head = line.split()[0] if line.split() else ""
    return {"S": "search", "I": "insert", "D": "walk", "A": "walk", "L": "length", "N": "new",
            "KF": "key_free", "B": "bounds", "BX": "bounds", "NB": "node", "NN": "node",
            "PN": "point"}.get(head, "other:" + head)


# Memory guard. Some inputs (bounds whose width overflows to inf) make the C (and the Rust) split
# without end; at -O2/-O3 the tail call becomes a loop that allocates without bound. Plain runs get
# RLIMIT_AS = MEM_LIMIT (malloc then fails); sanitizer runs keep the shared configuration
# (oracle_ub_common.SAN_ENV) plus ASan's hard_rss_limit_mb (they need a large address space for the
# shadow). The container itself also has a hard memory limit (run_in_docker.sh).
MEM_LIMIT = 1 << 30
TIMEOUT = 20


def _limit_as():
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (MEM_LIMIT, MEM_LIMIT))


def run_plain(argv, data, timeout=TIMEOUT):
    try:
        p = subprocess.run(argv, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, preexec_fn=_limit_as)
        return p.stdout, p.stderr, p.returncode, False
    except subprocess.TimeoutExpired as e:
        return e.stdout or b"", e.stderr or b"", -1, True


def run_san_limited(argv, data, timeout=60, retries=6):
    """oracle_ub_common.run_san with ASAN_OPTIONS += hard_rss_limit_mb (same detection semantics)."""
    e = dict(os.environ)
    e.update(ou.SAN_ENV)
    e["ASAN_OPTIONS"] = e["ASAN_OPTIONS"] + f":hard_rss_limit_mb={MEM_LIMIT >> 20}"
    last = None
    for _ in range(retries):
        out, err, rc, to = ou.run_proc(argv, data, None, e, timeout)
        last = (out, err, rc, to)
        if not to and ou.is_startup_infra(rc, err):
            continue
        return {"stdout": out, "stderr": err, "rc": rc, "timed_out": to,
                "report": ou.san_report(err), "infra": False}
    out, err, rc, to = last
    return {"stdout": out, "stderr": err, "rc": rc, "timed_out": to, "report": None, "infra": True}


def run_bin(b, script, san=False):
    data = script.encode()
    if san:
        r = run_san_limited([b], data)
        return {"out": r["stdout"], "err": r["stderr"], "rc": r["rc"], "to": r["timed_out"],
                "report": r["report"], "infra": r["infra"]}
    o, e, rc, to = run_plain([b], data)
    return {"out": o, "err": e, "rc": rc, "to": to, "report": None, "infra": False}


def run_case(case, cbins, rbins):
    s = case["script"]
    res = {"ref": run_bin(cbins["ref"], s)}
    for v in UB_VARIANTS:
        if v not in cbins:
            continue
        res[v] = run_bin(cbins[v], s, san=v.startswith("san_"))
    for name, b in rbins.items():
        res[name] = run_bin(b, s)
    return case, res


def short(b, n=600):
    return b.decode("utf-8", "replace")[-n:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--art", required=True, help="artifact root (read-only)")
    ap.add_argument("--build", default="/tmp/qt_build")
    ap.add_argument("--toolchain", default="1.86.0")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--out", default=os.path.join(REPO, "results", "ptrtrans_quadtree_diff.json"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--filter", default="", help="only cases whose id contains this (smoke tests)")
    ap.add_argument("--no-ub", action="store_true",
                    help="cross-toolchain check only: C ref + shipped Rust; UB verdicts taken from --ub-from")
    ap.add_argument("--ub-from", default=os.path.join(REPO, "results", "ptrtrans_quadtree_diff.json"))
    args = ap.parse_args()
    t0 = time.time()
    os.makedirs(args.build, exist_ok=True)
    cbins, clogs = build_c(args.build, args.art, only=("ref",) if args.no_ub else None)
    shipped = build_rust(args.build, args.art, args.toolchain)
    if not args.no_ub:
        repaired = build_rust(args.build, args.art, args.toolchain, repaired=True)
    else:
        repaired = {"skipped": "cross-toolchain run"}
        prior = {r["id"]: r for r in json.load(open(args.ub_from))["verdicts"]}
    rbins = {}
    for tag, info in (("rust_debug", shipped["sut_debug"]), ("rust_release", shipped["sut_release"]),
                      ("repaired_debug", repaired.get("sut_debug", {"bin": None}))):
        if info["bin"]:
            rbins[tag] = info["bin"]
    if "rust_debug" not in rbins:
        out = {"provenance": ou.provenance(__file__), "build": {"c": clogs, "rust_shipped": shipped},
               "verdict": "S3: Rust driver did not build"}
        ou.dump(args.out, out)
        print(json.dumps(out["build"], indent=1)[:4000])
        return
    cases = corpus()
    if args.filter:
        cases = [c for c in cases if args.filter in c["id"]]
    if args.limit:
        cases = cases[:args.limit]
    rows = []
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        for n_done, (case, res) in enumerate(ex.map(lambda c: run_case(c, cbins, rbins), cases), 1):
            if n_done % 250 == 0:
                print(f"[progress] {n_done}/{len(cases)} cases, {round(time.time() - t0)} s", file=sys.stderr, flush=True)
            ref = res["ref"]
            row = {"id": case["id"], "family": case["family"], "meta": case["meta"],
                   "c_rc": ref["rc"], "c_timeout": ref["to"]}
            if ref["rc"] != 0 or ref["to"]:
                row["c_stderr_tail"] = short(ref["err"], 300)
            ub = []
            impl = []   # C output depends on the compiler without any UB report: not UB, listed apart
            for v in UB_VARIANTS:
                if v not in res:
                    continue
                r = res[v]
                same = r["out"] == ref["out"] and r["rc"] == ref["rc"]
                if v.startswith("san_"):
                    if r["report"]:
                        ub.append(f"{v}: {r['report']}")
                    elif r["infra"]:      # audit 8 uniform rule: a failed run is not UB-clean
                        ub.append(f"{v}: sanitizer start-up infra failure (not UB-clean)")
                    elif r["to"]:
                        ub.append(f"{v}: timed out (not UB-clean)")
                    elif not same:
                        dl = compare(ref["out"], ref["rc"], r["out"], r["rc"])
                        msg = (f"{v}: output differs from gcc -O3 ref without a sanitizer report"
                               + (" [equal within float tolerance]" if dl is None else
                                  f" (ref: {dl['c_line']} | {v}: {dl['rust_line']})"))
                        # beyond the harness's float tolerance the sanitizer build disagrees: not UB-clean
                        (impl if dl is None else ub).append(msg)
                elif not same:
                    ub.append(f"{v}: output differs from ref (-O3)")
            if args.no_ub:
                ub = prior[case["id"]]["c_ub_evidence"]
                impl = prior[case["id"]].get("c_impl_dependent", [])
                row["c_ub_evidence_source"] = os.path.relpath(args.ub_from, REPO)
                row["primary_run_final"] = prior[case["id"]]["final"]
            row["c_ub_evidence"] = ub
            row["c_impl_dependent"] = impl
            for tag in rbins:
                r = res[tag]
                d = compare(ref["out"], ref["rc"], r["out"], r["rc"])
                row[tag] = {"rc": r["rc"], "timeout": r["to"], "div": d,
                            "bit_exact_differs": bit_exact_differs(ref["out"], r["out"])}
                if r["rc"] not in (0,) and tag == "rust_debug":
                    row[tag]["stderr_tail"] = short(r["err"], 400)
            d = row["rust_debug"]["div"]
            pre = bool(case["meta"].get("violates_min_le_max"))
            if d is None:
                final = "agree"
            elif ub:
                final = "original_C_UB"
            elif impl:
                final = "c_implementation_dependent"
            elif pre:
                final = "precondition_violating_input"
            else:
                final = "divergent"
            row["final"] = final
            if d is not None or ub:
                row["c_out"] = short(ref["out"], 1200)
                row["rust_out"] = short(res["rust_debug"]["out"], 1200)
                row["script"] = case["script"] if len(case["script"]) < 1500 else case["script"][:1500] + "..."
            rows.append(row)
    fam = collections.defaultdict(collections.Counter)
    for r in rows:
        fam[r["family"]][r["final"]] += 1
    counted = [r for r in rows if r["final"] == "divergent"]
    summary = {
        "same_final_verdicts_as_ub_from_run": (all(r["final"] == r["primary_run_final"] for r in rows)
                                               if args.no_ub else None),
        "n_cases": len(rows),
        "by_final": dict(collections.Counter(r["final"] for r in rows)),
        "by_family": {k: dict(v) for k, v in fam.items()},
        "counted_by_channel": dict(collections.Counter(r["rust_debug"]["div"]["channel"] for r in counted)),
        "counted_rust_exit_codes": dict(collections.Counter(r["rust_debug"]["rc"] for r in counted)),
        "c_crash_cases": sum(1 for r in rows if r["c_rc"] != 0),
        "c_crash_cases_all_have_ub_evidence": all(r["c_ub_evidence"] for r in rows if r["c_rc"] != 0),
        "ub_evidence_cases": sum(1 for r in rows if r["c_ub_evidence"]),
        "ub_evidence_kinds": dict(collections.Counter(
            re.sub(r"==\d+==|0x[0-9a-f]+|/[^ ]*:\d+:\d+:?", "", e)[:120]
            for r in rows for e in r["c_ub_evidence"])),
        "c_impl_dependent_cases": sum(1 for r in rows if r["c_impl_dependent"]),
        "c_impl_dependent_kinds": dict(collections.Counter(
            e.split(" (ref:")[0] for r in rows for e in r["c_impl_dependent"])),
        "rust_panics_or_crashes_on_ub_clean": sum(1 for r in rows if not r["c_ub_evidence"]
                                                 and r["rust_debug"]["rc"] != 0),
        "rust_release_vs_debug_verdict_differs": sum(
            1 for r in rows if "rust_release" in r and (r["rust_release"]["div"] is None) != (r["rust_debug"]["div"] is None)),
        "bit_exact_only_differences_ub_clean": sum(
            1 for r in rows if not r["c_ub_evidence"] and r["rust_debug"]["div"] is None
            and r["rust_debug"]["bit_exact_differs"]),
        "repaired": {
            "divergent_ub_clean": sum(1 for r in rows if "repaired_debug" in r and not r["c_ub_evidence"]
                                      and r["repaired_debug"]["div"] is not None),
            "divergent_ub_clean_by_channel": dict(collections.Counter(
                r["repaired_debug"]["div"]["channel"] for r in rows if "repaired_debug" in r
                and not r["c_ub_evidence"] and r["repaired_debug"]["div"] is not None)),
        },
    }
    out = {
        "provenance": ou.provenance(__file__, {
            "docker_image": os.environ.get("OUB_IMAGE", "?"),
            "artifact": {"repo": "github.com/FudanSELab/PtrTrans-C2Rust",
                         "commit": os.environ.get("PT_COMMIT", "?"),
                         "c_digest": ou.tree_digest(os.path.join(args.art, C_REL)),
                         "rust_digest": ou.tree_digest(os.path.join(args.art, RS_REL, "src")),
                         "rust_cargo_toml_sha256": ou.tree_digest(os.path.join(args.art, RS_REL, "Cargo.toml"))},
            "rust_toolchain_used": args.toolchain,
            "rust_toolchain_recorded_by_artifact": "rustc 1.90.0-nightly (e43d139a8 2025-07-09) "
                                                   "(dataset/Trans_C-Rust-KG/quadtree/target/.rustc_info.json)",
            "float_tolerance": "|a-b| <= 1e-6*max(1,|a|); NaN==NaN; inf exact",
            "limits": {"plain_runs_RLIMIT_AS_bytes": MEM_LIMIT, "asan_hard_rss_limit_mb": MEM_LIMIT >> 20,
                       "timeout_s_plain": TIMEOUT, "timeout_s_san": 60},
            "c_builds": clogs,
            "rust_shipped_build": shipped,
            "rust_repaired_build": repaired,
            "repair_patch": {"file": "src/quadtree.rs (find_)", "from": FIND_BUG, "to": FIND_FIX},
            "wall_seconds": round(time.time() - t0),
            "sanitizer_positive_controls": (None if args.no_ub else
                                            ou.positive_controls(os.path.join(args.build, "controls"))),
        }),
        "summary": summary,
        "verdicts": rows,
    }
    ou.dump(args.out, out)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
