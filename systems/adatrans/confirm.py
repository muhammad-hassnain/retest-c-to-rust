"""Confirm each defect in the translated source by a one-mechanism patch.

For every defect in systems/adatrans/defects_<run>.json we apply ONLY the listed source edit(s) of
that mechanism to a copy of the translation (exact string replacement, each `old` must occur exactly
once), rebuild exactly as run_diff.py does, and re-run every input on which the original translation
diverged (counted or not). The defect is confirmed when the patched translation agrees with the C on
all of the defect's counted cases; cases attributed to another defect of the same translation are
reported separately.

  python3 -I systems/adatrans/confirm.py --run run1 --defects systems/adatrans/defects_run1.json \
      --cases /b/cases_run1_1.86.0_ours.jsonl /b/cases_run1_1.86.0_system.jsonl
Writes <build>/confirm_<run>.json.
"""
import argparse
import json
import multiprocessing as mp
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_diff as rd  # noqa: E402


def load_inputs(inst, inputs_dir, fuzz_dir):
    """(src, idx) -> stdin bytes, for our inputs and the artifact's own."""
    m = {}
    d = os.path.join(inputs_dir, inst)
    for f in sorted(os.listdir(d)):
        m[("ours", f)] = open(os.path.join(d, f), "rb").read()
    c, p = inst[2:].split("_")
    fd = os.path.join(fuzz_dir, f"weekly_contest_{c}_p{p}")
    for kind, fname in (("examples", "examples"), ("fuzz", "outputs")):
        for j, cs in enumerate(rd.parse_cases(os.path.join(fd, fname))):
            m[(kind, j + 1)] = cs["input"].encode()
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="run1")
    ap.add_argument("--defects", required=True)
    ap.add_argument("--cases", nargs="+", required=True)
    ap.add_argument("--build", default="/b")
    ap.add_argument("--inputs", default="/inputs")
    ap.add_argument("--fuzz", default="/fuzz")
    ap.add_argument("--jobs", type=int, default=1)
    a = ap.parse_args()
    defects = json.load(open(a.defects))["defects"]
    div = {}
    for cf in a.cases:
        for line in open(cf):
            r = json.loads(line)
            if r["run"] == a.run and r["kind"] not in ("agree", "c_timeout", "rust_build_fail"):
                div.setdefault(r["inst"], []).append(r)
    ws = os.path.join(a.build, f"confirm_ws_{a.run}")
    if os.path.isdir(ws):
        shutil.rmtree(ws)
    os.makedirs(ws)
    members = []
    for d in defects:
        src = open(os.path.join(a.build, "extracted", a.run, d["inst"] + ".rs")).read()
        for old, new in d["patch"]:
            assert src.count(old) == 1, (d["id"], old, src.count(old))
            src = src.replace(old, new)
        name = ("p_" + d["id"]).lower().replace("-", "_")
        pd = os.path.join(ws, name)
        os.makedirs(os.path.join(pd, "src"))
        open(os.path.join(pd, "src", "main.rs"), "w").write(src)
        open(os.path.join(pd, "Cargo.toml"), "w").write(rd.CARGO_TOML.format(name=name))
        members.append(name)
    open(os.path.join(ws, "Cargo.toml"), "w").write(
        "[workspace]\nresolver = \"2\"\nmembers = [\n" + "".join(f'  "{m}",\n' for m in members) + "]\n")
    shutil.copy(os.path.join(a.build, "vendor_tpl", "Cargo.lock"), os.path.join(ws, "Cargo.lock"))
    pr = subprocess.run(["cargo", f"+{rd.RUST_TC}", "build", "--offline", "--keep-going", "-j", str(a.jobs),
                         "--message-format=short"], cwd=ws, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    jobs = []
    out = {}
    for d, name in zip(defects, members):
        b = os.path.join(ws, "target", "debug", name)
        if not os.path.isfile(b):
            out[d["id"]] = {"built": False, "build_log": pr.stdout[-1500:]}
            continue
        jobs.append((d, b, div.get(d["inst"], []), a))
    with mp.Pool(a.jobs) as pool:
        for did, res in pool.imap_unordered(_one, jobs):
            out[did] = res
            print(did, json.dumps({k: v for k, v in res.items() if k != "still_diverge_detail"}), flush=True)
    json.dump(out, open(os.path.join(a.build, f"confirm_{a.run}.json"), "w"), indent=1, sort_keys=True)


def _one(job):
    """Re-run every divergent input of the translation on the patched build with a 10 s CPU limit
    (conservative: a patched run that needs more than 10 s CPU counts as still diverging)."""
    d, b, rows, a = job
    inp = load_inputs(d["inst"], a.inputs, a.fuzz)
    cdir = os.path.join(a.build, "c", d["inst"])
    tally = {"built": True, "rerun": 0, "now_agree": 0, "still_diverge": 0, "now_agree_by_orig_kind": {},
             "still_by_orig_kind": {}, "still_diverge_detail": []}
    for r in rows:
        stdin = inp[(r["src"], r["idx"])]
        cr = rd.run1(os.path.join(cdir, "ref"), stdin, rd.C_TIMEOUT)
        pr2 = rd.run1(b, stdin, rd.R_TIMEOUT)
        kind, ch = rd.classify(cr, pr2)
        tag = f'{r["kind"]}{"" if r.get("c_ub_clean") else "(C-UB)"}'
        tally["rerun"] += 1
        if kind in ("agree", "whitespace_only") and r["kind"] != "whitespace_only":
            tally["now_agree"] += 1
            tally["now_agree_by_orig_kind"][tag] = tally["now_agree_by_orig_kind"].get(tag, 0) + 1
        elif kind != "agree" and r["kind"] != "whitespace_only":
            tally["still_diverge"] += 1
            tally["still_by_orig_kind"][tag] = tally["still_by_orig_kind"].get(tag, 0) + 1
            if len(tally["still_diverge_detail"]) < 10:
                tally["still_diverge_detail"].append({"src": r["src"], "idx": r["idx"], "orig_kind": r["kind"],
                                                      "patched_kind": kind, "c_ub_clean": r.get("c_ub_clean")})
    return d["id"], tally


if __name__ == "__main__":
    main()
