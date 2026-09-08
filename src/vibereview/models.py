"""Pydantic schemas and intrinsic Phase-0 contract validation."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .enums import (
    AggregateRelation,
    AggregateStrength,
    Assessability,
    AuditClassVerdict,
    AuditProvenanceVerdict,
    ClaimDecision,
    CorpusFactDerivationType,
    EvidenceDirectness,
    EvidenceRelation,
    EvidenceStrength,
    EvidenceSufficiency,
    FinalClaimStatus,
    IndependenceStatus,
    MethodologicalRelevance,
    Origin,
    PropositionContentClass,
    RejectionBasis,
    RetrievalDispositionStatus,
    RetrievalIntent,
    ReviewProcessSourceType,
    ValidationCheckResult,
    WithinPaperConsistency,
)
from .ids import (
    ClaimId,
    ClaimPaperEvidenceId,
    CorpusFactId,
    EvidenceId,
    PaperId,
    ProcessFactId,
    PropositionId,
    QueryId,
    SemanticAuditId,
    SentenceAuditId,
    SentenceId,
    Sha256,
    SpanId,
    ThemeId,
    normalize_doi,
    normalize_identity_key,
    parse_query_id,
)

NonEmptyStr = Annotated[str, Field(min_length=1)]


class ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        allow_inf_nan=False,
    )


class ThemeRecord(ContractModel):
    theme_id: ThemeId
    title: str
    description: str
    origin: Origin
    parent_theme_id: ThemeId | None


class Paper(ContractModel):
    paper_id: PaperId
    title: str | None
    authors: list[str] | None
    year: int | None
    doi: str | None
    journal: str | None
    identity_keys: Annotated[list[str], Field(min_length=1)]
    study_group_id: str | None
    related_publications: list[PaperId]
    independence_status: IndependenceStatus
    raw_md_path: NonEmptyStr
    source_hash: Sha256
    raw_md_hash: Sha256

    @field_validator("doi", mode="before")
    @classmethod
    def _normalize_doi(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        return normalize_doi(value)

    @field_validator("identity_keys", mode="before")
    @classmethod
    def _normalize_identity_keys(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        normalized = [normalize_identity_key(item) for item in value]
        return list(dict.fromkeys(normalized))


class CandidateClaim(ContractModel):
    claim_id: ClaimId
    theme_id: ThemeId
    candidate_claim: NonEmptyStr
    origin: Origin
    origin_refs: list[str]


class RetrievalQuery(ContractModel):
    query_id: QueryId
    claim_id: ClaimId
    candidate_claim_hash: Sha256
    intent: RetrievalIntent
    query_text: NonEmptyStr

    @model_validator(mode="after")
    def _query_id_matches_fields(self) -> "RetrievalQuery":
        encoded_claim, encoded_intent, _ = parse_query_id(self.query_id)
        if encoded_claim != self.claim_id:
            raise ValueError("query ID claim component must equal claim_id")
        if encoded_intent != self.intent.value:
            raise ValueError("query ID intent code must equal intent")
        return self


class SpanLocator(ContractModel):
    raw_md_path: NonEmptyStr
    page: Annotated[int, Field(ge=1)] | None
    section: str | None
    start_offset: Annotated[int, Field(ge=0)]
    end_offset: int
    source_span_hash: Sha256

    @model_validator(mode="after")
    def _ordered_offsets(self) -> "SpanLocator":
        if self.end_offset <= self.start_offset:
            raise ValueError("end_offset must be greater than start_offset")
        return self


class RetrievalMetadata(ContractModel):
    query_id: QueryId
    intent: RetrievalIntent
    retrieval_score: float


class RetrievedSpan(ContractModel):
    span_id: SpanId
    paper_id: PaperId
    locator: SpanLocator
    source_text: NonEmptyStr
    retrieval: RetrievalMetadata


class RetrievalDisposition(ContractModel):
    span_id: SpanId
    status: RetrievalDispositionStatus
    reason: str | None
    canonical_span_id: SpanId | None

    @model_validator(mode="after")
    def _status_fields(self) -> "RetrievalDisposition":
        if self.status is RetrievalDispositionStatus.ASSESSED:
            if self.canonical_span_id is not None:
                raise ValueError("assessed disposition cannot have canonical_span_id")
        elif self.status is RetrievalDispositionStatus.DUPLICATE:
            if self.canonical_span_id is None:
                raise ValueError("duplicate disposition requires canonical_span_id")
        elif self.status is RetrievalDispositionStatus.REDUNDANT:
            if self.canonical_span_id is None and not self.reason:
                raise ValueError("redundant disposition requires canonical_span_id or reason")
        elif self.status in {
            RetrievalDispositionStatus.EXCLUDED_BY_BUDGET,
            RetrievalDispositionStatus.INVALID_LOCATOR,
        }:
            if not self.reason:
                raise ValueError(f"{self.status.value} disposition requires reason")
        return self


class EvidenceQuality(ContractModel):
    directness: EvidenceDirectness
    methodological_relevance: MethodologicalRelevance
    strength: EvidenceStrength
    assessability: Assessability
    limitations: list[str]

    @model_validator(mode="after")
    def _not_assessable_constraints(self) -> "EvidenceQuality":
        if self.assessability is Assessability.NOT_ASSESSABLE:
            if self.strength not in {
                EvidenceStrength.UNKNOWN,
                EvidenceStrength.NOT_ASSESSABLE,
            }:
                raise ValueError("not-assessable evidence cannot have authoritative strength")
            if self.methodological_relevance in {
                MethodologicalRelevance.HIGH,
                MethodologicalRelevance.MODERATE,
            }:
                raise ValueError(
                    "not-assessable evidence cannot have authoritative methodological relevance"
                )
            if self.directness not in {
                EvidenceDirectness.UNCLEAR,
                EvidenceDirectness.NOT_ASSESSABLE,
            }:
                raise ValueError("not-assessable evidence cannot have authoritative directness")
        return self


class EvidenceRecord(ContractModel):
    evidence_id: EvidenceId
    claim_id: ClaimId
    retrieved_span_id: SpanId
    paper_id: PaperId
    relation_to_candidate: EvidenceRelation
    evidence_summary: str
    quality: EvidenceQuality
    assessment_note: str


class ComponentRelations(ContractModel):
    supports: list[EvidenceId] = Field(default_factory=list)
    contradicts: list[EvidenceId] = Field(default_factory=list)
    qualifies: list[EvidenceId] = Field(default_factory=list)
    contextual: list[EvidenceId] = Field(default_factory=list)
    unclear: list[EvidenceId] = Field(default_factory=list)

    def grouped(self) -> dict[EvidenceRelation, list[str]]:
        return {
            EvidenceRelation.SUPPORTS: list(self.supports),
            EvidenceRelation.CONTRADICTS: list(self.contradicts),
            EvidenceRelation.QUALIFIES: list(self.qualifies),
            EvidenceRelation.CONTEXTUAL: list(self.contextual),
            EvidenceRelation.UNCLEAR: list(self.unclear),
        }


class ClaimPaperEvidence(ContractModel):
    claim_paper_evidence_id: ClaimPaperEvidenceId
    claim_id: ClaimId
    paper_id: PaperId
    evidence_ids: Annotated[list[EvidenceId], Field(min_length=1)]
    relation_to_candidate: AggregateRelation
    component_relations: ComponentRelations
    strength: EvidenceStrength
    within_paper_consistency: WithinPaperConsistency
    assessment_note: str

    @model_validator(mode="after")
    def _component_partition(self) -> "ClaimPaperEvidence":
        evidence_ids = list(self.evidence_ids)
        grouped_ids = [item for group in self.component_relations.grouped().values() for item in group]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence_ids must not contain duplicates")
        if len(grouped_ids) != len(set(grouped_ids)):
            raise ValueError("each evidence ID must appear in exactly one component group")
        if set(grouped_ids) != set(evidence_ids):
            raise ValueError("component relation union must equal evidence_ids")
        if self.relation_to_candidate is AggregateRelation.MIXED:
            substantive = (
                self.component_relations.supports,
                self.component_relations.contradicts,
                self.component_relations.qualifies,
            )
            if sum(bool(group) for group in substantive) < 2:
                raise ValueError("mixed CPE requires at least two substantive relation groups")
        else:
            substantive_groups = {
                AggregateRelation.SUPPORTS: self.component_relations.supports,
                AggregateRelation.CONTRADICTS: self.component_relations.contradicts,
                AggregateRelation.QUALIFIES: self.component_relations.qualifies,
            }
            populated_substantive = {
                relation for relation, values in substantive_groups.items() if values
            }
            if self.relation_to_candidate in substantive_groups:
                if populated_substantive != {self.relation_to_candidate}:
                    raise ValueError(
                        "aggregate relation must match the sole substantive component relation"
                    )
            elif self.relation_to_candidate is AggregateRelation.CONTEXTUAL:
                if populated_substantive or not self.component_relations.contextual:
                    raise ValueError(
                        "contextual aggregate requires contextual evidence and no substantive group"
                    )
            elif self.relation_to_candidate is AggregateRelation.UNCLEAR:
                if populated_substantive or not self.component_relations.unclear:
                    raise ValueError(
                        "unclear aggregate requires unclear evidence and no substantive group"
                    )
        return self


class ClaimAssessment(ContractModel):
    claim_id: ClaimId
    aggregate_strength: AggregateStrength
    evidence_sufficiency: EvidenceSufficiency
    decision: ClaimDecision
    rejection_basis: RejectionBasis | None
    support_summary: str
    contradiction_summary: str
    qualification_summary: str
    reason: str

    @model_validator(mode="after")
    def _rejection_basis_matches_decision(self) -> "ClaimAssessment":
        if self.decision is ClaimDecision.REJECT and self.rejection_basis is None:
            raise ValueError("REJECT requires rejection_basis")
        if self.decision is not ClaimDecision.REJECT and self.rejection_basis is not None:
            raise ValueError("non-REJECT decision cannot have rejection_basis")
        return self


class FinalPaperRelation(ContractModel):
    claim_paper_evidence_id: ClaimPaperEvidenceId
    paper_id: PaperId
    relation_to_final_claim: AggregateRelation


class FinalClaimValidation(ContractModel):
    claim_id: ClaimId
    final_claim: NonEmptyStr
    status: FinalClaimStatus
    paper_relations: list[FinalPaperRelation]
    scope_check: ValidationCheckResult
    certainty_check: ValidationCheckResult
    causal_language_check: ValidationCheckResult
    numerical_claim_check: ValidationCheckResult
    notes: str

    @model_validator(mode="after")
    def _valid_status_requires_passing_checks(self) -> "FinalClaimValidation":
        checks = (
            self.scope_check,
            self.certainty_check,
            self.causal_language_check,
            self.numerical_claim_check,
        )
        if self.status is FinalClaimStatus.VALID and any(
            check in {ValidationCheckResult.FAIL, ValidationCheckResult.UNCLEAR}
            for check in checks
        ):
            raise ValueError("VALID final claim cannot contain failed or unclear checks")
        if self.status is FinalClaimStatus.VALID:
            if not self.paper_relations:
                raise ValueError("VALID final claim requires at least one paper relation")
            if self.scope_check is not ValidationCheckResult.PASS:
                raise ValueError("VALID final claim requires scope_check=pass")
            if self.certainty_check is not ValidationCheckResult.PASS:
                raise ValueError("VALID final claim requires certainty_check=pass")
            if self.causal_language_check not in {
                ValidationCheckResult.PASS,
                ValidationCheckResult.NOT_APPLICABLE,
            }:
                raise ValueError(
                    "VALID final claim requires causal_language_check=pass or not_applicable"
                )
            if self.numerical_claim_check not in {
                ValidationCheckResult.PASS,
                ValidationCheckResult.NOT_APPLICABLE,
            }:
                raise ValueError(
                    "VALID final claim requires numerical_claim_check=pass or not_applicable"
                )
        cpe_ids = [relation.claim_paper_evidence_id for relation in self.paper_relations]
        if len(cpe_ids) != len(set(cpe_ids)):
            raise ValueError("each CPE may occur only once in final paper relations")
        return self


class ClaimPacket(ContractModel):
    claim_id: ClaimId
    theme_id: ThemeId
    candidate_claim: NonEmptyStr
    final_claim: NonEmptyStr
    aggregate_strength: AggregateStrength
    claim_paper_evidence_ids: Annotated[
        list[ClaimPaperEvidenceId], Field(min_length=1)
    ]


class CitationBinding(ContractModel):
    paper_id: PaperId
    claim_id: ClaimId
    claim_paper_evidence_id: ClaimPaperEvidenceId


class CorpusFactDerivation(ContractModel):
    task_version: str | None
    model_signature: str | None
    input_hash: Sha256 | None
    output_hash: Sha256 | None


class CorpusFact(ContractModel):
    corpus_fact_id: CorpusFactId
    text: str
    derivation_type: CorpusFactDerivationType
    source_paper_ids: list[PaperId]
    derivation: CorpusFactDerivation

    @model_validator(mode="after")
    def _semantic_derivation_has_provenance(self) -> "CorpusFact":
        if self.derivation_type is CorpusFactDerivationType.SEMANTIC_CLASSIFICATION:
            if any(
                value is None
                for value in (
                    self.derivation.task_version,
                    self.derivation.model_signature,
                    self.derivation.input_hash,
                    self.derivation.output_hash,
                )
            ):
                raise ValueError("semantic classification requires complete provenance metadata")
        return self


class ReviewProcessFact(ContractModel):
    process_fact_id: ProcessFactId
    text: str
    source_type: ReviewProcessSourceType
    source_key: str


class PropositionRecord(ContractModel):
    proposition_id: PropositionId
    text: str
    content_class: PropositionContentClass
    claim_ids: list[ClaimId]
    citation_bindings: list[CitationBinding]
    corpus_fact_ids: list[CorpusFactId]
    process_fact_ids: list[ProcessFactId]

    @model_validator(mode="after")
    def _exclusive_provenance_class(self) -> "PropositionRecord":
        if self.content_class is PropositionContentClass.SCIENTIFIC_CLAIM:
            if not self.claim_ids or not self.citation_bindings:
                raise ValueError("ScientificClaim requires claims and citation bindings")
            if self.corpus_fact_ids or self.process_fact_ids:
                raise ValueError("ScientificClaim cannot contain other provenance classes")
            binding_claims = {binding.claim_id for binding in self.citation_bindings}
            if binding_claims != set(self.claim_ids):
                raise ValueError("ScientificClaim citations must cover every and only declared claim")
        elif self.content_class is PropositionContentClass.CORPUS_FACT:
            if self.claim_ids or self.citation_bindings or not self.corpus_fact_ids or self.process_fact_ids:
                raise ValueError("CorpusFact proposition must contain only corpus-fact provenance")
        elif self.content_class is PropositionContentClass.REVIEW_PROCESS_STATEMENT:
            if self.claim_ids or self.citation_bindings or self.corpus_fact_ids or not self.process_fact_ids:
                raise ValueError(
                    "ReviewProcessStatement must contain only process-fact provenance"
                )
        elif any((self.claim_ids, self.citation_bindings, self.corpus_fact_ids, self.process_fact_ids)):
            raise ValueError("Rhetorical proposition cannot contain provenance")
        return self


class SemanticAuditResult(ContractModel):
    audit_id: SemanticAuditId
    target_type: Literal["PropositionRecord"]
    target_id: PropositionId
    class_verdict: AuditClassVerdict
    provenance_verdict: AuditProvenanceVerdict
    reason: str
    referenced_claim_ids: list[ClaimId]
    referenced_corpus_fact_ids: list[CorpusFactId]
    referenced_process_fact_ids: list[ProcessFactId]


class RenderedSentence(ContractModel):
    sentence_id: SentenceId
    text: NonEmptyStr
    source_proposition_ids: Annotated[list[PropositionId], Field(min_length=1)]


class RenderedSentenceAudit(ContractModel):
    audit_id: SentenceAuditId
    sentence_id: SentenceId
    verdict: AuditProvenanceVerdict
    reason: str
