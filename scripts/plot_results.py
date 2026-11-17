#!/usr/bin/env python3
"""The paper's figures, drawn from this repository's result files.

  pipeline.pdf   Fig. 1 (figure*, 7.16 in): the study in five stages. The defects card reads its
                 numbers from results/defect_ledger.json; the rest is protocol text.
  outcomes.pdf   (figure*, 7.16 in): per system, (a) defective translations by what the
                 system's own check recorded (accepted / rejected / no record), (b) defects by the
                 inputs that expose them (results/defect_ledger.json; counted and UB-pending
                 defects, the outcome table's units via scripts/make_tables.py)
  vert_checks.pdf   (column, 3.5 in): VERT's own checks re-run as documented, one 100% bar each,
                 segments labelled in place (results/vert_kani_vacuity.json,
                 vert_kani_current_check.json, vert_bolero_vacuity_current.json)
  harness_power.pdf (column, 3.5 in): % of compiled mutants each harness kills, dot and Wilson 95%
                 whisker, sorted by rate, under ONE kill rule (KILL_RULE) recomputed from the
                 per-mutant records (results/vert_mutation_primary_grid2.json, vert_arrays_mutation_primary.json,
                 flourine_mutation.json, cli_mutation_full.json, sactor_libogg_mutation.json,
                 sactor_crust_mutation.json, his2trans_power.json, adatrans_mutation_run1.json,
                 ptrtrans_quadtree_mutation.json, actoruw_mutation.json); rows and notes also go to
                 results/harness_power.json (default run) or to harness_power.json next to the PDFs (--out,
                 --check), so a redraw into a scratch directory never rewrites the shipped file.
                 arXiv version: C2SaferRust, ACTOR and FLOURINE take killed/built
                 from the UB-filtered re-scans (ARXIV_UB_FILTER; FLOURINE's row in FLOURINE_POWER), so a kill
                 counts only on an input the defect count would count. scripts/arxiv_harness_power_ubfiltered.py
                 read the submitted version's harness_power.json as its "old" rows; it is not re-run on this one.

Every plotted number is read from those files at run time; nothing is cached or typed in. Systems
come in Table I order from scripts/make_tables.py (ORDER, norm_system, row_for), so Fig. 2 and
the outcome table count the same things. A system appears in Fig. 2 as soon as the ledger has rows or totals
for it (results/<system>_summary.json, then scripts/build_defect_ledger.py), and Fig. 2 grows by
one row. A system with UB-pending defects gets a dagger and a footnote until the UB rule clears it.

Style: figures4papers (C. Liu, github.com/ChenLiu-1996/figures4papers), as in the author's SE4All
figures: sans-serif (Liberation Sans if installed, else Arial, else DejaVu Sans), no top or right
spines, black-edged bars, hatching so series survive greyscale, values printed on the marks, one
palette (navy/blue measured, red/pink problems, greys for no verdict), TrueType (Type 42) fonts.
Each PDF is exactly 3.5 in or 7.16 in wide, so \\includegraphics[width=\\columnwidth] (or
\\textwidth) prints every label at its nominal size (6.5-7.5 pt); the script warns if anything
is drawn outside the canvas.

  python3 scripts/plot_results.py                     # writes out/figures/*.pdf
                                                      # and results/harness_power.json
  python3 scripts/plot_results.py --out DIR [--png DIR]   # DIR/*.pdf and DIR/harness_power.json
  python3 scripts/plot_results.py --check DIR         # redraw into a temp dir; exit 1 unless the
                                                      # PDFs are byte-identical to DIR's
The PDFs carry no creation date, so a re-run on the same data, matplotlib and fonts gives the same
bytes. Needs python3 + matplotlib only.
"""
import argparse
import collections
import json
import math
import os
import sys
import tempfile

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mticker  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import Circle, FancyArrowPatch, FancyBboxPatch, Rectangle  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import make_tables as MT  # noqa: E402  the outcome table's system order, names and per-system counts

OUT_DEFAULT = os.path.join(REPO, "out", "figures")
NAMES = ("pipeline", "outcomes", "vert_checks", "harness_power")

# ---------------------------------------------------------------- style (as the SE4All figures)
NAVY, BLUE, LIGHT = "#0F4D92", "#3775BA", "#A9C8EA"
RED, PINK = "#B64342", "#E9A6A1"
LIGHTGRAY, INK, INK2, ARROW = "#E3E3E3", "#272727", "#4D4D4D", "#6B7380"
PANEL, CARD_EDGE = "#F2F4F7", "#CDD3DC"
COL_W, PAGE_W = 3.5, 7.16          # IEEE column and text width, inches
NUM = 6.5                          # numbers printed on marks, pt


def _font_stack():
    have = {f.name for f in font_manager.fontManager.ttflist}
    stack = [f for f in ("Liberation Sans", "Arial", "Helvetica", "DejaVu Sans") if f in have]
    return stack or ["DejaVu Sans"]


FONTS = _font_stack()
plt.rcParams.update({
    "font.family": FONTS,
    "font.size": 7.5, "axes.labelsize": 7.5, "xtick.labelsize": 7, "ytick.labelsize": 7.5,
    "legend.fontsize": 7, "axes.spines.top": False, "axes.spines.right": False,
    "axes.linewidth": 0.8, "axes.edgecolor": INK, "axes.labelcolor": INK, "text.color": INK,
    "xtick.color": INK, "ytick.color": INK, "xtick.major.width": 0.8, "ytick.major.width": 0.8,
    "xtick.major.size": 2.5, "ytick.major.size": 2.5, "xtick.major.pad": 2,
    "legend.frameon": False, "hatch.linewidth": 0.5, "hatch.color": INK,
    "pdf.fonttype": 42, "ps.fonttype": 42, "savefig.bbox": None, "savefig.pad_inches": 0,
    "path.simplify": False,
})
EDGE = dict(edgecolor=INK, linewidth=0.6)
ERR = dict(ecolor=INK, elinewidth=0.7, capsize=2, capthick=0.7)
WARN = []


def warn(msg):
    if msg not in WARN:
        WARN.append(msg)


def J(p):
    with open(os.path.join(REPO, p)) as f:
        return json.load(f)


def J_opt(p):
    return J(p) if os.path.isfile(os.path.join(REPO, p)) else None


def wilson(k, n, z=1.96):
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return 100 * p, 100 * (c - h), 100 * (c + h)


def axes_in(fig, left, bottom, width, height):
    """An axes placed in inches from the figure's bottom-left corner."""
    W, H = fig.get_size_inches()
    return fig.add_axes([left / W, bottom / H, width / W, height / H])


def text_w(fig, s, size, weight="normal"):
    """Rendered width of a string, inches."""
    t = fig.text(0, 0, s, fontsize=size, fontweight=weight)
    w = t.get_window_extent(fig.canvas.get_renderer()).width / fig.dpi
    t.remove()
    return w


def save(fig, name, out, png):
    """Save at the exact canvas size; warn if any artist is drawn outside the canvas."""
    fig.canvas.draw()
    bb = fig.get_tightbbox(fig.canvas.get_renderer())
    W, H = fig.get_size_inches()
    if bb.x0 < -0.005 or bb.y0 < -0.005 or bb.x1 > W + 0.005 or bb.y1 > H + 0.005:
        warn(f"{name}: drawing extends outside the {W:.2f} x {H:.2f} in canvas "
             f"({bb.x0:.2f}, {bb.y0:.2f})-({bb.x1:.2f}, {bb.y1:.2f})")
    os.makedirs(out, exist_ok=True)
    fig.savefig(os.path.join(out, f"{name}.pdf"), metadata={"CreationDate": None, "ModDate": None})
    if png:
        os.makedirs(png, exist_ok=True)
        fig.savefig(os.path.join(png, f"{name}.png"), dpi=300)
    plt.close(fig)
    return W, H


def fit_xlim(fig, w_in, items, off_in, floor, size=NUM):
    """Smallest xlim >= floor such that each (value, label) bar-end label fits inside the axes."""
    need = floor
    for v, lab in items:
        frac = 1 - (off_in + text_w(fig, lab, size) + 0.03) / w_in
        need = max(need, v / frac if frac > 0 else need * 2)
    return need


def int_ticks(ax, top, xlim):
    """At most three integer ticks from 0, the bottom spine ending at the last one."""
    ticks = [t for t in mticker.MaxNLocator(nbins=3, integer=True, steps=[1, 2, 5, 10]).tick_values(0, top)
             if 0 <= t <= xlim]
    ax.set_xticks(ticks)
    ax.spines["bottom"].set_bounds(0, ticks[-1])


# ---------------------------------------------------------------- data: the defect ledger
COUNTED = ("counted", "counted-ub-pending")


def exposure_class(r):
    """Where a counted defect shows, from the ledger's exposed_outside (an exposing input the check
    did not run) and on_check_inputs (an exposing input among those the system's documented checks
    actually ran). A null in either field, or a contradiction, is "not measurable". Rows without
    on_check_inputs (older ledgers) fall back to found_on, with a warning."""
    eo = r.get("exposed_outside")
    if "on_check_inputs" not in r:
        warn(f"{r['system']}: ledger rows lack on_check_inputs; Fig. 2b falls back to found_on (not the strict field)")
        oc = None if eo is None else (r.get("found_on") in ("both", "own_inputs"))
    else:
        oc = r["on_check_inputs"]
    if eo is True and oc is False:
        return "outside_only"
    if eo is True and oc is True:
        return "both"
    if eo is False and oc is True:
        return "own_only"
    if eo is not None or oc is not None:
        warn(f"{r['defect_id']}: exposed_outside={eo}, on_check_inputs={oc}: drawn as not measurable")
    return "nm"


def ledger():
    """Per system in Table I order: the outcome table's counts (make_tables.row_for) plus exposure classes."""
    L = J("results/defect_ledger.json")
    keys = [k for k, _, _ in MT.ORDER]

    def sysname(s):                                 # e.g. "ACTOR (UW; Schesch and Ernst, ...)" -> "ACTOR"
        k = MT.norm_system(s)
        if k not in keys and MT.norm_system(s.split(" (")[0]) in keys:
            warn(f"ledger system name {s!r} read as {MT.norm_system(s.split(' (')[0])!r} (make_tables.norm_system does not map it)")
            k = MT.norm_system(s.split(" (")[0])
        return k
    rows = [dict(r, system=sysname(r["system"])) for r in L["rows"]]
    totals = {sysname(k): v for k, v in L["totals"].items()}
    extra = list(dict.fromkeys(k for k in [MT.norm_system(r["system"]) for r in rows] + list(totals) if k not in keys))
    for k in extra:
        warn(f"ledger system {k!r} is not in make_tables.ORDER; drawn after the others")
    out = []
    for key, name, _ in list(MT.ORDER) + [(k, k, None) for k in extra]:
        t = MT.row_for(key, rows, totals)
        if not t["present"]:
            warn(f"{name}: no ledger rows yet (results/<system>_summary.json absent?); not in Fig. 2")
            continue
        rs = [r for r in rows if MT.norm_system(r["system"]) == key and r.get("count_status") in COUNTED]
        exp = collections.Counter(exposure_class(r) for r in rs)
        assert sum(exp.values()) == t["n"], (key, exp, t["n"])
        # arXiv: Fig. 2b draws one "Yes" for both classes in which the check ran an exposing input
        # ("both": other exposing inputs exist too; "own_only": none do, e.g. a program that reads no input)
        exp["ran"] = exp["both"] + exp["own_only"]
        assert t["acc"] + t["rej"] + t["nr"] == t["defective"], (key, t)
        if t["held"] != "n.m.":
            assert sum(r.get("exposed_outside") is True for r in rs) == int(t["held"]), (key, t["held"])
        if t["pending"]:
            warn(f"{name}: UB-pending defects are counted (dagger in Fig. 2)")
        out.append(dict(key=key, name=name, **t, exp=exp))
    return out


# ---------------------------------------------------------------- data plots: shared helpers
# One palette, shared with the paper's code listings. Hatching only where two fills would merge in
# greyscale: Rejected (blue) vs Accepted (red).
GREY = "#A0A7B1"
LBL, AXT, TICK = 7.5, 8.0, 7.5                  # direct labels and numbers, axis titles, ticks (pt)
CAT = collections.namedtuple("CAT", "key label fill hatch hatch_color text")
ACCEPT = [CAT("acc", "Passed (counted as correct)", RED, None, None, "white"),
          CAT("rej", "Failed (flagged)", BLUE, "////", "white", "white"),
          CAT("nr", "No verdict recorded", GREY, None, None, INK)]
EXPOSURE = [CAT("outside_only", "No: only inputs it never ran reveal it", NAVY, None, None, "white"),
            CAT("ran", "Yes: it ran an input that reveals it", LIGHT, None, None, INK),
            CAT("nm", "Unknown: its inputs are not recorded", LIGHTGRAY, "....", INK2, INK)]


def bar(ax, y, v, left, h, c):
    """One black-edged bar segment; a hatch, if any, is drawn in its own colour on top."""
    ax.barh(y, v, left=left, height=h, color=c.fill, **EDGE)
    if c.hatch:
        ax.barh(y, v, left=left, height=h, fill=False, hatch=c.hatch, edgecolor=c.hatch_color, linewidth=0)
        ax.barh(y, v, left=left, height=h, fill=False, **EDGE)


def on_bar(ax, x, y, s, c, ha="center"):
    """A number on a segment; on a hatched fill it sits on a small patch of the fill."""
    ax.text(x, y, s, ha=ha, va="center_baseline", fontsize=LBL, color=c.text, fontweight="bold" if c.text == "white" else "normal",
            bbox=dict(boxstyle="square,pad=0.04", fc=c.fill, ec="none") if c.hatch else None)


def key_row(fig, x_in, y_in, cats, max_w):
    """Inline category key (swatch + name, no frame), left to right, wrapping at max_w inches.
    y_in is the centre line of the first row; returns the centre line of the last row."""
    W, H = fig.get_size_inches()
    sw, pad, gap, line = 0.11, 0.05, 0.17, 0.17
    x, y = x_in, y_in
    for c in cats:
        need = sw + pad + text_w(fig, c.label, LBL)
        if x > x_in and x + need > x_in + max_w:
            x, y = x_in, y - line
        for kw in ([dict(fc=c.fill, ec=INK, lw=0.5)] +
                   ([dict(fill=False, hatch=c.hatch, ec=c.hatch_color, lw=0), dict(fill=False, ec=INK, lw=0.5)] if c.hatch else [])):
            fig.add_artist(Rectangle(((x) / W, (y - sw / 2) / H), sw / W, sw / H,
                                                        transform=fig.transFigure, **kw))
        fig.text((x + sw + pad) / W, y / H, c.label, va="center_baseline", fontsize=LBL)
        x += need + gap
    return y


# ---------------------------------------------------------------- Fig. 2: outcomes (figure*)
def _stacked_panel(fig, ax, w_in, S, cats, value, total, end_label, pitch_in):
    h = 0.64
    for y, s in enumerate(S):
        left = 0
        for c in cats:
            v = value(s, c.key)
            if v:
                bar(ax, y, v, left, h, c)
            left += v
    top = max([total(s) for s in S] + [1])
    off_in = 4 / 72
    ax.set_xlim(0, fit_xlim(fig, w_in, [(total(s), end_label(s)) for s in S], off_in, floor=top * 1.06, size=LBL))
    xlim = ax.get_xlim()[1]
    for y, s in enumerate(S):
        ax.annotate(end_label(s), (total(s), y), xytext=(4, 0), textcoords="offset points", va="center_baseline",
                    fontsize=LBL, color=INK)
        left = 0
        for c in cats:
            v = value(s, c.key)
            if v and v / xlim * w_in >= text_w(fig, str(v), LBL, "bold") + 0.07:
                on_bar(ax, left + v / 2, y, str(v), c)
            left += v
    int_ticks(ax, top, xlim)
    ax.tick_params(axis="x", labelsize=TICK)
    ax.tick_params(axis="y", length=0)


def fig_outcomes(S, out, png):
    n = len(S)
    pend = any(s["pending"] for s in S)
    W = PAGE_W
    fig = plt.figure(figsize=(W, 1))             # height set below, once the key rows are measured
    names = [s["name"] + ("†" if s["pending"] else "") for s in S]
    L = max(text_w(fig, nm, AXT) for nm in names) + 0.14
    gap, R = 0.50, 0.04
    wa = wb = (W - L - gap - R) / 2
    pitch = 0.16
    hp = pitch * n
    bot = 0.40 + (0.17 if pend else 0.0)
    # key rows: count how many each panel needs at this width
    def rows_needed(cats, max_w):
        x, rows = 0.0, 1
        for c in cats:
            need = 0.11 + 0.05 + text_w(fig, c.label, LBL)
            if x > 0 and x + need > max_w:
                x, rows = 0.0, rows + 1
            x += need + 0.17
        return rows
    krows = max(rows_needed(ACCEPT, wa), rows_needed(EXPOSURE, wb))
    head = 0.19 + krows * 0.17 + 0.07
    H = bot + hp + head
    fig.set_size_inches(W, H)
    a = axes_in(fig, L, bot, wa, hp)
    b = axes_in(fig, L + wa + gap, bot, wb, hp)
    _stacked_panel(fig, a, wa, S, ACCEPT, lambda s, k: s[k], lambda s: s["defective"],
                   lambda s: f"{s['defective']}" + ("" if s["tested"] is None else f" of {s['tested']:,}"), pitch)
    _stacked_panel(fig, b, wb, S, EXPOSURE, lambda s, k: s["exp"][k], lambda s: s["n"],
                   lambda s: f"{s['n']}" + (f" ({s['crash']} crash)" if s["crash"] else ""), pitch)
    a.set_yticks(range(n), names, fontsize=AXT)
    b.set_yticks(range(n), [""] * n)
    b.tick_params(axis="y", length=0)
    for ax in (a, b):
        ax.set_ylim(n - 0.5, -0.5)
    a.set_xlabel("Translations with a defect (k of n tested)", fontsize=AXT, labelpad=3)
    b.set_xlabel("Defects (crashes in parentheses)", fontsize=AXT, labelpad=3)
    for x0, w, title, cats in ((L, wa, "(a) Did the system's own check pass the defective translation?", ACCEPT),
                               (L + wa + gap, wb, "(b) Did the check run an input that reveals the defect?", EXPOSURE)):
        fig.text(x0 / W, (H - 0.04) / H, title, fontsize=AXT, fontweight="bold", va="top", ha="left")
        key_row(fig, x0, H - 0.04 - 0.19 - 0.05, cats, w)
    if pend:
        fig.text(0.02 / W, 0.03 / H, "† UB check still pending for some of these defects", fontsize=LBL, color=INK2,
                 va="bottom", ha="left")
    return save(fig, "outcomes", out, png)


# ---------------------------------------------------------------- Fig. 3: VERT's own checks
VC = {"vacuous": CAT("vacuous", "comparison never runs", RED, None, None, "white"),
      "reached": CAT("reached", "comparison runs", NAVY, None, None, "white"),
      "other_fail": CAT("other_fail", "fails another check", BLUE, "////", "white", "white"),
      "nocompile": CAT("nocompile", "does not compile", GREY, None, None, INK),
      "timeout": CAT("timeout", "times out", LIGHTGRAY, None, None, INK)}
VC_ORDER = ["vacuous", "reached", "other_fail", "nocompile", "timeout"]


def vert_checks():
    k = J("results/vert_kani_vacuity.json")["summary"]["all_90"]
    vc = k["verdict_counts"]
    n90 = sum(vc.values())
    kani = dict(vacuous=k["vacuous_success"], reached=vc.get("SUCCESSFUL", 0) - k["vacuous_success"],
                other_fail=vc.get("FAILED", 0), nocompile=vc.get("COMPILE_ERROR", 0), timeout=vc.get("TIMEOUT", 0))
    assert sum(kani.values()) == n90 and k["equivalence_assertion_reached"] == kani["reached"], (kani, k)
    c = J("results/vert_kani_current_check.json")["current_14"]
    unw = collections.Counter()
    for r in c["rows"]:
        u = r["unwinding_on"]
        v = u["verdict"]
        unw["other_fail" if v == "FAILED" and r["fails_on_unwinding_assertion"]
            else "nocompile" if v == "COMPILE_ERROR" else "timeout" if v == "TIMEOUT"
            else "reached" if u.get("equivalence_assertion_derived_from_summary") not in ("UNREACHABLE", "UNDETERMINED", None)
            else "vacuous" if v == "SUCCESSFUL" else "other_fail"] += 1
    assert unw["other_fail"] == c["fails_on_unwinding_assertion_when_on"], (unw, c["fails_on_unwinding_assertion_when_on"])
    # arXiv (issues.md 47): the long-return defective functions, same command (results/arxiv_vert_wide_kani_unwinding_on.json)
    wk = J("results/arxiv_vert_wide_kani_unwinding_on.json")["rows"]
    for r in wk:
        unw["other_fail" if r["verdict"] == "FAILED" and r["unwinding_assertion_failed"] else "nocompile" if r["verdict"] == "COMPILE_ERROR"
            else "timeout" if r["verdict"] == "TIMEOUT" else "reached" if r["equivalence_assertion"] not in ("UNREACHABLE", "UNDETERMINED", None)
            else "vacuous" if r["verdict"] == "SUCCESSFUL" else "other_fail"] += 1
    b = J("results/vert_bolero_vacuity_current.json")
    bw = J("results/arxiv_vert_wide_bolero_replay.json")     # arXiv (issues.md 47): same arms and rule
    bol = collections.Counter()
    for r in b["rows"] + bw["rows"]:
        a = r["arms"]["vert_reduce"]
        ev = a["evidence"]
        ran = ev["libfuzzer_started"] or ev["running_1_test"] or bool(ev["merge_inner_total_files"])
        bol["nocompile" if a["vert_verdict"] == "compile_error" else "timeout" if a["timeout"]
            else ("reached" if ran else "vacuous") if a["vert_verdict"] == "pass" else "other_fail"] += 1
    sb, sw = b["summary"], bw["summary"]
    nfun = sb["functions"] + sw["functions"]
    assert sum(bol.values()) == nfun and bol["vacuous"] + bol["reached"] == sb["vert_reduce"]["pass"] + sw["vert_reduce"]["pass"], (bol, sb, sw)
    assert sb["vert_reduce_no_input_evidence_any_outcome"] + sw["vert_reduce_no_input_evidence_any_outcome"] == nfun - bol["reached"], (bol, sb, sw)
    return [("Bounded Kani, as VERT runs it", f"{n90} recorded passes", kani),
            ("Bounded Kani, unwinding checks on", f"{len(c['rows']) + len(wk)} defective", dict(unw)),
            ("Bolero replay, as VERT documents it", f"{nfun} defective", dict(bol))]


def fig_vert_checks(rows, out, png):
    """One 100% bar per re-run; segments labelled in place, or by a callout stacked under the bar's
    right end (elbow leaders, rightmost segment nearest the bar) when a segment is too narrow."""
    W = COL_W
    x0, bar_w = 0.04, 2.08
    cx = x0 + bar_w + 0.12                        # callout text column
    bh, title_h, line, gap = 0.17, 0.19, 0.145, 0.13
    fig = plt.figure(figsize=(W, 1))
    plan = []
    for title, sub, v in rows:
        tot = sum(v.values())
        segs, left = [], 0.0
        for key in VC_ORDER:
            if v.get(key):
                w = v[key] / tot * bar_w
                lab = VC[key].label
                inside = w >= text_w(fig, f"{v[key]}", LBL, "bold") + 0.08 + text_w(fig, lab, LBL) + 0.12
                segs.append((key, v[key], left, w, inside))
                left += w
        n_call = sum(1 for s in segs if not s[4])
        plan.append((title, sub, segs, n_call))
    H = 0.06 + sum(title_h + bh + (0.05 + n * line if n else 0) + gap for *_, n in plan) - gap + 0.04
    fig.set_size_inches(W, H)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(0, H)
    ax.axis("off")
    y = H - 0.06
    for title, sub, segs, n_call in plan:
        ax.text(x0, y - 0.13, title, fontsize=LBL, fontweight="bold", va="baseline")
        ax.text(x0 + text_w(fig, title, LBL, "bold") + 0.07, y - 0.13, sub, fontsize=LBL, color=INK2, va="baseline")
        yb = y - title_h - bh / 2                  # bar centre
        for key, cnt, left, w, inside in segs:
            c = VC[key]
            ax.add_patch(Rectangle((x0 + left, yb - bh / 2), w, bh, fc=c.fill, ec=INK, lw=0.6))
            if c.hatch:
                ax.add_patch(Rectangle((x0 + left, yb - bh / 2), w, bh, fill=False, hatch=c.hatch,
                                                          ec=c.hatch_color, lw=0))
                ax.add_patch(Rectangle((x0 + left, yb - bh / 2), w, bh, fill=False, ec=INK, lw=0.6))
            if inside:
                t1 = f"{cnt}"
                wtot = text_w(fig, t1, LBL, "bold") + 0.06 + text_w(fig, c.label, LBL)
                xs = x0 + left + (w - wtot) / 2
                if c.hatch:                         # one patch of plain fill behind the whole label
                    ax.add_patch(Rectangle((xs - 0.05, yb - bh / 2 + 0.025), wtot + 0.10, bh - 0.05, fc=c.fill, ec="none"))
                ax.text(xs, yb, t1, fontsize=LBL, fontweight="bold", color=c.text, va="center_baseline")
                ax.text(xs + text_w(fig, t1, LBL, "bold") + 0.06, yb, c.label, fontsize=LBL, color=c.text,
                        va="center_baseline")
        calls = [s for s in segs if not s[4]][::-1]  # rightmost first: nearest the bar, leaders never cross
        for j, (key, cnt, left, w, _) in enumerate(calls):
            c = VC[key]
            yc = yb - bh / 2 - 0.05 - (j + 0.5) * line
            xs = x0 + left + w / 2
            ax.plot([xs, xs, cx - 0.04], [yb - bh / 2, yc, yc], color=INK2, lw=0.6, solid_capstyle="butt")
            ax.text(cx, yc, f"{cnt}", fontsize=LBL, fontweight="bold", va="center_baseline", color=INK)
            ax.text(cx + text_w(fig, f"{cnt}", LBL, "bold") + 0.06, yc, c.label, fontsize=LBL, va="center_baseline",
                    color=INK)
        y -= title_h + bh + (0.05 + n_call * line if n_call else 0) + gap
    return save(fig, "vert_checks", out, png)


# ---------------------------------------------------------------- Fig. 4: harness power
KILL_RULE = ("a mutant is killed if OUR harness, on OUR inputs, reports a wrong answer (value, stdout, exit code or "
             "output file) or a crash; not counted: stderr-only differences, hangs/timeouts, kills by the system's own test")
HARNESS_PARTS = {}
HARNESS_ROWS = []      # full per-row record, written to results/harness_power.json

# arXiv version: a kill counts only on an input where the C is UB-clean under the rule the defect
# count uses (and, for FLOURINE, not on a min > max input, the domain the defect count uses). The rows below take
# `killed`/`built` from the arXiv re-scan instead of the shipped scan; the other rows' scans already apply the rule
# (results/arxiv_harness_power_ubfiltered.json ub_filter_audit; VERT re-checked under the full rule with no change,
# results/arxiv_harness_power_ubfiltered_vert.json). Each entry: file, key path to killed, built and (optional)
# undetermined, and the note appended to the row. FLOURINE_POWER is the one place to change FLOURINE's row.
FLOURINE_POWER = {
    "file": "results/arxiv_mutation_ubfilter_flourine_fuzzprofile.json",
    "killed": ("summary", "variants", "B_translation_agrees_on_counted_inputs", "ub_clean_and_not_min_gt_max", "killed"),
    "built": ("summary", "variants", "B_translation_agrees_on_counted_inputs", "ub_clean_and_not_min_gt_max", "built"),
    "mutated": "FLOURINE's shipped success translations that agree with the C on every counted input in FLOURINE's own build profile "
               "(fuzz: opt-level 3, overflow checks on; harness/arxiv_mutate_full_flourine_ubfilter.py --profile fuzz)",
    "note": "arXiv: results/arxiv_mutation_ubfilter_flourine_fuzzprofile.json, treatment B (B_translation_agrees_on_counted_inputs), "
            "count 'UB-clean, not min > max': every mutant rebuilt in FLOURINE's fuzz profile; a kill counts only on an input where the C "
            "is UB-clean and min <= max (the defect count's domain); treatment B drops the translations whose unmutated build diverges "
            "from the C on a counted input (the two compute_phase_step files, now counted defects), so the pool is translations that "
            "agree with the C"}
ARXIV_UB_FILTER = {
    "C2SaferRust coreutils": {
        "file": "results/arxiv_mutation_ubfilter_c2saferrust.json",
        "killed": ("summary", "rust", "killed_new"), "built": ("summary", "rust", "compiled"), "killed_shipped": ("summary", "rust", "killed_old"),
        "note": "arXiv: results/arxiv_mutation_ubfilter_c2saferrust.json (harness/arxiv_mutate_full_ubfilter.py): kills whose first killing "
                "case is not UB-clean re-scanned over the UB-clean cases only (all in uniq: clang's NULL+0 at linebuffer.c:65)"},
    "ACTOR": {
        "file": "results/arxiv_mutation_ubfilter_actor.json",
        "killed": ("summary", "killed_new"), "built": ("summary", "compiled"), "undetermined": ("summary", "undetermined_new"),
        "killed_shipped": ("summary", "killed_old"),
        "note": "arXiv: results/arxiv_mutation_ubfilter_actor.json (systems/actoruw/arxiv_mutate_ubfilter.py): the C checked under the full "
                "UB rule on every recorded killing input; mutants killed only on inputs that are not UB-clean re-scanned"},
    "FLOURINE scalar": FLOURINE_POWER,
}


def _dig(d, path):
    for k in path:
        d = d[k]
    return d


def apply_arxiv_ub_filter():
    """Replace killed/built of the ARXIV_UB_FILTER rows with the arXiv re-scan; keep the shipped scan's numbers."""
    for r in HARNESS_ROWS:
        a = ARXIV_UB_FILTER.get(r["label"])
        if not a:
            continue
        d = J_opt(a["file"])
        if d is None:
            warn(f"harness_power: {r['label']} kept at the shipped scan ({a['file']} absent)")
            continue
        if "killed_shipped" in a:
            assert _dig(d, a["killed_shipped"]) == r["killed"], (r["label"], "re-scan's killed_old differs from the shipped scan")
        r["killed_shipped_scan"], r["built_shipped_scan"] = r["killed"], r["built"]
        r["killed"], r["built"] = _dig(d, a["killed"]), _dig(d, a["built"])
        if "undetermined" in a:
            r["undetermined"] = _dig(d, a["undetermined"])
        r["rate"] = round(r["killed"] / r["built"], 4) if r["built"] else None
        if a.get("mutated"):
            r["mutated_program"] = a["mutated"]
        r["source_arxiv"] = a["file"] + " " + ".".join(a["killed"])
        r["note"] = (r["note"] + " | " if r["note"] else "") + a["note"] + f" (shipped scan: {r['killed_shipped_scan']}/{r['built_shipped_scan']})"
        if r["label"] == "C2SaferRust coreutils":
            w = (d["summary"].get("rust_WIP") or {})
            r["note"] += f"; on C2SaferRust's own output (rust_WIP) under the same rule: {w.get('killed_new')}/{w.get('compiled')}"


def _hp_row(label, killed, built, *, undetermined=0, separable=True, mutated, source, note):
    r = dict(label=label, killed=killed, built=built, rate=round(killed / built, 4) if built else None,
             undetermined=undetermined, rule_separable=separable, mutated_program=mutated, source=source, note=note)
    HARNESS_ROWS.append(r)
    return r


def harness_power():
    """(label, killed, built) per harness under ONE kill rule (KILL_RULE), recomputed from the per-mutant
    records. `undetermined` = mutants the rule cannot decide from the record (a hang ended or masked the
    run; or inputs not re-checked); they are NOT counted as killed. A row whose per-mutant record cannot
    separate stderr-only/timeout kills is flagged (label dagger, rule_separable False): its rate is an upper bound. Rows with undetermined
    mutants get '*': their rate is a lower bound."""
    HARNESS_ROWS.clear()
    # VERT scalar: the grid-v2 run (the primary VERT scalar harness; results/vert_mutation_primary.json is grid v1)
    v = J_opt("results/vert_mutation_primary_grid2.json")
    if v:
        hops = set(v["summary"]["headline_ops"])
        ms = [m for f in v["per_function"] for m in f["mutants"] if m["op"] in hops and m["outcome"] != "compile_fail"]
        k = sum(m["outcome"] in ("killed_value", "killed_panic") for m in ms)
        und = sum(m["outcome"] not in ("killed_value", "killed_panic") and (m.get("unrechecked_slow") or 0) > 0 for m in ms)
        h = v["summary"]["overall_headline"]
        assert (k, len(ms)) == (h["killed"], h["assessable"]), ("VERT scalar per-mutant vs summary", k, len(ms), h)
        _hp_row("VERT scalar", k, len(ms), undetermined=und,
                mutated=f"each agreeing VERT translation (the {v['summary']['population']['control_agree']} functions with no divergence on grid v2; systems/vert/mutate_primary.py --grid v2)",
                source="results/vert_mutation_primary_grid2.json per_function[].mutants[] (headline ops)",
                note=f"{sum(m['outcome'] == 'rust_hang' for m in ms)} hang-only and {sum(m['outcome'] == 'ub_only' for m in ms)} UB-only mutants not killed; "
                     f"{und} not-killed mutants have slow inputs that were not re-checked")
    else:
        warn("harness_power.pdf row skipped: VERT scalar (results/vert_mutation_primary_grid2.json absent)")
    v = J_opt("results/vert_arrays_mutation_primary.json")
    if v:
        hops = set(v["summary"]["headline_ops"])
        ms = [m for f in v["per_function"] for m in f["mutants"] if m["op"] in hops and m["outcome"] != "compile_fail"]
        hang = [m for m in ms if m.get("rust_batch_timeout")]
        k = sum(m["outcome"] in ("killed_value", "killed_panic") and not m.get("rust_batch_timeout") for m in ms)
        assert k == v["summary"]["overall_headline"]["hang_kills_not_counted"]["killed"], "VERT arrays per-mutant vs summary"
        _hp_row("VERT array/float", k, len(ms), undetermined=len(hang),
                mutated="each agreeing VERT array/float translation (systems/vert/mutate_primary.py)",
                source="results/vert_arrays_mutation_primary.json per_function[].mutants[] (headline ops)",
                note=f"{len(hang)} mutants hit the 25 s Rust batch timeout; the array harness then reports a crash on every row "
                     "(no per-input fallback), so whether they also give a wrong answer is unknown; not counted")
    else:
        warn("harness_power.pdf row skipped: VERT array/float (results/vert_arrays_mutation_primary.json absent)")
    f = J("results/flourine_mutation.json")
    st = collections.Counter(m["status"] for m in f["mutants"])
    k, n = st["killed_value"] + st["killed_crash"], sum(c for s_, c in st.items() if s_ != "noncompile")
    _hp_row("FLOURINE scalar", k, n, mutated="FLOURINE's agreeing shipped translations (harness/mutate_full_flourine.py)",
            source="results/flourine_mutation.json mutants[].status", note=f"statuses {dict(st)}; value or Rust panic only")
    c = J("results/cli_mutation_full.json")
    st = collections.Counter(m["status"] for m in c["mutants"])
    k, n = st["killed_s1"] + st["killed_crash"], sum(x for s_, x in st.items() if s_ != "noncompile")
    w = ((c.get("rust_WIP_reported_separately") or {}).get("summary") or {}).get("overall") or {}
    _hp_row("C2SaferRust coreutils", k, n,
            mutated="C2Rust's starting translation (coreutils/src/<util>/rust), not C2SaferRust's output (harness/mutate_full.py:12-14, 96)",
            source="results/cli_mutation_full.json mutants[].status",
            note=f"{st['killed_s2']} stderr-only and {st['hang']} hang-only mutants not counted; on C2SaferRust's own output "
                 f"(rust_WIP, cases where it agrees): {w.get('killed_s1_or_crash')}/{w.get('compiled')}")
    s_ = J_opt("results/sactor_libogg_mutation.json")
    s2 = J_opt("results/sactor_crust_mutation.json")
    if s_ and s2:
        lk = sum(t["killed"] for t in s_["targets"].values())
        ln = sum(t["valid"] for t in s_["targets"].values())
        cm = [m for smp in s2["samples"].values() for m in smp.get("mutants", []) if m.get("status") == "built"]
        ck = sum(bool(m.get("killed_by_grid")) for m in cm)
        HARNESS_PARTS["SACTOR"] = [("libogg harness", lk, ln), ("CRUST grid (own test excluded)", ck, len(cm))]
        _hp_row("SACTOR, pooled", lk + ck, ln + len(cm), separable=False,
                mutated="SACTOR's agreeing translations (libogg gpt-5 framing.c, gpt-4o bitwise.c; CRUST samples)",
                source="results/sactor_libogg_mutation.json targets[].mutants[], results/sactor_crust_mutation.json samples[].mutants[].killed_by_grid",
                note="kills by SACTOR's own test excluded; both harnesses kill on any (stdout, stderr, exit code) difference and a "
                     "timeout also differs (systems/sactor/libogg/mutate.py:147-148, systems/sactor/crust/mutate.py:90); the per-mutant "
                     "records keep only killing case ids / a boolean, so stderr-only and timeout kills cannot be separated: upper bound")
    else:
        warn("harness_power.pdf row skipped: SACTOR (per-mutant files absent)")
    h = J_opt("results/his2trans_power.json")
    if h:
        ms = [m for p in h["mutation"].values() for m in p["mutants"] if m["status"] != "stillborn"]
        st = collections.Counter(m["status"] for m in ms)
        _hp_row("His2Trans", st["killed"], len(ms),
                mutated="His2Trans's OSS8 crates for avl, qsort, quadtree, urlparser, on the cases where the unmutated crate agrees (systems/his2trans/power.py:100-102)",
                source="results/his2trans_power.json mutation[].mutants[].status",
                note=f"statuses {dict(st)}: no 'killed(hang)' (power.py:123), so every kill has a non-hang difference. power.py:115-118 "
                     "also kills on a raw stderr difference and the record keeps no channel; from code, neither the drivers "
                     "(systems/his2trans/drivers) nor the mutated crates write to stderr, so Rust stderr is non-empty only on a "
                     "panic/abort, which also changes the exit code: no stderr-only kill is possible")
    a = J_opt("results/adatrans_mutation_run1.json")
    if a:
        ms = [m for m in a["mutants_detail"].values() if m["status"] != "stillborn"]
        k = sum(m["status"] == "killed" and m.get("kind") in ("S1", "panic", "crash") for m in ms)
        und = sum(m["status"] == "killed" and m.get("kind") == "hang" for m in ms)
        other = sum(m["status"] == "killed" and m.get("kind") not in ("S1", "panic", "crash", "hang") for m in ms)
        _hp_row("AdaTrans", k, len(ms), undetermined=und + other,
                mutated="AdaTrans run1 translations that agree with the C on all our inputs (systems/adatrans/mutate.py:1-8)",
                source="results/adatrans_mutation_run1.json mutants_detail[].kind",
                note=f"mutate.py stops at the first non-agreeing input (eval_mutant); {und} mutants stopped at a hang and "
                     f"{other} at a stderr/whitespace-only difference, so whether a later input gives a wrong answer is unknown; not counted")
    p = J_opt("results/ptrtrans_quadtree_mutation.json")
    if p:
        ms = [m for m in p["mutants"] if m["status"] != "build_fail"]
        k = sum(m["status"] == "killed" and bool(set(m.get("kill_kinds") or {}) & {"wrong_output", "panic", "crash"}) for m in ms)
        _hp_row("PtrTrans", k, len(ms),
                mutated=f"PtrTrans's shipped quadtree crate plus a one-line find_ repair, not the shipped crate itself; "
                        f"{p['summary'].get('cases_baseline_disagrees')} cases where that baseline disagrees are excluded",
                source="results/ptrtrans_quadtree_mutation.json mutants[].kill_kinds",
                note="kills are wrong_output/panic; no batch timeout among killed mutants")
    a = J_opt("results/actoruw_mutation.json")
    if a:
        ms = [m for m in a["mutants"] if m["status"] not in ("stillborn", "baseline_excluded")]
        k = sum(m["status"] == "killed" and m.get("kill_kind") in ("wrong_answer", "crash") for m in ms)
        und = sum(m["status"] == "undetermined" for m in ms)
        assert (k, len(ms)) == (a["total"]["killed"], a["total"]["compiled"]), ("ACTOR per-mutant vs total", k, len(ms))
        tm = a["translations_mutated"]
        _hp_row("ACTOR", k, len(ms), undetermined=und,
                mutated=f"ACTOR translations that agree with the C on all our inputs ({tm['exe']} executables, {tm['lib']} "
                        "libraries; systems/actoruw/mutate.py)",
                source="results/actoruw_mutation.json mutants[].status/kill_kind",
                note=f"every input is run (no stop at a hang); {und} mutants hang on some input and give no wrong answer or "
                     f"crash on any input: undetermined, not counted; stderr-only differences are not kills")
    else:
        warn("harness_power.pdf row skipped: ACTOR (results/actoruw_mutation.json absent)")
    apply_arxiv_ub_filter()
    out = [(r["label"] + ("\u2020" if not r["rule_separable"] else "*" if r["undetermined"] else ""), r["killed"], r["built"])
           for r in HARNESS_ROWS]
    return out


def write_harness_power_json(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    json.dump({"provenance": {"script": "scripts/plot_results.py (harness_power)",
                              "command": "python3 -I scripts/plot_results.py",
                              "arxiv_ub_filter": {k: v["file"] for k, v in ARXIV_UB_FILTER.items()}},
               "kill_rule": KILL_RULE + "; a kill counts only on an input where the C is UB-clean under the rule the defect count uses "
                                        "(FLOURINE: and min <= max, as the defect count)",
               "label_marks": {"*": "lower bound: `undetermined` mutants are not counted as killed",
                               "\u2020": "upper bound: the per-mutant record cannot separate stderr-only and timeout kills (rule_separable false)"},
               "rows": HARNESS_ROWS}, open(path, "w"), indent=1)
    return path


def fig_harness_power(mut, out, png):
    """Dot (rate) and whisker (Wilson 95%) per harness, sorted by rate, one colour; names and k/n on the left."""
    rows = sorted(mut, key=lambda r: (-r[1] / r[2], r[0]))
    W = COL_W
    fig = plt.figure(figsize=(W, 1))
    counts = [f"{k:,}/{n:,}" for _, k, n in rows]
    wn = max(text_w(fig, lab, LBL) for lab, *_ in rows)
    wc = max(text_w(fig, s, LBL) for s in counts)
    x_names, R = 0.04, 0.16
    L = x_names + wn + 0.14 + wc + 0.14
    pitch = 0.255
    hp = pitch * len(rows)
    bot, top = 0.42, 0.06
    H = bot + hp + top
    fig.set_size_inches(W, H)
    ax = axes_in(fig, L, bot, W - L - R, hp)
    for y, (lab, k, n) in enumerate(rows):
        p, lo, hi = wilson(k, n)
        ax.plot([0, 100], [y, y], color="#E6E8EC", lw=0.6, zorder=0, solid_capstyle="butt")
        ax.plot([lo, hi], [y, y], color=NAVY, lw=1.3, zorder=2, solid_capstyle="butt")
        for xx in (lo, hi):
            ax.plot([xx, xx], [y - 0.13, y + 0.13], color=NAVY, lw=1.0, zorder=2)
        ax.plot([p], [y], "o", ms=4.6, mfc=NAVY, mec="white", mew=0.7, zorder=3)
        ax.annotate(f"{p:.1f}%", (p, y), xytext=(0, 5.0), textcoords="offset points", ha="center", va="bottom",
                    fontsize=LBL, color=INK)
        yy = bot + hp - (y + 0.5) * pitch           # inches, row centre
        fig.text(x_names / W, yy / H, lab, fontsize=LBL, va="center_baseline", ha="left", color=INK)
        fig.text((L - 0.14) / W, yy / H, counts[y], fontsize=LBL, va="center_baseline", ha="right", color=INK2)
    ax.set_ylim(len(rows) - 0.5, -0.62)
    ax.set_xlim(0, 100)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.tick_params(axis="x", labelsize=TICK)
    ax.set_yticks([])
    ax.spines["left"].set_visible(False)
    ax.set_xlabel("% of compiled mutants killed (Wilson 95% CI)", fontsize=AXT, labelpad=3, loc="right")
    return save(fig, "harness_power", out, png)


# ---------------------------------------------------------------- Fig. 1: pipeline
def _card(ax, x, y, w, h, fc="white", ec=CARD_EDGE, r=0.05, lw=0.6):
    ax.add_patch(FancyBboxPatch((x + r, y + r), w - 2 * r, h - 2 * r, boxstyle=f"round,pad={r}", fc=fc, ec=ec, lw=lw))


def _arrow(ax, a, b, head=True):
    ax.add_patch(FancyArrowPatch(a, b, arrowstyle="-|>" if head else "-", mutation_scale=7, lw=0.8, color=ARROW,
                                 shrinkA=0, shrinkB=0))


def _lines(ax, x, y, items, ha="left", step=0.118):
    """items: (text, size, weight, color); y is the first baseline, going down. Returns the next y."""
    for text, size, weight, color in items:
        ax.text(x, y, text, ha=ha, va="baseline", fontsize=size, fontweight=weight, color=color)
        y -= step if size < 7 else step + 0.02
    return y


def _wrap(names, width):
    """Comma-separated lines of at most `width` characters."""
    lines, cur = [], ""
    for i, nm in enumerate(names):
        piece = nm + ("," if i < len(names) - 1 else "")
        if cur and len(cur) + 1 + len(piece) > width:
            lines.append(cur)
            cur = piece
        else:
            cur = f"{cur} {piece}".strip()
    return lines + [cur]


def fig_pipeline(S, out, png):
    D = sum(s["n"] for s in S)
    T = sum(s["defective"] for s in S)
    A = sum(s["acc"] for s in S)
    names = [s["name"] for s in S]                 # the systems in the ledger, Table I order
    sv = J_opt("docs/survey-evidence.json")
    if sv:
        rule = {MT.norm_system(x["display"]): (x.get("artifact") or {}).get("regrade_rule") for x in sv["systems"]}
        for s in S:
            if rule.get(s["key"]) not in ("Meets", "Partial"):
                warn(f"pipeline: {s['name']} has re-grade rule {rule.get(s['key'])!r} in docs/survey-evidence.json")

    W, H = PAGE_W, 2.30
    fig = plt.figure(figsize=(W, H))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(0, H)
    ax.axis("off")
    pws = [1.30, 1.24, 1.48, 1.22, 1.30]           # stage widths: RUN AND COMPARE holds the most text
    gap = (W - sum(pws)) / 4
    xs = [sum(pws[:i]) + i * gap for i in range(5)]
    cx = lambda i: xs[i] + 0.08                      # card left
    cw = lambda i: pws[i] - 0.16                     # card width
    top, bot = H - 0.32, 0.10                       # card area
    T7, B, S7 = 7, 6.5, 7.5                         # title, body, section sizes
    for i, name in enumerate(["ARTIFACTS", "BUILD", "RUN AND COMPARE", "DEFECTS", "ANALYSES"]):
        ax.add_patch(FancyBboxPatch((xs[i] + 0.06, 0.08), pws[i] - 0.12, H - 0.16, boxstyle="round,pad=0.06",
                                    fc=PANEL, ec="none"))
        ax.text(xs[i] + 0.09, H - 0.18, str(i + 1), fontsize=7.5, fontweight="bold", color=BLUE, va="baseline")
        ax.text(xs[i] + 0.21, H - 0.18, name, fontsize=7.5, fontweight="bold", color=INK, va="baseline")

    # 1 released artifacts
    _card(ax, cx(0), bot, cw(0), top - bot)
    m = cx(0) + cw(0) / 2
    y = _lines(ax, m, top - 0.22, [("Released artifacts", S7, "bold", INK)], ha="center")
    y = _lines(ax, m, y - 0.10, [(f"{len(names)} systems", T7, "bold", INK), ("ship the original C", B, "normal", INK2),
                                  ("and their own Rust:", B, "normal", INK2)], ha="center")
    _lines(ax, m, y - 0.05, [(ln, B, "normal", NAVY) for ln in _wrap(names, 22)], ha="center", step=0.115)

    # 2 build: original C, its own Rust
    h2 = 0.74
    ys2 = [top - h2, bot]
    build = [("Original C", ["the oracle; its own", "build files, else gcc"]),
             ("Its own Rust", ["as released, in the", "profile its pipeline uses"])]
    for yb, (title, body) in zip(ys2, build):
        _card(ax, cx(1), yb, cw(1), h2)
        _lines(ax, cx(1) + 0.10, yb + h2 / 2 + 0.105, [(title, T7, "bold", INK)] + [(t, B, "normal", INK2) for t in body])

    # 3 run and compare: four steps
    _card(ax, cx(2), bot, cw(2), top - bot)
    steps = [("1", "Generated inputs", ["argument grids, flags", "× corpus, API calls"]),
             ("2", "Run both", ["C and Rust, same input,", "same machine"]),
             ("3", "Compare outputs", ["value, stdout, exit", "code, stderr, files"]),
             ("4", "One UB rule", ["C clean under ASan +", "UBSan, GCC and Clang;", "-O0/1/2; char sign"])]
    yb = top - 0.21
    for num, title, body in steps:
        ax.add_patch(Circle((cx(2) + 0.14, yb + 0.03), 0.062, fc=NAVY, ec="none"))
        ax.text(cx(2) + 0.14, yb + 0.03, num, ha="center", va="center", fontsize=6, fontweight="bold", color="white")
        _lines(ax, cx(2) + 0.26, yb, [(title, T7, "bold", INK)] + [(t, B, "normal", INK2) for t in body], step=0.108)
        yb -= 0.13 + 0.108 * len(body) + 0.08

    # 4 defects (numbers from the ledger)
    mid = (top + bot) / 2
    rh = 1.16
    _card(ax, cx(3), mid - rh / 2, cw(3), rh, fc=NAVY, ec=NAVY)
    m = cx(3) + cw(3) / 2
    y = _lines(ax, m, mid + rh / 2 - 0.22, [("Defects", S7, "bold", "white"),
                                            ("one per mechanism,", B, "normal", "#C9D9F0"),
                                            ("confirmed in the source", B, "normal", "#C9D9F0")], ha="center")
    _lines(ax, m, y - 0.10, [(f"{D} defects", T7 + 0.5, "bold", "white"),
                             (f"in {T} translations;", B + 0.2, "normal", "#E6EEFB"),
                             (f"{A} accepted by", B + 0.2, "normal", "#E6EEFB"),
                             ("the system's own check", B + 0.2, "normal", "#E6EEFB")], ha="center")

    # 5 analyses
    rq = [("RQ1", "Defects?", ["wrong answers and", "crashes, per system"]),
          ("RQ2", "Own check", ["accepted? did it run", "an exposing input?"]),
          ("RQ3", "Real?", ["C UB, preconditions,", "harness, toolchain"]),
          ("+", "Harness power", ["seeded faults that", "our harness detects"])]
    h5 = 0.42
    g5 = (top - bot - len(rq) * h5) / (len(rq) - 1)
    ys5 = [top - h5 - k * (h5 + g5) for k in range(len(rq))]
    for yb, (tag, title, body) in zip(ys5, rq):
        _card(ax, cx(4), yb, cw(4), h5)
        ax.text(cx(4) + 0.09, yb + h5 - 0.145, tag, fontsize=T7, fontweight="bold", color=BLUE, va="baseline")
        ax.text(cx(4) + (0.37 if tag != "+" else 0.20), yb + h5 - 0.145, title, fontsize=T7, fontweight="bold",
                color=INK, va="baseline")
        ax.text(cx(4) + 0.09, yb + 0.165, body[0], fontsize=B, color=NAVY, va="baseline")
        ax.text(cx(4) + 0.09, yb + 0.058, body[1], fontsize=B, color=NAVY, va="baseline")

    # arrows
    right = lambda i: cx(i) + cw(i)
    for yb in ys2:
        yc = yb + h2 / 2
        _arrow(ax, (right(0), yc), (cx(1), yc))
        _arrow(ax, (right(1), yc), (cx(2), yc))
    _arrow(ax, (right(2), mid), (cx(3), mid))
    xm = (right(3) + cx(4)) / 2
    centers = [yb + h5 / 2 for yb in ys5]
    _arrow(ax, (right(3), mid), (xm, mid), head=False)
    _arrow(ax, (xm, centers[0]), (xm, centers[-1]), head=False)
    for yc in centers:
        _arrow(ax, (xm, yc), (cx(4), yc))
    # text must stay inside its card: check every text's right edge against the card it starts in
    r_ = fig.canvas.get_renderer()
    for t in ax.texts:
        e = t.get_window_extent(r_)
        if e.y1 / fig.dpi > top:                    # stage headers sit on the panel, not in a card
            continue
        x0, x1 = e.x0 / fig.dpi, e.x1 / fig.dpi
        for i in range(5):
            if cx(i) - 0.01 <= x0 <= right(i) and x1 > right(i) - 0.03:
                warn(f"pipeline: text {t.get_text()!r} runs past card {i + 1}'s edge by {x1 - right(i) + 0.03:.2f} in")
    sz = save(fig, "pipeline", out, png)
    return dict(defects=D, translations=T, accepted=A, systems=len(names), size=sz)


# ---------------------------------------------------------------- main
def draw(out, png, hp_json):
    S = ledger()
    pipe = fig_pipeline(S, out, png)
    so = fig_outcomes(S, out, png)
    rows, mut = vert_checks(), harness_power()
    sv = fig_vert_checks(rows, out, png)
    sh = fig_harness_power(mut, out, png)
    hpj = write_harness_power_json(hp_json)
    print(f"font: {FONTS[0]} (stack {FONTS})")
    print(f"pipeline.pdf {pipe['size'][0]:.2f} x {pipe['size'][1]:.2f} in: {pipe['defects']} defects, "
          f"{pipe['translations']} translations, {pipe['accepted']} accepted, {pipe['systems']} systems (from the ledger)")
    print(f"outcomes.pdf {so[0]:.2f} x {so[1]:.2f} in:")
    for s in S:
        print(f"  {s['name']:12s} tested={s['tested']} defective={s['defective']} acc/rej/nr={s['acc']}/{s['rej']}/{s['nr']}"
              f"  defects={s['n']} (S1 {s['s1']}, crash {s['crash']})  exposure={dict(sorted(s['exp'].items()))}"
              f"{'  UB-pending' if s['pending'] else ''}")
    print(f"vert_checks.pdf {sv[0]:.2f} x {sv[1]:.2f} in:")
    for title, sub, v in rows:
        print(f"  {title + ' (' + sub + ')':55s} {dict((k, x) for k, x in v.items() if x)}")
    hpj_shown = os.path.relpath(hpj, REPO) if os.path.abspath(hpj).startswith(REPO + os.sep) else hpj
    print(f"harness_power.pdf {sh[0]:.2f} x {sh[1]:.2f} in ({hpj_shown}):")
    for name, parts in HARNESS_PARTS.items():
        print(f"  {name} pools: " + "; ".join(f"{p} {k}/{n}" for p, k, n in parts))
    for lab, k, n in mut:
        p, lo, hi = wilson(k, n)
        print(f"  {lab:45s} {k}/{n} = {p:.1f}% [{lo:.1f}, {hi:.1f}]")
    for w in WARN:
        print("WARN:", w, file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", help="output directory (default out/figures)")
    ap.add_argument("--png", help="also write 300-dpi PNG previews here")
    ap.add_argument("--check", metavar="DIR", help="redraw into a temp dir and compare with DIR's PDFs")
    ap.add_argument("--harness-json", metavar="FILE",
                    help="where to write the harness-power rows (default: results/harness_power.json when neither "
                         "--out nor --check is given, else harness_power.json next to the PDFs)")
    a = ap.parse_args()
    if a.check:
        tmp = tempfile.mkdtemp(prefix="figures-")
        draw(tmp, a.png, a.harness_json or os.path.join(tmp, "harness_power.json"))
        ok = True
        for nm in NAMES:
            p, q = os.path.join(tmp, f"{nm}.pdf"), os.path.join(os.path.abspath(a.check), f"{nm}.pdf")
            same = os.path.isfile(q) and open(p, "rb").read() == open(q, "rb").read()
            print(f"{nm}.pdf: {'identical to' if same else 'DIFFERS from'} {q}")
            ok &= same
        sys.exit(0 if ok else 1)
    out = os.path.abspath(a.out) if a.out else OUT_DEFAULT
    hp_json = a.harness_json or (os.path.join(out, "harness_power.json") if a.out else os.path.join(REPO, "results", "harness_power.json"))
    draw(out, a.png, hp_json)
    print("wrote", ", ".join(os.path.join(out, f"{nm}.pdf") for nm in NAMES))


if __name__ == "__main__":
    main()
