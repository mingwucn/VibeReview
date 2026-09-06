"""Output-policy contract surface (goal.md §6.3, §6.6, §6.7, §6.9, §6.10)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from vibereview.runtime.confinement import ConfinementLevel, allows_real_engine
from vibereview.runtime.execution_inventory import ExecutionFileRecord
from vibereview.runtime.records import (
    FALLBACK_OUTCOMES,
    AttemptOutcome,
    fallback_allowed,
)
from vibereview.runtime.subprocess import (
    SubprocessPolicy,
    deterministic_test_policy,
    primary_attempt_outcome,
)

VALID_HASH = "sha256:" + "a" * 64

UNSAFE_FILE_TYPES = ["fifo", "socket", "device", "unsafe_symlink", "oversized"]


def _policy_kwargs():
    return dict(
        timeout_seconds=10.0,
        terminate_grace_seconds=1.0,
        max_stdout_bytes=65536,
        max_stderr_bytes=65536,
        max_proposal_bytes=1048576,
        max_writable_tree_bytes=16777216,
        max_writable_files=256,
        max_writable_single_file_bytes=4194304,
        max_writable_directory_depth=8,
        writable_tree_scan_interval_seconds=0.05,
        inherited_environment_allowlist=("PATH",),
    )


def _record(file_type, relative_path, size_bytes, content_hash):
    return ExecutionFileRecord(
        relative_path=Path(relative_path),
        file_type=file_type,
        size_bytes=size_bytes,
        content_hash=content_hash,
    )


def test_engine_output_policy_failure_exists_with_contract_value():
    outcome = AttemptOutcome.ENGINE_OUTPUT_POLICY_FAILURE
    assert outcome.value == "engine_output_policy_failure"
    assert outcome == "engine_output_policy_failure"
    assert len(AttemptOutcome) == 13


def test_engine_output_policy_failure_is_fallback_eligible():
    outcome = AttemptOutcome.ENGINE_OUTPUT_POLICY_FAILURE
    assert outcome in FALLBACK_OUTCOMES
    assert fallback_allowed(outcome)


def test_fallback_set_is_exactly_the_seven_engine_failures():
    assert len(FALLBACK_OUTCOMES) == 7
    assert all(
        outcome.value.startswith("engine_") for outcome in FALLBACK_OUTCOMES
    )
    for outcome in AttemptOutcome:
        assert fallback_allowed(outcome) is (outcome in FALLBACK_OUTCOMES)
    assert not fallback_allowed(AttemptOutcome.VALID_SCIENTIFIC_RESULT)


def test_allowed_output_files_defaults_to_exactly_proposal_json():
    for policy in (deterministic_test_policy(), SubprocessPolicy(**_policy_kwargs())):
        assert policy.allowed_output_files == ("proposal.json",)
        assert isinstance(policy.allowed_output_files, tuple)


def test_allowed_output_files_accepts_list_and_coerces_to_tuple():
    policy = SubprocessPolicy(
        **{**_policy_kwargs(), "allowed_output_files": ["proposal.json", "extra.json"]}
    )
    assert policy.allowed_output_files == ("proposal.json", "extra.json")


@pytest.mark.parametrize(
    "bad_value",
    ["proposal.json", b"proposal.json", 42, None, {"name": "proposal.json"}],
)
def test_allowed_output_files_rejects_non_list_types(bad_value):
    with pytest.raises(ValidationError):
        SubprocessPolicy(
            **{**_policy_kwargs(), "allowed_output_files": bad_value}
        )


def test_allowed_output_files_assignment_is_validated():
    policy = SubprocessPolicy(**_policy_kwargs())
    with pytest.raises(ValidationError):
        policy.allowed_output_files = "proposal.json"


def test_safe_file_record_carries_size_and_content_hash():
    content = b'{"disposition": "REJECT"}'
    record = _record(
        "regular",
        "output/proposal.json",
        len(content),
        f"sha256:{hashlib.sha256(content).hexdigest()}",
    )
    assert record.relative_path == Path("output/proposal.json")
    assert record.file_type == "regular"
    assert record.size_bytes == len(content)
    assert record.content_hash == f"sha256:{hashlib.sha256(content).hexdigest()}"


@pytest.mark.parametrize("file_type", UNSAFE_FILE_TYPES)
def test_unsafe_file_records_carry_no_size_or_hash(file_type):
    """Unsafe entries are never opened or hashed; type and path suffice (§6.9)."""

    record = _record(file_type, f"scratch/{file_type}_entry", None, None)
    assert record.size_bytes is None
    assert record.content_hash is None
    assert record.relative_path == Path(f"scratch/{file_type}_entry")
    assert record.file_type == file_type


@pytest.mark.parametrize(
    "fields",
    [{}, {"size_bytes": 3}, {"content_hash": VALID_HASH}],
)
def test_execution_file_record_requires_explicit_nullable_fields(fields):
    """``None`` for unsafe files is an explicit decision, not a silent default."""

    with pytest.raises(ValidationError):
        ExecutionFileRecord(
            relative_path=Path("output/proposal.json"),
            file_type="regular",
            **fields,
        )


def test_execution_file_record_rejects_negative_size_and_malformed_hash():
    with pytest.raises(ValidationError):
        _record("regular", "output/proposal.json", -1, None)
    with pytest.raises(ValidationError):
        _record("regular", "output/proposal.json", 3, "not-a-sha256")
    with pytest.raises(ValidationError):
        _record("regular", "output/proposal.json", 3, "sha256:" + "A" * 64)


def test_execution_file_record_json_round_trip():
    safe = _record("regular", "output/proposal.json", 12, VALID_HASH)
    unsafe = _record("fifo", "scratch/pipe", None, None)
    for record in (safe, unsafe):
        restored = ExecutionFileRecord.model_validate_json(record.model_dump_json())
        assert restored == record
        assert restored.size_bytes == record.size_bytes
        assert restored.content_hash == record.content_hash


def test_confinement_levels_match_spec_values():
    assert len(ConfinementLevel) == 4
    assert {level.value for level in ConfinementLevel} == {
        "test_only",
        "path_hygiene",
        "os_sandbox",
        "engine_native_sandbox",
    }
    assert ConfinementLevel.TEST_ONLY == "test_only"


def test_allows_real_engine_requires_qualified_sandbox():
    """§8.3: a real engine requires OS_SANDBOX or ENGINE_NATIVE_SANDBOX."""

    expected = {
        ConfinementLevel.TEST_ONLY: False,
        ConfinementLevel.PATH_HYGIENE: False,
        ConfinementLevel.OS_SANDBOX: True,
        ConfinementLevel.ENGINE_NATIVE_SANDBOX: True,
    }
    assert set(expected) == set(ConfinementLevel)
    for level, allowed in expected.items():
        assert allows_real_engine(level) is allowed


def test_output_policy_outranks_proposal_stage_failures():
    """§6.6 worked example: unauthorized output + malformed proposal."""

    assert (
        primary_attempt_outcome(
            output_policy_violated=True, proposal_format_invalid=True
        )
        is AttemptOutcome.ENGINE_OUTPUT_POLICY_FAILURE
    )
    assert (
        primary_attempt_outcome(
            output_policy_violated=True,
            proposal_format_invalid=True,
            proposal_schema_invalid=True,
            proposal_task_invalid=True,
        )
        is AttemptOutcome.ENGINE_OUTPUT_POLICY_FAILURE
    )


@pytest.mark.parametrize(
    ("higher_flag", "expected"),
    [
        ("resource_limit_breached", AttemptOutcome.ENGINE_RESOURCE_LIMIT_FAILURE),
        ("execution_failed", AttemptOutcome.ENGINE_EXECUTION_FAILURE),
        ("bundle_modified", AttemptOutcome.ENGINE_WORKSPACE_INTEGRITY_FAILURE),
    ],
)
def test_output_policy_yields_to_higher_priority_failures(higher_flag, expected):
    outcome = primary_attempt_outcome(
        output_policy_violated=True,
        proposal_format_invalid=True,
        **{higher_flag: True},
    )
    assert outcome is expected
    assert fallback_allowed(outcome)
