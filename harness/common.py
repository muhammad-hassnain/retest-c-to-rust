"""Shared helpers for the held-out differential harness.

Key disciplines:
  * read OUTPUT as bytes, compare byte-exact, zero tolerance;
  * run oracle and SUT under an identical, controlled environment (so locale /
    env can't be a silent confound -- it is applied symmetrically to both);
  * meaningful exit codes (2=setup/build error, 1=differential mismatch, 0=agree).
"""
import base64
import os
import subprocess
from dataclasses import dataclass, field, asdict

# Repo root = parent of this harness/ dir.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ARTIFACT = os.path.join(REPO_ROOT, "artifacts", "c2saferrust")


def artifact_root() -> str:
    return os.environ.get("C2SR_ROOT", DEFAULT_ARTIFACT)


def util_c_dir(util: str) -> str:
    return os.path.join(artifact_root(), "coreutils", "src", util, "c")


def rust_variant() -> str:
    """Which Rust tree is the SUT. C2SaferRust's OUTPUT is `rust_WIP` (translate.py copies
    `rust` -> `rust_WIP` and works there; upstream collect_results.py scores `rust_WIP` as the
    translation). `rust` is the plain c2rust translation it starts from -- the baseline, not the
    system under test. (Audit 4 found earlier results were measured on `rust`.)"""
    return os.environ.get("C2SR_VARIANT", "rust_WIP")


def util_rust_dir(util: str) -> str:
    return os.path.join(artifact_root(), "coreutils", "src", util, rust_variant())


def tests_dir(util: str) -> str:
    return os.path.join(artifact_root(), "coreutils", "tests", util)


# Controlled environment applied IDENTICALLY to oracle and SUT. This removes
# locale/PATH as a confound; any remaining divergence is a real behavioural one.
def controlled_env() -> dict:
    return {
        "LC_ALL": "C",
        "LANG": "C",
        "PATH": "/usr/bin:/bin",
        "TZ": "UTC",
        "HOME": "/tmp",
    }


@dataclass
class RunResult:
    stdout: bytes
    stderr: bytes
    returncode: int
    timed_out: bool


def run_capture(argv, stdin_bytes=b"", cwd=None, timeout=15, env_overrides=None) -> RunResult:
    """Run a program, capturing stdout/stderr bytes, exit code. Never raises on
    non-zero exit; returns timed_out=True if it overran. env_overrides (applied
    symmetrically to both oracle and SUT) lets a case vary e.g. the locale."""
    env = controlled_env()
    if env_overrides:
        env.update(env_overrides)
    try:
        p = subprocess.run(
            argv,
            input=stdin_bytes,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return RunResult(p.stdout, p.stderr, p.returncode, False)
    except subprocess.TimeoutExpired as e:
        out = e.stdout or b""
        err = e.stderr or b""
        return RunResult(out, err, -1, True)


def normalize_stderr(data: bytes, prog_oracle: str, prog_sut: str) -> bytes:
    """coreutils sets program_name from argv[0], which here is each binary's full
    invocation path -- different for oracle vs SUT. Replace the full paths (and the
    basename) with a placeholder so stderr comparison reflects REAL message
    differences (wording, duplication, ordering), not the binary's filename/path.
    Applied symmetrically; documented normalisation."""
    # Full invocation paths first (both sides), THEN basenames -- otherwise one
    # side's basename replacement corrupts the other side's full-path match.
    for p in (prog_oracle, prog_sut):
        data = data.replace(p.encode(), b"PROG")            # full invocation path
    for p in (prog_oracle, prog_sut):
        data = data.replace(os.path.basename(p).encode() + b":", b"PROG:")
    return data


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


@dataclass
class Case:
    id: str
    util: str
    argv: list           # args after program name (may name files created below, or '-')
    stdin_b64: str        # base64 of stdin bytes
    cwd_kind: str = "tmp"  # "tmp" (fresh empty dir) or a specific need (pwd)
    files: dict = field(default_factory=dict)  # name -> base64 bytes, created in the cwd
    make_dirs: list = field(default_factory=list)  # dir names to mkdir in the cwd
    env: dict = field(default_factory=dict)     # per-case env overrides (both sides)
    writes_files: bool = False  # util writes/modifies files (split, truncate): compare the
                                # post-run file TREE too, running oracle & SUT in SEPARATE dirs
    note: str = ""

    def stdin(self) -> bytes:
        return unb64(self.stdin_b64)

    def decoded_files(self) -> dict:
        return {name: unb64(b) for name, b in (self.files or {}).items()}

    def to_json(self):
        return asdict(self)

    @staticmethod
    def from_json(d):
        return Case(**d)
