"""Self-test for the file-OUTPUT differential path (split, truncate).

A 0-divergence result for a file-writing util is only meaningful if the harness
CAN detect a file-output difference. This asserts three things for each util:
  1. non-vacuous: the oracle actually writes/changes files (so we're comparing something),
  2. real oracle vs real SUT AGREE (the honest result),
  3. a deliberately-broken SUT (a wrapper that changes the output) makes the file-tree
     comparison go RED -- proving mismatch detection works for filenames AND content.

Run inside the build container:
    docker run --rm -v "$PWD:/work" -w /work crust-heldout python3 harness/selftest_filetree.py
Exit 0 = all assertions hold; exit 1 = the harness failed to detect an injected fault.
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import Case, b64              # noqa: E402
from difftest import compare_case, _snapshot_tree  # noqa: E402
from build import build_both             # noqa: E402


def _wrapper(path, real_bin, extra_args):
    """A fake SUT: runs the real oracle but with injected extra args (the 'mutation'),
    appended AFTER the case's own args so last-option-wins favours the injected value
    (needed when the mutation overrides an option the case already sets, e.g. -s)."""
    with open(path, "w") as f:
        f.write('#!/bin/sh\nexec %s "$@" %s\n' % (real_bin, extra_args))
    os.chmod(path, 0o755)


def check(util, case, mutant_extra_args, expect_kind):
    bi = build_both(util)
    assert bi["oracle"]["ok"] and bi["sut"]["ok"], f"{util}: build failed"
    o, s = bi["oracle"]["bin"], bi["sut"]["bin"]

    # 1) non-vacuous: run the oracle alone and confirm it left files behind
    with tempfile.TemporaryDirectory() as wd:
        for name, data in case.decoded_files().items():
            with open(os.path.join(wd, name), "wb") as f:
                f.write(data)
        subprocess.run([o] + list(case.argv), cwd=wd, input=case.stdin(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        produced = sorted(_snapshot_tree(wd))
    assert produced, f"{util}: oracle produced no files -- comparison would be vacuous"

    # 2) real vs real -> AGREE
    v = compare_case(case, o, s)
    assert v["mismatch"] is False and v.get("file_tree_match") is True, \
        f"{util}: real oracle vs real SUT unexpectedly diverged: {v}"

    # 3) injected fault -> RED
    wrap = f"/tmp/bad_{util}"
    _wrapper(wrap, o, mutant_extra_args)
    v2 = compare_case(case, o, wrap)
    kinds = {d["kind"] for d in (v2.get("file_tree_diffs") or [])}
    assert v2["mismatch"] is True and expect_kind in kinds, \
        f"{util}: injected fault NOT detected (mismatch={v2['mismatch']}, kinds={kinds})"
    print(f"[selftest] {util}: produced={produced} | real-vs-real agree | mutant caught ({expect_kind})")


def main():
    # split: a suffix-length mutation renames the output files -> only-in-oracle/sut
    check("split",
          Case(id="st", util="split", argv=["-l", "2", "in.txt"], stdin_b64=b64(b""),
               files={"in.txt": b64(b"1\n2\n3\n4\n5\n")}, writes_files=True),
          mutant_extra_args="-a 4", expect_kind="only-in-oracle")
    # truncate: a size mutation changes the file's content/length -> content-differs
    check("truncate",
          Case(id="tt", util="truncate", argv=["-s", "5", "t.bin"], stdin_b64=b64(b""),
               files={"t.bin": b64(b"ab")}, writes_files=True),
          mutant_extra_args="-s 6", expect_kind="content-differs")
    print("[selftest] OK: file-output harness detects injected faults for split + truncate")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print("[selftest] FAIL:", e)
        sys.exit(1)
