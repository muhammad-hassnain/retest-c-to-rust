"""Shared helpers for the SACTOR re-grade (systems/sactor/). Runs INSIDE the Docker container
(see run_docker.sh): crust-heldout image (Ubuntu 22.04, gcc 11.4, clang 14) with the host's
nightly Rust toolchains mounted read-only at /opt/tc/<name>, the libc crate vendored at /vendor,
the pinned artifacts read-only at /art, and --network none.

SACTOR pins nightly-2024-12-18 (rust-toolchain.toml in both sactor and sactor-datasets). That
exact toolchain is not on either machine, so every Rust build
is done with the two bracketing nightlies that are present (2024-09-03 and 2025-05-10); the
results are compared across both.
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

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "harness"))
import oracle_ub_common as ou  # noqa: E402

ART = os.environ.get("SACTOR_ART", "/art/sactor")
TOOLCHAINS = {  # name -> path of a mounted rustup toolchain dir
    "nightly-2024-09-03": "/opt/tc/nightly-2024-09-03",
    "nightly-2025-05-10": "/opt/tc/nightly-2025-05-10",
}
PRIMARY_TC = "nightly-2024-09-03"
SACTOR_PINNED_TC = "nightly-2024-12-18"

# SACTOR's own Cargo.toml for a library build attempt (sactor/utils.py:create_rust_proj)
CARGO_TOML = """[package]
name = "build_attempt"
version = "0.1.0"
edition = "2021"

[dependencies]
libc = "0.2.159"
[lib]
name = "build_attempt"
crate-type = ["cdylib"]
"""


def sh(argv, cwd=None, env=None, timeout=600):
    p = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=timeout)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def tc_env(tc):
    e = dict(os.environ)
    root = TOOLCHAINS[tc]
    e["PATH"] = f"{root}/bin:" + e.get("PATH", "")
    e["RUSTC"] = f"{root}/bin/rustc"
    e["CARGO_HOME"] = os.environ.get("CARGO_HOME", "/cargo_home")
    e["CARGO_NET_OFFLINE"] = "true"
    e.pop("RUSTUP_TOOLCHAIN", None)
    return e


def tc_version(tc):
    rc, out = sh([f"{TOOLCHAINS[tc]}/bin/rustc", "--version"])
    return out.strip()


FN_RE = re.compile(r'^(?P<attrs>(?:#\[[^\n]*\]\n)*)(?P<vis>pub(?:\([^)]*\))?\s+)?(?P<unsafe>unsafe\s+)?'
                   r'(?:extern\s+"C"\s+)?fn\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)', re.M)


def expose_to_c(code, names):
    """Re-implementation of SACTOR's rust_ast_parser.expose_function_to_c for every top-level fn
    whose name is in `names`: make it `pub [unsafe] extern "C" fn` and add #[no_mangle]. Only
    column-0 items are top level in SACTOR's (prettyplease/rustfmt-formatted) output."""
    def rep(m):
        if m.group("name") not in names:
            return m.group(0)
        attrs = m.group("attrs") or ""
        if "no_mangle" not in attrs:
            attrs += "#[no_mangle]\n"
        return f'{attrs}pub {m.group("unsafe") or ""}extern "C" fn {m.group("name")}'
    return FN_RE.sub(rep, code)


def _flatten_use(tree, prefix=""):
    """`a::{b, c::{d, e as f}}` -> ['a::b', 'a::c::d', 'a::c::e as f'] (SACTOR's used_code_list)."""
    tree = tree.strip()
    k = tree.find("{")
    if k < 0:
        return [prefix + tree] if tree else []
    head, body = tree[:k], tree[k + 1:tree.rfind("}")]
    parts, depth, cur = [], 0, ""
    for ch in body:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    out = []
    for x in parts:
        x = x.strip()
        if x == "self":
            out.append(prefix + head.rstrip(":").rstrip(":"))
        elif x:
            out += _flatten_use(x, prefix + head)
    return out


def _merge_uses(paths):
    """SACTOR's combiner.merge_uses: std::ffi / std::os::raw names that libc also provides are
    taken from libc; std::os::raw names already imported from std::ffi are dropped; dedup."""
    split = [p.split("::") for p in paths]
    libc_ids = {"::".join(u[1:]) for u in split if u[0] == "libc"}
    ffi_ids = {"::".join(u[2:]) for u in split if u[:2] == ["std", "ffi"]}
    out = []
    for u in split:
        if u[:2] == ["std", "ffi"] and "::".join(u[2:]) in libc_ids:
            u = ["libc"] + u[2:]
        elif u[:3] == ["std", "os", "raw"]:
            ident = "::".join(u[3:])
            if ident in ffi_ids:
                continue
            if ident in libc_ids:
                u = ["libc"] + u[3:]
        p = "::".join(u)
        if p not in out:
            out.append(p)
    return out


def _top_items(code):
    """Split Rust source into top-level items (attributes kept with their item)."""
    items, i, n, start, depth = [], 0, len(code), 0, 0
    while i < n:
        c = code[i]
        if code.startswith("//", i):
            j = code.find("\n", i)
            i = n if j < 0 else j
            continue
        if code.startswith("/*", i):
            j = code.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == '"':
            j = i + 1
            while j < n and code[j] != '"':
                j += 2 if code[j] == "\\" else 1
            i = j + 1
            continue
        if c == "b" and code.startswith('b"', i):
            i += 1
            continue
        if c == "'" and i + 2 < n and (code[i + 2] == "'" or (code[i + 1] == "\\" and "'" in code[i + 2:i + 8])):
            j = code.find("'", i + 2 if code[i + 1] != "\\" else i + 3)
            i = j + 1
            continue
        if c in "{([":
            depth += 1
        elif c in "})]":
            depth -= 1
            if depth == 0 and c == "}":
                # an item ends at a top-level '}' unless a ';' follows (e.g. `static X: T = S { .. };`)
                k = i + 1
                while k < n and code[k] in " \t":
                    k += 1
                if k < n and code[k] == ";":
                    i = k
                items.append(code[start:i + 1])
                start = i + 1
        elif c == ";" and depth == 0:
            items.append(code[start:i + 1])
            start = i + 1
        i += 1
    tail = code[start:]
    if tail.strip():
        items.append(tail)
    return [x.strip("\n") for x in items if x.strip()]


ITEM_KEY = re.compile(r"^(?:\s*#\[[^\n]*\]\s*)*\s*(?:pub(?:\([^)]*\))?\s+)?(?:unsafe\s+)?"
                      r"(type|const|static(?:\s+mut)?|struct|enum|union)\s+([A-Za-z_][A-Za-z0-9_]*)")


def assemble(tdir):
    """Combine per-item files the way SACTOR's Combiner._combine_code does (combiner/combiner.py):
    flattened, merged `use` paths, then data types (structs, enums, global vars), then functions,
    then dedup_items (rust_ast_parser: first type/const/static/struct/enum/union of each name
    wins; identical `use` items once). Used only where the artifact ships no combined.rs."""
    uses, bodies = [], []
    for sub in ("structs", "enums", "global_vars", "functions"):
        d = os.path.join(tdir, sub)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".rs"):
                continue
            text = open(os.path.join(d, fn)).read()
            for m in re.finditer(r"^\s*use\s+([^;]+);[ \t]*$", text, re.M | re.S):
                uses += _flatten_use(re.sub(r"\s+", " ", m.group(1)))
            bodies.append(re.sub(r"^\s*use\s+[^;]+;[ \t]*$", "", text, flags=re.M | re.S))
    seen = set()
    kept = []
    for item in _top_items("\n".join(bodies)):
        m = ITEM_KEY.match(item)
        if m:
            key = (m.group(1).split()[0], m.group(2))
            if key in seen:
                continue
            seen.add(key)
        kept.append(item)
    return "".join(f"use {p};\n" for p in _merge_uses(uses)) + "\n".join(kept) + "\n"


def build_crate(code, crate_dir, tc, profile="debug"):
    """Write SACTOR's build_attempt crate around `code` and cargo build it. Returns (ok, log, so)."""
    if os.path.isdir(crate_dir):
        shutil.rmtree(crate_dir)
    os.makedirs(os.path.join(crate_dir, "src"))
    with open(os.path.join(crate_dir, "Cargo.toml"), "w") as f:
        f.write(CARGO_TOML)
    with open(os.path.join(crate_dir, "src", "lib.rs"), "w") as f:
        f.write(code)
    argv = ["cargo", "build", "--offline"] + (["--release"] if profile == "release" else [])
    rc, log = sh(argv, cwd=crate_dir, env=tc_env(tc), timeout=900)
    so = os.path.join(crate_dir, "target", profile, "libbuild_attempt.so")
    return rc == 0 and os.path.isfile(so), log, so


def sha256_file(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def provenance(script, command, extra=None):
    p = {
        "script": os.path.relpath(os.path.abspath(script), REPO),
        "command": command,
        "git_rev": os.environ.get("SACTOR_GIT_REV", "?"),
        "git_worktree_note": "study repo files copied to the run host (uncommitted systems/sactor/)",
        "host": os.environ.get("SACTOR_HOST", platform.system()),
        "arch": platform.machine(),
        "kernel": platform.release(),
        "docker_image": os.environ.get("SACTOR_IMAGE", "?"),
        "docker_image_id": os.environ.get("SACTOR_IMAGE_ID", "?"),
        "toolchains": {
            "gcc": ou.tool_version(["gcc", "--version"]),
            "clang": ou.tool_version(["clang", "--version"]),
            "rust": {tc: tc_version(tc) for tc in TOOLCHAINS},
            "rust_pinned_by_artifact": SACTOR_PINNED_TC + " (not available offline; not used)",
            "libc_crate": "0.2.191 (vendored offline from the host cargo cache, crates.io sha256 "
                          "65cd87c9a6d60536580b316ad985726104730a5151c99845f2490428ef91240a). SACTOR's "
                          "Cargo.toml asks libc = \"0.2.159\", i.e. the newest 0.2.x at build time; 0.2.191 "
                          "is the newest cached (cached 2026-10-19, a day before the CRUST-bench results "
                          "commit). An earlier run with 0.2.169 failed Linear-Algebra-C only because "
                          "libc::backtrace_symbols first appears in libc 0.2.176.",
            "python": sys.version.split()[0],
        },
        "artifacts": {
            "sactor": "github.com/qsdrqs/sactor@577c3d2b8074d5fed7c47b988c0acd175e498f98 "
                      "(git-archive sha256 0d595857ee451bbb1e44434df994714480758d163f41be4af1ca29ae7ac0de61)",
            "sactor-datasets": "github.com/qsdrqs/sactor-datasets@1060b3b27d151e2d4919dd2db69abcfa91153399 "
                               "(git-archive sha256 1d180c10513d12749d497ec8bed1d56e1c01f4bd7aee0c3eeb3ed6686f92a8bf)",
            "CRUST-bench": "github.com/qsdrqs/CRUST-bench@69970f9c9a583290f943145eada5e8948b1b91ff "
                           "(git-archive sha256 3374c0ff570266411c4974aaf26333ea982ef8b06560f594c26b3ede85fe4cc3)",
        },
        "date": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    if extra:
        p.update(extra)
    return p


def dump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=1)
        f.write("\n")
