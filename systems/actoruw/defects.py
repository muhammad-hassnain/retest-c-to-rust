"""Cluster counted (UB-clean) divergences of the ACTOR re-grade into defects.

A defect is ONE mechanism in ONE translation's source (paper, "Units and terms"); every counted
case that exposes it counts toward its exposure. Mechanisms (each confirmed in the translation's
source; locations in LOCATIONS below, checked by hand):

  PARSE_NUM    the C reads a number with scanf/sscanf/strtol/atoi/atof, which accept a numeric
               PREFIX of a token ("3.14159" -> 3, "1e10" -> 1, "12abc" -> 12), skip leading
               white space (" 12"), accept "" (strtol: 0), a sign on %u/%zu ("-1" -> UINT_MAX),
               hex floats (atof "0x1F"), and read one token of a line; the Rust parses the whole
               token / line / input with str::parse and falls back to a default or an error path.
  UTF8_PANIC   the Rust reads stdin with read_to_string(..).unwrap() / read_line(..).unwrap()
               and panics on bytes that are not UTF-8; the C reads bytes.
  UTF8_EOF     the Rust treats a read error on non-UTF-8 stdin as end of input (no panic); the C
               reads the bytes.
  TOKEN_PANIC  the Rust indexes / unwraps the next stdin token and panics when the input has
               fewer (or malformed) tokens than the format; the C's scanf leaves the variables at
               their initial values.
  LINE_SPLIT   the C reads lines with fgets(buf, N), which returns a line longer than N-1 bytes
               in pieces; the Rust reads the whole line.
  NUL_BYTE     the C's string functions stop at an embedded NUL byte; the Rust keeps it.
  WS_SET       the Rust's split_whitespace also splits at '\\r'; the C's tokenizer does not.
  NAN_FMT      printf("%.2f", NaN) prints "nan"; Rust's {:.2} prints "NaN".
  CTYPE_VALUE  (library) the C prints the value isdigit() returns (glibc: the mask 2048); the Rust's
               extern isdigit call is folded by LLVM to a 0/1 comparison and prints 1.
  SHIFT_SIGN   (library) right shift of a negative int: arithmetic in the C (gcc/clang), logical
               in the Rust (cast to u32).
  ASSERT_DROPPED  hashmap-tree: the C calls tree_add_node(...) inside assert(); its build defines
               no NDEBUG, so the calls run. The Rust drops them (as if NDEBUG), builds an empty
               tree and prints "(empty tree)".
  BREAK_LOOP   a C `break` inside a switch case leaves the switch (the menu loop goes on); the
               Rust `break` inside the match arm leaves the loop, so the program ends at EOF in a
               sub-prompt where the C prints the menu again.

Usage: python3 -I defects.py <difftest.json> <ub_filter.json> <build.json> [--kind exe|lib] -> JSON on stdout
"""
import base64
import json
import sys


def B(s):
    return base64.b64decode(s)


def mechanism(prog, dv):
    """Assign one mechanism to a counted divergent case (priority order matters)."""
    case = prog["case"]
    stdin = B(dv["input"]["stdin_b64"])
    rerr = B(dv["rust"]["stderr_b64"])
    cls = dv["class"]
    argv = dv["input"]["argv"]
    if case == "hashmap-tree":
        return "ASSERT_DROPPED"
    if cls == "rust_crash" and b"did not contain valid UTF-8" in rerr:
        return "UTF8_PANIC"
    if cls == "rust_crash" and (b"index out of bounds" in rerr or b"on a `None` value" in rerr
                                or b"ParseIntError" in rerr or b"ParseFloatError" in rerr):
        return "TOKEN_PANIC"
    if case.startswith("007_errno-pow") and any(a.lower() in ("nan", "-nan") for a in argv):
        return "NAN_FMT"
    try:
        stdin.decode("utf-8")
        utf8 = True
    except UnicodeDecodeError:
        utf8 = False
    if not utf8:
        return "UTF8_EOF"
    if b"\x00" in stdin and case == "strcmp":
        return "NUL_BYTE"
    if b"\r" in stdin and case == "strcmp":
        return "WS_SET"
    if any(len(l) >= 99 for l in stdin.split(b"\n")) and case in FGETS_USERS:
        return "LINE_SPLIT"
    cout, rout = B(dv["c"]["stdout_b64"]), B(dv["rust"]["stdout_b64"])
    if case in BREAK_LOOP_CASES and len(rout) < len(cout) and cout.startswith(rout) and rout.rstrip(b" ").endswith(b":"):
        return "BREAK_LOOP"
    return "PARSE_NUM"


# executables whose C reads lines with fgets into a fixed buffer (grep fgets in c_src)
FGETS_USERS = {"002_stdin_echo", "009_stack_buffer_overflow", "016_divide_by_zero_float",
               "017_signed_length_confusion", "023_struct_and_errno", "025_struct_and_errno_and_static",
               "029_strcspn", "char-to-bool", "generic-foreach", "mutable-duplication-dag",
               "pointer-comparison-ascii-art", "static-vars-fpts", "strcmp", "tu_linkage"}
# translations where BREAK_LOOP was confirmed in the source (see LOCATIONS)
BREAK_LOOP_CASES = {"mutable-duplication-dag"}


def lib_mechanism(prog, dv):
    """Library defects (function-grid harness)."""
    case = prog["case"]
    if case == "027_ctype_ascii_lib":
        return "CTYPE_VALUE"
    if case == "checkshift_lib":
        return "SHIFT_SIGN"
    return "UNCLASSIFIED"


# Hand-confirmed source locations (file:line in the graded phase dir) per (translation, mechanism).
LOCATIONS = {
    # (translation case, mechanism): "file:lines in the graded phase dir -- what the code does"
    ("005_static_loop", "PARSE_NUM"): "src/main.rs:21-25 -- takes the [-+0-9] prefix of argv[1] without skipping leading white space (strtol does)",
    ("007_errno-pow", "NAN_FMT"): "src/main.rs:41 -- println!(\"Result: {:.2}\") prints NaN; C printf %.2f prints nan",
    ("011_uninit_char_ptr", "PARSE_NUM"): "src/main.rs:30-32 -- first token .parse::<i32>() else 0 (scanf %d reads the numeric prefix)",
    ("015_return_stack_buffer", "PARSE_NUM"): "src/main.rs:32-34 -- first token .parse() else 0",
    ("019_integer_overflow_char_max_multiply", "PARSE_NUM"): "src/main.rs:51 -- input.trim().parse() of the WHOLE stdin else 0",
    ("021_complex_goto", "PARSE_NUM"): "src/main.rs:41-42 -- token .parse().unwrap_or(0)",
    ("021_complex_goto", "UTF8_PANIC"): "src/main.rs:39 -- read_to_string(&mut input).unwrap()",
    ("022_stdlib_div", "PARSE_NUM"): "src/main.rs:9,12 -- token .parse::<i32>()",
    ("026_goto_and_static", "PARSE_NUM"): "src/main.rs:36-38 -- token .parse().unwrap_or(0); also y defaults to 0 where C keeps static y = 123",
    ("026_goto_and_static", "UTF8_PANIC"): "src/main.rs:33 -- read_to_string(&mut input).unwrap()",
    ("029_strcspn", "LINE_SPLIT"): "src/main.rs:10-12 -- stdin.lines() reads whole lines; C fgets(s1, 100) (c_src/src/main.c:33-34)",
    ("030_mutable_buffer_overlap_extrahard", "PARSE_NUM"): "src/main.rs:32-36 -- token .parse::<i32>() (scanf %d loop stops at the first non-number)",
    ("030_mutable_buffer_overlap_extrahard", "UTF8_PANIC"): "src/main.rs:27 -- read_to_string(&mut input).unwrap()",
    ("031_disjoint_arrays", "PARSE_NUM"): "src/main.rs:27-31 -- token .parse::<i32>()",
    ("032_comma_operator", "PARSE_NUM"): "src/main.rs:14 -- first token .parse().unwrap_or(0)",
    ("032_comma_operator", "UTF8_PANIC"): "src/main.rs:13 -- read_to_string(&mut input).unwrap()",
    ("033_bitfield", "PARSE_NUM"): "src/main.rs:29-32 -- tokens parsed as u32/i32 else 0 (scanf %u accepts a sign and a numeric prefix)",
    ("033_bitfield", "UTF8_PANIC"): "src/main.rs:26 -- read_to_string(&mut input).unwrap()",
    ("034_cast_to_char_ptr_int", "PARSE_NUM"): "src/main.rs:18-21 -- first token .parse() else 0",
    ("036_cast_to_char_ptr_struct", "PARSE_NUM"): "src/main.rs:34 -- input.trim().parse() of the WHOLE stdin else 0",
    ("036_cast_to_char_ptr_struct", "UTF8_PANIC"): "src/main.rs:33 -- read_to_string(&mut input).unwrap()",
    ("037_cast_to_char_ptr_int_no_strict_aliasing", "PARSE_NUM"): "src/main.rs:18-20 -- first token .parse() else 0",
    ("039_cast_to_char_ptr_struct_no_strict_aliasing", "PARSE_NUM"): "src/main.rs:36 -- first token .parse().unwrap_or(0)",
    ("040_storage_class_auto", "PARSE_NUM"): "src/main.rs:12 -- first token .parse() else 0",
    ("041_storage_class_register", "PARSE_NUM"): "src/main.rs:6 -- first token .parse() else 0",
    ("042_float_union", "PARSE_NUM"): "src/main.rs:53 -- input.trim().parse::<f64>() of the WHOLE stdin else 0.0 (scanf %lf reads the first number)",
    ("042_float_union", "UTF8_PANIC"): "src/main.rs:52 -- read_to_string(&mut input).unwrap()",
    ("043_iso646_and_digraphs", "TOKEN_PANIC"): "src/main.rs:12-13 -- iter.next().unwrap().parse().unwrap() (C scanf leaves x, y at their initial values)",
    ("043_iso646_and_digraphs", "UTF8_PANIC"): "src/main.rs:10 -- read_to_string(&mut input).unwrap()",
    ("qmath", "PARSE_NUM"): "src/main.rs:28-30 -- args[i].parse::<f64>().unwrap_or(0.0) (atof accepts hex floats and numeric prefixes)",
    ("char-to-bool", "LINE_SPLIT"): "src/main.rs:211-237 -- stdin.lines() reads whole lines; C fgets(input_buffer, 1024) (c_src/src/main.c:43-58)",
    ("char-to-bool", "UTF8_EOF"): "src/main.rs:213-218 -- lines().next() = Some(Err) on non-UTF-8 is treated as a read failure",
    ("generic-foreach", "LINE_SPLIT"): "src/main.rs:406-414 -- lines() reads whole lines; C fgets(input, 256) (c_src/src/main.c:353,359)",
    ("generic-foreach", "PARSE_NUM"): "src/main.rs:416 -- line.trim().parse() of the whole line; C sscanf(input, \"%d\") reads the first number",
    ("generic-foreach", "UTF8_EOF"): "src/main.rs:411-414 -- Some(Err) from lines() ends the loop",
    ("hashmap-tree", "ASSERT_DROPPED"): "src/main.rs:220-225 -- test_tree_complex_structure omits the tree_add_node calls that the C makes inside assert() (c_src/src/main.c:359-368)",
    ("macrodepth_add_5", "PARSE_NUM"): "src/main.rs:12-13 -- args[i].parse().unwrap_or(0) (C atoi: prefix, leading white space)",
    ("macrodepth_mul_4", "PARSE_NUM"): "src/main.rs:12-13 (translated/) -- args[i].parse().unwrap_or(0)",
    ("macrodepth_sub_6", "PARSE_NUM"): "src/main.rs:12-13 (translated/) -- args[i].parse().unwrap_or(0)",
    ("memcpy-fun-buffers", "PARSE_NUM"): "src/main.rs:197-199 -- token .parse::<i32>() (scanf %d reads \"0\" of \"0.5\")",
    ("memmove", "PARSE_NUM"): "src/main.rs:20-46 -- tokens parsed as u32/i32/usize (scanf %u/%zu accept a sign and a numeric prefix)",
    ("memmove", "UTF8_PANIC"): "src/main.rs:5 -- read_to_string(&mut input).unwrap()",
    ("mutable-duplication-dag", "BREAK_LOOP"): "src/main.rs:309-312 -- `None => break` inside the match arm leaves the menu loop; C `break` (c_src/src/main.c:75-77) leaves only the switch",
    ("mutable-duplication-dag", "LINE_SPLIT"): "src/main.rs:266-272 -- read_line reads whole lines; C fgets(input, 256)",
    ("mutable-duplication-dag", "PARSE_NUM"): "src/main.rs:297 -- input.trim().parse() of the whole line",
    ("mutable-duplication-dag", "UTF8_EOF"): "src/main.rs:268-271 -- read_line Err (non-UTF-8) -> None, treated as EOF",
    ("pointer-comparison-ascii-art", "LINE_SPLIT"): "src/main.rs:449-458 -- fgets_line reads whole lines; C fgets with a fixed buffer",
    ("pointer-comparison-ascii-art", "PARSE_NUM"): "src/main.rs:825 -- input.trim().parse() of the whole line",
    ("pointer-comparison-ascii-art", "UTF8_EOF"): "src/main.rs:457 -- read_line Err (non-UTF-8) -> None, treated as EOF",
    ("static-vars-fpts", "LINE_SPLIT"): "src/main.rs:140 -- read_line reads whole lines; C fgets(input, 256) (c_src/src/main.c:143)",
    ("static-vars-fpts", "PARSE_NUM"): "src/main.rs:144 -- input.trim().parse() of the whole line",
    ("static-vars-fpts", "UTF8_EOF"): "src/main.rs:140 -- read_line(..).unwrap_or(0) == 0: a non-UTF-8 read error ends the loop",
    ("strcmp", "LINE_SPLIT"): "src/main.rs:633 -- read_line reads whole lines; C fgets(input, MAX_INPUT=256) (c_src/src/main.c:670)",
    ("strcmp", "NUL_BYTE"): "src/main.rs:633,84 -- the line keeps embedded NUL bytes; C string functions stop at the first NUL",
    ("strcmp", "WS_SET"): "src/main.rs:637-638,84 -- strips a trailing '\\r' and split_whitespace() splits at '\\r'; the C tokenizer does not",
    ("strcpy", "PARSE_NUM"): "src/main.rs:308-344 -- parse_token! .parse::<u32/usize/i32>() (scanf %u/%zu accept a sign and a numeric prefix)",
    ("tu_linkage", "PARSE_NUM"): "src/main.rs:523 -- args[i].parse::<i64>() (C strtol skips leading white space and accepts \"\" as 0)",
    ("001_perlin_noise", "TOKEN_PANIC"): "src/main.rs:289-298 -- Scanner indexes tokens[pos] and .parse().unwrap(); C scanf leaves missing fields at 0",
    ("001_perlin_noise", "UTF8_PANIC"): "src/main.rs:284 -- read_to_string(&mut input).unwrap()",
    ("027_ctype_ascii_lib", "CTYPE_VALUE"): "src/lib.rs:10,45 -- prints the raw value of an extern isdigit() call, which LLVM folds to a 0/1 range check; the C (glibc macro) prints the ctype mask 2048. Implementation-defined value (C only promises nonzero)",
    ("checkshift_lib", "SHIFT_SIGN"): "src/lib.rs:42 -- (b as u32 >> n): logical shift; C b >> n on a negative int is arithmetic under gcc and clang (c_src/src/lib.c:63). Implementation-defined in ISO C",
}


def main():
    dt = json.load(open(sys.argv[1]))
    ub = json.load(open(sys.argv[2]))
    counted = {}
    for p in ub["programs"]:
        for v in p["verdicts"]:
            counted[(p["battery"], p["case"], v["id"])] = v["counted"]
    out = {}
    for p in dt["programs"]:
        for dv in p["divergent"]:
            if not counted.get((p["battery"], p["case"], dv["input"]["id"])):
                continue
            m = mechanism(p, dv)
            key = f"{p['battery']}/{p['case']}::{m}"
            d = out.setdefault(key, {"translation": f"{p['battery']}/{p['case']}", "mechanism": m,
                                     "location": LOCATIONS.get((p["case"], m), "UNCONFIRMED"),
                                     "classes": {}, "exposure": 0, "examples": [], "case_ids": []})
            d["case_ids"].append(dv["input"]["id"])
            d["exposure"] += 1
            d["classes"][dv["class"]] = d["classes"].get(dv["class"], 0) + 1
            if len(d["examples"]) < 3:
                d["examples"].append({"id": dv["input"]["id"], "argv": dv["input"]["argv"],
                                      "stdin": B(dv["input"]["stdin_b64"])[:120].decode("latin-1"),
                                      "class": dv["class"],
                                      "c": {"rc": dv["c"]["rc"], "stdout": B(dv["c"]["stdout_b64"])[:300].decode("utf-8", "replace"),
                                            "stderr": B(dv["c"]["stderr_b64"])[:200].decode("utf-8", "replace")},
                                      "rust": {"rc": dv["rust"]["rc"], "stdout": B(dv["rust"]["stdout_b64"])[:300].decode("utf-8", "replace"),
                                               "stderr": B(dv["rust"]["stderr_b64"])[:300].decode("utf-8", "replace")}})
    json.dump(sorted(out.values(), key=lambda d: (d["translation"], d["mechanism"])), sys.stdout, indent=1)


if __name__ == "__main__":
    main()
