"""SubprocessPolicy model and writable-quota scope (goal.md §6.7, §6.12)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from vibereview.runtime.subprocess import (
    QUOTA_EXEMPT_ROOTS,
    WRITABLE_QUOTA_ROOTS,
    SubprocessPolicy,
    deterministic_test_policy,
    writable_quota_applies,
)

EXPECTED_FIELDS = {
    "timeout_seconds",
    "terminate_grace_seconds",
    "max_stdout_bytes",
    "max_stderr_bytes",
    "max_proposal_bytes",
    "max_writable_tree_bytes",
    "max_writable_entries",
    "max_writable_single_file_bytes",
    "max_writable_directory_depth",
    "max_open_files",
    "max_processes",
    "max_cpu_seconds",
    "max_address_space_bytes",
    "writable_tree_scan_interval_seconds",
    "inherited_environment_allowlist",
    "allowed_output_files",
}

KERNEL_LIMIT_FIELDS = (
    "max_open_files",
    "max_processes",
    "max_cpu_seconds",
    "max_address_space_bytes",
)


def _policy_kwargs(**overrides):
    values = deterministic_test_policy().model_dump()
    values.update(overrides)
    return values


def test_policy_field_set_matches_section_6_7():
    assert set(SubprocessPolicy.model_fields) == EXPECTED_FIELDS


def test_allowed_output_files_defaults_to_proposal_json():
    policy = deterministic_test_policy()
    assert policy.allowed_output_files == ("proposal.json",)
    assert SubprocessPolicy.model_fields["allowed_output_files"].default == (
        "proposal.json",
    )


def test_kernel_limit_fields_default_to_none():
    policy = deterministic_test_policy()
    for field in KERNEL_LIMIT_FIELDS:
        assert getattr(policy, field) is None
        assert SubprocessPolicy.model_fields[field].default is None


def test_deterministic_test_policy_matches_suggested_values():
    policy = deterministic_test_policy()
    assert policy.timeout_seconds == 10.0
    assert policy.terminate_grace_seconds == 1.0
    assert policy.max_stdout_bytes == 65536
    assert policy.max_stderr_bytes == 65536
    assert policy.max_proposal_bytes == 1048576
    assert policy.max_writable_tree_bytes == 16777216
    assert policy.max_writable_entries == 256
    assert policy.max_writable_files == 256
    assert policy.max_writable_single_file_bytes == 4194304
    assert policy.max_writable_directory_depth == 8
    assert policy.writable_tree_scan_interval_seconds == 0.05
    assert policy.inherited_environment_allowlist == (
        "PATH",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
    )
    assert policy.allowed_output_files == ("proposal.json",)


def test_deterministic_test_policy_round_trips_through_json():
    policy = deterministic_test_policy()
    assert SubprocessPolicy.model_validate_json(policy.model_dump_json()) == policy


def test_quota_root_constants_match_section_6_7():
    assert WRITABLE_QUOTA_ROOTS == ("output", "scratch", "home", "tmp")
    assert QUOTA_EXEMPT_ROOTS == ("bundle", "launcher", "credentials")
    assert not set(WRITABLE_QUOTA_ROOTS) & set(QUOTA_EXEMPT_ROOTS)


@pytest.mark.parametrize("root", WRITABLE_QUOTA_ROOTS)
def test_writable_quota_applies_to_quota_roots(root):
    assert writable_quota_applies(Path(root))
    assert writable_quota_applies(Path(root) / "file.txt")
    assert writable_quota_applies(Path(root) / "nested" / "deep" / "file.txt")


@pytest.mark.parametrize("root", QUOTA_EXEMPT_ROOTS)
def test_writable_quota_exempts_bundle_launcher_credentials(root):
    assert not writable_quota_applies(Path(root))
    assert not writable_quota_applies(Path(root) / "file.txt")
    assert not writable_quota_applies(Path(root) / "nested" / "deep" / "file.txt")


@pytest.mark.parametrize(
    "relative_path",
    [
        Path(""),
        Path("."),
        Path("input/resources/RES0001/content.md"),
        Path("proposal.json"),
        Path("Output/file.txt"),
    ],
)
def test_writable_quota_rejects_empty_and_unknown_roots(relative_path):
    assert not writable_quota_applies(relative_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout_seconds", 0.0),
        ("timeout_seconds", -1.0),
        ("terminate_grace_seconds", -0.5),
        ("max_stdout_bytes", 0),
        ("max_stdout_bytes", -1),
        ("max_stderr_bytes", 0),
        ("max_proposal_bytes", 0),
        ("max_writable_tree_bytes", 0),
        ("max_writable_entries", 0),
        ("max_writable_files", 0),
        ("max_writable_single_file_bytes", 0),
        ("max_writable_directory_depth", 0),
        ("writable_tree_scan_interval_seconds", 0.0),
        ("writable_tree_scan_interval_seconds", -0.05),
        ("max_open_files", 0),
        ("max_open_files", -1),
        ("max_processes", 0),
        ("max_cpu_seconds", 0),
        ("max_address_space_bytes", 0),
    ],
)
def test_non_positive_limits_rejected(field, value):
    with pytest.raises(ValidationError):
        SubprocessPolicy(**_policy_kwargs(**{field: value}))


def test_zero_terminate_grace_is_allowed():
    policy = SubprocessPolicy(**_policy_kwargs(terminate_grace_seconds=0.0))
    assert policy.terminate_grace_seconds == 0.0


@pytest.mark.parametrize("field", KERNEL_LIMIT_FIELDS)
def test_kernel_limits_accept_positive_values(field):
    policy = SubprocessPolicy(**_policy_kwargs(**{field: 64}))
    assert getattr(policy, field) == 64


def test_extra_fields_rejected():
    with pytest.raises(ValidationError):
        SubprocessPolicy(**_policy_kwargs(unknown_limit=1))


def test_assignment_is_validated():
    policy = deterministic_test_policy()
    with pytest.raises(ValidationError):
        policy.max_stdout_bytes = 0


def test_allowlist_accepts_sequences_and_stores_tuples():
    policy = SubprocessPolicy(
        **_policy_kwargs(inherited_environment_allowlist=["PATH", "LANG"])
    )
    assert policy.inherited_environment_allowlist == ("PATH", "LANG")


def test_missing_required_fields_rejected():
    with pytest.raises(ValidationError):
        SubprocessPolicy()
