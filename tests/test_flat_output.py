"""
End-to-end tests: for each supported ABI (24.11, 25.05, 25.11, 26.05), build
a mock libslurmdb.so against that release's real vendored headers
(mock/include/<version>/), point fastsacct.py at it, and check the flat
JSON output is exactly right -- including the handful of fields that only
exist on some releases (lft on 24.11; resv_req/segment_size on 25.05+;
exclusive/oversubscribe/sluid on 26.05+).

Run with: uv run pytest
"""

import json

import pytest
from conftest import ABI_VERSIONS, expected_jobs, run_fastsacct

import fastsacct

VERSION_26_05_ONLY = {"exclusive", "oversubscribe", "sluid"}

VERSION_ONLY_FIELDS = {
    "24.11": {
        "present": {"lft"},
        "absent": {"resv_req", "segment_size"} | VERSION_26_05_ONLY,
    },
    "25.05": {
        "present": {"resv_req", "segment_size"},
        "absent": {"lft"} | VERSION_26_05_ONLY,
    },
    "25.11": {
        "present": {"resv_req", "segment_size"},
        "absent": {"lft"} | VERSION_26_05_ONLY,
    },
    "26.05": {
        "present": {"resv_req", "segment_size"} | VERSION_26_05_ONLY,
        "absent": {"lft"},
    },
}


def assert_jobs_match_expected(jobs, version):
    expected = expected_jobs(version)
    assert len(jobs) == len(expected)

    only = VERSION_ONLY_FIELDS[version]
    for job, exp in zip(jobs, expected):
        assert only["present"] <= job.keys()
        assert not (only["absent"] & job.keys())

        assert isinstance(job.pop("group"), str)
        job = {k: v for k, v in job.items() if k not in only["absent"]}
        assert job == exp


@pytest.mark.parametrize("version", ABI_VERSIONS)
def test_flat_output(mock_libs, version):
    output = run_fastsacct(mock_libs[version], abi=version)

    assert output["meta"]["schema_version"] == fastsacct.SCHEMA_VERSION
    assert output["meta"]["slurm_abi_version"] == version
    assert output["meta"]["job_count"] == len(output["jobs"])

    assert_jobs_match_expected(output["jobs"], version)


@pytest.mark.parametrize("version", ABI_VERSIONS)
def test_autodetect_via_slurm_api_version(mock_libs, version):
    """No --abi: fastsacct must probe the mock .so's slurm_api_version()
    and land on the same abi/vXX_YY.py module as when told explicitly."""
    detected = run_fastsacct(mock_libs[version])
    explicit = run_fastsacct(mock_libs[version], abi=version)
    assert detected == explicit


@pytest.mark.parametrize("version", ABI_VERSIONS)
def test_jsonl_output(mock_libs, version):
    """--jsonl must produce the same data as the default single-blob mode,
    just reshaped into one JSON value per line (meta first, then one job
    per line) -- including MOCK_JOBS' deliberately nasty job name (tab,
    newline, CR, braces, brackets, quotes, backslash), which must round-
    trip byte-for-byte without corrupting the line-per-value framing."""
    raw = run_fastsacct(
        mock_libs[version], abi=version, extra_args=["--jsonl"], raw=True
    )

    # No trailing garbage, no blank lines in the middle -- exactly one
    # non-empty line per meta/job value, each independently json.loads()-able
    # regardless of what a job name contains (that's the whole point).
    assert raw.endswith("\n")
    lines = raw[:-1].split("\n")
    assert all(line for line in lines)

    meta_line, *job_lines = lines
    meta = json.loads(meta_line)["meta"]
    assert meta["schema_version"] == fastsacct.SCHEMA_VERSION_JSONL
    assert meta["slurm_abi_version"] == version
    assert meta["job_count"] == len(expected_jobs(version))

    jobs = [json.loads(line) for line in job_lines]
    assert_jobs_match_expected(jobs, version)

    # Cross-check against the default mode's meta and jobs, to make sure
    # --jsonl isn't just internally self-consistent but actually carries
    # the same data as the non-streaming path.
    blob = run_fastsacct(mock_libs[version], abi=version)
    meta_wo_ver = {k: v for k, v in meta.items() if k != "schema_version"}
    blob_wo_ver = {k: v for k, v in blob["meta"].items() if k != "schema_version"}
    assert meta_wo_ver == blob_wo_ver
    jsonl_names = [json.loads(line)["name"] for line in job_lines]
    blob_names = [job["name"] for job in blob["jobs"]]
    assert jsonl_names == blob_names
    assert (
        "\t" in jsonl_names[-1] and "\n" in jsonl_names[-1] and "\r" in jsonl_names[-1]
    )
