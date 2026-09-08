"""Typed canonical repository snapshots and dependency hashing."""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict

from vibereview.models import (
    CandidateClaim,
    ClaimAssessment,
    ClaimPacket,
    ClaimPaperEvidence,
    CorpusFact,
    EvidenceRecord,
    FinalClaimValidation,
    Paper,
    PropositionRecord,
    RenderedSentence,
    RenderedSentenceAudit,
    RetrievalDisposition,
    RetrievalQuery,
    RetrievedSpan,
    ReviewProcessFact,
    SemanticAuditResult,
    ThemeRecord,
)
from vibereview.validators import validate_repository

from .hashing import hash_json
from .records import RuntimeModel


class RepositorySnapshot(RuntimeModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    themes: tuple[ThemeRecord, ...] = ()
    papers: tuple[Paper, ...] = ()
    candidate_claims: tuple[CandidateClaim, ...] = ()
    retrieval_queries: tuple[RetrievalQuery, ...] = ()
    retrieved_spans: tuple[RetrievedSpan, ...] = ()
    retrieval_dispositions: tuple[RetrievalDisposition, ...] = ()
    evidence_records: tuple[EvidenceRecord, ...] = ()
    claim_paper_evidence: tuple[ClaimPaperEvidence, ...] = ()
    claim_assessments: tuple[ClaimAssessment, ...] = ()
    final_claim_validations: tuple[FinalClaimValidation, ...] = ()
    claim_packets: tuple[ClaimPacket, ...] = ()
    corpus_facts: tuple[CorpusFact, ...] = ()
    process_facts: tuple[ReviewProcessFact, ...] = ()
    proposition_records: tuple[PropositionRecord, ...] = ()
    semantic_audits: tuple[SemanticAuditResult, ...] = ()
    rendered_sentences: tuple[RenderedSentence, ...] = ()
    rendered_sentence_audits: tuple[RenderedSentenceAudit, ...] = ()

    def validate_repository(self) -> None:
        validate_repository(**self.as_validation_kwargs())

    def as_validation_kwargs(self) -> dict[str, tuple[Any, ...]]:
        return {name: getattr(self, name) for name in type(self).model_fields}

    def canonical_hash(self) -> str:
        return hash_json(self.model_dump(mode="json"))

    def object_index(self) -> dict[str, RuntimeModel | Any]:
        index: dict[str, Any] = {}
        id_fields = {
            "themes": "theme_id",
            "papers": "paper_id",
            "candidate_claims": "claim_id",
            "retrieval_queries": "query_id",
            "retrieved_spans": "span_id",
            "retrieval_dispositions": "span_id",
            "evidence_records": "evidence_id",
            "claim_paper_evidence": "claim_paper_evidence_id",
            "claim_assessments": "claim_id",
            "final_claim_validations": "claim_id",
            "claim_packets": "claim_id",
            "corpus_facts": "corpus_fact_id",
            "process_facts": "process_fact_id",
            "proposition_records": "proposition_id",
            "semantic_audits": "audit_id",
            "rendered_sentences": "sentence_id",
            "rendered_sentence_audits": "audit_id",
        }
        for collection_name, id_field in id_fields.items():
            for value in getattr(self, collection_name):
                identifier = getattr(value, id_field)
                # Claim-keyed objects intentionally share claim IDs; dependencies use the
                # first canonical object with that exact ID unless a unique object ID exists.
                index.setdefault(identifier, value)
                index[f"{type(value).__name__}:{identifier}"] = value
        return index

    def dependency_hash(self, identifier: str) -> str:
        value = self.object_index().get(identifier)
        if value is None:
            raise KeyError(f"canonical dependency {identifier} does not exist")
        return hash_json(value.model_dump(mode="json"))

    def all_identifiers(self) -> list[str]:
        id_fields = {
            "themes": "theme_id",
            "papers": "paper_id",
            "candidate_claims": "claim_id",
            "retrieval_queries": "query_id",
            "retrieved_spans": "span_id",
            "evidence_records": "evidence_id",
            "claim_paper_evidence": "claim_paper_evidence_id",
            "corpus_facts": "corpus_fact_id",
            "process_facts": "process_fact_id",
            "proposition_records": "proposition_id",
            "semantic_audits": "audit_id",
            "rendered_sentences": "sentence_id",
            "rendered_sentence_audits": "audit_id",
        }
        return [
            getattr(value, id_field)
            for collection_name, id_field in id_fields.items()
            for value in getattr(self, collection_name)
        ]
