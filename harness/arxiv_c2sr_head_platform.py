"""arXiv check: C2SaferRust `head` on the container's native architecture.

The C2Rust starting tree (`rust/`) and C2SaferRust's output (`rust_WIP/`) declare their own `struct stat`
for fstat(). This script measures, on whatever arch it runs on, what that does to head's routing and to
the counted head defects. It uses the shipped pipeline's own pieces unchanged:

  build       harness/build.py: C `make`; `cargo build --bin head` (debug, rust-toolchain.toml pin) for
              rust_WIP (C2SR_VARIANT=rust_WIP) and the C2Rust baseline (C2SR_VARIANT=rust)
  layout      a C probe: sizeof/offsets of this arch's glibc struct stat next to the layout the Rust
              declares (head.rs struct stat), and the st_mode/st_size/st_blksize values the Rust's
              fields read after fstat() on a regular file, a pipe and a directory; plus, when strace is
              on PATH, the lseek() calls each binary makes for `head -n -1 <regular file>`
  grid        harness/difftest.run on results/head_cases_v4.json: C vs rust_WIP, and C vs rust (baseline);
              every stdout/exit mismatch mapped to a counted defect with the ledger's rule
              (scripts/build_defect_ledger.py c2sr_assign, head part) and compared with the shipped x86
              verdicts (results/head_diff_v4.json)
  acceptance  harness/acceptance_suite.py's build()/passes() (translate.py select_tests: shebang exec from
              the artifact root, 25 s timeout, pass = exit 0, RUNS runs) for tests/head/*.sh on rust/ and
              rust_WIP/
  own         the head invocations traced by harness/heldout_c2saferrust.py (results/heldout_c2saferrust.json)
              rebuilt from regenerated inputs (each checked against its recorded sha256) and replayed with
              heldout_c2saferrust.replay_one; plus the head-pos.sh check itself (what the next reader of
              the shared stdin sees)

  docker run --rm --cpus=3 -v "$PWD:/work" -w /work -e C2SR_ROOT=<private copy of artifacts/c2saferrust> \
      <crust-heldout image> python3 -I harness/arxiv_c2sr_head_platform.py run
writes the per-arch part file OUT (outside results/); `combine` merges the parts into
results/arxiv_c2saferrust_head_aarch64.json.
"""
import base64
import datetime
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "scripts"))
WORK = tempfile.mkdtemp(prefix="arxiv_c2sr_head_")
__import__("atexit").register(shutil.rmtree, WORK, True)
os.environ["HELDOUT_WORK"] = os.path.join(WORK, "heldout")
from common import run_capture, normalize_stderr  # noqa: E402
from build import build_oracle, build_sut, tool_versions  # noqa: E402
import difftest  # noqa: E402
import acceptance_suite  # noqa: E402
import heldout_c2saferrust as hc  # noqa: E402
from provenance import stamp  # noqa: E402

ROOT = os.environ.get("C2SR_ROOT", os.path.join(REPO, "artifacts", "c2saferrust"))
RUNS = int(os.environ.get("RUNS", "3"))
CASES = os.path.join(REPO, "results", "head_cases_v4.json")
X86_DIFF = os.path.join(REPO, "results", "head_diff_v4.json")
X86_BASE = os.path.join(REPO, "results", "head_diff_v4_c2rust_baseline.json")
HELDOUT = os.path.join(REPO, "results", "heldout_c2saferrust.json")
LEDGER = os.path.join(REPO, "results", "defect_ledger.json")
UB = os.path.join(REPO, "results", "oracle_ub_c2saferrust.json")


def sha(b):
    return hashlib.sha256(b).hexdigest()


def sh(cmd, cwd=None, inp=None, timeout=120):
    p = subprocess.run(cmd, shell=True, cwd=cwd, input=inp, capture_output=True, timeout=timeout,
                       executable="/bin/bash")
    return p.returncode, p.stdout, p.stderr


# ------------------------------------------------------------------------------------------- build
def build():
    """Binaries by name; a variant that does not build is None and its errors are recorded."""
    o = build_oracle("head")
    os.environ["C2SR_VARIANT"] = "rust_WIP"
    w = build_sut("head")
    werr = None
    if not w["ok"]:
        _, log = __import__("build")._run(["cargo", "build", "--bin", "head"], os.path.join(ROOT, "coreutils/src/head/rust_WIP"))
        errs = [l for l in log.splitlines() if l.startswith("error")]
        lines = log.splitlines()
        locs = []
        for i, l in enumerate(lines):
            if l.startswith("error["):
                nxt = next((x.strip() for x in lines[i + 1:i + 4] if x.strip().startswith("--> src/")), None)
                locs.append(nxt)
        found = [l.split("expected", 1)[1].strip() for l in log.splitlines() if "expected `" in l and "found `" in l]
        werr = {"errors": len([e for e in errs if not e.startswith("error: could not compile")]),
                "error_kinds": sorted(set(e.split(":")[0] for e in errs)), "error_locations": locs,
                "expected_found": sorted(set(found)), "summary_line": next((e for e in errs if e.startswith("error: could not compile")), None)}
    os.environ["C2SR_VARIANT"] = "rust"
    b = build_sut("head")
    os.environ["C2SR_VARIANT"] = "rust_WIP"
    if not (o["ok"] and b["ok"]):
        raise SystemExit(f"oracle/baseline build failed: {o.get('log_tail')} {b.get('log_tail')}")
    return {"tools": tool_versions(), "oracle": o["bin"], "rust_WIP": w["bin"] if w["ok"] else None,
            "rust": b["bin"], "rust_WIP_build_ok": w["ok"], "rust_WIP_build_errors": werr}


# ------------------------------------------------------------------------------------------ layout
PROBE_C = r'''
#include <stdio.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/stat.h>
#include <time.h>
/* the struct stat head.rs declares (rust/src/head.rs:186-203, rust_WIP/src/head.rs:213-230), field by field:
   __dev_t=c_ulong, __ino_t=c_ulong, __mode_t=c_uint, __nlink_t=c_uint, __uid_t/__gid_t=c_uint,
   __off_t=c_long, __blksize_t=c_int, __blkcnt_t=c_long */
struct rust_stat {
  unsigned long st_dev, st_ino; unsigned int st_mode, st_nlink, st_uid, st_gid;
  unsigned long st_rdev, __pad1; long st_size; int st_blksize, __pad2; long st_blocks;
  struct timespec st_atim, st_mtim, st_ctim; int __glibc_reserved[2];
};
static void show(const char *what, int fd) {
  unsigned char buf[512]; memset(buf, 0xAB, sizeof buf);
  struct stat st; fstat(fd, &st);
  fstat(fd, (struct stat *)buf);
  struct rust_stat *r = (struct rust_stat *)buf;
  int written = 0; for (int i = 0; i < (int)sizeof buf; i++) if (buf[i] != 0xAB) written = i + 1;
  unsigned m = r->st_mode;
  int usable = ((m & 0170000) == 0100000) || ((m & 0170000) == 0120000);
  printf("{\"input\":\"%s\",\"glibc_st_mode_octal\":\"%o\",\"glibc_st_nlink\":%lu,\"glibc_st_size\":%ld,\"glibc_st_blksize\":%ld,"
         "\"rust_field_st_mode_octal\":\"%o\",\"rust_field_st_size\":%ld,\"rust_field_st_blksize\":%d,"
         "\"rust_usable_st_size\":%s,\"bytes_written_by_fstat_at_least\":%d}\n",
         what, (unsigned)st.st_mode, (unsigned long)st.st_nlink, (long)st.st_size, (long)st.st_blksize,
         m, r->st_size, r->st_blksize, usable ? "true" : "false", written);
}
int main(int argc, char **argv) {
  printf("{\"glibc_sizeof_stat\":%zu,\"glibc_offsetof_st_mode\":%zu,\"glibc_offsetof_st_nlink\":%zu,"
         "\"glibc_sizeof_st_nlink\":%zu,\"glibc_offsetof_st_size\":%zu,\"glibc_offsetof_st_blksize\":%zu,"
         "\"rust_sizeof_stat\":%zu,\"rust_offsetof_st_mode\":%zu,\"rust_offsetof_st_nlink\":%zu,"
         "\"rust_offsetof_st_size\":%zu,\"rust_offsetof_st_blksize\":%zu}\n",
         sizeof(struct stat), offsetof(struct stat, st_mode), offsetof(struct stat, st_nlink),
         sizeof(((struct stat *)0)->st_nlink), offsetof(struct stat, st_size), offsetof(struct stat, st_blksize),
         sizeof(struct rust_stat), offsetof(struct rust_stat, st_mode), offsetof(struct rust_stat, st_nlink),
         offsetof(struct rust_stat, st_size), offsetof(struct rust_stat, st_blksize));
  int fd = open(argv[1], O_RDONLY); show("regular file", fd); close(fd);
  int p[2]; pipe(p); show("pipe", p[0]);
  fd = open(argv[2], O_RDONLY); show("directory", fd); close(fd);
  return 0;
}
'''


def layout(bins):
    d = os.path.join(WORK, "layout")
    os.makedirs(os.path.join(d, "adir"))
    open(os.path.join(d, "probe.c"), "w").write(PROBE_C)
    open(os.path.join(d, "reg"), "wb").write(b"a\nb\nc\n")
    rc, _, err = sh("gcc -O0 -o probe probe.c", d)
    if rc != 0:
        return {"error": err.decode()[-500:]}
    rc, out, _ = sh("./probe reg adir", d)
    lines = [json.loads(x) for x in out.decode().splitlines()]
    res = {"structs": lines[0], "fstat_reads": lines[1:]}
    if shutil.which("strace"):
        st = {}
        open(os.path.join(d, "seq"), "wb").write(b"".join(b"%d\n" % i for i in range(1, 70001)))
        for name in [n for n in ("oracle", "rust", "rust_WIP") if bins.get(n)]:
            for f in ("reg", "seq"):
                rc, out, err = sh(f"strace -f -e trace=lseek,fstat,newfstatat {bins[name]} -n -1 < {f} > /dev/null", d)
                calls = [x for x in err.decode().splitlines() if ("lseek(" in x or "fstat" in x) and "(0" in x]
                st[f"{name} head -n -1 < {f}"] = calls
        res["strace_fd0"] = st
    else:
        res["strace_fd0"] = "strace not on PATH"
    return res


# -------------------------------------------------------------------------------------------- grid
def assign_head(v, c):
    """scripts/build_defect_ledger.py c2sr_assign, head branch; returns (ids, kind, rule_ok)."""
    e = v["sut_exit"]
    kind = "crash-segv" if e in (-11, 139) else "crash-panic" if e == 101 else "S1"
    a, note = v["argv"], c["note"]
    if kind == "S1" and note == "two-files":
        return "C2SR-head-1", kind, (not v["stdout_match"] and v["exit_match"])
    if kind == "S1" and note == "is-a-directory":
        return "C2SR-head-2", kind, (v["stdout_match"] and v["oracle_exit"] == 1 and e == 0)
    if kind == "crash-panic":
        return "C2SR-head-3", kind, ("-n" in a and a[a.index("-n") + 1].startswith("-"))
    if kind == "crash-segv":
        return "C2SR-head-4", kind, ("-q" in a or "-z" in a)
    return None, kind, False


def grid(bins):
    util, cases = difftest.load_cases(CASES)
    cmap = {c.id: c.to_json() for c in cases}
    out = {}
    for variant in [v for v in ("rust_WIP", "rust") if bins.get(v)]:
        summary, verdicts = difftest.run(util, cases, bins["oracle"], bins[variant])
        mism = [v for v in verdicts if v["mismatch"]]
        by = {}
        for v in mism:
            did, kind, ok = assign_head(v, cmap[v["id"]])
            key = did or f"unassigned:{kind}:{cmap[v['id']]['note']}"
            b = by.setdefault(key, {"cases": 0, "kinds": {}, "rule_holds": 0, "ids": [],
                                    "by_flags_and_input_source": {}})
            b["cases"] += 1
            b["kinds"][kind] = b["kinds"].get(kind, 0) + 1
            b["rule_holds"] += bool(ok)
            b["ids"].append(v["id"])
            fk = f"head {' '.join(x for x in v['argv'] if x.startswith('-') or x.lstrip('-').isdigit() or x.startswith('+'))} [{cmap[v['id']]['note']}]"
            b["by_flags_and_input_source"][fk] = b["by_flags_and_input_source"].get(fk, 0) + 1
        out[variant] = {"summary": summary, "mismatch_by_defect": by,
                        "mismatch_detail": {v["id"]: {"argv": v["argv"], "note": cmap[v["id"]]["note"],
                                                      "oracle_exit": v["oracle_exit"], "sut_exit": v["sut_exit"],
                                                      "detail": v.get("detail")} for v in mism}}
        with open(os.path.join(WORK, f"grid_{variant}.json"), "w") as f:
            json.dump(verdicts, f)
    return out


# -------------------------------------------------------------------------------------- acceptance
def acceptance():
    tdir = os.path.join("coreutils", "tests", "head")
    base, wip = "coreutils/src/head/rust", "coreutils/src/head/rust_WIP"
    if not acceptance_suite.build(base, "head"):
        return {"error": "baseline build failed"}
    wip_ok = acceptance_suite.build(wip, "head")
    tests = sorted(t for t in os.listdir(os.path.join(ROOT, tdir)) if t.endswith(".sh"))
    rows = {}
    for t in tests:
        rel = os.path.join(tdir, t)
        b = [acceptance_suite.passes(rel, base) for _ in range(RUNS)]
        w = [acceptance_suite.passes(rel, wip) for _ in range(RUNS)] if wip_ok else None
        rows[t] = {"start_pass": sum(b), "wip_pass": sum(w) if w is not None else None}
    # one verbose run per test and variant, to record why a test fails
    why = {}
    for t in tests:
        for cd in ((base, wip) if wip_ok else (base,)):
            try:
                r = subprocess.run(f"{os.path.join(tdir, t)} {cd}/target/debug", shell=True, cwd=ROOT, timeout=25,
                                   capture_output=True, env=dict(os.environ, VERBOSE="yes"))
                why[f"{t} on {cd.split('/')[-1]}"] = {"rc": r.returncode, "stdout_tail": r.stdout.decode("latin1")[-600:],
                                                      "stderr_tail": r.stderr.decode("latin1")[-1200:]}
            except subprocess.TimeoutExpired:
                why[f"{t} on {cd.split('/')[-1]}"] = {"rc": "timeout"}
    selected = [t for t, r in rows.items() if r["start_pass"] == RUNS]
    return {"procedure": "harness/acceptance_suite.py build()/passes() (translate.py select_tests replicated), "
                         "tests/head/*.sh only", "runs_per_test": RUNS, "rows": rows,
            "rust_WIP_built": wip_ok, "selected_on_start": selected,
            "wip_fails_selected": [t for t in selected if wip_ok and rows[t]["wip_pass"] < RUNS],
            "single_run_output": why}


# ------------------------------------------------------------------------------------- own inputs
def candidates():
    """Inputs the head tests build, regenerated: sha256 -> bytes."""
    c = {}
    def add(b):
        c[sha(b)] = b
    add(b"abc\n"); add(b"abc\ndef\n"); add(b"a\nb\n"); add(b"0\n"); add(b"1\n")
    add(b"".join(b"%d\n" % i for i in range(1, 70001)))
    for n in (65536, 73728, 106496, 10510336, 10485760):
        add((b"y\n" * (n // 2 + 1))[:n])
    return c


def own(bins):
    held = json.load(open(HELDOUT))
    cand = candidates()
    blobs = os.path.join(hc.WORK, "log", "blobs")
    os.makedirs(blobs, exist_ok=True)
    for h, b in cand.items():
        open(os.path.join(blobs, h), "wb").write(b)
    rows = []
    for name in ("head/head-c.sh", "head/head-pos.sh", "head/head-write-error.sh"):
        for inv0 in held["per_test"][name]["invocation_list"]:
            need = []
            s = inv0.get("stdin")
            if s and s.get("kind") == "reg":
                need.append(s["sha256"])
            if s and s.get("kind") == "pipe":
                need.append(s["kept_sha256"])
            for a, info in (inv0.get("files") or {}).items():
                if not info.get("abs") and info.get("kind") == "reg":
                    need.append(info.get("sha256"))
            missing = [h for h in need if h not in cand]
            row = {"test": name, "argv": inv0["argv"],
                   "x86_shipped": {k: inv0["replay"].get(k) for k in ("mismatch", "mismatch_kind", "oracle_stdout_len",
                                                                      "sut_stdout_len", "sut_exit", "baseline_rust_mismatch")}}
            if missing:
                row["replayed"] = False
                row["why_not"] = f"input bytes not regenerable here (sha256 {missing}); e.g. a copy of the x86 host's /proc/version"
                rows.append(row)
                continue
            inv = {"util": "head", "test": name.split("/")[1], "argv": inv0["argv"], "env": inv0["env"],
                   "stdin": s, "files": inv0.get("files") or {},
                   "fd0": {"kind": inv0["fd0"]}, "fd1": {"kind": "other", "path": inv0["fd1"]}}
            r = hc.replay_one(inv, bins["oracle"], bins["rust_WIP"] or bins["rust"], bins["rust"])
            if not bins["rust_WIP"]:   # SUT slot held the baseline only to run the shipped replay code
                for k in ("mismatch", "mismatch_kind", "sut_stdout_len", "sut_exit", "first_stdout_diff"):
                    r[k] = None
            row.update({"replayed": True, "inputs_sha256_verified": need,
                        "here": {k: r.get(k) for k in ("mismatch", "mismatch_kind", "oracle_stdout_len", "sut_stdout_len",
                                                       "oracle_exit", "sut_exit", "baseline_rust_mismatch",
                                                       "baseline_rust_mismatch_kind", "first_stdout_diff", "notes")}})
            rows.append(row)
    # head-pos.sh's own check: what the next reader of the shared stdin sees
    d = os.path.join(WORK, "pos")
    os.makedirs(d)
    open(os.path.join(d, "in"), "wb").write(b"a\nb\n")
    open(os.path.join(d, "in2"), "wb").write(cand[sha(b"".join(b"%d\n" % i for i in range(1, 70001)))])
    pos = {}
    for label, cmd, expect in (("head -n -1 then cat (in = a,b)", "({B} -n -1 >/dev/null; cat) < in", b"b\n"),
                               ("head -n 1 then cat (in = a,b)", "({B} -n 1 >/dev/null; cat) < in", b"b\n"),
                               ("head -n-50000 then wc -l (in2 = seq 70000)", "({B} -n-50000 >/dev/null; wc -l) < in2", b"50000\n")):
        pos[label] = {"head-pos.sh expects": expect.decode()}
        for name in [n for n in ("oracle", "rust", "rust_WIP") if bins.get(n)]:
            rc, out, err = sh(cmd.format(B=bins[name]), d)
            pos[label][name] = {"rc": rc, "next_reader_stdout": out.decode("latin1")[:40],
                                "stderr_tail": err.decode("latin1")[-200:]}
    return {"replays": rows, "head_pos_next_reader": pos}


# ------------------------------------------------------------------------------------------- main
def run():
    out_path = os.environ["OUT"]   # a per-arch part file outside results/; `combine` writes the result file
    bins = build()
    res = {"_provenance": stamp(image=os.environ.get("CRUST_IMAGE")),
           "host": os.environ.get("HOST_DESC"), "image": os.environ.get("IMAGE_DESC"),
           "arch": platform.machine(), "c2sr_root": "private copy of artifacts/c2saferrust (git archive at the pinned commit: "
                                                   "coreutils/src/head/{c,rust,rust_WIP}, coreutils/tests, init.cfg, getlimits)",
           "patches_applied": (json.load(open(os.path.join(ROOT, "ARXIV_PATCHES.json")))
                               if os.path.exists(os.path.join(ROOT, "ARXIV_PATCHES.json")) else []),
           "date": datetime.datetime.now(datetime.timezone.utc).isoformat(), "build": bins}
    res["layout"] = layout(bins)
    print("layout done", flush=True)
    res["grid"] = grid(bins)
    print("grid done", flush=True)
    res["acceptance"] = acceptance()
    print("acceptance done", flush=True)
    res["own_inputs"] = own(bins)
    print("own done", flush=True)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    json.dump(res, open(out_path, "w"), indent=1)
    for v in ("rust_WIP", "rust"):
        if os.path.exists(os.path.join(WORK, f"grid_{v}.json")):
            shutil.copy(os.path.join(WORK, f"grid_{v}.json"), out_path + f".grid_{v}.verdicts.json")
    print(json.dumps({v: res["grid"][v]["summary"] for v in res["grid"]}, indent=1))
    shutil.rmtree(WORK, ignore_errors=True)


# ------------------------------------------------------------------------------------------ patch
X86_STAT = """pub struct stat {
    pub st_dev: __dev_t,
    pub st_ino: __ino_t,
    pub st_nlink: libc::c_ulong,
    pub st_mode: __mode_t,
    pub st_uid: __uid_t,
    pub st_gid: __gid_t,
    pub __pad2: libc::c_int,
    pub st_rdev: __dev_t,
    pub st_size: __off_t,
    pub st_blksize: __blksize_t,
    pub __pad1: libc::c_int,
    pub st_blocks: __blkcnt_t,
    pub st_atim: timespec,
    pub st_mtim: timespec,
    pub st_ctim: timespec,
    pub __glibc_reserved: [libc::c_int; 6],
}"""


def patch(kind):
    """Diagnostic patches, applied ONLY to a private copy (C2SR_ROOT), never to artifacts/c2saferrust.
    char      rust_WIP/src/{quotearg,head}.rs: `as i8` / `as *const i8` / `[i8; 8192]` -> libc::c_char (the
              rewrite hardcodes x86-64's signed c_char; on aarch64 c_char is u8 and the tree does not compile).
              Bit patterns are unchanged; the edited values are only stored, passed or compared for equality.
    stat-x86  rust/src/head.rs and rust_WIP/src/head.rs: the declared struct stat (aarch64/generic layout,
              128 bytes) replaced by the x86-64 glibc layout (144 bytes), keeping the field names and the
              field types the code uses (st_blksize as the low half, a 32-bit pad as the high half), and the
              two struct literals' `__glibc_reserved: [0; 2]` -> `[0; 6]`. Nothing else changes."""
    if os.path.realpath(ROOT) == os.path.realpath(os.path.join(REPO, "artifacts", "c2saferrust")):
        raise SystemExit("refusing to patch the shared artifact tree; set C2SR_ROOT to a private copy")
    rec_path = os.path.join(ROOT, "ARXIV_PATCHES.json")
    recs = json.load(open(rec_path)) if os.path.exists(rec_path) else []
    changed = []
    if kind == "char":
        for rel in ("coreutils/src/head/rust_WIP/src/quotearg.rs", "coreutils/src/head/rust_WIP/src/head.rs"):
            f = os.path.join(ROOT, rel)
            t = open(f).read()
            for old, new in ((" as *const i8", " as *const libc::c_char"), ("[i8; 8192]", "[libc::c_char; 8192]"),
                             (" as i8", " as libc::c_char")):
                n = t.count(old)
                if n:
                    t = t.replace(old, new)
                    changed.append({"file": rel, "replacements": n, "edit": f"'{old}' -> '{new}'"})
            open(f, "w").write(t)
    elif kind == "stat-x86":
        import re
        for v in ("rust", "rust_WIP"):
            f = os.path.join(ROOT, f"coreutils/src/head/{v}/src/head.rs")
            t = open(f).read()
            t2, n1 = re.subn(r"pub struct stat \{.*?\n\}", X86_STAT, t, count=1, flags=re.S)
            n2 = t2.count("__glibc_reserved: [0; 2]")
            t2 = t2.replace("__glibc_reserved: [0; 2]", "__glibc_reserved: [0; 6]")
            open(f, "w").write(t2)
            changed.append({"file": f"coreutils/src/head/{v}/src/head.rs", "struct_replaced": n1,
                            "literals_edited": n2, "new_struct": X86_STAT})
    else:
        raise SystemExit(f"unknown patch {kind}")
    recs.append({"patch": kind, "doc": " ".join(patch.__doc__.split()), "changes": changed})
    json.dump(recs, open(rec_path, "w"), indent=1)
    print(json.dumps(changed, indent=1))


# ---------------------------------------------------------------------------------------- combine
DEFECTS = ["C2SR-head-1", "C2SR-head-2", "C2SR-head-3", "C2SR-head-4", "C2SR-head-5"]
CODE_PATH = [
    "artifacts/c2saferrust/coreutils/src/head/rust/src/head.rs:122-131 (C2Rust type aliases: __nlink_t = c_uint, "
    "__blksize_t = c_int, the aarch64/asm-generic sizes; x86-64 glibc has 64-bit nlink_t and blksize_t)",
    "artifacts/c2saferrust/coreutils/src/head/rust/src/head.rs:186-203 (struct stat: st_mode at offset 16, size 128; "
    "x86-64 glibc: st_nlink at 16, st_mode at 24, size 144)",
    "artifacts/c2saferrust/coreutils/src/head/rust/src/head.rs:38 (extern fstat(fd, *mut stat) into that struct)",
    "artifacts/c2saferrust/coreutils/src/head/rust/src/head.rs:246-252 (usable_st_size tests st_mode & S_IFMT == S_IFREG)",
    "artifacts/c2saferrust/coreutils/src/head/rust/src/head.rs:1920 and 1965-1979 (head(): fstat; only if usable_st_size "
    "is current_pos set by elseek(fd, 0, SEEK_CUR); else it stays -1)",
    "artifacts/c2saferrust/coreutils/src/head/rust/src/head.rs:1682-1707 (elide_tail_lines_file: current_pos < 0 -> "
    "elide_tail_lines_pipe)",
    "artifacts/c2saferrust/coreutils/src/head/rust_WIP/src/head.rs:213-230, 65, 273-279, 1567, 1612-1626, 1359-1385 "
    "(the same struct, fstat, usable_st_size and routing, carried into C2SaferRust's output)",
    "artifacts/c2saferrust/coreutils/src/head/c/head.c:837-866 (head(): fstat, usable_st_size at :853, elseek) and "
    ":755-760 (elide_tail_lines_file: presume_input_pipe || current_pos < 0 || size <= ST_BLKSIZE -> pipe path)",
]


def _ids(by):
    return {d: set(v["ids"]) for d, v in by.items()}


def combine():
    import build_defect_ledger as bdl
    outdir = os.environ["PARTS"]
    parts = {}
    for n in ("aarch64_asshipped", "aarch64_charport", "x86_64_asshipped", "x86_64_statx86"):
        f = os.path.join(outdir, n + ".json")
        if os.path.exists(f):
            parts[n] = json.load(open(f))
    cases = {c["id"]: c for c in json.load(open(CASES))["cases"]}
    shipped = json.load(open(X86_DIFF))
    ubv = {v["id"]: v for v in json.load(open(UB))["verdicts"]}
    ship_ids = {}
    for v in shipped["verdicts"]:
        if v["mismatch"]:
            for d in bdl.c2sr_assign("head", v, cases[v["id"]])[0]:
                if bdl.clean_case_c2sr(ubv.get(v["id"])):
                    ship_ids.setdefault(d, set()).add(v["id"])
    ledger = {r["defect_id"]: r for r in json.load(open(LEDGER))["rows"] if r["defect_id"].startswith("C2SR-head")}

    runs, grid, acc, own_, vs = {}, {}, {}, {}, {}
    for n, p in parts.items():
        runs[n] = {"_provenance": p["_provenance"], "host": p["host"], "image": p["image"], "arch": p["arch"],
                   "date": p["date"], "patches_applied": p.get("patches_applied", []),
                   "toolchains": p["build"]["tools"], "rust_WIP_build_ok": p["build"].get("rust_WIP_build_ok", True),
                   "rust_WIP_build_errors": p["build"].get("rust_WIP_build_errors")}
        grid[n] = {}
        for var, g in p["grid"].items():
            grid[n][var] = {"summary": g["summary"],
                            "mismatch_by_defect": {d: {k: x[k] for k in ("cases", "kinds", "rule_holds", "by_flags_and_input_source")}
                                                   for d, x in g["mismatch_by_defect"].items()}}
        if "rust_WIP" in p["grid"]:
            here = _ids(p["grid"]["rust_WIP"]["mismatch_by_defect"])
            all_here = set().union(*here.values()) if here else set()
            cmp_ = {}
            for d in sorted(set(ship_ids) | set(here)):
                a, b = ship_ids.get(d, set()), here.get(d, set())
                cmp_[d] = {"shipped_x86_ub_clean_cases": len(a), "here_cases": len(b), "in_both": len(a & b),
                           "only_shipped": len(a - b), "only_here": len(b - a),
                           "only_shipped_now_agree": len((a - b) - all_here),
                           "only_shipped_now_other_defect": sorted({e for e, ids in here.items() if e != d and (a - b) & ids}),
                           "only_shipped_examples": [{"id": i, "argv": cases[i]["argv"], "note": cases[i]["note"]}
                                                     for i in sorted(a - b)[:4]],
                           "only_here_examples": [{"id": i, "argv": cases[i]["argv"], "note": cases[i]["note"],
                                                   "here": p["grid"]["rust_WIP"]["mismatch_detail"][i]} for i in sorted(b - a)[:4]],
                           "only_here_with_shipped_ub_clean_verdict": sum(bdl.clean_case_c2sr(ubv.get(i)) for i in b - a)}
            vs[n] = cmp_
        acc[n] = {k: p["acceptance"].get(k) for k in ("rows", "rust_WIP_built", "selected_on_start", "wip_fails_selected", "error")}
        acc[n]["single_run_failures"] = {k: v for k, v in p["acceptance"].get("single_run_output", {}).items()
                                         if v.get("rc") not in (0,)}
        own_[n] = {"replays": [{"test": r["test"], "argv": r["argv"], "replayed": r["replayed"],
                                "x86_shipped": r["x86_shipped"], "here": r.get("here"), "why_not": r.get("why_not")}
                               for r in p["own_inputs"]["replays"]],
                   "head_pos_next_reader": p["own_inputs"]["head_pos_next_reader"]}
    layout = {n: p["layout"] for n, p in parts.items()}

    def present(n, d):
        p = parts.get(n)
        if not p or "rust_WIP" not in p["grid"]:
            return None
        if d == "C2SR-head-5":
            hits = [r for r in p["own_inputs"]["replays"] if r.get("here") and r["here"].get("mismatch")
                    and r["argv"][0].startswith(("-n-", "--lines=-")) and r["argv"] != ["-n", "-1"]]
            rows = [{"test": r["test"], "argv": r["argv"], "oracle_stdout_len": r["here"]["oracle_stdout_len"],
                     "sut_stdout_len": r["here"]["sut_stdout_len"], "oracle_exit": r["here"]["oracle_exit"],
                     "sut_exit": r["here"]["sut_exit"], "baseline_mismatch": r["here"]["baseline_rust_mismatch"]} for r in hits]
            return {"own_invocations_with_truncated_stdout (this defect)":
                        [x for x in rows if x["sut_stdout_len"] < x["oracle_stdout_len"]],
                    "own_invocations_with_equal_stdout_but_exit_1 (not this defect: elide_tail_lines_seekable, see new_divergence_with_correct_routing)":
                        [x for x in rows if x["sut_stdout_len"] == x["oracle_stdout_len"]]}
        g = p["grid"]["rust_WIP"]["mismatch_by_defect"].get(d)
        return {"grid_cases": g["cases"] if g else 0, "kinds": g["kinds"] if g else {},
                "by_flags_and_input_source": g["by_flags_and_input_source"] if g else {}}

    per_defect = {}
    for d in DEFECTS:
        per_defect[d] = {"ledger_mechanism": ledger[d]["mechanism"], "ledger_exposure": ledger[d]["exposure_count"],
                         "ledger_introduced_by": ledger[d].get("introduced_by"),
                         "by_run": {n: present(n, d) for n in parts},
                         "conclusion": CONCLUSIONS.get(d)}
    out = {
        "_provenance": stamp(),
        "question": "Issue 11 / review A.12: the C2Rust starting tree C2SaferRust rewrites declares an aarch64 struct "
                    "stat, so on x86-64 regular files take head's pipe path. Which counted head defects depend on that "
                    "routing (inherited) and which are introduced by the rewrite?",
        "script": "harness/arxiv_c2sr_head_platform.py (run per platform; `patch char` / `patch stat-x86` on a private "
                  "copy; `combine` writes this file)",
        "runs_explained": {
            "aarch64_asshipped": "aarch64, shipped trees unchanged (rust_WIP does not compile; C and C2Rust baseline only)",
            "aarch64_charport": "aarch64, rust_WIP with the `char` type-only port (i8 -> libc::c_char), C2Rust baseline unchanged",
            "x86_64_asshipped": "x86-64 (the shipped platform), shipped trees unchanged: reproduces the shipped numbers",
            "x86_64_statx86": "x86-64, struct stat in rust/ and rust_WIP/ head.rs replaced by the x86-64 layout, nothing else",
        },
        "runs": runs,
        "routing_claim": {"verdict": CONCLUSIONS.get("routing_verdict"), "code_path": CODE_PATH, "layout_probe": layout},
        "grid": grid,
        "grid_rust_WIP_vs_shipped_x86_ub_clean": vs,
        "acceptance": acc,
        "own_inputs": own_,
        "per_counted_defect": per_defect,
        "summary": CONCLUSIONS.get("summary"),
        "new_divergence_with_correct_routing": CONCLUSIONS.get("new_divergence_with_correct_routing"),
        "acceptance_with_correct_routing": CONCLUSIONS.get("acceptance_with_correct_routing"),
        "layout_probe_note": "layout_probe.*.structs.rust_* mirror the struct as shipped (rust/src/head.rs:186-203) in "
                             "every run, including x86_64_statx86; that run's strace shows the patched binaries.",
        "text_affected": CONCLUSIONS.get("text_affected"),
        "limits": CONCLUSIONS.get("limits"),
    }
    path = os.environ.get("COMBINED_OUT") or os.path.join(REPO, "results", "arxiv_c2saferrust_head_aarch64.json")
    if os.path.exists(path) and os.environ.get("OVERWRITE") != "1":
        raise SystemExit(f"{path} exists")
    json.dump(out, open(path, "w"), indent=1, default=sorted)
    print(json.dumps({d: per_defect[d]["by_run"] for d in DEFECTS}, indent=1, default=sorted)[:6000])


CONCLUSIONS = {
    "routing_verdict": (
        "Confirmed. The struct stat that the C2Rust tree declares (rust/src/head.rs:186-203, with __nlink_t = c_uint and "
        "__blksize_t = c_int at :122-131) is the aarch64 glibc layout: on aarch64 the probe finds the same size (128) and "
        "offsets as glibc. On x86-64, glibc's struct stat is 144 bytes with st_nlink (8 bytes) at offset 16 and st_mode at "
        "24, so after fstat() the Rust field st_mode (offset 16) reads st_nlink (1 for a regular file; probe: "
        "rust_field_st_mode_octal 1), usable_st_size() (rust/src/head.rs:246-252) is false, head() leaves current_pos at -1 "
        "(rust/src/head.rs:1965-1979) and elide_tail_lines_file() sends the file to elide_tail_lines_pipe() "
        "(rust/src/head.rs:1690-1704). strace on x86-64: neither rust/ nor rust_WIP/ makes any lseek() on a regular-file "
        "stdin for `head -n -1` (the C does lseek(0,0,SEEK_CUR) and seeks back); on aarch64, and on x86-64 with the struct "
        "replaced by the x86-64 layout, both make the same lseek() calls as the C. fstat() also writes 144 bytes into the "
        "128-byte struct on x86-64 (16 bytes past it on the stack). C2SaferRust's output keeps the same struct, fstat "
        "declaration, usable_st_size and routing (rust_WIP/src/head.rs:213-230, 65, 273-279, 1612-1626, 1367-1381), so "
        "the routing is inherited from the C2Rust tree, not introduced by the rewrite."),
    "summary": (
        "rust_WIP as shipped does not compile on aarch64 (22 E0308 errors, all in its rewritten quotearg.rs: it hardcodes "
        "i8 where aarch64's c_char is u8), so the aarch64 run of C2SaferRust's output uses a type-only port "
        "(25 lines: i8 -> libc::c_char, bit patterns unchanged). The same measurement on x86-64 with only the struct stat "
        "corrected (x86_64_statx86) gives identical results to aarch64 (561 grid mismatches, same per-defect splits, same "
        "acceptance and own-input outcomes), and x86-64 as shipped reproduces the shipped numbers (566; 83/169/80/234). "
        "So every x86/aarch64 difference comes from the routing. The C2Rust baseline agrees with the C on stdout and exit "
        "code on all 1404 head grid cases on aarch64 (676 stderr-only differences, the doubled error messages, as on "
        "x86-64), passes all three head .sh tests 3/3, agrees on every replayed own-test invocation and positions the "
        "shared stdin like the C. All five counted head defects are in code the rewrite changed and the baseline is "
        "correct under both routings, so all five stay introduced by the rewrite; what the routing changes is which "
        "inputs reach head-3/head-5's code and how it fails."),
    "C2SR-head-1": "Present unchanged with correct routing (the same 83 cases). Introduced by the rewrite (write_header). The 26 extra two-files cases the ledger's rule would assign to head-1 on aarch64 are head -n -1/-2 on two small files, which the head-3/head-5 final-buffer block truncates (they were head-3 panics on x86-64).",
    "C2SR-head-2": "Present unchanged (169 cases). Introduced by the rewrite (safe_read). Independent of routing.",
    "C2SR-head-3": "The rewritten block is still reached and still wrong, so the defect stays introduced by the rewrite (the baseline uses wrapping_add and agrees under both routings). But 62 of its 80 shipped exposures exist only because of the inherited routing: with correct routing only the 18 real-pipe (stdin) cases panic; of the 62 regular-file cases (18 file, 18 ok+missing, 26 two-files), 56 give a wrong stdout (files of at most 4096 bytes still take elide_tail_lines_pipe, now with current_pos 0, so the checked add does not overflow and the block writes one byte per line of the final buffer), 1 differs only in exit code (a 9006-byte file, which now takes elide_tail_lines_seekable; see new_divergence_with_correct_routing) and 5 agree. Its own-input mapping (head-pos.sh -n -1: panic) also becomes a wrong output.",
    "C2SR-head-4": "Present unchanged (234 cases). Introduced by the rewrite (main_0 getopt). Independent of routing.",
    "C2SR-head-5": "Present with correct routing on the pipe case of its own acceptance test (head-write-error.sh `yes | head --lines=-1 > /dev/full`: 65534 B from the C, 61439 B from the Rust here; 63487 B in the shipped replay, so the shortfall depends on how the pipe reads are chunked). Introduced by the rewrite (elide_tail_lines_pipe final block, log.txt:159). Its two regular-file exposures (bigseek --lines=-1: 4,095 B lost; head-pos.sh -n-50000: 1,998 B lost) exist only because of the inherited routing: with correct routing those files take elide_tail_lines_seekable, whose stdout is correct (but see the exit-1 divergence below).",
    "new_divergence_with_correct_routing": (
        "Not in the ledger and not reachable on x86-64 as shipped: rust_WIP's rewritten elide_tail_lines_seekable returns "
        "`elseek(...) <= 0` (rust_WIP/src/head.rs:1333) where the C returns `0 <= elseek(...)` (c/head.c:721; the baseline "
        "has it right), so `head -n -N` on a regular file larger than one 4096-byte block prints the right bytes but "
        "exits 1 with no message. Seen on aarch64 and on x86_64_statx86: 2 grid cases (head-00751, exit only; head-00754, "
        "whose second, small file is also truncated; both with a 9006-byte in1.txt) and the own "
        "invocations head-pos.sh -n-50000, head-write-error.sh --lines=-0 bigseek and --lines=-1 bigseek; "
        "head-write-error.sh expects exit 1 there anyway (/dev/full). log.txt:161-162 record that function's chunks 0 "
        "and 1 as Success."),
    "acceptance_with_correct_routing": (
        "On aarch64 (and x86_64_statx86) head-pos.sh passes on the C2Rust starting tree (3/3), so the select_tests "
        "procedure selects 3 head tests instead of 2, and C2SaferRust's output fails head-pos.sh 3/3 (head -n -1 on a "
        "4-byte file prints 'a' and leaves the offset at 1; the next reader sees '\\nb\\n'). On x86-64 as shipped head-pos.sh "
        "fails on the starting tree (the C2Rust routing leaves the shared offset at EOF), so it is not selected."),
    "text_affected": [
        "Paper results.tex:92-93 ('head drops a final partial buffer, losing 4,095 bytes of its own acceptance test's file'): "
        "the 4,095 bytes and 'file' come from the bigseek regular file, which reaches the faulty block only through the "
        "routing inherited from the C2Rust tree on x86-64. The routing-independent instance is the same test's piped "
        "input (yes | head --lines=-1 > /dev/full), which loses 2,047 B (shipped replay) or 4,095 B (these re-runs).",
        "Ledger C2SR-head-5 mechanism: 'The rewrite also routes regular files to this pipe path (current_pos -1)' "
        "attributes the routing to the rewrite; it is the C2Rust tree's aarch64 struct stat read on x86-64. Its example "
        "(bigseek, 10485758 vs 10481663 B) and exposure entries for bigseek and head-pos.sh -n-50000 hold only under "
        "that routing; the head-write-error.sh pipe entry holds under both.",
        "scripts/build_defect_ledger.py C2SR_OWN_MAP head-write-error.sh justification ('the 10 MiB regular file, which "
        "the rewrite also routes to elide_tail_lines_pipe') and the head-pos.sh -n -1 -> C2SR-head-3 panic entry: same "
        "routing dependence.",
        "Ledger C2SR-head-3: exposure 80 (all panics) is 18 panics under correct routing (pipes); the other 62 are "
        "regular files routed to the pipe path by the inherited struct stat (under correct routing 56 wrong stdout, "
        "1 exit only, 5 agree).",
        "Unchanged: every head row's introduced_by (the baseline agrees with the C under both routings), the five counted "
        "head defects, and 'Four defects crash' (head-3 still panics on pipes).",
        "Study notes (not the paper): systems/c2saferrust/notes.md 'head -n -N leaves a shared file position in the wrong "
        "place' and docs/reviews/c2saferrust-ablation-triage.md head-pos.sh 'REAL S1': on x86-64 that is the C2Rust "
        "tree's routing (the baseline mis-positions too); on aarch64 the baseline positions like the C.",
    ],
    "limits": [
        "The aarch64 run of rust_WIP needs the type-only port; the as-shipped tree does not compile there.",
        "The UB filter was not re-run on aarch64; the 57 regular-file cases that still diverge (no longer as panics) all carry a UB-clean "
        "x86-64 verdict in results/oracle_ub_c2saferrust.json.",
        "One head-c.sh invocation (a copy of the x86 host's /proc/version) cannot be regenerated and was not replayed.",
        "Only head was re-run; cat, tail, uniq declare their own structs and were not checked here.",
    ],
}


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else "run"
    if step == "run":
        run()
    elif step == "patch":
        patch(sys.argv[2])
    elif step == "combine":
        combine()
    else:
        raise SystemExit(f"unknown step {step}")
