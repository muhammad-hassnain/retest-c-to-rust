"""Build the Rust SUT driver from RustAssure's composed libcsv translation.

RustAssure ships the per-function translations assembled into a runnable parser at
  artifacts/rustassure/src/Link/csv_link/src/main.rs
but driven by a hard-coded self-test. We keep ONLY the translated library core
(types/consts + csv_init/csv_fini/csv_set_*/csv_increase_buffer/csv_parse, lines up
to the first test function), drop the self-test's checking callbacks and driver, and
append our own recording callbacks + a stdin main that emits the SAME canonical event
stream as csv_oracle.c. Nothing in the translated functions is modified.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
MAIN = os.path.join(REPO, "artifacts", "rustassure", "src", "Link", "csv_link", "src", "main.rs")

DRIVER = r'''
// ---------------- differential driver (added; translated code above is untouched) ----------------
unsafe fn rec_cb1(data: &[u8], len: usize, _t: &mut ()) {
    use std::io::Write;
    let o = std::io::stdout();
    let mut h = o.lock();
    let _ = write!(h, "F {}", len);
    for b in data.iter().take(len) { let _ = write!(h, " {:02x}", b); }
    let _ = writeln!(h);
}
unsafe fn rec_cb2(c: i32, _t: &mut ()) {
    use std::io::Write;
    let o = std::io::stdout();
    let mut h = o.lock();
    let _ = writeln!(h, "R {}", c);
}
fn d_space(c: u8) -> i32 { if c == 0x20 || c == 0x09 { 1 } else { 0 } }
fn d_term(c: u8) -> i32 { if c == 0x0d || c == 0x0a { 1 } else { 0 } }

fn main() {
    use std::io::{Read, Write};
    let args: Vec<String> = std::env::args().collect();
    let options: u8 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
    let delim: u8 = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(0x2c);
    let quote: u8 = args.get(3).and_then(|s| s.parse().ok()).unwrap_or(0x22);
    let mut input = Vec::new();
    std::io::stdin().read_to_end(&mut input).unwrap();
    unsafe {
        let mut parser = CsvParser::new(options);
        let p: &mut CsvParser = &mut parser;
        csv_set_delim(p, delim);
        csv_set_quote(p, quote);
        csv_set_space_func(p, d_space);
        csv_set_term_func(p, d_term);
        let processed = csv_parse(p, &input, Some(rec_cb1), Some(rec_cb2), &mut ());
        {
            let o = std::io::stdout(); let mut h = o.lock();
            let _ = writeln!(h, "P {}", processed);
        }
        let fini = csv_fini(p, Some(rec_cb1), Some(rec_cb2), &mut ());
        let status = p.status;
        {
            let o = std::io::stdout(); let mut h = o.lock();
            let _ = writeln!(h, "Z {}", fini);
            let _ = writeln!(h, "E {}", status);
        }
        csv_free(p);
    }
    std::io::stdout().flush().unwrap();
}
'''


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "csv_sut.rs")
    with open(MAIN) as f:
        lines = f.readlines()

    def find(prefix):
        for i, ln in enumerate(lines):
            if ln.startswith(prefix):
                return i
        raise SystemExit(f"marker not found: {prefix!r}")

    i_cb1 = find("pub unsafe fn cb1(")   # start of self-test checking callbacks
    i_trait = find("trait AsBool")        # resume core here (after cb1/cb2)
    i_test = find("fn test_parser(")      # everything from here down is self-test + main

    core = lines[:i_cb1] + lines[i_trait:i_test]
    # The shipped composition imports `libc::option`, which does not resolve (it is an
    # unused, bogus import -- the composed crate does not build clean without this fix).
    # Strip ONLY that token; no translated function is touched.
    core = [ln.replace("use libc::{c_char, c_int, option, size_t};",
                       "use libc::{c_char, c_int, size_t};") for ln in core]
    header = ("#![allow(dead_code, unused_variables, unused_mut, unused_imports, "
              "non_snake_case, non_upper_case_globals, static_mut_refs, unused_unsafe)]\n")

    with open(out, "w") as f:
        f.write(header)
        f.writelines(core)
        f.write(DRIVER)
    print(f"[extract] wrote {out} ({i_cb1} head + {i_test - i_trait} core lines + driver)")


if __name__ == "__main__":
    main()
