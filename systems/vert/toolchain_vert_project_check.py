"""M3 item 2 follow-up: do VERT's 17 scalar translations that fail in our driver compile inside
VERT's OWN, unmodified project under VERT's pinned toolchain?

VERT's c_results.csv records compile=1 for all of them. For each function we copy
benchmark/c_transcoder/<F>/out-rwasm-bolero (unchanged: Cargo.toml, Cargo.lock, src/main.rs) to a
temp dir, drop VERT's benchmark/rust-toolchain.toml next to it, and run
`cargo check --tests` (the bolero test target VERT's evaluator builds) under that pin. As a
second, dependency-free check we compile ONLY the LLM-output segment of main.rs (between the
`////// LLM Output //////` markers, what run.py extracts) as a library with `rustc --crate-type lib
--edition 2018`, under the pin and under 1.86.0. Controls: functions that build in our driver.

  python3 systems/vert/toolchain_vert_project_check.py --out /scratch/vert_project_check.json F1 F2 ...
(run in a container with nightly-2023-07-29 and 1.86.0 installed, network access for crates.io)
"""
import argparse, datetime, json, os, platform, re, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
BENCH = os.path.join(REPO, "artifacts", "vert", "benchmark")
TC = os.path.join(BENCH, "c_transcoder")
PIN = "nightly-2023-07-29"
MARK = "////// LLM Output //////"


def errs(txt):
    return [l for l in txt.splitlines() if re.match(r"error(\[E\d+\])?:", l)][:4]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--controls", default="")
    ap.add_argument("--mode", choices=["check", "build"], default="check",
                    help="cargo check --tests (type errors) or cargo build --tests (also runs the MIR "
                         "lints such as unconditional_panic that `check` does not)")
    ap.add_argument("funcs", nargs="+")
    a = ap.parse_args()
    controls = [c for c in a.controls.split(",") if c]
    tgt = tempfile.mkdtemp(prefix="vert_tgt_")       # shared CARGO_TARGET_DIR: deps built once
    rows = []
    for f in a.funcs + controls:
        src = os.path.join(TC, f, "out-rwasm-bolero")
        with tempfile.TemporaryDirectory() as d:
            proj = os.path.join(d, "proj")
            shutil.copytree(src, proj)
            shutil.copy(os.path.join(BENCH, "rust-toolchain.toml"), os.path.join(d, "rust-toolchain.toml"))
            env = dict(os.environ, CARGO_TARGET_DIR=tgt)
            env.pop("RUSTUP_TOOLCHAIN", None)        # let VERT's rust-toolchain.toml choose
            v = subprocess.run(["rustc", "-V"], cwd=proj, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT).stdout.decode().strip()
            p = subprocess.run(["cargo", a.mode, "--tests", "--quiet"], cwd=proj, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            out = p.stdout.decode("utf8", "replace")
            text = open(os.path.join(proj, "src", "main.rs"), errors="replace").read()
            parts = text.split(MARK)
            seg = parts[1] if len(parts) >= 3 else None
            seg_res = {}
            if seg is not None:
                sf = os.path.join(d, "seg.rs")
                open(sf, "w").write(seg)
                for tc in (PIN, "1.86.0"):
                    q = subprocess.run(["rustc", f"+{tc}", "--crate-type", "lib", "--edition", "2018",
                                        "-A", "warnings", sf, "-o", os.path.join(d, "seg.rlib")],
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    seg_res[tc] = {"ok": q.returncode == 0, "errors": errs(q.stdout.decode("utf8", "replace"))}
            rows.append({"func": f, "role": "control" if f in controls else "our_driver_build_fail",
                         "vert_project_rustc": v, "cargo_mode": a.mode, "cargo_check_tests_ok": p.returncode == 0,
                         "cargo_errors": errs(out), "cargo_tail": out.strip().splitlines()[-3:] if p.returncode else [],
                         "llm_segment_as_lib": seg_res})
            print(f, rows[-1]["cargo_check_tests_ok"], {k: x["ok"] for k, x in seg_res.items()}, flush=True)
    vert_csv = {}
    for line in open(os.path.join(REPO, "artifacts", "vert", "c_results.csv")):
        m = re.match(r"\s*([A-Za-z0-9_]+),\s*compile=(\d)", line)
        if m:
            vert_csv[m.group(1)] = int(m.group(2))
    res = {"provenance": {"script": "systems/vert/toolchain_vert_project_check.py", "command": " ".join(sys.argv),
                          "git_rev": os.environ.get("GIT_REV"), "host": os.environ.get("HOST_DESC"),
                          "arch": platform.machine(), "image": os.environ.get("IMAGE_DESC"),
                          "date_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()},
           "summary": {"checked": len(a.funcs), "controls": len(controls),
                       "vert_csv_compile_1": sum(1 for r in rows if vert_csv.get(r["func"]) == 1 and r["role"] != "control"),
                       "cargo_check_tests_ok_under_pin": sum(1 for r in rows if r["role"] != "control" and r["cargo_check_tests_ok"]),
                       "cargo_check_tests_fail_under_pin": [r["func"] for r in rows if r["role"] != "control" and not r["cargo_check_tests_ok"]],
                       "llm_segment_fails_under_pin": [r["func"] for r in rows if r["role"] != "control" and not r["llm_segment_as_lib"].get(PIN, {}).get("ok", True)],
                       "llm_segment_fails_under_1.86.0": [r["func"] for r in rows if r["role"] != "control" and not r["llm_segment_as_lib"].get("1.86.0", {}).get("ok", True)],
                       "controls_ok": {r["func"]: r["cargo_check_tests_ok"] for r in rows if r["role"] == "control"}},
           "rows": rows}
    json.dump(res, open(a.out, "w"), indent=2)
    print(json.dumps(res["summary"], indent=1))


if __name__ == "__main__":
    main()
