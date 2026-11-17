"""Toolchain audit (which compiler each system pins or documents, and the ones we used), and RustMap Rosetta rebuilds.

Sub-commands
  rustmap  (run INSIDE a container that has the toolchains listed with --toolchains)
      For each toolchain T: compile every RustMap Rosetta translation exactly as
      systems/rustmap/run_rosetta.py does (`rustc -A warnings <task>-c.rs -o bin`, plain rustc as in
      RustMap's 125-rosetta-rs.sh:16), record pass/fail + the first error lines, and run
      run_rosetta.py's own main() unchanged under RUSTUP_TOOLCHAIN=T (its differential verdicts).
      Also parses RustMap's shipped run log (rust_run_log.txt) to get the per-file compile result
      RustMap itself recorded, and compares it with each toolchain.
        python3 harness/toolchain_audit.py rustmap --toolchains 1.86.0,1.73.0 --out /scratch/x.json
  assemble (host, macOS; uses docker + ssh to measure image toolchains)
      Verifies every file:line evidence item below (the cited line must contain the cited text),
      measures which toolchains the harness images actually contain, folds in the RustMap rebuild
      files and the VERT pinned-nightly result files, and writes results/toolchain_audit.json.
        python3 harness/toolchain_audit.py assemble --rustmap-mac A.json --rustmap-linux B.json
"""
import argparse
import datetime
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
A = os.path.join(REPO, "artifacts")
LINUX = os.environ.get("LINUX_HOST", "linux-host")   # ssh name of the x86_64 Linux machine


# --------------------------------------------------------------------------- rustmap rebuild
def rustmap_own_log():
    p = os.path.join(A, "rustmap", "rust-code", "rosetta_code_gpt", "125-rosetta-code-gpt", "rust_run_log.txt")
    attempted, failed = [], []
    for line in open(p, errors="replace"):
        m = re.match(r"Compiling and running (\S+)", line)
        if m:
            attempted.append(m.group(1))
        m = re.match(r"Compilation failed for (\S+)", line)
        if m:
            failed.append(m.group(1))
    return {"file": os.path.relpath(p, REPO), "attempted": len(attempted), "failed": sorted(failed),
            "n_failed": len(failed), "attempted_files": attempted}


def cmd_rustmap(args):
    sys.path.insert(0, os.path.join(REPO, "systems", "rustmap"))
    import run_rosetta as rr
    pairs = rr.pairs()
    own = rustmap_own_log()
    out = {"pairs": len(pairs), "rustmap_own_log": {k: v for k, v in own.items() if k != "attempted_files"},
           "toolchains": {}}
    # which pairs' C builds (run_rosetta skips the Rust build when the C fails)
    c_ok = {}
    with tempfile.TemporaryDirectory() as d:
        for task, cmain, rfile in pairs:
            c_ok[task] = rr.build_c(cmain, d) is not None
    out["c_build_fail"] = sorted(t for t, ok in c_ok.items() if not ok)
    for tc in args.toolchains.split(","):
        env = dict(os.environ, RUSTUP_TOOLCHAIN=tc)
        ver = subprocess.run(["rustc", "-vV"], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT).stdout.decode().strip()
        per = {}
        for task, cmain, rfile in pairs:
            with tempfile.TemporaryDirectory() as d:
                p = subprocess.run(["rustc", "-A", "warnings", rfile, "-o", os.path.join(d, "r_bin")],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                err = p.stderr.decode("utf8", "replace")
                per[task] = {"ok": p.returncode == 0,
                             "errors": [l for l in err.splitlines() if l.startswith("error")][:3]}
        fails = sorted(t for t, v in per.items() if not v["ok"])
        fails_c_ok = sorted(t for t in fails if c_ok[t])
        own_failed = {f[:-len("-c.rs")] for f in own["failed"]}
        own_attempted = {f[:-len("-c.rs")] for f in own["attempted_files"]}
        common = [t for t in per if t in own_attempted]
        agree = sum(1 for t in common if (t in own_failed) == (not per[t]["ok"]))
        # run_rosetta.py's own main(), unchanged, under this toolchain
        rosetta_out = os.path.join(args.scratch, f"rustmap_rosetta_{tc}_{platform.machine()}.json")
        r = subprocess.run([sys.executable, os.path.join(REPO, "systems", "rustmap", "run_rosetta.py"),
                            "--out", rosetta_out], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        summ = json.load(open(rosetta_out))["summary"] if r.returncode == 0 else {"error": r.stdout.decode()[-2000:]}
        abort_lines = sorted({m for v in per.values() for e in v["errors"]
                              for m in re.findall(r"aborting due to .*", e)})
        out["toolchains"][tc] = {
            "rustc": ver, "compile_fail_all_124": len(fails), "compile_fail_among_c_buildable": len(fails_c_ok),
            "compile_fail_tasks": fails,
            "first_errors": {t: per[t]["errors"] for t in fails},
            "vs_rustmap_own_log": {"files_in_both": len(common), "same_compile_outcome": agree,
                                   "fail_here_not_in_log": sorted(t for t in fails if t in own_attempted and t not in own_failed),
                                   "fail_in_log_not_here": sorted(t for t in own_failed if t in per and per[t]["ok"]),
                                   "not_in_log": sorted(t for t in per if t not in own_attempted)},
            "abort_message_forms": abort_lines,
            "run_rosetta_summary": summ,
        }
    out["provenance"] = {"script": "harness/toolchain_audit.py rustmap", "command": " ".join(sys.argv),
                         "arch": platform.machine(), "host": os.environ.get("HOST_DESC"),
                         "image": os.environ.get("IMAGE_DESC"), "git_rev": os.environ.get("GIT_REV"),
                         "gcc": subprocess.run(["gcc", "--version"], stdout=subprocess.PIPE).stdout.decode().splitlines()[0],
                         "date_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    json.dump(out, open(args.out, "w"), indent=2)
    print(json.dumps({tc: {k: v[k] for k in ("compile_fail_all_124", "compile_fail_among_c_buildable", "vs_rustmap_own_log")}
                      for tc, v in out["toolchains"].items()}, indent=1))


# --------------------------------------------------------------------------- rustassure summary
def cmd_rustassure(args):
    """Summarise systems/vert/toolchain_rustassure.sh's outputs (DIR) against the repo's primary
    RustAssure results. Pure file comparison; writes ARGS.out."""
    D = args.dir

    def load(p):
        return json.load(open(p)) if os.path.isfile(p) else None
    prim_pf = load(os.path.join(REPO, "results", "rustassure_diff.json"))
    prim_u8c = load(os.path.join(REPO, "systems", "rustassure", "u8c_e2e", "results.json"))
    prim_csv = load(os.path.join(REPO, "systems", "rustassure", "libcsv_e2e", "results.json"))
    scal = lambda s: {k: v for k, v in s.items() if not isinstance(v, (list, dict))}  # noqa: E731
    out = {"documented_toolchain": "1.64.0 (artifacts/rustassure/README.md:38-41)",
           "primary_toolchains": {"per_function": "1.86.0", "u8c": "1.86.0", "libcsv": "nightly-2022-08-08"},
           "versions_txt": open(os.path.join(D, "versions.txt")).read().splitlines(),
           "per_function": {"primary": scal(prim_pf["summary"])}, "u8c": {}, "libcsv": {}}
    for tc in ("1.86.0", "1.64.0"):
        r = load(os.path.join(D, f"rustassure_diff_{tc}.json"))
        out["per_function"][tc] = scal(r["summary"]) if r else None
        if r:
            out["per_function"][tc]["same_as_primary"] = (scal(r["summary"]) == scal(prim_pf["summary"])
                                                          and r["value_bugs"] == prim_pf["value_bugs"])
    pu = {"n_cases": prim_u8c["n_cases"], "agree": prim_u8c["agree"], "divergent": prim_u8c["divergent"]}
    out["u8c"]["primary (repo results.json, platform not recorded)"] = pu
    for tag in ("1.86.0", "1.64.0", "1.86.0-funsigned-char", "1.86.0-fsigned-char"):
        r = load(os.path.join(D, f"u8c_{tag}.json"))
        if r:
            out["u8c"][tag] = {"n_cases": r["n_cases"], "agree": r["agree"], "divergent": r["divergent"],
                               "same_divergent_inputs_as_primary": r["divergent_inputs"] == prim_u8c["divergent_inputs"],
                               "extra_vs_primary": sorted(set(r["divergent_inputs"]) - set(prim_u8c["divergent_inputs"])),
                               "missing_vs_primary": sorted(set(prim_u8c["divergent_inputs"]) - set(r["divergent_inputs"]))}
    out["libcsv"]["primary"] = {k: prim_csv[k] for k in ("n_cases", "agree", "divergent", "divergence_by_channel")}
    for tc in ("nightly-2022-08-08", "1.64.0"):
        r = load(os.path.join(D, f"libcsv_{tc}.json"))
        if r:
            out["libcsv"][tc] = {k: r[k] for k in ("n_cases", "agree", "divergent", "divergence_by_channel")}
            out["libcsv"][tc]["identical_to_primary"] = all(r[k] == prim_csv[k] for k in
                                                            ("n_cases", "agree", "divergent", "divergence_by_channel",
                                                             "divergence_by_option", "samples"))
    out["provenance"] = {"script": "harness/toolchain_audit.py rustassure (summarises systems/vert/toolchain_rustassure.sh)",
                         "command": " ".join(sys.argv), "runs_host": "Linux x86-64 (desktop), private repo copy, docker --cpus 4/1",
                         "date_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    json.dump(out, open(args.out, "w"), indent=2)
    print(json.dumps(out, indent=1)[:4000])


# --------------------------------------------------------------------------- static audit
# (path relative to repo, 1-based line, text that line must contain)
EVIDENCE = {
    "VERT": {
        "pin": [("artifacts/vert/benchmark/rust-toolchain.toml", 2, 'channel = "nightly-2023-07-29"')],
        "harness": [("systems/vert/run.py", 18, "crust-stable"),
                    ("systems/vert/run.py", 176, '["rustc", "-O", "-A", "warnings", rfile, "-o", binr]'),
                    ("systems/vert/run_arrays.py", 14, "crust-stable"),
                    ("systems/vert/run_arrays.py", 241, '["rustc", "-O", "-A", "warnings", rfile, "-o", binr]'),
                    ("docker/Dockerfile.stable", 14, "--default-toolchain 1.86.0"),
                    ("systems/vert/bolero_vacuity/run_in_container.sh", 10, "rustup toolchain install nightly-2023-07-29")],
        "note": "run.py/run_arrays.py compile a standalone prog.rs in a temp dir from the repo root, so the "
                "benchmark's rust-toolchain.toml is never consulted; the toolchain is whatever the image "
                "defaults to. VERT's own Kani/bolero re-runs (bolero_vacuity, kani_vacuity) install the pin.",
    },
    "Flourine": {
        "pin": [],
        "pin_search": "no rust-toolchain(.toml), Dockerfile or toolchain line in artifacts/flourine_dl/flourine-artifact "
                      "(README is 65 lines and names no Rust version)",
        "artifact_hints": [("artifacts/flourine_dl/flourine-artifact/fixer.py", 27, 'RUSTFLAGS="-Z track-diagnostics -Z time-passes"')],
        "harness": [("systems/flourine/run.py", 9, "crust-stable"),
                    ("systems/flourine/run.py", 97, '["rustc", "-O", "-A", "warnings", p, "-o", b]'),
                    ("systems/flourine/run_struct.py", 22, "crust-stable"),
                    ("docker/Dockerfile.stable", 14, "--default-toolchain 1.86.0")],
        "note": "Flourine's own pipeline passes -Z flags (nightly-only) but names no nightly; nothing to match.",
    },
    "RustAssure": {
        "pin": [("artifacts/rustassure/README.md", 38, "downgrade to version 1.64.0"),
                ("artifacts/rustassure/README.md", 40, "rustup install 1.64.0")],
        "harness": [("systems/rustassure/run.py", 8, "crust-stable"),
                    ("systems/rustassure/run.py", 92, '["rustc", "-O", "-A", "warnings", p, "-o", b]'),
                    ("systems/rustassure/libcsv_e2e/run.py", 10, "crust-heldout"),
                    ("systems/rustassure/u8c_e2e/run.py", 8, "crust-stable"),
                    ("docker/Dockerfile", 28, "rustup toolchain install nightly-2022-08-08"),
                    ("systems/rustassure/libcsv_e2e/sut_crate/Cargo.toml", 7, "only compiles on a pre-1.73 toolchain")],
        "note": "README (a documented install step, not a rust-toolchain file) says 1.64.0; the harness uses 1.86.0 "
                "(per-function, u8c) and nightly-2022-08-08 = rustc 1.65.0-nightly (libcsv).",
    },
    "C2SaferRust": {
        "pin": [("artifacts/c2saferrust/coreutils/src/head/rust_WIP/rust-toolchain.toml", 2, 'channel = "nightly-2022-08-08"'),
                ("artifacts/c2saferrust/coreutils/src/tail/rust_WIP/rust-toolchain.toml", 2, 'channel = "nightly-2022-08-08"'),
                ("artifacts/c2saferrust/coreutils/src/uniq/rust_WIP/rust-toolchain.toml", 2, 'channel = "nightly-2022-08-08"'),
                ("artifacts/c2saferrust/coreutils/src/cat/rust_WIP/rust-toolchain.bak", 2, 'channel = "nightly-2022-08-08"')],
        "harness": [("docker/Dockerfile", 28, "rustup toolchain install nightly-2022-08-08"),
                    ("docker/Dockerfile", 30, "rustup default nightly-2022-08-08"),
                    ("harness/build.py", 5, "nightly-2022-08-08"),
                    ("harness/runall.sh", 6, "crust-heldout")],
        "recorded": "results/{cat,head,tail,uniq}_diff_v4.json record rustc 1.65.0-nightly (d394408fb 2022-08-07) = nightly-2022-08-08",
        "note": "cat/rust_WIP ships rust-toolchain.bak (inactive), so cat builds with the image default, which is the same channel.",
    },
    "Syzygy": {
        "pin": [],
        "pin_search": "no rust-toolchain file; README says only `cargo build --release`",
        "artifact_hints": [("artifacts/syzygy_zopfli/rust_code/Cargo.toml", 4, 'edition = "2024"'),
                           ("artifacts/syzygy_zopfli/README.md", 7, "cargo build --release")],
        "harness": [("docker/Dockerfile.syzygy", 14, "--default-toolchain 1.86.0"),
                    ("systems/syzygy/run.py", 10, "crust-syzygy")],
        "recorded": "results/syzygy_empty_deflate.json records rustc 1.86.0 (05f9846f8 2025-03-31)",
        "note": "edition 2024 needs rustc >= 1.85, which 1.86.0 satisfies; nothing narrower is pinned.",
    },
    "RustMap": {
        "pin": [],
        "pin_search": "Rosetta translations: no rust-toolchain file; RustMap's runner is plain `rustc \"$file\"` with no "
                      "version. Pins that exist are for other trees: cognitive_study/rossta-c2rust/*/rust-toolchain.toml "
                      "(nightly-2022-08-08, C2Rust outputs) and scaffolding_test/bzip2/rust-toolchain (nightly-2019-12-05). "
                      "README points to a Docker image (cxm211/rustmap-new) whose metadata records no toolchain.",
        "artifact_hints": [("artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/125-rosetta-rs.sh", 16, 'rustc "$file"'),
                           ("artifacts/rustmap/README.md", 76, "cxm211/rustmap-new"),
                           ("artifacts/rustmap/rust-code/bzip2_rs_gpt/Cargo.toml", 4, 'edition = "2021"'),
                           ("artifacts/rustmap/cognitive_study/rossta-c2rust/100_doors/rust-toolchain.toml", 2, 'channel = "nightly-2022-08-08"'),
                           ("artifacts/rustmap/scaffolding_test/bzip2/rust-toolchain", 1, "nightly-2019-12-05")],
        "harness": [("systems/rustmap/run_rosetta.py", 14, "crust-stable"),
                    ("systems/rustmap/run_rosetta.py", 97, '["rustc", "-A", "warnings", rfile, "-o", b]'),
                    ("systems/rustmap/check_bzip2.sh", 8, "crust-syzygy"),
                    ("docker/Dockerfile.stable", 14, "--default-toolchain 1.86.0")],
        "recorded": "results/rustmap_bzip2_check.json records rustc 1.86.0 (05f9846f8 2025-03-31); results/rustmap_rosetta.json records none",
    },
}

STATUS = {
    "VERT": "MISMATCH: pinned nightly-2023-07-29, primary scalar and array/float runs used rustc 1.86.0 "
            "(Kani/bolero re-runs used the pin). Effect measured in vert_diff_pinned_nightly.json / vert_arrays_pinned_nightly.json.",
    "Flourine": "NO PIN: artifact implies an unspecified nightly (-Z flags); harness used rustc 1.86.0; nothing to match.",
    "RustAssure": "MISMATCH: README documents 1.64.0; harness used 1.86.0 (per-function, u8c) and 1.65.0-nightly (libcsv).",
    "C2SaferRust": "MATCH: pinned nightly-2022-08-08; harness image defaults to it and results record rustc 1.65.0-nightly (2022-08-07).",
    "Syzygy": "NO PIN: edition 2024 implies >= 1.85; harness used 1.86.0 (amd64).",
    "RustMap": "NO PIN for the Rosetta or bzip2 translations; harness used rustc 1.86.0. RustMap's own Rosetta log "
               "is compared per file with several toolchains in rustmap_rebuild.",
}


def check_evidence(items):
    """Third-party artifacts are read from disk (gitignored); study-repo files are read from the
    committed HEAD (`git show HEAD:path`), because the working tree may hold uncommitted edits
    to docker/* and the runs being audited used the committed harness."""
    out = []
    for path, line, text in items:
        if path.startswith("artifacts/"):
            src = "disk"
            try:
                lines = open(os.path.join(REPO, path), errors="replace").read().splitlines()
            except OSError:
                lines = []
        else:
            src = "git HEAD"
            r = subprocess.run(["git", "-C", REPO, "show", f"HEAD:{path}"], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
            lines = r.stdout.decode("utf8", "replace").splitlines() if r.returncode == 0 else []
        got = lines[line - 1] if 0 < line <= len(lines) else None
        out.append({"at": f"{path}:{line}", "read_from": src, "expect": text, "line": got,
                    "verified": bool(got is not None and text in got)})
    return out


def sh(cmd, timeout=180):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return p.stdout.decode("utf8", "replace").strip()
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def image_toolchains(images, remote=None):
    out = {}
    for img in images:
        probe = ("rustup toolchain list 2>/dev/null; echo RUSTC=$(rustc -V); echo GCC=$(gcc --version | head -1); "
                 "echo CLANG=$(command -v clang || echo none)")
        if remote:
            q = (f"docker image inspect {img} --format '{{{{.Id}}}} {{{{.Architecture}}}}' && "
                 f"docker run --rm {img} bash -c {shlex.quote(probe)}")
            txt = sh(["ssh", remote, q])
        else:
            iid = sh(["docker", "image", "inspect", img, "--format", "{{.Id}} {{.Architecture}}"])
            txt = iid + "\n" + sh(["docker", "run", "--rm", img, "bash", "-c", probe])
        out[img] = txt.splitlines()
    return out


def cmd_assemble(args):
    systems = {}
    for name, ev in EVIDENCE.items():
        rec = {"status": STATUS[name]}
        for k in ("pin", "artifact_hints", "harness"):
            if k in ev:
                rec[k] = check_evidence(ev[k])
        for k in ("pin_search", "note", "recorded"):
            if k in ev:
                rec[k] = ev[k]
        systems[name] = rec
    all_ok = all(e["verified"] for s in systems.values() for k in ("pin", "artifact_hints", "harness")
                 for e in s.get(k, []))
    images = {"mac_arm64": image_toolchains(["crust-stable", "crust-vert-toolchain"]),
              "linux_x86_64": image_toolchains(["crust-stable", "crust-heldout", "crust-syzygy", "crust-afl",
                                                "crust-afl-nightly"], remote=LINUX)}
    out = {
        "provenance": {
            "script": "harness/toolchain_audit.py", "command": " ".join(["python3"] + sys.argv),
            "git_rev": sh(["git", "-C", REPO, "rev-parse", "HEAD"]),
            "host": platform.platform(), "arch": platform.machine(),
            "date_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "images_probed": "docker run <image> rustup toolchain list / rustc -V / gcc --version / command -v clang",
        },
        "all_evidence_lines_verified": all_ok,
        "systems": systems,
        "harness_images_measured": images,
    }
    # Linux runs used ~/Desktop/c-to-rust-validation/artifacts (old checkout) read-only: check the
    # inputs they read are byte-identical to the Mac copy.
    sets = {
        "vert_c_transcoder_c_and_out-rwasm-bolero": ("vert", "find benchmark/c_transcoder -type f \\( -name '*.c' -o -path '*out-rwasm-bolero*' \\) -not -path '*/target/*'"),
        "vert_c_results_csv_and_pin": ("vert", "printf 'c_results.csv\\nbenchmark/rust-toolchain.toml\\n'"),
        "rustassure_inputs-complex_and_Link": ("rustassure", "find src/python/inputs-complex src/Link -type f -not -path '*/target/*'"),
        "rustmap_rosetta_c_and_rs": ("rustmap", "find c-code/rosseta-125 rust-code/rosetta_code_gpt/125-rosetta-code-gpt -type f \\( -name '*.c' -o -name '*.rs' -o -name '*.txt' \\)"),
    }
    sums = {}
    for k, (d, find) in sets.items():
        mac = sh(["bash", "-c", f"cd {A}/{d} && {find} | LC_ALL=C sort | tr '\\n' '\\0' | xargs -0 shasum -a 256 | shasum -a 256 | cut -c1-64; {find} | wc -l"])
        lin = sh(["ssh", LINUX, f"cd ~/Desktop/c-to-rust-validation/artifacts/{d} && {find} | LC_ALL=C sort | tr '\\n' '\\0' | xargs -0 sha256sum | sha256sum | cut -c1-64; {find} | wc -l"])
        sums[k] = {"mac": mac.split(), "linux_old_checkout": lin.split(), "identical": mac.split() == lin.split()}
    out["artifact_digests_mac_vs_linux"] = sums
    # Which toolchain did each x86-64 VERT result use? Evidence inside each file + repo docs.
    x86 = {}
    for f in ("results/vert_diff_stable_x86.json", "results/vert_diff_msan_x86.json",
              "results/vert_diff_x86_fixed.json", "results/vert_diff_x86_spread.json"):
        d = json.load(open(os.path.join(REPO, f)))
        t = [r for r in d["all"] if r.get("status") == "tested"]
        s = d["summary"]
        x86[f] = {"tested": len(t), "value_bug": s.get("value_bug"),
                  "functions_with_msan_true": sum(1 for r in t if r.get("msan")),
                  "functions_with_alt_true": sum(1 for r in t if r.get("alt")),
                  "toolchain_recorded_in_file": s.get("toolchain"),
                  "provenance_in_file": d.get("provenance"),
                  "last_commit": sh(["git", "-C", REPO, "log", "-1", "--format=%h %ad %s", "--date=iso", "--", f])}
    out["x86_vert_results_toolchain"] = {
        "files": x86,
        "evidence": [
            "run.py builds the MSan oracle only when `clang` is on PATH (systems/vert/run.py build_c mode 'msan'); "
            "every tested function in all four files has msan=True, so each ran in an image with clang.",
            "On the Linux box every image with clang (crust-stable, crust-heldout, crust-afl, crust-afl-nightly) has "
            "only nightly-2022-08-08 = rustc 1.65.0-nightly (d394408fb 2022-08-07); crust-stable and crust-heldout are "
            "the same image id. The 1.86.0 x86 images (crust-syzygy) have no clang. See harness_images_measured.",
            "vert_diff_x86_fixed.json records 'rust nightly-2022-08-08' in summary.toolchain; pipeline.json (stage "
            "vert.diff_x86_spread) records that the spread re-run used crust-heldout with MSan.",
            "Reproduction (x86_rerun below): the current harness re-run on x86-64 in one image under both candidate "
            "toolchains, each diffed per function against vert_diff_x86_spread.json.",
        ],
    }
    reruns = {}
    for spec in args.x86_rerun or []:
        label, path = spec.split("=", 1)
        d = json.load(open(path))
        tc = d["toolchain_comparison"]
        reruns[label] = {"file_on_linux": path, "provenance": d["provenance"], "summary": {
            k: d["summary"].get(k) for k in ("tested", "agree", "value_bug", "rust_panic", "rust_hang", "ub_only",
                                             "timeout_only", "rust-build-fail")},
            "rust_build_fail": tc["compile"]["rust_build_fail"],
            "diffs": {k: {"n_verdict_changes": v["n_verdict_changes"], "verdict_changes": v["verdict_changes"]}
                      for k, v in tc["diffs"].items()},
            "value_bug_functions": d["summary"]["value_bug_functions"]}
    out["x86_vert_results_toolchain"]["reproduction"] = reruns
    concl = []
    for label, r in reruns.items():
        concl.append(f"{label}: {r['summary']['value_bug']} wrong / {r['summary']['tested']} tested; "
                     f"{r['diffs']['primary']['n_verdict_changes']} per-function verdict changes vs vert_diff_x86_spread.json"
                     + "".join(f"; {r['diffs'][k]['n_verdict_changes']} vs {k}" for k in r["diffs"] if k != "primary"))
    out["x86_vert_results_toolchain"]["reproduction_outcome"] = concl
    rm = {}
    for label, path in (("mac_arm64", args.rustmap_mac), ("linux_x86_64", args.rustmap_linux)):
        if path and os.path.isfile(path):
            rm[label] = json.load(open(path))
    out["rustmap_rebuild"] = rm
    vert = {}
    for label, path in (("scalar", "results/vert_diff_pinned_nightly.json"),
                        ("arrays", "results/vert_arrays_pinned_nightly.json")):
        p = os.path.join(REPO, path)
        if os.path.isfile(p):
            d = json.load(open(p))
            tcmp = d["toolchain_comparison"]
            vert[label] = {"file": path, "summary_pinned": d["summary"],
                           "summary_control_1.86.0": d.get("control_same_session", {}).get("summary"),
                           "compile_pinned": {k: tcmp["compile"][k] for k in ("rust_build_ok_among_in_scope", "rust_build_fail")},
                           "compile_control_1.86.0": {k: d.get("control_same_session", {}).get("compile", {}).get(k)
                                                     for k in ("rust_build_ok_among_in_scope", "rust_build_fail")},
                           "verdict_changes_vs": {k: v["n_verdict_changes"] for k, v in tcmp["diffs"].items()}}
    out["vert_pinned"] = vert
    for p in args.extra or []:
        label, path = p.split("=", 1)
        out[label] = json.load(open(path))
    # compact, computed summary
    summ = {"status_per_system": STATUS}
    for label, r in rm.items():
        summ[f"rustmap_rosetta_{label}"] = {
            tc: {"compile_fail_of_124": v["compile_fail_all_124"],
                 "same_outcome_as_rustmap_own_log": f"{v['vs_rustmap_own_log']['same_compile_outcome']}/{v['vs_rustmap_own_log']['files_in_both']}",
                 "run_rosetta": {k: v["run_rosetta_summary"].get(k) for k in
                                 ("agree", "agree_after_formatting", "agree_float_tolerance", "divergent_real",
                                  "divergent_nondeterministic", "timeout", "c_build_fail", "rust_build_fail")}}
            for tc, v in r["toolchains"].items()}
    for k in ("vert_own_project_scalar", "vert_own_project_arrays", "vert_own_project_arrays_cargo_build"):
        if k in out:
            summ[k] = out[k]["summary"]
    if "rustassure" in out:
        ra = out["rustassure"]
        summ["rustassure"] = {"per_function_1.64.0_same_as_primary": ra["per_function"]["1.64.0"].get("same_as_primary"),
                              "libcsv": {k: {kk: v.get(kk) for kk in ("agree", "divergent", "identical_to_primary")}
                                         for k, v in ra["libcsv"].items()},
                              "u8c": {k: {kk: v.get(kk) for kk in ("agree", "divergent")} for k, v in ra["u8c"].items()}}
    summ["vert_pinned"] = vert
    summ["x86_vert_reproduction"] = out["x86_vert_results_toolchain"]["reproduction_outcome"]
    out = {"provenance": out.pop("provenance"), "summary": summ, **out}
    json.dump(out, open(os.path.join(REPO, "results", "toolchain_audit.json"), "w"), indent=2)
    print("evidence verified:", all_ok)
    for n, s in systems.items():
        bad = [e for k in ("pin", "artifact_hints", "harness") for e in s.get(k, []) if not e["verified"]]
        print(n, "unverified:", bad)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("rustmap")
    r.add_argument("--toolchains", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--scratch", required=True)
    s = sub.add_parser("rustassure")
    s.add_argument("--dir", required=True)
    s.add_argument("--out", required=True)
    a = sub.add_parser("assemble")
    a.add_argument("--rustmap-mac")
    a.add_argument("--rustmap-linux")
    a.add_argument("--extra", action="append", help="label=path of another JSON to embed")
    a.add_argument("--x86-rerun", action="append", help="label=path of a toolchain_x86_rerun.sh output")
    args = ap.parse_args()
    {"rustmap": cmd_rustmap, "rustassure": cmd_rustassure, "assemble": cmd_assemble}[args.cmd](args)


if __name__ == "__main__":
    main()
