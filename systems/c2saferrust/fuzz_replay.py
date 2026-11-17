"""Replay an AFL corpus through a coreutils C oracle and its Rust translation (same flags, stdin).

A divergence on a fuzzer-found stdin is a byte-formatting/output difference the coverage-guided
search reached. We classify: stdout/exit mismatch (primary) vs stderr-only (secondary, after
program-name normalisation).
"""
import json
import os
import sys

sys.path.insert(0, "/work/harness")
from common import run_capture, normalize_stderr  # noqa: E402

corpus, cbin, rbin, flags = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4].split()
files = sorted(f for f in os.listdir(corpus) if not f.startswith("."))
n = stdout_div = stderr_div = 0
samples = []
for fn in files:
    data = open(os.path.join(corpus, fn), "rb").read()
    o = run_capture([cbin] + flags, data, timeout=15)
    s = run_capture([rbin] + flags, data, timeout=15)
    n += 1
    if o.timed_out or s.timed_out:
        continue
    oe = normalize_stderr(o.stderr, cbin, rbin)
    se = normalize_stderr(s.stderr, cbin, rbin)
    if o.stdout != s.stdout or o.returncode != s.returncode:
        stdout_div += 1
        if len(samples) < 4:
            samples.append({"input_len": len(data), "c_out_len": len(o.stdout),
                            "rust_out_len": len(s.stdout), "exit": [o.returncode, s.returncode],
                            "c_head": o.stdout[:40].hex(), "rust_head": s.stdout[:40].hex()})
    elif oe != se:
        stderr_div += 1
print(json.dumps({"util_flags": [os.path.basename(cbin)] + flags,
                  "method": "AFL++ coverage-guided (stdin) + differential replay",
                  "corpus_inputs": n, "stdout_or_exit_divergences": stdout_div,
                  "stderr_only_divergences": stderr_div, "samples": samples}, indent=2))
