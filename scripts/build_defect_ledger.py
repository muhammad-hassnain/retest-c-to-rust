#!/usr/bin/env python3
"""One defect ledger for every re-tested system: one unit (defect), one definition of "accepted".

UNITS
  defect       one distinct mechanism in a translation's source that makes it diverge from the C on
               UB-clean input: one code location or one coherent mistake, confirmed by reading the
               translated source. Inputs, flags, locales or configurations that hit the same mechanism
               are ONE defect. Faulty statements that each produce a divergence on their own inputs
               (and so can be observed separately) are separate defects, even inside one translation.
  translation  the unit the system ships (VERT: function; Flourine: function x model x configuration
               file; RustAssure: function; C2SaferRust: utility crate; Syzygy: library; RustMap:
               program).
  exposure     number of our held-out cases/inputs that show the defect (C2SaferRust also reports the
               count with the two locale variants folded).

ACCEPTANCE STATUS (per defect's translation; exactly one of)
  accepted-recorded  the released artifact records that the system's own check passed the translation
  accepted-rerun     no record, but the system's own check, re-run by us with its documented command
                     on its own inputs, passes it
  rejected           the record, or our re-run of the system's own check, fails it
  not-exercised      the check never runs on this unit
  no-record          shipped as output, no record of the check's verdict, no feasible faithful re-run
  Separately, `check_exercises_defect` says whether the check ever runs the defective behaviour
  (exercised-passed / exercised-failed / not-exercised / unknown), with evidence.

MODES
  probe   run inside the pinned C2SaferRust image (crust-heldout, gdb installed) against the built
          C oracle and rust_WIP binaries; records stdout/stderr/exit and gdb backtraces that confirm
          each C2SaferRust mechanism:
            docker exec <ctr> python3 /work/scripts/build_defect_ledger.py probe --root /w --out /w/probe.json
  build   (default) read the committed result files + artifacts (+ the probe JSON) and write
          results/defect_ledger.json:
            python3 -I scripts/build_defect_ledger.py build --probe <probe.json>
"""
import argparse
import base64
import collections
import csv
import datetime
import hashlib
import json
import os
import platform
import re
import struct
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
R = lambda *p: os.path.join(REPO, *p)


# ============================================================================================ PROBE
PROBES = [
    # id, util, argv, stdin bytes, gdb backtrace on rust?
    ("cat-meta", "cat", ["-v"], b"\x85\xa1\xff\n", False),
    ("head-two-files", "head", ["in1.txt", "in2.txt"], b"", False),
    ("head-two-files-bytes", "head", ["-c", "100", "in1.txt", "in2.txt"], b"", False),
    ("head-directory", "head", ["adir"], b"", False),
    ("head-n-minus-1", "head", ["-n", "-1"], b"a\nb\nc\n", False),
    ("head-q", "head", ["-q", "in1.txt"], b"", True),
    ("head-z", "head", ["-z", "in1.txt"], b"", True),
    ("tail-n-plus-2", "tail", ["-n", "+2"], b"l1\nl2\nl3\nl4\nl5\n", False),
    ("tail-c-3", "tail", ["-c", "3"], b"alpha\nbeta\ngamma\n", False),
    ("tail-n-minus-1", "tail", ["-n", "-1", "in1.txt"], b"", True),
    ("tail-n-abc", "tail", ["-n", "abc", "in1.txt"], b"", True),
    ("uniq-i-equal-length", "uniq", ["-i"], b"x\nx\n", False),
    ("uniq-i-equal-length-file", "uniq", ["-i", "xx.txt"], b"", True),
    ("uniq-i-unequal-length", "uniq", ["-i"], b"ab\nabc\n", False),
    ("uniq-directory", "uniq", ["adir"], b"", True),
]


def probe(root, out):
    import tempfile
    env = {"LC_ALL": "C", "LANG": "C", "PATH": "/usr/bin:/bin", "HOME": "/tmp", "RUST_BACKTRACE": "0"}
    work = tempfile.mkdtemp(prefix="ledger-probe-")
    with open(os.path.join(work, "in1.txt"), "w") as f:
        f.write("alpha\nbeta\n")
    with open(os.path.join(work, "in2.txt"), "w") as f:
        f.write("one\ntwo\nthree\n")
    with open(os.path.join(work, "xx.txt"), "w") as f:
        f.write("x\nx\n")
    os.makedirs(os.path.join(work, "adir"), exist_ok=True)

    def run(argv, stdin):
        p = subprocess.run(argv, input=stdin, cwd=work, env=env, capture_output=True, timeout=30)
        return {"exit": p.returncode, "stdout": p.stdout[:300].decode("latin1"), "stderr": p.stderr[:600].decode("latin1")}

    rows = []
    for pid, util, argv, stdin, bt in PROBES:
        cb = os.path.join(root, util, "c", util)
        rb = os.path.join(root, util, "rust_WIP", "target", "debug", util)
        row = {"id": pid, "command": " ".join([util] + argv), "stdin": stdin.decode("latin1"),
               "c": run([cb] + argv, stdin), "rust": run([rb] + argv, stdin)}
        if bt:
            sp = os.path.join(work, "stdin.bin")
            with open(sp, "wb") as f:
                f.write(stdin)
            import shlex
            g = subprocess.run(["gdb", "-q", "-batch", "-ex", f"run {' '.join(shlex.quote(a) for a in argv)} < {sp}", "-ex", "bt", rb],
                               cwd=work, env=env, capture_output=True, text=True, errors="replace", timeout=120)
            frames = [l for l in g.stdout.splitlines() if l.startswith("#") or "received signal" in l]
            row["rust_gdb_backtrace"] = [re.sub(r"0x[0-9a-f]{8,}", "0x…", l)[:300] for l in frames[:12]]
        rows.append(row)
    tools = {}
    for name, argv in (("gcc", ["gcc", "--version"]), ("rustc", ["rustc", "--version"]), ("gdb", ["gdb", "--version"])):
        try:
            tools[name] = subprocess.run(argv, capture_output=True, text=True).stdout.splitlines()[0]
        except Exception as e:  # noqa: BLE001
            tools[name] = f"unavailable: {e}"
    bins = {}
    for util in ["cat", "head", "tail", "uniq"]:
        for side, p in (("c", os.path.join(root, util, "c", util)),
                        ("rust_WIP", os.path.join(root, util, "rust_WIP", "target", "debug", util))):
            with open(p, "rb") as f:
                bins[f"{util}/{side}"] = hashlib.sha256(f.read()).hexdigest()
    res = {"provenance": {"script": "scripts/build_defect_ledger.py", "command": " ".join(sys.argv),
                          "host": platform.system(), "arch": platform.machine(), "toolchains": tools,
                          "binaries_sha256": bins, "date": datetime.datetime.now().isoformat(timespec="seconds"),
                          "build": "make (C, in <util>/c) and cargo build --bin <util> (rust_WIP, nightly-2022-08-08 per its rust-toolchain.toml), copies of artifacts/c2saferrust/coreutils/src/{cat,head,tail,uniq}"},
           "rows": rows}
    with open(out, "w") as f:
        json.dump(res, f, indent=1)
    print(f"wrote {out}: {len(rows)} probes")


def probe_bzip2(art, out):
    """RustMap bzip2, inside the image that produced results/rustmap_bzip2_check.json (crust-syzygy,
    linux/amd64): rebuild both sides as systems/rustmap/check_bzip2.sh does, regenerate its exact inputs
    (same LCG, same seeds), and record per input WHY RustMap's own check fails (CRC error vs silent
    corruption), which bytes change, the longest run after clamping bytes >= 0x80 to 0xFF, and whether
    the Rust .bz2 starts with 5000 zero bytes. Adds two text inputs that isolate the run-length fault."""
    import shutil
    import tempfile
    W = tempfile.mkdtemp(prefix="ledger-bz-")
    shutil.copytree(os.path.join(art, "rust-code/bzip2_rs_gpt"), os.path.join(W, "rs"), ignore=shutil.ignore_patterns("target"))
    shutil.copytree(os.path.join(art, "c-code/bzip2-1.0.8"), os.path.join(W, "c"))
    subprocess.run("make bzip2 bzip2recover", shell=True, cwd=os.path.join(W, "c"), capture_output=True, check=True)
    subprocess.run(["cargo", "build"], cwd=os.path.join(W, "rs"), capture_output=True, check=True)
    C, REC, RB = os.path.join(W, "c/bzip2"), os.path.join(W, "c/bzip2recover"), os.path.join(W, "rs/target/debug/bzip2_rs_gpt")

    def gen(kind, n, i):
        seed = (i * 2654435761 + 12345) & 0xffffffff
        b = bytearray()
        for _ in range(n):
            seed = (1103515245 * seed + 12345) & 0xffffffff
            v = (seed >> 16) & 0xff
            b.append(v if kind == "binary" else 33 + v % 94)
        return bytes(b)

    def maxrun(b):
        best = cur = 0
        prev = None
        for x in b:
            cur = cur + 1 if x == prev else 1
            prev = x
            best = max(best, cur)
        return best

    cases, i = [], 0
    for kind in ("binary", "text"):
        for sz in (1, 10, 20, 64, 100, 128, 512, 1000, 2048, 5000):
            i += 1
            cases.append((f"{kind} {sz} B (check_bzip2.sh seed {i})", gen(kind, sz, i)))
    cases += [("text 'xaaaaby' (run of 4)", b"xaaaaby"), ("text 'xaaaaaby' (run of 5)", b"xaaaaaby"),
              ("text 'ab\\x80cd' (one high byte)", b"ab\x80cd")]
    rows = []
    for k, (label, data) in enumerate(cases):
        d = os.path.join(W, f"p{k}")
        os.makedirs(d)
        with open(os.path.join(d, "data"), "wb") as f:
            f.write(data)
        rr = subprocess.run([RB, "--keep", "--force", "data"], cwd=d, capture_output=True, timeout=120)
        row = {"input": label, "size": len(data), "bytes_ge_0x80": sum(x >= 0x80 for x in data), "max_run": maxrun(data),
               "max_run_after_clamp": maxrun(bytes(0xFF if x >= 0x80 else x for x in data)), "rust_exit": rr.returncode,
               "rust_stderr": rr.stderr[-300:].decode("latin1")}
        bz = os.path.join(d, "data.bz2")
        if os.path.isfile(bz):
            with open(bz, "rb") as f:
                raw = f.read()
            row["rust_bz2_size"] = len(raw)
            row["starts_with_5000_zero_bytes"] = raw[:5000] == b"\0" * 5000 and len(raw) > 5000
            row["plain_bzip2_d_accepts_rust_output"] = subprocess.run([C, "-t", "data.bz2"], cwd=d, capture_output=True).returncode == 0
            subprocess.run([REC, "data.bz2"], cwd=d, capture_output=True)
            dec = subprocess.run([C, "-dc", "rec00001data.bz2"], cwd=d, capture_output=True)
            row["own_check_decompress_exit"] = dec.returncode
            row["own_check_decompress_stderr"] = dec.stderr[-200:].decode("latin1")
            row["own_check_crc_error"] = b"CRC" in dec.stderr or b"integrity" in dec.stderr
            row["own_check_output_equals_input"] = dec.returncode == 0 and dec.stdout == data
            if dec.returncode == 0 and len(dec.stdout) == len(data):
                diff = [(j, data[j], dec.stdout[j]) for j in range(len(data)) if data[j] != dec.stdout[j]]
                row["changed_bytes"] = len(diff)
                row["every_change_is_high_byte_to_0xFF"] = all(a >= 0x80 and b == 0xFF for _, a, b in diff)
        rows.append(row)
    tools = {n: subprocess.run(a, capture_output=True, text=True).stdout.splitlines()[0] for n, a in
             (("rustc", ["rustc", "--version"]), ("cargo", ["cargo", "--version"]), ("gcc", ["gcc", "--version"]))}
    res = {"provenance": {"script": "scripts/build_defect_ledger.py", "command": " ".join(sys.argv), "host": platform.system(),
                          "arch": platform.machine(), "toolchains": tools, "date": datetime.datetime.now().isoformat(timespec="seconds"),
                          "build": "copies of artifacts/rustmap/{rust-code/bzip2_rs_gpt (cargo build, debug, README 4.1.1), c-code/bzip2-1.0.8 (make bzip2 bzip2recover)}"},
           "rows": rows}
    with open(out, "w") as f:
        json.dump(res, f, indent=1)
    print(f"wrote {out}: {len(rows)} rows")


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "probe-bzip2":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode")
    ap.add_argument("--art", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    probe_bzip2(a.art, a.out)
    sys.exit(0)


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "probe":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode")
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    probe(a.root, a.out)
    sys.exit(0)


# ===================================================================================== CURATED DATA
# Mechanisms below were confirmed by reading the translated source (file:line given) and, for
# C2SaferRust, by the probe (gdb backtraces / panic messages). Every count is computed in build()
# from the result files; nothing numeric is curated except where marked "probe".
C2S = "artifacts/c2saferrust/coreutils/src"
C2SR_DEFECTS = [
    dict(id="C2SR-cat-1", util="cat", severity="S1",
         mechanism="cat -v/-A/-e/-t: the meta-character (byte >= 0x80) branch of the rewritten cat() writes 'M' and '-' but never advances bpout after '-', so the next character overwrites it; for bytes 0x80-0x9f it also writes ch+64 instead of ch-128+64 (two faults in one 20-line branch, always co-exposed). 0x85 renders as 'M^\\xc5' instead of 'M-^E'.",
         rust=(f"{C2S}/cat/rust_WIP/src/cat.rs", 769, 789, ["*bpout = '-' as i8;", "(ch + 64) as i8"]),
         c=(f"{C2S}/cat/c/cat.c", 427, 442, ["*bpout++ = '-';"]),
         fn="cat", chunk="3", probes=["cat-meta"],
         exercised=("not-exercised", "The selected cat tests (cat-E.sh, cat-proc.sh, cat-self.sh) run cat with no option or -E only; none passes -v/-A/-e/-t (artifacts/c2saferrust/coreutils/tests/cat/cat-E.sh:25,32,39; cat-proc.sh:33-34; cat-self.sh:24-51).")),
    dict(id="C2SR-head-1", util="head", severity="S1",
         mechanism="Multi-file headers: the rewritten write_header() prints '==> f <==' with Rust println! (Rust's own stdout buffer) while file contents go through C stdio fwrite_unlocked(stdout) (xwrite_stdout), so with two or more files all headers appear before any content. head -c N (written through Rust stdout too) is unaffected.",
         rust=(f"{C2S}/head/rust_WIP/src/head.rs", 627, 632, ["println!(\"{}==> {} <==\", prefix, filename);"]),
         c=(f"{C2S}/head/c/head.c", 167, 173, ["printf (\"%s==> %s <==\\n\""]),
         fn="write_header", chunk="root", probes=["head-two-files", "head-two-files-bytes"],
         exercised=("not-exercised", "No selected head test (head-c.sh, head-write-error.sh) passes two files (artifacts/c2saferrust/coreutils/tests/head/head-c.sh:25-53; head-write-error.sh:29-44).")),
    dict(id="C2SR-head-2", util="head", severity="S1",
         mechanism="Read errors become EOF: the rewritten safe_read() returns total_read (0) when read() fails instead of SAFE_READ_ERROR, so head on a directory prints nothing, reports no error and exits 0 (C: \"error reading 'adir': Is a directory\", exit 1).",
         rust=(f"{C2S}/head/rust_WIP/src/safe_read.rs", 14, 33, ["return total_read;"]),
         c=(f"{C2S}/head/c/safe-read.c", 55, 71, ["return result;"]),
         fn="safe_read", chunk="root", probes=["head-directory"],
         exercised=("not-exercised", "No selected head test reads a directory or an unreadable file (tests/head/head-c.sh, head-write-error.sh).")),
    dict(id="C2SR-head-3", util="head", severity="crash",
         mechanism="head -n -N (elide_tail_lines_pipe): the rewritten final-buffer block adds to desired_pos with checked '+=' although desired_pos starts at (uintmax_t)-1 for a non-seekable input (the C relies on unsigned wrap-around), so the debug build panics 'attempt to add with overflow' (head.rs:1253); the same block also advances p one byte per line instead of to the next line end.",
         rust=(f"{C2S}/head/rust_WIP/src/head.rs", 1244, 1255, ["desired_pos += p.as_ptr().offset_from"]),
         c=(f"{C2S}/head/c/head.c", 607, 622, ["desired_pos += p - tmp->buffer;"]),
         fn="elide_tail_lines_pipe", chunk="1", probes=["head-n-minus-1"],
         exercised=("not-exercised", "head-write-error.sh:39 runs `yes | head --lines=-N > /dev/full`, which exits on the first write error before reaching the end-of-input block; no selected test reaches it.")),
    dict(id="C2SR-head-4", util="head", severity="crash",
         mechanism="Option parsing: the rewritten getopt loop in main_0 builds CStr::from_ptr(optarg) before dispatching on the option, so any option without an argument (-q, -z, -v) dereferences a NULL optarg and segfaults (gdb: CStr::from_ptr(ptr=0x0) at head.rs:1920).",
         rust=(f"{C2S}/head/rust_WIP/src/head.rs", 1920, 1920, ["CStr::from_ptr(optarg)"]),
         c=(f"{C2S}/head/c/head.c", 1050, 1060, ["case 'q':"]),
         fn="main_0", chunk="1", probes=["head-q", "head-z"],
         exercised=("not-exercised", "No selected head test passes -q, -z or -v (tests/head/head-c.sh, head-write-error.sh).")),
    dict(id="C2SR-tail-1", util="tail", severity="S1",
         mechanism="tail -c N / -n +N: the rewritten -c/-n handler in parse_options binds the parsed mode to new locals (is_count_lines, is_from_start) and never assigns the globals count_lines and from_start, so -c N is executed as -n N (lines, not bytes) and +N (from start) as N (from end). tail -c 3 on 'alpha\\nbeta\\ngamma\\n' prints all three lines; tail -n +2 on 5 lines prints the last 2.",
         rust=(f"{C2S}/tail/rust_WIP/src/tail.rs", 4650, 4660, ["let is_count_lines = c == 'n' as i32;", "let is_from_start = match"]),
         c=(f"{C2S}/tail/c/tail.c", 2213, 2217, ["count_lines = (c == 'n');"]),
         fn="parse_options", chunk="0", probes=["tail-c-3", "tail-n-plus-2"],
         exercised=("exercised-failed", "big-4gb.sh (selected: passes on the C2Rust starting tree) runs `tail -c1 big` (artifacts/c2saferrust/coreutils/tests/tail/big-4gb.sh:41); the shipped tree fails it in 3 of 3 runs (results/c2saferrust_acceptance_suite.json), printing the start of the file, which is this defect (-c1 executed as -n1 on a file with no newline).")),
    dict(id="C2SR-tail-2", util="tail", severity="crash",
         mechanism="tail -n -N / -c -N: the rewritten handler strips a leading '-' into optarg but passes the unstripped optarg_str to xdectoumax, so '-1' is rejected as an invalid number; the error path then crashes (C2SR-tail-3) instead of tail printing the last N lines.",
         rust=(f"{C2S}/tail/rust_WIP/src/tail.rs", 4653, 4663, ["optarg = optarg_str[1..].as_ptr()", "optarg_str.as_ptr() as *const i8"]),
         c=(f"{C2S}/tail/c/tail.c", 2216, 2217, ["++optarg;"]),
         fn="parse_options", chunk="0", probes=["tail-n-minus-1"],
         exercised=("not-exercised", "No selected tail test (big-4gb.sh, inotify-hash-abuse2.sh, proc-ksyms.sh, start-middle.sh) passes a negative count.")),
    dict(id="C2SR-tail-3", util="tail", severity="crash",
         mechanism="quotearg (tail's copy): gettext_quote was rewritten to return a String, and its rewritten call sites in quotearg_buffer_restyled (locale/clocale quoting styles) declare new `let left_quote`/`let right_quote` inside the if-block, shadowing the outer variables, which stay NULL; *quote_string then dereferences NULL. Any error message that quotes its argument with quote() crashes (tail -n abc: C prints \"invalid number of lines: 'abc'\" and exits 1; Rust segfaults). In our grid it is reached only through C2SR-tail-2, so its grid exposure equals tail-2's.",
         rust=(f"{C2S}/tail/rust_WIP/src/quotearg.rs", 376, 377, ["let left_quote = gettext_quote(", "let right_quote = gettext_quote("]),
         c=(f"{C2S}/tail/c/quotearg.c", 352, 356, ["left_quote = gettext_quote (N_(\"`\"), quoting_style);"]),
         fn="gettext_quote", chunk="root", callsite=True, probes=["tail-n-minus-1", "tail-n-abc"],
         exercised=("not-exercised", "No selected tail test triggers an invalid-number (or other quote()-ing) diagnostic.")),
    dict(id="C2SR-uniq-1", util="uniq", severity="crash",
         mechanism="uniq -i: memcasecmp was rewritten as a Rust-ABI fn taking two slices (memcasecmp.rs:9) but uniq.rs still declares and calls it as extern \"C\" (ptr, ptr, len) (uniq.rs:149-153, call at 840-844); the callee reads the C arguments as slice (ptr,len) pairs and dereferences garbage. different() calls it only when two adjacent lines have equal length, and the callee then dereferences that length as an address, so -i crashes on inputs with two adjacent non-empty lines of equal length (gdb: memcasecmp.rs:13 <- different uniq.rs:840); for two adjacent empty lines it returned without crashing in every grid case (see meta.C2SaferRust.uniq_i_prediction).",
         rust=(f"{C2S}/uniq/rust_WIP/src/memcasecmp.rs", 9, 21, ["pub fn memcasecmp(vs1: &[u8], vs2: &[u8])"]),
         c=(f"{C2S}/uniq/c/uniq.c", 288, 288, ["memcasecmp (old, new, oldlen)"]),
         fn="memcasecmp", chunk="root", probes=["uniq-i-equal-length", "uniq-i-equal-length-file", "uniq-i-unequal-length"],
         exercised=("not-exercised", "uniq -i is tested only by uniq.pl, and translate.py's select_tests keeps only *.sh tests (artifacts/c2saferrust/translate.py:239); the selected uniq-collate.sh and uniq-perf.sh never pass -i.")),
    dict(id="C2SR-uniq-2", util="uniq", severity="crash",
         mechanism="uniq read-error path: quotearg_style was rewritten as a Rust-ABI fn (s, arg: &str) -> String (quotearg.rs:1445-1453) but uniq.rs declares and calls it as extern \"C\" (int, char*) -> char* (uniq.rs:105, call at 1190); on any read error (e.g. a directory operand) the call passes C arguments to a Rust-ABI callee and crashes in CString::new (gdb: quotearg.rs:1450 <- check_file uniq.rs:1190). C: \"error reading 'adir': Is a directory\", exit 1.",
         rust=(f"{C2S}/uniq/rust_WIP/src/quotearg.rs", 1446, 1453, ["pub fn quotearg_style(", "arg: &str,"]),
         c=(f"{C2S}/uniq/c/uniq.c", 450, 451, ["error (EXIT_FAILURE, errno, _(\"error reading %s\"), quoteaf (infile));"]),
         fn="quotearg_style", chunk="root", probes=["uniq-directory"],
         exercised=("not-exercised", "Neither selected uniq test (uniq-collate.sh, uniq-perf.sh) triggers a read error.")),
]


def c2sr_assign(util, v, c):
    """Map one stdout/exit mismatch to the defect id(s) that produce it (asserted in build)."""
    e = v["sut_exit"]
    kind = "crash-segv" if e in (-11, 139) else "crash-panic" if e == 101 else "S1"
    a, note = v["argv"], c["note"]
    if util == "cat":
        assert kind == "S1" and any(f in a for f in ("-v", "-A", "-e", "-t")), (v["id"], a)
        return ["C2SR-cat-1"], kind
    if util == "head":
        if kind == "S1" and note == "two-files":
            assert not v["stdout_match"] and v["exit_match"], v["id"]
            return ["C2SR-head-1"], kind
        if kind == "S1" and note == "is-a-directory":
            assert v["stdout_match"] and v["oracle_exit"] == 1 and e == 0, v["id"]
            return ["C2SR-head-2"], kind
        if kind == "crash-panic":
            assert "-n" in a and a[a.index("-n") + 1].startswith("-"), a
            return ["C2SR-head-3"], kind
        if kind == "crash-segv":
            assert "-q" in a or "-z" in a, a
            return ["C2SR-head-4"], kind
    if util == "tail":
        if kind == "S1":
            assert "-c" in a or any(x.startswith("+") for x in a), a
            return ["C2SR-tail-1"], kind
        if kind == "crash-segv":
            assert any(x.startswith("-") and x[1:].isdigit() for x in a), a
            return ["C2SR-tail-2", "C2SR-tail-3"], kind
    if util == "uniq" and kind == "crash-segv":
        if note == "is-a-directory":
            return ["C2SR-uniq-2"], kind
        assert "-i" in a, a
        return ["C2SR-uniq-1"], kind
    raise AssertionError(f"unassigned {util} case {v['id']} {a} {note} {kind}")


FL = "artifacts/flourine_dl/flourine-artifact"
FLOURINE_DEFECTS = [
    dict(id="FLOURINE-operator_list-1", benchmark="opl/opl_emu_registers_operator_list", file="results/flourine_diff.json", degenerate=False,
         mechanism="Byte packing o1 | o2<<8 | o3<<16 | o4<<24 became o1 + o2.wrapping_shl(8) + o3.wrapping_shl(16) + o4.wrapping_shl(24) on u8 values; wrapping_shl masks the shift to 8 bits' width (8,16,24 -> 0), so the four bytes are summed: (24,211,56,46) -> 337 instead of 775,475,992. The two configurations ship byte-identical files.",
         rust_rel="opl_emu_registers_operator_list.rs", rust_lines=(6, 9), rust_needle="wrapping_shl(8)",
         c=(f"{FL}/bms/c/opl/opl_emu_registers_operator_list/opl_emu_registers_operator_list.c", 18, "o1 | (o2 << 8)"),
         note="Possible exposure overcount (not judged here): the C shifts an int-promoted uint8_t by 24, which is signed overflow (UB) when o4 >= 128; results/flourine_diff.json keeps only samples, so the UB-clean share of the 1998 diverging inputs per file is not recorded."),
    dict(id="FLOURINE-lfo_pm_depth-1", benchmark="opl/opl_emu_registers_lfo_pm_depth", file="results/flourine_struct.json", degenerate=False,
         mechanism="Bitfield mask (1 << length) - 1 written as (1u32 >> length) - 1 in opl_emu_bitfield; for length 1 this is 0 - 1, which panics ('attempt to subtract with overflow') on every call in FLOURINE's own build profile (overflow checks on). Built with rustc -O instead, it wraps to 0xFFFFFFFF and the accessor returns all bits above `start` instead of one bit (a wrong value on 4 of our 8 inputs).",
         rust_rel="opl_emu_registers_lfo_pm_depth.rs", rust_lines=(17, 19), rust_needle="1u32.shr(length as u32)",
         c=(f"{FL}/bms/c/opl/opl_emu_registers_lfo_pm_depth/opl_emu_registers_lfo_pm_depth.c", 35, "((1 << length) - 1)"),
         panic="attempt to subtract with overflow",
         note="Wrong output (S1) in the submitted version, whose driver built the Rust with rustc -O; a crash in FLOURINE's own build profile (results/arxiv_flourine_devprofile.json)."),
    # arXiv (decided 2026-11-16): found by re-running FLOURINE in its own build profile; both agree
    # with the C on every input of ours under rustc -O (results/arxiv_flourine_devprofile.json).
    dict(id="FLOURINE-lfo_am_depth-1", benchmark="opl/opl_emu_registers_lfo_am_depth", file=None, degenerate=False,
         mechanism="Same bitfield helper as lfo_pm_depth: the mask (1 << length) - 1 is written as (1u32 >> length) - 1, and the accessor reads one bit (start 7, length 1), so 0 - 1 panics ('attempt to subtract with overflow') on every call in FLOURINE's own build profile (overflow checks on). Under rustc -O the wrapped all-ones mask still selects the right bit, so the release build agrees with the C.",
         rust_rel="opl_emu_registers_lfo_am_depth.rs", rust_lines=(17, 19), rust_needle="1u32.shr(length as u32)",
         c=(f"{FL}/bms/c/opl/opl_emu_registers_lfo_am_depth/opl_emu_registers_lfo_am_depth.c", 35, "((1 << length) - 1)"),
         panic="attempt to subtract with overflow",
         note="Exposed only in FLOURINE's own build profile (overflow checks on); agrees with the C on all 8 inputs under rustc -O."),
    dict(id="FLOURINE-compute_phase_step-1", benchmark="opl/opl_emu_opl_compute_phase_step", file=None, degenerate=False,
         mechanism="The C's unsigned multiplications wrap, which C defines: lfo_raw_pm * opl_emu_bitfield(block_freq, 7, 3) (int32 times uint32, so unsigned) and phase_step * multiple (uint32). The translation writes both as plain u32 `*` (it uses wrapping_add for the add next to the first), so in FLOURINE's own build profile (overflow checks on) they panic ('attempt to multiply with overflow'): the first for any negative lfo_raw_pm with nonzero bits 7-9 of block_freq, e.g. (16383, 1, -1), the second when phase_step * multiple exceeds 32 bits. Under rustc -O both wrap and the release build agrees with the C. One coherent mistake (wrapping unsigned multiplies made checked); the two gpt4 files are byte-identical.",
         rust_rel="opl_emu_opl_compute_phase_step.rs", rust_lines=(8, 11), rust_needle=["(lfo_raw_pm as u32) * opl_emu_bitfield(block_freq, 7, 3)", "(phase_step * multiple) >> 1"],
         c=(f"{FL}/bms/c/opl/opl_emu_opl_compute_phase_step/opl_emu_opl_compute_phase_step.c", 32, 42, ["lfo_raw_pm * opl_emu_bitfield(block_freq, 7, 3)", "phase_step * multiple"]),
         panic="attempt to multiply with overflow",
         note="Exposed only in FLOURINE's own build profile (overflow checks on); agrees with the C on all 2000 inputs under rustc -O."),
    dict(id="FLOURINE-clip-1", benchmark="libopenaptx/clip", file="results/flourine_diff.json", degenerate=True,
         mechanism="clip(a, amin, amax): the Rust match returns a when a == amin without the C's second test a > amax, so when amin > amax and a == amin it returns a instead of amax (the two configurations are written differently but share this arm).",
         rust_rel="clip.rs", rust_lines=None, rust_needle=["Ordering::Equal | Ordering::Greater => a,", "Ordering::Equal => a,"],
         c=(f"{FL}/bms/c/libopenaptx/clip/clip.c", 16, "if      (a < amin) return amin;"), note="Diverges only on a degenerate min > max range."),
    dict(id="FLOURINE-opl_emu_clamp-1", benchmark="opl/opl_emu_clamp", file="results/flourine_diff.json", degenerate=True,
         mechanism="opl_emu_clamp: same arm as clip; value == minval returns value without testing value > maxval, so with minval > maxval it returns value instead of maxval.",
         rust_rel="opl_emu_clamp.rs", rust_lines=None, rust_needle="Ordering::Equal | Ordering::Greater => value",
         c=(f"{FL}/bms/c/opl/opl_emu_clamp/opl_emu_clamp.c", 18, "if (value < minval)"), note="Diverges only on a degenerate min > max range."),
]

RA = "artifacts/rustassure/src"
RUSTASSURE_RERUN = {
    "what": "Exploratory re-run of RustAssure's own per-function differential symbolic check (python/performSymbolExecution.py backend, GPT-4o map 2) on GPT-4o's csv_parse, by a helper agent on the Linux box (~/work/defects-ra); NOT produced by a repo script, so it is evidence, not a verdict.",
    "deviations_from_documented_command": [
        "KLEE --max-time 300 s for C and Rust instead of the documented 7200 s / 10800 s (python/symbolicExecution.py:172,255)",
        "KLEE fork davsec-lab/rustify-klee at HEAD d880ceb4: the artifact's last pinned KLEE commit 43d729cc lacks the --target-function-name option the pipeline passes (python/symbolicExecution.py:173,256)",
        "nlohmann json.hpp fetched at the pinned submodule commit (the submodule is empty in the artifact); LLVM 14, rustc 1.64.0, rustfilt 0.2.1 installed from the internet"],
    "outcome": "5 of 10 compared values have edit distance 0; pstate, quoted, entry_pos, the return value and data are nonzero or unmatched, so the run does not report csv_parse equivalent. None of the differences involves the field callback.",
    "callback_visibility": "The check cannot observe callback arguments: Symbolizer/Pass/SymbolizerPass.cpp:1125-1265 (convert_function_calls, invoked at :1621) replaces every call through a function pointer with a no-op dummy that drops its arguments, and :798-800 skips function-valued arguments when printing outputs. In the run, KLEE reached the CSV_EMPTY_IS_NULL branch on both sides (C cb1(NULL,...) 18 times, Rust cb1(&[],...) 15 times) and the arguments went to the dummies.",
}
RUSTASSURE_DEFECTS = [
    dict(id="RUSTASSURE-libcsv-csv_parse-1", translation="libcsv csv_parse (GPT-4o, run 2025-07-02, composed in src/Link/csv_link)", severity="S1",
         mechanism="With CSV_EMPTY_IS_NULL, the C passes the field callback a NULL pointer for an empty unquoted field; GPT-4o's csv_parse changed the callback parameter from nullable void* to non-nullable &[u8] and passes an empty slice, so 'a,,b' and 'a,\"\",b' become indistinguishable to the callback.",
         rust=(f"{RA}/python/inputs-complex/libcsv/link/individual-funcs_gpt-4o_2025-07-02_14-41-02__complete/csv_parse.rs", 18, 28),
         c=(f"{RA}/python/inputs-complex/libcsv/libcsv.c", None, "csv_parse("),
         results="systems/rustassure/libcsv_e2e/results.json",
         acceptance_status="no-record",
         acceptance_evidence=["No per-function verdict for GPT-4o csv_parse exists in artifacts/rustassure (working tree or git history): no KLEE output, edit-distance table or result.csv for it; the libcsv validator log for the 2025-07-02 run is a translation log only (src/Link/csv_link/source/individual-funcs_gpt-4o_2025-07-02_14-41-02__complete/libcsv_validator.log).",
                              "python/performSymbolExecution.py:61 evaluates the 2024-09-17 libcsv run (in which csv_parse.rs never compiled to bitcode); the 2025-07-02 run used by the composed crate is commented out at :63.",
                              "src/bug_description.csv lists six libcsv bugs (lines 2-7: csv_write2, csv_set_blk_size, csv_set_delim, csv_free); none covers csv_parse or CSV_EMPTY_IS_NULL.",
                              "A faithful re-run with the documented budgets was not done; see rerun_probe."],
         exercised=("not-exercised", "Structural: the per-function check replaces calls through function pointers with argument-dropping dummies, so the callback argument is never compared (see rerun_probe.callback_visibility).")),
    dict(id="RUSTASSURE-u8c-u8next_-1", translation="u8c u8next_ (GPT-4o, composed in src/Link/u8c_link)", severity="S1",
         mechanism="On invalid UTF-8 the C's FSM invalid state resets val = first (the original first byte) before storing it to *ch; the Rust u8next_ leaves every invalid exit with the partially masked val, so *ch holds e.g. 0 instead of 0xE0 for an overlong E0 80 80.",
         rust=(f"{RA}/Link/u8c_link/src/main.rs", 77, 315),
         c=(f"{RA}/python/inputs-complex/u8c/u8c.c", 142, "val = first"),
         results="systems/rustassure/u8c_e2e/results.json",
         acceptance_status="rejected",
         acceptance_evidence=["RustAssure's own bug list records this bug: src/bug_description.csv lines 12-15 ('u8c,GPT-4o,u8next_': invalid UTF-8 such as 0xC2 0x41, C returns the first byte, Rust the masked value); line 17 repeats it for GPT-4o-mini.",
                              "No per-function KLEE verdict for u8next_ is shipped."],
         exercised=("exercised-failed", "The bug list attributes it to RustAssure's evaluation (src/bug_description.csv:12-15).")),
]

SYZ = "artifacts/syzygy_zopfli"
SYZYGY_DEFECT = dict(
    id="SYZYGY-zopfli-ZopfliDeflate-1", severity="S1",
    mechanism="ZopfliDeflate's master-block loop is a do-while in the C (always emits a final block); the Rust zopfli_deflate uses `while i < insize`, so for empty input it emits zero bytes, which zlib rejects (the C emits a valid empty deflate stream, e.g. 03 00).",
    rust=(f"{SYZ}/rust_code/src/main.rs", 7017, 7017, "while i < insize {"),
    c=(f"{SYZ}/c_code/zopfli.c", 3449, 3458, "do"),
    acceptance_status="no-record",
    acceptance_evidence=["The artifact ships no verdict, log or test inputs for its own check: run_equiv.py compares a C and a Rust driver on N_TESTS generated strings in TEST_DIR (README: 'Create NUM_TESTS tests in the TEST_DIR'), and none are shipped (artifacts/syzygy_zopfli: README.md, run_equiv.py, custom_multi.py only).",
                         "A faithful re-run is not possible: the inputs are not shipped, and the shipped C driver calls strlen on a buffer it never NUL-terminates (artifacts/syzygy_zopfli/c_code/zopfli.c:3483,3504), so it cannot serve as the oracle."],
    exercised=("unknown", "Whether Syzygy's random strings include the empty string is not recorded."))


# ---- VERT and RustMap mechanisms: read from source (C and Rust side by side, hand-evaluated on a diverging
# sample, and emulated over the exact grid by a helper; locations re-checked by build()).
CURATED_VERT = [{'func': 'PROGRAM_TO_CHECK_IF_A_GIVEN_NUMBER_IS_LUCKY_ALL_DIGITS_ARE_DIFFERENT',
  'family': 'scalar',
  'mistake_class': 'f32 used for integer arithmetic (non-truncating division)',
  'mechanisms': [{'mechanism': 'Integer digit loop rendered in f32 (`let mut n = n as f32; ... n /= 10.0`): float division does not truncate, so after the '
                               'integer digits are consumed n becomes 0.1, 0.01, ... whose extracted digit is 0 twice, and the function returns 0 (false) for '
                               'every n > 0.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/PROGRAM_TO_CHECK_IF_A_GIVEN_NUMBER_IS_LUCKY_ALL_DIGITS_ARE_DIFFERENT/out-rwasm-bolero/src/main.rs:617-624',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/PROGRAM_TO_CHECK_IF_A_GIVEN_NUMBER_IS_LUCKY_ALL_DIGITS_ARE_DIFFERENT/PROGRAM_TO_CHECK_IF_A_GIVEN_NUMBER_IS_LUCKY_ALL_DIGITS_ARE_DIFFERENT.c:30'}],
  'triage_note': None},
 {'func': 'MAXIMUM_GAMES_PLAYED_WINNER',
  'family': 'scalar',
  'mistake_class': 'do-while / post-increment loop control mistranslated (off-by-one)',
  'mechanisms': [{'mechanism': 'C do-while with post-increment in its test (`do { dp[i] = dp[i-1]+dp[i-2]; } while (dp[i++] <= N);`) rendered as a pre-test '
                               '`while dp[i-1]+dp[i-2] <= N { ...; i += 1 }`: C increments i also on the final failing test, Rust does not, so the returned '
                               'i-2 is one smaller for every N.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/MAXIMUM_GAMES_PLAYED_WINNER/out-rwasm-bolero/src/main.rs:594-597',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/MAXIMUM_GAMES_PLAYED_WINNER/MAXIMUM_GAMES_PLAYED_WINNER.c:26-29'}],
  'triage_note': 'none for the 77 value inputs (emulated C stays in bounds for N>=4). The 3 ub inputs (N=0,1,3) are VLA dp[N] writes past the end in the C, '
                 'already filtered.'},
 {'func': 'COUNT_STRINGS_CAN_FORMED_USING_B_C_GIVEN_CONSTRAINTS_1',
  'family': 'scalar',
  'mistake_class': 'wrong closed-form formula',
  'mechanisms': [{'mechanism': 'Wrong closed form: Rust adds n*n*(n-1)/2 (`n2 * (n - 1.0) / 2.0` with n2 = n*n) where C adds n*(n*n-1)/2, i.e. n^3-n^2 instead '
                               'of n^3-n; wrong for every n>=2.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/COUNT_STRINGS_CAN_FORMED_USING_B_C_GIVEN_CONSTRAINTS_1/out-rwasm-bolero/src/main.rs:424-426',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/COUNT_STRINGS_CAN_FORMED_USING_B_C_GIVEN_CONSTRAINTS_1/COUNT_STRINGS_CAN_FORMED_USING_B_C_GIVEN_CONSTRAINTS_1.c:22'}],
  'triage_note': None},
 {'func': 'SUM_SERIES_12_32_52_2N_12_1',
  'family': 'scalar',
  'mistake_class': 'f32 used for integer arithmetic (precision loss above 2^24)',
  'mechanisms': [{'mechanism': 'Exact integer formula n(2n-1)(2n+1)/3 evaluated in f32 and truncated with `as i32`: once the product exceeds 2^24 (first '
                               'divergence at n=163) f32 rounding drops low bits and the result is off by one.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/SUM_SERIES_12_32_52_2N_12_1/out-rwasm-bolero/src/main.rs:422-426',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/SUM_SERIES_12_32_52_2N_12_1/SUM_SERIES_12_32_52_2N_12_1.c:22'}],
  'triage_note': 'none (C product <= 4.2e8 < INT_MAX on the whole grid).'},
 {'func': 'SUM_LARGEST_PRIME_FACTOR_NUMBER_LESS_EQUAL_N',
  'family': 'scalar',
  'mistake_class': 'loop stride dropped (step-1 range)',
  'mechanisms': [{'mechanism': 'Sieve inner loop lost its stride: C `for (i = p*2; i <= n; i += p)` became `for i in (p * 2)..=n` (step 1), so every index >= '
                               '2p is overwritten with p rather than only the multiples of p.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/SUM_LARGEST_PRIME_FACTOR_NUMBER_LESS_EQUAL_N/out-rwasm-bolero/src/main.rs:1085-1087',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/SUM_LARGEST_PRIME_FACTOR_NUMBER_LESS_EQUAL_N/SUM_LARGEST_PRIME_FACTOR_NUMBER_LESS_EQUAL_N.c:29-31'}],
  'triage_note': None},
 {'func': 'NEWMAN_CONWAY_SEQUENCE_1',
  'family': 'scalar',
  'mistake_class': 'index arithmetic off-by-one',
  'mechanisms': [{'mechanism': 'Spurious -1 in an index: C `f[f[i-1]]` became `f[(f[i-1] - 1)]`, so the recurrence reads the wrong term (Rust returns 1 for '
                               'every n>=3).',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/NEWMAN_CONWAY_SEQUENCE_1/out-rwasm-bolero/src/main.rs:657',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/NEWMAN_CONWAY_SEQUENCE_1/NEWMAN_CONWAY_SEQUENCE_1.c:29'}],
  'triage_note': 'none for the 78 value inputs; the 2 ub inputs (n=0,1) are the C writing f[1]/f[2] past a VLA of size n+1, already filtered.'},
 {'func': 'RECURSIVELY_BREAK_NUMBER_3_PARTS_GET_MAXIMUM_SUM_1',
  'family': 'scalar',
  'mistake_class': 'max/min call restructured (method-call precedence)',
  'mechanisms': [{'mechanism': 'Method-call precedence: C `max(dp[i/2] + dp[i/3] + dp[i/4], i)` became `dp[i/2] + dp[i/3] + dp[i/4].max(i)`, so max applies '
                               'only to dp[i/4] and the sum is inflated.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/RECURSIVELY_BREAK_NUMBER_3_PARTS_GET_MAXIMUM_SUM_1/out-rwasm-bolero/src/main.rs:759',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/RECURSIVELY_BREAK_NUMBER_3_PARTS_GET_MAXIMUM_SUM_1/RECURSIVELY_BREAK_NUMBER_3_PARTS_GET_MAXIMUM_SUM_1.c:26'}],
  'triage_note': 'none for value inputs; the 1 ub input (n=0) is the C writing dp[1] past a VLA of size 1, already filtered.'},
 {'func': 'NTH_PENTAGONAL_NUMBER',
  'family': 'scalar',
  'mistake_class': 'operator precedence (dropped parentheses)',
  'mechanisms': [{'mechanism': 'Missing parentheses: C `(3*n*n - n) / 2` became `3 * n * n - n / 2`, i.e. 3n^2 - floor(n/2).',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/NTH_PENTAGONAL_NUMBER/out-rwasm-bolero/src/main.rs:398',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/NTH_PENTAGONAL_NUMBER/NTH_PENTAGONAL_NUMBER.c:22'}],
  'triage_note': None},
 {'func': 'HEXAGONAL_NUMBER',
  'family': 'scalar',
  'mistake_class': 'wrong closed-form formula',
  'mechanisms': [{'mechanism': 'Wrong formula: C returns n*(2n-1); Rust sums (2i-1) for i in 1..=n, which is n^2.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/HEXAGONAL_NUMBER/out-rwasm-bolero/src/main.rs:393-395',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/HEXAGONAL_NUMBER/HEXAGONAL_NUMBER.c:22'}],
  'triage_note': None},
 {'func': 'NTH_NON_FIBONACCI_NUMBER',
  'family': 'scalar',
  'mistake_class': 'sign error',
  'mechanisms': [{'mechanism': 'Sign flip: C `n = n - (curr - prev - 1)` became `n = n + ((curr - prev - 1.0) as i32)`, so n never decreases; the loop runs '
                               'until i32 n wraps negative (43 iterations, release-mode wrapping) and returns the input-independent constant 1647462400.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/NTH_NON_FIBONACCI_NUMBER/out-rwasm-bolero/src/main.rs:538',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/NTH_NON_FIBONACCI_NUMBER/NTH_NON_FIBONACCI_NUMBER.c:27'}],
  'triage_note': 'none (the C terminates after a few iterations with no overflow; the constant Rust value comes from Rust-side i32 wraparound in the -O build, '
                 'which would be an overflow panic in a debug build -- a divergence either way).'},
 {'func': 'WAYS_TO_WRITE_N_AS_SUM_OF_TWO_OR_MORE_POSITIVE_INTEGERS',
  'family': 'scalar',
  'mistake_class': 'loop bound off-by-one (inclusive vs exclusive)',
  'mechanisms': [{'mechanism': 'Outer loop bound made inclusive: C `for (i = 1; i < n; i++)` became `for i in 1..=n`, so the one-part partition {n} is also '
                               'counted and the result is C+1 for every n>=1.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/WAYS_TO_WRITE_N_AS_SUM_OF_TWO_OR_MORE_POSITIVE_INTEGERS/out-rwasm-bolero/src/main.rs:924',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/WAYS_TO_WRITE_N_AS_SUM_OF_TWO_OR_MORE_POSITIVE_INTEGERS/WAYS_TO_WRITE_N_AS_SUM_OF_TWO_OR_MORE_POSITIVE_INTEGERS.c:25-26'}],
  'triage_note': 'none for the 34 value inputs (n<=120). The 45 ub inputs (n>=124 on the grid) are signed-int overflow of partition counts in the C (p(122) > '
                 '2^31), already filtered.'},
 {'func': 'MINIMUM_TIME_WRITE_CHARACTERS_USING_INSERT_DELETE_COPY_OPERATION',
  'family': 'scalar',
  'mistake_class': 'max/min call restructured (method-call precedence)',
  'mechanisms': [{'mechanism': 'Method-call precedence in both DP branches: C `min(dp[i-1] + insert, dp[i/2] + copy)` (and the odd-i `+ remove` variant) '
                               'became `dp[i-1] + insert.min(dp[i/2] + copy)`, i.e. dp[i-1] + min(insert, ...), so the copy option can never replace the '
                               'insert.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/MINIMUM_TIME_WRITE_CHARACTERS_USING_INSERT_DELETE_COPY_OPERATION/out-rwasm-bolero/src/main.rs:1255-1256',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/MINIMUM_TIME_WRITE_CHARACTERS_USING_INSERT_DELETE_COPY_OPERATION/MINIMUM_TIME_WRITE_CHARACTERS_USING_INSERT_DELETE_COPY_OPERATION.c:29-30'}],
  'triage_note': 'none (C dp values stay far below INT_MAX on the grid).'},
 {'func': 'MAXIMUM_NUMBER_SEGMENTS_LENGTHS_B_C',
  'family': 'scalar',
  'mistake_class': 'max/min argument restructured + missing bounds guard',
  'mechanisms': [{'mechanism': '`+1` moved to the wrong operand: C `dp[i+a] = max(dp[i] + 1, dp[i+a])` became `dp[(i+a)] = dp[i].max(dp[(i+a)] + 1)` (same for '
                               'b and c), so a reachable length is credited dp[i] instead of dp[i]+1 and repeated relaxations add spurious +1s.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/MAXIMUM_NUMBER_SEGMENTS_LENGTHS_B_C/out-rwasm-bolero/src/main.rs:1402-1404',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/MAXIMUM_NUMBER_SEGMENTS_LENGTHS_B_C/MAXIMUM_NUMBER_SEGMENTS_LENGTHS_B_C.c:29-31'},
                 {'mechanism': 'Bounds guards dropped: C only updates when `i + a <= n` (and likewise b, c); Rust indexes dp[(i + a) as usize] '
                               'unconditionally, so any segment length that overshoots n panics with index-out-of-bounds where the C skips it.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/MAXIMUM_NUMBER_SEGMENTS_LENGTHS_B_C/out-rwasm-bolero/src/main.rs:1402-1404',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/MAXIMUM_NUMBER_SEGMENTS_LENGTHS_B_C/MAXIMUM_NUMBER_SEGMENTS_LENGTHS_B_C.c:29-31'}],
  'triage_note': "none. Note the row's value_div is only 4 because 64 of the 80 inputs are absorbed by the second mechanism (Rust panic before a value is "
                 'produced).'},
 {'func': 'DIFFERENT_WAYS_SUM_N_USING_NUMBERS_GREATER_EQUAL_M',
  'family': 'scalar',
  'mistake_class': 'reversed range bounds `(hi..=lo).rev()`',
  'mechanisms': [{'mechanism': 'Countdown loop bounds swapped: C `for (k = n; k >= m; k--)` became `for k in (n..=m).rev()`, an empty range whenever n > m, so '
                               'the DP table is never filled and dp[n][m] = 0 is returned.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/DIFFERENT_WAYS_SUM_N_USING_NUMBERS_GREATER_EQUAL_M/out-rwasm-bolero/src/main.rs:1241',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/DIFFERENT_WAYS_SUM_N_USING_NUMBERS_GREATER_EQUAL_M/DIFFERENT_WAYS_SUM_N_USING_NUMBERS_GREATER_EQUAL_M.c:25-27'}],
  'triage_note': 'none. (The first sample in the result file, [1,0] C=2, uses m=0, where the C doubles dp[i][0] -- well defined but outside the intended m>=1 '
                 'domain; [32,1] is an in-domain example.) The same swapped range also causes the 1 Rust panic ([0,1], C=1): with n<m Rust iterates k=m..n and '
                 'indexes dp[i][k+1] past the table. The 44 ub inputs are C int overflow or dp[n][m] read with m>n+1, already filtered.'},
 {'func': 'NUMBER_TRIANGLES_N_MOVES_1',
  'family': 'scalar',
  'mistake_class': 'f32 used for integer arithmetic (precision loss above 2^24)',
  'mechanisms': [{'mechanism': 'C computes 2*pow(3,n)-1 in double; Rust uses `3f32.powf(n as f32)` and `2 * val as i32 - 1`, so 3^n is rounded to f32 once it '
                               'exceeds 2^24 (n>=16) and the result is wrong.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/NUMBER_TRIANGLES_N_MOVES_1/out-rwasm-bolero/src/main.rs:1416-1417',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/NUMBER_TRIANGLES_N_MOVES_1/NUMBER_TRIANGLES_N_MOVES_1.c:22'}],
  'triage_note': 'LIKELY OVERCOUNT: 73 of the 74 diverging inputs (every grid n>=21; in general n>=19) are original-C UB: 2*3^n-1 > INT_MAX and the C converts '
                 "that double to int (C11 6.3.1.4p1). GCC's -fsanitize=undefined does not include float-cast-overflow, so the harness's filter does not catch "
                 "it; C=2147483647 is arm64's saturating conversion (x86 gives -2147483648, results/vert_diff_x86_spread.json), and Rust's -3 comes from "
                 '2*i32::MAX wrapping. Only n=18 is a clean counterexample on the grid (n=16,17,18 in the full 0..18 defined range). The function stays a '
                 'genuine S1, but its clean exposure is 1/80, not 74/80.'},
 {'func': 'HEIGHT_COMPLETE_BINARY_TREE_HEAP_N_NODES',
  'family': 'scalar',
  'mistake_class': 'floor vs ceil',
  'mechanisms': [{'mechanism': 'Computes floor instead of ceil: the doubling loop `while base <= N+1 { result += 1; base *= 2 }` yields floor(log2(N+1)), and '
                               'Rust returns floor(log2(N+1)) - 1 where C returns ceil(log2(N+1)) - 1; off by one whenever N+1 is not a power of two.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/HEIGHT_COMPLETE_BINARY_TREE_HEAP_N_NODES/out-rwasm-bolero/src/main.rs:823-828',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/HEIGHT_COMPLETE_BINARY_TREE_HEAP_N_NODES/HEIGHT_COMPLETE_BINARY_TREE_HEAP_N_NODES.c:22'}],
  'triage_note': 'none. This is not an f32-rounding defect (all f32 values here are exact powers of two); it is a floor-vs-ceil logic error.'},
 {'func': 'MAXIMUM_NUMBER_2X2_SQUARES_CAN_FIT_INSIDE_RIGHT_ISOSCELES_TRIANGLE',
  'family': 'scalar',
  'mistake_class': 'f32 used for integer arithmetic (non-truncating division)',
  'mechanisms': [{'mechanism': 'Integer division `base = (base - 2) / 2` rendered as f32 division `(base - 2) as f32 / 2.0`, so the floor is lost for odd base '
                               'and the triangular number is computed on a half-integer (wrong for every odd base >= 7).',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/MAXIMUM_NUMBER_2X2_SQUARES_CAN_FIT_INSIDE_RIGHT_ISOSCELES_TRIANGLE/out-rwasm-bolero/src/main.rs:418-420',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/MAXIMUM_NUMBER_2X2_SQUARES_CAN_FIT_INSIDE_RIGHT_ISOSCELES_TRIANGLE/MAXIMUM_NUMBER_2X2_SQUARES_CAN_FIT_INSIDE_RIGHT_ISOSCELES_TRIANGLE.c:22-23'}],
  'triage_note': None},
 {'func': 'NUMBER_RECTANGLES_NM_GRID',
  'family': 'scalar',
  'mistake_class': 'f32 used for integer arithmetic (precision loss above 2^24)',
  'mechanisms': [{'mechanism': 'Exact integer formula n(n+1)m(m+1)/4 evaluated in f32 (`temp1 = n*(n+1.0)*0.5`, `temp2 = m*(m+1.0)*0.5`, `(temp1*temp2) as '
                               'i32`), so products above 2^24 are rounded before truncation.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/NUMBER_RECTANGLES_NM_GRID/out-rwasm-bolero/src/main.rs:425-429',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/NUMBER_RECTANGLES_NM_GRID/NUMBER_RECTANGLES_NM_GRID.c:22'}],
  'triage_note': 'none for the 17 value inputs (C product < 2^31 there); the 16 ub inputs are C signed overflow of m*n*(n+1)*(m+1), already filtered.'},
 {'func': 'COUNT_DISTINCT_NON_NEGATIVE_PAIRS_X_Y_SATISFY_INEQUALITY_XX_YY_N_2_1',
  'family': 'scalar',
  'mistake_class': 'floor vs ceil',
  'mechanisms': [{'mechanism': "Initial yCount computed as floor(sqrt(n)) (`(n as f32).sqrt() as i32`) instead of the C's smallest y with y*y >= n "
                               '(ceil(sqrt(n))), so for every non-square n the first column count is one short.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/COUNT_DISTINCT_NON_NEGATIVE_PAIRS_X_Y_SATISFY_INEQUALITY_XX_YY_N_2_1/out-rwasm-bolero/src/main.rs:625',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/COUNT_DISTINCT_NON_NEGATIVE_PAIRS_X_Y_SATISFY_INEQUALITY_XX_YY_N_2_1/COUNT_DISTINCT_NON_NEGATIVE_PAIRS_X_Y_SATISFY_INEQUALITY_XX_YY_N_2_1.c:23-25'}],
  'triage_note': None},
 {'func': 'NUMBER_WHICH_HAS_THE_MAXIMUM_NUMBER_OF_DISTINCT_PRIME_FACTORS_IN_RANGE_M_TO_N',
  'family': 'scalar',
  'mistake_class': 'loop stride dropped (step-1 range)',
  'mechanisms': [{'mechanism': 'Sieve inner loop lost its stride: C `for (j = i*2; j <= n; j += i)` became `for j in (i * 2)..=n` (step 1), so every j >= 2i '
                               'is counted and marked composite; only 2 and 3 stay prime and the answer is the first index >= 6.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/NUMBER_WHICH_HAS_THE_MAXIMUM_NUMBER_OF_DISTINCT_PRIME_FACTORS_IN_RANGE_M_TO_N/out-rwasm-bolero/src/main.rs:1044',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/NUMBER_WHICH_HAS_THE_MAXIMUM_NUMBER_OF_DISTINCT_PRIME_FACTORS_IN_RANGE_M_TO_N/NUMBER_WHICH_HAS_THE_MAXIMUM_NUMBER_OF_DISTINCT_PRIME_FACTORS_IN_RANGE_M_TO_N.c:35-37'}],
  'triage_note': 'none for the 36 value inputs; the 34 ub inputs have m > n, where the C reads factorCount[m] past the VLA, already filtered.'},
 {'func': 'MINIMUM_STEPS_MINIMIZE_N_PER_GIVEN_CONDITION',
  'family': 'scalar',
  'mistake_class': 'reversed range bounds `(hi..=lo).rev()`',
  'mechanisms': [{'mechanism': 'Countdown loop bounds swapped: C `for (i = n; i >= 1; i--)` became `for i in (n..=1).rev()`, empty for n > 1, so table[1] '
                               'keeps its initial value n-1.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/MINIMUM_STEPS_MINIMIZE_N_PER_GIVEN_CONDITION/out-rwasm-bolero/src/main.rs:928',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/MINIMUM_STEPS_MINIMIZE_N_PER_GIVEN_CONDITION/MINIMUM_STEPS_MINIMIZE_N_PER_GIVEN_CONDITION.c:26-28'}],
  'triage_note': 'none; the 1 ub input (n=0) is the C reading table[1] past a VLA of size 1, already filtered.'},
 {'func': 'SUM_BINOMIAL_COEFFICIENTS',
  'family': 'scalar',
  'mistake_class': 'f32 used for integer arithmetic (precision loss above 2^24)',
  'mechanisms': [{'mechanism': 'Pascal-triangle entries stored and added in f32 (`Vec<Vec<f32>>`); binomials above 2^24 (C(29,k) up to 77558760) are rounded, '
                               'so the row sum after per-entry `as i32` is off.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/SUM_BINOMIAL_COEFFICIENTS/out-rwasm-bolero/src/main.rs:1071,1078,1085',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/SUM_BINOMIAL_COEFFICIENTS/SUM_BINOMIAL_COEFFICIENTS.c:22,30'}],
  'triage_note': 'none for n=29 (largest C entry 77558760, sum 2^29, no overflow). The 70 ub inputs (n>=32) are C int overflow, already filtered. In the full '
                 'C-defined range n<=30 the Rust diverges at n=28,29,30; only n=29 is on the grid.'},
 {'func': 'PIZZA_CUT_PROBLEM_CIRCLE_DIVISION_LINES',
  'family': 'scalar',
  'mistake_class': 'wrong closed-form formula',
  'mechanisms': [{'mechanism': 'Constant term dropped: C returns 1 + n(n+1)/2; Rust returns the sum 1..=n = n(n+1)/2 (accumulator starts at 0, no +1), wrong '
                               'for every n.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/PIZZA_CUT_PROBLEM_CIRCLE_DIVISION_LINES/out-rwasm-bolero/src/main.rs:396-400',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/PIZZA_CUT_PROBLEM_CIRCLE_DIVISION_LINES/PIZZA_CUT_PROBLEM_CIRCLE_DIVISION_LINES.c:22'}],
  'triage_note': None},
 {'func': 'NUMBER_OF_TRIANGLES_IN_A_PLANE_IF_NO_MORE_THAN_TWO_POINTS_ARE_COLLINEAR',
  'family': 'scalar',
  'mistake_class': 'wrong closed-form formula',
  'mechanisms': [{'mechanism': "Wrong formula: the (n-2) factor of C's n(n-1)(n-2)/6 is replaced by prod = 2*3*...*(n-1) = (n-1)!, so Rust computes "
                               'n(n-1)(n-1)!/6.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/NUMBER_OF_TRIANGLES_IN_A_PLANE_IF_NO_MORE_THAN_TWO_POINTS_ARE_COLLINEAR/out-rwasm-bolero/src/main.rs:405-409',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/NUMBER_OF_TRIANGLES_IN_A_PLANE_IF_NO_MORE_THAN_TWO_POINTS_ARE_COLLINEAR/NUMBER_OF_TRIANGLES_IN_A_PLANE_IF_NO_MORE_THAN_TWO_POINTS_ARE_COLLINEAR.c:22'}],
  'triage_note': 'none (C value <= 1.7e7 on the grid; the Rust factorial wraps for n>=14, but the formula is wrong from n=3).'},
 {'func': 'CHECK_IF_A_NUMBER_IS_JUMBLED_OR_NOT',
  'family': 'scalar',
  'mistake_class': 'missing guard / condition',
  'mechanisms': [{'mechanism': "In-loop termination guard dropped: C checks `if (num / 10 == 0) return true;` at the top of every iteration; Rust's loop lacks "
                               'it, so on the last (leading) digit d it compares d with a phantom 0 and returns false whenever the leading digit is >= 2.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/CHECK_IF_A_NUMBER_IS_JUMBLED_OR_NOT/out-rwasm-bolero/src/main.rs:607-613',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/CHECK_IF_A_NUMBER_IS_JUMBLED_OR_NOT/CHECK_IF_A_NUMBER_IS_JUMBLED_OR_NOT.c:24'}],
  'triage_note': None},
 {'func': 'SQUARE_ROOT_OF_AN_INTEGER_1',
  'family': 'scalar',
  'mistake_class': 'loop condition dropped (unbounded loop)',
  'mechanisms': [{'mechanism': 'Loop condition dropped: C `while (start <= end)` became an unconditional `loop { ... }`, so for any non-square x the binary '
                               'search reaches start = end+1 and spins forever instead of exiting and returning ans.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/SQUARE_ROOT_OF_AN_INTEGER_1/out-rwasm-bolero/src/main.rs:674-686',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/SQUARE_ROOT_OF_AN_INTEGER_1/SQUARE_ROOT_OF_AN_INTEGER_1.c:24'}],
  'triage_note': 'none (C mid*mid <= 236^2 on the grid, no overflow).'},
 {'func': 'FIND_FIRST_NATURAL_NUMBER_WHOSE_FACTORIAL_DIVISIBLE_X',
  'family': 'scalar',
  'mistake_class': 'loop condition dropped (unbounded loop)',
  'mechanisms': [{'mechanism': 'Loop bound dropped: C `for (i = 1; i < x; i++)` became an unconditional `loop`, so at x=0 (where the C body never runs and '
                               'returns 1) Rust evaluates fact % 0.0 = NaN != 0 forever.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/FIND_FIRST_NATURAL_NUMBER_WHOSE_FACTORIAL_DIVISIBLE_X/out-rwasm-bolero/src/main.rs:480-486',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/FIND_FIRST_NATURAL_NUMBER_WHOSE_FACTORIAL_DIVISIBLE_X/FIND_FIRST_NATURAL_NUMBER_WHOSE_FACTORIAL_DIVISIBLE_X.c:24-26'}],
  'triage_note': "The single hang input is x=0, a degenerate input outside the 'natural number' domain, but the C is well defined there (no modulo is "
                 'executed), so the hang is a real divergence. The other 56 non-agreeing inputs are C signed overflow of fact (filtered as ub).'},
 {'func': 'MINIMUM_LENGTH_SUBARRAY_SUM_GREATER_GIVEN_VALUE',
  'family': 'array/float',
  'mistake_class': 'sentinel value mistranslated',
  'mechanisms': [{'mechanism': 'Sentinel mistranslated: C initialises `min_len = n + 1`; Rust uses `INFINITY as i32` (saturates to i32::MAX), so when no '
                               'subarray sum exceeds x Rust returns 2147483647 instead of n+1.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/MINIMUM_LENGTH_SUBARRAY_SUM_GREATER_GIVEN_VALUE/out-rwasm-bolero/src/main.rs:843',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/MINIMUM_LENGTH_SUBARRAY_SUM_GREATER_GIVEN_VALUE/MINIMUM_LENGTH_SUBARRAY_SUM_GREATER_GIVEN_VALUE.c:22'}],
  'triage_note': None},
 {'func': 'MINIMUM_OPERATIONS_MAKE_GCD_ARRAY_MULTIPLE_K',
  'family': 'array/float',
  'mistake_class': 'max/min argument restructured',
  'mechanisms': [{'mechanism': '`min(a[i] % k, k - a[i] % k)` split into two additions (`result += a % k; result += k - a % k;`), so k is added instead of the '
                               'minimum.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/MINIMUM_OPERATIONS_MAKE_GCD_ARRAY_MULTIPLE_K/out-rwasm-bolero/src/main.rs:864-865',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/MINIMUM_OPERATIONS_MAKE_GCD_ARRAY_MULTIPLE_K/MINIMUM_OPERATIONS_MAKE_GCD_ARRAY_MULTIPLE_K.c:27'}],
  'triage_note': None},
 {'func': 'HOW_TO_CHECK_IF_A_GIVEN_ARRAY_REPRESENTS_A_BINARY_HEAP_1',
  'family': 'array/float',
  'mistake_class': 'loop bound off-by-one + usize underflow',
  'mechanisms': [{'mechanism': 'Inclusive loop bound made exclusive: C `for (i = 0; i <= (n-2)/2; i++)` became `for i in 0..(n - 2) / 2`, dropping the last '
                               'parent; for n=2 the only parent check (arr[1] > arr[0]) is skipped and Rust returns 1.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/HOW_TO_CHECK_IF_A_GIVEN_ARRAY_REPRESENTS_A_BINARY_HEAP_1/out-rwasm-bolero/src/main.rs:770',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/HOW_TO_CHECK_IF_A_GIVEN_ARRAY_REPRESENTS_A_BINARY_HEAP_1/HOW_TO_CHECK_IF_A_GIVEN_ARRAY_REPRESENTS_A_BINARY_HEAP_1.c:23'},
                 {'mechanism': 'Unsigned underflow: n is cast to usize (line 769) so for n=1 `n - 2` wraps to usize::MAX (overflow checks off under -O) where '
                               "the C's signed (1-2)/2 = 0; the loop runs on and panics indexing arr[3].",
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/HOW_TO_CHECK_IF_A_GIVEN_ARRAY_REPRESENTS_A_BINARY_HEAP_1/out-rwasm-bolero/src/main.rs:769-771',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/HOW_TO_CHECK_IF_A_GIVEN_ARRAY_REPRESENTS_A_BINARY_HEAP_1/HOW_TO_CHECK_IF_A_GIVEN_ARRAY_REPRESENTS_A_BINARY_HEAP_1.c:23'}],
  'triage_note': 'Value inputs: none. Crash inputs (n=1): the original C itself reads arr[1], one past the logical length n=1; it is in bounds only because '
                 'the harness passes a 2-element buffer ([i32;2]), so the C is defined in the harness. The Rust panic (arr[3]) is a genuine translation '
                 'divergence.'},
 {'func': 'FIND_THE_ELEMENT_BEFORE_WHICH_ALL_THE_ELEMENTS_ARE_SMALLER_THAN_IT_AND_AFTER_WHICH_ALL_ARE_GREATER_THAN_IT',
  'family': 'array/float',
  'mistake_class': 'sentinel value mistranslated',
  'mechanisms': [{'mechanism': 'Sentinel sign error: C seeds `leftMax[0] = INT_MIN`; Rust seeds `left_max` with `INFINITY` (+inf), so every left_max[i] is '
                               '+inf, `left_max[i] < arr[i]` is never true, and the function always returns -1.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/FIND_THE_ELEMENT_BEFORE_WHICH_ALL_THE_ELEMENTS_ARE_SMALLER_THAN_IT_AND_AFTER_WHICH_ALL_ARE_GREATER_THAN_IT/out-rwasm-bolero/src/main.rs:1112-1113',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/FIND_THE_ELEMENT_BEFORE_WHICH_ALL_THE_ELEMENTS_ARE_SMALLER_THAN_IT_AND_AFTER_WHICH_ALL_ARE_GREATER_THAN_IT/FIND_THE_ELEMENT_BEFORE_WHICH_ALL_THE_ELEMENTS_ARE_SMALLER_THAN_IT_AND_AFTER_WHICH_ALL_ARE_GREATER_THAN_IT.c:23'}],
  'triage_note': None},
 {'func': 'MAXIMUM_SUM_SUBARRAY_REMOVING_ONE_ELEMENT',
  'family': 'array/float',
  'mistake_class': 'chained assignment mistranslated',
  'mechanisms': [{'mechanism': 'Chained reset mistranslated: C `cur_max = max_so_far = bw[n-1] = arr[n-1];` became `cur_max = max_so_far; bw[n-1] = '
                               'arr[n-1];`, so the backward Kadane pass starts from the forward maximum instead of arr[n-1] (and max_so_far is not reset), '
                               'double-counting.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/MAXIMUM_SUM_SUBARRAY_REMOVING_ONE_ELEMENT/out-rwasm-bolero/src/main.rs:1230-1231',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/MAXIMUM_SUM_SUBARRAY_REMOVING_ONE_ELEMENT/MAXIMUM_SUM_SUBARRAY_REMOVING_ONE_ELEMENT.c:32'}],
  'triage_note': None},
 {'func': 'MINIMUM_SUM_PRODUCT_TWO_ARRAYS',
  'family': 'array/float',
  'mistake_class': 'max/min argument restructured (abs expression rewritten)',
  'mechanisms': [{'mechanism': '`d = abs(pro - temp)` rewritten as `temp.abs() - pro.abs()` (in all four branches), which differs whenever pro and temp have '
                               'opposite signs or |temp| < |pro|.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/MINIMUM_SUM_PRODUCT_TWO_ARRAYS/out-rwasm-bolero/src/main.rs:1397,1400,1404,1407',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/MINIMUM_SUM_PRODUCT_TWO_ARRAYS/MINIMUM_SUM_PRODUCT_TWO_ARRAYS.c:33'}],
  'triage_note': 'none for the 22 value inputs. The 8 ub inputs have pro==0 at i=0, where the C reads the uninitialised `temp` (filtered by the -O0/-O1 '
                 'cross-check).'},
 {'func': 'LARGEST_SUM_CONTIGUOUS_SUBARRAY_2',
  'family': 'array/float',
  'mistake_class': 'loop bound off-by-one',
  'mechanisms': [{'mechanism': 'Kadane loop start off by one: C `for (i = 1; i < size; i++)` (after seeding with a[0]) became `for i in 0..size`, so a[0] is '
                               'added a second time.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/LARGEST_SUM_CONTIGUOUS_SUBARRAY_2/out-rwasm-bolero/src/main.rs:693',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/LARGEST_SUM_CONTIGUOUS_SUBARRAY_2/LARGEST_SUM_CONTIGUOUS_SUBARRAY_2.c:24'}],
  'triage_note': None},
 {'func': 'MAXIMIZE_ARRAY_ELEMENTS_UPTO_GIVEN_NUMBER',
  'family': 'array/float',
  'mistake_class': 'loop bound off-by-one (inclusive vs exclusive)',
  'mechanisms': [{'mechanism': 'Final scan `for (val = maxLimit; val >= 0; val--)` became `for val in (0..max_limit).rev()`, an exclusive range that never '
                               'examines val = maxLimit.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/MAXIMIZE_ARRAY_ELEMENTS_UPTO_GIVEN_NUMBER/out-rwasm-bolero/src/main.rs:1905',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/MAXIMIZE_ARRAY_ELEMENTS_UPTO_GIVEN_NUMBER/MAXIMIZE_ARRAY_ELEMENTS_UPTO_GIVEN_NUMBER.c:55-57'}],
  'triage_note': 'none for value inputs; the 2 ub inputs have negative arr entries, where the C indexes dp[ind-1][val -/+ arr[ind]] outside [0, maxLimit], '
                 'already filtered.'},
 {'func': 'FIND_MAXIMUM_DOT_PRODUCT_TWO_ARRAYS_INSERTION_0S',
  'family': 'array/float',
  'mistake_class': 'max/min argument restructured',
  'mechanisms': [{'mechanism': 'Wrong DP recurrence: C `dp[i][j] = max(dp[i-1][j-1] + A[j-1]*B[i-1], dp[i][j-1])` became `dp[i-1][j-1].max(A[j-1]*B[i-1])` -- '
                               'the product is compared with, not added to, the diagonal term (and the dp[i][j-1] option is dropped).',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/FIND_MAXIMUM_DOT_PRODUCT_TWO_ARRAYS_INSERTION_0S/out-rwasm-bolero/src/main.rs:1319',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/FIND_MAXIMUM_DOT_PRODUCT_TWO_ARRAYS_INSERTION_0S/FIND_MAXIMUM_DOT_PRODUCT_TWO_ARRAYS_INSERTION_0S.c:28'}],
  'triage_note': 'none. Side notes: the C returns long long while the Rust returns i32 (not the cause; values <= 5); the harness always sets m = n, so the '
                 "dropped dp[i][j-1] option is only weakly exercised and the visible error is the '+ -> max' change."},
 {'func': 'FIND_SMALLEST_VALUE_REPRESENTED_SUM_SUBSET_GIVEN_ARRAY',
  'family': 'array/float',
  'mistake_class': 'loop-exit condition moved into body (break -> skip)',
  'mechanisms': [{'mechanism': 'Loop-exit condition turned into a skip: C `for (i = 0; i < n && arr[i] <= res; i++)` stops at the first arr[i] > res; Rust '
                               '`for i in 0..n { if arr[i] <= res { ... } }` skips that element and keeps adding later ones.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/FIND_SMALLEST_VALUE_REPRESENTED_SUM_SUBSET_GIVEN_ARRAY/out-rwasm-bolero/src/main.rs:594-597',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/FIND_SMALLEST_VALUE_REPRESENTED_SUM_SUBSET_GIVEN_ARRAY/FIND_SMALLEST_VALUE_REPRESENTED_SUM_SUBSET_GIVEN_ARRAY.c:24'}],
  'triage_note': 'PRECONDITION: the C algorithm requires arr sorted ascending; all 3 diverging inputs ([2,1], [3,1], [100,1]) are unsorted. On sorted input '
                 'the two versions are equivalent (0 divergences over all sorted 2-element arrays with values -3..7, n in {1,2}). The C is well defined on '
                 "unsorted input, so this is a real semantic difference, but only outside the function's intended domain."},
 {'func': 'CEILING_IN_A_SORTED_ARRAY',
  'family': 'array/float',
  'mistake_class': 'loop condition dropped (unbounded loop)',
  'mechanisms': [{'mechanism': 'Loop bound dropped: C `for (i = low; i < high; i++)` (then `return -1`) became an unbounded `loop { ...; i += 1 }`, so when no '
                               'ceiling lies in [low, high) Rust scans past the array and panics on arr[i + 1] instead of returning -1.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/CEILING_IN_A_SORTED_ARRAY/out-rwasm-bolero/src/main.rs:796-800',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/CEILING_IN_A_SORTED_ARRAY/CEILING_IN_A_SORTED_ARRAY.c:24-25'}],
  'triage_note': 'none (crash inputs are sanitizer-clean in the C; the 15 ub inputs have low=2, where the C reads arr[2]). The harness always sets low = high '
                 '= x, so the C loop never iterates. The same unbounded loop also yields wrong values when the ceiling lies beyond high (e.g. arr=[0,1], '
                 'low=high=0, x=1: C -1, Rust 1), which the grid does not exercise.'},
 {'func': 'SPACE_OPTIMIZED_DP_SOLUTION_0_1_KNAPSACK_PROBLEM_1',
  'family': 'array/float',
  'mistake_class': 'loop bound dropped (lower bound replaced by 0)',
  'mechanisms': [{'mechanism': 'Inner-loop lower bound dropped: C `for (j = W; j >= wt[i]; j--)` became `for j in (0..=W).rev()`, so for j < wt[i] the index '
                               '(j - wt[i]) as usize wraps to a huge value and panics.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/SPACE_OPTIMIZED_DP_SOLUTION_0_1_KNAPSACK_PROBLEM_1/out-rwasm-bolero/src/main.rs:1189-1190',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/SPACE_OPTIMIZED_DP_SOLUTION_0_1_KNAPSACK_PROBLEM_1/SPACE_OPTIMIZED_DP_SOLUTION_0_1_KNAPSACK_PROBLEM_1.c:26-28'}],
  'triage_note': 'none (the 4 ub inputs have negative weights, where the C indexes dp out of range).'},
 # ---- found only by input grid v2 (results/vert_diff_grid2.json; results/vert_grid2_new_defects.json): v1's index
 # thinning dropped the powers of two +-1 that expose them. Mechanisms read from source and reproduced by a numeric model.
 {'func': 'PRIMALITY_TEST_SET_5USING_LUCAS_LEHMER_SERIES',
  'family': 'scalar',
  'mistake_class': 'integer width narrowed (long long rendered as i32, wraps)',
  'mechanisms': [{'mechanism': 'Integer width narrowed: C runs the Lucas-Lehmer recurrence in long long (checkNumber = pow(2,p)-1, '
                               'nextval = (nextval*nextval-2) % checkNumber), the Rust in i32, so nextval*nextval wraps for p >= 17 '
                               '(nextval up to 2^p-2; rustc -O, overflow checks off) and the test misses the Mersenne primes M17 and M31 '
                               '(C 1, Rust 0 at p = 17 and p = 31).',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/PRIMALITY_TEST_SET_5USING_LUCAS_LEHMER_SERIES/out-rwasm-bolero/src/main.rs:1583-1586',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/PRIMALITY_TEST_SET_5USING_LUCAS_LEHMER_SERIES/PRIMALITY_TEST_SET_5USING_LUCAS_LEHMER_SERIES.c:22-26'}],
  'triage_note': 'none (grid v2: both diverging inputs, p = 17 and 31, pass the full UB rule; the C is long long throughout and does not overflow there).'},
 {'func': 'NUMBER_WAYS_NODE_MAKE_LOOP_SIZE_K_UNDIRECTED_COMPLETE_CONNECTED_GRAPH_N_NODES',
  'family': 'scalar',
  'mistake_class': 'double narrowed to f32 (precision loss above 2^24)',
  'mechanisms': [{'mechanism': 'Float width narrowed: C computes (pow(n-1,k) + p*(n-1)) / n in double, the Rust in f32 '
                               '((n - 1.0).powf(k)), whose 24-bit significand cannot hold 126^5 = 31757969376 exactly, so the truncated '
                               'quotient is off by 2 at (127, 5): C 250062750, Rust 250062752.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/NUMBER_WAYS_NODE_MAKE_LOOP_SIZE_K_UNDIRECTED_COMPLETE_CONNECTED_GRAPH_N_NODES/out-rwasm-bolero/src/main.rs:1499-1500',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/NUMBER_WAYS_NODE_MAKE_LOOP_SIZE_K_UNDIRECTED_COMPLETE_CONNECTED_GRAPH_N_NODES/NUMBER_WAYS_NODE_MAKE_LOOP_SIZE_K_UNDIRECTED_COMPLETE_CONNECTED_GRAPH_N_NODES.c:24'}],
  'triage_note': 'none (grid v2: the diverging input passes the full UB rule).'},
 {'func': 'SUM_SERIES_555555_N_TERMS',
  'family': 'scalar',
  'mistake_class': 'double narrowed to f32 (precision loss above 2^24)',
  'mechanisms': [{'mechanism': 'Float width narrowed: C evaluates 0.6172*(pow(10,n)-1) - 0.55*n in double, the Rust in f32 '
                               '(10_f32.powf), which cannot represent the intermediates above 2^24, so the truncated result differs at '
                               'n = 8 (C 61719994, Rust 61719996) and n = 9.',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/SUM_SERIES_555555_N_TERMS/out-rwasm-bolero/src/main.rs:1428',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/SUM_SERIES_555555_N_TERMS/SUM_SERIES_555555_N_TERMS.c:22'}],
  'triage_note': 'none (grid v2: both diverging inputs pass the full UB rule).'},
 # ---- arXiv (issues.md 47): functions whose C returns long / long long, tested since the arXiv version
 # (systems/vert/run.py --wide-int; results/arxiv_vert_diff_grid2_wide.json). VERT's Rust returns i32 for all of them.
 {'func': 'COUNT_DIGITS_FACTORIAL_SET_2',
  'family': 'scalar',
  'mistake_class': 'wrong library function (ln for log10)',
  'wide_int_return': True,
  'mechanisms': [{'mechanism': "Kamenetsky's digit-count formula computed with the natural logarithm: the C evaluates `n*log10(n/M_E) + log10(2*M_PI*n)/2` in double and returns floor + 1, the Rust uses `f32::ln` in both terms (and f32), so the digit count of n! is wrong for every n >= 3 (n = 5: C 3, Rust 5).",
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/COUNT_DIGITS_FACTORIAL_SET_2/out-rwasm-bolero/src/main.rs:886-889',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/COUNT_DIGITS_FACTORIAL_SET_2/COUNT_DIGITS_FACTORIAL_SET_2.c:24-25'}],
  'triage_note': 'logic error: the C value fits in i32 on every exposing input.'},
 {'func': 'FIND_LARGEST_PRIME_FACTOR_NUMBER',
  'family': 'scalar',
  'mistake_class': 'loop bound off-by-one (inclusive vs exclusive)',
  'wide_int_return': True,
  'mechanisms': [{'mechanism': 'Trial-division bound made exclusive: the C tries odd i while `i <= sqrt(n)`, the Rust `for i in (3..(n as f32).sqrt() as i32).step_by(2)`, which stops before sqrt(n); when what remains of n is the square of an odd prime the factor is never divided out and the Rust returns that square (n = 9: C 3, Rust 9; n = 18: C 3, Rust 9).',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/FIND_LARGEST_PRIME_FACTOR_NUMBER/out-rwasm-bolero/src/main.rs:657',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/FIND_LARGEST_PRIME_FACTOR_NUMBER/FIND_LARGEST_PRIME_FACTOR_NUMBER.c:27-29'}],
  'triage_note': "logic error: the C value fits in i32 on every exposing input; the C's long long argument and return are not the cause."},
 {'func': 'CALCULATING_FACTORIALS_USING_STIRLING_APPROXIMATION',
  'family': 'scalar',
  'mistake_class': 'double narrowed to f32, long narrowed to i32',
  'wide_int_return': True,
  'mechanisms': [{'mechanism': "Stirling's formula computed in f32 and returned as i32: the C evaluates sqrt(2*3.14*n) * pow(n/e, n) in double and stores it in a long, the Rust computes in f32 and casts with `z as i32`, so n = 12 rounds differently (C 493303222, Rust 493303232) and from n = 13 the value exceeds i32 and the cast saturates at 2147483647 (n = 13: C 6435976349).",
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/CALCULATING_FACTORIALS_USING_STIRLING_APPROXIMATION/out-rwasm-bolero/src/main.rs:1504-1507',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/CALCULATING_FACTORIALS_USING_STIRLING_APPROXIMATION/CALCULATING_FACTORIALS_USING_STIRLING_APPROXIMATION.c:23-26'}],
  'triage_note': "on our grid it shows as width only: its UB-clean exposing grid inputs (n = 14 to 18) all have a C value past i32; the f32 rounding error at n = 12 (C 493303222, Rust 493303232, C value inside i32) is off the grid. From n = 21 the C's double-to-long conversion overflows (C UB, filtered: 85 of the 90 grid divergences)."},
 {'func': 'COUNT_WAYS_BUILD_STREET_GIVEN_CONSTRAINTS',
  'family': 'scalar',
  'mistake_class': 'integer width narrowed (long rendered as i32, wraps)',
  'wide_int_return': True,
  'mechanisms': [{'mechanism': 'Integer width narrowed: the C keeps its DP table and result in long (64-bit), the Rust in i32 (`vec![vec![0; n as usize + 1]; 2]`, `-> i32`), so the count wraps once it passes 2^31-1, from n = 25 (C 4478554083, Rust 183586787; rustc -O, overflow checks off).',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/COUNT_WAYS_BUILD_STREET_GIVEN_CONSTRAINTS/out-rwasm-bolero/src/main.rs:928-935',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/COUNT_WAYS_BUILD_STREET_GIVEN_CONSTRAINTS/COUNT_WAYS_BUILD_STREET_GIVEN_CONSTRAINTS.c:22-31'}],
  'triage_note': "width only: every exposing C value exceeds i32. From n = 51 the C's long table overflows (C UB, filtered: 75 of the 84 grid divergences)."},
 {'func': 'DICE_THROW_PROBLEM_1',
  'family': 'scalar',
  'mistake_class': 'integer width narrowed (long rendered as i32, wraps)',
  'wide_int_return': True,
  'mechanisms': [{'mechanism': "Integer width narrowed: the C's table mem and its result are long (64-bit), the Rust's i32 (`vec![vec![0; s as usize + 1]; d as usize + 1]`, `-> i32`), so counts above 2^31-1 wrap (f = 8, d = 17, s = 44: C 197935851868, Rust 367356252).",
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/DICE_THROW_PROBLEM_1/out-rwasm-bolero/src/main.rs:1280-1290',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/DICE_THROW_PROBLEM_1/DICE_THROW_PROBLEM_1.c:22-35'}],
  'triage_note': 'width only: every exposing C value exceeds i32. The other 11 grid divergences are C UB (filtered).'},
 {'func': 'NUMBER_N_DIGIT_STEPPING_NUMBERS',
  'family': 'scalar',
  'mistake_class': 'integer width narrowed (long long rendered as i32, wraps)',
  'wide_int_return': True,
  'mechanisms': [{'mechanism': "Integer width narrowed at the sum: the C adds its int table entries into a long long, the Rust into an i32 (`let mut sum = 0`, `-> i32`), which wraps for n = 31 to 33 (n = 31: C 2620797529, Rust -1674169767); for larger n the C's int table itself overflows (C UB, filtered).",
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/NUMBER_N_DIGIT_STEPPING_NUMBERS/out-rwasm-bolero/src/main.rs:1267-1272',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/NUMBER_N_DIGIT_STEPPING_NUMBERS/NUMBER_N_DIGIT_STEPPING_NUMBERS.c:38-42'}],
  'triage_note': 'width only: every exposing C value exceeds i32. The other 57 grid divergences (n >= 36) are C UB: the int table overflows (filtered).'},
 {'func': 'SUM_PAIRWISE_PRODUCTS',
  'family': 'scalar',
  'mistake_class': 'integer width narrowed (long long rendered as i32, wraps)',
  'wide_int_return': True,
  'mechanisms': [{'mechanism': 'Integer width narrowed: the C accumulates i*j in a long long, the Rust in an i32 (`let mut sum = 0`, `-> i32`), which wraps once the sum passes 2^31-1, from n = 362 (C 2166381217, Rust -2128586079).',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/SUM_PAIRWISE_PRODUCTS/out-rwasm-bolero/src/main.rs:526-532',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/SUM_PAIRWISE_PRODUCTS/SUM_PAIRWISE_PRODUCTS.c:22-28'}],
  'triage_note': 'width only: every exposing C value exceeds i32.'},
 {'func': 'SUM_PAIRWISE_PRODUCTS_1',
  'family': 'scalar',
  'mistake_class': 'integer width narrowed (long long rendered as i32, wraps)',
  'wide_int_return': True,
  'mechanisms': [{'mechanism': 'Integer width narrowed: the C keeps multiTerms and sum in long long, the Rust computes multiTerms in f32 and the sum in an i32 (`-> i32`), which wraps once the sum passes 2^31-1, from n = 362 (C 2166381217, Rust -2128586079).',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/SUM_PAIRWISE_PRODUCTS_1/out-rwasm-bolero/src/main.rs:516-523',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/SUM_PAIRWISE_PRODUCTS_1/SUM_PAIRWISE_PRODUCTS_1.c:22-30'}],
  'triage_note': 'width only: every exposing C value in 0..512 exceeds i32.'},
 {'func': 'DELANNOY_NUMBER_1',
  'family': 'scalar',
  'mistake_class': None,
  'mechanisms': [{'mechanism': 'Not a translation defect: the ORIGINAL C reads uninitialised VLA memory. Its second init loop is '
                               '`for (i = 0; i <= m; i++) dp[0][i] = 1;` (bound m, should be n), so for n > m the cells dp[0][m+1..n] are '
                               'never written and are read in the DP; for larger inputs it also overflows int. The Rust initialises every '
                               'cell to 1 (vec![vec![1; n+1]; m+1]).',
                  'rust_location': 'artifacts/vert/benchmark/c_transcoder/DELANNOY_NUMBER_1/out-rwasm-bolero/src/main.rs:1039',
                  'c_location': 'artifacts/vert/benchmark/c_transcoder/DELANNOY_NUMBER_1/DELANNOY_NUMBER_1.c:26-28'}],
  'triage_note': 'Excluded by the full UB rule (results/vert_grid2_ub_recheck.json): the one v2 value divergence (127, 5) passes the primary filter, but the gcc sanitizer build returns another value there and clang -O1 UBSan reports signed overflow.'}]

CURATED_ROSETTA = [{'task': 'box_the_compass',
  'severity': 'S1',
  'mechanisms': [{'mechanism': 'C truncates 0.5 + deg*32/360 to int (round-half-up once); Rust adds 0.5 AND calls .round(), rounding twice, so the '
                               'compass-point index is one too high for most headings (and wraps 354.37 to North).',
                  'rust_location': 'artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/box_the_compass-c.rs:21 `let j = (0.5 + degree * 32.0 / '
                                   '360.0).round() as usize % 32;`',
                  'c_location': 'artifacts/rustmap/c-code/rosseta-125/box_the_compass/main.c:46 `j = .5 + degrees[i] * 32 / 360;`'}],
  'record': {'status': 'iii_no_verdict (raw divergent diff hunk shipped, never interpreted as pass or fail)',
             'evidence': 'artifacts/rustmap/c-code/rosseta-125/original_rosetta_result.log:505 (C output) vs '
                         'artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/rustmap_rosetta_result.log:513 (Rust output, same wrong output as '
                         'our run); raw hunk(s) in artifacts/rustmap/c-code/rosseta-125/diff_rosetta_result.log at line(s) 119, 175-211. README.md:291-294 '
                         'only gives the diff command; no verdict, tally or pass/fail list for this task anywhere in artifacts/rustmap.'},
  'triage_note': 'None; genuine logic error. Column-width differences (%.22s/%6.2f vs {:22}/{:.2}) are cosmetic and separate.'},
 {'task': 'floyds_triangle',
  'severity': 'S1',
  'mechanisms': [{'mechanism': 'Counter is initialised from i = n*(n-1)/2 (the width-calculation variable) instead of 1, so numbering starts at n(n-1)/2+1.',
                  'rust_location': 'artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/floyds_triangle-c.rs:25 `let mut num = i;` (i set at '
                                   'line 2; incremented before print at line 28)',
                  'c_location': 'artifacts/rustmap/c-code/rosseta-125/floyds_triangle/main.c:49 `for (num = i = 1; i <= n; i++)`'}],
  'record': {'status': 'iii_no_verdict (raw divergent diff hunk shipped, never interpreted as pass or fail)',
             'evidence': 'artifacts/rustmap/c-code/rosseta-125/original_rosetta_result.log:1815 (C output) vs '
                         'artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/rustmap_rosetta_result.log:1830 (Rust output, same wrong output as '
                         'our run); raw hunk(s) in artifacts/rustmap/c-code/rosseta-125/diff_rosetta_result.log at line(s) 551. README.md:291-294 only gives '
                         'the diff command; no verdict, tally or pass/fail list for this task anywhere in artifacts/rustmap.'},
  'triage_note': 'None. Width differences follow from the larger numbers; the single mechanism is the start value.'},
 {'task': 'isbn13_check_digit',
  'severity': 'S1',
  'mechanisms': [{'mechanism': 'Weight parity is taken from the character index (enumerate over all chars, including the hyphen) instead of the digit count '
                               'that C decrements for hyphens, so every digit after the hyphen gets the wrong 1/3 weight.',
                  'rust_location': 'artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/isbn13_check_digit-c.rs:10 `if idx % 2 == 0 {` (idx from '
                                   '`.chars().enumerate()` at line 5; hyphen skipped at line 7 without adjusting idx)',
                  'c_location': 'artifacts/rustmap/c-code/rosseta-125/isbn13_check_digit/main.c:15 `if (count & 1)` with `--count` for hyphens at line 9'}],
  'record': {'status': 'iii_no_verdict (raw divergent diff hunk shipped, never interpreted as pass or fail)',
             'evidence': 'artifacts/rustmap/c-code/rosseta-125/original_rosetta_result.log:2034 (C output) vs '
                         'artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/rustmap_rosetta_result.log:1995 (Rust output, same wrong output as '
                         'our run); raw hunk(s) in artifacts/rustmap/c-code/rosseta-125/diff_rosetta_result.log at line(s) 894, 900. README.md:291-294 only '
                         'gives the diff command; no verdict, tally or pass/fail list for this task anywhere in artifacts/rustmap.'},
  'triage_note': 'None.'},
 {'task': 'knapsack_problem_0_1',
  'severity': 'S1',
  'mechanisms': [{'mechanism': "The item table is truncated to its first entry ('map'); 21 of 22 items are omitted. The DP and backtrack (lines 8-38) are a "
                               'faithful translation of C lines 35-63.',
                  'rust_location': 'artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/knapsack_problem_0_1-c.rs:41-43 `let items = [ Item { '
                                   'name: "map", weight: 9, value: 150 }, ];`',
                  'c_location': 'artifacts/rustmap/c-code/rosseta-125/knapsack_problem_0_1/main.c:10-33 (22-item table)'}],
  'record': {'status': 'iii_no_verdict (raw divergent diff hunk shipped, never interpreted as pass or fail)',
             'evidence': 'artifacts/rustmap/c-code/rosseta-125/original_rosetta_result.log:2079 (C output) vs '
                         'artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/rustmap_rosetta_result.log:2020 (Rust output, same wrong output as '
                         'our run); raw hunk(s) in artifacts/rustmap/c-code/rosseta-125/diff_rosetta_result.log at line(s) 931, 944. README.md:291-294 only '
                         'gives the diff command; no verdict, tally or pass/fail list for this task anywhere in artifacts/rustmap.'},
  'triage_note': "LIKELY MISCLASSIFIED under the triage's own taxonomy: the algorithm is correct and the output is right for the 1-item input the Rust "
                 "hard-codes, so this is 'different inputs' (same bucket as number_names, whose Rust driver also hard-codes a different input and is "
                 'excluded). Count it only if data-table truncation is counted as an omission defect consistently (then number_names should be revisited '
                 'too).'},
 {'task': 'knapsack_problem_continuous',
  'severity': 'S1',
  'mechanisms': [{'mechanism': "The item table is truncated to its first entry ('beef'); 8 of 9 items are omitted.",
                  'rust_location': 'artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/knapsack_problem_continuous-c.rs:15-17 `let mut items = '
                                   '[ Item { w: 3.8, v: 36.0, name: "beef" }, ];`',
                  'c_location': 'artifacts/rustmap/c-code/rosseta-125/knapsack_problem_continuous/main.c:4-14 (9-item table)'},
                 {'mechanism': 'LATENT, not visible on the shipped 1-item input: the comparator sorts descending (ub vs ua) and the loop then iterates .rev(), '
                               "so items are taken lowest value/weight first; C sorts ascending and walks from the end (highest first). With the C's 9 items "
                               'this would print flitch/pork/beef/partial sausage.',
                  'rust_location': 'artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/knapsack_problem_continuous-c.rs:11 '
                                   '`ub.partial_cmp(&ua)` + line 22 `items.iter().rev()`',
                  'c_location': 'artifacts/rustmap/c-code/rosseta-125/knapsack_problem_continuous/main.c:20 `return ua < ub ? -1 : ua > ub;` + line 29 `for '
                                '(it = items + 9; it---items ...`'}],
  'record': {'status': 'iii_no_verdict (raw divergent diff hunk shipped, never interpreted as pass or fail)',
             'evidence': 'artifacts/rustmap/c-code/rosseta-125/original_rosetta_result.log:2093 (C output) vs '
                         'artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/rustmap_rosetta_result.log:2023 (Rust output, same wrong output as '
                         'our run); raw hunk(s) in artifacts/rustmap/c-code/rosseta-125/diff_rosetta_result.log at line(s) 944. README.md:291-294 only gives '
                         'the diff command; no verdict, tally or pass/fail list for this task anywhere in artifacts/rustmap.'},
  'triage_note': "PARTLY MISCLASSIFIED: the visible divergence comes only from the truncated data table (a 'different inputs' case under the triage taxonomy, "
                 'like number_names). The reversed sort order is a real logic error, but it is latent in the shipped program. Either reclassify as '
                 'different-inputs, or count it and record that the counted mechanism is data omission, with the order bug as latent.'},
 {'task': 'roman_numerals_decode',
  'severity': 'S1',
  'mechanisms': [{'mechanism': 'When a larger numeral follows, Rust subtracts the larger following numeral (which it then adds back) instead of the smaller '
                               'preceding one, so each subtractive pair contributes 0 (CM->0, XC->0, XL->0).',
                  'rust_location': 'artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/roman_numerals_decode-c.rs:17 `arabic -= '
                                   'value(chars.next().unwrap());` (after `current = next;` at line 16)',
                  'c_location': 'artifacts/rustmap/c-code/rosseta-125/roman_numerals_decode/main.c:28-30 `arabic += VALUE(*bigger); while (roman < bigger) '
                                'arabic -= VALUE(*(roman++));`'}],
  'record': {'status': "iii_no_record (Rust never ran in RustMap's shipped run)",
             'evidence': 'C output at artifacts/rustmap/c-code/rosseta-125/original_rosetta_result.log:3529; the shipped Rust log '
                         "artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/rustmap_rosetta_result.log ends at line 2312 'Compiling and "
                         "running rational-c.rs' (run never finished; leftover binary "
                         'artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/rational-c), so no Rust output exists for this task. The diff log '
                         'records only the unmatched C tail (artifacts/rustmap/c-code/rosseta-125/diff_rosetta_result.log:2433, hunk 3488,3590c2302,2312). No '
                         'verdict anywhere.'},
  'triage_note': 'None. Also latent: Rust peeks only at the next character, while C scans ahead past smaller-or-equal numerals (main.c:23). No shipped input '
                 'shows this; with line 17 fixed, all four shipped inputs would match.'},
 {'task': 'sexy_primes',
  'severity': 'S1',
  'mechanisms': [{'mechanism': 'Incomplete translation: Rust main builds the sieve and returns. The whole counting/collection/printing body of C main is '
                               'missing, so nothing is printed (exit 0).',
                  'rust_location': 'artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/sexy_primes-c.rs:39-44 (main ends after `sieve(&mut sv, '
                                   'lim);`; print_helper/print_array at 24-37 are never called)',
                  'c_location': 'artifacts/rustmap/c-code/rosseta-125/sexy_primes/main.c:59-168 (counting loops and all output)'}],
  'record': {'status': "iii_no_record (Rust never ran in RustMap's shipped run)",
             'evidence': 'C output at artifacts/rustmap/c-code/rosseta-125/original_rosetta_result.log:3555; '
                         'artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/rustmap_rosetta_result.log stops at line 2312 (rational-c.rs), '
                         'before sexy_primes; only the unmatched C tail appears in artifacts/rustmap/c-code/rosseta-125/diff_rosetta_result.log:2433. No '
                         'verdict anywhere.'},
  'triage_note': 'Counting is defensible (a missing computation, not a different task), but the triage is inconsistent: number_names (Rust translates only '
                 "say_hundred and hard-codes one other number) is the same kind of incomplete translation and is excluded. C uses setlocale+%'d; with the "
                 "runner's default C locale the output is deterministic."},
 {'task': 'formatted_numeric_output',
  'severity': 'S1',
  'mechanisms': [{'mechanism': "printf's sign-aware '0' flag is translated as Rust fill character '0' with right alignment ({:0>9.3}), which pads before the "
                               'sign.',
                  'rust_location': 'artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/formatted_numeric_output-c.rs:7 `println!("{:0>9.3}", '
                                   '-r);`',
                  'c_location': 'artifacts/rustmap/c-code/rosseta-125/formatted_numeric_output/main.c:7 `printf(" %09.3f\\n",-r);`'},
                 {'mechanism': "For %-09.3f, C ignores '0' when '-' is present (left-justified, space-padded); Rust uses fill '0' with left alignment "
                               '({:0<9.3}), so it appends zeros.',
                  'rust_location': 'artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/formatted_numeric_output-c.rs:9 `println!("{:0<9.3}", '
                                   'r);`',
                  'c_location': 'artifacts/rustmap/c-code/rosseta-125/formatted_numeric_output/main.c:9 `printf(" %-09.3f\\n",r);`'}],
  'record': {'status': 'iii_no_verdict (raw divergent diff hunk shipped, never interpreted as pass or fail)',
             'evidence': 'artifacts/rustmap/c-code/rosseta-125/original_rosetta_result.log:1835 (C output) vs '
                         'artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/rustmap_rosetta_result.log:1850 (Rust output, same wrong output as '
                         'our run); raw hunk(s) in artifacts/rustmap/c-code/rosseta-125/diff_rosetta_result.log at line(s) 551. README.md:291-294 only gives '
                         'the diff command; no verdict, tally or pass/fail list for this task anywhere in artifacts/rustmap.'},
  'triage_note': 'Both mechanisms are one coherent mistake (printf 0-flag rendered as a Rust fill character) at two lines. Ledger can count this as 1 defect '
                 '(coherent mistake) or 2 (two printf rules); recommend 1. The leading literal space dropped from every C format (lines 4-9) is cosmetic. C is '
                 'UB-clean.'},
 {'task': 'approximate_equility',
  'severity': 'S1',
  'mechanisms': [{'mechanism': 'Negation is moved inside the square root: C computes -sqrt(2)*sqrt(2) = -2.0000000000000004; Rust computes sqrt(-2)*sqrt(-2) = '
                               'NaN.',
                  'rust_location': 'artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/approximate_equility-c.rs:17 `test((-2.0 as f64).sqrt() '
                                   '* (-2.0 as f64).sqrt(), -2.0);`',
                  'c_location': 'artifacts/rustmap/c-code/rosseta-125/approximate_equility/main.c:21 `test(-sqrt(2.0) * sqrt(2.0), -2.0);`'}],
  'record': {'status': 'iii_no_verdict (raw divergent diff hunk shipped, never interpreted as pass or fail)',
             'evidence': 'artifacts/rustmap/c-code/rosseta-125/original_rosetta_result.log:432 (C output) vs '
                         'artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/rustmap_rosetta_result.log:443 (Rust output, same wrong output as '
                         'our run); raw hunk(s) in artifacts/rustmap/c-code/rosseta-125/diff_rosetta_result.log at line(s) 13. README.md:291-294 only gives '
                         'the diff command; no verdict, tally or pass/fail list for this task anywhere in artifacts/rustmap.'},
  'triage_note': 'Genuine, but low impact: only the printed value changes; the approxEquals verdict (0/false) is the same. Other lines (%f vs {} precision, '
                 '1/0 vs true/false) are cosmetic.'},
 {'task': 'hailstone_sequence',
  'severity': 'crash',
  'mechanisms': [{'mechanism': "C's NULL-pointer test `if (arry)` is translated as `if !arry.is_empty()`. The caller passes an empty Vec, so nothing is ever "
                               "recorded, and the print indexes an empty Vec: panic 'index out of bounds: the len is 0 but the index is 0' at "
                               'hailstone_sequence-c.rs:33:13 (exit 101).',
                  'rust_location': 'artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/hailstone_sequence-c.rs:6 and :11 `if !arry.is_empty() '
                                   '{` (caller line 29-30 passes `Vec::new()`; panic at line 33 `arry[0]`)',
                  'c_location': 'artifacts/rustmap/c-code/rosseta-125/hailstone_sequence/main.c:10 and :13 `if (arry) *arry++ = n;` (caller line 31-32 passes '
                                "a malloc'd buffer)"}],
  'record': {'status': 'iii_no_verdict for the shipped translation (empty Rust section; panic on stderr not logged). Separate record (ii) of a FAILED EARLIER '
                       'VERSION.',
             'evidence': 'C output at artifacts/rustmap/c-code/rosseta-125/original_rosetta_result.log:1979; '
                         "artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/rustmap_rosetta_result.log:1957 has the header 'Compiling and "
                         "running hailstone_sequence-c.rs' followed directly by the next task (no output; stderr not captured); hunk at "
                         'artifacts/rustmap/c-code/rosseta-125/diff_rosetta_result.log:842. Separately, '
                         'artifacts/rustmap/rust-code/rosetta_code_gpt/11-failed-rossta-code-gpt/hailstone_sequence.rs is an earlier, different translation '
                         '(uses Option) whose compile failure is logged in rust-code/rosetta_code_gpt/rust_errors/hailstone_sequence.log:1-53 (4 errors '
                         'E0277/E0308). That is a recorded compile fail of a predecessor, not of the shipped file.'},
  'triage_note': 'None. C is UB-clean (max trajectory value < INT_MAX for j < 100000).'},
 {'task': 'matrix_chain_multiplication',
  'severity': 'crash',
  'mechanisms': [{'mechanism': "The whole DP cost table is initialised to usize::MAX instead of C's calloc zeros, so the base case m[i][i] is MAX. The first "
                               "cost = m[i][k] + m[k+1][j] + temp overflows: debug panic 'attempt to add with overflow' at "
                               'matrix_chain_multiplication-c.rs:12:28 (exit 101). Under -O it wraps and gives wrong costs.',
                  'rust_location': 'artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/matrix_chain_multiplication-c.rs:3 `let mut m = '
                                   'vec![vec![usize::MAX; n]; n];` (overflow at line 12)',
                  'c_location': 'artifacts/rustmap/c-code/rosseta-125/matrix_chain_multiplication/main.c:13 `m[i] = (int *)calloc(n, sizeof(int));` (only '
                                'm[i][j], j>i, set to INT_MAX at line 24)'}],
  'record': {'status': 'iii_no_verdict (raw divergent diff hunk shipped, never interpreted as pass or fail)',
             'evidence': 'artifacts/rustmap/c-code/rosseta-125/original_rosetta_result.log:2188 (C output) vs '
                         'artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/rustmap_rosetta_result.log:2085 (Rust output, same wrong output as '
                         'our run); raw hunk(s) in artifacts/rustmap/c-code/rosseta-125/diff_rosetta_result.log at line(s) 1064. README.md:291-294 only gives '
                         'the diff command; no verdict, tally or pass/fail list for this task anywhere in artifacts/rustmap. The shipped Rust log shows only '
                         "'Dims  : [[5, 6, 3, 1]]' (the panic went to stderr, which was not captured)."},
  'triage_note': "None. The '[[...]]' double brackets (line 43 `[{:?}]`) are cosmetic."}]

CURATED_BZIP2 = {'defects': [{'id': 'BZ-1 high-byte clamp',
              'mechanism': 'bz_stream.next_in is declared *mut i8, so `*strm.next_in as u32` sign-extends every input byte >= 0x80 to 0xFFFFFF80..0xFFFFFFFF, '
                           "which the next line clamps to 255. Every byte 0x80-0xFF is therefore stored (and CRC'd) as 0xFF. The stream is self-consistent: "
                           'bzip2 -d reports matching CRCs and silently outputs 0xFF in place of every high byte. C casts through (UChar*) first.',
              'rust_location': 'artifacts/rustmap/rust-code/bzip2_rs_gpt/src/bzlib/scc_30_copy_input_until_stop_31_copy_output_until_stop.rs:26-29 `let mut '
                               'zchh = unsafe { *strm.next_in as u32 }; if zchh > 255 { zchh = 255; }` (type at '
                               'artifacts/rustmap/rust-code/bzip2_rs_gpt/src/global_vars/bzip2_c1.rs:560 `pub next_in: *mut i8`)',
              'c_location': 'artifacts/rustmap/c-code/bzip2-1.0.8/bzlib.c:302 `ADD_CHAR_TO_BLOCK ( s, (UInt32)(*((UChar*)(s->strm->next_in))) );`',
              'inputs_affected': 'Every input containing a byte >= 0x80, i.e. all binary inputs (10..2048 B). Never printable text.',
              'evidence': 'Run (Docker, --cpus=1, aarch64 crust-stable, debug build): binary 10 B (check-script seed) 0d f2 6d 12 a4 86 7b 44 73 5b. '
                          'bzip2recover + bzip2 -dvvvv: block CRC {0x2a5fcbc5, 0x2a5fcbc5} match, exit 0; output 0d ff 6d 12 ff ff 7b 44 73 5b (cmp fails). '
                          "Same for binary 20 B, binary 64 B and 'ab\\x80cd' -> 'ab\\xffcd': all CRC-OK silent corruption."},
             {'id': 'BZ-2 run-length encoding of runs >= 5',
              'mechanism': 'add_pair_to_block writes state_in_len copies of the byte followed by the count byte (len-4) for every run of 4..255. C writes '
                           'exactly 4 copies plus (len-4). For len >= 5 the decoder reads the 5th copy as the repeat count, so output length and content '
                           "change. The block CRC is still computed over the true run, so bzip2 -d stops with 'Data integrity error' (CRC mismatch).",
              'rust_location': 'artifacts/rustmap/rust-code/bzip2_rs_gpt/src/bzlib/scc_29_root_22_add_pair_to_block.rs:16-23 (`match s.state_in_len { 1 | 2 | '
                               '3 | _ => { for _ in 0..s.state_in_len { s.block[s.nblock] = ch; ... } } }`) and 31-37 (count byte)',
              'c_location': 'artifacts/rustmap/c-code/bzip2-1.0.8/bzlib.c:237-243 (`default:` writes ch exactly 4 times, then `s->block[s->nblock] = '
                            '((UChar)(s->state_in_len-4));`)',
              'inputs_affected': 'Any input with a run of >= 5 identical bytes, text or binary. In binary input BZ-1 creates such runs (about half of all '
                                 'bytes become 0xFF), so binary 128/512 B (and in all likelihood 1000/2048 B) end in a CRC error. Binary 10/20/64 B with the '
                                 'check-script seeds have no post-clamp run >= 5 and fail via BZ-1 alone.',
              'evidence': "Text 'xaaaaaby' (run of 5, all ASCII): bzip2 -d 'Data integrity error', block CRC {stored 0x5a9221b1, computed 0xe70f40a2}. A "
                          "CRC-disabled C decoder outputs 'x' + 101 'a' + 0x01 + 'by' (105 B). 'xaaaaby' (run of 4) passes. 'xyaaaaaaaaaz' (run of 9) also "
                          'gives a CRC error (113 B). Binary 128 B (max raw run 1, max post-clamp run 5) and 512 B (post-clamp run 11): CRC error.'},
             {'id': 'BZ-3 1-byte panic (fallbackSort ftab sizing)',
              'mechanism': "fallbackSort sizes ftab as (max of the u32-packed eclass words)+1 instead of C's fixed 257 entries, then zeroes ftab[0..257]. For "
                           "a 1-byte block the only non-zero word is that byte (< 256), so the loop indexes past the end: panic 'index out of bounds: the len "
                           "is b+1 but the index is b+1' at scc_71_fallbackSort.rs:140:13 (exit 101). Only the 5000-byte zero prefix (BZ-4) has been written "
                           'by then.',
              'rust_location': 'artifacts/rustmap/rust-code/bzip2_rs_gpt/src/blocksort/scc_71_fallbackSort.rs:120,124 `let max_eclass = '
                               '*eclass.iter().max()...; let mut ftab: Vec<i32> = vec![0; max_eclass + 1];`, panic at :140 `ftab[i as usize] = 0;` in `for i '
                               'in 0..257` (eclass = u32 view of block, from scc_72_BZ2_blockSort.rs:46/49)',
              'c_location': 'artifacts/rustmap/c-code/bzip2-1.0.8/blocksort.c:218 `Int32 ftab[257];` and :231 `for (i = 0; i < 257; i++) ftab[i] = 0;`',
              'inputs_affected': '1-byte inputs of any kind (binary and text). In general, any block whose packed 32-bit words are all < 256. Every input of '
                                 '2+ bytes with a non-zero byte past the first gets a huge but lazily-zeroed ftab and does not panic here.',
              'evidence': "Binary 1 B (0x74): 'index out of bounds: the len is 117 but the index is 117' at src/blocksort/scc_71_fallbackSort.rs:140:13, rc "
                          "101, data.bz2 = 5000 zero bytes. Text 1 B ('/', 0x2f): 'the len is 48 but the index is 48', same location."}],
 'prefix': {'id': 'BZ-4 5000-zero-byte prefix',
            'mechanism': 'In BZ2_bzWrite the loop sets avail_in = len where C sets avail_out = BZ_MAX_UNUSED (5000). avail_out stays at its default 0, so '
                         "after each BZ_RUN call the 'avail_out < 5000' branch writes n = 5000 - 0 = 5000 bytes of the untouched, zero-initialised output "
                         'buffer. The real stream is written later by bzWriteClose64, which does reset avail_out = 5000.',
            'rust_location': 'artifacts/rustmap/rust-code/bzip2_rs_gpt/src/bzlib/scc_73_BZ2_bzWrite.rs:48 `bzf.strm.avail_in = len as u32;` (should be '
                             'avail_out = 5000), write at :57-59; defaults at artifacts/rustmap/rust-code/bzip2_rs_gpt/src/global_vars/bzip2_c1.rs:519 `buf: '
                             '[0; 5000]` and :528 `avail_out: 0`; close path correct at bzlib/scc_75_BZ2_bzCompress_76_BZ2_bzWriteClose64.rs:141',
            'c_location': 'artifacts/rustmap/c-code/bzip2-1.0.8/bzlib.c:988 `bzf->strm.avail_out = BZ_MAX_UNUSED;`',
            'effect': 'Each bzWrite call (one per 5000-byte read chunk in compress_stream) prepends 5000 zero bytes. Inputs <= 5000 B get exactly one prefix. '
                      "Plain `bzip2 -d` rejects the file; RustMap's README check passes only because bzip2recover scans past the prefix for the block magic. "
                      "So the check's design hides this defect.",
            'is_distinct_defect': 'Yes, a fourth independent mechanism (output framing), in a different function from BZ-1..3.',
            'evidence': "Every Rust output we produced starts with exactly 5000 zero bytes followed by 'BZh9'. The 1-byte panic case leaves a 5000-byte "
                        'all-zero .bz2. All shipped Rust outputs (rust-code/bzip2_testcases/random_{1,10,100,1000}_chars.txt.bz2 and '
                        'compress_output_bz2_files/random_{1,10,5000}_chars.txt.bz2) start with 5000 zero bytes.'},
 'record': {'status': 'i_implicit_pass on 5 printable-text inputs only (no stated verdict). No recorded fail.',
            'evidence': 'README.md:229-275 (4.1.2 compress, 4.1.3 bzip2recover + bzip2 -d + diff) writes diff_random_*.txt, but those diff files are not '
                        'shipped. Shipped artifacts/rustmap/rust-code/bzip2_testcases/compress_output_bz2_files/rec00001random_{1,10,100,1000,5000}_chars.txt '
                        'are byte-identical to the inputs random_{1,10,100,1000,5000}_chars.txt (cmp), which is an implicit pass of their own check. Those '
                        'inputs are printable ASCII from random-test-case-generation.py (string.printable): 0 bytes >= 0x80, max run length 2. '
                        "'random_1_chars.txt' is actually 2 bytes ('C\\n'), so it never exercised the 1-byte panic. None of BZ-1/2/3 can trigger on these "
                        'inputs. timings.txt records only run times. Oddities: compress_output_bz2_files/random_100_chars.txt.bz2 and '
                        "random_1000_chars.txt.bz2 are 6148-byte macOS .DS_Store files (magic 00 00 00 01 'Bud1'), not bzip2 streams. No pass/fail table, log "
                        'or success list for bzip2 anywhere in artifacts/rustmap.'},
 'unconfirmed': 'Runs were on a native aarch64 debug build in crust-stable (not the x86 crust-syzygy used for results/rustmap_bzip2_check.json). To compile on '
                'aarch64, one line in the scratch copy was patched: scc_84_...saveInputFileMetaInfo.rs:31 cast `as *const i8` -> `as *const libc::c_char` '
                '(utime timestamps only, unrelated to compression). BZ-1/2/3 depend on explicit i8/u32 types, not on platform c_char, so they should behave '
                'the same on x86, but we did not re-run on x86. Binary 1000/2048 B were not re-run. Their CRC-error-vs-silent split is inferred: post-clamp '
                'runs >= 5 are near-certain at those sizes, and both mechanisms fail the check either way. Not traced: the full Huffman/MTF path for inputs > '
                '5000 B (multiple bzWrite calls), and the slowness (rebuild_arr*/update_arr2 per write), which is performance, not a divergence.',
 'crc_note': "The binary failure is not always a CRC error. With the check script's own seeds, binary 10/20/64 B decompress with matching CRCs and differ only "
             "by high bytes becoming 0xFF (BZ-1). A CRC error ('Data integrity error') appears only when a run of >= 5 identical bytes exists after clamping "
             '(BZ-2): observed for binary 128 and 512 B. results/rustmap_bzip2_check.json records only pass/fail, not the error kind. The 5000-B rows (rc 124) '
             "are 60-s timeouts of the very slow debug build (RustMap's own timings.txt: 7m31s for 5000 chars), not divergences."}


# ============================================================================================ BUILD
def load(p):
    with open(R(p)) as f:
        return json.load(f)


def src_lines(path):
    with open(R(path), errors="replace") as f:
        return f.read().splitlines()


CHECKS = []   # (what, ok, detail): every citation/assumption build() verified


def check(what, ok, detail=""):
    CHECKS.append({"check": what, "ok": bool(ok), "detail": detail})
    return ok


def cite(path, a, b=None, needles=()):
    """Return 'path:a-b' after verifying each needle occurs within lines a..b (1-based)."""
    b = b or a
    L = src_lines(path)
    seg = "\n".join(L[a - 1:b])
    for n in needles:
        check(f"citation {path}:{a}-{b} contains {n!r}", n in seg)
    return f"{path}:{a}" + (f"-{b}" if b != a else "")


def parse_loc(loc):
    """'path:12-15 ...' -> (path, 12, 15)."""
    m = re.match(r"(artifacts/[^:\s]+):(\d+)(?:-(\d+))?", loc)
    return (m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))) if m else (None, None, None)


def verify_loc(loc):
    p, a, b = parse_loc(loc)
    if not p:
        return check(f"location parse {loc[:80]}", False)
    ok = os.path.isfile(R(p)) and len(src_lines(p)) >= b
    return check(f"location exists {p}:{a}-{b}", ok)


def git_rev():
    """HEAD of the repository the ledger is built in. This artifact repository is published as one amended commit,
    whose hash at build time never resolves later; there the ledger records that instead (its inputs are pinned by
    sha256 in provenance)."""
    run = lambda *a: subprocess.run(["git", "-C", REPO, *a], capture_output=True, text=True).stdout.strip()
    return "artifact repository (one amended commit, so no stable hash; inputs are pinned by sha256)" if run("rev-list", "--count", "HEAD") == "1" else run("rev-parse", "HEAD")


def sha(path):
    with open(R(path), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def row(system, translation, defect_id, mechanism, source_location, severity, example_input, c_output,
        rust_output, exposure_count, exposure_breakdown, acceptance_status, acceptance_evidence, **extra):
    assert acceptance_status in ("accepted-recorded", "accepted-rerun", "rejected", "not-exercised", "no-record"), acceptance_status
    r = dict(system=system, translation=translation, defect_id=defect_id, mechanism=mechanism,
             source_location=source_location, severity=severity, example_input=example_input,
             c_output=c_output, rust_output=rust_output, exposure_count=exposure_count,
             exposure_breakdown=exposure_breakdown, acceptance_status=acceptance_status,
             acceptance_evidence=acceptance_evidence)
    r.update(extra)
    return r


# ------------------------------------------------------------------------------------ C2SaferRust
def build_c2saferrust(probe_json):
    rows = []
    probes = {p["id"]: p for p in (probe_json or {}).get("rows", [])}
    acc = load("results/c2saferrust_acceptance_suite.json")
    per = {d["id"]: {"cases": [], "kinds": collections.Counter(), "s1": collections.Counter(), "patterns": collections.Counter(),
                     "folded": set(), "noloc": collections.Counter(), "loc": collections.Counter(), "baseline_mismatch": 0} for d in C2SR_DEFECTS}
    grid_distinct = {}
    totals = {}
    for util in ["cat", "head", "tail", "uniq"]:
        cs = {c["id"]: c for c in load(f"results/{util}_cases_v4.json")["cases"]}
        base = {v["id"]: v for v in load(f"results/{util}_diff_v4_c2rust_baseline.json")["verdicts"]}
        dd = load(f"results/{util}_diff_v4.json")
        grid_distinct[util] = {"cases": len(cs),
                               "distinct_inputs_incl_locale": len({(tuple(c["argv"]), c["stdin_b64"], json.dumps(c["files"], sort_keys=True), c["note"], json.dumps(c["env"], sort_keys=True)) for c in cs.values()}),
                               "distinct_inputs_ignoring_locale": len({(tuple(c["argv"]), c["stdin_b64"], json.dumps(c["files"], sort_keys=True), c["note"]) for c in cs.values()})}
        n_mis = 0
        for v in dd["verdicts"]:
            if not v["mismatch"]:
                continue
            n_mis += 1
            c = cs[v["id"]]
            ids, kind = c2sr_assign(util, v, c)
            n_op = {"stdin": 0, "file": 1, "missing-file": 1, "is-a-directory": 1, "two-files": 2, "ok+missing": 2}[c["note"]]
            flags = " ".join(v["argv"][:len(v["argv"]) - n_op]) or "(none)"
            fold = (tuple(v["argv"]), c["stdin_b64"], json.dumps(c["files"], sort_keys=True), c["note"])
            for i in ids:
                P = per[i]
                P["cases"].append(v["id"])
                P["kinds"][kind] += 1
                P["patterns"][f"{util} {flags} [{c['note']}]"] += 1
                P["folded"].add(fold)
                P["loc" if c["env"] else "noloc"][fold] += 1
                if kind == "S1":
                    P["s1"]["stdout differs" if not v["stdout_match"] else "exit code only"] += 1
                if base.get(v["id"], {}).get("mismatch"):
                    P["baseline_mismatch"] += 1
        totals[util] = {"cases": dd["summary"]["n_cases"], "stdout_or_exit_mismatches": n_mis}
    # uniq -i: the mechanism predicts a crash iff two adjacent lines have equal length; test it on every -i case
    cs = {c["id"]: c for c in load("results/uniq_cases_v4.json")["cases"]}
    pred = collections.Counter()
    for v in load("results/uniq_diff_v4.json")["verdicts"]:
        c = cs[v["id"]]
        if "-i" not in v["argv"] or c["note"] == "is-a-directory":
            continue
        data = base64.b64decode(c["files"]["in1.txt"]) if c["note"] in ("file", "two-files", "ok+missing") else \
            base64.b64decode(c["stdin_b64"]) if c["note"] == "stdin" else None
        if data is None:   # missing-file: nothing is read
            pred[("no input read", v["sut_exit"] in (-11, 139))] += 1
            continue
        delim = b"\0" if "-z" in v["argv"] else b"\n"
        lines = data.split(delim)
        if lines and lines[-1] == b"":
            lines = lines[:-1]
        eq = any(len(lines[k]) == len(lines[k + 1]) > 0 for k in range(len(lines) - 1))
        pred[(f"adjacent equal-length non-empty lines={eq}", v["sut_exit"] in (-11, 139))] += 1
    uniq_pred = {f"{k[0]}; crashed={k[1]}": n for k, n in sorted(pred.items())}
    check("uniq -i: crash iff two adjacent non-empty lines of equal length (every -i case that reads input)",
          all((("=True" in k[0]) == k[1]) for k in pred if k[0] != "no input read") and all(not k[1] for k in pred if k[0] == "no input read"),
          json.dumps(uniq_pred))

    for d in C2SR_DEFECTS:
        util = d["util"]
        P = per[d["id"]]
        rp, ra, rb, rn = d["rust"]
        cp, ca, cb, cn = d["c"]
        loc_r = cite(rp, ra, rb, rn)
        loc_c = cite(cp, ca, cb, cn)
        # loop-log entry for the rewrite that produced the faulty code
        logp = f"{C2S}/{util}/rust_WIP/log.txt"
        slices = load(f"{C2S}/{util}/rust_WIP/slices.json")
        fn = [f for f in slices if f["func_defid"].endswith(f"::{d['fn']})")]
        check(f"{d['id']}: function {d['fn']} found in slices.json", len(fn) == 1)
        rel = rp.split("/rust_WIP/")[1]

        def span_has(span, line):
            m = re.match(r"(\S+):(\d+):\d+: (\d+):\d+", span)
            return m and m.group(1) == rel and int(m.group(2)) <= line <= int(m.group(3))

        chunk = d["chunk"]
        if d.get("callsite"):   # the faulty code is a call site rewritten together with fn (translate.py <CALL> tags)
            sites = [c["span"] for c in fn[0]["calls"]]
            check(f"{d['id']}: faulty line {rel}:{ra}-{rb} is a rewritten call site of {d['fn']} {sites}",
                  any(span_has(s, ra) for s in sites) and any(span_has(s, rb) for s in sites))
        if chunk is None:   # find the innermost chunk whose final span contains the faulty line
            cands = [(i, ch["span"]) for i, ch in enumerate(fn[0]["chunks"]) if span_has(ch["span"], ra)]
            cands.sort(key=lambda x: int(re.match(r"\S+:(\d+):\d+: (\d+)", x[1]).group(2)) - int(re.match(r"\S+:(\d+)", x[1]).group(1)))
            chunk = str(cands[0][0]) if cands else "root"
        span = fn[0]["span"] if chunk == "root" else fn[0]["chunks"][int(chunk)]["span"]
        if not d.get("callsite"):
            check(f"{d['id']}: faulty line {rel}:{ra} lies in {d['fn']} chunk {chunk} span {span}", span_has(span, ra))
        log_hits = [(i, l.strip()) for i, l in enumerate(open(R(logp)), 1)
                    if f"::{d['fn']})" in l and f" Chunk {chunk}," in l]
        check(f"{d['id']}: loop log has an entry for {d['fn']} chunk {chunk}", len(log_hits) == 1, str(log_hits))
        log_ok = bool(log_hits) and ", Success," in log_hits[0][1]
        check(f"{d['id']}: that entry records Success", log_ok)
        u = acc["per_util"][util]
        sel = [t for t, r in u["rows"].items() if r["start_pass"] > 0]
        fails = u.get("wip_fails_selected", [])
        status = "rejected" if fails else "accepted-recorded"
        ev = [f"Loop log records acceptance of the rewrite that produced this code: {logp}:{log_hits[0][0]} '{log_hits[0][1]}' (translate.py:759-761 writes Success only after the rewrite compiled and every selected test passed)." if log_hits else "no loop-log entry",
              f"Our re-run of its acceptance procedure on the shipped rust_WIP tree (results/c2saferrust_acceptance_suite.json, 3 runs): {util} selected tests {sel}; shipped tree fails {fails or 'none'}."]
        if probes:
            pr = [probes[p] for p in d["probes"] if p in probes]
        else:
            pr = []
        ex = pr[0] if pr else None
        n_cases = len(P["cases"])
        check(f"{d['id']}: C2Rust starting translation agrees on every exposing case", P["baseline_mismatch"] == 0, str(P["baseline_mismatch"]))
        rows.append(row(
            "C2SaferRust", f"coreutils {util} (rust_WIP crate)", d["id"], d["mechanism"],
            {"rust": loc_r, "c": loc_c}, d["severity"],
            example_input=(ex["command"] + (f"  (stdin {ex['stdin']!r})" if ex["stdin"] else "")) if ex else None,
            c_output=({"exit": ex["c"]["exit"], "stdout": ex["c"]["stdout"], "stderr": ex["c"]["stderr"]} if ex else None),
            rust_output=({"exit": ex["rust"]["exit"], "stdout": ex["rust"]["stdout"], "stderr": ex["rust"]["stderr"]} if ex else None),
            exposure_count=n_cases,
            exposing_case_ids=P["cases"],
            exposure_breakdown={"cases": n_cases,
                                "cases_locale_folded": sum(P["noloc"].values()) + sum(max(0, n - P["noloc"][k]) for k, n in P["loc"].items()),
                                "distinct_inputs": len(P["folded"]),
                                "by_outcome": dict(P["kinds"]), "S1_kind": dict(P["s1"]) or None,
                                "by_flags_and_input_source": dict(P["patterns"].most_common())},
            acceptance_status=status, acceptance_evidence=ev,
            check_exercises_defect={"status": d["exercised"][0], "evidence": d["exercised"][1]},
            introduced_by="C2SaferRust's LLM rewrite: the C2Rust starting translation (rust/) agrees with the C on every exposing case (results/" + util + "_diff_v4_c2rust_baseline.json)",
            probe=[{"id": p["id"], "command": p["command"], "c": p["c"], "rust": p["rust"], "rust_gdb_backtrace": p.get("rust_gdb_backtrace")} for p in pr] or None,
            notes=("Exposure overlaps C2SR-tail-2: both faults are needed for the crash on `-n -1`; tail-3 alone shows on invalid numbers (probe tail-n-abc), which the grid does not contain." if d["id"] == "C2SR-tail-3" else
                   "Exposure overlaps C2SR-tail-3 (the crash happens in tail-3's code)." if d["id"] == "C2SR-tail-2" else None)))
    allc = set()
    for d in C2SR_DEFECTS:
        allc |= {(d["util"], i) for i in per[d["id"]]["cases"]}
    kinds_distinct = collections.Counter()
    inp_noloc, inp_loc = collections.defaultdict(set), collections.defaultdict(set)
    for util in ["cat", "head", "tail", "uniq"]:
        cs = {c["id"]: c for c in load(f"results/{util}_cases_v4.json")["cases"]}
        for v in load(f"results/{util}_diff_v4.json")["verdicts"]:
            if (util, v["id"]) in allc:
                e = v["sut_exit"]
                k = "crash" if e in (-11, 139, 101) else "S1"
                kinds_distinct[k] += 1
                c = cs[v["id"]]
                key = (util, tuple(c["argv"]), c["stdin_b64"], json.dumps(c["files"], sort_keys=True), c["note"])
                inp_noloc[k].add(key)
                inp_loc[k].add(key + (json.dumps(c["env"], sort_keys=True),))
    check("C2SaferRust: every stdout/exit mismatch is assigned to a defect",
          sum(kinds_distinct.values()) == sum(t["stdout_or_exit_mismatches"] for t in totals.values()), str(kinds_distinct))
    return rows, {"per_util": totals, "grid_duplicates": grid_distinct, "distinct_exposed_cases": dict(kinds_distinct),
                  "distinct_exposed_inputs": {"incl_locale": {k: len(v) for k, v in inp_loc.items()},
                                              "ignoring_locale": {k: len(v) for k, v in inp_noloc.items()},
                                              "note": "the grid repeats each missing-file / is-a-directory command line once per stdin body (13x), because those scenarios read no body"}, "uniq_i_prediction": uniq_pred, "probe": probe_json,
                  "acceptance_suite": {u: {"selected": [t for t, r in acc["per_util"][u]["rows"].items() if r["start_pass"] > 0],
                                           "shipped_fails": acc["per_util"][u].get("wip_fails_selected", [])} for u in ["cat", "head", "tail", "uniq"]}}


# ------------------------------------------------------------------------------------------- VERT
def vert_records():
    rec = {}
    for i, r in enumerate(csv.reader(open(R("artifacts/vert/c_results.csv"))), 1):
        if r:
            rec[r[0].strip()] = (i, {kv.split("=")[0].strip(): kv.split("=")[1].strip() for kv in r[1:] if "=" in kv})
    return rec


# VERT scalar results: the grid-v2 run is primary (systems/enumerate_inputs.py grid="v2";
# results/vert_diff.json, grid v1, is kept as the legacy run and is no longer read here).
VERT_SCALAR = "results/vert_diff_grid2.json"
VERT_SCALAR_UB = "results/vert_grid2_ub_recheck.json"          # full UB rule, scalar wrong answers on grid v2
VERT_ARRAY_UB = "results/vert_wrong_answer_ub_recheck.json"     # full UB rule, array/float wrong answers (unchanged)
VERT_SCALAR_HELDOUT = "results/heldout_vert_grid2.json"         # held-out / own inputs, scalar rows on grid v2
# arXiv (issues.md 47): the scalar functions whose C returns long / long long, tested with the same harness, grid and
# rules (systems/vert/run.py --wide-int). Each file is merged by function name into the one above it.
VERT_SCALAR_WIDE = "results/arxiv_vert_diff_grid2_wide.json"
VERT_SCALAR_WIDE_UB = "results/arxiv_vert_wide_ub_recheck.json"
VERT_SCALAR_WIDE_HELDOUT = "results/arxiv_heldout_vert_wide.json"
VERT_WIDE_BOLERO = "results/arxiv_vert_wide_bolero_replay.json"
VERT_WIDE_KANI_UNWIND = "results/arxiv_vert_wide_kani_unwinding_on.json"


def vert_scalar_all():
    """(rows, value_bugs, source per function) of the scalar harness: the 176 int-return functions plus the wide ones."""
    rows, vb, src = [], [], {}
    for path in (VERT_SCALAR, VERT_SCALAR_WIDE):
        d = load(path) if path == VERT_SCALAR else opt_load(path)
        if not d:
            continue
        for r in d["all"]:
            if r.get("status") == "tested":
                check(f"VERT scalar: {r['func']} tested once", r["func"] not in src)
                rows.append(r)
                src[r["func"]] = path
        vb += d["value_bugs"]
    return rows, vb, src
# array/float harness after the audit 8 fixes (per-input fallback, float equality, sanitizer rule); per function
# identical to the earlier results/vert_arrays.json except one non-counted ub_only row's agree/timeout split
VERT_ARRAYS = "results/vert_arrays_fixed.json"


def build_vert():
    rows = []
    rec = vert_records()
    _sc_rows, _, sc_src = vert_scalar_all()
    sc = {r["func"]: r for r in _sc_rows}
    ar = {r["func"]: r for r in load(VERT_ARRAYS)["all"] if r.get("status") == "tested"}
    adj = {r["func"]: r["kind"] for r in load("results/vert_arrays_adjusted.json")["rows"]}
    kani = {r["function"]: r for r in load("results/vert_kani_vacuity.json")["rows"]}
    kon = {r["function"]: r for r in load("results/vert_kani_unwinding_on.json")["rows"]}
    kon_src = {f: "results/vert_kani_unwinding_on.json" for f in kon}
    for r in (opt_load(VERT_WIDE_KANI_UNWIND) or {}).get("rows", []):
        kon.setdefault(r["function"], r)
        kon_src.setdefault(r["function"], VERT_WIDE_KANI_UNWIND)
    bol = {r["function"]: r for r in load("results/vert_bolero_vacuity.json")["rows"]}
    # functions the older 26-function bolero run did not cover: the current-set run (same arms)
    bol_cur = {r["function"]: r for r in (opt_load("results/vert_bolero_vacuity_current.json") or {}).get("rows", [])}
    bol_cur_src = {f: "results/vert_bolero_vacuity_current.json" for f in bol_cur}
    for r in (opt_load(VERT_WIDE_BOLERO) or {}).get("rows", []):
        bol_cur.setdefault(r["function"], r)
        bol_cur_src.setdefault(r["function"], VERT_WIDE_BOLERO)
    cur = {x["func"]: x for x in CURATED_VERT}
    # the set of translations we count must equal the result files' set
    want = {f for f, r in sc.items() if r["value_div"] or r.get("rust_panic_c_ok") or r.get("rust_hang")} | \
           {f for f, r in ar.items() if adj.get(f) in ("value", "rust_crash")}
    check("VERT: curated mechanisms cover exactly the functions with S1/crash/hang in the result files",
          set(cur) == want, f"missing={sorted(want - set(cur))} extra={sorted(set(cur) - want)}")
    # array/float: functions with an assessable input, the same set the totals' translations_tested counts
    # (results/vert_arrays_adjusted.json kind != no_assessable_input; 116 of the 117 tested rows)
    tested = {"scalar": len(sc), "array/float": len([f for f in ar if adj.get(f) != "no_assessable_input"])}
    for f in sorted(cur, key=lambda f: (cur[f]["family"], f)):
        x = cur[f]
        if x["family"] == "scalar":
            r = sc[f]
            exp = {"S1": r["value_div"], "crash": r.get("rust_panic_c_ok", 0), "hang": r.get("rust_hang", 0)}
            tried = r["cases"]
            samples = [(s["in"], s["c"], s["rust"]) for s in (r.get("value_samples") or [])]
            psamples = [(s["in"], s.get("c"), "panic") for s in (r.get("rust_panic_samples") or [])]
            src = sc_src[f]
        else:
            r = ar[f]
            # audit 8 harness: a Rust hang is its own count (rust_hang); "timeout" is the C side (no line / sanitizer timeout)
            exp = {"S1": r["value"] if adj[f] == "value" else 0, "crash": r["crash"], "hang": r.get("rust_hang", 0) if adj[f] != "ub_only" else 0}
            tried = r["agree"] + r["value"] + r["crash"] + r["ub"] + r["timeout"] + r.get("rust_hang", 0) + r.get("san_unresolved", 0)
            samples = [(s["in"], s["c"], s["rust"]) for s in r.get("samples", [])]
            psamples = [(s["in"], s.get("c"), "crash") for s in r.get("crash_samples", [])]
            src = f"{VERT_ARRAYS} + results/vert_arrays_adjusted.json"
        sev_present = [s for s in ("S1", "crash", "hang") if exp[s]]
        mechs = x["mechanisms"]
        if len(mechs) == 1:
            assign = [(mechs[0], sev_present[0], {s: exp[s] for s in sev_present})]
        else:   # two mechanisms: first explains the wrong values, second the crashes (checked)
            check(f"VERT {f}: two mechanisms map to S1 and crash", sev_present == ["S1", "crash"], str(sev_present))
            assign = [(mechs[0], "S1", {"S1": exp["S1"]}), (mechs[1], "crash", {"crash": exp["crash"]})]
        line, rr = rec.get(f, (None, {}))
        k, ko, b = kani.get(f), kon.get(f), bol.get(f)
        b_src = "results/vert_bolero_vacuity.json"
        if b is None and f in bol_cur:
            b, b_src = bol_cur[f], bol_cur_src[f]
        status = "accepted-recorded" if rr.get("bolero") == "1" else "rejected"
        ev = [f"artifacts/vert/c_results.csv:{line} records compile={rr.get('compile')}, bolero={rr.get('bolero')}, bounded_kani={rr.get('bounded_kani')}, full_kani={rr.get('full_kani')} (VERT's evaluator marks a translation verified at the highest tier it passes)."]
        cov = []
        if k:
            cov.append(f"bounded Kani re-run with VERT's command and Kani 0.55.0: verdict {k['verdict']}, equivalence assertion {k['equivalence_assertion']} (results/vert_kani_vacuity.json)")
        if ko:
            cov.append(f"same with unwinding checks on: {ko['verdict']} on {ko.get('failed_checks')}, equivalence assertion {ko['equivalence_assertion']} ({kon_src[f]})")
        if b:
            vr = b["arms"]["vert_reduce"]
            cov.append(f"documented bolero step (cargo bolero reduce) re-run: outcome {vr.get('outcome')}, VERT's rule says {vr.get('vert_verdict')} ({b_src})")
        cdir = R(f"artifacts/vert/benchmark/c_transcoder/{f}/out-rwasm-bolero/src/__fuzz__/bolero_wasm_eq/corpus")
        ncorp = len(os.listdir(cdir)) if os.path.isdir(cdir) else 0
        cov.append(f"VERT's documented bolero step replays the shipped corpus, which has {ncorp} file(s) for this function")
        tiers = {"bolero": "unknown: the recorded pass may come from a fuzzing run whose inputs were not shipped; the documented replay step has "
                           + ("no input to run" if ncorp == 0 else f"{ncorp} input(s)"),
                 "bounded_kani": ("not-exercised: the equivalence assertion is unreachable in the re-run of VERT's command" if k and k["equivalence_assertion"] == "UNREACHABLE"
                                  else "not recorded as a pass" if rr.get("bounded_kani") != "1" else f"re-run verdict {k['verdict'] if k else 'n/a'}"),
                 "full_kani": "not recorded as a pass" if rr.get("full_kani") != "1" else "recorded pass"}
        cs = "unknown"
        for i, (m, sev, ex) in enumerate(assign, 1):
            verify_loc(m["rust_location"])
            verify_loc(m["c_location"])
            smp = (psamples if sev == "crash" else samples) or samples
            s0 = smp[0] if smp else (None, None, None)
            rows.append(row(
                "VERT", f"{f} (function, TransCoder c_transcoder)", f"VERT-{f}-{i}", m["mechanism"],
                {"rust": m["rust_location"], "c": m["c_location"]}, sev,
                example_input=("f_gold(" + ", ".join(map(str, s0[0])) + ")") if s0[0] is not None else None, c_output=s0[1], rust_output=s0[2],
                exposure_count=sum(ex.values()),
                exposure_breakdown={"by_outcome": ex, "inputs_tried": tried, "family": x["family"], "source": src},
                acceptance_status=status, acceptance_evidence=ev,
                check_exercises_defect={"status": cs, "per_tier": tiers, "evidence": "; ".join(cov)},
                mistake_class=x.get("mistake_class"), triage_note=x.get("triage_note")))
    return rows, {"translations_tested": tested}


# --------------------------------------------------------------------------------------- Flourine
# arXiv (2026-11-16): FLOURINE's verdicts come from the profile its own check builds the Rust in.
# FLOURINE's check runs `cargo bolero test`, i.e. cargo-bolero's fuzz profile (opt-level 3, overflow checks on);
# the submitted version built with rustc -O (results/flourine_diff.json, flourine_struct.json). The shipped
# comparison was re-run in the fuzz profile (same grid, C oracle and UB rule): results/arxiv_flourine_devprofile*.json.
FL_SCALAR = "results/arxiv_flourine_devprofile_scalar.json"      # harness/oracle_ub_flourine_current.py --profile fuzz
FL_STRUCT = "results/arxiv_flourine_devprofile_struct.json"
FL_PROFILE = "results/arxiv_flourine_devprofile.json"             # harness/arxiv_flourine_devprofile.py (release vs fuzz)
FL_HELDOUT_PROFILE = "results/arxiv_heldout_flourine_fuzzprofile.json"   # harness/arxiv_heldout_flourine_fuzz.py


def _fl_clean(x):
    u = x.get("ub_rule") or {}
    return (u.get("value_inputs_ub_clean") or 0) + (u.get("crash_inputs_ub_clean") or 0)


def build_flourine():
    rows = []
    table = list(csv.reader(open(R(f"{FL}/all_statistics/new_big_table.csv"))))
    hdr = table[0]
    idx = {h: i for i, h in enumerate(hdr)}
    sd, st, fp = load(FL_SCALAR), load(FL_STRUCT), load(FL_PROFILE)
    check("Flourine fuzz-profile run: per-translation totals equal the shipped scripts' summaries",
          (sd.get("consistency") or {}).get("per_translation_totals_equal_shipped_summary") is True
          and (st.get("consistency") or {}).get("per_translation_totals_equal_shipped_summary") is True)
    recs = {}      # every success-labelled file that diverges in the fuzz profile: tag -> (record, source, driver)
    for x in sd["per_translation"]:
        if x.get("label") == "success" and x.get("status") == "tested" and x.get("verdict") not in (None, "agree"):
            recs[x["tag"]] = (x, FL_SCALAR, "scalar")
    for x in st["per_translation"]:
        if x.get("label") == "success" and x.get("status") == "tested" and x.get("verdict") not in (None, "agree"):
            recs["opl/" + x["tag"]] = (x, FL_STRUCT, "struct")
    divs = {t: v for t, v in recs.items() if _fl_clean(v[0]) > 0}           # at least one UB-clean exposing input
    ub_only = sorted(t for t in recs if t not in divs)                        # divergences only where the C has UB
    fex = {}
    for c in fp.get("changes", []):
        t = c["tag"] if c["driver"].startswith("scalar") else "opl/" + c["tag"]
        fex[t] = [e for e in ((c.get("fuzz_examples") or {}).get("crash") or []) if e.get("ub_clean") and not e.get("degenerate_min_gt_max")]
    covered = set()
    for d in FLOURINE_DEFECTS:
        files = sorted(t for t in divs if t.startswith(d["benchmark"] + "/"))
        covered |= set(files)
        exp, clean, ev, locs, ex, kinds = {}, {}, [], [], None, set()
        for t in files:
            x, src, drv = divs[t]
            proj, bm, llm, strat = t.split("/")[:4]
            ta = x["tally"]
            exp[t] = (ta.get("value") or 0) + (ta.get("rust_panic") or 0) + (ta.get("crash") or 0)
            clean[t] = _fl_clean(x)
            u = x.get("ub_rule") or {}
            kinds |= ({"value"} if u.get("value_inputs_ub_clean") else set()) | ({"crash"} if u.get("crash_inputs_ub_clean") else set())
            hit = [(i + 1, r) for i, r in enumerate(table) if i and r[idx["project"]] == proj and r[idx["benchmark_name"]] == bm
                   and r[idx["llm"]] == llm and r[idx["feedback_strategy"]] == strat]
            check(f"Flourine record row for {t}", len(hit) == 1)
            ln, r = hit[0]
            ev.append(f"{FL}/all_statistics/new_big_table.csv:{ln} final_status={r[idx['final_status']]} ({r[idx['status_detail']]}); shipped under {FL}/translations_only/success/c/{t} (README:52: 'Success' = 'a translation that passed the oracle').")
            rpath = f"{FL}/translations_only/success/c/{t}"
            L = src_lines(rpath)
            nd = d["rust_needle"] if isinstance(d["rust_needle"], list) else [d["rust_needle"]]
            if d["rust_lines"]:
                a, b = d["rust_lines"]
                hit_n = nd
            else:
                n0 = next(n for n in nd if any(n in l for l in L))   # needles in priority order
                a = b = next(i for i, l in enumerate(L, 1) if n0 in l)
                hit_n = [n0]
            locs.append(cite(rpath, a, b, hit_n))
            if ex is None:
                if "value" in kinds and x.get("value_samples"):
                    s0 = x["value_samples"][0]
                    ex = (s0.get("in") or s0.get("line_in"), s0["c"], s0["rust"])
                elif fex.get(t):
                    s0 = fex[t][0]
                    ex = (s0["in"], s0["c"], f"panic ('{d.get('panic')}'): our driver prints __PANIC__")
        sev = "S1" if "value" in kinds else "crash"
        if d["c"][2:] and isinstance(d["c"][2], int):
            cp, ca, cb, cn = d["c"]
        else:
            cp, ca, cn = d["c"]
            cb, cn = ca, [cn]
        rows.append(row(
            "Flourine", f"{d['benchmark']} ({len(files)} shipped 'success' file(s): {', '.join(t.split('/', 2)[2] for t in files)})",
            d["id"], d["mechanism"], {"rust": locs, "c": cite(cp, ca, cb, cn)},
            sev, example_input=str(ex[0]) if ex else None, c_output=ex[1] if ex else None, rust_output=ex[2] if ex else None,
            exposure_count=sum(exp.values()),
            exposure_breakdown={"diverging_inputs_per_file": exp, "source": divs[files[0]][1] if files else None,
                                "ub_clean_diverging_inputs_per_file": clean,
                                "rust_profile": "fuzz (FLOURINE's check profile: opt-level 3, overflow checks on; " + FL_PROFILE + ")"},
            acceptance_status="accepted-recorded", acceptance_evidence=ev,
            check_exercises_defect={"status": "unknown", "evidence": "Flourine's fuzzer inputs are not shipped; the record says the fuzzer passed the translation."},
            degenerate_input_only=d["degenerate"], translations_with_defect=len(files), notes=d["note"],
            _fl_records={t: divs[t][0] for t in files}))
    check("Flourine: every 'success' divergence with a UB-clean exposing input (fuzz profile) is covered by a ledger row",
          covered == set(divs), str(sorted(set(divs) - covered)))
    return rows, {"translations_tested": {"scalar_success": sd["summary"]["success_tested"], "struct_success": st["summary"]["success_tested"]},
                  "fail_labelled_divergent_not_in_ledger": {"scalar": sd["summary"]["fail_diverge"], "struct": st["summary"]["fail_diverge"]},
                  "rust_profile": {"profile": "fuzz", "rustc_flags": (sd["summary"].get("rust_profile") or {}).get("rustc_flags"),
                                   "why": "FLOURINE's own check builds with cargo-bolero's fuzz profile (" + FL_PROFILE + " flourine_check_profile)",
                                   "sources": [FL_SCALAR, FL_STRUCT, FL_PROFILE, FL_HELDOUT_PROFILE]},
                  "success_divergences_only_on_c_ub_inputs": ub_only,
                  "release_profile_success_divergences": {"scalar": [x["t"] for x in load("results/flourine_diff.json")["success_divergences"]],
                                                          "struct": [x["t"] for x in load("results/flourine_struct.json")["success_divergences"]],
                                                          "note": "the submitted version's rustc -O run; every counted file there is counted here"}}


# ------------------------------------------------------------------------------------- RustAssure
def build_rustassure():
    rows = []
    for d in RUSTASSURE_DEFECTS:
        res = load(d["results"])
        rp, ra, rb = d["rust"]
        cp, cl, cn = d["c"]
        if cl is None:
            cl = next(i for i, l in enumerate(src_lines(cp), 1) if cn in l)
        loc = {"rust": cite(rp, ra, rb, []), "c": cite(cp, cl, cl, [cn])}
        smp = res["samples"]
        if "libcsv" in d["id"]:
            ok = all(any(o != s and o.startswith("F NULL") and s.startswith("F ") for o, s in zip(x["oracle"], x["sut"])) and
                     all(o == s or (o.startswith("F NULL") and s == "F 0") for o, s in zip(x["oracle"], x["sut"])) and len(x["oracle"]) == len(x["sut"])
                     for x in smp)
            check("RustAssure libcsv: every divergent case differs only by 'F NULL' (C) vs 'F 0' (Rust) field events", ok and len(smp) == res["divergent"])
            by = collections.Counter(x["options"] for x in smp)
            ex = smp[0]
            exp = {"cases": res["divergent"], "of": res["n_cases"], "by_option_set": dict(by), "inputs": sorted({x["input"] for x in smp})}
            example = (f"csv_parse(corpus '{ex['input']}', options {ex['options']})", ex["oracle"], ex["sut"])
        else:
            def diff_ok(x):
                pairs = [(o, s) for o, s in zip(x["oracle"], x["sut"]) if o != s]
                return pairs and all(o.split()[0] == "D" and o.split()[-1] == "-1" and s.split()[-1] == "-1" for o, s in pairs)
            check("RustAssure u8c: every divergent case differs only in the code point of an invalid (-1) decode", all(diff_ok(x) for x in smp) and len(smp) == res["divergent"])
            ex = smp[0]
            exp = {"cases": res["divergent"], "of": res["n_cases"], "inputs": res.get("divergent_inputs")}
            example = (f"u8next_ over bytes {ex['hex']} ({ex['input']})", ex["oracle"], ex["sut"])
        rows.append(row("RustAssure", d["translation"], d["id"], d["mechanism"], loc, d["severity"],
                        example_input=example[0], c_output=example[1], rust_output=example[2],
                        exposure_count=res["divergent"], exposure_breakdown=exp,
                        acceptance_status=d["acceptance_status"], acceptance_evidence=d["acceptance_evidence"],
                        check_exercises_defect={"status": d["exercised"][0], "evidence": d["exercised"][1]},
                        rerun_probe=RUSTASSURE_RERUN if "libcsv" in d["id"] else None))
    return rows, {}


# ----------------------------------------------------------------------------------------- Syzygy
def build_syzygy():
    d = SYZYGY_DEFECT
    res = load("results/syzygy_empty_deflate.json")
    rp, ra, rb, rn = d["rust"]
    cp, ca, cb, cn = d["c"]
    check("Syzygy: every btype gives zero Rust bytes that zlib rejects, C output valid", all((not x["rust_hex"]) and (not x["rust_inflate"]["ok"]) and x["c_inflate"]["ok"] for x in res["rows"]))
    x = res["rows"][1]
    return [row("Syzygy", "Zopfli (library; ZopfliDeflate)", d["id"], d["mechanism"],
                {"rust": cite(rp, ra, rb, [rn]), "c": cite(cp, ca, cb, [cn])}, d["severity"],
                example_input=f"ZopfliDeflate(btype={x['btype']}, final=1, in=\"\", insize=0)", c_output=x["c_hex"], rust_output=x["rust_hex"] or "(0 bytes)",
                exposure_count=len(res["rows"]), exposure_breakdown={"btypes": [r["btype"] for r in res["rows"]], "source": "results/syzygy_empty_deflate.json"},
                acceptance_status=d["acceptance_status"], acceptance_evidence=d["acceptance_evidence"],
                check_exercises_defect={"status": d["exercised"][0], "evidence": d["exercised"][1]})], {}


# ---------------------------------------------------------------------------------------- RustMap
def build_rustmap():
    rows = []
    tri = load("results/rustmap_rosetta_triage.json")
    samples = {v["task"]: v for v in load("results/rustmap_rosetta.json")["divergence_samples"]}
    counted = dict(tri["wrong_output_S1"], **tri["rust_panic"])
    cur = {x["task"]: x for x in CURATED_ROSETTA}
    check("RustMap Rosetta: curated mechanisms cover exactly the counted programs", set(cur) == set(counted), str(set(cur) ^ set(counted)))
    for t in sorted(cur):
        x = cur[t]
        sev = "crash" if t in tri["rust_panic"] else "S1"
        check(f"RustMap {t}: severity matches triage", sev == x["severity"])
        m0 = x["mechanisms"][0]
        for m in x["mechanisms"]:
            verify_loc(m["rust_location"])
            verify_loc(m["c_location"])
        rec = x["record"]
        sm = samples.get(t, {})
        rows.append(row(
            "RustMap", f"Rosetta {t} (program)", f"RUSTMAP-rosetta-{t}-1",
            " ".join(m["mechanism"] for m in x["mechanisms"]) if t == "formatted_numeric_output" else m0["mechanism"],
            {"rust": [m["rust_location"] for m in x["mechanisms"]] if t == "formatted_numeric_output" else m0["rust_location"],
             "c": m0["c_location"]}, sev,
            example_input="(no input: the program prints its own fixed cases)", c_output=sm.get("c"), rust_output=sm.get("rust"),
            exposure_count=1, exposure_breakdown={"runs": 1, "note": "one run per program; the input is fixed, in-distribution (RustMap's own run), not held out", "source": "results/rustmap_rosetta.json"},
            acceptance_status="rejected",
            acceptance_evidence=[f"RustMap's documented check is a whole-log diff of all C vs all Rust outputs (artifacts/rustmap/README.md, section 4.2.2); our re-run of it (results/rustmap_rosetta.json) differs on this program.",
                                 f"Shipped record: {rec['status']}. {rec['evidence']}",
                                 "RustMap ships the raw diff (artifacts/rustmap/c-code/rosseta-125/diff_rosetta_result.log) but reports no verdict drawn from it."],
            check_exercises_defect={"status": "exercised-failed", "evidence": "The program's only run is the one the diff compares."},
            other_mechanisms=[m["mechanism"] for m in x["mechanisms"][1:]] if t not in ("formatted_numeric_output",) and len(x["mechanisms"]) > 1 else None,
            triage_note=x.get("triage_note")))
    # bzip2
    bz = load("results/rustmap_bzip2_check.json")
    rws = bz["rows"]
    binfail = [r for r in rws if r["kind"] == "binary" and r["size"] >= 10 and r["rust_rc"] == 0 and not r["rust_passes_rustmap_own_check"]]
    onebyte = [r for r in rws if r["size"] == 1 and r["rust_rc"] == 101]
    textpass = [r for r in rws if r["kind"] == "text" and r["rust_passes_rustmap_own_check"]]
    tc = R("artifacts/rustmap/rust-code/bzip2_testcases")
    shipped = []
    for dp, _, fs in os.walk(tc):
        for fn in fs:
            if fn.endswith(".bz2"):
                with open(os.path.join(dp, fn), "rb") as f:
                    head = f.read(5003)
                shipped.append((os.path.relpath(os.path.join(dp, fn), REPO), head[:5000] == b"\0" * 5000 and len(head) > 5000, head[:4]))
    D = {d["id"].split()[0]: d for d in CURATED_BZIP2["defects"]}
    p4 = CURATED_BZIP2["prefix"]
    bzrec = CURATED_BZIP2["record"]
    pb = PROBE_BZIP2 or {"rows": []}
    chk = [r for r in pb["rows"] if "check_bzip2.sh seed" in r["input"]]
    iso = {r["input"]: r for r in pb["rows"] if "check_bzip2.sh seed" not in r["input"]}
    if pb["rows"]:
        check("bzip2 probe regenerates the 20 inputs of results/rustmap_bzip2_check.json",
              [(r["input"].split()[0], r["size"]) for r in chk] == [(r["kind"], r["size"]) for r in rws])
        check("bzip2 probe: the own-check verdict agrees with results/rustmap_bzip2_check.json wherever that file graded the input",
              all(p["own_check_output_equals_input"] == r["rust_passes_rustmap_own_check"] for p, r in zip(chk, rws) if r["rust_rc"] in (0, 101)))
        check("bzip2 probe: every silent (CRC-valid) corruption changes only bytes >= 0x80, each into 0xFF",
              all(r.get("every_change_is_high_byte_to_0xFF") for r in pb["rows"] if r.get("changed_bytes")))
        check("bzip2 probe: CRC error iff the longest run after clamping bytes >= 0x80 to 0xFF is >= 5 (non-panicking inputs)",
              all(r["own_check_crc_error"] == (r["max_run_after_clamp"] >= 5) for r in pb["rows"] if r["rust_exit"] == 0))
    bz1 = [r for r in chk if r["bytes_ge_0x80"] > 0 and r["rust_exit"] == 0]
    bz2 = [r for r in chk if r["rust_exit"] == 0 and r.get("own_check_crc_error")]
    bz3 = [r for r in chk if r["rust_exit"] == 101]
    bz4 = [r for r in chk if r.get("starts_with_5000_zero_bytes")]
    common_ev = [f"Shipped record: {bzrec['status']} {bzrec['evidence']}",
                 f"Our re-run of RustMap's own check (results/rustmap_bzip2_check.json) passes {len(textpass)} printable-text inputs and fails every binary input from 10 B to 2048 B."]
    specs = [
        ("BZ-1", D["BZ-1"], "S1", len(bz1),
         {"held-out binary inputs (all contain bytes >= 0x80)": [r["input"] for r in bz1],
          "silently corrupted, CRC valid (every changed byte is a byte >= 0x80 turned into 0xFF)": [r["input"] for r in bz1 if r.get("changed_bytes")],
          "CRC error (BZ-2 also hit, after clamping)": [r["input"] for r in bz1 if r.get("own_check_crc_error")]},
         ("not-exercised", "RustMap's own test inputs are printable ASCII (no byte >= 0x80)."),
         iso.get("text 'ab\\x80cd' (one high byte)")),
        ("BZ-2", D["BZ-2"], "S1", len(bz2),
         {"held-out inputs with a CRC error (each has a run >= 5 after clamping)": [f"{r['input']} (max run after clamp {r['max_run_after_clamp']})" for r in bz2],
          "isolating pair (no high bytes)": {k: iso[k].get("own_check_crc_error") for k in iso if "run of" in k}},
         ("not-exercised", "RustMap's own test inputs have runs of at most 2 identical bytes."),
         iso.get("text 'xaaaaaby' (run of 5)")),
        ("BZ-3", D["BZ-3"], "crash", len(bz3), {"1-byte inputs": [r["input"] for r in bz3]},
         ("not-exercised", "RustMap's 'random_1_chars.txt' is 2 bytes ('C\\n'), so its own inputs never include a 1-byte file."),
         bz3[0] if bz3 else None),
        ("BZ-4", p4, "S1", len(bz4),
         {"held-out inputs whose Rust .bz2 starts with 5000 zero bytes (plain bzip2 -t rejects each)": [r["input"] for r in bz4],
          "authors' shipped .bz2 outputs starting with 5000 zero bytes": [s[0] for s in shipped if s[1]],
          "shipped .bz2 files without the prefix": [f"{s[0]} (magic {s[2]!r})" for s in shipped if not s[1]]},
         ("exercised-passed", "RustMap's check runs bzip2recover before bzip2 -d (README 4.1.3), which skips the zero prefix, so the check passes by design."),
         bz4[0] if bz4 else None),
    ]
    for key, d, sev, n, br, exr, ex in specs:
        rl = d["rust_location"]
        verify_loc(rl)
        verify_loc(d["c_location"])
        if ex:
            ein = ex["input"]
            eco = "C bzip2 round-trips it"
            ero = {"rust_exit": ex["rust_exit"], "rust_stderr_tail": ex["rust_stderr"][-160:] if ex["rust_exit"] else "",
                   "starts_with_5000_zero_bytes": ex.get("starts_with_5000_zero_bytes"), "plain_bzip2_accepts": ex.get("plain_bzip2_d_accepts_rust_output"),
                   "own_check_crc_error": ex.get("own_check_crc_error"), "own_check_output_equals_input": ex.get("own_check_output_equals_input"),
                   "changed_bytes": ex.get("changed_bytes")}
        else:
            ein = eco = ero = None
        rows.append(row("RustMap", "bzip2 (program, artifacts/rustmap/rust-code/bzip2_rs_gpt)", f"RUSTMAP-bzip2-{key}", d["mechanism"],
                        {"rust": rl, "c": d["c_location"]}, sev, example_input=ein, c_output=eco, rust_output=ero,
                        exposure_count=n if pb["rows"] else None,
                        exposure_breakdown=dict(br, of=f"{len(chk)} held-out inputs of systems/rustmap/check_bzip2.sh, regenerated by the bzip2 probe",
                                                source="bzip2 probe (meta.RustMap.bzip2_probe) + results/rustmap_bzip2_check.json"),
                        acceptance_status="accepted-recorded", acceptance_evidence=common_ev,
                        check_exercises_defect={"status": exr[0], "evidence": exr[1]}))
    return rows, {"bzip2_probe": PROBE_BZIP2}


# -------------------------------------------------------------------------------------- totals
TESTED = {}
PROBE_BZIP2 = None


def exposure_by_outcome(rows):
    """Sum exposure by observed outcome (a row's inputs can show more than one outcome, e.g. a VERT
    function whose one mechanism gives wrong values on some inputs and a panic on another)."""
    c = collections.Counter()
    for r in rows:
        bo = (r.get("exposure_breakdown") or {}).get("by_outcome")
        if bo:
            for k, v in bo.items():
                c["crash" if k.startswith("crash") else k] += v
        elif r["exposure_count"] is not None:
            c[r["severity"]] += r["exposure_count"]
    return dict(c)


def totals(rows, extra):
    out = {}
    for sysname in ["VERT", "Flourine", "RustAssure", "C2SaferRust", "Syzygy", "RustMap"]:
        rs = [r for r in rows if r["system"] == sysname]
        main = [r for r in rs if r["severity"] in ("S1", "crash") and not r.get("degenerate_input_only")]
        tr = collections.OrderedDict()
        for r in main:
            tr[r["translation"]] = tr.get(r["translation"], 0) + 1
        n_tr = sum(r.get("translations_with_defect", 1) for r in {r["translation"]: r for r in main}.values()) if sysname == "Flourine" else len(tr)
        out[sysname] = {
            "defects": {"S1": sum(r["severity"] == "S1" for r in main), "crash": sum(r["severity"] == "crash" for r in main), "total": len(main)},
            "translations_with_defect": n_tr,
            "translations_tested": TESTED.get(sysname),
            "exposure_by_outcome": exposure_by_outcome(main),
            "exposure": {"distinct_cases": extra.get(sysname, {}).get("distinct_exposed_cases"),
                         "S1": sum((r["exposure_count"] or 0) for r in main if r["severity"] == "S1"),
                         "crash": sum((r["exposure_count"] or 0) for r in main if r["severity"] == "crash"),
                         "unmeasured_defects": [r["defect_id"] for r in main if r["exposure_count"] is None]},
            "acceptance_status_of_defects": dict(collections.Counter(r["acceptance_status"] for r in main)),
            "check_exercises_defect": dict(collections.Counter(r["check_exercises_defect"]["status"] for r in main)),
            "not_in_totals": {r["defect_id"]: ("hang" if r["severity"] == "hang" else "diverges only on degenerate min>max input") for r in rs if r not in main},
        }
    return out



# ============================================================================================ V2
# Ledger v2 applies five rules uniformly:
#  (1) acceptance: the released record of the system's own check decides when one exists; only
#      without a record does a re-run (documented command, the system's own inputs) decide. A
#      disagreeing re-run is kept in `rerun_disagrees`; a conflicting second record in `record_conflict`.
#  (2) one UB rule (results/oracle_ub_*.json): a defect counts only if at least one exposing input
#      has C that is clean under clang ASan+full UBSan and gcc ASan+UBSan, gives the same output at
#      -O0/-O1(/-O2), and does not depend on char signedness; exposure counts only those inputs.
#  (3) one precondition rule: a divergence that occurs only on inputs violating a precondition the
#      original states (name, comment, documentation) is `precondition_only`, not counted.
#  (4) explicit Rosetta triage rules (ROSETTA_RULES), re-applied to all 37 differing programs.
#  (5) held-out status: `found_on` (our_inputs / own_inputs / both) and `heldout`
#      (outside / inside / not-measurable), from results/heldout_*.json and heldout_overlap.json.
COUNT_STATUSES = ("counted", "counted-ub-pending", "excluded_original_c_ub", "precondition_only",
                  "hang-not-counted", "ingest-incomplete")
NEW_SYSTEMS = ["his2trans", "adatrans", "actoruw", "sactor", "ptrtrans"]
EXTRA_SUMMARY_DIRS = []   # test hook (--extra-summary-dir); results/ is always read


def opt_load(p):
    try:
        return load(p)
    except (OSError, ValueError):
        return None


def run_not_clean(x):
    """Audit 8 uniform rule on ONE recorded sanitizer / opt-level / char-signedness run: a run that
    failed (sanitizer infra), timed out, or disagrees with the reference leaves the input not UB-clean
    (harness/oracle_ub_common.not_clean_reasons; the ledger re-applies it to the stored records)."""
    return bool(isinstance(x, dict) and (x.get("infra") or x.get("timed_out") or x.get("same_as_ref") is False
                                         or x.get("same_stdout_rc_as_ref") is False))


def clean_case_c2sr(v):
    """oracle_ub_c2saferrust verdict -> True iff the C passes the full UB rule on this case."""
    if not v or not v.get("reproduced", True):
        return False
    if v.get("c_ub_evidence") or v.get("char_signedness_dependent") or v.get("san_infra") or v.get("ub_rule_not_clean"):
        return False
    if any(run_not_clean(v.get(k)) for k in ("san_clang", "san_gcc")):
        return False
    for k in ("o1", "o2", "uchar", "schar"):
        if k in v and not v[k].get("same_as_ref", True):
            return False
    for k in ("san_clang", "san_gcc"):
        if v.get(k, {}).get("reports"):
            return False
    return v.get("final_class") != "original_C_UB"


def clean_generic(v, builds=("san_clang", "san_gcc", "o0", "o1", "o2", "uchar", "schar")):
    if not v or v.get("c_ub_evidence") or v.get("char_signedness_dependent"):
        return False
    for k in builds:
        x = v.get(k)
        if isinstance(x, dict):
            if x.get("reports") or x.get("san_reports") or x.get("c_roundtrip_ok") is False or run_not_clean(x):
                return False
    return not v.get("ub_rule_not_clean")


# ---- (4) Rosetta triage rules
ROSETTA_RULES = {
    "nondeterministic_or_UB": "The C's output differs between two runs or between -O0/-O1/-O2, or a sanitizer reports on the C (results/oracle_ub_rustmap_rosetta.json). Not counted.",
    "cosmetic": "After normalisation the outputs are equal. Normalisation: whitespace (spaces/tabs/blank lines) collapsed; case of nan/inf/true/false folded; quotes, brackets, commas and Rust Debug string rendering (surrounding quotes, escaped \\n) removed; runs of a separator character compared as one token; the program's own path in usage lines ignored; the C's 'no result' sentinel (0) equal to Rust's None; every number equal to the C's value at the precision the C prints. Not counted.",
    "different_task": "The Rust is a translation of a different program: none of the C's functions has a counterpart in the Rust (no function of the same role/name), so the output is not an attempt at the C's computation (file mix-up). Not counted.",
    "wrong_output": "Otherwise: the Rust has counterparts of the C's functions (the same task), and its output differs after normalisation, including when the Rust drops or alters the C's built-in data or omits part of the C's computation (the C program, data included, is the specification). Counted S1, or crash if the Rust exits by panic/signal.",
}
ROSETTA_V2 = {
    # task: (v1 label, v2 label, one-line justification)
    "array_length": ("different_task", "different_task", "Rust is an array-concatenation program (array_concat); the C has no such function and prints sizeof-based length."),
    "binary_search": ("different_task", "different_task", "Rust has only bin() printing binary digits; no counterpart of the C's bsearch/bsearch_r."),
    "lu_decomposition": ("different_task", "different_task", "Rust is a Luhn checker (luhn); none of the C's mat_* functions has a counterpart."),
    "n_queens_problem": ("different_task", "different_task", "Rust main only prints 1..10; no counterpart of the C's solve()."),
    "perfect_numbers": ("different_task", "different_task", "Rust main asks for an argument; no counterpart of the C's perfect()."),
    "runge_kutta_method": ("different_task", "different_task", "Rust computes semiprimes (semiprime); no counterpart of the C's rk4/rate."),
    "number_names": ("different_task", "wrong_output", "Rust translates say_hundred (a counterpart) but omits say_number/say_maxillion and the C's six test numbers, calling say_hundred(\"123\") only: same task, incomplete translation, like sexy_primes."),
    "numerical_integration": ("different_task", "wrong_output", "Rust has all five int_* counterparts, but its main replaces the C's test table (intervals, step counts, antiderivatives) with [0,1], 1000 steps and f(b)-f(a) of the integrand itself: same task, altered data."),
    "knapsack_problem_0_1": ("wrong_output_S1", "wrong_output", "knapsack() is a counterpart; the item table is truncated to its first entry: altered data of the same task (unchanged)."),
    "knapsack_problem_continuous": ("wrong_output_S1", "wrong_output", "Same task (counterpart structure), item table truncated to one entry (unchanged)."),
    "sexy_primes": ("wrong_output_S1", "wrong_output", "sieve() is a counterpart; the counting/printing part of main is missing: same task, incomplete translation (unchanged)."),
}
ROSETTA_NEW_DEFECTS = {
    "number_names": dict(mechanism="Incomplete translation: only say_hundred is translated; say_number and say_maxillion are missing and main calls say_hundred(\"123\") instead of say_number on the C's six numbers (-42, 1984, 10000, 1024, 1001001001001, a 57-digit number).",
                         rust="artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/number_names-c.rs:42-45",
                         c="artifacts/rustmap/c-code/rosseta-125/number_names/main.c:126-135"),
    "numerical_integration": dict(mechanism="main's test table rewritten: every integrand is integrated over [0,1] with 1000 steps (C: intervals [0,1],[1,100],[0,5000],[0,6000] with 100..6,000,000 steps), and the analytic value is f(b)-f(a) of the integrand itself instead of its antiderivative (C: f1a/f2a/f3a).",
                                  rust="artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/numerical_integration-c.rs:72-87",
                                  c="artifacts/rustmap/c-code/rosseta-125/numerical_integration/main.c:109-125"),
}

# ---- C2SaferRust: mismatches on the system's own (shell-test) inputs, mapped by mechanism
C2SR_OWN_MAP = [
    # (test, argv-prefix match, defect id or exclusion, justification)
    ("cat/cat-proc.sh", None, "excluded_nondeterministic_oracle", "reads /proc/cpuinfo; the C's output differs between two runs"),
    ("head/head-pos.sh", ["-n", "-1"], "C2SR-head-3", "4-byte regular-file stdin fits one buffer, so desired_pos is still (uintmax_t)-1 at head.rs:1253: same panic (probe head-n-minus-1). The regular file reaches elide_tail_lines_pipe through the routing inherited from the C2Rust tree: the struct stat that C2SaferRust's output keeps from the C2Rust starting tree has the aarch64 layout, so on x86-64 st_mode reads st_nlink, usable_st_size is false and current_pos stays -1 (results/arxiv_c2saferrust_head_aarch64.json); the faulty block is the rewrite's"),
    ("head/head-pos.sh", ["-n-50000"], "C2SR-head-5", "multi-buffer input: final partial buffer written one byte per line (gdb: p advanced 399 bytes for 399 lines at head.rs:1254). The regular file reaches elide_tail_lines_pipe through the routing inherited from the C2Rust tree: the struct stat that C2SaferRust's output keeps from the C2Rust starting tree has the aarch64 layout, so on x86-64 st_mode reads st_nlink, usable_st_size is false and current_pos stays -1 (results/arxiv_c2saferrust_head_aarch64.json)"),
    ("head/head-write-error.sh", ["--lines=-1"], "C2SR-head-5", "multi-buffer input: same final-buffer truncation on the fifo (under either routing) and on the 10 MiB regular file, which reaches elide_tail_lines_pipe with current_pos -1 through the routing inherited from the C2Rust tree, not the rewrite: the struct stat that C2SaferRust's output keeps from the C2Rust starting tree has the aarch64 layout, so on x86-64 st_mode reads st_nlink, usable_st_size is false and current_pos stays -1 (results/arxiv_c2saferrust_head_aarch64.json)"),
    ("tail/big-4gb.sh", ["-c1"], "C2SR-tail-1", "-c1 executed as -n1"),
    ("tail/pid.sh", None, "C2SR-tail-3", "--pid= with an empty value: xdectoumax's 'invalid PID' diagnostic calls quote(), which segfaults in the shadowed left_quote (gdb: quotearg.rs:381 <- xdectoumax <- parse_options tail.rs:4739)"),
    ("tail/pipe-f.sh", None, "excluded_environment_dependent", "follow mode on a pipe; two of the four also differ in the C2Rust starting translation, so a follow-mode/environment effect is present (the paper does not count follow-mode tail); the two rust_WIP-only ones are consistent with C2SR-tail-1 (-c3 as -n3)"),
    ("tail/tail-c.sh", ["-c", "-1"], "C2SR-tail-2", "-c -1: sign not stripped, then the quote() crash (C2SR-tail-3)"),
]
C2SR_HEAD5 = dict(
    id="C2SR-head-5", util="head", severity="S1",
    mechanism="head -n -N, final partial buffer (elide_tail_lines_pipe): the rewritten loop advances p by one byte per counted line (`p = &p[1..]`) instead of to the byte after the next line end (C: p = memchr(...) + 1), so only one byte per line of the last partial buffer is written. It shows when at least one full buffer was written first (desired_pos has wrapped, so C2SR-head-3's checked add does not fire), as on the piped input of head-write-error.sh (yes | head --lines=-1). On x86-64 regular files take this pipe path too (current_pos -1), a routing inherited from the C2Rust starting tree, not introduced by the rewrite: its struct stat has the aarch64 layout, so st_mode reads st_nlink and usable_st_size is false (results/arxiv_c2saferrust_head_aarch64.json). Through that routing the block also truncates regular files: head --lines=-1 on the 10 MiB bigseek file drops 4,095 bytes. The faulty block is the rewrite's under either routing (the C2Rust baseline agrees with the C under both).",
    rust=("artifacts/c2saferrust/coreutils/src/head/rust_WIP/src/head.rs", 1247, 1254, ["p = &p[1..];"]),
    c=("artifacts/c2saferrust/coreutils/src/head/c/head.c", 613, 620, ["while (n && (p = memchr (p, line_end, buffer_end - p)))"]),
    fn="elide_tail_lines_pipe", chunk="1")


def apply_v2(rows, meta):
    V2 = {"rules": {"acceptance": "record first; re-run only when no record (rerun_disagrees / record_conflict kept)",
                    "ub": "count only inputs whose C passes clang ASan+UBSan, gcc ASan+UBSan, -O0/-O1(/-O2) agreement, no char-signedness dependence",
                    "precondition": "precondition_only if every exposing input violates a precondition stated in the original's name, comment or documentation",
                    "rosetta_triage": ROSETTA_RULES}}
    by = {r["defect_id"]: r for r in rows}
    # ---------------- C2SaferRust
    oub = opt_load("results/oracle_ub_c2saferrust.json") or {"verdicts": []}
    ov = {v["id"]: v for v in oub["verdicts"]}
    hc = opt_load("results/heldout_c2saferrust.json")
    acc = load("results/c2saferrust_acceptance_suite.json")
    for r in [r for r in rows if r["system"] == "C2SaferRust"]:
        util = r["translation"].split()[1]
        ids = r.pop("exposing_case_ids")
        clean = [i for i in ids if clean_case_c2sr(ov.get(i))]
        nulls = sum(1 for i in ids if ov.get(i, {}).get("null_plus_zero_only"))
        r["exposure_before_ub_filter"] = r["exposure_count"]
        r["exposure_count"] = len(clean)
        r["ub_rule"] = {"status": "clean" if clean else "excluded_original_c_ub", "clean_inputs": len(clean), "of": len(ids),
                        "evidence": "results/oracle_ub_c2saferrust.json per-case verdicts" + (f"; {nulls} of {len(ids)} exposing cases are UB only through clang's 'applying zero offset to null pointer' at the C's linebuffer.c:65 (NULL+0: undefined in C17, defined from C2y/N3322; gcc does not flag it)" if nulls else "")}
        fails = acc["per_util"][util].get("wip_fails_selected", [])
        r["acceptance_status"] = "accepted-recorded"
        r["acceptance_basis"] = "record: rust_WIP/log.txt Success entry for the rewrite that produced the faulty code"
        r["rerun_disagrees"] = (f"Our re-run of the acceptance procedure fails the shipped {util} tree on {', '.join(fails)} (3 of 3 runs, results/c2saferrust_acceptance_suite.json)" if fails else None)
        r["record_conflict"] = None
    # arXiv: which head inputs reach elide_tail_lines_pipe only through the struct stat routing inherited
    # from the C2Rust tree (aarch64 layout read on x86-64); counts unchanged, all five head defects are the rewrite's
    hp = opt_load("results/arxiv_c2saferrust_head_aarch64.json")
    if hp:
        pc3 = hp["per_counted_defect"]["C2SR-head-3"]["by_run"]
        n_ship, n_ok = pc3["x86_64_asshipped"]["grid_cases"], pc3["x86_64_statx86"]["grid_cases"]
        check("C2SR-head-3: aarch64 routing check: x86-64 with corrected struct stat equals aarch64",
              pc3["x86_64_statx86"]["grid_cases"] == pc3["aarch64_charport"]["grid_cases"])
        for r in rows:
            if r["defect_id"] == "C2SR-head-3":
                check("C2SR-head-3: aarch64 file's x86-64 as-shipped exposure equals the ledger's", n_ship == r["exposure_before_ub_filter"],
                      f"{n_ship} vs {r['exposure_before_ub_filter']}")
                r["exposure_breakdown"]["routing_note"] = (
                    f"{n_ok} of the {n_ship} exposing grid cases are real pipes (stdin), which reach elide_tail_lines_pipe under any routing; "
                    f"the other {n_ship - n_ok} are regular files that reach it only because the struct stat C2SaferRust's output keeps "
                    "from the C2Rust starting tree has the aarch64 layout: on x86-64 st_mode reads st_nlink, usable_st_size is false and "
                    "current_pos stays -1. With the routing corrected (aarch64, or x86-64 with the x86-64 struct) only the pipe cases panic. "
                    "The routing is inherited; the faulty block (checked add, one byte per line) is the rewrite's, and the C2Rust baseline "
                    "agrees with the C under both routings (results/arxiv_c2saferrust_head_aarch64.json per_counted_defect.C2SR-head-3).")
                r["introduced_by_routing_check"] = "rewrite (the faulty block); the regular-file routing to it is inherited from the C2Rust tree (results/arxiv_c2saferrust_head_aarch64.json)"
    # new defect head-5 (found only on the system's own inputs)
    d = C2SR_HEAD5
    rp, ra, rb, rn = d["rust"]
    cp, ca, cb, cn = d["c"]
    logp = f"{C2S}/head/rust_WIP/log.txt"
    hits = [(i, l.strip()) for i, l in enumerate(open(R(logp)), 1) if "::elide_tail_lines_pipe)" in l and " Chunk 1," in l]
    check("C2SR-head-5: loop log Success for elide_tail_lines_pipe chunk 1", len(hits) == 1 and ", Success," in hits[0][1])
    own = []
    if hc:
        for t, v in hc["per_test"].items():
            for inv in v["invocation_list"]:
                rp_ = inv.get("replay", {})
                if rp_.get("mismatch") or rp_.get("oracle_deterministic") is False:
                    own.append((t, inv, rp_))
    own_rows = []
    for t, inv, rp_ in own:
        tgt = next((m for m in C2SR_OWN_MAP if m[0] == t and (m[1] is None or inv["argv"][:len(m[1])] == m[1])), None)
        check(f"C2SaferRust own-input mismatch {t} {inv['argv']} is mapped", tgt is not None)
        own_rows.append({"test": t, "argv": inv["argv"], "kind": rp_.get("mismatch_kind") or "oracle nondeterministic",
                         "c_exit": rp_.get("oracle_exit"), "rust_exit": rp_.get("sut_exit"),
                         "c_stdout_len": rp_.get("oracle_stdout_len"), "rust_stdout_len": rp_.get("sut_stdout_len"),
                         "also_in_c2rust_starting_translation": rp_.get("baseline_rust_mismatch"),
                         "mapped_to": tgt[2] if tgt else None, "why": tgt[3] if tgt else None})
    meta["C2SaferRust"]["own_input_mismatches"] = own_rows
    h5 = [o for o in own_rows if o["mapped_to"] == "C2SR-head-5"]
    rows.append(row("C2SaferRust", "coreutils head (rust_WIP crate)", d["id"], d["mechanism"],
                    {"rust": cite(rp, ra, rb, rn), "c": cite(cp, ca, cb, cn)}, d["severity"],
                    example_input="head --lines=-1 bigseek  (10 MiB file of 'y\\n', from head-write-error.sh)",
                    c_output={"stdout_bytes": next((o["c_stdout_len"] for o in h5 if "bigseek" in o["argv"]), None)},
                    rust_output={"stdout_bytes": next((o["rust_stdout_len"] for o in h5 if "bigseek" in o["argv"]), None)},
                    exposure_count=0, exposure_breakdown={"our_grid_cases": 0, "own_input_invocations": len(h5),
                                                          "own_inputs": [f"{o['test']} {' '.join(o['argv'])}: C {o['c_stdout_len']} B, Rust {o['rust_stdout_len']} B" for o in h5]},
                    acceptance_status="accepted-recorded",
                    acceptance_evidence=[f"Loop log records acceptance of the rewrite: {logp}:{hits[0][0]} '{hits[0][1]}'" if hits else "none"],
                    acceptance_basis="record: rust_WIP/log.txt Success", rerun_disagrees=None, record_conflict=None,
                    check_exercises_defect={"status": "exercised-passed", "evidence": "head-write-error.sh (in the acceptance set) runs head --lines=-1 on the 10 MiB file and on a pipe, but sends stdout to /dev/full and checks only the exit status and error message, so the truncated output passes."},
                    introduced_by="C2SaferRust's LLM rewrite (the C2Rust starting translation agrees on these invocations: results/heldout_c2saferrust.json baseline_rust_mismatch=false; and under the corrected struct stat routing: results/arxiv_c2saferrust_head_aarch64.json)",
                    routing_note=("Of the three own-input invocations, the piped one (head-write-error.sh, yes | head --lines=-1) truncates under either routing; "
                                  "head-pos.sh -n-50000 and head-write-error.sh --lines=-1 bigseek read regular files, which reach elide_tail_lines_pipe only "
                                  "through the struct stat routing inherited from the C2Rust tree (aarch64 layout read on x86-64). With the routing corrected "
                                  "those two print the right bytes and exit 1, a separate divergence of elide_tail_lines_seekable that x86-64 as shipped never "
                                  "reaches and the ledger does not count (results/arxiv_c2saferrust_head_aarch64.json new_divergence_with_correct_routing)."),
                    ub_rule={"status": "clean", "clean_inputs": None, "of": len(h5),
                             "evidence": "not in results/oracle_ub_c2saferrust.json (own-input replays are not UB-checked there); the same C code path is clean on all 80 head -n -N grid cases there"},
                    notes="UB status of the exact own-input invocations is not separately recorded; counted as clean by the same code path."))
    rows[-1]["ub_rule"]["status"] = "pending"
    # ---------------- Flourine
    # UB verdicts: the fuzz-profile run's per-translation ub_rule (uniform rule of harness/oracle_ub_flourine_current.py)
    hf = {x["translation"]: x for x in (opt_load("results/heldout_flourine.json") or {}).get("translations", [])}
    hf_src = {k: "results/heldout_flourine.json" for k in hf}
    for x in (opt_load(FL_HELDOUT_PROFILE) or {}).get("translations", []):      # fuzz-profile re-measurement of the
        hf[x["translation"]], hf_src[x["translation"]] = x, FL_HELDOUT_PROFILE   # files whose verdict the profile changes
    svl = opt_load("results/flourine_shipped_vs_log.json")
    svm = {}
    for x in ((svl or {}).get("rows") or (svl or {}).get("translations") or []):
        k = x.get("translation") or x.get("tag")
        if k:
            svm[k[:-3].rsplit("/", 1)[0] if k.endswith(".rs") else k] = x
    meta["Flourine"]["shipped_vs_log_totals"] = (svl or {}).get("totals")
    for r in [r for r in rows if r["system"] == "Flourine"]:
        files = list(r["exposure_breakdown"]["diverging_inputs_per_file"])
        clean, ev_ub, rec_rej, conflicts, found, held = {}, [], [], [], set(), set()
        flrec = r.pop("_fl_records")
        for t in files:
            u = flrec[t]
            ub = u.get("ub_rule") or {}
            clean[t] = _fl_clean(u)
            ndiv = (ub.get("value_inputs") or 0) + (ub.get("crash_inputs") or 0)
            ntried = (u.get("tally") or {}).get("cases") or sum((u.get("tally") or {}).values())
            ev_ub.append(f"{t}: {clean[t]} of {ndiv} diverging inputs ({ntried} tried) have UB-clean C")
            key = t.rsplit("/", 1)[0]
            h = hf.get(key)
            ident = (h or {}).get("identity", h or {})
            sv = svm.get(key)
            if sv is not None:   # most specific record: the log entry for the shipped file content
                rec_rej.append((sv.get("log_record_for_shipped_content"),
                                f"{key}: Flourine's log records the shipped content (sha256 {sv.get('shipped_sha256', '')[:12]}, = {sv.get('shipped_equals')}) as {sv.get('log_record_for_shipped_content')}; first verification {sv.get('first_verification')}, final verdict by {sv.get('final_verdict')}, accepted candidate {sv.get('accepted_candidate_file')} (results/flourine_shipped_vs_log.json)"))
            elif h and ident.get("shipped_equals_first_candidate_prog_0") and not ident.get("shipped_equals_verified_candidate"):
                ev = [e for e in h.get("flourine_log_events", []) if "not equivalent" in e.lower()]
                rec_rej.append(("not_equivalent", f"{key}: the shipped file (sha256 {ident.get('shipped_sha256', '')[:12]}) is byte-identical to Flourine's first candidate prog_0.rs, which its log records as {ev[0] if ev else 'not equivalent'!r}; the candidate it accepted (sha256 {ident.get('logs_wspace_src_lib_rs_sha256', '')[:12]}) is not shipped ({hf_src.get(key)})"))
            if h:
                if h.get("shipped_diverges_on_logged_inputs_iso_c_clean"):
                    found.add("own")
                held.add("outside" if h.get("counted_divergences_inside_logged_inputs") == [] and h.get("logged_inputs") else "not-measurable")
        conflicts = [e for e in r["acceptance_evidence"] if "new_big_table.csv" in e]
        r["exposure_before_ub_filter"] = r["exposure_count"]
        r["exposure_count"] = sum(v for v in clean.values() if v is not None) if any(v is not None for v in clean.values()) else r["exposure_count"]
        r["exposure_breakdown"]["ub_clean_diverging_inputs_per_file"] = clean
        r["exposure_breakdown"]["grid"] = f"current spread grid (2000 inputs per scalar translation; run_struct.py's 8 register fills per accessor) of {FL_SCALAR} / {FL_STRUCT}"
        r["ub_rule"] = {"status": "clean" if any(clean.values()) else ("pending" if all(v is None for v in clean.values()) else "excluded_original_c_ub"),
                        "clean_inputs": r["exposure_count"], "evidence": "; ".join(ev_ub) + f" ({FL_SCALAR}, {FL_STRUCT}: uniform rule of harness/oracle_ub_flourine_current.py rule_clean)"}
        verdicts = {v for v, _ in rec_rej}
        if rec_rej and len(rec_rej) == len(files) and verdicts <= {"not_equivalent", "equivalent"}:
            r["acceptance_status"] = "rejected" if "not_equivalent" in verdicts else "accepted-recorded"
            r["acceptance_basis"] = "record: Flourine's log entry for the shipped file content"
            r["record_conflict"] = ("all_statistics/new_big_table.csv labels the experiment Success and the file is shipped under success/: " + " | ".join(conflicts)) if "not_equivalent" in verdicts else None
            r["acceptance_evidence"] = [e for _, e in rec_rej]
            if len(verdicts) > 1:
                r["notes"] = (r.get("notes") or "") + " Files of this defect have different log records; status set by the not-equivalent one."
        else:
            r["acceptance_basis"] = "record: new_big_table.csv (log-vs-shipped identity not checked for this file)"
            r["record_conflict"] = None
        r["rerun_disagrees"] = None
        r["_found_own"] = "own" in found
        r["_heldout"] = held.pop() if len(held) == 1 else ("not-measurable" if not held else "outside")
        if r.get("degenerate_input_only"):
            r["precondition"] = {"status": "precondition_only",
                                 "evidence": f"Every diverging input has min > max; the C signature names the bounds ({r['source_location']['c']}: clip(a, amin, amax) / opl_emu_clamp(value, minval, maxval)), stating min <= max."}
    # ---------------- RustAssure
    our = opt_load("results/oracle_ub_rustassure.json") or {"verdicts": []}
    hr = opt_load("results/heldout_rustassure.json") or {}
    for r in [r for r in rows if r["system"] == "RustAssure"]:
        r["rerun_disagrees"] = None
        r["record_conflict"] = None
        if "libcsv" in r["defect_id"]:
            dv = [v for v in our["verdicts"] if v.get("divergent")]
            cl = [v for v in dv if clean_generic(v, ("san_clang", "san_gcc", "o0", "o1", "uchar", "schar"))]
            r["exposure_before_ub_filter"] = r["exposure_count"]
            r["exposure_count"] = len(cl)
            r["ub_rule"] = {"status": "clean" if cl else "excluded_original_c_ub", "clean_inputs": len(cl), "of": len(dv), "evidence": "results/oracle_ub_rustassure.json"}
            r["acceptance_basis"] = "none: no record of the per-function verdict; no faithful re-run (see rerun_probe)"
            oc = hr.get("our_comparison_on_rustassure_inputs", {})
            ocr = hr.get("own_check_run", {}).get("debug", {})
            r["_found_own"] = bool(oc.get("diverge_null_vs_empty"))
            r["_heldout"] = "outside" if hr.get("counted_defects", {}).get("inside_rustassure_set") == 0 else "not-measurable"
            r["own_inputs_note"] = (f"RustAssure's own composed-crate test (csv_link main.rs test_all_parse, {hr.get('system_check', {}).get('tests_called')} fixed inputs) contains {', '.join(oc.get('diverging_tests', []))}, which diverges under our comparison (NULL vs empty field); "
                                    f"its own run aborts ('{ocr.get('stderr_tail', '').strip()}', exit {ocr.get('exit')}) after test {ocr.get('last_header')}, before reaching test_19 (results/heldout_rustassure.json)")
        else:
            r["ub_rule"] = {"status": "pending", "clean_inputs": None, "evidence": "no oracle_ub file for u8c; note the C stores a plain char into *ch, so its printed value depends on char signedness (negative on x86-64), which the full rule must check"}
            r["acceptance_basis"] = "record: src/bug_description.csv lists the bug"
            r["_found_own"] = False
            r["_heldout"] = "not-measurable"
    # ---------------- Syzygy
    for r in [r for r in rows if r["system"] == "Syzygy"]:
        r.update(ub_rule={"status": "pending", "clean_inputs": None, "evidence": "only gcc ASan+UBSan -O0 recorded (results/syzygy_empty_deflate.json); no clang / opt-level / char-signedness check"},
                 acceptance_basis="none: no record, no faithful re-run", rerun_disagrees=None, record_conflict=None, _found_own=False, _heldout="not-measurable")
    # ---------------- RustMap
    ob = {(v["kind"], v["size"]): v for v in (opt_load("results/oracle_ub_rustmap_bzip2.json") or {"verdicts": []})["verdicts"]}
    hm = opt_load("results/heldout_rustmap.json") or {}
    sysin = hm.get("bzip2", {}).get("our_comparison_on_system_inputs", [])
    timings = src_lines("artifacts/rustmap/rust-code/bzip2_testcases/timings.txt") if os.path.isfile(R("artifacts/rustmap/rust-code/bzip2_testcases/timings.txt")) else []
    t5000 = None
    for i, l in enumerate(timings):
        if "random_5000_chars" in l:
            t5000 = next((f"timings.txt:{j + 1} '{timings[j].strip()}'" for j in range(i, min(i + 4, len(timings))) if timings[j].startswith("real")), None)
    passed = [x["input"] for x in sysin if x.get("rust_passes_rustmap_own_check")]
    notfin = [x["input"] for x in sysin if x.get("rust_rc") == 124]
    for r in [r for r in rows if r["system"] == "RustMap"]:
        r["rerun_disagrees"] = None
        r["record_conflict"] = None
        if "bzip2" in r["defect_id"]:
            labs = []
            for k, v in r["exposure_breakdown"].items():
                if isinstance(v, list):
                    labs += [x for x in v if isinstance(x, str) and "check_bzip2.sh seed" in x]
            labs = sorted(set(labs))
            keyed = [(l.split()[0], int(l.split()[1])) for l in labs]
            cl = [k for k in keyed if clean_generic(ob.get(k, {}), ())
                  and all(isinstance(ob.get(k, {}).get(b), dict) and ob[k][b].get("c_roundtrip_ok") and not ob[k][b].get("san_reports") for b in ("ref", "o0", "o1", "san_clang", "san_gcc", "uchar", "schar"))]
            r["ub_rule"] = {"status": "clean" if cl else ("pending" if not keyed else "excluded_original_c_ub"), "clean_inputs": len(cl), "of": len(keyed),
                            "evidence": "results/oracle_ub_rustmap_bzip2.json: C round-trips and is sanitizer/opt-level/char-signedness clean on these inputs"}
            r["acceptance_status"] = "accepted-rerun"
            # arXiv: the acceptance basis is the uncapped re-run (README 4.1.1-4.1.3 as written, no 60 s cap)
            unc = opt_load("results/arxiv_rustmap_bzip2_check_uncapped.json")
            if unc:
                us = unc["summary"]
                check("RustMap bzip2: uncapped re-run of its own check passes every one of its own files",
                      us["pass"] == us["files"] == len(unc["rows"]) and all(x.get("passes_rustmap_check") for x in unc["rows"]))
                if us["pass"] != us["files"]:
                    r["acceptance_status"] = "rejected"
                slow = max(us["wall_s"].items(), key=lambda kv: kv[1])
                r["acceptance_basis"] = "re-run: no shipped verdict (the diff files of README 4.1.3 are not shipped); our uncapped re-run of RustMap's own check on its own five inputs (results/arxiv_rustmap_bzip2_check_uncapped.json)"
                r["acceptance_evidence"] = [f"results/arxiv_rustmap_bzip2_check_uncapped.json: RustMap's own check (README 4.1.1-4.1.3 as written, no time cap) passes {us['pass']} of its {us['files']} own files; the slowest, {slow[0]}, compresses in {slow[1]:.0f} s" + (f" (the authors' {t5000})" if t5000 else ""),
                                            f"Earlier capped re-run (results/heldout_rustmap.json, 60 s per file): passes {len(passed)} of {len(sysin)} ({', '.join(passed)}); {', '.join(notfin) or 'none'} did not finish in 60 s",
                                            "Shipped compress_output_bz2_files/rec00001random_*_chars.txt equal the inputs (the check's intermediate output, not a verdict)."]
            else:
                r["acceptance_basis"] = "re-run: no shipped verdict (the diff files of README 4.1.3 are not shipped); our re-run of RustMap's own check on its own five inputs"
                r["acceptance_evidence"] = [f"results/heldout_rustmap.json: RustMap's own check passes {len(passed)} of {len(sysin)} of its own files ({', '.join(passed)}); {', '.join(notfin) or 'none'} did not finish in 60 s" + (f" (the authors' {t5000} for that file)" if t5000 else ""),
                                            "Shipped compress_output_bz2_files/rec00001random_*_chars.txt equal the inputs (the check's intermediate output, not a verdict)."]
            if r["defect_id"].endswith("BZ-4"):
                r["_found_own"] = sum(1 for x in sysin if x.get("rust_bz2_leading_zero_bytes") == 5000) > 0
            else:
                r["_found_own"] = False
            r["_heldout"] = "outside"
        else:
            task = r["defect_id"].split("rosetta-")[1][:-2]
            v = next((x for x in (opt_load("results/oracle_ub_rustmap_rosetta.json") or {"verdicts": []})["verdicts"] if x["task"] == task), None)
            ok = v is not None and clean_generic(v, ("san_clang", "san_gcc", "o1", "o2", "uchar", "schar")) and v.get("ref_repeat_identical", True)
            r["ub_rule"] = {"status": "clean" if ok else "pending" if v is None else "excluded_original_c_ub", "clean_inputs": 1 if ok else 0, "of": 1, "evidence": "results/oracle_ub_rustmap_rosetta.json"}
            r["exposure_count"] = 1 if ok else (0 if v is not None else 1)
            r["acceptance_basis"] = "record: shipped raw diff (diff_rosetta_result.log)" if "iii_no_record" not in " ".join(r["acceptance_evidence"]) else "re-run: no Rust output on record; our re-run of the documented whole-log diff"
            r["_found_own"] = True
            r["_heldout"] = "inside"
            r["rosetta_label_v1_v2"] = ROSETTA_V2.get(task, ("wrong_output_S1" if r["severity"] == "S1" else "rust_panic", "wrong_output" if r["severity"] == "S1" else "wrong_output (panic)", "unchanged under the rules"))
    # new Rosetta rows (relabelled different_task -> wrong_output)
    samples = {v["task"]: v for v in load("results/rustmap_rosetta.json")["divergence_samples"]}
    for task, nd in ROSETTA_NEW_DEFECTS.items():
        verify_loc(nd["rust"])
        verify_loc(nd["c"])
        sm = samples.get(task, {})
        v = next((x for x in (opt_load("results/oracle_ub_rustmap_rosetta.json") or {"verdicts": []})["verdicts"] if x["task"] == task), None)
        rows.append(row("RustMap", f"Rosetta {task} (program)", f"RUSTMAP-rosetta-{task}-1", nd["mechanism"], {"rust": nd["rust"], "c": nd["c"]}, "S1",
                        example_input="(no input: the program prints its own fixed cases)", c_output=sm.get("c", "")[:600], rust_output=sm.get("rust", "")[:600],
                        exposure_count=1, exposure_breakdown={"runs": 1, "source": "results/rustmap_rosetta.json"},
                        acceptance_status="rejected", acceptance_evidence=["Our re-run of RustMap's documented whole-log diff (results/rustmap_rosetta.json) differs on this program; RustMap reports no verdict from the diff."],
                        acceptance_basis="re-run (raw diff shipped, never interpreted)", rerun_disagrees=None, record_conflict=None,
                        check_exercises_defect={"status": "exercised-failed", "evidence": "The program's only run is the one the diff compares."},
                        ub_rule={"status": "clean" if v and clean_generic(v) else "pending", "clean_inputs": None, "of": 1,
                                 "evidence": "not in results/oracle_ub_rustmap_rosetta.json (relabelled in v2); needs the same C check"},
                        rosetta_label_v1_v2=ROSETTA_V2[task], _found_own=True, _heldout="inside"))
    meta["RustMap"]["rosetta_triage_v2"] = {"rules": ROSETTA_RULES, "changes": {t: {"v1": a, "v2": b, "why": w} for t, (a, b, w) in ROSETTA_V2.items() if a.split("_S1")[0] != b},
                                           "reviewed_unchanged": {t: {"v1": a, "v2": b, "why": w} for t, (a, b, w) in ROSETTA_V2.items() if a.split("_S1")[0] == b},
                                           "cosmetic_16_verified_by_normaliser": True}
    # ---------------- VERT
    rec_a, rec_s, rec_w = opt_load(VERT_ARRAY_UB), opt_load(VERT_SCALAR_UB), opt_load(VERT_SCALAR_WIDE_UB)
    rec = rec_a or rec_s
    recmap, recsrc = {}, {}
    for path, rr_, harness in ((VERT_ARRAY_UB, rec_a, "arrays"), (VERT_SCALAR_UB, rec_s, "scalar"), (VERT_SCALAR_WIDE_UB, rec_w, "scalar")):
        for x in ((rr_ or {}).get("rows") or (rr_ or {}).get("functions") or (rr_ or {}).get("verdicts") or []):
            f = x.get("function") or x.get("func")
            if f and x.get("harness", harness) == harness:
                recmap[f], recsrc[f] = x, path
    scalar_fams = {x["func"] for x in CURATED_VERT if x["family"] == "scalar"}
    _vb = {v["func"] for v in vert_scalar_all()[1]}
    _miss = sorted(f for f in scalar_fams & _vb if recsrc.get(f) not in (VERT_SCALAR_UB, VERT_SCALAR_WIDE_UB))
    check(f"VERT: every scalar wrong answer has a grid-v2 full-UB-rule row ({VERT_SCALAR_UB}, {VERT_SCALAR_WIDE_UB})", not _miss, str(_miss))
    ho = {x["defect"]: x for x in (opt_load("results/heldout_overlap.json") or {}).get("counted_defects", []) if x["system"] == "VERT"}
    # scalar rows: held-out measured on grid v2 (systems/vert/heldout_grid2.py)
    ho_s = {x["defect"]: x for x in (opt_load(VERT_SCALAR_HELDOUT) or {}).get("counted_defects", []) if x["system"] == "VERT"}
    ho_s_src = {f_: VERT_SCALAR_HELDOUT for f_ in ho_s}
    for x in (opt_load(VERT_SCALAR_WIDE_HELDOUT) or {}).get("counted_defects", []):
        if x["system"] == "VERT":
            check(f"VERT held-out: {x['defect']} measured once", x["defect"] not in ho_s)
            ho_s[x["defect"]], ho_s_src[x["defect"]] = x, VERT_SCALAR_WIDE_HELDOUT
    for f_ in scalar_fams:
        if f_ in ho_s:
            ho[f_] = ho_s[f_]
        else:
            ho.pop(f_, None)
    for r in [r for r in rows if r["system"] == "VERT"]:
        f = r["translation"].split()[0]
        r["rerun_disagrees"] = None
        r["record_conflict"] = None
        r["acceptance_basis"] = "record: artifacts/vert/c_results.csv"
        x = recmap.get(f)
        if x is not None and r["severity"] == "S1":
            # the recheck covers value (S1) divergences; prefer the stricter count where clang's -fwrapv output also agrees with gcc's
            cl = next((x[k] for k in ("clean_divergences_clang_agrees", "clean_divergences", "clean_divergent_inputs", "clean_value_inputs", "clean_inputs")
                       if k in x and isinstance(x[k], int)), None)
            of = x.get("value_divergences", x.get("rechecked"))
            r["ub_rule"] = {"status": "clean" if cl else "excluded_original_c_ub" if cl == 0 else "pending",
                            "clean_inputs": cl, "of": of,
                            "evidence": f"{recsrc[f]}: {cl} of {of} value divergences clean (gcc -O0 ASan+UBSan, clang-14 -O0/-O1 ASan+UBSan+float-cast-overflow, clang MSan, gcc -O0 == -O1, clang agrees with gcc; char-signedness verdict changes: none)"
                                        + (f"; float-cast UB inputs {x.get('float_cast_ub_inputs')}" if x.get("float_cast_ub_inputs") else "")
                                        + (f"; smallest clean {x.get('smallest_clean_clang_agrees', {}).get('in')}" if cl else "")}
            if cl is not None:
                bo = r["exposure_breakdown"].get("by_outcome", {})
                r["exposure_breakdown"]["by_outcome_before_ub_recheck"] = dict(bo)
                r["exposure_breakdown"]["by_outcome"] = {"S1": cl}
                if bo.get("crash"):
                    r["exposure_breakdown"]["crash_inputs_not_ub_rechecked"] = bo["crash"]
                r["exposure_before_ub_filter"] = r["exposure_count"]
                r["exposure_count"] = cl
            check(f"VERT {f}: recheck value_divergences cover the ledger's S1 exposure", (x.get("value_divergences") or 0) >= (r["exposure_breakdown"].get("by_outcome_before_ub_recheck", {}).get("S1") or 0))
        elif x is not None or rec is not None:
            r["ub_rule"] = {"status": "pending", "clean_inputs": None,
                            "evidence": ("results/vert_wrong_answer_ub_recheck.json covers value divergences only; this crash/hang mechanism's inputs are not rechecked" if x is not None
                                         else "function not in results/vert_wrong_answer_ub_recheck.json (it covers value divergences only)")}
        else:
            r["ub_rule"] = {"status": "pending", "clean_inputs": None,
                            "evidence": "pending clang recheck: results/vert_wrong_answer_ub_recheck.json not present at build time; current filter is gcc ASan/UBSan + -O0/-O1 (does not flag float-to-int overflow)"
                                        + ("; triage: 73 of 74 diverging inputs are C double->int overflow, only n=18 clean" if f == "NUMBER_TRIANGLES_N_MOVES_1" else "")}
        h = ho.get(f)
        if h:
            vis = h.get("visible_on_system_inputs") or {}
            r["_found_own"] = any(v is True for v in vis.values()) if isinstance(vis, dict) else bool(vis)
            r["_heldout"] = "outside" if h.get("all_diverging_inputs_outside") is True else "inside" if h.get("all_diverging_inputs_outside") is False else "not-measurable"
            r["own_inputs_note"] = f"visible on VERT's recorded inputs: {vis}; diverging inputs inside VERT's input set: {h.get('diverging_inputs_in_system_set')}"
        else:
            r["_found_own"], r["_heldout"] = False, "not-measurable"
        # preconditions stated in the benchmark name
        if "SORTED" in f:
            cs = r["exposure_breakdown"].get("source", "")
            smp = [s for s in (load(VERT_ARRAYS)["rust_crash_bugs"] if r["severity"] == "crash" else []) if s["func"] == f]
            sorted_ex = [s2["in"] for s in smp for s2 in s.get("crash_samples", []) if s2["in"][0] <= s2["in"][1]]
            r["precondition"] = {"status": "satisfied" if sorted_ex else "precondition_only",
                                 "evidence": f"name states a sorted array; exposing samples with a sorted 2-element array: {sorted_ex} ({VERT_ARRAYS} crash_samples; only samples are recorded, so the exposure count is an upper bound)"}
        elif "NATURAL_NUMBER" in f and r["severity"] == "hang":
            r["precondition"] = {"status": "precondition_only", "evidence": f"name states a natural number; the only hang input is x=0 ({VERT_SCALAR})"}
        elif f == "FIND_SMALLEST_VALUE_REPRESENTED_SUM_SUBSET_GIVEN_ARRAY":
            r["precondition"] = {"status": "none_stated",
                                 "evidence": "The algorithm assumes a sorted array, but neither the name nor the C file states it: the file's only comments are the license header (lines 1-6), and it defines an unused sort() helper (line 19) like every TransCoder file. All 3 diverging inputs are unsorted, so it stays counted under the rule; flag for the paper."}
    # ---------------- defaults, found_on/heldout, count status
    for r in rows:
        r.setdefault("precondition", {"status": "none_stated", "evidence": "no precondition stated in the name, comments or documentation that the exposing inputs violate (checked: VERT names and C comments, FLOURINE signatures, C2SaferRust usage, RustAssure options, Syzygy's ZopfliDeflate doc comment, RustMap fixed runs)"})
        if r["system"] == "C2SaferRust":
            ownmap = [o for o in meta["C2SaferRust"]["own_input_mismatches"] if o["mapped_to"] == r["defect_id"] or (r["defect_id"] == "C2SR-tail-3" and o["mapped_to"] == "C2SR-tail-2")]
            # one input set: the check's inputs are the acceptance set (the .sh tests that pass on the
            # starting translation; translate.py select_tests :231-248, called :523, run on every rewrite :712)
            acc_t = {t for t, v in ((hc or {}).get("per_test") or {}).items() if v.get("in_acceptance_set")}
            chk = [o for o in ownmap if o["test"] in acc_t]
            unrun = [o for o in ownmap if o["test"] not in acc_t]
            r["own_input_mismatches"] = [f"{o['test']} {' '.join(o['argv'])}" for o in chk] or None
            r["shipped_test_mismatches_not_run_by_check"] = [f"{o['test']} {' '.join(o['argv'])}" for o in unrun] or None
            on_ours = (r.get("exposure_before_ub_filter") or 0) > 0
            r["found_on"] = "both" if on_ours and chk else "own_inputs" if chk else "our_inputs"
            r["heldout"] = "outside" if on_ours or unrun else "inside"
        else:
            own_ = r.pop("_found_own", False)
            r["found_on"] = "both" if own_ else "our_inputs"
            r["heldout"] = r.pop("_heldout", "not-measurable")
        if r["system"] == "RustMap" and "rosetta" in r["defect_id"]:
            r["found_on"] = "both"
        ub = r.get("ub_rule", {}).get("status", "pending")
        if r["severity"] == "hang":
            cs_ = "hang-not-counted"
        elif r["precondition"]["status"] == "precondition_only":
            cs_ = "precondition_only"
        elif ub == "excluded_original_c_ub":
            cs_ = "excluded_original_c_ub"
        elif ub == "pending":
            cs_ = "counted-ub-pending"
        else:
            cs_ = "counted"
        r["count_status"] = cs_
    # ---------------- results/oracle_ub_pending.json: UB verdicts for defects without an oracle_ub file
    pend = opt_load("results/oracle_ub_pending.json") or {}
    meta["ub_pending_file"] = {"present": bool(pend), "defects": sorted((pend.get("defects") or {}).keys()),
                               "provenance": pend.get("provenance")}
    overrides = dict(pend.get("defects") or {})
    vcr = opt_load("results/vert_crash_ub_recheck.json") or {}
    for x in vcr.get("functions", []):
        if x.get("defect_id") and x.get("status") == "checked":
            fb = x.get("failing_by_check", {})
            overrides[x["defect_id"]] = {"ub_rule": {
                "status": "clean" if x["passing_full_rule"] else "excluded_original_c_ub",
                "clean_inputs": x["passing_full_rule"], "of": x["crash_exposing_inputs"],
                "evidence": f"results/vert_crash_ub_recheck.json: {x['passing_full_rule']} of {x['crash_exposing_inputs']} crash-exposing inputs pass the full rule (gcc ASan+UBSan, clang full sanitizers -O0/-O2, MSan, -O0/-O1/-O2 agreement, char signedness); failing by check {fb}; smallest passing {x.get('smallest_passing', {}).get('in')}"},
                "per_input": None}
    meta["ub_pending_file"]["vert_crash_recheck"] = {"present": bool(vcr), "defects": sorted(x.get("defect_id") for x in vcr.get("functions", []) if x.get("defect_id"))}
    for r in rows:
        e = overrides.get(r["defect_id"])
        if not e:
            continue
        u = dict(e["ub_rule"])
        pi = e.get("per_input") or []
        if pi and u.get("clean_inputs") is not None and "san_output_same_as_ref_when_clean" in pi[0]:
            # audit 8 uniform rule on the stored per-input records: a sanitizer run that printed
            # another output (or timed out), or a C output that changes with char signedness, is not clean
            keep = [p for p in pi if p.get("exposing_in_ledger", True)]
            cl8 = sum(1 for p in keep if p.get("clean") and all(p.get("san_output_same_as_ref_when_clean", {}).get(s, True)
                                                                  for s in ("san_clang", "san_gcc"))
                      and not p.get("char_signedness_dependent_C") and not p.get("ub_rule_not_clean"))
            if cl8 != u["clean_inputs"] and len(keep) == (u.get("of") or len(keep)):
                u["clean_inputs_before_audit8_rule"] = u["clean_inputs"]
                u["clean_inputs"] = cl8
                u["status"] = "clean" if cl8 else "excluded_original_c_ub"
                u["evidence"] += (f"; audit 8 uniform rule (re-applied to the stored per_input records): {cl8} of {len(keep)} "
                                  "exposing inputs clean once a C output that changes with -funsigned-char/-fsigned-char, or a "
                                  "sanitizer run that disagrees with the reference, counts as not UB-clean")
        check(f"{r['defect_id']}: oracle_ub_pending ub_rule has status/clean_inputs/of/evidence",
              {"status", "clean_inputs", "of", "evidence"} <= set(u) and u["status"] in ("clean", "excluded_original_c_ub", "pending"))
        if e.get("per_input") is not None:
            check(f"{r['defect_id']}: per-input verdicts listed for every checked input", len(e["per_input"]) >= (u.get("of") or 0))
        r["ub_rule"] = u
        if u.get("clean_inputs") is not None:
            r["exposure_before_ub_filter"] = r.get("exposure_before_ub_filter", r["exposure_count"])
            r["exposure_count"] = u["clean_inputs"]
            if r["defect_id"] == "C2SR-head-5":
                r["exposure_breakdown"]["note"] = "exposure counts the system's own shell-test invocations (found_on = own_inputs)"
        st = u["status"]
        if r["count_status"] in ("counted", "counted-ub-pending", "excluded_original_c_ub"):
            r["count_status"] = "counted" if st == "clean" else "excluded_original_c_ub" if st == "excluded_original_c_ub" else "counted-ub-pending"
    # re-derive count_status for VERT after the recheck mapping
    for r in rows:
        if r["system"] == "VERT" and r["count_status"] in ("counted", "counted-ub-pending", "excluded_original_c_ub"):
            st = r["ub_rule"]["status"]
            r["count_status"] = "counted" if st == "clean" else "excluded_original_c_ub" if st == "excluded_original_c_ub" else "counted-ub-pending"
    # ---------------- exposed_outside and translation_units
    for r in rows:
        sysn, did = r["system"], r["defect_id"]
        clean = r.get("ub_rule", {}).get("clean_inputs")
        if sysn == "VERT":
            f = r["translation"].split()[0]
            h = ho.get(f) or {}
            n_out = h.get("diverging_inputs_outside_system_set")
            r["exposed_outside"] = (n_out > 0) if isinstance(n_out, int) else (True if h.get("all_diverging_inputs_outside") is True else None)
            r["exposed_outside_evidence"] = f"{ho_s_src[f] if f in ho_s else 'results/heldout_overlap.json'}: {n_out if n_out is not None else 'all' if h.get('all_diverging_inputs_outside') is True else '?'} diverging inputs outside VERT's recorded inputs (Kani's oracle-side n in {{0,1}} and its Rust point; the TransCoder tuples, which evaluation.py never runs, are not in this set)"
            r["translation_units"] = [f"VERT:{f}"]
        elif sysn == "Flourine":
            files = list(r["exposure_breakdown"]["diverging_inputs_per_file"])
            ubc = r["exposure_breakdown"].get("ub_clean_diverging_inputs_per_file") or {}
            outs = [(hf.get(t.rsplit('/', 1)[0]) or {}).get("counted_divergences_outside_logged_inputs") for t in files]
            # struct accessors: no our-grid replay on logged inputs, but the overlap with them is computed (0 => all outside)
            outs = [o if o is not None else (ubc.get(t) if (hf.get(t.rsplit('/', 1)[0]) or {}).get("overlap_ours_logged") == 0 else None)
                    for o, t in zip(outs, files)]
            r["exposed_outside"] = True if any(isinstance(x, int) and x > 0 for x in outs) else (None if all(x is None for x in outs) else False)
            r["exposed_outside_evidence"] = f"{', '.join(sorted({hf_src.get(t.rsplit('/', 1)[0], 'results/heldout_flourine.json') for t in files}))} counted_divergences_outside_logged_inputs per file (struct accessors: UB-clean exposing inputs when the overlap with the logged inputs is 0): {outs}"
            r["translation_units"] = [f"Flourine:{t}" for t in files]
        elif sysn == "RustAssure":
            r["exposed_outside"] = True if "libcsv" in did and (r["exposure_count"] or 0) > 0 and r["heldout"] == "outside" else None
            r["exposed_outside_evidence"] = "results/heldout_rustassure.json: 0 of the 12 counted cases are in RustAssure's input set" if "libcsv" in did else "RustAssure's check is symbolic and records no concrete inputs: not measurable"
            r["translation_units"] = ["RustAssure:libcsv/csv_parse(GPT-4o, 2025-07-02)" if "libcsv" in did else "RustAssure:u8c/u8next_(GPT-4o)"]
        elif sysn == "C2SaferRust":
            util = r["translation"].split()[1]
            _si = (hc or {}).get("system_inputs", {})
            _acc, _all = _si.get("acceptance_set_tests", {}), _si.get("all_sh_tests", {})
            _ov = f"results/heldout_c2saferrust.json: {(hc or {}).get('overlap', {}).get('distinct_inputs_in_both')} of our {(hc or {}).get('our_inputs', {}).get('cases')} cases equal any of the {_acc.get('invocations')} invocations of the {_acc.get('tests')} tests the check runs (nor any of all {_all.get('invocations')} traced invocations of the {_all.get('tests')} shipped tests)"
            if did == "C2SR-head-5":
                unrun = r.get("shipped_test_mismatches_not_run_by_check") or []
                r["exposed_outside"] = bool(unrun)
                r["exposed_outside_evidence"] = (f"not on our grid; diverges (UB-clean, results/oracle_ub_pending.json) on shipped shell-test invocations the check never runs on a rewrite, because their test fails on the starting translation and select_tests drops it: {unrun}"
                                                 if unrun else "exposed only by the check's own shell-test invocations (results/heldout_c2saferrust.json)")
            elif clean:
                r["exposed_outside"] = True
                r["exposed_outside_evidence"] = f"{clean} UB-clean grid cases; " + _ov
            else:
                r["exposed_outside"] = None
                r["exposed_outside_evidence"] = "no UB-clean exposing input (excluded_original_c_ub)"
            r["translation_units"] = [f"C2SaferRust:{util}"]
        elif sysn == "Syzygy":
            r["exposed_outside"] = None
            r["exposed_outside_evidence"] = "Syzygy's check inputs are not shipped: not measurable"
            r["translation_units"] = ["Syzygy:zopfli"]
        elif sysn == "RustMap":
            if "bzip2" in did:
                r["exposed_outside"] = True
                r["exposed_outside_evidence"] = "results/heldout_rustmap.json: 0 of our 20 bzip2 inputs equal one of RustMap's 5 test files"
                r["translation_units"] = ["RustMap:bzip2"]
            else:
                r["exposed_outside"] = False
                r["exposed_outside_evidence"] = "the program's only run (no arguments, no stdin) is the run RustMap's documented diff performs"
                r["translation_units"] = [f"RustMap:rosetta/{r['translation'].split()[1]}"]
    # ---------------- on_check_inputs / on_shipped_unrun_inputs (held out = the check did not run that exact input)
    hv = {f["function"]: f for f in (opt_load("results/heldout_vert.json") or {}).get("functions", [])}
    hv_src = {f_: "results/heldout_vert.json" for f_ in hv}
    _scalar_cur = {x["func"] for x in CURATED_VERT if x["family"] == "scalar"}
    for f_ in _scalar_cur:      # scalar rows: grid-v2 measurement only (VERT's own inputs do not depend on our grid)
        hv.pop(f_, None); hv_src.pop(f_, None)
    for _hp in (VERT_SCALAR_HELDOUT, VERT_SCALAR_WIDE_HELDOUT):
        for f_ in (opt_load(_hp) or {}).get("functions", []):
            hv[f_["function"]], hv_src[f_["function"]] = f_, _hp
    kani = {x["function"]: x for x in (opt_load("results/vert_kani_vacuity.json") or {}).get("rows", [])}
    # Kani re-runs from systems/vert/kani_verify.py (VERT's command and VERT's 180 s budget) for functions
    # vert_kani_vacuity.json does not cover; mapped to the same keys.
    for _kv in ("results/vert_kani_verify_8.json", "results/vert_kani_verify_grid2.json",   # grid2: the 3 defects grid v2 adds
                "results/arxiv_vert_wide_kani_verify.json"):                                # arXiv: the long-return functions (issues.md 47)
        for x in (opt_load(_kv) or {}).get("rows", []):
            if x.get("mode") == "vert" and x["function"] not in kani:
                kani[x["function"]] = {"function": x["function"], "verdict": x["verdict"], "source": _kv,
                                       "equivalence_assertion": x.get("equivalence") or ("NO_VERDICT_WITHIN_VERT_BUDGET" if x["verdict"] == "TIMEOUT" else None)}
    nsev = collections.Counter(r["translation"] for r in rows if r["system"] == "VERT")
    hc_tests = (hc or {}).get("per_test", {})
    for r in rows:
        sysn, did = r["system"], r["defect_id"]
        if sysn == "VERT":
            f = r["translation"].split()[0]
            cmpv = (hv.get(f) or {}).get("our_comparison_on_vert_inputs") or {}
            if nsev[r["translation"]] > 1:
                want = {"value"} if r["severity"] == "S1" else {"rust_panic", "crash"}
            else:
                want = {"value", "rust_panic", "crash", "rust_hang", "timeout", "hang"}
            ran = [x for k in ("kani_oracle_side", "kani_rust_side") for x in (cmpv.get(k) or []) if x.get("v") in want]
            # transcoder_tests is None where the C's TransCoder test array is not `int param0[]` (not parsed: arXiv wide functions)
            tc_known = cmpv.get("transcoder_tests") is not None
            unrun = [x for x in (cmpv.get("transcoder_tests") or []) if x.get("v") in want]
            corpus = ((hv.get(f) or {}).get("overlap") or {}).get("ours_vs_bolero_corpus")
            listed = (f"{hv_src.get(f, 'results/heldout_vert.json')}: diverges ({'/'.join(sorted(want))}) on {len(ran)} input(s) Kani's harness lists "
                      f"{[x['in'] for x in ran][:4]} (oracle-side tuples and the Rust-side static point; UB-flagged inputs excluded)")
            kv0 = kani.get(f)
            bol = "bolero: the documented step (cargo bolero reduce) replays the shipped corpus; " + (f"{corpus}, so it executes no input" if corpus == "no corpus shipped" else f"corpus: {corpus}")
            if kv0 is not None and kv0.get("equivalence_assertion") != "REACHABLE" and kv0.get("verdict") in ("SUCCESSFUL", "COMPILE_ERROR", "TIMEOUT", "FAILED"):
                # Kani executed no comparison: the equivalence assertion is unreachable (or the harness did not compile / timed out)
                r["on_check_inputs"] = False
                r["on_check_inputs_evidence"] = (f"Kani re-run with VERT's command ({kv0.get('source', 'results/vert_kani_vacuity.json')}): verdict {kv0.get('verdict')}, equivalence assertion {kv0.get('equivalence_assertion')}; "
                                                 + ("the harness never reaches the comparison (unwinding cut at 10 with the Rust evaluated at 12), so no input was run through it. " if kv0.get("verdict") == "SUCCESSFUL" else "the equivalence assertion is never evaluated (no verdict within VERT's 180 s budget, or a failed bounds check before it), so no input was run through the comparison. ") + bol + ". " + listed)
                if ran:
                    r["on_shipped_unrun_inputs"] = True
            else:
                vr = vert_records().get(f, (None, {}))[1]
                r["on_check_inputs"] = None if ran else False
                r["on_check_inputs_evidence"] = (f"VERT records bounded_kani={vr.get('bounded_kani')} (evaluation.py:77-92 writes 'Kani failed' on a FAILED verdict or 'Kani timeout'); whether its harness reached the equivalence assertion is not recorded and Kani was not re-run on this function, so whether the comparison executed is unknown" + ("" if ran else " (moot: the defect diverges on none of the listed inputs)") + ". "
                                                 + bol + ". " + listed)
            if r.get("on_shipped_unrun_inputs") is not True:
                r["on_shipped_unrun_inputs"] = (len(unrun) > 0) if tc_known else None
            r["on_shipped_unrun_inputs_evidence"] = ((f"TransCoder test tuples shipped in the C file but never run by evaluation.py: diverges on {len(unrun)}" if tc_known
                                                      else "no TransCoder tuples recorded for this harness (the C's test array is not an int array)" if "transcoder_tests" in cmpv
                                                      else "no TransCoder tuples recorded for this harness")
                                                     + (f"; Kani-listed inputs never executed through the comparison: diverges on {len(ran)}" if r["on_check_inputs"] is False and ran else ""))
        elif sysn == "C2SaferRust":
            own = (r.get("own_input_mismatches") or []) + (r.get("shipped_test_mismatches_not_run_by_check") or [])
            tests = sorted({o.split()[0] for o in own})
            acc_tests = [t for t in tests if hc_tests.get(t, {}).get("in_acceptance_set")]
            unrun_tests = [t for t in tests if not hc_tests.get(t, {}).get("in_acceptance_set")]
            # strict: the loop runs only its acceptance set (the .sh tests that pass on the starting translation)
            r["on_check_inputs"] = bool(acc_tests)
            r["on_check_inputs_evidence"] = ("diverges on traced shell-test invocations: " + "; ".join(own[:4]) + (f" (+{len(own) - 4} more)" if len(own) > 4 else "")
                                            + f"; in the acceptance set (run on the rewritten tree): {[t for t in tests if hc_tests.get(t, {}).get('in_acceptance_set')]}, run only by select_tests on the starting tree: {[t for t in tests if not hc_tests.get(t, {}).get('in_acceptance_set')]}"
                                            if own else "no traced shell-test invocation (results/heldout_c2saferrust.json, 96 invocations of 42 .sh tests) diverges through this defect")
            r["on_check_inputs_evidence"] = ("diverges on acceptance-set invocations (tests the loop runs on every rewrite): "
                                            + ", ".join(acc_tests) if acc_tests else
                                            "no divergence on the acceptance set (the 11 .sh tests, 47 invocations, that pass on the starting translation; results/heldout_c2saferrust.json)")
            r["on_shipped_unrun_inputs"] = bool(unrun_tests) if own else None
            r["on_shipped_unrun_inputs_evidence"] = (f"diverges on shipped .sh tests outside the acceptance set, which the loop never runs: {unrun_tests} ("
                                                     + "; ".join(o for o in own if o.split()[0] in unrun_tests)[:300] + ")" if unrun_tests else
                                                     "none (the Perl tests are shipped but never run; not replayed)")
        elif sysn == "Flourine":
            files = list(r["exposure_breakdown"]["diverging_inputs_per_file"])
            n = [(hf.get(t.rsplit('/', 1)[0]) or {}).get("shipped_diverges_on_logged_inputs_iso_c_clean") for t in files]
            r["on_check_inputs"] = True if any(isinstance(x, int) and x > 0 for x in n) else (None if all(x is None for x in n) else False)
            why = [(hf.get(t.rsplit('/', 1)[0]) or {}).get("shipped_diverges_on_logged_inputs_reason") for t in files]
            r["on_check_inputs_evidence"] = (f"{', '.join(sorted({hf_src.get(t.rsplit('/', 1)[0], 'results/heldout_flourine.json') for t in files}))}: UB-clean divergences on Flourine's logged prompt examples per file: {n}"
                                             + "".join(f"; unknown: {w}" for w in why if w))
            r["on_shipped_unrun_inputs"], r["on_shipped_unrun_inputs_evidence"] = None, "none identified"
        elif sysn == "RustAssure":
            if "libcsv" in did:
                oc = hr.get("our_comparison_on_rustassure_inputs", {})
                ocr = hr.get("own_check_run", {})
                reached = set()
                for mode in ("debug", "release"):
                    reached |= set((ocr.get(mode) or {}).get("per_test_header", {}))
                div = [t for t in oc.get("diverging_tests", [])]
                div_reached = [t for t in div if t.replace("test_", "") in reached]
                div_unreached = [t for t in div if t not in div_reached]
                r["on_check_inputs"] = bool(div_reached)
                r["on_check_inputs_evidence"] = (f"csv_link test_all_parse executes tests {sorted(reached)} in our debug and release runs, then aborts ('malloc(): corrupted top size', exit -6) "
                                                 f"after test {(ocr.get('debug') or {}).get('last_header')}; none of the executed tests diverges under our comparison (results/heldout_rustassure.json)")
                r["on_shipped_unrun_inputs"] = bool(div_unreached)
                r["on_shipped_unrun_inputs_evidence"] = (f"{', '.join(div_unreached)}: listed in test_all_parse but never reached: the test aborts at test_04a; it diverges under our comparison (NULL vs empty field)" if div_unreached else "none")
            else:
                r["on_check_inputs"], r["on_check_inputs_evidence"] = None, "per-function KLEE check: symbolic, no concrete inputs recorded"
                r["on_shipped_unrun_inputs"], r["on_shipped_unrun_inputs_evidence"] = None, "none identified"
        elif sysn == "Syzygy":
            r["on_check_inputs"], r["on_check_inputs_evidence"] = None, "the random strings Syzygy's check ran are not shipped"
            r["on_shipped_unrun_inputs"], r["on_shipped_unrun_inputs_evidence"] = None, "none identified"
        elif sysn == "RustMap":
            if "bzip2" in did:
                if did.endswith("BZ-4"):
                    n = sum(1 for x in sysin if x.get("rust_bz2_leading_zero_bytes") == 5000 and x.get("plain_bzip2_d_accepts_rust_output", {}).get("rc") not in (0, None))
                    r["on_check_inputs"] = n > 0
                    r["on_check_inputs_evidence"] = f"results/heldout_rustmap.json: on {n} of RustMap's {len(sysin)} bzip2 test files the Rust output starts with 5000 zero bytes and plain bzip2 -d rejects it (RustMap's check passes them via bzip2recover)"
                else:
                    r["on_check_inputs"] = False
                    r["on_check_inputs_evidence"] = f"results/heldout_rustmap.json: RustMap's own check passes {len(passed)} of its {len(sysin)} test files; {', '.join(notfin) or 'none'} did not finish (no divergence observed); the files have no byte >= 0x80, no run >= 5 and no 1-byte file"
            else:
                task = r["translation"].split()[1]
                rlog = src_lines("artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/rustmap_rosetta_result.log")
                ran_tasks = {m.group(1) for l in rlog for m in [re.match(r"Compiling and running (\S+)-c\.rs", l)] if m}
                last = next((l.strip() for l in reversed(rlog) if l.startswith("Compiling and running")), None)
                # the last program started is listed as run only if output follows; the shipped log ends with its header
                finished = ran_tasks - ({last.split()[-1][:-5]} if last and rlog and rlog[-1].strip() == last else set())
                if task in finished:
                    r["on_check_inputs"] = True
                    r["on_check_inputs_evidence"] = "the program's run is in RustMap's shipped Rust run log (rustmap_rosetta_result.log), i.e. the run its documented whole-log diff compares"
                    r["on_shipped_unrun_inputs"], r["on_shipped_unrun_inputs_evidence"] = None, "none identified"
                else:
                    r["on_check_inputs"] = False
                    r["on_check_inputs_evidence"] = f"RustMap's shipped Rust run log never reaches this program: it ends at '{last}' (rustmap_rosetta_result.log:{len(rlog)})"
                    r["on_shipped_unrun_inputs"] = True
                    r["on_shipped_unrun_inputs_evidence"] = "listed in RustMap's runner (every program is run by the documented script) but never reached: the shipped run stops at rational-c.rs; diverges under our run"
                    r["exposed_outside"] = True
                    r["exposed_outside_evidence"] = "held out under the literal definition: RustMap's check never ran this program (its shipped Rust run log stops at rational-c.rs)"
                continue
            r["on_shipped_unrun_inputs"], r["on_shipped_unrun_inputs_evidence"] = None, "none identified"
    rows += ingest_new_systems(meta)
    # one held-out rule for every counted row, the one Fig. 2(b) draws: outside iff an exposing input the
    # check did not run (exposed_outside); inside iff exposed only on the check's inputs; else not measurable
    for r in rows:
        if r.get("count_status") not in ("counted", "counted-ub-pending"):
            continue
        eo, oc = r.get("exposed_outside"), r.get("on_check_inputs")
        want = "outside" if eo is True else "inside" if eo is False and oc is True else "not-measurable"
        if r.get("heldout") != want:
            r["heldout_before_rule"] = r.get("heldout")
            r["heldout"] = want
        check(f"{r['defect_id']}: heldout agrees with exposed_outside/on_check_inputs (Fig. 2b)",
              r["heldout"] == want and not (eo is False and oc is not True))
    meta["v2"] = V2
    return rows


# ---- (5) new re-graded systems: results/<system>_summary.json
SYN = {"defect_id": ("defect_id", "id", "defect"), "translation": ("translation", "unit", "function", "program"),
       "mechanism": ("mechanism", "cause", "description"), "source_location": ("source_location", "location", "rust_location"),
       "severity": ("severity", "kind", "class"), "example_input": ("example_input", "input"), "c_output": ("c_output", "c", "oracle"),
       "rust_output": ("rust_output", "rust", "sut"), "exposure_count": ("exposure_count", "exposure", "diverging_inputs", "count"),
       "acceptance_status": ("acceptance_status", "acceptance"), "acceptance_evidence": ("acceptance_evidence", "acceptance_evidence_list")}
SEV = {"s1": "S1", "wrong": "S1", "wrong_answer": "S1", "value": "S1", "crash": "crash", "panic": "crash", "segv": "crash", "rust_panic": "crash", "hang": "hang"}


def adapt_ptrtrans(d):
    """results/ptrtrans_summary.json -> normalised defect items. Policy: the paper compares a
    library's results AND callbacks (RustAssure's csv_parse callback divergence is counted S1), so the listed-but-not-
    counted 'key_free callback never called' is counted as a second S1 defect when its exposing cases pass the UB rule."""
    diff = opt_load("results/ptrtrans_quadtree_diff.json") or {"verdicts": []}
    vs = diff.get("verdicts", [])
    cases = d.get("cases", {})
    unit = "PtrTrans:quadtree"
    hd = d.get("heldout", {})
    items = []
    for x in d.get("defects", []):
        if not x.get("counted", True):
            continue
        fam_div = [v for v in vs if v.get("family") in ("tree.search", "upstream") and v.get("final") == "divergent"]
        clean = [v for v in fam_div if not v.get("c_ub_evidence")]
        y = dict(x)
        y.update(translation="quadtree (library)", source_location=x.get("location"), acceptance_evidence=x.get("evidence"), exposure_count=x.get("exposure"),
                 example_input="\n".join(x["example_input"]) if isinstance(x.get("example_input"), list) else x.get("example_input"),
                 ub_rule={"status": "clean" if clean else "pending", "clean_inputs": len(clean), "of": len(fam_div),
                          "evidence": f"results/ptrtrans_quadtree_diff.json: {len(clean)} of {len(fam_div)} divergent search/upstream cases have no C UB evidence (clang+gcc ASan/UBSan, -O0/-O1/-O2/-O3, char signedness); {d.get('ub_excluded', {}).get('cases')} other cases are original-C UB and excluded"},
                 precondition={"status": "none_stated", "evidence": d.get("precondition_only", {}).get("note")},
                 found_on="our_inputs", heldout="not-measurable", exposed_outside=None, on_check_inputs=None,
                 on_shipped_unrun_inputs=True if x.get("upstream_test") else None,
                 on_shipped_unrun_inputs_evidence=x.get("upstream_test"),
                 translation_units=[unit], acceptance_basis="none: no shipped verdict; the unit tests behind %Equiv. are not shipped (no re-run possible)")
        items.append(y)
    for x in d.get("not_in_totals", []):
        if "key_free" not in x.get("id", ""):
            continue
        kf = [v for v in vs if v.get("family") == "keyfree" and v.get("final") == "divergent"]
        clean = [v for v in kf if not v.get("c_ub_evidence") and not v.get("c_impl_dependent")]
        items.append({
            "defect_id": "PTRTRANS-quadtree-insert_-key_free-1", "translation": "quadtree (library)", "severity": "S1",
            "mechanism": x["mechanism"] + " (A deallocation hook: PtrTrans's Free Judge step removes deallocation calls by design: " + x.get("why_not_counted", "")[:300] + ")",
            "source_location": x.get("location"), "example_input": x.get("example_case_id"), "c_output": x.get("c_output"), "rust_output": x.get("rust_output"),
            "exposure_count": len(clean), "exposure_breakdown": {"divergent_keyfree_cases": len(kf), "ub_clean": len(clean), "per_insert_check": x.get("per_insert_check"),
                                                                  "precondition_violating_cases_left_out": cases.get("by_family", {}).get("keyfree", {}).get("precondition_violating_input")},
            "acceptance_status": d.get("acceptance_status", {}).get("status", "no-record"), "acceptance_evidence": d.get("acceptance_status", {}).get("evidence", []),
            "acceptance_basis": "none: no shipped verdict; the unit tests behind %Equiv. are not shipped",
            "check_exercises_defect": {"status": "unknown", "evidence": "PtrTrans's unit tests are not shipped; its metric excludes deallocation functions (paper Sec. 4.1.3)"},
            "ub_rule": {"status": "clean" if clean else ("pending" if not kf else "excluded_original_c_ub"), "clean_inputs": len(clean), "of": len(kf),
                        "evidence": f"results/ptrtrans_quadtree_diff.json: {len(clean)} of {len(kf)} divergent keyfree cases have empty c_ub_evidence (same sanitizer/opt-level/char-signedness builds as the search cases); results/ptrtrans_quadtree_keyfree.json has no UB fields of its own"},
            "precondition": {"status": "none_stated", "evidence": "3 keyfree divergences on min>max bounds are left out; the mechanism also diverges on valid input"},
            "found_on": "our_inputs", "heldout": "not-measurable", "exposed_outside": None, "on_check_inputs": None, "on_shipped_unrun_inputs": None,
            "translation_units": [unit]})
    return items, tested_building(d, items, d.get("translation_unit")), hd


def tested_building(d, items, unit):
    """'Translations tested' = translations we can build and drive (VERT's 176 already excludes its non-compiling
    functions). Non-building translations are S3 and fall outside 'tested'."""
    n0 = d.get("translations_tested")
    n0 = n0.get("n") if isinstance(n0, dict) else n0
    s3 = {x.get("translation") for x in items if str(x.get("severity")).upper().startswith("S3")}
    units = {u.get("unit"): u for u in d.get("units", []) if isinstance(u, dict)}
    lst = d.get("translations_tested_list")
    if isinstance(lst, list):
        names = [u if isinstance(u, str) else u.get("unit") for u in lst]
        nonbuild = [u for u in names if u in s3 or (units.get(u, {}).get("builds") and not any(units[u]["builds"].values()))]
        n = len(names) - len(nonbuild)
        note = f"{len(names)} listed, minus {len(nonbuild)} that do not build (S3): {nonbuild}"
    else:
        nonbuild = sorted(s3)
        n = (n0 - len(nonbuild)) if isinstance(n0, int) else n0
        note = f"summary's {n0} minus {len(nonbuild)} S3 (non-building) translation(s): {nonbuild}" if nonbuild else f"summary's {n0}; no non-building translation reported"
    return {"unit": unit, "n": n, "summary_value": n0, "rule": "translations we can build and drive; non-building ones are S3 and not tested", "note": note}


def _acc_status(status, evidence):
    """Map a summary's acceptance wording onto the five categories (record first)."""
    st = (status or "").lower()
    ev = (evidence if isinstance(evidence, str) else " ".join(evidence or [])).lower()
    if st in ("accepted-recorded", "accepted-rerun", "rejected", "not-exercised", "no-record"):
        return st
    if st.startswith("rejected"):
        return "rejected"
    if st.startswith("accepted"):
        recorded = any(w in ev for w in ("shipped verdict", "record", "status 'success'", "gives ", "exists", "log"))
        return "accepted-recorded" if recorded else "accepted-rerun" if "re-run" in ev or "rerun" in ev else "accepted-recorded"
    return "no-record"


# arXiv (decided 2026-11-16): rule (ii) applied strictly. Each defect below diverges only on
# inputs that violate a precondition the original states (here a comment, and a function name). SACTOR-P1/P2
# (precondition_only in results/sactor_summary.json) quote the C's own comments the same way.
PRECONDITION_STATED = {
    "SACTOR-P1": {
        "stated_precondition": "offset names one of the 4 bases in the byte: the C's comment says \"Given a byte containing 4 bases, return the character representation of the offset'th base\" (artifacts/sactor/CRUST-bench/datasets/CRUST_bench/CBench/lib2bit/lib.c:200-203, byte2base(uint8_t byte, int offset))",
        "cite": ("artifacts/sactor/CRUST-bench/datasets/CRUST_bench/CBench/lib2bit/lib.c", 200, 203, ["Given a byte containing 4 bases, return the character representation of the offset'th base", "char byte2base(uint8_t byte, int offset)"]),
        "violation": "every UB-clean exposing input has a negative offset (-1, -2, -3, -7, -8), which names none of the 4 bases; the other 70 divergent inputs are C UB (a negative shift exponent) (results/sactor_summary.json)",
        "system_input_domain": "not shown to include the violating inputs (SACTOR's check runs the sample's own test command)",
    },
    "SACTOR-P2": {
        "stated_precondition": "lower <= upper for randrange and value - 1 >= 2 for randinverse: the C's doc comments say randrange returns \"a random unsigned 64-bit integer within the specified range [lower, upper]\" (@param lower \"The lower bound of the range\", @param upper \"The upper bound of the range\") and randinverse \"selects a random number 'a' within the range [2, value - 1]\" (artifacts/sactor/CRUST-bench/datasets/CRUST_bench/CBench/c-aces/lib.c:88-114)",
        "cite": ("artifacts/sactor/CRUST-bench/datasets/CRUST_bench/CBench/c-aces/lib.c", 88, 114, ["It selects a random number 'a' within the range [2, value - 1]", "range [lower, upper].", "@param lower The lower bound of the range.", "@param upper The upper bound of the range."]),
        "violation": "all 42 UB-clean exposing inputs break it: randrange's 41 have lower > upper and randinverse's has value = 1; the Rust's debug build panics on the overflow, the release build agrees with the C (results/sactor_summary.json)",
        "system_input_domain": "not shown to include the violating inputs (SACTOR's check runs the sample's own test command)",
    },
    "ADATRANS-run1-C_425_4": {
        "stated_precondition": "the input is a tree of n nodes, so n - 1 edges follow the header 'n k': the C comment says \"Number of edges is n - 1 for a tree\" and reads that many edges (artifacts/adatrans/dataset/C_CPP/C/src/weekly_contest_425_p4.c:200-201)",
        "cite": ("artifacts/adatrans/dataset/C_CPP/C/src/weekly_contest_425_p4.c", 200, 201, ["// Number of edges is n - 1 for a tree", "int edgesSize = n - 1;"]),
        "violation": "the only exposing inputs are AdaTrans's own two loop examples, which give a 4-node tree 4 edge lines (header '4 2'); the translation agrees with the C on every input of ours (results/adatrans_summary.json: our_inputs_diverging 0 of our_inputs_run 80)",
        "system_input_domain": "AdaTrans's own loop examples violate it (both exposing inputs are those examples); no other input of AdaTrans's check or of ours exposes the defect",
    },
    "H2T-BUF-3": {
        "stated_precondition": "len is the length of str: the function's name and signature say so (buffer_new_with_string_length(char *str, size_t len), artifacts/his2trans/data/rq2/deepseek/c_source/c_source/buffer/src/buffer.c:61)",
        "cite": ("artifacts/his2trans/data/rq2/deepseek/c_source/c_source/buffer/src/buffer.c", 61, 64, ["buffer_new_with_string_length(char *str, size_t len)", "self->len = len;"]),
        "violation": "every exposing input passes len != strlen(str) (e.g. newstrlen 'hello' 0): the translation sets the length to strlen(str), so it agrees with the C whenever len == strlen(str)",
        "system_input_domain": "His2Trans's RQ2 tests never call it with len != strlen(str) (own_inputs_expose_it false)",
    },
}


def _precondition_stated(did):
    p = PRECONDITION_STATED.get(did)
    if not p:
        return None
    path, a, b, needles = p["cite"]
    ok = os.path.isfile(R(path))
    if ok:
        L = src_lines(path)
        ok = all(any(n in L[i - 1] for i in range(a, b + 1)) for n in needles)
        check(f"{did}: stated precondition cited at {path}:{a}-{b}", ok)
    return {"status": "precondition_only", "evidence": f"{p['stated_precondition']}; {p['violation']} (rule ii)"}


def adapt_his2trans(d):
    items = []
    for x in d.get("defects", []):
        ex = x.get("exposure") or {}
        h = x.get("heldout") or {}
        own, out = h.get("own_inputs_expose_it"), h.get("diverges_on_input_outside_own_set")
        e = x.get("example") or {}
        items.append({
            "defect_id": x["id"], "translation": f"{x['translation']} (crate)", "severity": x.get("severity"),
            "mechanism": f"{x.get('function')}: {x.get('mechanism')}", "source_location": {"rust": x.get("rust_location"), "c": x.get("c_location")},
            "example_input": " ; ".join(e.get("input_script", [])) or x.get("example_case"), "c_output": e.get("C"), "rust_output": e.get("Rust"),
            "exposure_count": ex.get("cases"), "exposure_breakdown": ex,
            "acceptance_status": _acc_status(x.get("acceptance_status"), x.get("acceptance_evidence")),
            "acceptance_evidence": [x.get("acceptance_evidence")], "acceptance_basis": "record: the shipped RQ2 verdict (oss8_rq2.json); our re-run agrees",
            "check_exercises_defect": {"status": "exercised-passed" if own else "not-exercised" if own is False else "unknown",
                                       "evidence": f"own RQ2 test cases exposing it: {h.get('own_cases_exposing')}"},
            "ub_rule": {"status": "clean" if ex.get("cases") else "pending", "clean_inputs": ex.get("cases"), "of": ex.get("cases"),
                        "evidence": "results/his2trans_summary.json: exposure counts only UB-clean cases (clang+gcc ASan/UBSan, -O0/-O1 vs -O2, char signedness); "
                                    f"{d.get('ub_excluded', {}).get('divergent_cases_excluded_as_original_C_UB')} divergent cases across the archive are excluded as original-C UB"},
            "precondition": _precondition_stated(x["id"]) or {"status": "none_stated", "evidence": "not reported as precondition-only by the summary"},
            "found_on": "both" if own and out else "own_inputs" if own else "our_inputs",
            "heldout": "outside" if out else "inside" if own else "not-measurable",
            "exposed_outside": out, "on_check_inputs": own, "on_shipped_unrun_inputs": None,
            "translation_units": [f"His2Trans:{x['translation']}"]})
    return items, tested_building(d, items, "crate (OSS8, DeepSeek-V4-Pro)")


_SACTOR_RESULTS = {}


def _sactor_result(name):
    if name not in _SACTOR_RESULTS:
        _SACTOR_RESULTS[name] = json.load(open(R("results", name)))
    return _SACTOR_RESULTS[name]


def _sactor_c_clean(u):
    """A SACTOR case/input is UB-clean when the C has no sanitizer report and no sanitizer-output difference."""
    return not (u or {}).get("c_ub") and not (u or {}).get("san_output_differs_without_report")


def _sactor_example_outputs(x):
    """(c_output, rust_output) of one exposing input, read from the SACTOR result files (the summary records
    the exposing inputs only as prose). libogg: the first differing stdout line of the first UB-clean
    divergent call sequence (the first one the summary names, if it names ids); 'build': the build result;
    own test: the C's stdout and the first differing line; CRUST grid: the return value of the first UB-clean
    divergent input on the primary build (nightly-2024-09-03 debug, SACTOR's own)."""
    t = str(x.get("translation") or "").split()
    ex = str(x.get("exposing_inputs") or "")
    fn = (re.match(r"\w+", str(x.get("function") or "")) or [None])[0]
    if t[:1] == ["libogg"]:
        model, fk = t[1], t[2].split(".")[0]
        if ex == "build":
            tr = _sactor_result("sactor_libogg.json")["translations"][f"{model}/{t[2]}/{t[3]}"]
            b = tr["builds"]["nightly-2024-09-03:debug"]
            ct = _sactor_result("sactor_libogg.json")["files"][fk]["sactor_own_test_on_C"]
            return (f"builds; SACTOR's own test passes on the C (exit {ct.get('exit')})",
                    f"build fails ({b.get('n_errors')} errors): {(b.get('errors') or [''])[0]}")
        rows = _sactor_result("sactor_libogg.json")["files"][fk]["rows"]
        named = re.findall(r"\b(?:bw|fr)\d{4}\b", ex)
        for r in [r for r in rows if r["id"] in named[:1]] + rows:
            rr = r["rust"].get(f"rust:{model}:nightly-2024-09-03:debug")
            if isinstance(rr, dict) and rr.get("first_diff") and _sactor_c_clean(r):
                fd = rr["first_diff"]
                return f"{r['id']}, stdout line {fd['line']}: {fd['c']}", f"{r['id']}, stdout line {fd['line']}: {fd['rust']}"
        return None, None
    if ex.startswith("SACTOR's own test"):
        s = _sactor_result("sactor_crust_owntests.json")["samples"][t[1]]
        for c in s["cases"]:
            rr = c["rust"].get(f"{t[2]}:nightly-2024-09-03:debug") or {}
            if rr.get("agree") is False and _sactor_c_clean(c) and rr.get("first_diff_stdout"):
                fd = rr["first_diff_stdout"]
                return (f"own test {c['case']}: exit {c['c_exit']}, stdout {c['c_stdout_tail']!r}",
                        f"own test {c['case']}: exit {rr.get('exit')}, stdout line {fd['line']}: {fd['rust']!r} (C: {fd['c']!r})")
        return None, None
    pf = _sactor_result("sactor_crust_grid.json")["samples"][t[1]]["per_function"][fn]
    for dv in pf["rust"]["rust:nightly-2024-09-03:debug"]["divergent_inputs"]:
        if not _sactor_c_clean(pf["ub"].get(str(dv["input"]))):
            continue
        call = f"{fn}({', '.join(json.dumps(a) for a in dv['args'])})"
        val = lambda o: " ".join(o.split("end\n")[0].split()[1:]) or "(no output)"
        c_out, r_out = val(dv["c"][0]), val(dv["rust"][0])
        rc = dv["rust"][1]
        if rc != 0:
            msg = [ln.strip() for ln in (dv["rust"][2] or "").splitlines() if "panic" in ln]
            r_out += f", exit {rc}" + (f" (signal {-rc})" if rc < 0 else "") + (f": {msg[-1]}" if msg else "")
        return f"{call} -> {c_out}", f"{call} -> {r_out}"
    return None, None


def adapt_sactor(d):
    items = []
    for x in d.get("defects", []):
        sev_raw = str(x.get("severity", ""))
        prec_only = "precondition_only" in sev_raw
        sev = "crash" if "crash" in sev_raw else "S1" if sev_raw.startswith("S1") else ("S1" if prec_only else sev_raw)
        h = x.get("heldout") or {}
        hs = str(h.get("status", "")).lower()
        acc = x.get("acceptance") or {}
        ub_txt = x.get("ub_rule")
        clean = x.get("exposure_ub_clean_cases")
        own = ("inside" in hs) if hs not in ("n/a", "n/a (s3)", "") else None
        out = ("outside" in hs) if hs not in ("n/a", "n/a (s3)", "") else None
        c_out, r_out = _sactor_example_outputs(x)
        items.append({
            "defect_id": x["id"], "translation": x.get("translation"), "severity": sev,
            "mechanism": f"{x.get('function')}: {x.get('mechanism')}", "source_location": {"rust": x.get("where"), "c": x.get("c_reference")},
            "example_input": x.get("exposing_inputs"), "c_output": c_out, "rust_output": r_out,
            "exposure_count": clean, "exposure_breakdown": {"ub_clean_cases": clean, "before_ub_filter": x.get("exposure_before_ub_filter"),
                                                            "per_build": (d.get("exposure_by_defect_and_build") or {}).get(x["id"])},
            "acceptance_status": _acc_status(acc.get("status"), acc.get("evidence")) if not prec_only else "no-record",
            "acceptance_evidence": [acc.get("evidence")] if acc.get("evidence") else [],
            "acceptance_basis": ("record: SACTOR's per-function success record (function file / unidiomatic_failure_info.json 'success') and the shipped combined.rs; "
                                 "our re-run of its check also passes (exit 0), so no disagreement" if not prec_only and "accepted" in str(acc.get("status")) else f"summary: {acc.get('status')}"),
            "rerun_disagrees": None,
            "check_exercises_defect": {"status": "exercised-passed" if own else "not-exercised" if own is False else "unknown",
                                       "evidence": (h.get("evidence") or "") + (" SACTOR's check reads only the test's exit code; CuTest returns 0 although it prints the failure." if own else "")},
            "ub_rule": {"status": "clean" if isinstance(clean, int) and clean > 0 else "pending", "clean_inputs": clean, "of": x.get("exposure_before_ub_filter"),
                        "evidence": f"results/sactor_summary.json: {ub_txt}"},
            "precondition": ((_precondition_stated(x["id"]) or {"status": "precondition_only", "evidence": (x.get("precondition") or {}).get("evidence") or x.get("mechanism")})
                             if prec_only else {"status": "none_stated", "evidence": "not reported"}),
            "found_on": "both" if own and out else "own_inputs" if own else "our_inputs",
            "heldout": "outside" if out and not own else "inside" if own and not out else "outside" if out else "not-measurable",
            "exposed_outside": out, "on_check_inputs": own, "on_shipped_unrun_inputs": None,
            "translation_units": [f"SACTOR:{x.get('translation')}"],
            "summary_counted": x.get("counted"), "summary_severity": sev_raw})
    return items, tested_building(d, items, "translation unit (sample x model x style)")


EXTRA_TOTALS = {}
ACTOR_MISSCORED = {"B02_synthetic/macrodepth_mul_4", "B02_synthetic/macrodepth_sub_6"}
# replay (own-input) clusters whose classifier label is only the fallback, settled by reading the source
ACTOR_REPLAY_DECISIONS = {
    ("B02_synthetic/strcmp", "PARSE_NUM"): ("excluded_original_c_ub",
        "Both replay inputs pass 999999999999999 to atoi (c_src/src/main.c:114,425); a result that int cannot represent is undefined behaviour in ISO C (C11 7.22.1). glibc truncates strtol's long, which no sanitizer reports, so the automatic rule passed them."),
    ("B02_organic/underhanded-c-luggage", "PARSE_NUM"): ("excluded_original_c_ub",
        "Original-C UB (uninitialized read): the C's output depends on indeterminate stack contents: a comment longer than 80 bytes (scanf %80[^\\n], luggage.c:111) leaves the rest of the line to the next iteration, whose scanf matching failures leave luggage_id/flight_id/departure/arrival (declared without initialiser in the loop body, luggage.c:94-100) unset, and strcpy copies whatever the previous iteration left on the stack ('UA129100 LH1230 FRA DUB'). The Rust (src/main.rs:145-180) builds that record with empty fields. The rule's sanitizers do not detect it (no MemorySanitizer), and -O0/-O1/-O2 happen to agree."),
    ("B02_synthetic/char-to-bool", "PARSE_NUM"): ("unconfirmed",
        "Classifier fallback label; the one replay input ('0\\n0\\nyy\\0yy\\n': C prints -2, Rust 56) has an embedded NUL. Mechanism not yet confirmed in the source, so not counted."),
}


# (translation, mechanism) -> evidence file of inputs from the blind second annotator (docs/reviews/second-rater/)
ACTOR_SECOND_ANNOTATOR = {("B02_synthetic/memmove", "PARSE_NUM"): "results/actoruw_second_rater_memmove.json"}


def actor_mechanism_text(m, doc):
    """The mechanism's description from systems/actoruw/defects.py's docstring, joined across its wrapped
    lines (an entry runs from `  NAME  ` at a two-space indent to the next such entry or a blank line)."""
    hit = re.search(r"\n  " + re.escape(m) + r"\s+(.*?)(?=\n  [A-Z][A-Z0-9_]+\s|\n\n)", doc, re.S)
    return " ".join(hit.group(1).split()) if hit else m

def adapt_actor(d, meta):
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location("actoruw_defects", R("systems/actoruw/defects.py"))
    AD = _ilu.module_from_spec(spec)
    spec.loader.exec_module(AD)
    hs = (opt_load("results/actoruw_heldout_summary.json") or {}).get("data", {})
    hdef = hs.get("exe", {}).get("defects", {})
    rp = (opt_load("results/actoruw_heldout_replay_difftest.json") or {}).get("data", {})
    ubr = (opt_load("results/actoruw_ub_filter_replay.json") or {}).get("data", {})
    counted = {(p["battery"], p["case"], v["id"]): v["counted"] and not any(run_not_clean(x) for x in v.get("variants", {}).values())
               for p in ubr.get("programs", []) for v in p.get("verdicts", [])}   # audit 8 uniform rule
    replay = collections.defaultdict(list)
    for prog in rp.get("programs", []):
        for dv in prog["divergent"]:
            if counted.get((prog["battery"], prog["case"], dv["input"]["id"])):
                replay[(f"{prog['battery']}/{prog['case']}", AD.mechanism(prog, dv))].append(dv)
    by_tm = {(x["translation"], x["mechanism"]): x for x in d.get("defects", [])}
    items, s2 = [], []
    for x in d.get("defects", []):
        if x.get("severity") not in ("S1", "crash"):
            s2.append(f"{x['translation']}::{x['mechanism']}")
            continue
        t, m, kind = x["translation"], x["mechanism"], x.get("kind")
        rep = replay.get((t, m), [])
        hk = hdef.get(f"{t}::{m}") if kind == "exe" else None
        acc = x.get("acceptance_evidence") or {}
        passed = acc.get("passed")
        items.append({
            "defect_id": f"ACTOR-{t}::{m}", "translation": f"{t} ({'executable' if kind == 'exe' else 'library'})", "severity": x["severity"],
            "mechanism": f"{m}: {actor_mechanism_text(m, AD.__doc__)}",
            "source_location": {"rust": f"artifacts/actoruw/results/Test-Corpus/kiro/{t}/{acc.get('phase', 'verified')}/{x.get('location')}"},
            "example_input": json.dumps((x.get("examples") or [{}])[0].get("input")), "c_output": (x.get("examples") or [{}])[0].get("c"),
            "rust_output": (x.get("examples") or [{}])[0].get("rust"),
            "exposure_count": x.get("exposure_cases"), "exposure_breakdown": {"by_outcome": x.get("classes"), "own_input_replay_cases": len(rep), "kind": kind},
            "acceptance_status": "accepted-recorded" if passed is True else "rejected" if passed is False else "no-record",
            "acceptance_evidence": [f"{acc.get('record')}: passed={passed}, vectors_failed={acc.get('vectors_failed')}, phase={acc.get('phase')}"],
            "acceptance_basis": "record: ACTOR's result.json for the graded phase",
            "record_conflict": ("ACTOR's scorer built the default Cargo features (add, 5), not this case's configuration; built as configured, it agrees with the C on all 12 recorded inputs (scratchpad report regrade-actoruw.md, section 5)" if t in ACTOR_MISSCORED else None),
            "check_exercises_defect": {"status": ("exercised-failed" if rep and passed is False else "exercised-passed" if rep else "not-exercised" if kind == "exe" else "unknown"),
                                       "evidence": f"{len(rep)} of the system's recorded inputs diverge through this mechanism (results/actoruw_heldout_replay_difftest.json, UB-clean per actoruw_ub_filter_replay.json)" if kind == "exe" else "library vectors are not recorded"},
            "ub_rule": {"status": "clean" if x.get("exposure_cases") else "pending", "clean_inputs": x.get("exposure_cases"), "of": x.get("exposure_cases"),
                        "evidence": "results/actoruw_summary.json: exposure counts only cases passing the common UB rule (clang+gcc ASan/UBSan+float-cast, -O1/-O2 agreement, char signedness, reproducible oracle; results/actoruw_ub_filter*.json)"},
            "precondition": {"status": "none_stated", "evidence": "none reported"},
            "found_on": "both" if rep else "our_inputs",
            "heldout": ("outside" if hk and hk.get("cases_outside_recorded_set") else "inside" if hk else "not-measurable"),
            "exposed_outside": (bool(hk.get("cases_outside_recorded_set")) if hk else None) if kind == "exe" else None,
            "on_check_inputs": (bool(rep)) if kind == "exe" else None,
            "on_check_inputs_evidence": ("replayed 1,129 of the 1,164 recorded executable vectors (22 truncated previews not replayable)" if kind == "exe" else "library vectors not recorded"),
            "on_shipped_unrun_inputs": None,
            "impl_defined_only": True if m in ("CTYPE_VALUE", "SHIFT_SIGN") else None,
            "translation_units": [f"ACTOR:{t}"], "actor_kind": kind})
    # own-input clusters with no S1/crash defect of that (translation, mechanism)
    for (t, m), dvs in sorted(replay.items()):
        ex = by_tm.get((t, m))
        if ex and ex.get("severity") in ("S1", "crash"):
            continue
        dec = ACTOR_REPLAY_DECISIONS.get((t, m))
        loc = AD.LOCATIONS.get((t.split("/", 1)[1], m))
        cls = collections.Counter(dv["class"] for dv in dvs)
        status = dec[0] if dec else ("counted" if loc else "unconfirmed")
        rec = json.load(open(R(f"artifacts/actoruw/results/Test-Corpus/kiro/{t}/{'verified' if os.path.isdir(R(f'artifacts/actoruw/results/Test-Corpus/kiro/{t}/verified')) else 'translated'}/result.json")))
        dv0 = dvs[0]
        items.append({
            "defect_id": f"ACTOR-{t}::{m}-own", "translation": f"{t} (executable)", "severity": "crash" if any(c == "rust_crash" for c in cls) else "S1",
            "mechanism": f"{m} (found only on the system's recorded inputs)" + (f"; {dec[1]}" if dec else "") + (f" Location: {loc}" if loc else ""),
            "source_location": {"rust": loc or "not confirmed"}, "example_input": json.dumps(dv0["input"]["argv"]) + " stdin " + repr(base64.b64decode(dv0["input"]["stdin_b64"])[:120]),
            "c_output": base64.b64decode(dv0["c"]["stdout_b64"])[:300].decode("utf-8", "replace"), "rust_output": base64.b64decode(dv0["rust"]["stdout_b64"])[:300].decode("utf-8", "replace"),
            "exposure_count": len(dvs), "exposure_breakdown": {"own_input_replay_cases": len(dvs), "by_outcome": dict(cls),
                                                              "our_grid": (f"exposed only as S2 on our grid ({ex.get('exposure_cases')} cases)" if ex else "not exposed by our grid")},
            "acceptance_status": "accepted-recorded" if rec.get("passed") is True else "rejected" if rec.get("passed") is False else "no-record",
            "acceptance_evidence": [f"result.json passed={rec.get('passed')}"], "acceptance_basis": "record: ACTOR's result.json",
            "record_conflict": None,
            "check_exercises_defect": {"status": "exercised-failed" if rec.get("passed") is False else "exercised-passed", "evidence": f"{len(dvs)} recorded inputs diverge"},
            "ub_rule": ({"status": "excluded_original_c_ub", "clean_inputs": 0, "of": len(dvs), "evidence": dec[1]} if status == "excluded_original_c_ub" else
                        {"status": "clean", "clean_inputs": len(dvs), "of": len(dvs), "evidence": "results/actoruw_ub_filter_replay.json: counted=true"}),
            "precondition": {"status": "none_stated", "evidence": "none"},
            "found_on": "own_inputs", "heldout": "inside", "exposed_outside": False, "on_check_inputs": True,
            "on_check_inputs_evidence": "diverges on recorded TRACTOR vectors (replayed)", "on_shipped_unrun_inputs": None,
            "translation_units": [f"ACTOR:{t}"], "actor_kind": "exe", "actor_count_override": None if status == "counted" else status})
    meta.setdefault("ACTOR", {})["replay_clusters"] = {f"{t}::{m}": {"cases": len(v), "maps_to_existing_S1_crash_defect": bool(by_tm.get((t, m)) and by_tm[(t, m)].get("severity") in ("S1", "crash")),
                                                                     "decision": (ACTOR_REPLAY_DECISIONS.get((t, m)) or (None, None))[0]} for (t, m), v in sorted(replay.items())}
    EXTRA_TOTALS["ACTOR"] = {"S2_only_findings": s2, "tested_by_kind": d.get("translations_tested_detail"),
                             "graded_by_acceptance": d.get("graded_translations_by_acceptance_detail")}
    tested = {"unit": "translation (executable or library)", "n": d.get("translations_tested"), "by_kind": d.get("translations_tested_detail"),
              "rule": "translations we can build and drive; non-building ones are S3 and not tested", "note": "S3 = 0 in the summary; 149 = 61 executables + 88 scalar-interface libraries"}
    return items, tested


ADATRANS_C_BUG_NOT_COPIED = {"run1/C_417_2", "run1/C_425_4", "run1/C_434_4", "run1/C_438_3"}   # Rust matches the recorded example output, the C does not (report sec. 9)


def adapt_adatrans(d):
    items = []
    pt = d.get("per_translation", {})
    for x in d.get("defects", []):
        sev_raw = str(x.get("severity"))
        sev = "crash" if sev_raw in ("panic", "crash") else sev_raw
        t = x["translation"]
        h = x.get("heldout") or {}
        ex = x.get("exposure") or {}
        e = x.get("example") or {}
        ptx = pt.get(t, {})
        acc = str(x.get("acceptance_status") or ptx.get("acceptance_status") or "").lower()
        ub_ex = ptx.get("original_C_UB_excluded")
        c_file = (x.get("c_location") or "").split(":")[0]
        items.append({
            "defect_id": f"ADATRANS-{x['id']}", "translation": f"{t} (program)", "severity": sev,
            "mechanism": x.get("mechanism"), "source_location": {"rust": x.get("rust_location"), "c": x.get("c_location")},
            "example_input": e.get("input") or e.get("case"), "c_output": e.get("C"), "rust_output": e.get("Rust"),
            "exposure_count": ex.get("cases"), "exposure_breakdown": ex,
            "acceptance_status": "accepted-recorded" if acc.startswith("accepted") else "rejected" if acc.startswith("rejected") else "no-record",
            "acceptance_evidence": [x.get("acceptance_evidence")], "acceptance_basis": "record: AdaTrans's logs/<inst>.json loop status and validation/<inst>.json fuzz verdict (solved = both pass); our re-run of its check reproduces every run1 verdict",
            "check_exercises_defect": {"status": ("exercised-passed" if h.get("diverges_on_systems_own_inputs") and acc.startswith("accepted") else
                                                  "exercised-failed" if h.get("diverges_on_systems_own_inputs") else "not-exercised"),
                                       "evidence": f"diverges on {h.get('systems_own_input_cases')} of AdaTrans's own check inputs (loop examples + fuzz); its loop compares with recorded example outputs, not the C"},
            "ub_rule": {"status": "clean" if ex.get("cases") else "pending", "clean_inputs": ex.get("cases"), "of": ex.get("cases"),
                        "evidence": f"results/adatrans_summary.json: counted cases pass the common UB rule (clang-14 + gcc-11 ASan/UBSan, -O1/-O2, char signedness, reproducible); {ub_ex or 0} UB cases of this translation excluded"},
            "precondition": _precondition_stated(f"ADATRANS-{x['id']}") or {"status": "none_stated", "evidence": "inputs follow the problem's own fuzz-script bounds (its stated constraints)"},
            "found_on": ("both" if h.get("diverges_on_heldout_input") and h.get("diverges_on_systems_own_inputs") else
                         "own_inputs" if h.get("diverges_on_systems_own_inputs") else "our_inputs"),
            "heldout": "outside" if h.get("diverges_on_heldout_input") else "inside",
            "exposed_outside": bool(h.get("diverges_on_heldout_input")), "on_check_inputs": bool(h.get("diverges_on_systems_own_inputs")),
            "on_check_inputs_evidence": "AdaTrans's check runs its loop examples and its fuzz-oracle inputs (both recorded in the Zenodo bundle)",
            "on_shipped_unrun_inputs": None,
            "translation_units": [f"AdaTrans:{t}"],
            "adatrans_class": "c_bug_not_copied" if t in ADATRANS_C_BUG_NOT_COPIED else None,
            "contested_scan": f"{c_file}: no scanf %u or printf %x/%u/%o conversion" if c_file else None})
    n_built = d.get("translations_built")
    tested = {"unit": "program (run1)", "n": n_built, "summary_value": d.get("translations_tested"),
              "rule": "translations we can build and drive; non-building ones are S3 and not tested",
              "note": f"{d.get('translations_tested')} run1 programs, {n_built} build; S3 build failures: {(d.get('S3') or {}).get('build_failures_run1')}"}
    return items, tested


def ingest_new_systems(meta):
    rows, report = [], {}
    for name in NEW_SYSTEMS:
        paths = [R("results", f"{name}_summary.json")] + [os.path.join(d, f"{name}_summary.json") for d in EXTRA_SUMMARY_DIRS]
        p = next((q for q in paths if os.path.isfile(q)), None)
        if not p:
            report[name] = {"status": "absent", "looked_for": os.path.relpath(paths[0], REPO)}
            continue
        with open(p) as f:
            d = json.load(f)
        tested_override = None
        if name == "ptrtrans":
            items, tested_override, _ = adapt_ptrtrans(d)
        elif name == "his2trans":
            items, tested_override = adapt_his2trans(d)
        elif name == "sactor":
            items, tested_override = adapt_sactor(d)
        elif name == "adatrans":
            d = dict(d, system="AdaTrans")
            items, tested_override = adapt_adatrans(d)
        elif name == "actoruw":
            d = dict(d, system="ACTOR")
            items, tested_override = adapt_actor(d, meta)
        else:
            items = d.get("defects") or d.get("ledger_rows") or d.get("rows") or []
        ok, bad = 0, []
        for i, x in enumerate(items):
            g = {k: next((x[s] for s in syns if s in x), None) for k, syns in SYN.items()}
            sev = SEV.get(str(g["severity"]).lower(), g["severity"])
            acc = g["acceptance_status"]
            probs = [m for m, c in (("severity", sev in ("S1", "crash", "hang", "S3")),
                                    ("acceptance_status", acc in ("accepted-recorded", "accepted-rerun", "rejected", "not-exercised", "no-record")),
                                    ("mechanism", bool(g["mechanism"])), ("source_location", bool(g["source_location"]))) if not c]
            r = row(d.get("system", name), g["translation"] or "?", g["defect_id"] or f"{name.upper()}-{i + 1}", g["mechanism"],
                    g["source_location"], sev, g["example_input"], g["c_output"], g["rust_output"], g["exposure_count"],
                    x.get("exposure_breakdown"), acc if acc in ("accepted-recorded", "accepted-rerun", "rejected", "not-exercised", "no-record") else "no-record",
                    g["acceptance_evidence"] or [], ingested_from=os.path.relpath(p, REPO) if p.startswith(REPO) else p,
                    acceptance_basis=x.get("acceptance_basis"), rerun_disagrees=x.get("rerun_disagrees"), record_conflict=x.get("record_conflict"),
                    on_shipped_unrun_inputs_evidence=x.get("on_shipped_unrun_inputs_evidence"),
                    check_exercises_defect=x.get("check_exercises_defect") or {"status": "unknown", "evidence": "not provided"},
                    ub_rule=x.get("ub_rule") or {"status": "pending", "evidence": "not provided by the summary"},
                    precondition=x.get("precondition") or {"status": "none_stated", "evidence": "not provided by the summary"},
                    found_on=x.get("found_on", "our_inputs"), heldout=x.get("heldout", "not-measurable"), ingest_problems=probs or None,
                    exposed_outside=x.get("exposed_outside"), on_check_inputs=x.get("on_check_inputs"), on_shipped_unrun_inputs=x.get("on_shipped_unrun_inputs"),
                    translation_units=x.get("translation_units") or [f"{d.get('system', name)}:{g['translation'] or '?'}"])
            for extra_k in ("on_check_inputs_evidence", "impl_defined_only", "actor_kind", "exposed_outside_evidence", "adatrans_class", "contested_scan"):
                if x.get(extra_k) is not None:
                    r[extra_k] = x[extra_k]
            for fld in ("ub_rule", "precondition", "check_exercises_defect"):
                if not isinstance(r.get(fld), dict):
                    r[fld] = {"status": "pending" if fld == "ub_rule" else "unknown", "evidence": r.get(fld)}
                    probs.append(f"{fld} not structured")
            ub = r["ub_rule"].get("status")
            r["count_status"] = ("ingest-incomplete" if probs else "S3-not-counted" if sev == "S3" else "hang-not-counted" if sev == "hang" else "precondition_only" if r["precondition"].get("status") == "precondition_only"
                                 else "excluded_original_c_ub" if ub == "excluded_original_c_ub" else "counted-ub-pending" if ub != "clean" else "counted")
            if x.get("actor_count_override"):
                r["count_status"] = x["actor_count_override"]
            rows.append(r)
            ok += not probs
            if probs:
                bad.append({"index": i, "problems": probs})
        TESTED[d.get("system", name)] = tested_override or tested_building(d, items, d.get("unit") or d.get("translation_unit"))
        report[name] = {"status": "ingested", "file": os.path.relpath(p, REPO), "rows": len(items), "clean_rows": ok, "problems": bad}
    meta["new_systems"] = report
    return rows


def totals_v2(rows, extra):
    out = {}
    systems = []
    for r in rows:
        if r["system"] not in systems:
            systems.append(r["system"])
    for sysname in systems:
        rs = [r for r in rows if r["system"] == sysname]
        cnt = [r for r in rs if r["count_status"] == "counted"]
        pend = [r for r in rs if r["count_status"] == "counted-ub-pending"]
        both = cnt + pend
        tr = {r["translation"]: r.get("translations_with_defect", 1) for r in both}
        tr_c = {r["translation"]: r.get("translations_with_defect", 1) for r in cnt}
        u_both = set().union(*[set(r["translation_units"]) for r in both]) if both else set()
        u_cnt = set().union(*[set(r["translation_units"]) for r in cnt]) if cnt else set()
        check(f"{sysname}: |union translation_units| over counted rows = translations_with_counted_defect", len(u_cnt) == sum(tr_c.values()), f"{len(u_cnt)} vs {sum(tr_c.values())}")
        check(f"{sysname}: |union translation_units| over counted+pending rows = translations_with_counted_or_pending_defect", len(u_both) == sum(tr.values()), f"{len(u_both)} vs {sum(tr.values())}")
        for r in rs:
            if r.get("count_status") in ("counted", "counted-ub-pending"):
                check(f"{r['defect_id']}: counted row has a class label and a precondition_review",
                      r.get("class") in ("outright_error", "edge_input_robustness", "c_bug_not_copied", "other") and isinstance(r.get("precondition_review"), dict))
            check(f"{r['defect_id']}: on_check_inputs and on_shipped_unrun_inputs present and true/false/null",
                  "on_check_inputs" in r and "on_shipped_unrun_inputs" in r and r["on_check_inputs"] in (True, False, None) and r["on_shipped_unrun_inputs"] in (True, False, None))
            check(f"{r['defect_id']}: exposed_outside is true/false/null and translation_units is a non-empty list",
                  r.get("exposed_outside") in (True, False, None) and "exposed_outside" in r and isinstance(r.get("translation_units"), list) and len(r["translation_units"]) >= 1)
        out[sysname] = {
            "defects_counted": {"S1": sum(r["severity"] == "S1" for r in cnt), "crash": sum(r["severity"] == "crash" for r in cnt), "total": len(cnt)},
            "defects_counted_ub_pending": {"S1": sum(r["severity"] == "S1" for r in pend), "crash": sum(r["severity"] == "crash" for r in pend), "total": len(pend)},
            "translations_with_counted_defect": sum(tr_c.values()),
            "translations_with_counted_or_pending_defect": sum(tr.values()),
            "translation_units_counted": sorted(u_cnt),
            "exposed_outside_of_counted_and_pending": dict(collections.Counter(str(r.get("exposed_outside")) for r in both)),
            "outside_vs_check_inputs_of_counted": dict(collections.Counter(
                ("null" if r.get("exposed_outside") is None or r.get("on_check_inputs") is None else
                 "both" if r["exposed_outside"] and r["on_check_inputs"] else
                 "outside_only" if r["exposed_outside"] else
                 "check_inputs_only" if r["on_check_inputs"] else "neither") for r in cnt)),
            "translations_tested": TESTED.get(sysname),
            "exposure_counted": exposure_by_outcome(cnt),
            "acceptance_of_counted_and_pending": dict(collections.Counter(r["acceptance_status"] for r in both)),
            "found_only_on_own_inputs": [r["defect_id"] for r in both if r.get("found_on") == "own_inputs"],
            "heldout_of_counted_and_pending": dict(collections.Counter(r.get("heldout") for r in both)),
            "not_counted": {r["defect_id"]: r["count_status"] for r in rs if r not in both},
            "class_counts_of_counted": dict(collections.Counter(r.get("class") for r in cnt)),
        }
        if sysname in EXTRA_TOTALS:
            out[sysname].update(EXTRA_TOTALS[sysname])
            kinds = collections.defaultdict(set)
            for r in cnt:
                kinds[r.get("actor_kind")].update(r["translation_units"])
            out[sysname]["translations_with_counted_defect_by_kind"] = {k: len(v) for k, v in kinds.items()}
    return out



# ======================================================================== METHODOLOGY REVIEW PASS
# Applied to every system after apply_v2 and the unspecified-value / deallocation-hook post-pass:
#  (a) contested C-library conversions (a negative value to scanf %u/%zu, a negative int to printf
#      %x/%u; C11 7.21.6.1p9, 7.21.6.2p10): keep only exposing inputs that avoid them; a defect with
#      none left is excluded_contested_conversion;
#  (b) a `class` label on every counted row; (c) a per-row `precondition_review` with the tie-breaker
#      "the system's own input domain" (used where the original states no precondition); (d) hygiene:
#      ACTOR examples re-selected from the full difftest records so they show the divergence and avoid
#      contested inputs; RustMap triage notes brought in line with the v2 triage rules.
NEG_TOKEN = re.compile(rb"(?:^|[\s,;:=(])-\d")
CLASS_RULES = {
    # (system, mechanism-or-id) -> class; default per system below
    ("ACTOR", "PARSE_NUM"): "edge_input_robustness", ("ACTOR", "UTF8_PANIC"): "edge_input_robustness",
    ("ACTOR", "UTF8_EOF"): "edge_input_robustness", ("ACTOR", "TOKEN_PANIC"): "edge_input_robustness",
    ("ACTOR", "WS_SET"): "edge_input_robustness", ("ACTOR", "NAN_FMT"): "edge_input_robustness",
    ("ACTOR", "LINE_SPLIT"): "c_bug_not_copied", ("ACTOR", "NUL_BYTE"): "c_bug_not_copied",
    ("ACTOR", "ASSERT_DROPPED"): "outright_error", ("ACTOR", "BREAK_LOOP"): "outright_error",
    ("ACTOR", "SHIFT_SIGN"): "other", ("ACTOR", "CTYPE_VALUE"): "other",
    ("His2Trans", "H2T-BUF-4"): "c_bug_not_copied", ("His2Trans", "H2T-RGBA-1"): "edge_input_robustness",
    ("C2SaferRust", "C2SR-head-2"): "edge_input_robustness", ("C2SaferRust", "C2SR-tail-3"): "edge_input_robustness",
    ("Syzygy", "SYZYGY-zopfli-ZopfliDeflate-1"): "edge_input_robustness",
    ("RustMap", "RUSTMAP-bzip2-BZ-3"): "edge_input_robustness",
    ("RustAssure", "RUSTASSURE-u8c-u8next_-1"): "edge_input_robustness",
    ("VERT", "VERT-FIND_SMALLEST_VALUE_REPRESENTED_SUM_SUBSET_GIVEN_ARRAY-1"): "edge_input_robustness",
    ("SACTOR", "SACTOR-C3"): "edge_input_robustness",
    ("AdaTrans", "ADATRANS-run1-C_432_4"): "edge_input_robustness",
}
CLASS_WHY = {
    "outright_error": "wrong result or crash on ordinary, well-formed input",
    "edge_input_robustness": "differs only on malformed, empty, invalid-encoding or extreme input whose C behaviour is defined",
    "c_bug_not_copied": "the Rust does what the C evidently intends where the C is buggy or truncating",
    "other": "differs only in a documented implementation-defined value of the platform C",
}


def his2trans_rgba_inputs():
    import importlib.util as _ilu
    sys.path.insert(0, R("systems"))
    spec = _ilu.spec_from_file_location("h2t_gen", R("systems/his2trans/gen_inputs.py"))
    G = _ilu.module_from_spec(spec)
    spec.loader.exec_module(G)
    out = {}
    for c in G.gen_rgba():
        if c["func"] == "rgba_to_string":
            ts = [l for l in c["lines"] if l.startswith("ts ")][0].split()[1:]
            out[c["id"]] = tuple(struct.unpack(">d", bytes.fromhex(h))[0] for h in ts)
    return out


def review_pass(rows, meta):
    changes = []
    # ---- (a) contested conversions
    actor_sum = opt_load("results/actoruw_summary.json") or {}
    actor_dt = (opt_load("results/actoruw_difftest.json") or {}).get("data", {})
    dv_by_id = {}
    for prog in actor_dt.get("programs", []):
        for dv in prog.get("divergent", []):
            dv_by_id[(f"{prog['battery']}/{prog['case']}", dv["input"]["id"])] = dv
    rp = (opt_load("results/actoruw_heldout_replay_difftest.json") or {}).get("data", {})
    replay_by_t = collections.defaultdict(list)
    for prog in rp.get("programs", []):
        for dv in prog.get("divergent", []):
            replay_by_t[f"{prog['battery']}/{prog['case']}"].append(dv)
    case_ids = {(x["translation"], x["mechanism"]): x.get("case_ids", []) for x in actor_sum.get("defects", [])}
    # ACTOR translations whose C reads with scanf-family %u / %zu (scanned from the shipped C sources)
    u_scan = set()
    import glob as _glob
    for cdir in _glob.glob(R("artifacts/actoruw/results/Test-Corpus/kiro/*/*/*/c_src")):
        t = "/".join(cdir.split("/")[-4:-2])
        for f in _glob.glob(cdir + "/**/*.c", recursive=True):
            src = open(f, errors="replace").read()
            if re.search(r'\b(?:f?scanf|sscanf)\s*\([^;]*?"[^"]*%[0-9]*(?:z|l|ll|h)?u', src):
                u_scan.add(t)
    meta.setdefault("review", {})["actor_translations_reading_with_scanf_u"] = sorted(u_scan)
    for r in rows:
        if r["system"] != "ACTOR" or r.get("count_status") not in ("counted", "counted-ub-pending"):
            continue
        t, m = r["translation"].split(" (")[0], r["defect_id"].split("::")[-1].replace("-own", "")
        ids = case_ids.get((t, m)) or []
        dvs = [dv_by_id[(t, i)] for i in ids if (t, i) in dv_by_id]
        if r["defect_id"].endswith("-own"):   # own-input rows: the replayed recorded inputs of this translation
            dvs = list(replay_by_t.get(t, []))
        def contested(dv):
            if t not in u_scan or m != "PARSE_NUM":
                return False
            data = base64.b64decode(dv["input"].get("stdin_b64", "")) + b" " + " ".join(dv["input"].get("argv", [])).encode()
            return bool(NEG_TOKEN.search(b" " + data))
        keep = [dv for dv in dvs if not contested(dv)]
        r["contested_conversion_review"] = {"scanf_u_reader": t in u_scan, "exposing_cases": len(dvs), "kept": len(keep),
                                            "rule": "a negative number read by scanf %u/%zu (C11 7.21.6.2p10, strtoul semantics) is contested; such inputs are dropped"}
        # (d) example re-selection: first kept case, showing the first differing output line
        if keep:
            dv = keep[0]
            co, ro = base64.b64decode(dv["c"]["stdout_b64"]), base64.b64decode(dv["rust"]["stdout_b64"])
            cl, rl = co.split(b"\n"), ro.split(b"\n")
            k = next((i for i, (x, y) in enumerate(zip(cl, rl)) if x != y), min(len(cl), len(rl)))
            r["example_input"] = json.dumps({"argv": dv["input"].get("argv"), "stdin": base64.b64decode(dv["input"].get("stdin_b64", ""))[:160].decode("latin-1")})
            r["c_output"] = {"exit": dv["c"]["rc"], "first_differing_stdout_line": k + 1, "line": (cl[k] if k < len(cl) else b"<end>")[:200].decode("utf-8", "replace"),
                             "stderr": base64.b64decode(dv["c"].get("stderr_b64", ""))[:160].decode("utf-8", "replace")}
            r["rust_output"] = {"exit": dv["rust"]["rc"], "first_differing_stdout_line": k + 1, "line": (rl[k] if k < len(rl) else b"<end>")[:200].decode("utf-8", "replace"),
                                "stderr": base64.b64decode(dv["rust"].get("stderr_b64", ""))[:240].decode("utf-8", "replace")}
        if dvs and len(keep) < len(dvs):
            before = r["exposure_count"]
            r["exposure_before_contested_filter"] = before
            r["exposure_count"] = len(keep)
            if r["exposure_breakdown"].get("by_outcome"):
                r["exposure_breakdown"]["by_outcome_before_contested_filter"] = dict(r["exposure_breakdown"]["by_outcome"])
                r["exposure_breakdown"]["by_outcome"] = dict(collections.Counter("crash" if dv["class"] == "rust_crash" else dv["class"] for dv in keep))
            sr = ACTOR_SECOND_ANNOTATOR.get((t, m))
            sr = opt_load(sr) if sr and not keep else None
            sr_in = [x for x in (sr or {}).get("inputs", []) if not x["agree"] and x["ub_filter"]["counted"]]
            if not keep and sr_in:
                # inputs from a blind second annotator (no negative number, so not contested), run through the
                # ACTOR difftest + ub_filter on the stage binaries (systems/actoruw/second_rater_memmove.py)
                x0, src = sr_in[0], ACTOR_SECOND_ANNOTATOR[(t, m)]
                r["exposure_count"] = len(sr_in)
                r["exposure_breakdown"]["by_outcome"] = dict(collections.Counter(x["class"] for x in sr_in))
                r["exposure_breakdown"]["second_annotator_inputs"] = {"file": src, "inputs": len(sr["inputs"]), "divergent_ub_clean": len(sr_in)}
                r["example_input"] = json.dumps({"argv": x0["argv"], "stdin": x0["stdin"]})
                r["c_output"], r["rust_output"] = x0["c"], x0["rust"]
                r["ub_rule"] = {"status": "clean", "clean_inputs": len(sr_in), "of": len(sr["inputs"]), "evidence": f"{src}: ub_filter.check_case counted=true"}
                r["found_on"] = "second_annotator_input"
                outside = [x for x in sr_in if x["in_recorded_set"] == "no"]
                r["exposed_outside"], r["on_check_inputs"] = bool(outside), False
                r["heldout"] = "outside" if outside else "inside"
                r["on_check_inputs_evidence"] = (f"{len(outside)} of {len(sr_in)} second-annotator inputs are not among ACTOR's recorded inputs "
                                                 f"({sr['provenance']['recorded_inputs']['vectors']} vectors in {sr['provenance']['recorded_inputs']['log']}); "
                                                 f"the {len(dvs)} recorded input(s) that diverge through this mechanism pass a negative number to scanf %u and stay excluded")
                r["notes"] = ((r.get("notes") or "") + f" Counted on {len(sr_in)} UB-clean inputs from a blind second annotator's rating "
                              f"(docs/reviews/second-rater/ratings.json, case 11; {src}): a byte token with a numeric prefix such as 5x, which "
                              f"scanf %u reads as 5 and the Rust rejects. The {len(dvs)} exposure(s) on ACTOR's recorded inputs (a negative "
                              "number read by scanf %u) remain excluded under the contested-conversion rule.").strip()
                changes.append(f"{r['defect_id']}: recorded exposure contested; counted on {len(sr_in)} second-annotator inputs ({src})")
            elif not keep:
                r["count_status"] = "excluded_contested_conversion"
                changes.append(f"{r['defect_id']}: excluded_contested_conversion (all {len(dvs)} exposing inputs pass a negative number to scanf %u)")
            else:
                changes.append(f"{r['defect_id']}: exposure {before} -> {len(keep)} (contested %u inputs dropped)")
    # His2Trans rgba_to_string: a negative int to %.2x (alpha == 1 branch) is contested
    rg = [r for r in rows if r["defect_id"] == "H2T-RGBA-1"]
    if rg and rg[0].get("count_status") in ("counted", "counted-ub-pending"):
        r = rg[0]
        ins = his2trans_rgba_inputs()
        att = ((opt_load("results/his2trans_attribution.json") or {}).get("attribution", {}).get("rgba", {}).get("per_defect", {}).get("H2T-RGBA-1", {}).get("fixed_ids", []))
        def contested_rgba(t4):
            rr, gg, bb, aa = t4
            return aa == 1 and any(int(c * 255) < 0 for c in (rr, gg, bb))
        keep = [i for i in att if i in ins and not contested_rgba(ins[i])]
        check("H2T-RGBA-1: every exposing case id regenerated from systems/his2trans/gen_inputs.py", all(i in ins for i in att), str(len(att)))
        r["contested_conversion_review"] = {"exposing_cases": len(att), "kept": len(keep),
                                            "dropped": [f"{i}: (r,g,b,a)={ins[i]}" for i in att if i not in keep],
                                            "rule": "a negative int passed to printf %x (the alpha == 1 branch, rgba.c:254-257) is contested; %d with a negative int (other branch) is fine"}
        if len(keep) < len(att):
            changes.append(f"H2T-RGBA-1: exposure {r['exposure_count']} -> {len(keep)} (negative channel to %.2x dropped)")
            r["exposure_before_contested_filter"] = r["exposure_count"]
            r["exposure_count"] = len(keep)
            if not keep:
                r["count_status"] = "excluded_contested_conversion"
    # ---- (b) class labels
    for r in rows:
        if r.get("count_status") not in ("counted", "counted-ub-pending"):
            continue
        mech = r["defect_id"].split("::")[-1].replace("-own", "") if r["system"] == "ACTOR" else None
        c = CLASS_RULES.get((r["system"], mech)) if mech else None
        c = c or r.get("adatrans_class") or CLASS_RULES.get((r["system"], r["defect_id"])) or "outright_error"
        r["class"] = c
        r["class_rule"] = CLASS_WHY[c]
    # ---- (c) precondition review
    for r in rows:
        did, sysn = r["defect_id"], r["system"]
        pr = r.get("precondition", {})
        if did == "VERT-FIND_SMALLEST_VALUE_REPRESENTED_SUM_SUBSET_GIVEN_ARRAY-1":
            rev = {"decision": "keep", "stated_precondition": "none (name and C file state no sortedness; only license comments, lines 1-6)",
                   "system_input_domain": "the benchmark's own TransCoder test tuples include unsorted arrays: param0_1 = {-14,-82,12,...} and param0_3 = {17,89,44} (FIND_SMALLEST_VALUE_REPRESENTED_SUM_SUBSET_GIVEN_ARRAY.c:35,37)",
                   "justification": "no stated precondition; tie-breaker: the system's own input domain includes unsorted arrays, so the unsorted exposing inputs are in domain"}
        elif did in ("FLOURINE-clip-1", "FLOURINE-opl_emu_clamp-1"):
            rev = {"decision": "exclude (precondition_only)", "stated_precondition": "min <= max, stated by the C's parameter names (clip(a, amin, amax), opl_emu_clamp(value, minval, maxval))",
                   "system_input_domain": "Flourine's own logged fuzzer inputs violate it (e.g. clip(-1, -1, -1979770625), results/heldout_flourine.json logged_examples)",
                   "justification": "a stated precondition decides; the tie-breaker applies only where none is stated. This is the one row where the stated precondition and the system's own domain disagree (the paper says so)."}
        elif did in PRECONDITION_STATED:
            p = PRECONDITION_STATED[did]
            rev = {"decision": "exclude (precondition_only)", "stated_precondition": p["stated_precondition"],
                   "system_input_domain": p["system_input_domain"],
                   "justification": f"a stated precondition decides (rule ii, applied strictly in the arXiv version): {p['violation']}"}
        elif did == "FLOURINE-compute_phase_step-1":
            rev = {"decision": "keep", "stated_precondition": "none: the C (opl_emu_opl_compute_phase_step.c:22-43) states no range for block_freq, multiple or lfo_raw_pm; its comments only describe fnum as 10 bits of block_freq and multiple as an x.1 value",
                   "system_input_domain": "Flourine's fuzzer inputs (logged examples), which are full-width u32/i32 values such as (4278230271, 1644166913, -16714241); the panic also occurs within OPL register ranges, e.g. (256, 2, -1) (" + FL_PROFILE + " compute_phase_step_full_grid_crash_inputs.probes_outside_grid)",
                   "justification": "no stated precondition is violated by the exposing inputs; every exposing input of our grid has block_freq >= 8192, but the function reads only bits 0-12 of it and the same panic occurs at block_freq 256"}
        elif did == "VERT-CEILING_IN_A_SORTED_ARRAY-1":
            rev = {"decision": "keep", "stated_precondition": "sorted array (benchmark name)", "system_input_domain": "TransCoder tuples",
                   "justification": "exposing inputs include sorted arrays (e.g. [0,0], [-1,-1]), so the precondition is met on some exposing input"}
        elif pr.get("status") == "precondition_only":
            rev = {"decision": "exclude (precondition_only)", "stated_precondition": pr.get("evidence"), "system_input_domain": "not shown to include the violating inputs",
                   "justification": "every exposing input violates a stated precondition"}
        else:
            dom = {"VERT": "TransCoder test tuples and VERT's Kani/bolero inputs", "Flourine": "Flourine's fuzzer inputs (logged examples)",
                   "RustAssure": "libcsv/u8c composed tests and per-function KLEE harness", "C2SaferRust": "coreutils shell tests",
                   "Syzygy": "Syzygy's random-string equivalence suite (10 B to 10 MB; not shipped)", "RustMap": "fixed program runs (Rosetta) / printable test files (bzip2)",
                   "His2Trans": "RQ2 Rust tests", "SACTOR": "the sample's own test command", "PtrTrans": "function-level unit tests (not shipped)",
                   "ACTOR": "TRACTOR test vectors (leading blanks, '--1', non-numeric menu inputs among them)",
                   "AdaTrans": "the problem's loop examples and fuzz-oracle inputs; our inputs stay within its fuzz-script bounds"}.get(sysn, "not recorded")
            rev = {"decision": "keep", "stated_precondition": "none found in the name, comments, header or documentation of the exposing function",
                   "system_input_domain": dom, "justification": "no stated precondition is violated by the exposing inputs"}
        r["precondition_review"] = rev
    # ---- (d) RustMap triage notes in line with the v2 rules
    for r in rows:
        if r["system"] == "RustMap" and r["defect_id"].startswith("RUSTMAP-rosetta-"):
            task = r["translation"].split()[1]
            v = ROSETTA_V2.get(task)
            old = r.get("triage_note")
            if v:
                r["triage_note"] = f"v2 triage rules: {v[1]} ({v[2]})" + (f" [superseded helper note: {old}]" if old else "")
            elif old and ("MISCLASSIFIED" in old or "inconsistent" in old):
                r["triage_note"] = "v2 triage rules: wrong_output (same task, counterpart functions) [superseded helper note: " + old + "]"
    meta.setdefault("review", {})["status_changes"] = changes
    return rows


def build(probe_path, out_path, probe_bzip2_path=None):
    global PROBE_BZIP2
    if probe_bzip2_path:
        with open(probe_bzip2_path) as f:
            PROBE_BZIP2 = json.load(f)
    probe_json = None
    if probe_path:
        with open(probe_path) as f:
            probe_json = json.load(f)
    rows, meta = [], {}
    for name, fn in [("VERT", build_vert), ("Flourine", build_flourine), ("RustAssure", build_rustassure),
                     ("C2SaferRust", lambda: build_c2saferrust(probe_json)), ("Syzygy", build_syzygy), ("RustMap", build_rustmap)]:
        r, m = fn()
        rows += r
        meta[name] = m
    vs = dict(load(VERT_SCALAR)["summary"])
    _vw = opt_load(VERT_SCALAR_WIDE)
    vs["tested"] += (_vw or {}).get("summary", {}).get("tested", 0)
    va = load("results/vert_arrays_adjusted.json")
    TESTED.update({
        "VERT": {"unit": "function", "n": vs["tested"] + len([r for r in va["rows"] if r["kind"] != "no_assessable_input"]),
                 "detail": f"{vs['tested']} drivable integer-return scalar functions (int, and since the arXiv version long / long long: {VERT_SCALAR_WIDE}) + {len([r for r in va['rows'] if r['kind'] != 'no_assessable_input'])} array/float functions with an assessable input"},
        "Flourine": {"unit": "shipped 'success' translation file (function x model x configuration)",
                     "n": meta["Flourine"]["translations_tested"]["scalar_success"] + meta["Flourine"]["translations_tested"]["struct_success"],
                     "detail": f"{meta['Flourine']['translations_tested']['scalar_success']} scalar + {meta['Flourine']['translations_tested']['struct_success']} OPL struct accessors"},
        "RustAssure": {"unit": "function", "n": None,
                       "detail": "2 scalar GPT-4o functions driven black-box (both agree) + the functions composed into the two shipped crates (libcsv csv_link, u8c u8c_link) driven end to end; the composed crates exercise an unmeasured subset of their functions, so no denominator is claimed"},
        "C2SaferRust": {"unit": "utility crate (rust_WIP)", "n": 4, "detail": "cat, head, tail, uniq (pwd, split, truncate not grid-tested)"},
        "Syzygy": {"unit": "library", "n": 1, "detail": "Zopfli; only the empty-input property was tested"},
        "RustMap": {"unit": "program", "n": None,
                    "detail": "bzip2 + the Rosetta programs whose C and Rust both build under RustMap's build commands (results/rustmap_rosetta.json: pairs - rust_build_fail - c_build_fail)"},
    })
    rr = load("results/rustmap_rosetta.json")["summary"]
    TESTED["RustMap"]["n"] = 1 + rr["pairs"] - rr["rust_build_fail"] - rr["c_build_fail"]
    rows = apply_v2(rows, meta)
    # Two general rules from the methodology review (13 Nov), applied to every system:
    #  (1) A divergence only in a value the C standard leaves unspecified is not a defect (the oracle is the C
    #      built by GCC/Clang on LP64 glibc; implementation-defined behaviour is fixed by that platform, but an
    #      unspecified value such as isdigit()'s "nonzero" is not).
    #  (2) A callback that only releases memory is not a result: dropping it changes no returned value or state.
    #      Callbacks that deliver data (e.g. a parser's field callback) remain results.
    UNSPECIFIED_VALUE = {"ACTOR-B01_synthetic/027_ctype_ascii_lib::CTYPE_VALUE":
                         "the Rust and the C call the same libc isdigit(); ISO C 7.4.1 only promises a nonzero value, and the outputs differ only in which nonzero value",
                         "H2T-GEN-1":
                         "every exposing output is a weight drawn from the C library's rand() (GENANN_RANDOM() = rand()/RAND_MAX); ISO C 7.22.2.1 leaves the rand() sequence unspecified (glibc's is one choice), so the C's values are not a specified result; the contract change (srand and the GENANN_RANDOM hook no longer have an effect) is noted but is not observable in values ISO C specifies"}
    DEALLOC_HOOK = {"PTRTRANS-quadtree-insert_-key_free-1":
                    "key_free is a deallocation hook; PtrTrans replaces manual frees with Rust ownership by design, and every returned value and tree state agrees"}
    for r in rows:
        if r["defect_id"] in UNSPECIFIED_VALUE and r.get("count_status") in ("counted", "counted-ub-pending"):
            r["count_status"] = "excluded_unspecified_value"; r["exclusion_note"] = UNSPECIFIED_VALUE[r["defect_id"]]
        if r["defect_id"] in DEALLOC_HOOK and r.get("count_status") in ("counted", "counted-ub-pending"):
            r["count_status"] = "excluded_dealloc_hook"; r["exclusion_note"] = DEALLOC_HOOK[r["defect_id"]]
    # Rosetta acceptance (methodology review, 13 Nov): the shipped whole-log diff differs on EVERY program
    # (its header lines differ), so neither it nor our re-run of it is a per-program verdict. Under the
    # paper's definition these translations therefore have no record. RustMap's paper (Sec. 5.1) reports
    # all 125 Rosetta programs as producing the same output as the C.
    for r in rows:
        if r["system"] == "RustMap" and r["defect_id"].startswith("RUSTMAP-rosetta-"):
            r["acceptance_status"] = "no-record"
            r["acceptance_basis"] = "no per-program record; the documented whole-log diff differs on every program"
            r["acceptance_evidence"] = ["The shipped whole-log diff (artifacts/rustmap/c-code/rosseta-125/diff_rosetta_result.log) "
                                        "differs on every program, including programs whose outputs agree after folding, so it records "
                                        "no per-program verdict; RustMap's paper (Sec. 5.1) reports all 125 Rosetta programs as giving "
                                        "the same output as the C."] + list(r.get("acceptance_evidence") or [])
    rows = review_pass(rows, meta)
    tot = totals_v2(rows, meta)
    h2s = opt_load("results/his2trans_summary.json")
    if h2s and "His2Trans" in tot:
        check("His2Trans: results/his2trans_summary.json translations_with_defect = the ledger's translations_with_counted_defect",
              h2s.get("translations_with_defect") == tot["His2Trans"]["translations_with_counted_defect"],
              f"{h2s.get('translations_with_defect')} vs {tot['His2Trans']['translations_with_counted_defect']}")
        check("His2Trans: the summary's defects_not_counted are the ledger's not-counted His2Trans rows",
              set(h2s.get("defects_not_counted") or {}) == set(tot["His2Trans"]["not_counted"]),
              f"{sorted(h2s.get('defects_not_counted') or {})} vs {sorted(tot['His2Trans']['not_counted'])}")
    prov = {
        "script": "scripts/build_defect_ledger.py",
        "command": "python3 -I scripts/build_defect_ledger.py build" + (f" --probe {probe_path}" if probe_path else "") + (f" --probe-bzip2 {probe_bzip2_path}" if probe_bzip2_path else ""),
        "git_rev": git_rev(), "host": platform.system(), "arch": platform.machine(),
        "python": platform.python_version(),
        "toolchains_images": {"build": "none (reads committed result files and artifacts only)",
                              "probe_c2saferrust": (probe_json or {}).get("provenance"),
                              "probe_bzip2": (PROBE_BZIP2 or {}).get("provenance")},
        "inputs_sha256": {p: sha(p) for p in sorted({
            "results/vert_diff_grid2.json", "results/vert_grid2_ub_recheck.json", "results/vert_bolero_vacuity_current.json", VERT_ARRAYS, "results/vert_arrays_adjusted.json", "results/vert_kani_vacuity.json",
            "results/vert_kani_unwinding_on.json", "results/vert_bolero_vacuity.json", *[p_ for p_ in ("results/vert_kani_verify_8.json", "results/vert_kani_verify_grid2.json", "results/arxiv_vert_wide_kani_verify.json") if os.path.isfile(R(p_))], "results/flourine_diff.json", "results/flourine_struct.json",
            "systems/rustassure/libcsv_e2e/results.json", "systems/rustassure/u8c_e2e/results.json", "results/syzygy_empty_deflate.json",
            "results/rustmap_bzip2_check.json", "results/rustmap_rosetta.json", "results/rustmap_rosetta_triage.json",
            "results/c2saferrust_acceptance_suite.json", "artifacts/vert/c_results.csv", f"{FL}/all_statistics/new_big_table.csv",
            *[p for p in ("results/vert_crash_ub_recheck.json", "results/oracle_ub_c2saferrust.json", "results/oracle_ub_flourine.json", "results/oracle_ub_rustassure.json",
                          "results/oracle_ub_rustmap_bzip2.json", "results/oracle_ub_rustmap_rosetta.json", "results/oracle_ub_pending.json",
                          "results/vert_wrong_answer_ub_recheck.json", "results/heldout_c2saferrust.json", "results/heldout_rustassure.json",
                          "results/heldout_rustmap.json", "results/heldout_flourine.json", "results/heldout_overlap.json", "results/heldout_vert_grid2.json",
                          "results/flourine_shipped_vs_log.json", FL_SCALAR, FL_STRUCT, FL_PROFILE, FL_HELDOUT_PROFILE,
                          "results/arxiv_c2saferrust_head_aarch64.json", "results/arxiv_rustmap_bzip2_check_uncapped.json",
                          VERT_SCALAR_WIDE, VERT_SCALAR_WIDE_UB, VERT_SCALAR_WIDE_HELDOUT, VERT_WIDE_BOLERO, VERT_WIDE_KANI_UNWIND,
                          "results/his2trans_summary.json", "results/adatrans_summary.json") if os.path.isfile(R(p))],
            *[f"results/{u}_{k}_v4.json" for u in ["cat", "head", "tail", "uniq"] for k in ("cases", "diff")],
            *[f"results/{u}_diff_v4_c2rust_baseline.json" for u in ["cat", "head", "tail", "uniq"]]})},
        "date": datetime.datetime.now().isoformat(timespec="seconds"),
        "probe_environment": {"host": "Linux x86_64 box, private work dir ~/work/defects (copies of the artifact trees; artifacts read-only)",
                              "c2saferrust_image": "crust-heldout (= crust-stable) sha256:d57dd28c18eea29cbcca12693e1da442f37be6ac4800c77ef7a0ed96f1ce46ba, docker --cpus=1, gdb 12.1 installed in the probe container",
                              "bzip2_image": "crust-syzygy sha256:807b161a09f5189d5845d5d109f7cf81f7b8d05e152a7ec4d3b79c7a6c6d9e2b (linux/amd64), docker --cpus=1",
                              "commands": ["docker exec <crust-heldout container> python3 build_defect_ledger.py probe --root /w --out /w/probe.json",
                                           "docker run --cpus=1 --platform linux/amd64 -v <rustmap artifacts>:/art:ro crust-syzygy python3 build_defect_ledger.py probe-bzip2 --art /art --out /w/probe_bzip2.json"]},
        "mechanism_sources": "C2SaferRust/Flourine/RustAssure/Syzygy mechanisms read and cited by the ledger author; VERT and RustMap mechanisms read by helper agents (C and Rust side by side, hand-evaluated on a diverging sample; VERT also emulated over the exact grid); all cited locations re-checked by this script (see checks).",
    }
    failed = [c for c in CHECKS if not c["ok"]]
    out = {
        "ledger_version": 2,
        "provenance": prov,
        "definitions": {k: v for k, v in {
            "defect": "one distinct mechanism in a translation's source that makes it diverge from the C on UB-clean input: one code location or one coherent mistake, confirmed by reading the translated source. Inputs, flags, locales or configurations that hit the same mechanism are one defect; faulty statements that each produce a divergence on their own inputs are separate defects.",
            "translation": "the unit each system ships (see totals.*.translations_tested.unit)",
            "exposure": "number of our cases/inputs that show the defect (C2SaferRust also with the two locale variants folded); exposures of co-located defects can overlap",
            "severity": "S1 = wrong output or exit code; crash = segfault or panic; hang = Rust never terminates where the C returns (listed, not in totals)",
            "acceptance_status": "accepted-recorded | accepted-rerun | rejected | not-exercised | no-record (module docstring)",
            "check_exercises_defect": "exercised-passed | exercised-failed | not-exercised | unknown: whether the system's own check ever runs the defective behaviour"}.items()},
        "totals": tot,
        "meta": meta,
        "checks": {"n": len(CHECKS), "failed": failed, "all": CHECKS},
        "rows": rows,
    }
    with open(out_path, "w") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    print(f"wrote {out_path}: {len(rows)} rows; checks {len(CHECKS) - len(failed)}/{len(CHECKS)} ok")
    for c in failed:
        print("  FAILED:", c["check"], c["detail"][:200])
    for s, t in tot.items():
        print(f"  {s:12s} counted {t['defects_counted']['total']} ({t['defects_counted']['S1']} S1, {t['defects_counted']['crash']} crash) + pending {t['defects_counted_ub_pending']['total']}"
              f"  translations {t['translations_with_counted_defect']}|{t['translations_with_counted_or_pending_defect']}/{(t['translations_tested'] or {}).get('n')}"
              f"  acc {t['acceptance_of_counted_and_pending']}  own-only {t['found_only_on_own_inputs']}  not counted {t['not_counted']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", nargs="?", default="build", choices=["build"])
    ap.add_argument("--probe", default="results/ledger_probe_c2saferrust.json", help="JSON from the `probe` mode (C2SaferRust); shipped in results/")
    ap.add_argument("--probe-bzip2", default="results/ledger_probe_rustmap_bzip2.json", help="JSON from the `probe-bzip2` mode (RustMap bzip2); shipped in results/")
    ap.add_argument("--out", default=R("results/defect_ledger.json"))
    ap.add_argument("--extra-summary-dir", action="append", default=[], help="also look here for <system>_summary.json (testing)")
    a = ap.parse_args()
    EXTRA_SUMMARY_DIRS.extend(a.extra_summary_dir)
    build(a.probe, a.out, a.probe_bzip2)
