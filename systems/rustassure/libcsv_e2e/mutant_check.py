"""Self-test: prove the libcsv end-to-end differential harness can go RED.

A 348/360 "agree" result only means something if the harness would catch a real
field-level divergence. This injects a one-line fault into the TRANSLATED csv_parse
(shift the delimiter it compares against by one), rebuilds, and asserts the event
stream diverges from the original C. Exit 0 = harness detects the injected fault.

    docker run --rm -v "$PWD:/work" -w /work crust-heldout \
        python3 systems/rustassure/libcsv_e2e/mutant_check.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
LIBCSV = os.path.join(REPO, "artifacts", "rustassure", "src", "python", "inputs-complex", "libcsv")
CRATE = os.path.join(HERE, "sut_crate")


def sh(argv, **kw):
    return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)


def build_variant(mutate):
    """Generate the SUT source, optionally mutate it, build, return the binary path."""
    gen = os.path.join(HERE, "csv_sut.rs")
    sh([sys.executable, os.path.join(HERE, "extract_sut.py"), gen])
    with open(gen) as f:
        src = f.read()
    if mutate:
        src2 = src.replace("let delim = p.delim_char;",
                           "let delim = p.delim_char.wrapping_add(1);")
        assert src2 != src, "mutation site not found"
        src = src2
    os.makedirs(os.path.join(CRATE, "src"), exist_ok=True)
    with open(os.path.join(CRATE, "src", "main.rs"), "w") as f:
        f.write(src)
    try:
        os.remove(os.path.join(CRATE, "Cargo.lock"))
    except OSError:
        pass
    sh(["cargo", "build", "--release"], cwd=CRATE)
    return os.path.join(CRATE, "target", "release", "csv_sut")


def oracle():
    o = "/tmp/csv_oracle_mc"
    sh(["gcc", "-O2", "-I", LIBCSV, os.path.join(LIBCSV, "libcsv.c"),
        os.path.join(HERE, "csv_oracle.c"), "-o", o])
    return o


def run(binp, data):
    return subprocess.run([binp, "0"], input=data, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=20).stdout


def main():
    o = oracle()
    data = b"a,b,c\n"
    mut = build_variant(mutate=True)
    o_out, m_out = run(o, data), run(mut, data)
    build_variant(mutate=False)  # restore clean source for the next real run
    if o_out == m_out:
        print("[mutant_check] FAIL: harness blind to a shifted-delimiter fault")
        print("  oracle:", o_out)
        sys.exit(1)
    print("[mutant_check] OK: injected delimiter fault detected")
    print("  oracle:", o_out.decode().strip().split("\n")[:4])
    print("  mutant:", m_out.decode().strip().split("\n")[:4])


if __name__ == "__main__":
    main()
