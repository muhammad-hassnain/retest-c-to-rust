"""Provenance for result files: which commit, machine, toolchain and command produced them.

Library (for new runs; scripts/ on sys.path):

    from provenance import stamp, write_json
    write_json("out/x.json", {"summary": ...})      # adds a "_provenance" block, then writes
    d["_provenance"] = stamp(image="crust-stable")   # or stamp a dict yourself

CLI:

    python3 -I scripts/provenance.py                       # print stamp() for this machine
    python3 -I scripts/provenance.py --stamp --image IMG --step NAME   # stamp for one run step (run_all.sh)
    python3 -I scripts/provenance.py --index               # rewrite results/PROVENANCE.json
    python3 -I scripts/provenance.py --index --check       # exit 1 if results/PROVENANCE.json is stale
    python3 -I scripts/provenance.py --index --dev-repo DIR   # commits from the development repository

results/PROVENANCE.json maps every file under results/ to the git commit that last changed it
(`git log -1 --format=%H -- <file>`), says whether the working tree differs from that commit, and,
(This artifact's index is written with --dev-repo: the commits are those of the development repository
the study was run in, which is not shipped, so they survive any rewrite of this repository's history.
Without that repository, --check compares the file list and the documented hosts only.)
where the study's own documentation states it, the machine architecture / image the file was
produced on, with the sentence that says so. Files the documentation does not place are left null:
nothing is inferred. No host names are recorded.
"""
import datetime
import json
import os
import platform
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(REPO, "results", "PROVENANCE.json")


def _run(*cmd, cwd=REPO):
    try:
        p = subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True, timeout=30)
        return p.stdout.strip() if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _first_line(*cmd):
    out = _run(*cmd)
    return out.splitlines()[0] if out else None


def stamp(argv=None, image=None, step=None):
    """A provenance block for a result produced now, on this machine, from this checkout."""
    commit = _run("git", "rev-parse", "HEAD") or os.environ.get("CRUST_ARTIFACT_COMMIT")
    dirty = _run("git", "status", "--porcelain", "--untracked-files=no", "--", "harness", "systems", "scripts", "docker")
    return {
        "git_commit": commit,
        "git_dirty_code": (bool(dirty) if dirty is not None else None),   # uncommitted changes to harness/systems/scripts/docker
        "generated_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "arch": platform.machine(),
        "os": f"{platform.system()} {platform.release()}",
        "in_container": os.path.exists("/.dockerenv") or bool(os.environ.get("CRUST_IMAGE")),
        "image": image or os.environ.get("CRUST_IMAGE"),
        "step": step,
        "python": platform.python_version(),
        "gcc": _first_line("gcc", "--version"),
        "rustc": _first_line("rustc", "--version"),
        "argv": argv if argv is not None else sys.argv,
    }


def write_json(path, obj, **kw):
    """Write obj (a dict) to path with a "_provenance" block added."""
    obj = dict(obj)
    obj["_provenance"] = stamp(**kw)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=1, default=str)
    return path


# Where pipeline.json records the machine a result file was produced on. Each entry:
# (regex on the repo-relative path, the recorded machine, where pipeline.json records it).
DOCUMENTED = [
    (r"^results/vert_diff\.json$", "arm64 (crust-stable image)", "pipeline.json, stage vert.diff_grid_v1: where.arch = arm64"),
    (r"^results/vert_diff_x86_fixed\.json$", "x86_64 (MSan)", "pipeline.json, stage vert.diff_x86_legacy: where.arch = x86_64 (clang MSan)"),
    (r"^results/vert_diff_x86_spread\.json$", "x86_64 (crust-heldout image, MSan)", "pipeline.json, stage vert.diff_x86_spread: where.arch = x86_64 (clang MSan)"),
    (r"^results/vert_diff_(msan|stable)_x86\.json$", "x86_64", "pipeline.json, results_without_stage: x86 MSan / stable VERT variants"),
    (r"^results/(cat|head|tail|uniq)_(diff|mut)_v4\.json$", "x86_64 (crust-heldout image)", "pipeline.json, stages c2saferrust.diff and c2saferrust.mutation: where.arch = x86_64"),
    (r"^results/c2saferrust_ablation\.json$", "x86_64 (crust-heldout image)", "pipeline.json, stage c2saferrust.ablation: where.arch = x86_64"),
    (r"^results/(cat|head|tail|uniq)_diff_v4_c2rust_baseline\.json$", "arm64", "pipeline.json, stage c2saferrust.diff_c2rust_baseline: where.arch = arm64"),
    (r"^results/c2saferrust_ablation_c2rust_baseline\.json$", "x86_64", "pipeline.json, results_without_stage: harness/ablation.py re-run natively on x86_64"),
    (r"^results/syzygy_[a-z_]+\.json$", "x86_64 (amd64 image)", "pipeline.json, stages syzygy.diff and syzygy.empty_deflate: where.arch = x86_64"),
    (r"^results/rustmap_bzip2_check\.json$", "x86_64 (amd64 image)", "pipeline.json, stage rustmap.bzip2_check: where.arch = x86_64"),
    (r"^results/rustine_build_.*\.json$", "x86_64", "the file's own \"arch\" field; pipeline.json, stages rustine.build_186 and rustine.build_nightly: where.arch = x86_64"),
]


def _documented(rel):
    for pat, arch, src in DOCUMENTED:
        if re.search(pat, rel):
            return {"arch": arch, "source": src}
    return None


def build_index(dev=None):
    files = []
    for dp, dn, fn in os.walk(os.path.join(REPO, "results")):
        dn.sort()
        for f in sorted(fn):
            rel = os.path.relpath(os.path.join(dp, f), REPO).replace(os.sep, "/")
            if rel != "results/PROVENANCE.json" and not f.startswith("."):
                files.append(rel)
    if dev:
        rows = {}
        for rel in files:
            log = _run("git", "log", "-1", "--format=%H %cI", "--", rel, cwd=dev) or ""
            sha, _, date = log.partition(" ")
            if sha:
                try:
                    old = subprocess.run(["git", "show", f"{sha}:{rel}"], cwd=dev, capture_output=True, timeout=60).stdout
                    differs = old != open(os.path.join(REPO, rel), "rb").read()
                except (OSError, subprocess.SubprocessError):
                    differs = None
                rows[rel] = {"commit": sha, "commit_date": date or None, "differs_from_that_commit": differs}
                if differs:
                    rows[rel]["note"] = "this copy differs from the development repository's (machine identity scrubbed, or rebuilt in this artifact)"
            else:
                rows[rel] = {"commit": None, "commit_date": None, "differs_from_that_commit": None,
                             "note": "not in the development repository (written in this artifact)"}
            rows[rel]["documented_host"] = _documented(rel)
        return {
            "about": "Every file under results/ -> the commit of the DEVELOPMENT repository (where the study was run; not shipped) "
                     "that last changed it, whether this copy differs from that commit, and, where pipeline.json records it, the "
                     "architecture/image it was produced on. documented_host=null means it is not recorded; nothing is inferred. "
                     "Written with python3 -I scripts/provenance.py --index --dev-repo DIR; without DIR, --check compares the file "
                     "list and the documented hosts only.",
            "commits_from": "development repository",
            "dev_repo_head_when_written": _run("git", "rev-parse", "HEAD", cwd=dev),
            "files": rows,
            "counts": {"files": len(rows), "with_dev_commit": sum(r["commit"] is not None for r in rows.values()),
                       "differs_from_dev_commit": sum(bool(r["differs_from_that_commit"]) for r in rows.values()),
                       "with_documented_host": sum(r["documented_host"] is not None for r in rows.values())},
        }
    tracked = set((_run("git", "ls-files", "results") or "").splitlines())
    modified = set((_run("git", "diff", "--name-only", "HEAD", "--", "results") or "").splitlines())
    rows = {}
    for rel in files:
        if rel in tracked:
            log = _run("git", "log", "-1", "--format=%H %cI", "--", rel) or ""
            sha, _, date = log.partition(" ")
            rows[rel] = {"commit": sha or None, "commit_date": date or None,
                         "worktree_differs_from_commit": rel in modified}
        else:
            rows[rel] = {"commit": None, "commit_date": None, "worktree_differs_from_commit": None,
                         "note": "not committed when this index was written"}
        doc = _documented(rel)
        rows[rel]["documented_host"] = doc
    head = _run("git", "rev-parse", "HEAD")
    return {
        "about": "Every file under results/ -> the git commit that last changed it (git log -1 --format=%H -- <file>), "
                 "and, where the study's documentation says so, the architecture/image it was produced on. "
                 "documented_host=null means the docs do not say; nothing is inferred. A file committed in the same "
                 "commit as this index points at an older commit: regenerate after committing results "
                 "(python3 -I scripts/provenance.py --index) and commit this file on its own.",
        "repo_head_when_written": head,
        "files": rows,
        "counts": {"files": len(rows), "committed": sum(r["commit"] is not None for r in rows.values()),
                   "with_documented_host": sum(r["documented_host"] is not None for r in rows.values())},
    }


def main():
    a = sys.argv[1:]
    if "-h" in a or "--help" in a:
        print(__doc__.split("results/PROVENANCE.json maps")[0].strip())
        return
    if "--index" in a:
        dev = os.path.abspath(a[a.index("--dev-repo") + 1]) if "--dev-repo" in a else None
        new = build_index(dev)
        if "--check" in a:
            try:
                old = json.load(open(INDEX))
            except (OSError, ValueError):
                old = {}
            if old.get("commits_from") == "development repository" and not dev:
                # the commits are the development repository's: check the file list and documented hosts only
                stale = sorted(old.get("files", {})) != sorted(new["files"]) or \
                        any(old["files"][k].get("documented_host") != new["files"][k]["documented_host"] for k in new["files"] if k in old.get("files", {}))
                print("results/PROVENANCE.json is " + ("STALE" if stale else "current") + " (file list and documented hosts; commits are the development repository's)")
                sys.exit(1 if stale else 0)
            stale = old.get("files") != new["files"]
            print("results/PROVENANCE.json is " + ("STALE" if stale else "current"))
            sys.exit(1 if stale else 0)
        with open(INDEX, "w") as f:
            json.dump(new, f, indent=1)
        print(f"wrote {os.path.relpath(INDEX, REPO)}: {new['counts']}")
        return
    image = a[a.index("--image") + 1] if "--image" in a else None
    step = a[a.index("--step") + 1] if "--step" in a else None
    print(json.dumps(stamp(argv=None if "--stamp" not in a else [], image=image, step=step), indent=1))


if __name__ == "__main__":
    main()
