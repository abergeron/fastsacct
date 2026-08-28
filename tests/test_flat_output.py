"""
End-to-end tests: for each supported ABI (24.11, 25.05, 25.11, 26.05), build
a mock libslurmdb.so against that release's real vendored headers
(mock/include/<version>/), point fastsacct.py at it, and check the flat
JSON output is exactly right -- including the handful of fields that only
exist on some releases (lft on 24.11; resv_req/segment_size on 25.05+;
exclusive/oversubscribe/sluid on 26.05+).

Run with: uv run pytest
"""

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


@pytest.mark.parametrize("version", ABI_VERSIONS)
def test_flat_output(mock_libs, version):
    output = run_fastsacct(mock_libs[version], abi=version)

    assert output["meta"]["schema_version"] == fastsacct.SCHEMA_VERSION
    assert output["meta"]["slurm_abi_version"] == version

    jobs = output["jobs"]
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
def test_autodetect_via_slurm_api_version(mock_libs, version):
    """No --abi: fastsacct must probe the mock .so's slurm_api_version()
    and land on the same abi/vXX_YY.py module as when told explicitly."""
    detected = run_fastsacct(mock_libs[version])
    explicit = run_fastsacct(mock_libs[version], abi=version)
    assert detected == explicit
