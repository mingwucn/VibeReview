"""Fallback eligibility and frozen primary-outcome precedence (goal.md §6.3-§6.6)."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from vibereview.enums import ClaimDecision, RejectionBasis
from vibereview.models import ClaimAssessment
from vibereview.runtime.records import (
    FALLBACK_OUTCOMES,
    AttemptFailure,
    AttemptFailureStage,
    AttemptOutcome,
    ResourceLimitCode,
    TaskAttemptRecord,
    fallback_allowed,
)
from vibereview.runtime.subprocess import primary_attempt_outcome

ENGINE_FAILURE_OUTCOMES = (
    AttemptOutcome.ENGINE_EXECUTION_FAILURE,
    AttemptOutcome.ENGINE_FORMAT_FAILURE,
    AttemptOutcome.ENGINE_SCHEMA_FAILURE,
    AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE,
    AttemptOutcome.ENGINE_WORKSPACE_INTEGRITY_FAILURE,
    AttemptOutcome.ENGINE_OUTPUT_POLICY_FAILURE,
    AttemptOutcome.ENGINE_RESOURCE_LIMIT_FAILURE,
)

NON_FALLBACK_OUTCOMES = (
    AttemptOutcome.VALID_SCIENTIFIC_RESULT,
    AttemptOutcome.STALE_SNAPSHOT,
    AttemptOutcome.TASK_TYPE_NOT_IMPLEMENTED,
    AttemptOutcome.INTERNAL_RUNTIME_FAILURE,
    AttemptOutcome.TRANSACTION_FAILURE,
    AttemptOutcome.CONTRACT_IMPLEMENTATION_FAILURE,
)

EXPECTED_OUTCOME_VALUES = {
    "ENGINE_EXECUTION_FAILURE": "engine_execution_failure",
    "ENGINE_FORMAT_FAILURE": "engine_format_failure",
    "ENGINE_SCHEMA_FAILURE": "engine_schema_failure",
    "ENGINE_PROPOSAL_VALIDATION_FAILURE": "engine_proposal_validation_failure",
    "ENGINE_WORKSPACE_INTEGRITY_FAILURE": "engine_workspace_integrity_failure",
    "ENGINE_OUTPUT_POLICY_FAILURE": "engine_output_policy_failure",
    "ENGINE_RESOURCE_LIMIT_FAILURE": "engine_resource_limit_failure",
    "VALID_SCIENTIFIC_RESULT": "valid_scientific_result",
    "STALE_SNAPSHOT": "stale_snapshot",
    "TASK_TYPE_NOT_IMPLEMENTED": "task_type_not_implemented",
    "INTERNAL_RUNTIME_FAILURE": "internal_runtime_failure",
    "TRANSACTION_FAILURE": "transaction_failure",
    "CONTRACT_IMPLEMENTATION_FAILURE": "contract_implementation_failure",
}

PRECEDENCE = (
    ("resource_limit_breached", AttemptOutcome.ENGINE_RESOURCE_LIMIT_FAILURE),
    ("execution_failed", AttemptOutcome.ENGINE_EXECUTION_FAILURE),
    ("bundle_modified", AttemptOutcome.ENGINE_WORKSPACE_INTEGRITY_FAILURE),
    ("output_policy_violated", AttemptOutcome.ENGINE_OUTPUT_POLICY_FAILURE),
    ("proposal_format_invalid", AttemptOutcome.ENGINE_FORMAT_FAILURE),
    ("proposal_schema_invalid", AttemptOutcome.ENGINE_SCHEMA_FAILURE),
    ("proposal_task_invalid", AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE),
)

FLAG_NAMES = tuple(name for name, _ in PRECEDENCE)


def _expected_outcome(active_flags):
    for name, outcome in PRECEDENCE:
        if name in active_flags:
            return outcome
    return AttemptOutcome.VALID_SCIENTIFIC_RESULT


def test_attempt_outcome_has_exactly_the_thirteen_spec_values():
    assert len(AttemptOutcome) == 13
    assert {outcome.name: outcome.value for outcome in AttemptOutcome} == (
        EXPECTED_OUTCOME_VALUES
    )


def test_fallback_outcomes_is_exactly_the_seven_engine_failures():
    assert isinstance(FALLBACK_OUTCOMES, frozenset)
    assert FALLBACK_OUTCOMES == frozenset(ENGINE_FAILURE_OUTCOMES)
    assert AttemptOutcome.ENGINE_OUTPUT_POLICY_FAILURE in FALLBACK_OUTCOMES
    assert AttemptOutcome.ENGINE_RESOURCE_LIMIT_FAILURE in FALLBACK_OUTCOMES


@pytest.mark.parametrize("outcome", ENGINE_FAILURE_OUTCOMES)
def test_engine_failures_are_fallback_eligible(outcome):
    assert fallback_allowed(outcome)


@pytest.mark.parametrize("outcome", NON_FALLBACK_OUTCOMES)
def test_non_engine_outcomes_forbid_fallback(outcome):
    assert not fallback_allowed(outcome)


def test_fallback_partition_covers_every_outcome():
    engine = set(ENGINE_FAILURE_OUTCOMES)
    non_fallback = set(NON_FALLBACK_OUTCOMES)
    assert engine.isdisjoint(non_fallback)
    assert engine | non_fallback == set(AttemptOutcome)
    for outcome in AttemptOutcome:
        assert fallback_allowed(outcome) == (outcome in engine)


def test_primary_attempt_outcome_signature_freezes_the_seven_conditions():
    parameters = inspect.signature(primary_attempt_outcome).parameters
    assert tuple(parameters) == FLAG_NAMES
    for parameter in parameters.values():
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is False


def test_primary_attempt_outcome_is_keyword_only():
    with pytest.raises(TypeError):
        primary_attempt_outcome(True)


def test_no_defect_flags_is_valid_scientific_result():
    assert primary_attempt_outcome() is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert primary_attempt_outcome(**{name: False for name in FLAG_NAMES}) is (
        AttemptOutcome.VALID_SCIENTIFIC_RESULT
    )


@pytest.mark.parametrize("flag,outcome", PRECEDENCE)
def test_single_defect_flag_maps_to_its_primary_outcome(flag, outcome):
    assert primary_attempt_outcome(**{flag: True}) is outcome


@pytest.mark.parametrize(
    "higher,lower",
    [
        (higher, lower)
        for index, (higher, _) in enumerate(PRECEDENCE)
        for lower, _ in PRECEDENCE[index + 1 :]
    ],
)
def test_higher_priority_condition_wins_over_every_lower_one(higher, lower):
    assert primary_attempt_outcome(**{higher: True, lower: True}) is (
        dict(PRECEDENCE)[higher]
    )


def test_every_flag_combination_selects_the_highest_priority_outcome():
    for mask in range(1 << len(FLAG_NAMES)):
        active = {
            name for bit, name in enumerate(FLAG_NAMES) if mask & (1 << bit)
        }
        kwargs = {name: name in active for name in FLAG_NAMES}
        expected = _expected_outcome(active)
        assert primary_attempt_outcome(**kwargs) is expected
        assert primary_attempt_outcome(**kwargs) is expected


def test_outcome_is_independent_of_flag_iteration_order():
    orders = (
        FLAG_NAMES,
        tuple(reversed(FLAG_NAMES)),
        tuple(sorted(FLAG_NAMES)),
        FLAG_NAMES[3:] + FLAG_NAMES[:3],
    )
    for mask in range(1, 1 << len(FLAG_NAMES)):
        active = {
            name for bit, name in enumerate(FLAG_NAMES) if mask & (1 << bit)
        }
        outcomes = {
            primary_attempt_outcome(
                **{name: True for name in order if name in active}
            )
            for order in orders
        }
        assert outcomes == {_expected_outcome(active)}


def test_all_defects_at_once_is_resource_limit_failure():
    kwargs = {name: True for name in FLAG_NAMES}
    assert primary_attempt_outcome(**kwargs) is (
        AttemptOutcome.ENGINE_RESOURCE_LIMIT_FAILURE
    )


def test_nonzero_exit_with_valid_proposal_is_execution_failure():
    outcome = primary_attempt_outcome(execution_failed=True)
    assert outcome is AttemptOutcome.ENGINE_EXECUTION_FAILURE
    assert fallback_allowed(outcome)


def test_bundle_mutation_with_malformed_proposal_is_workspace_integrity_failure():
    outcome = primary_attempt_outcome(
        bundle_modified=True,
        proposal_format_invalid=True,
    )
    assert outcome is AttemptOutcome.ENGINE_WORKSPACE_INTEGRITY_FAILURE
    assert fallback_allowed(outcome)


def test_unauthorized_output_with_malformed_proposal_is_output_policy_failure():
    outcome = primary_attempt_outcome(
        output_policy_violated=True,
        proposal_format_invalid=True,
    )
    assert outcome is AttemptOutcome.ENGINE_OUTPUT_POLICY_FAILURE
    assert fallback_allowed(outcome)


def test_reject_claim_assessment_is_valid_scientific_result_not_fallback():
    assessment = ClaimAssessment(
        claim_id="C0001",
        aggregate_strength="low",
        evidence_sufficiency="insufficient",
        decision="REJECT",
        rejection_basis="contradicted",
        support_summary="No support in the supplied corpus.",
        contradiction_summary="Directly contradicted by the supplied corpus.",
        qualification_summary="None.",
        reason="The supplied corpus contradicts the candidate claim.",
    )
    assert assessment.decision is ClaimDecision.REJECT
    assert assessment.rejection_basis is RejectionBasis.CONTRADICTED
    outcome = primary_attempt_outcome()
    assert outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert not fallback_allowed(outcome)


def test_resource_limit_failure_example_matches_spec_json():
    failure = AttemptFailure.model_validate(
        {
            "code": "max_writable_tree_bytes",
            "stage": "resource_limit",
            "message": "Writable tree exceeded 16777216 bytes.",
            "relative_path": "scratch",
        }
    )
    assert failure.code == ResourceLimitCode.MAX_WRITABLE_TREE_BYTES
    assert failure.stage is AttemptFailureStage.RESOURCE_LIMIT
    assert failure.relative_path == Path("scratch")
    outcome = primary_attempt_outcome(resource_limit_breached=True)
    assert outcome is AttemptOutcome.ENGINE_RESOURCE_LIMIT_FAILURE


def test_detected_failures_defaults_to_empty():
    record = _attempt_record(outcome=AttemptOutcome.VALID_SCIENTIFIC_RESULT)
    assert record.detected_failures == ()


def test_secondary_failures_are_recorded_separately_from_primary_outcome():
    record = _attempt_record(
        outcome=AttemptOutcome.ENGINE_WORKSPACE_INTEGRITY_FAILURE,
        detected_failures=(
            AttemptFailure(
                code=ResourceLimitCode.MAX_WRITABLE_TREE_BYTES,
                stage=AttemptFailureStage.RESOURCE_LIMIT,
                message="Writable tree exceeded 16777216 bytes.",
                relative_path=Path("scratch"),
            ),
            AttemptFailure(
                code="proposal_malformed_json",
                stage=AttemptFailureStage.FORMAT,
                message="proposal.json is not valid JSON.",
                relative_path=Path("output/proposal.json"),
            ),
        ),
        format_valid=False,
        schema_valid=False,
    )
    assert record.outcome is AttemptOutcome.ENGINE_WORKSPACE_INTEGRITY_FAILURE
    assert [failure.stage for failure in record.detected_failures] == [
        AttemptFailureStage.RESOURCE_LIMIT,
        AttemptFailureStage.FORMAT,
    ]


def _attempt_record(**overrides):
    values = {
        "task_id": "TASK0001",
        "attempt_id": "attempt-1",
        "engine": "fake",
        "engine_version": None,
        "outcome": AttemptOutcome.VALID_SCIENTIFIC_RESULT,
        "format_valid": True,
        "schema_valid": True,
        "proposal_validation_valid": True,
        "accepted_attempt": True,
        "validation_errors": [],
        "output_hash": None,
        "agent_result_path": Path("attempt/agent_result.json"),
    }
    values.update(overrides)
    return TaskAttemptRecord(**values)
