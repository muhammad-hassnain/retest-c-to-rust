#!/usr/bin/env python3
"""Build the defect explorer (explorer/index.html) from results/defect_ledger.json.

    python3 -I scripts/build_explorer.py                    # -> out/explorer/index.html
    python3 -I scripts/build_explorer.py --out FILE
    python3 -I scripts/build_explorer.py --update-shipped   # -> explorer/index.html (needs artifacts/ for code panes)

One tab per re-tested system, in Table I's order. Each tab shows the system's own check (Table I), what
we re-tested, what we found, and every candidate defect in the ledger: the 160 counted defects and the
candidates we did not count, each with its reason. A defect shows its mechanism, an exposing input, the
C and Rust outputs, what the system's records say, and the evidence. Every number comes from the ledger.

Without artifacts/ the page is complete except for the source-code panes (upstream links remain).
"""
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import make_tables as MT  # noqa: E402  system order, display names and per-system counts


def R(*p):
    return os.path.join(ROOT, *p)


# Table I of the paper (sections/survey.tex): each system's own check.
OWN_CHECK = {
    "VERT": "fuzzer and bounded model checker against a Rust copy of the C (rWasm)",
    "Flourine": "cross-language differential fuzzing",
    "Syzygy": "equivalence tests against the C",
    "C2SaferRust": "the coreutils end-to-end tests",
    "SACTOR": "the C project's end-to-end tests, run through the C interface (FFI)",
    "RustMap": "functional tests; a whole-log diff",
    "RustAssure": "KLEE differential symbolic execution per function",
    "PtrTrans": "equivalence unit tests",
    "His2Trans": "the project's tests",
    "AdaTrans": "the repair loop's example tests, then a fuzzing oracle",
    "ACTOR": "the benchmark's hidden test vectors",
}
FOLDER = {"VERT": "vert", "Flourine": "flourine", "Syzygy": "syzygy", "C2SaferRust": "c2saferrust", "SACTOR": "sactor",
          "RustMap": "rustmap", "RustAssure": "rustassure", "PtrTrans": "ptrtrans", "His2Trans": "his2trans",
          "AdaTrans": "adatrans", "ACTOR": "actoruw"}
CLASS = {"outright_error": "outright error", "edge_input_robustness": "edge-input",
         "c_bug_not_copied": "follows a bug fix or recorded output", "other": "other"}
NOT_COUNTED = {"excluded_original_c_ub": "not counted: undefined behavior in the C (rule i)",
               "precondition_only": "not counted: violates a stated precondition (rule ii)",
               "excluded_unspecified_value": "not counted: unspecified value (rule iii)",
               "excluded_dealloc_hook": "not counted: memory-freeing callback (rule iv)",
               "excluded_contested_conversion": "not counted: negative value to %u / %x (rule v)",
               "hang-not-counted": "not counted: hang",
               "S3-not-counted": "not counted: build failure",
               "unconfirmed": "not counted: no cause we could confirm"}


def not_counted(r):
    """The exclusion label for a row. Rule (i) also covers a C whose result depends on the signedness of
    char (the paper's UB rule, condition iii); say so rather than 'undefined behavior'."""
    if r["count_status"] == "excluded_original_c_ub" and "signed-char" in json.dumps(r.get("ub_rule") or {}):
        return "not counted: the C's result depends on char signedness (rule i)"
    return NOT_COUNTED.get(r["count_status"], "not counted: " + r["count_status"])
ACCEPT = {"accepted-recorded": "accepted (the artifact records a pass)",
          "accepted-rerun": "accepted (no record; the documented check passes when re-run)",
          "rejected": "rejected (the artifact records a failure)",
          "no-record": "no record"}

# Upstream hosting of each artifact at the commit we tested (the pins in systems/*/fetch.sh).
PINNED = {
    "c2saferrust": ("https://github.com/vikramnitin9/c2saferrust", "e8d5c3a18b91523f0ca6e52117cbd2f288d21c31"),
    "rustassure": ("https://github.com/davsec-lab/rustassure", "39618406bd0c6acb1c5ca2cd697c3781a8e4c296"),
    "rustmap": ("https://github.com/Cxm211/RustMap", "cb1df95fb9f4950f0ae1a43863c0051ff991891d"),
    "syzygy_zopfli": ("https://github.com/syzygy-project/Syzygy_Zopfli", "e0e7666d5c5b6872894ba0909700a7cbaafcf4ec"),
    "vert": ("https://github.com/YoshikiTakashima/vert", "3270ceccb63a28acd611c8fa6105ed37dc76de07"),
    "actoruw/ACTOR": ("https://github.com/UW-HARVEST/ACTOR", "1b1e70ac84e10ca73efa7b8a8b9e5d6f6ab8b885"),
    "actoruw/results": ("https://github.com/UW-HARVEST/harvest-agentic-translate-results", "e31aec03eb21dde31c399b64d49b806c6470bdf4"),
    "adatrans": ("https://github.com/SlainTroyard/adatrans_dev", "31f4031aadb7ee9d4a2fb1a180e78ae9b45e5578"),
    "his2trans": ("https://github.com/SYSUSELab/His2Trans", "3a0ec5544c8e6788280b3520105a77432ed8a730"),
    "ptrtrans": ("https://github.com/FudanSELab/PtrTrans-C2Rust", "b20d5bb68a598a739f79d52114651e37cc6999cb"),
    "sactor/sactor": ("https://github.com/qsdrqs/sactor", "577c3d2b8074d5fed7c47b988c0acd175e498f98"),
    "sactor/sactor-datasets": ("https://github.com/qsdrqs/sactor-datasets", "1060b3b27d151e2d4919dd2db69abcfa91153399"),
    "sactor/CRUST-bench": ("https://github.com/qsdrqs/CRUST-bench", "69970f9c9a583290f943145eada5e8948b1b91ff"),
}
FLOURINE_TARBALL = "https://d34gtk3knhjgeg.cloudfront.net/artifact.tar.gz"
_TRACKED = {}


def tracked(key):
    """Files git tracks in artifacts/<key> (None when the clone is absent)."""
    if key not in _TRACKED:
        d = R("artifacts", key)
        try:
            out = subprocess.run(["git", "-C", d, "ls-files"], capture_output=True, text=True, check=True).stdout
            _TRACKED[key] = set(out.splitlines())
        except (OSError, subprocess.CalledProcessError):
            _TRACKED[key] = None
    return _TRACKED[key]


def upstream(path, a=None, b=None):
    """URL of an artifacts/... path at the tested commit, or None."""
    if "#" in path:   # code inside a JSON log: link the file, not lines
        path, a, b = path.split("#")[0], None, None
    if not path.startswith("artifacts/"):
        return None
    rest = path[len("artifacts/"):]
    if rest.startswith("flourine_dl/"):
        return FLOURINE_TARBALL
    for key in sorted(PINNED, key=len, reverse=True):
        if rest.startswith(key + "/"):
            rel = rest[len(key) + 1:]
            t = tracked(key)
            if t is not None and rel not in t:
                return None
            base, sha = PINNED[key]
            frag = f"#L{a}-L{b}" if a and b and b != a else (f"#L{a}" if a else "")
            return f"{base}/blob/{sha}/{rel}{frag}"
    return None


LOC = re.compile(r"(?<![\w/.-])(artifacts/[^\s:;,)`'\"]+):(\d+)(?:-(\d+))?")
REL = re.compile(r"(?<![\w/.-])([A-Za-z0-9_][^\s:;,)`'\"]*\.(?:rs|c|h|cc|cpp)):(\d+)(?:-(\d+))?")
CODE = {}   # this builder embeds code excerpts per defect; the template's whole-file viewer stays unused
# Some ledger locations are relative to a folder inside the system's artifact; these are the folders tried.
ROOTS = {"SACTOR": ["sactor/sactor-datasets", "sactor/CRUST-bench", "sactor/CRUST-bench/datasets/CRUST_bench/CBench"],
         "His2Trans": ["his2trans"], "AdaTrans": ["adatrans"], "PtrTrans": ["ptrtrans"]}
PREFIXES = ("sactor-datasets ", "qsdrqs/CRUST-bench ")


# AdaTrans's Rust exists only inside its conversation logs: "C_432_4.rs:45-47 ... -- source: artifacts/adatrans/...json
# history[15].extracted_code". The excerpt is read from that JSON field (path "<json>#<index>").
CONV = re.compile(r"^\s*(C_\d+_\d+\.rs):(\d+)(?:-(\d+))?.*?source: (artifacts/\S+\.json) history\[(\d+)\]\.extracted_code", re.S)


def locations(value, system=None):
    text = "; ".join(value) if isinstance(value, list) else (value or "")
    m = CONV.match(text)
    if m:
        return text, [(f"{m.group(4)}#{m.group(5)}", int(m.group(2)), int(m.group(3) or m.group(2)))]
    locs = [(m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))) for m in LOC.finditer(text)]
    if not locs and system in ROOTS:
        t = text
        for pre in PREFIXES:
            t = t.replace(pre, "")
        for m in REL.finditer(t):
            for root in ROOTS[system]:
                cand = f"artifacts/{root}/{m.group(1)}"
                if os.path.isfile(R(cand)):
                    locs.append((cand, int(m.group(2)), int(m.group(3) or m.group(2))))
                    break
    return text, locs


def excerpt(path, a, b, ctx=5):
    try:
        if "#" in path:
            j, i = path.split("#")
            src = json.load(open(R(j)))["history"][int(i)]["extracted_code"].splitlines()
        else:
            src = open(R(path), errors="replace").read().splitlines()
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None
    lo, hi = max(1, a - ctx), min(len(src), b + ctx)
    w = len(str(hi))
    body = "\n".join(f"{i:>{w}}{'>' if a <= i <= b else ' '} {src[i - 1]}" for i in range(lo, hi + 1))
    where = path if "#" not in path else "{} history[{}].extracted_code".format(*path.split("#"))
    return f"// {where}, lines {a}-{b} (marked >)\n{body}"


def show(v, limit=1500):
    if v is None:
        return "(not recorded)"
    if isinstance(v, dict):
        parts = []
        for k in ("exit", "stdout", "stderr"):
            if k in v:
                x = v[k]
                parts.append(f"{k}: {x}" if k == "exit" else f"{k}: {json.dumps(x, ensure_ascii=False)}")
        rest = {k: x for k, x in v.items() if k not in ("exit", "stdout", "stderr")}
        if rest:
            parts.append(json.dumps(rest, ensure_ascii=False))
        s = "\n".join(parts)
    elif isinstance(v, (list, tuple)):
        s = json.dumps(v, ensure_ascii=False)
    else:
        s = str(v)
    return s if len(s) <= limit else s[:limit] + f" ... ({len(s) - limit} more characters)"


def evidence(r):
    ev = []
    for e in r.get("acceptance_evidence") or []:
        ev.append("Acceptance: " + str(e))
    ce = r.get("check_exercises_defect") or {}
    if isinstance(ce, dict) and ce.get("evidence"):
        ev.append(f"Did the check run an exposing input ({ce.get('status')}): {ce['evidence']}")
    if r.get("on_check_inputs_evidence"):
        ev.append("Inputs the check ran: " + str(r["on_check_inputs_evidence"]))
    ub = r.get("ub_rule") or {}
    if isinstance(ub, dict) and ub.get("evidence"):
        ev.append(f"Undefined-behavior rule ({ub.get('status')}): {ub['evidence']}")
    pre = r.get("precondition") or {}
    if isinstance(pre, dict) and pre.get("evidence"):
        ev.append(f"Precondition ({pre.get('status')}): {pre['evidence']}")
    if r.get("triage_note"):
        ev.append("Triage note: " + str(r["triage_note"]))
    return ev


def case(r):
    counted = r["count_status"] == "counted"
    verdict = ("crash" if r.get("severity") == "crash" else "wrong output") if counted else \
        not_counted(r)
    family = CLASS.get(r.get("class"), r.get("class") or "unclassified") if counted else "not counted"
    sl = r.get("source_location") or {}
    sysn = MT.norm_system(r["system"])
    rust_text, rust_locs = locations(sl.get("rust") if isinstance(sl, dict) else None, sysn)
    c_text, c_locs = locations(sl.get("c") if isinstance(sl, dict) else None, sysn)
    links = []
    for label, locs in (("Rust", rust_locs), ("C", c_locs)):
        for p, a, b in locs:
            u = upstream(p, a, b)
            if u and not any(x["url"] == u for x in links):
                links.append({"label": f"{label}: {p.split('/')[-1]}:{a}" + (f"-{b}" if b != a else ""), "url": u})
    ce = r.get("check_exercises_defect") or {}
    oc = r.get("on_check_inputs")
    rec = {"acceptance": ACCEPT.get(r.get("acceptance_status"), r.get("acceptance_status") or "n/a"),
           "check ran an exposing input": (ce.get("status") if isinstance(ce, dict) else ce) or "n/a",
           "only on inputs the check never ran": {True: "no", False: "yes", None: "not measurable"}.get(oc, str(oc))}
    return {
        "id": r["defect_id"],
        "family": family,
        "verdict": verdict,
        "mechanism": r.get("mechanism"),
        "translation": r.get("translation"),
        "location": "; ".join(x for x in (("Rust: " + rust_text) if rust_text else "", ("C: " + c_text) if c_text else "") if x),
        "examples": [{"input": show(r.get("example_input")), "c": show(r.get("c_output")), "rust": show(r.get("rust_output"))}],
        "their_record": rec,
        "inputs_tried": None,
        "diverging_inputs": r.get("exposure_count"),
        "c_src": excerpt(*c_locs[0]) if c_locs else None,
        "rust_src": excerpt(*rust_locs[0]) if rust_locs else None,
        "links": links,
        "evidence": evidence(r),
    }


def system_tab(key, name, rows, totals):
    rs = [r for r in rows if MT.norm_system(r["system"]) == key]
    t = MT.row_for(key, rows, totals)
    counted = [r for r in rs if r["count_status"] == "counted"]
    tt = (totals.get(key) or {}).get("translations_tested") or {}
    unit = tt.get("unit") or "translation"
    tested = None if t["tested"] is None else str(t["tested"])
    scope = (f"{tested} translation{'s' if tested != '1' else ''} re-tested (unit: {unit})" if tested is not None
             else f"Re-tested: {tt.get('detail') or 'see the paper, Section IV'}")
    by_class = {}
    for r in counted:
        k = CLASS.get(r.get("class"), r.get("class"))
        by_class[k] = by_class.get(k, 0) + 1
    excl = {}
    for r in rs:
        if r["count_status"] != "counted":
            k = not_counted(r).replace("not counted: ", "").replace("violates a stated precondition", "stated precondition")
            excl[k] = excl.get(k, 0) + 1
    order = {"outright error": 0, "edge-input": 1, "follows a bug fix or recorded output": 2, "other": 3, "not counted": 4}
    cases = sorted((case(r) for r in rs), key=lambda c: (order.get(c["family"], 9), c["id"]))
    found = (f"{t['n']} defect{'s' if t['n'] != 1 else ''} ({t['s1']} wrong output, {t['crash']} crash) in {t['defective']} "
             f"translation{'s' if t['defective'] != 1 else ''}; "
             + (f"{t['only']} {'appear' if t['only'] != '1' else 'appears'} only on inputs the system's check never ran."
                if t["only"] != "n.m." else "held-out status is not measurable: the artifact records no check inputs."))
    if excl:
        found += " Not counted: " + "; ".join(f"{k}: {n}" for k, n in sorted(excl.items())) + "."
    return {
        "name": name,
        "unit": scope,
        "headline": (f"{t['n']} defect{'s' if t['n'] != 1 else ''} in {t['defective']}"
                     + (f" of {tested}" if tested is not None else f" translation{'s' if t['defective'] != 1 else ''}")
                     + f"; its check accepted {t['acc']}"),
        "claim": {"text": f"{scope}, against the original C. Fetch script and harness: systems/{FOLDER[key]}/.",
                  "source": "results/defect_ledger.json (totals); paper, Section IV"},
        "their_check": {"text": f"{OWN_CHECK[key]}. What the artifact records for the defective translations:",
                        "table": [["accepted", "rejected", "no record"], [str(t["acc"]), str(t["rej"]), str(t["nr"])]],
                        "source": "paper, Table I; results/defect_ledger.json (acceptance_status)"},
        "our_test": {"text": found, "table": [["class", "defects"]] + [[k, str(by_class[k])] for k in sorted(by_class, key=lambda x: order.get(x, 9))],
                     "source": "results/defect_ledger.json"},
        "cases": cases,
    }


def main():
    a = sys.argv[1:]
    shipped = R("explorer", "index.html")
    out = os.path.abspath(a[a.index("--out") + 1]) if "--out" in a else (shipped if "--update-shipped" in a else R("out", "explorer", "index.html"))
    if out == shipped and not os.path.isdir(R("artifacts")):
        sys.exit("refusing to overwrite the shipped explorer/index.html without artifacts/ (its code panes need them); use --out FILE")
    L = json.load(open(R("results", "defect_ledger.json")))
    totals = {MT.norm_system(k): v for k, v in L["totals"].items()}
    tabs = [system_tab(key, name, L["rows"], totals) for key, name, _cite in MT.ORDER
            if any(MT.norm_system(r["system"]) == key for r in L["rows"])]
    data = json.dumps(tabs, ensure_ascii=False).replace("</", "<\\/")
    code = json.dumps(CODE, ensure_ascii=False).replace("</", "<\\/")
    tpl = open(R("scripts", "explorer_template.html")).read().replace("/*__CODE__*/null", code)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(tpl.replace("/*__DATA__*/null", data))
    n = sum(len(t["cases"]) for t in tabs)
    print(out, os.path.getsize(out) // 1024, "KB;", len(tabs), "systems,", n, "candidate defects;",
          {t["name"]: len(t["cases"]) for t in tabs})
    if not os.path.isdir(R("artifacts")):
        print("built without artifacts/: source-code panes are empty; upstream links remain (fetch with systems/*/fetch.sh)")


if __name__ == "__main__":
    main()
