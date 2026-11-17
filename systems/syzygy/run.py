"""Re-grade Syzygy's released Zopfli translation with held-out differential testing.

Oracle = original C (c_code/zopfli.c) compiled with gcc.
SUT     = the released translated Rust (rust_code -> bin testing_equiv), built with a
          recent stable toolchain.
We compare stdout + exit + STDERR byte-exact on fresh held-out byte streams. (Syzygy's
own harness compares only stderr, and never on a disjoint/held-out split.)

Run inside the amd64 image (the amalgamated zopfli.c hardcodes an x86_64 include path):
  docker run --rm --platform linux/amd64 -v "$PWD:/work" -w /work crust-syzygy \
      python3 systems/syzygy/run.py -n 200
"""
import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "harness"))
sys.path.insert(0, HERE)

import gen  # noqa: E402
from difftest import run as diff_run  # noqa: E402

ART = os.path.join(REPO, "artifacts", "syzygy_zopfli")
C_SRC = os.path.join(ART, "c_code", "zopfli.c")
C_BIN = os.path.join(ART, "c_code", "zopfli")
RUST_DIR = os.path.join(ART, "rust_code")
RUST_BIN = os.path.join(RUST_DIR, "target", "release", "testing_equiv")
RUST_MAIN = os.path.join(RUST_DIR, "src", "main.rs")


def sh(argv, cwd=None):
    p = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout


def build_oracle():
    rc, log = sh(["gcc", "-O2", C_SRC, "-o", C_BIN, "-lm"])
    return os.path.isfile(C_BIN) and rc == 0, log


def build_sut():
    rc, log = sh(["cargo", "build", "--release"], cwd=RUST_DIR)
    return os.path.isfile(RUST_BIN) and rc == 0, log


def sanity_mutant_caught(cases):
    """Inject a guaranteed-divergent stderr line into the SUT and confirm the harness
    reports a divergence (proves it can go red for this system)."""
    with open(RUST_MAIN) as f:
        orig = f.read()
    m = re.search(r"fn\s+main\s*\([^)]*\)[^{]*\{", orig)
    if not m:
        return None
    mutated = orig[:m.end()] + '\n    eprintln!("SANITY_MUTANT");\n' + orig[m.end():]
    try:
        with open(RUST_MAIN, "w") as f:
            f.write(mutated)
        ok, _ = build_sut()
        if not ok:
            return {"status": "noncompile"}
        summ, _ = diff_run("zopfli", cases[:30], C_BIN, RUST_BIN,
                           timeout=30, stop_on_any=True)
        return {"status": "compiled", "caught": (summ["any_divergences"] or 0) > 0}
    finally:
        with open(RUST_MAIN, "w") as f:
            f.write(orig)
        build_sut()  # restore clean SUT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=20261016)
    ap.add_argument("--out", default=os.path.join(REPO, "results", "syzygy_diff.json"))
    args = ap.parse_args()

    ob, olog = build_oracle()
    if not ob:
        print(json.dumps({"error": "oracle build failed", "log": olog[-800:]})); sys.exit(2)
    sb, slog = build_sut()
    if not sb:
        print(json.dumps({"error": "SUT build failed", "log": slog[-800:]})); sys.exit(2)

    cases = gen.generate(args.n, args.seed)
    summary, verdicts = diff_run("zopfli", cases, C_BIN, RUST_BIN, timeout=30)
    sanity = sanity_mutant_caught(cases)

    out = {"system": "syzygy", "program": "zopfli", "summary": summary, "sanity": sanity}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump({"summary": summary, "sanity": sanity, "verdicts": verdicts}, f, indent=2)
    print(json.dumps(out, indent=2))
    sys.exit(1 if summary["any_divergences"] else 0)


if __name__ == "__main__":
    main()
