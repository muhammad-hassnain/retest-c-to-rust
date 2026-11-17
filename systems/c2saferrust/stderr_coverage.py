"""Count which of C2SaferRust's coreutils shell tests (the only ones its repair loop runs,
translate.py:239 skips non-.sh tests) check standard error.

A test "checks stderr" if it captures the utility's stderr in a file -- `2>FILE`, or
`>FILE 2>&1` (merged with stdout) -- and later inspects that FILE with `compare`, `grep`, `wc`,
`cmp`, `diff` or `test -s`. Redirections to /dev/null and `2>&1` inside `$(...)` helper lines are
ignored. (Audit 4: an earlier version only matched `compare ... err` and missed merged captures.)

  python3 -I systems/c2saferrust/stderr_coverage.py
"""
import glob
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
TESTS = os.path.join(REPO, "artifacts", "c2saferrust", "coreutils", "tests")

ERR_ONLY = re.compile(r"(?<![&\d])2>\s*([\w./-]+)")                 # 2>FILE
MERGED = re.compile(r"(?<![\d&])>\s*([\w./-]+)\s+2>&1")             # >FILE 2>&1
CHECKERS = {"compare", "cmp", "diff", "grep", "wc", "test"}
SEP = re.compile(r"\|\||&&|;|\|")


def segments(line):
    """Split a shell line into simple-command segments, remembering what follows each."""
    parts = SEP.split(line)
    return [(seg.strip(), parts[i + 1] if i + 1 < len(parts) else "") for i, seg in enumerate(parts)]


def words(seg):
    return re.findall(r"[\w./-]+", seg.replace("<", " "))


def captured_stderr_files(text, util):
    files = set()
    for line in text.splitlines():
        m = re.search(rf"\b{util}\b", line)
        if line.lstrip().startswith("#") or not m:
            continue
        if "$(" in line[:m.start()] and ")" not in line[line.index("$("):m.start()]:
            continue  # the utility runs inside a $(...) helper, not as the tested command
        if "gdb" in line or "--eval-command" in line:
            continue  # lines run under gdb as a precondition/skip probe, not the test itself
        for m in ERR_ONLY.finditer(line):
            files.add(m.group(1))
        for m in MERGED.finditer(line):
            files.add(m.group(1))
    files.discard("/dev/null")
    # follow derivations such as `sed ... errt > err`
    for _ in range(3):
        for line in text.splitlines():
            for seg, _n in segments(line):
                w = words(seg)
                m = re.search(r">\s*([\w./-]+)\s*$", seg)
                if m and any(f in w[:-1] for f in files):
                    files.add(m.group(1))
    files.discard("/dev/null")
    return files


def checked(text, files):
    """Files among `files` used as an argument of a checking command (not as a skip guard)."""
    hit = set()
    for line in text.splitlines():
        if "skip_" in line:
            continue
        for seg, _n in segments(line):
            w = words(seg)
            w = [x for x in w if x not in ("returns_", "1", "0")]
            if not w or w[0] not in CHECKERS:
                continue
            hit |= {f for f in files if f in w[1:]}
    return hit


out = {}
for prog in ["cat", "head", "tail", "uniq", "split"]:
    shs = sorted(glob.glob(os.path.join(TESTS, prog, "*.sh")))
    hits = {}
    for f in shs:
        text = open(f, errors="replace").read()
        c = sorted(checked(text, captured_stderr_files(text, prog)))
        if c:
            hits[os.path.basename(f)] = c
    others = sorted(os.path.basename(f) for f in glob.glob(os.path.join(TESTS, prog, "*"))
                    if not f.endswith(".sh"))
    out[prog] = {"sh_tests": len(shs), "check_stderr": len(hits), "which": hits,
                 "non_sh_tests_skipped_by_loop": others}
json.dump(out, open(os.path.join(REPO, "results", "c2saferrust_stderr_coverage.json"), "w"), indent=1)
for p, v in out.items():
    print(p, f"{v['check_stderr']}/{v['sh_tests']}", v["which"])
