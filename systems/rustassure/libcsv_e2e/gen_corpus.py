"""Deterministic CSV corpus for the libcsv end-to-end differential test.

No randomness: a fixed, structured set of inputs exercising the parser state machine
(quoting, escaped quotes, embedded newlines, empty fields, spaces, CR/LF variants,
NUL/high bytes, unterminated quotes, delimiter/quote-as-data). The same set every run.
"""

def corpus():
    c = []

    def add(name, b):
        c.append((name, b))

    # --- basic records ---
    add("simple_lf", b"a,b,c\n")
    add("simple_crlf", b"1,2,3\r\n")
    add("no_trailing_nl", b"x,y,z")
    add("single_field", b"hello\n")
    add("single_char", b"a")
    add("empty_input", b"")

    # --- empty fields ---
    add("all_empty", b",,\n")
    add("mid_empty", b"a,,c\n")
    add("lead_empty", b",a,b\n")
    add("trail_empty", b"a,b,\n")
    add("just_nl", b"\n")
    add("blank_line", b"a,b\n\nc,d\n")
    add("only_delims", b",,,\n")

    # --- quoting ---
    add("quoted_fields", b'"a","b","c"\n')
    add("quoted_delim", b'"a,b",c\n')
    add("escaped_quote", b'"a""b",c\n')
    add("empty_quoted", b'"",x\n')
    add("quoted_newline", b'"line1\nline2",b\n')
    add("quoted_crlf", b'"a\r\nb",c\n')
    add("quote_then_text", b'a,"b"junk,c\n')
    add("unterminated_quote", b'"unterminated,b\n')
    add("quote_at_eof", b'a,"b')
    add("quoted_spaces", b'" a ",b\n')

    # --- spaces / terms ---
    add("spaces_around", b" a , b \n")
    add("space_before_delim", b"a ,b\n")
    add("tab_field", b"a\tb,c\n")
    add("trailing_spaces", b"a,b   \n")
    add("cr_only", b"a,b\rc,d\r")
    add("lf_in_mid", b"a\nb\n")
    add("cr_lf_mix", b"a,b\r\nc,d\n")

    # --- bytes / adversarial ---
    add("nul_byte", b"a\x00b,c\n")
    add("high_bytes", bytes([0xff, 0xfe, 0x2c, 0x80, 0x0a]))
    add("delim_as_data_only", b",\n")
    add("quote_only", b'"\n')
    add("many_quotes", b'""""""\n')
    add("binary_mix", bytes(range(0, 48)))

    # --- scale ---
    add("wide_row", (b",".join(b"f%d" % i for i in range(40)) + b"\n"))
    add("many_rows", (b"\n".join(b"r%d,v%d" % (i, i) for i in range(200)) + b"\n"))
    add("long_field", b"x" * 500 + b",y\n")
    add("big_quoted", b'"' + b"z" * 400 + b'"\n')

    return c


OPTIONS = [
    ("none", 0),
    ("strict", 1),
    ("repall_nl", 2),
    ("strict_fini", 4),
    ("append_null", 8),
    ("empty_is_null", 16),
    ("strict+append", 1 | 8),
    ("append+empty_null", 8 | 16),
    ("strict+fini", 1 | 4),
]


if __name__ == "__main__":
    cs = corpus()
    print(f"{len(cs)} inputs x {len(OPTIONS)} option sets = {len(cs) * len(OPTIONS)} cases")
    for n, b in cs:
        print(f"  {n:24s} {len(b):4d} bytes")
