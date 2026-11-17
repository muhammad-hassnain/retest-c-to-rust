#!/usr/bin/env python3
"""Deterministic call-sequence generator for the libogg library-level differential test.

By program shape (method.tex, "Inputs"): for a library we use a fixed set of API call sequences.
Arguments come from per-argument grids (small values, type boundaries, powers of two +-1, sizes
around libogg's internal boundaries: 255-byte lacing values, 256-byte bitwise growth, the 4096-byte
page fill target), and multi-argument choices are spread over the product with a Halton
low-discrepancy sequence, so every argument sweeps its whole grid. No model is queried; nothing is
random. Output: cases_bitwise.json and cases_framing.json, lists of {"id", "family", "script"}.

    python3 gen_cases.py OUTDIR
"""
import json
import os
import sys

# ---------------------------------------------------------------------------------------------
def halton(i, base):
    f, r = 1.0, 0.0
    while i > 0:
        f /= base
        r += f * (i % base)
        i //= base
    return r


def pick(grid, i, base):
    return grid[int(halton(i + 1, base) * len(grid)) % len(grid)]


PRIMES = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29]

# ---------------------------------------------------------------------------------------------
# bitwise.c
VALS = [0, 1, 2, 3, 5, 0x7F, 0x80, 0xFF, 0x100, 0x1234, 0x7FFF, 0x8000, 0xFFFF, 0x10000,
        0x7FFFFFFF, 0x80000000, 0xFFFFFFFE, 0xFFFFFFFF, 0x100000000, 0xFFFFFFFFFFFFFFFF,
        0xDEADBEEF, 0x5A5A5A5A, 0x12345678, 0xA5]
BITS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 12, 15, 16, 17, 23, 24, 25, 31, 32]
BAD_BITS = [-1, 33, 40, 64, -32]
COPY_PREFILL = [0, 1, 2, 7, 8, 9, 15, 16, 17, 31, 32, 33, 63, 64, 65]
COPY_BITS = [0, 1, 7, 8, 9, 15, 16, 17, 31, 32, 33, 255, 256, 257, 2047, 2048, 2049, 2403, 8000]
RBYTES = [0, 1, 2, 3, 4, 5, 6, 8, 9]


def lcg_bytes(seed, n):
    s = seed
    out = []
    for _ in range(n):
        s = (s * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
        out.append((s >> 33) & 0xFF)
    return out


def writes(i, n, salt):
    return [(pick(VALS, i * 31 + k + salt, PRIMES[k % 5]), pick(BITS, i * 37 + k + salt, PRIMES[(k + 2) % 7]))
            for k in range(n)]


def bitwise_cases():
    cases = []

    def add(fam, lines):
        cases.append({"id": f"bw{len(cases):04d}", "family": fam, "script": "\n".join(lines) + "\n"})

    for mode in ("L", "B"):
        # A: write a sequence, then read it back with read() (+ reads past the end)
        for i in range(40):
            ws = writes(i, 1 + (i % 9) + (i // 9), 0)
            L = [f"mode {mode}", "winit"] + [f"w {v} {b}" for v, b in ws] + ["wstate", "rfromw"]
            L += [f"read {b}" for _, b in ws]
            L += ["read 1", "read 8", "read 32", "look 0", "read 0", "look1", "read1", "rstate"]
            add("roundtrip_read", L)
        # A2: same, read back with look()+adv() and look1()/read1()
        for i in range(25):
            ws = writes(i, 2 + (i % 7), 101)
            L = [f"mode {mode}", "winit"] + [f"w {v} {b}" for v, b in ws] + ["wstate", "rfromw"]
            for _, b in ws:
                L += [f"look {b}", f"adv {b}"]
            L += ["look1", "read1", "adv1", "look 1", "look 7", "look 32", "rstate"]
            add("roundtrip_lookadv", L)
        # B: truncation within the written range, then more writes
        for i in range(25):
            ws = writes(i, 3 + (i % 6), 211)
            total = sum(b for _, b in ws)
            cut = int(halton(i + 1, 3) * (total + 1))
            more = writes(i, 1 + i % 4, 307)
            L = [f"mode {mode}", "winit"] + [f"w {v} {b}" for v, b in ws] + [f"wtrunc {cut}", "wstate"]
            L += [f"w {v} {b}" for v, b in more] + ["wstate", "rfromw"] + [f"read {b}" for _, b in more[:2]] + ["rstate"]
            add("trunc", L)
        # C: alignment
        for i in range(15):
            ws = writes(i, 2 + (i % 5), 401)
            L = [f"mode {mode}", "winit"]
            for k, (v, b) in enumerate(ws):
                L.append(f"w {v} {b}")
                if (i + k) % 2 == 0:
                    L.append("walign")
            L += ["walign", "walign", "wstate", "rfromw", "read 32", "read 32", "rstate"]
            add("align", L)
        # D: writecopy after a prefill (the 256-byte growth boundary included)
        for i, p in enumerate(COPY_PREFILL):
            for j in range(3):
                c = pick(COPY_BITS, i * 3 + j, 3)
                L = [f"mode {mode}", "winit"]
                left = p
                while left > 0:
                    b = min(left, pick([1, 3, 8, 13, 32], i + left, 2))
                    L.append(f"w {(0x5A5A5A5A ^ left) & 0xFFFFFFFF} {b}")
                    left -= b
                L += [f"wcopy {1000 + i * 7 + j} {c}", "wstate", f"wcopy {77 + j} {pick(COPY_BITS, j + i, 5)}", "wstate"]
                add("copy", L)
        # E: reader over explicit bytes (declared length <= real length), reads across the end
        for i in range(40):
            n = pick(RBYTES, i, 2)
            declared = max(0, n - (i % 3)) if n else 0
            data = lcg_bytes(9000 + i, n)
            if i % 5 == 0:
                data = [0xFF] * n
            hexs = "".join(f"{x:02x}" for x in data) or "-"
            L = [f"mode {mode}", f"rhex {declared} {hexs}"]
            for k in range(6):
                b = pick(BITS + [32, 32], i * 11 + k, PRIMES[k % 4])
                L.append(["look", "read", "adv"][(i + k) % 3] + f" {b}")
                if (i + k) % 4 == 0:
                    L.append(["look1", "read1", "adv1"][(i + k) % 3])
            L += ["rstate", "look 0", "read 0", "look1", "read1", "rstate"]
            add("reader", L)
        # F: large writes, forcing several reallocations
        for i in range(8):
            n = [70, 130, 200, 400, 600, 900, 1200, 2000][i]
            b = pick(BITS[1:], i, 3)
            L = [f"mode {mode}", "winit"] + [f"w {(k * 2654435761) & 0xFFFFFFFF} {b}" for k in range(n)]
            L += ["wstate", "rfromw"] + [f"read {b}" for _ in range(5)] + ["rstate"]
            add("large", L)
        # G: invalid bit counts (write/look/read/adv); the C clears the buffer or returns -1
        for i, bb in enumerate(BAD_BITS):
            for j in range(2):
                ws = writes(i + j, 2, 503)
                L = [f"mode {mode}", "winit"] + [f"w {v} {b}" for v, b in ws]
                L += [f"w 1 {bb}", "wcheck", "wstate", "w 5 3", "walign", "wstate", "wreset", "wstate"]
                L += [f"rhex 4 {'a5c3' * 2}", f"look {bb}", f"read {bb}", "rstate", "read 4", "rstate"]
                if j:
                    L += [f"adv {bb if bb > 0 else 1}", "rstate", "look 1", "read 1"]
                add("bad_bits", L)
        # H: reset and reuse
        for i in range(8):
            ws = writes(i, 3, 601)
            L = [f"mode {mode}", "winit"] + [f"w {v} {b}" for v, b in ws] + ["wreset", "wstate"]
            L += [f"w {v} {b}" for v, b in writes(i, 2, 607)] + ["wstate", "wclear", "wcheck", "wstate", "winit", "w 3 2", "wstate"]
            add("reset", L)
    # I: the two families on one buffer (LSb writes read MSb and vice versa)
    for i in range(10):
        ws = writes(i, 4, 701)
        m1, m2 = ("L", "B") if i % 2 == 0 else ("B", "L")
        L = [f"mode {m1}", "winit"] + [f"w {v} {b}" for v, b in ws] + ["wstate", "rfromw", f"mode {m2}"]
        L += [f"read {b}" for _, b in ws] + ["rstate"]
        add("cross_family", L)
    return cases


# ---------------------------------------------------------------------------------------------
# framing.c
SIZES = [0, 1, 2, 17, 127, 254, 255, 256, 257, 509, 510, 511, 1000, 4095, 4096, 4097, 8000,
         65024, 65025, 65026, 70000, 100000]
SMALL = [0, 1, 2, 3, 17, 100, 254, 255, 256]
GRANS = [0, 1, -1, 2, 255, 256, 65535, 2**31 - 1, 2**31, 2**32, 2**63 - 1, -2]
SERIALS = [0, 1, -1, 0x7FFFFFFF, -2147483648, 12345, 255, 256]
FILLS = [0, 1, 255, 4096, 65307, 100000, -1]
CHUNKS = [1, 3, 27, 100, 4096, 1 << 20]
EMIT = ["drain 0", "drain 1", "drain 2 {n}", "drain 3 {n}"]


def packets(i, n, salt, sizes=SIZES):
    out = []
    g = 0
    for k in range(n):
        size = pick(sizes, i * 13 + k + salt, PRIMES[k % 6])
        if (i + k) % 5 == 0:
            g = pick(GRANS, i + k + salt, 3)
        else:
            g = g + size + 1 if 0 <= g < 2**62 else g
        out.append((size, 4000 + i * 101 + k, 1 if k == 0 else 0, 1 if k == n - 1 else 0, g, k))
    return out


def framing_cases():
    cases = []

    def add(fam, lines):
        cases.append({"id": f"fr{len(cases):04d}", "family": fam, "script": "\n".join(lines) + "\n"})

    def encode(i, pk, emit_idx, serial, fill):
        L = [f"einit {serial}"]
        for k, (sz, seed, bos, eos, g, pno) in enumerate(pk):
            L.append(f"pin {sz} {seed} {bos} {eos} {g} {pno}")
            if (i + k) % 3 == 0:
                L.append(EMIT[emit_idx].format(n=fill))
        L.append(EMIT[emit_idx].format(n=fill))
        L.append("drain 1")
        L.append("eeos")
        return L

    # R: encode -> decode round trips over packet sizes, emit modes, fill targets and feed chunks
    for i in range(110):
        n = 1 + (i % 8) + (i // 40)
        pk = packets(i, n, 0)
        serial = pick(SERIALS, i, 2)
        L = encode(i, pk, i % 4, serial, pick(FILLS, i, 5))
        L += ["yinit", f"dinit {serial}", f"decode {pick(CHUNKS, i, 3)} 0", "deos", "wcrc 0", "wcrc 1", "dcheck", "ycheck"]
        add("roundtrip", L)
    # N: many tiny packets (lacing table > 255 segments per page)
    for i in range(14):
        n = [100, 254, 255, 256, 300, 600, 1000][i % 7]
        L = [f"einit {pick(SERIALS, i, 3)}"]
        for k in range(n):
            L.append(f"pin {pick(SMALL[:6], i + k, 2)} {k} {1 if k == 0 else 0} {1 if k == n - 1 else 0} {k} {k}")
            if i >= 7 and k % 97 == 0:
                L.append("pout")
        L += ["drain 1", "yinit", "decode 4096 1", "deos"]
        add("many_packets", L)
    # X: corrupt one byte of the wire (header fields, lacing, body), then decode
    for i in range(70):
        pk = packets(i, 3, 17, SIZES[:14])
        L = encode(i, pk, i % 2, 4242, 4096)
        pos = [0, 1, 3, 4, 5, 6, 13, 14, 18, 22, 25, 26, 27, 28, 40, 100, 300, 1000][i % 18] + (i // 18) * 7
        x = [1, 0x80, 0xFF][i % 3]
        L += [f"wcorrupt {pos} {x}", "yinit", "dinit 4242", f"decode {pick(CHUNKS, i, 5)} 0", "deos"]
        add("corrupt", L)
    # L: page loss / reordering (holes and sequence gaps)
    for i in range(30):
        pk = packets(i, 4 + i % 4, 29, SIZES[:16])
        L = encode(i, pk, 1 + 2 * (i % 2), 77, pick([1, 255, 1000], i, 2))
        L.append(f"wdrop {i % 3}" if i % 2 == 0 else f"wswap {i % 3}")
        L += ["yinit", "dinit 77", f"decode {pick(CHUNKS, i, 3)} 0", "deos"]
        add("loss", L)
    # G: garbage before / inside the stream, and truncated streams
    for i in range(30):
        pk = packets(i, 3, 41, SIZES[:12])
        L = encode(i, pk, i % 4, 5, 255)
        if i % 3 == 0:
            L.append(f"wins 0 {pick([1, 3, 4, 27, 100, 5000], i, 2)} {i}")
        elif i % 3 == 1:
            L.append(f"wins {pick([4, 20, 27, 28, 200, 600], i, 2)} {pick([1, 2, 5, 100], i, 3)} {i}")
        else:
            L.append(f"wtrunc {pick([0, 1, 4, 26, 27, 28, 100, 300, 1000], i, 2)}")
        L += ["yinit", "dinit 5", f"decode {pick(CHUNKS, i, 5)} 0", "deos"]
        add("garbage", L)
    # M: manual sync API: pageseek / pageout step by step with tiny feeds, buffer/wrote edge cases
    for i in range(24):
        pk = packets(i, 2 + i % 3, 53, SIZES[:10])
        L = encode(i, pk, 1, 9, 0)
        if i % 4 == 1:
            L.append(f"wins 0 {1 + i} {i}")
        L += ["yinit"]
        step = pick([1, 2, 13, 27, 64, 512], i, 2)
        for k in range(12):
            L.append(f"feed {step * (1 + k % 3)}")
            L.append("yseek" if (i + k) % 2 else "ypout")
        L += ["ybuf 0", "ywrote 0", "ybuf 10", f"ywrote {pick([-1, 5, 10, 1 << 20], i, 3)}", "ycheck", "ypout", "yreset", "ypout", "ycheck"]
        add("sync_manual", L)
    # S: serial mismatch, version, BOS handling, reset_serialno on the decoder
    for i in range(16):
        pk = packets(i, 3, 61, SIZES[:9])
        s = pick(SERIALS, i, 2)
        L = encode(i, pk, 1, s, 0) + ["yinit", f"dinit {s + 1 if s < 2**31 - 1 else 0}", "decode 4096 0"]
        L += ["yreset", f"dserial {s}", "yinit", "decode 100 0", "deos", "dreset", "dcheck"]
        add("serial", L)
    # P: packetpeek / packetout interleavings
    for i in range(16):
        pk = packets(i, 2 + i % 5, 71, SIZES[:15])
        L = encode(i, pk, i % 2, 3, 0) + ["yinit", "dinit 3"]
        for k in range(6):
            L += ["feed 100000", "ypout", "dpin", "dpeekn", "dpeek", "dpeek" if k % 2 else "dpout", "dpout", "dpeek"]
        add("peek", L)
    # I: ogg_stream_iovecin
    for i in range(16):
        nv = 1 + i % 5
        lens = [pick(SIZES[:16], i * 5 + k, 3) for k in range(nv)]
        L = [f"einit {i}"]
        L.append(f"iov {nv} {' '.join(map(str, lens))} {i} 0 {pick(GRANS, i, 2)}")
        L.append(f"iov 2 {pick(SIZES, i, 5)} 3 {i + 1} {i % 2} {pick(GRANS, i, 3)}")
        L += ["drain 0", "drain 1", "yinit", f"dinit {i}", "decode 4096 0", "deos"]
        add("iovec", L)
    # Z: resets mid-stream, eos then more input, clear and check
    for i in range(16):
        pk = packets(i, 3, 83, SIZES[:12])
        L = encode(i, pk, i % 2, 11, 0)
        L += ["ereset", "echeck", f"pin 10 1 1 0 5 0", "drain 1", f"eserial {pick(SERIALS, i, 3)}", "pin 20 2 1 1 6 0", "drain 1"]
        L += [f"pin 5 3 0 0 7 1", "drain 1", "eeos", "yinit", "dinit 11", "decode 4096 0"]
        L += [f"dserial {pick(SERIALS, i, 3)}", "decode 4096 0", "deos", "eclear", "yclear"]
        add("reset", L)
    # H: heap-allocated states (destroy) and ogg_packet_clear
    add("heap", ["heap", "pclear", "heap"])
    return cases


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(out, exist_ok=True)
    for name, cs in (("bitwise", bitwise_cases()), ("framing", framing_cases())):
        with open(os.path.join(out, f"cases_{name}.json"), "w") as f:
            json.dump(cs, f, indent=0)
        fams = {}
        for c in cs:
            fams[c["family"]] = fams.get(c["family"], 0) + 1
        print(name, len(cs), fams)


if __name__ == "__main__":
    main()
