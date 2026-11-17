# Systems (one folder each)

Each folder holds the system's pinned fetch script and our harness for it. Current counts
for every system are in [`../results/defect_ledger.json`](../results/defect_ledger.json) and the table
in the top-level [`README.md`](../README.md); the survey that selects these systems is
[`../docs/systems-survey.md`](../docs/systems-survey.md).

| Folder | System | Re-tested |
|---|---|---|
| `vert/` | VERT | scalar and array/float functions; Kani and bolero re-runs |
| `flourine/` | FLOURINE (artifact name `flourine`) | scalar functions and struct accessors |
| `syzygy/` | Syzygy | Zopfli, empty input |
| `c2saferrust/` | C2SaferRust | `cat`, `head`, `tail`, `uniq` (harness in `../harness/`) |
| `sactor/` | SACTOR | shipped complete translations with a C interface |
| `rustmap/` | RustMap | bzip2; Rosetta programs |
| `rustassure/` | RustAssure | libcsv and u8c crates; scalar functions |
| `ptrtrans/` | PtrTrans | `quadtree` |
| `his2trans/` | His2Trans | the 8 open-source benchmark libraries |
| `actoruw/` | ACTOR | executables and scalar/string-interface libraries |
| `adatrans/` | AdaTrans | first run's 104 programs (100 build); `pipeline.sh` |
| `rustine/` | Rustine | examined: its shipped C does not build into an oracle |


