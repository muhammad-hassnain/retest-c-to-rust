"""Oracle-UB check of the defects that results/defect_ledger.json (v2) marks UB-pending:

  RUSTASSURE-u8c-u8next_-1           malformed UTF-8 (systems/rustassure/u8c_e2e): the 13 exposing corpus inputs
  SYZYGY-zopfli-ZopfliDeflate-1      empty input, btype 0/1/2, C called directly (systems/syzygy/empty_deflate.sh)
  C2SR-head-5                        head -n -N on the 3 own-input invocations of results/heldout_c2saferrust.json
  RUSTMAP-rosetta-number_names-1, RUSTMAP-rosetta-numerical_integration-1

Same rule and tooling as the other oracle_ub_* scripts (harness/oracle_ub_common.py): the C must give no
report under clang-14 and gcc-11 ASan+UBSan+float-cast-overflow (-O0, no recovery; clang non-PIE; plus the
clang build with only NULL+0 recoverable), must give the same output at -O0, -O1 and -O2, and the
divergence must not depend on char signedness (-funsigned-char / -fsigned-char) or plain bit-field
signedness (-funsigned-bitfields). An exposing input is "clean" iff all of that holds AND the Rust still
differs from the C under every one of those non-sanitizer builds. Audit 8 (uniform rule, as every other
script): the C's own output must also be identical under -funsigned-char and -fsigned-char, and each
sanitizer run must finish (no start-up failure, no timeout) with the reference's stdout and exit code.

Phase "rust" (crust-syzygy, rustc 1.86.0): u8c SUT (extract_sut.py + rustc -O, as u8c_e2e/run.py), the
Syzygy crate with the empty_deflate.sh harness main, the two Rosetta translations (rustc -A warnings).
Phase "c" (crust-heldout): all C builds and runs. head reuses the C builds and the rust_WIP SUT built by
harness/oracle_ub_c2saferrust.py in /oub/c2sr/head (same image; their sha256 is recorded).
Inputs: Syzygy from a clean copy of the Mac artifact (--syzygy, digest recorded and compared with the
artifacts/ tree); head-5 inputs from the capture blobs of harness/heldout_c2saferrust.py (--blobs; each
file's sha256 is checked against its name and against results/heldout_c2saferrust.json).

  bash harness/oracle_ub_run.sh pending
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import oracle_ub_common as ou  # noqa: E402
from common import controlled_env, normalize_stderr  # noqa: E402

REPO = ou.REPO
sys.path.insert(0, os.path.join(REPO, "systems", "rustassure", "u8c_e2e"))
sys.path.insert(0, os.path.join(REPO, "systems", "rustmap"))
import run_rosetta as rr  # noqa: E402

U8C = os.path.join(REPO, "artifacts", "rustassure", "src", "python", "inputs-complex", "u8c")
U8E = os.path.join(REPO, "systems", "rustassure", "u8c_e2e")
SANS = ("san_clang", "san_gcc", "san_clang_nz")
NONSAN = ("o0", "o1", "o2", "uchar", "schar", "ubitfield")
ROSETTA = ["number_names", "numerical_integration"]


def cvariants(base_opt):
    """name -> (cc, flags). `ref` is the harness's own oracle flags (base_opt)."""
    return {
        "ref": ("gcc", ["-w"] + base_opt),
        "o0": ("gcc", ["-w", "-O0"]), "o1": ("gcc", ["-w", "-O1"]), "o2": ("gcc", ["-w", "-O2"]),
        "san_clang": ("clang", ["-w"] + ou.san_cflags("clang") + ou.san_ldflags("clang")),
        "san_gcc": ("gcc", ["-w"] + ou.san_cflags("gcc") + ou.san_ldflags("gcc")),
        "san_clang_nz": ("clang", ["-w"] + ou.san_cflags("clang") + ou.NZ_RECOVER + ou.san_ldflags("clang")),
        "uchar": ("gcc", ["-w"] + base_opt + ["-funsigned-char"]),
        "schar": ("gcc", ["-w"] + base_opt + ["-fsigned-char"]),
        "ubitfield": ("gcc", ["-w"] + base_opt + ["-funsigned-bitfields"]),
    }


def build_set(srcs, out_dir, base_opt, extra=(), libs=("-lm",)):
    os.makedirs(out_dir, exist_ok=True)
    bins = {}
    for name, (cc, flags) in cvariants(base_opt).items():
        b = os.path.join(out_dir, name)
        p = subprocess.run([cc] + flags + list(extra) + list(srcs) + ["-o", b] + list(libs),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if p.returncode != 0 or not os.path.isfile(b):
            raise SystemExit(f"build {name} failed:\n{p.stdout[-1500:]}")
        bins[name] = b
    return bins


def run_any(name, argv, **kw):
    if name.startswith("san_"):
        return ou.run_san(argv, halt=not name.endswith("_nz"), **kw)
    o, e, rc, to = ou.run_proc(argv, **kw)
    return {"stdout": o, "stderr": e, "rc": rc, "timed_out": to, "report": None, "reports": [], "infra": False}


def judge(res, rust_out, same_fn=None):
    """res: variant -> run dict (must include 'ref'). rust_out: comparable Rust output.
    same_fn(a, b) compares two run dicts' primary output. Returns per-input verdict dict."""
    same = same_fn or (lambda a, b: a["stdout"] == b["stdout"] and a["rc"] == b["rc"])
    ref = res["ref"]
    v = {"diverges_under_ref": not same(ref, rust_out)}
    v["sanitizer_reports"] = {n: res[n]["reports"] for n in SANS if res[n]["reports"]}
    v["sanitizer_reports_other_than_null_plus_zero"] = {
        n: ou.other_than_null_zero(res[n]["reports"]) for n in SANS if ou.other_than_null_zero(res[n]["reports"])}
    v["san_infra"] = [n for n in SANS if res[n]["infra"]]
    v["san_output_same_as_ref_when_clean"] = {n: same(res[n], ref) for n in SANS if not res[n]["report"]}
    v["build_output_same_as_ref"] = {n: same(res[n], ref) for n in NONSAN}
    v["diverges_under"] = {n: not same(res[n], rust_out) for n in NONSAN}
    v["opt_level_agree"] = all(v["build_output_same_as_ref"][n] for n in ("o0", "o1", "o2"))
    v["char_signedness_dependent_C"] = not (v["build_output_same_as_ref"]["uchar"] and v["build_output_same_as_ref"]["schar"])
    v["bitfield_signedness_dependent_C"] = not v["build_output_same_as_ref"]["ubitfield"]
    v["sanitizer_clean"] = not any(res[n]["report"] for n in SANS) and not v["san_infra"]
    # audit 8 uniform rule: a sanitizer run that timed out or printed something else than the
    # reference, or a char-signedness build that disagrees with it, leaves the input NOT UB-clean
    v["ub_rule_not_clean"] = [x for n in SANS if not n.endswith("_nz") and not res[n]["report"]
                              for x in ou.not_clean_reasons(n, res[n], same(res[n], ref))]
    v["ub_rule_not_clean"] += [f"{n}: output differs from the reference build (char signedness)"
                               for n in ("uchar", "schar") if not v["build_output_same_as_ref"][n]]
    v["clean"] = (v["diverges_under_ref"] and v["sanitizer_clean"] and v["opt_level_agree"]
                  and not v["ub_rule_not_clean"] and all(v["diverges_under"].values()))
    v["clean_except_null_plus_zero"] = (v["diverges_under_ref"] and not v["sanitizer_reports_other_than_null_plus_zero"]
                                        and not v["san_infra"] and v["opt_level_agree"] and not v["ub_rule_not_clean"]
                                        and all(v["diverges_under"].values()))
    return v


def sha_file(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# ===================================================================== phase rust
def phase_rust(bdir, syz):
    os.makedirs(bdir, exist_ok=True)
    info = {"rustc": ou.tool_version(["rustc", "--version"]), "image": os.environ.get("OUB_IMAGE"),
            "image_id": os.environ.get("OUB_IMAGE_ID")}
    # u8c: exactly u8c_e2e/run.py's SUT build, into scratch
    gen = os.path.join(bdir, "u8c_sut.rs")
    subprocess.run([sys.executable, os.path.join(U8E, "extract_sut.py"), gen], check=True)
    b = os.path.join(bdir, "u8c_sut")
    p = subprocess.run(["rustc", "-O", gen, "-o", b], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    info["u8c"] = {"ok": os.path.isfile(b), "bin": b, "cmd": "extract_sut.py + rustc -O", "log": p.stdout[-300:]}
    # Syzygy: the empty_deflate.sh Rust side, verbatim
    rs = os.path.join(bdir, "syz_rs")
    ou.copy_sources(os.path.join(syz, "rust_code"), rs)
    mainrs = os.path.join(rs, "src", "main.rs")
    src = open(mainrs).read()
    src = re.sub(r"(?m)^fn main\(\) \{", "fn syzygy_driver_main() {", src)
    src += '''
fn main() {
    for bt in 0..=2 {
        let o = ZopfliOptions { verbose: false, verbose_more: false, numiterations: 15,
                                blocksplitting: 1, blocksplittinglast: 0, blocksplittingmax: 15 };
        let mut out: Vec<u8> = Vec::new(); let mut outsize: usize = 0; let mut bp: u8 = 0;
        zopfli_deflate(&o, bt, 1, &[], 0, &mut bp, &mut out, &mut outsize);
        let hex: String = out[..outsize.min(out.len())].iter().map(|b| format!("{:02x}", b)).collect();
        println!("{} {}", bt, hex);
    }
}
'''
    open(mainrs, "w").write(src)
    p = subprocess.run(["cargo", "build", "-q"], cwd=rs, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    pkg = re.search(r'(?m)^name\s*=\s*"([^"]+)"', open(os.path.join(rs, "Cargo.toml")).read()).group(1)
    sb = os.path.join(rs, "target", "debug", pkg)
    info["syzygy"] = {"ok": os.path.isfile(sb), "bin": sb, "cmd": "empty_deflate.sh Rust side (cargo build, debug)",
                      "log": p.stdout[-300:]}
    # Rosetta
    info["rosetta"] = {}
    for t in ROSETTA:
        rb = os.path.join(bdir, f"rosetta_{t}_r")
        p = subprocess.run(["rustc", "-A", "warnings", os.path.join(rr.RDIR, t + "-c.rs"), "-o", rb],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        info["rosetta"][t] = {"ok": os.path.isfile(rb), "bin": rb, "log": p.stdout[-300:]}
    ou.dump(os.path.join(bdir, "rust_build.json"), info)
    print(json.dumps(info, indent=1))


# ===================================================================== phase c
def do_u8c(bdir, rinfo):
    from gen_corpus import corpus
    d = os.path.join(bdir, "u8c_c")
    csrc = os.path.join(d, "src")
    ou.copy_sources(U8C, csrc)
    shutil.copy(os.path.join(U8E, "u8c_oracle.c"), os.path.join(d, "u8c_oracle.c"))
    bins = build_set([os.path.join(csrc, "u8c.c"), os.path.join(d, "u8c_oracle.c")], os.path.join(d, "bin"),
                     ["-O2"], extra=["-I", csrc], libs=())
    sut = rinfo["u8c"]["bin"]
    before = json.load(open(os.path.join(U8E, "results.json")))
    exposing = before["divergent_inputs"]
    rows = []
    for name, data in corpus():
        res = {n: run_any(n, [b], stdin=data, timeout=60) for n, b in bins.items()}
        o, e, rc, to = ou.run_proc([sut], data, timeout=20)
        rust = {"stdout": o, "rc": rc}
        v = judge(res, rust)
        v.update({"input": name, "hex": data.hex()[:80], "exposing_in_ledger": name in exposing,
                  "c_ref_first_lines": res["ref"]["stdout"].decode().splitlines()[:2],
                  "c_uchar_first_lines": res["uchar"]["stdout"].decode().splitlines()[:2],
                  "rust_first_lines": o.decode().splitlines()[:2]})
        rows.append(v)
    ex = [r for r in rows if r["exposing_in_ledger"]]
    return {
        "exposing_inputs": exposing,
        "summary": {"corpus_inputs": len(rows),
                    "diverge_under_ref_x86_signed_char": sum(r["diverges_under_ref"] for r in rows),
                    "diverge_under_-funsigned-char": sum(r["diverges_under"]["uchar"] for r in rows),
                    "exposing_checked": len(ex),
                    "exposing_sanitizer_clean": sum(r["sanitizer_clean"] for r in ex),
                    "exposing_opt_level_agree": sum(r["opt_level_agree"] for r in ex),
                    "exposing_C_output_char_signedness_dependent": sum(r["char_signedness_dependent_C"] for r in ex),
                    "exposing_divergent_under_both_char_signedness": sum(
                        r["diverges_under"]["uchar"] and r["diverges_under"]["schar"] for r in ex),
                    "exposing_clean": sum(r["clean"] for r in ex),
                    "non_exposing_that_diverge_under_some_build": sorted(
                        r["input"] for r in rows if not r["exposing_in_ledger"]
                        and (r["diverges_under_ref"] or any(r["diverges_under"].values())))},
        "per_input": rows,
        "builds": {k: v for k, v in cvariants(["-O2"]).items()},
    }


def do_syzygy(bdir, rinfo, syz):
    d = os.path.join(bdir, "syz_c")
    os.makedirs(d, exist_ok=True)
    h = os.path.join(d, "h.c")
    sh = open(os.path.join(REPO, "systems", "syzygy", "empty_deflate.sh")).read()
    open(h, "w").write(re.search(r"cat > \"\$W/h.c\" <<'EOF'\n(.*?)\nEOF", sh, re.S).group(1) + "\n")
    # as empty_deflate.sh: zopfli.c compiled with its driver main renamed away, h.c linked to it
    bins = {}
    for name, (cc, flags) in cvariants(["-O0", "-g"]).items():
        zo = os.path.join(d, f"z_{name}.o")
        p1 = subprocess.run([cc] + flags + ["-c", "-I",
                            os.path.join(syz, "c_code"), "-Dmain=syzygy_driver_main",
                            os.path.join(syz, "c_code", "zopfli.c"), "-o", zo],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        b = os.path.join(d, f"c_h_{name}")
        p2 = subprocess.run([cc] + flags + ["-I", os.path.join(syz, "c_code"), h, zo, "-o", b, "-lm"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if p1.returncode or p2.returncode:
            raise SystemExit(f"syzygy build {name} failed:\n{p1.stdout[-800:]}\n{p2.stdout[-800:]}")
        bins[name] = b
    res = {n: run_any(n, [b], timeout=120) for n, b in bins.items()}
    o, e, rc, to = ou.run_proc([rinfo["syzygy"]["bin"]], timeout=120)

    def parse(s):
        out = {}
        for line in s.decode().strip().splitlines():
            parts = line.split()
            out[int(parts[0])] = bytes.fromhex(parts[1]) if len(parts) > 1 else b""
        return out

    def inflate_ok(bts):
        try:
            z = zlib.decompressobj(-15)
            r = z.decompress(bts)
            return z.eof and r == b""
        except zlib.error:
            return False
    rust = parse(o)
    rows = []
    for bt in (0, 1, 2):
        per = {}
        for n, r in res.items():
            p_ = parse(r["stdout"]) if r["rc"] == 0 else {}
            per[n] = {"stdout": p_.get(bt), "rc": r["rc"], "report": r["report"], "reports": r["reports"],
                      "infra": r["infra"]}
        v = judge(per, {"stdout": rust.get(bt), "rc": 0})
        v.update({"btype": bt, "c_hex": (per["ref"]["stdout"] or b"").hex(), "rust_hex": (rust.get(bt) or b"").hex(),
                  "c_inflate_ok": inflate_ok(per["ref"]["stdout"] or b""), "rust_inflate_ok": inflate_ok(rust.get(bt) or b"")})
        rows.append(v)
    return {"per_input": rows, "rust_exit": rc,
            "summary": {"exposing_checked": 3, "clean": sum(r["clean"] for r in rows),
                        "c_valid_deflate": sum(r["c_inflate_ok"] for r in rows),
                        "rust_rejected_by_zlib": sum(not r["rust_inflate_ok"] for r in rows)}}


def do_head5(bdir, blobs):
    hb = os.path.join(bdir, "..", "c2sr", "head")
    hb = os.path.normpath(hb)
    variants = ["ref", "o1", "o2", "san_clang", "san_gcc", "san_clang_nz", "uchar", "schar", "ubitfield"]
    bins = {v: os.path.join(hb, "c_bin", v, "head") for v in variants}
    sut = os.path.join(hb, "rust_WIP", "target", "debug", "head")
    held = json.load(open(os.path.join(REPO, "results", "heldout_c2saferrust.json")))["per_test"]
    # exactly the own inputs the ledger lists for C2SR-head-5 ("<test> <argv>: C .. B, Rust .. B")
    led = next(r for r in json.load(open(os.path.join(REPO, "results", "defect_ledger.json")))["rows"]
               if r["defect_id"] == "C2SR-head-5")
    want = [x.split(":")[0] for x in led["exposure_breakdown"]["own_inputs"]]
    invs = []
    for w in want:
        test, argv = w.split(" ", 1)
        hits = [(i, inv) for i, inv in enumerate(held[test]["invocation_list"]) if " ".join(inv["argv"]) == argv]
        assert len(hits) == 1, (w, len(hits))
        invs.append((test, hits[0][0], hits[0][1]))
    rows = []
    for test, i, inv in invs:
        with tempfile.TemporaryDirectory() as wd:
            for a, info in inv.get("files", {}).items():
                p = os.path.join(blobs, info["sha256"])
                assert sha_file(p) == info["sha256"]
                shutil.copy(p, os.path.join(wd, a))
            st = inv.get("stdin")
            env = controlled_env()
            env.update(inv.get("env", {}))
            stdin_desc = None
            if st and st.get("kind") == "reg":
                sp = os.path.join(blobs, st["sha256"])
                assert sha_file(sp) == st["sha256"]
                stdin_desc = f"regular file {st['size']} B at offset {st.get('offset', 0)}"
            elif st and st.get("kind") == "pipe":
                sp = os.path.join(blobs, st["kept_sha256"])
                assert sha_file(sp) == st["kept_sha256"]
                data = open(sp, "rb").read()
                stdin_desc = f"pipe, {len(data)} B"

            def go(name, binp):
                argv = [binp] + inv["argv"]
                if st and st.get("kind") == "reg":
                    with open(sp, "rb") as f:
                        f.seek(st.get("offset", 0))
                        if name.startswith("san_"):
                            e2 = dict(env); e2.update(ou.SAN_ENV)
                            if name.endswith("_nz"):
                                e2["UBSAN_OPTIONS"] = e2["UBSAN_OPTIONS"].replace("halt_on_error=1", "halt_on_error=0")
                            p = subprocess.run(argv, stdin=f, cwd=wd, env=e2, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
                            return {"stdout": p.stdout, "stderr": p.stderr, "rc": p.returncode, "timed_out": False,
                                    "report": ou.san_report(p.stderr), "reports": ou.san_reports_all(p.stderr),
                                    "infra": ou.is_startup_infra(p.returncode, p.stderr)}
                        p = subprocess.run(argv, stdin=f, cwd=wd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
                        return {"stdout": p.stdout, "stderr": p.stderr, "rc": p.returncode, "timed_out": False,
                                "report": None, "reports": [], "infra": False}
                stdin = data if (st and st.get("kind") == "pipe") else b""
                return run_any(name, argv, stdin=stdin, cwd=wd, env=env, timeout=300)
            res = {n: go(n, b) for n, b in bins.items()}
            res["o0"] = res["ref"]          # the shipped Makefile build is -O0 (gcc -g)
            rust = go("sut", sut)
        v = judge(res, rust)
        v.update({"test": test, "invocation": i, "argv": inv["argv"], "stdin": stdin_desc,
                  "files": {a: f"{x['size']} B sha256 {x['sha256'][:16]}" for a, x in inv.get("files", {}).items()},
                  "c_stdout_len": len(res["ref"]["stdout"]), "rust_stdout_len": len(rust["stdout"]),
                  "recorded_c_len": inv["replay"]["oracle_stdout_len"], "recorded_rust_len": inv["replay"]["sut_stdout_len"]})
        v["reproduced"] = (v["c_stdout_len"] == v["recorded_c_len"] and v["rust_stdout_len"] == v["recorded_rust_len"])
        rows.append(v)
    return {"per_input": rows, "binaries_sha256": {k: sha_file(b)[:16] for k, b in list(bins.items()) + [("sut", sut)]},
            "binaries_from": "harness/oracle_ub_c2saferrust.py build dir /oub/c2sr/head (crust-heldout)",
            "summary": {"exposing_checked": len(rows), "clean": sum(r["clean"] for r in rows),
                        "reproduced": sum(r["reproduced"] for r in rows)}}


def do_rosetta(bdir, rinfo):
    out = {}
    for t in ROSETTA:
        cmain = os.path.join(rr.CDIR, t, "main.c")
        bins = build_set([cmain], os.path.join(bdir, f"rosetta_{t}"), [])
        res = {n: run_any(n, [b], stdin=b"", timeout=60) for n, b in bins.items()}
        o, e, rc, to = ou.run_proc([rinfo["rosetta"][t]["bin"]], b"", timeout=10)
        rust = {"stdout": o, "rc": rc}
        v = judge(res, rust)
        again = ou.run_proc([bins["ref"]], b"", timeout=10)
        v["ref_repeat_identical"] = again[0] == res["ref"]["stdout"] and again[2] == res["ref"]["rc"]
        v["clean"] = v["clean"] and v["ref_repeat_identical"]
        v["run_rosetta_class"] = rr.classify(t, res["ref"]["stdout"], res["ref"]["rc"], o, rc,
                                             res["ref"]["timed_out"], to)
        v.update({"c_head": res["ref"]["stdout"][:200].decode("latin1"), "rust_head": o[:200].decode("latin1"),
                  "c_exit": res["ref"]["rc"], "rust_exit": rc})
        out[t] = {"per_input": [v], "summary": {"exposing_checked": 1, "clean": int(v["clean"])}}
    return out


def ub_rule(summary_clean, of, evidence, extra_status=None):
    st = "clean" if summary_clean > 0 else (extra_status or "excluded_original_c_ub")
    return {"status": st, "clean_inputs": summary_clean, "of": of, "evidence": evidence}


def phase_c(bdir, out_path, syz, blobs):
    t0 = time.time()
    rinfo = json.load(open(os.path.join(bdir, "rust_build.json")))
    u8 = do_u8c(bdir, rinfo)
    print(f"[{time.time()-t0:.0f}s] u8c {u8['summary']}", flush=True)
    sy = do_syzygy(bdir, rinfo, syz)
    print(f"[{time.time()-t0:.0f}s] syzygy {sy['summary']}", flush=True)
    h5 = do_head5(bdir, blobs)
    print(f"[{time.time()-t0:.0f}s] head-5 {h5['summary']}", flush=True)
    ro = do_rosetta(bdir, rinfo)
    print(f"[{time.time()-t0:.0f}s] rosetta {[(k, v['summary']) for k, v in ro.items()]}", flush=True)
    E = "results/oracle_ub_pending.json"
    us = u8["summary"]
    defects = {
        "RUSTASSURE-u8c-u8next_-1": {
            "ub_rule": ub_rule(us["exposing_clean"], us["exposing_checked"],
                               f"{E}: {us['exposing_clean']} of {us['exposing_checked']} exposing inputs clean (clang+gcc ASan/UBSan, "
                               f"-O0/-O1/-O2 agree, Rust differs under -fsigned-char and -funsigned-char); the C's printed value "
                               f"for an invalid lead byte depends on char signedness on {us['exposing_C_output_char_signedness_dependent']} of them"),
            **u8},
        "SYZYGY-zopfli-ZopfliDeflate-1": {
            "ub_rule": ub_rule(sy["summary"]["clean"], 3, f"{E}: {sy['summary']['clean']} of 3 btypes clean (empty input)"), **sy},
        "C2SR-head-5": {
            "ub_rule": ub_rule(h5["summary"]["clean"], h5["summary"]["exposing_checked"],
                               f"{E}: {h5['summary']['clean']} of {h5['summary']['exposing_checked']} own-input invocations clean"), **h5},
    }
    for t, v in ro.items():
        defects[f"RUSTMAP-rosetta-{t}-1"] = {"ub_rule": ub_rule(v["summary"]["clean"], 1, f"{E}: C clean, deterministic, -O0/-O1/-O2 and char/bit-field toggles agree" if v["summary"]["clean"] else f"{E}: see per_input"), **v}
    out = {
        "provenance": ou.provenance(__file__, {
            "rust_build": {k: rinfo[k] for k in ("rustc", "image", "image_id")},
            "artifact_source_digests": {
                "u8c": ou.tree_digest(U8C), "u8c_link_main.rs": ou.tree_digest(os.path.join(REPO, "artifacts", "rustassure", "src", "Link", "u8c_link", "src", "main.rs")),
                "syzygy_clean_copy_c_code": ou.tree_digest(os.path.join(syz, "c_code")),
                "syzygy_clean_copy_rust_code": ou.tree_digest(os.path.join(syz, "rust_code")),
                "syzygy_artifacts_tree_c_code": ou.tree_digest(os.path.join(REPO, "artifacts", "syzygy_zopfli", "c_code")),
                "syzygy_artifacts_tree_rust_code": ou.tree_digest(os.path.join(REPO, "artifacts", "syzygy_zopfli", "rust_code")),
                **{f"rosetta_{t}": {"c": ou.tree_digest(os.path.join(rr.CDIR, t, "main.c")),
                                    "rust": ou.tree_digest(os.path.join(rr.RDIR, t + "-c.rs"))} for t in ROSETTA}},
            "syzygy_source": "clean rsync of the Mac artifacts/syzygy_zopfli (target/ excluded), digest above",
            "head5_inputs": "capture blobs of harness/heldout_c2saferrust.py; each checked against its sha256 and the invocation record",
            "rule": "clean input = diverges under the harness oracle AND no clang/gcc ASan+UBSan report AND -O0/-O1/-O2 "
                    "outputs equal AND the Rust differs from the C under -O0/-O1/-O2/-funsigned-char/-fsigned-char/-funsigned-bitfields builds",
            "wall_seconds": round(time.time() - t0),
            "sanitizer_positive_controls": ou.positive_controls(os.path.join(bdir, "controls")),
        }),
        "builder_note": "Each defects[<defect_id>].ub_rule has the ledger's ub_rule shape {status, clean_inputs, of, evidence}.",
        "defects": defects,
    }

    def clean_bytes(o):
        if isinstance(o, dict):
            return {k: clean_bytes(v) for k, v in o.items()}
        if isinstance(o, list):
            return [clean_bytes(x) for x in o]
        if isinstance(o, (bytes, bytearray)):
            return o.hex()
        if isinstance(o, tuple):
            return [clean_bytes(x) for x in o]
        return o
    ou.dump(out_path, clean_bytes(out))
    print(json.dumps({k: v["ub_rule"] for k, v in defects.items()}, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["rust", "c"], required=True)
    ap.add_argument("--build", default="/tmp/oub_pending")
    ap.add_argument("--syzygy", default="/oub/syzygy_src")
    ap.add_argument("--blobs", default="/oub/head5_blobs")
    ap.add_argument("--out", default=os.path.join(REPO, "results", "oracle_ub_pending.json"))
    args = ap.parse_args()
    if args.phase == "rust":
        phase_rust(args.build, args.syzygy)
    else:
        phase_c(args.build, args.out, args.syzygy, args.blobs)


if __name__ == "__main__":
    main()
