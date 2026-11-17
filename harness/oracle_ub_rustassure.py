"""Oracle-UB validation for RustAssure libcsv (systems/rustassure/libcsv_e2e): is the original
libcsv C well-defined on all 360 cases, in particular the 12 counted divergences?

C builds of (original libcsv.c + our csv_oracle.c driver):
  ref        gcc -O2  (exactly as libcsv_e2e/run.py builds the oracle)
  o0, o1     gcc -O0 / -O1   (optimization-level cross-check: must equal ref)
  san_clang  clang-14 ASan+UBSan+float-cast-overflow -O0 (non-PIE), -fno-sanitize-recover=all
  san_gcc    gcc-11, same sanitizer set
  san_clang_nz  clang, NULL+0 pointer-overflow recoverable (reports, continues; see oracle_ub_common)
  uchar      gcc -O2 -funsigned-char ; schar  gcc -O2 -fsigned-char
  ubitfield  gcc -O2 -funsigned-bitfields (implementation-defined: plain int bit-field signedness)
SUT: RustAssure's composed csv_link translation, extracted by libcsv_e2e/extract_sut.py into a
scratch crate (same Cargo.toml), cargo build --release on nightly-2022-08-08.

Every case is re-run against every build; the run.py channel tagging is reused unchanged. A counted
divergence becomes original-C UB if a sanitizer reports or the C output changes with -O level.
Nothing under systems/ or artifacts/ is written (run.py itself writes csv_sut.rs, sut_crate/ and
/tmp/csv_oracle, so we do not call it).

  bash harness/oracle_ub_run.sh rustassure     (crust-heldout, x86-64 box)
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import oracle_ub_common as ou  # noqa: E402

REPO = ou.REPO
E2E = os.path.join(REPO, "systems", "rustassure", "libcsv_e2e")
sys.path.insert(0, E2E)
from gen_corpus import corpus, OPTIONS  # noqa: E402
from run import classify  # noqa: E402  (run.py's channel tagging, reused unchanged)

SANS = ("san_clang", "san_gcc", "san_clang_nz")
LIBCSV = os.path.join(REPO, "artifacts", "rustassure", "src", "python", "inputs-complex", "libcsv")

VARIANTS = {
    "ref": ("gcc", ["-O2"], []),
    "o0": ("gcc", ["-O0"], []),
    "o1": ("gcc", ["-O1"], []),
    "san_clang": ("clang", ["-w"] + ou.san_cflags("clang"), ou.san_ldflags("clang")),
    "san_gcc": ("gcc", ["-w"] + ou.san_cflags("gcc"), ou.san_ldflags("gcc")),
    "san_clang_nz": ("clang", ["-w"] + ou.san_cflags("clang") + ou.NZ_RECOVER, ou.san_ldflags("clang")),
    "uchar": ("gcc", ["-O2", "-funsigned-char"], []),
    "schar": ("gcc", ["-O2", "-fsigned-char"], []),
    "ubitfield": ("gcc", ["-O2", "-funsigned-bitfields"], []),
}


def build(bdir):
    os.makedirs(bdir, exist_ok=True)
    bins = {}
    csrc = os.path.join(bdir, "c_src")
    ou.copy_sources(LIBCSV, csrc)
    shutil.copy(os.path.join(E2E, "csv_oracle.c"), os.path.join(bdir, "csv_oracle.c"))
    for name, (cc, cflags, ldflags) in VARIANTS.items():
        b = os.path.join(bdir, f"csv_oracle_{name}")
        p = subprocess.run([cc] + cflags + ["-I", csrc, os.path.join(csrc, "libcsv.c"),
                            os.path.join(bdir, "csv_oracle.c"), "-o", b] + ldflags,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if p.returncode != 0:
            raise SystemExit(f"C build {name} failed:\n{p.stdout[-2000:]}")
        bins[name] = b
    crate = os.path.join(bdir, "sut_crate")
    if os.path.isdir(crate):
        shutil.rmtree(crate)
    os.makedirs(os.path.join(crate, "src"))
    shutil.copy(os.path.join(E2E, "sut_crate", "Cargo.toml"), os.path.join(crate, "Cargo.toml"))
    p = subprocess.run([sys.executable, os.path.join(E2E, "extract_sut.py"),
                        os.path.join(crate, "src", "main.rs")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if p.returncode != 0:
        raise SystemExit("extract_sut failed: " + p.stdout.decode()[-1000:])
    p = subprocess.run(["cargo", "build", "--release"], cwd=crate, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True)
    sut = os.path.join(crate, "target", "release", "csv_sut")
    if not os.path.isfile(sut):
        raise SystemExit("SUT build failed:\n" + p.stdout[-2000:])
    bins["sut"] = sut
    return bins


def tags_of(o_out, o_rc, s_out, s_rc):
    t = classify(o_out, s_out)
    if o_rc != s_rc:
        t.add(f"exit({o_rc}vs{s_rc})")
    return sorted(t)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="/tmp/oub_libcsv")
    ap.add_argument("--out", default=os.path.join(REPO, "results", "oracle_ub_rustassure.json"))
    args = ap.parse_args()
    t0 = time.time()
    bins = build(args.build)
    before = json.load(open(os.path.join(E2E, "results.json")))
    rows = []
    for oname, opt in OPTIONS:
        for cname, data in corpus():
            res = {}
            for name, b in bins.items():
                if name.startswith("san_"):
                    r = ou.run_san([b, str(opt)], data, timeout=60, halt=not name.endswith("_nz"))
                else:
                    o, e, rc, to = ou.run_proc([b, str(opt)], data, timeout=20)
                    r = {"stdout": o, "stderr": e, "rc": rc, "timed_out": to, "report": None, "infra": False}
                res[name] = r
            ref, sut = res["ref"], res["sut"]
            tags = tags_of(ref["stdout"], ref["rc"], sut["stdout"], sut["rc"])
            row = {"input": cname, "options": oname, "opt": opt, "divergent": bool(tags), "tags": tags,
                   "ref_exit": ref["rc"], "sut_exit": sut["rc"]}
            ub, unres = [], []
            for name in ("o0", "o1", "san_clang", "san_gcc", "san_clang_nz", "uchar", "schar", "ubitfield"):
                r = res[name]
                same = r["stdout"] == ref["stdout"] and r["rc"] == ref["rc"]
                rec = {"same_as_ref": same, "timed_out": r["timed_out"]}
                if name in ("san_clang", "san_gcc", "uchar", "schar") and not r["report"]:
                    unres += ou.not_clean_reasons(name, r, same)      # audit 8 uniform rule
                if name.startswith("san_"):
                    rec["report"] = r["report"]
                    rec["reports"] = r["reports"]
                    rec["other_than_null_plus_zero"] = ou.other_than_null_zero(r["reports"])
                    rec["infra"] = r["infra"]
                    if r["report"]:
                        ub.append(f"{name}: {r['report']}")
                elif name in ("o0", "o1") and not same:
                    ub.append(f"opt-level: {name} output differs from -O2")
                if name in ("uchar", "schar", "ubitfield"):
                    rec["tags_vs_sut"] = tags_of(r["stdout"], r["rc"], sut["stdout"], sut["rc"])
                row[name] = rec
            row["c_ub_evidence"] = ub
            row["c_ub_evidence_excluding_null_plus_zero"] = (
                ([f"san_gcc: {row['san_gcc']['report']}"] if row["san_gcc"]["report"] else [])
                + [f"san_clang_nz: {x}" for x in row["san_clang_nz"]["other_than_null_plus_zero"]]
                + [u for u in ub if u.startswith("opt-level")])
            row["ub_rule_not_clean"] = unres
            row["final"] = ("original_C_UB" if (tags and ub) else "not_ub_clean" if (tags and unres)
                            else ("divergent" if tags else "agree"))
            rows.append(row)
    div = [r for r in rows if r["divergent"]]
    by_tag = {}
    for r in div:
        for t in r["tags"]:
            by_tag[t] = by_tag.get(t, 0) + 1
    by_opt = {}
    for r in div:
        by_opt[r["options"]] = by_opt.get(r["options"], 0) + 1
    summary = {
        "before": {k: before[k] for k in ("n_cases", "agree", "divergent", "divergence_by_channel",
                                          "divergence_by_option")},
        "fresh": {"n_cases": len(rows), "agree": len(rows) - len(div), "divergent": len(div),
                  "divergence_by_channel": by_tag, "divergence_by_option": by_opt},
        "after_ub_filter": {"divergent_counted": sum(1 for r in rows if r["final"] == "divergent"),
                            "original_C_UB": sum(1 for r in rows if r["final"] == "original_C_UB"),
                            "not_ub_clean": sum(1 for r in rows if r["final"] == "not_ub_clean"),
                            "agree": sum(1 for r in rows if r["final"] == "agree")},
        "after_ub_filter_excluding_null_plus_zero": {
            "divergent_counted": sum(1 for r in div if not r["c_ub_evidence_excluding_null_plus_zero"] and not r["ub_rule_not_clean"]),
            "original_C_UB": sum(1 for r in div if r["c_ub_evidence_excluding_null_plus_zero"])},
        "sanitizer_reports_any_case": {s: sum(1 for r in rows if r[s]["report"]) for s in SANS},
        "distinct_reports": sorted({x for r in rows for s in SANS for x in r[s]["reports"]}),
        "san_infra_cases": sum(1 for r in rows if any(r[s]["infra"] for s in SANS)),
        "san_output_differs_without_report": sum(
            1 for r in rows for s in SANS if not r[s]["report"] and not r[s]["same_as_ref"]),
        "opt_level_differs_any_case": sum(1 for r in rows if not (r["o0"]["same_as_ref"] and r["o1"]["same_as_ref"])),
        "char_signedness": {
            "cases_where_C_output_changes_-funsigned-char": sum(1 for r in rows if not r["uchar"]["same_as_ref"]),
            "cases_where_C_output_changes_-fsigned-char": sum(1 for r in rows if not r["schar"]["same_as_ref"]),
            "counted_cases_whose_verdict_changes": sum(
                1 for r in div if r["uchar"]["tags_vs_sut"] != r["tags"] or r["schar"]["tags_vs_sut"] != r["tags"]),
        },
        "bitfield_signedness": {
            "cases_where_C_output_changes_-funsigned-bitfields": sum(1 for r in rows if not r["ubitfield"]["same_as_ref"]),
            "counted_cases_whose_verdict_changes": sum(1 for r in div if r["ubitfield"]["tags_vs_sut"] != r["tags"])},
    }
    summary["reproduces_before"] = (summary["fresh"]["divergent"] == before["divergent"]
                                    and by_tag == before["divergence_by_channel"]
                                    and by_opt == before["divergence_by_option"])
    out = {
        "provenance": ou.provenance(__file__, {
            "artifact_source_digests": {"libcsv": ou.tree_digest(LIBCSV),
                                        "csv_link_main.rs": ou.tree_digest(os.path.join(
                                            REPO, "artifacts", "rustassure", "src", "Link", "csv_link", "src", "main.rs"))},
            "builds": {k: {"cc": v[0], "flags": v[1] + v[2]} for k, v in VARIANTS.items()},
            "sut_build": "extract_sut.py -> scratch crate (systems/rustassure/libcsv_e2e/sut_crate/Cargo.toml), "
                         "cargo build --release",
            "compared_against": "systems/rustassure/libcsv_e2e/results.json",
            "wall_seconds": round(time.time() - t0),
            "sanitizer_positive_controls": ou.positive_controls(os.path.join(args.build, "controls")),
        }),
        "summary": summary,
        "verdicts": rows,
    }
    ou.dump(args.out, out)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
