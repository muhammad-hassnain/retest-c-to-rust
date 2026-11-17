"""End-to-end differential test: ORIGINAL u8c (C) vs RustAssure's composed u8c
translation (Rust), decoding a deterministic UTF-8/byte corpus with u8next_.

Reaches the pointer-based decoder a scalar per-function harness can't drive, by
walking the whole string through u8next_ and comparing (codepoint, bytes-consumed)
at every step, byte-for-byte.

    docker run --rm -v "$PWD:/work" -w /work crust-stable \
        python3 systems/rustassure/u8c_e2e/run.py
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
U8C = os.path.join(REPO, "artifacts", "rustassure", "src", "python", "inputs-complex", "u8c")
sys.path.insert(0, HERE)
from gen_corpus import corpus  # noqa: E402


def sh(argv, **kw):
    return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)


def build():
    # SUT: extract then compile with rustc (u8c_link has no deps)
    gen = os.path.join(HERE, "u8c_sut.rs")
    sh([sys.executable, os.path.join(HERE, "extract_sut.py"), gen])
    sut = "/tmp/u8c_sut"
    r = sh(["rustc", "-O", gen, "-o", sut])
    if not os.path.isfile(sut):
        print(r.stdout.decode("utf-8", "replace")[-2500:])
        raise SystemExit("SUT build failed")
    # oracle: original u8c.c + driver
    oracle = "/tmp/u8c_oracle"
    r = sh(["gcc", "-O2", "-I", U8C, os.path.join(U8C, "u8c.c"),
            os.path.join(HERE, "u8c_oracle.c"), "-o", oracle])
    if not os.path.isfile(oracle):
        print(r.stdout.decode("utf-8", "replace")[-2500:])
        raise SystemExit("oracle build failed")
    return oracle, sut


def run_one(binp, data):
    p = subprocess.run([binp], input=data, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=20)
    return p.stdout, p.returncode


def main():
    oracle, sut = build()
    cases = corpus()
    n = agree = 0
    samples = []
    div_names = []
    for cname, data in cases:
        o_out, o_rc = run_one(oracle, data)
        s_out, s_rc = run_one(sut, data)
        n += 1
        if o_out == s_out and o_rc == s_rc:
            agree += 1
            continue
        div_names.append(cname)
        if len(samples) < 16:
            samples.append({
                "input": cname, "hex": data.hex(),
                "oracle": o_out.decode("utf-8", "replace").strip().split("\n"),
                "sut": s_out.decode("utf-8", "replace").strip().split("\n"),
                "exit": [o_rc, s_rc],
            })
    out = {
        "system": "RustAssure",
        "target": "u8c (end-to-end UTF-8 decode, composed translation vs original C)",
        "method": "differential: original u8c.c u8next_ vs RustAssure u8c_link composition, "
                  "deterministic UTF-8/byte corpus, byte-exact (codepoint, bytes) stream",
        "n_cases": n, "agree": agree, "divergent": n - agree,
        "divergent_inputs": div_names, "samples": samples,
    }
    print(json.dumps(out, indent=2))
    with open(os.path.join(HERE, "results.json"), "w") as f:
        json.dump(out, f, indent=2)


if __name__ == "__main__":
    main()
