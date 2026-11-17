"""Oracle-UB validation for Flourine's two harnesses (systems/flourine/run.py, run_struct.py).

The paper says the struct harness "keeps every read in bounds by construction" and the scalar
harness builds the C with -fwrapv. Both C oracles are built -O1 -fwrapv with no sanitizer. Here we
build the SAME generated C driver (each harness's own build_c writes it; we reuse that p.c) as:
  ref           gcc -w -O1 -fwrapv            (exactly the harness's oracle)
  alt_o0        gcc -w -O0 -fwrapv            (optimization-level cross-check, as the VERT harness)
  alt_o2        gcc -w -O2 -fwrapv            (also -fstrict-aliasing)
  nowrap        gcc -w -O1                    (ISO C semantics: does the output depend on -fwrapv?)
  san_clang     clang-14 ASan+UBSan+float-cast-overflow -O0, no -fwrapv (non-PIE), no recovery
  san_gcc       gcc-11, same sanitizer set, no -fwrapv
  san_gcc_wrapv gcc-11 sanitizers WITH -fwrapv (UB that remains even under the harness's -fwrapv)
  uchar/schar   ref + -funsigned-char / -fsigned-char
  ubitfield     ref + -funsigned-bitfields
and re-run the counted divergences: scalar = the 5 success translations in results/flourine_diff.json
(clip x2 and opl_emu_clamp: min>max; opl_emu_registers_operator_list x2: byte-pack), on the full
run.py grid (enumerate_cases, cap 2000); struct = the success translation of
opl_emu_registers_lfo_pm_depth in results/flourine_struct.json (the OPL mask case), on run_struct's
full feed. For the struct harness we also run the sanitized C of ALL 15 drivable functions over the
whole feed, which tests "in bounds by construction" directly. (ASan cannot see an over-read that
stays inside struct opl_emu_registers; UBSan -fsanitize=bounds does catch m_regdata[i] with
i >= 0x200 when the access is an array subscript.)

Sanitized runs are batched and resumed: the driver flushes one line per input, so when a
non-recoverable check aborts after k lines, input k is the UB input; we restart at k+1.
Phase "rust" (crust-syzygy, rustc 1.86.0) builds the Rust with the harnesses' own build_rust;
phase "c" (crust-heldout, which has clang) builds the C and runs everything.

  bash harness/oracle_ub_run.sh flourine
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
sys.path.insert(0, os.path.join(REPO, "systems", "flourine"))
sys.path.insert(0, os.path.join(REPO, "systems"))
import run as fl_scalar  # noqa: E402  systems/flourine/run.py
import run_struct as fl_struct  # noqa: E402  systems/flourine/run_struct.py
from enumerate_inputs import enumerate_cases  # noqa: E402
from batch_diff import eval_cases  # noqa: E402

CC_VARIANTS = {
    "ref": ("gcc", ["-w", "-O1", "-fwrapv"]),
    "alt_o0": ("gcc", ["-w", "-O0", "-fwrapv"]),
    "alt_o2": ("gcc", ["-w", "-O2", "-fwrapv"]),
    "nowrap": ("gcc", ["-w", "-O1"]),
    "san_clang": ("clang", ["-w"] + ou.san_cflags("clang") + ou.san_ldflags("clang")),
    "san_gcc": ("gcc", ["-w"] + ou.san_cflags("gcc") + ou.san_ldflags("gcc")),
    "san_gcc_wrapv": ("gcc", ["-w", "-fwrapv"] + ou.san_cflags("gcc") + ou.san_ldflags("gcc")),
    "uchar": ("gcc", ["-w", "-O1", "-fwrapv", "-funsigned-char"]),
    "schar": ("gcc", ["-w", "-O1", "-fwrapv", "-fsigned-char"]),
    "ubitfield": ("gcc", ["-w", "-O1", "-fwrapv", "-funsigned-bitfields"]),
}
SANS = ("san_clang", "san_gcc", "san_gcc_wrapv")


SPREAD = os.path.join(REPO, "results", "rerun-2026-11-12", "flourine_diff_spread.json")


def scalar_targets():
    d = json.load(open(os.path.join(REPO, "results", "flourine_diff.json")))
    # results/flourine_diff.json predates the audit-7 spread multi-arg grid; the current
    # enumerate_inputs.py grid was re-run into results/rerun-2026-11-12/flourine_diff_spread.json
    spread = {s["t"]: s["value"] for s in json.load(open(SPREAD))["success_divergences"]} \
        if os.path.isfile(SPREAD) else {}
    out = []
    for s in d["success_divergences"]:
        tag = s["t"]
        rs = os.path.join(fl_scalar.FL, "translations_only", "success", "c", tag)
        proj, name = tag.split("/")[0], tag.split("/")[1]
        cfile = os.path.join(fl_scalar.BMS, proj, name, name + ".c")
        out.append({"tag": tag, "rs": rs, "cfile": cfile, "name": name, "before_value": s["value"],
                    "before_value_spread_grid": spread.get(tag)})
    return out


def struct_targets():
    d = json.load(open(os.path.join(REPO, "results", "flourine_struct.json")))
    return [{"tag": s["t"], "rs": os.path.join(fl_struct.TR, "success", "c", "opl", s["t"]),
             "name": s["t"].split("/")[0], "before_value": s["value"]} for s in d["success_divergences"]]


# ------------------------------------------------------------------ phase rust
def phase_rust(bdir):
    info = {"rustc": ou.tool_version(["rustc", "--version"]), "image": os.environ.get("OUB_IMAGE"),
            "image_id": os.environ.get("OUB_IMAGE_ID"), "scalar": {}, "struct": {}}
    for i, t in enumerate(scalar_targets()):
        d = os.path.join(bdir, "scalar_rs", str(i))
        os.makedirs(d, exist_ok=True)
        sig = fl_scalar.parse_rust_fn(open(t["rs"], errors="replace").read(), t["name"])
        b = fl_scalar.build_rust(t["name"], t["rs"], sig, d)
        info["scalar"][t["tag"]] = {"bin": b, "sig": sig}
    for i, t in enumerate(struct_targets()):
        d = os.path.join(bdir, "struct_rs", str(i))
        os.makedirs(d, exist_ok=True)
        rinfo = fl_struct.classify_regfn(open(t["rs"], errors="replace").read(), t["name"])
        b, st = fl_struct.build_rust(t["rs"], t["name"], rinfo, d)
        info["struct"][t["tag"]] = {"bin": b, "status": st, "info": rinfo}
    ou.dump(os.path.join(bdir, "rust_build.json"), info)
    print(json.dumps(info, indent=1))


# ------------------------------------------------------------------ C side
def build_variants(pc, d):
    """pc = the harness-generated C driver source file. Returns {variant: binary}."""
    bins = {}
    for name, (cc, flags) in CC_VARIANTS.items():
        b = os.path.join(d, f"c_{name}")
        p = subprocess.run([cc] + flags + [pc, "-o", b], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if p.returncode != 0 or not os.path.isfile(b):
            raise SystemExit(f"C build {name} failed for {pc}:\n{p.stdout[-1500:]}")
        bins[name] = b
    return bins


def batch_lines(binp, lines, timeout=120):
    o, e, rc, to = ou.run_proc([binp], ("\n".join(lines) + "\n").encode(), timeout=timeout)
    return o.decode("latin1").splitlines(), rc, to


def san_per_line(binp, lines):
    """Resumable batch: returns (reports, outs, infra, status) aligned to lines. status[i] is "ok"
    (the sanitizer build printed this line), "report", "infra", "timeout" (the line hung or was not
    reached because an earlier one did) or "not_run" (the run ended before printing it). Audit 8:
    only "ok" lines can be UB-clean -- a line the sanitizer never ran used to count as clean."""
    reports = [None] * len(lines)
    outs = [None] * len(lines)
    status = ["not_run"] * len(lines)
    infra = 0
    k = 0
    while k < len(lines):
        r = ou.run_san([binp], ("\n".join(lines[k:]) + "\n").encode(), timeout=300)
        got = r["stdout"].decode("latin1").splitlines()
        for j, g in enumerate(got):
            if k + j < len(lines):
                outs[k + j] = g
                status[k + j] = "ok"
        if r["infra"]:
            infra += 1
            for j in range(k, min(len(lines), k + max(1, len(got)))):
                status[j] = "infra"
            k += max(1, len(got))     # should not happen (non-PIE / gcc); do not loop forever
            continue
        if r["report"]:
            bad = k + len(got)
            if bad < len(lines):
                reports[bad] = r["report"]
                status[bad] = "report"
            k = bad + 1
        elif r["timed_out"]:
            for j in range(k + len(got), len(lines)):
                status[j] = "timeout"
            break
        else:
            break                     # ran to the end cleanly
    return reports, outs, infra, status


def c_side(bins, lines):
    """Run every C variant over all input lines."""
    res = {}
    for name, b in bins.items():
        if name in SANS:
            reps, outs, infra, status = san_per_line(b, lines)
            res[name] = {"reports": reps, "outs": outs, "infra": infra, "status": status}
        else:
            outs, rc, to = batch_lines(b, lines)
            res[name] = {"outs": outs + [None] * (len(lines) - len(outs)), "rc": rc, "timed_out": to}
    return res


def per_input_flags(i, c, ref_line):
    f = {}
    for s in SANS:
        if c[s]["reports"][i]:
            f[s] = c[s]["reports"][i]
        elif c[s]["status"][i] != "ok":           # audit 8: failed / timed out / never reached
            f[s + "_not_clean"] = c[s]["status"][i]
        elif c[s]["outs"][i] != ref_line:         # audit 8: sanitizer build disagrees with the oracle
            f[s + "_differs"] = c[s]["outs"][i]
    for n in ("alt_o0", "alt_o2", "nowrap", "uchar", "schar", "ubitfield"):
        if c[n]["outs"][i] != ref_line:
            f[n + "_differs"] = c[n]["outs"][i]
    return f


def summarize_inputs(div):
    """div: list of per-input dicts with 'flags'. Clean = no sanitizer (without -fwrapv), and
    -O0 == -O1, and the output does not depend on -fwrapv or char signedness."""
    n = len(div)
    ub_iso = [d for d in div if any(s in d["flags"] for s in ("san_clang", "san_gcc"))]
    ub_wrapv = [d for d in div if "san_gcc_wrapv" in d["flags"]]
    opt = [d for d in div if "alt_o0_differs" in d["flags"] or "alt_o2_differs" in d["flags"]]
    wrapdep = [d for d in div if "nowrap_differs" in d["flags"]]
    chardep = [d for d in div if "uchar_differs" in d["flags"] or "schar_differs" in d["flags"]]
    bitdep = [d for d in div if "ubitfield_differs" in d["flags"]]
    san_nc = [d for d in div if any(k.endswith("_not_clean") for k in d["flags"])]
    san_df = [d for d in div if any(s + "_differs" in d["flags"] for s in SANS)]
    clean = [d for d in div if not d["flags"]]
    kinds = {}
    for d in ub_iso:
        for s in ("san_clang", "san_gcc"):
            if s in d["flags"]:
                k = d["flags"][s].split("runtime error:")[-1].strip()[:90]
                kinds[k] = kinds.get(k, 0) + 1
    return {"divergent_inputs": n, "ub_under_iso_c_sanitizers": len(ub_iso),
            "ub_even_with_fwrapv": len(ub_wrapv), "opt_level_dependent": len(opt),
            "fwrapv_dependent_output": len(wrapdep), "char_signedness_dependent": len(chardep),
            "bitfield_signedness_dependent": len(bitdep),
            "sanitizer_run_failed_or_timed_out": len(san_nc), "sanitizer_output_differs_without_report": len(san_df),
            "clean_divergent_inputs": len(clean), "report_kinds": kinds,
            "smallest_clean": sorted((d for d in clean), key=lambda d: sum(abs(x) for x in d["in"]))[:3]
            if clean and isinstance(clean[0]["in"], list) else clean[:3]}


def phase_c(bdir, out_path, t0):
    rinfo = json.load(open(os.path.join(bdir, "rust_build.json")))
    scalar_rows, struct_rows, struct_all = [], [], []
    cache = {}
    # ---------------- scalar harness ----------------
    for i, t in enumerate(scalar_targets()):
        rb = rinfo["scalar"][t["tag"]]
        sig = rb["sig"]
        cases = enumerate_cases(sig["ptypes"], cap=2000)
        lines = [" ".join(str(x) for x in row) for row in cases]
        key = t["cfile"]
        if key not in cache:
            d = os.path.join(bdir, "scalar_c", t["name"])
            if os.path.isdir(d):
                shutil.rmtree(d)
            os.makedirs(d)
            ref = fl_scalar.build_c(t["name"], t["cfile"], sig, d)      # writes d/p.c (+ its own -O1 -fwrapv build)
            if not ref:
                raise SystemExit(f"scalar C build failed: {t['tag']}")
            bins = build_variants(os.path.join(d, "p.c"), d)
            cache[key] = (bins, c_side(bins, lines))
        bins, c = cache[key]
        trip = eval_cases(bins["ref"], rb["bin"], cases)                # the harness's own pairing
        div = []
        for j, (row, cl, rl) in enumerate(trip):
            if cl == "__PARSE__" or rl == "__PARSE__":
                continue
            if cl is not None and cl != "__TIMEOUT__" and cl == rl:
                continue
            kind = "crash" if (rl is None or rl == "__PANIC__") else ("hang" if rl == "__TIMEOUT__" else "value")
            fl = per_input_flags(j, c, cl)
            div.append({"in": row, "c": cl, "rust": rl, "kind": kind, "flags": fl,
                        "rust_vs_uchar_agree": c["uchar"]["outs"][j] == rl})
        s = summarize_inputs([d for d in div if d["kind"] == "value"])
        row = {"tag": t["tag"], "before_value_divergences": t["before_value"],
               "before_value_divergences_spread_grid": t["before_value_spread_grid"], "fresh": {
            "value": sum(1 for d in div if d["kind"] == "value"), "crash": sum(1 for d in div if d["kind"] == "crash"),
            "hang": sum(1 for d in div if d["kind"] == "hang")}, "inputs": len(cases), **s}
        row["reproduced_old_grid_count"] = row["fresh"]["value"] == t["before_value"]
        row["reproduced"] = row["fresh"]["value"] == t["before_value_spread_grid"]
        row["counted_after"] = s["clean_divergent_inputs"] > 0
        row["divergent_inputs_detail"] = div[:60]
        if t["name"] == "opl_emu_registers_operator_list":
            # the paper's example is not on the grid: run it directly through every build
            ex = ["24 211 56 46"]
            exr = {}
            for n, b in bins.items():
                if n in SANS:
                    rep_, outs, _, _ = san_per_line(b, ex)
                    exr[n] = {"out": outs[0], "report": rep_[0]}
                else:
                    exr[n] = {"out": batch_lines(b, ex)[0][0]}
            exr["rust"] = {"out": batch_lines(rb["bin"], ex)[0][0]}
            row["paper_example_24_211_56_46"] = exr
            # by o4 (the byte shifted left by 24, promoted to int)
            row["divergent_by_o4_ge_128"] = {
                "o4>=128": sum(1 for d in div if d["in"][3] >= 128),
                "o4>=128_with_ub_report": sum(1 for d in div if d["in"][3] >= 128 and ("san_gcc" in d["flags"] or "san_clang" in d["flags"])),
                "o4<128": sum(1 for d in div if d["in"][3] < 128),
                "o4<128_with_ub_report": sum(1 for d in div if d["in"][3] < 128 and ("san_gcc" in d["flags"] or "san_clang" in d["flags"]))}
        scalar_rows.append(row)
        print(f"[{time.time()-t0:.0f}s] scalar {t['tag']}: {row['fresh']} clean={s['clean_divergent_inputs']}", flush=True)
    # ---------------- struct harness: every drivable function's C over the whole feed -------------
    targets = {name: (cfile, info) for name, cfile, _v, info in fl_struct.discover()}
    struct_bins = {}
    for name, (cfile, info) in sorted(targets.items()):
        d = os.path.join(bdir, "struct_c", name)
        if os.path.isdir(d):
            shutil.rmtree(d)
        os.makedirs(d)
        if not fl_struct.build_c(cfile, name, info, d):
            struct_all.append({"fn": name, "error": "ref build failed"})
            continue
        bins = build_variants(os.path.join(d, "p.c"), d)
        lines = [l for l in fl_struct.feed(info).split("\n") if l.strip()]
        c = c_side(bins, lines)
        struct_bins[name] = (bins, c, lines)
        flagged = [(lines[i], per_input_flags(i, c, c["ref"]["outs"][i])) for i in range(len(lines))]
        flagged = [(l, f) for l, f in flagged if f]
        struct_all.append({"fn": name, "inputs": len(lines),
                           "inputs_with_sanitizer_report": sum(1 for _, f in flagged if any(s in f for s in SANS)),
                           "inputs_opt_level_dependent": sum(1 for _, f in flagged if "alt_o0_differs" in f or "alt_o2_differs" in f),
                           "inputs_bitfield_signedness_dependent": sum(1 for _, f in flagged if "ubitfield_differs" in f),
                           "inputs_fwrapv_dependent": sum(1 for _, f in flagged if "nowrap_differs" in f),
                           "inputs_char_signedness_dependent": sum(1 for _, f in flagged if "uchar_differs" in f or "schar_differs" in f),
                           "san_infra": sum(c[s]["infra"] for s in SANS),
                           "examples": flagged[:5]})
        print(f"[{time.time()-t0:.0f}s] struct C {name}: {struct_all[-1]['inputs_with_sanitizer_report']} UB inputs", flush=True)
    # ---------------- struct harness: the counted divergence -------------
    for t in struct_targets():
        rb = rinfo["struct"][t["tag"]]
        bins, c, lines = struct_bins[t["name"]]
        ro, rrc, rto = batch_lines(rb["bin"], lines)
        div = []
        for i, l in enumerate(lines):
            cl = c["ref"]["outs"][i]
            rl = ro[i] if i < len(ro) else None
            if cl is None or cl == rl:
                continue
            kind = "crash" if (rl is None or rl == "__PANIC__") else "value"
            div.append({"in": l, "c": cl, "rust": rl, "kind": kind, "flags": per_input_flags(i, c, cl)})
        s = summarize_inputs([d for d in div if d["kind"] == "value"])
        row = {"tag": t["tag"], "before_value_divergences": t["before_value"],
               "fresh": {"value": sum(1 for d in div if d["kind"] == "value"),
                         "crash": sum(1 for d in div if d["kind"] == "crash")}, "inputs": len(lines), **s,
               "divergent_inputs_detail": div}
        row["reproduced"] = row["fresh"]["value"] == t["before_value"]
        row["counted_after"] = s["clean_divergent_inputs"] > 0
        struct_rows.append(row)
        print(f"[{time.time()-t0:.0f}s] struct {t['tag']}: {row['fresh']} clean={s['clean_divergent_inputs']}", flush=True)

    summary = {
        "scalar": {"counted_before": len(scalar_rows),
                   "counted_after": sum(1 for r in scalar_rows if r["counted_after"]),
                   "reproduced": sum(1 for r in scalar_rows if r["reproduced"]),
                   "per_translation": {r["tag"]: {k: r[k] for k in ("fresh", "divergent_inputs", "ub_under_iso_c_sanitizers",
                                                                     "ub_even_with_fwrapv", "opt_level_dependent",
                                                                     "fwrapv_dependent_output", "char_signedness_dependent",
                                                                     "bitfield_signedness_dependent",
                                                                     "clean_divergent_inputs", "report_kinds")}
                                       for r in scalar_rows}},
        "struct_counted": {"counted_before": len(struct_rows),
                           "counted_after": sum(1 for r in struct_rows if r["counted_after"]),
                           "reproduced": sum(1 for r in struct_rows if r["reproduced"]),
                           "per_translation": {r["tag"]: {k: r[k] for k in ("fresh", "divergent_inputs", "ub_under_iso_c_sanitizers",
                                                                             "opt_level_dependent", "fwrapv_dependent_output",
                                                                             "char_signedness_dependent", "bitfield_signedness_dependent",
                                                                             "clean_divergent_inputs")}
                                               for r in struct_rows}},
        "struct_in_bounds_by_construction": {
            "functions": len(struct_all),
            "inputs_total": sum(r.get("inputs", 0) for r in struct_all),
            "inputs_with_sanitizer_report": sum(r.get("inputs_with_sanitizer_report", 0) for r in struct_all),
            "functions_with_any_report": sorted(r["fn"] for r in struct_all if r.get("inputs_with_sanitizer_report")),
            "inputs_opt_level_dependent": sum(r.get("inputs_opt_level_dependent", 0) for r in struct_all),
            "inputs_fwrapv_dependent": sum(r.get("inputs_fwrapv_dependent", 0) for r in struct_all),
            "inputs_char_signedness_dependent": sum(r.get("inputs_char_signedness_dependent", 0) for r in struct_all),
            "inputs_bitfield_signedness_dependent": sum(r.get("inputs_bitfield_signedness_dependent", 0) for r in struct_all)},
    }
    out = {
        "provenance": ou.provenance(__file__, {
            "rust_build": {"rustc": rinfo["rustc"], "image": rinfo["image"], "image_id": rinfo["image_id"],
                           "how": "systems/flourine/run.py build_rust / run_struct.py build_rust (rustc -O)"},
            "artifact_source_digests": {"bms/c": ou.tree_digest(os.path.join(fl_scalar.FL, "bms", "c")),
                                        "translations_only": ou.tree_digest(os.path.join(fl_scalar.FL, "translations_only"))},
            "c_builds": {k: [v[0]] + v[1] for k, v in CC_VARIANTS.items()},
            "c_driver": "generated by the harnesses' own build_c (p.c), compiled per variant",
            "compared_against": ["results/flourine_diff.json", "results/flourine_struct.json"],
            "wall_seconds": round(time.time() - t0),
            "sanitizer_positive_controls": ou.positive_controls(os.path.join(bdir, "controls")),
        }),
        "summary": summary,
        "scalar_verdicts": scalar_rows,
        "struct_counted_verdicts": struct_rows,
        "struct_all_functions_c_check": struct_all,
    }
    ou.dump(out_path, out)
    print(json.dumps(summary, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["rust", "c"], required=True)
    ap.add_argument("--build", default="/tmp/oub_flourine")
    ap.add_argument("--out", default=os.path.join(REPO, "results", "oracle_ub_flourine.json"))
    args = ap.parse_args()
    os.makedirs(args.build, exist_ok=True)
    if args.phase == "rust":
        phase_rust(args.build)
    else:
        phase_c(args.build, args.out, time.time())


if __name__ == "__main__":
    main()
