"""Build every Rustine stage (translate/, automatic_debug/, manual_debug/) from a FRESH copy.

Replaces compile_rate.sh's in-place build (which reused stale target/ dirs and rewrote
Cargo.lock). Each stage directory is copied without target/ to a temp dir, then
`cargo build --bins` runs there with the toolchain on PATH (or RUSTUP_TOOLCHAIN).
Records the toolchain, per-program verdict and the first error line, so the count is
reproducible and attributable to a toolchain.

  docker run --rm -v "$PWD:/work" -w /work crust-stable \
      python3 systems/rustine/build_matrix.py --out results/rustine_build_<tag>.json
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
ART = os.path.join(REPO, "artifacts", "rustine", "artifacts")
STAGES = ["translate", "automatic_debug", "manual_debug"]


def first_error(log):
    for line in log.splitlines():
        if re.match(r"\s*error(\[E\d+\])?:", line) or "failed to parse" in line:
            return line.strip()[:200]
    return log.strip().splitlines()[-1][:200] if log.strip() else ""


def build(src, timeout, shipped_locks):
    tmp = tempfile.mkdtemp(prefix="rustine-")
    dst = os.path.join(tmp, "p")
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("target"))
    # Rustine ships no Cargo.lock for these stages; any lockfile here was left by an earlier
    # in-place build of ours (audit 4). Keep a lockfile only if upstream tracks it.
    lock = os.path.join(dst, "Cargo.lock")
    rel = os.path.relpath(os.path.join(src, "Cargo.lock"), os.path.join(REPO, "artifacts", "rustine"))
    if os.path.exists(lock) and rel not in shipped_locks:
        os.remove(lock)
    try:
        p = subprocess.run(["cargo", "build", "--bins"],
                           cwd=dst, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        log = p.stdout.decode("utf8", "replace")
        return ("BUILD" if p.returncode == 0 else "FAIL"), ("" if p.returncode == 0 else first_error(log))
    except subprocess.TimeoutExpired:
        return "TIMEOUT", ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--shipped-locks", default="", help="JSON list of Cargo.lock paths upstream tracks "
                    "(from `git -C artifacts/rustine ls-files`); others are ignored")
    a = ap.parse_args()
    tc = subprocess.run(["rustc", "--version"], stdout=subprocess.PIPE).stdout.decode().strip()
    arch = os.uname().machine
    shipped_locks = set(json.load(open(a.shipped_locks))) if a.shipped_locks else set()
    rows = []
    for prog in sorted(os.listdir(ART)):
        for st in STAGES:
            d = os.path.join(ART, prog, st)
            if not os.path.isfile(os.path.join(d, "Cargo.toml")):
                continue
            v, err = build(d, a.timeout, shipped_locks)
            rows.append({"program": prog, "stage": st, "verdict": v, "first_error": err})
            print(prog, st, v, err, flush=True)
            summ = {}
            for r in rows:
                s = summ.setdefault(r["stage"], {"programs": 0, "build": 0})
                s["programs"] += 1
                s["build"] += r["verdict"] == "BUILD"
            json.dump({"toolchain": tc, "arch": arch, "lockfiles": "only upstream-tracked (none for these stages); dependencies resolved at build time", "summary": summ, "rows": rows}, open(a.out, "w"), indent=1)


if __name__ == "__main__":
    main()
