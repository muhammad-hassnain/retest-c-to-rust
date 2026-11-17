"""M2 (held-out measurement), Flourine part, from Flourine's per-experiment logs.

Flourine's logs (translations.tar.gz, separate download; README: "wspace/logs/prog_*.ans : the prompts
used to obtain transpilations, fix compilation errors, and fix counterexamples") are extracted, for
the counted translations only, into $FL_LOGS (default flourine_logs/extract). They are DATA: this
script only reads text and hashes from them; nothing from the archive is compiled or executed. Only
the shipped translation (local artifact) and the C oracle are built, exactly as run.py builds them.

Per counted success translation (byte-pack x2, min>max x3; lfo_pm_depth: identity only) it records
  * which candidate Flourine's run produced/verified: transpilation.log, wspace/logs/prog_*.rs,
    results/<f>.rs and wspace/src/lib.rs (the last candidate, the one the final "Verifying again"
    run checked), compared by sha256 with the shipped translations_only/success/... file we test;
  * the inputs the logs record: the input/output examples in the counterexample-fix prompts
    (prog_*_*.ans; the fuzzer's own example sets are not saved, the prompts carry a sample);
  * exact overlap of those inputs with OUR grid (enumerate_cases(ptypes, cap=2000), what
    systems/flourine/run.py generates now);
  * OUR comparison (run.py build_c/build_rust, -O1 -fwrapv C, batch_diff classification) of the
    shipped translation, on our grid and on the logged inputs; every
    divergent input is re-checked against an ISO-C sanitizer build (gcc -O0 ASan+UBSan, no
    -fwrapv): a divergence is "counted" only where that build is clean (as results/oracle_ub_flourine.json).

  docker run --rm --cpus 4 -v "$PWD:/work" -w /work -e FL_LOGS=/work/flourine_logs/extract \
      crust-syzygy python3 -I harness/heldout_flourine.py
Writes results/heldout_flourine.json.
"""
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

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "systems"))
sys.path.insert(0, os.path.join(REPO, "systems", "flourine"))
sys.path.insert(0, HERE)
import batch_diff as bd  # noqa: E402
import run as frun  # noqa: E402  (systems/flourine/run.py)
from enumerate_inputs import enumerate_cases  # noqa: E402
from heldout_vert import tally_all  # noqa: E402  (batch_diff.tally, verdict of every input)

FL = os.path.join(REPO, "artifacts", "flourine_dl", "flourine-artifact")
LOGS = os.environ.get("FL_LOGS", os.path.join(REPO, "flourine_logs", "extract"))
OUT = os.path.join(REPO, "results", "heldout_flourine.json")
COUNTED = [("libopenaptx", "clip", "claude2", "BaseRepair", "min>max (not counted as wrong answer)"),
           ("libopenaptx", "clip", "claude2", "CAPR", "min>max (not counted as wrong answer)"),
           ("opl", "opl_emu_clamp", "claude2", "BaseRepair", "min>max (not counted as wrong answer)"),
           ("opl", "opl_emu_registers_operator_list", "claude2", "BaseRepair", "byte-pack (counted wrong answer)"),
           ("opl", "opl_emu_registers_operator_list", "claude2", "CAPR", "byte-pack (counted wrong answer)"),
           ("opl", "opl_emu_registers_lfo_pm_depth", "claude3", "BaseRepair", "struct mask (counted wrong answer; identity only)")]
DIV = ("value", "rust_panic", "rust_hang")
# struct accessors (OPL register readers): identity + logged-input overlap only (struct_overlap); the arXiv
# fuzz-profile run (harness/arxiv_heldout_flourine_fuzz.py) adds lfo_am_depth, which reads the same way
STRUCT_ACCESSORS = ("opl_emu_registers_lfo_pm_depth", "opl_emu_registers_lfo_am_depth")


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def parse_examples(ans_text):
    """Examples in a counterexample-fix prompt: arguments, expected (C) output, actual (Rust) output."""
    body = ans_text.split("<testcases>", 1)[1].split("</testcases>", 1)[0] if "<testcases>" in ans_text else ""
    out = []
    for blk in re.split(r"\n\s*Example \d+\s*\n", "\n" + body)[1:]:
        args = [a.strip() for a in re.findall(r"Argument \d+:\s*(.*)", blk)]
        exp = re.search(r"Expected Output:\s*(.*)", blk)
        act = re.search(r"Actual Output:\s*(.*)", blk)
        out.append({"args": args, "expected": exp.group(1).strip() if exp else None,
                    "actual": act.group(1).strip() if act else None})
    return out


def build_san_c(name, cfile, sig, d):
    """ISO-C sanitizer build of the SAME driver run.py writes (its p.c), without -fwrapv."""
    frun.build_c(name, cfile, sig, d)              # writes d/p.c
    b = os.path.join(d, "csan")
    r = subprocess.run(["gcc", "-w", "-O0", "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                        os.path.join(d, "p.c"), "-o", b], capture_output=True)
    return b if r.returncode == 0 else None


def iso_c_ub(binsan, rows):
    return {tuple(r): bd.san_verdict(binsan, r) for r in rows}


def one(proj, name, llm, strat, label):
    rel = os.path.join(proj, name, llm, strat)
    shipped = os.path.join(FL, "translations_only", "success", "c", rel, name + ".rs")
    ld = os.path.join(LOGS, "translations", "success", "c", rel)
    tlog = open(os.path.join(ld, "transpilation.log"), errors="replace").read()
    progs = sorted(glob.glob(os.path.join(ld, "wspace", "logs", "prog_*.rs")))
    accepted = os.path.join(ld, "wspace", "src", "lib.rs")
    res_rs = os.path.join(ld, "results", name + ".rs")
    ident = {"shipped_translation": os.path.relpath(shipped, REPO), "shipped_sha256": sha(shipped),
             "logs_results_rs_sha256": sha(res_rs),
             "logs_candidates": {os.path.basename(p): sha(p) for p in progs},
             "logs_wspace_src_lib_rs_sha256": sha(accepted)}
    ident["shipped_equals_first_candidate_prog_0"] = ident["shipped_sha256"] == ident["logs_candidates"].get("prog_0.rs")
    ident["shipped_equals_verified_candidate"] = ident["shipped_sha256"] == ident["logs_wspace_src_lib_rs_sha256"]
    last = sorted((p for p in progs if os.path.basename(p) != "prog_0.rs"),
                  key=lambda p: [int(x) for x in re.findall(r"\d+", os.path.basename(p))])
    ident["verified_candidate_equals_last_prog"] = bool(last) and sha(last[-1]) == ident["logs_wspace_src_lib_rs_sha256"]
    events = [l.split(" - INFO - ", 1)[1] for l in tlog.splitlines()
              if re.search(r"Start verifying|not equivalent|soft-verifying|Highest score|Verifying again|Equivalent transpilation", l)]
    examples = []
    for a in sorted(glob.glob(os.path.join(ld, "wspace", "logs", "prog_*.ans"))):
        for e in parse_examples(open(a, errors="replace").read()):
            examples.append(dict(e, prompt=os.path.basename(a)))
    cov = open(os.path.join(ld, "cov_show.txt"), errors="replace").read() if os.path.isfile(os.path.join(ld, "cov_show.txt")) else ""
    cov_counts = re.findall(r"^\s*([\d.]+k?)\|fn ", cov, re.M)
    row = {"translation": rel, "label": label, "identity": ident, "flourine_log_events": events,
           "logged_examples": examples, "final_verification_coverage_count_on_fn_line": cov_counts}
    if name in STRUCT_ACCESSORS:
        return dict(row, **struct_overlap(row, shipped))
    cfile = os.path.join(FL, "bms", "c", proj, name, name + ".c")
    sig = frun.parse_rust_fn(open(shipped, errors="replace").read(), name)
    ours = enumerate_cases(sig["ptypes"], cap=2000)
    logged = []
    for e in examples:
        try:
            logged.append([int(x) for x in e["args"]])
        except ValueError:
            pass
    our_set = {tuple(x) for x in ours}
    log_set = {tuple(x) for x in logged}
    with tempfile.TemporaryDirectory() as d:
        binc = frun.build_c(name, cfile, sig, d)
        binsan = build_san_c(name, cfile, sig, d)
        with tempfile.TemporaryDirectory() as d1:
            # only the SHIPPED translation (local artifact, the file we count) is built and run; nothing
            # from the downloaded logs archive is compiled or executed (its candidates are compared by hash).
            bin_ship = frun.build_rust(name, shipped, sig, d1)
            ship_ours = tally_all(binc, bin_ship, ours)
            ship_logged = tally_all(binc, bin_ship, logged) if logged else []
        div = [p for p in ship_ours if p["v"] in DIV]
        ub = iso_c_ub(binsan, [p["in"] for p in div]) if binsan else {}
        ldiv = [p for p in ship_logged if p["v"] in DIV]
        lub = iso_c_ub(binsan, [p["in"] for p in ldiv]) if binsan else {}
    clean = [p for p in div if ub.get(tuple(p["in"])) == "clean"]

    def cnt(per):
        c = {}
        for p in per or []:
            c[p["v"]] = c.get(p["v"], 0) + 1
        return c
    row.update({
        "signature": sig, "our_inputs": len(ours), "logged_inputs": len(logged), "logged_inputs_distinct": len(log_set),
        "overlap_ours_logged": sorted(our_set & log_set),
        "shipped_on_our_grid": cnt(ship_ours),
        "shipped_divergent_inputs": len(div),
        "shipped_divergent_iso_c_clean": len(clean),
        "shipped_divergent_iso_c_ub": sum(v == "ub" for v in ub.values()),
        "counted_divergences_inside_logged_inputs": sorted({tuple(p["in"]) for p in clean} & log_set),
        "counted_divergences_outside_logged_inputs": len({tuple(p["in"]) for p in clean} - log_set),
        "shipped_on_logged_inputs": [{"in": p["in"], "v": p["v"], "c": p.get("c"), "rust": p.get("rust"),
                                      "iso_c": lub.get(tuple(p["in"]))} for p in ship_logged],
        "shipped_diverges_on_logged_inputs_iso_c_clean": sum(lub.get(tuple(p["in"])) == "clean" for p in ldiv),
        "verified_candidate_executed": False,
        "smallest_counted": sorted(clean, key=lambda p: sum(abs(x) for x in p["in"]))[:3],
    })
    return row


def struct_overlap(row, shipped):
    """lfo_pm_depth is a struct accessor: our inputs are systems/flourine/run_struct.py's (every field
    zero except m_regdata[i] = (i*131 + seed*197 + 17) & 0xff, seeds SEEDS). Flourine's prompts log
    each struct truncated ("... and N other elements"), so an input is compared on the fields the log
    records: a mismatch in any recorded field proves the inputs differ; agreement on all recorded
    fields would leave the pair undecided."""
    import ast
    import run_struct as rs  # noqa: E402  (systems/flourine/run_struct.py)
    reads = re.search(r"opl_emu_registers_byte\(regs,\s*(0x[0-9a-fA-F]+|\d+)", open(shipped, errors="replace").read())
    byte_read = int(reads.group(1), 0) if reads else None
    ours = [{"seed": sd, "m_lfo_am_counter": 0, "m_lfo_pm_counter": 0, "m_noise_lfsr": 0, "m_lfo_am": 0,
             "m_regdata": [(i * 131 + sd * 197 + 17) & 0xff for i in range(0x200)]} for sd in rs.SEEDS]
    res, undecided = [], 0
    for e in row["logged_examples"]:
        try:
            lg = ast.literal_eval(e["args"][0])
        except (ValueError, SyntaxError):
            res.append({"prompt": e["prompt"], "parsed": False})
            undecided += len(ours)
            continue
        pre = [v for v in lg.get("m_regdata", []) if isinstance(v, int)]
        diff_all = True
        for o in ours:
            differs = any(k in lg and lg[k] != o[k] for k in ("m_lfo_am_counter", "m_lfo_pm_counter", "m_noise_lfsr", "m_lfo_am")) \
                or pre != o["m_regdata"][:len(pre)]
            diff_all &= differs
            undecided += not differs
        res.append({"prompt": e["prompt"], "parsed": True, "m_regdata_bytes_recorded": len(pre),
                    "byte_read_by_function": byte_read, "byte_read_recorded": byte_read is not None and byte_read < len(pre),
                    "differs_from_every_input_of_ours": diff_all})
    return {"our_inputs": len(ours), "our_inputs_source": "systems/flourine/run_struct.py (SEEDS, register fill; other fields zero)",
            "logged_inputs": len(row["logged_examples"]), "logged_inputs_per_example": res,
            "overlap_ours_logged": 0 if res and undecided == 0 else None,
            "overlap_method": "field-by-field on the fields the log records (the log truncates m_regdata/m_waveform)",
            "counted_divergences_inside_logged_inputs": 0 if res and undecided == 0 else None,
            "shipped_diverges_on_logged_inputs_iso_c_clean": None,
            "shipped_diverges_on_logged_inputs_reason": (
                "not computable: every logged example records only the first "
                f"{min((x.get('m_regdata_bytes_recorded') or 0) for x in res) if res else 0} of 512 m_regdata bytes, and the function reads byte "
                f"{hex(byte_read) if byte_read is not None else '?'}, so our comparison cannot be run on the logged inputs. Flourine's own "
                "log reports 'Runtime crash' (expected '0') for the shipped content on all of them, consistent with the "
                "unconditional `(1u32 >> 1) - 1` overflow panic in a debug build; that is the check failing on the defect's "
                "line, not a divergence under our (release-build, value) comparison")}


def struct_overlap_only():
    """Recompute the struct-accessor overlap from results/heldout_flourine.json alone (no build, no logs)."""
    d = json.load(open(OUT))
    for r in d["translations"]:
        if "lfo_pm_depth" in r["translation"]:
            r.update(struct_overlap(r, os.path.join(REPO, r["identity"]["shipped_translation"])))
            print(r["translation"], {k: r[k] for k in ("our_inputs", "logged_inputs", "overlap_ours_logged",
                                                         "shipped_diverges_on_logged_inputs_iso_c_clean")})
    d["provenance"]["struct_overlap"] = {"command": "python3 -I harness/heldout_flourine.py struct-overlap",
                                         "date": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    json.dump(d, open(OUT, "w"), indent=1)


def main():
    if sys.argv[1:] == ["struct-overlap"]:
        return struct_overlap_only()
    rows = [one(*c) for c in COUNTED]
    for r in rows:
        print(r["translation"], {k: r.get(k) for k in ("shipped_divergent_iso_c_clean", "logged_inputs",
              "overlap_ours_logged", "shipped_diverges_on_logged_inputs_iso_c_clean")},
              r["identity"]["shipped_equals_first_candidate_prog_0"], r["identity"]["shipped_equals_verified_candidate"], flush=True)

    def ver(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True).stdout.splitlines()[0]
        except Exception:  # noqa: BLE001
            return None
    out = {
        "provenance": {
            "script": "harness/heldout_flourine.py",
            "command": "docker run --rm --cpus 4 -v \"$PWD:/work\" -w /work -e FL_LOGS=/work/flourine_logs/extract "
                       "crust-syzygy python3 -I harness/heldout_flourine.py",
            "git_rev": os.environ.get("GIT_REV"), "host": os.environ.get("HOST_DESC", platform.system()),
            "arch": platform.machine(), "image": os.environ.get("IMAGE_DESC"),
            "toolchains": {"gcc": ver(["gcc", "--version"]), "rustc": ver(["rustc", "--version"])},
            "date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "logs_archive": {"url": "https://d34gtk3knhjgeg.cloudfront.net/translations.tar.gz",
                             "bytes": int(os.environ.get("TAR_BYTES", "0")) or None,
                             "sha256": os.environ.get("TAR_SHA256"),
                             "listing_members": int(os.environ.get("TAR_MEMBERS", "0")) or None,
                             "members_extracted": "translations/success/c/<proj>/<fn>/<llm>/<strategy>/ for the 6 rows below",
                             "member_types": os.environ.get("TAR_TYPES")},
            "grid": "enumerate_cases(ptypes, cap=2000) as systems/flourine/run.py",
        },
        "translations": rows,
    }
    json.dump(out, open(os.environ.get("OUT", OUT), "w"), indent=1)


if __name__ == "__main__":
    main()
