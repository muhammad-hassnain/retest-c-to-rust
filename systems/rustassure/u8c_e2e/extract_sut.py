"""Build the Rust SUT driver from RustAssure's composed u8c translation.

Keeps the translated library core from
  artifacts/rustassure/src/Link/u8c_link/src/main.rs
(everything up to the first self-test function), drops the self-test + its empty main,
and appends a stdin driver that walks u8next_ exactly as u8c_oracle.c does, emitting the
same canonical event stream. Translated functions are not modified.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
MAIN = os.path.join(REPO, "artifacts", "rustassure", "src", "Link", "u8c_link", "src", "main.rs")

DRIVER = r'''
// ---------------- differential driver (added; translated code above is untouched) ----------------
fn main() {
    use std::io::{Read, Write};
    let mut input = Vec::new();
    std::io::stdin().read_to_end(&mut input).unwrap();
    input.push(0u8); // NUL-terminate
    let n = input.len() - 1;
    let base = input.as_ptr();
    let o = std::io::stdout();
    let mut h = o.lock();
    let mut pos: usize = 0;
    let mut steps: usize = 0;
    let guard = n + 2;
    while pos <= n && steps < guard {
        let mut ch: i32 = 0;
        let consumed = unsafe { u8next_(base.add(pos), &mut ch) };
        let _ = writeln!(h, "D {} {}", ch, consumed);
        steps += 1;
        if consumed == 0 { break; }
        else if consumed < 0 { pos += 1; }
        else { pos += consumed as usize; }
    }
    let _ = writeln!(h, "END {}", steps);
    let _ = h.flush();
}
'''


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "u8c_sut.rs")
    with open(MAIN) as f:
        lines = f.readlines()

    def find(prefix):
        for i, ln in enumerate(lines):
            if ln.startswith(prefix):
                return i
        raise SystemExit(f"marker not found: {prefix!r}")

    i_test = find("fn test_01(")   # self-test + main start here
    core = lines[:i_test]
    header = ("#![allow(dead_code, unused_variables, unused_mut, unused_imports, "
              "non_snake_case, non_upper_case_globals, unused_unsafe)]\n")
    with open(out, "w") as f:
        f.write(header)
        f.writelines(core)
        f.write(DRIVER)
    print(f"[extract] wrote {out} ({i_test} core lines + driver)")


if __name__ == "__main__":
    main()
