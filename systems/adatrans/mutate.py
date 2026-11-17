"""Harness power for the AdaTrans re-grade: mutation analysis on agreeing translations.

For every translation that agrees with the C on ALL of our inputs (strict comparison), we seed one
fault at a time into its Rust source, rebuild exactly as run_diff.py does (cargo debug, same vendored
crates, rustc 1.86.0), and re-run our inputs. A mutant is KILLED if the harness reports a divergence
on an input where the C passes the UB rule (run_diff.ub_check); it SURVIVES if it agrees on every
input; a mutant that does not compile is STILLBORN and not counted. Equivalent mutants are not
removed, so the kill rate is a lower bound.

Operators (Rust tokens outside comments, strings and char literals; binary operators only when
written with a space on both sides, which separates them from generics such as Vec<i32>):
  relational  ` < `<->` <= `, ` > `<->` >= `, ` == `<->` != `
  arithmetic  ` + `<->` - `, ` * `->` + `, ` += `<->` -= `
  constant    a decimal integer literal N -> N+1 (and N-1 for N>0)
Site choice is deterministic: per translation and operator class, up to PER_CLASS sites evenly
spaced over the class's site list (first, middle, last ...), one fault per mutant.

  python3 -I systems/adatrans/mutate.py --run run1 --build /b --inputs /inputs
Writes <build>/mutation_<run>.json.
"""
import argparse
import json
import multiprocessing as mp
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_diff as rd  # noqa: E402

PER_CLASS = 2
REL = [(" <= ", " < "), (" >= ", " > "), (" < ", " <= "), (" > ", " >= "), (" == ", " != "), (" != ", " == ")]
ARI = [(" += ", " -= "), (" -= ", " += "), (" + ", " - "), (" - ", " + "), (" * ", " + ")]
INT_RE = re.compile(r"(?<![\w.])(\d+)(?![\w.])")


def mask(src):
    """Same length as src, with comments, string and char literals replaced by spaces."""
    out = list(src)
    i, n = 0, len(src)

    def blank(a, b):
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "
    while i < n:
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif src[i] == '"' or (src[i] == "b" and i + 1 < n and src[i + 1] == '"' and (i == 0 or not (src[i - 1].isalnum() or src[i - 1] == "_"))):
            j = i + (2 if src[i] == "b" else 1)
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            blank(i, min(n, j + 1))
            i = j + 1
        elif src[i] == "'":
            m = re.match(r"'(\\.|\\u\{[0-9a-fA-F]+\}|\\x[0-9a-fA-F]{2}|[^\\'])'", src[i:])
            if m:
                blank(i, i + m.end())
                i += m.end()
            else:
                i += 1  # a lifetime
        else:
            i += 1
    return "".join(out)


def sites(src):
    m = mask(src)
    res = {"relational": [], "arithmetic": [], "constant": []}
    for cls, table in (("relational", REL), ("arithmetic", ARI)):
        taken = set()
        for old, new in table:
            start = 0
            while True:
                k = m.find(old, start)
                if k < 0:
                    break
                start = k + 1
                if any(k <= t < k + len(old) for t in taken):
                    continue
                # skip `->`, `=>`, `<<`, `>>`, `&&`-style neighbours and generic-looking `< T >`
                if old.strip() in ("<", ">") and (m[k + len(old):k + len(old) + 1] in "=<>" or m[k - 1:k] in "<>-="):
                    continue
                for t in range(k, k + len(old)):
                    taken.add(t)
                res[cls].append((k, old, new))
    for mm in INT_RE.finditer(m):
        v = int(mm.group(1))
        if len(mm.group(1)) > 18:
            continue
        res["constant"].append((mm.start(1), mm.group(1), str(v + 1)))
        if v > 0:
            res["constant"].append((mm.start(1), mm.group(1), str(v - 1)))
    for cls in res:
        res[cls].sort()
    return res


def pick(lst, k):
    if len(lst) <= k:
        return lst
    return [lst[round(i * (len(lst) - 1) / (k - 1))] for i in range(k)] if k > 1 else [lst[0]]


def mutants(src):
    out = []
    for cls, lst in sites(src).items():
        for (pos, old, new) in pick(lst, PER_CLASS):
            assert src[pos:pos + len(old)] == old
            line = src.count("\n", 0, pos) + 1
            out.append({"class": cls, "line": line, "old": old.strip(), "new": new.strip(),
                        "src": src[:pos] + new + src[pos + len(old):]})
    return out


def eval_mutant(args):
    name, binp, inst, bdir, inputs_dir = args
    cdir = os.path.join(bdir, "c", inst)
    d = os.path.join(inputs_dir, inst)
    n = 0
    for f in sorted(os.listdir(d)):
        stdin = open(os.path.join(d, f), "rb").read()
        cr = rd.run1(os.path.join(cdir, "ref"), stdin, rd.C_TIMEOUT)
        rr = rd.run1(binp, stdin, rd.R_TIMEOUT)
        kind, ch = rd.classify(cr, rr)
        n += 1
        if kind in ("agree", "c_timeout"):
            continue
        clean, ev, _ = rd.ub_check(cdir, stdin, cr)
        if clean:
            return name, {"status": "killed", "by_input": f, "kind": kind, "channels": ch, "inputs_run": n}
    return name, {"status": "survived", "inputs_run": n}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="run1")
    ap.add_argument("--build", default="/b")
    ap.add_argument("--inputs", default="/inputs")
    ap.add_argument("--agreeing", required=True, help="JSON list of instances that agree on all our inputs")
    ap.add_argument("--jobs", type=int, default=3)
    a = ap.parse_args()
    t0 = time.time()
    agreeing = json.load(open(a.agreeing))
    mws = os.path.join(a.build, f"mut_ws_{a.run}")
    if os.path.isdir(mws):
        shutil.rmtree(mws)
    os.makedirs(mws)
    meta, members = {}, []
    for inst in agreeing:
        src = open(os.path.join(a.build, "extracted", a.run, inst + ".rs")).read()
        for j, mu in enumerate(mutants(src)):
            name = f"m_{a.run}_{inst}_{j}".lower()
            pd = os.path.join(mws, name)
            os.makedirs(os.path.join(pd, "src"))
            with open(os.path.join(pd, "src", "main.rs"), "w") as fh:
                fh.write(mu.pop("src"))
            with open(os.path.join(pd, "Cargo.toml"), "w") as fh:
                fh.write(rd.CARGO_TOML.format(name=name))
            meta[name] = dict(mu, inst=inst)
            members.append(name)
    with open(os.path.join(mws, "Cargo.toml"), "w") as fh:
        fh.write("[workspace]\nresolver = \"2\"\nmembers = [\n" + "".join(f'  "{m}",\n' for m in members) + "]\n")
    shutil.copy(os.path.join(a.build, "vendor_tpl", "Cargo.lock"), os.path.join(mws, "Cargo.lock"))
    pr = subprocess.run(["cargo", f"+{rd.RUST_TC}", "build", "--offline", "--keep-going", "-j", str(a.jobs),
                         "--message-format=short"], cwd=mws, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    jobs = []
    for name in members:
        b = os.path.join(mws, "target", "debug", name)
        if os.path.isfile(b):
            jobs.append((name, b, meta[name]["inst"], a.build, a.inputs))
        else:
            meta[name]["status"] = "stillborn"
    with mp.Pool(a.jobs) as pool:
        for name, r in pool.imap_unordered(eval_mutant, jobs):
            meta[name].update(r)
    by = {}
    for v in meta.values():
        c = by.setdefault(v["class"], {"killed": 0, "survived": 0, "stillborn": 0})
        c[v["status"]] += 1
    tot = {k: sum(c[k] for c in by.values()) for k in ("killed", "survived", "stillborn")}
    rate = tot["killed"] / max(1, tot["killed"] + tot["survived"])
    out = {"run": a.run, "translations": len(agreeing), "mutants": len(meta), "by_class": by, "total": tot,
           "kill_rate": round(rate, 4), "per_class_sites": PER_CLASS, "seconds": round(time.time() - t0),
           "mutants_detail": meta}
    with open(os.path.join(a.build, f"mutation_{a.run}.json"), "w") as fh:
        json.dump(out, fh, indent=1, sort_keys=True)
    print(json.dumps({k: v for k, v in out.items() if k != "mutants_detail"}, indent=1))


if __name__ == "__main__":
    main()
