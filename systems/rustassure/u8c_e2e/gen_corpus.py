"""Deterministic UTF-8 / byte corpus for the u8c decoder differential test.

No randomness: a fixed set covering valid 1-4 byte codepoints, boundaries, and the
malformed cases the decoder FSM has explicit states for (overlong, truncated, invalid
continuation, surrogates, out-of-range, modified-UTF-8 NUL C0 80), plus every lead byte.
"""

def corpus():
    c = []

    def add(name, b):
        c.append((name, bytes(b)))

    # valid
    add("ascii", b"hello")
    add("ascii_sym", b"A!~ 09")
    add("two_byte", "é".encode())
    add("two_byte2", "ñλ".encode())
    add("three_byte", "€".encode())
    add("three_byte_cjk", "日本語".encode())
    add("four_byte", "😀".encode())
    add("mixed", "aé日😀z".encode())
    add("max_4byte", [0xF4, 0x8F, 0xBF, 0xBF])     # U+10FFFF (max valid)
    add("min_2byte", [0xC2, 0x80])                  # U+0080
    add("min_3byte", [0xE0, 0xA0, 0x80])            # U+0800
    add("min_4byte", [0xF0, 0x90, 0x80, 0x80])      # U+10000

    # modified-UTF-8 NUL (C0 80 -> U+0000, a special FSM state)
    add("mutf8_nul", [0xC0, 0x80])
    add("mutf8_nul_then", [0xC0, 0x80, 0x41])

    # overlong / invalid lead
    add("overlong_c1", [0xC1, 0xBF])                # <= 0xC1 -> invalid
    add("overlong_3", [0xE0, 0x80, 0x80])           # len3_0 needs >=0xA0 -> invalid
    add("overlong_4", [0xF0, 0x80, 0x80, 0x80])     # len4_0 needs >=0x90 -> invalid
    add("out_of_range_4", [0xF4, 0x90, 0x80, 0x80]) # len4_2 needs <=0x8F -> invalid

    # surrogates (len3_2 restricts to <=0x9F)
    add("surrogate_d800", [0xED, 0xA0, 0x80])
    add("surrogate_dfff", [0xED, 0xBF, 0xBF])
    add("just_below_surrogate", [0xED, 0x9F, 0xBF]) # U+D7FF (valid)

    # truncated
    add("trunc_2", [0xC3])
    add("trunc_3", [0xE2, 0x82])
    add("trunc_4", [0xF0, 0x9F, 0x98])

    # invalid continuation placement
    add("stray_cont", [0x41, 0x80, 0x42])
    add("lead_then_ascii", [0xC3, 0x41])            # 2-byte lead then ASCII (bad cont)
    add("double_lead", [0xE2, 0xE2, 0x82, 0xAC])

    # high / reserved lead bytes
    add("ff", [0xFF])
    add("fe", [0xFE])
    add("f8", [0xF8])
    add("c0_alone", [0xC0])
    add("all_continuation", [0x80, 0x81, 0xBF])

    # every lead byte 0x80..0xFF (one case, catches any per-byte FSM divergence)
    add("sweep_high", list(range(0x80, 0x100)))
    # a run mixing valid and malformed
    add("mix_valid_invalid", "a".encode() + bytes([0xFF]) + "é".encode() + bytes([0xC0]) + "z".encode())

    # interior NUL stops decoding (both sides)
    add("interior_nul", [0x61, 0x00, 0x62])
    add("empty", [])
    return c


if __name__ == "__main__":
    for n, b in corpus():
        print(f"{n:22s} {b.hex()}")
    print(len(corpus()), "inputs")
