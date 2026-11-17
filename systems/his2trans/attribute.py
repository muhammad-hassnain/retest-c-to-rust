"""Fix-and-rerun attribution of the counted His2Trans divergences to defects (mechanisms).

Each defect D below is a hypothesis "mechanism M at file:line of the released crate". We confirm it
by building the crate with a minimal patch that makes ONLY that code behave like the C, re-running
every counted divergent case of the project (UB-clean), and recording which cases now agree with the
C. A case is attributed to D if D's patch alone makes it agree. With ALL of a project's patches
applied we re-run EVERY case of the project: no counted divergence may remain (otherwise a
mechanism is missing), and we report any case the patches newly break.

The patches are diagnostic only (they emulate the C, bugs included); they are not proposed fixes.
"""
import argparse
import collections
import concurrent.futures as cf
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run as R  # noqa: E402
import gen_inputs  # noqa: E402

B = "src/src_buffer.rs"
DEFECTS = {
    "buffer": {
        "H2T-BUF-1": [(B, "    _len: size_t,\n) -> c_int {\n    let len = data.len();",
                       "    _len: size_t,\n) -> c_int {\n    let len = _len as usize;\n"
                       "    let data = &data[..core::cmp::min(len, data.len())];"),
                      (B, "    b.storage[start..start + len].copy_from_slice(data);",
                       "    let len = data.len();\n    b.storage[start..start + len].copy_from_slice(data);")],
        "H2T-BUF-2": [(B, "    if b.storage.is_empty() || needle.is_empty() {\n        return -1;\n    }",
                       "    if needle.is_empty() {\n        return 0;\n    }\n    if b.storage.is_empty() {\n        return -1;\n    }")],
        "H2T-BUF-3": [(B, "    // _len kept for backward compatibility; actual length is data.len().\n    buffer_new_with_slice(data)",
                       "    let p = buffer_new_with_slice(data);\n    unsafe { (*p).len = _len; }\n    p")],
        "H2T-BUF-4": [(B, "    b.storage\n        .copy_within(b.data_offset..b.data_offset + prev + 1, b.data_offset + len);",
                       "    let end = core::cmp::min(b.data_offset + len + 1, b.storage.len() - len);\n"
                       "    b.storage\n        .copy_within(b.data_offset..end, b.data_offset + len);")],
        "H2T-BUF-5": [(B, "    b.storage.resize(n + 1, 0u8);", "    b.storage.resize(n + 1, 0u8);\n    b.storage[n] = 0u8;")],
        "H2T-BUF-6": [(B, "    let start = b.data_offset + prev;", "    let prev = content_len(b);\n    let start = b.data_offset + prev;")],
    },
    "genann": {
        "H2T-GEN-1": [("src/src_genann.rs",
                       "    static STATE: AtomicU64 = AtomicU64::new(1);\n    let old = STATE.load(Ordering::Relaxed);\n"
                       "    let new = old.wrapping_mul(1664525).wrapping_add(1013904223);\n"
                       "    STATE.store(new, Ordering::Relaxed);\n    (new & 0x7FFFFFFF) as f64 / 0x7FFFFFFF as f64",
                       "    unsafe { libc::rand() as f64 / libc::RAND_MAX as f64 }")],
    },
    "rgba": {
        "H2T-RGBA-1": [("src/src_rgba.rs",
                        "            (rgba.r * 255.0) as u8,\n            (rgba.g * 255.0) as u8,\n            (rgba.b * 255.0) as u8\n",
                        "            (rgba.r * 255.0) as i32,\n            (rgba.g * 255.0) as i32,\n            (rgba.b * 255.0) as i32\n"),
                       ("src/src_rgba.rs",
                        "            (rgba.r * 255.0) as u8,\n            (rgba.g * 255.0) as u8,\n            (rgba.b * 255.0) as u8,\n",
                        "            (rgba.r * 255.0) as i32,\n            (rgba.g * 255.0) as i32,\n            (rgba.b * 255.0) as i32,\n")],
    },
}


def run_cases(sut, cases, ref_out, jobs):
    def one(c):
        data = ("\n".join(c["lines"]) + "\n").encode()
        rs = R.run_bin(sut, data, False)
        o = ref_out[c["id"]]
        same = (R.first_diff(o["stdout"], rs["stdout"]) is None and o["rc"] == rs["rc"]
                and o["stderr"] == rs["stderr"] and not rs["timed_out"])
        return c["id"], same
    with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
        return dict(ex.map(one, cases))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--diff", required=True)
    ap.add_argument("--build", default="/b/attr")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    t0 = time.time()
    rows = json.load(open(args.diff))["verdicts"]
    out = {}
    for project, defects in DEFECTS.items():
        allc = gen_inputs.GENERATORS[project]()
        verdict = {r["id"]: r for r in rows if r["project"] == project}
        counted = [c for c in allc if verdict[c["id"]]["verdict"] == "divergent"]
        bins = R.build_c(project, args.build, {"ref": R.VARIANTS["ref"]})
        ref_out = {c["id"]: R.run_bin(bins["ref"], ("\n".join(c["lines"]) + "\n").encode(), False) for c in allc}
        per = {}
        fixed_by = collections.defaultdict(list)
        for did, edits in defects.items():
            sut, log = R.build_rust(project, args.build, mutate=edits, tag=did)
            if sut is None:
                per[did] = {"build": "FAILED", "log": log[-1500:]}
                continue
            res = run_cases(sut, counted, ref_out, args.jobs)
            fixed = sorted(i for i, same in res.items() if same)
            for i in fixed:
                fixed_by[i].append(did)
            per[did] = {"build": "ok", "cases_fixed": len(fixed), "fixed_ids": fixed}
            print(project, did, len(fixed), "of", len(counted), flush=True)
        # cases no single patch explains: find the minimal pairs of patches that make them agree
        rest = [c for c in counted if not fixed_by[c["id"]]]
        pairs = {}
        if rest:
            ids = list(defects)
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    sut, log = R.build_rust(project, args.build, mutate=defects[ids[i]] + defects[ids[j]],
                                            tag=f"{ids[i]}+{ids[j]}")
                    if sut is None:
                        continue
                    res = run_cases(sut, rest, ref_out, args.jobs)
                    for cid, same in res.items():
                        if same:
                            pairs.setdefault(cid, []).append([ids[i], ids[j]])
        sut, log = R.build_rust(project, args.build, mutate=[e for ed in defects.values() for e in ed], tag="allpatches")
        allres = run_cases(sut, allc, ref_out, args.jobs) if sut else {}
        still = [i for i in (c["id"] for c in counted) if not allres.get(i, False)]
        newly = [i for i, same in allres.items() if not same and verdict[i]["verdict"] == "agree"
                 and not verdict[i]["c_ub_evidence"]]
        out[project] = {
            "counted_divergent": len(counted),
            "per_defect": per,
            "attributed_to_exactly_one": sum(1 for c in counted if len(fixed_by[c["id"]]) == 1),
            "attributed_to_several": {i: v for i, v in fixed_by.items() if len(v) > 1},
            "fixed_by_no_single_patch": [c["id"] for c in counted if not fixed_by[c["id"]]],
            "fixed_by_pair_of_patches": pairs,
            "all_patches": {"build": "ok" if sut else "FAILED", "counted_divergences_remaining": still,
                            "ub_clean_agreeing_cases_newly_diverging": newly},
        }
    out = {"provenance": R.ou.provenance(__file__, {"wall_seconds": round(time.time() - t0), "diff_input": args.diff,
                                                     "patches": {p: {d: [list(e) for e in eds] for d, eds in ds.items()}
                                                                 for p, ds in DEFECTS.items()}}),
           "attribution": out}
    R.ou.dump(args.out, out)


if __name__ == "__main__":
    main()
