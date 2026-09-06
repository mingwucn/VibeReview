"""Fixed TaskType-to-contract registry and TaskSpec executability preflight."""

from __future__ import annotations

from pathlib import Path

from vibereview.models import (
    CandidateClaim,
    ClaimPaperEvidence,
    EvidenceRecord,
    Paper,
    PropositionRecord,
    RenderedSentence,
    RetrievedSpan,
    ThemeRecord,
)

from .dto import (
    AggregatePaperEvidenceInput,
    AggregatePaperEvidenceInvocation,
    AssessClaimInput,
    AssessClaimInvocation,
    AssessEvidenceInput,
    AssessEvidenceInvocation,
    AuditPropositionInput,
    AuditPropositionInvocation,
    AuditRenderedSentenceInput,
    AuditRenderedSentenceInvocation,
    CandidateClaimProposal,
    ClaimAssessmentProposal,
    ClaimPaperEvidenceProposal,
    CorpusChallengerInput,
    CorpusChallengerInvocation,
    DiscoveryProposalBundle,
    EvidenceRecordProposalBundle,
    FinalClaimValidationProposal,
    GenerateCandidateClaimsInput,
    GenerateCandidateClaimsInvocation,
    GeneratePropositionsInput,
    GeneratePropositionsInvocation,
    GenerateRetrievalQueriesInput,
    GenerateRetrievalQueriesInvocation,
    ParseDeepResearchInput,
    ParseDeepResearchInvocation,
    PropositionProposalBundle,
    RenderProseInput,
    RenderProseInvocation,
    RenderedSentenceAuditProposal,
    RenderedSentenceProposalBundle,
    RetrievalQueryProposalBundle,
    ReviseClaimInput,
    ReviseClaimInvocation,
    RevisedClaimProposal,
    SemanticAuditProposal,
    ValidateFinalClaimInput,
    ValidateFinalClaimInvocation,
)
from .promotion import DISPOSITION_HANDLERS, PROMOTION_HANDLERS
from .records import (
    ProjectContext,
    SnapshottedResource,
    TaskResourceRequest,
    TaskSpec,
    TaskSpecNotExecutableError,
    TaskType,
    build_resource_requests,
)
from .state import RepositorySnapshot


PROMPT_ROOT = Path(__file__).resolve().parent.parent / "prompts"


def _no_dependencies(_invocation, _snapshot: RepositorySnapshot) -> tuple[str, ...]:
    return ()


def _corpus_challenger_dependencies(
    invocation: CorpusChallengerInvocation, _snapshot: RepositorySnapshot
) -> tuple[str, ...]:
    return tuple(f"{Paper.__name__}:{paper_id}" for paper_id in invocation.paper_ids)


def _generate_candidate_claims_dependencies(
    invocation: GenerateCandidateClaimsInvocation, _snapshot: RepositorySnapshot
) -> tuple[str, ...]:
    return tuple(
        f"{ThemeRecord.__name__}:{theme_id}"
        for theme_id in invocation.existing_theme_ids
    )


def _claim_list_dependencies(invocation, _snapshot: RepositorySnapshot) -> tuple[str, ...]:
    return tuple(f"{CandidateClaim.__name__}:{claim_id}" for claim_id in invocation.claim_ids)


def _assess_evidence_dependencies(
    invocation: AssessEvidenceInvocation, _snapshot: RepositorySnapshot
) -> tuple[str, ...]:
    return (
        f"{CandidateClaim.__name__}:{invocation.claim_id}",
        *(
            f"{RetrievedSpan.__name__}:{span_id}"
            for span_id in invocation.span_ids
        ),
    )


def _aggregate_paper_evidence_dependencies(
    invocation: AggregatePaperEvidenceInvocation, _snapshot: RepositorySnapshot
) -> tuple[str, ...]:
    return (
        f"{CandidateClaim.__name__}:{invocation.claim_id}",
        f"{Paper.__name__}:{invocation.paper_id}",
        *(
            f"{EvidenceRecord.__name__}:{evidence_id}"
            for evidence_id in invocation.evidence_ids
        ),
    )


def _assess_claim_dependencies(
    invocation: AssessClaimInvocation, _snapshot: RepositorySnapshot
) -> tuple[str, ...]:
    return (
        f"{CandidateClaim.__name__}:{invocation.claim_id}",
        *(
            f"{ClaimPaperEvidence.__name__}:{cpe_id}"
            for cpe_id in invocation.claim_paper_evidence_ids
        ),
    )


def _single_claim_dependencies(invocation, _snapshot: RepositorySnapshot) -> tuple[str, ...]:
    return (f"{CandidateClaim.__name__}:{invocation.claim_id}",)


def _audit_proposition_dependencies(
    invocation: AuditPropositionInvocation, _snapshot: RepositorySnapshot
) -> tuple[str, ...]:
    return (f"{PropositionRecord.__name__}:{invocation.proposition_id}",)


def _render_prose_dependencies(
    invocation: RenderProseInvocation, _snapshot: RepositorySnapshot
) -> tuple[str, ...]:
    return tuple(
        f"{PropositionRecord.__name__}:{proposition_id}"
        for proposition_id in invocation.proposition_ids
    )


def _audit_rendered_sentence_dependencies(
    invocation: AuditRenderedSentenceInvocation, _snapshot: RepositorySnapshot
) -> tuple[str, ...]:
    return (f"{RenderedSentence.__name__}:{invocation.sentence_id}",)


def _no_resources(
    _invocation, _snapshot: RepositorySnapshot, _context: ProjectContext
) -> tuple[TaskResourceRequest, ...]:
    return ()


def _parse_deep_research_resources(
    invocation: ParseDeepResearchInvocation,
    _snapshot: RepositorySnapshot,
    _context: ProjectContext,
) -> tuple[TaskResourceRequest, ...]:
    return build_resource_requests(invocation.document_paths, media_type="text/markdown")


def _parse_deep_research_engine_input(
    invocation: ParseDeepResearchInvocation,
    resources: tuple[SnapshottedResource, ...],
) -> ParseDeepResearchInput:
    return ParseDeepResearchInput(
        topic=invocation.topic,
        document_resource_ids=[resource.resource_id for resource in resources],
    )


def _mirror_engine_input(engine_input_model):
    def build(invocation, resources: tuple[SnapshottedResource, ...]):
        if resources:
            raise ValueError(
                f"{engine_input_model.__name__} does not consume snapshotted resources"
            )
        return engine_input_model.model_validate(invocation.model_dump())

    return build


def _spec(
    task_type: TaskType,
    *,
    invocation_model,
    engine_input_model,
    proposal_model,
    dependency_builder,
    resource_builder,
    engine_input_builder,
    promotion_handler: str,
    disposition_handler: str,
) -> TaskSpec:
    return TaskSpec(
        task_type=task_type,
        version="1",
        invocation_model=invocation_model,
        engine_input_model=engine_input_model,
        proposal_model=proposal_model,
        dependency_builder=dependency_builder,
        resource_builder=resource_builder,
        engine_input_builder=engine_input_builder,
        prompt_path=PROMPT_ROOT / f"{task_type.value}.md",
        prompt_version="1",
        promotion_handler=promotion_handler,
        disposition_handler=disposition_handler,
    )


TASK_SPECS: dict[TaskType, TaskSpec] = {
    TaskType.PARSE_DEEP_RESEARCH: _spec(
        TaskType.PARSE_DEEP_RESEARCH,
        invocation_model=ParseDeepResearchInvocation,
        engine_input_model=ParseDeepResearchInput,
        proposal_model=DiscoveryProposalBundle,
        dependency_builder=_no_dependencies,
        resource_builder=_parse_deep_research_resources,
        engine_input_builder=_parse_deep_research_engine_input,
        promotion_handler="promote_discovery",
        disposition_handler="interpret_positive",
    ),
    TaskType.CORPUS_CHALLENGER: _spec(
        TaskType.CORPUS_CHALLENGER,
        invocation_model=CorpusChallengerInvocation,
        engine_input_model=CorpusChallengerInput,
        proposal_model=DiscoveryProposalBundle,
        dependency_builder=_corpus_challenger_dependencies,
        resource_builder=_no_resources,
        engine_input_builder=_mirror_engine_input(CorpusChallengerInput),
        promotion_handler="promote_discovery",
        disposition_handler="interpret_positive",
    ),
    TaskType.GENERATE_CANDIDATE_CLAIMS: _spec(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        invocation_model=GenerateCandidateClaimsInvocation,
        engine_input_model=GenerateCandidateClaimsInput,
        proposal_model=DiscoveryProposalBundle,
        dependency_builder=_generate_candidate_claims_dependencies,
        resource_builder=_no_resources,
        engine_input_builder=_mirror_engine_input(GenerateCandidateClaimsInput),
        promotion_handler="promote_discovery",
        disposition_handler="interpret_positive",
    ),
    TaskType.GENERATE_RETRIEVAL_QUERIES: _spec(
        TaskType.GENERATE_RETRIEVAL_QUERIES,
        invocation_model=GenerateRetrievalQueriesInvocation,
        engine_input_model=GenerateRetrievalQueriesInput,
        proposal_model=RetrievalQueryProposalBundle,
        dependency_builder=_claim_list_dependencies,
        resource_builder=_no_resources,
        engine_input_builder=_mirror_engine_input(GenerateRetrievalQueriesInput),
        promotion_handler="promote_retrieval_queries",
        disposition_handler="interpret_positive",
    ),
    TaskType.ASSESS_EVIDENCE: _spec(
        TaskType.ASSESS_EVIDENCE,
        invocation_model=AssessEvidenceInvocation,
        engine_input_model=AssessEvidenceInput,
        proposal_model=EvidenceRecordProposalBundle,
        dependency_builder=_assess_evidence_dependencies,
        resource_builder=_no_resources,
        engine_input_builder=_mirror_engine_input(AssessEvidenceInput),
        promotion_handler="promote_evidence",
        disposition_handler="interpret_positive",
    ),
    TaskType.AGGREGATE_PAPER_EVIDENCE: _spec(
        TaskType.AGGREGATE_PAPER_EVIDENCE,
        invocation_model=AggregatePaperEvidenceInvocation,
        engine_input_model=AggregatePaperEvidenceInput,
        proposal_model=ClaimPaperEvidenceProposal,
        dependency_builder=_aggregate_paper_evidence_dependencies,
        resource_builder=_no_resources,
        engine_input_builder=_mirror_engine_input(AggregatePaperEvidenceInput),
        promotion_handler="promote_claim_paper_evidence",
        disposition_handler="interpret_positive",
    ),
    TaskType.ASSESS_CLAIM: _spec(
        TaskType.ASSESS_CLAIM,
        invocation_model=AssessClaimInvocation,
        engine_input_model=AssessClaimInput,
        proposal_model=ClaimAssessmentProposal,
        dependency_builder=_assess_claim_dependencies,
        resource_builder=_no_resources,
        engine_input_builder=_mirror_engine_input(AssessClaimInput),
        promotion_handler="promote_claim_assessment",
        disposition_handler="interpret_claim_assessment",
    ),
    TaskType.REVISE_CLAIM: _spec(
        TaskType.REVISE_CLAIM,
        invocation_model=ReviseClaimInvocation,
        engine_input_model=ReviseClaimInput,
        proposal_model=RevisedClaimProposal,
        dependency_builder=_single_claim_dependencies,
        resource_builder=_no_resources,
        engine_input_builder=_mirror_engine_input(ReviseClaimInput),
        promotion_handler="promote_revised_claim",
        disposition_handler="interpret_positive",
    ),
    TaskType.VALIDATE_FINAL_CLAIM: _spec(
        TaskType.VALIDATE_FINAL_CLAIM,
        invocation_model=ValidateFinalClaimInvocation,
        engine_input_model=ValidateFinalClaimInput,
        proposal_model=FinalClaimValidationProposal,
        dependency_builder=_single_claim_dependencies,
        resource_builder=_no_resources,
        engine_input_builder=_mirror_engine_input(ValidateFinalClaimInput),
        promotion_handler="promote_final_claim_validation",
        disposition_handler="interpret_final_claim_validation",
    ),
    TaskType.GENERATE_PROPOSITIONS: _spec(
        TaskType.GENERATE_PROPOSITIONS,
        invocation_model=GeneratePropositionsInvocation,
        engine_input_model=GeneratePropositionsInput,
        proposal_model=PropositionProposalBundle,
        dependency_builder=_claim_list_dependencies,
        resource_builder=_no_resources,
        engine_input_builder=_mirror_engine_input(GeneratePropositionsInput),
        promotion_handler="promote_propositions",
        disposition_handler="interpret_positive",
    ),
    TaskType.AUDIT_PROPOSITION: _spec(
        TaskType.AUDIT_PROPOSITION,
        invocation_model=AuditPropositionInvocation,
        engine_input_model=AuditPropositionInput,
        proposal_model=SemanticAuditProposal,
        dependency_builder=_audit_proposition_dependencies,
        resource_builder=_no_resources,
        engine_input_builder=_mirror_engine_input(AuditPropositionInput),
        promotion_handler="promote_semantic_audit",
        disposition_handler="interpret_semantic_audit",
    ),
    TaskType.RENDER_PROSE: _spec(
        TaskType.RENDER_PROSE,
        invocation_model=RenderProseInvocation,
        engine_input_model=RenderProseInput,
        proposal_model=RenderedSentenceProposalBundle,
        dependency_builder=_render_prose_dependencies,
        resource_builder=_no_resources,
        engine_input_builder=_mirror_engine_input(RenderProseInput),
        promotion_handler="promote_rendered_sentences",
        disposition_handler="interpret_positive",
    ),
    TaskType.AUDIT_RENDERED_SENTENCE: _spec(
        TaskType.AUDIT_RENDERED_SENTENCE,
        invocation_model=AuditRenderedSentenceInvocation,
        engine_input_model=AuditRenderedSentenceInput,
        proposal_model=RenderedSentenceAuditProposal,
        dependency_builder=_audit_rendered_sentence_dependencies,
        resource_builder=_no_resources,
        engine_input_builder=_mirror_engine_input(AuditRenderedSentenceInput),
        promotion_handler="promote_rendered_sentence_audit",
        disposition_handler="interpret_rendered_sentence_audit",
    ),
}


assert set(TASK_SPECS) == set(TaskType)


def validate_task_spec_executable(spec: TaskSpec) -> None:
    """A12 preflight: fail with TASK_TYPE_NOT_IMPLEMENTED before any engine use.

    Handler names are validated against the exact dispatch tables that
    promotion.py consults (PROMOTION_HANDLERS / DISPOSITION_HANDLERS).
    """

    problems: list[str] = []
    if not spec.prompt_path.is_file():
        problems.append(f"prompt file {spec.prompt_path} does not exist")
    if spec.invocation_model is None:
        problems.append("invocation model is not registered")
    if spec.engine_input_model is None:
        problems.append("engine-input model is not registered")
    if spec.proposal_model is None:
        problems.append("proposal model is not registered")
    if not callable(spec.dependency_builder):
        problems.append("dependency builder is not registered")
    if not callable(spec.resource_builder):
        problems.append("resource builder is not registered")
    if not callable(spec.engine_input_builder):
        problems.append("engine-input builder is not registered")
    if spec.promotion_handler not in PROMOTION_HANDLERS:
        problems.append(
            f"promotion handler {spec.promotion_handler!r} is not implemented"
        )
    if spec.disposition_handler not in DISPOSITION_HANDLERS:
        problems.append(
            f"disposition handler {spec.disposition_handler!r} is not implemented"
        )
    if problems:
        raise TaskSpecNotExecutableError(spec.task_type, problems)


def executable_task_types() -> frozenset[TaskType]:
    executable: set[TaskType] = set()
    for task_type, spec in TASK_SPECS.items():
        try:
            validate_task_spec_executable(spec)
        except TaskSpecNotExecutableError:
            continue
        executable.add(task_type)
    return frozenset(executable)
