"""TaskSpec executability preflight (goal.md A12)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from vibereview.runtime.records import (
    AttemptOutcome,
    TaskSpecNotExecutableError,
    TaskType,
    fallback_allowed,
)
from vibereview.runtime.specs import (
    TASK_SPECS,
    executable_task_types,
    validate_task_spec_executable,
)

FULLY_IMPLEMENTED = frozenset(
    {
        TaskType.PARSE_DEEP_RESEARCH,
        TaskType.CORPUS_CHALLENGER,
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        TaskType.ASSESS_CLAIM,
        TaskType.AUDIT_PROPOSITION,
    }
)


def test_fully_implemented_specs_pass_preflight():
    for task_type in FULLY_IMPLEMENTED:
        validate_task_spec_executable(TASK_SPECS[task_type])


def test_specs_with_unimplemented_handlers_fail_preflight():
    unimplemented = set(TaskType) - FULLY_IMPLEMENTED
    assert unimplemented, "the registry lists planned tasks without handlers"
    for task_type in unimplemented:
        with pytest.raises(TaskSpecNotExecutableError) as excinfo:
            validate_task_spec_executable(TASK_SPECS[task_type])
        assert excinfo.value.code == "TASK_TYPE_NOT_IMPLEMENTED"
        assert excinfo.value.task_type is task_type
        assert excinfo.value.problems


def test_executable_task_types_is_a_proper_subset():
    executable = executable_task_types()
    assert executable == FULLY_IMPLEMENTED
    assert executable < set(TaskType)


def test_every_spec_passes_or_fails_with_task_type_not_implemented():
    for spec in TASK_SPECS.values():
        try:
            validate_task_spec_executable(spec)
        except TaskSpecNotExecutableError:
            continue


def test_missing_prompt_fails_preflight():
    spec = dataclasses.replace(
        TASK_SPECS[TaskType.ASSESS_CLAIM],
        prompt_path=Path("/nonexistent/prompts/assess_claim.md"),
    )
    with pytest.raises(TaskSpecNotExecutableError, match="prompt file"):
        validate_task_spec_executable(spec)


@pytest.mark.parametrize(
    "field",
    [
        "invocation_model",
        "engine_input_model",
        "proposal_model",
        "dependency_builder",
        "resource_builder",
        "engine_input_builder",
    ],
)
def test_missing_model_or_builder_fails_preflight(field):
    spec = dataclasses.replace(TASK_SPECS[TaskType.ASSESS_CLAIM], **{field: None})
    with pytest.raises(TaskSpecNotExecutableError) as excinfo:
        validate_task_spec_executable(spec)
    assert any(field.split("_")[0] in problem for problem in excinfo.value.problems)


def test_unknown_handler_names_fail_preflight():
    spec = dataclasses.replace(
        TASK_SPECS[TaskType.ASSESS_CLAIM], promotion_handler="promote_nothing"
    )
    with pytest.raises(TaskSpecNotExecutableError, match="promotion handler"):
        validate_task_spec_executable(spec)
    spec = dataclasses.replace(
        TASK_SPECS[TaskType.ASSESS_CLAIM], disposition_handler="interpret_nothing"
    )
    with pytest.raises(TaskSpecNotExecutableError, match="disposition handler"):
        validate_task_spec_executable(spec)


def test_task_type_not_implemented_is_distinct_from_engine_failures():
    outcome = AttemptOutcome.TASK_TYPE_NOT_IMPLEMENTED
    assert outcome.value == "task_type_not_implemented"
    assert not fallback_allowed(outcome)
    assert TaskSpecNotExecutableError.code == "TASK_TYPE_NOT_IMPLEMENTED"
