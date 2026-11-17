"""Systematic per-function differential tester for RustAssure's translations.

Deterministic enumeration (shared grid) + batched execution, same as the VERT/Flourine runners.
RustAssure ships, per function, preprocessed C (<fn>.i) and translated Rust (<fn>.rs); the filename
is the C function name. We drive the functions whose args + return are scalar integers; the rest are
pointer/struct (why RustAssure used KLEE) and aren't reachable by a black-box differential.

  docker run --rm -v "$PWD:/work" -w /work crust-stable python3 systems/rustassure/run.py
"""
import argparse, glob, json, os, re, subprocess, tempfile, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
RA = os.path.join(REPO, "artifacts", "rustassure", "src", "python", "inputs-complex")
sys.path.insert(0, os.path.dirname(HERE))
from enumerate_inputs import enumerate_cases, supported as enum_supported  # noqa: E402
from batch_diff import tally  # noqa: E402

CTYPE = {"u8": "uint8_t", "u16": "uint16_t", "u32": "uint32_t", "u64": "uint64_t",
         "i8": "int8_t", "i16": "int16_t", "i32": "int32_t", "i64": "int64_t",
         "usize": "size_t", "isize": "ssize_t", "bool": "int",
         "c_int": "int", "c_uint": "unsigned", "c_char": "char"}
RET_FMT = {"u8": "%u", "u16": "%u", "u32": "%u", "u64": "%llu", "i8": "%d", "i16": "%d",
           "i32": "%d", "i64": "%lld", "usize": "%zu", "isize": "%zd", "bool": "%d",
           "c_int": "%d", "c_uint": "%u", "c_char": "%d"}


def parse_rust_fn(text):
    m = re.search(r"\bfn\s+(\w+)\s*\(([^)]*)\)\s*(->\s*([^\{]+?))?\s*\{", text, re.S)
    if not m:
        return None
    ptypes = []
    if m.group(2).strip():
        for p in m.group(2).split(","):
            if ":" not in p:
                return None
            ptypes.append(p.split(":", 1)[1].strip())
    return {"name": m.group(1), "ptypes": ptypes, "ret": (m.group(4) or "()").strip()}


def ok_sig(sig):
    if not sig or not sig["ptypes"]:
        return False
    types = sig["ptypes"] + ([sig["ret"]] if sig["ret"] != "()" else [])
    return all(t in CTYPE for t in types) and enum_supported(sig["ptypes"])


def build_c(cname, idotc, sig, d):
    src = re.sub(r"\bint\s+main\s*\(", "int _disabled_main(", idotc)
    k = len(sig["ptypes"])
    scan = " ".join(["%lld"] * k)
    addr = ", ".join(f"&t[{i}]" for i in range(k))
    call = ", ".join(f"({CTYPE[sig['ptypes'][i]]})t[{i}]" for i in range(k))
    rfmt = RET_FMT.get(sig["ret"], "%lld")
    main = f"""
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
int main(void){{ long long t[{max(k,1)}]; char line[8192];
 while(fgets(line,sizeof line,stdin)){{
   if(sscanf(line,"{scan}",{addr})!={k}){{printf("__PARSE__\\n");continue;}}
   printf("{rfmt}\\n",{cname}({call})); fflush(stdout);
 }} return 0; }}
"""
    p = os.path.join(d, "p.c"); open(p, "w").write(src + "\n" + main)
    b = os.path.join(d, "cb")
    r = subprocess.run(["gcc", "-w", "-O1", "-fwrapv", p, "-o", b],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return b if (r.returncode == 0 and os.path.isfile(b)) else None


def build_rust(rdotrs, sig, d):
    src = re.sub(r"\bfn\s+main\s*\(", "fn _disabled_main(", rdotrs)
    k = len(sig["ptypes"])
    reads = "\n".join(f"  let a{i}:{sig['ptypes'][i]}=t[{i}].parse().unwrap_or(0);" for i in range(k))
    call = ", ".join(f"a{i}" for i in range(k))
    body = ("println!(\"{}\", %s(%s) as i64);" % (sig['name'], call)) if sig["ret"] == "bool" \
        else ("println!(\"{}\", %s(%s));" % (sig['name'], call))
    main = f"""
use std::io::BufRead;
fn main(){{ std::panic::set_hook(Box::new(|_|{{}}));
 for line in std::io::stdin().lock().lines(){{ let line=match line{{Ok(l)=>l,Err(_)=>break}};
   let t:Vec<String>=line.split_whitespace().map(|x|x.to_string()).collect();
   if t.len()<{k}{{println!("__PARSE__");continue;}}
{reads}
   let r=std::panic::catch_unwind(std::panic::AssertUnwindSafe(||{{ {body} }}));
   if r.is_err(){{println!("__PANIC__");}}
 }} }}
"""
    p = os.path.join(d, "p.rs"); open(p, "w").write(src + "\n" + main)
    b = os.path.join(d, "rb")
    r = subprocess.run(["rustc", "-O", "-A", "warnings", p, "-o", b],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return b if (r.returncode == 0 and os.path.isfile(b)) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=int, default=2000)
    ap.add_argument("--model", default="gpt-4o")
    ap.add_argument("--out", default=os.path.join(REPO, "results", "rustassure_diff.json"))
    args = ap.parse_args()

    agg = {"functions_found": 0, "tested": 0, "agree": 0, "value_bug": 0, "rust_crash_bug": 0,
           "unsupported": 0, "c-build-fail": 0, "rust-build-fail": 0}
    value_bugs = []
    seen = set()
    for app in sorted(os.listdir(RA)):
        for dd in sorted(glob.glob(os.path.join(RA, app, "archive", f"individual-funcs_{args.model}_*complete"))):
            if app in seen:
                continue
            seen.add(app)
            for rs in sorted(glob.glob(os.path.join(dd, "*.rs"))):
                if rs.endswith(".ll"):
                    continue
                cname = os.path.basename(rs)[:-3]
                i = rs[:-3] + ".i"
                if not os.path.isfile(i):
                    continue
                agg["functions_found"] += 1
                sig = parse_rust_fn(open(rs, errors="replace").read())
                if not ok_sig(sig):
                    agg["unsupported"] += 1
                    continue
                with tempfile.TemporaryDirectory() as d:
                    binc = build_c(cname, open(i, errors="replace").read(), sig, d)
                    if not binc:
                        agg["c-build-fail"] += 1
                        continue
                    binr = build_rust(open(rs, errors="replace").read(), sig, d)
                    if not binr:
                        agg["rust-build-fail"] += 1
                        continue
                    cases = enumerate_cases(sig["ptypes"], cap=args.cap)
                    t = tally(binc, binr, cases)
                    agg["tested"] += 1
                    if t["value"] > 0:
                        agg["value_bug"] += 1
                        value_bugs.append({"func": cname, "value": t["value"], "samples": t["value_samples"]})
                    elif t["rust_crash"] > 0:
                        agg["rust_crash_bug"] += 1
                    else:
                        agg["agree"] += 1
            break

    summary = {"system": "RustAssure", "model": args.model,
               "generation": "deterministic enumeration", **agg,
               "value_bug_functions": [v["func"] for v in value_bugs]}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump({"summary": summary, "value_bugs": value_bugs}, open(args.out, "w"), indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
