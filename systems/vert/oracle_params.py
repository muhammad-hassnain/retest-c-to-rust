"""Does VERT's rWasm oracle read all of a function's arguments?

VERT's bolero/Kani harness (out-rwasm-bolero/src/main.rs) sets globals PARAM1..PARAMn from the
fuzzer and compares the LLM translation f_gold(PARAM1..PARAMn) against the oracle
f_gold_wasm_thread_unsafe(), which is rWasm-compiled from a C stub that calls f_gold with fixed
literals (e.g. `f_gold(1,29)` in *_towasm_mutated.c, the file the bolero oracle is built from). The 2023 generator that produced the shipped
benchmark (VERT commit 3509329d, generate_utils.py) patches only the first wasm argument
(`v0 = TaggedVal::from(unsafe {PARAM1})`); later arguments keep the stub's literals.

Static part (all benchmarks): declared parameter count (bolero with_type tuple) vs the PARAMk the
oracle code reads. Empirical part (every multi-parameter function whose parameter types we can
construct): copy VERT's project unchanged except the bolero test is replaced by a main that, for each
fixed PARAM1, varies PARAM2..PARAMn and prints the oracle's result and VERT's LLM translation's
result (same argument expression VERT's test uses). invariant = oracle never changes when only
PARAM2..n change; llm_varies = the LLM translation's result does change (so the translation reads
them and the comparison VERT's PBT makes would be meaningless for those arguments).

  python3 -I systems/vert/oracle_params.py [JOBS]   # writes results/vert_oracle_params.json
"""
import json, os, re, shutil, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
BENCH = os.path.join(REPO, "artifacts", "vert", "benchmark", "c_transcoder")
OUT = os.path.join(REPO, "results", "vert_oracle_params.json")

P1 = {"i32": ["0", "3", "9"], "u32": ["0", "3", "9"], "f32": ["0.0", "3.0", "9.0"],
      "[i32;2]": ["[0, 0]", "[3, 5]", "[9, 2]"]}
REST = {"i32": ["0", "1", "2", "5", "11", "-4"], "u32": ["0", "1", "2", "5", "11", "40"],
        "f32": ["0.0", "1.0", "2.0", "5.0", "11.0", "-4.0"],
        "[i32;2]": ["[0, 0]", "[1, 2]", "[2, 1]", "[5, 5]", "[11, -3]", "[-4, 7]"]}


def vert_results():
    res = {}
    for l in open(os.path.join(REPO, "artifacts", "vert", "c_results.csv")):
        p = [x.strip() for x in l.split(",")]
        res[p[0]] = {k: int(v) for k, v in (x.split("=") for x in p[1:])}
    return res


def static(f):
    s = open(os.path.join(BENCH, f, "out-rwasm-bolero", "src", "main.rs")).read()
    t = re.search(r"with_type::<\((.*?)\)>", s)
    if not t:
        return None
    types = [x.strip() for x in t.group(1).split(",") if x.strip()]
    orc = s[:s.index("////// LLM Output")]
    reads = sorted({int(x) for x in re.findall(r"unsafe\s*\{\s*PARAM(\d+)", orc)})
    stub = open(os.path.join(BENCH, f, f + "_towasm_mutated.c")).read()
    call = re.findall(r"f_gold\s*\(([^;]*)\)\s*;", stub)
    return {"types": types, "declared": len(types), "oracle_reads": reads, "stub_call": call[-1] if call else None,
            "src": s}


def make_main(src, types):
    src = re.sub(r"^fn main\(\) \{", "fn _rwasm_main() {", src, count=1, flags=re.M)
    args = re.search(r"let result = f_gold\((.*)\);", src[src.index("use bolero::check;"):]).group(1)
    i = src.index("use bolero::check;")
    rows = []
    rest_len = len(REST[types[1]])
    for a in P1[types[0]]:
        for j in range(rest_len):
            rows.append([a] + [REST[t][(j + k) % rest_len] for k, t in enumerate(types[1:])])
    body = []
    for idx, r in enumerate(rows):
        sets = " ".join(f"PARAM{k + 1} = {v};" for k, v in enumerate(r))
        body.append(f"""    if which == {idx} {{ unsafe {{ {sets} }}
    let llm = std::panic::catch_unwind(|| f_gold({args}));
    let orc = std::panic::catch_unwind(|| f_gold_wasm_thread_unsafe());
    println!("{json.dumps(r).replace('"', '').replace('{', '{{').replace('}', '}}')}\\t{{}}\\t{{}}", show(llm), show(orc)); }}""")
    return src[:i] + """
fn show<T: std::fmt::Debug>(r: std::thread::Result<T>) -> String { match r { Ok(v) => format!("{:?}", v), Err(_) => "panic".to_string() } }
fn main() {
    std::panic::set_hook(Box::new(|_| {}));
    let which: usize = std::env::args().nth(1).unwrap().parse().unwrap();
""" + "\n".join(body) + "\n}\n", len(rows)


def empirical(f, st, tgt):
    work = tempfile.mkdtemp(prefix="vop_")
    try:
        shutil.copytree(os.path.join(BENCH, f, "out-rwasm-bolero"), os.path.join(work, "p"), ignore=shutil.ignore_patterns("target"))
        code, nrows = make_main(st["src"], st["types"])
        open(os.path.join(work, "p", "src", "main.rs"), "w").write(code)
        env = {**os.environ, "CARGO_TARGET_DIR": tgt, "RUSTFLAGS": "-C overflow-checks=false -A warnings"}
        b = subprocess.run(["cargo", "build", "--release", "-q"], cwd=os.path.join(work, "p"), capture_output=True, text=True, env=env)
        if b.returncode:
            return {"build_ok": False, "build_error": b.stderr[-600:]}
        name = re.search(r'name\s*=\s*"([^"]+)"', open(os.path.join(work, "p", "Cargo.toml")).read()).group(1)
        # one process per input row, so a translation that hangs on one input costs only that row
        rows = []
        for idx in range(nrows):
            try:
                p = subprocess.run([os.path.join(tgt, "release", name), str(idx)], capture_output=True, text=True, timeout=10)
                line = p.stdout.strip()
                rows.append(line.split("\t") if line.count("\t") == 2 else [f"row{idx}", "crash", "crash"])
            except subprocess.TimeoutExpired:
                rows.append([f"row{idx}", "timeout", "timeout"])
        # a timeout/crash row hides which side hung; drop such rows from invariance (recorded in n_unresolved)
        unresolved = sum(1 for r in rows if r[1] in ("timeout", "crash"))
        rows = [r for r in rows if r[1] not in ("timeout", "crash")]
        groups = {}
        for inp, llm, orc in rows:
            groups.setdefault(inp.split(",")[0], []).append((inp, llm, orc))
        inv = all(len({o for _, _, o in g}) == 1 for g in groups.values())
        llm_var = any(len({l for _, l, _ in g}) > 1 for g in groups.values())
        disagree = sum(1 for _, l, o in rows if l != o)
        return {"build_ok": True, "rows": [{"in": i, "llm": l, "oracle": o} for i, l, o in rows],
                "oracle_invariant_in_params_2_to_n": inv, "llm_varies_in_params_2_to_n": llm_var,
                "llm_vs_oracle_disagreements": disagree, "n_rows": len(rows), "n_unresolved": unresolved}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    jobs = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    vr = vert_results()
    funcs = sorted(f for f in os.listdir(BENCH) if os.path.exists(os.path.join(BENCH, f, "out-rwasm-bolero", "src", "main.rs")))
    rows, todo = [], []
    for f in funcs:
        st = static(f)
        if st is None:
            rows.append({"function": f, "static": "no bolero with_type"}); continue
        row = {"function": f, "types": st["types"], "declared": st["declared"], "oracle_reads": st["oracle_reads"],
               "towasm_stub_call": st["stub_call"], "vert_result": vr.get(f),
               "oracle_ignores_params": st["declared"] > 1 and len(st["oracle_reads"]) < st["declared"]}
        rows.append(row)
        if row["oracle_ignores_params"] and all(t in P1 for t in st["types"]) and (vr.get(f) or {}).get("compile"):
            todo.append((row, st))
    tgts = [tempfile.mkdtemp(prefix=f"vop_tgt{i}_") for i in range(jobs)]

    def work(k):
        row, st = todo[k]
        row["empirical"] = empirical(row["function"], st, tgts[k % jobs])
        e = row["empirical"]
        print(row["function"], "BUILD-FAIL" if not e["build_ok"] else
              f"invariant={e['oracle_invariant_in_params_2_to_n']} llm_varies={e['llm_varies_in_params_2_to_n']} dis={e['llm_vs_oracle_disagreements']}", flush=True)
    # jobs must not share a target dir concurrently: worker i only takes k % jobs == i
    with ThreadPoolExecutor(jobs) as ex:
        list(ex.map(lambda i: [work(k) for k in range(i, len(todo), jobs)], range(jobs)))
    multi = [r for r in rows if r.get("declared", 0) > 1]
    emp = [r for r in rows if r.get("empirical", {}).get("build_ok")]
    def vr_(r, k): return (r.get("vert_result") or {}).get(k, 0)
    summ = {
        "benchmarks": len(rows),
        "multi_param": len(multi),
        "multi_param_oracle_reads_only_param1": sum(r["oracle_reads"] == [1] for r in multi),
        "multi_param_oracle_ignores_some_param": sum(r["oracle_ignores_params"] for r in multi),
        "ignored_and_vert_compile": sum(vr_(r, "compile") for r in multi if r["oracle_ignores_params"]),
        "ignored_and_vert_pbt_pass": sum(vr_(r, "bolero") for r in multi if r["oracle_ignores_params"]),
        "ignored_and_vert_bounded_kani_pass": sum(vr_(r, "bounded_kani") for r in multi if r["oracle_ignores_params"]),
        "vert_pbt_pass_total": sum(vr_(r, "bolero") for r in rows),
        "vert_bounded_kani_pass_total": sum(vr_(r, "bounded_kani") for r in rows),
        "empirical_tested": len(emp),
        "empirical_oracle_invariant": sum(r["empirical"]["oracle_invariant_in_params_2_to_n"] for r in emp),
        "empirical_llm_varies": sum(r["empirical"]["llm_varies_in_params_2_to_n"] for r in emp),
        "empirical_vert_pbt_pass_but_llm_disagrees_with_oracle": sum(1 for r in emp if vr_(r, "bolero") and r["empirical"]["llm_vs_oracle_disagreements"]),
        "empirical_vert_pbt_pass": sum(1 for r in emp if vr_(r, "bolero")),
        "empirical_build_fail": sum(1 for r in rows if r.get("empirical") and not r["empirical"]["build_ok"]),
        "source_of_bug": "VERT commit 3509329d (2023-11-26) generate_utils.py patches only v0 = PARAM1 into the rWasm oracle; shipped benchmark committed 736013dc the same day",
    }
    json.dump({"summary": summ, "rows": rows}, open(OUT, "w"), indent=1)
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()
