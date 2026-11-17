# AGENTS.md: working in this repository

Guidance for AI coding agents (and people) who need to understand, check or extend this artifact.
Read this first, then `README.md` (what was found) and `REPRODUCE.md` (how to re-check it).

## What this repository is

The artifact of the paper "Re-Testing LLM-Based C-to-Rust Translations Against Their Original C"
(arXiv version, `paper/main.pdf`). Eleven C-to-Rust systems released their Rust translations and the
original C. For each, we build the C as the oracle and the system's Rust, run both on generated
inputs, compare every observable output, and count a divergence only where the C has no undefined
behavior. Divergences are grouped into defects, each confirmed in the translated source. For each
defect we also measure, from the system's own artifact, whether its check accepted the translation
and whether that check ever ran an input that exposes the defect.

Every number in the paper comes from a file in `results/`. Nothing in this repository calls a
language model; the translations under test are third-party and are fetched, not shipped.

## Layout

| Path | What it holds |
|---|---|
| `results/` | every result file behind the paper's numbers (JSON, plus some log directories) |
| `results/defect_ledger.json` | one row per candidate defect (180), 160 counted; the hub most numbers come from |
| `results/PIPELINE_HASHES.json` | normalised sha256 of every stage output; `pipeline.py verify` compares against it |
| `results/PROVENANCE.json` | each result file -> the development-repository commit that produced it (see below) |
| `pipeline.json` | every stage of every system: exact command, image, arch, inputs, outputs, determinism |
| `systems/<name>/` | per-system fetch script (pinned commit), drivers and notes; `systems/vert/run.py` etc. |
| `systems/enumerate_inputs.py`, `systems/batch_diff.py` | shared input grids and the batch C-vs-Rust comparison |
| `harness/` | shared harness: input generation, the UB rule (`oracle_ub_common.py`), held-out checks, mutation analysis |
| `scripts/` | ledger builder, tables, figures, pipeline driver, explorer, dashboard, provenance |
| `docker/` | Dockerfiles (bases pinned by digest), `quickstart.sh`, package manifests |
| `docs/` | 34-system survey with evidence, partial comparisons, second-rater labeling data |
| `explorer/` | defect explorer (`index.html`) and labeling app (`rate.html`), served by `explorer/serve.py` |
| `paper/` | the paper's PDF (`paper/main.pdf`); its figures are drawn from `results/` by `scripts/` |
| `artifacts/` | NOT shipped (gitignored): third-party artifacts fetched by `systems/*/fetch.sh` (about 8 GB) |
| `out/` | NOT shipped (gitignored): scratch output of every reproduction command |
| `LICENSE`, `LICENSE-DATA` | MIT for code; CC BY 4.0 for data and documentation (README, section Licence) |

## Ground rules

1. **Never hand-edit a result file.** Change the code that produces it and re-run its stage
   (`python3 scripts/pipeline.py show <system.stage>` prints the command). The only hand-made result
   files are `results/vert_arrays_adjusted.json` and `results/rustmap_rosetta_triage.json`, each with its
   reasons inside; `pipeline.json` lists them under `results_without_stage`.
2. **Never hand-edit `results/defect_ledger.json`.** It is written by
   `python3 -I scripts/build_defect_ledger.py`, which reads the per-system result files, re-checks every
   cited source location and runs about 1,460 internal checks; it needs `artifacts/`. Mechanisms for
   VERT and RustMap are curated lists inside that script (`CURATED_VERT`, `CURATED_ROSETTA`, `CURATED_BZIP2`).
3. **Keep the counting rules.** A divergence counts only on an input where the C passes the UB rule:
   ASan + UBSan (including float-cast-overflow) under GCC and Clang, identical results at `-O0`, `-O1`
   and `-O2` (and with `-fwrapv`), and independent of `char` signedness (`harness/oracle_ub_common.py`).
   Not counted (rules i-v): C UB; inputs that break a precondition the original states; a value the C
   standard leaves unspecified; a callback that frees memory; a negative value passed to `%u` / `%x`.
   The oracle is the C as built for LP64 Linux with glibc (64-bit `long` is part of the C).
   A *defect* is one mechanism in the translated source, confirmed by reading it; input counts are
   exposure only.
4. **Do not overwrite shipped outputs by accident.** `pipeline.py run` works in a scratch copy under
   `out/`; only `--in-place` writes into `results/`. `scripts/plot_results.py` without `--out` writes
   `out/figures/*.pdf` and rewrites `results/harness_power.json`; pass `--out out/figures` to leave
   `results/` as it is.
5. **After any change to a result file**, in this order: rebuild the ledger (if it reads the file),
   `python3 -I scripts/make_tables.py --md` (README table), the figures and code listings if affected,
   `python3 scripts/pipeline.py hashes`, then `python3 scripts/pipeline.py verify` must report
   `0 mismatch/fail`. Every number quoted in `README.md`, `REPRODUCE.md`, `dashboard.html` and
   `paper/main.pdf` must match the result files; fix every place a changed number appears, not just one.
6. **Record provenance, never machine identity.** New result files carry a `provenance` or
   `_provenance` block (script, exact command, image and digest, arch, OS name, toolchains, dates;
   `scripts/provenance.py` has `stamp()` and `write_json()`). Never write a host name, IP address, user
   name or absolute home-directory path (`/Users/<name>`, `/home/<name>`) into any file; write the OS name
   and arch instead (the scripts use `platform.system()` / `uname -s`). Some recorded commands run from
   `~/work/...` on the machines used; they name no user.
7. **New stage, new entry.** A new result file needs a stage in `pipeline.json` (fields: `id`
   `<system>.<stage>`, `kind`, `command`, `where` with `runner`/`arch`/`image`/`image_id`/`arch_source`,
   `needs`, `outputs`, `deterministic`, `runtime`, `note`; the schema is in `pipeline.json` itself).
   Mark it `"cheap": true` only if it runs in seconds without Docker, so `verify` re-runs it.
8. **Third-party code is read-only and not vendored.** Do not copy files from `artifacts/` into the
   repository; cite them by path and line (`artifacts/<system>/...:line`), which the ledger re-checks.
   The explorer embeds only a few lines around each cited location; the labeling page (`rate.html`) reads
   whole files at view time through `explorer/serve.py` (`GET /src?path=artifacts/...`).

## Quick commands

```bash
bash docker/quickstart.sh                          # offline: verify + figures + explorer + dashboard into out/; exit 0 = all match
python3 scripts/pipeline.py verify                 # cheap stages vs shipped files and PIPELINE_HASHES.json
python3 scripts/pipeline.py list | grep arxiv      # stages added for the arXiv version
python3 scripts/pipeline.py show vert.diff         # one stage: command, image, arch, inputs, outputs
python3 -I scripts/make_tables.py --md             # the per-system outcome table
python3 scripts/plot_results.py --out out/figures  # Figs. 1 and 2 (+2 extra); needs matplotlib, so no -I if it is a user install
python3 -I scripts/provenance.py --index --check   # PROVENANCE.json file list and hosts match the tree
bash scripts/provision.sh artifacts                # fetch every third-party artifact at its pinned commit (~8 GB)
python3 -I scripts/build_defect_ledger.py          # rebuild the ledger and run its checks (needs artifacts/)
```

Shipped state: `verify` prints `9 match, 0 mismatch/fail, 3 skipped` without `artifacts/` and
`10 match, 0 mismatch/fail, 2 skipped` with them; the paper builds to 7 pages. On a machine whose fonts or
matplotlib differ from the shipped figures', the figure stages report MATCH on their plotted values.

## Reading the defect ledger

`results/defect_ledger.json` has `rows` (one per candidate defect) and `meta` (per-system notes and
probes). Fields you will use most:

| Field | Meaning |
|---|---|
| `system`, `translation`, `defect_id` | system name as spelled in code (`Flourine` is FLOURINE), the translation unit, a stable id |
| `mechanism`, `source_location` | what is wrong, with `rust` and `c` file:line locations in `artifacts/` |
| `severity` | `S1` wrong output, `crash`, `S2` stderr only, `S3` build failure or hang; only S1 and crash count |
| `example_input`, `c_output`, `rust_output` | one exposing input and both outputs |
| `exposure_count`, `exposure_breakdown` | how many generated inputs expose it, by outcome, and the source file |
| `ub_rule` | UB-rule status and how many exposing inputs are UB-clean |
| `count_status` | `counted`, or why not (`excluded_original_c_ub`, `precondition_only`, `hang-not-counted`, ...) |
| `class` | for counted rows: `outright_error`, `edge_input_robustness`, `c_bug_not_copied`, `other` |
| `acceptance_status`, `acceptance_evidence` | what the artifact records about the system's own check |
| `on_check_inputs`, `heldout`, `exposed_outside` | whether the check ever ran an exposing input (held-out measure) |

```bash
python3 -c "import json; L=json.load(open('results/defect_ledger.json')); \
  print([r['defect_id'] for r in L['rows'] if r['system']=='VERT' and r['count_status']=='counted'][:5])"
```

The explorer (`python3 explorer/serve.py`, then http://127.0.0.1:8790/) shows the same rows with code
and evidence; without `artifacts/` its source panes are empty.

## Things that trip people up

- **Two architectures.** Some stages ran on arm64 (macOS, Docker), others on x86_64 Linux;
  `pipeline.json` (`where.arch`) says which. Syzygy and RustMap bzip2 build only on x86_64; a few counts
  are architecture-dependent and documented as such.
- **Build profiles.** Each system's Rust is built in the profile its own pipeline uses: integer
  overflow wraps without overflow checks and panics with them. `results/profile_check.json` re-runs
  every counted crash and integer wrong output in the other profile.
- **VERT scope.** The scalar harness tests functions with integer arguments: those whose C returns
  `int`, and since the arXiv version also `long` / `long long` (`systems/vert/run.py --wide-int`);
  array and float functions use `systems/vert/run_arrays.py`. VERT's own checks compare against an
  rWasm copy of the C, not the C.
- **`python3 -I`.** Most scripts are run isolated (`-I`). The figure scripts need matplotlib and the
  code listings need Pygments; if those are user installs, run those scripts without `-I`.
- **Provenance hashes.** Commit hashes in `results/PROVENANCE.json` and in result files' `git_rev` /
  `git_commit` refer to the development repository the study was run in, which is not shipped; this
  repository has a single commit. `--index --check` therefore compares the file list and recorded
  hosts only. The two files rebuilt here (the defect ledger, `his2trans_summary.json`) say so in `git_rev`.
- **Pointers to the development log.** Notes in `pipeline.json`, scripts and some result files cite
  "issues.md item N", `docs/findings.md` or scratch reports. Those belong to the development repository's
  log and are not shipped; the facts they support are in the result files themselves.
- **Images.** `bash scripts/provision.sh images` builds `crust-stable`, `crust-heldout` and
  `crust-syzygy`; `crust-kani` is `docker build -t crust-kani -f docker/Dockerfile.kani docker/` and the
  reviewer image `crust-artifact` is `bash docker/build_artifact_image.sh`. `pipeline.json` (`images`)
  gives every image's recipe and digest, including the few used by single stages.
