from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import vibereview.library.retrieval_promotion as retrieval_promotion_module
from vibereview.enums import RetrievalIntent
from vibereview.ids import candidate_claim_hash
from vibereview.library.git_source import PinnedGitSource
from vibereview.library.inventory import build_library_inventory
from vibereview.library.models import (
    CorpusIntegrityError,
    CorpusSelectionDocument,
    CorpusSelectionManifest,
    DocumentKind,
    LibraryConfig,
)
from vibereview.library.retrieval import (
    CandidateHitOrigin,
    DeterministicTextRetriever,
    RawCandidateHit,
    RetrievalLedger,
    UnifiedRetrievalCoordinator,
    _candidate_key,
)
from vibereview.library.retrieval_promotion import (
    CoupledEvidenceProposal,
    RetrievalPromotionDecision,
    RetrievalPromotionError,
    RetrievalPromotionRecord,
    RetrievalPromotionRequest,
    _validate_decisions_against_snapshot,
    _promote_retrieval_candidates_for_testing as promote_retrieval_candidates,
    promote_retrieval_candidates as public_promote_retrieval_candidates,
)
from vibereview.library.selection import import_selected_corpus, load_corpus_lock
from vibereview.models import (
    CandidateClaim,
    ClaimAssessment,
    ClaimPaperEvidence,
    ComponentRelations,
    RetrievalQuery,
    ThemeRecord,
)
from vibereview.runtime.hashing import hash_bytes
from vibereview.runtime.registry import IdKind
from vibereview.runtime.repository import (
    CrashPoint,
    GenerationStore,
    InjectedCrash,
    PromotionPayload,
    StaleSnapshotError,
    UnsafeRepositoryEntryError,
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
                reason="coupled retrieval promotion test",
            )
            for item in inventory
            if item.document_kind is DocumentKind.CANDIDATE_PAPER_MARKDOWN
        ],
    )


@dataclass(frozen=True)
class PromotionCase:
    review_root: Path
    store: GenerationStore
    candidates: list[RawCandidateHit]
    ledger: RetrievalLedger


@pytest.fixture
def promotion_case(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
) -> PromotionCase:
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
        mixed_query_id, registry = registry.allocate_query(
            claim_ids[0], RetrievalIntent.BOUNDARY
        )
        invalid_query_id, registry = registry.allocate_query(
            claim_ids[0], RetrievalIntent.CONTRADICTION
        )
        theme = ThemeRecord(
            theme_id=theme_ids[0],
            title="Synthetic lattice response",
            description="Clearly fictitious retrieval promotion fixture.",
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
        mixed_query = RetrievalQuery(
            query_id=mixed_query_id,
            claim_id=claim.claim_id,
            candidate_claim_hash=candidate_claim_hash(claim.candidate_claim),
            intent=RetrievalIntent.BOUNDARY,
            query_text="Alpha lattice overflow",
        )
        invalid_query = RetrievalQuery(
            query_id=invalid_query_id,
            claim_id=claim.claim_id,
            candidate_claim_hash=candidate_claim_hash(claim.candidate_claim),
            intent=RetrievalIntent.CONTRADICTION,
            query_text="overflow",
        )
        return PromotionPayload(
            snapshot.model_copy(
                update={
                    "themes": snapshot.themes + (theme,),
                    "candidate_claims": snapshot.candidate_claims + (claim,),
                    "retrieval_queries": snapshot.retrieval_queries
                    + (query, mixed_query, invalid_query),
                }
            ),
            registry,
            {"theme": theme.theme_id, "claim": claim.claim_id, "query": query.query_id},
        )

    store.commit(
        base_generation=base_generation,
        dependencies={},
        promotion=add_query,
    )
    _, snapshot, _ = store.load_current()
    query = snapshot.retrieval_queries[0]
    candidates, ledger = UnifiedRetrievalCoordinator.from_generation(
        review_root
    ).retrieve(query.query_text, query.query_id, query.intent, top_k=1)
    assert len(candidates) == 1
    return PromotionCase(review_root, store, candidates, ledger)


def _evidence() -> CoupledEvidenceProposal:
    return CoupledEvidenceProposal(
        relation_to_candidate="supports",
        evidence_summary="The fictitious source reports a changed lattice signal.",
        quality={
            "directness": "direct",
            "methodological_relevance": "moderate",
            "strength": "low",
            "assessability": "partial",
            "limitations": ["Synthetic fixture only."],
        },
        assessment_note="Synthetic coupled-promotion exercise.",
    )


def _assessed_request(ledger: RetrievalLedger) -> RetrievalPromotionRequest:
    return RetrievalPromotionRequest(
        ledger=ledger,
        decisions=(
            RetrievalPromotionDecision(
                candidate_key=ledger.selected_candidate_keys[0],
                status="assessed",
                evidence=_evidence(),
            ),
        ),
    )


def _canonical_retrieval(
    case: PromotionCase,
    query_text: str,
    *,
    generation: int | None = None,
    top_k: int = 1,
) -> tuple[list[RawCandidateHit], RetrievalLedger]:
    source_generation = (
        case.store.current_generation() if generation is None else generation
    )
    snapshot, _ = case.store.load_generation(source_generation)
    query = next(
        item for item in snapshot.retrieval_queries if item.query_text == query_text
    )
    return UnifiedRetrievalCoordinator.from_generation(
        case.review_root, generation=source_generation
    ).retrieve(query.query_text, query.query_id, query.intent, top_k=top_k)


def _additional_hit(
    case: PromotionCase,
    *,
    valid: bool = True,
) -> RawCandidateHit:
    base = case.candidates[0]
    _, snapshot, _ = case.store.load_current()
    paper = next(item for item in snapshot.papers if item.paper_id == base.paper_id)
    if valid:
        relative = base.source_text.index("lattice signal")
        assert base.start_offset is not None
        start = base.start_offset + relative
        source_text = "lattice signal"
        end = start + len(source_text)
        source_hash = hash_bytes(source_text.encode("utf-8"))
        error = None
    else:
        start = end = None
        source_text = "deliberately absent synthetic diagnostic"
        source_hash = None
        error = "synthetic graph locator is not an exact source slice"
    key = _candidate_key(
        paper_id=base.paper_id,
        raw_md_hash=paper.raw_md_hash,
        query_id=base.query_id,
        query_text_hash=base.query_text_hash,
        source_text=source_text,
        start_offset=start,
        end_offset=end,
        origin=CandidateHitOrigin.GRAPH,
    )
    return RawCandidateHit(
        candidate_key=key,
        paper_id=base.paper_id,
        raw_md_path=base.raw_md_path,
        query_id=base.query_id,
        query_text_hash=base.query_text_hash,
        intent=base.intent,
        origin=CandidateHitOrigin.GRAPH,
        upstream_node_id="synthetic-node",
        upstream_relation="supports",
        source_text=source_text,
        start_offset=start,
        end_offset=end,
        source_span_hash=source_hash,
        context_text=base.context_text if valid else None,
        context_start_offset=base.context_start_offset if valid else None,
        context_end_offset=base.context_end_offset if valid else None,
        context_utf8_hash=base.context_utf8_hash if valid else None,
        section=base.section if valid else None,
        score=0.9,
        is_valid=valid,
        validation_error=error,
    )


def _ledger_with_ledger_only_hits(case: PromotionCase) -> RetrievalLedger:
    selected = case.candidates[0]
    excluded = _additional_hit(case)
    invalid = _additional_hit(case, valid=False)
    return RetrievalLedger(
        source_generation=case.ledger.source_generation,
        query_id=case.ledger.query_id,
        query_text_hash=case.ledger.query_text_hash,
        corpus_lock_hash=case.ledger.corpus_lock_hash,
        requested_top_k=case.ledger.requested_top_k,
        max_query_terms=case.ledger.max_query_terms,
        total_candidates=3,
        valid_candidates=2,
        invalid_candidates=1,
        selected_candidate_keys=[selected.candidate_key],
        excluded_by_budget_candidate_keys=[excluded.candidate_key],
        raw_hits=[selected, excluded, invalid],
    )


def test_atomic_promotion_allocates_span_disposition_evidence_and_record(
    promotion_case: PromotionCase,
) -> None:
    request = _assessed_request(promotion_case.ledger)
    result = promote_retrieval_candidates(
        promotion_case.review_root, request
    )
    generation, snapshot, registry = promotion_case.store.load_current()

    assert generation == result.generation == request.ledger.source_generation + 1
    assert result.commit_performed is True
    assert result.reused_generation is None
    assert result.span_ids == {request.ledger.selected_candidate_keys[0]: "R0001"}
    assert result.evidence_ids == {
        request.ledger.selected_candidate_keys[0]: "E0001"
    }
    assert [item.span_id for item in snapshot.retrieved_spans] == ["R0001"]
    assert [item.span_id for item in snapshot.retrieval_dispositions] == ["R0001"]
    assert snapshot.retrieval_dispositions[0].status.value == "assessed"
    assert snapshot.evidence_records[0].retrieved_span_id == "R0001"
    assert registry.counters[IdKind.SPAN] == 1
    assert registry.counters[IdKind.EVIDENCE] == 1
    snapshot.validate_repository()

    record_path = promotion_case.review_root / result.promotion_record_path
    record = RetrievalPromotionRecord.model_validate_json(record_path.read_bytes())
    assert record.span_ids == result.span_ids
    assert record.evidence_ids == result.evidence_ids
    assert record.ledger == request.ledger
    assert not (record_path.stat().st_mode & 0o222)


def test_direct_entry_point_rejects_assessed_evidence_without_task_receipt(
    promotion_case: PromotionCase,
) -> None:
    with pytest.raises(
        RetrievalPromotionError,
        match="requires an accepted ASSESS_EVIDENCE task receipt",
    ):
        public_promote_retrieval_candidates(
            promotion_case.review_root,
            _assessed_request(promotion_case.ledger),
        )
    generation, snapshot, registry = promotion_case.store.load_current()
    assert generation == promotion_case.ledger.source_generation
    assert snapshot.retrieved_spans == ()
    assert snapshot.evidence_records == ()
    assert registry.counters.get(IdKind.SPAN, 0) == 0
    assert registry.counters.get(IdKind.EVIDENCE, 0) == 0


def test_invalid_and_over_budget_candidates_are_recorded_but_never_allocated(
    promotion_case: PromotionCase,
) -> None:
    _, ledger = _canonical_retrieval(
        promotion_case,
        "Alpha lattice overflow",
        generation=promotion_case.ledger.source_generation,
    )
    assert ledger.valid_candidates == 2
    assert ledger.invalid_candidates == 1
    result = promote_retrieval_candidates(
        promotion_case.review_root, _assessed_request(ledger)
    )
    _, snapshot, registry = promotion_case.store.load_current()
    record = RetrievalPromotionRecord.model_validate_json(
        (promotion_case.review_root / result.promotion_record_path).read_bytes()
    )

    assert record.ledger.total_candidates == 3
    assert len(record.ledger.excluded_by_budget_candidate_keys) == 1
    assert record.ledger.invalid_candidates == 1
    assert len(snapshot.retrieved_spans) == 1
    assert len(snapshot.evidence_records) == 1
    assert registry.counters[IdKind.SPAN] == 1
    assert registry.counters[IdKind.EVIDENCE] == 1
    assert set(result.span_ids) == set(ledger.selected_candidate_keys)


def test_invalid_only_ledger_commits_a_record_without_allocating_scientific_ids(
    promotion_case: PromotionCase,
) -> None:
    _, ledger = _canonical_retrieval(
        promotion_case,
        "overflow",
        generation=promotion_case.ledger.source_generation,
    )
    assert ledger.valid_candidates == 0
    assert ledger.invalid_candidates == 1

    result = promote_retrieval_candidates(
        promotion_case.review_root,
        RetrievalPromotionRequest(ledger=ledger, decisions=()),
    )
    _, snapshot, registry = promotion_case.store.load_current()
    record = RetrievalPromotionRecord.model_validate_json(
        (promotion_case.review_root / result.promotion_record_path).read_bytes()
    )
    assert record.ledger.invalid_candidates == 1
    assert result.span_ids == result.evidence_ids == {}
    assert snapshot.retrieved_spans == snapshot.evidence_records == ()
    assert registry.counters.get(IdKind.SPAN, 0) == 0
    assert registry.counters.get(IdKind.EVIDENCE, 0) == 0


def test_ledger_rejects_duplicate_candidate_key_across_validity_classes(
    promotion_case: PromotionCase,
) -> None:
    invalid = _additional_hit(promotion_case, valid=False).model_copy(
        update={"candidate_key": promotion_case.candidates[0].candidate_key}
    )
    payload = promotion_case.ledger.model_dump(mode="json")
    payload.update(
        {
            "total_candidates": 2,
            "invalid_candidates": 1,
            "raw_hits": [
                promotion_case.candidates[0].model_dump(mode="json"),
                invalid.model_dump(mode="json"),
            ],
        }
    )

    with pytest.raises(ValidationError, match="candidate keys must be unique"):
        RetrievalLedger.model_validate(payload)


def test_unified_coordinator_preserves_valid_ensemble_exclusions_in_ledger() -> None:
    lock_hash = "sha256:" + "a" * 64
    query_id = "Q-C0001-SUP-01"
    query_hash = hash_bytes(b"synthetic")

    def hit(key_digit: str, origin: CandidateHitOrigin, start: int, score: float):
        text = f"hit-{key_digit}"
        return RawCandidateHit(
            candidate_key="sha256:" + key_digit * 64,
            paper_id="P0001",
            raw_md_path="synthetic/raw.md",
            query_id=query_id,
            query_text_hash=query_hash,
            intent="support",
            origin=origin,
            source_text=text,
            start_offset=start,
            end_offset=start + len(text),
            source_span_hash=hash_bytes(text.encode("utf-8")),
            context_text=text,
            context_start_offset=start,
            context_end_offset=start + len(text),
            context_utf8_hash=hash_bytes(text.encode("utf-8")),
            upstream_node_id=("node" if origin is CandidateHitOrigin.GRAPH else None),
            upstream_relation=("supports" if origin is CandidateHitOrigin.GRAPH else None),
            score=score,
            is_valid=True,
        )

    first = hit("1", CandidateHitOrigin.TEXT_BASELINE, 0, 0.9)
    second = hit("2", CandidateHitOrigin.GRAPH, 10, 0.8)

    class StaticRetriever:
        def __init__(self, values):
            self.values = values
            self.corpus = SimpleNamespace(generation=7, lock_hash=lock_hash)

        def search(self, *_args, **_kwargs):
            return list(self.values)

    selected, ledger = UnifiedRetrievalCoordinator(
        StaticRetriever([first]), StaticRetriever([second])
    ).retrieve("synthetic", query_id, RetrievalIntent.SUPPORT, top_k=1)

    assert selected == [first]
    assert ledger.valid_candidates == 2
    assert ledger.selected_candidate_keys == [first.candidate_key]
    assert ledger.excluded_by_budget_candidate_keys == [second.candidate_key]
    assert ledger.raw_hits == [first, second]


@pytest.mark.parametrize("crash_at", list(CrashPoint))
def test_crash_recovery_exposes_all_coupled_objects_or_none(
    promotion_case: PromotionCase,
    crash_at: CrashPoint,
) -> None:
    source_generation = promotion_case.ledger.source_generation
    with pytest.raises(InjectedCrash):
        promote_retrieval_candidates(
            promotion_case.review_root,
            _assessed_request(promotion_case.ledger),
            crash_at=crash_at,
        )

    generation, snapshot, registry = promotion_case.store.load_current()
    if crash_at is CrashPoint.AFTER_CURRENT:
        assert generation == source_generation + 1
        assert len(snapshot.retrieved_spans) == 1
        assert len(snapshot.retrieval_dispositions) == 1
        assert len(snapshot.evidence_records) == 1
        assert registry.counters[IdKind.SPAN] == 1
        assert registry.counters[IdKind.EVIDENCE] == 1
        records = list(
            (promotion_case.store.generation_path(generation) / "auxiliary").rglob(
                "*.json"
            )
        )
        assert len(records) == 1
        RetrievalPromotionRecord.model_validate_json(records[0].read_bytes())
        snapshot.validate_repository()
    else:
        assert generation == source_generation
        assert snapshot.retrieved_spans == ()
        assert snapshot.retrieval_dispositions == ()
        assert snapshot.evidence_records == ()
        assert registry.counters.get(IdKind.SPAN, 0) == 0
        assert registry.counters.get(IdKind.EVIDENCE, 0) == 0


def test_stale_batch_is_rejected_before_id_allocation(
    promotion_case: PromotionCase,
) -> None:
    promotion_case.store.commit(
        base_generation=promotion_case.ledger.source_generation,
        dependencies={},
        promotion=lambda snapshot, registry: PromotionPayload(snapshot, registry, {}),
    )

    with pytest.raises(StaleSnapshotError):
        promote_retrieval_candidates(
            promotion_case.review_root, _assessed_request(promotion_case.ledger)
        )
    generation, snapshot, registry = promotion_case.store.load_current()
    assert generation == promotion_case.ledger.source_generation + 1
    assert snapshot.retrieved_spans == ()
    assert registry.counters.get(IdKind.SPAN, 0) == 0
    assert registry.counters.get(IdKind.EVIDENCE, 0) == 0


def test_locked_raw_object_is_rehashed_before_allocation(
    promotion_case: PromotionCase,
) -> None:
    lock, _ = load_corpus_lock(promotion_case.review_root)
    raw_path = promotion_case.review_root / lock.papers[0].raw_md_path
    raw_path.chmod(0o644)
    raw_path.write_text("corrupted synthetic content\n", encoding="utf-8")

    with pytest.raises(CorpusIntegrityError):
        promote_retrieval_candidates(
            promotion_case.review_root, _assessed_request(promotion_case.ledger)
        )
    generation, snapshot, registry = promotion_case.store.load_current()
    assert generation == promotion_case.ledger.source_generation
    assert snapshot.retrieved_spans == ()
    assert registry.counters.get(IdKind.SPAN, 0) == 0


def test_locked_validation_rejects_forged_section_before_allocation(
    promotion_case: PromotionCase,
) -> None:
    payload = promotion_case.ledger.model_dump(mode="json")
    payload["raw_hits"][0]["section"] = "Forged section"
    forged = RetrievalLedger.model_validate(payload)

    with pytest.raises(RetrievalPromotionError, match="section"):
        promote_retrieval_candidates(
            promotion_case.review_root, _assessed_request(forged)
        )
    _, snapshot, registry = promotion_case.store.load_current()
    assert snapshot.retrieved_spans == ()
    assert registry.counters.get(IdKind.SPAN, 0) == 0


def test_locked_validation_rejects_nondeterministic_source_context_before_allocation(
    promotion_case: PromotionCase,
) -> None:
    payload = promotion_case.ledger.model_dump(mode="json")
    hit = payload["raw_hits"][0]
    assert hit["context_text"] != hit["source_text"]
    hit.update(
        {
            "context_text": hit["source_text"],
            "context_start_offset": hit["start_offset"],
            "context_end_offset": hit["end_offset"],
            "context_utf8_hash": hash_bytes(hit["source_text"].encode("utf-8")),
        }
    )
    forged = RetrievalLedger.model_validate(payload)

    with pytest.raises(RetrievalPromotionError, match="context differs"):
        promote_retrieval_candidates(
            promotion_case.review_root, _assessed_request(forged)
        )
    _, snapshot, registry = promotion_case.store.load_current()
    assert snapshot.retrieved_spans == ()
    assert registry.counters.get(IdKind.SPAN, 0) == 0
    assert registry.counters.get(IdKind.EVIDENCE, 0) == 0


@pytest.mark.parametrize(
    ("status", "canonical_span_id", "evidence", "match"),
    [
        ("assessed", None, None, "requires exactly one evidence"),
        ("duplicate", None, None, "requires a canonical span"),
        ("redundant", None, None, "requires a canonical span or reason"),
        ("excluded_by_budget", None, None, "ledger-only"),
        ("invalid_locator", None, None, "ledger-only"),
        ("duplicate", "R0001", _evidence(), "cannot carry evidence"),
    ],
)
def test_decision_contract_prevents_orphan_or_ledger_only_promotion(
    status: str,
    canonical_span_id: str | None,
    evidence: CoupledEvidenceProposal | None,
    match: str,
) -> None:
    with pytest.raises(ValidationError, match=match):
        RetrievalPromotionDecision(
            candidate_key="sha256:" + "1" * 64,
            status=status,
            canonical_span_id=canonical_span_id,
            evidence=evidence,
        )


def test_decisions_must_cover_exactly_selected_candidates(
    promotion_case: PromotionCase,
) -> None:
    with pytest.raises(ValidationError, match="cover exactly"):
        RetrievalPromotionRequest(ledger=promotion_case.ledger, decisions=())


def test_coupled_evidence_text_is_bounded() -> None:
    payload = _evidence().model_dump(mode="json")
    payload["evidence_summary"] = "x" * 16_385
    with pytest.raises(ValidationError, match="at most 16384"):
        CoupledEvidenceProposal.model_validate(payload)


@pytest.mark.parametrize("field", ["evidence_summary", "assessment_note"])
def test_coupled_evidence_required_text_is_nonempty(field: str) -> None:
    payload = _evidence().model_dump(mode="json")
    payload[field] = ""
    with pytest.raises(ValidationError, match="at least 1"):
        CoupledEvidenceProposal.model_validate(payload)


def test_decision_order_cannot_change_id_allocation_order(
    promotion_case: PromotionCase,
) -> None:
    candidates, ledger = _canonical_retrieval(
        promotion_case,
        "Alpha lattice overflow",
        generation=promotion_case.ledger.source_generation,
        top_k=2,
    )
    assert len(candidates) == 2
    first, second = candidates
    decisions = tuple(
        RetrievalPromotionDecision(
            candidate_key=key,
            status="assessed",
            evidence=_evidence(),
        )
        for key in reversed(ledger.selected_candidate_keys)
    )

    result = promote_retrieval_candidates(
        promotion_case.review_root,
        RetrievalPromotionRequest(ledger=ledger, decisions=decisions),
    )
    assert result.span_ids == {
        first.candidate_key: "R0001",
        second.candidate_key: "R0002",
    }
    assert result.evidence_ids == {
        first.candidate_key: "E0001",
        second.candidate_key: "E0002",
    }
    canonical_order = tuple(reversed(decisions))
    reused = promote_retrieval_candidates(
        promotion_case.review_root,
        RetrievalPromotionRequest(ledger=ledger, decisions=canonical_order),
    )
    assert reused.commit_performed is False
    assert reused.reused_generation == result.generation
    assert reused.request_hash == result.request_hash
    assert promotion_case.store.current_generation() == result.generation


def test_sequential_identical_request_reuses_exact_winner_without_new_artifact(
    promotion_case: PromotionCase,
) -> None:
    request = _assessed_request(promotion_case.ledger)
    first = promote_retrieval_candidates(promotion_case.review_root, request)
    generation_paths = sorted(promotion_case.store.generations_dir.iterdir())

    second = promote_retrieval_candidates(promotion_case.review_root, request)

    assert first.commit_performed is True
    assert second.commit_performed is False
    assert second.generation == first.generation
    assert second.reused_generation == first.generation
    assert second.span_ids == first.span_ids
    assert second.evidence_ids == first.evidence_ids
    assert second.promotion_record_path == first.promotion_record_path
    assert sorted(promotion_case.store.generations_dir.iterdir()) == generation_paths


def test_identical_request_reuses_winner_after_unrelated_generation(
    promotion_case: PromotionCase,
) -> None:
    request = _assessed_request(promotion_case.ledger)
    first = promote_retrieval_candidates(promotion_case.review_root, request)
    unrelated = promotion_case.store.commit(
        base_generation=first.generation,
        dependencies={},
        promotion=lambda snapshot, registry: PromotionPayload(
            snapshot, registry, {}
        ),
    )
    generation_paths = sorted(promotion_case.store.generations_dir.iterdir())

    reused = promote_retrieval_candidates(promotion_case.review_root, request)

    assert reused.commit_performed is False
    assert reused.generation == unrelated.generation
    assert reused.reused_generation == first.generation
    assert reused.promotion_record_path == first.promotion_record_path
    assert sorted(promotion_case.store.generations_dir.iterdir()) == generation_paths


def test_noncanonical_query_text_cannot_be_promoted(
    promotion_case: PromotionCase,
) -> None:
    _, snapshot, _ = promotion_case.store.load_current()
    query = snapshot.retrieval_queries[0]
    candidates, ledger = UnifiedRetrievalCoordinator(
        DeterministicTextRetriever(promotion_case.review_root)
    ).retrieve("changes load", query.query_id, query.intent, top_k=1)
    assert candidates

    with pytest.raises(RetrievalPromotionError, match="query text differs"):
        promote_retrieval_candidates(
            promotion_case.review_root, _assessed_request(ledger)
        )
    assert promotion_case.store.current_generation() == ledger.source_generation


def test_ledger_selection_must_match_deterministic_budget_ranking(
    promotion_case: PromotionCase,
) -> None:
    ledger = _ledger_with_ledger_only_hits(promotion_case)
    payload = ledger.model_dump(mode="json")
    payload["selected_candidate_keys"], payload[
        "excluded_by_budget_candidate_keys"
    ] = (
        payload["excluded_by_budget_candidate_keys"],
        payload["selected_candidate_keys"],
    )

    with pytest.raises(ValidationError, match="deterministic ranking"):
        RetrievalLedger.model_validate(payload)


def test_valid_candidate_score_is_recomputed_before_promotion(
    promotion_case: PromotionCase,
) -> None:
    payload = promotion_case.ledger.model_dump(mode="json")
    payload["raw_hits"][0]["score"] = 0.5
    forged = RetrievalLedger.model_validate(payload)

    with pytest.raises(RetrievalPromotionError, match="score differs"):
        promote_retrieval_candidates(
            promotion_case.review_root, _assessed_request(forged)
        )


def test_promotion_rejects_omitted_backend_candidate_before_allocation(
    promotion_case: PromotionCase,
) -> None:
    _, complete = _canonical_retrieval(
        promotion_case,
        "Alpha lattice overflow",
        generation=promotion_case.ledger.source_generation,
    )
    assert complete.invalid_candidates == 1
    payload = complete.model_dump(mode="json")
    payload["raw_hits"] = payload["raw_hits"][:-1]
    payload["total_candidates"] -= 1
    payload["invalid_candidates"] -= 1
    omitted = RetrievalLedger.model_validate(payload)

    with pytest.raises(RetrievalPromotionError, match="source-generation replay"):
        promote_retrieval_candidates(
            promotion_case.review_root, _assessed_request(omitted)
        )
    _, snapshot, registry = promotion_case.store.load_current()
    assert snapshot.retrieved_spans == ()
    assert registry.counters.get(IdKind.SPAN, 0) == 0


def test_promotion_rejects_fabricated_candidate_before_allocation(
    promotion_case: PromotionCase,
) -> None:
    fabricated = _ledger_with_ledger_only_hits(promotion_case)

    with pytest.raises(RetrievalPromotionError, match="source-generation replay"):
        promote_retrieval_candidates(
            promotion_case.review_root, _assessed_request(fabricated)
        )
    _, snapshot, registry = promotion_case.store.load_current()
    assert snapshot.retrieved_spans == ()
    assert registry.counters.get(IdKind.SPAN, 0) == 0


def test_promotion_rejects_forged_backend_truncation_count(
    promotion_case: PromotionCase,
) -> None:
    payload = promotion_case.ledger.model_dump(mode="json")
    payload["text_truncated_valid_candidates"] = 1
    forged = RetrievalLedger.model_validate(payload)

    with pytest.raises(RetrievalPromotionError, match="source-generation replay"):
        promote_retrieval_candidates(
            promotion_case.review_root, _assessed_request(forged)
        )


def test_promotion_replay_includes_generation_owned_graph_backend(
    promotion_case: PromotionCase,
) -> None:
    snapshot, _ = promotion_case.store.load_generation(
        promotion_case.ledger.source_generation
    )
    query = snapshot.retrieval_queries[0]
    candidates, text_only = UnifiedRetrievalCoordinator(
        DeterministicTextRetriever(promotion_case.review_root)
    ).retrieve(query.query_text, query.query_id, query.intent, top_k=1)
    assert candidates[0].origin is CandidateHitOrigin.TEXT_BASELINE
    assert promotion_case.candidates[0].origin is CandidateHitOrigin.BOTH

    with pytest.raises(RetrievalPromotionError, match="source-generation replay"):
        promote_retrieval_candidates(
            promotion_case.review_root, _assessed_request(text_only)
        )


def test_retrieval_ledger_rejects_reordered_complete_candidates(
    promotion_case: PromotionCase,
) -> None:
    _, complete = _canonical_retrieval(
        promotion_case,
        "Alpha lattice overflow",
        generation=promotion_case.ledger.source_generation,
    )
    payload = complete.model_dump(mode="json")
    payload["raw_hits"] = list(reversed(payload["raw_hits"]))

    with pytest.raises(ValidationError, match="deterministic order"):
        RetrievalLedger.model_validate(payload)


def test_reuse_rejects_generations_path_swap_instead_of_following_replacement(
    promotion_case: PromotionCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _assessed_request(promotion_case.ledger)
    promote_retrieval_candidates(promotion_case.review_root, request)
    generations = promotion_case.store.generations_dir
    retained = generations.with_name("generations-retained")
    original_snapshot = retrieval_promotion_module._validated_auxiliary_snapshot
    swapped = False

    def swap_then_read(root, requested):
        nonlocal swapped
        generations.rename(retained)
        generations.mkdir()
        swapped = True
        return original_snapshot(root, requested)

    monkeypatch.setattr(
        retrieval_promotion_module,
        "_validated_auxiliary_snapshot",
        swap_then_read,
    )
    try:
        with pytest.raises(UnsafeRepositoryEntryError, match="layout changed"):
            promote_retrieval_candidates(promotion_case.review_root, request)
    finally:
        if swapped:
            generations.rmdir()
            retained.rename(generations)

    assert promotion_case.store.current_generation() == request.ledger.source_generation + 1


@pytest.mark.parametrize("change", ["decision", "ledger"])
def test_stale_changed_request_cannot_reuse_prior_winner(
    promotion_case: PromotionCase,
    change: str,
) -> None:
    original = _assessed_request(promotion_case.ledger)
    first = promote_retrieval_candidates(promotion_case.review_root, original)
    if change == "decision":
        changed = RetrievalPromotionRequest(
            ledger=promotion_case.ledger,
            decisions=(
                RetrievalPromotionDecision(
                    candidate_key=promotion_case.ledger.selected_candidate_keys[0],
                    status="redundant",
                    reason="Changed synthetic scientific decision.",
                ),
            ),
        )
    else:
        payload = promotion_case.ledger.model_dump(mode="json")
        payload["raw_hits"][0]["score"] = 0.75
        changed_ledger = RetrievalLedger.model_validate(payload)
        changed = _assessed_request(changed_ledger)

    with pytest.raises(StaleSnapshotError):
        promote_retrieval_candidates(promotion_case.review_root, changed)
    assert promotion_case.store.current_generation() == first.generation


def test_same_base_concurrency_has_one_winner_and_no_id_gap(
    promotion_case: PromotionCase,
) -> None:
    request = _assessed_request(promotion_case.ledger)

    def run():
        return promote_retrieval_candidates(promotion_case.review_root, request)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run) for _ in range(2)]
    results = [future.result() for future in futures]
    committed = [item for item in results if item.commit_performed]
    reused = [item for item in results if not item.commit_performed]
    assert len(committed) == len(reused) == 1
    assert reused[0].reused_generation == committed[0].generation
    _, snapshot, registry = promotion_case.store.load_current()
    assert [item.span_id for item in snapshot.retrieved_spans] == ["R0001"]
    assert [item.evidence_id for item in snapshot.evidence_records] == ["E0001"]
    assert registry.counters[IdKind.SPAN] == 1
    assert registry.counters[IdKind.EVIDENCE] == 1


def test_duplicate_promotion_has_disposition_but_no_orphan_evidence(
    promotion_case: PromotionCase,
) -> None:
    first = promote_retrieval_candidates(
        promotion_case.review_root, _assessed_request(promotion_case.ledger)
    )
    _, snapshot, _ = promotion_case.store.load_current()
    query = snapshot.retrieval_queries[0]
    candidates, ledger = UnifiedRetrievalCoordinator.from_generation(
        promotion_case.review_root
    ).retrieve(query.query_text, query.query_id, query.intent, top_k=1)
    request = RetrievalPromotionRequest(
        ledger=ledger,
        decisions=(
            RetrievalPromotionDecision(
                candidate_key=candidates[0].candidate_key,
                status="duplicate",
                canonical_span_id=next(iter(first.span_ids.values())),
            ),
        ),
    )

    second = promote_retrieval_candidates(promotion_case.review_root, request)
    _, final, registry = promotion_case.store.load_current()
    assert second.span_ids == {candidates[0].candidate_key: "R0002"}
    assert second.evidence_ids == {}
    assert [item.status.value for item in final.retrieval_dispositions] == [
        "assessed",
        "duplicate",
    ]
    assert [item.evidence_id for item in final.evidence_records] == ["E0001"]
    assert registry.counters[IdKind.SPAN] == 2
    assert registry.counters[IdKind.EVIDENCE] == 1
    final.validate_repository()


def test_duplicate_target_must_be_the_same_exact_source_span(
    promotion_case: PromotionCase,
) -> None:
    first = promote_retrieval_candidates(
        promotion_case.review_root, _assessed_request(promotion_case.ledger)
    )
    current_generation = promotion_case.store.current_generation()
    candidates, ledger = _canonical_retrieval(
        promotion_case,
        "Alpha lattice overflow",
        generation=current_generation,
    )
    different = candidates[0]
    request = RetrievalPromotionRequest(
        ledger=ledger,
        decisions=(
            RetrievalPromotionDecision(
                candidate_key=different.candidate_key,
                status="duplicate",
                canonical_span_id=next(iter(first.span_ids.values())),
            ),
        ),
    )

    with pytest.raises(RetrievalPromotionError, match="same locked source span"):
        promote_retrieval_candidates(promotion_case.review_root, request)
    assert promotion_case.store.current_generation() == current_generation


def test_existing_canonical_locator_cannot_be_assessed_again(
    promotion_case: PromotionCase,
) -> None:
    first = promote_retrieval_candidates(
        promotion_case.review_root, _assessed_request(promotion_case.ledger)
    )
    _, snapshot, registry_before = promotion_case.store.load_current()
    query = snapshot.retrieval_queries[0]
    _, ledger = UnifiedRetrievalCoordinator.from_generation(
        promotion_case.review_root
    ).retrieve(query.query_text, query.query_id, query.intent, top_k=1)

    with pytest.raises(RetrievalPromotionError, match="already canonicalized"):
        promote_retrieval_candidates(
            promotion_case.review_root, _assessed_request(ledger)
        )

    generation, snapshot_after, registry_after = promotion_case.store.load_current()
    assert generation == first.generation
    assert snapshot_after == snapshot
    assert registry_after == registry_before


def test_existing_canonical_locator_cannot_be_called_redundant(
    promotion_case: PromotionCase,
) -> None:
    first = promote_retrieval_candidates(
        promotion_case.review_root, _assessed_request(promotion_case.ledger)
    )
    _, snapshot, registry_before = promotion_case.store.load_current()
    query = snapshot.retrieval_queries[0]
    _, ledger = UnifiedRetrievalCoordinator.from_generation(
        promotion_case.review_root
    ).retrieve(query.query_text, query.query_id, query.intent, top_k=1)
    request = RetrievalPromotionRequest(
        ledger=ledger,
        decisions=(
            RetrievalPromotionDecision(
                candidate_key=ledger.selected_candidate_keys[0],
                status="redundant",
                reason="This must not hide an exact canonical duplicate.",
            ),
        ),
    )

    with pytest.raises(RetrievalPromotionError, match="must use duplicate"):
        promote_retrieval_candidates(promotion_case.review_root, request)

    generation, snapshot_after, registry_after = promotion_case.store.load_current()
    assert generation == first.generation
    assert snapshot_after == snapshot
    assert registry_after == registry_before


def test_new_assessment_rejects_stale_claim_paper_aggregate(
    promotion_case: PromotionCase,
) -> None:
    first = promote_retrieval_candidates(
        promotion_case.review_root, _assessed_request(promotion_case.ledger)
    )

    def add_aggregate(snapshot, registry):
        evidence = snapshot.evidence_records[0]
        aggregate = ClaimPaperEvidence(
            claim_paper_evidence_id=(
                f"CPE-{evidence.claim_id}-{evidence.paper_id}"
            ),
            claim_id=evidence.claim_id,
            paper_id=evidence.paper_id,
            evidence_ids=[evidence.evidence_id],
            relation_to_candidate="supports",
            component_relations=ComponentRelations(
                supports=[evidence.evidence_id]
            ),
            strength="low",
            within_paper_consistency="consistent",
            assessment_note="Synthetic complete aggregate before new evidence.",
        )
        return PromotionPayload(
            snapshot.model_copy(update={"claim_paper_evidence": (aggregate,)}),
            registry,
            {},
        )

    aggregate_commit = promotion_case.store.commit(
        base_generation=first.generation,
        dependencies={},
        promotion=add_aggregate,
    )
    _, ledger = _canonical_retrieval(
        promotion_case,
        "Alpha lattice overflow",
        generation=aggregate_commit.generation,
    )

    with pytest.raises(RetrievalPromotionError, match="claim-paper aggregate"):
        promote_retrieval_candidates(
            promotion_case.review_root, _assessed_request(ledger)
        )

    generation, _, registry = promotion_case.store.load_current()
    assert generation == aggregate_commit.generation
    assert registry.counters[IdKind.SPAN] == 1
    assert registry.counters[IdKind.EVIDENCE] == 1


def test_new_assessment_rejects_stale_downstream_claim_record(
    promotion_case: PromotionCase,
) -> None:
    first = promote_retrieval_candidates(
        promotion_case.review_root, _assessed_request(promotion_case.ledger)
    )

    def add_assessment(snapshot, registry):
        assessment = ClaimAssessment(
            claim_id=snapshot.candidate_claims[0].claim_id,
            aggregate_strength="low",
            evidence_sufficiency="sufficient",
            decision="RETAIN",
            rejection_basis=None,
            support_summary="Synthetic support summary.",
            contradiction_summary="No synthetic contradiction.",
            qualification_summary="Synthetic fixture boundary.",
            reason="Synthetic downstream record for freshness testing.",
        )
        return PromotionPayload(
            snapshot.model_copy(update={"claim_assessments": (assessment,)}),
            registry,
            {},
        )

    assessment_commit = promotion_case.store.commit(
        base_generation=first.generation,
        dependencies={},
        promotion=add_assessment,
    )
    _, ledger = _canonical_retrieval(
        promotion_case,
        "Alpha lattice overflow",
        generation=assessment_commit.generation,
    )

    with pytest.raises(RetrievalPromotionError, match="downstream claim records"):
        promote_retrieval_candidates(
            promotion_case.review_root, _assessed_request(ledger)
        )

    generation, _, registry = promotion_case.store.load_current()
    assert generation == assessment_commit.generation
    assert registry.counters[IdKind.SPAN] == 1
    assert registry.counters[IdKind.EVIDENCE] == 1


def test_new_assessment_rejects_downstream_proposition_reference(
    promotion_case: PromotionCase,
) -> None:
    snapshot, registry = promotion_case.store.load_generation(
        promotion_case.ledger.source_generation
    )
    query = next(
        item
        for item in snapshot.retrieval_queries
        if item.query_id == promotion_case.ledger.query_id
    )
    synthetic_descendant = snapshot.model_copy(
        update={
            "proposition_records": (
                SimpleNamespace(claim_ids=[query.claim_id]),
            )
        }
    )

    with pytest.raises(RetrievalPromotionError, match="downstream proposition"):
        _validate_decisions_against_snapshot(
            _assessed_request(promotion_case.ledger),
            synthetic_descendant,
            query,
        )

    assert registry.counters.get(IdKind.SPAN, 0) == 0
    assert registry.counters.get(IdKind.EVIDENCE, 0) == 0
