"""
Shared fixtures/helpers for testing fastsacct.py against a mocked
libslurmdb.so, once per supported ABI (see mock/mock_libslurmdb.c and
mock/include/<version>/ -- one real vendored header set per Slurm release).
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MOCK_C = REPO_ROOT / "mock" / "mock_libslurmdb.c"
MOCK_INCLUDE = REPO_ROOT / "mock" / "include"
FASTSACCT_PY = REPO_ROOT / "fastsacct.py"

sys.path.insert(0, str(REPO_ROOT))
import fastsacct  # noqa: E402
from abi import v24_11, v25_05, v25_11, v26_05  # noqa: E402

# version string -> (abi module, MOCK_API_MAJOR to pass to clang). Must
# match abi/vXX_YY.py's SLURM_API_MAJOR so slurm_api_version()-based
# auto-detection (fastsacct.detect_abi) resolves to the right module.
ABI_MODULES = {
    v24_11.SLURM_ABI_VERSION: v24_11,
    v25_05.SLURM_ABI_VERSION: v25_05,
    v25_11.SLURM_ABI_VERSION: v25_11,
    v26_05.SLURM_ABI_VERSION: v26_05,
}
ABI_VERSIONS = sorted(ABI_MODULES)


def _find_cc():
    for name in ("cc", "clang", "gcc"):
        path = shutil.which(name)
        if path:
            return path
    return None


@pytest.fixture(scope="session")
def cc():
    path = _find_cc()
    if path is None:
        pytest.skip("no C compiler (cc/clang/gcc) found on PATH")
    return path


@pytest.fixture(scope="session")
def mock_libs(cc, tmp_path_factory):
    """Compile one mock libslurmdb.so per supported ABI, each against that
    release's real vendored headers, and return {abi_version: so_path}."""
    build_dir = tmp_path_factory.mktemp("mock-libs")
    libs = {}
    for version, module in ABI_MODULES.items():
        so_path = build_dir / f"libslurmdb_{version.replace('.', '_')}.so"
        include_dir = MOCK_INCLUDE / version
        assert include_dir.is_dir(), f"missing vendored headers: {include_dir}"
        subprocess.run(
            [
                cc,
                "-shared",
                "-fPIC",
                "-I",
                str(include_dir),
                f"-DMOCK_API_MAJOR={module.SLURM_API_MAJOR}",
                "-o",
                str(so_path),
                str(MOCK_C),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        libs[version] = so_path
    return libs


def run_fastsacct(library, abi=None, extra_args=(), raw=False):
    """Run fastsacct.py against `library`, with the required -S/-E/-D/-X/
    -a/--json flags, under TZ=UTC (fastsacct.require_utc_timezone() refuses
    to run otherwise). Returns the parsed JSON stdout, or the raw stdout
    text if raw=True (e.g. for --jsonl, which isn't a single JSON document
    and so isn't json.loads()-able as a whole)."""
    args = [
        sys.executable,
        str(FASTSACCT_PY),
        "-S",
        "2026-01-01",
        "-E",
        "2026-01-02",
        "-D",
        "-X",
        "-a",
        "--json",
        "--library",
        str(library),
        *extra_args,
    ]
    if abi is not None:
        args += ["--abi", abi]
    env = dict(os.environ, TZ="UTC")
    result = subprocess.run(args, capture_output=True, text=True, env=env)
    assert result.returncode == 0, (
        f"fastsacct.py exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result.stdout if raw else json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Expected output, derived independently from mock_libslurmdb.c's make_job()
# (the actual C values written into the struct) plus fastsacct.py's own
# (shared, version-independent, separately validated -- see README's
# "Validation" section) decode helpers. This checks the part that *is*
# version-specific: that each abi/vXX_YY.py's CDEF reads those C values back
# from the right offsets.
# ---------------------------------------------------------------------------
MOCK_NOW_ARGS = ("2026-01-01", "2026-01-02")
MOCK_JOBS = [
    {"jobid": 1001, "account": "mila-account", "jobname": "train_job", "delta": (3600, 1800), "timelimit": 60},
    {"jobid": 1002, "account": "mila-account", "jobname": "eval_job", "delta": (1800, 900), "timelimit": 60},
    # No time limit -- exercises the INFINITE-sentinel-to-0 mapping for
    # "u32_inf0" fields (see fastsacct.py's _job_to_dict).
    {"jobid": 1003, "account": "other-account", "jobname": "sweep_job", "delta": (7200, 6900), "timelimit": fastsacct.INFINITE},
    # Deliberately nasty job name (tab, newline, CR, braces, brackets,
    # quotes, backslash) -- exercises the --jsonl one-JSON-value-per-line
    # framing guarantee (see test_flat_output.py's jsonl test). Must match
    # mock_libslurmdb.c's slurmdb_jobs_get() exactly.
    {
        "jobid": 1004,
        "account": "mila-account",
        "jobname": "weird\tname\nwith\r{braces}[brackets]\"quotes\"\\backslash",
        "delta": (100, 50),
        "timelimit": 60,
    },
]


def expected_jobs(version):
    now = fastsacct.parse_time(MOCK_NOW_ARGS[1])
    module = ABI_MODULES[version]
    return [_expected_job(job, now, version, module) for job in MOCK_JOBS]


def _expected_job(job, now, version, module):
    start = now - job["delta"][0]
    end = now - job["delta"][1]
    raw = {
        "jobid": job["jobid"],
        "account": job["account"],
        "jobname": job["jobname"],
        "cluster": "mila",
        "partition": "main",
        "nodes": "node[01-02]",
        "user": "someuser",
        "uid": 1000,
        "gid": 1000,
        "state": 3,
        "start": start,
        "end": end,
        "submit": start - 60,
        "eligible": start - 60,
        "elapsed": end - start,
        "req_cpus": 4,
        "alloc_nodes": 2,
        "tres_alloc_str": "1=4,2=17179869184,4=2",
        "tres_req_str": "1=4,2=17179869184,4=2",
        "req_mem": 0x8000000000000000 | 4096,
        "exitcode": 0,
        "priority": 100,
        "qosid": 1,
        "timelimit": job["timelimit"],
        "wckeyid": 0,
        "wckey": "*default",
        "tot_cpu_sec": 120,
        "user_cpu_sec": 100,
        "sys_cpu_sec": 20,
        "lineage": "/some/lineage/path",
        "work_dir": "/home/someuser",
    }
    if version == "24.11":
        raw["lft"] = 7
    else:
        raw["resv_req"] = "normal"
        raw["segment_size"] = 8
    if version == "26.05":
        raw["exclusive"] = "NO"
        raw["oversubscribe"] = "NO"
        raw["sluid"] = 0x123456789

    out = {}
    for json_key, c_field, kind in module.JOB_FIELDS:
        val = raw.get(c_field)
        if kind == "str":
            out[json_key] = val
        elif kind == "group":
            continue  # system-dependent (getgrgid); checked separately
        elif kind == "flags":
            out[json_key] = fastsacct.job_flags(val or 0)
        elif kind == "job_state":
            out[json_key] = fastsacct.job_state(val or 0)
        elif kind == "qos_name":
            out[json_key] = "normal" if val else ""
        elif kind == "no_val32":
            ival = val or 0
            out[json_key] = None if ival in (fastsacct.NO_VAL, fastsacct.INFINITE) else ival
        elif kind == "u32_inf0":
            ival = val or 0
            out[json_key] = 0 if ival == fastsacct.INFINITE else ival
        else:
            out[json_key] = val if val is not None else 0
    out["allocated_cpu"] = out["requested_cpu"] = 4
    out["allocated_mem"] = out["requested_mem"] = 17179869184
    out["allocated_node"] = out["requested_node"] = 2
    out["exitcode_return_code"] = 0
    out["exitcode_signal"] = None
    return out
