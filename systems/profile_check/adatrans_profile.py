"""AdaTrans: re-run the 3 counted crash defects under the OTHER cargo profile.

AdaTrans's own check and our harness build the translation with `cargo build` = DEBUG (overflow checks
ON): systems/adatrans/run_diff.py:12-15 (the artifact's check builds `cargo build --quiet` and runs
target/debug/solution) and :124-132 (our build, target/debug), rustc/cargo 1.86.0 in crust-heldout-plus186.
This script rebuilds the same main.rs (pipeline extract, <build>/extracted/run1/<inst>.rs) in a scratch
one-package workspace (run_diff.CARGO_TOML, the pipeline's vendored crates and Cargo.lock template,
--offline) in debug (the counted profile, re-confirmed) and `--release` (opt-level 3, overflow checks
off), and runs the unchanged C oracle binary the pipeline built (<build>/c/<inst>/ref, `gcc -w`) and both
Rust builds on every UB-clean exposing case of the translation (<build>/cases_run1_ub.jsonl rows with
kind panic and c_ub_clean true), with run_diff.run1 / run_diff.classify (same CPU limits, same strict
comparison). Each case's stdin is checked against the input_sha recorded by the pipeline.

One instance per process (the driver adatrans_profile.sh starts one container per defect in parallel):
  docker run --rm --network none --cpus 4 -u UID:GID -v REPO_COPY:/work:ro -v PIPE_BUILD:/b:ro \
    -v PIPE_INPUTS:/inputs:ro -v FUZZ:/fuzz:ro -v SCRATCH/<inst>:/s -v OUT:/out -w /work \
    crust-heldout-plus186 python3 -I systems/profile_check/adatrans_profile.py run <inst> /out/adatrans_<inst>.json
  python3 -I systems/profile_check/adatrans_profile.py merge OUT/adatrans.json OUT/adatrans_C_*.json
The per-defect files hold no timings or paths outside the container, so they are identical whether the
three run in parallel or in sequence.
"""
import hashlib, json, os, shutil, subprocess, sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "systems", "adatrans"))
import run_diff as RD  # noqa: E402

DEFECTS = {"C_421_2": "ADATRANS-run1-C_421_2", "C_430_2": "ADATRANS-run1-C_430_2", "C_432_4": "ADATRANS-run1-C_432_4"}
EXAMPLE = {"C_421_2": ("ours", "029.in"), "C_430_2": ("ours", "017.in"), "C_432_4": ("ours", "000.in")}  # ledger example cases
B, INPUTS, FUZZ, S = "/b", "/inputs", "/fuzz", "/s"
DIVERGENT = ("panic", "crash", "hang", "S1", "S2")   # whitespace_only and agree are not counted divergences


def sha_file(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def build(inst):
    name = f"run1_{inst}".lower()   # same package name as the pipeline (panic messages name run1_c_xxx/src/main.rs)
    ws = os.path.join(S, "ws")
    shutil.rmtree(ws, ignore_errors=True)
    os.makedirs(os.path.join(ws, name, "src"))
    shutil.copy(os.path.join(B, "extracted", "run1", inst + ".rs"), os.path.join(ws, name, "src", "main.rs"))
    open(os.path.join(ws, name, "Cargo.toml"), "w").write(RD.CARGO_TOML.format(name=name))
    open(os.path.join(ws, "Cargo.toml"), "w").write(f'[workspace]\nresolver = "2"\nmembers = ["{name}"]\n')
    shutil.copy(os.path.join(B, "vendor_tpl", "Cargo.lock"), os.path.join(ws, "Cargo.lock"))
    ch = os.path.join(S, "cargo_home")
    os.makedirs(ch, exist_ok=True)
    open(os.path.join(ch, "config.toml"), "w").write(
        '[source.crates-io]\nreplace-with = "vendored-sources"\n\n[source.vendored-sources]\ndirectory = "/b/vendor"\n\n[net]\noffline = true\n')
    env = dict(os.environ, CARGO_HOME=ch)
    out = {}
    for prof, flags in (("debug", []), ("release", ["--release"])):
        pr = subprocess.run(["cargo", f"+{RD.RUST_TC}", "build", "--offline", "-j", "4"] + flags, cwd=ws, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        b = os.path.join(ws, "target", prof, name)
        if pr.returncode or not os.path.isfile(b):
            raise SystemExit(f"{inst} {prof} build failed rc={pr.returncode}\n{pr.stdout[-2000:]}")
        out[prof] = b
    return out


def run(inst, out_path):
    bins = build(inst)
    cref = os.path.join(B, "c", inst, "ref")
    rows = [json.loads(l) for l in open(os.path.join(B, "cases_run1_ub.jsonl"))]
    rows = sorted((r for r in rows if r["run"] == "run1" and r["inst"] == inst and r["kind"] == "panic" and r.get("c_ub_clean") is True),
                  key=RD.row_key)
    cases, counts = [], {"debug": {}, "release": {}}
    for r in rows:
        stdin = RD.load_stdin(inst, r["src"], r["idx"], INPUTS, FUZZ)
        if RD.sha(stdin) != r["input_sha"]:
            raise SystemExit(f"{inst} {r['src']}/{r['idx']}: input sha mismatch")
        c = RD.run1(cref, stdin, RD.C_TIMEOUT)
        o = {"src": r["src"], "idx": r["idx"], "pipeline_kind": r["kind"],
             "c": {"rc": c["rc"], "stdout_head": c["stdout"][:80].decode("latin1")}}
        for prof in ("debug", "release"):
            x = RD.run1(bins[prof], stdin, RD.R_TIMEOUT)
            if x["timed_out"] and not c["timed_out"]:
                x = RD.run1(bins[prof], stdin, RD.R_TIMEOUT_LONG)   # as run_diff: a timeout is re-run before it is a hang
            k, ch = RD.classify(c, x)
            counts[prof][k] = counts[prof].get(k, 0) + 1
            o[prof] = {"kind": k, "rc": x["rc"], "stdout_head": x["stdout"][:80].decode("latin1"),
                       "stderr_head": x["stderr"][:200].decode("latin1")}
        if (r["src"], r["idx"]) == EXAMPLE[inst]:
            o["ledger_example"] = True
        cases.append(o)
    ex = [o for o in cases if o.get("ledger_example")]
    res = {"inst": inst, "defect_id": DEFECTS[inst],
           "rustc": subprocess.run(["rustc", f"+{RD.RUST_TC}", "--version"], stdout=subprocess.PIPE, text=True).stdout.strip(),
           "rs_sha256": sha_file(os.path.join(B, "extracted", "run1", inst + ".rs")), "c_ref_sha256": sha_file(cref),
           "n_cases": len(cases), "by_src": {s: sum(1 for o in cases if o["src"] == s) for s in ("ours", "examples", "fuzz")},
           "outcomes": counts,
           "diverging": {p: sum(v for k, v in counts[p].items() if k in DIVERGENT) for p in ("debug", "release")},
           "example": ex[0] if ex else None, "cases": cases}
    json.dump(res, open(out_path, "w"), indent=1, sort_keys=True)
    print(inst, json.dumps({"outcomes": counts, "diverging": res["diverging"]}))


def merge(out_path, parts):
    per = {}
    for p in parts:
        d = json.load(open(p))
        per[d["defect_id"]] = d
    if sorted(per) != sorted(DEFECTS.values()):
        raise SystemExit(f"missing per-defect results: {sorted(set(DEFECTS.values()) - set(per))}")
    json.dump({"rows": {k: per[k] for k in sorted(per)}}, open(out_path, "w"), indent=1, sort_keys=True)


if __name__ == "__main__":
    if sys.argv[1] == "run":
        run(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "merge":
        merge(sys.argv[2], sys.argv[3:])
    else:
        raise SystemExit(__doc__)
