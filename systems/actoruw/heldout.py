"""Held-out measurement for the ACTOR re-grade.

The system's own check for a TRACTOR executable is the benchmark's test vectors, run by MIT's
`runtests` (ACTOR tools/src/oracle/runtests.rs, --verbose). The vectors themselves live in the
`test-corpus` submodule (github.com/UW-HARVEST/Test-Corpus, pinned 6fefd73), which is NOT public
(git ls-remote: "Repository not found"). The released results do record, per battery, the verbose
runner log `<battery>/test.log`: for every executable vector the full argv
("[test] Testing ['.../driver', args...]:<vector>") and a stdin preview ("[run] stdin preview:"
followed by a Python bytes literal, cut at ~1000 bytes with "... [truncated]"). Library vectors are
JSON files run by a generated runner ("[test] Running [runner, 'lib', '-c', 'testNN.json']"); their
contents are not in the log, so for libraries the system's inputs are not recorded.

This script
  1. parses every recorded executable vector (argv, stdin, truncated?) per case;
  2. measures the overlap of our generated inputs with them (exact match; a truncated record
     counts as a POSSIBLE match when our stdin starts with the recorded prefix);
  3. replays every non-truncated recorded vector through our comparison (C oracle vs ACTOR's Rust,
     same channels as difftest.py), so the system's own inputs are also graded by our oracle.

Usage (in container): python3 -I heldout.py   -> /w/out/heldout.json
"""
import ast
import base64
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import difftest  # noqa: E402
import gen_inputs  # noqa: E402

TRUNC = b"... [truncated]\n"


def parse_log(path):
    """{case: [{"vector", "argv", "stdin" (bytes|None), "truncated"}]} for executable vectors, and
    {case: n} for library vectors (contents not recorded).

    runtests runs cases in parallel, so log lines of different cases can share a physical line.
    We first break every physical line at each "[test] " / "[run] " marker, then walk the events:
    "[test] Testing [argv]:vec" queues a vector for its case (case = directory above
    translated_rust/ in argv[0]); "[run] <cmd> (cwd = ...)" names the case being run; the
    following "[run] stdin preview:" + literal (or "[run] stdin: <None>") is assigned to the oldest
    queued vector of that case that has no stdin yet. Vectors left without stdin are reported."""
    exe, lib = {}, {}
    raw = open(path, encoding="utf-8", errors="replace").read()
    raw = re.sub(r"(?<!^)(?<!\n)(\[(?:test|run)\] )", r"\n\1", raw)
    lines = raw.splitlines()
    pending = {}
    cur_case = None
    i = 0
    case_of = lambda p: (re.search(r"/([^/]+)/translated_rust/", p) or re.search(r"/([^/]+)/?$", p)).group(1)
    while i < len(lines):
        ln = lines[i]
        m = re.match(r"\[test\] Testing (\[.*?\]):(\S+)\s*$", ln)
        if m:
            argv = ast.literal_eval(m.group(1))
            case = case_of(argv[0])
            rec = {"vector": m.group(2), "argv": [str(x) for x in argv[1:]], "stdin": None, "truncated": False,
                   "case": case}
            exe.setdefault(case, []).append(rec)
            pending.setdefault(case, []).append(rec)
            i += 1
            continue
        m = re.match(r"\[test\] Running \[(.*?)\]:(\S+)", ln)
        if m:
            mm = re.search(r"release/_?(\S+?)_runner'", m.group(1))
            case = mm.group(1) if mm else "?"
            lib[case] = lib.get(case, 0) + 1
            i += 1
            continue
        m = re.match(r"\[run\] (/\S+/translated_rust/target/release/\S+)", ln)
        if m:
            cur_case = case_of(m.group(1))
            i += 1
            continue
        if ln.startswith("[run] stdin preview:") or ln.startswith("[run] stdin: <None>"):
            if ln.startswith("[run] stdin: <None>"):
                b, step = b"", 1
            else:
                lit = lines[i + 1] if i + 1 < len(lines) else "b''"
                try:
                    b = ast.literal_eval(lit)
                except (ValueError, SyntaxError):
                    b = None
                step = 2
            q = pending.get(cur_case) or []
            if q and isinstance(b, bytes):
                rec = q.pop(0)
                if b.endswith(TRUNC):
                    rec["stdin"], rec["truncated"] = b[: -len(TRUNC)], True
                else:
                    rec["stdin"] = b
            i += step
            continue
        i += 1
    return exe, lib


def main():
    dt = json.load(open(os.path.join(common.W, "out", "difftest.json")))
    bj = json.load(open(os.path.join(common.W, "out", "build.json")))
    builds = {(r["battery"], r["case"]): r for r in bj["cases"]}
    recorded, libvec = {}, {}
    for b in common.BATTERIES:
        p = os.path.join(common.KIRO, b, "test.log")
        if os.path.isfile(p):
            e, l = parse_log(p)
            for c, v in e.items():
                recorded[(b, c)] = v
            for c, n in l.items():
                libvec[(b, c)] = n
    out = {"source": "results/Test-Corpus/kiro/<battery>/test.log (runtests --verbose)", "programs": [],
           "library_vectors_recorded_without_contents": {f"{b}/{c}": n for (b, c), n in sorted(libvec.items())}}
    for p in dt["programs"]:
        key = (p["battery"], p["case"])
        rec = recorded.get(key, [])
        ph, src = common.phase_dir(*key)
        _, ours = gen_inputs.generate(os.path.join(src, "c_src"))
        exact_set = {(tuple(r["argv"]), r["stdin"]) for r in rec if not r["truncated"] and r["stdin"] is not None}
        trunc = [r for r in rec if r["truncated"]]
        exact, possible = [], []
        for x in ours:
            s = base64.b64decode(x["stdin_b64"])
            if x["files"] or x["dirs"]:
                continue
            if (tuple(x["argv"]), s) in exact_set:
                exact.append(x["id"])
            elif any(tuple(r["argv"]) == tuple(x["argv"]) and s.startswith(r["stdin"]) and len(s) > len(r["stdin"])
                     for r in trunc):
                possible.append(x["id"])
        # replay the system's own (non-truncated) inputs through our comparison
        cbin = os.path.join(common.W, "build", key[0], key[1], "c", "driver")
        rbin = os.path.join(common.W, builds[key]["rust_bin"])
        replay = []
        for r in rec:
            if r["truncated"] or r["stdin"] is None:
                continue
            x = gen_inputs.case(r["argv"], r["stdin"], note=f"system:{r['vector']}")
            cr = difftest.run_one(cbin, x)
            rr = difftest.run_one(rbin, x)
            row = {"vector": r["vector"], "input": x, "agree": difftest.same(cr, rr)}
            if not row["agree"]:
                row["class"] = difftest.classify(cr, rr)
                row["c"] = difftest.enc(cr)
                row["rust"] = difftest.enc(rr)
                row["c_reproducible"] = difftest.same(cr, difftest.run_one(cbin, x))
                row["channels"] = [ch for ch in ("stdout", "rc", "stderr", "timeout") if cr[ch] != rr[ch]]
            replay.append(row)
        out["programs"].append({
            "battery": key[0], "case": key[1],
            "recorded_vectors": len(rec), "recorded_truncated": len(trunc),
            "recorded_unique_inputs": len(exact_set),
            "our_inputs": len(ours), "our_inputs_exactly_recorded": len(exact),
            "our_inputs_possibly_recorded_truncated": len(possible),
            "exact_ids": exact, "possible_ids": possible,
            "replay_n": len(replay), "replay_divergent": [r for r in replay if not r["agree"]],
        })
        print(f"{key[0]}/{key[1]}: recorded={len(rec)} (trunc {len(trunc)}) ours={len(ours)} "
              f"exact={len(exact)} possible={len(possible)} replay_div={sum(1 for r in replay if not r['agree'])}/{len(replay)}",
              flush=True)
    common.dump(os.path.join(common.W, "out", "heldout.json"), out)


if __name__ == "__main__":
    main()
