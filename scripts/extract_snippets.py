#!/usr/bin/env python3
"""Extract the code shown in the paper's snippet figure, verbatim, from the systems' public artifacts.

Each snippet names its defect in results/defect_ledger.json and gives line ranges in the original C
and the translated Rust. Lines are copied exactly (tabs expanded to 4 spaces); a gap between two
ranges becomes one elision line. The output, results/code_snippets.json, records every file's path,
sha256 and the line numbers shown, so the figure (scripts/plot_snippets.py) is drawn from data and a
reader can check each line against the artifact.

    python3 -I scripts/extract_snippets.py        # needs artifacts/ (systems/*/fetch.sh)
"""
import hashlib, json, os, datetime, platform, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
A = "artifacts/"
SPEC = [
    dict(system="VERT", defect="VERT-HEXAGONAL_NUMBER-1", title="VERT · HEXAGONAL_NUMBER",
         note="n = 3: C returns 15, the Rust 9 (it computes n²)",
         c=(A + "vert/benchmark/c_transcoder/HEXAGONAL_NUMBER/HEXAGONAL_NUMBER.c", [(21, 23)], [22]),
         rust=(A + "vert/benchmark/c_transcoder/HEXAGONAL_NUMBER/out-rwasm-bolero/src/main.rs", [(388, 388), (392, 396)], [394])),
    dict(system="FLOURINE", defect="FLOURINE-operator_list-1", title="FLOURINE · opl_emu_registers_operator_list",
         note="shifting a u8 by 8, 16, 24 wraps to a no-op: (24,211,56,46) gives 337, not 775,475,992",
         c=(A + "flourine_dl/flourine-artifact/bms/c/opl/opl_emu_registers_operator_list/opl_emu_registers_operator_list.c", [(16, 19)], [18]),
         rust=(A + "flourine_dl/flourine-artifact/translations_only/success/c/opl/opl_emu_registers_operator_list/claude2/BaseRepair/opl_emu_registers_operator_list.rs", [(4, 10)], [6, 7, 8])),
    dict(system="C2SaferRust", defect="C2SR-tail-1", title="C2SaferRust · tail -c / -n +N",
         note="the C sets the global count_lines; the Rust parses into a new local that only picks an error "
              "message, so count_lines keeps the true that main sets and tail -c 3 prints 3 lines, not 3 bytes",
         c=(A + "c2saferrust/coreutils/src/tail/c/tail.c", [(169, 169), (2211, 2213)], [2213]),
         rust=(A + "c2saferrust/coreutils/src/tail/rust_WIP/src/tail.rs", [(835, 835), (4649, 4650), (4667, 4667)], [4650])),
    dict(system="RustAssure", defect="RUSTASSURE-libcsv-csv_parse-1", title="RustAssure · libcsv csv_parse",
         note="with CSV_EMPTY_IS_NULL, an empty field reaches the callback as NULL in C, as an empty slice in Rust",
         c=(A + "rustassure/src/python/inputs-complex/libcsv/libcsv.c", [(55, 58)], [56]),
         rust=(A + "rustassure/src/python/inputs-complex/libcsv/link/individual-funcs_gpt-4o_2025-07-02_14-41-02__complete/csv_parse.rs",
               [(23, 23), (81, 84)], [23, 83])),
    dict(system="SACTOR", defect="SACTOR-C1", title="SACTOR · Holdem-Odds NewCard",
         note="a nested stub accepts every card: NewCard(4, 5) is invalid in C, valid in Rust",
         c=(A + "sactor/CRUST-bench/datasets/CRUST_bench/CBench/Holdem-Odds/src/cards.c", [(46, 53)], [49]),
         rust=(A + "sactor/CRUST-bench/datasets/CRUST_bench/CBench/Holdem-Odds/result/translated_code_unidiomatic/combined.rs", [(81, 87)], [82, 83, 84])),
    dict(system="His2Trans", defect="H2T-BUF-1", title="His2Trans · buffer_append_n",
         note="the length argument is ignored: append_n(\"hello\", 1) appends \"h\" in C, \"hello\" in Rust",
         c=(A + "his2trans/data/rq2/deepseek/c_source/c_source/buffer/src/buffer.c", [(195, 197), (200, 201)], [201]),
         rust=(A + "his2trans/data/paper_artifacts/deepseek_v4_pro_oss8_0613_rq2_100pct_minimal/projects/buffer/src/src_buffer.rs", [(154, 159)], [157, 159])),
]


def take(path, ranges, hl):
    raw = open(os.path.join(ROOT, path), "rb").read()
    lines = raw.decode("utf-8", "replace").split("\n")
    out, prev = [], None
    for a, b in ranges:
        if prev is not None and a > prev + 1:
            out.append({"line": None, "text": "…", "hl": False})
        block = [lines[n - 1].expandtabs(4).rstrip() for n in range(a, b + 1)]
        ind = min((len(x) - len(x.lstrip(" ")) for x in block if x.strip()), default=0)
        ind = ind if ind >= 8 else 0  # dedent only deeply nested excerpts
        for n, x in zip(range(a, b + 1), block):
            out.append({"line": n, "text": x[ind:], "hl": n in hl})
        prev = b
    rec = {"file": path, "sha256": hashlib.sha256(raw).hexdigest(), "lines": out}
    rec.update(permalink(path, ranges, hl))
    return rec


# Artifacts that are not git checkouts: link to the pinned archive (no per-file URL exists).
ARCHIVES = {A + "flourine_dl/": ("https://d34gtk3knhjgeg.cloudfront.net/artifact.tar.gz", "flourine-artifact/")}


def git(d, *args):
    return subprocess.run(["git", "-C", d, *args], capture_output=True, text=True)


def permalink(path, ranges, hl):
    """GitHub permalink to the shown lines at the checkout's commit, after checking the local file is the
    committed one; for an archive, the archive URL and the path inside it."""
    for prefix, (url, inner) in ARCHIVES.items():
        if path.startswith(prefix):
            return {"url": None, "source_url": url, "source_kind": "archive",
                    "path_in_source": path[len(prefix):]}
    d = os.path.join(ROOT, os.path.dirname(path))
    while d != ROOT and not os.path.isdir(os.path.join(d, ".git")):
        d = os.path.dirname(d)
    rel = os.path.relpath(os.path.join(ROOT, path), d)
    sha = git(d, "rev-parse", "HEAD").stdout.strip()
    remote = git(d, "remote", "get-url", "origin").stdout.strip().removesuffix(".git")
    assert git(d, "ls-files", "--error-unmatch", rel).returncode == 0, f"{rel} not tracked in {d}"
    assert git(d, "diff", "--quiet", "HEAD", "--", rel).returncode == 0, f"{rel} differs from {sha}"
    last = max(hl) if hl else ranges[-1][1]
    a, b = next((a, b) for a, b in ranges if a <= last <= b)   # the excerpt range holding the last highlight
    anchor = f"#L{a}" if a == b else f"#L{a}-L{b}"
    return {"url": f"{remote}/blob/{sha}/{rel}{anchor}", "source_url": f"{remote}/tree/{sha}",
            "source_kind": "git", "commit": sha, "path_in_source": rel}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "results/code_snippets.json"))
    out_path = ap.parse_args().out
    ledger = {r["defect_id"]: r for r in json.load(open(os.path.join(ROOT, "results/defect_ledger.json")))["rows"]}
    snippets = []
    for s in SPEC:
        assert s["defect"] in ledger and ledger[s["defect"]].get("count_status") == "counted", s["defect"]
        snippets.append({k: s[k] for k in ("system", "defect", "title", "note")} |
                        {"c": take(*s["c"]), "rust": take(*s["rust"])})
    try:
        rev = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    except OSError:
        rev = None
    out = {"provenance": {"script": "scripts/extract_snippets.py", "git_rev": rev, "host": platform.system(),
                          "date": datetime.datetime.now().isoformat(timespec="seconds")},
           "snippets": snippets}
    json.dump(out, open(out_path, "w"), indent=1, ensure_ascii=False)
    for s in snippets:
        print(s["title"])
        for side in ("c", "rust"):
            for l in s[side]["lines"]:
                print(f"  {side:4} {l['line'] or '':>5} {'>' if l['hl'] else ' '} {l['text']}")


if __name__ == "__main__":
    main()
