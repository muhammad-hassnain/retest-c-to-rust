"""Flourine (not in the task's harness list; checked because both of its counted S1 rows involve
wrapping): FLOURINE-lfo_pm_depth-1 (`(1u32 >> length) - 1` underflows for length=1) and
FLOURINE-operator_list-1 (`wrapping_shl` on u8). The shipped translations are compiled as-is, with a
two-line `main` appended, under rustc release (-O) and debug (default). C reference values are
computed by the same formula in C (gcc) from the artifact's C: lfo = bit 6 of regdata[0xbd];
operator_list = o1|o2<<8|o3<<16|o4<<24.

  docker run --rm -v $PWD:/work:ro -w /work crust-stable python3 systems/profile_check/flourine_profile.py OUT.json
"""
import json, os, subprocess, sys, tempfile
ART = "/work/artifacts/flourine_dl/flourine-artifact/translations_only/success/c/opl"
cases = {
 "FLOURINE-lfo_pm_depth-1": (ART + "/opl_emu_registers_lfo_pm_depth/claude3/BaseRepair/opl_emu_registers_lfo_pm_depth.rs",
   'fn main(){ let mut r=OplEmuRegisters{m_lfo_am_counter:0,m_lfo_pm_counter:0,m_noise_lfsr:0,m_lfo_am:0,m_regdata:Box::new([0u8;0x200]),m_waveform:Box::new([[0u16;0x400];8])};'
   ' r.m_regdata[0xbd]=0xFF; println!("{}", opl_emu_registers_lfo_pm_depth(&r)); }', "1"),
 "FLOURINE-operator_list-1": (ART + "/opl_emu_registers_operator_list/claude2/CAPR/opl_emu_registers_operator_list.rs",
   'fn main(){ println!("{}", opl_emu_registers_operator_list(24,211,56,46)); }', str(24 | 211 << 8 | 56 << 16 | 46 << 24)),
}
out = {"rustc": subprocess.run(["rustc", "--version"], capture_output=True, text=True).stdout.strip(), "rows": {}}
for k, (path, main, cval) in cases.items():
    row = {"c_value": cval}
    for prof, flags in (("release", ["-O"]), ("debug", [])):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "t.rs"); open(src, "w").write(open(path).read() + "\n" + main + "\n")
            b = os.path.join(d, "t")
            c = subprocess.run(["rustc", "-A", "warnings"] + flags + [src, "-o", b], capture_output=True, text=True)
            if c.returncode:
                row[prof] = {"error": c.stderr[-300:]}; continue
            r = subprocess.run([b], capture_output=True, text=True)
            o = r.stdout.strip()
            row[prof] = {"rc": r.returncode, "stdout": o, "stderr": r.stderr.strip()[:160],
                         "outcome": "crash" if r.returncode else ("agrees" if o == cval else "wrong answer")}
    out["rows"][k] = row
json.dump(out, open(sys.argv[1], "w"), indent=1); print(json.dumps(out))
