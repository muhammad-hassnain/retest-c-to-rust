#!/usr/bin/env python3
"""SACTOR libogg re-grade: library-level differential test of every shipped SACTOR translation of
libogg's bitwise.c and framing.c against the original C (upstream 0288fad, shipped in
sactor-datasets/ogg/ogg), with the oracle-UB rule of harness/oracle_ub_common.py.

Run inside the container (systems/sactor/run_docker.sh):
    python3 systems/sactor/libogg/run.py --out /work/results/sactor_libogg.json

Translations (sactor-datasets/ogg/translation/<model>/<file>.c/translated_code_<style>/):
  unidiomatic: C ABI (raw pointers, repr(C) structs). Built exactly as SACTOR's own E2E check
    builds it (combiner/program_combiner.py + verifier/e2e_verifier.py): SACTOR's build_attempt
    crate (cdylib, libc 0.2.x), every C function exposed with #[no_mangle] extern "C", `cargo
    build` (debug profile). combined.rs is used where shipped; otherwise the per-item files are
    combined as SACTOR's Combiner does. Also built --release, and with both bracketing nightlies.
  idiomatic: Rust-native API, partial item sets, no combined.rs; SACTOR itself skips the E2E test
    for idiomatic library code ("api mismatch to C"). We record whether the item set builds.

Builds per C file (same driver source in every build; only the library differs):
  ref        gcc -O2 + libogg's own CFLAGS (-Wall -Wextra -ffast-math -fsigned-char), -DHAVE_CONFIG_H
  o0, o1     same at -O0 / -O1                       (optimization-level cross-check)
  san_clang  clang-14 ASan+UBSan+float-cast-overflow, -O0, non-PIE, -fno-sanitize-recover=all
  san_gcc    gcc-11, same sanitizer set
  uchar      ref with -funsigned-char instead of -fsigned-char   (char-signedness check)
  rust:<model>:<tc>:<profile>   driver + SACTOR's Rust cdylib
A divergence (stdout, exit code or stderr differs from ref) counts only if, on that case, every C
check passes: no sanitizer report from either compiler, o0 == o1 == ref, uchar == ref.
"""
import argparse
import concurrent.futures as cf
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import sactor_common as sc  # noqa: E402
ou = sc.ou
sys.path.insert(0, HERE)
import gen_cases  # noqa: E402

OGG = os.path.join(sc.ART, "sactor-datasets", "ogg")
TRANS = os.path.join(OGG, "translation")
MODELS = ["gpt-4o", "gpt-5"]
FILES = ["bitwise", "framing"]
PROJECT_CFLAGS = ["-DHAVE_CONFIG_H", "-Wall", "-Wextra", "-ffast-math", "-fsigned-char", "-g"]

C_VARIANTS = {
    "ref": ("gcc", ["-O2"] + PROJECT_CFLAGS, []),
    "o0": ("gcc", ["-O0"] + PROJECT_CFLAGS, []),
    "o1": ("gcc", ["-O1"] + PROJECT_CFLAGS, []),
    "san_clang": ("clang", ["-w"] + PROJECT_CFLAGS + ou.san_cflags("clang"), ou.san_ldflags("clang")),
    "san_gcc": ("gcc", ["-w"] + PROJECT_CFLAGS + ou.san_cflags("gcc"), ou.san_ldflags("gcc")),
    "uchar": ("gcc", ["-O2"] + [f for f in PROJECT_CFLAGS if f != "-fsigned-char"] + ["-funsigned-char"], []),
    # clang with only the NULL+0 pointer-overflow check recoverable (oracle_ub_common: *_nz)
    "san_clang_nz": ("clang", ["-w"] + PROJECT_CFLAGS + ou.san_cflags("clang") + ou.NZ_RECOVER, ou.san_ldflags("clang")),
}
UB_CHECKS = ["o0", "o1", "san_clang", "san_gcc", "uchar"]


def prepare_tree(bdir):
    """Copy the pristine libogg tree; generate the two files ./configure would (config_types.h for
    x86-64 glibc: every AC_CHECK_SIZEOF hits the <stdint.h> type; config.h: CRC enabled, nothing
    else that bitwise.c/framing.c read)."""
    src = os.path.join(bdir, "ogg")
    ou.copy_sources(os.path.join(OGG, "ogg"), src)
    os.chmod(src, 0o755)
    for dp, dns, fns in os.walk(src):
        for d in dns:
            os.chmod(os.path.join(dp, d), 0o755)
        for f in fns:
            os.chmod(os.path.join(dp, f), 0o644)
    tin = open(os.path.join(src, "include", "ogg", "config_types.h.in")).read()
    subs = {"@INCLUDE_INTTYPES_H@": "1", "@INCLUDE_STDINT_H@": "1", "@INCLUDE_SYS_TYPES_H@": "1",
            "@SIZE16@": "int16_t", "@USIZE16@": "uint16_t", "@SIZE32@": "int32_t", "@USIZE32@": "uint32_t",
            "@SIZE64@": "int64_t", "@USIZE64@": "uint64_t"}
    for k, v in subs.items():
        tin = tin.replace(k, v)
    with open(os.path.join(src, "include", "ogg", "config_types.h"), "w") as f:
        f.write(tin)
    with open(os.path.join(src, "config.h"), "w") as f:
        f.write("/* generated for the re-grade: default ./configure (CRC enabled; DISABLE_CRC undefined) */\n")
    for t in ("test_bitwise.c", "test_framing.c"):
        shutil.copy(os.path.join(OGG, t), os.path.join(bdir, t))
    return src


def inc(src):
    return ["-I" + src, "-I" + os.path.join(src, "src"), "-I" + os.path.join(src, "include")]


def build_c(bdir, src, file):
    bins, logs = {}, {}
    drv = os.path.join(HERE, f"drv_{file}.c")
    for name, (cc, cflags, ldflags) in C_VARIANTS.items():
        out = os.path.join(bdir, f"c_{file}_{name}")
        rc, log = sc.sh([cc] + cflags + inc(src) + [os.path.join(src, "src", f"{file}.c"), drv, "-o", out] + ldflags)
        if rc != 0:
            raise SystemExit(f"C build {file}/{name} failed:\n{log[-3000:]}")
        bins[name] = out
    return bins


def driver_obj(bdir, src, file):
    o = os.path.join(bdir, f"drv_{file}.o")
    rc, log = sc.sh(["gcc", "-O2", "-g"] + PROJECT_CFLAGS + inc(src) + ["-c", os.path.join(HERE, f"drv_{file}.c"), "-o", o])
    if rc != 0:
        raise SystemExit("driver build failed:\n" + log)
    return o


def c_function_names(tdir):
    names = set()
    for sub in ("functions",):
        d = os.path.join(tdir, sub)
        if os.path.isdir(d):
            names |= {f[:-3] for f in os.listdir(d) if f.endswith(".rs")}
    return names


def sactor_test_obj(bdir, src, file):
    """test_<file>.o built with the command in sactor-datasets/ogg/README.md (step 2)."""
    o = os.path.join(bdir, f"test_{file}.o")
    flags = ["-DHAVE_CONFIG_H", "-I" + os.path.join(src, "src"), "-I" + src, "-I" + os.path.join(src, "include"),
             "-D_V_SELFTEST", "-O2", "-Wall", "-Wextra", "-ffast-math", "-fsigned-char", "-g", "-O2",
             "-c", "-o", o, os.path.join(OGG, f"test_{file}.c"), "-Og", "-g"]
    rc, log = sc.sh(["gcc"] + flags)
    if rc != 0:
        raise SystemExit("test obj build failed:\n" + log)
    return o


def run_sactor_test(exe, env=None):
    e = dict(env or os.environ)
    e["LC_ALL"] = "C"
    e["LANG"] = "C"
    out, err, rc, to = ou.run_proc([exe], b"", env=e, timeout=120)
    return {"exit": rc, "timed_out": to, "pass": (rc == 0 and not to),
            "stdout_tail": out.decode("utf-8", "replace")[-400:], "stderr_tail": err.decode("utf-8", "replace")[-600:]}


def build_rust(bdir, src, model, file, style, drvo, testo):
    """Build one SACTOR translation; returns dict with build info and the linked binaries."""
    tdir = os.path.join(TRANS, model, f"{file}.c", f"translated_code_{style}")
    info = {"dir": os.path.relpath(tdir, sc.ART), "items": {s: len(os.listdir(os.path.join(tdir, s)))
                                                           for s in ("functions", "structs", "global_vars", "enums")
                                                           if os.path.isdir(os.path.join(tdir, s))},
            "combined_rs_shipped": os.path.isfile(os.path.join(tdir, "combined.rs")), "builds": {}}
    if info["combined_rs_shipped"]:
        code = open(os.path.join(tdir, "combined.rs")).read()
        info["source"] = "combined.rs"
    else:
        code = sc.assemble(tdir)
        info["source"] = "per-item files combined (structs, global_vars, functions; merged uses)"
    names = c_function_names(tdir)
    if style == "unidiomatic":
        code = sc.expose_to_c(code, names)
    tag = f"{model}_{file}_{style}"
    with open(os.path.join(bdir, f"lib_{tag}.rs"), "w") as f:
        f.write(code)
    bins = {}
    for tc in sc.TOOLCHAINS:
        for prof in ("debug", "release"):
            crate = os.path.join(bdir, "crates", f"{tag}_{tc}_{prof}")
            ok, log, so = sc.build_crate(code, crate, tc, prof)
            b = {"ok": ok}
            if not ok:
                errs = [ln for ln in log.splitlines() if ln.startswith("error")]
                b["errors"] = errs[:12]
                b["n_errors"] = len(errs)
                b["log_tail"] = log[-1500:]
            info["builds"][f"{tc}:{prof}"] = b
            if not ok or style != "unidiomatic":
                continue
            libdir = os.path.dirname(so)
            exe = os.path.join(bdir, f"rs_{tag}_{tc}_{prof}")
            rc, log = sc.sh(["gcc", "-o", exe, drvo, "-L" + libdir, "-lbuild_attempt", "-lm", "-Wl,-rpath," + libdir])
            b["link_driver"] = rc == 0
            if rc != 0:
                b["link_log"] = log[-1500:]
                continue
            bins[f"rust:{model}:{tc}:{prof}"] = exe
            # SACTOR's own check: test_<file>.o + the Rust lib (e2e_verifier.py), exit 0 = pass
            texe = os.path.join(bdir, f"sactortest_{tag}_{tc}_{prof}")
            rc, log = sc.sh(["gcc", "-o", texe, testo, "-L" + libdir, "-lbuild_attempt", "-lm", "-Wl,-rpath," + libdir])
            b["sactor_test"] = run_sactor_test(texe) if rc == 0 else {"link_failed": log[-800:]}
    return info, bins


def run_case(bins, script):
    res = {}
    data = script.encode()
    for name, exe in bins.items():
        if name.startswith("san_"):
            r = ou.run_san([exe], data, timeout=120, halt=not name.endswith("_nz"))
            res[name] = {"stdout": r["stdout"], "stderr": r["stderr"], "rc": r["rc"], "to": r["timed_out"],
                         "report": r["report"], "infra": r["infra"], "reports": r["reports"]}
        else:
            o, e, rc, to = ou.run_proc([exe], data, timeout=60)
            res[name] = {"stdout": o, "stderr": e, "rc": rc, "to": to, "report": None, "infra": False}
    return res


def first_diff(a, b):
    al, bl = a.split(b"\n"), b.split(b"\n")
    for i in range(max(len(al), len(bl))):
        x = al[i] if i < len(al) else b"<EOF>"
        y = bl[i] if i < len(bl) else b"<EOF>"
        if x != y:
            return {"line": i + 1, "c": x.decode("utf-8", "replace")[:300], "rust": y.decode("utf-8", "replace")[:300]}
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="/build/libogg")
    ap.add_argument("--out", default="/work/results/sactor_libogg.json")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--extra-cases", default=None, help="JSON {file: [ {id, family, script} ]} appended (SACTOR-own-input replay)")
    args = ap.parse_args()
    t0 = time.time()
    os.makedirs(args.build, exist_ok=True)
    src = prepare_tree(args.build)
    cases = {"bitwise": gen_cases.bitwise_cases(), "framing": gen_cases.framing_cases()}
    out = {"system": "SACTOR", "target": "libogg bitwise.c / framing.c (sactor-datasets ogg/)",
           "provenance": sc.provenance(__file__, os.environ.get("SACTOR_COMMAND", " ".join(sys.argv)),
                                       {"positive_controls": ou.positive_controls(os.path.join(args.build, "controls"))}),
           "c_variants": {k: [v[0]] + v[1] + v[2] for k, v in C_VARIANTS.items()},
           "translations": {}, "files": {}}
    for file in FILES:
        fdir = os.path.join(args.build, file)
        os.makedirs(fdir, exist_ok=True)
        cbins = build_c(fdir, src, file)
        drvo = driver_obj(fdir, src, file)
        testo = sactor_test_obj(fdir, src, file)
        # SACTOR's own test against the original C (sanity: must pass)
        cexe = os.path.join(fdir, f"sactortest_C_{file}")
        rc, log = sc.sh(["gcc", "-o", cexe, testo, os.path.join(src, "src", f"{file}.c"),
                         "-DHAVE_CONFIG_H", "-O2", "-ffast-math", "-fsigned-char"] + inc(src) + ["-lm"])
        c_own = run_sactor_test(cexe) if rc == 0 else {"build_failed": log[-1500:]}
        bins = dict(cbins)
        for model in MODELS:
            for style in ("unidiomatic", "idiomatic"):
                info, rb = build_rust(fdir, src, model, file, style, drvo, testo)
                out["translations"][f"{model}/{file}.c/{style}"] = info
                bins.update(rb)
        rust_names = [b for b in bins if b.startswith("rust:")]
        fcases = list(cases[file])
        if args.extra_cases:
            fcases += json.load(open(args.extra_cases)).get(file, [])
        rows = []
        with cf.ThreadPoolExecutor(args.jobs) as ex:
            futs = {ex.submit(run_case, bins, c["script"]): c for c in fcases}
            for fu in cf.as_completed(futs):
                c = futs[fu]
                res = fu.result()
                ref = res["ref"]
                ub, san_nr = [], []
                for k in UB_CHECKS:
                    r = res[k]
                    if k.startswith("san_"):
                        if r["report"]:
                            ub.append(f"{k}: {r['report']}")
                        elif r["infra"]:
                            ub.append(f"{k}: infra")
                        elif (r["stdout"], r["rc"]) != (ref["stdout"], ref["rc"]):
                            # audit 8 uniform rule: disagreement (or timeout) -> not UB-clean
                            san_nr.append(k)
                            ub.append(f"{k}: output differs from ref without a sanitizer report (not UB-clean)")
                    elif (r["stdout"], r["rc"], r["stderr"]) != (ref["stdout"], ref["rc"], ref["stderr"]):
                        ub.append(f"{k}: output differs from ref")
                # secondary view (as oracle_ub_rustassure.py): NULL+0 alone not counted as UB
                nz = res["san_clang_nz"]
                ub_x = ([u for u in ub if not u.startswith("san_clang:")]
                        + [f"san_clang_nz: {x}" for x in ou.other_than_null_zero(nz["reports"])])
                row = {"id": c["id"], "family": c["family"], "c_exit": ref["rc"], "c_ub": ub,
                       "c_ub_excluding_null_plus_zero": ub_x, "san_output_differs_without_report": san_nr, "rust": {}}
                for rn in rust_names:
                    r = res[rn]
                    if (r["stdout"], r["rc"], r["stderr"]) == (ref["stdout"], ref["rc"], ref["stderr"]):
                        row["rust"][rn] = "agree"
                        continue
                    d = {"exit": r["rc"], "timed_out": r["to"],
                         "stdout_differs": r["stdout"] != ref["stdout"], "stderr_differs": r["stderr"] != ref["stderr"],
                         "first_diff": first_diff(ref["stdout"], r["stdout"]),
                         "stderr": r["stderr"].decode("utf-8", "replace")[-400:]}
                    row["rust"][rn] = d
                rows.append(row)
        rows.sort(key=lambda r: r["id"])
        out["files"][file] = {"n_cases": len(fcases), "c_bins": list(cbins), "rust_bins": rust_names,
                              "sactor_own_test_on_C": c_own, "rows": rows}
        out["files"][file]["c_ub_cases"] = sum(1 for r in rows if r["c_ub"])
    out["elapsed_s"] = round(time.time() - t0, 1)
    sc.dump(args.out, out)
    # short console summary
    for file, fd in out["files"].items():
        print(file, "cases", fd["n_cases"], "c_ub", fd["c_ub_cases"])
        for rn in fd["rust_bins"]:
            div = [r for r in fd["rows"] if r["rust"].get(rn) != "agree"]
            div_clean = [r for r in div if not r["c_ub"]]
            print(f"  {rn}: divergent {len(div)} (UB-clean {len(div_clean)})")
    for t, info in out["translations"].items():
        print(t, info["source"], {k: v["ok"] for k, v in info["builds"].items()},
              {k: v.get("sactor_test", {}).get("pass") for k, v in info["builds"].items() if "sactor_test" in v})


if __name__ == "__main__":
    main()
