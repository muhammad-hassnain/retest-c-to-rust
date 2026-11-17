"""Held-out input generator for coreutils C-vs-Rust differential testing.

SYSTEMATIC + DETERMINISTIC (v3): no randomness. For each utility we take the cartesian product of
  * a structured set of FLAG combinations (empty, every single flag, and fixed pairs),
  * a fixed STDIN corpus (empty, text, binary, multibyte, NUL-delimited, larger), and
  * fixed INPUT-SOURCE scenarios (stdin, a named file, a nonexistent file -> error path,
    a directory -> error path, two files),
capped deterministically. The same utility always yields the same test set. These inputs are
"held-out": drawn from this fixed grid, independent of the hand-written coreutils test scripts the
C2SaferRust repair loop used. (Earlier versions sampled randomly with a seed; this enumerates.)
"""
import argparse
import itertools
import json
import sys

from common import Case, b64

# ---------------------------------------------------------------- fixed corpora

def _text():
    return b"alpha\nbeta\n\ngamma\n  spaced \ndelta"

def _text_nl():
    return b"one\ntwo\nthree\n"

def _binary():
    return bytes(range(0, 256, 7)) + b"\x00\x00\xff\xfe\n\x01"

def _multibyte():
    return "café\nMoscowМосква\n日本語\n😀\nstraße\n".encode("utf-8")

def _runs():
    return b"x\nx\nx\ny\ny\n\n\nz\n"

def _big():
    return ("\n".join(f"line{i}" for i in range(500)) + "\n").encode()

# v4 edge-case corpus: these reach EOF / buffer-refill / line-counting branches that
# the gentler corpus never exercised -- the code regions where relational-operator
# faults (the ROR mutants) actually bite, so they lift the measured kill rate.
def _no_nl():
    return b"alpha\nbeta\nlast line without newline"      # missing final \n

def _long_line():
    return b"A" * 9000 + b"\n" + b"tail\n"                # > stdio buffer, forces refill

def _allblank():
    return b"\n\n\n\n\n"                                   # squeeze / blank-run edges

def _cr():
    return b"a\rb\rc\r"                                    # CR-only line endings

def _spaces():
    return b"   \n\t\n \t \n   spaced\n"                   # leading-space / field edges

def _one_char():
    return b"x"                                            # single byte, no newline

STDIN_CORPUS = [b"", _text(), _text_nl(), _binary(), _multibyte(), _runs(), _big(),
                _no_nl(), _long_line(), _allblank(), _cr(), _spaces(), _one_char()]

# input-source scenarios: (argv-fragment, files, make_dirs, note)
def SCENARIOS(body):
    return [
        ([], {}, [], "stdin"),                                        # read stdin
        (["in1.txt"], {"in1.txt": b64(body)}, [], "file"),            # one named file
        (["nope_does_not_exist.txt"], {}, [], "missing-file"),        # error path
        (["adir"], {}, ["adir"], "is-a-directory"),                   # error path
        (["in1.txt", "in2.txt"], {"in1.txt": b64(body), "in2.txt": b64(_text_nl())}, [], "two-files"),
        (["in1.txt", "missing.txt"], {"in1.txt": b64(body)}, [], "ok+missing"),
    ]

# ------------------------------------------------- per-util flag combinations

def flag_combos(flags, numeric=None, triples=None):
    """empty + each single flag + ALL pairs (true pairwise coverage) (+ numeric-arg flags
    + a fixed set of hand-chosen triples for 3-way interactions)."""
    combos = [[]]
    combos += [[f] for f in flags]
    combos += [list(p) for p in itertools.combinations(flags, 2)]   # all pairs
    if numeric:
        combos += [[nf, nv] for (nf, nv) in numeric]
    if triples:
        combos += [list(t) for t in triples]
    return combos

CAT = ["-n", "-A", "-E", "-T", "-s", "-b", "-v", "-e", "-t"]
HEAD_NUM = [("-n", "0"), ("-n", "1"), ("-n", "2"), ("-n", "3"), ("-n", "7"), ("-n", "-1"),
            ("-n", "-2"), ("-n", "1000000"), ("-c", "0"), ("-c", "1"), ("-c", "5"),
            ("-c", "-3"), ("-c", "+4"), ("-c", "100")]
TAIL_NUM = [("-n", "0"), ("-n", "1"), ("-n", "3"), ("-n", "7"), ("-n", "-1"), ("-n", "+1"),
            ("-n", "+2"), ("-c", "0"), ("-c", "1"), ("-c", "5"), ("-c", "-1"), ("-c", "+4"),
            ("-n", "1000000")]
UNIQ = ["-c", "-d", "-u", "-i", "-z", "-D"]
UNIQ_NUM = [("-f", "0"), ("-f", "1"), ("-s", "0"), ("-s", "2"), ("-w", "0"), ("-w", "1"), ("-w", "3")]

# A few semantically interesting flag TRIPLES (pairs alone miss 3-way interactions in
# the numbering/squeeze and dedup/skip logic where several ROR sites live).
CAT_TRIPLES = [["-s", "-n", "-A"], ["-b", "-E", "-T"], ["-n", "-v", "-s"], ["-A", "-b", "-e"]]
UNIQ_TRIPLES = [["-c", "-d", "-i"], ["-u", "-w", "3"], ["-c", "-f", "1"], ["-D", "-i", "-c"]]


# Locale axis: a UTF-8 locale changes how coreutils classify/print bytes (cat -v/-A/-t,
# uniq -i case folding). Ubuntu 22.04 ships C.UTF-8 built in. Applied SYMMETRICALLY to
# oracle and SUT (per-case env), so any divergence is a real one, not a locale confound.
def build(util, flag_sets, cap, locales=None):
    cases = []
    i = 0
    locales = locales or [None]
    # iterate flags (outer), corpus+scenario (inner); cap deterministically
    for flags in flag_sets:
        for body in STDIN_CORPUS:
            for frag, files, dirs, note in SCENARIOS(body):
                stdin = body if note in ("stdin",) else _text_nl()
                for loc in locales:
                    env = {"LC_ALL": loc, "LANG": loc} if loc else {}
                    suffix = "" if not loc else f"-{loc}"
                    # v5: `uniq IN OUT` -- uniq's second operand is its OUTPUT file, so
                    # those cases must run oracle/SUT in separate dirs and diff the tree.
                    wf = util == "uniq" and len(frag) == 2
                    cases.append(Case(id=f"{util}-{i:05d}{suffix}", util=util,
                                      argv=list(flags) + frag, stdin_b64=b64(stdin),
                                      files=files, make_dirs=dirs, env=env, note=note,
                                      writes_files=wf))
                    i += 1
                    if len(cases) >= cap:
                        return cases
    return cases


def gen_cat(cap):
    return build("cat", flag_combos(CAT, triples=CAT_TRIPLES), cap, locales=[None, "C.UTF-8"])

def gen_head(cap):
    return build("head", flag_combos(["-q", "-z"], numeric=HEAD_NUM), cap)

def gen_tail(cap):
    return build("tail", flag_combos(["-z"], numeric=TAIL_NUM), cap)

def gen_uniq(cap):
    return build("uniq", flag_combos(UNIQ, numeric=UNIQ_NUM, triples=UNIQ_TRIPLES), cap,
                 locales=[None, "C.UTF-8"])

def gen_pwd(cap):
    return [Case(id=f"pwd-{i:04d}", util="pwd", argv=a, stdin_b64=b64(b""))
            for i, a in enumerate([[], ["-L"], ["-P"]])]

# ---------------------------------------------- file-WRITING utils (output = files)
# These leave files behind, so difftest runs oracle & SUT in SEPARATE dirs and diffs
# the resulting file TREE (writes_files=True). Same deterministic grid discipline.

SPLIT_FLAGS = [
    ["-l", "2"], ["-l", "1"], ["-l", "1000"],
    ["-b", "4"], ["-b", "10"], ["-b", "1000"],
    ["-C", "8"],
    ["-n", "3"], ["-n", "1"], ["-n", "l/2"], ["-n", "r/2"],
    ["-a", "1"], ["-a", "3"],
    ["-d"], ["-x"],
    ["--additional-suffix=.txt"],
    ["-l", "2", "-d"], ["-b", "5", "-a", "1"],
    ["-l", "3", "--additional-suffix=.log"], ["-n", "2", "-d"],
]
SPLIT_CORPUS = [b"", _text(), _text_nl(), _binary(), _runs(), _big()]

def gen_split(cap):
    cases, i = [], 0
    for flags in SPLIT_FLAGS:
        for body in SPLIT_CORPUS:
            for frag, files, note in (
                (["-"], {}, "stdin"),                                   # read stdin, prefix 'x'
                (["in.txt"], {"in.txt": b64(body)}, "file"),            # named file, prefix 'x'
                (["in.txt", "pre_"], {"in.txt": b64(body)}, "file+prefix"),
            ):
                sin = body if note == "stdin" else b""
                cases.append(Case(id=f"split-{i:05d}", util="split", argv=list(flags) + frag,
                                  stdin_b64=b64(sin), files=files, writes_files=True, note=note))
                i += 1
                if len(cases) >= cap:
                    return cases
    return cases

TRUNC_SIZES = ["0", "1", "5", "100", "+10", "-3", "%4", "/4", ">8", "<8", "1000000", "+0"]
TRUNC_CORPUS = [b"", _text(), _binary(), b"0123456789"]

def gen_truncate(cap):
    cases, i = [], 0
    for size in TRUNC_SIZES:
        for body in TRUNC_CORPUS:
            cases.append(Case(id=f"truncate-{i:05d}", util="truncate", argv=["-s", size, "target.bin"],
                              stdin_b64=b64(b""), files={"target.bin": b64(body)},
                              writes_files=True, note="existing"))
            i += 1
            if len(cases) >= cap:
                return cases
        for frag, files, note in (
            (["-s", size, "newfile.bin"], {}, "create-missing"),            # creates zero-filled
            (["-c", "-s", size, "nope.bin"], {}, "no-create-missing"),      # must NOT create/err
        ):
            cases.append(Case(id=f"truncate-{i:05d}", util="truncate", argv=frag,
                              stdin_b64=b64(b""), files=files, writes_files=True, note=note))
            i += 1
            if len(cases) >= cap:
                return cases
    for body in TRUNC_CORPUS:                                               # reference-file mode
        for frag, note in ((["-r", "ref.bin", "target.bin"], "reference"),
                           (["-r", "ref.bin", "-s", "+4", "target.bin"], "reference+rel")):
            cases.append(Case(id=f"truncate-{i:05d}", util="truncate", argv=frag,
                              stdin_b64=b64(b""),
                              files={"ref.bin": b64(body), "target.bin": b64(_text())},
                              writes_files=True, note=note))
            i += 1
            if len(cases) >= cap:
                return cases
    return cases

GENERATORS = {"cat": gen_cat, "head": gen_head, "tail": gen_tail, "uniq": gen_uniq,
              "pwd": gen_pwd, "split": gen_split, "truncate": gen_truncate}


def generate(util, n, seed=None):
    """Deterministic: n is the cap on the enumerated grid (seed is ignored, kept for CLI compat)."""
    return GENERATORS[util](n)


def main():
    ap = argparse.ArgumentParser(description="Generate systematic (deterministic) coreutils cases.")
    ap.add_argument("--util", required=True, choices=sorted(GENERATORS))
    ap.add_argument("-n", type=int, default=600, help="cap on the enumerated grid")
    ap.add_argument("--seed", type=int, default=0, help="ignored (generation is deterministic)")
    ap.add_argument("-o", "--out", default="-")
    args = ap.parse_args()
    cases = [c.to_json() for c in generate(args.util, args.n)]
    payload = json.dumps({"util": args.util, "deterministic": True, "cases": cases}, indent=0)
    if args.out == "-":
        sys.stdout.write(payload)
    else:
        with open(args.out, "w") as f:
            f.write(payload)
        print(f"[gen] wrote {len(cases)} systematic cases -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
