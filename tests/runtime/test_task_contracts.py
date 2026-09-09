from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from vibereview.runtime.dto import (
    AggregatePaperEvidenceInvocation,
    AssessClaimInput,
    AssessClaimInvocation,
    AssessEvidenceInvocation,
    AuditPropositionInvocation,
    AuditRenderedSentenceInvocation,
    CandidateClaimProposal,
    CorpusChallengerInvocation,
    DiscoveryProposalBundle,
    GenerateCandidateClaimsInput,
    GenerateCandidateClaimsInvocation,
    GeneratePropositionsInvocation,
    GenerateRetrievalQueriesInvocation,
    ParseDeepResearchInput,
    ParseDeepResearchInvocation,
    RenderProseInvocation,
    ReviseClaimInvocation,
    ThemeProposal,
    ValidateFinalClaimInvocation,
)
from vibereview.runtime.hashing import hash_file, hash_tree
from vibereview.runtime.promotion import ProposalValidationError, validate_proposal
from vibereview.runtime.records import (
    MEDIA_EXTENSIONS,
    AttemptOutcome,
    FALLBACK_OUTCOMES,
    ProjectContext,
    ResourceValidationError,
    SnapshottedResource,
    TaskResourceRequest,
    TaskType,
    allocate_resource_id,
    build_resource_requests,
    fallback_allowed,
    resource_destination,
    validate_resource_requests,
)
from vibereview.runtime.specs import TASK_SPECS
from vibereview.runtime.state import RepositorySnapshot
from vibereview.runtime.tasks import TaskWorkspace

VALID_HASH = "sha256:" + "a" * 64

SAMPLE_INVOCATIONS = {
    TaskType.PARSE_DEEP_RESEARCH: ParseDeepResearchInvocation(
        topic="residual stress", document_paths=[Path("/docs/a.md")]
    ),
    TaskType.CORPUS_CHALLENGER: CorpusChallengerInvocation(
        topic="residual stress", paper_ids=["P0001"]
    ),
    TaskType.GENERATE_CANDIDATE_CLAIMS: GenerateCandidateClaimsInvocation(
        topic="residual stress", existing_theme_ids=["T0001"]
    ),
    TaskType.GENERATE_RETRIEVAL_QUERIES: GenerateRetrievalQueriesInvocation(
        claim_ids=["C0001"]
    ),
    TaskType.ASSESS_EVIDENCE: AssessEvidenceInvocation(
        source_generation=0,
        claim_id="C0001",
        query_id="Q-C0001-SUP-01",
        candidate_refs=["sha256:" + "1" * 64],
        canonical_span_refs=[],
        retrieval_ledger_path=Path("/docs/retrieval-ledger.json"),
    ),
    TaskType.AGGREGATE_PAPER_EVIDENCE: AggregatePaperEvidenceInvocation(
        claim_id="C0001", paper_id="P0001", evidence_ids=["E0001"]
    ),
    TaskType.ASSESS_CLAIM: AssessClaimInvocation(
        claim_id="C0001", claim_paper_evidence_ids=["CPE-C0001-P0001"]
    ),
    TaskType.REVISE_CLAIM: ReviseClaimInvocation(
        claim_id="C0001", current_candidate_claim="Preheating reduces stress."
    ),
    TaskType.VALIDATE_FINAL_CLAIM: ValidateFinalClaimInvocation(
        claim_id="C0001",
        proposed_final_claim="Preheating reduced stress in the tested window.",
    ),
    TaskType.GENERATE_PROPOSITIONS: GeneratePropositionsInvocation(
        claim_packet_ids=["C0001"],
        corpus_fact_ids=["CF0001"],
        process_fact_ids=["PF0001"],
    ),
    TaskType.AUDIT_PROPOSITION: AuditPropositionInvocation(
        draft_kind="proposition_draft",
        draft_owner_generation=1,
        draft_source_generation=0,
        draft_task_id="TASK0001",
        draft_artifact_hash=VALID_HASH,
        draft_local_ref="proposition_one",
        claim_packet_ids=["C0001"],
        corpus_fact_ids=["CF0001"],
        process_fact_ids=["PF0001"],
        draft_artifact_path=Path("/docs/proposition-draft.json"),
    ),
    TaskType.RENDER_PROSE: RenderProseInvocation(proposition_ids=["PR0001"]),
    TaskType.AUDIT_RENDERED_SENTENCE: AuditRenderedSentenceInvocation(
        draft_kind="rendered_sentence_draft",
        draft_owner_generation=1,
        draft_source_generation=0,
        draft_task_id="TASK0002",
        draft_artifact_hash=VALID_HASH,
        draft_local_ref="sentence_one",
        source_proposition_ids=["PR0001"],
        draft_artifact_path=Path("/docs/rendered-sentence-draft.json"),
    ),
}


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


def _request(
    resource_id="RES0001",
    logical_name="notes.md",
    source_path=Path("/tmp/docs/notes.md"),
    media_type="text/markdown",
):
    return TaskResourceRequest(
        resource_id=resource_id,
        logical_name=logical_name,
        source_path=source_path,
        media_type=media_type,
    )


def _snapshotted(**overrides):
    values = {
        "resource_id": "RES0001",
        "logical_name": "notes.md",
        "snapshot_relative_path": Path("input/resources/RES0001/content.md"),
        "media_type": "text/markdown",
        "size_bytes": 12,
        "content_hash": VALID_HASH,
    }
    values.update(overrides)
    return SnapshottedResource(**values)


def _snapshotted_for(requests):
    return tuple(
        SnapshottedResource(
            resource_id=request.resource_id,
            logical_name=request.logical_name,
            snapshot_relative_path=resource_destination(
                request.resource_id, request.media_type
            ),
            media_type=request.media_type,
            size_bytes=12,
            content_hash=VALID_HASH,
        )
        for request in requests
    )


def _mentions_path(annotation) -> bool:
    if annotation is Path:
        return True
    return any(_mentions_path(arg) for arg in get_args(annotation))


def test_every_task_type_has_complete_fixed_spec_and_prompt():
    assert set(TASK_SPECS) == set(TaskType)
    for task_type, spec in TASK_SPECS.items():
        assert spec.task_type is task_type
        assert spec.version
        assert spec.prompt_version
        assert spec.prompt_path.is_file()
        assert spec.invocation_model is not None
        assert spec.engine_input_model is not None
        assert spec.proposal_model is not None
        assert callable(spec.dependency_builder)
        assert callable(spec.resource_builder)
        assert callable(spec.engine_input_builder)
        assert spec.promotion_handler
        assert spec.disposition_handler


def test_task_spec_is_frozen_and_keeps_input_model_alias():
    spec = TASK_SPECS[TaskType.ASSESS_CLAIM]
    assert spec.input_model is spec.engine_input_model
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.version = "2"


def test_invocation_models_match_registry_and_builders_run(bundle_factory):
    snapshot = RepositorySnapshot.model_validate(bundle_factory())
    context = ProjectContext(project_root=Path("/project"))
    assert set(SAMPLE_INVOCATIONS) == set(TaskType)
    for task_type, invocation in SAMPLE_INVOCATIONS.items():
        spec = TASK_SPECS[task_type]
        assert type(invocation) is spec.invocation_model
        keys = spec.dependency_builder(invocation, snapshot)
        assert isinstance(keys, tuple)
        for key in keys:
            assert re.fullmatch(r"[A-Z][A-Za-z0-9]*:.+", key), key
            snapshot.dependency_hash(key)
        requests = spec.resource_builder(invocation, snapshot, context)
        if task_type in {
            TaskType.PARSE_DEEP_RESEARCH,
            TaskType.ASSESS_EVIDENCE,
            TaskType.AUDIT_PROPOSITION,
            TaskType.AUDIT_RENDERED_SENTENCE,
        }:
            assert requests
        else:
            assert requests == ()


def test_dependency_builders_return_qualified_keys(bundle_factory):
    snapshot = RepositorySnapshot.model_validate(bundle_factory())
    cases = {
        TaskType.ASSESS_EVIDENCE: (
            SAMPLE_INVOCATIONS[TaskType.ASSESS_EVIDENCE],
            (
                "CandidateClaim:C0001",
                "RetrievalQuery:Q-C0001-SUP-01",
            ),
        ),
        TaskType.ASSESS_CLAIM: (
            AssessClaimInvocation(
                claim_id="C0001", claim_paper_evidence_ids=["CPE-C0001-P0001"]
            ),
            (
                "CandidateClaim:C0001",
                "ClaimPaperEvidence:CPE-C0001-P0001",
                "Paper:P0001",
                "EvidenceRecord:E0001",
                "RetrievedSpan:R0001",
                "RetrievalDisposition:R0001",
                "RetrievalQuery:Q-C0001-SUP-01",
            ),
        ),
        TaskType.AGGREGATE_PAPER_EVIDENCE: (
            AggregatePaperEvidenceInvocation(
                claim_id="C0001", paper_id="P0001", evidence_ids=["E0001"]
            ),
            (
                "CandidateClaim:C0001",
                "Paper:P0001",
                "EvidenceRecord:E0001",
                "RetrievedSpan:R0001",
                "RetrievalDisposition:R0001",
                "RetrievalQuery:Q-C0001-SUP-01",
            ),
        ),
        TaskType.REVISE_CLAIM: (
            ReviseClaimInvocation(
                claim_id="C0001",
                current_candidate_claim="Preheating reduces residual stress.",
            ),
            (
                "CandidateClaim:C0001",
                "ClaimAssessment:C0001",
                "ClaimPaperEvidence:CPE-C0001-P0001",
                "Paper:P0001",
                "EvidenceRecord:E0001",
                "RetrievedSpan:R0001",
                "RetrievalDisposition:R0001",
                "RetrievalQuery:Q-C0001-SUP-01",
            ),
        ),
        TaskType.VALIDATE_FINAL_CLAIM: (
            ValidateFinalClaimInvocation(
                claim_id="C0001", proposed_final_claim="Preheating reduced stress."
            ),
            (
                "CandidateClaim:C0001",
                "ClaimAssessment:C0001",
                "ClaimPaperEvidence:CPE-C0001-P0001",
                "Paper:P0001",
                "EvidenceRecord:E0001",
                "RetrievedSpan:R0001",
                "RetrievalDisposition:R0001",
                "RetrievalQuery:Q-C0001-SUP-01",
            ),
        ),
        TaskType.AUDIT_RENDERED_SENTENCE: (
            SAMPLE_INVOCATIONS[TaskType.AUDIT_RENDERED_SENTENCE],
            (
                "PropositionRecord:PR0001",
                "SemanticAuditResult:SA0001",
            ),
        ),
    }
    for task_type, (invocation, expected) in cases.items():
        keys = TASK_SPECS[task_type].dependency_builder(invocation, snapshot)
        assert keys == expected
        for key in keys:
            snapshot.dependency_hash(key)


def test_engine_input_models_never_serialize_paths():
    for spec in TASK_SPECS.values():
        for field in spec.engine_input_model.model_fields.values():
            assert not _mentions_path(field.annotation), (
                spec.task_type,
                field,
            )


def test_only_resource_backed_invocations_may_hold_paths():
    for task_type, spec in TASK_SPECS.items():
        for field in spec.invocation_model.model_fields.values():
            if _mentions_path(field.annotation):
                assert task_type in {
                    TaskType.PARSE_DEEP_RESEARCH,
                    TaskType.CORPUS_CHALLENGER,
                    TaskType.GENERATE_CANDIDATE_CLAIMS,
                    TaskType.ASSESS_EVIDENCE,
                    TaskType.AUDIT_PROPOSITION,
                    TaskType.AUDIT_RENDERED_SENTENCE,
                }


def test_parse_deep_research_resource_builder_allocates_ordered_resources():
    spec = TASK_SPECS[TaskType.PARSE_DEEP_RESEARCH]
    invocation = ParseDeepResearchInvocation(
        topic="residual stress",
        document_paths=[
            Path("/home/user/research/alpha.md"),
            Path("/home/user/research/beta.md"),
        ],
    )
    requests = spec.resource_builder(
        invocation,
        RepositorySnapshot(),
        ProjectContext(project_root=Path("/home/user/project")),
    )
    assert [request.resource_id for request in requests] == ["RES0001", "RES0002"]
    assert [request.logical_name for request in requests] == ["alpha.md", "beta.md"]
    assert all(request.media_type == "text/markdown" for request in requests)
    assert requests[0].source_path == Path("/home/user/research/alpha.md")


def test_parse_deep_research_engine_input_builder_maps_resource_ids_in_order():
    spec = TASK_SPECS[TaskType.PARSE_DEEP_RESEARCH]
    invocation = ParseDeepResearchInvocation(
        topic="residual stress",
        document_paths=[
            Path("/home/user/research/alpha.md"),
            Path("/home/user/research/beta.md"),
        ],
    )
    requests = spec.resource_builder(
        invocation,
        RepositorySnapshot(),
        ProjectContext(project_root=Path("/home/user/project")),
    )
    engine_input = spec.engine_input_builder(invocation, _snapshotted_for(requests))
    assert engine_input == ParseDeepResearchInput(
        topic="residual stress",
        document_resource_ids=["RES0001", "RES0002"],
    )
    payload = engine_input.model_dump_json()
    assert "/home/user" not in payload
    assert "alpha.md" not in payload
    assert "source_path" not in payload


def test_engine_input_builder_copies_invocation_fields_for_structured_tasks():
    spec = TASK_SPECS[TaskType.ASSESS_CLAIM]
    invocation = AssessClaimInvocation(
        claim_id="C0001", claim_paper_evidence_ids=["CPE-C0001-P0001"]
    )
    engine_input = spec.engine_input_builder(invocation, ())
    assert engine_input == AssessClaimInput(
        claim_id="C0001", claim_paper_evidence_ids=["CPE-C0001-P0001"]
    )


def test_media_extensions_map():
    assert MEDIA_EXTENSIONS == {
        "text/markdown": ".md",
        "text/plain": ".txt",
        "application/json": ".json",
    }


def test_allocate_resource_id_sequence():
    assert allocate_resource_id(1) == "RES0001"
    assert allocate_resource_id(2) == "RES0002"
    assert allocate_resource_id(42) == "RES0042"
    with pytest.raises(ResourceValidationError):
        allocate_resource_id(0)


def test_task_resource_request_validates_resource_id_pattern():
    assert _request(resource_id="RES0001").resource_id == "RES0001"
    with pytest.raises(ValidationError):
        _request(resource_id="DOC0001")


def test_validate_resource_requests_rejects_duplicate_ids():
    with pytest.raises(ResourceValidationError, match="duplicate resource_id"):
        validate_resource_requests(
            [
                _request("RES0001", source_path=Path("/tmp/a.md")),
                _request("RES0001", source_path=Path("/tmp/b.md")),
            ]
        )


def test_validate_resource_requests_rejects_unsupported_media_type():
    with pytest.raises(ResourceValidationError, match="unsupported media type"):
        validate_resource_requests([_request(media_type="application/pdf")])
    with pytest.raises(ResourceValidationError, match="unsupported media type"):
        build_resource_requests([Path("/tmp/a.pdf")], media_type="application/pdf")


@pytest.mark.parametrize("name", ["a/b.md", "a\\b.md", ".", "..", ""])
def test_validate_resource_requests_rejects_path_like_logical_names(name):
    with pytest.raises(ResourceValidationError, match="logical_name"):
        validate_resource_requests([_request(logical_name=name)])


def test_duplicate_source_filenames_stay_distinct():
    requests = build_resource_requests(
        [Path("/x/notes.md"), Path("/y/notes.md")], media_type="text/markdown"
    )
    assert [request.resource_id for request in requests] == ["RES0001", "RES0002"]
    assert requests[0].logical_name == requests[1].logical_name == "notes.md"
    destinations = {
        resource_destination(request.resource_id, request.media_type)
        for request in requests
    }
    assert len(destinations) == 2


def test_resource_destination_is_python_controlled():
    destination = resource_destination("RES0007", "text/markdown")
    assert destination == Path("input/resources/RES0007/content.md")
    assert not destination.is_absolute()
    assert ".." not in destination.parts
    with pytest.raises(ResourceValidationError, match="unsupported media type"):
        resource_destination("RES0001", "video/mp4")
    with pytest.raises(ResourceValidationError, match="invalid resource_id"):
        resource_destination("X1", "text/markdown")


def test_snapshotted_resource_accepts_valid_snapshot_path():
    resource = _snapshotted()
    assert resource.snapshot_relative_path == Path(
        "input/resources/RES0001/content.md"
    )


def test_snapshotted_resource_rejects_source_path_and_extra_fields():
    with pytest.raises(ValidationError):
        _snapshotted(source_path=Path("/tmp/notes.md"))


def test_snapshotted_resource_rejects_absolute_snapshot_path():
    with pytest.raises(ValidationError, match="relative"):
        _snapshotted(snapshot_relative_path=Path("/tmp/notes.md"))


@pytest.mark.parametrize(
    "bad_path", ["../escape.md", "input/../escape.md", "input/../../escape.md"]
)
def test_snapshotted_resource_rejects_traversal(bad_path):
    with pytest.raises(ValidationError, match="\\.\\."):
        _snapshotted(snapshot_relative_path=Path(bad_path))


def test_snapshotted_resource_rejects_bad_resource_id():
    with pytest.raises(ValidationError):
        _snapshotted(resource_id="DOC0001")


def test_workspace_integrity_failure_is_engine_attributable():
    assert AttemptOutcome.ENGINE_WORKSPACE_INTEGRITY_FAILURE in FALLBACK_OUTCOMES
    assert fallback_allowed(AttemptOutcome.ENGINE_WORKSPACE_INTEGRITY_FAILURE)


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
    invocation = GenerateCandidateClaimsInvocation(topic="stress", existing_theme_ids=[])
    task_dir, manifest, provenance = workspace.create(
        spec=spec,
        invocation=invocation,
        base_generation=7,
        dependencies={},
        snapshot=RepositorySnapshot(),
        context=ProjectContext(project_root=tmp_path),
    )
    assert manifest.base_generation == 7
    assert manifest.instructions_hash == hash_file(task_dir / "bundle" / "instructions.md")
    assert manifest.input_snapshot_hash == hash_tree(task_dir / "bundle" / "input")
    assert (
        json.loads((task_dir / "bundle" / "input" / "input.json").read_text())["topic"]
        == "stress"
    )
    assert not ((task_dir / "bundle" / "instructions.md").stat().st_mode & 0o222)
    assert not ((task_dir / "bundle" / "input" / "input.json").stat().st_mode & 0o222)


def test_qualified_dependency_keys_snapshot_claim_keyed_objects(
    tmp_path, bundle_factory
):
    snapshot = RepositorySnapshot.model_validate(bundle_factory())
    workspace = TaskWorkspace(tmp_path)
    spec = TASK_SPECS[TaskType.GENERATE_CANDIDATE_CLAIMS]
    dependency = "ClaimAssessment:C0001"
    task_dir, manifest, provenance = workspace.create(
        spec=spec,
        invocation=GenerateCandidateClaimsInvocation(
            topic="stress", existing_theme_ids=["T0001"]
        ),
        base_generation=0,
        dependencies={dependency: snapshot.dependency_hash(dependency)},
        snapshot=snapshot,
        context=ProjectContext(project_root=tmp_path),
    )
    assert dependency in manifest.dependencies
    assert (
        task_dir / "bundle" / "input" / "dependencies" / "ClaimAssessment__C0001.json"
    ).is_file()
