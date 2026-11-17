"""Mutation-test the HARNESS itself: seed known-wrong translations and confirm the
held-out differential harness catches them. A validator that never goes red is an
assertion, not a measurement.

Two levels:
  (A) SANITY mutant -- a guaranteed-divergent change: inject stray stdout bytes at
      the entry of the translated program (main_0). The oracle never emits them, so
      an assessable harness MUST report a mismatch. If it stays green, the harness
      is broken.
  (B) ADEQUACY mutants -- Offutt-style relational-operator swaps (ROR) applied one at
      a time to the translation's primary source. For each that still compiles, run
      the held-out harness and record kill/survive. Survivors are flagged as
      *possible equivalent mutants or generator-unreached* -- documented, not hidden.

Mutation is applied IN PLACE to the fetched artifact's single source file and ALWAYS
restored in a finally block; the artifact is gitignored and re-fetchable.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

import gen_inputs
from common import util_rust_dir
from build import build_oracle, build_sut
from difftest import run as diff_run

ROR = {
    " == ": " != ",
    " != ": " == ",
    " <= ": " > ",
    " >= ": " < ",
    " < ": " >= ",
    " > ": " <= ",
}
# longest-first so " <= " is matched before " < "
ROR_KEYS = sorted(ROR, key=len, reverse=True)


def primary_src(util):
    return os.path.join(util_rust_dir(util), "src", f"{util}.rs")


def _cargo_build(util):
    rdir = util_rust_dir(util)
    p = subprocess.run(["cargo", "build", "--bin", util], cwd=rdir,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    binpath = os.path.join(rdir, "target", "debug", util)
    return (p.returncode == 0 and os.path.isfile(binpath)), binpath, p.stdout


def inject_sanity(src: str):
    """Inject a stray stdout write at the top of main_0's body."""
    m = re.search(r"fn\s+main_0\s*\([^;{]*?\)\s*->\s*libc::c_int\s*\{", src, re.S)
    if not m:
        return None
    pos = m.end()
    stray = ('\n    {{ use std::io::Write; let mut _o = std::io::stdout(); '
             'let _ = _o.write_all(b"\\x00MUTANT"); let _ = _o.flush(); }}\n')
    return src[:pos] + stray + src[pos:]


def find_ror_sites(src: str, cap: int):
    """Return list of (index, old_op, new_op), sampled evenly across the file."""
    sites = []
    for key in ROR_KEYS:
        start = 0
        while True:
            i = src.find(key, start)
            if i == -1:
                break
            # skip "->" false positives for " > "
            if key == " > " and i >= 1 and src[i - 1] == "-":
                start = i + 1
                continue
            sites.append((i, key, ROR[key]))
            start = i + len(key)
    sites.sort()
    if len(sites) <= cap:
        return sites
    step = len(sites) / cap
    return [sites[int(k * step)] for k in range(cap)]


def apply_at(src: str, index: int, old: str, new: str):
    return src[:index] + new + src[index + len(old):]


def with_mutation(util, mutate_fn, cases, oracle_bin):
    """Apply mutate_fn(src)->src' in place, build, difftest, restore. Returns dict."""
    path = primary_src(util)
    with open(path) as f:
        orig = f.read()
    mutated = mutate_fn(orig)
    if mutated is None or mutated == orig:
        return {"status": "no-op", "killed": False}
    try:
        with open(path, "w") as f:
            f.write(mutated)
        ok, binpath, log = _cargo_build(util)
        if not ok:
            return {"status": "noncompile", "killed": False,
                    "log_tail": "\n".join(log.splitlines()[-6:])}
        # Short timeout + early-stop: a mutant that merely hangs (e.g. an
        # operator swap that makes a loop non-terminating) can't stall the run,
        # and one mismatch is enough to record a kill.
        # Kill on a stdout/exit divergence. We deliberately do NOT credit stderr
        # kills here: the baseline (unmutated) translation already diverges on
        # stderr for error-path cases (the duplication defect), so an stderr
        # divergence can't be attributed to the mutation. stdout/exit is the
        # channel where the baseline is 100% equivalent, so a kill there is caused
        # by the mutation -- a clean measure of the generator's fault-detection power.
        summary, _ = diff_run(util, cases, oracle_bin, binpath,
                              timeout=5, stop_on_mismatch=True)
        killed = (summary["mismatches"] or 0) > 0
        return {"status": "compiled", "killed": killed,
                "mismatches": summary["mismatches"], "assessable": summary["assessable"]}
    finally:
        with open(path, "w") as f:
            f.write(orig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--util", required=True)
    ap.add_argument("-n", type=int, default=60, help="held-out cases per mutant")
    ap.add_argument("--seed", type=int, default=777)
    ap.add_argument("--max-mutants", type=int, default=8)
    ap.add_argument("--out")
    args = ap.parse_args()
    util = args.util

    ob = build_oracle(util)
    if not ob["ok"]:
        print(json.dumps({"error": "oracle build failed", "oracle": ob}, indent=2))
        sys.exit(2)
    oracle_bin = ob["bin"]
    cases = gen_inputs.generate(util, args.n, args.seed)

    # A kill must be caused by the mutation. Keep only cases on which the UNMUTATED SUT already
    # agrees with the C on stdout/exit; otherwise a translation that is wrong on some inputs
    # (e.g. C2SaferRust's rust_WIP) "kills" every mutant trivially (audit 4).
    ok, base_bin, _ = _cargo_build(util)
    if not ok:
        print(json.dumps({"error": "unmutated SUT build failed"}, indent=2))
        sys.exit(2)
    _, base_verdicts = diff_run(util, cases, oracle_bin, base_bin, timeout=5)
    n_generated = len(cases)
    cases = [c for c, v in zip(cases, base_verdicts) if not v["mismatch"]]

    report = {"util": util, "n_cases": args.n, "seed": args.seed,
              "n_generated": n_generated, "n_cases_unmutated_agrees": len(cases)}

    # (A) sanity mutant -- MUST be killed
    sanity = with_mutation(util, inject_sanity, cases, oracle_bin)
    report["sanity_mutant"] = sanity
    report["sanity_ok"] = bool(sanity.get("killed"))

    # (B) ROR adequacy mutants
    with open(primary_src(util)) as f:
        src = f.read()
    sites = find_ror_sites(src, args.max_mutants)
    muts = []
    for k, (idx, old, new) in enumerate(sites):
        res = with_mutation(
            util,
            (lambda s, i=idx, o=old, n=new: apply_at(s, i, o, n)),
            cases, oracle_bin)
        res.update({"site": k, "op": f"{old.strip()}->{new.strip()}", "at": idx})
        muts.append(res)
    report["ror_mutants"] = muts
    compiled = [m for m in muts if m["status"] == "compiled"]
    report["ror_summary"] = {
        "total": len(muts),
        "compiled": len(compiled),
        "noncompile": sum(1 for m in muts if m["status"] == "noncompile"),
        "killed": sum(1 for m in compiled if m["killed"]),
        "survived": sum(1 for m in compiled if not m["killed"]),
    }

    # restore a clean SUT build for downstream use
    _cargo_build(util)

    if args.out:
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w") as f:
            json.dump(report, f, indent=2)
    print(json.dumps({k: report[k] for k in
                      ("util", "sanity_ok", "sanity_mutant", "ror_summary")}, indent=2))

    # Exit 3 if the harness could NOT be shown to go red (sanity mutant survived).
    sys.exit(0 if report["sanity_ok"] else 3)


if __name__ == "__main__":
    main()
