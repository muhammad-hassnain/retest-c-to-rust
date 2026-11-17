"""Deterministic case generator for the PtrTrans quadtree differential harness.

No model is queried and nothing is random: every input is built from fixed shapes (as in the paper's
method section). A *case* is one command script (see qt_oracle.c) run once on the C and once on the
Rust. Families:

  tree.dump    quadtree_new + an insert sequence (every insert's return value and the length are
               printed) + a full quadtree_walk dump of the tree (per node: isleaf/isempty/ispointer,
               bounds, point, key) + length.
  tree.search  the same tree built silently, then ONE quadtree_search. One query per case, because
               the C's find_ dereferences NULL when a query path ends at an empty or missing
               quadrant (that input is then UB in the C and excluded by the UB rule; isolating each
               query keeps the other queries of the same tree countable).
  tree.prefix  a search for the first inserted point after only the first insert (root is a leaf).
  keyfree      tree.dump with tree->key_free set to a logging callback (a public struct field).
  unit.bounds  quadtree_bounds_new + quadtree_bounds_extend sequences.
  unit.node    quadtree_node_with_bounds / quadtree_node_new predicates and bounds.
  unit.point   quadtree_point_new.
  upstream     the exact call sequence of the C project's own test.c (test_tree), plus its search.

Shapes: tree bounds (benchmark 0..1000, test.c 1..10, centred, non-square, swapped min/max,
huge, overflowing width, tiny, zero width, infinite, NaN) x insert sequences (empty, single, two
in different / same quadrant, duplicates, boundary, internal split lines, out of bounds / NaN /
inf, lattices, deterministic-LCG integer and real points, 1-ulp neighbours, signed zero, NULL keys).
Queries per tree: every distinct inserted point (at most 16, evenly spaced) and fixed absent,
outside, split-line, signed-zero and NaN points.
"""
import math
import struct

INF = math.inf
NAN = float("nan")
QUERY_CAP = 16


def h(x):
    """IEEE-754 bits as 16 hex digits. Every NaN is written as the positive quiet NaN: a NaN computed
    by the generator (e.g. 0*inf) has an architecture-dependent sign (x86-64 gives 0xfff8..., arm64
    0x7ff8...), and the case set must not depend on the host."""
    if math.isnan(x):
        return "7ff8000000000000"
    return struct.pack(">d", float(x)).hex()


BOUNDS = [
    # name, (minx, miny, maxx, maxy), violates the parameter names' min<=max precondition
    ("bench_0_1000", (0.0, 0.0, 1000.0, 1000.0)),
    ("testc_1_10", (1.0, 1.0, 10.0, 10.0)),
    ("centred_50", (-50.0, -50.0, 50.0, 50.0)),
    ("nonsquare_100x1", (0.0, 0.0, 100.0, 1.0)),
    ("swapped_10_1", (10.0, 10.0, 1.0, 1.0)),
    ("huge_1e300", (-1e300, -1e300, 1e300, 1e300)),
    ("overflow_1e308", (-1e308, -1e308, 1e308, 1e308)),
    ("tiny_1e-300", (0.0, 0.0, 1e-300, 1e-300)),
    ("zero_width", (5.0, 0.0, 5.0, 10.0)),
    ("infinite", (-INF, -INF, INF, INF)),
    ("unit_offset", (-1.0, -1.0, 3.0, 3.0)),
    ("nan_minx", (NAN, 0.0, 10.0, 10.0)),
]


def lcg(seed):
    s = seed & 0xFFFFFFFF
    while True:
        s = (1103515245 * s + 12345) & 0x7FFFFFFF
        yield s


def frac_point(b, fx, fy):
    minx, miny, maxx, maxy = b
    lox, hix = min(minx, maxx), max(minx, maxx)
    loy, hiy = min(miny, maxy), max(miny, maxy)
    if math.isnan(minx):
        lox = hix = maxx
    return (lox + fx * (hix - lox), loy + fy * (hiy - loy))


def sequences(bname, b):
    """name -> list of (x, y, key) inserts."""
    P = lambda fx, fy: frac_point(b, fx, fy)  # noqa: E731
    seqs = {}
    seqs["empty"] = []
    seqs["single"] = [(*P(0.3, 0.7), 0)]
    seqs["two_diff_quadrants"] = [(*P(0.3, 0.7), 0), (*P(0.8, 0.2), 1)]
    seqs["two_same_quadrant"] = [(*P(0.1, 0.1), 0), (*P(0.2, 0.15), 1)]
    seqs["three_testc_like"] = [(*P(7 / 9, 1 / 9), 0), (*P(1 / 9, 2 / 9), 1), (*P(1 / 9, 2 / 9), 2),
                                (*P(2 / 9, 0.1 / 9), 3)]
    seqs["duplicates"] = [(*P(0.3, 0.3), 0), (*P(0.3, 0.3), 1), (*P(0.3, 0.3), 2), (*P(0.7, 0.6), 3),
                          (*P(0.3, 0.3), 4), (*P(0.7, 0.6), 5)]
    minx, miny, maxx, maxy = b
    seqs["boundary"] = [(minx, miny, 0), (maxx, maxy, 1), (minx, (miny + maxy) / 2, 2),
                        ((minx + maxx) / 2, maxy, 3),
                        (math.nextafter(minx, INF), math.nextafter(miny, INF), 4),
                        (math.nextafter(maxx, -INF), math.nextafter(maxy, -INF), 5)]
    seqs["split_lines"] = [(*P(0.5, 0.5), 0), (*P(0.2, 0.8), 1), (*P(0.5, 0.25), 2), (*P(0.75, 0.5), 3),
                           (*P(0.25, 0.25), 4)]
    seqs["out_of_bounds"] = [(*P(-0.5, 0.5), 0), (*P(0.5, 1.5), 1), (NAN, NAN, 2), (INF, 0.5, 3),
                             (-INF, -INF, 4), (*P(0.4, 0.6), 5)]
    seqs["lattice_4x4_centres"] = [(*P((i + 0.5) / 4, (j + 0.5) / 4), 4 * i + j)
                                   for i in range(4) for j in range(4)]
    seqs["lattice_5x5_with_lines"] = [(*P(i / 4, j / 4), 5 * i + j) for i in range(5) for j in range(5)]
    g = lcg(1)
    if bname == "bench_0_1000":
        seqs["lcg_int_256"] = [(float(next(g) % 1000), float(next(g) % 1000), k) for k in range(256)]
    else:
        seqs["lcg_int_64"] = [(*P((next(g) % 1000) / 1000, (next(g) % 1000) / 1000), k) for k in range(64)]
    g = lcg(7)
    seqs["lcg_real_64"] = [(*P(next(g) / 2 ** 31, next(g) / 2 ** 31), k) for k in range(64)]
    x0, y0 = P(0.3, 0.3)
    seqs["ulp_neighbours"] = [(x0, y0, 0), (math.nextafter(x0, INF), y0, 1), (x0, math.nextafter(y0, INF), 2),
                              (*P(0.9, 0.9), 3)]
    seqs["signed_zero"] = [(0.0, 0.5, 0), (-0.0, 0.5, 1), (0.5, -0.0, 2), (0.5, 0.0, 3), (*P(0.9, 0.1), 4)]
    seqs["null_keys"] = [(*P(0.3, 0.7), -1), (*P(0.8, 0.2), 1), (*P(0.3, 0.7), -1), (*P(0.6, 0.6), -1)]
    return seqs


def fmt_new(b, silent):
    return ("n " if silent else "N ") + " ".join(h(v) for v in b)


def script_build(b, seq, silent):
    lines = [fmt_new(b, silent)]
    for x, y, k in seq:
        lines.append(("i " if silent else "I ") + f"{h(x)} {h(y)} {k}")
    return lines


def same(a, b):
    return h(a[0]) == h(b[0]) and h(a[1]) == h(b[1])


def queries(b, seq):
    """(label, x, y) search points for one tree."""
    pts = []
    for x, y, _ in seq:
        if not any(same((x, y), p) for p in pts):
            pts.append((x, y))
    if len(pts) > QUERY_CAP:
        step = len(pts) / QUERY_CAP
        pts = [pts[int(i * step)] for i in range(QUERY_CAP)]
    qs = [("inserted", x, y) for x, y in pts]
    P = lambda fx, fy: frac_point(b, fx, fy)  # noqa: E731
    qs += [("absent_inside", *P(0.11, 0.13)), ("absent_inside", *P(0.61, 0.87)),
           ("centre_split_point", *P(0.5, 0.5)), ("outside", *P(-0.5, -0.5)),
           ("outside", *P(1.5, 0.5)), ("nan", NAN, NAN)]
    # signed-zero twin of an inserted point with a zero coordinate
    for x, y in pts:
        if x == 0.0 or y == 0.0:
            qs.append(("signed_zero_twin", -x if x == 0.0 else x, -y if y == 0.0 else y))
            break
    return qs


def corpus():
    cases = []

    def add(cid, family, script, **meta):
        cases.append({"id": cid, "family": family, "script": "\n".join(script) + "\n", "meta": meta})

    for bname, b in BOUNDS:
        swapped = b[0] > b[2] or b[1] > b[3]
        for sname, seq in sequences(bname, b).items():
            base = f"{bname}/{sname}"
            meta = {"bounds": bname, "seq": sname, "n_inserts": len(seq),
                    "violates_min_le_max": swapped}
            add(base + "/dump", "tree.dump", script_build(b, seq, False) + ["W", "L"], **meta)
            if len(seq) >= 1 and sname in ("duplicates", "two_diff_quadrants", "lattice_4x4_centres",
                                           "null_keys", "three_testc_like"):
                add(base + "/keyfree", "keyfree",
                    [fmt_new(b, False), "K"] + script_build(b, seq, False)[1:] + ["W", "L"], **meta)
            for qi, (ql, qx, qy) in enumerate(queries(b, seq)):
                add(f"{base}/search{qi:02d}_{ql}", "tree.search",
                    script_build(b, seq, True) + [f"S {h(qx)} {h(qy)}"], query=ql, **meta)
            if len(seq) >= 2:
                x, y, k = seq[0]
                add(base + "/prefix1_search_first", "tree.prefix",
                    script_build(b, seq[:1], True) + [f"S {h(x)} {h(y)}"], query="inserted", **meta)

    # upstream test.c test_tree, verbatim call sequence (val keys -> key 0), then its search
    tc = [fmt_new((1, 1, 10, 10), False), *(f"I {h(x)} {h(y)} 0" for x, y in
          [(0, 0), (10, 10), (110.0, 110.0), (8.0, 2.0), (2.0, 3.0), (2.0, 3.0), (3.0, 1.1)]),
          "L", f"S {h(3.0)} {h(1.1)}", "W"]
    add("upstream/test_c_test_tree", "upstream", tc, bounds="testc_1_10", seq="test.c",
        violates_min_le_max=False)

    # unit level: bounds_new + extend sequences
    vals = [0.0, -0.0, 1.0, -1.0, 5.0, 10.0, 1e308, -1e308, 5e-324, INF, -INF, NAN, 0.1, 3.0]
    for i, a in enumerate(vals):
        for j, c in enumerate(vals):
            add(f"unit.bounds/{i:02d}_{j:02d}", "unit.bounds",
                ["B", f"BX {h(a)} {h(c)}", f"BX {h(c)} {h(a)}", f"BX {h(5.0)} {h(5.0)}"],
                violates_min_le_max=False)
    # unit level: node_with_bounds over a grid of (min, max) pairs, incl. swapped / NaN / inf
    edges = [(0.0, 1.0), (1.0, 0.0), (-1.0, 1.0), (5.0, 5.0), (-0.0, 0.0), (0.0, -0.0),
             (NAN, 1.0), (1.0, NAN), (-INF, INF), (-1e308, 1e308), (1e-300, 2e-300)]
    for i, (x0, x1) in enumerate(edges):
        for j, (y0, y1) in enumerate(edges):
            add(f"unit.node/{i:02d}_{j:02d}", "unit.node", [f"NB {h(x0)} {h(y0)} {h(x1)} {h(y1)}"],
                violates_min_le_max=bool(x0 > x1 or y0 > y1))
    add("unit.node/new", "unit.node", ["NN"], violates_min_le_max=False)
    for i, a in enumerate(vals):
        add(f"unit.point/{i:02d}", "unit.point", [f"PN {h(a)} {h(vals[-1 - i])}"], violates_min_le_max=False)
    return cases


if __name__ == "__main__":
    import collections
    import json
    import sys
    cs = corpus()
    if len(sys.argv) > 1 and sys.argv[1] == "--dump":
        json.dump(cs, sys.stdout, indent=0)
    else:
        print(len(cs), dict(collections.Counter(c["family"] for c in cs)))
