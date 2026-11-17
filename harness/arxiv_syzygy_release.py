"""Issue 4 (arXiv): Syzygy's empty-input check (the only counted Syzygy defect,
SYZYGY-zopfli-ZopfliDeflate-1) rebuilt in release, the profile Syzygy's own runner uses
(artifacts/syzygy_zopfli/README.md:7 `cargo build --release`; run_equiv.py:16 runs
./rust_code/target/release/testing_equiv). The shipped results/syzygy_empty_deflate.json was built in
debug (systems/syzygy/empty_deflate.sh: `cargo build`).

Inputs are the outputs of `bash systems/syzygy/empty_deflate.sh --profile release` and (control)
`--profile debug`, both run in crust-syzygy on x86_64.

  python3 -I harness/arxiv_syzygy_release.py --release <json> --debug <json> --image-id <id> --host <text> \
      [--out results/arxiv_syzygy_release.json]
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "scripts"))
from provenance import stamp  # noqa: E402


def rows(d):
    return {r["btype"]: r for r in d["rows"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", required=True)
    ap.add_argument("--debug", required=True)
    ap.add_argument("--image-id", required=True)
    ap.add_argument("--host", required=True)
    ap.add_argument("--run-command", required=True)
    ap.add_argument("--shipped", default=os.path.join(REPO, "results", "syzygy_empty_deflate.json"))
    ap.add_argument("--ledger", default=os.path.join(REPO, "results", "defect_ledger.json"))
    ap.add_argument("--ub", default=os.path.join(REPO, "results", "oracle_ub_pending.json"))
    ap.add_argument("--out", default=os.path.join(REPO, "results", "arxiv_syzygy_release.json"))
    a = ap.parse_args()
    rel, dbg, shp = (json.load(open(p)) for p in (a.release, a.debug, a.shipped))
    led = [r for r in json.load(open(a.ledger))["rows"] if r["system"] == "Syzygy"]
    R, D, S = rows(rel), rows(dbg), rows(shp)
    cmp_rows = []
    for bt in sorted(S):
        cmp_rows.append({
            "btype": bt, "c_hex": R[bt]["c_hex"],
            "rust_hex_release": R[bt]["rust_hex"], "rust_hex_debug_rerun": D[bt]["rust_hex"], "rust_hex_shipped_debug": S[bt]["rust_hex"],
            "rust_release_inflates": R[bt]["rust_inflate"]["ok"], "c_inflates": R[bt]["c_inflate"]["ok"],
            "diverges_release": R[bt]["c_hex"] != R[bt]["rust_hex"], "diverges_shipped_debug": S[bt]["c_hex"] != S[bt]["rust_hex"],
            "c_same_as_shipped": R[bt]["c_hex"] == S[bt]["c_hex"]})
    out = {
        "provenance": {
            "script": "harness/arxiv_syzygy_release.py",
            "command": " ".join(["python3", "-I"] + sys.argv),
            "run_command": a.run_command,
            "host": a.host, "docker_image": "crust-syzygy", "docker_image_id": a.image_id,
            "rustc": rel.get("rustc"), "gcc": rel.get("gcc"),
            "_stamp": stamp(),
        },
        "syzygy_runner_profile": {
            "readme": "artifacts/syzygy_zopfli/README.md:7: Rust: `cargo build --release`",
            "runner": "artifacts/syzygy_zopfli/run_equiv.py:16: ./rust_code/target/release/testing_equiv",
            "cargo_toml": "artifacts/syzygy_zopfli/rust_code/Cargo.toml: no [profile] section (cargo release defaults: "
                          "opt-level 3, debug-assertions off, overflow-checks off)",
            "shipped_study_build": "systems/syzygy/empty_deflate.sh: `cargo build` (debug) for results/syzygy_empty_deflate.json; "
                                   "systems/syzygy/run.py already builds --release (results/syzygy_diff.json)",
        },
        "rows": cmp_rows,
        "debug_rerun_byte_identical_to_shipped": dbg == shp,
        "summary": {
            "btypes": len(cmp_rows),
            "diverge_in_release": sum(r["diverges_release"] for r in cmp_rows),
            "diverge_in_shipped_debug": sum(r["diverges_shipped_debug"] for r in cmp_rows),
            "rust_output_release_equals_debug": all(r["rust_hex_release"] == r["rust_hex_shipped_debug"] for r in cmp_rows),
            "verdict_changes": sum(r["diverges_release"] != r["diverges_shipped_debug"] for r in cmp_rows),
        },
        "ub_status": {"source": os.path.relpath(a.ub, REPO),
                      "note": "the C side does not depend on the Rust build profile; the empty input is UB-clean under the full rule",
                      "ledger_ub_rule": [r.get("ub_rule") for r in led]},
        "ledger_rows": [{k: r.get(k) for k in ("defect_id", "severity", "count_status", "exposure_count")} for r in led],
        "counted_defect_unchanged": all(r["diverges_release"] for r in cmp_rows) and len(cmp_rows) == 3,
    }
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1)
        f.write("\n")
    print(json.dumps({"summary": out["summary"], "debug_rerun_byte_identical_to_shipped": out["debug_rerun_byte_identical_to_shipped"],
                      "counted_defect_unchanged": out["counted_defect_unchanged"]}, indent=1))


if __name__ == "__main__":
    main()
