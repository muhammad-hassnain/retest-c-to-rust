"""SACTOR: re-run SACTOR-C3 (the one counted crash) and SACTOR-L1 (S1 with a `<<` on a narrowed u32)
under both cargo profiles, rebuilding every crate in a scratch copy.

Profiles: SACTOR's own pipeline builds its candidate crate with plain `cargo build` (debug) -- the
build_attempt step; our harness records debug as the primary build and ALSO builds --release and
both bracketing nightlies (systems/sactor/crust/grid.py:19 and :426, systems/sactor/libogg/run.py:13
and :164, sactor_common.build_crate:222/231). So both profiles were already part of the counted rows'
per_build evidence; this script rebuilds and re-runs them independently.

A) fs_c (SACTOR-C3): rebuild crates/unidiomatic_<tc>_<profile> from their src/lib.rs with
   sactor_common.build_crate, then run the unchanged harness systems/sactor/crust/grid.py --only fs_c.
B) libogg bitwise (SACTOR-L1): rebuild the gpt-4o unidiomatic translation (both nightlies x both
   profiles), link it to the harness's drv_bitwise.o, and replay the 8 exposing call sequences
   bw0220 bw0232 bw0237 bw0240 bw0266 bw0276 bw0300 bw0320 (plus the whole 442-sequence set for the
   count) against the C reference driver c_bitwise_ref.

Run via docker as in systems/sactor/run_docker.sh (--network none, artifacts read-only).
"""
import json, os, subprocess, sys

sys.path.insert(0, "/work/systems/sactor")
sys.path.insert(0, "/work/systems/sactor/libogg")
sys.path.insert(0, "/work/harness")
sys.path.insert(0, "/work")
import sactor_common as sc  # noqa: E402
import gen_cases  # noqa: E402

OUT = sys.argv[1]
res = {}

# ---------------------------------------------------------------- A) fs_c
b = "/build/crust/fs_c"
for tc in sc.TOOLCHAINS:
    for prof in ("debug", "release"):
        cd = f"{b}/crates/unidiomatic_{tc}_{prof}"
        code = open(f"{cd}/src/lib.rs").read()
        ok, log, so = sc.build_crate(code, cd, tc, prof)
        res.setdefault("fs_c_builds", {})[f"{tc}:{prof}"] = ok
p = subprocess.run([sys.executable, "/work/systems/sactor/crust/grid.py", "--build", "/build/crust", "--only", "fs_c",
                    "--jobs", "2", "--out", "/out/sactor_fs_c_grid.json"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
res["fs_c_grid_rc"] = p.returncode
g = json.load(open("/out/sactor_fs_c_grid.json"))["samples"]["fs_c"]["per_function"]
chown = g["fs_chown"]
res["fs_chown"] = {"inputs": chown["inputs"], "per_build": {}}
for k, v in chown["rust"].items():
    if v == "agree":
        res["fs_chown"]["per_build"][k] = {"diverging_inputs": 0}
    else:
        d = v["divergent_inputs"]
        res["fs_chown"]["per_build"][k] = {"diverging_inputs": len(d),
                                            "crash_signal_or_abort": sum(1 for x in d if x["rust"][1] < 0),
                                            "ub_filtered_out_by_harness": len(chown.get("ub", {}))}
res["fs_chown"]["c_ub"] = chown.get("ub")
res["fs_chown"]["other_exported_fns_diverging"] = {fn: [k for k, v in rec["rust"].items() if v != "agree"]
                                                  for fn, rec in g.items() if fn != "fs_chown" and any(v != "agree" for v in rec["rust"].values())}

# ---------------------------------------------------------------- B) libogg bitwise
bd = "/build/libogg/bitwise"
code = open(f"{bd}/lib_gpt-4o_bitwise_unidiomatic.rs").read()
cases = {c["id"]: c for c in gen_cases.bitwise_cases()}
ids8 = ["bw0220", "bw0232", "bw0237", "bw0240", "bw0266", "bw0276", "bw0300", "bw0320"]
cexe = f"{bd}/c_bitwise_ref"
res["L1"] = {"n_cases_total": len(cases), "per_build": {}}


def run(exe, script):
    o, e, rc, to = sc.ou.run_proc([exe], script.encode(), timeout=60)
    return o, rc, e, to


cref = {i: run(cexe, c["script"]) for i, c in cases.items()}
for tc in sc.TOOLCHAINS:
    for prof in ("debug", "release"):
        crate = f"{bd}/crates/L1_{tc}_{prof}"
        ok, log, so = sc.build_crate(code, crate, tc, prof)
        key = f"{tc}:{prof}"
        if not ok:
            res["L1"]["per_build"][key] = {"build_ok": False, "log": log[-300:]}
            continue
        libdir = os.path.dirname(so)
        exe = f"{bd}/rsL1_{tc}_{prof}"
        rc, lg = sc.sh(["gcc", "-o", exe, f"{bd}/drv_bitwise.o", "-L" + libdir, "-lbuild_attempt", "-lm", "-Wl,-rpath," + libdir])
        if rc != 0:
            res["L1"]["per_build"][key] = {"build_ok": True, "link_ok": False, "log": lg[-300:]}
            continue
        div, crash, sample = [], 0, {}
        for i, c in cases.items():
            r = run(exe, c["script"])
            if (r[0], r[1]) != (cref[i][0], cref[i][1]) or r[2] != cref[i][2]:
                div.append(i)
                if r[1] != 0 and r[1] != cref[i][1]:
                    crash += 1
        ex = {}
        for i in ids8:
            r = run(exe, cases[i]["script"])
            same = (r[0], r[1], r[2]) == cref[i][:3]
            ex[i] = "agrees" if same else ("crash rc=%s" % r[1] if r[1] != cref[i][1] else "wrong answer")
        res["L1"]["per_build"][key] = {"build_ok": True, "diverging_sequences_total": len(div),
                                       "crashing_sequences": crash, "the_8_exposing": ex,
                                       "of_the_8_still_diverging": sum(1 for v in ex.values() if v != "agrees")}
json.dump(res, open(OUT, "w"), indent=1)
print(json.dumps(res)[:1500])
