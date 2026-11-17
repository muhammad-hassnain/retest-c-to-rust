"""M2 (held-out measurement), RustMap part.

bzip2: RustMap's documented functional check (README 4.1.2-4.1.3) compresses the five shipped files
rust-code/bzip2_testcases/random_{1,10,100,1000,5000}_chars.txt with the Rust bzip2, salvages each
.bz2 with bzip2recover, decompresses rec00001*.bz2 with bzip2 -d and diffs it against the input.
This script
  * hashes those five files and our 20 inputs (regenerated with the exact generator in
    systems/rustmap/check_bzip2.sh) and reports the exact overlap;
  * runs RustMap's five files through our comparison: C round trip, RustMap's own check on the
    Rust output (as in check_bzip2.sh), and two stronger channels: does plain `bzip2 -d` accept
    the Rust output, and is the Rust .bz2 byte-identical to the C .bz2;
  * counts leading zero bytes of the Rust outputs and of the authors' shipped .bz2 outputs.
Rosetta: parses RustMap's two runner scripts to confirm each program is run with no arguments and
no stdin redirection, and scans the C sources for stdin/argv use, so "our runs = RustMap's runs"
is checked rather than assumed.

  docker run --rm --platform linux/amd64 -v "$PWD:/work" -w /work crust-syzygy python3 -I harness/heldout_rustmap.py
Writes results/heldout_rustmap.json.
"""
import datetime
import glob
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ART = os.path.join(REPO, "artifacts", "rustmap")
TCDIR = os.path.join(ART, "rust-code", "bzip2_testcases")
OUT = os.path.join(REPO, "results", "heldout_rustmap.json")
SIZES = [1, 10, 20, 64, 100, 128, 512, 1000, 2048, 5000]


def sha(b):
    return hashlib.sha256(b).hexdigest()


def gen(kind, n, idx):
    """Exact port of the generator embedded in systems/rustmap/check_bzip2.sh (gen kind size seed)."""
    seed = (idx * 2654435761 + 12345) & 0xffffffff
    b = bytearray()
    for _ in range(n):
        seed = (1103515245 * seed + 12345) & 0xffffffff
        v = (seed >> 16) & 0xff
        b.append(v if kind == "binary" else 33 + v % 94)
    return bytes(b)


def our_inputs():
    out, i = [], 0
    for kind in ("binary", "text"):
        for sz in SIZES:
            i += 1
            out.append({"name": f"{kind}_{sz}", "data": gen(kind, sz, i)})
    return out


def lead_zeros(b):
    return len(b) - len(b.lstrip(b"\0"))


def sh(cmd, cwd, timeout=60):
    try:
        p = subprocess.run(cmd, cwd=cwd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, b"", b"timeout"


def check_one(name, data, C, REC, R, work):
    d = os.path.join(work, name)
    for side in ("c", "r", "plain"):
        os.makedirs(os.path.join(d, side), exist_ok=True)
        open(os.path.join(d, side, "data"), "wb").write(data)
    sh(f"{C} --keep --force data", os.path.join(d, "c"))
    c_bz = open(os.path.join(d, "c", "data.bz2"), "rb").read()
    rc, out, _ = sh(f"{C} -dc data.bz2", os.path.join(d, "c"))
    c_ok = rc == 0 and out == data
    rrc, _, rerr = sh(f"timeout 60 {R} --keep --force data", os.path.join(d, "r"), timeout=90)
    rpath = os.path.join(d, "r", "data.bz2")
    r_bz = open(rpath, "rb").read() if os.path.isfile(rpath) else None
    own = plain = None
    if r_bz is not None:
        rc1, _, _ = sh(f"{REC} data.bz2 && {C} -d rec00001data.bz2", os.path.join(d, "r"))
        rec = os.path.join(d, "r", "rec00001data")
        own = rc1 == 0 and os.path.isfile(rec) and open(rec, "rb").read() == data
        open(os.path.join(d, "plain", "x.bz2"), "wb").write(r_bz)
        rc2, out2, err2 = sh(f"{C} -dc x.bz2", os.path.join(d, "plain"))
        plain = {"rc": rc2, "roundtrip": rc2 == 0 and out2 == data, "stderr": err2.decode("latin1")[-160:]}
    return {"input": name, "size": len(data), "sha256": sha(data),
            "c_roundtrip_ok": c_ok, "rust_rc": rrc,
            "rust_panicked": b"panicked" in rerr,
            "rust_passes_rustmap_own_check": own,
            "plain_bzip2_d_accepts_rust_output": plain,
            "rust_bz2_equals_c_bz2": (r_bz == c_bz) if r_bz is not None else None,
            "rust_bz2_size": len(r_bz) if r_bz is not None else None, "c_bz2_size": len(c_bz),
            "rust_bz2_leading_zero_bytes": lead_zeros(r_bz) if r_bz is not None else None}


def rosetta_check():
    c_sh = open(os.path.join(ART, "c-code", "rosseta-125", "rosetta-125.sh")).read()
    r_sh = open(os.path.join(ART, "rust-code", "rosetta_code_gpt", "125-rosetta-code-gpt", "125-rosetta-rs.sh")).read()
    c_run = [l.strip() for l in c_sh.splitlines() if re.search(r"^\s*\./main\b", l)]
    r_run = [l.strip() for l in r_sh.splitlines() if re.search(r'^\s*"\./\$filename"', l)]
    reads = {}
    for p in sorted(glob.glob(os.path.join(ART, "c-code", "rosseta-125", "*", "main.c"))):
        s = open(p, errors="replace").read()
        hits = sorted(set(re.findall(r"\b(scanf|getchar|fgets|getline|gets|fread|read)\s*\(", s)))
        uses_stdin = bool(re.search(r"\bstdin\b", s)) or "scanf" in hits or "getchar" in hits or "gets" in hits
        uses_argv = bool(re.search(r"\bargv\s*\[", s))
        if uses_stdin or uses_argv:
            reads[os.path.basename(os.path.dirname(p))] = {"stdin_calls": hits, "mentions_stdin": uses_stdin,
                                                            "indexes_argv": uses_argv}
    return {"c_runner_exec_lines": c_run, "rust_runner_exec_lines": r_run,
            "c_programs": len(glob.glob(os.path.join(ART, "c-code", "rosseta-125", "*", "main.c"))),
            "c_programs_that_read_stdin_or_argv": reads,
            "our_runner": "systems/rustmap/run_rosetta.py runs each binary with no arguments and stdin = empty pipe"}


def main():
    work = tempfile.mkdtemp(prefix="heldout_rm_")
    shutil.copytree(os.path.join(ART, "rust-code", "bzip2_rs_gpt"), os.path.join(work, "rs"),
                    ignore=shutil.ignore_patterns("target"))
    shutil.copytree(os.path.join(ART, "c-code", "bzip2-1.0.8"), os.path.join(work, "c"))
    b1 = sh("make clean >/dev/null 2>&1; make bzip2 bzip2recover", os.path.join(work, "c"), timeout=600)
    b2 = sh("cargo build", os.path.join(work, "rs"), timeout=1800)
    C, REC = os.path.join(work, "c", "bzip2"), os.path.join(work, "c", "bzip2recover")
    R = os.path.join(work, "rs", "target", "debug", "bzip2_rs_gpt")
    if not (os.path.isfile(C) and os.path.isfile(R)):
        raise SystemExit(f"build failed: {b1[2][-500:]} {b2[2][-1500:]}")
    theirs = []
    for n in (1, 10, 100, 1000, 5000):
        p = os.path.join(TCDIR, f"random_{n}_chars.txt")
        theirs.append({"name": os.path.basename(p), "data": open(p, "rb").read()})
    ours = our_inputs()
    their_h = {sha(t["data"]): t["name"] for t in theirs}
    our_h = {sha(o["data"]): o["name"] for o in ours}
    overlap = sorted(set(their_h) & set(our_h))
    rows_theirs = [check_one(t["name"], t["data"], C, REC, R, work) for t in theirs]
    rows_ours = [check_one(o["name"], o["data"], C, REC, R, work) for o in ours]
    shipped = {}
    for p in sorted(glob.glob(os.path.join(TCDIR, "*.bz2")) + glob.glob(os.path.join(TCDIR, "compress_output_bz2_files", "*.bz2"))):
        b = open(p, "rb").read()
        shipped[os.path.relpath(p, ART)] = {"size": len(b), "leading_zero_bytes": lead_zeros(b)}

    def ver(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, shell=True).stdout.splitlines()[0]
        except Exception:  # noqa: BLE001
            return None
    # counted bzip2 defects (paper): binary inputs 10 B..2 KB fail RustMap's own check (8), 1-byte input panics
    counted = [r for r in rows_ours if (r["input"].startswith("binary") and 10 <= r["size"] <= 2048
                                         and r["rust_passes_rustmap_own_check"] is False)
               or (r["size"] == 1 and r["rust_rc"] != 0)]
    out = {
        "provenance": {
            "script": "harness/heldout_rustmap.py",
            "command": "docker run --rm --platform linux/amd64 -v \"$PWD:/work\" -w /work crust-syzygy python3 -I harness/heldout_rustmap.py",
            "git_rev": os.environ.get("GIT_REV"), "host": os.environ.get("HOST_DESC", platform.system()),
            "arch": platform.machine(), "image": os.environ.get("IMAGE_DESC"),
            "toolchains": {"gcc": ver("gcc --version"), "rustc": ver("rustc --version"), "cargo": ver("cargo --version")},
            "build": "C: make clean; make bzip2 bzip2recover (c-code/bzip2-1.0.8); Rust: cargo build (debug), README 4.1.1",
            "date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        },
        "bzip2": {
            "system_inputs": [{"name": t["name"], "size": len(t["data"]), "sha256": sha(t["data"]),
                               "printable_ascii_only": all(32 <= c < 127 or c in (9, 10, 13) for c in t["data"])}
                              for t in theirs],
            "our_inputs": [{"name": o["name"], "size": len(o["data"]), "sha256": sha(o["data"])} for o in ours],
            "overlap_exact": [{"sha256": h, "theirs": their_h[h], "ours": our_h[h]} for h in overlap],
            "our_comparison_on_system_inputs": rows_theirs,
            "our_comparison_on_our_inputs": rows_ours,
            "counted_defects": [{"input": r["input"], "size": r["size"], "sha256": r["sha256"],
                                 "kind": "panic" if r["size"] == 1 else "fails RustMap's own check",
                                 "in_system_input_set": r["sha256"] in their_h} for r in counted],
            "shipped_author_outputs": shipped,
        },
        "rosetta": rosetta_check(),
    }
    json.dump(out, open(os.environ.get("OUT", OUT), "w"), indent=1)
    print(json.dumps(out["bzip2"]["our_comparison_on_system_inputs"], indent=1))
    print("overlap", len(overlap), "counted", len(counted))
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
