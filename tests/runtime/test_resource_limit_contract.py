"""Resource-limit codes, secondary-failure records and precedence (goal.md §6.4-§6.7)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from vibereview.runtime.records import (
    FALLBACK_OUTCOMES,
    AttemptFailure,
    AttemptFailureStage,
    AttemptOutcome,
    ResourceLimitCode,
    TaskAttemptRecord,
    fallback_allowed,
)
from vibereview.runtime.subprocess import (
    QUOTA_EXEMPT_ROOTS,
    WRITABLE_QUOTA_ROOTS,
    deterministic_test_policy,
    primary_attempt_outcome,
    writable_quota_applies,
)

EXPECTED_RESOURCE_LIMIT_CODES = {
    "MAX_WRITABLE_TREE_BYTES": "max_writable_tree_bytes",
    "MAX_WRITABLE_FILE_COUNT": "max_writable_file_count",
    "MAX_WRITABLE_ENTRY_COUNT": "max_writable_entry_count",
    "MAX_WRITABLE_SINGLE_FILE_BYTES": "max_writable_single_file_bytes",
    "MAX_WRITABLE_DIRECTORY_DEPTH": "max_writable_directory_depth",
    "MAX_PROCESS_COUNT": "max_process_count",
    "MAX_OPEN_FILES": "max_open_files",
    "MAX_CPU_TIME": "max_cpu_time",
    "MAX_ADDRESS_SPACE": "max_address_space",
}

EXPECTED_FAILURE_STAGES = {
    "PROCESS": "process",
    "WORKSPACE": "workspace",
    "OUTPUT_TREE": "output_tree",
    "PROPOSAL_FILE": "proposal_file",
    "FORMAT": "format",
    "SCHEMA": "schema",
    "PROPOSAL_VALIDATION": "proposal_validation",
    "RESOURCE_LIMIT": "resource_limit",
}

ENGINE_OUTCOMES = frozenset(
    {
        AttemptOutcome.ENGINE_EXECUTION_FAILURE,
        AttemptOutcome.ENGINE_FORMAT_FAILURE,
        AttemptOutcome.ENGINE_SCHEMA_FAILURE,
        AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE,
        AttemptOutcome.ENGINE_WORKSPACE_INTEGRITY_FAILURE,
        AttemptOutcome.ENGINE_OUTPUT_POLICY_FAILURE,
        AttemptOutcome.ENGINE_RESOURCE_LIMIT_FAILURE,
    }
)

NON_FALLBACK_OUTCOMES = (
    AttemptOutcome.VALID_SCIENTIFIC_RESULT,
    AttemptOutcome.STALE_SNAPSHOT,
    AttemptOutcome.TASK_TYPE_NOT_IMPLEMENTED,
    AttemptOutcome.INTERNAL_RUNTIME_FAILURE,
    AttemptOutcome.TRANSACTION_FAILURE,
    AttemptOutcome.CONTRACT_IMPLEMENTATION_FAILURE,
)

PRECEDENCE_ROWS = (
    ("resource_limit_breached", AttemptOutcome.ENGINE_RESOURCE_LIMIT_FAILURE),
    ("execution_failed", AttemptOutcome.ENGINE_EXECUTION_FAILURE),
    ("bundle_modified", AttemptOutcome.ENGINE_WORKSPACE_INTEGRITY_FAILURE),
    ("output_policy_violated", AttemptOutcome.ENGINE_OUTPUT_POLICY_FAILURE),
    ("proposal_format_invalid", AttemptOutcome.ENGINE_FORMAT_FAILURE),
    ("proposal_schema_invalid", AttemptOutcome.ENGINE_SCHEMA_FAILURE),
    ("proposal_task_invalid", AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE),
)


def _attempt_record(**overrides) -> TaskAttemptRecord:
    values = {
        "task_id": "TASK0001",
        "attempt_id": "attempt-1",
        "engine": "mock",
        "engine_version": None,
        "outcome": AttemptOutcome.ENGINE_RESOURCE_LIMIT_FAILURE,
        "format_valid": False,
        "schema_valid": False,
        "proposal_validation_valid": None,
        "accepted_attempt": False,
        "validation_errors": [],
        "output_hash": None,
        "agent_result_path": Path("attempt/agent_result.json"),
    }
    values.update(overrides)
    return TaskAttemptRecord(**values)


def test_resource_limit_code_has_exactly_the_spec_codes():
    assert len(ResourceLimitCode) == len(EXPECTED_RESOURCE_LIMIT_CODES)
    assert {
        member.name: member.value for member in ResourceLimitCode
    } == EXPECTED_RESOURCE_LIMIT_CODES


@pytest.mark.parametrize(
    ("member", "value"),
    [(ResourceLimitCode[name], value) for name, value in EXPECTED_RESOURCE_LIMIT_CODES.items()],
    ids=list(EXPECTED_RESOURCE_LIMIT_CODES),
)
def test_resource_limit_code_string_values(member, value):
    assert member == value
    assert member.value == value
    assert str(member) == value


def test_attempt_failure_stage_has_exactly_the_eight_spec_stages():
    assert len(AttemptFailureStage) == 8
    assert {
        member.name: member.value for member in AttemptFailureStage
    } == EXPECTED_FAILURE_STAGES


@pytest.mark.parametrize(
    ("member", "value"),
    [(AttemptFailureStage[name], value) for name, value in EXPECTED_FAILURE_STAGES.items()],
    ids=list(EXPECTED_FAILURE_STAGES),
)
def test_attempt_failure_stage_string_values(member, value):
    assert member == value
    assert member.value == value


def test_attempt_failure_serializes_to_the_spec_example():
    failure = AttemptFailure(
        code=ResourceLimitCode.MAX_WRITABLE_TREE_BYTES,
        stage=AttemptFailureStage.RESOURCE_LIMIT,
        message="Writable tree exceeded 16777216 bytes.",
        relative_path=Path("scratch"),
    )
    expected = {
        "code": "max_writable_tree_bytes",
        "stage": "resource_limit",
        "message": "Writable tree exceeded 16777216 bytes.",
        "relative_path": "scratch",
    }
    assert failure.model_dump(mode="json") == expected
    assert json.loads(failure.model_dump_json()) == expected
    assert failure.model_dump_json() == (
        '{"code":"max_writable_tree_bytes","stage":"resource_limit",'
        '"message":"Writable tree exceeded 16777216 bytes.",'
        '"relative_path":"scratch"}'
    )


def test_attempt_failure_relative_path_defaults_to_none():
    failure = AttemptFailure(
        code="max_process_count",
        stage=AttemptFailureStage.RESOURCE_LIMIT,
        message="Process count exceeded 64.",
    )
    assert failure.relative_path is None
    assert failure.model_dump(mode="json")["relative_path"] is None


@pytest.mark.parametrize("code", list(ResourceLimitCode))
def test_attempt_failure_accepts_resource_limit_code_members(code):
    failure = AttemptFailure(
        code=code,
        stage=AttemptFailureStage.RESOURCE_LIMIT,
        message=f"breached {code.value}",
    )
    assert failure.code == code.value
    assert json.loads(failure.model_dump_json())["code"] == code.value
    assert json.loads(failure.model_dump_json())["stage"] == "resource_limit"


def test_attempt_failure_rejects_unknown_stage():
    with pytest.raises(ValidationError):
        AttemptFailure(
            code="max_open_files",
            stage="launch",
            message="unknown stage",
        )


def test_attempt_failure_rejects_extra_fields():
    with pytest.raises(ValidationError):
        AttemptFailure(
            code="max_cpu_time",
            stage=AttemptFailureStage.RESOURCE_LIMIT,
            message="cpu",
            detail="not a contract field",
        )


def test_task_attempt_record_detected_failures_defaults_to_empty():
    record = _attempt_record()
    assert record.detected_failures == ()
    assert isinstance(record.detected_failures, tuple)
    assert json.loads(record.model_dump_json())["detected_failures"] == []


def test_task_attempt_record_accepts_detected_failures_tuple():
    failures = (
        AttemptFailure(
            code=ResourceLimitCode.MAX_OPEN_FILES,
            stage=AttemptFailureStage.RESOURCE_LIMIT,
            message="Open files exceeded 1024.",
        ),
        AttemptFailure(
            code="unexpected_output_file",
            stage=AttemptFailureStage.OUTPUT_TREE,
            message="output/notes.txt is not an allowed output file.",
            relative_path=Path("output/notes.txt"),
        ),
    )
    record = _attempt_record(detected_failures=failures)
    assert record.detected_failures == failures
    assert isinstance(record.detected_failures, tuple)


def test_task_attempt_record_round_trips_detected_failures_through_json():
    failure = AttemptFailure(
        code=ResourceLimitCode.MAX_WRITABLE_TREE_BYTES,
        stage=AttemptFailureStage.RESOURCE_LIMIT,
        message="Writable tree exceeded 16777216 bytes.",
        relative_path=Path("scratch"),
    )
    record = _attempt_record(detected_failures=(failure,))
    payload = json.loads(record.model_dump_json())
    assert payload["detected_failures"] == [
        {
            "code": "max_writable_tree_bytes",
            "stage": "resource_limit",
            "message": "Writable tree exceeded 16777216 bytes.",
            "relative_path": "scratch",
        }
    ]
    restored = TaskAttemptRecord.model_validate(payload)
    assert restored == record
    assert isinstance(restored.detected_failures, tuple)


def test_resource_limit_failure_is_fallback_eligible():
    outcome = AttemptOutcome.ENGINE_RESOURCE_LIMIT_FAILURE
    assert outcome.value == "engine_resource_limit_failure"
    assert outcome in FALLBACK_OUTCOMES
    assert fallback_allowed(outcome)


def test_fallback_set_is_exactly_the_seven_engine_outcomes():
    assert FALLBACK_OUTCOMES == ENGINE_OUTCOMES
    assert len(FALLBACK_OUTCOMES) == 7
    assert set(AttemptOutcome) == ENGINE_OUTCOMES | frozenset(NON_FALLBACK_OUTCOMES)


@pytest.mark.parametrize("outcome", NON_FALLBACK_OUTCOMES)
def test_scientific_dispositions_remain_non_fallback(outcome):
    assert outcome not in FALLBACK_OUTCOMES
    assert not fallback_allowed(outcome)


@pytest.mark.parametrize(
    ("flag", "expected"),
    list(PRECEDENCE_ROWS),
    ids=[name for name, _ in PRECEDENCE_ROWS],
)
def test_primary_outcome_outranks_all_lower_priority_conditions(flag, expected):
    row_index = [name for name, _ in PRECEDENCE_ROWS].index(flag)
    lower_flags = {name for name, _ in PRECEDENCE_ROWS[row_index + 1 :]}
    flags = {name: True for name in {flag} | lower_flags}
    assert primary_attempt_outcome(**flags) is expected


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        (
            {"execution_failed": True},
            AttemptOutcome.ENGINE_EXECUTION_FAILURE,
        ),
        (
            {"bundle_modified": True, "proposal_format_invalid": True},
            AttemptOutcome.ENGINE_WORKSPACE_INTEGRITY_FAILURE,
        ),
        (
            {"output_policy_violated": True, "proposal_format_invalid": True},
            AttemptOutcome.ENGINE_OUTPUT_POLICY_FAILURE,
        ),
        (
            {},
            AttemptOutcome.VALID_SCIENTIFIC_RESULT,
        ),
    ],
    ids=[
        "non_zero_exit_with_valid_proposal",
        "bundle_mutation_with_malformed_proposal",
        "unauthorized_output_with_malformed_proposal",
        "reject_assessment_is_valid_result",
    ],
)
def test_primary_outcome_spec_worked_examples(flags, expected):
    assert primary_attempt_outcome(**flags) is expected


def test_primary_outcome_is_deterministic():
    cases = [
        {"resource_limit_breached": True, "execution_failed": True},
        {"bundle_modified": True, "proposal_schema_invalid": True},
        {"proposal_task_invalid": True},
        {},
    ]
    for flags in cases:
        results = {primary_attempt_outcome(**flags) for _ in range(3)}
        assert len(results) == 1


def test_writable_quota_roots_match_spec():
    assert WRITABLE_QUOTA_ROOTS == ("output", "scratch", "home", "tmp")
    assert QUOTA_EXEMPT_ROOTS == ("bundle", "launcher", "credentials")
    assert set(WRITABLE_QUOTA_ROOTS).isdisjoint(QUOTA_EXEMPT_ROOTS)


@pytest.mark.parametrize(
    "relative_path",
    [
        Path("output"),
        Path("output/proposal.json"),
        Path("scratch"),
        Path("scratch/deep/nested.txt"),
        Path("home/.config/engine/settings.json"),
        Path("tmp/cache.bin"),
    ],
)
def test_writable_quota_applies_to_quota_roots(relative_path):
    assert writable_quota_applies(relative_path)


@pytest.mark.parametrize(
    "relative_path",
    [
        Path("bundle"),
        Path("bundle/input/input.json"),
        Path("launcher/worker.py"),
        Path("credentials/token"),
        Path("outputs"),
        Path(""),
    ],
)
def test_writable_quota_excludes_bundle_and_other_roots(relative_path):
    assert not writable_quota_applies(relative_path)


def test_proposal_remains_subject_to_max_proposal_bytes():
    policy = deterministic_test_policy()
    assert policy.allowed_output_files == ("proposal.json",)
    proposal_path = Path("output") / policy.allowed_output_files[0]
    assert writable_quota_applies(proposal_path)
    assert policy.max_proposal_bytes == 1048576
    assert policy.max_writable_tree_bytes == 16777216
