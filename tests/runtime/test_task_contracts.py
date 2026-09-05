from __future__ import annotations

import json

import pytest

from vibereview.runtime.dto import (
    CandidateClaimProposal,
    DiscoveryProposalBundle,
    GenerateCandidateClaimsInput,
    ThemeProposal,
)
from vibereview.runtime.hashing import hash_file, hash_tree
from vibereview.runtime.promotion import ProposalValidationError, validate_proposal
from vibereview.runtime.records import TaskType
from vibereview.runtime.specs import TASK_SPECS
from vibereview.runtime.state import RepositorySnapshot
from vibereview.runtime.tasks import TaskWorkspace


def _compound():
    return DiscoveryProposalBundle(
        themes=[
            ThemeProposal(
                local_ref="theme_1",
                title="Thermal management",
                description="parent",
                origin="human",
            ),
            ThemeProposal(
                local_ref="theme_2",
                title="Substrate preheating",
                description="child",
                origin="human",
                parent_ref="theme_1",
            ),
        ],
        claims=[
            CandidateClaimProposal(
                local_ref="claim_1",
                theme_ref="theme_2",
                candidate_claim="Preheating changes residual stress.",
                origin="human",
                origin_refs=[],
            )
        ],
    )


def test_every_task_type_has_complete_fixed_spec_and_prompt():
    assert set(TASK_SPECS) == set(TaskType)
    for task_type, spec in TASK_SPECS.items():
        assert spec.task_type is task_type
        assert spec.version
        assert spec.prompt_version
        assert spec.prompt_path.is_file()
        assert spec.input_model is not None
        assert spec.proposal_model is not None
        assert spec.promotion_handler
        assert spec.disposition_handler


def test_compound_local_graph_is_valid():
    proposal = _compound()
    validate_proposal(
        TASK_SPECS[TaskType.GENERATE_CANDIDATE_CLAIMS],
        proposal,
        RepositorySnapshot(),
    )


def test_duplicate_local_ref_is_proposal_invalid():
    proposal = _compound().model_copy(
        update={
            "claims": [
                _compound().claims[0].model_copy(update={"local_ref": "theme_1"})
            ]
        }
    )
    with pytest.raises(ProposalValidationError):
        validate_proposal(
            TASK_SPECS[TaskType.GENERATE_CANDIDATE_CLAIMS],
            proposal,
            RepositorySnapshot(),
        )


def test_unknown_local_theme_ref_is_proposal_invalid():
    proposal = _compound().model_copy(
        update={
            "claims": [
                _compound().claims[0].model_copy(update={"theme_ref": "missing"})
            ]
        }
    )
    with pytest.raises(ProposalValidationError):
        validate_proposal(
            TASK_SPECS[TaskType.GENERATE_CANDIDATE_CLAIMS],
            proposal,
            RepositorySnapshot(),
        )


def test_task_workspace_writes_immutable_snapshot_and_manifest(tmp_path):
    workspace = TaskWorkspace(tmp_path)
    spec = TASK_SPECS[TaskType.GENERATE_CANDIDATE_CLAIMS]
    input_dto = GenerateCandidateClaimsInput(topic="stress", existing_theme_ids=[])
    task_dir, manifest = workspace.create(
        spec=spec,
        input_dto=input_dto,
        base_generation=7,
        dependencies={},
        snapshot=RepositorySnapshot(),
    )
    assert manifest.base_generation == 7
    assert manifest.instructions_hash == hash_file(task_dir / "instructions.md")
    assert manifest.input_snapshot_hash == hash_tree(task_dir / "input")
    assert json.loads((task_dir / "input" / "input.json").read_text())["topic"] == "stress"
    assert not ((task_dir / "instructions.md").stat().st_mode & 0o222)
    assert not ((task_dir / "input" / "input.json").stat().st_mode & 0o222)


def test_qualified_dependency_keys_snapshot_claim_keyed_objects(
    tmp_path, bundle_factory
):
    snapshot = RepositorySnapshot.model_validate(bundle_factory())
    workspace = TaskWorkspace(tmp_path)
    spec = TASK_SPECS[TaskType.GENERATE_CANDIDATE_CLAIMS]
    dependency = "ClaimAssessment:C0001"
    task_dir, manifest = workspace.create(
        spec=spec,
        input_dto=GenerateCandidateClaimsInput(
            topic="stress", existing_theme_ids=["T0001"]
        ),
        base_generation=0,
        dependencies={dependency: snapshot.dependency_hash(dependency)},
        snapshot=snapshot,
    )
    assert dependency in manifest.dependencies
    assert (task_dir / "input" / "dependencies" / "ClaimAssessment__C0001.json").is_file()
