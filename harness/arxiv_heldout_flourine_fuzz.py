"""arXiv: held-out status of the FLOURINE defects that FLOURINE's own build profile shows.

harness/heldout_flourine.py (results/heldout_flourine.json) measured the counted FLOURINE files with the
Rust built by rustc -O. In FLOURINE's fuzz profile (opt-level 3, overflow checks on; systems/flourine/run.py
RUSTC_PROFILE_FLAGS["fuzz"], results/arxiv_flourine_devprofile.json) lfo_pm_depth panics instead of
returning a wrong value, and three more shipped success files panic on UB-clean input: lfo_am_depth
(claude3/CAPR) and compute_phase_step (gpt4/BaseRepair, gpt4/CAPR). This script runs heldout_flourine's own
per-translation measurement for those four files with the Rust built in the fuzz profile:

  * identity of the shipped file against FLOURINE's logged candidates (sha256; heldout_flourine.one);
  * the inputs FLOURINE's counterexample prompts record (prog_*.ans) and their exact overlap with our grid;
  * scalar files: our comparison of the shipped file (fuzz profile) on our grid and on the logged inputs,
    every divergence re-checked against the ISO-C sanitizer build (gcc -O0 ASan+UBSan, no -fwrapv);
  * struct accessors: the field-by-field overlap of heldout_flourine.struct_overlap (the logs truncate the
    register file, so our comparison cannot run on the logged inputs); the reason text is rewritten for
    the fuzz profile from the logged examples.

Nothing from the logs archive is compiled or executed; only the shipped translation and the C oracle are
built, exactly as systems/flourine/run.py builds them (Rust: --profile fuzz flags).

  docker run --rm --cpus 4 -v "$PWD:/work" -w /work -e FL_LOGS=/work/flourine_logs/extract \\
      crust-syzygy python3 -I harness/arxiv_heldout_flourine_fuzz.py
Writes results/arxiv_heldout_flourine_fuzzprofile.json (OUT overrides).
"""
import datetime
import json
import os
import platform
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "scripts"))
import heldout_flourine as H  # noqa: E402  (also puts systems/ and systems/flourine/ on sys.path)
from provenance import stamp  # noqa: E402

PROFILE = "fuzz"
OUT = os.environ.get("OUT", os.path.join(REPO, "results", "arxiv_heldout_flourine_fuzzprofile.json"))
ROWS = [("opl", "opl_emu_registers_lfo_pm_depth", "claude3", "BaseRepair", "struct mask (counted; crash in the fuzz profile)"),
        ("opl", "opl_emu_registers_lfo_am_depth", "claude3", "CAPR", "struct mask (counted; crash in the fuzz profile only)"),
        ("opl", "opl_emu_opl_compute_phase_step", "gpt4", "BaseRepair", "u32 multiply overflow (counted; crash in the fuzz profile only)"),
        ("opl", "opl_emu_opl_compute_phase_step", "gpt4", "CAPR", "u32 multiply overflow (counted; crash in the fuzz profile only)")]


def struct_reason(row):
    ex = row.get("logged_examples") or []
    crash = [e for e in ex if (e.get("actual") or "").strip() == "Runtime crash"]
    exp = sorted({(e.get("expected") or "").strip() for e in crash})
    per = row.get("logged_inputs_per_example") or []
    rec = min((x.get("m_regdata_bytes_recorded") or 0) for x in per) if per else 0
    br = next((x.get("byte_read_by_function") for x in per if x.get("byte_read_by_function") is not None), None)
    return ("not computable: every logged example records only the first "
            f"{rec} of 512 m_regdata bytes and the function reads byte {hex(br) if br is not None else '?'}, so our comparison "
            f"cannot be run on the logged inputs. FLOURINE's own log reports 'Runtime crash' (expected {exp}) for the shipped "
            f"content on {len(crash)} of its {len(ex)} logged examples; in the fuzz profile the shipped file panics on every call "
            "(`(1u32.shr(length)) - 1` with length 1 overflows whatever the register contents), which is that crash.")


def main():
    H.frun.RUST_PROFILE = PROFILE           # systems/flourine/run.py build_rust default profile
    rows = []
    for c in ROWS:
        r = H.one(*c)
        if c[1] in H.STRUCT_ACCESSORS:
            r["shipped_diverges_on_logged_inputs_reason"] = struct_reason(r)
        rows.append(r)
        print(r["translation"], {k: r.get(k) for k in ("shipped_divergent_iso_c_clean", "logged_inputs", "overlap_ours_logged",
              "shipped_diverges_on_logged_inputs_iso_c_clean", "counted_divergences_outside_logged_inputs")},
              r["identity"]["shipped_equals_first_candidate_prog_0"], r["identity"]["shipped_equals_verified_candidate"], flush=True)

    def ver(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True).stdout.splitlines()[0]
        except Exception:  # noqa: BLE001
            return None
    shipped = json.load(open(os.path.join(REPO, "results", "heldout_flourine.json")))["provenance"]
    out = {
        "provenance": {
            "script": "harness/arxiv_heldout_flourine_fuzz.py",
            "command": "docker run --rm --cpus 4 -v \"$PWD:/work\" -w /work -e FL_LOGS=/work/flourine_logs/extract "
                       "crust-syzygy python3 -I harness/arxiv_heldout_flourine_fuzz.py",
            "git_rev": os.environ.get("GIT_REV"), "host": os.environ.get("HOST_DESC", platform.system()),
            "arch": platform.machine(), "image": os.environ.get("IMAGE_DESC"),
            "toolchains": {"gcc": ver(["gcc", "--version"]), "rustc": ver(["rustc", "--version"])},
            "date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "logs_archive": dict(shipped.get("logs_archive") or {},
                                 members_extracted="translations/success/c/<proj>/<fn>/<llm>/<strategy>/ for every success row "
                                                   "(the extraction results/flourine_shipped_vs_log.json reads)"),
            "grid": shipped.get("grid"),
            "rust_profile": {"name": PROFILE, "rustc_flags": H.frun.RUSTC_PROFILE_FLAGS[PROFILE],
                             "why": "FLOURINE's check builds with cargo-bolero's fuzz profile (results/arxiv_flourine_devprofile.json flourine_check_profile)"},
            "same_method_as": "results/heldout_flourine.json (harness/heldout_flourine.py one(), struct_overlap())",
        },
        "translations": rows,
        "_provenance": stamp(image=os.environ.get("IMAGE_DESC")),
    }
    json.dump(out, open(OUT, "w"), indent=1)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
