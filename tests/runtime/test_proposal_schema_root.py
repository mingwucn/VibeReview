"""Object-root proposal-schema preflight (goal.md §6.11)."""

from __future__ import annotations

import dataclasses

import pytest
from pydantic import BaseModel, RootModel

from vibereview.runtime.dto import (
    EvidenceRecordProposal,
    EvidenceRecordProposalBundle,
)
from vibereview.runtime.records import TaskSpecNotExecutableError, TaskType
from vibereview.runtime.specs import TASK_SPECS, validate_task_spec_executable


class EvidenceProposalList(RootModel[list[EvidenceRecordProposal]]):
    """The §6.11 invalid example: a root list instead of a named object."""


class EvidenceProposalMapping(RootModel[dict[str, str]]):
    """Any RootModel is rejected, even one whose schema is object-shaped."""


class NamedEvidenceBundle(BaseModel):
    items: list[EvidenceRecordProposal]


class RecursiveProposalBundle(BaseModel):
    note: str
    children: list[RecursiveProposalBundle] = []


class NonObjectProposal(BaseModel):
    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        return {"type": "array", "items": {"type": "string"}}


def _spec_with_proposal(proposal_model):
    # ASSESS_CLAIM is fully implemented, so a preflight failure can only
    # come from the swapped-in proposal model.
    return dataclasses.replace(
        TASK_SPECS[TaskType.ASSESS_CLAIM], proposal_model=proposal_model
    )


@pytest.mark.parametrize(
    "proposal_model", [EvidenceProposalList, EvidenceProposalMapping]
)
def test_root_model_proposals_fail_preflight(proposal_model):
    spec = _spec_with_proposal(proposal_model)
    with pytest.raises(TaskSpecNotExecutableError, match="is a RootModel") as excinfo:
        validate_task_spec_executable(spec)
    assert excinfo.value.code == "TASK_TYPE_NOT_IMPLEMENTED"
    assert excinfo.value.task_type is TaskType.ASSESS_CLAIM
    assert any(
        "named object models" in problem for problem in excinfo.value.problems
    )


@pytest.mark.parametrize(
    "proposal_model", [NamedEvidenceBundle, EvidenceRecordProposalBundle]
)
def test_named_bundle_proposals_pass_schema_root_check(proposal_model):
    validate_task_spec_executable(_spec_with_proposal(proposal_model))


def test_top_level_ref_resolving_to_object_passes():
    schema = RecursiveProposalBundle.model_json_schema()
    assert schema["$ref"] == "#/$defs/RecursiveProposalBundle"
    assert schema["$defs"]["RecursiveProposalBundle"]["type"] == "object"
    validate_task_spec_executable(_spec_with_proposal(RecursiveProposalBundle))


def test_non_object_schema_root_fails_preflight():
    assert NonObjectProposal.model_json_schema()["type"] == "array"
    spec = _spec_with_proposal(NonObjectProposal)
    with pytest.raises(
        TaskSpecNotExecutableError, match="schema root must be type 'object'"
    ) as excinfo:
        validate_task_spec_executable(spec)
    assert any("'array'" in problem for problem in excinfo.value.problems)


def test_registry_specs_have_no_schema_root_problems():
    assert len(TASK_SPECS) == len(TaskType) == 13
    for spec in TASK_SPECS.values():
        try:
            validate_task_spec_executable(spec)
        except TaskSpecNotExecutableError as excinfo:
            # Registry proposals are object-rooted; preflight may fail only
            # because some handlers remain unimplemented.
            assert excinfo.problems
            for problem in excinfo.problems:
                assert "RootModel" not in problem
                assert "schema root" not in problem
                assert "handler" in problem and "not implemented" in problem
