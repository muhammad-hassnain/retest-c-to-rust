"""Oracle-UB validation for RustMap bzip2 (systems/rustmap/check_bzip2.sh): is the original C bzip2
well-defined on every input behind the counted result (8 binary inputs 10 B..2 KB fail RustMap's own
check, a 1-byte input panics, the C round-trips all), and are the C tools of RustMap's own check
(bzip2recover, bzip2 -d) well-defined on the Rust's output?

Same 20 inputs as check_bzip2.sh (identical LCG generator; binary and printable text; sizes
1,10,20,64,100,128,512,1000,2048,5000). C builds of bzip2-1.0.8 (bzip2 + bzip2recover):
  ref        the shipped Makefile as is (gcc -O2 -g --coverage -pg), as check_bzip2.sh builds it
  o0, o1     -O0 / -O1 (optimization-level cross-check: output must equal ref)
  san_clang  clang-14 ASan+UBSan+float-cast-overflow -O0 (non-PIE), -fno-sanitize-recover=all
  san_gcc    gcc-11, same sanitizer set
  san_clang_nz  clang with NULL+0 pointer-overflow recoverable (see oracle_ub_common)
  uchar/schar  -O2 with -funsigned-char / -fsigned-char
  ubitfield    -O2 with -funsigned-bitfields
The Rust (cargo build, debug, as README 4.1.1) is built in phase "rust" (crust-syzygy, rustc
1.86.0, the toolchain of results/rustmap_bzip2_check.json); everything else runs in phase "c"
(crust-heldout, which has clang).

  bash harness/oracle_ub_run.sh rustmap_bzip2
"""
import argparse
import filecmp
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
ART = os.path.join(REPO, "artifacts", "rustmap")
CSRC = os.path.join(ART, "c-code", "bzip2-1.0.8")
RSRC = os.path.join(ART, "rust-code", "bzip2_rs_gpt")
BIG = "-D_FILE_OFFSET_BITS=64"

VARIANTS = {
    "ref": None,   # Makefile defaults
    "o0": {"CFLAGS": f"-Wall -Winline -O0 -g {BIG}"},
    "o1": {"CFLAGS": f"-Wall -Winline -O1 -g {BIG}"},
    "san_clang": {"CC": "clang", "CFLAGS": " ".join(ou.san_cflags("clang") + ["-w", BIG]),
                  "LDFLAGS": " ".join(ou.san_ldflags("clang"))},
    "san_gcc": {"CC": "gcc", "CFLAGS": " ".join(ou.san_cflags("gcc") + ["-w", BIG]),
                "LDFLAGS": " ".join(ou.san_ldflags("gcc"))},
    "san_clang_nz": {"CC": "clang", "CFLAGS": " ".join(ou.san_cflags("clang") + ou.NZ_RECOVER + ["-w", BIG]),
                     "LDFLAGS": " ".join(ou.san_ldflags("clang"))},
    "uchar": {"CFLAGS": f"-Wall -Winline -O2 -g {BIG} -funsigned-char"},
    "schar": {"CFLAGS": f"-Wall -Winline -O2 -g {BIG} -fsigned-char"},
    "ubitfield": {"CFLAGS": f"-Wall -Winline -O2 -g {BIG} -funsigned-bitfields"},
}
SANS = ("san_clang", "san_gcc", "san_clang_nz")
SIZES = [1, 10, 20, 64, 100, 128, 512, 1000, 2048, 5000]


def gen(kind, n, seed):
    """Byte-identical to the generator embedded in systems/rustmap/check_bzip2.sh."""
    seed = (seed * 2654435761 + 12345) & 0xffffffff
    b = bytearray()
    for _ in range(n):
        seed = (1103515245 * seed + 12345) & 0xffffffff
        v = (seed >> 16) & 0xff
        b.append(v if kind == "binary" else 33 + v % 94)
    return bytes(b)


def phase_rust(bdir):
    rs = os.path.join(bdir, "rs")
    ou.copy_sources(RSRC, rs)
    p = subprocess.run(["cargo", "build"], cwd=rs, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    binp = os.path.join(rs, "target", "debug", "bzip2_rs_gpt")
    info = {"ok": os.path.isfile(binp), "bin": binp, "rustc": ou.tool_version(["rustc", "--version"]),
            "cargo": ou.tool_version(["cargo", "--version"]), "build": "cargo build (debug), as README 4.1.1",
            "image": os.environ.get("OUB_IMAGE"), "image_id": os.environ.get("OUB_IMAGE_ID"),
            "log_tail": p.stdout[-600:]}
    ou.dump(os.path.join(bdir, "rust_build.json"), info)
    print(json.dumps(info, indent=1))
    if not info["ok"]:
        raise SystemExit(2)


def build_c(bdir):
    bins = {}
    for name, over in VARIANTS.items():
        d = os.path.join(bdir, f"c_{name}")
        ou.copy_sources(CSRC, d)
        args = ["make", "bzip2", "bzip2recover"] + [f"{k}={v}" for k, v in (over or {}).items()]
        p = subprocess.run(args, cwd=d, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if p.returncode != 0 or not os.path.isfile(os.path.join(d, "bzip2")):
            raise SystemExit(f"C build {name} failed:\n{p.stdout[-2000:]}")
        bins[name] = {"bzip2": os.path.join(d, "bzip2"), "bzip2recover": os.path.join(d, "bzip2recover"),
                      "make_overrides": over or "Makefile defaults (-O2 -g --coverage -pg)"}
    return bins


def run(argv, cwd, san, timeout=60, halt=True):
    if san:
        r = ou.run_san(argv, cwd=cwd, timeout=timeout, halt=halt)
    else:
        o, e, rc, to = ou.run_proc(argv, cwd=cwd, timeout=timeout)
        r = {"stdout": o, "stderr": e, "rc": rc, "timed_out": to, "report": None, "infra": False}
    return r


def phase_c(bdir, out_path, t0):
    rinfo = json.load(open(os.path.join(bdir, "rust_build.json")))
    R = rinfo["bin"]
    bins = build_c(bdir)
    before = {(r["kind"], r["size"]): r for r in json.load(open(os.path.join(REPO, "results", "rustmap_bzip2_check.json")))["rows"]}
    rows = []
    i = 0
    for kind in ("binary", "text"):
        for sz in SIZES:
            i += 1
            data = gen(kind, sz, i)
            cd = os.path.join(bdir, f"case{i}")
            if os.path.isdir(cd):
                shutil.rmtree(cd)
            os.makedirs(os.path.join(cd, "r"))
            with open(os.path.join(cd, "r", "data"), "wb") as f:
                f.write(data)
            rr = run([R, "--keep", "--force", "data"], os.path.join(cd, "r"), False, timeout=60)
            rust_rc = 124 if rr["timed_out"] else rr["rc"]
            rust_bz = os.path.join(cd, "r", "data.bz2")
            row = {"kind": kind, "size": sz, "rust_rc": rust_rc, "rust_wrote_bz2": os.path.isfile(rust_bz)}
            ref_c_bz = None
            for name, b in bins.items():
                san = name.startswith("san_")
                halt = not name.endswith("_nz")
                vd = os.path.join(cd, name)
                os.makedirs(os.path.join(vd, "c"))
                with open(os.path.join(vd, "c", "data"), "wb") as f:
                    f.write(data)
                steps = {}
                # 1) the C round trip
                steps["c_compress"] = run([b["bzip2"], "--keep", "--force", "data"], os.path.join(vd, "c"), san, halt=halt)
                cbz = os.path.join(vd, "c", "data.bz2")
                steps["c_decompress"] = run([b["bzip2"], "-dc", "data.bz2"], os.path.join(vd, "c"), san, halt=halt)
                c_ok = (os.path.isfile(cbz) and steps["c_decompress"]["rc"] == 0
                        and steps["c_decompress"]["stdout"] == data)
                cbytes = open(cbz, "rb").read() if os.path.isfile(cbz) else None
                if name == "ref":
                    ref_c_bz = cbytes
                rec = {"c_roundtrip_ok": c_ok, "c_bz2_same_as_ref": cbytes == ref_c_bz}
                # 2) RustMap's own check with THIS build of the C tools, on the Rust's output
                own = False
                if os.path.isfile(rust_bz):
                    rd = os.path.join(vd, "r")
                    os.makedirs(rd)
                    shutil.copy(rust_bz, os.path.join(rd, "data.bz2"))
                    steps["check_bzip2recover"] = run([b["bzip2recover"], "data.bz2"], rd, san, halt=halt)
                    rec00 = os.path.join(rd, "rec00001data.bz2")
                    if steps["check_bzip2recover"]["rc"] == 0 and os.path.isfile(rec00):
                        steps["check_bzip2_d"] = run([b["bzip2"], "-d", "rec00001data.bz2"], rd, san, halt=halt)
                        outp = os.path.join(rd, "rec00001data")
                        own = (steps["check_bzip2_d"]["rc"] == 0 and os.path.isfile(outp)
                               and open(outp, "rb").read() == data)
                        got = open(outp, "rb").read() if os.path.isfile(outp) else None
                        err = steps["check_bzip2_d"]["stderr"]
                        if own:
                            rec["check_outcome"] = "passes"
                        elif steps["check_bzip2_d"]["rc"] == 0 and got is not None:
                            rec["check_outcome"] = "exit0_but_output_differs"
                            rec["check_output_len"] = len(got)
                            rec["check_bytes_differing"] = sum(1 for a, b2 in zip(got, data) if a != b2) + abs(len(got) - len(data))
                            rec["check_input_bytes_ge_0x80"] = sum(1 for x in data if x >= 0x80)
                            diffpos = [k for k, (a, b2) in enumerate(zip(got, data)) if a != b2]
                            rec["check_differing_positions_are_exactly_input_bytes_ge_0x80"] = (
                                len(got) == len(data) and diffpos == [k for k, x in enumerate(data) if x >= 0x80])
                            rec["check_values_at_differing_positions"] = sorted({got[k] for k in diffpos})
                        elif b"Data integrity error" in err:
                            rec["check_outcome"] = "bzip2_data_integrity_error"
                        else:
                            rec["check_outcome"] = f"other(rc={steps['check_bzip2_d']['rc']})"
                        rec["check_bzip2_d_stderr"] = err.decode("utf-8", "replace").strip()[:200]
                        if name == "ref":
                            # bzip2 -tvvv prints the stored vs computed block CRC
                            t = run([b["bzip2"], "-tvvv", "rec00001data.bz2"], rd, False)
                            rec["check_tvvv"] = [ln for ln in t["stderr"].decode("utf-8", "replace").splitlines()
                                                 if "CRC" in ln or "crc" in ln or "{0x" in ln or "integrity" in ln][:6]
                    else:
                        rec["check_outcome"] = "bzip2recover_failed"
                    # plain `bzip2 -d` on the Rust's output (no recovery)
                    steps["plain_bzip2_dc_on_rust_output"] = run([b["bzip2"], "-dc", "data.bz2"], rd, san, halt=halt)
                    rec["check_bzip2_d_rc"] = steps.get("check_bzip2_d", {}).get("rc")
                    rec["plain_bzip2_d_rc"] = steps["plain_bzip2_dc_on_rust_output"]["rc"]
                rec["rust_passes_rustmap_own_check"] = own
                rec["san_reports"] = {k: v["report"] for k, v in steps.items() if v.get("report")}
                rec["san_reports_other_than_null_plus_zero"] = {
                    k: ou.other_than_null_zero(v.get("reports", [])) for k, v in steps.items()
                    if ou.other_than_null_zero(v.get("reports", []))}
                rec["san_infra"] = [k for k, v in steps.items() if v.get("infra")]
                rec["steps_timed_out"] = [k for k, v in steps.items() if v.get("timed_out")]
                rec["step_exits"] = {k: v["rc"] for k, v in steps.items()}
                row[name] = rec
            ref = row["ref"]
            b4 = before[(kind, sz)]
            row["before"] = b4
            row["reproduced"] = (ref["c_roundtrip_ok"] == b4["c_roundtrip_ok"] and rust_rc == b4["rust_rc"]
                                 and ref["rust_passes_rustmap_own_check"] == b4["rust_passes_rustmap_own_check"])
            ub = [f"{n}: {s}: {rep}" for n in SANS for s, rep in row[n]["san_reports"].items()]
            for n in ("o0", "o1"):
                if not (row[n]["c_roundtrip_ok"] == ref["c_roundtrip_ok"] and row[n]["c_bz2_same_as_ref"]
                        and row[n]["rust_passes_rustmap_own_check"] == ref["rust_passes_rustmap_own_check"]
                        and row[n].get("check_bzip2_d_rc") == ref.get("check_bzip2_d_rc")):
                    ub.append(f"opt-level: {n} differs from ref")
            row["c_ub_evidence"] = ub
            row["char_signedness_dependent"] = any(
                not (row[n]["c_roundtrip_ok"] == ref["c_roundtrip_ok"] and row[n]["c_bz2_same_as_ref"]
                     and row[n]["rust_passes_rustmap_own_check"] == ref["rust_passes_rustmap_own_check"]
                     and row[n].get("check_bzip2_d_rc") == ref.get("check_bzip2_d_rc"))
                for n in ("uchar", "schar"))
            row["bitfield_signedness_dependent"] = not (
                row["ubitfield"]["c_roundtrip_ok"] == ref["c_roundtrip_ok"] and row["ubitfield"]["c_bz2_same_as_ref"]
                and row["ubitfield"]["rust_passes_rustmap_own_check"] == ref["rust_passes_rustmap_own_check"]
                and row["ubitfield"].get("check_bzip2_d_rc") == ref.get("check_bzip2_d_rc"))
            # counted = the paper's S1 claims: binary 10..2048 fail the own check; 1-byte panics
            row["counted_before"] = ((kind == "binary" and 10 <= sz <= 2048 and not b4["rust_passes_rustmap_own_check"]
                                      and b4["rust_rc"] == 0)
                                     or (sz == 1 and b4["rust_rc"] == 101))
            # uniform rule (audit 8): a sanitizer or char-signedness run with a step that failed to
            # start or timed out, or whose round trip / .bz2 bytes disagree with ref -> not UB-clean
            unres = []
            for n in [x for x in SANS if not x.endswith("_nz")] + ["uchar", "schar"]:
                x = row[n]
                if x["san_infra"]:
                    unres.append(f"{n}: run failed (sanitizer runtime did not start) in {x['san_infra']}")
                elif x["steps_timed_out"]:
                    unres.append(f"{n}: timed out in {x['steps_timed_out']}")
                elif not x["san_reports"] and not (x["c_roundtrip_ok"] == ref["c_roundtrip_ok"] and x["c_bz2_same_as_ref"]):
                    unres.append(f"{n}: output differs from the reference build")
            row["ub_rule_not_clean"] = unres
            row["counted_after"] = (row["counted_before"] and not ub and not unres and not row["char_signedness_dependent"]
                                  and ref["c_roundtrip_ok"])
            rows.append(row)
            print(f"[{time.time()-t0:.0f}s] {kind} {sz}: ub={len(ub)} reproduced={row['reproduced']}", flush=True)
    counted_b = [r for r in rows if r["counted_before"]]
    summary = {
        "n_inputs": len(rows),
        "reproduced_rows": sum(r["reproduced"] for r in rows),
        "counted_before": {"binary_10B_2KB_fail_own_check": sum(1 for r in counted_b if r["kind"] == "binary" and r["size"] > 1),
                           "one_byte_panic": sum(1 for r in counted_b if r["size"] == 1)},
        "counted_after": {"binary_10B_2KB_fail_own_check": sum(1 for r in rows if r["counted_after"] and r["kind"] == "binary" and r["size"] > 1),
                          "one_byte_panic": sum(1 for r in rows if r["counted_after"] and r["size"] == 1)},
        "binary_10B_2KB_check_outcome": {
            k: sum(1 for r in rows if r["kind"] == "binary" and 10 <= r["size"] <= 2048 and r["ref"].get("check_outcome") == k)
            for k in sorted({r["ref"].get("check_outcome") for r in rows if r["kind"] == "binary" and 10 <= r["size"] <= 2048})},
        "c_roundtrip_ok_all_builds": {n: sum(1 for r in rows if r[n]["c_roundtrip_ok"]) for n in VARIANTS},
        "rows_with_any_sanitizer_report": sum(1 for r in rows if any(r[n]["san_reports"] for n in SANS)),
        "distinct_reports": sorted({f"{n}/{st}: {rep}" for r in rows for n in SANS
                                    for st, rep in r[n]["san_reports"].items()}),
        "rows_with_opt_level_difference": sum(1 for r in rows if any("opt-level" in u for u in r["c_ub_evidence"])),
        "rows_char_signedness_dependent": sum(1 for r in rows if r["char_signedness_dependent"]),
        "rows_bitfield_signedness_dependent": sum(1 for r in rows if r["bitfield_signedness_dependent"]),
        "san_infra_steps": sum(len(r[n]["san_infra"]) for r in rows for n in SANS),
    }
    out = {
        "provenance": ou.provenance(__file__, {
            "artifact_source_digests": {"bzip2-1.0.8": ou.tree_digest(CSRC), "bzip2_rs_gpt": ou.tree_digest(RSRC)},
            "rust_build": {k: v for k, v in rinfo.items() if k != "log_tail"},
            "c_builds": {k: v["make_overrides"] for k, v in bins.items()},
            "compared_against": "results/rustmap_bzip2_check.json",
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
    ap.add_argument("--build", default="/tmp/oub_bzip2")
    ap.add_argument("--out", default=os.path.join(REPO, "results", "oracle_ub_rustmap_bzip2.json"))
    args = ap.parse_args()
    os.makedirs(args.build, exist_ok=True)
    if args.phase == "rust":
        phase_rust(args.build)
    else:
        phase_c(args.build, args.out, time.time())


if __name__ == "__main__":
    main()
