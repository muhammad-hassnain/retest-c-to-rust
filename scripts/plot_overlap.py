#!/usr/bin/env python3
"""Draw overlap.pdf: for each system whose artifact records its check's inputs, two circles, the
inputs we ran and the inputs the system's check ran, with areas proportional to the counts and the
overlap drawn to scale. All numbers are read from the result files (the same fields the claims
ledger checks).

    python3 scripts/plot_overlap.py --out out/figures
"""
import argparse, math, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plot_results as P                     # style, colours, J(), save()
from matplotlib.patches import Circle
from matplotlib.lines import Line2D

plt = P.plt


def rows():
    c2 = P.J("results/heldout_c2saferrust.json")
    ac = P.J("results/actoruw_summary.json")["heldout"]["exe"]
    ad = P.J("results/adatrans_summary.json")["heldout"]["overlap_of_our_inputs_with_checks"]
    ra = P.J("results/heldout_rustassure.json")
    bz = P.J("results/heldout_rustmap.json")["bzip2"]
    n_csv = P.J("systems/rustassure/libcsv_e2e/results.json")["n_cases"]
    return [
        ("C2SaferRust", "coreutils", c2["our_inputs"]["cases"], c2["system_inputs"]["acceptance_set_tests"]["invocations"],
         c2["overlap"]["distinct_inputs_in_both"]),
        ("ACTOR", "executables", ac["our_inputs"], ac["recorded_vectors"], ac["our_inputs_exactly_recorded"]),
        ("AdaTrans", "programs", ad["ours"], ad["examples"] + ad["fuzz"], ad["ours_in_check_exact"]),
        ("RustAssure", "libcsv", n_csv, ra["system_check"]["distinct_inputs"], ra["overlap"]["exact_input_and_options"]),
        ("RustMap", "bzip2", len(bz["our_inputs"]), len(bz["system_inputs"]), len(bz["overlap_exact"])),
    ]


def lens(r1, r2, d):
    """Area of the intersection of two circles with radii r1, r2 at centre distance d."""
    if d >= r1 + r2:
        return 0.0
    if d <= abs(r1 - r2):
        return math.pi * min(r1, r2) ** 2
    a = r1 * r1 * math.acos((d * d + r1 * r1 - r2 * r2) / (2 * d * r1))
    b = r2 * r2 * math.acos((d * d + r2 * r2 - r1 * r1) / (2 * d * r2))
    c = 0.5 * math.sqrt((-d + r1 + r2) * (d + r1 - r2) * (d - r1 + r2) * (d + r1 + r2))
    return a + b - c


def distance_for(r1, r2, target):
    """Centre distance giving an intersection of area `target` (bisection); a small gap if 0."""
    if target <= 0:
        return r1 + r2 + 0.06 * max(r1, r2)
    lo, hi = abs(r1 - r2), r1 + r2
    for _ in range(80):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if lens(r1, r2, mid) > target else (lo, mid)
    return (lo + hi) / 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(P.REPO, "figures"))
    ap.add_argument("--png", default=None)
    a = ap.parse_args()
    data = rows()
    W = P.COL_W
    n = len(data)
    top, mid, bot = 0.20, 0.50, 0.30           # legend band, circle band, label band (in)
    H = top + mid + bot
    fig = plt.figure(figsize=(W, H))
    cell = (W - 0.04) / n
    rmax = 0.17                                  # radius (in) of the larger circle in each cell
    for i, (sysname, what, ours, theirs, shared) in enumerate(data):
        x0 = 0.02 + i * cell
        big = max(ours, theirs)
        r_o = rmax * math.sqrt(ours / big)
        r_t = max(rmax * math.sqrt(theirs / big), 0.012)
        scale = math.pi * rmax ** 2 / big        # area (in^2) per input
        d = distance_for(r_o, max(rmax * math.sqrt(theirs / big), 1e-6), shared * scale)
        span = r_o + d + r_t                     # left edge of blue to right edge of red
        ax = P.axes_in(fig, x0, bot, cell, mid)
        ax.set_xlim(0, cell); ax.set_ylim(-mid / 2, mid / 2); ax.set_aspect("equal"); ax.axis("off")
        x_o = (cell - span) / 2 + r_o
        ax.add_patch(Circle((x_o, 0), r_o, facecolor=P.LIGHT, edgecolor=P.NAVY, lw=0.7, alpha=0.9))
        ax.add_patch(Circle((x_o + d, 0), r_t, facecolor=P.PINK, edgecolor=P.RED, lw=0.7, alpha=0.85))
        ax.add_patch(Circle((x_o + d, 0), r_t, fill=False, hatch="////", edgecolor=P.RED, lw=0))   # survives greyscale
        cx = (x0 + cell / 2) / W
        fig.text(cx, (bot - 0.07) / H, sysname, fontsize=P.LBL - 0.5, fontweight="bold", color=P.INK, ha="center", va="center")
        fig.text(cx, (bot - 0.20) / H, f"{shared:,} shared", fontsize=P.LBL - 1, color=P.INK2, ha="center", va="center")
    # legend at the top (patches, so the hatch shows in the key too)
    from matplotlib.patches import Patch
    handles = [Patch(facecolor=P.LIGHT, edgecolor=P.NAVY, lw=0.7, label="inputs we ran"),
               Patch(facecolor=P.PINK, edgecolor=P.RED, hatch="////", lw=0.7, label="inputs the system's check ran")]
    fig.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.0, 1.0), ncol=2, fontsize=P.LBL - 0.5,
               handlelength=1.4, handleheight=0.9, columnspacing=1.2, borderaxespad=0.15, frameon=False)
    P.save(fig, "overlap", a.out, a.png)
    for r in data:
        print(r)


if __name__ == "__main__":
    main()
