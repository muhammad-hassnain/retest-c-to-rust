"""Print counted (UB-clean) divergences per translation, grouped by a coarse signature, for the
manual defect clustering step. Pure JSON processing (runs anywhere).

Usage: python3 -I show_divergences.py <difftest.json> <ub_filter.json> [battery/case] [--all]
"""
import base64
import json
import sys


def b(s):
    return base64.b64decode(s)


def sig(dv):
    c, r = dv["c"], dv["rust"]
    co, ro = b(c["stdout_b64"]).splitlines(), b(r["stdout_b64"]).splitlines()
    first = None
    for i in range(max(len(co), len(ro))):
        x = co[i] if i < len(co) else None
        y = ro[i] if i < len(ro) else None
        if x != y:
            first = (i, x, y)
            break
    return (dv["class"], tuple(dv["channels"]), c["rc"], r["rc"],
            (first[1][:60] if first and first[1] else None, first[2][:60] if first and first[2] else None))


def main():
    dt = json.load(open(sys.argv[1]))
    ub = json.load(open(sys.argv[2]))
    only = sys.argv[3] if len(sys.argv) > 3 and not sys.argv[3].startswith("--") else None
    show_all = "--all" in sys.argv
    verd = {}
    for p in ub["programs"]:
        for v in p["verdicts"]:
            verd[(p["battery"], p["case"], v["id"])] = v
    for p in dt["programs"]:
        key = f"{p['battery']}/{p['case']}"
        if only and key != only:
            continue
        rows = []
        for dv in p["divergent"]:
            v = verd.get((p["battery"], p["case"], dv["input"]["id"]))
            if v is None:
                continue
            if v["counted"] or show_all:
                rows.append((dv, v))
        if not rows:
            continue
        groups = {}
        for dv, v in rows:
            groups.setdefault(sig(dv), []).append((dv, v))
        print(f"=== {key}: counted={sum(1 for _, v in rows if v['counted'])} / divergent={len(p['divergent'])}")
        for s, items in groups.items():
            dv, v = items[0]
            print(f"  [{len(items)}] class={s[0]} ch={s[1]} rc C={s[2]} R={s[3]} counted={v['counted']} {v['ub_reasons'][:2]}")
            print(f"      argv={dv['input']['argv'][:6]} stdin={b(dv['input']['stdin_b64'])[:80]!r}")
            print(f"      C : out={b(dv['c']['stdout_b64'])[:220]!r} err={b(dv['c']['stderr_b64'])[:120]!r}")
            print(f"      R : out={b(dv['rust']['stdout_b64'])[:220]!r} err={b(dv['rust']['stderr_b64'])[:160]!r}")


if __name__ == "__main__":
    main()
