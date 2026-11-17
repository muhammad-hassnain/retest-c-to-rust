"""Follow-up: VERT's documented bolero replay on the 12 VERT array/float defective
functions of results/defect_ledger.json, using systems/vert/bolero_vacuity/check_current.py UNCHANGED
(same command, env, timeout, outcome labels and "executed any input" evidence) but only its
`vert_reduce` arm (VERT ARTIFACT_README: mkdir -p src/__fuzz__; RUSTFLAGS='-C overflow-checks=false'
cargo bolero reduce bolero_wasm_eq; pass iff no 'Test Failure').

  BOLERO_LOGDIR=/abs/logs python3 systems/vert/toolchain_bolero_remaining.py BENCH PART.json FUNC [FUNC ...]
Run inside the container prepared by toolchain_kani_bolero.sh (same setup as run_in_container_current.sh).
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "bolero_vacuity"))
import check_current as cc  # noqa: E402

cc.ARMS = ("vert_reduce",)
cc.main()
