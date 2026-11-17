"""Systematic per-function differential tester for Flourine's C->Rust translations.

Deterministic enumeration (shared grid, no AI/randomness) + batched execution. We re-test the
translations Flourine marked SUCCESS (its differential fuzzer declared them equivalent) against the
original C on a fixed, structured input grid; a success that diverges here is oracle-fitting. FAIL
translations are run too as a reach check. Scope: the scalar-integer benchmarks (18/112); the rest
take struct pointers (Flourine's own JSON-FFI harness territory).

  docker run --rm -v "$PWD:/work" -w /work crust-stable python3 systems/flourine/run.py
"""
import argparse, glob, json, os, re, subprocess, tempfile, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
FL = os.path.join(REPO, "artifacts", "flourine_dl", "flourine-artifact")
BMS = os.path.join(FL, "bms", "c")
sys.path.insert(0, os.path.dirname(HERE))   # systems/
from enumerate_inputs import enumerate_cases, supported as enum_supported  # noqa: E402
from batch_diff import tally  # noqa: E402

CTYPE = {"u8": "uint8_t", "u16": "uint16_t", "u32": "uint32_t", "u64": "uint64_t",
         "i8": "int8_t", "i16": "int16_t", "i32": "int32_t", "i64": "int64_t",
         "usize": "size_t", "isize": "ssize_t", "bool": "int",
         "c_int": "int", "c_uint": "unsigned", "c_char": "char"}
RET_FMT = {"u8": "%u", "u16": "%u", "u32": "%u", "u64": "%llu", "i8": "%d", "i16": "%d",
           "i32": "%d", "i64": "%lld", "usize": "%zu", "isize": "%zd", "bool": "%d",
           "c_int": "%d", "c_uint": "%u", "c_char": "%d"}

# rustc flags per build profile (--profile). "release" is the shipped default: rustc -O (integer
# overflow wraps). "fuzz" is the profile FLOURINE's own check builds the Rust in: `cargo bolero test`
# (oracle.py:196) uses cargo profile "fuzz" (cargo-bolero 0.10 default), defined in FLOURINE's
# Cargo.toml template (Differential_Tester/instrumentor/src/template.rs:37-41) as inherits = "dev"
# (debug-assertions and overflow-checks on), opt-level = 3, incremental = false, codegen-units = 1;
# cargo-bolero also adds -Cdebug-assertions -Coverflow_checks -Cdebuginfo=2 to RUSTFLAGS. Integer
# overflow panics. (Fuzzer instrumentation flags and -Ctarget-cpu=native are left out.)
RUSTC_PROFILE_FLAGS = {
    "release": ["-O"],
    "fuzz": ["-C", "opt-level=3", "-C", "debug-assertions=on", "-C", "overflow-checks=on",
             "-C", "codegen-units=1", "-C", "debuginfo=2"],
}
RUST_PROFILE = "release"


def parse_rust_fn(text, name):
    m = re.search(r"\bfn\s+" + re.escape(name) + r"\s*\(([^)]*)\)\s*(->\s*([^\{]+?))?\s*\{", text, re.S)
    if not m:
        return None
    params = m.group(1).strip()
    ret = (m.group(3) or "()").strip()
    ptypes = []
    if params:
        for p in params.split(","):
            if ":" not in p:
                return None
            ptypes.append(p.split(":", 1)[1].strip())
    return {"ptypes": ptypes, "ret": ret}


def ok_sig(sig):
    if not sig or not sig["ptypes"]:
        return False
    types = sig["ptypes"] + ([sig["ret"]] if sig["ret"] != "()" else [])
    return all(t in CTYPE for t in types) and enum_supported(sig["ptypes"])


def build_c(name, cfile, sig, d):
    src = re.sub(r"\bint\s+main\s*\(", "int _disabled_main(", open(cfile, errors="replace").read())
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
   printf("{rfmt}\\n",{name}({call})); fflush(stdout);
 }} return 0; }}
"""
    p = os.path.join(d, "p.c"); open(p, "w").write(src + "\n" + main)
    b = os.path.join(d, "cb")
    r = subprocess.run(["gcc", "-w", "-O1", "-fwrapv", p, "-o", b],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return b if (r.returncode == 0 and os.path.isfile(b)) else None


def build_rust(name, rsfile, sig, d, profile=None):
    src = re.sub(r"\bfn\s+main\s*\(", "fn _disabled_main(", open(rsfile, errors="replace").read())
    k = len(sig["ptypes"])
    reads = "\n".join(f"  let a{i}:{sig['ptypes'][i]}=t[{i}].parse().unwrap_or(0);" for i in range(k))
    call = ", ".join(f"a{i}" for i in range(k))
    body = ("println!(\"{}\", %s(%s) as i64);" % (name, call)) if sig["ret"] == "bool" \
        else ("println!(\"{}\", %s(%s));" % (name, call))
    main = f"""
use std::io::BufRead;
fn main(){{ std::panic::set_hook(Box::new(|_|{{}}));
 let s=std::io::stdin();
 for line in s.lock().lines(){{ let line=match line{{Ok(l)=>l,Err(_)=>break}};
   let t:Vec<String>=line.split_whitespace().map(|x|x.to_string()).collect();
   if t.len()<{k}{{println!("__PARSE__");continue;}}
{reads}
   let r=std::panic::catch_unwind(std::panic::AssertUnwindSafe(||{{ {body} }}));
   if r.is_err(){{println!("__PANIC__");}}
 }} }}
"""
    p = os.path.join(d, "p.rs"); open(p, "w").write(src + "\n" + main)
    b = os.path.join(d, "rb")
    r = subprocess.run(["rustc"] + RUSTC_PROFILE_FLAGS[profile or RUST_PROFILE] + ["-A", "warnings", p, "-o", b],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return b if (r.returncode == 0 and os.path.isfile(b)) else None


def main():
    global RUST_PROFILE
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=int, default=2000)
    ap.add_argument("--out", default=os.path.join(REPO, "results", "flourine_diff.json"))
    ap.add_argument("--profile", choices=sorted(RUSTC_PROFILE_FLAGS), default="release",
                    help="Rust build profile (default release = rustc -O, as shipped; fuzz = FLOURINE's check profile)")
    args = ap.parse_args()
    RUST_PROFILE = args.profile

    agg = {"benchmarks_scalar": 0, "success_tested": 0, "success_agree": 0,
           "success_value_bug": 0, "success_crash": 0,
           "fail_tested": 0, "fail_diverge": 0, "fail_agree": 0, "build_fail": 0}
    bug_benches, succ_div = set(), []

    for cfile in sorted(glob.glob(os.path.join(BMS, "*", "*", "*.c"))):
        name = os.path.basename(cfile)[:-2]
        proj = cfile.split("/bms/c/")[1].split("/")[0]
        anyrs = glob.glob(os.path.join(FL, "translations_only", "*", "c", proj, name, "*", "*", name + ".rs"))
        if not anyrs:
            continue
        sig = parse_rust_fn(open(anyrs[0], errors="replace").read(), name)
        if not ok_sig(sig):
            continue
        agg["benchmarks_scalar"] += 1
        cases = enumerate_cases(sig["ptypes"], cap=args.cap)
        with tempfile.TemporaryDirectory() as d:
            binc = build_c(name, cfile, sig, d)
            if not binc:
                agg["build_fail"] += 1
                continue
            for verdict in ("success", "fail"):
                for rs in glob.glob(os.path.join(FL, "translations_only", verdict, "c", proj, name, "*", "*", name + ".rs")):
                    s2 = parse_rust_fn(open(rs, errors="replace").read(), name)
                    if not ok_sig(s2):
                        continue
                    with tempfile.TemporaryDirectory() as d2:
                        binr = build_rust(name, rs, s2, d2)
                        if not binr:
                            continue
                        t = tally(binc, binr, cases)
                        tag = rs.split("/c/")[1]
                        if verdict == "success":
                            agg["success_tested"] += 1
                            if t["value"] > 0:
                                agg["success_value_bug"] += 1; bug_benches.add(name)
                                succ_div.append({"t": tag, "value": t["value"], "samples": t["value_samples"]})
                            elif t["rust_crash"] > 0:
                                agg["success_crash"] += 1
                            else:
                                agg["success_agree"] += 1
                        else:
                            agg["fail_tested"] += 1
                            if t["value"] > 0 or t["rust_crash"] > 0:
                                agg["fail_diverge"] += 1
                            else:
                                agg["fail_agree"] += 1

    summary = {"system": "Flourine", "generation": "deterministic enumeration", **agg,
               "success_but_divergent_benchmarks": sorted(bug_benches)}
    if RUST_PROFILE != "release":
        summary["rust_profile"] = {"name": RUST_PROFILE, "rustc_flags": RUSTC_PROFILE_FLAGS[RUST_PROFILE]}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump({"summary": summary, "success_divergences": succ_div}, open(args.out, "w"), indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
