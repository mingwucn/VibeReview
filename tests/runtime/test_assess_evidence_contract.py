"""Bounded runtime contract for coupled ASSESS_EVIDENCE tasks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import vibereview.runtime as public_runtime
from vibereview.runtime import (
    AssessEvidenceInput,
    AssessEvidenceInvocation,
    AssessEvidenceProposalBundle,
    AttemptOutcome,
    CoupledProposalValidationError,
    EvidenceAssessmentProposal,
    EvidenceCandidateDecisionProposal,
    MockEngine,
    MockResponse,
    ProjectContext,
    ProjectRuntime,
    RepositorySnapshot,
    TaskSpecNotExecutableError,
    TaskType,
    TaskWorkspace,
)
from vibereview.runtime.coupled import CoupledCommitPlan
from vibereview.runtime.dto import (
    MAX_ASSESS_EVIDENCE_CANDIDATES,
    MAX_EVIDENCE_PROPOSAL_LIMITATION_CHARS,
    MAX_EVIDENCE_PROPOSAL_LIMITATIONS,
    MAX_EVIDENCE_PROPOSAL_TEXT_CHARS,
)
from vibereview.runtime.records import SnapshottedResource
from vibereview.runtime.repository import PromotionPayload
from vibereview.runtime.specs import (
    TASK_SPECS,
    executable_task_types,
    validate_task_spec_executable,
)


CANDIDATE_A = "sha256:" + "1" * 64
CANDIDATE_B = "sha256:" + "2" * 64


def _invocation(ledger_path: Path, **updates) -> AssessEvidenceInvocation:
    values = {
        "source_generation": 0,
        "claim_id": "C0001",
        "query_id": "Q-C0001-SUP-01",
        "candidate_refs": [CANDIDATE_A],
        "canonical_span_refs": [],
        "retrieval_ledger_path": ledger_path,
    }
    values.update(updates)
    return AssessEvidenceInvocation.model_validate(values)


def _evidence(**updates) -> EvidenceAssessmentProposal:
    values = {
        "relation_to_candidate": "supports",
        "evidence_summary": "The synthetic result supports the candidate claim.",
        "quality": {
            "directness": "direct",
            "methodological_relevance": "high",
            "strength": "high",
            "assessability": "full",
            "limitations": [],
        },
        "assessment_note": "Synthetic coupled-evidence assessment.",
    }
    values.update(updates)
    return EvidenceAssessmentProposal.model_validate(values)


def _assessed_bundle() -> AssessEvidenceProposalBundle:
    return AssessEvidenceProposalBundle(
        decisions=[
            EvidenceCandidateDecisionProposal(
                candidate_ref=CANDIDATE_A,
                status="assessed",
                evidence=_evidence(),
            )
        ]
    )


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"candidate_refs": []}, "at least 1"),
        (
            {"candidate_refs": [CANDIDATE_A, CANDIDATE_A]},
            "candidate_refs must be unique",
        ),
        (
            {"canonical_span_refs": ["R0001", "R0001"]},
            "canonical_span_refs must be unique",
        ),
        ({"source_generation": -1}, "greater than or equal to 0"),
        ({"query_id": "Q-C0002-SUP-01"}, "query_id must encode claim_id"),
    ],
)
def test_invocation_rejects_unbound_or_empty_identity(tmp_path, updates, message):
    with pytest.raises(ValidationError, match=message):
        _invocation(tmp_path / "ledger.json", **updates)


def test_invocation_candidate_refs_are_bounded(tmp_path):
    candidate_refs = [
        f"sha256:{ordinal:064x}"
        for ordinal in range(1, MAX_ASSESS_EVIDENCE_CANDIDATES + 2)
    ]
    with pytest.raises(ValidationError, match="at most 100"):
        _invocation(tmp_path / "ledger.json", candidate_refs=candidate_refs)
    canonical_span_refs = [
        f"R{ordinal:04d}"
        for ordinal in range(1, MAX_ASSESS_EVIDENCE_CANDIDATES + 2)
    ]
    with pytest.raises(ValidationError, match="at most 100"):
        _invocation(
            tmp_path / "ledger.json",
            canonical_span_refs=canonical_span_refs,
        )


@pytest.mark.parametrize(
    "decision",
    [
        {
            "candidate_ref": CANDIDATE_A,
            "status": "assessed",
        },
        {
            "candidate_ref": CANDIDATE_A,
            "status": "assessed",
            "canonical_span_ref": "R0001",
            "evidence": _evidence().model_dump(mode="json"),
        },
        {
            "candidate_ref": CANDIDATE_A,
            "status": "duplicate",
        },
        {
            "candidate_ref": CANDIDATE_A,
            "status": "duplicate",
            "canonical_span_ref": "R0001",
            "evidence": _evidence().model_dump(mode="json"),
        },
        {
            "candidate_ref": CANDIDATE_A,
            "status": "redundant",
        },
        {
            "candidate_ref": CANDIDATE_A,
            "status": "redundant",
            "reason": "Already covered.",
            "evidence": _evidence().model_dump(mode="json"),
        },
        {
            "candidate_ref": CANDIDATE_A,
            "status": "excluded_by_budget",
            "reason": "Not selectable by this task.",
        },
        {
            "candidate_ref": CANDIDATE_A,
            "status": "invalid_locator",
            "reason": "Not selectable by this task.",
        },
    ],
    ids=[
        "assessed-without-evidence",
        "assessed-with-canonical",
        "duplicate-without-canonical",
        "duplicate-with-evidence",
        "redundant-without-reason-or-canonical",
        "redundant-with-evidence",
        "excluded-by-budget",
        "invalid-locator",
    ],
)
def test_decision_rejects_invalid_status_or_coupling(decision):
    with pytest.raises(ValidationError):
        EvidenceCandidateDecisionProposal.model_validate(decision)


def test_all_three_permitted_dispositions_have_valid_bounded_forms():
    bundle = AssessEvidenceProposalBundle(
        decisions=[
            EvidenceCandidateDecisionProposal(
                candidate_ref=CANDIDATE_A,
                status="assessed",
                evidence=_evidence(),
            ),
            EvidenceCandidateDecisionProposal(
                candidate_ref=CANDIDATE_B,
                status="duplicate",
                canonical_span_ref="R0001",
            ),
            EvidenceCandidateDecisionProposal(
                candidate_ref="sha256:" + "3" * 64,
                status="redundant",
                reason="The result adds no new information.",
            ),
        ]
    )
    assert [item.status.value for item in bundle.decisions] == [
        "assessed",
        "duplicate",
        "redundant",
    ]
    status_schema = EvidenceCandidateDecisionProposal.model_json_schema()[
        "properties"
    ]["status"]
    assert status_schema["enum"] == ["assessed", "duplicate", "redundant"]


def test_proposal_bundle_is_nonempty_bounded_and_unique():
    with pytest.raises(ValidationError, match="at least 1"):
        AssessEvidenceProposalBundle(decisions=[])
    duplicate = EvidenceCandidateDecisionProposal(
        candidate_ref=CANDIDATE_A,
        status="redundant",
        reason="Synthetic redundant result.",
    )
    with pytest.raises(ValidationError, match="candidate_refs must be unique"):
        AssessEvidenceProposalBundle(decisions=[duplicate, duplicate])
    too_many = [
        EvidenceCandidateDecisionProposal(
            candidate_ref=f"sha256:{ordinal:064x}",
            status="redundant",
            reason="Synthetic redundant result.",
        )
        for ordinal in range(1, MAX_ASSESS_EVIDENCE_CANDIDATES + 2)
    ]
    with pytest.raises(ValidationError, match="at most 100"):
        AssessEvidenceProposalBundle(decisions=too_many)


@pytest.mark.parametrize(
    "updates",
    [
        {"evidence_summary": "x" * (MAX_EVIDENCE_PROPOSAL_TEXT_CHARS + 1)},
        {
            "assessment_note": "x"
            * (MAX_EVIDENCE_PROPOSAL_TEXT_CHARS + 1)
        },
        {
            "quality": {
                "directness": "direct",
                "methodological_relevance": "high",
                "strength": "high",
                "assessability": "full",
                "limitations": [
                    "bounded"
                    for _ in range(MAX_EVIDENCE_PROPOSAL_LIMITATIONS + 1)
                ],
            }
        },
        {
            "quality": {
                "directness": "direct",
                "methodological_relevance": "high",
                "strength": "high",
                "assessability": "full",
                "limitations": [
                    "x" * (MAX_EVIDENCE_PROPOSAL_LIMITATION_CHARS + 1)
                ],
            }
        },
    ],
    ids=["summary", "note", "limitation-count", "limitation-text"],
)
def test_coupled_evidence_text_and_diagnostics_are_bounded(updates):
    with pytest.raises(ValidationError):
        _evidence(**updates)


def test_spec_is_object_rooted_and_executable_only_with_coupled_handler():
    spec = TASK_SPECS[TaskType.ASSESS_EVIDENCE]
    assert spec.version == "2"
    assert spec.prompt_version == "2"
    assert spec.invocation_model is AssessEvidenceInvocation
    assert spec.engine_input_model is AssessEvidenceInput
    assert spec.proposal_model is AssessEvidenceProposalBundle
    assert spec.proposal_model.model_json_schema()["type"] == "object"
    assert spec.promotion_handler == "promote_evidence"
    assert TaskType.ASSESS_EVIDENCE not in executable_task_types()
    with pytest.raises(TaskSpecNotExecutableError, match="promote_evidence"):
        validate_task_spec_executable(spec)
    validate_task_spec_executable(
        spec, additional_promotion_handlers=frozenset({"promote_evidence"})
    )


def test_task_bundle_has_exact_dependencies_and_sanitized_ledger_resource(
    tmp_path, bundle_factory
):
    project = tmp_path / "project"
    ledger_path = project / "retrieval" / "ledger.json"
    ledger_path.parent.mkdir(parents=True)
    ledger_bytes = b'{"synthetic":"bounded retrieval ledger"}\n'
    ledger_path.write_bytes(ledger_bytes)
    invocation = _invocation(
        ledger_path,
        candidate_refs=[CANDIDATE_B, CANDIDATE_A],
        canonical_span_refs=["R0001"],
    )
    snapshot = RepositorySnapshot.model_validate(bundle_factory())
    spec = TASK_SPECS[TaskType.ASSESS_EVIDENCE]
    dependency_keys = spec.dependency_builder(invocation, snapshot)
    assert dependency_keys == (
        "CandidateClaim:C0001",
        "RetrievalQuery:Q-C0001-SUP-01",
        "RetrievedSpan:R0001",
        "RetrievalDisposition:R0001",
        "Paper:P0001",
    )
    dependencies = {
        key: snapshot.dependency_hash(key) for key in dependency_keys
    }
    context = ProjectContext(project_root=project)
    requests = spec.resource_builder(invocation, snapshot, context)
    assert len(requests) == 1
    request = requests[0]
    assert request.resource_id == "RES0001"
    assert request.logical_name == "retrieval_ledger.json"
    assert request.media_type == "application/json"
    assert request.source_path == ledger_path

    task_dir, manifest, provenance = TaskWorkspace(project).create(
        spec=spec,
        invocation=invocation,
        base_generation=0,
        dependencies=dependencies,
        resource_requests=requests,
        snapshot=snapshot,
        context=context,
    )
    assert tuple(manifest.dependencies) == dependency_keys
    assert tuple(provenance.dependencies) == dependency_keys
    assert len(provenance.resources) == 1
    assert provenance.resources[0].resource_id == "RES0001"
    copied_ledger = (
        task_dir / "bundle" / "input" / "resources" / "RES0001" / "content.json"
    )
    assert copied_ledger.read_bytes() == ledger_bytes

    engine_input_path = task_dir / "bundle" / "input" / "input.json"
    engine_input = json.loads(engine_input_path.read_text(encoding="utf-8"))
    assert engine_input == {
        "source_generation": 0,
        "claim_id": "C0001",
        "query_id": "Q-C0001-SUP-01",
        "candidate_refs": [CANDIDATE_B, CANDIDATE_A],
        "canonical_span_refs": ["R0001"],
        "retrieval_ledger_resource_id": "RES0001",
    }
    assert str(ledger_path) not in engine_input_path.read_text(encoding="utf-8")
    private_path = str(ledger_path).encode("utf-8")
    for path in (task_dir / "bundle").rglob("*"):
        if path.is_file():
            assert private_path not in path.read_bytes(), path
    visible_manifest = json.loads(
        (task_dir / "bundle" / "bundle_manifest.json").read_text(encoding="utf-8")
    )
    assert [item["key"] for item in visible_manifest["dependencies"]] == list(
        dependency_keys
    )
    canonical_index = snapshot.object_index()
    dependency_root = task_dir / "bundle" / "input" / "dependencies"
    for key in dependency_keys:
        dependency_path = dependency_root / f"{key.replace(':', '__')}.json"
        assert json.loads(dependency_path.read_text(encoding="utf-8")) == json.loads(
            canonical_index[key].model_dump_json()
        )
    assert visible_manifest["resources"] == [
        {
            "resource_id": "RES0001",
            "logical_name": "retrieval_ledger.json",
            "path": "input/resources/RES0001/content.json",
            "media_type": "application/json",
            "size_bytes": len(ledger_bytes),
            "sha256": provenance.resources[0].snapshot_hash,
        }
    ]


def test_engine_input_builder_requires_the_fixed_json_ledger(tmp_path):
    invocation = _invocation(tmp_path / "ledger.json")
    spec = TASK_SPECS[TaskType.ASSESS_EVIDENCE]
    with pytest.raises(ValueError, match="RES0001 JSON retrieval ledger"):
        spec.engine_input_builder(invocation, ())
    wrong = SnapshottedResource(
        resource_id="RES0002",
        logical_name="retrieval_ledger.json",
        snapshot_relative_path=Path("input/resources/RES0002/content.json"),
        media_type="application/json",
        size_bytes=2,
        content_hash="sha256:" + "a" * 64,
    )
    with pytest.raises(ValueError, match="RES0001 JSON retrieval ledger"):
        spec.engine_input_builder(invocation, (wrong,))


class _NoOpAssessEvidenceAdapter:
    task_type = TaskType.ASSESS_EVIDENCE
    promotion_handler = "promote_evidence"
    promotion_fingerprint = "sha256:" + "9" * 64
    rebuild_on_stale = False

    def validate_pre_execution(self, **_kwargs) -> None:
        return None

    def validate_proposal(
        self, *, proposal, invocation, dependency_keys, **_kwargs
    ) -> None:
        assert isinstance(proposal, AssessEvidenceProposalBundle)
        assert isinstance(invocation, AssessEvidenceInvocation)
        if {item.candidate_ref for item in proposal.decisions} != set(
            invocation.candidate_refs
        ):
            raise CoupledProposalValidationError(
                "decisions must cover exactly the invocation candidate refs"
            )
        if any(
            item.canonical_span_ref is not None
            and item.canonical_span_ref not in invocation.canonical_span_refs
            for item in proposal.decisions
        ):
            raise CoupledProposalValidationError(
                "canonical span ref is outside the invocation allowlist"
            )
        assert tuple(dependency_keys) == (
            "CandidateClaim:C0001",
            "RetrievalQuery:Q-C0001-SUP-01",
        )

    def prepare_commit(self, **_kwargs) -> CoupledCommitPlan:
        def promote(snapshot, registry):
            return PromotionPayload(snapshot, registry, {})

        return CoupledCommitPlan(promotion=promote)

    def verify_receipt_reuse(self, **_kwargs) -> tuple[bool, str | None]:
        return True, None


def test_runtime_requires_and_accepts_the_matching_coupled_adapter(
    tmp_path, bundle_factory
):
    project = tmp_path / "project"
    ledger_path = project / "retrieval" / "ledger.json"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text('{"synthetic":"ledger"}\n', encoding="utf-8")
    invocation = _invocation(ledger_path)
    proposal = _assessed_bundle()
    runtime = ProjectRuntime.create(
        project,
        project_name="assess-evidence-contract",
        initial_snapshot=RepositorySnapshot.model_validate(bundle_factory()),
    )
    blocked_engine = MockEngine(
        [MockResponse(proposal=proposal.model_dump(mode="json"))]
    )
    blocked = runtime.run(
        TaskType.ASSESS_EVIDENCE,
        invocation,
        engines=[blocked_engine],
    )
    assert blocked.outcome is AttemptOutcome.TASK_TYPE_NOT_IMPLEMENTED
    assert blocked_engine.calls == 0

    engine = MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])
    result = runtime.run(
        TaskType.ASSESS_EVIDENCE,
        invocation,
        engines=[engine],
        promotion_adapter=_NoOpAssessEvidenceAdapter(),
    )
    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert result.commit_performed
    assert result.generation == 1
    assert engine.calls == 1


def test_coupled_adapter_rejects_unmanifested_canonical_span_target(
    tmp_path, bundle_factory
):
    project = tmp_path / "project"
    ledger_path = project / "retrieval" / "ledger.json"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text('{"synthetic":"ledger"}\n', encoding="utf-8")
    invocation = _invocation(ledger_path, canonical_span_refs=[])
    proposal = AssessEvidenceProposalBundle(
        decisions=[
            EvidenceCandidateDecisionProposal(
                candidate_ref=CANDIDATE_A,
                status="duplicate",
                canonical_span_ref="R0001",
            )
        ]
    )
    runtime = ProjectRuntime.create(
        project,
        project_name="assess-evidence-ownership",
        initial_snapshot=RepositorySnapshot.model_validate(bundle_factory()),
    )
    engine = MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])

    result = runtime.run(
        TaskType.ASSESS_EVIDENCE,
        invocation,
        engines=[engine],
        promotion_adapter=_NoOpAssessEvidenceAdapter(),
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert not result.commit_performed
    assert result.generation is None
    assert runtime.store.current_generation() == 0
    assert "outside the invocation allowlist" in (
        result.attempt_records[0].validation_errors[0]
    )
    assert engine.calls == 1


def test_public_runtime_exports_assess_evidence_contract():
    expected = {
        "AssessEvidenceInput": AssessEvidenceInput,
        "AssessEvidenceInvocation": AssessEvidenceInvocation,
        "AssessEvidenceProposalBundle": AssessEvidenceProposalBundle,
        "EvidenceAssessmentProposal": EvidenceAssessmentProposal,
        "EvidenceCandidateDecisionProposal": EvidenceCandidateDecisionProposal,
        "CoupledProposalValidationError": CoupledProposalValidationError,
    }
    for name, value in expected.items():
        assert name in public_runtime.__all__
        assert getattr(public_runtime, name) is value
