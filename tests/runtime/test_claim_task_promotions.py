from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from vibereview.models import ClaimAssessment, ComponentRelations
from vibereview.runtime import (
    AggregatePaperEvidenceInvocation,
    AssessClaimInvocation,
    AttemptOutcome,
    ClaimAssessmentProposal,
    MockEngine,
    MockResponse,
    ProjectRuntime,
    RepositorySnapshot,
    RuntimeConfig,
    TaskType,
    ValidateFinalClaimInvocation,
)
from vibereview.runtime.dto import (
    MAX_CLAIM_TASK_ITEMS,
    MAX_CLAIM_TASK_TEXT_CHARS,
    ClaimPaperEvidenceProposal,
    FinalClaimValidationProposal,
    FinalPaperRelationProposal,
)
from vibereview.runtime.hashing import hash_json
from vibereview.runtime.repository import APPLIED_TASKS_FILE


_DOWNSTREAM_COLLECTIONS = (
    "claim_assessments",
    "final_claim_validations",
    "claim_packets",
    "proposition_records",
    "semantic_audits",
    "rendered_sentences",
    "rendered_sentence_audits",
)


def test_claim_task_inputs_and_text_are_schema_bounded() -> None:
    with pytest.raises(ValidationError, match="at most 500"):
        AggregatePaperEvidenceInvocation(
            claim_id="C0001",
            paper_id="P0001",
            evidence_ids=[
                f"E{ordinal:04d}"
                for ordinal in range(1, MAX_CLAIM_TASK_ITEMS + 2)
            ],
        )

    with pytest.raises(ValidationError, match="at most 16384 characters"):
        ClaimAssessmentProposal(
            claim_ref="C0001",
            aggregate_strength="weak",
            evidence_sufficiency="insufficient",
            decision="REJECT",
            rejection_basis="insufficient_evidence",
            support_summary="",
            contradiction_summary="",
            qualification_summary="",
            reason="x" * (MAX_CLAIM_TASK_TEXT_CHARS + 1),
        )


def _snapshot(bundle: dict[str, list[object]]) -> RepositorySnapshot:
    value = RepositorySnapshot.model_validate(bundle)
    value.validate_repository()
    return value


def _without(bundle: dict[str, list[object]], *collections: str) -> None:
    for collection in collections:
        bundle[collection] = []


def _aggregate_ready_snapshot(bundle_factory) -> RepositorySnapshot:
    bundle = bundle_factory()
    _without(bundle, "claim_paper_evidence", *_DOWNSTREAM_COLLECTIONS)
    return _snapshot(bundle)


def _two_evidence_one_paper_snapshot(bundle_factory) -> RepositorySnapshot:
    bundle = bundle_factory()
    span = bundle["retrieved_spans"][0]
    locator = span.locator.model_copy(
        update={
            "start_offset": 30,
            "end_offset": 46,
            "source_span_hash": "sha256:" + "d" * 64,
        }
    )
    second_span = span.model_copy(
        update={
            "span_id": "R0002",
            "locator": locator,
            "source_text": "Stress also fell",
        }
    )
    second_disposition = bundle["retrieval_dispositions"][0].model_copy(
        update={"span_id": "R0002"}
    )
    second_evidence = bundle["evidence_records"][0].model_copy(
        update={
            "evidence_id": "E0002",
            "retrieved_span_id": "R0002",
            "evidence_summary": "A second synthetic measurement also declined.",
        }
    )
    bundle["retrieved_spans"].append(second_span)
    bundle["retrieval_dispositions"].append(second_disposition)
    bundle["evidence_records"].append(second_evidence)
    _without(bundle, "claim_paper_evidence", *_DOWNSTREAM_COLLECTIONS)
    return _snapshot(bundle)


def _two_cpe_snapshot(bundle_factory) -> RepositorySnapshot:
    bundle = bundle_factory()
    paper = bundle["papers"][0]
    second_paper = paper.model_copy(
        update={
            "paper_id": "P0002",
            "title": "Independent synthetic replication",
            "doi": "10.1234/SECOND",
            "identity_keys": [
                "doi:10.1234/second",
                "bib:researcher 2026 replication",
            ],
            "raw_md_path": "papers/P0002/raw.md",
            "source_hash": "sha256:" + "d" * 64,
            "raw_md_hash": "sha256:" + "e" * 64,
        }
    )
    span = bundle["retrieved_spans"][0]
    second_span = span.model_copy(
        update={
            "span_id": "R0002",
            "paper_id": "P0002",
            "locator": span.locator.model_copy(
                update={
                    "raw_md_path": "papers/P0002/raw.md",
                    "start_offset": 30,
                    "end_offset": 46,
                    "source_span_hash": "sha256:" + "f" * 64,
                }
            ),
            "source_text": "Stress also fell",
        }
    )
    second_disposition = bundle["retrieval_dispositions"][0].model_copy(
        update={"span_id": "R0002"}
    )
    second_evidence = bundle["evidence_records"][0].model_copy(
        update={
            "evidence_id": "E0002",
            "retrieved_span_id": "R0002",
            "paper_id": "P0002",
            "evidence_summary": "The independent synthetic result also declined.",
        }
    )
    second_cpe = bundle["claim_paper_evidence"][0].model_copy(
        update={
            "claim_paper_evidence_id": "CPE-C0001-P0002",
            "paper_id": "P0002",
            "evidence_ids": ["E0002"],
            "component_relations": ComponentRelations(supports=["E0002"]),
            "assessment_note": "The second publication supports the candidate.",
        }
    )
    bundle["papers"].append(second_paper)
    bundle["retrieved_spans"].append(second_span)
    bundle["retrieval_dispositions"].append(second_disposition)
    bundle["evidence_records"].append(second_evidence)
    bundle["claim_paper_evidence"].append(second_cpe)
    _without(bundle, *_DOWNSTREAM_COLLECTIONS)
    return _snapshot(bundle)


def _runtime(
    tmp_path, snapshot: RepositorySnapshot, *, receipts: bool = True
) -> ProjectRuntime:
    return ProjectRuntime.create(
        tmp_path / "project",
        project_name="synthetic-claim-task-promotions",
        initial_snapshot=snapshot,
        config=RuntimeConfig(enable_receipts=receipts),
    )


def _cpe_proposal(*evidence_refs: str) -> ClaimPaperEvidenceProposal:
    return ClaimPaperEvidenceProposal(
        claim_ref="C0001",
        paper_ref="P0001",
        evidence_refs=list(evidence_refs),
        relation_to_candidate="supports",
        component_relations=ComponentRelations(supports=list(evidence_refs)),
        strength="high",
        within_paper_consistency="consistent",
        assessment_note="All synthetic within-paper evidence supports the claim.",
    )


def _assessment_proposal() -> ClaimAssessmentProposal:
    return ClaimAssessmentProposal(
        claim_ref="C0001",
        aggregate_strength="high",
        evidence_sufficiency="sufficient",
        decision="RETAIN",
        rejection_basis=None,
        support_summary="Two synthetic publications support the claim.",
        contradiction_summary="None in the synthetic fixture.",
        qualification_summary="Limited to the tested process window.",
        reason="The complete synthetic CPE set is sufficient.",
    )


def _proposal_from_assessment(assessment: ClaimAssessment) -> ClaimAssessmentProposal:
    return ClaimAssessmentProposal(
        claim_ref=assessment.claim_id,
        aggregate_strength=assessment.aggregate_strength,
        evidence_sufficiency=assessment.evidence_sufficiency,
        decision=assessment.decision,
        rejection_basis=assessment.rejection_basis,
        support_summary=assessment.support_summary,
        contradiction_summary=assessment.contradiction_summary,
        qualification_summary=assessment.qualification_summary,
        reason=assessment.reason,
    )


def _final_proposal(
    final_claim: str,
    *,
    status: str = "VALID",
    relation_ids: tuple[str, ...] = (
        "CPE-C0001-P0001",
        "CPE-C0001-P0002",
    ),
) -> FinalClaimValidationProposal:
    papers = {
        "CPE-C0001-P0001": "P0001",
        "CPE-C0001-P0002": "P0002",
    }
    return FinalClaimValidationProposal(
        claim_ref="C0001",
        final_claim=final_claim,
        status=status,
        paper_relations=[
            FinalPaperRelationProposal(
                claim_paper_evidence_ref=cpe_id,
                paper_ref=papers[cpe_id],
                relation_to_final_claim="supports",
            )
            for cpe_id in relation_ids
        ],
        scope_check="pass",
        certainty_check="pass",
        causal_language_check="pass",
        numerical_claim_check="not_applicable",
        notes="Checked against the complete synthetic CPE set.",
    )


def _run_final(
    runtime: ProjectRuntime,
    invocation: ValidateFinalClaimInvocation,
    proposal: FinalClaimValidationProposal,
    *,
    name: str = "primary",
):
    engine = MockEngine(
        [MockResponse(proposal=proposal.model_dump(mode="json"))], name=name
    )
    result = runtime.run(
        TaskType.VALIDATE_FINAL_CLAIM, invocation, engines=[engine]
    )
    return result, engine


def test_aggregate_promotes_deterministic_complete_cpe(tmp_path, bundle_factory):
    runtime = _runtime(tmp_path, _aggregate_ready_snapshot(bundle_factory))
    proposal = _cpe_proposal("E0001")

    result = runtime.run(
        TaskType.AGGREGATE_PAPER_EVIDENCE,
        AggregatePaperEvidenceInvocation(
            claim_id="C0001", paper_id="P0001", evidence_ids=["E0001"]
        ),
        engines=[MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])],
    )

    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert result.allocated_ids == {
        "claim_paper_evidence": "CPE-C0001-P0001"
    }
    assert result.transition is not None and result.transition.canonicalized
    _, snapshot, _ = runtime.store.load_current()
    assert len(snapshot.claim_paper_evidence) == 1
    aggregate = snapshot.claim_paper_evidence[0]
    assert aggregate.claim_paper_evidence_id == "CPE-C0001-P0001"
    assert aggregate.evidence_ids == ["E0001"]
    snapshot.validate_repository()


def test_aggregate_invocation_requires_complete_evidence_set_before_engine(
    tmp_path, bundle_factory
):
    runtime = _runtime(tmp_path, _two_evidence_one_paper_snapshot(bundle_factory))
    engine = MockEngine([], name="unused")

    with pytest.raises(ValueError, match="exactly equal all current EvidenceRecord"):
        runtime.run(
            TaskType.AGGREGATE_PAPER_EVIDENCE,
            AggregatePaperEvidenceInvocation(
                claim_id="C0001", paper_id="P0001", evidence_ids=["E0001"]
            ),
            engines=[engine],
        )

    assert engine.calls == 0
    assert runtime.store.current_generation() == 0


def test_aggregate_proposal_requires_complete_owned_evidence_set(
    tmp_path, bundle_factory
):
    runtime = _runtime(tmp_path, _two_evidence_one_paper_snapshot(bundle_factory))
    proposal = _cpe_proposal("E0001")

    result = runtime.run(
        TaskType.AGGREGATE_PAPER_EVIDENCE,
        AggregatePaperEvidenceInvocation(
            claim_id="C0001",
            paper_id="P0001",
            evidence_ids=["E0001", "E0002"],
        ),
        engines=[MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])],
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert "exactly equal all current EvidenceRecord" in (
        result.attempt_records[0].validation_errors[0]
    )
    assert runtime.store.current_generation() == 0


def test_aggregate_proposal_cannot_cross_task_paper_scope(
    tmp_path, bundle_factory
):
    snapshot = _two_cpe_snapshot(bundle_factory).model_copy(
        update={"claim_paper_evidence": ()}
    )
    snapshot.validate_repository()
    runtime = _runtime(tmp_path, snapshot)
    proposal = _cpe_proposal("E0002").model_copy(
        update={
            "paper_ref": "P0002",
            "component_relations": ComponentRelations(supports=["E0002"]),
        }
    )

    result = runtime.run(
        TaskType.AGGREGATE_PAPER_EVIDENCE,
        AggregatePaperEvidenceInvocation(
            claim_id="C0001", paper_id="P0001", evidence_ids=["E0001"]
        ),
        engines=[MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])],
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert "paper_ref P0002 is outside the task dependency scope" in (
        result.attempt_records[0].validation_errors[0]
    )
    assert runtime.store.current_generation() == 0


def test_aggregate_component_relations_must_match_evidence_records(
    tmp_path, bundle_factory
):
    runtime = _runtime(tmp_path, _aggregate_ready_snapshot(bundle_factory))
    base = _cpe_proposal("E0001")
    proposal = ClaimPaperEvidenceProposal.model_validate(
        {
            **base.model_dump(mode="json"),
            "relation_to_candidate": "contextual",
            "component_relations": ComponentRelations(
                contextual=["E0001"]
            ).model_dump(mode="json"),
        }
    )

    result = runtime.run(
        TaskType.AGGREGATE_PAPER_EVIDENCE,
        AggregatePaperEvidenceInvocation(
            claim_id="C0001", paper_id="P0001", evidence_ids=["E0001"]
        ),
        engines=[MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])],
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert "EvidenceRecord relation is supports" in (
        result.attempt_records[0].validation_errors[0]
    )
    assert runtime.store.current_generation() == 0


def test_aggregate_change_is_blocked_after_claim_assessment(
    tmp_path, bundle_factory
):
    snapshot = _snapshot(bundle_factory())
    runtime = _runtime(tmp_path, snapshot)
    proposal = _cpe_proposal("E0001").model_copy(
        update={"assessment_note": "A changed downstream-sensitive summary."}
    )

    result = runtime.run(
        TaskType.AGGREGATE_PAPER_EVIDENCE,
        AggregatePaperEvidenceInvocation(
            claim_id="C0001", paper_id="P0001", evidence_ids=["E0001"]
        ),
        engines=[MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])],
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert "after ClaimAssessment exists" in (
        result.attempt_records[0].validation_errors[0]
    )
    assert runtime.store.current_generation() == 0


def test_assess_claim_invocation_requires_every_current_cpe(
    tmp_path, bundle_factory
):
    snapshot = _two_cpe_snapshot(bundle_factory)
    runtime = _runtime(tmp_path, snapshot)
    engine = MockEngine([], name="unused")

    with pytest.raises(ValueError, match="exactly equal all current CPE"):
        runtime.run(
            TaskType.ASSESS_CLAIM,
            AssessClaimInvocation(
                claim_id="C0001",
                claim_paper_evidence_ids=["CPE-C0001-P0001"],
            ),
            engines=[engine],
        )

    assert engine.calls == 0
    complete = AssessClaimInvocation(
        claim_id="C0001",
        claim_paper_evidence_ids=[
            "CPE-C0001-P0002",
            "CPE-C0001-P0001",
        ],
    )
    result = runtime.run(
        TaskType.ASSESS_CLAIM,
        complete,
        engines=[
            MockEngine(
                [MockResponse(proposal=_assessment_proposal().model_dump(mode="json"))]
            )
        ],
    )
    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT


def test_claim_assessment_change_is_blocked_while_descendants_exist(
    tmp_path, bundle_factory
):
    snapshot = _snapshot(bundle_factory())
    runtime = _runtime(tmp_path, snapshot)
    proposal = _proposal_from_assessment(snapshot.claim_assessments[0]).model_copy(
        update={"reason": "A changed downstream-sensitive assessment."}
    )
    engine = MockEngine(
        [MockResponse(proposal=proposal.model_dump(mode="json"))]
    )

    result = runtime.run(
        TaskType.ASSESS_CLAIM,
        AssessClaimInvocation(
            claim_id="C0001",
            claim_paper_evidence_ids=["CPE-C0001-P0001"],
        ),
        engines=[engine],
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert "cannot change ClaimAssessment" in (
        result.attempt_records[0].validation_errors[0]
    )
    assert engine.calls == 1
    assert not result.commit_performed
    assert runtime.store.current_generation() == 0


def test_identical_claim_assessment_remains_valid_with_descendants(
    tmp_path, bundle_factory
):
    snapshot = _snapshot(bundle_factory())
    runtime = _runtime(tmp_path, snapshot)
    proposal = _proposal_from_assessment(snapshot.claim_assessments[0])

    result = runtime.run(
        TaskType.ASSESS_CLAIM,
        AssessClaimInvocation(
            claim_id="C0001",
            claim_paper_evidence_ids=["CPE-C0001-P0001"],
        ),
        engines=[
            MockEngine(
                [MockResponse(proposal=proposal.model_dump(mode="json"))]
            )
        ],
    )

    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert result.commit_performed
    _, current, _ = runtime.store.load_current()
    assert current.claim_assessments == snapshot.claim_assessments
    assert current.final_claim_validations == snapshot.final_claim_validations
    assert current.claim_packets == snapshot.claim_packets
    assert current.proposition_records == snapshot.proposition_records
    current.validate_repository()


def test_valid_final_claim_atomically_creates_validation_and_packet(
    tmp_path, bundle_factory
):
    snapshot = _two_cpe_snapshot(bundle_factory).model_copy(
        update={"claim_assessments": (_assessment_proposal_to_model(),)}
    )
    snapshot.validate_repository()
    runtime = _runtime(tmp_path, snapshot)
    wording = "Synthetic preheating reduced stress in the tested process window."
    invocation = ValidateFinalClaimInvocation(
        claim_id="C0001", proposed_final_claim=wording
    )

    result, _ = _run_final(runtime, invocation, _final_proposal(wording))

    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert result.transition is not None
    assert result.transition.scientific_disposition == "VALID"
    assert result.transition.downstream_eligible
    _, current, _ = runtime.store.load_current()
    assert len(current.final_claim_validations) == 1
    assert len(current.claim_packets) == 1
    packet = current.claim_packets[0]
    assert packet.theme_id == current.candidate_claims[0].theme_id
    assert packet.candidate_claim == current.candidate_claims[0].candidate_claim
    assert packet.final_claim == wording
    assert packet.aggregate_strength == current.claim_assessments[0].aggregate_strength
    assert packet.claim_paper_evidence_ids == [
        "CPE-C0001-P0001",
        "CPE-C0001-P0002",
    ]
    receipt = runtime.store.load_receipts(result.generation)[0]
    assert {item.qualified_id for item in receipt.canonical_objects} == {
        "ClaimPacket:C0001",
        "FinalClaimValidation:C0001",
    }
    current.validate_repository()


def _assessment_proposal_to_model() -> ClaimAssessment:
    proposal = _assessment_proposal()
    return ClaimAssessment(
        claim_id=proposal.claim_ref,
        aggregate_strength=proposal.aggregate_strength,
        evidence_sufficiency=proposal.evidence_sufficiency,
        decision=proposal.decision,
        rejection_basis=proposal.rejection_basis,
        support_summary=proposal.support_summary,
        contradiction_summary=proposal.contradiction_summary,
        qualification_summary=proposal.qualification_summary,
        reason=proposal.reason,
    )


@pytest.mark.parametrize(
    ("status", "human_review"),
    [("REVISE_AGAIN", False), ("REJECT", False), ("UNCLEAR", True)],
)
def test_negative_final_status_is_canonical_without_packet_or_fallback(
    tmp_path, bundle_factory, status, human_review
):
    snapshot = _two_cpe_snapshot(bundle_factory).model_copy(
        update={"claim_assessments": (_assessment_proposal_to_model(),)}
    )
    runtime = _runtime(tmp_path, snapshot)
    wording = "A bounded synthetic final claim."
    proposal = _final_proposal(wording, status=status)
    primary = MockEngine(
        [MockResponse(proposal=proposal.model_dump(mode="json"))], name="primary"
    )
    fallback = MockEngine([], name="fallback")

    result = runtime.run(
        TaskType.VALIDATE_FINAL_CLAIM,
        ValidateFinalClaimInvocation(
            claim_id="C0001", proposed_final_claim=wording
        ),
        engines=[primary, fallback],
    )

    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert primary.calls == 1
    assert fallback.calls == 0
    assert result.transition is not None
    assert result.transition.scientific_disposition == status
    assert not result.transition.downstream_eligible
    assert result.transition.human_review_required is human_review
    _, current, _ = runtime.store.load_current()
    assert current.final_claim_validations[0].status.value == status
    assert current.claim_packets == ()
    current.validate_repository()


def test_final_validation_rejects_incomplete_cpe_continuity(
    tmp_path, bundle_factory
):
    snapshot = _two_cpe_snapshot(bundle_factory).model_copy(
        update={"claim_assessments": (_assessment_proposal_to_model(),)}
    )
    runtime = _runtime(tmp_path, snapshot)
    wording = "A bounded synthetic final claim."
    result, _ = _run_final(
        runtime,
        ValidateFinalClaimInvocation(
            claim_id="C0001", proposed_final_claim=wording
        ),
        _final_proposal(
            wording, relation_ids=("CPE-C0001-P0001",)
        ),
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert "exactly cover every current CPE" in (
        result.attempt_records[0].validation_errors[0]
    )
    assert runtime.store.current_generation() == 0


def test_final_validation_rejects_relation_paper_mismatch(
    tmp_path, bundle_factory
):
    snapshot = _two_cpe_snapshot(bundle_factory).model_copy(
        update={"claim_assessments": (_assessment_proposal_to_model(),)}
    )
    runtime = _runtime(tmp_path, snapshot)
    wording = "A bounded synthetic final claim."
    proposal = _final_proposal(wording)
    first_relation = proposal.paper_relations[0].model_copy(
        update={"paper_ref": "P0002"}
    )
    proposal = proposal.model_copy(
        update={"paper_relations": [first_relation, proposal.paper_relations[1]]}
    )

    result, _ = _run_final(
        runtime,
        ValidateFinalClaimInvocation(
            claim_id="C0001", proposed_final_claim=wording
        ),
        proposal,
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert "does not match CPE" in result.attempt_records[0].validation_errors[0]
    assert runtime.store.current_generation() == 0


def test_final_change_is_blocked_while_proposition_descendants_exist(
    tmp_path, bundle_factory
):
    snapshot = _snapshot(bundle_factory())
    runtime = _runtime(tmp_path, snapshot)
    wording = "A changed synthetic final claim."

    result, _ = _run_final(
        runtime,
        ValidateFinalClaimInvocation(
            claim_id="C0001", proposed_final_claim=wording
        ),
        _final_proposal(
            wording, relation_ids=("CPE-C0001-P0001",)
        ),
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert "proposition descendants exist" in (
        result.attempt_records[0].validation_errors[0]
    )
    assert runtime.store.current_generation() == 0


def test_rejected_assessment_cannot_yield_valid_final_claim(
    tmp_path, bundle_factory
):
    snapshot = _two_cpe_snapshot(bundle_factory)
    rejected = ClaimAssessment.model_validate(
        {
            **_assessment_proposal_to_model().model_dump(mode="json"),
            "decision": "REJECT",
            "rejection_basis": "insufficient_evidence",
        }
    )
    snapshot = snapshot.model_copy(update={"claim_assessments": (rejected,)})
    snapshot.validate_repository()
    runtime = _runtime(tmp_path, snapshot)
    wording = "A bounded synthetic final claim."
    result, _ = _run_final(
        runtime,
        ValidateFinalClaimInvocation(
            claim_id="C0001", proposed_final_claim=wording
        ),
        _final_proposal(wording),
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert "rejected ClaimAssessment" in result.attempt_records[0].validation_errors[0]
    assert runtime.store.current_generation() == 0


def test_final_wording_mismatch_is_rejected_on_live_path(tmp_path, bundle_factory):
    snapshot = _two_cpe_snapshot(bundle_factory).model_copy(
        update={"claim_assessments": (_assessment_proposal_to_model(),)}
    )
    runtime = _runtime(tmp_path, snapshot)
    invocation = ValidateFinalClaimInvocation(
        claim_id="C0001", proposed_final_claim="Authoritative wording."
    )

    result, engine = _run_final(
        runtime, invocation, _final_proposal("Different wording.")
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert engine.calls == 1
    assert "exactly equal" in result.attempt_records[0].validation_errors[0]
    assert runtime.store.current_generation() == 0


def test_final_wording_mismatch_is_rejected_on_cache_path(tmp_path, bundle_factory):
    snapshot = _two_cpe_snapshot(bundle_factory).model_copy(
        update={"claim_assessments": (_assessment_proposal_to_model(),)}
    )
    runtime = _runtime(tmp_path, snapshot, receipts=False)
    wording = "Authoritative wording."
    invocation = ValidateFinalClaimInvocation(
        claim_id="C0001", proposed_final_claim=wording
    )
    first, _ = _run_final(runtime, invocation, _final_proposal(wording), name="stable")
    assert first.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    cache_proposal = next((runtime.project_root / "work" / "cache").glob("*/proposal.json"))
    cache_proposal.write_text(
        json.dumps(_final_proposal("Different wording.").model_dump(mode="json")),
        encoding="utf-8",
    )
    engine = MockEngine(
        [MockResponse(proposal=_final_proposal(wording).model_dump(mode="json"))],
        name="stable",
    )

    second = runtime.run(
        TaskType.VALIDATE_FINAL_CLAIM, invocation, engines=[engine]
    )

    assert second.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert engine.calls == 1
    task_dir = runtime.project_root / "work" / "tasks" / second.task_id
    rejected = list(task_dir.glob("attempts/*/cache_rejected.txt"))
    assert len(rejected) == 1


def test_final_wording_mismatch_is_rejected_on_receipt_path(tmp_path, bundle_factory):
    snapshot = _two_cpe_snapshot(bundle_factory).model_copy(
        update={"claim_assessments": (_assessment_proposal_to_model(),)}
    )
    runtime = _runtime(tmp_path, snapshot)
    wording = "Authoritative wording."
    invocation = ValidateFinalClaimInvocation(
        claim_id="C0001", proposed_final_claim=wording
    )
    first, _ = _run_final(runtime, invocation, _final_proposal(wording), name="stable")
    assert first.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    receipts_path = runtime.store.generation_path(first.generation) / APPLIED_TASKS_FILE
    receipt = runtime.store.load_receipts(first.generation)[0]
    mismatched_payload = _final_proposal("Different wording.").model_dump(mode="json")
    tampered = receipt.model_copy(
        update={
            "proposal_payload": mismatched_payload,
            "proposal_hash": hash_json(mismatched_payload),
        }
    )
    receipts_path.chmod(0o600)
    receipts_path.write_text(
        json.dumps([tampered.model_dump(mode="json")], indent=2) + "\n",
        encoding="utf-8",
    )

    second = runtime.run(
        TaskType.VALIDATE_FINAL_CLAIM,
        invocation,
        engines=[
            MockEngine(
                [MockResponse(proposal=_final_proposal(wording).model_dump(mode="json"))],
                name="stable",
            )
        ],
    )

    assert not second.receipt_reused
    rejected_path = (
        runtime.project_root
        / "work"
        / "tasks"
        / second.task_id
        / "receipt_rejected.txt"
    )
    assert "final_claim must exactly equal" in rejected_path.read_text(
        encoding="utf-8"
    )


def test_final_task_requires_claim_assessment_before_engine(tmp_path, bundle_factory):
    snapshot = _two_cpe_snapshot(bundle_factory)
    runtime = _runtime(tmp_path, snapshot)
    engine = MockEngine([], name="unused")

    with pytest.raises(KeyError, match="ClaimAssessment:C0001"):
        runtime.run(
            TaskType.VALIDATE_FINAL_CLAIM,
            ValidateFinalClaimInvocation(
                claim_id="C0001", proposed_final_claim="Synthetic wording."
            ),
            engines=[engine],
        )

    assert engine.calls == 0
