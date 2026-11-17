"""Is each shipped Flourine "success" translation the candidate Flourine's own log records as equivalent?

Scope: every success-labelled translation our two Flourine harnesses test --
  * systems/flourine/run.py: scalar benchmarks whose signature ok_sig() accepts, success files whose
    own signature is accepted and whose Rust builds (run.py's build_rust);
  * systems/flourine/run_struct.py: discover()'d OPL register accessors, success files that
    classify_regfn() accepts and whose Rust builds/constructs (run_struct.build_rust).
Only the SHIPPED translations (local artifact) are compiled, to reproduce the tested set; nothing from
the downloaded logs archive is compiled or executed -- the logs are read as text and hashed.

For each file: sha256 of the shipped .rs vs every candidate in that experiment's log folder
(wspace/logs/prog_*.rs, results/<f>.rs, wspace/src/lib.rs), the log's final verdict
("Equivalent transpilation has been found at initial attempt / by fix strategy / by restart strategy"),
and the log's record for the exact shipped content:
  equivalent      shipped == the candidate the final verification checked, in a log that ends with
                  "Equivalent transpilation has been found": the compiling candidate (its prog_*.err has no
                  rustc error) -- for "at initial attempt" the compiling prog_<a>.rs (flagged ambiguous if
                  several compile), for "by restart strategy" the last compiling prog_<a>.rs, for "by fix
                  strategy" the last compiling fix-round prog_<a>_<budget>_<i>.rs (rounds by descending budget).
                  wspace/src/lib.rs is recorded as a cross-check only.
  not_equivalent  shipped == prog_0.rs, not the accepted candidate, and the first verification printed
                  "Transpilation is not equivalent"
  superseded      shipped == another logged candidate that is not the accepted one
  none            shipped equals no logged candidate
Cross-tabulated with our verdicts (results/flourine_diff.json, results/flourine_struct.json) and with
new_big_table.csv's status.

  docker run --rm --cpus 4 -v "$PWD:/work" -w /work -e FL_LOGS=/work/flourine_logs/extract \
      crust-syzygy python3 -I harness/heldout_flourine_shipped.py
Writes results/flourine_shipped_vs_log.json.
"""
import csv
import datetime
import glob
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "systems"))
sys.path.insert(0, os.path.join(REPO, "systems", "flourine"))
import run as fr  # noqa: E402
import run_struct as fs  # noqa: E402

FL = fr.FL
LOGS = os.environ.get("FL_LOGS", os.path.join(REPO, "flourine_logs", "extract"))
OUT = os.path.join(REPO, "results", "flourine_shipped_vs_log.json")
JOBS = int(os.environ.get("JOBS", "4"))


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest() if os.path.isfile(p) else None


def scope():
    rows = []
    for cfile in sorted(glob.glob(os.path.join(fr.BMS, "*", "*", "*.c"))):
        name = os.path.basename(cfile)[:-2]
        proj = cfile.split("/bms/c/")[1].split("/")[0]
        anyrs = glob.glob(os.path.join(FL, "translations_only", "*", "c", proj, name, "*", "*", name + ".rs"))
        if not anyrs:
            continue
        sig = fr.parse_rust_fn(open(anyrs[0], errors="replace").read(), name)
        if not fr.ok_sig(sig):
            continue
        for f in sorted(glob.glob(os.path.join(FL, "translations_only", "success", "c", proj, name, "*", "*", name + ".rs"))):
            s2 = fr.parse_rust_fn(open(f, errors="replace").read(), name)
            if fr.ok_sig(s2):
                rows.append({"harness": "scalar", "name": name, "file": f, "sig": s2,
                             "tag": f.split("/success/c/")[1]})
    for name, cfile, verds, info in fs.discover():
        for f in verds["success"]:
            s2 = fs.classify_regfn(open(f, errors="replace").read(), name)
            if s2:
                rows.append({"harness": "struct", "name": name, "file": f, "info": s2,
                             "tag": f.split("/opl/")[1]})
    return rows


def builds(r):
    with tempfile.TemporaryDirectory() as d:
        if r["harness"] == "scalar":
            return fr.build_rust(r["name"], r["file"], r["sig"], d) is not None
        b, st = fs.build_rust(r["file"], r["name"], r["info"], d)
        return b is not None


def log_record(r):
    rel = os.path.dirname(r["file"]).split("/translations_only/success/c/")[1]
    ld = os.path.join(LOGS, "translations", "success", "c", rel)
    tl = os.path.join(ld, "transpilation.log")
    if not os.path.isfile(tl):
        return {"log": "missing"}
    log = open(tl, errors="replace").read()
    progs = {os.path.basename(p): sha(p) for p in glob.glob(os.path.join(ld, "wspace", "logs", "prog_*.rs"))}
    lib = sha(os.path.join(ld, "wspace", "src", "lib.rs"))
    res = sha(os.path.join(ld, "results", r["name"] + ".rs"))
    shipped = sha(r["file"])
    m = re.search(r"Equivalent transpilation has been found (at initial attempt|by fix strategy|by restart strategy)", log)
    final = {"at initial attempt": "initial", "by fix strategy": "fix", "by restart strategy": "restart"}.get(m.group(1)) if m else None
    # first verification outcome of prog_0 (the first "Start verifying" block)
    first = None
    blocks = log.split("Start verifying")
    if len(blocks) > 1:
        nxt = blocks[1]
        if re.search(r"Transpilation is not equivalent", nxt.split("Instrumenting")[0]):
            first = "not_equivalent"
        elif re.search(r"Equivalent transpilation has been found at initial attempt", nxt.split("Instrumenting")[0]):
            first = "equivalent"
    score = re.search(r"Transpilation is not equivalent: candidate score = (\d+(?:\.\d+)?(?:e-?\d+)?)", log)

    def key(k):
        """creation order: prog_a (attempt/restart a), then its fix rounds prog_a_<budget>_<i> by DEScending budget"""
        n = [int(x) for x in re.findall(r"\d+", k)]
        return (n[0], 0, 0, 0) if len(n) == 1 else (n[0], 1, -n[1], n[2])
    def compiles(k):
        e = os.path.join(ld, "wspace", "logs", k[:-3] + ".err")
        return os.path.isfile(e) and not re.search(r"^error(\[E\d+\])?:", open(e, errors="replace").read(), re.M)
    plain = sorted((k for k in progs if k.count("_") == 1), key=key)
    fixes = sorted((k for k in progs if k.count("_") >= 3), key=key)
    comp_plain = [k for k in plain if compiles(k)]
    comp_fix = [k for k in fixes if compiles(k)]
    if final == "fix":
        cand = comp_fix
    elif final in ("initial", "restart"):
        cand = comp_plain
    else:
        cand = []
    last = cand[-1] if cand else None
    ambiguous = final == "initial" and len(comp_plain) > 1
    accepted = progs.get(last) if last else None
    if accepted and shipped == accepted:
        rec = "equivalent"
    elif ambiguous and any(progs.get(k) == shipped for k in comp_plain):
        rec = "undetermined"          # several candidates compile in an initial-attempt log
    elif shipped == progs.get("prog_0.rs") and first == "not_equivalent":
        rec = "not_equivalent"
    elif shipped in progs.values():
        rec = "superseded"
    else:
        rec = "none"
    which = ("first_candidate" if shipped == progs.get("prog_0.rs") else
             "later_candidate" if shipped in progs.values() else "none")
    if accepted and shipped == accepted:
        which += "+accepted"
    return {"log": "present", "final_verdict": final, "first_verification": first,
            "first_score": float(score.group(1)) if score else None,
            "candidates": len(progs), "shipped_equals": which,
            "shipped_equals_lib_rs": shipped == lib, "shipped_equals_results_rs": shipped == res,
            "accepted_candidate_file": last,
            "accepted_ambiguous": ambiguous,
            "compiling_candidates": comp_plain + comp_fix,
            "results_rs_equals_accepted": (res == accepted) if accepted else None,
            "lib_rs_equals_accepted": (lib == accepted) if accepted else None,
            "results_rs_equals_prog_0": res == progs.get("prog_0.rs"),
            "log_record_for_shipped_content": rec}


def main():
    rows = scope()
    with ThreadPoolExecutor(max_workers=JOBS) as ex:
        built = list(ex.map(builds, rows))
    for r, b in zip(rows, built):
        r["tested_by_our_harness"] = b
    sc_div = {d["t"] for d in json.load(open(os.path.join(REPO, "results", "flourine_diff.json")))["success_divergences"]}
    st_div = {d["t"] for d in json.load(open(os.path.join(REPO, "results", "flourine_struct.json")))["success_divergences"]}
    table = list(csv.DictReader(open(os.path.join(FL, "all_statistics", "new_big_table.csv"))))
    tidx = {(t["project"], t["benchmark_name"], t["llm"], t["feedback_strategy"]): t for t in table}
    out_rows = []
    for r in rows:
        rec = log_record(r)
        parts = r["file"].split("/translations_only/success/c/")[1].split("/")
        proj, bench, llm, strat = parts[0], parts[1], parts[2], parts[3]
        t = tidx.get((proj, bench, llm, strat), {})
        if not r["tested_by_our_harness"]:
            ours = "not_tested"
        elif r["harness"] == "scalar":
            ours = ("wrong_answer" if r["tag"] in sc_div and "operator_list" in r["tag"] else
                    "diverges_min_gt_max_only" if r["tag"] in sc_div else "agrees")
        else:
            ours = "wrong_answer" if r["tag"] in st_div else "agrees"
        out_rows.append({"harness": r["harness"], "translation": "/".join(parts), "our_verdict": ours,
                         "table_final_status": t.get("final_status"), "table_status_detail": t.get("status_detail"),
                         "shipped_sha256": sha(r["file"]), **rec})
    tested = [x for x in out_rows if x["our_verdict"] != "not_tested"]

    def tally(rows_, key):
        c = {}
        for x in rows_:
            c[x[key]] = c.get(x[key], 0) + 1
        return c
    xtab = {}
    for x in tested:
        xtab.setdefault(x["our_verdict"], {}).setdefault(x["log_record_for_shipped_content"], 0)
        xtab[x["our_verdict"]][x["log_record_for_shipped_content"]] += 1
    table_vs_log = {}
    for x in tested:
        k = f"{x['table_status_detail']} | log final: {x['final_verdict']}"
        table_vs_log[k] = table_vs_log.get(k, 0) + 1

    def ver(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True).stdout.splitlines()[0]
        except Exception:  # noqa: BLE001
            return None
    out = {
        "provenance": {
            "script": "harness/heldout_flourine_shipped.py",
            "command": "docker run --rm --cpus 4 -v \"$PWD:/work\" -w /work -e FL_LOGS=/work/flourine_logs/extract "
                       "crust-syzygy python3 -I harness/heldout_flourine_shipped.py",
            "git_rev": os.environ.get("GIT_REV"), "host": os.environ.get("HOST_DESC", platform.system()),
            "arch": platform.machine(), "image": os.environ.get("IMAGE_DESC"),
            "toolchains": {"rustc": ver(["rustc", "--version"]), "gcc": ver(["gcc", "--version"])},
            "date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "logs_archive": {"url": "https://d34gtk3knhjgeg.cloudfront.net/translations.tar.gz",
                             "sha256": os.environ.get("TAR_SHA256"), "bytes": int(os.environ.get("TAR_BYTES", "0")) or None,
                             "extracted": "translations/success/c/<proj>/<fn>/<llm>/<strategy>/ for every row (288 folders)"},
            "our_verdicts_from": ["results/flourine_diff.json", "results/flourine_struct.json"],
            "note": "nothing from the logs archive is compiled or executed",
        },
        "totals": {
            "in_scope_static": len(out_rows),
            "tested_by_our_harness": {"scalar": sum(x["harness"] == "scalar" for x in tested),
                                      "struct": sum(x["harness"] == "struct" for x in tested)},
            "shipped_equals": tally(tested, "shipped_equals"),
            "log_record_for_shipped_content": tally(tested, "log_record_for_shipped_content"),
            "log_final_verdict": tally(tested, "final_verdict"),
            "shipped_is_log_accepted_candidate": sum(x["log_record_for_shipped_content"] == "equivalent" for x in tested),
            "shipped_is_log_rejected_candidate": sum(x["log_record_for_shipped_content"] in ("not_equivalent", "superseded") for x in tested),
            "shipped_undetermined": sum(x["log_record_for_shipped_content"] == "undetermined" for x in tested),
            "results_rs_equals_accepted": tally(tested, "results_rs_equals_accepted"),
            "results_rs_equals_accepted_by_final_verdict": {f: tally([x for x in tested if x["final_verdict"] == f], "results_rs_equals_accepted")
                                                            for f in ("initial", "fix", "restart")},
            "shipped_equals_results_rs": tally(tested, "shipped_equals_results_rs"),
            "accepted_ambiguous": tally(tested, "accepted_ambiguous"),
            "lib_rs_equals_accepted": tally(tested, "lib_rs_equals_accepted"),
            "our_verdict_x_log_record": xtab,
            "table_status_detail_x_log_final": table_vs_log,
        },
        "rows": out_rows,
    }
    json.dump(out, open(os.environ.get("OUT", OUT), "w"), indent=1)
    print(json.dumps(out["totals"], indent=1))


if __name__ == "__main__":
    main()
