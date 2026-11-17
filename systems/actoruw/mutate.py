"""Harness power for the ACTOR re-test: mutation analysis on AGREEING translations (mutation_run1).

Model: systems/adatrans/mutate.py. For every translation that agrees with the C on ALL of our inputs
(exe: no divergent input in difftest.json; lib: none in lib_difftest.json other than inputs where the
C itself times out), seed ONE fault into its Rust source (src/**/*.rs), rebuild exactly as the re-test
did (exe: build.py's `cargo build --release --offline --bins <features>`; lib: lib_harness.py's
`cargo build --release --offline --lib`, then the same driver_main.c link against the mutant .so), and
re-run the SAME inputs (gen_inputs.generate / lib_harness.inputs_for) through difftest.run_one against
the C oracle built by the re-test.

Operators (adatrans/mutate.py tables; tokens outside comments, string/char literals and #[...]
attributes; binary operators only when spaced on both sides, which separates them from generics):
  relational  ` < `<->` <= `, ` > `<->` >= `, ` == `<->` != `
  arithmetic  ` + `<->` - `, ` * `->` + `, ` += `<->` -= `
  constant    a decimal integer literal N (optionally with an integer type suffix) -> N+1 (and N-1 for N>0)
Sites: per translation and class, PER_CLASS = 2 sites drawn with random.Random(f"{SEED}:{kind}:{b}/{c}:{class}")
from the class's site list sorted by (file, offset). Translations are processed in sorted order.

Kill rule (the one in results/harness_power.json): a mutant is KILLED only if, on some input, our harness
reports a wrong answer (stdout or exit code differ from the C; lib: also the float tolerance of
lib_harness.same_lib on the #ret line) or a crash (Rust panic or signal) where the C's outcome differs.
A stderr-only difference does not count. A hang (mutant times out, C does not) does not count; a mutant
with a hang and no kill is UNDETERMINED. A candidate kill is confirmed by re-running both C and mutant on
that input (a flaky C output is not a kill). A mutant that does not build is STILLBORN (excluded).
Equivalent mutants are not removed (binary_identical records a byte-identical binary), so the kill rate
is a lower bound. The unmutated rebuild of each translation must itself pass the rule on every input;
a translation whose rebuild does not is excluded (baseline_excluded).

Usage (in container): python3 -I mutate.py [-j 16]  -> /w/out/mutation.json
"""
import argparse
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import difftest  # noqa: E402
import gen_inputs  # noqa: E402
import lib_harness  # noqa: E402

SEED = "actoruw-mutation-run1-2026-11-13"
PER_CLASS = 2
CLASSES = ("relational", "arithmetic", "constant")
REL = [(" <= ", " < "), (" >= ", " > "), (" < ", " <= "), (" > ", " >= "), (" == ", " != "), (" != ", " == ")]
ARI = [(" += ", " -= "), (" -= ", " += "), (" + ", " - "), (" - ", " + "), (" * ", " + ")]
INT_RE = re.compile(r"(?<![\w.])(\d+)(?:_?(?:u8|u16|u32|u64|u128|usize|i8|i16|i32|i64|i128|isize))?(?![\w.])")
KILL_RULE = ("killed only if our harness, on our inputs, reports a wrong answer (stdout or exit code; lib: "
             "same_lib float tolerance on #ret) or a crash (Rust panic/signal) that differs from the C; "
             "stderr-only differences and hangs/timeouts are not kills; a mutant with a hang and no kill is "
             "undetermined; ACTOR's own tests are not run")


def mask(src):
    """Same length as src, with comments, string/char literals and #[...] attributes blanked."""
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
        elif src.startswith("#[", i) or src.startswith("#![", i):
            j, depth = src.index("[", i), 0
            while j < n:
                depth += {"[": 1, "]": -1}.get(src[j], 0)
                j += 1
                if depth == 0:
                    break
            blank(i, j)
            i = j
        elif src[i] == '"' or (src[i] == "b" and i + 1 < n and src[i + 1] == '"' and (i == 0 or not (src[i - 1].isalnum() or src[i - 1] == "_"))):
            j = i + (2 if src[i] == "b" else 1)
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            blank(i, min(n, j + 1))
            i = j + 1
        elif src[i] == "r" and re.match(r'r#*"', src[i:i + 8]) and (i == 0 or not (src[i - 1].isalnum() or src[i - 1] == "_")):
            h = re.match(r'r(#*)"', src[i:]).group(1)
            j = src.find('"' + h, i + len(h) + 2)
            j = n if j < 0 else j + 1 + len(h)
            blank(i, j)
            i = j
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
    res = {c: [] for c in CLASSES}
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
    return res


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def sh(argv, cwd=None, env=None, timeout=900):
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"


def wilson(k, n, z=1.96):
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


# ------------------------------------------------------------------------------------- targets/inputs
def targets():
    out = []
    dt = json.load(open(os.path.join(common.W, "out", "difftest.json")))
    out += [("exe", p) for p in dt["programs"] if not p["divergent"]]
    ld = json.load(open(os.path.join(common.W, "out", "lib_difftest.json")))
    out += [("lib", p) for p in ld["programs"] if all(d["class"] == "c_timeout" for d in p["divergent"])]
    return sorted(out, key=lambda t: (t[0], t[1]["battery"], t[1]["case"]))


_SCAN = {}


def program_inputs(kind, p):
    if kind == "exe":
        _, src = common.phase_dir(p["battery"], p["case"])
        ins = gen_inputs.generate(os.path.join(src, "c_src"))[1]
    else:
        if not _SCAN:
            _SCAN.update({(r["battery"], r["case"]): r for r in
                          json.load(open(os.path.join(common.W, "out", "lib_scan.json")))["cases"]})
        ins = []
        for i, fn in enumerate(_SCAN[(p["battery"], p["case"])]["functions"]):
            for a in lib_harness.inputs_for(fn):
                ins.append(gen_inputs.case([str(i)] + a, b"", note=f"fn:{fn['name']}"))
    assert [x["id"] for x in ins] == p["input_ids"], ("inputs differ from the re-test", p["battery"], p["case"])
    return ins


def cbin_of(kind, p):
    d = os.path.join(common.W, "build", p["battery"], p["case"])
    return os.path.join(d, "c", "driver") if kind == "exe" else os.path.join(d, "c_driver")


_BJ, _LB = {}, {}


def build(kind, p, mut, wdir):
    """Copy the re-test's crate, apply the mutant (None = unmutated), build; return (binary, artifact, rc)."""
    b, c = p["battery"], p["case"]
    base = os.path.join(common.W, "build", b, c, "rust")
    crate = os.path.join(wdir, "crate")
    if os.path.isdir(crate):
        shutil.rmtree(crate)
    shutil.copytree(base, crate, ignore=shutil.ignore_patterns("target"))
    if mut is not None:
        f = os.path.join(crate, mut["file"])
        s = open(f).read()
        assert s[mut["pos"]:mut["pos"] + len(mut["old_tok"])] == mut["old_tok"]
        open(f, "w").write(s[:mut["pos"]] + mut["new_tok"] + s[mut["pos"] + len(mut["old_tok"]):])
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = os.path.join(wdir, "target")
    env.setdefault("CARGO_BUILD_JOBS", "2")
    if kind == "exe":
        if not _BJ:
            _BJ.update({(r["battery"], r["case"]): r for r in
                        json.load(open(os.path.join(common.W, "out", "build.json")))["cases"]})
        _, feats = common.case_config(c)
        binp = os.path.join(wdir, "target", "release", os.path.basename(_BJ[(b, c)]["rust_bin"]))
        if os.path.exists(binp):
            os.remove(binp)
        rc, _ = sh(["cargo", "build", "--release", "--offline", "--bins"] + feats, cwd=crate, env=env)
        return binp, binp, rc
    if not _LB:
        _LB.update({(r["battery"], r["case"]): r for r in
                    json.load(open(os.path.join(common.W, "out", "lib_build.json")))["cases"]})
    so = os.path.join(wdir, "target", "release", os.path.basename(_LB[(b, c)]["rust_so"]))
    if os.path.exists(so):
        os.remove(so)
    rc, _ = sh(["cargo", "build", "--release", "--offline", "--lib"], cwd=crate, env=env)
    if rc != 0 or not os.path.isfile(so):
        return None, None, rc or 1
    link = _LB[(b, c)]["rust_link"]["cmd"].split()
    out = os.path.join(wdir, "rust_driver")
    link[link.index("-o") + 1] = out
    link = [so if x.endswith(".so") else x for x in link]
    link = ["-Wl,-rpath," + os.path.dirname(so) if x.startswith("-Wl,-rpath,") else x for x in link]
    rc2, _ = sh(link)
    return out, so, rc2


# --------------------------------------------------------------------------------------- the rule
def judge(kind, cr, rr):
    """One input under the kill rule: agree | stderr_only | hang | wrong_answer | crash | c_timeout."""
    if cr["timeout"]:
        return "c_timeout"
    if rr["timeout"]:
        return "hang"
    if kind == "exe":
        diff = cr["stdout"] != rr["stdout"] or cr["rc"] != rr["rc"]
    else:
        diff = not lib_harness.same_lib(dict(cr, stderr=b""), dict(rr, stderr=b""))
    if not diff:
        return "agree" if cr["stderr"] == rr["stderr"] else "stderr_only"
    crash = (rr["rc"] == 101 and b"panicked" in rr["stderr"]) or (rr["rc"] is not None and rr["rc"] < 0)
    return "crash" if crash else "wrong_answer"


def evaluate(kind, cbin, ins, cres, mbin):
    hangs, s2, n = 0, 0, 0
    for x, cr in zip(ins, cres):
        n += 1
        rr = difftest.run_one(mbin, x, timeout=difftest.TIMEOUT if hangs < 3 else 1)
        v = judge(kind, cr, rr)
        if v == "hang":
            hangs += 1
        elif v == "stderr_only":
            s2 += 1
        elif v in ("wrong_answer", "crash"):
            v2 = judge(kind, difftest.run_one(cbin, x), difftest.run_one(mbin, x))   # confirm (flaky C/Rust)
            if v2 in ("wrong_answer", "crash"):
                return {"status": "killed", "kill_kind": v, "by_input": x["id"], "inputs_run": n,
                        "hangs_before": hangs, "stderr_only_before": s2}
    st = "undetermined" if hangs else "survived"
    return {"status": st, "inputs_run": n, "hang_inputs": hangs, "stderr_only_inputs": s2}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-j", type=int, default=16)
    ap.add_argument("--only", default=None, help="battery/case")
    ap.add_argument("--out", default=os.path.join(common.W, "out", "mutation.json"))
    a = ap.parse_args()
    t0 = time.time()
    tg = targets()
    if a.only:
        tg = [t for t in tg if f"{t[1]['battery']}/{t[1]['case']}" == a.only]
    plan, jobs = [], []
    for kind, p in tg:
        crate = os.path.join(common.W, "build", p["battery"], p["case"], "rust")
        files = sorted(os.path.relpath(os.path.join(dp, f), crate)
                       for dp, _, fs in os.walk(os.path.join(crate, "src")) for f in fs if f.endswith(".rs"))
        pool = {c: [] for c in CLASSES}
        for rel in files:
            src = open(os.path.join(crate, rel)).read()
            for cls, lst in sites(src).items():
                pool[cls] += [(rel, pos, old, new, src.count("\n", 0, pos) + 1) for pos, old, new in lst]
        chosen = []
        for cls in CLASSES:
            lst = sorted(pool[cls])
            rnd = random.Random(f"{SEED}:{kind}:{p['battery']}/{p['case']}:{cls}")
            for rel, pos, old, new, line in sorted(rnd.sample(lst, min(PER_CLASS, len(lst)))):
                chosen.append({"kind": kind, "translation": f"{p['battery']}/{p['case']}", "class": cls,
                               "file": rel, "line": line, "pos": pos, "old_tok": old, "new_tok": new,
                               "old": old.strip(), "new": new.strip()})
        plan.append({"kind": kind, "translation": f"{p['battery']}/{p['case']}", "rs_files": len(files),
                     "sites": {c: len(pool[c]) for c in CLASSES}, "mutants": len(chosen)})
        jobs += [(kind, p, m) for m in chosen]

    # oracle + unmutated rebuild per translation (parallel)
    base = {}

    def prep(t):
        kind, p = t
        key = f"{p['battery']}/{p['case']}"
        ins = program_inputs(kind, p)
        cbin = cbin_of(kind, p)
        cres = [difftest.run_one(cbin, x) for x in ins]
        wdir = os.path.join(common.W, "mut1", kind, p["battery"], p["case"], "base")
        os.makedirs(wdir, exist_ok=True)
        bbin, art, rc = build(kind, p, None, wdir)
        verdicts = [judge(kind, cr, difftest.run_one(bbin, x)) for x, cr in zip(ins, cres)] if rc == 0 else []
        ok = rc == 0 and all(v in ("agree", "stderr_only", "c_timeout") for v in verdicts)
        base[key] = {"ins": ins, "cres": cres, "cbin": cbin, "art_sha": sha(art) if rc == 0 else None,
                     "ok": ok, "rc": rc, "bad": sorted({v for v in verdicts if v not in ("agree", "stderr_only", "c_timeout")})}
        print(f"base {kind} {key}: inputs={len(ins)} ok={ok}", flush=True)

    with ThreadPoolExecutor(a.j) as ex:
        list(ex.map(prep, tg))

    def work(job):
        kind, p, m = job
        key = f"{p['battery']}/{p['case']}"
        bs = base[key]
        r = {k: v for k, v in m.items() if k not in ("old_tok", "new_tok", "pos")}
        if not bs["ok"]:
            r["status"] = "baseline_excluded"
            return r
        wdir = os.path.join(common.W, "mut1", kind, f"w{threading.get_ident()}", f"{p['battery']}_{p['case']}")
        os.makedirs(wdir, exist_ok=True)
        mbin, art, rc = build(kind, p, m, wdir)
        if rc != 0 or not mbin or not os.path.isfile(mbin):
            r["status"] = "stillborn"
            return r
        r["binary_identical"] = sha(art) == bs["art_sha"]
        r.update(evaluate(kind, bs["cbin"], bs["ins"], bs["cres"], mbin))
        print(f"{r['status']:12s} {kind} {key} {r['class']} {r['file']}:{r['line']} {r['old']}->{r['new']}", flush=True)
        return r

    with ThreadPoolExecutor(a.j) as ex:
        res = list(ex.map(work, jobs))

    def tally(rs):
        by = {}
        for c in CLASSES:
            x = [r for r in rs if r["class"] == c]
            by[c] = {s: sum(r["status"] == s for r in x) for s in ("killed", "survived", "undetermined", "stillborn")}
        tot = {s: sum(by[c][s] for c in CLASSES) for s in ("killed", "survived", "undetermined", "stillborn")}
        comp = tot["killed"] + tot["survived"] + tot["undetermined"]
        tot["compiled"] = comp
        return {"total": tot, "by_class": by,
                "kill_rate": round(tot["killed"] / comp, 4) if comp else None,
                "wilson95": wilson(tot["killed"], comp),
                "killed_by_kind": {k: sum(r.get("kill_kind") == k for r in rs) for k in ("wrong_answer", "crash")},
                "survived_binary_identical": sum(r["status"] == "survived" and bool(r.get("binary_identical")) for r in rs)}

    counted = [r for r in res if r["status"] != "baseline_excluded"]
    used = sorted({r["translation"] for r in counted})
    out = {"provenance": {"script": "systems/actoruw/mutate.py", "host": os.environ.get("OUB_HOST"),
                          "image": os.environ.get("OUB_IMAGE"), "image_id": os.environ.get("OUB_IMAGE_ID"),
                          "git_rev": os.environ.get("OUB_GIT_REV"), "command": os.environ.get("OUB_COMMAND"),
                          "argv": sys.argv, "rustc": sh(["rustc", "-V"])[1].strip(), "seconds": round(time.time() - t0),
                          "date": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
           "kill_rule": KILL_RULE, "seed": SEED, "per_class_sites": PER_CLASS,
           "translations_agreeing": {k: sum(t[0] == k for t in tg) for k in ("exe", "lib")},
           "translations_mutated": {k: sum(1 for t in used if any(r["translation"] == t and r["kind"] == k for r in counted))
                                    for k in ("exe", "lib")},
           "baseline_excluded": [{"translation": k, "rc": v["rc"], "verdicts": v["bad"]} for k, v in sorted(base.items()) if not v["ok"]],
           **tally(counted),
           "by_kind": {k: tally([r for r in counted if r["kind"] == k]) for k in ("exe", "lib")},
           "targets": plan, "mutants": res}
    common.dump(a.out, out)
    print(json.dumps({k: out[k] for k in ("translations_mutated", "total", "kill_rate", "wilson95", "killed_by_kind",
                                          "baseline_excluded")}, indent=1))
    print("seconds", out["provenance"]["seconds"])


if __name__ == "__main__":
    main()
