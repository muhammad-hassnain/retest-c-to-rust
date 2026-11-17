#!/usr/bin/env python3
"""One entry point for every stage of every system's re-test, driven by pipeline.json (repo root).

  python3 scripts/pipeline.py list [--system S]            every stage: where it runs, arch, cheap?, outputs
  python3 scripts/pipeline.py show <system|system.stage>   the full record(s), as JSON
  python3 scripts/pipeline.py run <system|all> [stage ...] [--out DIR] [--in-place] [options]
  python3 scripts/pipeline.py hashes [--out DIR]           -> results/PIPELINE_HASHES.json
  python3 scripts/pipeline.py verify [--out DIR]           re-run the CHEAP deterministic stages, compare hashes
  python3 scripts/pipeline.py spotcheck <stage-id ...>     run a stage on a small fixed subset, compare per-case verdicts

`run` never touches the shipped results/ unless --in-place: it copies the repository (without artifacts/,
out/, .git) to DIR/work (default DIR = out/pipeline), runs each stage there (its results/ starts as a copy
of the shipped one, so later stages read earlier stages' fresh outputs), and copies every declared output
to DIR/outputs/. A stage is skipped, with the reason printed, when it is 'unknown' (no command can be
derived), is a template, needs an artifacts/ directory that is missing, needs another architecture than
this host's (add --emulate to try Docker emulation), needs Docker/an image that is not present, or needs a
Python module (pipeline.json `python_modules`, e.g. matplotlib for the figures) that `python3` cannot import.

Runners: host (bash in the work copy), docker (the stage's pinned image, work copy mounted at /work and
artifacts/ read-only at /work/artifacts, like scripts/run_all.sh), wrapper (a repository script that starts
Docker itself; layouts under ~/work/... are redirected with --managed-src, see `show <stage>`). With
--runner local (or CRUST_IMAGE set, as in the reviewer image) docker stages run directly in this
environment.

Normalisation used by `hashes` and `verify`: JSON files are re-serialised with sorted keys after dropping
every "provenance"/"_provenance" object and every key that records a time or duration (date, timestamp,
elapsed, duration, wall, seconds, ...); PDFs drop /CreationDate and /ModDate; everything else is hashed
as is. Python standard library only (the figure stages need matplotlib and the code listings Pygments; without
them `verify` reports those stages as SKIP).
"""
import argparse
import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PJ = os.path.join(REPO, "pipeline.json")
HASHES = os.path.join(REPO, "results", "PIPELINE_HASHES.json")
COPY_EXCLUDE = {"artifacts", "out", ".git", "__pycache__", "explorer", "dashboard.html"}
DROP_RE = re.compile(r"(^|_)(provenance|elapsed|duration|wall|runtime|seconds|secs|timestamp|date|date_utc|generated|generated_at|"
                     r"started|finished|started_at|finished_at|time_s|mtime)(_|$)")


def load():
    with open(PJ) as f:
        return json.load(f)


def host_arch():
    m = platform.machine().lower()
    return "arm64" if m in ("arm64", "aarch64") else "x86_64" if m in ("x86_64", "amd64") else m


# ------------------------------------------------------------------ normalisation / hashing
ROOTS = [REPO]          # absolute roots replaced by <root> in strings (the work copy is added by run/verify)
# Result files written before the repository was renamed or moved record its old absolute path; any absolute
# path ending in the repository's folder name (old or new) counts as <root> too.
ROOT_RE = re.compile(r"(?:/[^/\s\"']+)*/(?:c-to-rust-validation|retest-c-to-rust)(?=/|$)")


def norm_json(o):
    if isinstance(o, str):
        for r in ROOTS:
            o = o.replace(r, "<root>")
        return ROOT_RE.sub("<root>", o)
    if isinstance(o, dict):
        return {k: norm_json(v) for k, v in o.items() if not DROP_RE.search(k)}
    if isinstance(o, list):
        return [norm_json(v) for v in o]
    return o


def file_digest(path):
    """sha256 of one file after normalisation (see module docstring)."""
    with open(path, "rb") as f:
        raw = f.read()
    if path.endswith(".json"):
        try:
            raw = json.dumps(norm_json(json.loads(raw)), sort_keys=True, separators=(",", ":")).encode()
        except ValueError:
            pass
    elif path.endswith(".pdf"):
        raw = re.sub(rb"/(CreationDate|ModDate) \([^)]*\)", b"", raw)
    return hashlib.sha256(raw).hexdigest()


def tree_digest(path):
    if os.path.isfile(path):
        return file_digest(path)
    h = hashlib.sha256()
    for dp, dn, fn in os.walk(path):
        dn.sort()
        for n in sorted(fn):
            p = os.path.join(dp, n)
            h.update(os.path.relpath(p, path).encode() + b"\0" + file_digest(p).encode() + b"\n")
    return h.hexdigest()


def values_digest(log, stage_dir):
    """Figure stages: sha256 of what the plotting script printed (the plotted numbers), without the command
    line, the font line, the 'wrote' line, warnings and the output directory. A PDF's bytes depend on the
    matplotlib version and the fonts installed; the numbers it plots do not."""
    try:
        lines = open(log, encoding="utf-8", errors="replace").read().splitlines()[1:]
    except OSError:
        return None
    keep = [ln.replace(stage_dir, "{out}") for ln in lines
            if not ln.startswith(("font:", "wrote ", "WARN:")) and not any(w in ln for w in ("Warning", "warnings.warn", "findfont"))]
    return hashlib.sha256("\n".join(keep).encode()).hexdigest()

def real_outputs(stage, out=None):
    """declared outputs that are concrete relative paths ({out} substituted when given)."""
    res = []
    for o in stage["outputs"]:
        o = o.split(" (")[0].strip()
        if any(c in o for c in "<>*()$") or o.startswith(("stdout", "WORK", "SCRATCH", "docker image", "$W", "artifacts/")):
            continue
        if "{out}" in o:
            if out is None:
                continue
            o = o.replace("{out}", out)
        m = re.fullmatch(r"(.*)\{([^}]*)\}(.*)", o)
        if m:
            res += [m.group(1) + x.strip() + m.group(3) for x in m.group(2).split(",")]
        else:
            res.append(o)
    return [r for r in res if not r.startswith(("SCRATCH", "/"))] if out is None else res


def expand_u(paths):
    r = []
    for p in paths:
        r += [p.replace("<u>", u) for u in ("cat", "head", "tail", "uniq")] if "<u>" in p else [p]
    return r


# ------------------------------------------------------------------ selection
def pick(pj, sel, names=()):
    st = pj["stages"]
    parts = [x for x in sel.split(",") if x]
    out = []
    for s in st:                                   # keep pipeline.json (run) order
        if "all" in parts or s["id"] in parts or s["system"] in parts:
            out.append(s)
    if names:
        out = [s for s in out if s["stage"] in names or s["id"] in names]
    if not out:
        sys.exit(f"no stage matches {sel} {' '.join(names)} (try: pipeline.py list)")
    return out


# ------------------------------------------------------------------ work copy
def make_work(outdir, in_place, fresh=False):
    """scratch copy of the repository at OUTDIR/work. An existing copy is reused (so a later invocation sees the
    earlier stages' fresh results/) unless fresh=True."""
    if in_place:
        return REPO
    work = os.path.join(outdir, "work")
    marker = os.path.join(work, ".pipeline_work")
    if os.path.exists(marker) and not fresh:
        return work
    if os.path.exists(work) and fresh:
        shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    open(marker, "w").write(datetime.datetime.now(datetime.timezone.utc).isoformat() + "\n")
    for name in os.listdir(REPO):
        if name in COPY_EXCLUDE or name.startswith("."):
            continue
        src, dst = os.path.join(REPO, name), os.path.join(work, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "rerun-*"), symlinks=True)
        else:
            shutil.copy2(src, dst)
    art = os.path.join(work, "artifacts")
    if not os.path.lexists(art) and os.path.isdir(os.path.join(REPO, "artifacts")):
        os.symlink(os.path.realpath(os.path.join(REPO, "artifacts")), art)
    os.makedirs(os.path.join(work, "results"), exist_ok=True)
    return work


# ------------------------------------------------------------------ run one stage
def skip_reason(s, args, work):
    if s["command"] == "unknown":
        return "command unknown: " + s.get("unknown_reason", "")
    if s.get("virtual"):
        return "not a separate command (" + s["command"][:60] + ")"
    c = s["command"]
    if re.search(r"<[A-Za-z_./-]+>|SCRATCH|<the |BOLERO_PARTS|KANI.json", c):
        return "command is a template (placeholders): " + c[:70]
    w = s["where"]
    if w.get("arch") not in ("any", "unknown", None) and w["arch"] != host_arch() and not args.emulate:
        return f"needs {w['arch']} (host is {host_arch()}; --emulate to try)"
    for n in s.get("needs", []):
        if n.startswith("artifacts/") and "*" not in n:
            if not os.path.isdir(os.path.join(REPO, n.split(" ")[0])) and not os.path.isdir(os.path.join(work, n.split(" ")[0])):
                return f"needs {n} (fetch stage of the same system)"
        elif n.startswith("results/") and "*" not in n and "<" not in n and not os.path.exists(os.path.join(work, n)):
            return f"needs {n}"
    if w["runner"] in ("docker", "wrapper") and not shutil.which("docker") and args.runner != "local":
        return "needs docker"
    if w["runner"] == "docker" and args.runner != "local":
        img = getattr(args, "image", None) or w.get("image", "")
        if not img.startswith("ubuntu") and subprocess.run(["docker", "image", "inspect", img], capture_output=True).returncode:
            return f"needs docker image {img}"
    if w["runner"] == "wrapper" and "managed" in w and not managed_ready(s, args)[0]:
        return "needs managed layout: " + managed_ready(s, args)[1]
    if s.get("network") and not args.network:
        return "needs network (pass --network to allow)"
    if w["runner"] != "docker" or args.runner == "local" or os.environ.get("CRUST_IMAGE"):
        miss = [m for m in s.get("python_modules", [])
                if subprocess.run(["python3", "-c", f"import {m}"], capture_output=True).returncode]
        if miss:
            return "needs Python module " + ", ".join(miss) + " (python3 cannot import it)"
    return None


def managed_root(s, args):
    return os.path.join(os.path.abspath(args.out), "managed", s["system"])


def managed_ready(s, args):
    m = s["where"]["managed"]
    src = getattr(args, "managed_src", None) or {}
    root = managed_root(s, args)
    for r in m.get("requires", []):
        if not (os.path.exists(os.path.join(root, r)) or (src.get(s["system"]) and os.path.exists(os.path.join(src[s["system"]], r)))):
            return False, f"{r} under {root} (or --managed-src {s['system']}=DIR)"
    return True, ""


def run_stage(s, args, work):
    """returns (status, seconds, log path)"""
    os.makedirs(os.path.join(args.out, "logs"), exist_ok=True)
    log = os.path.join(args.out, "logs", s["id"] + ".log")
    base_cmd = (s.get("env") or {}).get("PIPELINE_VERIFY_COMMAND") if getattr(args, "verifying", False) else None
    cmd = (base_cmd or s["command"]).replace("{out}", os.path.join(os.path.abspath(args.out), "stage_" + s["stage"]))
    cmd = re.sub(r"\s+#.*$", "", cmd)          # trailing comment
    if "{out}" in s["command"]:
        os.makedirs(os.path.join(os.path.abspath(args.out), "stage_" + s["stage"]), exist_ok=True)
    w = s["where"]
    art = os.path.realpath(os.path.join(REPO, "artifacts"))
    env = dict(os.environ)
    env.update({k: v.replace("$PWD", work) for k, v in (s.get("env") or {}).items() if not k.startswith("PIPELINE_")})
    extra = getattr(args, "extra_args", {}).get(s["id"], "")
    if extra:
        cmd = cmd + " " + extra
    if w["runner"] == "docker" and args.runner != "local" and not os.environ.get("CRUST_IMAGE"):
        plat = ["--platform", "linux/amd64"] if (w.get("arch") == "x86_64" and host_arch() != "x86_64") else []
        cpus = ["--cpus", str(args.cpus)] if args.cpus else []
        # entries of artifacts/ that are symlinks (e.g. to trees elsewhere on this host) are mounted at their own path too
        links = []
        if os.path.isdir(art):
            for n in sorted(os.listdir(art)):
                if os.path.islink(os.path.join(art, n)):
                    tgt = os.path.realpath(os.path.join(art, n))
                    links += ["-v", f"{tgt}:{tgt}:ro"]
        image = getattr(args, "image", None) or w["image"]
        mount = f"{art}:/work/artifacts" + ("" if s.get("artifact_rw") else ":ro")   # stages that build inside the artifact tree need it writable
        full = ["docker", "run", "--rm"] + plat + cpus + ["-v", f"{work}:/work", "-v", mount] + links + ["-w", "/work",
                "-e", "C2SR_ROOT=/work/artifacts/c2saferrust", "-e", "CRUST_IMAGE=" + image] + \
               sum((["-e", f"{k}={v.replace('$PWD', '/work').replace(work, '/work')}"] for k, v in (s.get("env") or {}).items() if not k.startswith("PIPELINE_")), []) + \
               [image, "bash", "-c", cmd.replace(work, "/work")]
        runner_cwd = None
    else:
        tc = {"crust-heldout": "nightly-2022-08-08", "crust-stable": "1.86.0", "crust-syzygy": "1.86.0"}.get(w.get("image", ""))
        if tc and os.environ.get("CRUST_IMAGE"):
            env["RUSTUP_TOOLCHAIN"] = tc
            if w["image"] == "crust-heldout":
                env["PATH"] = "/opt/clang-alias:" + env["PATH"]
        env.setdefault("C2SR_ROOT", os.path.join(work, "artifacts", "c2saferrust"))
        full = ["bash", "-c", cmd]
        runner_cwd = work
        if w["runner"] == "wrapper" and "managed" in w:
            m = w["managed"]
            root = managed_root(s, args)
            os.makedirs(root, exist_ok=True)
            src = (getattr(args, "managed_src", None) or {}).get(s["system"])
            for r in m.get("requires", []):
                if src and os.path.exists(os.path.join(src, r)) and not os.path.lexists(os.path.join(root, r)):
                    os.makedirs(os.path.dirname(os.path.join(root, r)), exist_ok=True)
                    os.symlink(os.path.realpath(os.path.join(src, r)), os.path.join(root, r))
            for d in ("build", "out"):
                os.makedirs(os.path.join(root, d), exist_ok=True)
            if m["var"] == "WORK":
                tree = os.path.join(root, m["tree"])
                for sub in ("harness", "systems", "results", "scripts"):
                    shutil.copytree(os.path.join(work, sub), os.path.join(tree, sub), dirs_exist_ok=True, symlinks=True,
                                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "rerun-*"))
                env["WORK"] = root
                if s["system"] == "sactor":
                    env["SACTOR_ART"] = "/art/sactor"
            elif m["var"] == "BUILD":
                env["BUILD"] = os.path.join(root, "build")
                env["REPO"] = work
                env["ART"] = os.path.join(art, s["system"])
            elif m["var"] == "HOME":
                home = root
                wk = os.path.join(home, "work", "regrade-actoruw")
                os.makedirs(os.path.join(wk, "scripts"), exist_ok=True)
                for f in os.listdir(os.path.join(work, "systems", "actoruw")):
                    if f.endswith((".py", ".sh")):
                        shutil.copy2(os.path.join(work, "systems", "actoruw", f), os.path.join(wk, "scripts", f))
                for f in ("systems/enumerate_inputs.py", "systems/batch_diff.py", "harness/oracle_ub_common.py", "harness/mutate_full.py"):
                    shutil.copy2(os.path.join(work, f), os.path.join(wk, "scripts", os.path.basename(f)))
                if not os.path.exists(os.path.join(wk, "artifact")):
                    # the container mounts only W, so the artifact must be a real copy there, not a symlink
                    shutil.copytree(os.path.join(art, "actoruw"), os.path.join(wk, "artifact"), symlinks=True,
                                    ignore=shutil.ignore_patterns("target", "__pycache__"))
                for d in (".rustup", ".cargo", ".docker"):
                    if not os.path.lexists(os.path.join(home, d)) and os.path.exists(os.path.join(os.path.expanduser("~"), d)):
                        os.symlink(os.path.join(os.path.expanduser("~"), d), os.path.join(home, d))
                env["HOME"] = home
    if args.dry_run:
        print("    DRY:", " ".join(full)[:400])
        return "dry-run", 0, log
    t0 = time.time()
    with open(log, "w") as lf:
        lf.write("$ " + " ".join(full)[:2000] + "\n")
        lf.flush()
        p = subprocess.run(full, cwd=runner_cwd, env=env, stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT)
    return ("ok" if p.returncode == 0 else f"exit {p.returncode}"), time.time() - t0, log


def collect(s, args, work):
    n = 0
    base = work
    if "managed" in s["where"] and s["where"]["managed"].get("tree") and s["where"]["managed"]["var"] == "WORK":
        base = os.path.join(managed_root(s, args), s["where"]["managed"]["tree"])
    for o in expand_u(real_outputs(s, os.path.join(os.path.abspath(args.out), "stage_" + s["stage"]))):
        src = o if os.path.isabs(o) else os.path.join(base, o)
        if not os.path.exists(src):
            continue
        dst = os.path.join(args.out, "outputs", o if not os.path.isabs(o) else os.path.relpath(o, os.path.abspath(args.out)))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        n += 1
        sp = os.path.join(REPO, o)
        if os.path.exists(sp) and not os.path.isabs(o):
            try:
                same = tree_digest(sp) == tree_digest(src)
            except OSError:
                same = False
            print(f"       {o}: {'equal to the shipped file after normalisation' if same else 'DIFFERS from the shipped file after normalisation'}")
    return n


def stage_ok(s, st):
    return st == "ok" or st in [f"exit {c}" for c in s.get("exit_ok", [])]


def cmd_run(args):
    pj = load()
    stages = pick(pj, args.system, args.stages)
    args.out = os.path.abspath(args.out)
    work = make_work(args.out, args.in_place)
    rows = []
    for s in stages:
        why = skip_reason(s, args, work)
        if why:
            print(f"[skip] {s['id']}: {why}")
            rows.append({"id": s["id"], "status": "skipped", "reason": why})
            continue
        print(f"[run ] {s['id']}  ({s['where']['runner']}{':' + s['where'].get('image', '') if s['where'].get('image') else ''}; shipped arch {s['where'].get('arch')})")
        st, sec, log = run_stage(s, args, work)
        n = collect(s, args, work) if stage_ok(s, st) and not args.in_place else 0
        print(f"       {st} in {sec:.0f}s, {n} output(s) copied to {os.path.join(args.out, 'outputs')}, log {os.path.relpath(log)}")
        rows.append({"id": s["id"], "status": st, "seconds": round(sec, 1), "log": log})
    os.makedirs(args.out, exist_ok=True)
    sp = os.path.join(args.out, "run_summary.json")
    prev = json.load(open(sp)) if os.path.exists(sp) else {"runs": []}
    prev["runs"].append({"host": platform.system(), "arch": host_arch(), "date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                         "in_place": args.in_place, "stages": rows})
    with open(sp, "w") as f:
        json.dump(prev, f, indent=1)
    okids = {s["id"] for s in stages if True}
    bad = [r for r in rows if r["status"] not in ("ok", "skipped", "dry-run") and not stage_ok(next(x for x in stages if x["id"] == r["id"]), r["status"])]
    print(f"{len(rows) - len(bad)} stage(s) ok or skipped, {len(bad)} failed -> {os.path.join(args.out, 'run_summary.json')}")
    return 1 if bad else 0


# ------------------------------------------------------------------ list / show
def cmd_list(args):
    pj = load()
    for s in pj["stages"]:
        if args.system and s["system"] != args.system:
            continue
        w = s["where"]
        tag = (w.get("image") or w["runner"])[:24]
        flag = ("CHEAP " if s.get("cheap") else "") + ("UNKNOWN " if s["command"] == "unknown" else "") + ("net " if s.get("network") else "")
        print(f"{s['id']:38s} {s['kind']:10s} {w['runner']:8s} {tag:24s} {str(w.get('arch')):7s} det={s['deterministic']:3s} {flag}")
    print(f"{len(pj['stages'])} stages; {len(pj['results_without_stage'])} result files have no producing stage (see pipeline.json results_without_stage)")


def cmd_show(args):
    pj = load()
    for s in pick(pj, args.target):
        print(json.dumps(s, indent=1))


# ------------------------------------------------------------------ hashes / verify
def shipped_paths(s):
    return [p for p in expand_u(real_outputs(s)) if os.path.exists(os.path.join(REPO, p))]


# stage id -> {fresh output: shipped file} for cheap stages whose output is shipped under another name
CHEAP_MAP = {"cross.figures": {"{out}/figures/harness_power.json": "results/harness_power.json"}}


def cmd_hashes(args):
    pj = load()
    ent = {}
    for s in pj["stages"]:
        files = {p: tree_digest(os.path.join(REPO, p)) for p in shipped_paths(s)}
        if files:
            ent[s["id"]] = {"source": "shipped", "files": files}
    # cheap stages whose outputs are not shipped: run them into DIR and hash the fresh files
    args.runner, args.emulate, args.network, args.dry_run, args.in_place, args.cpus = "docker", False, False, False, False, 0
    args.managed_src = {}
    work = make_work(os.path.abspath(args.out), False, fresh=True)
    for s in pj["stages"]:
        if not s.get("cheap") or any(p for p in real_outputs(s) if os.path.exists(os.path.join(REPO, p))) and s["id"] not in CHEAP_MAP:
            continue
        if skip_reason(s, args, work):
            continue
        st, _, log = run_stage(s, args, work)
        d = os.path.join(os.path.abspath(args.out), "stage_" + s["stage"])
        files = {}
        for p in real_outputs(s, d):
            if os.path.exists(p):
                key = os.path.relpath(p, d) if p.startswith(d) else p
                files[key] = tree_digest(p)
        if st == "ok" and files:
            ent[s["id"] + "#fresh"] = {"source": "fresh run of the cheap stage (output not shipped)", "files": files}
            if s.get("kind") == "figures":
                ent[s["id"] + "#fresh"]["values"] = values_digest(log, d)
    doc = {"about": "sha256 of each stage's outputs after normalisation (scripts/pipeline.py: JSON with provenance/time/duration keys dropped, PDF dates dropped). `shipped` entries hash the files in this repository; `#fresh` entries hash the output of a cheap deterministic stage whose output is not shipped. Regenerate: python3 scripts/pipeline.py hashes",
           "normalisation": DROP_RE.pattern, "stages": ent}
    with open(HASHES, "w") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
        f.write("\n")
    print(f"wrote {os.path.relpath(HASHES)}: {sum(len(v['files']) for v in ent.values())} file hashes in {len(ent)} stage entries")
    return 0


def cmd_verify(args):
    pj = load()
    ref = json.load(open(HASHES)) if os.path.exists(HASHES) else {"stages": {}}
    args.runner, args.emulate, args.network, args.dry_run, args.in_place, args.cpus = "docker", False, False, False, False, 0
    args.managed_src = {}
    args.verifying = True
    args.out = os.path.abspath(args.out)
    work = make_work(args.out, False, fresh=True)
    ROOTS.insert(0, work)
    # the ledger's probe inputs are embedded in the shipped ledger (meta); hand them to the build as files
    try:
        L = json.load(open(os.path.join(REPO, "results", "defect_ledger.json")))
        json.dump(L["meta"]["C2SaferRust"]["probe"], open(os.path.join(work, "probe_c2sr.json"), "w"))
        json.dump(L["meta"]["RustMap"]["bzip2_probe"], open(os.path.join(work, "probe_bzip2.json"), "w"))
    except (OSError, KeyError, ValueError):
        pass
    # stage order: inputs first, then summaries, ledger, tables, figures, claims
    res, bad = [], 0
    for s in pj["stages"]:
        if not s.get("cheap"):
            continue
        why = skip_reason(s, args, work)
        if not why and not expand_u(real_outputs(s, os.path.join(args.out, "stage_" + s["stage"]))):
            why = "stdout only, no output file to compare"
        if why:
            res.append((s["id"], "SKIP", why))
            continue
        st, sec, log = run_stage(s, args, work)
        okcodes = ["ok"] + [f"exit {c}" for c in s.get("exit_ok", [])]
        if st not in okcodes:
            res.append((s["id"], "FAIL", f"{st} (see {os.path.relpath(log)})"))
            bad += 1
            continue
        d = os.path.join(args.out, "stage_" + s["stage"])
        outs = expand_u(real_outputs(s, d))
        notes, ok = [], True
        for o in outs:
            fp = o if os.path.isabs(o) else os.path.join(work, o)
            if not os.path.exists(fp):
                notes.append(f"{o}: not written")
                ok = False
                continue
            h = tree_digest(fp)
            rel = os.path.relpath(fp, d) if fp.startswith(d + os.sep) else o
            shipped = CHEAP_MAP.get(s["id"], {}).get(o.replace(d, "{out}"), rel)
            sp = os.path.join(REPO, shipped)
            target = []
            if os.path.exists(sp):
                target.append(("shipped file", tree_digest(sp)))
            r = ref["stages"].get(s["id"], ref["stages"].get(s["id"] + "#fresh", {})).get("files", {}).get(rel)
            if r:
                target.append(("PIPELINE_HASHES.json", r))
            if not target:
                notes.append(f"{rel}: no reference (run `hashes`)")
                ok = False
            for name, th in target:
                if th != h:
                    notes.append(f"{rel}: MISMATCH vs {name}")
                    ok = False
        if not ok and s.get("kind") == "figures" and notes and all(n.split(":")[0].endswith(".pdf") and "MISMATCH" in n for n in notes):
            want = ref["stages"].get(s["id"] + "#fresh", ref["stages"].get(s["id"], {})).get("values")
            if want and values_digest(log, d) == want:
                ok, notes = True, [f"{len(outs)} file(s): plotted values equal; PDF bytes differ "
                                   "(this machine's fonts or matplotlib differ from the run in PIPELINE_HASHES.json)"]
        extra = f" [stage exit status {st.split()[-1]}: {s.get('exit_meaning', 'see log')}]" if st != "ok" else ""
        res.append((s["id"], "MATCH" if ok else "MISMATCH", ("; ".join(notes) or f"{len(outs)} file(s) equal after normalisation ({sec:.0f}s)") + extra))
        bad += 0 if ok else 1
    for i, st, msg in res:
        print(f"{st:9s} {i:34s} {msg}")
    print(f"{sum(1 for r in res if r[1] == 'MATCH')} match, {bad} mismatch/fail, {sum(1 for r in res if r[1] == 'SKIP')} skipped")
    return 1 if bad else 0


# ------------------------------------------------------------------ spot check (subset runs)
def jload(p):
    return json.load(open(p))


TMP_RE = re.compile(r"/tmp/tmp[0-9a-z_]{8}")
VOL_RE = re.compile(r"==\d+==|0x[0-9a-f]{6,}|/tmp/tmp[0-9a-z_]{8}")


def sub_equal(fresh, shipped):
    """True when every key of `shipped` is present in `fresh` with an equal value (keys only in `fresh` are fields added to the
    harness after the file was shipped); pids, addresses and temp-dir names in strings are scrubbed."""
    if isinstance(shipped, dict):
        return isinstance(fresh, dict) and all(k in fresh and sub_equal(fresh[k], v) for k, v in shipped.items() if not DROP_RE.search(k))
    if isinstance(shipped, list):
        return isinstance(fresh, list) and len(fresh) == len(shipped) and all(sub_equal(a, b) for a, b in zip(fresh, shipped))
    if isinstance(shipped, str):
        return isinstance(fresh, str) and VOL_RE.sub("#", fresh) == VOL_RE.sub("#", shipped)
    return fresh == shipped


def first_defective_index(rows, pred):
    for i, r in enumerate(rows):
        if pred(r):
            return i
    return 2


def subset_plan(s, work):
    """-> (extra args string, comparator(fresh_dir) -> (n_ok, n_total, [diffs]))"""
    sid = s["id"]
    comp = (s.get("subset") or {}).get("compare") or {}

    def cmp_rows(fresh, shipped, path, key, fields, restrict=True):
        fk = lambda r: tuple(r[k] for k in key) if isinstance(key, list) else r[key]
        fr = {fk(r): r for r in fresh[path]}
        sh = {fk(r): r for r in shipped[path]}
        diffs, ok = [], 0
        for k, r in fr.items():
            o = sh.get(k)
            if o is None:
                diffs.append(f"{k}: not in shipped")
            elif all(norm_json(r.get(f)) == norm_json(o.get(f)) for f in fields):
                ok += 1
            else:
                diffs.append(f"{k}: " + ", ".join(f"{f} {o.get(f)!r}->{r.get(f)!r}" for f in fields if r.get(f) != o.get(f)))
        return ok, len(fr), diffs

    ship = lambda name: jload(os.path.join(REPO, "results", name))
    if sid in ("vert.diff", "vert.arrays"):
        f = "vert_diff.json" if sid == "vert.diff" else "vert_arrays.json"
        allr = ship(f)["all"]
        k = max(3, 1 + first_defective_index(allr, lambda r: (r.get("value_div") or 0) > 0 or (r.get("rust_panic_c_ok") or 0) > 0 or (r.get("panic") or 0) > 0 or r.get("kind") in ("value", "rust_crash")))
        fields = comp["fields"] if sid == "vert.diff" else ["status", "kind", "agree", "value", "crash", "ub", "timeout"]
        return f"--limit {k}", lambda d: cmp_rows(jload(os.path.join(d, "results", f)), ship(f), "all", "func", fields)
    if sid == "syzygy.diff":
        return "", lambda d: cmp_rows(jload(os.path.join(d, "results", "syzygy_diff.json")), ship("syzygy_diff.json"), "verdicts", "id", comp["fields"])
    if sid == "c2saferrust.diff":
        return "", None   # handled by spot_c2sr
    if sid == "rustmap.bzip2_check":
        return "", lambda d: cmp_rows(jload(os.path.join(d, "results", "rustmap_bzip2_check.json")), ship("rustmap_bzip2_check.json"), "rows", ["kind", "size"], comp["fields"])
    if sid == "rustassure.diff":
        def c(d):
            fr, sh = jload(os.path.join(d, "results", "rustassure_diff.json")), ship("rustassure_diff.json")
            a, b = norm_json(fr["summary"]), norm_json(sh["summary"])
            diffs = [f"summary.{k}: {b.get(k)!r}->{a.get(k)!r}" for k in set(a) | set(b) if a.get(k) != b.get(k)]
            n_ok, n, dd = cmp_rows({"x": fr["value_bugs"]}, {"x": sh["value_bugs"]}, "x", "func", ["value", "samples", "value_div"]) if fr["value_bugs"] and "func" in fr["value_bugs"][0] else (0, 0, [])
            return (len(a) - len(diffs)) + n_ok, len(a) + n, diffs + dd
        return "", c
    if sid == "rustmap.rosetta":
        def c(d):
            fr, sh = jload(os.path.join(d, "results", "rustmap_rosetta.json")), ship("rustmap_rosetta.json")
            a, b = norm_json(fr["summary"]), norm_json(sh["summary"])
            diffs = [f"summary.{k}: {b.get(k)!r}->{a.get(k)!r}" for k in set(a) | set(b) if a.get(k) != b.get(k)]
            scrub = lambda rows: [{**r, "c": TMP_RE.sub("/tmp/T", str(r.get("c"))), "rust": TMP_RE.sub("/tmp/T", str(r.get("rust")))} for r in rows]
            n_ok, n, dd = cmp_rows({"x": scrub(fr["divergence_samples"])}, {"x": scrub(sh["divergence_samples"])}, "x", "task", ["exit_c", "exit_rust", "c", "rust"])
            return len(a) - len(diffs) + n_ok, len(a) + n, diffs + dd
        return "", c
    if sid == "flourine.diff":
        def c(d):
            # results/flourine_diff.json was produced on the earlier (truncated) input grid: its summary and the set of
            # divergent translations must match; the per-translation counts and samples are compared with the re-run on
            # the current grid, results/rerun-2026-11-12/flourine_diff_spread.json, when that file is shipped.
            fr, sh = jload(os.path.join(d, "results", "flourine_diff.json")), ship("flourine_diff.json")
            a, b = norm_json(fr["summary"]), norm_json(sh["summary"])
            diffs = [f"summary.{k}: {b.get(k)!r}->{a.get(k)!r}" for k in set(a) | set(b) if a.get(k) != b.get(k)]
            ids_f, ids_s = sorted(x["t"] for x in fr["success_divergences"]), sorted(x["t"] for x in sh["success_divergences"])
            n_ok, n = len(a) - len(diffs), len(a)
            n += len(ids_s)
            n_ok += len(set(ids_f) & set(ids_s))
            diffs += [f"divergent translation set differs: {sorted(set(ids_f) ^ set(ids_s))}"] if ids_f != ids_s else []
            rp = os.path.join(REPO, "results", "rerun-2026-11-12", "flourine_diff_spread.json")
            if os.path.exists(rp):
                n += 1
                if norm_json(fr) == norm_json(jload(rp)):
                    n_ok += 1
                else:
                    diffs.append("whole file differs from results/rerun-2026-11-12/flourine_diff_spread.json")
            return n_ok, n, diffs
        return "", c
    if sid == "his2trans.diff":
        projs = "qsort,ht,rgba"      # the first 3 projects in run order; rgba carries a counted defect (H2T-RGBA-1)
        return f"--projects {projs}", lambda d: cmp_rows({"v": [r for r in jload(os.path.join(d, "results", "his2trans_diff.json"))["verdicts"]]},
                                                        {"v": [r for r in ship("his2trans_diff.json")["verdicts"] if r["project"] in projs.split(",")]}, "v", "id", comp["fields"])
    if sid == "ptrtrans.diff":
        allr = ship("ptrtrans_quadtree_diff.json")["verdicts"]
        k = max(3, 1 + first_defective_index(allr, lambda r: r.get("final") == "divergent"))
        return f"--limit {k}", lambda d: cmp_rows({"v": jload(os.path.join(d, "results", "ptrtrans_quadtree_diff.json"))["verdicts"]}, {"v": allr[:k]}, "v", "id", comp["fields"])
    if sid == "sactor.crust_grid":
        only = "2DPartInt,42-Kocaeli-Printf,CircularBuffer,Holdem-Odds"      # first 3 sorted + the sample of SACTOR-C1 (first defective SACTOR CRUST row)

        def c(d):
            fr, sh = jload(os.path.join(d, "results", "sactor_crust_grid.json"))["samples"], ship("sactor_crust_grid.json")["samples"]
            diffs = [f"{k}: record differs" for k in fr if not sub_equal(fr[k], sh.get(k))]
            return len(fr) - len(diffs), len(fr), diffs
        return f"--only {only}", c
    if sid == "sactor.libogg_diff":
        def c(d):
            fr, sh = jload(os.path.join(d, "results", "sactor_libogg.json"))["translations"], ship("sactor_libogg.json")["translations"]
            diffs = [f"{k}: record differs" for k in fr if not sub_equal(fr[k], sh.get(k))]
            return len(fr) - len(diffs), len(fr), diffs
        return "", c
    if sid == "rustassure.libcsv_e2e":
        def c(d):
            fr, sh = jload(os.path.join(d, "systems/rustassure/libcsv_e2e/results.json")), jload(os.path.join(REPO, "systems/rustassure/libcsv_e2e/results.json"))
            ks = [k for k in sh if k != "provenance"]
            diffs = [f"{k}: differs" for k in ks if norm_json(fr.get(k)) != norm_json(sh.get(k))]
            return len(ks) - len(diffs), len(ks), diffs
        return "", c
    return None, None


def spot_c2sr(args, work, pj):
    """subset cases per util (first 100 + first 25 shipped mismatches), run difftest on them, compare."""
    import difflib  # noqa: F401
    sub = os.path.join(work, "subset")
    os.makedirs(sub, exist_ok=True)
    plan = {}
    for u in ("cat", "head", "tail", "uniq"):
        cases = jload(os.path.join(REPO, "results", f"{u}_cases_v4.json"))
        ver = {v["id"]: v for v in jload(os.path.join(REPO, "results", f"{u}_diff_v4.json"))["verdicts"]}
        ids = [c["id"] for c in cases["cases"]]
        pick_ids = set(ids[:100]) | set([i for i in ids if ver.get(i, {}).get("mismatch")][:25])
        sel = [c for c in cases["cases"] if c["id"] in pick_ids]
        d = dict(cases)
        d["cases"] = sel
        json.dump(d, open(os.path.join(sub, f"{u}_cases_v4.json"), "w"))
        plan[u] = (len(sel), ver)
    return plan


def spot_actoruw(args, work, pj, s):
    """first 3 cases in sorted battery/case order of results/actoruw_difftest.json plus the first case with a counted
    ledger defect (B01_synthetic/005_static_loop); build.py then difftest.py per case; compare the per-program record."""
    shipped = {f"{p['battery']}/{p['case']}": p for p in jload(os.path.join(REPO, "results", "actoruw_difftest.json"))["data"]["programs"]}
    L = jload(os.path.join(REPO, "results", "defect_ledger.json"))
    first_def = next(r["translation"].split(" (")[0] for r in L["rows"] if r["system"] == "ACTOR")
    cases = sorted(shipped)[:3] + ([first_def] if first_def in shipped and first_def not in sorted(shipped)[:3] else [])
    root = managed_root(s, args)
    n_ok, diffs, t0 = 0, [], time.time()
    why = skip_reason(s, args, work)
    if why:
        print(f"SKIP actoruw.diff: {why}")
        return 0, 0, [], 0
    for c in cases:
        for script in ("build.py", "difftest.py"):
            st = dict(s, command=f"bash systems/actoruw/run_in_container.sh {script} --only {c}")
            r, sec, log = run_stage(st, args, work)
            if r != "ok":
                diffs.append(f"{c}: {script} {r} (log {log})")
                break
        else:
            out = os.path.join(root, "home" if False else "work", "regrade-actoruw", "out", "difftest.json")
            fr = {f"{p['battery']}/{p['case']}": p for p in jload(out).get("programs", [])}
            if c in fr and norm_json(fr[c]) == norm_json(shipped[c]):
                n_ok += 1
            else:
                diffs.append(f"{c}: per-program record differs")
    return n_ok, len(cases), diffs, time.time() - t0


def cmd_spotcheck(args):
    pj = load()
    args.runner, args.network, args.dry_run, args.in_place = args.runner, False, False, False
    args.managed_src = dict(x.split("=", 1) for x in (args.managed_src or []))
    args.out = os.path.abspath(args.out)
    work = make_work(args.out, False, fresh=True)
    total_bad = 0
    for sid in args.stages:
        s = pick(pj, sid)[0]
        if s["id"] == "actoruw.diff":
            n_ok, n_all, diffs, sec = spot_actoruw(args, work, pj, s)
            print(f"RESULT {sid}: reproduced {n_ok}/{n_all} in {sec:.0f}s" + ("" if not diffs else "; differences:"))
            for d in diffs[:20]:
                print("   ", d)
            total_bad += 0 if n_ok == n_all else 1
            continue
        if s["id"] == "c2saferrust.diff":
            plan = spot_c2sr(args, work, pj)
            cmd = "W=$PWD; for u in cat head tail uniq; do (cd harness && python3 difftest.py --cases $W/subset/${u}_cases_v4.json --out $W/results/${u}_diff_v4.json); [ $? -le 1 ] || exit 1; done"
            s = dict(s, command=cmd)
            extra, comp = "", None
        else:
            extra, comp = subset_plan(s, work)
        why = skip_reason(s, args, work)
        if why:
            print(f"SKIP {sid}: {why}")
            continue
        if extra is None:
            print(f"SKIP {sid}: no subset rule implemented")
            continue
        args.extra_args = {s["id"]: extra}
        # delete the shipped copy of every output first, so a stage that fails to write cannot be mistaken for a match
        base0 = work
        if "managed" in s["where"] and s["where"]["managed"]["var"] == "WORK":
            base0 = os.path.join(managed_root(s, args), s["where"]["managed"]["tree"])
        for o in expand_u(real_outputs(s)):
            try:
                os.remove(os.path.join(base0, o))
            except OSError:
                pass
        # stages whose input is another stage's build output (sactor/crust/grid.py reuses the C lib.a that crust/run.py builds)
        for pre_id in {"sactor.crust_grid": ["sactor.crust_owntests"]}.get(sid, []):
            ps = pick(pj, pre_id)[0]
            args.extra_args = {ps["id"]: extra}
            print(f"[spot] prerequisite {pre_id} {extra}")
            pst, _, plog = run_stage(ps, args, work)
            if not stage_ok(ps, pst):
                print(f"       prerequisite {pst}, log {plog}")
        args.extra_args = {s["id"]: extra}
        print(f"[spot] {sid} {extra}")
        st, sec, log = run_stage(s, args, work)
        if not stage_ok(s, st):          # exit 1 is a harness's "divergences found" status (e.g. syzygy/run.py)
            print(f"FAIL {sid}: {st}, log {log}")
            total_bad += 1
            continue
        if sid == "c2saferrust.diff":
            n_ok = n_all = 0
            diffs = []
            for u, (n, ver) in plan.items():
                fr = jload(os.path.join(work, "results", f"{u}_diff_v4.json"))["verdicts"]
                for r in fr:
                    n_all += 1
                    o = ver.get(r["id"])
                    f = ["assessable", "stdout_match", "exit_match", "stderr_match", "mismatch", "stderr_divergence", "any_divergence"]
                    if o and all(r.get(x) == o.get(x) for x in f):
                        n_ok += 1
                    else:
                        diffs.append(f"{u}/{r['id']}: " + ", ".join(f"{x} {o.get(x) if o else None!r}->{r.get(x)!r}" for x in f if not o or r.get(x) != o.get(x)))
        else:
            base = work
            if "managed" in s["where"] and s["where"]["managed"]["var"] == "WORK":
                base = os.path.join(managed_root(s, args), s["where"]["managed"]["tree"])
            n_ok, n_all, diffs = comp(base)
        print(f"RESULT {sid}: reproduced {n_ok}/{n_all} in {sec:.0f}s" + ("" if not diffs else "; differences:"))
        for d in diffs[:20]:
            print("   ", d)
        total_bad += 0 if n_ok == n_all else 1
    return 1 if total_bad else 0


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sp = ap.add_subparsers(dest="cmd", required=True)
    a = sp.add_parser("list")
    a.add_argument("--system")
    a.set_defaults(fn=cmd_list)
    a = sp.add_parser("show")
    a.add_argument("target")
    a.set_defaults(fn=cmd_show)

    def common(p):
        p.add_argument("--out", default=os.path.join(REPO, "out", "pipeline"))
        p.add_argument("--runner", choices=["docker", "local"], default="local" if os.environ.get("CRUST_IMAGE") else "docker")
        p.add_argument("--emulate", action="store_true", help="run stages for another architecture (Docker emulation)")
        p.add_argument("--network", action="store_true", help="allow stages that download")
        p.add_argument("--cpus", type=int, default=0, help="docker --cpus")
        p.add_argument("--image", help="run every docker stage in this image instead of the stage's own")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--managed-src", action="append", metavar="SYSTEM=DIR", help="directory holding the layout a wrapper needs (artifact, vendor, ...)")
    a = sp.add_parser("run")
    a.add_argument("system", help="all, or a comma list of systems / system.stage ids")
    a.add_argument("stages", nargs="*")
    a.add_argument("--in-place", action="store_true", help="write into the repository's results/ (overwrites shipped files)")
    common(a)
    a.set_defaults(fn=lambda x: (setattr(x, "managed_src", dict(m.split("=", 1) for m in (x.managed_src or []))), cmd_run(x))[1])
    a = sp.add_parser("hashes")
    a.add_argument("--out", default=os.path.join(REPO, "out", "pipeline"))
    a.set_defaults(fn=cmd_hashes)
    a = sp.add_parser("verify")
    a.add_argument("--out", default=os.path.join(REPO, "out", "pipeline"))
    a.set_defaults(fn=cmd_verify)
    a = sp.add_parser("spotcheck")
    a.add_argument("stages", nargs="+", help="stage ids, e.g. vert.diff rustassure.diff")
    common(a)
    a.set_defaults(fn=cmd_spotcheck)
    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
