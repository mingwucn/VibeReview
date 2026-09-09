"""Locked promotion of verified runtime retrieval candidates.

This module is deliberately library/runtime infrastructure.  Its request,
ledger, and promotion-record models are not persistent scientific contracts.
Canonical ``RetrievedSpan``, ``RetrievalDisposition``, and ``EvidenceRecord``
objects still cross the normal immutable-generation boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
import math
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from vibereview.enums import EvidenceRelation, RetrievalDispositionStatus
from vibereview.ids import EvidenceId, QueryId, Sha256, SpanId
from vibereview.models import (
    EvidenceQuality,
    EvidenceRecord,
    RetrievalDisposition,
    RetrievalMetadata,
    RetrievalQuery,
    RetrievedSpan,
    SpanLocator,
)
from vibereview.runtime.hashing import canonical_json_bytes, hash_bytes, hash_json
from vibereview.runtime.records import RuntimeModel
from vibereview.runtime.registry import CanonicalIdRegistry, IdKind
from vibereview.runtime.repository import (
    AuxiliaryStagingWriter,
    CrashPoint,
    GenerationStore,
    PromotionPayload,
    StaleSnapshotError,
    _validated_auxiliary_snapshot,
)
from vibereview.runtime.state import RepositorySnapshot

from .models import CorpusIntegrityError
from .retrieval import (
    CandidateHitOrigin,
    RawCandidateHit,
    RetrievalLedger,
    UnifiedRetrievalCoordinator,
    _bounded_context_slice,
    _candidate_key,
    _heading_index,
    _query_terms,
    _query_text_hash,
    _section_at,
)
from .selection import load_corpus_lock, verify_corpus_lock


PROMOTION_RECORD_VERSION = "1"
PROMOTION_RECORD_ROOT = "retrieval/promotions"
MAX_PROMOTION_TEXT_CHARS = 16_384
MAX_EVIDENCE_LIMITATIONS = 100
MAX_LIMITATION_CHARS = 4_096


class RetrievalPromotionError(ValueError):
    """A runtime retrieval batch cannot be promoted canonically."""


class _ExactWinnerNotReusable(Exception):
    pass


class _FrozenRuntimeModel(RuntimeModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CoupledEvidenceProposal(_FrozenRuntimeModel):
    """ID-less evidence fields whose canonical references are derived in Python."""

    relation_to_candidate: EvidenceRelation
    evidence_summary: str = Field(min_length=1, max_length=MAX_PROMOTION_TEXT_CHARS)
    quality: EvidenceQuality
    assessment_note: str = Field(min_length=1, max_length=MAX_PROMOTION_TEXT_CHARS)

    @model_validator(mode="after")
    def _bounded_quality_diagnostics(self) -> "CoupledEvidenceProposal":
        if len(self.quality.limitations) > MAX_EVIDENCE_LIMITATIONS:
            raise ValueError("evidence proposal has too many limitations")
        if any(len(item) > MAX_LIMITATION_CHARS for item in self.quality.limitations):
            raise ValueError("evidence proposal limitation exceeds its text budget")
        return self


class RetrievalPromotionDecision(_FrozenRuntimeModel):
    """One disposition decision for a selected runtime candidate."""

    candidate_key: Sha256
    status: RetrievalDispositionStatus
    reason: str | None = Field(default=None, max_length=MAX_PROMOTION_TEXT_CHARS)
    canonical_span_id: SpanId | None = None
    evidence: CoupledEvidenceProposal | None = None

    @model_validator(mode="after")
    def _coupled_cardinality(self) -> "RetrievalPromotionDecision":
        if self.status is RetrievalDispositionStatus.ASSESSED:
            if self.canonical_span_id is not None:
                raise ValueError("assessed candidate cannot name a canonical span")
            if self.evidence is None:
                raise ValueError("assessed candidate requires exactly one evidence proposal")
        elif self.status is RetrievalDispositionStatus.DUPLICATE:
            if self.canonical_span_id is None:
                raise ValueError("duplicate candidate requires a canonical span")
            if self.evidence is not None:
                raise ValueError("duplicate candidate cannot carry evidence")
        elif self.status is RetrievalDispositionStatus.REDUNDANT:
            if self.canonical_span_id is None and not self.reason:
                raise ValueError("redundant candidate requires a canonical span or reason")
            if self.evidence is not None:
                raise ValueError("redundant candidate cannot carry evidence")
        else:
            raise ValueError(
                "invalid-locator and over-budget candidates must remain ledger-only"
            )
        return self


class RetrievalPromotionRequest(_FrozenRuntimeModel):
    """A detached, bounded ledger plus decisions for exactly its selected hits."""

    ledger: RetrievalLedger
    decisions: tuple[RetrievalPromotionDecision, ...]

    @model_validator(mode="after")
    def _decisions_cover_selected_candidates(self) -> "RetrievalPromotionRequest":
        decision_keys = [item.candidate_key for item in self.decisions]
        if len(decision_keys) != len(set(decision_keys)):
            raise ValueError("retrieval promotion decision candidate keys must be unique")
        if set(decision_keys) != set(self.ledger.selected_candidate_keys):
            raise ValueError(
                "retrieval promotion decisions must cover exactly the selected candidates"
            )
        return self


class RetrievalPromotionRecord(_FrozenRuntimeModel):
    """Generation-owned audit record for promoted and ledger-only candidates."""

    record_version: Literal["1"] = PROMOTION_RECORD_VERSION
    request_hash: Sha256
    ledger_hash: Sha256
    source_generation: int = Field(ge=0)
    committed_generation: int = Field(ge=1)
    query_id: QueryId
    corpus_lock_hash: Sha256
    ledger: RetrievalLedger
    decisions: tuple[RetrievalPromotionDecision, ...]
    span_ids: dict[Sha256, SpanId]
    evidence_ids: dict[Sha256, EvidenceId]

    @model_validator(mode="after")
    def _record_is_self_consistent(self) -> "RetrievalPromotionRecord":
        selected = set(self.ledger.selected_candidate_keys)
        decision_keys = [item.candidate_key for item in self.decisions]
        if (
            self.source_generation != self.ledger.source_generation
            or self.query_id != self.ledger.query_id
            or self.corpus_lock_hash != self.ledger.corpus_lock_hash
            or self.committed_generation != self.source_generation + 1
        ):
            raise ValueError("retrieval promotion record and ledger identity disagree")
        if self.ledger_hash != hash_bytes(_ledger_bytes(self.ledger)):
            raise ValueError("retrieval promotion record ledger hash disagrees")
        expected_request_hash = hash_json(
            {
                "ledger": self.ledger.model_dump(mode="json"),
                "decisions": [
                    item.model_dump(mode="json") for item in self.decisions
                ],
            }
        )
        if self.request_hash != expected_request_hash:
            raise ValueError("retrieval promotion record request hash disagrees")
        if set(self.span_ids) != selected:
            raise ValueError("retrieval promotion record lacks a selected span allocation")
        if len(decision_keys) != len(set(decision_keys)) or set(decision_keys) != selected:
            raise ValueError("retrieval promotion record decisions are incomplete")
        if len(self.span_ids.values()) != len(set(self.span_ids.values())):
            raise ValueError("retrieval promotion record repeats a canonical span ID")
        assessed = {
            item.candidate_key
            for item in self.decisions
            if item.status is RetrievalDispositionStatus.ASSESSED
        }
        if set(self.evidence_ids) != assessed:
            raise ValueError("retrieval promotion record evidence allocations disagree")
        if len(self.evidence_ids.values()) != len(set(self.evidence_ids.values())):
            raise ValueError("retrieval promotion record repeats a canonical evidence ID")
        return self


class RetrievalPromotionResult(_FrozenRuntimeModel):
    generation: int = Field(ge=1)
    commit_performed: bool
    reused_generation: int | None = None
    promotion_record_path: str
    promotion_record_hash: Sha256
    request_hash: Sha256
    ledger_hash: Sha256
    span_ids: dict[Sha256, SpanId]
    evidence_ids: dict[Sha256, EvidenceId]

    @model_validator(mode="after")
    def _reuse_fields_agree(self) -> "RetrievalPromotionResult":
        if self.commit_performed and self.reused_generation is not None:
            raise ValueError("a committed promotion cannot report a reused generation")
        if not self.commit_performed and self.reused_generation is None:
            raise ValueError("a reused promotion must report its committed generation")
        return self


def _ledger_bytes(ledger: RetrievalLedger) -> bytes:
    return canonical_json_bytes(ledger.model_dump(mode="json")) + b"\n"


def _record_bytes(record: RetrievalPromotionRecord) -> bytes:
    return canonical_json_bytes(record.model_dump(mode="json")) + b"\n"


def _record_relative_path(query_id: str, request_hash: str) -> str:
    digest = request_hash.removeprefix("sha256:")
    return f"{PROMOTION_RECORD_ROOT}/{query_id}/{digest}.json"


def _allocated_maps(
    allocated_ids: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    spans = {
        key.removeprefix("span:"): value
        for key, value in allocated_ids.items()
        if key.startswith("span:")
    }
    evidence = {
        key.removeprefix("evidence:"): value
        for key, value in allocated_ids.items()
        if key.startswith("evidence:")
    }
    return spans, evidence


def _canonical_request(
    request: RetrievalPromotionRequest,
) -> RetrievalPromotionRequest:
    detached = RetrievalPromotionRequest.model_validate(
        request.model_dump(mode="json")
    )
    by_key = {item.candidate_key: item for item in detached.decisions}
    return RetrievalPromotionRequest(
        ledger=detached.ledger,
        decisions=tuple(
            by_key[candidate_key]
            for candidate_key in detached.ledger.selected_candidate_keys
        ),
    )


def _span_from_candidate(hit: RawCandidateHit, span_id: str) -> RetrievedSpan:
    assert hit.start_offset is not None and hit.end_offset is not None
    assert hit.source_span_hash is not None
    return RetrievedSpan(
        span_id=span_id,
        paper_id=hit.paper_id,
        locator=SpanLocator(
            raw_md_path=hit.raw_md_path,
            page=None,
            section=hit.section,
            start_offset=hit.start_offset,
            end_offset=hit.end_offset,
            source_span_hash=hit.source_span_hash,
        ),
        source_text=hit.source_text,
        retrieval=RetrievalMetadata(
            query_id=hit.query_id,
            intent=hit.intent,
            retrieval_score=hit.score,
        ),
    )


def _candidate_source_identity(
    hit: RawCandidateHit,
) -> tuple[str, str, int, int, str, str]:
    if (
        hit.start_offset is None
        or hit.end_offset is None
        or hit.source_span_hash is None
    ):
        raise AssertionError("canonical source identity requires a valid candidate")
    return (
        hit.paper_id,
        hit.raw_md_path,
        hit.start_offset,
        hit.end_offset,
        hit.source_span_hash,
        hit.source_text,
    )


def _canonical_source_identity(
    span: RetrievedSpan,
) -> tuple[str, str, int, int, str, str]:
    return (
        span.paper_id,
        span.locator.raw_md_path,
        span.locator.start_offset,
        span.locator.end_offset,
        span.locator.source_span_hash,
        span.source_text,
    )


def _disposition_from_decision(
    decision: RetrievalPromotionDecision,
    span_id: str,
) -> RetrievalDisposition:
    return RetrievalDisposition(
        span_id=span_id,
        status=decision.status,
        reason=decision.reason,
        canonical_span_id=decision.canonical_span_id,
    )


def _evidence_from_decision(
    decision: RetrievalPromotionDecision,
    *,
    evidence_id: str,
    span_id: str,
    hit: RawCandidateHit,
    query: RetrievalQuery,
) -> EvidenceRecord:
    if decision.evidence is None:
        raise AssertionError("evidence allocation requires an assessed decision")
    return EvidenceRecord(
        evidence_id=evidence_id,
        claim_id=query.claim_id,
        retrieved_span_id=span_id,
        paper_id=hit.paper_id,
        relation_to_candidate=decision.evidence.relation_to_candidate,
        evidence_summary=decision.evidence.evidence_summary,
        quality=decision.evidence.quality,
        assessment_note=decision.evidence.assessment_note,
    )


def _validate_locked_request(
    review_root: Path,
    request: RetrievalPromotionRequest,
    snapshot: RepositorySnapshot,
) -> tuple[
    list[tuple[RawCandidateHit, RetrievalPromotionDecision]],
    RetrievalQuery,
]:
    ledger = request.ledger
    try:
        lock, observed_lock_hash = load_corpus_lock(
            review_root, generation=ledger.source_generation
        )
        verified_bytes = verify_corpus_lock(review_root, lock, snapshot)
    except CorpusIntegrityError:
        raise
    except (OSError, ValueError) as exc:
        raise CorpusIntegrityError("retrieval corpus failed locked verification") from exc
    if observed_lock_hash != ledger.corpus_lock_hash:
        raise RetrievalPromotionError("retrieval ledger corpus lock is stale")

    queries = [
        item for item in snapshot.retrieval_queries if item.query_id == ledger.query_id
    ]
    if len(queries) != 1:
        raise RetrievalPromotionError("retrieval ledger query is not uniquely canonical")
    query = queries[0]
    if ledger.query_text_hash != _query_text_hash(query.query_text):
        raise RetrievalPromotionError(
            "retrieval ledger query text differs from the canonical query"
        )
    query_terms = _query_terms(
        query.query_text, max_terms=ledger.max_query_terms
    )
    if not query_terms and ledger.valid_candidates:
        raise RetrievalPromotionError(
            "canonical retrieval query has no usable terms for valid candidates"
    )
    papers = {item.paper_id: item for item in snapshot.papers}
    locked_papers = {item.paper_id: item for item in lock.papers}

    # Validate the whole ledger before allocating any identifier.  Ledger-only
    # invalid and over-budget candidates therefore retain authenticated corpus
    # provenance without entering canonical scientific collections.
    for hit in ledger.raw_hits:
        if hit.intent != query.intent:
            raise RetrievalPromotionError("candidate intent differs from canonical query")
        paper = papers.get(hit.paper_id)
        locked = locked_papers.get(hit.paper_id)
        if paper is None or locked is None or hit.paper_id not in verified_bytes:
            raise RetrievalPromotionError("candidate paper is outside the locked corpus")
        if hit.raw_md_path != paper.raw_md_path or hit.raw_md_path != locked.raw_md_path:
            raise RetrievalPromotionError("candidate raw path differs from locked Paper")
        if not math.isfinite(hit.score):
            raise RetrievalPromotionError("candidate retrieval score must be finite")
        expected_key = _candidate_key(
            paper_id=hit.paper_id,
            raw_md_hash=paper.raw_md_hash,
            query_id=hit.query_id,
            query_text_hash=ledger.query_text_hash,
            source_text=hit.source_text,
            start_offset=hit.start_offset,
            end_offset=hit.end_offset,
            origin=hit.origin,
        )
        if expected_key != hit.candidate_key:
            raise RetrievalPromotionError("candidate identity does not match locked corpus")
        if not hit.is_valid:
            continue
        text_score = sum(
            term in hit.source_text.casefold() for term in query_terms
        ) / len(query_terms)
        expected_score = (
            text_score
            if hit.origin is CandidateHitOrigin.TEXT_BASELINE
            else 0.9
            if hit.origin is CandidateHitOrigin.GRAPH
            else max(text_score, 0.9)
        )
        if hit.score != expected_score:
            raise RetrievalPromotionError(
                "candidate score differs from deterministic retrieval policy"
            )
        assert hit.start_offset is not None and hit.end_offset is not None
        assert hit.source_span_hash is not None
        try:
            raw_text = verified_bytes[hit.paper_id].decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise CorpusIntegrityError("locked raw Markdown is not UTF-8") from exc
        if raw_text[hit.start_offset : hit.end_offset] != hit.source_text:
            raise RetrievalPromotionError("candidate is not the exact locked source slice")
        if hash_bytes(hit.source_text.encode("utf-8")) != hit.source_span_hash:
            raise RetrievalPromotionError("candidate source-span digest changed")
        assert hit.context_text is not None
        assert hit.context_start_offset is not None
        assert hit.context_end_offset is not None
        assert hit.context_utf8_hash is not None
        expected_context = _bounded_context_slice(
            raw_text, hit.start_offset, hit.end_offset
        )
        if expected_context != (
            hit.context_start_offset,
            hit.context_end_offset,
            hit.context_text,
        ):
            raise RetrievalPromotionError(
                "candidate context differs from the deterministic locked source window"
            )
        if hash_bytes(hit.context_text.encode("utf-8")) != hit.context_utf8_hash:
            raise RetrievalPromotionError("candidate context UTF-8 digest changed")
        if _section_at(_heading_index(raw_text), hit.start_offset) != hit.section:
            raise RetrievalPromotionError("candidate section differs from locked source")

    # A field-by-field locator check cannot prove that a backend result was
    # actually emitted, nor that lower-ranked and invalid candidates were not
    # omitted.  Rebuild the canonical ensemble solely from generation-owned
    # bytes and require the complete bounded ledger to agree.
    _, expected_ledger = UnifiedRetrievalCoordinator.from_generation(
        review_root, generation=ledger.source_generation
    ).retrieve(
        query.query_text,
        query.query_id,
        query.intent,
        top_k=ledger.requested_top_k,
        max_query_terms=ledger.max_query_terms,
    )
    if expected_ledger != ledger:
        raise RetrievalPromotionError(
            "retrieval ledger differs from deterministic source-generation replay"
        )

    return _validate_decisions_against_snapshot(request, snapshot, query)


def _validate_decisions_against_snapshot(
    request: RetrievalPromotionRequest,
    snapshot: RepositorySnapshot,
    query: RetrievalQuery,
) -> tuple[
    list[tuple[RawCandidateHit, RetrievalPromotionDecision]],
    RetrievalQuery,
]:
    """Validate proposal decisions against an already authenticated ledger.

    The task-bound evidence adapter authenticates and detaches its ledger and
    source snapshot before an engine runs.  Keeping the decision-only checks in
    one helper lets proposal validation use that immutable view, while the
    authoritative writer-locked promotion still repeats the complete corpus
    and deterministic-retrieval verification above.
    """

    ledger = request.ledger
    hits = {
        item.candidate_key: item for item in ledger.raw_hits if item.is_valid
    }
    decisions = {item.candidate_key: item for item in request.decisions}

    validated: list[tuple[RawCandidateHit, RetrievalPromotionDecision]] = []
    existing_spans = {item.span_id: item for item in snapshot.retrieved_spans}
    occupied_sources = {
        _canonical_source_identity(item): item.span_id
        for item in snapshot.retrieved_spans
    }
    newly_assessed: list[RawCandidateHit] = []
    for candidate_key in ledger.selected_candidate_keys:
        hit = hits[candidate_key]
        decision = decisions[candidate_key]
        source_identity = _candidate_source_identity(hit)
        if (
            source_identity in occupied_sources
            and decision.status is not RetrievalDispositionStatus.DUPLICATE
        ):
            raise RetrievalPromotionError(
                "candidate source locator is already canonicalized and must use "
                "duplicate disposition or accepted-request reuse"
            )
        if decision.status is RetrievalDispositionStatus.ASSESSED:
            newly_assessed.append(hit)
        if decision.canonical_span_id is not None:
            canonical = existing_spans.get(decision.canonical_span_id)
            if canonical is None:
                raise RetrievalPromotionError("canonical span target does not exist")
            if canonical.paper_id != hit.paper_id:
                raise RetrievalPromotionError("canonical span target belongs to another paper")
            if (
                decision.status is RetrievalDispositionStatus.DUPLICATE
                and _canonical_source_identity(canonical) != source_identity
            ):
                raise RetrievalPromotionError(
                    "duplicate target is not the same locked source span"
                )
        occupied_sources.setdefault(source_identity, candidate_key)
        validated.append((hit, decision))

    if newly_assessed:
        if any(
            item.claim_id == query.claim_id
            for item in snapshot.claim_paper_evidence
        ):
            raise RetrievalPromotionError(
                "cannot assess new evidence after its claim-paper aggregate exists"
            )
        if any(
            item.claim_id == query.claim_id
            for collection in (
                snapshot.claim_assessments,
                snapshot.final_claim_validations,
                snapshot.claim_packets,
            )
            for item in collection
        ):
            raise RetrievalPromotionError(
                "cannot assess new evidence after downstream claim records exist"
            )
        if any(
            query.claim_id in item.claim_ids
            for item in snapshot.proposition_records
        ):
            raise RetrievalPromotionError(
                "cannot assess new evidence after a downstream proposition exists"
            )
    return validated, query


def _build_locked_promotion(
    review_root: Path,
    request: RetrievalPromotionRequest,
):
    def promote(
        snapshot: RepositorySnapshot,
        registry: CanonicalIdRegistry,
    ) -> PromotionPayload:
        validated, query = _validate_locked_request(review_root, request, snapshot)
        span_values, registry = (
            registry.allocate(IdKind.SPAN, len(validated))
            if validated
            else ([], registry)
        )
        assessed_count = sum(
            decision.status is RetrievalDispositionStatus.ASSESSED
            for _, decision in validated
        )
        evidence_values, registry = (
            registry.allocate(IdKind.EVIDENCE, assessed_count)
            if assessed_count
            else ([], registry)
        )
        evidence_iterator = iter(evidence_values)
        spans: list[RetrievedSpan] = []
        dispositions: list[RetrievalDisposition] = []
        evidence_records: list[EvidenceRecord] = []
        allocated: dict[str, str] = {}
        for (hit, decision), span_id in zip(validated, span_values, strict=True):
            span = _span_from_candidate(hit, span_id)
            spans.append(span)
            dispositions.append(_disposition_from_decision(decision, span_id))
            allocated[f"span:{hit.candidate_key}"] = span_id
            if decision.evidence is not None:
                evidence_id = next(evidence_iterator)
                evidence_records.append(
                    _evidence_from_decision(
                        decision,
                        evidence_id=evidence_id,
                        span_id=span_id,
                        hit=hit,
                        query=query,
                    )
                )
                allocated[f"evidence:{hit.candidate_key}"] = evidence_id
        try:
            next(evidence_iterator)
        except StopIteration:
            pass
        else:  # pragma: no cover - defensive allocation invariant
            raise AssertionError("unused evidence identifier allocation")
        promoted = snapshot.model_copy(
            update={
                "retrieved_spans": snapshot.retrieved_spans + tuple(spans),
                "retrieval_dispositions": (
                    snapshot.retrieval_dispositions + tuple(dispositions)
                ),
                "evidence_records": snapshot.evidence_records
                + tuple(evidence_records),
            }
        )
        return PromotionPayload(promoted, registry, allocated)

    return promote


def _try_reuse_exact_winner(
    store: GenerationStore,
    review_root: Path,
    request: RetrievalPromotionRequest,
    *,
    request_hash: str,
    ledger_hash: str,
    record_relative: str,
) -> RetrievalPromotionResult | None:
    """Reuse an exact accepted winner that remains intact in CURRENT."""

    source_generation = request.ledger.source_generation
    winner_generation = source_generation + 1
    with store.writer_lock() as locked:
        if locked.layout is None:  # pragma: no cover - context invariant
            raise AssertionError("writer lock has no retained repository layout")
        layout = locked.layout
        # A stale commit already ran recovery before its freshness check, but
        # repeat it under this retained layout so reuse never depends on an
        # unreconciled staging or complete-before-CURRENT transaction.
        store._recover_locked(layout)
        layout.verify()
        try:
            current_generation = store._current_generation_at(layout)
            if current_generation < winner_generation:
                raise _ExactWinnerNotReusable
            source_snapshot, source_registry = store._load_generation_at(
                layout, source_generation
            )
            winner_snapshot, winner_registry = store._load_generation_at(
                layout, winner_generation
            )
            current_snapshot, _ = store._load_generation_at(
                layout, current_generation
            )
            winner_path = store._generation_path_at(layout, winner_generation)
            _, auxiliary = _validated_auxiliary_snapshot(
                winner_path / "auxiliary", {record_relative}
            )
            layout.verify()
            observed_content = auxiliary[record_relative]
            record = RetrievalPromotionRecord.model_validate_json(observed_content)
            if (
                record.request_hash != request_hash
                or record.ledger_hash != ledger_hash
                or record.source_generation != source_generation
                or record.committed_generation != winner_generation
                or record.ledger != request.ledger
                or record.decisions != request.decisions
            ):
                raise _ExactWinnerNotReusable

            # Re-run the same locked validator and pure allocation from the
            # anchored source generation.  Reuse is accepted only when the
            # exact winner is the result of this request. Later generations
            # may add unrelated canonical state, but may not alter or remove
            # any object allocated by the accepted promotion.
            expected = _build_locked_promotion(review_root, request)(
                source_snapshot, source_registry
            )
            if (
                expected.snapshot != winner_snapshot
                or expected.registry != winner_registry
            ):
                raise _ExactWinnerNotReusable
            span_ids, evidence_ids = _allocated_maps(expected.allocated_ids)
            if record.span_ids != span_ids or record.evidence_ids != evidence_ids:
                raise _ExactWinnerNotReusable

            winner_spans = {
                item.span_id: item for item in winner_snapshot.retrieved_spans
            }
            current_spans = {
                item.span_id: item for item in current_snapshot.retrieved_spans
            }
            winner_dispositions = {
                item.span_id: item
                for item in winner_snapshot.retrieval_dispositions
            }
            current_dispositions = {
                item.span_id: item
                for item in current_snapshot.retrieval_dispositions
            }
            winner_evidence = {
                item.evidence_id: item for item in winner_snapshot.evidence_records
            }
            current_evidence = {
                item.evidence_id: item for item in current_snapshot.evidence_records
            }
            if any(
                current_spans.get(span_id) != winner_spans.get(span_id)
                or current_dispositions.get(span_id)
                != winner_dispositions.get(span_id)
                for span_id in span_ids.values()
            ) or any(
                current_evidence.get(evidence_id)
                != winner_evidence.get(evidence_id)
                for evidence_id in evidence_ids.values()
            ):
                raise _ExactWinnerNotReusable
            current_snapshot.validate_repository()
            result = RetrievalPromotionResult(
                generation=current_generation,
                commit_performed=False,
                reused_generation=record.committed_generation,
                promotion_record_path=(
                    f"state/generations/{winner_generation:06d}/auxiliary/"
                    f"{record_relative}"
                ),
                promotion_record_hash=hash_bytes(observed_content),
                request_hash=request_hash,
                ledger_hash=ledger_hash,
                span_ids=span_ids,
                evidence_ids=evidence_ids,
            )
        except (
            _ExactWinnerNotReusable,
            CorpusIntegrityError,
            OSError,
            ValueError,
            KeyError,
            AssertionError,
        ):
            # Do not let a path-based or descriptor-path failure hide a public
            # state/generations swap.  Verify the retained layout before
            # converting a bad/missing record into an ordinary stale result.
            layout.verify()
            return None
        layout.verify()
        return result


def _promote_retrieval_candidates_for_testing(
    review_root: Path,
    request: RetrievalPromotionRequest,
    *,
    crash_at: CrashPoint | None = None,
) -> RetrievalPromotionResult:
    """Atomically promote selected candidates and retain the complete ledger.

    Freshness and corpus checks run inside the generation writer lock before
    any canonical ID allocation.  The promotion record is staged in the same
    immutable generation as the scientific objects.
    """

    root = review_root.resolve(strict=False)
    detached = _canonical_request(request)
    ledger_content = _ledger_bytes(detached.ledger)
    ledger_hash = hash_bytes(ledger_content)
    request_hash = hash_json(detached.model_dump(mode="json"))
    record_relative = _record_relative_path(detached.ledger.query_id, request_hash)
    expected_record: RetrievalPromotionRecord | None = None
    expected_record_content: bytes | None = None

    def materialize(
        writer: AuxiliaryStagingWriter,
        next_generation: int,
        payload: PromotionPayload,
    ) -> None:
        nonlocal expected_record, expected_record_content
        span_ids, evidence_ids = _allocated_maps(payload.allocated_ids)
        expected_record = RetrievalPromotionRecord(
            request_hash=request_hash,
            ledger_hash=ledger_hash,
            source_generation=detached.ledger.source_generation,
            committed_generation=next_generation,
            query_id=detached.ledger.query_id,
            corpus_lock_hash=detached.ledger.corpus_lock_hash,
            ledger=detached.ledger,
            decisions=detached.decisions,
            span_ids=span_ids,
            evidence_ids=evidence_ids,
        )
        expected_record_content = _record_bytes(expected_record)
        writer.write_bytes(record_relative, expected_record_content)

    store = GenerationStore(root)
    try:
        commit = store.commit(
            base_generation=detached.ledger.source_generation,
            dependencies={},
            promotion=_build_locked_promotion(root, detached),
            staging_materializer=materialize,
            crash_at=crash_at,
        )
    except StaleSnapshotError:
        reused = _try_reuse_exact_winner(
            store,
            root,
            detached,
            request_hash=request_hash,
            ledger_hash=ledger_hash,
            record_relative=record_relative,
        )
        if reused is not None:
            return reused
        raise
    except BaseException:
        store.recover()
        raise

    if expected_record is None or expected_record_content is None:
        raise AssertionError("retrieval promotion record was not materialized")
    snapshot, _, auxiliary = store.load_generation_auxiliary(
        commit.generation, {record_relative}
    )
    snapshot.validate_repository()
    observed_content = auxiliary[record_relative]
    if observed_content != expected_record_content:
        raise CorpusIntegrityError("committed retrieval promotion record changed")
    observed_record = RetrievalPromotionRecord.model_validate_json(observed_content)
    if observed_record != expected_record:
        raise CorpusIntegrityError("committed retrieval promotion record is inconsistent")
    span_ids, evidence_ids = _allocated_maps(commit.allocated_ids)
    return RetrievalPromotionResult(
        generation=commit.generation,
        commit_performed=True,
        reused_generation=None,
        promotion_record_path=(
            f"state/generations/{commit.generation:06d}/auxiliary/{record_relative}"
        ),
        promotion_record_hash=hash_bytes(observed_content),
        request_hash=request_hash,
        ledger_hash=ledger_hash,
        span_ids=span_ids,
        evidence_ids=evidence_ids,
    )


def promote_retrieval_candidates(
    review_root: Path,
    request: RetrievalPromotionRequest,
    *,
    crash_at: CrashPoint | None = None,
) -> RetrievalPromotionResult:
    """Promote non-assessed duplicate/redundant decisions without evidence.

    Semantic ASSESSED decisions must cross the task-bound adapter so their
    canonical objects, ordinary accepted-task receipt, and promotion artifact
    share one transaction.  This compatibility entry point intentionally
    refuses to create an EvidenceRecord.
    """

    detached = _canonical_request(request)
    if any(
        decision.status is RetrievalDispositionStatus.ASSESSED
        for decision in detached.decisions
    ):
        raise RetrievalPromotionError(
            "assessed evidence promotion requires an accepted ASSESS_EVIDENCE "
            "task receipt"
        )
    return _promote_retrieval_candidates_for_testing(
        review_root,
        detached,
        crash_at=crash_at,
    )


def decisions_for_selected_candidates(
    ledger: RetrievalLedger,
    decisions: Sequence[RetrievalPromotionDecision],
) -> RetrievalPromotionRequest:
    """Build and detach a promotion request from ordinary caller sequences."""

    return _canonical_request(
        RetrievalPromotionRequest(ledger=ledger, decisions=tuple(decisions))
    )
