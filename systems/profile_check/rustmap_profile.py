"""RustMap: re-run the counted crash / overflow defects under the OTHER profile.

Rosetta programs: RustMap's own runner and our harness compile with plain `rustc file` = DEBUG
(overflow checks ON): artifacts/rustmap/c-code/rosseta-125/125-rosetta-rs.sh:16 (as cited in
systems/rustmap/run_rosetta.py:96, which we mirror). This script rebuilds the same files with
`rustc -O` (release, overflow checks off) and compares both against the same gcc build of the C
(plain `gcc main.c`, run_rosetta.py:88), using run_rosetta.classify (byte-exact, then the same
benign-format folds).

bzip2 (BZ-3 crash): RustMap grades with a debug `cargo build` (README 4.1.1; our
systems/rustmap/check_bzip2.sh:16-17, target/debug). Here the crate is also built `--release`
(NB: the crate's own Cargo.toml sets `[profile.release] panic = "abort"`) in a scratch copy and
run on the two 1-byte inputs the ledger names (check_bzip2.sh generator, seeds 1 and 11).
In the scratch copy the unused dependencies rustix/filetime/tempfile/rand are dropped (never
referenced in src/, and not in the offline vendor set); no source file is changed.

  docker run --rm --network none --cpus 2 -v $W:/w -v $REPO:/repo:ro -v $OUT:/out \
     -e RUSTUP_TOOLCHAIN=1.86.0 crust-heldout-plus186 python3 /repo/systems/profile_check/rustmap_profile.py /out/rustmap.json
"""
import json, os, re, shutil, subprocess, sys, tempfile

REPO = os.environ.get("PC_REPO", "/repo")
ART = os.environ.get("PC_ART", "/art/rustmap")
sys.path.insert(0, os.path.join(REPO, "systems", "rustmap"))
import run_rosetta as RR  # noqa: E402
RR.RM = ART
RR.CDIR = os.path.join(ART, "c-code", "rosseta-125")
RR.RDIR = os.path.join(ART, "rust-code", "rosetta_code_gpt", "125-rosetta-code-gpt")

TASKS = {"matrix_chain_multiplication": "RUSTMAP-rosetta-matrix_chain_multiplication-1 (crash, overflow)",
         "hailstone_sequence": "RUSTMAP-rosetta-hailstone_sequence-1 (crash)",
         "box_the_compass": "RUSTMAP-rosetta-box_the_compass-1 (S1)",
         "roman_numerals_decode": "RUSTMAP-rosetta-roman_numerals_decode-1 (S1)"}


def sh(argv, cwd=None, env=None, timeout=900):
    p = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    return p.returncode, p.stdout.decode("latin1")


def rosetta():
    out = {}
    for t, label in TASKS.items():
        cm = os.path.join(RR.CDIR, t, "main.c"); rf = os.path.join(RR.RDIR, t + "-c.rs")
        with tempfile.TemporaryDirectory() as d:
            cb = RR.build_c(cm, d)
            bins = {}
            rd = os.path.join(d, "dbg"); os.makedirs(rd)
            bins["debug"] = RR.build_rust(rf, rd)                       # plain rustc (as shipped runner)
            rr = os.path.join(d, "rel"); os.makedirs(rr)
            b = os.path.join(rr, "r_bin")
            r = subprocess.run(["rustc", "-O", "-A", "warnings", rf, "-o", b], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            bins["release"] = b if r.returncode == 0 else None
            co, cr, cto = RR.run(cb)
            row = {"label": label, "c_exit": cr, "c_stdout_head": co[:300].decode("latin1")}
            for prof, bp in bins.items():
                if not bp:
                    row[prof] = {"error": "build failed"}; continue
                ro, rrc, rto = RR.run(bp)
                cls = RR.classify(t, co, cr, ro, rrc, cto, rto)
                kind = "agrees" if cls.startswith("agree") else ("crash (panic/signal)" if rrc != 0 and rrc != cr else "wrong answer")
                row[prof] = {"classify": cls, "outcome": kind, "exit": rrc, "stdout_head": ro[:300].decode("latin1")}
            out[t] = row
    return out


def bzip2():
    W = tempfile.mkdtemp(prefix="bz")
    src = os.path.join(ART, "rust-code", "bzip2_rs_gpt")
    rs = os.path.join(W, "rs"); shutil.copytree(src, rs, ignore=shutil.ignore_patterns("target"))
    toml = open(os.path.join(rs, "Cargo.toml")).read()
    toml = "".join(l for l in toml.splitlines(True) if not re.match(r"\s*(rustix|filetime|tempfile|rand)\s*=", l))
    open(os.path.join(rs, "Cargo.toml"), "w").write(toml)
    env = {**os.environ, "CARGO_HOME": "/cargo_home_bz", "CARGO_BUILD_JOBS": "2", "CARGO_TARGET_DIR": os.path.join(W, "tgt")}
    res = {"deviation": "Cargo.toml in the scratch copy lacks unused deps rustix/filetime/tempfile/rand (offline)", "builds": {}}
    bins = {}
    for prof in ("debug", "release"):
        rc, log = sh(["cargo", "build", "--offline"] + (["--release"] if prof == "release" else []), cwd=rs, env=env)
        b = os.path.join(W, "tgt", prof, "bzip2_rs_gpt")
        res["builds"][prof] = {"rc": rc, "log_tail": log[-400:]}
        bins[prof] = b if os.path.isfile(b) else None
    for kind, seed in (("binary", 1), ("text", 11)):
        s = (seed * 2654435761 + 12345) & 0xffffffff
        v = 0; bb = bytearray()
        for _ in range(1):
            s = (1103515245 * s + 12345) & 0xffffffff
            v = (s >> 16) & 0xff
            bb.append(v if kind == "binary" else 33 + v % 94)
        row = {"input_byte": bb[0]}
        for prof, bp in bins.items():
            if not bp:
                row[prof] = {"error": "build failed"}; continue
            d = tempfile.mkdtemp(dir=W); open(os.path.join(d, "data"), "wb").write(bytes(bb))
            try:
                p = subprocess.run([bp, "--keep", "--force", "data"], cwd=d, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
                row[prof] = {"rust_rc": p.returncode, "stderr_tail": p.stderr.decode("latin1")[-200:],
                             "outcome": "crash" if p.returncode != 0 else "no crash"}
            except subprocess.TimeoutExpired:
                row[prof] = {"rust_rc": None, "outcome": "hang (timeout 20 s)"}
            # does the compressor's output round-trip through the C bzip2 -d ?
            if os.path.isfile(os.path.join(d, "data.bz2")):
                q = subprocess.run(["bzip2", "-dc", os.path.join(d, "data.bz2")], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                row[prof]["bz2_roundtrip_ok"] = (q.returncode == 0 and q.stdout == bytes(bb))
        # C reference
        res.setdefault("rows", {})[f"{kind}_1B_seed{seed}"] = row
    return res


def main():
    out = {"rosetta": rosetta()}
    try:
        out["bzip2"] = bzip2()
    except Exception as e:  # keep rosetta result
        out["bzip2"] = {"error": repr(e)}
    json.dump(out, open(sys.argv[1], "w"), indent=1)
    for t, r in out["rosetta"].items():
        print(t, r.get("debug", {}).get("outcome"), r.get("release", {}).get("outcome"))
    print(json.dumps(out["bzip2"])[:600])


if __name__ == "__main__":
    main()
