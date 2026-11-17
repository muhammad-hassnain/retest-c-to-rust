"""ACTOR (UW): re-run the counted crash rows (and the overflow/shift-adjacent S1 rows) under DEBUG.

Our harness builds ACTOR's Rust with `cargo build --release --offline --bins`
(systems/actoruw/build.py:6 docstring and :77, binary under target/release); the counted ACTOR
results are therefore RELEASE results. ACTOR's own pipeline does not use a fixed cargo profile
for grading: its generated crate is checked by `cargo test` / build of the crate (debug,
overflow checks ON) -- see artifacts/actoruw/ (README, scoring) as cited in the output JSON.
Here the same crate copies (the build dir the harness built from) are rebuilt BOTH ways in a
scratch dir with the same toolchain (nightly-2026-01-10, offline), and:
  * the ledger example input (argv/stdin as latin-1 bytes) is run on C / Rust-release / Rust-debug,
  * the harness's own generated input grid (systems/actoruw/gen_inputs.py) is replayed on both
    Rust builds with the harness's run_one/same/classify, so every input that exposed the defect
    under release is re-checked under debug.

  docker run --rm --network none --cpus 2 --user $(id -u):$(id -g) -e HOME=/tmp \
    -e CARGO_HOME=/cargo_home -e CARGO_NET_OFFLINE=true -e PATH=/rust/bin:/usr/bin:/bin \
    -v $WORK/regrade-actoruw:/w:ro -v $PC/scratch:/s -v $PC/repo:/repo:ro -v $PC/cargo_home_actor:/cargo_home \
    -v ~/.rustup/toolchains/nightly-2026-01-10-x86_64-unknown-linux-gnu:/rust:ro -v ~/.cargo/registry:/cargo_ro:ro \
    crust-heldout python3 -I /repo/systems/profile_check/actor_profile.py /repo/results/profile_check.json /out/actor.json
"""
import base64, json, os, re, shutil, subprocess, sys

REPO = os.environ.get("PC_REPO", "/repo")
sys.path.insert(0, os.path.join(REPO, "systems", "actoruw"))
sys.path.insert(0, os.path.join(REPO, "systems"))
os.environ.setdefault("ACTORUW_W", "/w")
import common  # noqa: E402
import gen_inputs  # noqa: E402
import difftest  # noqa: E402

S = "/s/actor"
# crash rows (all) + S1 rows whose C or Rust side involves arithmetic overflow / shifts
WANT_S1 = {"B01_synthetic/019_integer_overflow_char_max_multiply", "B02_synthetic/macrodepth_add_5",
           "B02_synthetic/macrodepth_mul_4", "B02_synthetic/macrodepth_sub_6", "B02_organic/qmath",
           "B01_synthetic/033_bitfield", "B01_synthetic/042_float_union"}


def sh(argv, cwd=None, env=None, timeout=900):
    p = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def build_both(b, c):
    src = f"/w/build/{b}/{c}/rust"
    d = os.path.join(S, b, c)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    crate = os.path.join(d, "crate")
    shutil.copytree(src, crate, ignore=shutil.ignore_patterns("target"))
    _, feats = common.case_config(c)
    out = {}
    for prof in ("release", "debug"):
        env = {**os.environ, "CARGO_TARGET_DIR": os.path.join(d, "target"), "CARGO_BUILD_JOBS": "2"}
        rc, log = sh(["cargo", "build", "--offline", "--bins"] + (["--release"] if prof == "release" else []) + feats, cwd=crate, env=env)
        bp = os.path.join(d, "target", prof, "driver")
        out[prof] = bp if os.path.isfile(bp) else None
        if not out[prof]:
            out[prof + "_log"] = log[-400:]
    return out


def case_from_example(ex):
    j = json.loads(ex)
    return {"argv": [a.encode("latin-1").decode("latin-1") for a in j.get("argv", [])],
            "stdin_b64": base64.b64encode(j.get("stdin", "").encode("latin-1")).decode(),
            "files": {}, "dirs": [], "id": "ledger_example"}


def klass(cr, rr):
    if difftest.same(cr, rr):
        return "agrees"
    k = difftest.classify(cr, rr)
    return {"rust_crash": "crash", "S1": "wrong answer", "S2": "wrong answer (stderr only)",
            "S3_hang": "hang", "both_crash": "both crash", "c_timeout": "c timeout"}.get(k, k)


def main():
    led = json.load(open(os.path.join(REPO, "results", "defect_ledger.json")))
    rows = [r for r in led["rows"] if r["system"] == "ACTOR" and r.get("count_status") == "counted"
            and r["exposure_breakdown"].get("kind") == "exe"
            and (r["severity"] == "crash" or r["defect_id"].split("::")[0][len("ACTOR-"):] in WANT_S1)]
    by_case = {}
    for r in rows:
        by_case.setdefault(r["defect_id"].split("::")[0][len("ACTOR-"):], []).append(r)
    res = {}
    for key, rs in by_case.items():
        b, c = key.split("/")
        o = build_both(b, c)
        if not (o.get("release") and o.get("debug")):
            res[key] = {"error": "build failed", **o}
            continue
        cbin = f"/w/build/{b}/{c}/c/driver"
        ph, src = common.phase_dir(b, c)
        shape, cases = gen_inputs.generate(os.path.join(src, "c_src"))
        grid = {"release": {}, "debug": {}}
        sens = []
        rel_ids_by_class = {}
        for x in cases:
            cr = difftest.run_one(cbin, x)
            rr = difftest.run_one(o["release"], x)
            rd = difftest.run_one(o["debug"], x)
            kr, kd = klass(cr, rr), klass(cr, rd)
            grid["release"][kr] = grid["release"].get(kr, 0) + 1
            grid["debug"][kd] = grid["debug"].get(kd, 0) + 1
            rel_ids_by_class.setdefault(kr, []).append((x["id"], kd))
            if not difftest.same(rr, rd) and len(sens) < 3:
                sens.append({"input_id": x["id"], "release": difftest.enc(rr)["rc"], "debug": difftest.enc(rd)["rc"]})
        # per-defect: exposing inputs = release-diverging grid inputs of the row's class
        rowres = []
        for r in rs:
            ex = case_from_example(r["example_input"])
            cr = difftest.run_one(cbin, ex)
            rr = difftest.run_one(o["release"], ex)
            rd = difftest.run_one(o["debug"], ex)
            want = "crash" if r["severity"] == "crash" else "wrong answer"
            exposing = [(i, kd) for k, lst in rel_ids_by_class.items() if k.startswith(want.split()[0]) for (i, kd) in lst]
            kd_counts = {}
            for i, kd in exposing:
                kd_counts[kd] = kd_counts.get(kd, 0) + 1
            rowres.append({"defect_id": r["defect_id"], "severity": r["severity"],
                           "example": {"c_rc": cr["rc"], "release_rc": rr["rc"], "debug_rc": rd["rc"],
                                       "release_outcome": klass(cr, rr), "debug_outcome": klass(cr, rd),
                                       "debug_stderr_tail": rd["stderr"][-160:].decode("latin1")},
                           "grid_release_exposing_inputs": len(exposing),
                           "their_outcome_under_debug": kd_counts})
        res[key] = {"shape": shape, "n_inputs": len(cases), "grid_outcomes": grid,
                    "release_vs_debug_rust_differs_on": len(sens), "sensitive_samples": sens, "rows": rowres}
        print(key, grid, flush=True)
        json.dump(res, open(sys.argv[1], "w"), indent=1)
    json.dump(res, open(sys.argv[1], "w"), indent=1)


if __name__ == "__main__":
    main()
