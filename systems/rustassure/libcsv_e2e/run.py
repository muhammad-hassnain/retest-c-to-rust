"""End-to-end differential test: ORIGINAL libcsv (C) vs RustAssure's composed
translation (Rust), over a deterministic CSV corpus x an options grid.

This reaches exactly the pointer/struct functions a scalar per-function harness
can't (csv_parse / csv_fini / csv_increase_buffer / the state machine), by driving
the whole parser through its public API and comparing the emitted event stream
byte-for-byte (field bytes, row terminators, bytes-consumed, fini + error codes).

Run inside the nightly-2022 image (needed to build the translation):
    docker run --rm -v "$PWD:/work" -w /work crust-heldout \
        python3 systems/rustassure/libcsv_e2e/run.py
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
LIBCSV = os.path.join(REPO, "artifacts", "rustassure", "src", "python", "inputs-complex", "libcsv")
SUT_CRATE = os.path.join(HERE, "sut_crate")
sys.path.insert(0, HERE)
from gen_corpus import corpus, OPTIONS  # noqa: E402


def sh(argv, **kw):
    return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)


def build():
    # 1) extract the SUT from the shipped composition, then cargo build (nightly-2022)
    sh([sys.executable, os.path.join(HERE, "extract_sut.py"), os.path.join(HERE, "csv_sut.rs")])
    os.makedirs(os.path.join(SUT_CRATE, "src"), exist_ok=True)
    with open(os.path.join(HERE, "csv_sut.rs")) as f:
        src = f.read()
    with open(os.path.join(SUT_CRATE, "src", "main.rs"), "w") as f:
        f.write(src)
    try:
        os.remove(os.path.join(SUT_CRATE, "Cargo.lock"))
    except OSError:
        pass
    r = sh(["cargo", "build", "--release"], cwd=SUT_CRATE)
    sut = os.path.join(SUT_CRATE, "target", "release", "csv_sut")
    if not os.path.isfile(sut):
        print(r.stdout.decode("utf-8", "replace")[-2000:])
        raise SystemExit("SUT build failed")
    # 2) build the C oracle = original libcsv + our driver
    oracle = "/tmp/csv_oracle"
    r = sh(["gcc", "-O2", "-I", LIBCSV, os.path.join(LIBCSV, "libcsv.c"),
            os.path.join(HERE, "csv_oracle.c"), "-o", oracle])
    if not os.path.isfile(oracle):
        print(r.stdout.decode("utf-8", "replace")[-2000:])
        raise SystemExit("oracle build failed")
    return oracle, sut


def run_one(binp, opt, data):
    p = subprocess.run([binp, str(opt)], input=data, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=20)
    return p.stdout, p.returncode


def classify(o_out, s_out):
    """Which channel diverged? Returns a set of tags."""
    if o_out == s_out:
        return set()
    ol, sl = o_out.split(b"\n"), s_out.split(b"\n")
    tags = set()
    # the specific NULL-vs-empty-slice contract difference under CSV_EMPTY_IS_NULL
    if b"F NULL" in ol and b"F NULL" not in sl:
        tags.add("null_vs_empty_field")
    # per-prefix channel comparison
    for pref, tag in ((b"F ", "field_event"), (b"R ", "row_event"),
                      (b"P ", "processed"), (b"Z ", "fini_ret"), (b"E ", "error_code")):
        o_lines = [x for x in ol if x.startswith(pref)]
        s_lines = [x for x in sl if x.startswith(pref)]
        if o_lines != s_lines:
            tags.add(tag)
    if not tags:
        tags.add("other")
    return tags


def main():
    oracle, sut = build()
    cases = corpus()
    n = agree = 0
    div_by_tag = {}
    div_by_opt = {}
    samples = []
    for oname, opt in OPTIONS:
        for cname, data in cases:
            o_out, o_rc = run_one(oracle, opt, data)
            s_out, s_rc = run_one(sut, opt, data)
            n += 1
            tags = classify(o_out, s_out)
            if o_rc != s_rc:
                tags.add(f"exit({o_rc}vs{s_rc})")
            if not tags:
                agree += 1
                continue
            div_by_opt[oname] = div_by_opt.get(oname, 0) + 1
            for t in tags:
                div_by_tag[t] = div_by_tag.get(t, 0) + 1
            if len(samples) < 14:
                samples.append({
                    "input": cname, "options": oname, "tags": sorted(tags),
                    "oracle": o_out.decode("utf-8", "replace").strip().split("\n"),
                    "sut": s_out.decode("utf-8", "replace").strip().split("\n"),
                })
    out = {
        "system": "RustAssure",
        "target": "libcsv (end-to-end, composed translation vs original C)",
        "method": "differential: original libcsv.c vs RustAssure csv_link composition, "
                  "deterministic CSV corpus x options grid, byte-exact event stream",
        "n_cases": n,
        "agree": agree,
        "divergent": n - agree,
        "divergence_by_channel": div_by_tag,
        "divergence_by_option": div_by_opt,
        "samples": samples,
    }
    print(json.dumps(out, indent=2))
    with open(os.path.join(HERE, "results.json"), "w") as f:
        json.dump(out, f, indent=2)


if __name__ == "__main__":
    main()
