"""Held-out input generator for Syzygy's Zopfli -- SYSTEMATIC + DETERMINISTIC (v2).

No randomness: a fixed corpus of byte streams covering the structural cases that drive a
compressor -- empty, tiny, long runs (highly compressible), text, and a deterministic
"incompressible" pattern (an LCG byte sequence) at several sizes. The divergences it exposes are
on stderr only: streams containing a NUL byte (the C driver sizes input with strlen, the translated
driver uses the whole buffer) and the empty stream. The same corpus is produced every run; it is
held-out (Syzygy ships no test inputs of its own).
"""
from common import Case, b64

SIZES = [0, 1, 2, 7, 31, 100, 512, 2048, 8192, 32768]


def _incompressible(n):
    # deterministic pseudo-random bytes (LCG) -- looks random, so it does not compress
    out = bytearray(); x = 0x12345678
    for _ in range(n):
        x = (1103515245 * x + 12345) & 0xFFFFFFFF
        out.append((x >> 16) & 0xFF)
    return bytes(out)


def _runs(n):
    out = bytearray()
    b = 0
    while len(out) < n:
        out += bytes([b]) * 17
        b = (b + 1) & 0xFF
    return bytes(out[:n])


def _text(n):
    s = b"the quick brown fox jumps over the lazy dog\n"
    return (s * (n // len(s) + 1))[:n]


def generate(n=None, seed=None):
    """Deterministic corpus. n/seed are ignored (kept for CLI compatibility)."""
    cases = []
    i = 0
    for size in SIZES:
        for kind, fn in (("incompressible", _incompressible), ("runs", _runs), ("text", _text)):
            body = fn(size) if size else b""
            cases.append(Case(id=f"zopfli-{i:04d}", util="zopfli", argv=[],
                              stdin_b64=b64(body), note=f"{kind}:{size}"))
            i += 1
    return cases
