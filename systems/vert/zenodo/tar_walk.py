"""List and selectively fetch members of VERT's Zenodo replication package (record 15453515,
torust.tar, 13.17 GB) with HTTP range requests, without downloading the whole file.

  python3 -I tar_walk.py list                 # prints offset, size, name of every top-level tar member
  python3 -I tar_walk.py get NAME OUT         # fetches one member's bytes to OUT
"""
import json, sys, urllib.request

URL = "https://zenodo.org/records/15453515/files/torust.tar?download=1"


def rng(a, b):
    req = urllib.request.Request(URL, headers={"Range": f"bytes={a}-{b}", "User-Agent": "tar-walk"})
    with urllib.request.urlopen(req) as r:
        assert r.status == 206, r.status
        return r.read()


def walk():
    off, out, longname = 0, [], None
    while True:
        h = rng(off, off + 511)
        if h == b"\0" * 512:
            break
        name = h[0:100].rstrip(b"\0").decode()
        prefix = h[345:500].rstrip(b"\0").decode()
        size = int(h[124:136].rstrip(b"\0 ").decode() or "0", 8)
        typ = h[156:157]
        data = off + 512
        if typ == b"L":
            longname = rng(data, data + size - 1).rstrip(b"\0").decode()
        else:
            full = longname or ((prefix + "/" if prefix else "") + name)
            longname = None
            out.append({"name": full, "type": typ.decode() or "0", "size": size, "data_offset": data})
        off = data + (size + 511) // 512 * 512
    return out


if __name__ == "__main__":
    if sys.argv[1] == "list":
        m = walk()
        json.dump(m, sys.stdout, indent=1)
    else:
        m = {x["name"]: x for x in json.load(open(sys.argv[4]))} if len(sys.argv) > 4 else {x["name"]: x for x in walk()}
        x = m[sys.argv[2]]
        open(sys.argv[3], "wb").write(rng(x["data_offset"], x["data_offset"] + x["size"] - 1))
