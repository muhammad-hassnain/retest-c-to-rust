"""Build results/his2trans_summary.json (and results/his2trans_heldout.json) from the measured files:

  results/his2trans_diff.json         main differential run (run.py)
  results/his2trans_attribution.json  fix-and-rerun attribution of counted divergences (attribute.py)
  results/his2trans_power.json        injected fault per harness + mutation analysis (power.py)
  results/his2trans_own_check.json    the system's own RQ2 check re-run in our container (own_check.sh)
  results/his2trans_xcheck.json       toolchain/profile cross-check (xcheck.py)

Defect descriptions below are written by hand from the source; every count is computed here from
the measured files. Runs on the host (reads JSON only; no third-party code is executed).
"""
import collections
import datetime
import json
import os
import platform
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
RES = os.path.join(REPO, "results")
sys.path.insert(0, HERE)
import gen_inputs  # noqa: E402

CRATE = "data/paper_artifacts/deepseek_v4_pro_oss8_0613_rq2_100pct_minimal/projects"
CSRC = "data/rq2/deepseek/c_source/c_source"
OWN_TESTS = "data/paper_artifacts/deepseek_v4_pro_oss8_0613_rq2_100pct_minimal/tests/source_rq2_tests/<project>/c2r/test.rs"

DEFECTS = {
    "H2T-BUF-1": {
        "example_lines": ["newsz 0", "appn 68656c6c6f 1"],
        "translation": "buffer", "function": "buffer_append_n",
        "mechanism": "The translation ignores the length argument (`_len`) and appends the whole slice; the C appends at "
                     "most `len` bytes (strncat) and sizes the buffer from `len`.",
        "rust_location": f"{CRATE}/buffer/src/src_buffer.rs:154-173 (line 159: `let len = data.len();`, `_len` unused)",
        "c_location": f"{CSRC}/buffer/src/buffer.c:195-210 (strncat(self->data, str, len) at 201/208)",
    },
    "H2T-BUF-2": {
        "translation": "buffer", "function": "buffer_indexof",
        "mechanism": "Empty needle: the translation returns -1; the C returns 0 (strstr(data, \"\") returns data).",
        "rust_location": f"{CRATE}/buffer/src/src_buffer.rs:238-241 (`if b.storage.is_empty() || needle.is_empty() {{ return -1; }}`)",
        "c_location": f"{CSRC}/buffer/src/buffer.c:276-279",
    },
    "H2T-BUF-3": {
        "example_lines": ["newstrlen 68656c6c6f 0"],
        "translation": "buffer", "function": "buffer_new_with_string_length",
        "mechanism": "The translation ignores the length argument (`_len`, comment: 'kept for backward compatibility') and "
                     "sets the buffer size to the string length; the C sets self->len = len, so buffer_size() differs "
                     "whenever len != strlen(str).",
        "rust_location": f"{CRATE}/buffer/src/src_buffer.rs:65-68",
        "c_location": f"{CSRC}/buffer/src/buffer.c:61-67 (self->len = len at 64)",
    },
    "H2T-BUF-4": {
        "example_lines": ["newstr 68656c6c6f", "pre 30"],
        "translation": "buffer", "function": "buffer_prepend",
        "mechanism": "The translation shifts the existing string by prev+1 bytes; the C memmove shifts len+1 bytes (len = "
                     "length of the prepended string). When the existing string is longer than the prepended one the C "
                     "keeps only its first len+1 bytes' worth (the rest is overwritten). The C behaviour is an upstream "
                     "bug that the translation silently corrects; it is still a divergence from the C on a UB-clean input.",
        "rust_location": f"{CRATE}/buffer/src/src_buffer.rs:187-189 (copy_within(.. prev + 1 ..))",
        "c_location": f"{CSRC}/buffer/src/buffer.c:232 (memmove(self->data + len, self->data, len + 1))",
        "c_behaviour_is_upstream_bug": True,
        "note_on_crash": "After buffer_trim_left (data_offset > 0) a prepend that needs no resize makes the "
                         "translation's copy_within (prev+1 bytes from data_offset) run past the end of the storage "
                         "Vec: panic 'dest is out of bounds' at src_buffer.rs:189. The C stays in bounds only because "
                         "its memmove moves len+1 bytes. Counted as a crash, attributed to this mechanism (the patch "
                         "fixes it).",
    },
    "H2T-BUF-5": {
        "example_lines": ["newcopy 616263646566", "resize 0"],
        "translation": "buffer", "function": "buffer_resize",
        "mechanism": "Shrinking: the translation truncates the storage Vec to n+1 bytes but does not write the terminating "
                     "NUL at index n; the C writes alloc[n] = '\\0'. After resize(b, 0) (n rounds to 0) or any resize "
                     "below the current length, buffer_length/buffer_string see one byte too many.",
        "rust_location": f"{CRATE}/buffer/src/src_buffer.rs:100-107 (line 102: `b.storage.resize(n + 1, 0u8);`)",
        "c_location": f"{CSRC}/buffer/src/buffer.c:134-141 (self->alloc[n] = '\\0' at 139)",
    },
    "H2T-BUF-6": {
        "example_lines": ["newcopy 092078200a", "tl", "app 68656c6c6f"],
        "translation": "buffer", "function": "buffer_append_n (via buffer_append)",
        "mechanism": "After buffer_trim_left (data_offset > 0), an append that triggers buffer_resize: resize resets "
                     "data_offset to 0 (mirroring the C's data = alloc), but the translation writes at the stale "
                     "position data_offset + prev computed before the resize, overwriting the restored leading "
                     "characters; the C strncat appends at the end of the string seen from the reset base.",
        "rust_location": f"{CRATE}/buffer/src/src_buffer.rs:160,166-171 (prev computed before buffer_resize; "
                         "data_offset reset at 106)",
        "c_location": f"{CSRC}/buffer/src/buffer.c:206-208",
    },
    "H2T-GEN-1": {
        "translation": "genann", "function": "genann_randomize (and genann_init, which calls it)",
        "mechanism": "The translation replaces the C library rand() (GENANN_RANDOM() = rand()/RAND_MAX) with a private "
                     "LCG (state 1, a=1664525, c=1013904223). Every initial weight differs from the C's, and srand() no "
                     "longer controls them; genann_run on a freshly initialised network therefore returns different "
                     "outputs.",
        "rust_location": f"{CRATE}/genann/src/src_genann.rs:161-167 (genann_rand_double)",
        "c_location": f"{CSRC}/genann/src/genann.c:169-176 and genann.h:39",
    },
    "H2T-RGBA-1": {
        "translation": "rgba", "function": "rgba_to_string",
        "mechanism": "Channel values are converted with a saturating `as u8` cast; the C uses (int) and prints with "
                     "%.2x / %d. For a channel outside [0, 1] (e.g. r = 1.5 -> C '#17e7f3f', Rust '#ff7f3f'; r = -0.5 -> C "
                     "'ffffff81', Rust '00') the strings differ. Only inputs outside the nominal [0,1] range expose it.",
        "rust_location": f"{CRATE}/rgba/src/src_rgba.rs:195-213 (`as u8` at 199-201 and 206-208)",
        "c_location": f"{CSRC}/rgba/src/rgba.c:252-264",
        "exposed_only_outside_nominal_domain": True,
    },
}

# Confirmed here but not counted by the defect ledger (scripts/build_defect_ledger.py), which applies the paper's
# rules to every system: (ii) a divergence only on inputs that break a precondition the original states
# (PRECONDITION_STATED there) and (iii) a divergence only in a value the C standard leaves unspecified
# (UNSPECIFIED_VALUE there). translations_with_defect counts the crates with a defect the ledger counts; the
# ledger build checks that both agree.
NOT_COUNTED = {
    "H2T-BUF-3": "rule (ii): every exposing input passes len != strlen(str), which the function's name and signature "
                 "rule out (buffer_new_with_string_length(char *str, size_t len))",
    "H2T-GEN-1": "rule (iii): every exposing output is a weight drawn from the C library's rand(), whose sequence ISO C "
                 "7.22.2.1 leaves unspecified",
}


def load(name):
    p = os.path.join(RES, name)
    return json.load(open(p)) if os.path.exists(p) else None


def dec_line(line):
    if line is None:
        return None
    out = []
    for t in line.split(" "):
        if t.startswith("s:") and t != "s:~":
            try:
                out.append(json.dumps(bytes.fromhex(t[2:]).decode("latin1")))
                continue
            except ValueError:
                pass
        if t.startswith("d:") and len(t) == 18:
            out.append(repr(struct.unpack(">d", bytes.fromhex(t[2:]))[0]))
            continue
        out.append(t)
    return " ".join(out)


def git_rev():
    """HEAD of the repository this runs in; in the artifact repository (one amended commit) a note instead."""
    try:
        run = lambda *a: subprocess.run(["git", "-C", REPO, *a], stdout=subprocess.PIPE, text=True).stdout.strip()
        return "artifact repository (one amended commit, so no stable hash)" if run("rev-list", "--count", "HEAD") == "1" else run("rev-parse", "HEAD")
    except OSError:
        return "?"


def main():
    diff = load("his2trans_diff.json")
    attr = load("his2trans_attribution.json")
    power = load("his2trans_power.json")
    extra = load("his2trans_power_extra.json")
    if power and extra:
        power.setdefault("mutation", {}).update(extra.get("mutation") or {})
        power["provenance_extra"] = extra.get("provenance")
    own = load("his2trans_own_check.json")
    xchk = load("his2trans_xcheck.json")
    xexp = load("his2trans_xcheck_explain.json")
    rows = diff["verdicts"]
    byid = {r["id"]: r for r in rows}
    cases = {c["id"]: c for c in gen_inputs.all_cases()}
    projects = list(diff["per_project"].keys())

    # ---------------- held-out: our inputs vs the system's own (RQ2 test) inputs
    held = {}
    for p in projects:
        pc = [c for c in cases.values() if c["project"] == p]
        own_scripts = {tuple(c["lines"]) for c in pc if c["cls"] == "own"}
        own_calls = {ln for c in pc if c["cls"] == "own" for ln in c["lines"]}
        ours = [c for c in pc if c["cls"] != "own"]
        our_calls = {ln for c in ours for ln in c["lines"]}
        held[p] = {
            "own_inputs_recorded": "yes" if own_scripts else "yes (the only test calls the translated empty main(); "
                                                             "no library function is called)",
            "own_cases": sum(1 for c in pc if c["cls"] == "own"),
            "our_cases": len(ours),
            "our_cases_identical_to_an_own_case": sum(1 for c in ours if tuple(c["lines"]) in own_scripts),
            "distinct_calls_ours": len(our_calls), "distinct_calls_own": len(own_calls),
            "distinct_calls_shared": len(our_calls & own_calls),
            "own_cases_verdicts": dict(collections.Counter(byid[c["id"]]["verdict"] for c in pc if c["cls"] == "own")),
        }

    # ---------------- defects: exposure from the attribution run
    defects = []
    att = attr["attribution"] if attr else {}
    for did, meta in DEFECTS.items():
        p = meta["translation"]
        a = att.get(p, {}).get("per_defect", {}).get(did, {})
        ids = list(a.get("fixed_ids", []))
        # cases that need two patches: count them for a defect if it is in every minimal pair that fixes them
        two = []
        for cid, prs in (att.get(p, {}).get("fixed_by_pair_of_patches") or {}).items():
            if prs and all(did in pr for pr in prs):
                two.append(cid)
        ids += two
        ex = [byid[i] for i in ids]
        by_cls = collections.Counter(r["cls"] for r in ex)
        by_sev = collections.Counter(r["severity"] for r in ex)
        own_exposing = [r["id"] for r in ex if r["cls"] == "own"]
        pref = [r for r in ex if r["lines"] == meta.get("example_lines")]
        sample = pref or sorted(ex, key=lambda r: (len(r["lines"]), sum(len(x) for x in r["lines"])))[:1]
        example = None
        if sample:
            r = sample[0]
            example = {"case": r["id"], "input_script": [dec_line(x) for x in r["lines"]],
                       "C": dec_line(r["c_line"]), "Rust": dec_line(r["rust_line"]),
                       "note": "driver protocol: op name, then results; for buffer ' | size length data'"}
        d = dict(meta)
        d["_ids"] = set(ids)
        d.update({
            "id": did,
            "confirmed_by": "fix-and-rerun: the patch in results/his2trans_attribution.json (provenance.patches) "
                            "makes exactly these cases agree with the C",
            "exposure": {"cases": len(ex), "cases_fixed_by_this_patch_alone": len(ids) - len(two),
                         "cases_needing_this_and_one_other_patch": two,
                         "by_input_class": dict(by_cls), "by_severity": dict(by_sev)},
            "severity": "S1" if by_sev.get("S1") else (sorted(by_sev)[0] if by_sev else None),
            "example": example,
            "acceptance_status": "accepted",
            "acceptance_evidence": (
                f"The system's own check (RQ2 Rust tests, {OWN_TESTS.replace('<project>', p)}) passes the {p} crate: "
                f"shipped verdict data/paper_artifacts/deepseek_v4_pro_oss8_0613_rq2_100pct_minimal/results/oss8_rq2.json "
                f"({p}: all tests passed), and our re-run of the check in Docker as the artifact documents it "
                f"(results/his2trans_own_check.json) passes it too."),
            "heldout": {
                "own_inputs_expose_it": bool(own_exposing),
                "own_cases_exposing": own_exposing,
                "diverges_on_input_outside_own_set": any(r["cls"] != "own" and tuple(cases[r["id"]]["lines"]) not in
                                                         {tuple(c["lines"]) for c in cases.values()
                                                          if c["project"] == p and c["cls"] == "own"} for r in ex),
            },
        })
        if did == "H2T-GEN-1":
            pr = [r for r in rows if r["project"] == "genann" and r["cls"] == "probe"]
            d["srand_probe"] = {
                "cases": [r["lines"] for r in pr],
                "distinct_C_weight_vectors": len({r["c_line"] for r in pr}),
                "distinct_Rust_weight_vectors": len({r["rust_line"] for r in pr}),
                "reading": "srand(s) before genann_init: the C weights change with s, the Rust weights do not"}
        defects.append(d)

    # ---------------- per-translation table
    trans = {}
    for p in projects:
        s = diff["per_project"][p]["summary"]
        dl = [d["id"] for d in defects if d["translation"] == p and d["exposure"]["cases"] > 0]
        fc = collections.defaultdict(lambda: [0, 0])
        for r in rows:
            if r["project"] == p and r["cls"] != "own":
                fc[r["func"]][0] += 1
                fc[r["func"]][1] += 0 if r["c_ub_evidence"] else 1
        trans[p] = {"cases": s["cases"], "agree": s["agree"], "divergent_counted": s["divergent_counted"],
                    "cases_and_ub_clean_cases_by_function": {k: {"cases": v[0], "ub_clean": v[1]} for k, v in sorted(fc.items())},
                    "functions_with_no_ub_clean_case": sorted(k for k, v in fc.items() if v[1] == 0),
                    "original_C_UB_excluded": s["original_C_UB"], "cases_with_any_C_UB_evidence": s["c_ub_any_case"],
                    "defects": dl, "acceptance_status": "accepted",
                    "own_check_rerun": (own or {}).get("per_project", {}).get(p)}
    crashes = [r for r in rows if r["verdict"] == "divergent" and r["severity"] == "crash"]
    s2 = [r for r in rows if r["verdict"] == "divergent" and r["severity"] == "S2"]
    s3 = [r for r in rows if r["verdict"] == "divergent" and r["severity"] == "S3"]
    ub_rows = [r for r in rows if r["verdict"] == "original_C_UB"]
    ub_panics = [r for r in ub_rows if r["rust_panic"]]
    summary = {
        "system": "His2Trans",
        "scope": "OSS8 archive (paper Table RQ2, DeepSeek-V4-Pro): 8 crates. OHOS10 is out of scope (its C is not "
                 "shipped for 5 of 10 modules and its gtest bridge needs the full OpenHarmony tree).",
        "translations_tested": len(projects),
        "translations_with_defect": len({d["translation"] for d in defects if d["exposure"]["cases"] > 0 and d["id"] not in NOT_COUNTED}),
        "translations_with_defect_incl_not_counted": len({d["translation"] for d in defects if d["exposure"]["cases"] > 0}),
        "defects_not_counted": {k: v for k, v in NOT_COUNTED.items() if any(d["id"] == k for d in defects)},
        "defects": defects,
        "crashes": {"counted": len(crashes), "ids": [r["id"] for r in crashes],
                    "detail": [{"id": r["id"], "input_script": [dec_line(x) for x in r["lines"]],
                                "C": dec_line(r["c_line"]), "rust_stderr": r.get("rust_stderr", "")[-200:],
                                "attributed_to": [d["id"] for d in defects if r["id"] in d["_ids"]]} for r in crashes],
                    "rust_panics_on_C_UB_inputs_not_counted": len(ub_panics),
                    "examples_not_counted": [{"id": r["id"], "rust_stderr": r.get("rust_stderr", "")[-200:],
                                              "c_ub": r["c_ub_evidence"][:1]} for r in ub_panics[:5]]},
        "S2": {"counted": len(s2), "ids": [r["id"] for r in s2]},
        "S3": {"counted_in_main_run": len(s3),
               "count": len(s3),
               "toolchain_profile_crosscheck": xchk and {
                   "base": xchk["base"],
                   "comparisons": [{k: v for k, v in c.items() if k != "differing"} | {
                       "differing_ids": [d["id"] for d in c["differing"]]} for c in xchk["comparisons"]],
                   "explained": xexp and [{"id": r["id"], "pairs": r["pairs"], "nan_tokens": r["nan_tokens"]}
                                          for r in xexp["cases"]],
                   "reading": "No S3. rustc 1.99.1 (release) differs from 1.86.0 only on the two buffer cases where "
                              "both toolchains panic at the same line (1.99.1 adds the thread id to the panic message): "
                              "the counted crash and one UB-excluded case. The debug profile differs "
                              "from release only on two genann_train cases whose outputs are all NaN: the raw NaN sign "
                              "bits differ, the tolerant comparison (NaN == NaN) finds them equal to each other and to "
                              "the C (results/his2trans_xcheck_explain.json)."}},
        "ub_excluded": {"divergent_cases_excluded_as_original_C_UB": len(ub_rows),
                        "by_translation": dict(collections.Counter(r["project"] for r in ub_rows)),
                        "cases_where_each_check_fired": dict(collections.Counter(
                            e.split(":")[0] for r in ub_rows for e in sorted({x.split(":")[0] for x in r["c_ub_evidence"]}))),
                        "note": "ref = the gcc -O2 C build itself was killed by a signal or timed out on that input; o0/o1 = output differs "
                                "from -O2; uchar/schar = output depends on char signedness"},
        "per_translation": trans,
        "heldout": {"status": "measured (the own inputs are the RQ2 test call sequences shipped in "
                              + OWN_TESTS + ")", "per_translation": held},
        "harness_power": power and {"inject": power.get("inject"),
                                    "mutation": {p: {k: v for k, v in m.items() if k != "mutants"}
                                                 for p, m in (power.get("mutation") or {}).items()}},
        "attribution_check": {p: {k: v for k, v in a.items() if k != "per_defect"} for p, a in att.items()},
    }
    summary["provenance"] = {
        "script": "systems/his2trans/summarize.py", "command": "python3 systems/his2trans/summarize.py",
        "git_rev": git_rev(), "host": platform.system(), "arch": platform.machine(),
        "date": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "inputs": {"diff": diff["provenance"], "attribution": attr and attr["provenance"],
                   "power": power and power.get("provenance"), "own_check": own and own.get("provenance"),
                   "xcheck": xchk and xchk.get("provenance")},
    }
    for d in defects:
        d.pop("_ids", None)
    with open(os.path.join(RES, "his2trans_summary.json"), "w") as f:
        json.dump(summary, f, indent=1)
        f.write("\n")
    print(json.dumps({k: summary[k] for k in ("translations_tested", "translations_with_defect")}, indent=1))
    for d in defects:
        print(d["id"], d["exposure"], d["heldout"]["own_inputs_expose_it"])


if __name__ == "__main__":
    main()
