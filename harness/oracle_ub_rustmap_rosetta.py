"""Oracle-UB validation for the RustMap Rosetta programs: is the C of every counted program
(9 wrong output + 2 panic in results/rustmap_rosetta_triage.json), and of the 2 set aside as
nondeterministic (machine_code) or C-UB (chaocipher), well-defined?

This is the script behind the sentence "The C of every counted program is ASan/UBSan-clean".
C builds of each main.c (run_rosetta.py builds plain `gcc -w main.c -o c_bin -lm`):
  ref        gcc -w main.c -lm            (identical to run_rosetta.py / RustMap's runner)
  o1, o2     gcc -w -O1 / -O2             (optimization-level cross-check: output must equal ref)
  san_clang  clang-14 ASan+UBSan+float-cast-overflow -O0 (non-PIE), -fno-sanitize-recover=all
  san_gcc    gcc-11, same sanitizer set
  san_clang_nz  clang with NULL+0 pointer-overflow recoverable (see oracle_ub_common)
  uchar/schar   gcc -w -funsigned-char / -fsigned-char
  ubitfield     gcc -w -funsigned-bitfields
The Rust (`rustc -A warnings <task>-c.rs`, as run_rosetta.py) is built in phase "rust"
(crust-syzygy, rustc 1.86.0); the C builds and all runs are phase "c" (crust-heldout). The C is run
3 times (ref) to see nondeterminism. Classification reuses run_rosetta.classify / is_random.

  bash harness/oracle_ub_run.sh rustmap_rosetta
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import oracle_ub_common as ou  # noqa: E402

REPO = ou.REPO
sys.path.insert(0, os.path.join(REPO, "systems", "rustmap"))
import run_rosetta as rr  # noqa: E402  (classify / is_random / paths, reused unchanged)

SANS = ("san_clang", "san_gcc", "san_clang_nz")
VARIANTS = {
    "ref": ("gcc", ["-w"]),
    "o1": ("gcc", ["-w", "-O1"]),
    "o2": ("gcc", ["-w", "-O2"]),
    "san_clang": ("clang", ["-w"] + ou.san_cflags("clang") + ou.san_ldflags("clang")),
    "san_gcc": ("gcc", ["-w"] + ou.san_cflags("gcc") + ou.san_ldflags("gcc")),
    "san_clang_nz": ("clang", ["-w"] + ou.san_cflags("clang") + ou.NZ_RECOVER + ou.san_ldflags("clang")),
    "uchar": ("gcc", ["-w", "-funsigned-char"]),
    "schar": ("gcc", ["-w", "-fsigned-char"]),
    "ubitfield": ("gcc", ["-w", "-funsigned-bitfields"]),
}


def tasks():
    t = json.load(open(os.path.join(REPO, "results", "rustmap_rosetta_triage.json")))
    out = []
    for k in t["wrong_output_S1"]:
        out.append((k, "wrong_output_S1"))
    for k in t["rust_panic"]:
        out.append((k, "rust_panic"))
    for k in t["nondeterministic_C_not_counted"]:
        out.append((k, "nondeterministic_C_not_counted"))
    for k in t["excluded_C_UB"]:
        out.append((k, "excluded_C_UB"))
    return out


def paths(task):
    return os.path.join(rr.CDIR, task, "main.c"), os.path.join(rr.RDIR, task + "-c.rs")


def phase_rust(bdir):
    info = {"rustc": ou.tool_version(["rustc", "--version"]), "image": os.environ.get("OUB_IMAGE"),
            "image_id": os.environ.get("OUB_IMAGE_ID"), "cmd": "rustc -A warnings <task>-c.rs -o r_bin",
            "tasks": {}}
    for task, _ in tasks():
        _, rfile = paths(task)
        d = os.path.join(bdir, task)
        os.makedirs(d, exist_ok=True)
        b = os.path.join(d, "r_bin")
        p = subprocess.run(["rustc", "-A", "warnings", rfile, "-o", b], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True)
        info["tasks"][task] = {"ok": p.returncode == 0 and os.path.isfile(b), "bin": b}
    ou.dump(os.path.join(bdir, "rust_build.json"), info)
    print(json.dumps(info, indent=1))


def run(binp, san, halt=True, timeout=10):
    if san:
        r = ou.run_san([binp], b"", timeout=60, halt=halt)
    else:
        o, e, rc, to = ou.run_proc([binp], b"", timeout=timeout)
        r = {"stdout": o, "stderr": e, "rc": rc, "timed_out": to, "report": None, "reports": [], "infra": False}
    return r


def phase_c(bdir, out_path, t0):
    rinfo = json.load(open(os.path.join(bdir, "rust_build.json")))
    before = json.load(open(os.path.join(REPO, "results", "rustmap_rosetta.json")))
    before_real = set(before["summary"]["real_divergent_tasks"])
    rows = []
    for task, label in tasks():
        cmain, rfile = paths(task)
        d = os.path.join(bdir, task)
        row = {"task": task, "triage_label": label, "c_digest": ou.tree_digest(cmain)["sha256"],
               "rust_digest": ou.tree_digest(rfile)["sha256"]}
        bins = {}
        for name, (cc, flags) in VARIANTS.items():
            b = os.path.join(d, f"c_{name}")
            # flags before the source, -lm after it (like run_rosetta.py's `gcc -w main.c -o b -lm`)
            p = subprocess.run([cc] + flags + [cmain, "-o", b, "-lm"], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
            bins[name] = b if p.returncode == 0 and os.path.isfile(b) else None
            if not bins[name]:
                row.setdefault("build_fail", {})[name] = p.stdout[-400:]
        rbin = rinfo["tasks"][task]["bin"] if rinfo["tasks"][task]["ok"] else None
        if not (bins["ref"] and rbin):
            row["error"] = "ref C or Rust did not build"
            rows.append(row)
            continue
        rs = run(rbin, False)
        res = {n: run(b, n.startswith("san_"), halt=not n.endswith("_nz")) for n, b in bins.items() if b}
        ref = res["ref"]
        row["ref_repeat_identical"] = all(
            (lambda x: x["stdout"] == ref["stdout"] and x["rc"] == ref["rc"])(run(bins["ref"], False))
            for _ in range(2))
        k = rr.classify(task, ref["stdout"], ref["rc"], rs["stdout"], rs["rc"], ref["timed_out"], rs["timed_out"])
        if k == "divergent_real" and rr.is_random(cmain, rfile):
            k = "divergent_nondeterministic"
        row.update({"fresh_class": k, "before_divergent_real": task in before_real,
                    "reproduced": (k == "divergent_real") == (task in before_real),
                    "c_exit": ref["rc"], "rust_exit": rs["rc"],
                    "c_stdout_head": ref["stdout"][:160].decode("latin1"),
                    "rust_stdout_head": rs["stdout"][:160].decode("latin1")})
        ub, unres = [], []
        for n in ("o1", "o2", "san_clang", "san_gcc", "san_clang_nz", "uchar", "schar", "ubitfield"):
            if n not in res:
                continue
            r = res[n]
            rec = {"same_as_ref": r["stdout"] == ref["stdout"] and r["rc"] == ref["rc"], "exit": r["rc"],
                   "timed_out": r["timed_out"]}
            if n in ("san_clang", "san_gcc", "uchar", "schar") and not r["report"]:
                unres += ou.not_clean_reasons(n, r, rec["same_as_ref"])      # audit 8 uniform rule
            if n.startswith("san_"):
                rec.update({"report": r["report"], "reports": r["reports"], "infra": r["infra"],
                            "other_than_null_plus_zero": ou.other_than_null_zero(r["reports"])})
                if r["report"]:
                    ub.append(f"{n}: {r['report']}")
            else:
                kk = rr.classify(task, r["stdout"], r["rc"], rs["stdout"], rs["rc"], r["timed_out"], rs["timed_out"])
                rec["class_vs_rust"] = kk
                if n in ("o1", "o2") and not rec["same_as_ref"]:
                    ub.append(f"opt-level: -{n.upper()} output differs from -O0")
            row[n] = rec
        row["c_ub_evidence"] = ub
        row["c_ub_evidence_excluding_null_plus_zero"] = (
            ([f"san_gcc: {row['san_gcc']['report']}"] if row.get("san_gcc", {}).get("report") else [])
            + [f"san_clang_nz: {x}" for x in row.get("san_clang_nz", {}).get("other_than_null_plus_zero", [])]
            + [u for u in ub if u.startswith("opt-level")])
        row["char_signedness_dependent"] = not (row["uchar"]["same_as_ref"] and row["schar"]["same_as_ref"])
        row["bitfield_signedness_dependent"] = not row["ubitfield"]["same_as_ref"]
        counted_before = label in ("wrong_output_S1", "rust_panic")
        row["counted_before"] = counted_before
        row["ub_rule_not_clean"] = unres
        row["counted_after"] = (counted_before and k == "divergent_real" and not ub and not unres and row["ref_repeat_identical"]
                                and not any(row[n]["class_vs_rust"].startswith("agree")
                                            for n in ("o1", "o2", "uchar", "schar", "ubitfield")))
        rows.append(row)
        print(f"[{time.time()-t0:.0f}s] {task}: {k} ub={ub}", flush=True)
    cb = [r for r in rows if r.get("counted_before")]
    summary = {
        "tasks": len(rows),
        "counted_before": {"wrong_output_S1": sum(1 for r in cb if r["triage_label"] == "wrong_output_S1"),
                           "rust_panic": sum(1 for r in cb if r["triage_label"] == "rust_panic")},
        "counted_after": {"wrong_output_S1": sum(1 for r in cb if r["triage_label"] == "wrong_output_S1" and r["counted_after"]),
                          "rust_panic": sum(1 for r in cb if r["triage_label"] == "rust_panic" and r["counted_after"])},
        "counted_with_sanitizer_report": sorted(r["task"] for r in cb if r.get("c_ub_evidence")),
        "counted_char_signedness_dependent": sorted(r["task"] for r in cb if r.get("char_signedness_dependent")),
        "counted_bitfield_signedness_dependent": sorted(r["task"] for r in cb if r.get("bitfield_signedness_dependent")),
        "reproduced_divergence": sum(1 for r in rows if r.get("reproduced")),
        "not_reproduced": sorted(r["task"] for r in rows if not r.get("reproduced")),
        "set_aside": {r["task"]: {"label": r["triage_label"], "c_ub_evidence": r.get("c_ub_evidence"),
                                  "ref_repeat_identical": r.get("ref_repeat_identical")}
                      for r in rows if not r.get("counted_before")},
    }
    out = {
        "provenance": ou.provenance(__file__, {
            "rust_build": {k: v for k, v in rinfo.items() if k != "tasks"},
            "artifact_source_digests": {"rosseta-125": ou.tree_digest(rr.CDIR),
                                        "125-rosetta-code-gpt": ou.tree_digest(rr.RDIR)},
            "c_builds": {k: [v[0]] + v[1] for k, v in VARIANTS.items()},
            "compared_against": ["results/rustmap_rosetta.json", "results/rustmap_rosetta_triage.json"],
            "wall_seconds": round(time.time() - t0),
            "sanitizer_positive_controls": ou.positive_controls(os.path.join(bdir, "controls")),
        }),
        "summary": summary,
        "verdicts": rows,
    }
    ou.dump(out_path, out)
    print(json.dumps(summary, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["rust", "c"], required=True)
    ap.add_argument("--build", default="/tmp/oub_rosetta")
    ap.add_argument("--out", default=os.path.join(REPO, "results", "oracle_ub_rustmap_rosetta.json"))
    args = ap.parse_args()
    os.makedirs(args.build, exist_ok=True)
    if args.phase == "rust":
        phase_rust(args.build)
    else:
        phase_c(args.build, args.out, time.time())


if __name__ == "__main__":
    main()
