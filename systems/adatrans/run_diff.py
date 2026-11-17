"""Differential re-grade of AdaTrans's final Rust against the original C (the oracle).

Runs INSIDE Docker (crust-heldout-plus186: gcc 11.4, clang 14, rustc/cargo 1.86.0 via rustup),
with --network none. Phases:

  build   C: every program in 7 builds (oracle_ub_common flags):
            ref   gcc -w <c> -lm                       (the artifact's fuzz scripts: `gcc <c> -o solution`)
            o1/o2 gcc -w -O1 / -O2                      (optimization-level cross-check)
            san_clang  clang-14 ASan+UBSan+float-cast-overflow -O0 (non-PIE), -fno-sanitize-recover=all
            san_gcc    gcc-11, same sanitizer set
            uchar/schar gcc -w -funsigned-char / -fsigned-char
          Rust: one cargo workspace, one package per (run, instance), each package exactly the
            artifact's own project (src/runner.py::_init_cargo_project: edition 2021, deps libc 0.2,
            rand 0.8, regex 1, lazy_static 1.4), `cargo build` debug profile (the artifact's check
            builds `cargo build --quiet` and runs target/debug/solution), --offline from a vendored
            copy of those crates, --keep-going.
  run     every (translation, input): C ref and Rust on the same stdin; compare stdout bytes, exit code
          and stderr (strict), and stdout with whitespace folded (the artifact's own _normalize).
          Inputs: ours (gen_inputs.py), and the artifact's own check inputs, parsed exactly as
          src/runner.py::_load_test_cases_from_file does (examples = loop check, outputs = fuzz oracle).
          Every divergent case is re-checked under the UB rule: the C must be clean under both
          sanitizer builds, give identical stdout+exit at -O0/-O1/-O2, identical under
          -funsigned-char/-fsigned-char, and identical on a repeat run.
Writes <build>/cases_<run>.jsonl (one line per case) and <build>/run_<run>.json (summary).
"""
import argparse
import hashlib
import json
import multiprocessing as mp
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "harness"))
import oracle_ub_common as ou  # noqa: E402

ART = os.path.join(REPO, "artifacts", "adatrans")
CSRC = os.path.join(ART, "dataset", "C_CPP", "C", "src")
RUST_TC = os.environ.get("ADATRANS_RUST_TC", "1.86.0")
C_TIMEOUT = 10      # s of CPU time (the artifact's fuzz validation timeout is 10 s wall)
R_TIMEOUT = 10
R_TIMEOUT_LONG = 60  # a Rust timeout is re-run with this CPU limit before it is called a hang

VARIANTS = {
    "ref": ("gcc", ["-w"]),
    "o1": ("gcc", ["-w", "-O1"]),
    "o2": ("gcc", ["-w", "-O2"]),
    "san_clang": ("clang", ["-w"] + ou.san_cflags("clang") + ou.san_ldflags("clang")),
    "san_gcc": ("gcc", ["-w"] + ou.san_cflags("gcc") + ou.san_ldflags("gcc")),
    "uchar": ("gcc", ["-w", "-funsigned-char"]),
    "schar": ("gcc", ["-w", "-fsigned-char"]),
}

CARGO_TOML = """[package]
name = "{name}"
version = "0.1.0"
edition = "2021"

[dependencies]
libc = "0.2"
rand = "0.8"
regex = "1"
lazy_static = "1.4"
"""


def sha(b):
    return hashlib.sha256(b).hexdigest()


def insts():
    return sorted(f[len("weekly_contest_"):-2].replace("_p", "_") for f in os.listdir(CSRC)
                  if f.startswith("weekly_contest_") and f.endswith(".c"))


# ----------------------------------------------------------------------------------------- build
def build_c(bdir):
    info = {}
    for i in insts():
        c, p = i.split("_")
        src = os.path.join(CSRC, f"weekly_contest_{c}_p{p}.c")
        d = os.path.join(bdir, "c", "C_" + i)
        os.makedirs(d, exist_ok=True)
        r = {}
        for name, (cc, flags) in VARIANTS.items():
            out = os.path.join(d, name)
            pr = subprocess.run([cc] + flags + [src, "-o", out, "-lm"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
            r[name] = {"ok": pr.returncode == 0 and os.path.isfile(out), "log": pr.stdout[-600:]}
        info["C_" + i] = r
    return info


def build_rust(bdir, extracted, runs):
    ws = os.path.join(bdir, "ws")
    os.makedirs(ws, exist_ok=True)
    members = []
    for run in runs:
        for f in sorted(os.listdir(os.path.join(extracted, run))):
            if not f.endswith(".rs"):
                continue
            inst = f[:-3]
            name = f"{run}_{inst}".lower()
            pd = os.path.join(ws, name)
            os.makedirs(os.path.join(pd, "src"), exist_ok=True)
            shutil.copy(os.path.join(extracted, run, f), os.path.join(pd, "src", "main.rs"))
            with open(os.path.join(pd, "Cargo.toml"), "w") as fh:
                fh.write(CARGO_TOML.format(name=name))
            members.append(name)
    with open(os.path.join(ws, "Cargo.toml"), "w") as fh:
        fh.write("[workspace]\nresolver = \"2\"\nmembers = [\n" + "".join(f'  "{m}",\n' for m in members) + "]\n")
    env = dict(os.environ)
    t0 = time.time()
    lock = os.path.join(bdir, "vendor_tpl", "Cargo.lock")
    if os.path.isfile(lock) and not os.path.isfile(os.path.join(ws, "Cargo.lock")):
        shutil.copy(lock, os.path.join(ws, "Cargo.lock"))  # same crate versions as the vendored set
    pr = subprocess.run(["cargo", f"+{RUST_TC}", "build", "--offline", "--keep-going", "-j",
                         os.environ.get("CARGO_JOBS", "3"),
                         "--message-format=short"], cwd=ws, env=env, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True)
    with open(os.path.join(bdir, f"rust_build_{RUST_TC}.log"), "w") as fh:
        fh.write(pr.stdout)
    res = {}
    for m in members:
        b = os.path.join(ws, "target", "debug", m)
        res[m] = {"ok": os.path.isfile(b), "bin": b}
    return {"toolchain": RUST_TC, "rc": pr.returncode, "seconds": round(time.time() - t0),
            "members": len(members), "built": sum(1 for v in res.values() if v["ok"]), "per": res}


# ------------------------------------------------------------------------------------------- run
def parse_cases(path):
    """Exact copy of src/runner.py::_load_test_cases_from_file (the artifact's parser)."""
    if not os.path.exists(path):
        return []
    cases = []
    content = open(path).read()
    for block in content.split("-------------------------"):
        if not block.strip():
            continue
        input_str, output_str, mode = "", "", None
        for line in block.strip().splitlines():
            if line.strip() == "input:":
                mode = "input"
                continue
            elif line.strip() == "output:":
                mode = "output"
                continue
            if mode == "input":
                input_str += line + "\n"
            elif mode == "output":
                output_str += line + "\n"
        if input_str and output_str:
            cases.append({"input": input_str.strip(), "expected": output_str.strip()})
    return cases


def norm(b):
    return b" ".join(b.strip().split())


def _cpu_limit(sec):
    def f():
        resource.setrlimit(resource.RLIMIT_CPU, (sec, sec + 1))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return f


def run1(binp, stdin, timeout):
    """Run with a CPU-time limit of `timeout` s (RLIMIT_CPU: SIGXCPU, then SIGKILL) and a wall-clock
    backstop of 4*timeout+5 s. CPU time, not wall time, decides 'timed out', so the verdict does not
    depend on the load of the (shared) machine. Output of a timed-out run is dropped (it is partial
    and would vary)."""
    try:
        p = subprocess.run([binp], input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=4 * timeout + 5, preexec_fn=_cpu_limit(timeout))
        to = p.returncode in (-signal.SIGXCPU, -signal.SIGKILL)
        o, e, rc = p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired:
        to, o, e, rc = True, b"", b"", -1
    if to:
        o, e, rc = b"", b"", -1
    return {"stdout": o, "stderr": e, "rc": rc, "timed_out": to}


def short(r):
    return {"rc": r["rc"], "timed_out": r["timed_out"], "stdout_sha": sha(r["stdout"]),
            "stderr_sha": sha(r["stderr"]), "stdout_len": len(r["stdout"]),
            "stdout_head": r["stdout"][:240].decode("latin1"), "stderr_head": r["stderr"][:300].decode("latin1")}


_PID = re.compile(r"==\d+==")
_ADDR = re.compile(r"0x[0-9a-fA-F]+")


def stable(t):
    """Sanitizer text without PIDs and addresses (they change run to run)."""
    return _ADDR.sub("0xADDR", _PID.sub("==PID==", t)) if isinstance(t, str) else t


def ub_check(cdir, stdin, ref):
    """The UB rule. Returns (clean: bool, evidence list, detail dict)."""
    ev, det = [], {}
    rep = run1(os.path.join(cdir, "ref"), stdin, C_TIMEOUT)
    det["repeat_identical"] = rep["stdout"] == ref["stdout"] and rep["rc"] == ref["rc"]
    if not det["repeat_identical"]:
        ev.append("nondeterministic: repeat run differs")
    for n in ("o1", "o2", "uchar", "schar"):
        b = os.path.join(cdir, n)
        if not os.path.isfile(b):
            ev.append(f"{n}: build failed")
            continue
        r = run1(b, stdin, C_TIMEOUT)
        same = r["stdout"] == ref["stdout"] and r["rc"] == ref["rc"]
        det[n] = {"same_as_ref": same, "rc": r["rc"], "timed_out": r["timed_out"]}
        if not same:
            ev.append(f"{n}: output differs from -O0 ref" if n in ("o1", "o2") else f"{n}: char-signedness dependent")
    for n in ("san_clang", "san_gcc"):
        b = os.path.join(cdir, n)
        if not os.path.isfile(b):
            ev.append(f"{n}: build failed")
            continue
        r = ou.run_san([b], stdin, timeout=120)
        det[n] = {"report": stable(r["report"]), "infra": r["infra"], "rc": r["rc"], "timed_out": r["timed_out"]}
        if r["report"]:
            ev.append(f"{n}: {stable(r['report'])}")
        elif r["infra"]:
            ev.append(f"{n}: sanitizer infra failure")
        elif r["timed_out"]:
            ev.append(f"{n}: timed out (120 s)")
        elif not (r["stdout"] == ref["stdout"] and r["rc"] == ref["rc"]):
            ev.append(f"{n}: output differs from the -O0 ref without a sanitizer report")   # audit 8
        det[n]["same_as_ref"] = r["stdout"] == ref["stdout"] and r["rc"] == ref["rc"]
    return (not ev), ev, det


def classify(c, r):
    """-> (kind, channels). kind in agree | whitespace_only | S1 | S2 | panic | crash | hang | c_timeout."""
    if c["timed_out"]:
        return "c_timeout", []
    if r["timed_out"]:
        return "hang", ["timeout"]
    ch = []
    if r["stdout"] != c["stdout"]:
        ch.append("stdout")
    if r["rc"] != c["rc"]:
        ch.append("exit")
    if r["stderr"] != c["stderr"]:
        ch.append("stderr")
    if not ch:
        return "agree", ch
    if r["rc"] == 101 and b"panicked" in r["stderr"] and c["rc"] != 101:
        return "panic", ch
    if r["rc"] < 0 and c["rc"] >= 0:
        return "crash", ch
    if norm(r["stdout"]) != norm(c["stdout"]) or r["rc"] != c["rc"]:
        return "S1", ch
    if r["stdout"] != c["stdout"]:
        return "whitespace_only", ch
    return "S2", ch


SRC_ORDER = {"ours": 0, "examples": 1, "fuzz": 2}


def row_key(r):
    return (r["inst"], SRC_ORDER[r["src"]], str(r["idx"]).zfill(6))


def load_stdin(inst, src, idx, inputs_dir, fuzz_dir, _cache={}):
    if src == "ours":
        return open(os.path.join(inputs_dir, inst, idx), "rb").read()
    key = (inst, src)
    if key not in _cache:
        c, p = inst[2:].split("_")
        fd = os.path.join(fuzz_dir, f"weekly_contest_{c}_p{p}")
        _cache[key] = [cs["input"].encode() for cs in parse_cases(os.path.join(fd, "examples" if src == "examples" else "outputs"))]
    return _cache[key][idx - 1]


def work_ub(args):
    """The UB rule for every divergent row of one translation."""
    rows, bdir, inputs_dir, fuzz_dir = args
    out = []
    for row in rows:
        if row["kind"] not in ("agree", "c_timeout", "rust_build_fail") and "c_ub_clean" not in row:
            stdin = load_stdin(row["inst"], row["src"], row["idx"], inputs_dir, fuzz_dir)
            cdir = os.path.join(bdir, "c", row["inst"])
            cr = run1(os.path.join(cdir, "ref"), stdin, C_TIMEOUT)
            clean, ev, det = ub_check(cdir, stdin, cr)
            row.update({"c_ub_clean": clean, "c_ub_evidence": ev, "ub_detail": det})
        out.append(row)
    return out


def work_inst(args):
    run, inst, bdir, rbin, inputs_dir, fuzz_dir, want, do_ub, part, nparts = args
    cdir = os.path.join(bdir, "c", inst)
    rows = []
    c, p = inst[2:].split("_")
    srcs = []
    d = os.path.join(inputs_dir, inst)
    if "ours" in want and os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            srcs.append(("ours", f, open(os.path.join(d, f), "rb").read()))
    fd = os.path.join(fuzz_dir, f"weekly_contest_{c}_p{p}")
    expected = {}
    for kind, fname in (("examples", "examples"), ("fuzz", "outputs")):
        if "system" not in want:
            break
        for j, cs in enumerate(parse_cases(os.path.join(fd, fname))):
            srcs.append((kind, j + 1, cs["input"].encode()))
            expected[(kind, j + 1)] = cs["expected"].encode()
    for kind, idx, stdin in srcs[part::nparts]:
        row = {"run": run, "inst": inst, "src": kind, "idx": idx, "input_sha": sha(stdin),
               "input_len": len(stdin)}
        if not rbin:
            row["kind"] = "rust_build_fail"
            rows.append(row)
            continue
        cr = run1(os.path.join(cdir, "ref"), stdin, C_TIMEOUT)
        rr = run1(rbin, stdin, R_TIMEOUT)
        if rr["timed_out"] and not cr["timed_out"]:
            # every Rust timeout is re-run with a 60 s CPU limit before it is called a hang
            rr = run1(rbin, stdin, R_TIMEOUT_LONG)
            row["rust_rerun_long_timeout"] = True
        kind, ch = classify(cr, rr)
        row.update({"kind": kind, "channels": ch})
        if (kind_src := expected.get((row["src"], row["idx"]))) is not None:
            # The artifact's own check, re-run as src/runner.py does it: exit 0 and whitespace-folded
            # stdout equal to the recorded expected output (examples: 2 s timeout; fuzz: 10 s).
            row["c_matches_recorded"] = (not cr["timed_out"]) and norm(cr["stdout"]) == norm(kind_src)
            row["rust_passes_own_check"] = ((not rr["timed_out"]) and rr["rc"] == 0
                                            and norm(rr["stdout"]) == norm(kind_src))
        if kind not in ("agree", "c_timeout"):
            row["c"] = short(cr)
            row["rust"] = short(rr)
            if do_ub:
                clean, ev, det = ub_check(cdir, stdin, cr)
                row.update({"c_ub_clean": clean, "c_ub_evidence": ev, "ub_detail": det})
            if len(stdin) <= 4096:
                row["input_text"] = stdin.decode("latin1")
        rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["build", "run", "ub"])
    ap.add_argument("--no-ub", action="store_true", help="run phase: leave the UB rule to the ub phase")
    ap.add_argument("--tag", default="")
    ap.add_argument("--parts", type=int, default=8, help="run phase: split each translation's inputs into this many jobs")
    ap.add_argument("--build", default="/b")
    ap.add_argument("--extracted", default="/b/extracted")
    ap.add_argument("--inputs", default="/inputs")
    ap.add_argument("--fuzz", default="/fuzz")
    ap.add_argument("--runs", nargs="+", default=["run1"])
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--srcs", nargs="+", default=["ours", "system"], choices=["ours", "system"])
    a = ap.parse_args()
    t0 = time.time()
    if a.phase == "build":
        info = {"c": build_c(a.build), "rust": build_rust(a.build, a.extracted, a.runs),
                "toolchains": ou.toolchains(), "rustc_pinned_by_us": ou.tool_version(["rustc", f"+{RUST_TC}", "--version"]),
                "c_variants": {k: [v[0]] + v[1] for k, v in VARIANTS.items()}}
        ou.dump(os.path.join(a.build, f"build_{RUST_TC}.json"), info)
        print(json.dumps({"c_ref_ok": sum(1 for v in info["c"].values() if v["ref"]["ok"]),
                          "c_all_ok": sum(1 for v in info["c"].values() if all(x["ok"] for x in v.values())),
                          "rust": {k: v for k, v in info["rust"].items() if k != "per"}}, indent=1))
        return
    if a.phase == "ub":
        for run in a.runs:
            src = os.path.join(a.build, f"cases_{run}{a.tag}.jsonl")
            rows = [json.loads(x) for x in open(src)]
            groups = {}
            for r in rows:
                groups.setdefault(r["inst"], []).append(r)
            jobs = [(groups[k], a.build, a.inputs, a.fuzz) for k in sorted(groups)]
            with mp.Pool(a.jobs) as pool:
                done = [r for rs in pool.map(work_ub, jobs, chunksize=1) for r in rs]
            done.sort(key=row_key)
            out = os.path.join(a.build, f"cases_{run}{a.tag}_ub.jsonl")
            with open(out + ".tmp", "w") as fh:
                for r in done:
                    fh.write(json.dumps(r, sort_keys=True) + "\n")
            os.replace(out + ".tmp", out)
            print(f"{run}: UB rule on {sum(1 for r in done if 'c_ub_clean' in r)} divergent cases -> {out}")
        return
    binfo = json.load(open(os.path.join(a.build, f"build_{RUST_TC}.json")))
    for run in a.runs:
        jobs = []
        for inst in ["C_" + i for i in insts()]:
            if a.only and inst not in a.only:
                continue
            m = binfo["rust"]["per"].get(f"{run}_{inst}".lower())
            rbin = m["bin"] if m and m["ok"] else None
            for part in range(a.parts):
                jobs.append((run, inst, a.build, rbin, a.inputs, a.fuzz, a.srcs, not a.no_ub, part, a.parts))
        # longest-first scheduling (by the number of inputs is not known to be long; keep the fixed
        # instance order) -- results are merged and SORTED, so the file does not depend on scheduling
        out = os.path.join(a.build, f"cases_{run}{a.tag}.jsonl")
        allrows = []
        with mp.Pool(a.jobs) as pool:
            for rows in pool.imap_unordered(work_inst, jobs):
                allrows += rows
                if rows and rows[0]["src"] == "ours" and str(rows[0]["idx"]) == "000.in":
                    print(f"[{time.time()-t0:.0f}s] {rows[0]['inst']}", flush=True)
        allrows.sort(key=row_key)
        with open(out + ".tmp", "w") as fh:
            for r in allrows:
                fh.write(json.dumps(r, sort_keys=True) + "\n")
        os.replace(out + ".tmp", out)
        print(f"{run}: {len(allrows)} cases -> {out}")


if __name__ == "__main__":
    main()
