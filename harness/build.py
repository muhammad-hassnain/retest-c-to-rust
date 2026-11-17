"""Build the ORACLE (original C) and the SUT (translated Rust) for a coreutils util.

Oracle  = coreutils/src/<util>/c   built with its own Makefile (gcc).  -> ./<util>
SUT     = coreutils/src/<util>/$C2SR_VARIANT (default rust_WIP = C2SaferRust output) built with cargo + the pinned nightly
          (rust-toolchain.toml in that dir selects nightly-2022-08-08).
          -> target/debug/<util>

Tool versions are captured and returned so they can be recorded as part of every
result.
"""
import json
import os
import subprocess
import sys

from common import util_c_dir, util_rust_dir


def _run(argv, cwd):
    p = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout


def tool_versions():
    v = {}
    for name, argv in (("gcc", ["gcc", "--version"]),
                       ("rustc", ["rustc", "--version"]),
                       ("cargo", ["cargo", "--version"])):
        try:
            out = subprocess.run(argv, stdout=subprocess.PIPE, text=True).stdout
            v[name] = out.splitlines()[0] if out else "?"
        except FileNotFoundError:
            v[name] = "ABSENT"
    return v


def build_oracle(util):
    cdir = util_c_dir(util)
    if not os.path.isdir(cdir):
        return {"ok": False, "why": f"no C dir {cdir}"}
    rc, log = _run(["make"], cdir)
    binpath = os.path.join(cdir, util)
    ok = rc == 0 and os.path.isfile(binpath)
    return {"ok": ok, "bin": binpath if ok else None, "rc": rc,
            "log_tail": "\n".join(log.splitlines()[-15:])}


def build_sut(util):
    rdir = util_rust_dir(util)
    if not os.path.isdir(rdir):
        return {"ok": False, "why": f"no rust dir {rdir}"}
    rc, log = _run(["cargo", "build", "--bin", util], rdir)
    binpath = os.path.join(rdir, "target", "debug", util)
    ok = rc == 0 and os.path.isfile(binpath)
    return {"ok": ok, "bin": binpath if ok else None, "rc": rc,
            "log_tail": "\n".join(log.splitlines()[-25:])}


def build_both(util):
    return {
        "util": util,
        "tools": tool_versions(),
        "oracle": build_oracle(util),
        "sut": build_sut(util),
    }


def main():
    util = sys.argv[1] if len(sys.argv) > 1 else "pwd"
    res = build_both(util)
    print(json.dumps(res, indent=2))
    ok = res["oracle"]["ok"] and res["sut"]["ok"]
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
