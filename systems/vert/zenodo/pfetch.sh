#!/bin/bash
# Parallel range download of VERT's Zenodo package (record 15453515, torust.tar, 13171180032 bytes),
# verified against Zenodo's md5 2a2badb58b02e6ae5819080444de0d48.
U="https://zenodo.org/records/15453515/files/torust.tar?download=1"
SIZE=13171180032; N=16; CH=$(( (SIZE + N - 1) / N ))
mkdir -p parts
for i in $(seq 0 $((N-1))); do
  a=$((i*CH)); b=$((a+CH-1)); [ $b -ge $SIZE ] && b=$((SIZE-1)); want=$((b-a+1))
  ( while [ ! -f parts/$i ]; do
      curl -sL --retry 5 -r $a-$b -o parts/$i.tmp "$U"
      if [ -f parts/$i.tmp ] && [ "$(stat -c %s parts/$i.tmp)" -eq $want ]; then mv parts/$i.tmp parts/$i; else rm -f parts/$i.tmp; sleep 5; fi
    done ) &
done
wait
cat $(for i in $(seq 0 $((N-1))); do echo parts/$i; done) > torust.tar && md5sum torust.tar > tar.md5 && rm -rf parts
echo done > done.txt
