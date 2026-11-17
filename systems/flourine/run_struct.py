"""Struct-input differential tester for Flourine's OPL register-accessor family.

Flourine's scalar run.py reaches only the 18/112 benchmarks whose arguments are plain
integers. The large remainder take *struct pointers* -- which Flourine handles with its
own JSON-FFI harness. The biggest self-contained slice of that remainder is the OPL
"register accessor" family: ~15 functions that take `struct opl_emu_registers *` (whose
payload is a flat `uint8_t m_regdata[0x200]` byte array) plus at most one scalar channel
offset, and return a bitfield pulled out of that array.

We drive them differentially WITHOUT any cross-language memory layout assumption: each
side constructs its OWN struct natively, we fill `m_regdata` with the SAME deterministic
bytes on both sides (over several seeds), sweep the scalar offset over a bounded grid, and
compare the returned value. A divergence is a real translation bug in the offset math /
bitfield extraction. We keep inputs IN-BOUNDS so neither side executes UB: the register fill is a
fixed `m_regdata[0x200]` and the offset grid is capped (see `offset_grid`) so no read runs past the
array -- this harness does not build a sanitizer oracle (unlike the VERT harness); it avoids UB by
construction instead. Rust panics (if any) are reported separately from wrong answers.

We test SUCCESS translations (Flourine's fuzzer declared them equivalent -> a divergence is
oracle-fitting) and FAIL translations (reach check -- many "fail" are just Oracle-OOS).

  docker run --rm -v "$PWD:/work" -w /work crust-stable python3 systems/flourine/run_struct.py

Self-test (proves the harness can go red):
  ... python3 systems/flourine/run_struct.py --selftest
"""
import argparse, glob, json, os, re, subprocess, tempfile, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
FL = os.path.join(REPO, "artifacts", "flourine_dl", "flourine-artifact")
BMS = os.path.join(FL, "bms", "c", "opl")
TR = os.path.join(FL, "translations_only")

# ---- the register-accessor family: struct ptr (+ optional scalar), scalar return.
# We discover them automatically, but gate on this shape to stay faithful:
#   * first Rust param type mentions OplEmuRegisters (by value-ref or &Box)
#   * remaining params (if any) are a single scalar u32/i32 (the channel offset)
#   * return type is a scalar integer
#   * the translation body references m_regdata (so our fill reaches it)
SCALAR = {"u8", "u16", "u32", "u64", "i8", "i16", "i32", "i64", "usize", "isize", "c_int", "c_uint"}
RET_C_FMT = "%lld"   # we print every return widened to long long on both sides

# rustc flags per build profile (--profile); same table as systems/flourine/run.py. "release" is the
# shipped default (rustc -O, overflow wraps); "fuzz" is FLOURINE's check profile (cargo bolero test
# --profile fuzz: inherits "dev", opt-level 3, codegen-units 1, debug assertions and overflow checks on).
RUSTC_PROFILE_FLAGS = {
    "release": ["-O"],
    "fuzz": ["-C", "opt-level=3", "-C", "debug-assertions=on", "-C", "overflow-checks=on",
             "-C", "codegen-units=1", "-C", "debuginfo=2"],
}
RUST_PROFILE = "release"


def rust_sig(text, name):
    m = re.search(r"\bfn\s+" + re.escape(name) + r"\s*\(([^)]*)\)\s*(?:->\s*([^\{]+?))?\s*\{", text, re.S)
    if not m:
        return None
    params = [p.strip() for p in m.group(1).split(",") if p.strip()]
    ret = (m.group(2) or "()").strip()
    return {"params": params, "ret": ret}


def classify_regfn(text, name):
    """Return (recv_kind, extra_scalar_type|None, ret) if this is a drivable register
    accessor, else None. recv_kind in {'ref','refmut','refbox'}."""
    sig = rust_sig(text, name)
    if not sig or not sig["params"]:
        return None
    if "m_regdata" not in text:
        return None
    p0 = sig["params"][0]
    if "OplEmuRegisters" not in p0:
        return None
    if "&self" in p0:
        return None
    # receiver form
    if re.search(r":\s*&\s*Box\s*<", p0):
        recv = "refbox"
    elif re.search(r":\s*&\s*mut\b", p0):
        recv = "refmut"
    elif re.search(r":\s*&", p0):
        recv = "ref"
    else:
        return None
    # remaining params: allow zero, or exactly one scalar
    rest = sig["params"][1:]
    extra = None
    if len(rest) == 1:
        t = rest[0].split(":", 1)[1].strip() if ":" in rest[0] else ""
        if t not in SCALAR:
            return None
        extra = t
    elif len(rest) > 1:
        return None
    # scalar return only (exclude (), out-params, struct returns)
    base_ret = sig["ret"].replace("mut", "").strip()
    if base_ret not in SCALAR:
        return None
    return {"recv": recv, "extra": extra, "ret": base_ret}


def reg_struct_name(text):
    m = re.search(r"struct\s+([A-Za-z0-9_]*OplEmuRegisters[A-Za-z0-9_]*)\b", text)
    return m.group(1) if m else "OplEmuRegisters"


# --- robust struct construction -------------------------------------------------
# LLM translations represent the register struct many ways: inline arrays
# (`[u8; 0x200]`), boxed arrays (`Box<[u8; 0x200]>`), or `Vec<u8>`. We can't use
# `mem::zeroed()` (a null Box/Vec is invalid -> the binary dies with no output,
# which would masquerade as a value divergence). Instead we PARSE the struct's
# fields and emit a correct zero-initializer per field type, so every drivable
# translation actually runs. A struct we cannot construct is skipped (reported as
# not-drivable), never counted as a bug.
SCALAR_INTS = {"u8", "u16", "u32", "u64", "u128", "i8", "i16", "i32", "i64", "i128",
               "usize", "isize"}


def _split_array(t):
    """'[u8; 0x200]' -> ('u8','0x200'); '[[u16; 0x400]; 8]' -> ('[u16; 0x400]','8')."""
    inner = t[1:-1]
    depth = 0
    for i in range(len(inner) - 1, -1, -1):
        c = inner[i]
        if c == "]":
            depth += 1
        elif c == "[":
            depth -= 1
        elif c == ";" and depth == 0:
            return inner[:i].strip(), inner[i + 1:].strip()
    return None, None


def zero_init(t, field=None):
    """Return a Rust expression that zero-initializes type `t`, or None if we can't."""
    t = t.strip()
    if t in SCALAR_INTS:
        return "0"
    if t in ("f32", "f64"):
        return "0.0"
    if t == "bool":
        return "false"
    if t.startswith("Box<") and t.endswith(">"):
        inner = zero_init(t[4:-1], field)
        return f"Box::new({inner})" if inner else None
    if t.startswith("Vec<") and t.endswith(">"):
        # a Vec needs real length to be indexable; only m_regdata must be indexable
        if field == "m_regdata":
            return "vec![0u8; 0x200]"
        return "Vec::new()"
    if t.startswith("[") and t.endswith("]"):
        inner, n = _split_array(t)
        if inner is None:
            return None
        iz = zero_init(inner, field)
        return f"[{iz}; {n}]" if iz else None
    return None   # references, raw pointers, unknown -> not constructible here


def struct_initializer(text, sname):
    """Build `SName { field: <zero>, ... }` for the named struct, or None."""
    m = re.search(r"struct\s+" + re.escape(sname) + r"\s*\{(.*?)\n\}", text, re.S)
    if not m:
        m = re.search(r"struct\s+" + re.escape(sname) + r"\s*\{(.*?)\}", text, re.S)
        if not m:
            return None
    body = m.group(1)
    # strip // comments
    body = re.sub(r"//[^\n]*", "", body)
    fields = []
    for fm in re.finditer(r"(?:pub\s+)?([A-Za-z_]\w*)\s*:\s*([^,]+?)\s*(?:,|$)", body):
        fname, ftype = fm.group(1), fm.group(2).strip()
        iz = zero_init(ftype, fname)
        if iz is None:
            return None
        fields.append(f"{fname}: {iz}")
    if not fields:
        return None
    return sname + " { " + ", ".join(fields) + " }"


# deterministic register fills (same bytes both sides), several seeds
SEEDS = list(range(8))

def offset_grid(has_extra):
    if not has_extra:
        return [0]            # unused; one call per seed
    # Channel-offset args are added to a base register index (up to ~0xC0) before the
    # read, so we cap at 0x30 to keep every access inside m_regdata[0x200]. Larger
    # offsets make the C read adjacent struct memory (UB) and the Rust panic (OOB) --
    # a comparison of UB vs panic, not a real divergence -- so we stay in-domain.
    return sorted(set(list(range(0, 0x20)) + [0x20, 0x28, 0x30]))


def build_c(cfile, name, info, d, fill_mut=False):
    src = re.sub(r"\bint\s+main\s*\(", "int _disabled_main(", open(cfile, errors="replace").read())
    callargs = "&r" + (", (uint32_t)off" if info["extra"] else "")
    fill = "(uint8_t)((i*131u + seed*197u + 17u) & 0xff)"
    if fill_mut:  # self-test: perturb the C fill so the two sides must diverge
        fill = "(uint8_t)((i*131u + seed*197u + 18u) & 0xff)"
    main = f"""
#include <stdio.h>
#include <string.h>
int main(void){{
  char line[64];
  while(fgets(line,sizeof line,stdin)){{
    long long seed=0, off=0;
    if(sscanf(line,"%lld %lld",&seed,&off)<1){{printf("__PARSE__\\n");continue;}}
    struct opl_emu_registers r; memset(&r,0,sizeof r);
    for(int i=0;i<0x200;i++) r.m_regdata[i]={fill};
    printf("{RET_C_FMT}\\n",(long long)({name}({callargs})));
    fflush(stdout);
  }}
  return 0;
}}
"""
    p = os.path.join(d, "p.c"); open(p, "w").write(src + "\n" + main)
    b = os.path.join(d, "cb")
    r = subprocess.run(["gcc", "-w", "-O1", "-fwrapv", p, "-o", b],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return b if (r.returncode == 0 and os.path.isfile(b)) else None


def build_rust(rsfile, name, info, d, profile=None):
    """Returns (binpath, status). status in {'ok','no_construct','build_fail'}."""
    src = re.sub(r"\bfn\s+main\s*\(", "fn _disabled_main(", open(rsfile, errors="replace").read())
    sname = reg_struct_name(src)
    init = struct_initializer(src, sname)
    if init is None:
        return None, "no_construct"
    recv = {"ref": "&*regs", "refmut": "&mut *regs", "refbox": "&regs"}[info["recv"]]
    callargs = recv + (", off as u32" if info["extra"] else "")
    main = f"""
use std::io::BufRead;
fn main(){{ std::panic::set_hook(Box::new(|_|{{}}));
 let s=std::io::stdin();
 for line in s.lock().lines(){{
   let line=match line{{Ok(l)=>l,Err(_)=>break}};
   let t:Vec<i64>=line.split_whitespace().filter_map(|x|x.parse().ok()).collect();
   if t.is_empty(){{println!("__PARSE__");continue;}}
   let seed:u64 = t[0] as u64;
   let off:i64 = if t.len()>1 {{t[1]}} else {{0}};
   let _ = off;
   let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(||{{
     let mut regs: Box<{sname}> = Box::new({init});
     for i in 0..0x200u32 {{ regs.m_regdata[i as usize] = ((i as u64)*131 + seed*197 + 17) as u8; }}
     {name}({callargs}) as i64
   }}));
   match r {{ Ok(v)=>println!("{{}}",v), Err(_)=>println!("__PANIC__") }}
   use std::io::Write; let _ = std::io::stdout().flush();
 }}
}}
"""
    p = os.path.join(d, "p.rs"); open(p, "w").write(src + "\n" + main)
    b = os.path.join(d, "rb")
    r = subprocess.run(["rustc"] + RUSTC_PROFILE_FLAGS[profile or RUST_PROFILE] + ["-A", "warnings", p, "-o", b],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode == 0 and os.path.isfile(b):
        return b, "ok"
    return None, "build_fail"


def feed(info):
    lines = []
    offs = offset_grid(info["extra"])
    for seed in SEEDS:
        for off in offs:
            lines.append(f"{seed} {off}")
    return "\n".join(lines) + "\n"


def run_pair(binc, binr, info):
    feed_str = feed(info)
    inp = feed_str.encode()
    in_lines = [l for l in feed_str.split("\n") if l.strip()]
    oc = subprocess.run([binc], input=inp, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
    orr = subprocess.run([binr], input=inp, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
    co = [l.strip() for l in oc.stdout.decode(errors="replace").split("\n") if l.strip() != ""]
    ro = [l.strip() for l in orr.stdout.decode(errors="replace").split("\n") if l.strip() != ""]
    value = crash = agree = 0
    samples = []
    expected = len(in_lines)
    # If Rust produced fewer real lines than C, the missing ones are a RUN failure
    # (crash / no output), NOT a value divergence. This is the discipline that keeps
    # a struct we couldn't construct, or a translation that aborts, out of the bug count.
    for i in range(expected):
        a = co[i] if i < len(co) else None
        b = ro[i] if i < len(ro) else None
        if a is None:
            continue                       # C itself didn't emit -> not assessable
        if b is None or b == "__PANIC__":
            crash += 1
            continue
        if a in ("__PARSE__",) or b == "__PARSE__":
            continue
        if a == b:
            agree += 1
        else:
            value += 1
            if len(samples) < 4:
                samples.append({"line_in": in_lines[i] if i < len(in_lines) else "?",
                                "c": a, "rust": b})
    return {"agree": agree, "value": value, "crash": crash, "samples": samples}


def discover():
    """Yield (name, cfile, {verdict: [rsfiles]}, info) for drivable register accessors."""
    out = []
    for name in sorted(os.listdir(os.path.join(TR, "success", "c", "opl"))):
        cfile = os.path.join(BMS, name, name + ".c")
        if not os.path.isfile(cfile):
            continue
        # find any success translation to classify the shape
        succ = sorted(glob.glob(os.path.join(TR, "success", "c", "opl", name, "*", "*", name + ".rs")))
        info = None
        for rs in succ:
            info = classify_regfn(open(rs, errors="replace").read(), name)
            if info:
                break
        if not info:
            continue
        verds = {}
        for v in ("success", "fail"):
            verds[v] = sorted(glob.glob(os.path.join(TR, v, "c", "opl", name, "*", "*", name + ".rs")))
        out.append((name, cfile, verds, info))
    return out


def selftest():
    """Inject a one-byte difference into the C register fill and confirm the harness
    reports value divergences on an otherwise-agreeing function."""
    targets = discover()
    for name, cfile, verds, info in targets:
        rs = verds["success"][0] if verds["success"] else None
        if not rs:
            continue
        with tempfile.TemporaryDirectory() as d:
            binc = build_c(cfile, name, info, d, fill_mut=True)   # perturbed C
            binr, _st = build_rust(rs, name, info, d)
            if not (binc and binr):
                continue
            t = run_pair(binc, binr, info)
            ok = t["value"] > 0
            print(json.dumps({"selftest_fn": name, "divergences_detected": t["value"],
                              "harness_can_go_red": ok}, indent=2))
            sys.exit(0 if ok else 3)
    print(json.dumps({"selftest": "no drivable function found"}))
    sys.exit(3)


def main():
    global RUST_PROFILE
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--out", default=os.path.join(REPO, "results", "flourine_struct.json"))
    ap.add_argument("--profile", choices=sorted(RUSTC_PROFILE_FLAGS), default="release",
                    help="Rust build profile (default release = rustc -O, as shipped; fuzz = FLOURINE's check profile)")
    args = ap.parse_args()
    RUST_PROFILE = args.profile
    if args.selftest:
        selftest()
        return

    targets = discover()
    agg = {"family": "opl register accessors (struct opl_emu_registers*)",
           "drivable_functions": len(targets),
           "success_tested": 0, "success_agree": 0, "success_value_bug": 0, "success_crash": 0,
           "fail_tested": 0, "fail_agree": 0, "fail_diverge": 0, "build_fail": 0,
           "no_construct": 0, "rust_build_fail": 0}
    bug_fns, succ_div, fn_list = set(), [], []
    for name, cfile, verds, info in targets:
        fn_list.append({"fn": name, "extra_arg": info["extra"], "ret": info["ret"], "recv": info["recv"]})
        with tempfile.TemporaryDirectory() as d:
            binc = build_c(cfile, name, info, d)
            if not binc:
                agg["build_fail"] += 1
                continue
            for v in ("success", "fail"):
                for rs in verds[v]:
                    s2 = classify_regfn(open(rs, errors="replace").read(), name)
                    if not s2:
                        continue
                    with tempfile.TemporaryDirectory() as d2:
                        binr, st = build_rust(rs, name, s2, d2)
                        if not binr:
                            agg["no_construct" if st == "no_construct" else "rust_build_fail"] += 1
                            continue
                        t = run_pair(binc, binr, s2)
                        tag = rs.split("/opl/")[1]
                        if v == "success":
                            agg["success_tested"] += 1
                            if t["value"] > 0:
                                agg["success_value_bug"] += 1; bug_fns.add(name)
                                succ_div.append({"t": tag, "value": t["value"], "samples": t["samples"]})
                            elif t["crash"] > 0:
                                agg["success_crash"] += 1
                            else:
                                agg["success_agree"] += 1
                        else:
                            agg["fail_tested"] += 1
                            if t["value"] > 0 or t["crash"] > 0:
                                agg["fail_diverge"] += 1
                            else:
                                agg["fail_agree"] += 1
    summary = {"system": "Flourine", "scope": "struct subset (OPL register accessors)",
               "generation": "deterministic register fills x bounded offset grid", **agg,
               "success_but_divergent_functions": sorted(bug_fns)}
    if RUST_PROFILE != "release":
        summary["rust_profile"] = {"name": RUST_PROFILE, "rustc_flags": RUSTC_PROFILE_FLAGS[RUST_PROFILE]}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump({"summary": summary, "function_list": fn_list, "success_divergences": succ_div},
              open(args.out, "w"), indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
