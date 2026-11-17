#!/usr/bin/env python3
"""Generate the per-system outcome table (README; Fig. 2 counts the same units) from results/defect_ledger.json.

    python3 -I scripts/make_tables.py --out out/outcomes.tex

One row per system, in Table I's order. Every count is derived from the ledger rows, using the
paper's units:
  tested      translations we drive (ledger totals: translations_tested.n)
  defective   translations with >=1 counted defect
  defects     counted defects: wrong output + crash
  acc/rej/nr  acceptance status of the defective translations (accepted-recorded and
              accepted-rerun are both "accepted"; record first, re-run only without a record)
  held out    (LaTeX) counted defects with an exposing input outside the check's recorded inputs;
              "n.m." when the artifact records no check inputs for any of them
  only unrun  (Markdown, the paper's measure) counted defects that appear only on inputs the system's
              check never ran (on_check_inputs is False; VERT: the inputs its released commands run)
A row with any UB-pending defect is marked with \\TBD so it cannot reach a submission unnoticed.
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORDER = [  # (ledger system key, display name, cite key) in Table I order
    ("VERT", "VERT", "vert"), ("Flourine", "FLOURINE", "flourine"), ("Syzygy", "Syzygy", "syzygy"),
    ("C2SaferRust", "C2SaferRust", "c2saferrust"), ("SACTOR", "SACTOR", "sactor"),
    ("RustMap", "RustMap", "rustmap"), ("RustAssure", "RustAssure", "rustassure"),
    ("PtrTrans", "PtrTrans", "ptrtrans"), ("His2Trans", "His2Trans", "his2trans"),
    ("AdaTrans", "AdaTrans", "adatrans"), ("ACTOR", "ACTOR", "actoruw"),
]
COUNTED = {"counted"}
PENDING = {"counted-ub-pending"}


def norm_system(s):
    s = s.strip()
    if s.upper().startswith("ACTOR"):  # "ACTOR", "ACTOR (UW)", "ACTOR (UW; Schesch and Ernst ...)"
        return "ACTOR"
    aliases = {"Fluorine": "Flourine", "FLOURINE": "Flourine", "ACTOR (UW)": "ACTOR", "ACTOR(UW)": "ACTOR", "actoruw": "ACTOR",
               "his2trans": "His2Trans", "adatrans": "AdaTrans", "sactor": "SACTOR", "ptrtrans": "PtrTrans"}
    return aliases.get(s, s)


def accepted(status):
    return status.startswith("accepted")


def row_for(key, rows, totals):
    rs = [r for r in rows if norm_system(r["system"]) == key]
    counted = [r for r in rs if r.get("count_status") in COUNTED | PENDING]
    pending = any(r.get("count_status") in PENDING for r in counted)
    t = totals.get(key, {})
    tested = (t.get("translations_tested") or {}).get("n")
    by_tr = {}  # translation unit -> set of acceptance statuses of its counted defects
    for r in counted:
        for u in (r.get("translation_units") or [r["translation"]]):
            by_tr.setdefault(u, set()).add(r["acceptance_status"])
    acc = sum(1 for s in by_tr.values() if any(accepted(x) for x in s) and not any(x == "rejected" for x in s))
    rej = sum(1 for s in by_tr.values() if any(x == "rejected" for x in s))
    nr = len(by_tr) - acc - rej
    s1 = sum(1 for r in counted if r["severity"] == "S1")
    cr = sum(1 for r in counted if r["severity"] == "crash")
    if all("exposed_outside" in r for r in counted):
        eo = [r["exposed_outside"] for r in counted]
    else:  # older ledger: single-valued heldout field
        eo = [{"outside": True, "inside": False}.get(r.get("heldout")) for r in counted]
    held = "n.m." if counted and all(x is None for x in eo) else str(sum(1 for x in eo if x is True))
    oc = [r.get("on_check_inputs") for r in counted]
    only = "n.m." if counted and all(x is None for x in oc) else str(sum(1 for x in oc if x is False))
    return dict(tested=tested, defective=len(by_tr), s1=s1, crash=cr, acc=acc, rej=rej, nr=nr,
                held=held, only=only, n=len(counted), pending=pending, present=bool(rs) or key in totals)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default=os.path.join(ROOT, "results", "defect_ledger.json"))
    ap.add_argument("--out", default="-")
    ap.add_argument("--md", action="store_true", help="emit a Markdown table (for README.md) instead of LaTeX")
    a = ap.parse_args()
    L = json.load(open(a.ledger))
    rows, totals = L["rows"], {norm_system(k): v for k, v in L["totals"].items()}
    if a.md:
        out = ["| System | Tested | With a defect | Defects (wrong output / crash) | Accepted | Rejected | No record |"
               " Only on inputs the check never ran |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
        tot = dict(d=0, t=0, acc=0, only=0)
        for key, name, _cite in ORDER:
            r = row_for(key, rows, totals)
            if not r["present"] or r["n"] == 0:
                continue
            tested = "–" if r["tested"] is None else str(r["tested"])
            out.append(f"| {name} | {tested} | {r['defective']} | {r['n']} ({r['s1']} / {r['crash']}) | "
                       f"{r['acc']} | {r['rej']} | {r['nr']} | {r['only']} |")
            tot["d"] += r["n"]; tot["t"] += r["defective"]; tot["acc"] += r["acc"]
            tot["only"] += int(r["only"]) if r["only"].isdigit() else 0
        out.append(f"| **All** | | **{tot['t']}** | **{tot['d']}** | **{tot['acc']}** | | | **{tot['only']}** |")
        text = "\n".join(out) + "\n"
        (sys.stdout.write(text) if a.out == "-" else open(a.out, "w").write(text))
        return
    lines = [
        "% Generated by scripts/make_tables.py from results/defect_ledger.json.",
        "% Do not edit by hand.",
        "\\begin{table}[t]",
        "\\centering",
        "\\caption{Re-test outcomes. Tested: translations we drive; Def.: translations with a defect;",
        "Wrong/Cr.: wrong-output and crash defects. Acc./Rej./n.r.: the system's own check accepted,",
        "rejected, or left no record of each defective translation. Out: defects with an exposing input",
        "outside those the check ran (n.m.: not measurable).}",
        "\\label{tab:outcomes}",
        "\\footnotesize",
        "\\setlength{\\tabcolsep}{2.6pt}",
        "\\begin{tabular}{@{}lrrrrrrrr@{}}",
        "\\toprule",
        "System & Tested & Def. & Wrong & Cr. & Acc. & Rej. & n.r. & Out \\\\",
        "\\midrule",
    ]
    warn = []
    for key, name, _cite in ORDER:
        r = row_for(key, rows, totals)
        if not r["present"]:
            lines.append(f"{name} & \\TBD{{}} & & & & & & & \\\\")
            warn.append(f"{name}: no ledger data")
            continue
        tested = "--" if r["tested"] is None else str(r["tested"])
        mark = "\\TBD{UB}" if r["pending"] else ""
        if r["pending"]:
            warn.append(f"{name}: UB-pending defects")
        lines.append(f"{name}{mark} & {tested} & {r['defective']} & {r['s1']} & {r['crash']} & "
                     f"{r['acc']} & {r['rej']} & {r['nr']} & {r['held']} \\\\")
    lines += ["\\bottomrule", "\\end{tabular}", "\\end{table}", ""]
    text = "\n".join(lines)
    if a.out == "-":
        sys.stdout.write(text)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        open(a.out, "w").write(text)
    for w in warn:
        print("WARN:", w, file=sys.stderr)


if __name__ == "__main__":
    main()
