"""Build the C oracle and ACTOR's own Rust for every TRACTOR executable case (inside Docker).

C:    gcc, the case's own CMakeLists.txt recipe (sources, include dirs, compile options, link libs,
      CMAKE_C_FLAGS; no optimisation flag, as CMake's default build type), configured as the case
      name says for the three macrodepth cases (OP/REPEAT).
Rust: `cargo build --release --offline` in a copy of the graded phase dir (verified/ else
      translated/), with the matching Cargo features for macrodepth, under the toolchain mounted at
      /rust (see run_in_container.sh). No Cargo.lock ships; the resolved lockfile is recorded.

Usage (in container): python3 -I build.py [--only battery/case]
Writes /w/out/build.json and per-case binaries under /w/build/<battery>/<case>/{c,rust}/.
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

B = os.path.join(common.W, "build")


def sh(argv, cwd=None, env=None, timeout=900):
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as e:
        return -1, "TIMEOUT\n" + (e.stdout or b"").decode("utf-8", "replace")


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def c_cmd(cc, recipe, csrc, out, extra=()):
    srcs = [os.path.join(csrc, s) for s in recipe["sources"]]
    inc = ["-I" + os.path.join(csrc, i) for i in recipe["includes"]]
    return [cc] + list(extra) + recipe["cflags"] + inc + srcs + ["-o", out] + recipe["libs"] + ["-lm"]


def build_case(battery, case, rust_only=False):
    ph, src = common.phase_dir(battery, case)
    rec = {"battery": battery, "case": case, "phase": ph}
    if not src:
        rec["status"] = "no_crate"
        return rec
    over, feats = common.case_config(case)
    rec["config"] = {"c": over, "cargo": feats}
    d = os.path.join(B, battery, case)
    os.makedirs(d, exist_ok=True)
    # ---- C oracle
    csrc = os.path.join(src, "c_src")
    recipe = common.parse_cmake(os.path.join(csrc, "CMakeLists.txt"), over)
    rec["c_recipe"] = {k: v for k, v in recipe.items() if k != "vars"}
    cdir = os.path.join(d, "c")
    os.makedirs(cdir, exist_ok=True)
    cbin = os.path.join(cdir, "driver")
    cmd = c_cmd("gcc", recipe, csrc, cbin)
    rc, log = sh(cmd)
    rec["c_build"] = {"cmd": " ".join(cmd), "rc": rc, "log_tail": log[-1500:]}
    rec["c_ok"] = rc == 0 and os.path.isfile(cbin)
    if rec["c_ok"]:
        rec["c_sha256"] = sha(cbin)
    # ---- Rust
    rdir = os.path.join(d, "rust")
    if os.path.isdir(rdir):
        shutil.rmtree(rdir)
    shutil.copytree(src, rdir, ignore=shutil.ignore_patterns("target", "c_src", "c_libs", "logs",
                                                             "build*", "*.o", "*.so"))
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = os.path.join(d, "target")
    cmd = ["cargo", "build", "--release", "--offline", "--bins"] + feats
    rc, log = sh(cmd, cwd=rdir, env=env)
    rec["rust_build"] = {"cmd": " ".join(cmd), "rc": rc, "log_tail": log[-3000:]}
    name = common.cargo_bin_name(os.path.join(rdir, "Cargo.toml"))
    rbin = os.path.join(d, "target", "release", name or "driver")
    rec["rust_bin"] = os.path.relpath(rbin, common.W)
    rec["rust_ok"] = rc == 0 and os.path.isfile(rbin)
    if rec["rust_ok"]:
        rec["rust_sha256"] = sha(rbin)
    lock = os.path.join(rdir, "Cargo.lock")
    if os.path.isfile(lock):
        rec["cargo_lock_sha256"] = sha(lock)
        pk = []
        cur = {}
        for line in open(lock):
            if line.startswith("name = "):
                cur = {"name": line.split('"')[1]}
            elif line.startswith("version = ") and cur:
                cur["version"] = line.split('"')[1]
                pk.append(f'{cur["name"]} {cur["version"]}')
        rec["cargo_lock_packages"] = pk
    rec["status"] = "ok" if rec["c_ok"] and rec["rust_ok"] else (
        "c_build_fail" if not rec["c_ok"] else "rust_build_fail")
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None)
    ap.add_argument("--out", default=os.path.join(common.W, "out", "build.json"))
    a = ap.parse_args()
    cases = common.exe_cases()
    if a.only:
        cases = [x for x in cases if "/".join(x) == a.only]
    res = []
    for b, c in cases:
        r = build_case(b, c)
        print(f"{b}/{c}: {r['status']}", flush=True)
        res.append(r)
    vers = {}
    for t in (["gcc", "--version"], ["rustc", "-vV"], ["cargo", "--version"]):
        rc, o = sh(t)
        vers[t[0]] = o.strip().splitlines()[0] if t[0] != "rustc" else o.strip()
    common.dump(a.out, {"toolchains": vers, "cases": res})


if __name__ == "__main__":
    main()
