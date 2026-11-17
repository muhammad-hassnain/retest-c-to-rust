"""RustMap bzip2: RustMap's own functional check (README 4.1.1-4.1.3) on RustMap's own five test files,
with no 60 s cap (arXiv re-run; the shipped harness/heldout_rustmap.py caps the Rust at 60 s).

Procedure, as the README writes it:
  4.1.1  cd rust-code/bzip2_rs_gpt; cargo build            (debug)
  4.1.2  cd rust-code/bzip2_testcases; for N in 1 10 100 1000 5000:
             echo "Running random_N_chars.txt"; time .../target/debug/bzip2_rs_gpt random_N_chars.txt
  4.1.3  for N: bzip2recover random_N_chars.txt.bz2; bzip2 -d rec00001random_N_chars.txt.bz2
         for N: diff random_N_chars.txt rec00001random_N_chars.txt > diff_random_N_chars.txt
The commands run verbatim (bash -c) in a fresh copy of bzip2_testcases holding only the five
random_N_chars.txt files (the shipped folder also holds the authors' own .bz2 outputs, which the
compressor would otherwise meet). `bzip2` and `bzip2recover` on PATH are built from the artifact's
c-code/bzip2-1.0.8 (`make bzip2 bzip2recover`), as in harness/heldout_rustmap.py and
systems/rustmap/check_bzip2.sh. A file passes when its diff exits 0. Each compression has a safety
cap of CAP seconds (default 3600, eight times the authors' 7m31s for 5000 chars); wall, user and
sys time are recorded per file.

  docker run --rm --platform linux/amd64 --cpus=2 -v "$PWD:/work" -w /work crust-syzygy \
      python3 -I systems/rustmap/check_bzip2_own_uncapped.py
Writes results/arxiv_rustmap_bzip2_check_uncapped.json (OUT overrides).
"""
import hashlib
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "scripts"))
from provenance import stamp  # noqa: E402

ART = os.environ.get("ART", os.path.join(REPO, "artifacts", "rustmap"))
TCDIR = os.path.join(ART, "rust-code", "bzip2_testcases")
OUT = os.environ.get("OUT", os.path.join(REPO, "results", "arxiv_rustmap_bzip2_check_uncapped.json"))
CAP = int(os.environ.get("CAP", "3600"))
NS = [1, 10, 100, 1000, 5000]


def sha(b):
    return hashlib.sha256(b).hexdigest()


def sh(cmd, cwd, env=None, timeout=None):
    p = subprocess.run(cmd, cwd=cwd, shell=True, executable="/bin/bash", env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    return p.returncode, p.stdout.decode("latin1"), p.stderr.decode("latin1")


def timed(argv, cwd, env, cap):
    """Run argv, return rc, wall/user/sys seconds and stderr tail; kill after cap seconds."""
    ru0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.monotonic()
    p = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    timed_out = False
    try:
        out, err = p.communicate(timeout=cap)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
        timed_out = True
    wall = time.monotonic() - t0
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {"rc": p.returncode, "timed_out": timed_out, "wall_s": round(wall, 3),
            "user_s": round(ru.ru_utime - ru0.ru_utime, 3), "sys_s": round(ru.ru_stime - ru0.ru_stime, 3),
            "stdout_tail": out.decode("latin1")[-300:], "stderr_tail": err.decode("latin1")[-300:]}


def ver(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.splitlines()[0]
    except Exception:  # noqa: BLE001
        return None


def main():
    work = tempfile.mkdtemp(prefix="arxiv_rm_uncapped_")
    shutil.copytree(os.path.join(ART, "rust-code", "bzip2_rs_gpt"), os.path.join(work, "rs"),
                    ignore=shutil.ignore_patterns("target"))
    shutil.copytree(os.path.join(ART, "c-code", "bzip2-1.0.8"), os.path.join(work, "c"))
    b1 = sh("make clean >/dev/null 2>&1; make bzip2 bzip2recover", os.path.join(work, "c"), timeout=600)
    t0 = time.monotonic()
    b2 = sh("cargo build", os.path.join(work, "rs"), timeout=3600)
    build_s = round(time.monotonic() - t0, 1)
    C, REC = os.path.join(work, "c", "bzip2"), os.path.join(work, "c", "bzip2recover")
    R = os.path.join(work, "rs", "target", "debug", "bzip2_rs_gpt")
    if not (os.path.isfile(C) and os.path.isfile(REC) and os.path.isfile(R)):
        raise SystemExit(f"build failed: {b1[2][-500:]} {b2[2][-1500:]}")
    bindir = os.path.join(work, "bin")
    os.makedirs(bindir)
    os.symlink(C, os.path.join(bindir, "bzip2"))
    os.symlink(REC, os.path.join(bindir, "bzip2recover"))
    env = dict(os.environ, PATH=bindir + ":" + os.environ.get("PATH", "/usr/bin:/bin"))

    tc = os.path.join(work, "bzip2_testcases")
    os.makedirs(tc)
    inputs = {}
    for n in NS:
        name = f"random_{n}_chars.txt"
        shutil.copy2(os.path.join(TCDIR, name), os.path.join(tc, name))
        inputs[name] = open(os.path.join(tc, name), "rb").read()

    # 4.1.2: compress each file with the Rust bzip2, in order, as the README's block does
    compress = {}
    for n in NS:
        name = f"random_{n}_chars.txt"
        r = timed([R, name], tc, env, CAP)
        bz = os.path.join(tc, name + ".bz2")
        r["input_still_present"] = os.path.isfile(os.path.join(tc, name))
        r["bz2_written"] = os.path.isfile(bz)
        r["bz2_size"] = os.path.getsize(bz) if r["bz2_written"] else None
        compress[name] = r
        print(name, json.dumps({k: r[k] for k in ("rc", "timed_out", "wall_s")}), flush=True)

    # 4.1.3: bzip2recover + bzip2 -d, then diff, verbatim
    rows = []
    for n in NS:
        name = f"random_{n}_chars.txt"
        rec = sh(f"bzip2recover {name}.bz2", tc, env)
        dec = sh(f"bzip2 -d rec00001{name}.bz2", tc, env)
        dif = sh(f"diff {name} rec00001{name} > diff_{name}", tc, env)
        recf = os.path.join(tc, f"rec00001{name}")
        recovered = open(recf, "rb").read() if os.path.isfile(recf) else None
        dfile = os.path.join(tc, f"diff_{name}")
        c = compress[name]
        rows.append({
            "file": name, "size": len(inputs[name]), "sha256": sha(inputs[name]),
            "rust_compress": {k: c[k] for k in ("rc", "timed_out", "wall_s", "user_s", "sys_s", "stderr_tail",
                                                "input_still_present", "bz2_written", "bz2_size")},
            "bzip2recover": {"rc": rec[0], "stderr_tail": rec[2][-300:]},
            "bzip2_d": {"rc": dec[0], "stderr_tail": dec[2][-300:]},
            "diff": {"rc": dif[0], "diff_file_bytes": os.path.getsize(dfile) if os.path.isfile(dfile) else None,
                     "stderr_tail": dif[2][-300:]},
            "recovered_equals_input": recovered == inputs[name] if recovered is not None else None,
            "passes_rustmap_check": dif[0] == 0,
        })
    authors = {}
    tpath = os.path.join(TCDIR, "timings.txt")
    if os.path.isfile(tpath):
        cur = None
        for line in open(tpath):
            if line.startswith("Running "):
                cur = line.split()[1]
            elif line.startswith("real") and cur:
                authors[cur] = line.split()[1]
    out = {
        "_provenance": stamp(image=os.environ.get("CRUST_IMAGE")),
        "provenance": {
            "script": "systems/rustmap/check_bzip2_own_uncapped.py",
            "command": "docker run --rm --platform linux/amd64 --cpus=2 -v \"$PWD:/work\" -w /work crust-syzygy "
                       "python3 -I systems/rustmap/check_bzip2_own_uncapped.py",
            "host": os.environ.get("HOST_DESC"), "image": os.environ.get("IMAGE_DESC"),
            "toolchains": {"gcc": ver("gcc --version"), "rustc": ver("rustc --version"), "cargo": ver("cargo --version")},
            "build": "C: make clean; make bzip2 bzip2recover (c-code/bzip2-1.0.8); Rust: cargo build (debug), README 4.1.1",
            "rust_build_seconds": build_s,
            "per_file_cap_seconds": CAP,
            "procedure": "README 4.1.2 (Rust bzip2 on each random_N_chars.txt, no flags, in order) and 4.1.3 "
                         "(bzip2recover; bzip2 -d rec00001*; diff) verbatim via bash -c in a fresh copy of "
                         "bzip2_testcases holding only the five inputs; bzip2/bzip2recover built from c-code/bzip2-1.0.8",
        },
        "authors_timings_txt_real": authors,
        "rows": rows,
        "summary": {"files": len(rows), "pass": sum(r["passes_rustmap_check"] for r in rows),
                    "timed_out": sum(r["rust_compress"]["timed_out"] for r in rows),
                    "wall_s": {r["file"]: r["rust_compress"]["wall_s"] for r in rows}},
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps(out["summary"], indent=1))
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
