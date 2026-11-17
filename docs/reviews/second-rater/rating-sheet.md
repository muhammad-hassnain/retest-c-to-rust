# Second-rater sheet (blind)

Rate each case from the C, the Rust, the input and the two outputs only. Do not open `results/defect_ledger.json`, `systems/*/notes.md` or `docs/findings.md` (the study's working log, not part of this repository) until you are done. Fill in the three answer lines under each case. Links open the files.

## Rules (from the paper's method)
- A **defect** is one mechanism in the Rust that makes it return a wrong result or crash, on an input where the C is well-defined. The C is the oracle.
- **Not counted** if the divergence occurs only: (i) where the C has undefined behavior; (ii) on inputs breaking a precondition the original states (name, comments, docs); (iii) in a value the C standard leaves unspecified; (iv) in a callback that frees memory; (v) when a negative value is passed to `scanf %u` / `printf %x/%u`. Hangs and build failures are not defects.
- **Class** (counted only): `outright` = wrong on ordinary, well-formed input; `edge` = differs only on malformed or extreme inputs where the C is defined; `c_bug` = the Rust fixes a C bug or follows a recorded expected output instead of the C; `other`.

## Case 1 (item17) — VERT: function MAXIMUM_SUM_SUBARRAY_REMOVING_ONE_ELEMENT (`f_gold`)
- C: [artifacts/vert/benchmark/c_transcoder/MAXIMUM_SUM_SUBARRAY_REMOVING_ONE_ELEMENT/MAXIMUM_SUM_SUBARRAY_REMOVING_ONE_ELEMENT.c](../../../artifacts/vert/benchmark/c_transcoder/MAXIMUM_SUM_SUBARRAY_REMOVING_ONE_ELEMENT/MAXIMUM_SUM_SUBARRAY_REMOVING_ONE_ELEMENT.c)
- Rust: [artifacts/vert/benchmark/c_transcoder/MAXIMUM_SUM_SUBARRAY_REMOVING_ONE_ELEMENT/out-rwasm-bolero/src/main.rs](../../../artifacts/vert/benchmark/c_transcoder/MAXIMUM_SUM_SUBARRAY_REMOVING_ONE_ELEMENT/out-rwasm-bolero/src/main.rs) (search `fn f_gold`)
- Input: `"f_gold(1, 0, 2)"`
- C output: `"1"`
- Rust output: `"2"`

**Counted?** (yes / no + rule i–v): 

**Class** (outright / edge / c_bug / other): 

**Cause** (one line, file:line): 


## Case 2 (item20) — ACTOR: program 005_static_loop
- C: [artifacts/actoruw/results/Test-Corpus/kiro/B01_synthetic/005_static_loop/verified/c_src/src/main.c](../../../artifacts/actoruw/results/Test-Corpus/kiro/B01_synthetic/005_static_loop/verified/c_src/src/main.c)
- Rust: [artifacts/actoruw/results/Test-Corpus/kiro/B01_synthetic/005_static_loop/verified/src/main.rs](../../../artifacts/actoruw/results/Test-Corpus/kiro/B01_synthetic/005_static_loop/verified/src/main.rs)
- Input: `"{\"argv\": [\" 12\"], \"stdin\": \"\"}"`
- C output: `{"exit": 0, "first_differing_stdout_line": 1, "line": "0", "stderr": ""}`
- Rust output: `{"exit": 1, "first_differing_stdout_line": 1, "line": "Error: first argument must be an integer!", "stderr": ""}`

**Counted?** (yes / no + rule i–v): 

**Class** (outright / edge / c_bug / other): 

**Cause** (one line, file:line): 


## Case 3 (item38) — Fluorine: function opl_emu_registers_operator_list
- C: [artifacts/flourine_dl/flourine-artifact/bms/c/opl/opl_emu_registers_operator_list/opl_emu_registers_operator_list.c](../../../artifacts/flourine_dl/flourine-artifact/bms/c/opl/opl_emu_registers_operator_list/opl_emu_registers_operator_list.c)
- Rust: [artifacts/flourine_dl/flourine-artifact/translations_only/success/c/opl/opl_emu_registers_operator_list/claude2/BaseRepair/opl_emu_registers_operator_list.rs](../../../artifacts/flourine_dl/flourine-artifact/translations_only/success/c/opl/opl_emu_registers_operator_list/claude2/BaseRepair/opl_emu_registers_operator_list.rs)
- Input: `"[0, 1, 0, 0]"`
- C output: `"256"`
- Rust output: `"1"`

**Counted?** (yes / no + rule i–v): 

**Class** (outright / edge / c_bug / other): 

**Cause** (one line, file:line): 


## Case 4 (item14) — C2SaferRust: coreutils head
- C: [artifacts/c2saferrust/coreutils/src/head/c/head.c](../../../artifacts/c2saferrust/coreutils/src/head/c/head.c)
- Rust: [artifacts/c2saferrust/coreutils/src/head/rust_WIP/src/head.rs](../../../artifacts/c2saferrust/coreutils/src/head/rust_WIP/src/head.rs)
- Input: `"head -q in1.txt"`
- C output: `{"exit": 0, "stdout": "alpha\nbeta\n", "stderr": ""}`
- Rust output: `{"exit": -11, "stdout": "", "stderr": ""}`

**Counted?** (yes / no + rule i–v): 

**Class** (outright / edge / c_bug / other): 

**Cause** (one line, file:line): 


## Case 5 (item34) — ACTOR: library 027_ctype_ascii_lib (driver)
- C: [artifacts/actoruw/results/Test-Corpus/kiro/B01_synthetic/027_ctype_ascii_lib/verified/c_src/src/driver.c](../../../artifacts/actoruw/results/Test-Corpus/kiro/B01_synthetic/027_ctype_ascii_lib/verified/c_src/src/driver.c)
- Rust: [artifacts/actoruw/results/Test-Corpus/kiro/B01_synthetic/027_ctype_ascii_lib/verified/src/lib.rs](../../../artifacts/actoruw/results/Test-Corpus/kiro/B01_synthetic/027_ctype_ascii_lib/verified/src/lib.rs)
- Input: `"{\"argv\": [\"0\", \"49\"], \"stdin\": \"\", \"function\": \"driver\"}"`
- C output: `{"exit": 0, "stdout": "alphanumeric: 8\nalphabetic: 0\nlowercase: 0\nuppercase: 0\ndigit: 2048\nhexadecimal: 4096\ncontrol: 0\ngraphical: 32768\nspace: 0\nblank: 0\nprinting: 16384\npunctuation: 0\nto lower: 1\nto upper: 1\n\n#ret=void\n", "stderr": ""}`
- Rust output: `{"exit": 0, "stdout": "alphanumeric: 8\nalphabetic: 0\nlowercase: 0\nuppercase: 0\ndigit: 1\nhexadecimal: 4096\ncontrol: 0\ngraphical: 32768\nspace: 0\nblank: 0\nprinting: 16384\npunctuation: 0\nto lower: 1\nto upper: 1\n\n#ret=void\n", "stderr": ""}`

**Counted?** (yes / no + rule i–v): 

**Class** (outright / edge / c_bug / other): 

**Cause** (one line, file:line): 


## Case 6 (item19) — AdaTrans: LeetCode program weekly_contest_434_p2
- C: [artifacts/adatrans/dataset/C_CPP/C/src/weekly_contest_434_p2.c](../../../artifacts/adatrans/dataset/C_CPP/C/src/weekly_contest_434_p2.c)
- Rust: the last non-empty `extracted_code` in [artifacts/adatrans/results/adatrans_full/run1/conversations/C_434_2.json](../../../artifacts/adatrans/results/adatrans_full/run1/conversations/C_434_2.json) (rate.html shows it as C_434_2.rs)
- Input: `"4 1\nMESSAGE 912 id1 id1 id2 id2\n"`
- C output: `{"stdout_head": "Mentions: 0 1 0 0 \n", "exit": 0}`
- Rust output: `{"stdout_head": "Mentions: 1 2 1 1 \n", "exit": 0, "stderr_head": ""}`

**Counted?** (yes / no + rule i–v): 

**Class** (outright / edge / c_bug / other): 

**Cause** (one line, file:line): 


## Case 7 (item41) — VERT: function SUM_SERIES_555555_N_TERMS (`f_gold`)
- C: [artifacts/vert/benchmark/c_transcoder/SUM_SERIES_555555_N_TERMS/SUM_SERIES_555555_N_TERMS.c](../../../artifacts/vert/benchmark/c_transcoder/SUM_SERIES_555555_N_TERMS/SUM_SERIES_555555_N_TERMS.c)
- Rust: [artifacts/vert/benchmark/c_transcoder/SUM_SERIES_555555_N_TERMS/out-rwasm-bolero/src/main.rs](../../../artifacts/vert/benchmark/c_transcoder/SUM_SERIES_555555_N_TERMS/out-rwasm-bolero/src/main.rs) (search `fn f_gold`)
- Input: `"f_gold(8)"`
- C output: `"61719994"`
- Rust output: `"61719996"`

**Counted?** (yes / no + rule i–v): 

**Class** (outright / edge / c_bug / other): 

**Cause** (one line, file:line): 


## Case 8 (item06) — Fluorine: function opl_emu_clamp
- C: [artifacts/flourine_dl/flourine-artifact/bms/c/opl/opl_emu_clamp/opl_emu_clamp.c](../../../artifacts/flourine_dl/flourine-artifact/bms/c/opl/opl_emu_clamp/opl_emu_clamp.c)
- Rust: [artifacts/flourine_dl/flourine-artifact/translations_only/success/c/opl/opl_emu_clamp/claude2/BaseRepair/opl_emu_clamp.rs](../../../artifacts/flourine_dl/flourine-artifact/translations_only/success/c/opl/opl_emu_clamp/claude2/BaseRepair/opl_emu_clamp.rs)
- Input: `"[1, 1, -1]"`
- C output: `"-1"`
- Rust output: `"1"`

**Counted?** (yes / no + rule i–v): 

**Class** (outright / edge / c_bug / other): 

**Cause** (one line, file:line): 


## Case 9 (item31) — His2Trans: library buffer (`buffer_append_n` etc.)
- C: [artifacts/his2trans/data/rq2/deepseek/c_source/c_source/buffer/src/buffer.c](../../../artifacts/his2trans/data/rq2/deepseek/c_source/c_source/buffer/src/buffer.c)
- Rust: [artifacts/his2trans/data/paper_artifacts/deepseek_v4_pro_oss8_0613_rq2_100pct_minimal/projects/buffer/src/src_buffer.rs](../../../artifacts/his2trans/data/paper_artifacts/deepseek_v4_pro_oss8_0613_rq2_100pct_minimal/projects/buffer/src/src_buffer.rs)
- Input: `"newsz 0 ; appn 68656c6c6f 1"`
- C output: `"appn 0 | 1024 1 \"h\""`
- Rust output: `"appn 0 | 1024 5 \"hello\""`

**Counted?** (yes / no + rule i–v): 

**Class** (outright / edge / c_bug / other): 

**Cause** (one line, file:line): 


## Case 10 (item28) — RustMap: Rosetta program box_the_compass
- C: [artifacts/rustmap/c-code/rosseta-125/box_the_compass/main.c](../../../artifacts/rustmap/c-code/rosseta-125/box_the_compass/main.c)
- Rust: [artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/box_the_compass-c.rs](../../../artifacts/rustmap/rust-code/rosetta_code_gpt/125-rosetta-code-gpt/box_the_compass-c.rs)
- Input: `"(no input: the program prints its own fixed cases)"`
- C output: `" 1  North                     0.00\n 2  North by east            16.87\n 3  North-northeast          16.88\n 4  Northeast by north       33.75\n 5  Northeast                50.62\n 6  Northeast by east        50.63\n 7  East-northeast           67.50\n 8  East by north            84.37\n 9  East                     84.38\n10  East by south           101.25\n11  East-southeast          118.12\n12  Southeast by east       118.13\n13  Southeast               135.00\n14  Southeast by south      151.87\n15  South-southeast         151.88\n16  South by east           168.75\n17  South                   185.62\n18  South by west           185.63\n19  South-southwest         202.50\n20  Southwest by south      219.37\n21  Southwest               219.38\n22  Southwest by west       236.25\n23  West-southwest          253.12\n24  West by south           253.13\n25  West                    270.00\n26  West by north           286.87\n27  West-northwest          286.88\n28  Northwest by west       303.75\n29  Northwest               320.62\n30  Northwest by north      320.63\n31  North-northwest         337.50\n32  North by west           354.37\n 1  North                   354.38\n"`
- Rust output: `" 2  North by east           0.00\n 3  North-northeast         16.87\n 3  North-northeast         16.88\n 5  Northeast               33.75\n 6  Northeast by east       50.62\n 6  Northeast by east       50.63\n 8  East by north           67.50\n 9  East                    84.37\n 9  East                    84.38\n11  East-southeast          101.25\n12  Southeast by east       118.12\n12  Southeast by east       118.13\n14  Southeast by south      135.00\n15  South-southeast         151.87\n15  South-southeast         151.88\n17  South                   168.75\n18  South by west           185.62\n18  South by west           185.63\n20  Southwest by south      202.50\n21  Southwest               219.37\n21  Southwest               219.38\n23  West-southwest          236.25\n24  West by south           253.12\n24  West by south           253.13\n26  West by north           270.00\n27  West-northwest          286.87\n27  West-northwest          286.88\n29  Northwest               303.75\n30  Northwest by north      320.62\n30  Northwest by north      320.63\n32  North by west           337.50\n 1  North                   354.37\n 1  North                   354.38\n"`

**Counted?** (yes / no + rule i–v): 

**Class** (outright / edge / c_bug / other): 

**Cause** (one line, file:line): 


## Case 11 (item10) — ACTOR: program memmove
- C: [artifacts/actoruw/results/Test-Corpus/kiro/B02_synthetic/memmove/verified/c_src/src/main.c](../../../artifacts/actoruw/results/Test-Corpus/kiro/B02_synthetic/memmove/verified/c_src/src/main.c); [artifacts/actoruw/results/Test-Corpus/kiro/B02_synthetic/memmove/verified/c_src/src/lib.c](../../../artifacts/actoruw/results/Test-Corpus/kiro/B02_synthetic/memmove/verified/c_src/src/lib.c)
- Rust: [artifacts/actoruw/results/Test-Corpus/kiro/B02_synthetic/memmove/verified/src/main.rs](../../../artifacts/actoruw/results/Test-Corpus/kiro/B02_synthetic/memmove/verified/src/main.rs); [artifacts/actoruw/results/Test-Corpus/kiro/B02_synthetic/memmove/verified/src/lib.rs](../../../artifacts/actoruw/results/Test-Corpus/kiro/B02_synthetic/memmove/verified/src/lib.rs)
- Input: `"[] stdin b'0\\n0\\n0\\n2\\n-1 -128\\n'"`
- C output: `"2 255 128\n"`
- Rust output: `""`

**Counted?** (yes / no + rule i–v): 

**Class** (outright / edge / c_bug / other): 

**Cause** (one line, file:line): 


## Case 12 (item30) — SACTOR: CRUST-Bench library c-string
- C: [artifacts/sactor/CRUST-bench/datasets/CRUST_bench/CBench/c-string/lib.c](../../../artifacts/sactor/CRUST-bench/datasets/CRUST_bench/CBench/c-string/lib.c)
- Rust: [artifacts/sactor/CRUST-bench/datasets/CRUST_bench/CBench/c-string/result/translated_code_unidiomatic/combined.rs](../../../artifacts/sactor/CRUST-bench/datasets/CRUST_bench/CBench/c-string/result/translated_code_unidiomatic/combined.rs)
- Input: `"grid: string_t_is_space_char(0) -> C 0, Rust 1; all 4 builds"`
- C output: `null`
- Rust output: `null`

**Counted?** (yes / no + rule i–v): 

**Class** (outright / edge / c_bug / other): 

**Cause** (one line, file:line): 

