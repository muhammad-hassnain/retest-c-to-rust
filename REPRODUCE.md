# Reproducing the paper's numbers

Every number in the paper is computed from the files in `results/` (plus two files under
`systems/rustassure/*/results.json`). This page says what you can re-check offline in minutes, what needs the
third-party downloads, and how to re-run the experiments themselves. Nothing here modifies the
shipped `results/`, `explorer/index.html` or `dashboard.html`: all outputs go to `out/`
(or `/out` in the image).

Everything below marked *tested* was run on 2026-11-13 on an x86_64 Linux machine (Docker 29.1,
4 CPUs allotted) and on an arm64 Mac, from the repository state the reviewer image was built from.

## 1. Quick start: offline, no downloads (under a minute)

### With the reviewer image (`crust-artifact`, x86_64)

```sh
bash docker/build_artifact_image.sh                   # builds crust-artifact from docker/Dockerfile.artifact
docker run --rm crust-artifact                        # or: docker run --rm -v "$PWD/out:/out" crust-artifact
```

The image is not distributed: no `crust-artifact.tar.gz` is shipped or linked, so build it with
`docker/build_artifact_image.sh` (2 to 4 minutes, see "How long" below; `SAVE=crust-artifact.tar.gz` also
writes a `docker save` tarball and prints its sha256).

The image contains this repository (no third-party code), gcc 11.4, rustc 1.86.0 and
nightly-2022-08-08, python3 and matplotlib. Its default command (`docker/quickstart.sh`) does four
things (*tested* from a checkout on the Mac, 2026-11-17; not yet re-run inside the image):

1. **Pipeline verify.** `scripts/pipeline.py verify` re-runs the cheap stages of `pipeline.json`
   (the per-system outcome table, the figures, the code listings, the second-annotator agreement, and the defect ledger when
   `artifacts/` is present) and compares each output with the shipped file after normalisation. From a
   checkout without `artifacts/` it prints `9 match, 0 mismatch/fail, 3 skipped`: the defect-ledger stage
   needs `artifacts/`, and two stages print to stdout only. Without matplotlib or Pygments the figure and
   code-listing stages are reported SKIP too (`6 match, 0 mismatch/fail, 6 skipped`), and the exit status
   is still 0. A figure PDF's bytes depend on the matplotlib version and the fonts installed (the shipped
   ones were drawn with matplotlib 3.8.0 and Arial); where they differ, `verify` compares the numbers the
   figure scripts print instead and reports MATCH with "plotted values equal; PDF bytes differ".
2. **The figures**: the paper's Figs. 1 to 3 (pipeline, outcomes, overlap), plus two not in the paper (VERT's checks,
   harness power), redrawn from `results/` into
   `out/figures/`, with the plotted numbers printed.
3. **The defect explorer** (`out/explorer/index.html`), a self-contained page over all 180 candidate
   defects (160 counted, each other one with the reason it is not counted), rebuilt from
   `results/defect_ledger.json`. Without the downloads its source-code panes are empty; upstream links remain.
4. **The dashboard** (`out/dashboard.html`): the paper's headline numbers, the per-system table and a
   map of the artifact, each number computed from the result files.

Exit status 0 means every re-run stage matches the shipped files.

### From a checkout (python3 only; matplotlib for the figures, Pygments for the code listings)

```sh
python3 scripts/pipeline.py verify                      # -> out/pipeline/
python3 scripts/plot_results.py --out out/figures       # Figs. 1 and 2, VERT's checks, harness power (matplotlib)
python3 scripts/plot_overlap.py --out out/figures       # Fig. 3 (matplotlib)
python3 -I scripts/build_explorer.py                    # -> out/explorer/index.html
python3 -I scripts/build_dashboard.py                   # -> out/dashboard.html
bash docker/quickstart.sh                               # all four
```

*Tested* (Mac, 2026-11-17) from a checkout with no `artifacts/`, with and without matplotlib and Pygments:
`bash docker/quickstart.sh` exits 0, and `results/` is unchanged afterwards.

## 2. Checks that read the third-party artifacts

The released translations we re-test are not redistributed. Each is fetched at a pinned commit (the
fetch script fails if the checked-out commit differs) or, for FLOURINE, a pinned sha256:

| directory | fetch | pin | size of our copy* |
|---|---|---|---|
| `artifacts/vert/` | `bash systems/vert/fetch.sh` | `3270cec` | 1.3 GB |
| `artifacts/flourine_dl/` | `bash systems/flourine/fetch.sh` | tarball sha256 `87e570d4…0a32` (54 MB) | 247 MB |
| `artifacts/rustassure/` | `bash systems/rustassure/fetch.sh` | `3961840` | 661 MB |
| `artifacts/c2saferrust/` | `bash scripts/fetch_artifact.sh` | `e8d5c3a` | 563 MB |
| `artifacts/syzygy_zopfli/` | `bash systems/syzygy/fetch.sh` | `e0e7666` | 33 MB |
| `artifacts/rustmap/` | `bash systems/rustmap/fetch.sh` | `cb1df95` | 2.0 GB |
| `artifacts/rustine/` | `bash systems/rustine/fetch.sh` | `774ff51` | 2.6 GB |
| `artifacts/actoruw/` | `bash systems/actoruw/fetch.sh` | ACTOR `1b1e70a`, results `e31aec0` | 293 MB |
| `artifacts/adatrans/` | `bash systems/adatrans/fetch.sh` | `31f4031` | 153 MB |
| `artifacts/his2trans/` | `bash systems/his2trans/fetch.sh` | `3a0ec55` | 338 MB |
| `artifacts/ptrtrans/` | `bash systems/ptrtrans/fetch.sh` | `b20d5bb` | 74 MB |
| `artifacts/sactor/` | `bash systems/sactor/fetch.sh` | sactor `577c3d2`, datasets `1060b3b`, CRUST-bench `69970f9` | 197 MB |

\* on-disk size of our copy, which includes build outputs left by our runs; about 8.3 GB in all.
`bash scripts/provision.sh artifacts` fetches all twelve.

Then, *tested* on the Mac with our local `artifacts/` (2026-11-17):

```sh
python3 scripts/pipeline.py verify    # 10 match, 0 mismatch/fail, 2 skipped (stdout-only stages)
```

## 3. Re-running the experiments

`scripts/run_all.sh` runs every step behind a paper number, in a scratch copy of the repository
(`$OUT/work`, default `out/rerun-<UTC time>`), so the shipped `results/` is never overwritten. At
the end it redraws the figures, copies every result file that changed to `$OUT/results/` and lists it in
`$OUT/changed.txt`, and keeps each step's log and environment stamp (`$OUT/logs/`, `$OUT/provenance/`).

```sh
bash scripts/run_all.sh --list                    # every step: image, artifact, arch, output files
bash scripts/provision.sh images                  # crust-stable, crust-heldout, crust-syzygy (docker/)
docker build -t crust-kani -f docker/Dockerfile.kani docker/      # for the two Kani steps
STEPS="syzygy_empty flourine_scalar" bash scripts/run_all.sh      # a subset; default: all paper steps
```

Inside the reviewer image the same script runs the steps directly (no Docker needed), selecting the
toolchain each step's image pins: mount the artifacts and an output directory,

```sh
docker run --rm -v "$PWD/artifacts:/artifacts" -v "$PWD/out:/out" crust-artifact \
  bash -c 'OUT=/out/rerun STEPS="syzygy_empty flourine_scalar flourine_struct" bash scripts/run_all.sh'
```

*Tested* this way on x86_64: `syzygy_empty` ran in 6 s and wrote a `results/syzygy_empty_deflate.json`
byte-identical to the shipped one. *Tested* in Docker mode on the arm64 Mac (2 CPUs):
`rustassure_scalar` (2 s, byte-identical output) and `flourine_scalar` (433 s; the same counts as the
shipped `results/flourine_diff.json`, and byte-identical to `results/rerun-2026-11-12/flourine_diff_spread.json`,
the re-run on the current input grid).

**Which machine.** The paper's runs used two machines; `run_all.sh --list` gives the arch for each
step where our notes record it. The primary VERT scalar run and the C2Rust-baseline grids
ran on arm64 (Apple silicon, Docker; `pipeline.json` records arm64 for the VERT runs); the
C2SaferRust `rust_WIP` grid and its mutation analysis, the VERT x86-64 cross-check and the VERT
mutation analysis ran on x86_64. `results/PROVENANCE.json` cites the sentence for each file.
Syzygy and RustMap-bzip2 build only on x86_64 (the artifacts hard-code x86_64 paths and types); on
an arm64 host Docker runs them under emulation (`--platform linux/amd64`). The comparisons are
internally consistent on either arch (oracle and translation share the machine), but a few counts
are arch-dependent and documented as such (the VERT x86-64 run adds 5 original-C-UB cases; RustAssure
u8c differs because `char` signedness differs).

**How long.** Measured: quickstart 8 s (x86_64, 2 CPUs) to 14 s (arm64, with the artifacts); VERT scalar about 32 min and FLOURINE scalar about
1.3 min (arm64 Mac, crust-stable; logs in `results/rerun-2026-11-12/`); FLOURINE scalar 7.2 min with Docker
capped at 2 CPUs; Syzygy empty input 6 s; RustAssure scalar 2 s; Kani on one VERT function about
50 s (`crust-kani`; VERT records 90 bounded-Kani passes). Images: `crust-artifact` builds in about
4 min (x86_64, 4 CPUs) or 2 min (arm64, 2 CPUs), `crust-kani` in about 4 min. The other steps
were not timed in this pass; the C2SaferRust grid (8,574 cases, 4 utilities, plus mutation) and the
Rustine build matrix (24, 14 and 6 programs in its three stages, under two toolchains) are the longest.

**Not regenerated by any script:** `results/vert_arrays_adjusted.json` (two hand reclassifications of
`vert_arrays.json`, each with its reason in the file) and `results/rustmap_rosetta_triage.json` (the
hand triage of the 37 Rosetta divergences as done for the submission: 11 counted, 8 set aside as a
different task). The defect ledger reads both. For Rosetta it re-applies the v2 rules and moves two of
the eight to counted (`meta.RustMap.rosetta_triage_v2` in `results/defect_ledger.json`, with the reason
for each), which gives the paper's 13 counted and 6 set aside; the triage file says so in
`counts_superseded_by_ledger`.

## 4. Pinning and provenance

* **Images** (`docker/`): every base image is pinned by digest, the multi-arch manifest list
  `ubuntu:22.04@sha256:0a988793…8956`. On amd64, apt installs from the Ubuntu snapshot archive of
  2026-10-19 (`snapshot.ubuntu.com`, the day the x86_64 images were built); Ubuntu publishes no arm64
  snapshots, so arm64 builds use the live archive. rustup is pinned to 1.29.1 (sha256-checked, self-update
  off); toolchains are pinned by version. Kani 0.55.0 is installed when `crust-kani` is built, not at
  run time (its `cargo kani setup` also pip-installs cbmc-viewer and its Python dependencies, which are
  not version-pinned). Three images used by single stages are not built by `scripts/provision.sh`:
  `audit3-stable186` (the FLOURINE mutation analyses; recipe `docker/Dockerfile.audit3-stable186`, the
  pre-pinning `docker/Dockerfile.stable` with its base pinned by digest; image ID `sha256:c62c2fe8…0112`),
  `crust-heldout-arxiv-arm64` (`docker/Dockerfile` built for arm64; the aarch64 C2SaferRust `head` runs)
  and the public `rust:1.99.1-bookworm`, used as
  `rust:1.99.1-bookworm@sha256:7085845219c18e7c5b1f34ed9ce89b84f822f2e3d54a02774cedc996ac0f304f` (the
  His2Trans and PtrTrans rustc 1.99.1 cross-checks). `pipeline.json` (`images`) gives the recipe of every
  image. `docker/manifests/` lists every package installed in the images our results
  came from. Fetched at run time: crates.io dependencies of the translated crates (pinned by their
  `Cargo.lock` where the artifact ships one; e.g. VERT's Kani harnesses), VERT's bolero fork for the
  bolero step (pinned to a git commit), and Rustine's `nightly-2025-05-25` toolchain.
* **FLOURINE** has no git history; `systems/flourine/fetch.sh` checks the tarball's sha256.
* **Result files:** `results/PROVENANCE.json` maps every file under `results/` to the commit of the
  development repository (where the study was run; not shipped) that last changed it, says whether this
  copy differs from that commit (machine identity scrubbed, or rebuilt in this artifact), and, where
  `pipeline.json` records it, the arch/image it was produced on. `python3 -I scripts/provenance.py --index --check`
  compares its file list and recorded hosts with the tree; the commits can be checked only against the
  development repository (`--index --dev-repo DIR`). New runs record their environment with
  `scripts/provenance.py` (`run_all.sh` does this per step). The commit hashes in the result files'
  provenance (`git_rev`, `git_commit`) also refer to the development repository, not to this repository's history.
  The two result files rebuilt in this repository, `results/defect_ledger.json` and `results/his2trans_summary.json`,
  say so in `git_rev` instead of giving a hash (this repository is one amended commit); the ledger pins its inputs by
  sha256 (`provenance.inputs_sha256`).

## 5. Figures

`scripts/plot_results.py` draws Fig. 1 (`pipeline.pdf`) and Fig. 2 (`outcomes.pdf`) from `results/`, plus two figures
the paper does not use (`vert_checks.pdf`, `harness_power.pdf`); `scripts/plot_overlap.py` draws Fig. 3 (`overlap.pdf`)
and `scripts/make_snippet_listings.py` the code listings (Figs. 4 to 7). `python3 scripts/pipeline.py verify` redraws all of them and compares them with
`results/PIPELINE_HASHES.json`. *Tested*: on the Mac (matplotlib 3.8.0, Arial) both PDFs are byte-identical to
the ones in `paper/main.pdf` apart from `/CreationDate`. In the image
(matplotlib 3.5.1, DejaVu Sans) the bars and labels have the same values, but the bytes differ, so `verify`
there compares the plotted values (section 1).
`plot_results.py --out DIR` also writes `DIR/harness_power.json`; only the default run (no `--out`, no
`--check`) rewrites `results/harness_power.json`.

`scripts/make_snippet_listings.py` writes the code listings from `results/code_snippets.json`, one
`snip-<system>.tex` per system (FLOURINE's as `snip-fluorine.tex`, the name the paper inputs). It needs
Pygments for the syntax colouring (`pip install pygments`).

## 6. Pipeline

`pipeline.json` (repository root) lists every stage of every system we re-test, in run order: fetch, build,
inputs, diff, ub, heldout, acceptance, power / mutation, toolchain, summary, and the cross-system defect ledger, tables
and figures. Each stage records the exact command (from the repository root), where it runs (host, Docker image,
or a repository script that starts Docker itself; the architecture of the shipped result's run and the sentence
that documents it), the inputs it needs (`artifacts/<system>`, earlier result files), the files it writes,
whether it is deterministic (and its seed), and a rough runtime where the docs or a result file give one. A
command that cannot be derived from the repository is `"unknown"` with the reason; `results_without_stage` lists
the result files no script regenerates (hand triage, archival runs). `systems/adatrans/pipeline.sh` is
referenced, not reimplemented. `scripts/pipeline.py` (Python standard library only) drives it:

```sh
python3 scripts/pipeline.py list [--system vert]            # all stages: image, arch, cheap?, unknown?
python3 scripts/pipeline.py show vert.diff                  # one record (or a whole system: show vert)
python3 scripts/pipeline.py run vert,flourine.diff          # into out/pipeline/ (scratch copy), never over results/
python3 scripts/pipeline.py run syzygy --out /tmp/x --in-place   # --in-place overwrites the shipped results/
python3 scripts/pipeline.py hashes                          # (re)writes results/PIPELINE_HASHES.json
python3 scripts/pipeline.py verify                          # re-run the cheap stages, compare hashes
python3 scripts/pipeline.py spotcheck vert.diff rustassure.diff   # small fixed subset, per-case verdicts
```

The re-runs added for the arXiv version are the stages named `arxiv_*` (`python3 scripts/pipeline.py list | grep arxiv`)
and `cross.profile_check_flourine_fuzz` / `cross.profile_check_vert_wide`; they write `results/arxiv_*.json`,
`results/profile_check_vert_wide.json` and `results/profile_check.json`. The defect
ledger takes FLOURINE's verdicts from the run in FLOURINE's own build profile (`results/arxiv_flourine_devprofile*.json`),
and `scripts/plot_results.py` takes the C2SaferRust, ACTOR and FLOURINE harness-power rows from the UB-filtered re-scans.

`bash scripts/run_all.sh` is a thin wrapper over `pipeline.py run` (same options, old step names still accepted).

`run` works in a scratch copy of the repository, so a stage that writes `results/<x>.json` overwrites only the
copy and later stages read the fresh files; each output it produces is copied to `<out>/outputs/` and labelled
equal to or different from the shipped file after normalisation. It skips, and says why, a stage whose artifact
is not fetched, whose image is not built, that needs the other architecture (`--emulate` tries Docker emulation)
or a network (`--network`), or whose command is unknown or a template. Stages that start Docker through a repository
script with a fixed directory layout (`systems/his2trans/run_in_docker.sh`, `systems/sactor/run_docker.sh`,
`systems/actoruw/run_in_container.sh`, `systems/ptrtrans/quadtree_e2e/run_in_docker.sh`)
are run in a private layout under `<out>/managed/<system>`; `--managed-src SYSTEM=DIR` supplies the parts that cannot
be copied (a vendored crates directory, the fetched artifact).

**Normalisation.** `hashes` and `verify` compare sha256 of each file after dropping what legitimately varies:
in JSON every `provenance`/`_provenance` object and every key that records a date, timestamp or duration, and the
repository's absolute path in strings (under its current or earlier folder name); in PDF `/CreationDate` and `/ModDate`.
For the figure stages, `hashes` also records a digest of the numbers the plotting scripts print (the plotted values,
without the font line, warnings or output paths); `verify` falls back to it when only PDF bytes differ.

**`verify`** re-runs only the cheap deterministic stages (no Docker, seconds): the C2SaferRust input
generator (`harness/gen_inputs.py`, the four `*_cases_v4.json` grids), His2Trans's
`xcheck combine` and `summarize.py`, SACTOR's `build_summary.py`, the defect ledger (needs `artifacts/`; its two probe
inputs are taken from the shipped ledger's `meta`), the outcome table, the figures and `harness_power.json` (need
matplotlib; the fresh `harness_power.json` is compared with `results/harness_power.json`), the code listings (need
Pygments) and the second-annotator agreement. A stage whose Python module is missing is reported SKIP, not FAIL. For
each it prints MATCH, MISMATCH or SKIP, comparing the fresh output with the shipped file and with
`results/PIPELINE_HASHES.json` (so a shipped file edited after `hashes` was written is also caught). It does not
re-run any measurement; that is what `run` and `spotcheck` are for. Exit status 0 means every re-run stage matched.

*Tested* on 2026-11-13 (Mac arm64 Docker, x86-64 Linux box, 2 CPUs each): `verify` matched the cheap stages, and `spotcheck` re-ran each system's diff stage on a
small fixed subset and reproduced every per-case verdict of the shipped rows: VERT scalar 26/26 and arrays 92/92
(arm64), FLOURINE 18/18 (arm64), Syzygy 30/30, C2SaferRust 459/459 (first 100 cases plus up to 25 shipped mismatches per
utility), RustMap bzip2 20/20, RustMap Rosetta 48/49 (x86-64; `machine_code` is the program the study sets aside as
nondeterministic; on arm64 41/48, a few Rosetta outputs are architecture-dependent), RustAssure 12/12 and libcsv 9/9,
His2Trans 569/569 (qsort, ht, rgba), SACTOR libogg 8/8 and CRUST grid 4/4, PtrTrans 18/18, ACTOR 4/4.
