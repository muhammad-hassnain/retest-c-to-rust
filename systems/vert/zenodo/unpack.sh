#!/bin/bash
# Rebuild the final filesystem of VERT's Zenodo image (aidanben/torust, record 15453515) without
# `docker load`: extract each layer of manifest.json in order into rootfs/, applying OCI whiteouts
# (.wh.NAME deletes NAME; .wh..wh..opq empties the directory). Skips system paths we do not need.
set -e
cd "$(dirname "$0")"
mkdir -p img rootfs
[ -f img/manifest.json ] || tar -xf torust.tar -C img
python3 - <<'PY'
import json, os, shutil, subprocess, tarfile
m = json.load(open("img/manifest.json"))[0]
skip = ("usr/", "lib/", "lib64/", "bin/", "sbin/", "var/", "etc/", "boot/", "opt/wasi-sdk", "run/", "proc/", "sys/", "dev/", "tmp/")
for i, layer in enumerate(m["Layers"], 1):
    p = os.path.join("img", layer)
    with tarfile.open(p) as t:
        members = []
        for x in t:
            n = x.name.lstrip("./")
            if n.startswith(skip):
                continue
            base = os.path.basename(n); d = os.path.dirname(n)
            if base == ".wh..wh..opq":
                tgt = os.path.join("rootfs", d)
                if os.path.isdir(tgt):
                    for c in os.listdir(tgt):
                        cp = os.path.join(tgt, c)
                        shutil.rmtree(cp) if os.path.isdir(cp) and not os.path.islink(cp) else os.remove(cp)
                continue
            if base.startswith(".wh."):
                tgt = os.path.join("rootfs", d, base[4:])
                if os.path.lexists(tgt):
                    shutil.rmtree(tgt) if os.path.isdir(tgt) and not os.path.islink(tgt) else os.remove(tgt)
                continue
            if x.isdev():
                continue
            members.append(x)
        t.extractall("rootfs", members=members, filter="tar")
    print("layer", i, layer[-12:], "done", flush=True)
PY
find rootfs -name c_results.csv -not -path "*/target/*"
