"""Assemble results/vert_kani_bolero_remaining.json from the outputs of toolchain_kani_bolero.sh.

  python3 systems/vert/toolchain_kani_bolero_assemble.py KANI.json BOLERO_PARTS_DIR OUT.json
"""
import datetime, glob, json, os, subprocess, sys

kani_p, parts_dir, out = sys.argv[1], sys.argv[2], sys.argv[3]
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
K = json.load(open(kani_p))
rows = []
for p in sorted(glob.glob(os.path.join(parts_dir, "part*.json"))):
    d = json.load(open(p))
    assert d["partial"] is False, p
    rows += d["rows"]
rows.sort(key=lambda r: r["function"])
csv = {}
for line in open(os.path.join(REPO, "artifacts/vert/c_results.csv")):
    q = [x.strip() for x in line.split(",")]
    if q and q[0]:
        csv[q[0]] = q[1:]
bol = []
for r in rows:
    a = r["arms"]["vert_reduce"]
    ev = a["evidence"]
    executed = bool(ev["libfuzzer_started"] or ev["merge_inner_total_files"])
    bol.append({"function": r["function"], "vert_c_results_csv": csv.get(r["function"]),
                "shipped_corpus_files": r["shipped_corpus_files"],
                "outcome": a["outcome"], "vert_pass_rule_verdict": a["vert_verdict"],
                "pass_rule_passed": a["vert_verdict"] == "pass",
                "executed_any_input": executed, "evidence": ev, "rc": a["rc"], "seconds": a["secs"],
                "log": a.get("log"), "tail": a["tail"][-600:]})
toolch = os.path.join(parts_dir, "container_toolchains.json")
kr = K["rows"]
import re  # noqa: E402
for r in kr:
    # The Rust side of VERT's Kani harness is called with the PARAMs' STATIC initial values (kani::any()
    # assigns them only later, inside the rWasm side), so a failure Kani reports before any kani::any()
    # call (concrete playback with an empty value list) is at these fixed inputs.
    src = open(os.path.join(REPO, "artifacts/vert/benchmark/c_transcoder", r["function"], "out-rwasm-mutated",
                            "src", "main.rs"), errors="replace").read()
    r["harness_static_params"] = re.findall(r"^static mut (PARAM\d+)\s*:\s*([^=]+?)\s*=\s*([^;]+);", src, re.M)
    m = re.search(r"let result = (f_gold\([^\n]*\));", src)
    r["harness_rust_call"] = m.group(1) if m else None
    pb = r.get("concrete_playback")
    if pb:
        pb["playback_value_list_empty"] = all(re.search(r"vec!\[\s*\]", t) for t in pb["tests"]) if pb["tests"] else None
summary = {
    "kani": {"functions": len(kr),
             "verdicts": {v: [r["function"] for r in kr if r["verdict"] == v] for v in sorted({r["verdict"] for r in kr})},
             "equivalence_assertion": {r["function"]: r["equivalence_assertion"] for r in kr},
             "equivalence_assertion_reached": [r["function"] for r in kr if r["equivalence_assertion_reachable"]],
             "failed_checks": {r["function"]: [c["description"] for c in r["failed_checks"]] for r in kr if r["failed_checks"]},
             "vert_evaluator_would_print": {r["function"]: r["vert_evaluator_would_print"] for r in kr}},
    "bolero_vert_reduce": {"functions": len(bol),
                           "executed_any_input": [b["function"] for b in bol if b["executed_any_input"]],
                           "executed_no_input": [b["function"] for b in bol if not b["executed_any_input"]],
                           "pass_rule_passed": [b["function"] for b in bol if b["pass_rule_passed"]],
                           "pass_rule_failed_or_other": {b["function"]: b["vert_pass_rule_verdict"] for b in bol if not b["pass_rule_passed"]},
                           "outcomes": {o: sum(1 for b in bol if b["outcome"] == o) for o in sorted({b["outcome"] for b in bol})},
                           "vert_records_bolero_1": sum(1 for b in bol if "bolero=1" in (b["vert_c_results_csv"] or []))},
}
res = {"provenance": {"script": "systems/vert/toolchain_kani_bolero.sh (+ toolchain_kani_remaining.py, toolchain_bolero_remaining.py "
                                "wrapping bolero_vacuity/check_current.py vert_reduce arm, toolchain_kani_bolero_assemble.py)",
                      "command": "bash systems/vert/toolchain_kani_bolero.sh <scratch> <git_rev> all; python3 " + " ".join(sys.argv),
                      "git_rev": K["provenance"].get("git_rev"),
                      "host": K["provenance"].get("host") + " (private repo copy; artifacts = old checkout, read-only, "
                              "out-rwasm-mutated of the 8 and out-rwasm-bolero of the 12 sha256-identical to the Mac copy)",
                      "arch": K["provenance"].get("arch"),
                      "kani": {k: K["provenance"][k] for k in ("kani", "vert_command", "image", "date_start_utc", "date_end_utc")},
                      "bolero_container": json.load(open(toolch)) if os.path.isfile(toolch) else None,
                      "bolero_procedure": "VERT ARTIFACT_README PBT: mkdir -p src/__fuzz__; RUSTFLAGS='-C overflow-checks=false' "
                                          "cargo bolero reduce bolero_wasm_eq; pass iff no 'Test Failure' (check_current.py, unchanged)",
                      "assembled_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()},
       "summary": summary, "kani_rows": kr, "bolero_rows": bol}
json.dump(res, open(out, "w"), indent=2)
print(json.dumps(summary, indent=1))
