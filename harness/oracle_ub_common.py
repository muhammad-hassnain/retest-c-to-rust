"""Shared helpers for the oracle-UB validation (audit M3): is the C oracle well-defined on every
input behind a counted divergence?

Used by harness/oracle_ub_{c2saferrust,rustassure,rustmap_bzip2,rustmap_rosetta,flourine}.py.
One sanitizer configuration for every system, so the verdicts are comparable:

  * ASan + UBSan + float-cast-overflow, -O0 -g, -fno-sanitize-recover=all (first report aborts),
    built TWICE: clang-14 and gcc-11 (Ubuntu 22.04, the crust-heldout image).
    clang is built non-PIE (-fno-pie / -no-pie): on the 7.0 kernel of the x86 box, clang-14 ASan
    on a PIE binary dies at start-up with an empty SIGSEGV in ~25% of runs (76 of 300 in our test;
    0 of 300 non-PIE, 0 of 300 gcc). A start-up SIGSEGV with no sanitizer output is retried and,
    if it persists, reported as "infra" -- never as UB.
  * ASAN_OPTIONS=detect_leaks=0: a leak is not undefined behaviour (coreutils exits without
    freeing on purpose); LeakSanitizer would otherwise turn exit codes non-zero.
  * The optimization-level cross-check of the VERT harness: the same C at another -O level must
    give byte-identical stdout, exit code and (path-normalised) stderr.
  * Implementation-defined toggles: -funsigned-char and -fsigned-char.

One failure semantics for every script (audit 8, `not_clean_reasons`): an input is UB-clean only if
every sanitizer, optimization-level and char-signedness run of the C on it ran to completion and
agrees with the reference build. A run that reports, fails to start (sanitizer "infra"), times out,
or prints a different stdout / exit code leaves the input NOT UB-clean (never counted, never UB).
"""
import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SAN_COMMON = ["-O0", "-g", "-fno-omit-frame-pointer",
              "-fsanitize=address,undefined,float-cast-overflow", "-fno-sanitize-recover=all"]
SAN_LINK = ["-fsanitize=address,undefined,float-cast-overflow"]
CLANG_NOPIE_CC = ["-fno-pie"]
CLANG_NOPIE_LD = ["-no-pie"]

SAN_ENV = {
    "ASAN_OPTIONS": "detect_leaks=0:exitcode=86:abort_on_error=0:handle_abort=1",
    "UBSAN_OPTIONS": "print_stacktrace=1:halt_on_error=1:exitcode=87",
}

# A sanitizer REPORT (the thing that reclassifies a case as original-C UB).
SAN_RE = re.compile(rb"runtime error:|ERROR: AddressSanitizer|SUMMARY: AddressSanitizer|"
                    rb"SUMMARY: UndefinedBehaviorSanitizer|AddressSanitizer:DEADLYSIGNAL")
# Sanitizer runtime that never got to run the program.
INFRA_RE = re.compile(rb"out of application range|can ?not mmap|Shadow memory range interleaves|"
                      rb"FATAL: (Code|MemorySanitizer|AddressSanitizer)")


def san_cflags(cc):
    return SAN_COMMON + (CLANG_NOPIE_CC if cc == "clang" else [])


def san_ldflags(cc):
    return SAN_LINK + (CLANG_NOPIE_LD if cc == "clang" else [])


# clang's UBSan pointer-overflow check also flags NULL + 0, which ISO C11 leaves undefined (6.5.6p8)
# but C2y (N3322) and C++ define; gcc does not flag it. It fires at the first line read of every
# gnulib-linebuffer program, so it is tracked separately (variant *_nz: that one check made
# recoverable, so the run continues and every OTHER report is still seen).
NULL_ZERO_RE = re.compile(rb"applying zero offset to null pointer")
NZ_RECOVER = ["-fsanitize-recover=pointer-overflow"]


def san_reports_all(stderr: bytes, limit=20):
    """All distinct sanitizer report lines (str), in order."""
    seen = []
    for line in (stderr or b"").splitlines():
        if SAN_RE.search(line) and b"DEADLYSIGNAL" not in line:
            t = line.decode("utf-8", "replace").strip()[:300]
            if t not in seen:
                seen.append(t)
                if len(seen) >= limit:
                    break
    return seen


def other_than_null_zero(reports):
    """Reports other than the NULL+0 pointer-overflow one. SUMMARY lines are dropped: every
    UBSan finding has its own 'runtime error:' line and every ASan finding its 'ERROR:' line."""
    return [r for r in reports if not NULL_ZERO_RE.search(r.encode()) and not r.startswith("SUMMARY:")]


def san_report(stderr: bytes):
    """First sanitizer report line (str) or None."""
    if not SAN_RE.search(stderr or b""):
        return None
    for line in stderr.splitlines():
        if SAN_RE.search(line) and b"DEADLYSIGNAL" not in line:
            return line.decode("utf-8", "replace").strip()[:300]
    return stderr.decode("utf-8", "replace").strip().splitlines()[0][:300]


def is_startup_infra(rc, stderr):
    """A crash before the sanitizer could say anything (or an explicit runtime start-up FATAL)."""
    if INFRA_RE.search(stderr or b""):
        return True
    return rc in (-11, 139) and not (stderr or b"").strip()


def run_proc(argv, stdin=b"", cwd=None, env=None, timeout=15):
    """(stdout, stderr, rc, timed_out). Never raises on non-zero exit."""
    try:
        p = subprocess.run(argv, input=stdin, cwd=cwd, env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout)
        return p.stdout, p.stderr, p.returncode, False
    except subprocess.TimeoutExpired as e:
        return e.stdout or b"", e.stderr or b"", -1, True


def run_san(argv, stdin=b"", cwd=None, env=None, timeout=60, retries=6, halt=True):
    """Run a sanitizer build; retry start-up infra crashes. Returns dict with
    stdout/stderr/rc/timed_out/report/reports/infra. halt=False lets checks that were COMPILED
    recoverable (the *_nz variants) continue; non-recoverable checks still abort."""
    e = dict(env or os.environ)
    e.update(SAN_ENV)
    if not halt:
        e["UBSAN_OPTIONS"] = e["UBSAN_OPTIONS"].replace("halt_on_error=1", "halt_on_error=0")
    last = None
    for _ in range(retries):
        out, err, rc, to = run_proc(argv, stdin, cwd, e, timeout)
        last = (out, err, rc, to)
        if not to and is_startup_infra(rc, err):
            continue
        return {"stdout": out, "stderr": err, "rc": rc, "timed_out": to,
                "report": san_report(err), "reports": san_reports_all(err), "infra": False}
    out, err, rc, to = last
    return {"stdout": out, "stderr": err, "rc": rc, "timed_out": to, "report": None, "reports": [],
            "infra": True}


def not_clean_reasons(name, r, same_as_ref):
    """The uniform rule for ONE sanitizer / opt-level / char-signedness run `r` of the C on one input
    (`r` as returned by run_san / run_proc dicts; same_as_ref: does its output agree with the
    reference build). Returns [] when the run leaves the input UB-clean, else one reason string.
    Order: a sanitizer report is UB evidence; infra, timeout and disagreement are 'not clean'."""
    if r.get("report"):
        return [f"{name}: {r['report']}"]
    if r.get("infra"):
        return [f"{name}: run failed (sanitizer runtime did not start)"]
    if r.get("timed_out"):
        return [f"{name}: timed out"]
    if not same_as_ref:
        return [f"{name}: output differs from the reference build"]
    return []


def tool_version(argv):
    try:
        out = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True).stdout
        return out.strip().splitlines()[0] if out.strip() else "?"
    except (FileNotFoundError, OSError):
        return "ABSENT"


def toolchains():
    return {"gcc": tool_version(["gcc", "--version"]),
            "clang": tool_version(["clang", "--version"]),
            "rustc_in_this_container": tool_version(["rustc", "--version"]),
            "cargo_in_this_container": tool_version(["cargo", "--version"]),
            "python": sys.version.split()[0]}


def git_rev():
    if os.environ.get("OUB_GIT_REV"):
        return os.environ["OUB_GIT_REV"]
    try:
        return subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True).stdout.strip() or "?"
    except OSError:
        return "?"


def provenance(script, extra=None):
    p = {
        "script": os.path.relpath(os.path.abspath(script), REPO),
        "command": os.environ.get("OUB_COMMAND") or " ".join([sys.executable] + sys.argv),
        "git_rev": git_rev(),
        "git_worktree_note": os.environ.get("OUB_WORKTREE_NOTE",
                                            "study repo working tree copied (rsync, artifacts excluded) "
                                            "to the run host; new oracle_ub_* files uncommitted"),
        "host": os.environ.get("OUB_HOST", platform.system()),
        "arch": platform.machine(),
        "kernel": platform.release(),
        "docker_image": os.environ.get("OUB_IMAGE", "?"),
        "docker_image_id": os.environ.get("OUB_IMAGE_ID", "?"),
        "toolchains": toolchains(),
        "sanitizer_cflags": {"clang": san_cflags("clang"), "gcc": san_cflags("gcc")},
        "sanitizer_env": SAN_ENV,
        "date": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    if extra:
        p.update(extra)
    return p


SRC_EXT = (".c", ".h", ".rs", ".toml", ".lock", ".py", ".sh", ".in", ".def")
SRC_NAMES = ("Makefile", "makefile")


def tree_digest(path):
    """sha256 over (relpath, sha256(bytes)) of the SOURCE files under path (build products such as
    target/, *.o and binaries excluded). Used to show the artifact copy we built from is identical
    to the Mac copy."""
    h = hashlib.sha256()
    n = 0
    if os.path.isfile(path):
        with open(path, "rb") as f:
            return {"files": 1, "sha256": hashlib.sha256(f.read()).hexdigest()}
    for dp, dns, fns in os.walk(path):
        dns[:] = sorted(d for d in dns if d not in ("target", "__pycache__", ".git"))
        for fn in sorted(fns):
            if not (fn.endswith(SRC_EXT) or fn in SRC_NAMES):
                continue
            full = os.path.join(dp, fn)
            if os.path.islink(full):
                continue
            with open(full, "rb") as f:
                fh = hashlib.sha256(f.read()).hexdigest()
            h.update(os.path.relpath(full, path).encode() + b"\0" + fh.encode() + b"\n")
            n += 1
    return {"files": n, "sha256": h.hexdigest()}


def copy_sources(src, dst):
    """Copy a source tree to a scratch build dir, leaving out build products, so the artifact
    tree itself is never written to."""
    if os.path.isdir(dst):
        shutil.rmtree(dst)

    def ignore(d, names):
        return [n for n in names if n in ("target", "__pycache__") or n.endswith(".o")]
    shutil.copytree(src, dst, ignore=ignore, symlinks=True)


CONTROLS = {
    # name: (C source, argv) -- each has exactly one UB on the given input; the detector must fire
    "heap_oob_read": ("#include <stdlib.h>\nint main(int c,char**v){int*a=malloc(4*sizeof(int));"
                      "int r=a[atoi(v[1])];free(a);return r&1;}\n", ["4"]),
    "stack_oob_read": ("#include <stdlib.h>\nint main(int c,char**v){int a[4]={0};"
                       "return a[atoi(v[1])]&1;}\n", ["5"]),
    "signed_overflow": ("#include <stdlib.h>\n#include <limits.h>\nint main(int c,char**v){int x=INT_MAX;"
                        "x+=atoi(v[1]);return x&1;}\n", ["1"]),
    "shift_overflow": ("#include <stdlib.h>\nint main(int c,char**v){int x=atoi(v[1]);"
                       "return (x<<24)&1;}\n", ["200"]),
    "float_cast_overflow": ("#include <stdlib.h>\nint main(int c,char**v){double d=atof(v[1]);"
                            "int i=(int)d;return i&1;}\n", ["1e30"]),
    "clean": ("#include <stdlib.h>\nint main(int c,char**v){return atoi(v[1])&0;}\n", ["1"]),
}


def positive_controls(workdir):
    """Build each control with the exact sanitizer flags, for clang and gcc; record whether the
    detector fired. Expected: fires on every control except 'clean'."""
    os.makedirs(workdir, exist_ok=True)
    res = {}
    for cc in ("clang", "gcc"):
        if not shutil.which(cc):
            res[cc] = "ABSENT"
            continue
        r = {}
        for name, (src, argv) in CONTROLS.items():
            c = os.path.join(workdir, f"{name}.c")
            b = os.path.join(workdir, f"{name}_{cc}")
            with open(c, "w") as f:
                f.write(src)
            p = subprocess.run([cc] + san_cflags(cc) + [c, "-o", b] + san_ldflags(cc),
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if p.returncode != 0:
                r[name] = "build_fail"
                continue
            rr = run_san([b] + argv)
            r[name] = "fired" if rr["report"] else ("infra" if rr["infra"] else "clean")
        r["all_as_expected"] = all(v == "fired" for k, v in r.items() if k != "clean") and r.get("clean") == "clean"
        # Does float->int overflow fire under the VERT harness's flags (-fsanitize=address,undefined
        # with no explicit float-cast-overflow)? gcc leaves the check out of `undefined`.
        src, argv = CONTROLS["float_cast_overflow"]
        c = os.path.join(workdir, "fco_plain.c")
        b = os.path.join(workdir, f"fco_plain_{cc}")
        with open(c, "w") as f:
            f.write(src)
        extra = CLANG_NOPIE_CC + CLANG_NOPIE_LD if cc == "clang" else []
        p = subprocess.run([cc, "-O0", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"] + extra
                           + [c, "-o", b], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if p.returncode == 0:
            rr = run_san([b] + argv)
            r["float_cast_overflow_with_only_-fsanitize=address,undefined"] = (
                "fired" if rr["report"] else ("infra" if rr["infra"] else "clean"))
        res[cc] = r
    return res


def dump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=1)
        f.write("\n")


if __name__ == "__main__":
    # `python3 harness/oracle_ub_common.py digest <path>...` prints the source digests (used to
    # compare the Mac and Linux artifact copies).
    if len(sys.argv) > 2 and sys.argv[1] == "digest":
        print(json.dumps({p: tree_digest(p) for p in sys.argv[2:]}, indent=1))
