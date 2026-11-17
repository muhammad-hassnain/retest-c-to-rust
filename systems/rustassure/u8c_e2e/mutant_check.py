"""Self-test: prove the u8c decoder differential harness can go RED on a VALID input.

The real run already diverges on malformed input and agrees on valid input, but to show
the harness also catches a value bug on the *valid* decode path, inject a one-token fault
into the translated u8next_ (shift accumulation `val << 6` -> `val << 5`), rebuild, and
assert a valid multi-byte codepoint now mis-decodes. Exit 0 = fault detected.

    docker run --rm -v "$PWD:/work" -w /work crust-stable \
        python3 systems/rustassure/u8c_e2e/mutant_check.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
U8C = os.path.join(REPO, "artifacts", "rustassure", "src", "python", "inputs-complex", "u8c")


def sh(a, **k):
    return subprocess.run(a, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **k)


def build(mutate):
    gen = os.path.join(HERE, "u8c_sut.rs")
    sh([sys.executable, os.path.join(HERE, "extract_sut.py"), gen])
    with open(gen) as f:
        src = f.read()
    if mutate:
        m = src.replace("(val << 6)", "(val << 5)")
        assert m != src, "mutation site not found"
        src = m
        with open(gen, "w") as f:
            f.write(src)
    out = "/tmp/u8c_mut" if mutate else "/tmp/u8c_clean"
    sh(["rustc", "-O", gen, "-o", out])
    sh([sys.executable, os.path.join(HERE, "extract_sut.py"), gen])  # restore clean file
    return out


def oracle():
    o = "/tmp/u8c_oracle_mc"
    sh(["gcc", "-O2", "-I", U8C, os.path.join(U8C, "u8c.c"),
        os.path.join(HERE, "u8c_oracle.c"), "-o", o])
    return o


def run(b, data):
    return subprocess.run([b], input=data, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=20).stdout


def main():
    o = oracle()
    mut = build(mutate=True)
    data = "€".encode()  # a valid 3-byte codepoint (U+20AC)
    o_out, m_out = run(o, data), run(mut, data)
    if o_out == m_out:
        print("[mutant_check] FAIL: harness blind to a shifted-accumulation fault")
        sys.exit(1)
    print("[mutant_check] OK: injected decode fault detected on valid input")
    print("  oracle:", o_out.decode().strip().split("\n"))
    print("  mutant:", m_out.decode().strip().split("\n"))


if __name__ == "__main__":
    main()
