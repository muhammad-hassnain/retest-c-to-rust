"""Direct A/B evidence for the C2SaferRust in-distribution ablation triage.

The ablation (harness/ablation.py) runs C2SaferRust's own coreutils repair tests and reports 12/38
failing at the TEST-SCRIPT level. A test-script failure is not yet a translation defect: coreutils
`tail -f`/pipe/sysfs tests are environment-flaky, and `split` tests accumulate files across steps.
This script reproduces the DECISIVE sub-cases by running the shipped C and Rust binaries DIRECTLY on
the same inputs (no test framework), so the one confirmed semantic defect (`head -n -N` shared-fd
positioning) and the non-reproduction of the `split` failures are backed by a committed artifact
rather than prose. Emits results/c2saferrust_triage_evidence.json.

  docker run --rm -v "$PWD:/work" -w /work -e C2SR_ROOT=/work/artifacts/c2saferrust \
      crust-heldout python3 harness/verify_ablation_triage.py
"""
import json
import os
import subprocess
import sys
import tempfile

from build import build_oracle, build_sut


def run(argv_shell, stdin_path=None, cwd=None):
    """Run a bash -c command, return (rc, stdout, stderr)."""
    f = open(stdin_path, "rb") if stdin_path else None
    try:
        p = subprocess.run(["bash", "-c", argv_shell], stdin=f, cwd=cwd,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        return p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace")
    finally:
        if f:
            f.close()


def head_pos_check(c_bin, r_bin, total_lines=70000, ns=(5000, 50000, 69000)):
    """Triage claim, swept over several N: `(head -n -N >/dev/null; wc -l) < seq(total)` gives C=N,
    R=0. head and wc share the redirected stdin fd; a correct `head -n -N` prints all but the last N
    lines and leaves the fd positioned so the next reader (wc) sees exactly those last N lines. Rust
    mis-positions the shared fd -> wc sees 0, at every magnitude."""
    d = tempfile.mkdtemp()
    seqf = os.path.join(d, f"seq{total_lines}")
    with open(seqf, "w") as fh:
        fh.write("".join(f"{i}\n" for i in range(1, total_lines + 1)))
    sweep = []
    for n in ns:
        row = {"n": n, "expected_c": n, "expected_rust_if_correct": n}
        for label, b in (("c", c_bin), ("rust", r_bin)):
            rc, so, se = run(f'( "{b}" -n -{n} >/dev/null; wc -l )', stdin_path=seqf)
            row[label] = so.strip()
        row["agree"] = row.get("c") == row.get("rust")
        sweep.append(row)
    out = {
        "command": "( head -n -N >/dev/null; wc -l ) < seq(1..%d), swept over N" % total_lines,
        "explanation": "correct head leaves the shared fd showing the last N lines -> C=N; Rust -> 0",
        "sweep": sweep,
        # keep the flat fields the summary/prose reference, from the headline N=50000 row
        "c": next((r["c"] for r in sweep if r["n"] == 50000), sweep[0]["c"]),
        "rust": next((r["rust"] for r in sweep if r["n"] == 50000), sweep[0]["rust"]),
    }
    out["agree"] = all(r["agree"] for r in sweep)
    out["all_magnitudes_diverge"] = all((r["agree"] is False) for r in sweep)
    return out


def split_isolation(c_bin, r_bin, name, setup_cmd, probe_cmd):
    """Run split in a FRESH empty dir for each binary; probe the resulting file set."""
    res = {}
    for label, b in (("c", c_bin), ("rust", r_bin)):
        d = tempfile.mkdtemp()
        if setup_cmd:
            run(setup_cmd, cwd=d)
        run(probe_cmd.replace("@BIN@", b), cwd=d)
        files = sorted(f for f in os.listdir(d) if f.startswith("x"))
        res[label] = files
    res["agree"] = res["c"] == res["rust"]
    res["probe"] = probe_cmd.replace("@BIN@", "split")
    return {name: res}


def main():
    ev = {"system": "C2SaferRust",
          "what": "direct binary A/B for the in-distribution ablation triage (no test framework)",
          "checks": {}}

    ob, sb = build_oracle("head"), build_sut("head")
    if ob.get("ok") and sb.get("ok"):
        ev["checks"]["head_pos_shared_fd"] = head_pos_check(ob["bin"], sb["bin"])
    else:
        ev["checks"]["head_pos_shared_fd"] = {"error": "build failed",
                                              "c": ob.get("rc"), "rust": sb.get("rc")}

    ob, sb = build_oracle("split"), build_sut("split")
    if ob.get("ok") and sb.get("ok"):
        c, r = ob["bin"], sb["bin"]
        ev["checks"].update(split_isolation(c, r, "split_n_2over3_devnull", None,
                                             '"@BIN@" -n 2/3 /dev/null'))
        ev["checks"].update(split_isolation(c, r, "split_n_10_devnull", None,
                                             '"@BIN@" -n 10 /dev/null'))
        ev["checks"].update(split_isolation(c, r, "split_n_r3_devnull", None,
                                             '"@BIN@" -n r/3 /dev/null'))
        ev["checks"].update(split_isolation(c, r, "split_b1_suffix_autoextend",
                                             "head -c 680 /dev/zero | tr '\\0' 'a' > in680",
                                             '"@BIN@" -b 1 in680'))
    else:
        ev["checks"]["split"] = {"error": "build failed"}

    # verdict summary
    hp = ev["checks"].get("head_pos_shared_fd", {})
    splits = [k for k in ev["checks"] if k.startswith("split_")]
    ev["summary"] = {
        "head_pos_confirmed_S1": (hp.get("agree") is False),
        "head_pos_c": hp.get("c"), "head_pos_rust": hp.get("rust"),
        "split_cases_all_agree_in_isolation": all(ev["checks"][k].get("agree") for k in splits) if splits else None,
    }
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(os.path.join(root, "results"), exist_ok=True)
    outp = os.path.join(root, "results", "c2saferrust_triage_evidence.json")
    json.dump(ev, open(outp, "w"), indent=2)
    print(json.dumps(ev, indent=2))


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
