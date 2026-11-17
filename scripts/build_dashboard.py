#!/usr/bin/env python3
"""Build dashboard.html: a one-page guide to this artifact with the paper's headline numbers.

    python3 -I scripts/build_dashboard.py                    # -> out/dashboard.html
    python3 -I scripts/build_dashboard.py --update-shipped   # -> dashboard.html (repository root)

Every number is computed here from docs/survey-evidence.json, results/defect_ledger.json and
results/harness_power.json, with the same rules the paper uses; nothing is typed in by hand.
"""
import html
import json
import os
import sys
from decimal import Decimal, ROUND_HALF_UP

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import make_tables as MT  # noqa: E402


def R(*p):
    return os.path.join(ROOT, *p)


def J(p):
    with open(R(p)) as f:
        return json.load(f)


def pct(k, n):
    return int((Decimal(k) * 100 / Decimal(n)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


FOLDER = {"VERT": "vert", "Flourine": "flourine", "Syzygy": "syzygy", "C2SaferRust": "c2saferrust", "SACTOR": "sactor",
          "RustMap": "rustmap", "RustAssure": "rustassure", "PtrTrans": "ptrtrans", "His2Trans": "his2trans",
          "AdaTrans": "adatrans", "ACTOR": "actoruw"}


def numbers():
    sv = J("docs/survey-evidence.json")["counts"]
    L = J("results/defect_ledger.json")
    rows = L["rows"]
    counted = [r for r in rows if r["count_status"] == "counted"]
    units = lambda r: r.get("translation_units") or [r["translation"]]  # noqa: E731
    defective = {u for r in counted for u in units(r)}
    st = lambda r: (r.get("check_exercises_defect") or {}).get("status")  # noqa: E731
    acc = {}
    for r in counted:
        if r["acceptance_status"].startswith("accepted"):
            for u in units(r):
                acc.setdefault(u, []).append(r)
    rejected = {u for r in counted if r["acceptance_status"] == "rejected" for u in units(r)}
    acc = {u: rs for u, rs in acc.items() if u not in rejected}
    vert = [u for u in acc if u.startswith("VERT:")]   # VERT: its released commands never compare on an exposing input
    unrun = [u for u, rs in acc.items() if not u.startswith("VERT:") and all(st(x) == "not-exercised" for x in rs)]
    hp = [(r["label"], r["killed"], r["built"]) for r in J("results/harness_power.json")["rows"]]
    rates = [pct(k, n) for _, k, n in hp]
    return {
        "surveyed": sv["included"],
        "retested": sv["regrade_rule"].get("Meets", 0) + sv["regrade_rule"].get("Partial", 0),
        "held_out": sv["held_out"].get("Yes", 0),
        "mutation": sv["mut"].get("Yes", 0),
        "defects": len(counted),
        "candidates": len(rows),
        "defective": len(defective),
        "outright": sum(1 for r in counted if r["class"] == "outright_error"),
        "accepted": len(acc),
        "accepted_systems": len({u.split(":")[0] for u in acc}),
        "never_compared": len(vert) + len(unrun),
        "only_unrun": sum(1 for r in counted if r.get("on_check_inputs") is False),
        "hp_min": min(rates), "hp_max": max(rates), "hp_n": len(hp),
        "rows": L["rows"], "totals": {MT.norm_system(k): v for k, v in L["totals"].items()},
    }


CSS = """
:root{--bg:#f7f8fa;--card:#fff;--ink:#16202c;--muted:#5b6675;--line:#e1e5ea;--accent:#0f4d92;--accent2:#3775ba;--bad:#b64342}
@media (prefers-color-scheme:dark){:root{--bg:#0f141a;--card:#171e26;--ink:#e6ebf0;--muted:#9aa6b2;--line:#2a3440;--accent:#7fb0e6;--accent2:#5d93cf;--bad:#e08a89}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:1080px;margin:0 auto;padding:28px 16px 48px}
h1{font-size:24px;margin:0 0 6px}h2{font-size:17px;margin:32px 0 12px}
.lede{color:var(--muted);margin:0 0 4px;max-width:820px}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;margin-top:18px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.tile .v{font-size:26px;font-weight:650;color:var(--accent)}.tile .l{color:var(--muted);font-size:13.5px;margin-top:2px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:14px}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{color:var(--muted);font-weight:600;background:transparent}
tr:last-child td{border-bottom:none}tfoot td{font-weight:650}
.wrap{overflow-x:auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.item{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.item a{color:var(--accent);font-weight:600;text-decoration:none}.item a:hover{text-decoration:underline}
.item p{margin:4px 0 0;color:var(--muted);font-size:13.5px}
code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px}
pre{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;overflow-x:auto;margin:0}
a{color:var(--accent)}footer{color:var(--muted);font-size:13px;margin-top:28px}
"""


def page(N):
    e = html.escape
    tiles = [
        (N["surveyed"], "LLM-based C-to-Rust systems surveyed"),
        (N["held_out"], "report results on inputs their repair loop never used"),
        (N["mutation"], "tests its own check on known-wrong translations"),
        (N["retested"], "release C and Rust we could re-test"),
        (f"{N['defects']}", f"defects in {N['defective']} released translations"),
        (N["outright"], "outright errors: wrong results or crashes on ordinary inputs"),
        (N["accepted"], f"defective translations the systems' own checks accepted ({N['accepted_systems']} systems)"),
        (f"{N['never_compared']} of {N['accepted']}", "accepted ones whose check never compared the programs on an exposing input"),
        (f"{N['only_unrun']} of {N['defects']}", "defects that appear only on inputs the system's check never ran"),
        (f"{N['hp_min']}-{N['hp_max']}%", f"of planted bugs our harnesses catch ({N['hp_n']} harnesses), so the counts are lower bounds"),
    ]
    tile_html = "".join(f'<div class="tile"><div class="v">{e(str(v))}</div><div class="l">{e(l)}</div></div>' for v, l in tiles)
    trs, tot = [], dict(t=0, d=0, a=0, o=0)
    for key, name, _ in MT.ORDER:
        t = MT.row_for(key, N["rows"], N["totals"])
        if not t["present"] or t["n"] == 0:
            continue
        tested = "-" if t["tested"] is None else str(t["tested"])
        trs.append(f'<tr><td><a href="systems/{FOLDER[key]}/">{e(name)}</a></td><td>{tested}</td><td>{t["defective"]}</td>'
                   f'<td>{t["n"]} ({t["s1"]} / {t["crash"]})</td><td>{t["acc"]}</td><td>{t["rej"]}</td><td>{t["nr"]}</td><td>{e(t["only"])}</td></tr>')
        tot["t"] += t["defective"]; tot["d"] += t["n"]; tot["a"] += t["acc"]
        tot["o"] += int(t["only"]) if t["only"].isdigit() else 0
    table = ('<div class="wrap"><table><thead><tr><th>System</th><th>Tested</th><th>With a defect</th><th>Defects (wrong output / crash)</th>'
             '<th>Accepted</th><th>Rejected</th><th>No record</th><th>Only on inputs the check never ran</th></tr></thead><tbody>'
             + "".join(trs) + f'</tbody><tfoot><tr><td>All</td><td></td><td>{tot["t"]}</td><td>{tot["d"]}</td><td>{tot["a"]}</td><td></td><td></td><td>{tot["o"]}</td></tr></tfoot></table></div>')
    places = [
        ("paper/main.pdf", "The paper (arXiv version)", "7 pages with references; its figures are drawn from results/."),
        ("explorer/index.html", "Defect explorer", f"All {N['candidates']} candidate defects, {N['defects']} counted: mechanism, input, C and Rust outputs, records, evidence, code."),
        ("explorer/rate.html", "Blind labeling app", "The page an author used to label a blind sample; run python3 explorer/serve.py and open http://127.0.0.1:8790/rate.html (its code panes read artifacts/)."),
        ("results/defect_ledger.json", "Defect ledger", "One row per candidate defect; every count above comes from it."),
        ("docs/partial-comparisons.md", "Partial comparisons", "The three checks that ran an exposing input but compared only part of the output."),
        ("docs/systems-survey.md", "Survey of 34 systems", "Inclusion, held-out and mutation coding, with docs/survey-evidence.json holding a quote for every value."),
        ("REPRODUCE.md", "Reproducing the numbers", "What re-checks offline in minutes, what needs the downloads, and how to re-run every experiment."),
        ("pipeline.json", "Pipeline manifest", "Every stage behind a paper number: command, image, architecture, inputs and outputs."),
        ("systems/", "Per-system folders", "Fetch script (pinned commit), drivers and harness for each re-tested system."),
        ("harness/", "Shared harness", "Input generation, the undefined-behavior rule, held-out checks and mutation analysis."),
        ("docker/", "Docker images", "Pinned images used for every run, and the reviewer image."),
        ("results/", "Result files", "Every result behind a number in the paper, with provenance."),
    ]
    place_html = "".join(f'<div class="item"><a href="{e(h)}">{e(t)}</a><p>{e(d)}</p></div>' for h, t, d in places)
    cmds = ("bash docker/quickstart.sh                 # offline: pipeline verify, figures and explorer from results/\n"
            "python3 scripts/pipeline.py verify        # re-runs the cheap stages and compares them with the shipped files\n"
            "bash scripts/provision.sh artifacts       # fetches every system's artifact at its pinned commit\n"
            "python3 explorer/serve.py                 # explorer and labeling app at http://127.0.0.1:8790/")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Re-Test Artifact Dashboard</title><style>{CSS}</style></head><body><main>
<h1>Re-Testing LLM-Based C-to-Rust Translations Against Their Original C</h1>
<p class="lede">Artifact dashboard. It ships with the artifact so you can find your way around it: the paper's headline numbers,
the per-system results, and where each part of the artifact lives. Every number here is computed from the result files by
<code>scripts/build_dashboard.py</code>.</p>
<div class="tiles">{tile_html}</div>
<h2>Results per system</h2>
{table}
<p class="lede" style="margin-top:8px">Tested: translations we can build and drive. Accepted / Rejected / No record: what the
released artifact records about the system's own check for each defective translation. Only on inputs the check never ran:
every input that exposes the defect is one the check did not run (n.m.: the artifact records no check inputs).</p>
<h2>Where things are</h2>
<div class="grid">{place_html}</div>
<h2>Check it yourself</h2>
<pre>{e(cmds)}</pre>
<footer>Built from docs/survey-evidence.json, results/defect_ledger.json and results/harness_power.json.</footer>
</main></body></html>
"""


def main():
    a = sys.argv[1:]
    out = os.path.abspath(a[a.index("--out") + 1]) if "--out" in a else (R("dashboard.html") if "--update-shipped" in a else R("out", "dashboard.html"))
    N = numbers()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(page(N))
    print(out, {k: v for k, v in N.items() if k not in ("rows", "totals")})


if __name__ == "__main__":
    main()
