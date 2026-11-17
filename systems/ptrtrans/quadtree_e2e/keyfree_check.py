"""key_free channel, checked insert by insert (supports the classification of the key_free
divergence in results/ptrtrans_summary.json).

For every `keyfree` case of gen_cases.py (tree->key_free set to a logging callback), run the C (gcc
-O3, Makefile flags) and the shipped Rust (debug) once, with full outputs, and for each
quadtree_insert compare
  C:    the keys passed to tree->key_free during that insert ("KF k" lines before "I r L")
  Rust: the keys released through Drop during that insert ("# rust_key_drops_in_this_command")
and record whether the Rust ever calls key_free (it should print "KF" lines if it did).

  python3 systems/ptrtrans/quadtree_e2e/keyfree_check.py --art /art --build /oub/kf
"""
import argparse
import collections
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(REPO, "harness"))
sys.path.insert(0, HERE)
import oracle_ub_common as ou  # noqa: E402
import run as qrun  # noqa: E402
from gen_cases import corpus  # noqa: E402


def per_insert_c(out):
    res, pending = [], []
    for l in out.splitlines():
        if l.startswith("KF "):
            pending.append(int(l.split()[1]))
        elif l.startswith("I "):
            res.append({"ret": l, "key_free": pending})
            pending = []
    return res


def per_insert_rust(out):
    res = []
    for l in out.splitlines():
        if l.startswith("I "):
            res.append({"ret": l, "key_free": [], "dropped": []})
        elif l.startswith("KF ") and res:
            res[-1]["key_free"].append(int(l.split()[1]))
        elif l.startswith("# rust_key_drops_in_this_command") and res:
            res[-1]["dropped"] = json.loads(l.split(" ", 2)[2])
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--art", required=True)
    ap.add_argument("--build", default="/tmp/qt_kf")
    ap.add_argument("--toolchain", default="1.86.0")
    ap.add_argument("--out", default=os.path.join(REPO, "results", "ptrtrans_quadtree_keyfree.json"))
    args = ap.parse_args()
    t0 = time.time()
    os.makedirs(args.build, exist_ok=True)
    cbins, _ = qrun.build_c(args.build, args.art, only=("ref",))
    rs = qrun.build_rust(args.build, args.art, args.toolchain)
    sut = rs["sut_debug"]["bin"]
    rows = []
    for c in corpus():
        if c["family"] != "keyfree":
            continue
        co, ce, crc, cto = qrun.run_plain([cbins["ref"]], c["script"].encode())
        ro, re_, rrc, rto = qrun.run_plain([sut], c["script"].encode())
        ci, ri = per_insert_c(co.decode()), per_insert_rust(ro.decode())
        ins = []
        for a, b in zip(ci, ri):
            nonnull = [k for k in a["key_free"] if k >= 0]
            ins.append({"c_ret": a["ret"], "rust_ret": b["ret"], "c_key_free": a["key_free"],
                        "rust_key_free": b["key_free"], "rust_dropped": b["dropped"],
                        "c_nonnull_key_free_equals_rust_drops": nonnull == b["dropped"]})
        rows.append({"id": c["id"], "c_rc": crc, "rust_rc": rrc, "n_inserts": len(ci),
                     "same_insert_returns": [a["ret"] for a in ci] == [b["ret"] for b in ri],
                     "c_key_free_calls": sum(len(a["key_free"]) for a in ci),
                     "c_key_free_calls_null": sum(1 for a in ci for k in a["key_free"] if k < 0),
                     "rust_key_free_calls": sum(len(b["key_free"]) for b in ri),
                     "rust_drops_during_inserts": sum(len(b["dropped"]) for b in ri),
                     "all_inserts_c_nonnull_key_free_equals_rust_drops": all(
                         x["c_nonnull_key_free_equals_rust_drops"] for x in ins),
                     "inserts": ins})
    summ = {
        "cases": len(rows),
        "cases_where_c_calls_key_free": sum(1 for r in rows if r["c_key_free_calls"]),
        "c_key_free_calls_total": sum(r["c_key_free_calls"] for r in rows),
        "c_key_free_calls_with_NULL_key": sum(r["c_key_free_calls_null"] for r in rows),
        "rust_key_free_calls_total": sum(r["rust_key_free_calls"] for r in rows),
        "insert_returns_identical_in_all_cases": all(r["same_insert_returns"] for r in rows),
        # inserts where the C calls key_free on a non-NULL key (a duplicate replaced its key)
        "inserts_with_c_nonnull_key_free": sum(1 for r in rows for x in r["inserts"]
                                               if any(k >= 0 for k in x["c_key_free"])),
        "of_which_rust_drops_exactly_those_keys_in_the_same_insert": sum(
            1 for r in rows for x in r["inserts"]
            if any(k >= 0 for k in x["c_key_free"]) and x["c_nonnull_key_free_equals_rust_drops"]),
        # inserts where the Rust drops a key and the C calls no key_free
        "inserts_rust_drops_key_c_calls_no_key_free": sum(
            1 for r in rows for x in r["inserts"] if x["rust_dropped"] and not x["c_key_free"]),
        "of_which_insert_returned_0_without_storing": sum(
            1 for r in rows for x in r["inserts"] if x["rust_dropped"] and not x["c_key_free"]
            and x["c_ret"].startswith("I 0 ")),
        "note_on_rejected_inserts": ("when quadtree_insert rejects a point (returns 0, nothing stored) the C "
                                     "leaves the key with the caller and calls no key_free; the translation "
                                     "takes the key by value (Option<Box<dyn Any>>) and drops it. No C-visible "
                                     "channel shows this; it is the Rust ownership signature, not a key_free call."),
        "by_seq": dict(collections.Counter(r["id"].split("/")[1] for r in rows if r["c_key_free_calls"])),
    }
    out = {"provenance": ou.provenance(__file__, {"toolchain": args.toolchain, "rustc": rs["rustc"].splitlines()[0],
                                                  "wall_seconds": round(time.time() - t0)}),
           "summary": summ, "cases": rows}
    ou.dump(args.out, out)
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()
