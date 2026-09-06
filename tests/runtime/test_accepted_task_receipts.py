"""Tests for idempotent accepted-task receipts (goal.md §9, commit r5c)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from vibereview.enums import ClaimDecision
from vibereview.models import CandidateClaim, ClaimAssessment, ThemeRecord
from vibereview.runtime import (
    APPLIED_TASKS_FILE,
    AppliedTaskReceipt,
    AssessClaimInvocation,
    AttemptOutcome,
    CandidateClaimProposal,
    CanonicalObjectReceipt,
    ClaimAssessmentProposal,
    CrashPoint,
    DiscoveryProposalBundle,
    GenerateCandidateClaimsInvocation,
    GenerationStore,
    InjectedCrash,
    MockEngine,
    MockResponse,
    ProjectRuntime,
    PromotionPayload,
    RepositorySnapshot,
    TaskSemanticFingerprint,
    TaskType,
    ThemeProposal,
    compute_handler_fingerprint,
    compute_semantic_fingerprint,
    compute_semantic_task_key,
    extract_canonical_object_receipts,
    verify_receipt_canonical_objects,
)
from vibereview.runtime.promotion import (
    DISPOSITION_HANDLERS,
    PROMOTION_HANDLERS,
    interpret_positive,
    promote_discovery,
)


def _discovery_proposal(label: str = "t") -> DiscoveryProposalBundle:
    return DiscoveryProposalBundle(
        themes=[
            ThemeProposal(
                local_ref=f"{label}_theme_1",
                title="Residual Stress Mitigation",
                description="Analysis of thermal gradients",
                origin="generated",
            )
        ],
        claims=[
            CandidateClaimProposal(
                local_ref=f"{label}_claim_1",
                theme_ref=f"{label}_theme_1",
                candidate_claim="Laser preheating reduces residual tension.",
                origin="generated",
                origin_refs=[],
            )
        ],
    )


def _exploding_engine(name: str = "mock") -> MockEngine:
    def on_execute(_task: Any, _call_number: int) -> None:
        raise AssertionError("Engine must not be called when receipt is reused")

    return MockEngine([], name=name, on_execute=on_execute)


def _create_runtime(tmp_path: Path, snapshot: RepositorySnapshot | None = None) -> ProjectRuntime:
    return ProjectRuntime.create(
        tmp_path / "project",
        project_name="receipt-tests",
        initial_snapshot=snapshot,
    )


def test_discovery_replay_reuses_receipt_without_engine_or_new_ids(tmp_path: Path):
    """Same discovery task twice: second run reuses receipt, makes no engine call,

    creates no new generation, and allocates no new Theme/Claim IDs (goal.md §9.9).
    """
    runtime = _create_runtime(tmp_path)
    proposal = _discovery_proposal()
    engine1 = MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])

    invocation = GenerateCandidateClaimsInvocation(topic="welding", existing_theme_ids=[])
    result1 = runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        invocation,
        engines=[engine1],
    )

    assert result1.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert result1.commit_performed is True
    assert result1.receipt_reused is False
    assert result1.generation == 1
    assert result1.allocated_ids == {"t_theme_1": "T0001", "t_claim_1": "C0001"}

    # Second run with an exploding engine that raises if executed
    exploding = _exploding_engine()
    result2 = runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        invocation,
        engines=[exploding],
    )

    assert result2.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert result2.receipt_reused is True
    assert result2.commit_performed is False
    assert result2.reused_generation == 1
    assert result2.generation == 1
    assert result2.attempt_records == []
    assert result2.allocated_ids == {"t_theme_1": "T0001", "t_claim_1": "C0001"}
    assert result2.receipt_rejection_reason is None

    # Current generation remains 1
    assert runtime.store.current_generation() == 1
    _, current_snapshot, _ = runtime.store.load_current()
    assert len(current_snapshot.themes) == 1
    assert len(current_snapshot.candidate_claims) == 1
    assert current_snapshot.themes[0].theme_id == "T0001"
    assert current_snapshot.candidate_claims[0].claim_id == "C0001"


def test_scientific_rejection_replay_reuses_assessment_without_fallback(tmp_path: Path):
    """Same ClaimAssessment=REJECT task twice: reused, no fallback, no new generation (goal.md §9.9)."""
    initial = RepositorySnapshot(
        themes=(
            ThemeRecord(
                theme_id="T0001",
                title="Foundations",
                description="Fundamental physics",
                origin="human",
                parent_theme_id=None,
            ),
        ),
        candidate_claims=(
            CandidateClaim(
                claim_id="C0001",
                theme_id="T0001",
                candidate_claim="Perpetual motion is achievable via cold fusion.",
                origin="human",
                origin_refs=[],
            ),
        ),
    )
    runtime = _create_runtime(tmp_path, initial)

    reject_proposal = ClaimAssessmentProposal(
        claim_ref="C0001",
        aggregate_strength="low",
        evidence_sufficiency="insufficient",
        decision=ClaimDecision.REJECT,
        rejection_basis="contradicted",
        support_summary="No credible support found.",
        contradiction_summary="Directly contradicted by empirical physics.",
        qualification_summary="Unqualified rejection.",
        reason="Physical impossibility",
    )
    engine1 = MockEngine([MockResponse(proposal=reject_proposal.model_dump(mode="json"))])

    invocation = AssessClaimInvocation(claim_id="C0001", claim_paper_evidence_ids=[])
    result1 = runtime.run(
        TaskType.ASSESS_CLAIM,
        invocation,
        engines=[engine1],
    )

    assert result1.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert result1.transition is not None
    assert result1.transition.scientific_disposition == "REJECT"
    assert not result1.transition.downstream_eligible
    assert result1.commit_performed is True
    assert result1.receipt_reused is False
    assert result1.generation == 1

    # Second run with exploding engine: must reuse receipt without calling engine or triggering fallback
    exploding = _exploding_engine()
    result2 = runtime.run(
        TaskType.ASSESS_CLAIM,
        invocation,
        engines=[exploding],
    )

    assert result2.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert result2.receipt_reused is True
    assert result2.commit_performed is False
    assert result2.reused_generation == 1
    assert result2.generation == 1
    assert result2.attempt_records == []
    assert result2.transition is not None
    assert result2.transition.scientific_disposition == "REJECT"
    assert not result2.transition.downstream_eligible

    # Check store state: still generation 1 with exactly one assessment
    assert runtime.store.current_generation() == 1
    _, current_snapshot, _ = runtime.store.load_current()
    assert len(current_snapshot.claim_assessments) == 1
    assert current_snapshot.claim_assessments[0].decision is ClaimDecision.REJECT


def test_unrelated_generation_leaves_receipt_reusable(tmp_path: Path):
    """Unrelated canonical object added, task dependencies unchanged: receipt remains reusable (goal.md §9.9)."""
    runtime = _create_runtime(tmp_path)
    proposal_a = _discovery_proposal("a")
    engine_a = MockEngine([MockResponse(proposal=proposal_a.model_dump(mode="json"))])

    invocation_a = GenerateCandidateClaimsInvocation(topic="welding", existing_theme_ids=[])
    result_a1 = runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        invocation_a,
        engines=[engine_a],
    )
    assert result_a1.generation == 1
    assert result_a1.receipt_reused is False

    # Now add an unrelated theme directly to advance generation to 2
    def add_unrelated(snapshot, registry):
        theme = ThemeRecord(
            theme_id="T0099",
            title="Unrelated Theme",
            description="Completely unrelated topic",
            origin="human",
            parent_theme_id=None,
        )
        return PromotionPayload(
            snapshot.model_copy(update={"themes": snapshot.themes + (theme,)}),
            registry,
            {"unrelated": "T0099"},
        )

    runtime.store.commit(base_generation=1, dependencies={}, promotion=add_unrelated)
    assert runtime.store.current_generation() == 2

    # Run task A again: current generation is 2, but receipt A was committed in gen 1
    # Its dependencies and canonical objects are intact in gen 2!
    exploding = _exploding_engine()
    result_a2 = runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        invocation_a,
        engines=[exploding],
    )

    assert result_a2.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert result_a2.receipt_reused is True
    assert result_a2.commit_performed is False
    assert result_a2.reused_generation == 1
    assert result_a2.generation == 1
    assert result_a2.attempt_records == []
    # Current generation remains 2 (no new generation created)
    assert runtime.store.current_generation() == 2


def test_dependency_change_rejects_receipt(tmp_path: Path):
    """Dependent object hash changes: receipt is rejected and engine is called (goal.md §9.9)."""
    initial = RepositorySnapshot(
        themes=(
            ThemeRecord(
                theme_id="T0001",
                title="Foundations",
                description="Fundamental physics",
                origin="human",
                parent_theme_id=None,
            ),
        ),
        candidate_claims=(
            CandidateClaim(
                claim_id="C0001",
                theme_id="T0001",
                candidate_claim="Initial claim wording",
                origin="human",
                origin_refs=[],
            ),
        ),
    )
    runtime = _create_runtime(tmp_path, initial)

    assess_prop = ClaimAssessmentProposal(
        claim_ref="C0001",
        aggregate_strength="high",
        evidence_sufficiency="sufficient",
        decision=ClaimDecision.RETAIN,
        rejection_basis=None,
        support_summary="Strong evidence",
        contradiction_summary="No contradiction",
        qualification_summary="Clear support",
        reason="Good data",
    )
    engine1 = MockEngine([MockResponse(proposal=assess_prop.model_dump(mode="json"))])
    invocation = AssessClaimInvocation(claim_id="C0001", claim_paper_evidence_ids=[])

    result1 = runtime.run(TaskType.ASSESS_CLAIM, invocation, engines=[engine1])
    assert result1.generation == 1
    assert result1.commit_performed is True

    # Mutate C0001 in generation 2
    def mutate_claim(snapshot, registry):
        updated_claims = tuple(
            c.model_copy(update={"candidate_claim": "Mutated claim wording!"})
            if c.claim_id == "C0001"
            else c
            for c in snapshot.candidate_claims
        )
        return PromotionPayload(
            snapshot.model_copy(update={"candidate_claims": updated_claims}),
            registry,
            {},
        )

    runtime.store.commit(base_generation=1, dependencies={}, promotion=mutate_claim)
    assert runtime.store.current_generation() == 2

    # Run again with a new engine response: the receipt should be rejected because
    # the dependency C0001 hash has changed!
    engine2 = MockEngine([MockResponse(proposal=assess_prop.model_dump(mode="json"))])
    result2 = runtime.run(TaskType.ASSESS_CLAIM, invocation, engines=[engine2])

    assert result2.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert result2.receipt_reused is False
    assert result2.commit_performed is True
    assert result2.generation == 3


def test_transition_guard_disagreement_fails_closed(tmp_path: Path, monkeypatch):
    """Stored proposal valid, current disposition guard differs: fail closed with

    ACCEPTED_RECEIPT_REEVALUATION_REQUIRED and duplicate no objects (goal.md §9.7, §9.9).
    """
    runtime = _create_runtime(tmp_path)
    proposal = _discovery_proposal("guard")
    engine = MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])
    invocation = GenerateCandidateClaimsInvocation(topic="guard-test", existing_theme_ids=[])

    result1 = runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        invocation,
        engines=[engine],
    )
    assert result1.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert result1.generation == 1

    # Monkeypatch the disposition handler to return a disagreeing transition
    def conflicting_disposition(proposal_obj):
        return interpret_positive(proposal_obj).model_copy(
            update={"scientific_disposition": "DISAGREED_NEW_SEMANTICS"}
        )

    monkeypatch.setitem(DISPOSITION_HANDLERS, "interpret_positive", conflicting_disposition)

    # Re-run: the guard disagrees with the stored receipt recorded_transition
    result2 = runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        invocation,
        engines=[_exploding_engine()],
    )

    assert result2.outcome is AttemptOutcome.CONTRACT_IMPLEMENTATION_FAILURE
    assert result2.receipt_reused is False
    assert result2.receipt_rejection_reason == "ACCEPTED_RECEIPT_REEVALUATION_REQUIRED"
    assert result2.generation is None
    # Existing scientific state remains generation 1, untouched
    assert runtime.store.current_generation() == 1


def test_handler_fingerprint_change_prevents_reuse(tmp_path: Path, monkeypatch):
    """Promotion or disposition handler implementation changes: receipt not reused (goal.md §9.9)."""
    runtime = _create_runtime(tmp_path)
    proposal = _discovery_proposal("hfp")
    engine = MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])
    invocation = GenerateCandidateClaimsInvocation(topic="fingerprint-test", existing_theme_ids=[])

    result1 = runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        invocation,
        engines=[engine],
    )
    assert result1.generation == 1

    # Define a modified promotion handler with altered code
    def modified_promote_discovery(snapshot, registry, prop):
        return promote_discovery(snapshot, registry, prop)

    # Monkeypatch the promotion handler
    monkeypatch.setitem(PROMOTION_HANDLERS, "promote_discovery", modified_promote_discovery)

    # Second run: handler fingerprint differs, receipt must NOT be reused; engine is called
    engine2 = MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])
    result2 = runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        invocation,
        engines=[engine2],
    )

    assert result2.receipt_reused is False
    assert result2.commit_performed is True
    assert result2.generation == 2


def test_canonical_object_missing_or_modified_rejects_receipt(tmp_path: Path):
    """Receipt canonical object missing or hash differs: receipt rejected (goal.md §9.9)."""
    runtime = _create_runtime(tmp_path)
    proposal = _discovery_proposal("obj_check")
    engine = MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])
    invocation = GenerateCandidateClaimsInvocation(topic="obj-check", existing_theme_ids=[])

    result1 = runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        invocation,
        engines=[engine],
    )
    assert result1.generation == 1

    # Load receipts and inspect canonical objects
    receipts = runtime.store.load_receipts(1)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert len(receipt.canonical_objects) == 2  # Theme and CandidateClaim

    # Verify object verification function
    _, current_snapshot, _ = runtime.store.load_current()
    ok, err = verify_receipt_canonical_objects(receipt, current_snapshot)
    assert ok is True
    assert err is None

    # Tamper with snapshot by removing the theme
    tampered_snapshot = current_snapshot.model_copy(update={"themes": ()})
    ok_missing, err_missing = verify_receipt_canonical_objects(receipt, tampered_snapshot)
    assert ok_missing is False
    assert err_missing is not None
    assert "RECEIPT_CANONICAL_OBJECT_MISSING" in err_missing

    # Tamper with theme content
    tampered_theme = current_snapshot.themes[0].model_copy(update={"title": "TAMPERED TITLE"})
    tampered_snapshot2 = current_snapshot.model_copy(update={"themes": (tampered_theme,)})
    ok_mismatch, err_mismatch = verify_receipt_canonical_objects(receipt, tampered_snapshot2)
    assert ok_mismatch is False
    assert err_mismatch is not None
    assert "RECEIPT_CANONICAL_OBJECT_HASH_MISMATCH" in err_mismatch


@pytest.mark.parametrize(
    "crash_at",
    [
        CrashPoint.BEFORE_RECEIPT_CONSTRUCTION,
        CrashPoint.AFTER_SCIENTIFIC_STAGING_BEFORE_RECEIPT_STAGING,
        CrashPoint.AFTER_COMPLETION_MARKER_BEFORE_CURRENT,
        CrashPoint.AFTER_CURRENT,
    ],
)
def test_crash_safety_atomic_receipts(tmp_path: Path, crash_at: CrashPoint):
    """Crash injection before/during/after receipt staging: every recovered generation

    must contain either both effect and receipt or neither (goal.md §9.5, §9.9).
    """
    store = GenerationStore(tmp_path / crash_at.value)
    store.initialize()

    def _promote(snapshot, registry):
        theme = ThemeRecord(
            theme_id="T0001",
            title="Crash Test Theme",
            description="Testing atomicity",
            origin="human",
            parent_theme_id=None,
        )
        return PromotionPayload(
            snapshot.model_copy(update={"themes": snapshot.themes + (theme,)}),
            registry,
            {"theme": "T0001"},
        )

    def _fake_receipt(next_gen, before_snap, payload):
        canon = extract_canonical_object_receipts(before_snap, payload.snapshot, payload.allocated_ids)
        fp = TaskSemanticFingerprint(
            validator_fingerprint="sha256:" + "0" * 64,
            promotion_handler_fingerprint="sha256:" + "1" * 64,
            disposition_handler_fingerprint="sha256:" + "2" * 64,
            scientific_contract_version="V1.5.1b",
            runtime_contract_version="1.6",
            combined_fingerprint="sha256:" + "3" * 64,
        )
        from vibereview.runtime.records import TransitionDecision
        return AppliedTaskReceipt(
            semantic_task_key="sha256:" + "4" * 64,
            task_type=TaskType.GENERATE_CANDIDATE_CLAIMS,
            task_spec_version="1.0",
            proposal_hash="sha256:" + "5" * 64,
            proposal_payload={"test": "payload"},
            semantic_fingerprint=fp,
            source_generation=0,
            committed_generation=next_gen,
            canonical_objects=canon,
            local_ref_map={"theme": "T0001"},
            recorded_transition=TransitionDecision(
                scientific_disposition="VALID",
                canonicalized=True,
                downstream_eligible=True,
            ),
            engine="mock",
            engine_version="1.0",
            accepted_attempt_id="TASK0001/01-mock",
        )

    with pytest.raises(InjectedCrash):
        store.commit(
            base_generation=0,
            dependencies={},
            promotion=_promote,
            receipt_factory=_fake_receipt,
            crash_at=crash_at,
        )

    store.recover()
    gen, snapshot, _ = store.load_current()
    receipts = store.load_current_receipts()

    if crash_at is CrashPoint.AFTER_CURRENT:
        # Atomic commit crossed the CURRENT switch: both effect and receipt present!
        assert gen == 1
        assert len(snapshot.themes) == 1
        assert snapshot.themes[0].theme_id == "T0001"
        assert len(receipts) == 1
        assert receipts[0].committed_generation == 1
    else:
        # Commit aborted before CURRENT switch: neither effect nor receipt present!
        assert gen == 0
        assert len(snapshot.themes) == 0
        assert len(receipts) == 0
