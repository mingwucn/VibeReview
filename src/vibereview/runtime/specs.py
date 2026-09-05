"""Fixed TaskType-to-contract registry."""

from __future__ import annotations

from pathlib import Path

from .dto import (
    AggregatePaperEvidenceInput,
    AssessClaimInput,
    AssessEvidenceInput,
    AuditPropositionInput,
    AuditRenderedSentenceInput,
    CandidateClaimProposal,
    ClaimAssessmentProposal,
    ClaimPaperEvidenceProposal,
    CorpusChallengerInput,
    DiscoveryProposalBundle,
    EvidenceRecordProposalBundle,
    FinalClaimValidationProposal,
    GenerateCandidateClaimsInput,
    GeneratePropositionsInput,
    GenerateRetrievalQueriesInput,
    ParseDeepResearchInput,
    PropositionProposalBundle,
    RenderProseInput,
    RenderedSentenceAuditProposal,
    RenderedSentenceProposalBundle,
    RetrievalQueryProposalBundle,
    ReviseClaimInput,
    RevisedClaimProposal,
    SemanticAuditProposal,
    ValidateFinalClaimInput,
)
from .records import TaskSpec, TaskType


PROMPT_ROOT = Path(__file__).resolve().parent.parent / "prompts"


def _spec(
    task_type: TaskType,
    input_model,
    proposal_model,
    promotion_handler: str,
    disposition_handler: str,
) -> TaskSpec:
    return TaskSpec(
        task_type=task_type,
        version="1",
        input_model=input_model,
        proposal_model=proposal_model,
        prompt_path=PROMPT_ROOT / f"{task_type.value}.md",
        prompt_version="1",
        promotion_handler=promotion_handler,
        disposition_handler=disposition_handler,
    )


TASK_SPECS: dict[TaskType, TaskSpec] = {
    TaskType.PARSE_DEEP_RESEARCH: _spec(
        TaskType.PARSE_DEEP_RESEARCH,
        ParseDeepResearchInput,
        DiscoveryProposalBundle,
        "promote_discovery",
        "interpret_positive",
    ),
    TaskType.CORPUS_CHALLENGER: _spec(
        TaskType.CORPUS_CHALLENGER,
        CorpusChallengerInput,
        DiscoveryProposalBundle,
        "promote_discovery",
        "interpret_positive",
    ),
    TaskType.GENERATE_CANDIDATE_CLAIMS: _spec(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        GenerateCandidateClaimsInput,
        DiscoveryProposalBundle,
        "promote_discovery",
        "interpret_positive",
    ),
    TaskType.GENERATE_RETRIEVAL_QUERIES: _spec(
        TaskType.GENERATE_RETRIEVAL_QUERIES,
        GenerateRetrievalQueriesInput,
        RetrievalQueryProposalBundle,
        "promote_retrieval_queries",
        "interpret_positive",
    ),
    TaskType.ASSESS_EVIDENCE: _spec(
        TaskType.ASSESS_EVIDENCE,
        AssessEvidenceInput,
        EvidenceRecordProposalBundle,
        "promote_evidence",
        "interpret_positive",
    ),
    TaskType.AGGREGATE_PAPER_EVIDENCE: _spec(
        TaskType.AGGREGATE_PAPER_EVIDENCE,
        AggregatePaperEvidenceInput,
        ClaimPaperEvidenceProposal,
        "promote_claim_paper_evidence",
        "interpret_positive",
    ),
    TaskType.ASSESS_CLAIM: _spec(
        TaskType.ASSESS_CLAIM,
        AssessClaimInput,
        ClaimAssessmentProposal,
        "promote_claim_assessment",
        "interpret_claim_assessment",
    ),
    TaskType.REVISE_CLAIM: _spec(
        TaskType.REVISE_CLAIM,
        ReviseClaimInput,
        RevisedClaimProposal,
        "promote_revised_claim",
        "interpret_positive",
    ),
    TaskType.VALIDATE_FINAL_CLAIM: _spec(
        TaskType.VALIDATE_FINAL_CLAIM,
        ValidateFinalClaimInput,
        FinalClaimValidationProposal,
        "promote_final_claim_validation",
        "interpret_final_claim_validation",
    ),
    TaskType.GENERATE_PROPOSITIONS: _spec(
        TaskType.GENERATE_PROPOSITIONS,
        GeneratePropositionsInput,
        PropositionProposalBundle,
        "promote_propositions",
        "interpret_positive",
    ),
    TaskType.AUDIT_PROPOSITION: _spec(
        TaskType.AUDIT_PROPOSITION,
        AuditPropositionInput,
        SemanticAuditProposal,
        "promote_semantic_audit",
        "interpret_semantic_audit",
    ),
    TaskType.RENDER_PROSE: _spec(
        TaskType.RENDER_PROSE,
        RenderProseInput,
        RenderedSentenceProposalBundle,
        "promote_rendered_sentences",
        "interpret_positive",
    ),
    TaskType.AUDIT_RENDERED_SENTENCE: _spec(
        TaskType.AUDIT_RENDERED_SENTENCE,
        AuditRenderedSentenceInput,
        RenderedSentenceAuditProposal,
        "promote_rendered_sentence_audit",
        "interpret_rendered_sentence_audit",
    ),
}


assert set(TASK_SPECS) == set(TaskType)

