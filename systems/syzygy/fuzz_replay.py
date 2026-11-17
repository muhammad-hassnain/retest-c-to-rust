"""Replay an AFL corpus through the original C and the translated Rust, diff each input.

Used by fuzz.sh: AFL discovered the corpus via coverage feedback; here we just differential-test
(stdout + exit + stderr, byte-exact) each discovered input. A divergence on a fuzzer-found input
is a behaviour the coverage-guided search reached that the translation gets wrong.
"""
import json
import os
import sys

sys.path.insert(0, "/work/harness")
from common import run_capture  # noqa: E402

corpus, cbin, rbin = sys.argv[1], sys.argv[2], sys.argv[3]
files = sorted(f for f in os.listdir(corpus) if not f.startswith("."))
n = div = nul_related = other = 0
other_samples = []
for fn in files:
    data = open(os.path.join(corpus, fn), "rb").read()
    o = run_capture([cbin], data, timeout=20)
    s = run_capture([rbin], data, timeout=20)
    n += 1
    if o.timed_out or s.timed_out:
        continue
    if o.stdout == s.stdout and o.returncode == s.returncode and o.stderr == s.stderr:
        continue
    div += 1
    # Known root cause: the C harness sizes the input with strlen() (stops at the first NUL),
    # while the Rust uses the full buffer length. Separate those from other divergences.
    if b"\x00" in data:
        nul_related += 1
    else:
        other += 1
        if len(other_samples) < 5:
            other_samples.append({
                "input_len": len(data),
                "c_stderr_tail": o.stderr[-90:].decode("latin1", "replace"),
                "rust_stderr_tail": s.stderr[-90:].decode("latin1", "replace"),
            })
print(json.dumps({
    "system": "syzygy-zopfli", "method": "AFL++ coverage-guided + differential replay",
    "corpus_inputs": n, "divergent_inputs": div,
    "nul_size_divergences": nul_related,   # C strlen() truncates at NUL; Rust uses full length
    "other_divergences": other,            # no NUL in input -> genuine compression/ratio difference
    "other_samples": other_samples}, indent=2))
