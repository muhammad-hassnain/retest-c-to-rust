"""Per-input re-run of the scalar differential (systems/vert/run.py, unchanged helpers) on EXACTLY the
current 25 VERT scalar wrong answers (results/vert_diff.json value_bugs), keeping EVERY input's
verdict instead of the 3 smallest samples, to test the paper's sentence
  "Each counted wrong answer also diverges outside {0,1}, the harness's only inputs."
(VERT's bounded-Kani harness draws only PARAM1, with kani::assume((0..2).contains(&PARAM1)).)

Same grid as the current vert_diff.json (spread grid, n=80, non-negative, |x|<=512) and the same
classifier (batch_diff.tally), called once per input so each input's class is recorded. Two
UB-filter configurations, both on this host:
  primary_like: ASan/UBSan + the -O0/-O1 cross-check (what vert_diff.json records: san, alt, no msan)
  msan_like   : ASan/UBSan + clang MemorySanitizer (the x86-64 re-run's configuration)
A divergence is "confirmed" when it is a value divergence under both.
Run from the repo root inside the crust-stable image:
  docker run --rm -v "$PWD:/work" -w /work crust-stable python3 systems/vert/zero_one_current.py OUT.json
"""
import json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import run as R  # noqa: E402  (systems/vert/run.py: extract/build helpers)
from enumerate_inputs import enumerate_cases  # noqa: E402
from batch_diff import tally  # noqa: E402

OUT = sys.argv[1]
vd = json.load(open("results/vert_diff.json"))
rows = []
for v in vd["value_bugs"]:
    f = v["func"]
    rust, argtypes = R.extract_llm_rust(f)
    csrc = R.c_source(f)
    nargs = len(argtypes)
    with tempfile.TemporaryDirectory() as d:
        binc = R.build_c(f, csrc, nargs, d)
        binr = R.build_rust(rust, nargs, d)
        san = R.build_c(f, csrc, nargs, d, mode="san")
        msan = R.build_c(f, csrc, nargs, d, mode="msan")
        alt = None if R.c_uses_float(csrc) else R.build_c(f, csrc, nargs, d, mode="alt")
        cases = enumerate_cases(["i32"] * nargs, cap=80, max_abs=512, nonneg=True, legacy=False)
        batch_p = tally(binc, binr, cases, binc_san=san, binc_alt=alt, binc_msan=None)
        batch_m = tally(binc, binr, cases, binc_san=san, binc_alt=None, binc_msan=msan)
        per = []
        for c in cases:
            tp = tally(binc, binr, [c], binc_san=san, binc_alt=alt, binc_msan=None)
            tm = tally(binc, binr, [c], binc_san=san, binc_alt=None, binc_msan=msan)
            cls = lambda t: next((k for k in ("value", "rust_panic", "ub", "timeout", "rust_hang", "agree") if t.get(k)), "skip")
            s = (tp["value_samples"] or tm["value_samples"] or [{}])[0]
            per.append({"in": list(c), "primary_like": cls(tp), "msan_like": cls(tm),
                        "c": s.get("c"), "rust": s.get("rust")})
    rows.append({"function": f, "nargs": nargs, "cases": len(cases),
                 "builds": {"c": bool(binc), "rust": bool(binr), "san": bool(san), "msan": bool(msan), "alt": bool(alt)},
                 "batch_value_primary_like": batch_p["value"], "batch_value_msan_like": batch_m["value"],
                 "vert_diff_value_div": v["value_div"], "per_input": per})
    print(f, nargs, "batch p/m", batch_p["value"], batch_m["value"], "vert_diff", v["value_div"],
          "per-input p/m", sum(x["primary_like"] == "value" for x in per), sum(x["msan_like"] == "value" for x in per), flush=True)
json.dump({"rows": rows}, open(OUT, "w"), indent=1)
