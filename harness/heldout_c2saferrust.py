"""M2 (held-out measurement), C2SaferRust part.

C2SaferRust keeps an LLM rewrite only if the coreutils shell tests that passed on the starting
translation still pass (translate.py select_tests: every tests/<util>/*.sh, run as
`<test>.sh <code_dir>/target/debug` from the artifact root, timeout 25 s, pass = exit 0).
Those scripts build their inputs at run time, so the only exact record of "what the system's own
check fed the program" is a trace of each invocation. This script:

  trace   For cat/head/tail/uniq, runs EVERY tests/<util>/*.sh exactly as translate.py does,
          but with <code_dir>/target/debug replaced by a directory holding a logging shim named
          <util>. The shim records argv, cwd, locale env, the kind of fds 0/1/2, the exact stdin
          bytes the program is given (regular-file stdin: from the current offset, without moving
          it; pipe stdin: the bytes pumped through to the real program), and the content of every
          argv element that names an existing file (sparse files as data extents), then runs the
          real rust_WIP binary (exec for non-pipe stdin, so the test sees the same process).
  replay  Runs every traced invocation through OUR comparison (harness/common.run_capture +
          normalize_stderr, the same channels as harness/difftest.py: stdout, exit code, stderr)
          with the original C as oracle and C2SaferRust's rust_WIP as SUT.
  report  Exact overlap between the traced inputs and our 8,574 cases (argv + stdin bytes +
          contents of argv-named files; locale reported separately), membership of every counted
          divergence (results/<u>_diff_v4.json) in the traced set, and the replay verdicts.
          Writes results/heldout_c2saferrust.json.

  docker run --rm -v "$PWD:/work" -w /work crust-heldout python3 -I harness/heldout_c2saferrust.py all
"""
import base64
import datetime
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from common import run_capture, normalize_stderr  # noqa: E402
from build import build_oracle, build_sut, tool_versions  # noqa: E402

ROOT = os.environ.get("C2SR_ROOT", os.path.join(REPO, "artifacts", "c2saferrust"))
UTILS = ["cat", "head", "tail", "uniq"]
WORK = os.environ.get("HELDOUT_WORK", "/tmp/heldout_c2sr")
OUT = os.path.join(REPO, "results", "heldout_c2saferrust.json")
ACC = os.path.join(REPO, "results", "c2saferrust_acceptance_suite.json")

SHIM = r'''#!/usr/bin/python3 -I
import hashlib, json, os, signal, stat, subprocess, sys, threading, time
REAL = %(real)r
LOGDIR = %(logdir)r
TEST = %(test)r
UTIL = %(util)r
CAP = 16 << 20
ENVKEYS = ("LC_ALL", "LANG", "LC_CTYPE", "LC_COLLATE", "LC_MESSAGES", "LC_NUMERIC", "LANGUAGE",
           "POSIXLY_CORRECT", "_POSIX2_VERSION", "TZ")

def blob(data):
    h = hashlib.sha256(data).hexdigest()
    p = os.path.join(LOGDIR, "blobs", h)
    if not os.path.exists(p):
        t = p + ".tmp%%d" %% os.getpid()
        with open(t, "wb") as f:
            f.write(data)
        os.replace(t, p)
    return h

def kind(m):
    return ("reg" if stat.S_ISREG(m) else "dir" if stat.S_ISDIR(m) else "chr" if stat.S_ISCHR(m)
            else "fifo" if stat.S_ISFIFO(m) else "sock" if stat.S_ISSOCK(m)
            else "blk" if stat.S_ISBLK(m) else "other")

def pread_all(fd, n, off):
    out = []
    while n > 0:
        b = os.pread(fd, min(n, 1 << 20), off)
        if not b:
            break
        out.append(b); n -= len(b); off += len(b)
    return b"".join(out)

def reg_info(fd, off):
    st = os.fstat(fd)
    size = st.st_size
    info = {"size": size, "offset": off}
    n = max(0, size - off)
    if n <= CAP:
        info["sha256"] = blob(pread_all(fd, n, off)); info["complete"] = True
        return info
    saved = os.lseek(fd, 0, os.SEEK_CUR)
    ext, tot, pos = [], 0, off
    try:
        while pos < size:
            ds = os.lseek(fd, pos, os.SEEK_DATA)
            he = os.lseek(fd, ds, os.SEEK_HOLE)
            ext.append((ds, he)); tot += he - ds; pos = he
            if tot > CAP:
                break
    except OSError:
        pass
    os.lseek(fd, saved, os.SEEK_SET)
    if tot <= CAP:
        info["extents"] = [[a, b - a, blob(pread_all(fd, b - a, a))] for a, b in ext]
        info["complete"] = True
    else:
        info["complete"] = False
    return info

def fdinfo(fd):
    try:
        st = os.fstat(fd)
    except OSError:
        return {"kind": "closed"}
    d = {"kind": kind(st.st_mode)}
    try:
        d["path"] = os.readlink("/proc/self/fd/%%d" %% fd)
    except OSError:
        pass
    return d

argv = sys.argv[1:]
rec = {"test": TEST, "util": UTIL, "argv": argv, "cwd": os.getcwd(), "pid": os.getpid(),
       "t": time.time(), "env": {k: os.environ[k] for k in ENVKEYS if k in os.environ},
       "fd0": fdinfo(0), "fd1": fdinfo(1), "fd2": fdinfo(2)}
if rec["fd0"]["kind"] == "reg":
    rec["stdin"] = dict(reg_info(0, 0), kind="reg", offset=os.lseek(0, 0, os.SEEK_CUR))
files = {}
for a in argv:
    if a in files or a == "":
        continue
    try:
        st = os.stat(a)
    except OSError:
        continue
    info = {"kind": kind(st.st_mode), "symlink": os.path.islink(a), "abs": os.path.isabs(a)}
    if info["kind"] == "reg" and not (a.startswith("/proc") or a.startswith("/sys")):
        try:
            fd = os.open(a, os.O_RDONLY)
            info.update(reg_info(fd, 0)); os.close(fd)
        except OSError as e:
            info["read_error"] = str(e)
    elif info["kind"] == "reg":
        try:
            with open(a, "rb") as f:
                info["live_snapshot_sha256"] = blob(f.read(CAP))
        except OSError as e:
            info["read_error"] = str(e)
    files[a] = info
rec["files"] = files

def write_rec():
    p = os.path.join(LOGDIR, "inv", "%%.6f_%%d.json" %% (rec["t"], os.getpid()))
    with open(p + ".tmp", "w") as f:
        json.dump(rec, f)
    os.replace(p + ".tmp", p)

if rec["fd0"]["kind"] not in ("fifo", "sock"):
    rec["mode"] = "exec"
    write_rec()
    os.execv(REAL, [UTIL] + argv)   # argv[0] as a PATH lookup gives it

# pipe stdin: pump it through to the real program, recording what we deliver
rec["mode"] = "pump"
child = subprocess.Popen([UTIL] + argv, executable=REAL, stdin=subprocess.PIPE, bufsize=0)
_dn = os.open(os.devnull, os.O_WRONLY)
os.dup2(_dn, 2)   # the child already has the real stderr; keep the shim's own messages out of it
for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT, signal.SIGUSR1, signal.SIGALRM):
    signal.signal(s, lambda sig, _f: child.send_signal(sig))
state = {"buf": [], "kept": 0, "total": 0, "eof": False, "broken": False}
h = hashlib.sha256()
lock = threading.Lock()

def pump():
    try:
        while True:
            b = os.read(0, 65536)
            if not b:
                with lock:
                    state["eof"] = True
                break
            try:
                child.stdin.write(b); child.stdin.flush()
            except (BrokenPipeError, OSError):
                with lock:
                    state["broken"] = True
                break
            with lock:
                state["total"] += len(b); h.update(b)
                if state["kept"] < CAP:
                    state["buf"].append(b); state["kept"] += len(b)
    finally:
        try:
            child.stdin.close()
        except OSError:
            pass

th = threading.Thread(target=pump, daemon=True)
th.start()
rc = child.wait()
th.join(timeout=0.5)
with lock:
    data = b"".join(state["buf"])
    rec["stdin"] = {"kind": "pipe", "delivered_bytes": state["total"], "source_eof": state["eof"],
                    "child_stopped_reading": state["broken"], "sha256_delivered": h.hexdigest(),
                    "kept_sha256": blob(data[:CAP]), "kept_complete": state["total"] <= CAP}
rec["rc"] = rc
write_rec()
if rc < 0:
    signal.signal(-rc, signal.SIG_DFL)
    os.kill(os.getpid(), -rc)
sys.exit(rc)
'''


def sha(b):
    return hashlib.sha256(b).hexdigest()


# ------------------------------------------------------------------------------------- build
def build_all():
    info = {"tools": tool_versions(), "utils": {}}
    for u in UTILS:
        o, s = build_oracle(u), build_sut(u)
        os.environ["C2SR_VARIANT"] = "rust"           # the starting C2Rust translation, for attribution
        b = build_sut(u)
        os.environ["C2SR_VARIANT"] = "rust_WIP"
        info["utils"][u] = {"oracle": o.get("bin"), "oracle_ok": o["ok"], "sut": s.get("bin"), "sut_ok": s["ok"],
                            "baseline": b.get("bin"), "baseline_ok": b["ok"]}
        if not (o["ok"] and s["ok"] and b["ok"]):
            raise SystemExit(f"build failed for {u}: {o.get('log_tail')} {s.get('log_tail')} {b.get('log_tail')}")
    return info


# ------------------------------------------------------------------------------------- trace
def run_test(u, t, sut):
    logdir = os.path.join(WORK, "log", u, t)
    shimdir = os.path.join(WORK, "shim", u, t)
    for d in (os.path.join(logdir, "inv"), os.path.join(WORK, "log", "blobs"), shimdir):
        os.makedirs(d, exist_ok=True)
    blobs = os.path.join(WORK, "log", "blobs")
    if not os.path.exists(os.path.join(logdir, "blobs")):
        os.symlink(blobs, os.path.join(logdir, "blobs"))
    shim = os.path.join(shimdir, u)
    with open(shim, "w") as f:
        f.write(SHIM % {"real": os.path.abspath(sut), "logdir": logdir, "test": t, "util": u})
    os.chmod(shim, 0o755)
    rel = os.path.join("coreutils", "tests", u, t)
    try:
        r = subprocess.run(f"{rel} {shimdir}", shell=True, cwd=ROOT, timeout=25,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        rc = r.returncode
    except subprocess.TimeoutExpired:
        rc = "timeout"
    return u, t, rc


def trace(binfo):
    if os.path.isdir(os.path.join(WORK, "log")):
        shutil.rmtree(os.path.join(WORK, "log"))
    jobs = []
    for u in UTILS:
        tdir = os.path.join(ROOT, "coreutils", "tests", u)
        for t in sorted(x for x in os.listdir(tdir) if x.endswith(".sh")):
            jobs.append((u, t, binfo["utils"][u]["sut"]))
    with ThreadPoolExecutor(max_workers=int(os.environ.get("JOBS", "8"))) as ex:
        res = list(ex.map(lambda j: run_test(*j), jobs))
    return {f"{u}/{t}": rc for u, t, rc in res}


def load_invocations():
    invs = []
    for u in UTILS:
        base = os.path.join(WORK, "log", u)
        if not os.path.isdir(base):
            continue
        for t in sorted(os.listdir(base)):
            d = os.path.join(base, t, "inv")
            for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
                if f.endswith(".json"):
                    invs.append(json.load(open(os.path.join(d, f))))
    return invs


def blob_bytes(h):
    return open(os.path.join(WORK, "log", "blobs", h), "rb").read()


# ------------------------------------------------------------------------------------- canonical inputs
def reg_stdin_bytes(s):
    """Bytes a regular-file stdin presents to the program: file content from the fd offset."""
    if s.get("complete") and "sha256" in s:
        return blob_bytes(s["sha256"])[s.get("offset", 0):]
    return None


def stdin_key(inv):
    s = inv.get("stdin")
    k0 = inv["fd0"]["kind"]
    if s and s.get("kind") == "reg":
        b = reg_stdin_bytes(s)
        return ("bytes", sha(b)) if b is not None else ("reg-large", s.get("size"), s.get("offset"))
    if s and s.get("kind") == "pipe":
        if s["source_eof"] and s["kept_complete"]:
            return ("bytes", s["kept_sha256"])
        return ("pipe-unbounded", s["delivered_bytes"])
    if k0 == "chr" and inv["fd0"].get("path") == "/dev/null":
        return ("bytes", sha(b""))
    return ("fd0", k0, inv["fd0"].get("path"))


def file_key(info):
    if info is None:
        return None
    if info["kind"] == "reg":
        if info.get("complete") and "sha256" in info:
            return ("reg", info["sha256"])
        return ("reg-other", info.get("size"))
    return (info["kind"],)


def inv_key(inv):
    files = inv.get("files", {})
    fk = []
    for a in inv["argv"]:
        info = files.get(a)
        if info is not None and info.get("abs"):
            fk.append(("abs", a, info["kind"]))
        else:
            fk.append(file_key(info))
    return (inv["util"], tuple(inv["argv"]), stdin_key(inv), tuple(fk))


def case_key(c):
    stdin = base64.b64decode(c["stdin_b64"])
    files = {n: base64.b64decode(b) for n, b in c["files"].items()}
    fk = []
    for a in c["argv"]:
        if a in files:
            fk.append(("reg", sha(files[a])))
        elif a in c["make_dirs"]:
            fk.append(("dir",))
        elif os.path.isabs(a) and os.path.exists(a):
            fk.append(("abs", a, "reg" if os.path.isfile(a) else "dir" if os.path.isdir(a) else "other"))
        else:
            fk.append(None)
    return (c["util"], tuple(c["argv"]), ("bytes", sha(stdin)), tuple(fk))


# ------------------------------------------------------------------------------------- replay
def materialize(inv, wd):
    """Recreate the traced inputs in wd. Returns (stdin_spec, fidelity_notes, replayable).
    stdin_spec: ("bytes", b) | ("file", path, offset) | ("devnull",) | ("closed",)"""
    notes, ok = [], True
    for a, info in inv.get("files", {}).items():
        if info.get("abs"):
            notes.append(f"absolute path {a} read live")
            continue
        p = os.path.join(wd, a)
        os.makedirs(os.path.dirname(p) or wd, exist_ok=True)
        if info["kind"] == "dir":
            os.makedirs(p, exist_ok=True); notes.append(f"{a}: directory recreated empty")
        elif info["kind"] == "reg" and info.get("complete"):
            if "sha256" in info:
                open(p, "wb").write(blob_bytes(info["sha256"]))
            else:
                with open(p, "wb") as f:
                    f.truncate(info["size"])
                    for off, ln, h in info["extents"]:
                        f.seek(off); f.write(blob_bytes(h))
                notes.append(f"{a}: sparse file recreated from data extents")
        else:
            ok = False; notes.append(f"{a}: {info['kind']} not reproducible")
    s = inv.get("stdin")
    k0 = inv["fd0"]
    if s and s.get("kind") == "reg":
        if s.get("complete") and "sha256" in s:
            p = os.path.join(wd, ".heldout_stdin")
            open(p, "wb").write(blob_bytes(s["sha256"]))
            spec = ("file", p, s.get("offset", 0))
            notes.append(f"stdin: regular file, opened at offset {s.get('offset', 0)} (as in the test)")
        else:
            ok = False; spec = None; notes.append("stdin regular file too large")
    elif s and s.get("kind") == "pipe":
        spec = ("bytes", blob_bytes(s["kept_sha256"]))
        if not (s["source_eof"] and s["kept_complete"]):
            notes.append(f"stdin pipe was not at EOF when the program stopped; replayed the {len(spec[1])} bytes delivered")
    elif k0["kind"] == "chr" and k0.get("path") == "/dev/null":
        spec = ("devnull",)
    elif k0["kind"] == "closed":
        spec = ("closed",); notes.append("stdin closed (as in the test)")
    else:
        spec = ("bytes", b""); notes.append(f"stdin was {k0['kind']} {k0.get('path', '')}; replayed as an empty pipe")
    k1 = inv["fd1"]
    if not (k1["kind"] in ("fifo", "sock")):
        notes.append(f"stdout was {k1['kind']} {k1.get('path', '')}; captured via a pipe (our comparison)")
    return spec, notes, ok


def run_spec(argv, spec, cwd, env_overrides, timeout=15):
    """harness/common.run_capture, with the stdin given as in the test (bytes, file at offset, /dev/null, closed)."""
    from common import controlled_env, RunResult
    env = controlled_env()
    env.update(env_overrides or {})
    kw = {"cwd": cwd, "env": env, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "timeout": timeout}
    f = None
    if spec[0] == "bytes":
        kw["input"] = spec[1]
    elif spec[0] == "file":
        f = open(spec[1], "rb"); f.seek(spec[2]); kw["stdin"] = f
    elif spec[0] == "devnull":
        kw["stdin"] = subprocess.DEVNULL
    else:
        kw["stdin"] = subprocess.DEVNULL; kw["preexec_fn"] = lambda: os.close(0)
    try:
        p = subprocess.run(argv, **kw)
        return RunResult(p.stdout, p.stderr, p.returncode, False)
    except subprocess.TimeoutExpired as e:
        return RunResult(e.stdout or b"", e.stderr or b"", -1, True)
    finally:
        if f:
            f.close()


def classify_pair(o, s, oracle, sut):
    assess = not (o.timed_out or s.timed_out)
    oe, se = normalize_stderr(o.stderr, oracle, sut), normalize_stderr(s.stderr, oracle, sut)
    mism = assess and not (o.stdout == s.stdout and o.returncode == s.returncode)
    kind = None
    if mism:
        kind = "segv" if s.returncode in (-11, 139) else "panic" if s.returncode == 101 else "wrong"
    return {"assessable": assess, "stdout_match": o.stdout == s.stdout, "exit_match": o.returncode == s.returncode,
            "stderr_match": oe == se, "oracle_exit": o.returncode, "sut_exit": s.returncode,
            "mismatch": mism, "mismatch_kind": kind, "stderr_only": assess and not mism and oe != se,
            "oracle_stdout_len": len(o.stdout), "sut_stdout_len": len(s.stdout),
            "first_stdout_diff": next((i for i, (x, y) in enumerate(zip(o.stdout, s.stdout)) if x != y),
                                      min(len(o.stdout), len(s.stdout)) if len(o.stdout) != len(s.stdout) else -1)}


def replay_one(inv, oracle, sut, baseline):
    with tempfile.TemporaryDirectory() as wd:
        spec, notes, ok = materialize(inv, wd)
        if not ok:
            return {"replayable": False, "notes": notes}
        env = dict(inv.get("env", {}))
        env.pop("TZ", None)
        o = run_spec([oracle] + inv["argv"], spec, wd, env)
        o2 = run_spec([oracle] + inv["argv"], spec, wd, env)
        s = run_spec([sut] + inv["argv"], spec, wd, env)
        b = run_spec([baseline] + inv["argv"], spec, wd, env)
    v = classify_pair(o, s, oracle, sut)
    vb = classify_pair(o, b, oracle, baseline)
    det = (o.stdout == o2.stdout and o.returncode == o2.returncode)
    if not det:
        v["mismatch"] = False; v["mismatch_kind"] = None
        notes.append("oracle output differs between two runs on this input (nondeterministic input, e.g. a live /proc file): not counted")
    v.update({"replayable": True, "oracle_deterministic": det, "notes": notes,
              "baseline_rust_mismatch": vb["mismatch"] if det else False,
              "baseline_rust_mismatch_kind": vb["mismatch_kind"] if det else None,
              "oracle_stdout_head": o.stdout[:80].hex() if v["mismatch"] else None,
              "sut_stdout_head": s.stdout[:80].hex() if v["mismatch"] else None})
    return v


# ------------------------------------------------------------------------------------- report
def main():
    step = sys.argv[1] if len(sys.argv) > 1 else "all"
    binfo = build_all()
    acc = json.load(open(ACC))
    accepted = {f"{u}/{t}" for u, v in acc["per_util"].items() if u in UTILS
                for t, r in v.get("rows", {}).items() if r["start_pass"] == acc["runs_per_test"]}
    test_rc = trace(binfo) if step in ("all", "trace") else json.load(open(os.path.join(WORK, "test_rc.json")))
    json.dump(test_rc, open(os.path.join(WORK, "test_rc.json"), "w"))
    invs = load_invocations()
    # only invocations of the util under test (the shim is only on PATH for that util)
    by_test = {}
    for inv in invs:
        by_test.setdefault(f"{inv['util']}/{inv['test']}", []).append(inv)

    # replay through OUR comparison
    def rp(inv):
        u = inv["util"]
        return replay_one(inv, binfo["utils"][u]["oracle"], binfo["utils"][u]["sut"], binfo["utils"][u]["baseline"])
    with ThreadPoolExecutor(max_workers=int(os.environ.get("JOBS", "8"))) as ex:
        verdicts = list(ex.map(rp, invs))

    # our cases and the counted divergences
    our_keys, our_keys_env = {}, {}
    counted = []
    for u in UTILS:
        cases = json.load(open(os.path.join(REPO, "results", f"{u}_cases_v4.json")))["cases"]
        verd = {v["id"]: v for v in json.load(open(os.path.join(REPO, "results", f"{u}_diff_v4.json")))["verdicts"]}
        for c in cases:
            k = case_key(c)
            our_keys.setdefault(k, []).append(c["id"])
            our_keys_env.setdefault((k, json.dumps(c["env"], sort_keys=True)), []).append(c["id"])
            v = verd.get(c["id"])
            if v and v["mismatch"]:
                se = v["sut_exit"]
                counted.append({"id": c["id"], "util": u, "key": k,
                                "kind": "segv" if se in (-11, 139) else "panic" if se == 101 else "wrong"})
    inv_keys = {}
    for i, inv in enumerate(invs):
        inv_keys.setdefault(inv_key(inv), []).append(i)

    overlap = sorted(set(our_keys) & set(inv_keys), key=str)
    counted_inside = [c for c in counted if c["key"] in inv_keys]

    per_test = {}
    for name in sorted(set(test_rc) | set(by_test)):
        u, t = name.split("/", 1)
        idx = [i for i, inv in enumerate(invs) if inv["util"] == u and inv["test"] == t]
        vs = [verdicts[i] for i in idx]
        per_test[name] = {
            "in_acceptance_set": name in accepted,
            "test_exit_with_shim": test_rc.get(name),
            "acceptance_suite_wip_pass_runs": acc["per_util"][u]["rows"].get(t, {}).get("wip_pass"),
            "invocations": len(idx),
            "distinct_inputs": len({inv_key(invs[i]) for i in idx}),
            "replayed": sum(v["replayable"] for v in vs),
            "replay_mismatch": sum(bool(v.get("mismatch")) for v in vs),
            "replay_mismatch_kinds": sorted({v.get("mismatch_kind") for v in vs if v.get("mismatch")}),
            "replay_stderr_only": sum(bool(v.get("stderr_only")) for v in vs),
            "replay_mismatch_only_in_c2saferrust_output": sum(bool(v.get("mismatch")) and not v.get("baseline_rust_mismatch") for v in vs),
            "invocation_list": [{"argv": invs[i]["argv"], "env": invs[i]["env"], "fd0": invs[i]["fd0"]["kind"],
                                 "fd1": invs[i]["fd1"].get("path", invs[i]["fd1"]["kind"]),
                                 "stdin": invs[i].get("stdin"), "files": invs[i].get("files"),
                                 "replay": verdicts[i]} for i in idx],
        }

    def summ(names):
        idx = [i for i, inv in enumerate(invs) if f"{inv['util']}/{inv['test']}" in names]
        vs = [verdicts[i] for i in idx]
        return {"tests": len(names), "invocations": len(idx),
                "distinct_inputs": len({inv_key(invs[i]) for i in idx}),
                "replayed": sum(v["replayable"] for v in vs),
                "replay_mismatch": sum(bool(v.get("mismatch")) for v in vs),
                "replay_mismatch_by_kind": {k: sum(v.get("mismatch_kind") == k for v in vs) for k in ("wrong", "segv", "panic")},
                "replay_stderr_only": sum(bool(v.get("stderr_only")) for v in vs),
                "replay_oracle_nondeterministic": sum(v.get("oracle_deterministic") is False for v in vs),
                "replay_mismatch_also_in_starting_translation": sum(bool(v.get("mismatch")) and bool(v.get("baseline_rust_mismatch")) for v in vs),
                "replay_mismatch_only_in_c2saferrust_output": sum(bool(v.get("mismatch")) and not v.get("baseline_rust_mismatch") for v in vs),
                "tests_with_replay_mismatch": sorted({f"{invs[i]['util']}/{invs[i]['test']}" for i in idx
                                                      if verdicts[i].get("mismatch")})}

    all_tests = set(per_test)
    out = {
        "provenance": {
            "script": "harness/heldout_c2saferrust.py",
            "command": "docker run --rm -v \"$PWD:/work\" -w /work crust-heldout python3 -I harness/heldout_c2saferrust.py all",
            "git_rev": os.environ.get("GIT_REV"), "host": os.environ.get("HOST_DESC", platform.system()),
            "arch": platform.machine(), "image": os.environ.get("IMAGE_DESC"),
            "toolchains": binfo["tools"], "date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "sut": "coreutils/src/<util>/rust_WIP (cargo build --bin <util>, debug)",
            "baseline": "coreutils/src/<util>/rust (starting C2Rust translation), for attribution only",
            "oracle": "coreutils/src/<util>/c (make)",
            "procedure": "translate.py select_tests replicated (shebang exec from artifact root, timeout 25 s); "
                         "<code_dir>/target/debug replaced by a logging shim directory",
        },
        "definition_of_equal_input": "same util, identical argv list, identical stdin bytes, and for every argv "
                                     "element the same file kind/contents (sha256); locale reported separately",
        "system_inputs": {
            "all_sh_tests": summ(all_tests),
            "acceptance_set_tests": summ({n for n in all_tests if n in accepted}),
        },
        "our_inputs": {"cases": sum(len(v) for v in our_keys.values()), "distinct_inputs_ignoring_locale": len(our_keys)},
        "overlap": {
            "distinct_inputs_in_both": len(overlap),
            "items": [{"key": [str(x) for x in k], "our_case_ids": our_keys[k],
                       "tests": sorted({f"{invs[i]['util']}/{invs[i]['test']}" for i in inv_keys[k]})} for k in overlap],
        },
        "counted_defects": {
            "total": len(counted),
            "by_kind": {k: sum(c["kind"] == k for c in counted) for k in ("wrong", "segv", "panic")},
            "inside_system_input_set": len(counted_inside),
            "outside_system_input_set": len(counted) - len(counted_inside),
            "inside_ids": [c["id"] for c in counted_inside],
        },
        "per_test": per_test,
    }
    json.dump(out, open(os.environ.get("OUT", OUT), "w"), indent=1)
    print(json.dumps({k: out[k] for k in ("system_inputs", "our_inputs", "counted_defects")}, indent=1))
    print("overlap:", len(overlap))


if __name__ == "__main__":
    main()
