"""Harness power for the His2Trans function-level harnesses.

(1) Injected fault per harness: one hand-picked single-token fault in the translated Rust of each
    project (INJECT below). The harness must turn red: at least one case that agreed and was
    UB-clean in the main run must diverge.
(2) Mutation analysis on agreeing translations (--auto): every relational / arithmetic operator and
    every integer literal (+1) in the translated module(s), one at a time, deterministic order,
    thinned evenly to --cap mutants. A mutant is killed if any UB-clean agreeing case diverges
    (stdout/exit/stderr, same tolerant comparison as run.py) or the mutant hangs; mutants that do
    not compile are "stillborn" and excluded from the kill rate (reported).

Only the C reference build (gcc -O2) is needed: the UB verdicts come from the main run.
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run as R  # noqa: E402
import gen_inputs  # noqa: E402

# (relpath in crate, exact old text, new text, what it breaks)
# A first genann choice (genann_act_sigmoid clamp `a > 45.0` -> `a > 44.0`) survived: it is an
# equivalent mutant in double precision (1/(1+exp(-a)) == 1.0 for every a >= 37), so the fault
# below replaced it; the survivor is recorded in results/his2trans_power.json (inject_equivalent_note).
INJECT = {
    "qsort": ("src/src_qsort.rs", "if arr[j] <= pivot {", "if arr[j] < pivot {", "partition: <= -> <"),
    "ht": ("src/src_ht.rs", "hash ^= b as u64;", "hash ^= (b as u64) + 1;", "hash_key: xor input byte + 1"),
    "rgba": ("src/src_rgba.rs", "b'a'..=b'f' => (c - b'a' + 10) as u32,", "b'a'..=b'f' => (c - b'a' + 11) as u32,",
             "h(): hex digit a-f off by one"),
    "genann": ("src/src_genann.rs", "(target - o_val) * o_val * (1.0 - o_val)", "(target - o_val) * o_val * (1.0 + o_val)",
               "genann_train: output-delta derivative sign"),
    "buffer": ("src/src_buffer.rs", "fn is_trim_char(c: u8) -> bool {\n    matches!(c, b' ' | b'\\t'",
               "fn is_trim_char(c: u8) -> bool {\n    matches!(c, b'\\t'", "trim: space no longer trimmed"),
    "quadtree": ("src/src_quadtree.rs", "nw.x < it.x && nw.y > it.y", "nw.x <= it.x && nw.y > it.y",
                 "node_contains_: strict -> non-strict on x"),
    "urlparser": ("src/src_url.rs", 'protocol == "ssh" || protocol == "git"', 'protocol == "ssh"',
                  "url_is_ssh: git no longer ssh"),
    "avl": ("src/src_avl.rs", "let iResult = pfComparer(pstKey, key_ptr);\n\n        if iResult > 0 {",
            "let iResult = pfComparer(pstKey, key_ptr);\n\n        if iResult >= 0 {", "VOS_AVL3_Find: > -> >="),
}

MODULES = {"qsort": ["src/src_qsort.rs"], "ht": ["src/src_ht.rs"], "rgba": ["src/src_rgba.rs"],
           "genann": ["src/src_genann.rs"], "buffer": ["src/src_buffer.rs"],
           "quadtree": ["src/src_quadtree.rs", "src/src_node.rs", "src/src_bounds.rs"],
           "urlparser": ["src/src_url.rs"], "avl": ["src/src_avl.rs"]}

REL = [("<=", "<"), (">=", ">"), ("<", "<="), (">", ">="), ("==", "!="), ("!=", "==")]
ARI = [("+", "-"), ("-", "+"), ("*", "/"), ("/", "*")]
OP_RE = re.compile(r"(?<![<>=!&|\-+*/%^.:])(<=|>=|==|!=|<|>|\+|-|\*|/)(?![=<>&|\-+*/:.])")
INT_RE = re.compile(r"(?<![\w.])(\d+)(?:usize|u32|u64|i32|i64|u8|u16|i16|isize)?(?![\w.])")


def candidates(src):
    """Deterministic list of (offset, old, new, kind) single-token mutations in code lines (comments,
    attributes, `use`, string/char literals, generics and arrows skipped)."""
    out = []
    pos = 0
    for line in src.split("\n"):
        code = line.split("//")[0]
        st = code.strip()
        skip = (not st or st.startswith(("#", "use ", "pub use", "mod ", "pub mod", "//", "*", "extern")) or
                '"' in code or "'" in code)
        if not skip:
            for m in OP_RE.finditer(code):
                tok = m.group(1)
                before, after = code[:m.start()], code[m.end():]
                if tok in ("<", ">") and (re.search(r"[A-Za-z_]\s*$", before) and re.match(r"\s*[A-Za-z_&'(\[]", after)
                                          and (">" in after or "<" in before or "::" in before)):
                    continue                    # generics such as Option<Box<T>>
                if tok in ("-",) and re.match(r"\s*>", after):
                    continue
                if tok == "*" and (re.search(r"[(=,\s]$", before) or before.strip() == ""):
                    continue                    # deref / raw pointer type
                if tok == "-" and (re.search(r"[(=,\[\s]$", before) or before.strip() == ""):
                    continue                    # unary minus
                if tok == "&":
                    continue
                for a, b in REL + ARI:
                    if tok == a:
                        out.append((pos + m.start(), a, b, "relational" if (a, b) in REL else "arithmetic"))
            for m in INT_RE.finditer(code):
                v = m.group(1)
                if len(v) > 12:
                    continue
                out.append((pos + m.start(1), v, str(int(v) + 1), "constant+1"))
        pos += len(line) + 1
    return out


def apply(src, off, old, new):
    assert src[off:off + len(old)] == old
    return src[:off] + new + src[off + len(old):]


def clean_agree_cases(project, diff_rows):
    ok = {r["id"] for r in diff_rows if r["project"] == project and r["verdict"] == "agree" and not r["c_ub_evidence"]}
    return [c for c in gen_inputs.GENERATORS[project]() if c["id"] in ok]


def run_mutant(project, cases, ref_bin, ref_out, bdir, mutate, tag, jobs):
    sut, log = R.build_rust(project, bdir, mutate=mutate, tag=tag)
    if sut is None:
        return {"status": "stillborn", "log_tail": log[-600:]}

    def one(c):
        data = ("\n".join(c["lines"]) + "\n").encode()
        rs = R.run_bin(sut, data, False)
        o = ref_out[c["id"]]
        if rs["timed_out"]:
            return ("hang", c["id"])
        if R.first_diff(o["stdout"], rs["stdout"]) is not None or o["rc"] != rs["rc"] or o["stderr"] != rs["stderr"]:
            return ("killed", c["id"])
        return None
    with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
        res = [r for r in ex.map(one, cases) if r]
    if res:
        return {"status": "killed" if res[0][0] == "killed" else "killed(hang)", "killing_cases": len(res),
                "first_killing_case": res[0][1]}
    return {"status": "survived"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--projects", default="qsort,ht,rgba,genann,buffer,quadtree,urlparser,avl")
    ap.add_argument("--diff", action="append", required=True, help="results/his2trans_diff*.json (main run)")
    ap.add_argument("--auto", default="", help="projects for the mutation analysis (comma list)")
    ap.add_argument("--cap", type=int, default=40)
    ap.add_argument("--build", default="/b/power")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    t0 = time.time()
    rows = []
    for p in args.diff:
        rows += json.load(open(p))["verdicts"]
    out = {"inject": {}, "mutation": {}}
    projects = [p for p in args.projects.split(",") if p]
    auto = [p for p in args.auto.split(",") if p]
    for project in sorted(set(projects) | set(auto)):
        cases = clean_agree_cases(project, rows)
        if not cases:
            out["inject"][project] = {"status": "no UB-clean agreeing cases"}
            continue
        bins = R.build_c(project, args.build, {"ref": R.VARIANTS["ref"]})
        ref_out = {}
        for c in cases:
            ref_out[c["id"]] = R.run_bin(bins["ref"], ("\n".join(c["lines"]) + "\n").encode(), False)
        if project in projects:
            rel, old, new, what = INJECT[project]
            r = run_mutant(project, cases, bins["ref"], ref_out, args.build, (rel, old, new), "inject", args.jobs)
            r.update({"fault": what, "file": rel, "cases_used": len(cases)})
            out["inject"][project] = r
            print("inject", project, r, flush=True)
        if project in auto:
            muts = []
            crate = os.path.join(R.ART, R.OSS8, "projects", project)
            for rel in MODULES[project]:
                src = open(os.path.join(crate, rel)).read()
                for off, a, b, kind in candidates(src):
                    muts.append((rel, off, a, b, kind))
            sel = muts if len(muts) <= args.cap else [muts[int(i * len(muts) / args.cap)] for i in range(args.cap)]
            res = []
            for i, (rel, off, a, b, kind) in enumerate(sel):
                src = open(os.path.join(crate, rel)).read()
                line_no = src[:off].count("\n") + 1
                ctx = src.split("\n")[line_no - 1].strip()[:160]
                r = run_mutant(project, cases, bins["ref"], ref_out, args.build, (rel, a, b, off), f"m{i}", args.jobs)
                r.update({"file": rel, "line": line_no, "kind": kind, "op": f"{a} -> {b}", "source_line": ctx})
                res.append(r)
                print("mutant", project, i, r["status"], rel, line_no, a, "->", b, flush=True)
            valid = [r for r in res if r["status"] != "stillborn"]
            killed = [r for r in valid if r["status"].startswith("killed")]
            out["mutation"][project] = {
                "candidates": len(muts), "selected": len(sel), "stillborn": len(res) - len(valid),
                "valid": len(valid), "killed": len(killed),
                "kill_rate": round(len(killed) / len(valid), 3) if valid else None,
                "cases_used": len(cases), "mutants": res}
    out["inject_equivalent_note"] = {
        "project": "genann", "fault": "genann_act_sigmoid: `if a > 45.0` -> `if a > 44.0`", "status": "survived (first run)",
        "why": "equivalent in IEEE double: 1.0/(1.0+exp(-a)) rounds to exactly 1.0 for every a >= 37, so the clamp "
               "threshold can move anywhere in [37, 45] without changing any output; replaced by the genann_train fault"}
    out["provenance"] = R.ou.provenance(__file__, {"wall_seconds": round(time.time() - t0),
                                                    "diff_inputs": args.diff,
                                                    "selection": "all candidates, thinned evenly to --cap (deterministic)"})
    R.ou.dump(args.out, out)


if __name__ == "__main__":
    main()
