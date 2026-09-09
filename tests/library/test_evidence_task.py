from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

import vibereview.library as library_api
from vibereview.enums import RetrievalIntent
from vibereview.ids import candidate_claim_hash
from vibereview.library.evidence_task import (
    EVIDENCE_TASK_ARTIFACT_ROOT,
    AssessEvidencePromotionAdapter,
    AssessEvidencePromotionArtifact,
    run_assess_evidence_task,
)
from vibereview.library.git_source import PinnedGitSource
from vibereview.library.inventory import build_library_inventory
from vibereview.library.models import (
    CorpusIntegrityError,
    CorpusSelectionDocument,
    CorpusSelectionManifest,
    DocumentKind,
    LibraryConfig,
)
from vibereview.library.retrieval import RetrievalLedger, UnifiedRetrievalCoordinator
from vibereview.library.retrieval_promotion import (
    RetrievalPromotionRecord,
    _ledger_bytes,
)
from vibereview.library.selection import import_selected_corpus
from vibereview.models import CandidateClaim, RetrievalQuery, ThemeRecord
from vibereview.runtime.dto import (
    AssessEvidenceInvocation,
    AssessEvidenceProposalBundle,
    EvidenceAssessmentProposal,
    EvidenceCandidateDecisionProposal,
)
from vibereview.runtime.engine import MockEngine, MockResponse
from vibereview.runtime.hashing import hash_bytes, hash_json
from vibereview.runtime.kernel import ProjectRuntime
from vibereview.runtime.records import AttemptOutcome, GenerationManifest, TaskType
from vibereview.runtime.registry import IdKind
from vibereview.runtime.repository import (
    GenerationStore,
    PromotionPayload,
    _validated_auxiliary_hash,
)


def _selection(config: LibraryConfig) -> CorpusSelectionManifest:
    inventory, _ = build_library_inventory(PinnedGitSource.open(config), config)
    return CorpusSelectionManifest(
        library_id=config.library_id,
        source_commit=config.expected_commit,
        documents=[
            CorpusSelectionDocument(
                source_relative_path=item.source_relative_path,
                content_sha256=item.content_sha256,
                decision="include",
                role="synthetic_test",
                accepted_by="synthetic-fixture",
                accepted_at="2030-01-01T00:00:00Z",
                reason="task-bound evidence promotion test",
            )
            for item in inventory
            if item.document_kind is DocumentKind.CANDIDATE_PAPER_MARKDOWN
        ],
    )


@dataclass(frozen=True)
class EvidenceTaskCase:
    review_root: Path
    runtime: ProjectRuntime
    ledger: RetrievalLedger
    claim_id: str
    ledger_path: Path


@pytest.fixture
def evidence_task_case(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
) -> EvidenceTaskCase:
    _, config, _ = synthetic_library
    review_root = tmp_path / "review"
    import_selected_corpus(
        review_root,
        _selection(config),
        config,
        public_repository_root=tmp_path / "public-repository",
    )
    store = GenerationStore(review_root)
    base_generation, _, _ = store.load_current()

    def add_query(snapshot, registry):
        theme_ids, registry = registry.allocate(IdKind.THEME)
        claim_ids, registry = registry.allocate(IdKind.CLAIM)
        query_id, registry = registry.allocate_query(
            claim_ids[0], RetrievalIntent.SUPPORT
        )
        theme = ThemeRecord(
            theme_id=theme_ids[0],
            title="Synthetic evidence task",
            description="Fictitious task-bound promotion fixture.",
            origin="human",
            parent_theme_id=None,
        )
        claim = CandidateClaim(
            claim_id=claim_ids[0],
            theme_id=theme.theme_id,
            candidate_claim="A synthetic lattice signal changes under load.",
            origin="human",
            origin_refs=[],
        )
        query = RetrievalQuery(
            query_id=query_id,
            claim_id=claim.claim_id,
            candidate_claim_hash=candidate_claim_hash(claim.candidate_claim),
            intent=RetrievalIntent.SUPPORT,
            query_text="lattice signal",
        )
        return PromotionPayload(
            snapshot.model_copy(
                update={
                    "themes": snapshot.themes + (theme,),
                    "candidate_claims": snapshot.candidate_claims + (claim,),
                    "retrieval_queries": snapshot.retrieval_queries + (query,),
                }
            ),
            registry,
            {},
        )

    query_commit = store.commit(
        base_generation=base_generation,
        dependencies={},
        promotion=add_query,
    )
    snapshot, _ = store.load_generation(query_commit.generation)
    query = snapshot.retrieval_queries[0]
    candidates, ledger = UnifiedRetrievalCoordinator.from_generation(
        review_root, generation=query_commit.generation
    ).retrieve(
        query.query_text,
        query.query_id,
        query.intent,
        top_k=1,
    )
    assert len(candidates) == 1

    resource_root = review_root / "manual-task-resources"
    resource_root.mkdir()
    ledger_path = resource_root / "retrieval-ledger.json"
    ledger_path.write_bytes(_ledger_bytes(ledger))
    runtime = ProjectRuntime(
        review_root,
        allowed_source_roots=(review_root,),
    )
    return EvidenceTaskCase(
        review_root=review_root,
        runtime=runtime,
        ledger=ledger,
        claim_id=query.claim_id,
        ledger_path=ledger_path,
    )


def _invocation(case: EvidenceTaskCase) -> AssessEvidenceInvocation:
    return AssessEvidenceInvocation(
        source_generation=case.ledger.source_generation,
        claim_id=case.claim_id,
        query_id=case.ledger.query_id,
        candidate_refs=list(case.ledger.selected_candidate_keys),
        canonical_span_refs=[],
        retrieval_ledger_path=case.ledger_path,
    )


def _proposal(case: EvidenceTaskCase) -> AssessEvidenceProposalBundle:
    return AssessEvidenceProposalBundle(
        decisions=[
            EvidenceCandidateDecisionProposal(
                candidate_ref=case.ledger.selected_candidate_keys[0],
                status="assessed",
                evidence=EvidenceAssessmentProposal(
                    relation_to_candidate="supports",
                    evidence_summary=(
                        "The fictitious source reports a changed lattice signal."
                    ),
                    quality={
                        "directness": "direct",
                        "methodological_relevance": "moderate",
                        "strength": "low",
                        "assessability": "partial",
                        "limitations": ["Synthetic fixture only."],
                    },
                    assessment_note="Synthetic task-bound assessment.",
                ),
            )
        ]
    )


def _engine(
    proposal: AssessEvidenceProposalBundle,
    *,
    name: str = "evidence-mock",
) -> MockEngine:
    return MockEngine(
        [MockResponse(proposal=proposal.model_dump(mode="json"))],
        name=name,
    )


def test_assess_evidence_commits_state_receipt_and_artifacts_atomically(
    evidence_task_case: EvidenceTaskCase,
) -> None:
    case = evidence_task_case
    adapter = AssessEvidencePromotionAdapter(case.review_root, case.ledger)
    result = run_assess_evidence_task(
        case.runtime,
        case.ledger,
        [_engine(_proposal(case))],
    )

    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert result.commit_performed
    assert result.generation == case.ledger.source_generation + 1
    candidate_ref = case.ledger.selected_candidate_keys[0]
    assert result.allocated_ids == {
        f"span:{candidate_ref}": "R0001",
        f"evidence:{candidate_ref}": "E0001",
    }

    _, snapshot, registry = case.runtime.store.load_current()
    assert [item.span_id for item in snapshot.retrieved_spans] == ["R0001"]
    assert [item.span_id for item in snapshot.retrieval_dispositions] == ["R0001"]
    assert [item.evidence_id for item in snapshot.evidence_records] == ["E0001"]
    assert snapshot.evidence_records[0].claim_id == case.claim_id
    assert registry.counters[IdKind.SPAN] == 1
    assert registry.counters[IdKind.EVIDENCE] == 1
    snapshot.validate_repository()

    receipts = case.runtime.store.load_receipts(result.generation)
    assert len(receipts) == 1
    receipt = receipts[0]
    auxiliary_root = (
        case.runtime.store.generation_path(result.generation) / "auxiliary"
    )
    record_paths = list((auxiliary_root / "retrieval/promotions").rglob("*.json"))
    artifact_paths = list((auxiliary_root / EVIDENCE_TASK_ARTIFACT_ROOT).rglob("*.json"))
    assert len(record_paths) == len(artifact_paths) == 1
    record = RetrievalPromotionRecord.model_validate_json(record_paths[0].read_bytes())
    artifact = AssessEvidencePromotionArtifact.model_validate_json(
        artifact_paths[0].read_bytes()
    )
    assert record.span_ids == {candidate_ref: "R0001"}
    assert record.evidence_ids == {candidate_ref: "E0001"}
    assert artifact.adapter_fingerprint == adapter.promotion_fingerprint
    assert artifact.semantic_task_key == receipt.semantic_task_key
    assert artifact.proposal_hash == receipt.proposal_hash
    assert artifact.receipt_hash == hash_json(receipt.model_dump(mode="json"))
    assert artifact.request_hash == record.request_hash
    assert artifact.ledger_hash == record.ledger_hash
    assert artifact.assessed_candidate_refs == (candidate_ref,)
    assert set(artifact.evidence_ids) == set(artifact.assessed_candidate_refs)
    artifact_payload = artifact.model_dump(mode="json")
    artifact_payload["assessed_candidate_refs"] = []
    with pytest.raises(ValueError, match="evidence allocations disagree"):
        AssessEvidencePromotionArtifact.model_validate(artifact_payload)
    artifact_payload = artifact.model_dump(mode="json")
    artifact_payload["accepted_attempt_id"] = f"{artifact.task_id}/"
    with pytest.raises(ValueError, match="attempt does not belong"):
        AssessEvidencePromotionArtifact.model_validate(artifact_payload)
    assert not (record_paths[0].stat().st_mode & 0o222)
    assert not (artifact_paths[0].stat().st_mode & 0o222)

    ledger_digest = hash_bytes(_ledger_bytes(case.ledger)).removeprefix("sha256:")
    ledger_resource = (
        case.review_root
        / "work/task_resources/retrieval_ledgers/sha256"
        / f"{ledger_digest}.json"
    )
    assert ledger_resource.read_bytes() == _ledger_bytes(case.ledger)
    provenance = case.runtime.tasks.load_provenance(
        case.review_root / "work/tasks" / result.task_id
    )
    assert provenance.resources[0].source_path == ledger_resource


def test_exact_task_replay_reuses_receipt_without_engine_or_generation(
    evidence_task_case: EvidenceTaskCase,
) -> None:
    case = evidence_task_case
    adapter = AssessEvidencePromotionAdapter(case.review_root, case.ledger)
    first = run_assess_evidence_task(
        case.runtime,
        case.ledger,
        [_engine(_proposal(case))],
    )
    second_engine = _engine(_proposal(case))
    ledger_digest = hash_bytes(_ledger_bytes(case.ledger)).removeprefix("sha256:")
    ledger_resource = (
        case.review_root
        / "work/task_resources/retrieval_ledgers/sha256"
        / f"{ledger_digest}.json"
    )
    before_resource = ledger_resource.stat()

    second = run_assess_evidence_task(
        case.runtime,
        case.ledger,
        [second_engine],
    )

    assert second.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert second.receipt_reused
    assert not second.commit_performed
    assert second.generation == first.generation
    assert second.reused_generation == first.generation
    assert second.allocated_ids == first.allocated_ids
    assert second_engine.calls == 0
    assert case.runtime.store.current_generation() == first.generation
    after_resource = ledger_resource.stat()
    assert (after_resource.st_dev, after_resource.st_ino) == (
        before_resource.st_dev,
        before_resource.st_ino,
    )


def test_private_ledger_staging_rejects_symlink_escape_without_outside_write(
    evidence_task_case: EvidenceTaskCase,
    tmp_path: Path,
) -> None:
    case = evidence_task_case
    outside = tmp_path / "outside-ledger-target"
    outside.mkdir()
    work = case.review_root / "work"
    work.mkdir()
    (work / "task_resources").symlink_to(outside, target_is_directory=True)
    engine = _engine(_proposal(case))

    with pytest.raises(
        CorpusIntegrityError,
        match="private retrieval-ledger resource path is unsafe",
    ):
        run_assess_evidence_task(case.runtime, case.ledger, [engine])

    assert list(outside.iterdir()) == []
    assert engine.calls == 0


def test_exact_receipt_remains_reusable_after_unrelated_generation(
    evidence_task_case: EvidenceTaskCase,
) -> None:
    case = evidence_task_case
    first = run_assess_evidence_task(
        case.runtime,
        case.ledger,
        [_engine(_proposal(case))],
    )
    unrelated = case.runtime.store.commit(
        base_generation=first.generation,
        dependencies={},
        promotion=lambda snapshot, registry: PromotionPayload(
            snapshot, registry, {}
        ),
    )
    engine = _engine(_proposal(case))

    reused = run_assess_evidence_task(
        case.runtime,
        case.ledger,
        [engine],
    )

    assert reused.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert reused.receipt_reused
    assert not reused.commit_performed
    assert reused.generation == unrelated.generation
    assert reused.reused_generation == first.generation
    assert engine.calls == 0
    assert case.runtime.store.current_generation() == unrelated.generation


def test_historical_ledger_without_receipt_stops_before_engine(
    evidence_task_case: EvidenceTaskCase,
) -> None:
    case = evidence_task_case
    advanced = case.runtime.store.commit(
        base_generation=case.ledger.source_generation,
        dependencies={},
        promotion=lambda snapshot, registry: PromotionPayload(
            snapshot, registry, {}
        ),
    )
    engine = _engine(_proposal(case))

    result = run_assess_evidence_task(
        case.runtime,
        case.ledger,
        [engine],
    )

    assert result.outcome is AttemptOutcome.STALE_SNAPSHOT
    assert not result.commit_performed
    assert not result.receipt_reused
    assert engine.calls == 0
    generation, snapshot, registry = case.runtime.store.load_current()
    assert generation == advanced.generation
    assert snapshot.retrieved_spans == ()
    assert snapshot.evidence_records == ()
    assert registry.counters.get(IdKind.SPAN, 0) == 0
    assert registry.counters.get(IdKind.EVIDENCE, 0) == 0


def test_existing_exact_locator_requires_duplicate_on_task_path(
    evidence_task_case: EvidenceTaskCase,
) -> None:
    case = evidence_task_case
    first = run_assess_evidence_task(
        case.runtime,
        case.ledger,
        [_engine(_proposal(case))],
    )
    current_snapshot, _ = case.runtime.store.load_generation(first.generation)
    query = next(
        item
        for item in current_snapshot.retrieval_queries
        if item.query_id == case.ledger.query_id
    )
    _, next_ledger = UnifiedRetrievalCoordinator.from_generation(
        case.review_root, generation=first.generation
    ).retrieve(query.query_text, query.query_id, query.intent, top_k=1)
    assert next_ledger.selected_candidate_keys == case.ledger.selected_candidate_keys

    assessed_again = _engine(_proposal(case), name="second-evidence-mock")
    rejected = run_assess_evidence_task(
        case.runtime,
        next_ledger,
        [assessed_again],
    )
    assert rejected.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert assessed_again.calls == 1
    assert case.runtime.store.current_generation() == first.generation

    duplicate_proposal = AssessEvidenceProposalBundle(
        decisions=[
            EvidenceCandidateDecisionProposal(
                candidate_ref=next_ledger.selected_candidate_keys[0],
                status="duplicate",
                canonical_span_ref="R0001",
            )
        ]
    )
    duplicate = run_assess_evidence_task(
        case.runtime,
        next_ledger,
        [_engine(duplicate_proposal, name="duplicate-evidence-mock")],
    )
    assert duplicate.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert duplicate.allocated_ids == {
        f"span:{next_ledger.selected_candidate_keys[0]}": "R0002"
    }
    _, final, registry = case.runtime.store.load_current()
    assert [item.status.value for item in final.retrieval_dispositions] == [
        "assessed",
        "duplicate",
    ]
    assert [item.evidence_id for item in final.evidence_records] == ["E0001"]
    assert registry.counters[IdKind.SPAN] == 2
    assert registry.counters[IdKind.EVIDENCE] == 1


def test_receipt_reuse_rejects_changed_generation_owned_artifact_without_reexecution(
    evidence_task_case: EvidenceTaskCase,
) -> None:
    case = evidence_task_case
    first = run_assess_evidence_task(
        case.runtime,
        case.ledger,
        [_engine(_proposal(case))],
    )
    auxiliary_root = (
        case.runtime.store.generation_path(first.generation) / "auxiliary"
    )
    artifact_path = next(
        (auxiliary_root / EVIDENCE_TASK_ARTIFACT_ROOT).rglob("*.json")
    )
    artifact = AssessEvidencePromotionArtifact.model_validate_json(
        artifact_path.read_bytes()
    ).model_copy(update={"adapter_fingerprint": "sha256:" + "f" * 64})
    artifact_path.chmod(0o600)
    artifact_path.write_text(
        artifact.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    # Keep the repository-level auxiliary checksum internally consistent so
    # this reaches the adapter's receipt/artifact cross-check rather than the
    # earlier generic generation-integrity check.
    manifest_path = (
        case.runtime.store.generation_path(first.generation)
        / "generation_manifest.json"
    )
    manifest = GenerationManifest.model_validate_json(manifest_path.read_bytes())
    manifest = manifest.model_copy(
        update={"auxiliary_hash": _validated_auxiliary_hash(auxiliary_root)}
    )
    manifest_path.chmod(0o600)
    manifest_path.write_text(
        manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    second_engine = _engine(_proposal(case))

    second = run_assess_evidence_task(
        case.runtime,
        case.ledger,
        [second_engine],
    )

    assert second.outcome is AttemptOutcome.INTERNAL_RUNTIME_FAILURE
    assert not second.receipt_reused
    assert not second.commit_performed
    assert second_engine.calls == 0
    assert second.receipt_rejection_reason is not None
    assert second.receipt_rejection_reason.startswith(
        "ASSESS_EVIDENCE_RECEIPT_ARTIFACT_INVALID:"
    )
    assert case.runtime.store.current_generation() == first.generation


def test_proposal_candidate_mismatch_is_rejected_before_allocation(
    evidence_task_case: EvidenceTaskCase,
) -> None:
    case = evidence_task_case
    proposal = _proposal(case)
    proposal = proposal.model_copy(
        update={
            "decisions": [
                proposal.decisions[0].model_copy(
                    update={"candidate_ref": "sha256:" + "f" * 64}
                )
            ]
        }
    )

    result = case.runtime.run(
        TaskType.ASSESS_EVIDENCE,
        _invocation(case),
        engines=[_engine(proposal)],
        promotion_adapter=AssessEvidencePromotionAdapter(
            case.review_root, case.ledger
        ),
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert not result.commit_performed
    generation, snapshot, registry = case.runtime.store.load_current()
    assert generation == case.ledger.source_generation
    assert snapshot.retrieved_spans == ()
    assert snapshot.evidence_records == ()
    assert registry.counters.get(IdKind.SPAN, 0) == 0
    assert registry.counters.get(IdKind.EVIDENCE, 0) == 0


def test_duplicate_target_must_be_in_invocation_allowlist(
    evidence_task_case: EvidenceTaskCase,
) -> None:
    case = evidence_task_case
    proposal = AssessEvidenceProposalBundle(
        decisions=[
            EvidenceCandidateDecisionProposal(
                candidate_ref=case.ledger.selected_candidate_keys[0],
                status="duplicate",
                canonical_span_ref="R9999",
            )
        ]
    )

    result = case.runtime.run(
        TaskType.ASSESS_EVIDENCE,
        _invocation(case),
        engines=[_engine(proposal)],
        promotion_adapter=AssessEvidencePromotionAdapter(
            case.review_root, case.ledger
        ),
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert "outside the invocation allowlist" in (
        result.attempt_records[0].validation_errors[0]
    )
    assert case.runtime.store.current_generation() == case.ledger.source_generation


def test_noncanonical_ledger_resource_cannot_cross_commit(
    evidence_task_case: EvidenceTaskCase,
) -> None:
    case = evidence_task_case
    case.ledger_path.write_text(
        case.ledger.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )

    result = case.runtime.run(
        TaskType.ASSESS_EVIDENCE,
        _invocation(case),
        engines=[_engine(_proposal(case))],
        promotion_adapter=AssessEvidencePromotionAdapter(
            case.review_root, case.ledger
        ),
    )

    assert result.outcome is AttemptOutcome.INTERNAL_RUNTIME_FAILURE
    assert not result.commit_performed
    assert case.runtime.store.current_generation() == case.ledger.source_generation


def test_direct_semantic_retrieval_promotion_is_not_a_public_library_api() -> None:
    assert "promote_retrieval_candidates" not in library_api.__all__
    assert not hasattr(library_api, "promote_retrieval_candidates")
