"""Coverage helper for harness/mutate_full.py: region-level source coverage of the C2Rust
translation on the full v4 grid, without an LLVM-14 llvm-profdata.

The translations are pinned to rustc nightly-2022-08-08 (LLVM 14.0.6), whose
`-C instrument-coverage` runtime writes raw profiles in format version 8. The only
llvm-profdata/llvm-cov on the x86 box is LLVM 21, which refuses raw version 8 but still reads
(a) the text profile format and (b) older coverage-mapping versions in the binary. So:

  1. build the unmodified baseline with RUSTFLAGS='-C instrument-coverage' (separate target dir),
  2. run the full grid through the SAME harness comparison (difftest.compare_case), with
     LLVM_PROFILE_FILE pointing at a per-process .profraw,
  3. `profraw_to_text` (below) decodes each raw-v8 file and sums the counters per function,
  4. LLVM 21 `llvm-profdata merge` turns the text profile into an indexed one, and
     `llvm-cov export` maps counters to source regions (line:col spans) of <util>.rs.

The decoder follows LLVM 14's InstrProfData.inc / InstrProfReader.cpp for raw version 8:
  header: 11 x u64 (Magic, Version, BinaryIdsSize, DataSize, PaddingBytesBeforeCounters,
          CountersSize, PaddingBytesAfterCounters, NamesSize, CountersDelta, NamesDelta,
          ValueKindLast)
  binary ids (BinaryIdsSize bytes), then DataSize records of 48 bytes
          (NameRef u64, FuncHash u64, CounterPtr i64 (relative to the record),
           FunctionPointer u64, Values u64, NumCounters u32, NumValueSites u16[2]),
  padding, CountersSize x u64 counters, padding, names (uleb128 len, uleb128 zlen, data,
  '\\x01'-separated, zlib if zlen != 0).
Validation: `selfcheck` cross-checks that every NameRef equals MD5(name)[:8] (little-endian),
which fails loudly if the layout assumption is wrong.
"""
import hashlib
import os
import struct
import sys
import zlib

MAGIC = 0xff6c70726f667281  # little-endian 64-bit raw magic


def _uleb(buf, i):
    val = shift = 0
    while True:
        b = buf[i]
        i += 1
        val |= (b & 0x7f) << shift
        shift += 7
        if not b & 0x80:
            return val, i


def _names(blob):
    out = []
    i = 0
    while i < len(blob):
        ulen, i = _uleb(blob, i)
        zlen, i = _uleb(blob, i)
        if ulen == 0 and zlen == 0:
            break
        if zlen:
            data = zlib.decompress(blob[i:i + zlen])
            i += zlen
        else:
            data = blob[i:i + ulen]
            i += ulen
        out += [n.decode() for n in data.split(b"\x01") if n]
    return out


def _md5ref(name):
    return struct.unpack("<Q", hashlib.md5(name.encode()).digest()[:8])[0]


def parse_profraw(path):
    """Return {name: (funchash, [counters])} for one raw-v8 profile."""
    buf = open(path, "rb").read()
    hdr = struct.unpack_from("<11Q", buf, 0)
    (magic, version, bin_ids, n_data, pad_before, n_counters, pad_after, names_size,
     counters_delta, _names_delta, _vkl) = hdr
    if magic != MAGIC:
        raise ValueError(f"{path}: bad magic {magic:#x}")
    if version & 0xffffffff != 8:
        raise ValueError(f"{path}: raw version {version & 0xffffffff}, expected 8")
    off = 88 + bin_ids
    recs = []
    for k in range(n_data):
        nameref, fhash, cptr, _fp, _vals, ncnt, _v0, _v1 = struct.unpack_from(
            "<QQqQQIHH", buf, off + 48 * k)
        recs.append((nameref, fhash, cptr, ncnt))
    cnt_off = off + 48 * n_data + pad_before
    counters = struct.unpack_from(f"<{n_counters}Q", buf, cnt_off)
    names_off = cnt_off + 8 * n_counters + pad_after
    names = _names(buf[names_off:names_off + names_size])
    by_ref = {_md5ref(n): n for n in names}
    out = {}
    delta = counters_delta - (1 << 64) if counters_delta >= (1 << 63) else counters_delta
    for k, (nameref, fhash, cptr, ncnt) in enumerate(recs):
        # LLVM 14 RawInstrProfReader: CounterBaseOffset = CounterPtr - CountersDelta, and
        # CountersDelta shrinks by sizeof(Data) per record (pointer is record-relative).
        base = (cptr - delta) // 8
        delta -= 48
        if nameref not in by_ref:
            raise ValueError(f"{path}: NameRef {nameref:#x} not in names section")
        if base < 0 or base + ncnt > n_counters:
            raise ValueError(f"{path}: counter range out of bounds for {by_ref[nameref]}")
        out[by_ref[nameref]] = (fhash, list(counters[base:base + ncnt]))
    return out


def merge_profraws(paths):
    """Sum counters over raw profiles. Empty files (a process killed by a signal -- e.g. the
    rust_WIP segfaults -- leaves a 0-byte profile) are skipped and counted on stderr."""
    total = {}
    empty = [p for p in paths if os.path.getsize(p) == 0]
    if empty:
        print(f"[cov] skipping {len(empty)} empty profraw files (crashed runs)", file=sys.stderr)
    for p in paths:
        if os.path.getsize(p) == 0:
            continue
        for name, (fh, cs) in parse_profraw(p).items():
            if name in total:
                ofh, ocs = total[name]
                if ofh != fh or len(ocs) != len(cs):
                    raise ValueError(f"hash/size mismatch for {name}")
                total[name] = (fh, [a + b for a, b in zip(ocs, cs)])
            else:
                total[name] = (fh, cs)
    return total


def write_text_profile(total, out_path):
    with open(out_path, "w") as f:
        for name in sorted(total):
            fh, cs = total[name]
            f.write(f"{name}\n# Func Hash:\n{fh}\n# Num Counters:\n{len(cs)}\n"
                    "# Counter Values:\n")
            for c in cs:
                f.write(f"{c}\n")
            f.write("\n")


if __name__ == "__main__":
    # usage: mutate_full_cov.py OUT.proftext a.profraw [b.profraw ...]
    out, paths = sys.argv[1], sys.argv[2:]
    tot = merge_profraws(paths)
    write_text_profile(tot, out)
    print(f"[cov] {len(paths)} profraw files, {len(tot)} functions -> {out}", file=sys.stderr)
