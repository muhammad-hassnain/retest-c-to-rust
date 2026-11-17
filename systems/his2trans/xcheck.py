"""Toolchain / profile cross-check for the His2Trans Rust side (S3 = differs only under one toolchain
or build profile).

`dump`: build every crate (+ driver) with the rustc in this container and the given cargo profile,
run every case, and store per-case (stdout sha256, exit code, stderr sha256, Rust panic message).
`compare`: compare two dumps case by case.

The artifact pins only "Rust nightly" (README), no date. The main run uses rustc 1.86.0 (whose
warning counts reproduce the shipped results/warnings.json exactly); this check re-runs the same
Rust side under rustc 1.99.1 (the newest toolchain available offline here) and under the debug
profile (`cargo test`, the system's own check, builds the debug profile: overflow checks on).
"""
import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run as R  # noqa: E402
import gen_inputs  # noqa: E402


def dump(args):
    out = {"rustc": subprocess.run(["rustc", "--version"], stdout=subprocess.PIPE, text=True).stdout.strip(),
           "profile": args.profile, "cases": {}, "build": {}}
    for p in args.projects.split(","):
        dest = os.path.join(args.build, f"{p}_{args.profile}")
        R.prepare_crate(p, dest)
        cmd = ["cargo", "build", "--frozen", "-j", "3"] + (["--release"] if args.profile == "release" else [])
        rc, log = R.sh(cmd, cwd=dest)
        b = os.path.join(dest, "target", "release" if args.profile == "release" else "debug", p)
        out["build"][p] = {"rc": rc, "warnings": log.count("\nwarning"), "tail": log[-800:]}
        if rc != 0:
            continue
        cases = gen_inputs.GENERATORS[p]()

        def one(c):
            r = R.run_bin(b, ("\n".join(c["lines"]) + "\n").encode(), False)
            pan = [l for l in r["stderr"].decode("latin1").splitlines() if "panicked" in l or "overflow" in l]
            return c["id"], {"out": hashlib.sha256(r["stdout"]).hexdigest()[:16], "rc": r["rc"],
                             "err": hashlib.sha256(r["stderr"]).hexdigest()[:16], "panic": pan[:2],
                             "timeout": r["timed_out"]}
        with cf.ThreadPoolExecutor(max_workers=3) as ex:
            out["cases"].update(dict(ex.map(one, cases)))
        print(p, out["build"][p]["rc"], flush=True)
    out["provenance"] = R.ou.provenance(__file__)
    R.ou.dump(args.out, out)


def compare(args):
    a, b = json.load(open(args.a)), json.load(open(args.b))
    diff = []
    for k, va in a["cases"].items():
        vb = b["cases"].get(k)
        if vb is None or (va["out"], va["rc"], va["err"]) != (vb["out"], vb["rc"], vb["err"]):
            diff.append({"id": k, "a": va, "b": vb})
    res = {"a": {"rustc": a["rustc"], "profile": a["profile"], "build": {p: v["rc"] for p, v in a["build"].items()}},
           "b": {"rustc": b["rustc"], "profile": b["profile"], "build": {p: v["rc"] for p, v in b["build"].items()}},
           "cases_compared": len(a["cases"]), "cases_differing": len(diff), "differing": diff[:200]}
    print(json.dumps({k: v for k, v in res.items() if k != "differing"}, indent=1))
    if args.out:
        R.ou.dump(args.out, res)


def combine(args):
    """results/his2trans_xcheck.json: the base dump compared with each other dump (host-side, JSON only)."""
    base = json.load(open(args.base))
    res = {"base": {"rustc": base["rustc"], "profile": base["profile"], "file": os.path.basename(args.base)},
           "comparisons": []}
    for o in args.others:
        b = json.load(open(o))
        diff = []
        for k, va in base["cases"].items():
            vb = b["cases"].get(k)
            if vb is None or (va["out"], va["rc"], va["err"]) != (vb["out"], vb["rc"], vb["err"]):
                diff.append({"id": k, "base": va, "other": vb})
        res["comparisons"].append({
            "other": {"rustc": b["rustc"], "profile": b["profile"], "file": os.path.basename(o),
                      "build_rc": {p: v["rc"] for p, v in b["build"].items()},
                      "rustc_warning_lines": {p: v["warnings"] for p, v in b["build"].items()}},
            "cases_compared": len(base["cases"]), "cases_differing": len(diff),
            "differing_by_project": dict(__import__("collections").Counter(d["id"].split("/")[0] for d in diff)),
            "panics_in_other": sum(1 for v in b["cases"].values() if v["panic"]),
            "differing": diff[:300]})
    res["provenance"] = {"script": "systems/his2trans/xcheck.py", "command": " ".join(sys.argv),
                         "inputs_provenance": {os.path.basename(f): json.load(open(f)).get("provenance")
                                               for f in [args.base] + args.others},
                         "date": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds")}
    R.ou.dump(args.out, res)
    print(json.dumps([{k: v for k, v in c.items() if k != "differing"} for c in res["comparisons"]], indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump")
    d.add_argument("--projects", default="qsort,ht,rgba,genann,buffer,quadtree,urlparser,avl")
    d.add_argument("--profile", default="release", choices=["release", "debug"])
    d.add_argument("--build", default="/b/xcheck")
    d.add_argument("--out", required=True)
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    c.add_argument("--out")
    m = sub.add_parser("combine")
    m.add_argument("base")
    m.add_argument("others", nargs="+")
    m.add_argument("--out", required=True)
    args = ap.parse_args()
    {"dump": dump, "compare": compare, "combine": combine}[args.cmd](args)
