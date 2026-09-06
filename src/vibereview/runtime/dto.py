"""Private invocation DTOs and sanitized engine-facing input/proposal DTOs."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import ConfigDict, Field, model_validator

from vibereview.enums import (
    AggregateRelation,
    AggregateStrength,
    AuditClassVerdict,
    AuditProvenanceVerdict,
    ClaimDecision,
    EvidenceRelation,
    EvidenceStrength,
    EvidenceSufficiency,
    FinalClaimStatus,
    Origin,
    PropositionContentClass,
    RejectionBasis,
    RetrievalIntent,
    ValidationCheckResult,
    WithinPaperConsistency,
)
from vibereview.ids import (
    ClaimId,
    ClaimPaperEvidenceId,
    EvidenceId,
    PaperId,
    PropositionId,
    SentenceId,
    SpanId,
    ThemeId,
)
from vibereview.models import ComponentRelations, EvidenceQuality

from .records import RESOURCE_ID_PATTERN, RuntimeModel


class DTOModel(RuntimeModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


ResourceRef = Annotated[str, Field(pattern=RESOURCE_ID_PATTERN)]


class ParseDeepResearchInvocation(DTOModel):
    topic: str
    document_paths: list[Path]


class ParseDeepResearchInput(DTOModel):
    topic: str
    document_resource_ids: list[ResourceRef]


class CorpusChallengerInvocation(DTOModel):
    topic: str
    paper_ids: list[PaperId]


class CorpusChallengerInput(DTOModel):
    topic: str
    paper_ids: list[PaperId]


class GenerateCandidateClaimsInvocation(DTOModel):
    topic: str
    existing_theme_ids: list[ThemeId]


class GenerateCandidateClaimsInput(DTOModel):
    topic: str
    existing_theme_ids: list[ThemeId]


class GenerateRetrievalQueriesInvocation(DTOModel):
    claim_ids: list[ClaimId]


class GenerateRetrievalQueriesInput(DTOModel):
    claim_ids: list[ClaimId]


class AssessEvidenceInvocation(DTOModel):
    claim_id: ClaimId
    span_ids: list[SpanId]


class AssessEvidenceInput(DTOModel):
    claim_id: ClaimId
    span_ids: list[SpanId]


class AggregatePaperEvidenceInvocation(DTOModel):
    claim_id: ClaimId
    paper_id: PaperId
    evidence_ids: list[EvidenceId]


class AggregatePaperEvidenceInput(DTOModel):
    claim_id: ClaimId
    paper_id: PaperId
    evidence_ids: list[EvidenceId]


class AssessClaimInvocation(DTOModel):
    claim_id: ClaimId
    claim_paper_evidence_ids: list[ClaimPaperEvidenceId]


class AssessClaimInput(DTOModel):
    claim_id: ClaimId
    claim_paper_evidence_ids: list[ClaimPaperEvidenceId]


class ReviseClaimInvocation(DTOModel):
    claim_id: ClaimId
    current_candidate_claim: str


class ReviseClaimInput(DTOModel):
    claim_id: ClaimId
    current_candidate_claim: str


class ValidateFinalClaimInvocation(DTOModel):
    claim_id: ClaimId
    proposed_final_claim: str


class ValidateFinalClaimInput(DTOModel):
    claim_id: ClaimId
    proposed_final_claim: str


class GeneratePropositionsInvocation(DTOModel):
    claim_ids: list[ClaimId]


class GeneratePropositionsInput(DTOModel):
    claim_ids: list[ClaimId]


class AuditPropositionInvocation(DTOModel):
    proposition_id: PropositionId


class AuditPropositionInput(DTOModel):
    proposition_id: PropositionId


class RenderProseInvocation(DTOModel):
    proposition_ids: list[PropositionId]


class RenderProseInput(DTOModel):
    proposition_ids: list[PropositionId]


class AuditRenderedSentenceInvocation(DTOModel):
    sentence_id: SentenceId


class AuditRenderedSentenceInput(DTOModel):
    sentence_id: SentenceId


LocalRef = Annotated[str, Field(min_length=1, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")]
ObjectRef = Annotated[str, Field(min_length=1)]


class ThemeProposal(DTOModel):
    local_ref: LocalRef
    title: str
    description: str
    origin: Origin
    parent_ref: ObjectRef | None = None


class CandidateClaimProposal(DTOModel):
    local_ref: LocalRef
    theme_ref: ObjectRef
    candidate_claim: Annotated[str, Field(min_length=1)]
    origin: Origin
    origin_refs: list[str]


class DiscoveryProposalBundle(DTOModel):
    themes: list[ThemeProposal] = Field(default_factory=list)
    claims: list[CandidateClaimProposal] = Field(default_factory=list)


class RetrievalQueryProposal(DTOModel):
    local_ref: LocalRef
    claim_ref: ObjectRef
    intent: RetrievalIntent
    query_text: Annotated[str, Field(min_length=1)]


class RetrievalQueryProposalBundle(DTOModel):
    queries: list[RetrievalQueryProposal]


class EvidenceRecordProposal(DTOModel):
    local_ref: LocalRef
    claim_ref: ObjectRef
    retrieved_span_ref: ObjectRef
    paper_ref: ObjectRef
    relation_to_candidate: EvidenceRelation
    evidence_summary: str
    quality: EvidenceQuality
    assessment_note: str


class EvidenceRecordProposalBundle(DTOModel):
    evidence: list[EvidenceRecordProposal]


class ClaimPaperEvidenceProposal(DTOModel):
    claim_ref: ObjectRef
    paper_ref: ObjectRef
    evidence_refs: Annotated[list[ObjectRef], Field(min_length=1)]
    relation_to_candidate: AggregateRelation
    component_relations: ComponentRelations
    strength: EvidenceStrength
    within_paper_consistency: WithinPaperConsistency
    assessment_note: str


class ClaimAssessmentProposal(DTOModel):
    claim_ref: ObjectRef
    aggregate_strength: AggregateStrength
    evidence_sufficiency: EvidenceSufficiency
    decision: ClaimDecision
    rejection_basis: RejectionBasis | None
    support_summary: str
    contradiction_summary: str
    qualification_summary: str
    reason: str

    @model_validator(mode="after")
    def _rejection_basis(self) -> "ClaimAssessmentProposal":
        if self.decision is ClaimDecision.REJECT and self.rejection_basis is None:
            raise ValueError("REJECT requires rejection_basis")
        if self.decision is not ClaimDecision.REJECT and self.rejection_basis is not None:
            raise ValueError("non-REJECT cannot have rejection_basis")
        return self


class RevisedClaimProposal(DTOModel):
    claim_ref: ObjectRef
    final_claim: Annotated[str, Field(min_length=1)]


class FinalPaperRelationProposal(DTOModel):
    claim_paper_evidence_ref: ObjectRef
    paper_ref: ObjectRef
    relation_to_final_claim: AggregateRelation


class FinalClaimValidationProposal(DTOModel):
    claim_ref: ObjectRef
    final_claim: Annotated[str, Field(min_length=1)]
    status: FinalClaimStatus
    paper_relations: list[FinalPaperRelationProposal]
    scope_check: ValidationCheckResult
    certainty_check: ValidationCheckResult
    causal_language_check: ValidationCheckResult
    numerical_claim_check: ValidationCheckResult
    notes: str

    @model_validator(mode="after")
    def _valid_status_checks(self) -> "FinalClaimValidationProposal":
        if self.status is not FinalClaimStatus.VALID:
            return self
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
        return self


class CitationBindingProposal(DTOModel):
    paper_ref: ObjectRef
    claim_ref: ObjectRef
    claim_paper_evidence_ref: ObjectRef


class PropositionProposal(DTOModel):
    local_ref: LocalRef
    text: str
    content_class: PropositionContentClass
    claim_refs: list[ObjectRef]
    citation_bindings: list[CitationBindingProposal]
    corpus_fact_refs: list[ObjectRef]
    process_fact_refs: list[ObjectRef]

    @model_validator(mode="after")
    def _exclusive_provenance(self) -> "PropositionProposal":
        if self.content_class is PropositionContentClass.SCIENTIFIC_CLAIM:
            if not self.claim_refs or not self.citation_bindings:
                raise ValueError("ScientificClaim requires claims and citations")
            if self.corpus_fact_refs or self.process_fact_refs:
                raise ValueError("ScientificClaim cannot mix provenance classes")
            if {item.claim_ref for item in self.citation_bindings} != set(
                self.claim_refs
            ):
                raise ValueError("ScientificClaim citations must cover all claims")
        elif self.content_class is PropositionContentClass.CORPUS_FACT:
            if (
                self.claim_refs
                or self.citation_bindings
                or not self.corpus_fact_refs
                or self.process_fact_refs
            ):
                raise ValueError("CorpusFact must contain only corpus-fact provenance")
        elif self.content_class is PropositionContentClass.REVIEW_PROCESS_STATEMENT:
            if (
                self.claim_refs
                or self.citation_bindings
                or self.corpus_fact_refs
                or not self.process_fact_refs
            ):
                raise ValueError(
                    "ReviewProcessStatement must contain only process provenance"
                )
        elif any(
            (
                self.claim_refs,
                self.citation_bindings,
                self.corpus_fact_refs,
                self.process_fact_refs,
            )
        ):
            raise ValueError("Rhetorical proposal cannot contain provenance")
        return self


class PropositionProposalBundle(DTOModel):
    propositions: list[PropositionProposal]


class SemanticAuditProposal(DTOModel):
    target_ref: ObjectRef
    class_verdict: AuditClassVerdict
    provenance_verdict: AuditProvenanceVerdict
    reason: str
    referenced_claim_refs: list[ObjectRef]
    referenced_corpus_fact_refs: list[ObjectRef]
    referenced_process_fact_refs: list[ObjectRef]


class RenderedSentenceProposal(DTOModel):
    local_ref: LocalRef
    text: Annotated[str, Field(min_length=1)]
    source_proposition_refs: Annotated[list[ObjectRef], Field(min_length=1)]


class RenderedSentenceProposalBundle(DTOModel):
    sentences: list[RenderedSentenceProposal]


class RenderedSentenceAuditProposal(DTOModel):
    sentence_ref: ObjectRef
    verdict: AuditProvenanceVerdict
    reason: str
