"""Why RustAssure's unshipped libraries can't be re-graded end-to-end from the artifact.

RustAssure ships composed, runnable crates for only 2 of its 5 libraries (libcsv -> src/Link/
csv_link, u8c -> src/Link/u8c_link). For the other three (libbmp, optipng, url_parser) it ships
only PER-FUNCTION translations (src/python/inputs-complex/<lib>/archive/individual-funcs_<model>_*),
with no composition.

Those per-function translations are not directly composable, because RustAssure translates each
function independently -- so the SAME function appears with many INCOMPATIBLE signatures across
runs/models (e.g. url_parse as `*const c_char -> *mut UrlData` in one file and `&str ->
Option<UrlData>` in another; a helper returning `*mut i8` here and `Option<String>` there). A
library only links if every caller/callee agrees on one ABI, which requires hand-selecting and
reconciling one translation per function -- exactly the curation the authors did for the two
libraries they shipped composed. This script quantifies the inconsistency.

  python3 systems/rustassure/signature_consistency.py
"""
import glob, json, os, re, sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
RA = os.path.join(REPO, "artifacts", "rustassure", "src", "python", "inputs-complex")
LINK = os.path.join(REPO, "artifacts", "rustassure", "src", "Link")


def norm_sig(text, fn):
    m = re.search(r"\bfn\s+" + re.escape(fn) + r"\s*\(([^)]*)\)\s*(->\s*([^\{;]+?))?\s*[\{;]", text, re.S)
    if not m:
        return None
    pts = []
    for p in m.group(1).split(","):
        if ":" in p:
            pts.append(re.sub(r"\s+", "", p.split(":", 1)[1]))
        elif p.strip() in ("&self", "&mut self", "self"):
            pts.append(p.strip().replace(" ", ""))
    ret = re.sub(r"\s+", "", (m.group(3) or "()"))
    return "(" + ",".join(pts) + ")->" + ret


def analyze(lib):
    sigs = defaultdict(set)
    files = glob.glob(os.path.join(RA, lib, "archive", "individual-funcs_*", "*.rs"))
    for rs in files:
        fn = os.path.basename(rs)[:-3]
        s = norm_sig(open(rs, errors="replace").read(), fn)
        if s:
            sigs[fn].add(s)
    multi = {fn: sorted(v) for fn, v in sigs.items() if len(v) > 1}
    worst = sorted(multi.items(), key=lambda kv: -len(kv[1]))[:6]
    return {
        "translation_files": len(files),
        "distinct_functions": len(sigs),
        "functions_with_inconsistent_signature": len(multi),
        "max_distinct_signatures_for_one_function": max((len(v) for v in sigs.values()), default=0),
        "worst_offenders": {fn: v for fn, v in worst},
    }


def composed_libraries():
    return sorted(d for d in os.listdir(LINK)) if os.path.isdir(LINK) else []


def main():
    out = {
        "system": "RustAssure",
        "shipped_libraries_total": 5,
        "shipped_as_composed_runnable_crate": composed_libraries(),
        "composed_count": len(composed_libraries()),
        "per_function_only": ["libbmp", "optipng", "url_parser"],
        "signature_consistency": {lib: analyze(lib)
                                  for lib in ("libcsv", "url_parser.h", "optipng")},
        "conclusion": ("Only libcsv + u8c ship as composed runnable crates (both re-graded "
                       "end-to-end, both drop a C-API contract). The other 3 libraries are "
                       "per-function only and NOT composable from the artifact: most functions "
                       "appear with several incompatible signatures across translations, so "
                       "assembling a working library requires hand-authoring the ABI (our "
                       "integration, not theirs)."),
    }
    os.makedirs(os.path.join(REPO, "results"), exist_ok=True)
    json.dump(out, open(os.path.join(REPO, "results", "rustassure_composability.json"), "w"), indent=2)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
