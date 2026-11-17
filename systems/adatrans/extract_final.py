"""Extract AdaTrans's final Rust per instance per run, with the artifact's own verdicts.

Rule for "final" (the artifact's own definition, analysis/metrics.py::read_run):
  code = the last non-empty `extracted_code` in results/<variant>/<run>/conversations/C_<c>_<p>.json,
  "i.e. the program actually delivered for that instance". For a SUCCESS instance this is the code of
  the SUCCESS iteration (src/core.py::optimize returns it and stops); for a FAILURE instance it is the
  last attempt, which the loop rejected (core.optimize returns None) -- the artifact still counts it in
  its 104-program denominators (compile rate, unsafe rate).
Verdicts (the artifact's own definitions, metrics.py::read_run):
  loop_status   = logs/<inst>.json 'status' (SUCCESS = passed cargo build + the 2-3 example tests)
  fuzz_status   = validation/<inst>.json 'validation_status' (PASSED / FAILED; absent if not SUCCESS)
  solved        = loop_status == SUCCESS and fuzz_status == PASSED   (the paper's "functional equivalence")
Also checks that the C each conversation's first prompt translated is byte-identical (modulo
surrounding whitespace) to dataset/C_CPP/C/src/weekly_contest_<c>_p<p>.c, i.e. the C we use as the
oracle is the C AdaTrans translated.

Reads JSON only (no artifact code is executed), so it runs on the host:
  python3 -I systems/adatrans/extract_final.py --out <dir>
Writes <dir>/run{1,2,3}/C_<c>_<p>.rs and <dir>/manifest.json.
"""
import argparse
import glob
import hashlib
import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ART = os.path.join(REPO, "artifacts", "adatrans")
RUNS = ["run1", "run2", "run3"]


def sha(b):
    return hashlib.sha256(b if isinstance(b, bytes) else b.encode()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--variant", default="adatrans_full")
    a = ap.parse_args()
    man = {"rule": "last non-empty extracted_code in conversations/<inst>.json (analysis/metrics.py::read_run)",
           "variant": a.variant, "runs": {}}
    for run in RUNS:
        rd = os.path.join(ART, "results", a.variant, run)
        od = os.path.join(a.out, run)
        os.makedirs(od, exist_ok=True)
        rows = {}
        for lf in sorted(glob.glob(os.path.join(rd, "logs", "C_*.json"))):
            name = os.path.basename(lf)[:-5]
            _, c, p = name.split("_")
            log = json.load(open(lf))
            h = log.get("history", [])
            conv = json.load(open(os.path.join(rd, "conversations", name + ".json")))
            code, idx = "", None
            for i in range(len(conv["history"]) - 1, -1, -1):
                if (conv["history"][i].get("extracted_code") or "").strip():
                    code, idx = conv["history"][i]["extracted_code"], i
                    break
            vf = os.path.join(rd, "validation", name + ".json")
            val = json.load(open(vf)) if os.path.exists(vf) else None
            csrc = open(os.path.join(ART, "dataset", "C_CPP", "C", "src", f"weekly_contest_{c}_p{p}.c")).read()
            prompt0 = conv["history"][0].get("prompt", "") if conv["history"] else ""
            if code:
                with open(os.path.join(od, name + ".rs"), "w") as f:
                    f.write(code + "\n")
            rows[name] = {
                "contest": c, "problem": p,
                "loop_status": log.get("status"),
                "iterations": len(h),
                "last_iter_status": h[-1].get("status") if h else None,
                "final_index": idx,
                "final_iter_status": conv["history"][idx].get("status") if idx is not None else None,
                "fuzz_status": val.get("validation_status") if val else None,
                "fuzz_total": val["report"]["total"] if val else None,
                "fuzz_passed": val["report"]["passed"] if val else None,
                "fuzz_failures_recorded": val["report"].get("failures", []) if val else None,
                "solved": log.get("status") == "SUCCESS" and (val is None or val.get("validation_status") == "PASSED"),
                "code_sha256": sha(code) if code else None,
                "c_sha256": sha(csrc),
                "c_in_first_prompt": csrc.strip() in prompt0,
            }
        man["runs"][run] = rows
    s = {}
    for run, rows in man["runs"].items():
        s[run] = {"instances": len(rows),
                  "with_code": sum(1 for r in rows.values() if r["code_sha256"]),
                  "loop_SUCCESS": sum(1 for r in rows.values() if r["loop_status"] == "SUCCESS"),
                  "fuzz_PASSED": sum(1 for r in rows.values() if r["fuzz_status"] == "PASSED"),
                  "fuzz_FAILED": sum(1 for r in rows.values() if r["fuzz_status"] == "FAILED"),
                  "solved": sum(1 for r in rows.values() if r["solved"]),
                  "c_in_first_prompt": sum(1 for r in rows.values() if r["c_in_first_prompt"])}
    man["summary"] = s
    with open(os.path.join(a.out, "manifest.json"), "w") as f:
        json.dump(man, f, indent=1)
    print(json.dumps(s, indent=1))


if __name__ == "__main__":
    main()
