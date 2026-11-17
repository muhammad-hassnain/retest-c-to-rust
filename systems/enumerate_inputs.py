"""Deterministic, systematic input enumeration for integer-signature functions.

No randomness, no AI: given each argument's integer range, we build a fixed, structured grid of
"interesting" values and take the (capped) cartesian product. The grid is reproducible — the same
signature always yields the same test set — and systematic by construction:

  * EXHAUSTIVE when the whole domain is small (e.g. a single u8 -> all 256; u8 x u8 -> all 65536).
  * otherwise a per-argument GRID of: the full dense low range 0..255 (where these functions are
    usually defined), small negatives, the type boundaries (min, min+1, max, max-1, 0, +-1, +-2),
    every power of two +-1, and an even sweep across the whole range.

This replaces seeded-random sampling so a "no divergence" result means "agrees on a structured,
reproducible grid" rather than "agrees on some random draws".
"""
from itertools import product

# rust int type -> (min, max, signed)
RANGES = {
    "u8": (0, 255, False), "u16": (0, 65535, False), "u32": (0, 2**32 - 1, False),
    "u64": (0, 2**64 - 1, False), "usize": (0, 2**64 - 1, False),
    "i8": (-128, 127, True), "i16": (-32768, 32767, True), "i32": (-2**31, 2**31 - 1, True),
    "i64": (-2**63, 2**63 - 1, True), "isize": (-2**63, 2**63 - 1, True),
    "bool": (0, 1, False), "c_int": (-2**31, 2**31 - 1, True), "c_uint": (0, 2**32 - 1, False),
    "c_char": (-128, 127, True),
}


def grid_for(lo, hi, target):
    """A deterministic, structured set of <= ~target values in [lo, hi]."""
    pts = set()
    # dense low range (these functions are typically defined on small non-negatives)
    for v in range(max(lo, 0), min(hi, 255) + 1):
        pts.add(v)
    # small negatives (signed)
    for v in range(max(lo, -16), min(hi, -1) + 1):
        pts.add(v)
    # type boundaries
    for v in (lo, lo + 1, lo + 2, hi, hi - 1, hi - 2, 0, 1, -1, 2, -2):
        if lo <= v <= hi:
            pts.add(v)
    # powers of two +-1 (both signs)
    k = 1
    while k <= hi:
        for d in (-1, 0, 1):
            if lo <= k + d <= hi:
                pts.add(k + d)
        k *= 2
    k = -1
    while k >= lo:
        for d in (-1, 0, 1):
            if lo <= k + d <= hi:
                pts.add(k + d)
        k *= 2
    # even sweep across the full range
    span = hi - lo
    for i in range(64):
        pts.add(lo + span * i // 63)
    out = sorted(pts)
    # thin deterministically to <= target, but always keep the boundary extremes
    if len(out) > target:
        keep = {lo, lo + 1, hi, hi - 1, 0, 1, -1}
        keep = {v for v in keep if lo <= v <= hi}
        step = len(out) / target
        thinned = [out[int(i * step)] for i in range(target)]
        out = sorted(set(thinned) | keep)
    return out


def _pow2_pm1(lo, hi):
    """Every power of two, its negation, and each +-1, inside [lo, hi]."""
    pts = set()
    k = 1
    while k <= hi:
        for d in (-1, 0, 1):
            if lo <= k + d <= hi:
                pts.add(k + d)
        k *= 2
    k = -1
    while k >= lo:
        for d in (-1, 0, 1):
            if lo <= k + d <= hi:
                pts.add(k + d)
        k *= 2
    return pts


def grid_v2_for(lo, hi, target):
    """v2 per-argument grid: the v1 grid_for(lo, hi, target) UNION every power of two +-1 and
    lo, lo+1, lo+2, hi-2, hi-1, hi (all within [lo, hi]). Never thinned, so it is a superset of the
    v1 grid and may exceed `target`."""
    pts = set(grid_for(lo, hi, target)) | _pow2_pm1(lo, hi)
    for v in (lo, lo + 1, lo + 2, hi - 2, hi - 1, hi):
        if lo <= v <= hi:
            pts.add(v)
    return sorted(pts)


def enumerate_cases(types, cap=20000, max_abs=2**20, nonneg=False, legacy=False, grid="v1"):
    """types: list of rust int type names. Returns a deterministic list of argument tuples.
    Exhaustive if the full domain fits under `cap`, else a capped cartesian of per-arg grids.
    Values are clamped to +-max_abs so O(n) loop functions don't run for billions of iterations
    (still large enough to overflow 32-bit arithmetic); small types (u8/u16) are unaffected.

    nonneg=True restricts signed types to [0, max_abs] -- the in-domain range most TransCoder
    functions are actually defined on (they index arrays / recurse on the value). Combined with a
    small max_abs this gives the conservative in-domain grid used for the headline bug rate, where a
    divergence can't be blamed on a negative or overflowing input.

    grid="v1" (default) is the grid every shipped result uses. grid="v2" (opt-in) fixes two
    coverage gaps in v1: grid_for thins by index stepping, so the dense 0..255 block crowds out
    most powers of two, and one argument keeps only the first `cap` grid values, so the top of the
    range is cut. v2 uses grid_v2_for per argument (v1 grid + powers of two +-1 + both range ends)
    with NO one-argument truncation (a 1-arg case set can exceed `cap`); several arguments keep the
    _spread selection of `cap` tuples, over the v2 grids. The exhaustive branch is unchanged."""
    if grid not in ("v1", "v2"):
        raise ValueError("grid must be 'v1' or 'v2'")
    if grid == "v2" and legacy:
        raise ValueError("grid='v2' has no legacy (truncated-product) form")
    lo_floor = 0 if nonneg else -max_abs
    ranges = [(max(lo, lo_floor), min(hi, max_abs), s) for (lo, hi, s) in (RANGES[t] for t in types)]
    nargs = len(ranges)
    if nargs == 0:
        return []
    # exhaustive if the whole domain is small enough
    full = 1
    for (lo, hi, _s) in ranges:
        full *= (hi - lo + 1)
        if full > cap:
            break
    if full <= cap:
        axes = [range(lo, hi + 1) for (lo, hi, _s) in ranges]
        return [list(c) for c in product(*axes)]
    # otherwise: per-arg grid, then a capped selection from the cartesian product
    per_arg = max(2, int(round(cap ** (1.0 / nargs))))
    if grid == "v2":
        grids = [grid_v2_for(lo, hi, per_arg) for (lo, hi, _s) in ranges]
        if nargs == 1:
            return [[v] for v in grids[0]]
        return _spread(grids, cap)
    grids = [grid_for(lo, hi, per_arg) for (lo, hi, _s) in ranges]
    if legacy or nargs == 1:
        # legacy (paper v1): the FIRST `cap` tuples of the product in lexicographic order. For one
        # argument this is the first `cap` grid values; for several it truncates the product, so the
        # leading arguments barely vary (3 args: arg0 in {0,1}; 4 args: arg0 = 0).
        cases = []
        for combo in product(*grids):
            cases.append(list(combo))
            if len(cases) >= cap:
                break
        return cases
    return _spread(grids, cap)


def _spread(grids, cap):
    """Evenly spread `cap` tuples over the product of `grids` (several arguments).

    Each argument's index follows an additive-recurrence (Kronecker) low-discrepancy sequence
    with the generalised golden ratio for d dimensions (Roberts' R_d sequence), so every argument
    sweeps its whole grid evenly and argument pairs are spread over the plane. Deterministic: no
    randomness. The all-zero and all-one tuples (the smallest in-domain inputs) are always included.
    """
    d = len(grids)
    g = 2.0
    for _ in range(64):                      # root of x^(d+1) = x + 1
        g = (1 + g) ** (1.0 / (d + 1))
    alphas = [(1.0 / g) ** (k + 1) % 1.0 for k in range(d)]
    cases, seen = [], set()

    def add(t):
        if t not in seen:
            seen.add(t)
            cases.append(list(t))

    for anchor in (0, 1):
        if all(anchor in gr for gr in grids):
            add(tuple(anchor for _ in grids))
    i = 0
    total = 1
    for gr in grids:
        total *= len(gr)
    while len(cases) < min(cap, total):
        add(tuple(gr[int(((0.5 + i * a) % 1.0) * len(gr))] for gr, a in zip(grids, alphas)))
        i += 1
    return cases


def supported(types):
    return bool(types) and all(t in RANGES for t in types)
