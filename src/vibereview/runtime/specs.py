"""Fixed TaskType-to-contract registry and TaskSpec executability preflight."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel, RootModel

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
    RetrievalDisposition,
    RetrievalQuery,
    RetrievedSpan,
    ReviewProcessFact,
    SemanticAuditResult,
    ThemeRecord,
)
from vibereview.validators import derive_semantic_audit_disposition

from .dto import (
    AcceptedDiscoveryArtifactInput,
    AggregatePaperEvidenceInput,
    AggregatePaperEvidenceInvocation,
    AssessClaimInput,
    AssessClaimInvocation,
    AssessEvidenceInput,
    AssessEvidenceInvocation,
    AssessEvidenceProposalBundle,
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
    invocation: AssessEvidenceInvocation, snapshot: RepositorySnapshot
) -> tuple[str, ...]:
    span_ids = set(invocation.canonical_span_refs)
    spans = tuple(
        span for span in snapshot.retrieved_spans if span.span_id in span_ids
    )
    query_ids = {span.retrieval.query_id for span in spans}
    paper_ids = {span.paper_id for span in spans}
    return _ordered_unique_dependencies(
        _typed_dependency_keys(CandidateClaim, (invocation.claim_id,)),
        _typed_dependency_keys(RetrievalQuery, (invocation.query_id,)),
        _typed_dependency_keys(RetrievedSpan, span_ids),
        _typed_dependency_keys(RetrievalDisposition, span_ids),
        _typed_dependency_keys(RetrievalQuery, query_ids),
        _typed_dependency_keys(Paper, paper_ids),
    )


def _typed_dependency_keys(
    model: type[BaseModel], identifiers: Iterable[str]
) -> tuple[str, ...]:
    return tuple(
        f"{model.__name__}:{identifier}"
        for identifier in sorted(set(identifiers))
    )


def _ordered_unique_dependencies(*groups: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(key for group in groups for key in group))


def _evidence_context_dependencies(
    snapshot: RepositorySnapshot,
    evidence_ids: Iterable[str],
    *,
    paper_ids: Iterable[str] = (),
) -> tuple[str, ...]:
    """Return the exact canonical source chain behind selected evidence."""

    selected_evidence_ids = set(evidence_ids)
    evidence = tuple(
        item
        for item in snapshot.evidence_records
        if item.evidence_id in selected_evidence_ids
    )
    span_ids = {item.retrieved_span_id for item in evidence}
    spans = tuple(
        item for item in snapshot.retrieved_spans if item.span_id in span_ids
    )
    query_ids = {item.retrieval.query_id for item in spans}
    source_paper_ids = (
        set(paper_ids)
        | {item.paper_id for item in evidence}
        | {item.paper_id for item in spans}
    )
    return _ordered_unique_dependencies(
        _typed_dependency_keys(Paper, source_paper_ids),
        _typed_dependency_keys(EvidenceRecord, selected_evidence_ids),
        _typed_dependency_keys(RetrievedSpan, span_ids),
        _typed_dependency_keys(RetrievalDisposition, span_ids),
        _typed_dependency_keys(RetrievalQuery, query_ids),
    )


def _claim_evidence_context_dependencies(
    snapshot: RepositorySnapshot, cpe_ids: Iterable[str]
) -> tuple[str, ...]:
    """Return selected CPEs and the exact evidence/source chain they summarize."""

    selected_cpe_ids = set(cpe_ids)
    cpes = tuple(
        item
        for item in snapshot.claim_paper_evidence
        if item.claim_paper_evidence_id in selected_cpe_ids
    )
    evidence_ids = {
        evidence_id for cpe in cpes for evidence_id in cpe.evidence_ids
    }
    paper_ids = {cpe.paper_id for cpe in cpes}
    return _ordered_unique_dependencies(
        _typed_dependency_keys(ClaimPaperEvidence, selected_cpe_ids),
        _evidence_context_dependencies(
            snapshot, evidence_ids, paper_ids=paper_ids
        ),
    )


def _aggregate_paper_evidence_dependencies(
    invocation: AggregatePaperEvidenceInvocation, snapshot: RepositorySnapshot
) -> tuple[str, ...]:
    evidence_ids = list(invocation.evidence_ids)
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError("aggregate evidence_ids must not contain duplicates")

    dependencies = _ordered_unique_dependencies(
        _typed_dependency_keys(CandidateClaim, (invocation.claim_id,)),
        _typed_dependency_keys(Paper, (invocation.paper_id,)),
        _typed_dependency_keys(EvidenceRecord, evidence_ids),
    )
    index = snapshot.object_index()
    if any(dependency not in index for dependency in dependencies):
        # Preserve the canonical missing-dependency failure at task creation.
        return dependencies

    complete_ids = {
        evidence.evidence_id
        for evidence in snapshot.evidence_records
        if evidence.claim_id == invocation.claim_id
        and evidence.paper_id == invocation.paper_id
    }
    if set(evidence_ids) != complete_ids:
        raise ValueError(
            "aggregate evidence_ids must exactly equal all current EvidenceRecord "
            "IDs for the invocation claim and paper"
        )
    return _ordered_unique_dependencies(
        _typed_dependency_keys(CandidateClaim, (invocation.claim_id,)),
        _evidence_context_dependencies(
            snapshot, evidence_ids, paper_ids=(invocation.paper_id,)
        ),
    )


def _assess_claim_dependencies(
    invocation: AssessClaimInvocation, snapshot: RepositorySnapshot
) -> tuple[str, ...]:
    cpe_ids = list(invocation.claim_paper_evidence_ids)
    if len(cpe_ids) != len(set(cpe_ids)):
        raise ValueError(
            "claim_paper_evidence_ids must not contain duplicates"
        )

    dependencies = _ordered_unique_dependencies(
        _typed_dependency_keys(CandidateClaim, (invocation.claim_id,)),
        _typed_dependency_keys(ClaimPaperEvidence, cpe_ids),
    )
    index = snapshot.object_index()
    if any(dependency not in index for dependency in dependencies):
        # Preserve the canonical missing-dependency failure at task creation.
        return dependencies

    complete_ids = {
        cpe.claim_paper_evidence_id
        for cpe in snapshot.claim_paper_evidence
        if cpe.claim_id == invocation.claim_id
    }
    if set(cpe_ids) != complete_ids:
        raise ValueError(
            "claim_paper_evidence_ids must exactly equal all current CPE IDs "
            "for the invocation claim"
        )
    return _ordered_unique_dependencies(
        _typed_dependency_keys(CandidateClaim, (invocation.claim_id,)),
        _claim_evidence_context_dependencies(snapshot, cpe_ids),
    )


def _post_assessment_claim_dependencies(
    claim_id: str, snapshot: RepositorySnapshot
) -> tuple[str, ...]:
    cpe_ids = {
        cpe.claim_paper_evidence_id
        for cpe in snapshot.claim_paper_evidence
        if cpe.claim_id == claim_id
    }
    return _ordered_unique_dependencies(
        _typed_dependency_keys(CandidateClaim, (claim_id,)),
        _typed_dependency_keys(ClaimAssessment, (claim_id,)),
        _claim_evidence_context_dependencies(snapshot, cpe_ids),
    )


def _revise_claim_dependencies(
    invocation: ReviseClaimInvocation, snapshot: RepositorySnapshot
) -> tuple[str, ...]:
    return _post_assessment_claim_dependencies(invocation.claim_id, snapshot)


def _validate_final_claim_dependencies(
    invocation: ValidateFinalClaimInvocation, snapshot: RepositorySnapshot
) -> tuple[str, ...]:
    return _post_assessment_claim_dependencies(
        invocation.claim_id, snapshot
    )


def _proposition_source_dependencies(
    invocation: GeneratePropositionsInvocation | AuditPropositionInvocation,
    snapshot: RepositorySnapshot,
) -> tuple[str, ...]:
    packet_ids = set(invocation.claim_packet_ids)
    packets = tuple(
        item for item in snapshot.claim_packets if item.claim_id in packet_ids
    )
    claim_ids = {item.claim_id for item in packets}
    candidate_claims = tuple(
        item for item in snapshot.candidate_claims if item.claim_id in claim_ids
    )
    theme_ids = {item.theme_id for item in candidate_claims}
    cpe_ids = {
        cpe_id
        for packet in packets
        for cpe_id in packet.claim_paper_evidence_ids
    }
    corpus_fact_ids = set(invocation.corpus_fact_ids)
    corpus_facts = tuple(
        item
        for item in snapshot.corpus_facts
        if item.corpus_fact_id in corpus_fact_ids
    )
    corpus_paper_ids = {
        paper_id for fact in corpus_facts for paper_id in fact.source_paper_ids
    }
    return _ordered_unique_dependencies(
        _typed_dependency_keys(ClaimPacket, packet_ids),
        _typed_dependency_keys(CandidateClaim, claim_ids),
        _typed_dependency_keys(ThemeRecord, theme_ids),
        _typed_dependency_keys(ClaimAssessment, claim_ids),
        _typed_dependency_keys(FinalClaimValidation, claim_ids),
        _claim_evidence_context_dependencies(snapshot, cpe_ids),
        _typed_dependency_keys(CorpusFact, corpus_fact_ids),
        _typed_dependency_keys(Paper, corpus_paper_ids),
        _typed_dependency_keys(ReviewProcessFact, invocation.process_fact_ids),
    )


def _generate_propositions_dependencies(
    invocation: GeneratePropositionsInvocation, snapshot: RepositorySnapshot
) -> tuple[str, ...]:
    return _proposition_source_dependencies(invocation, snapshot)


def _audit_proposition_dependencies(
    invocation: AuditPropositionInvocation, snapshot: RepositorySnapshot
) -> tuple[str, ...]:
    return _proposition_source_dependencies(invocation, snapshot)


def _passing_proposition_dependencies(
    proposition_ids: Iterable[str], snapshot: RepositorySnapshot
) -> tuple[str, ...]:
    selected_ids = set(proposition_ids)
    proposition_keys = _typed_dependency_keys(PropositionRecord, selected_ids)
    index = snapshot.object_index()
    if any(key not in index for key in proposition_keys):
        return proposition_keys

    audit_ids: set[str] = set()
    for proposition_id in sorted(selected_ids):
        audits = tuple(
            audit
            for audit in snapshot.semantic_audits
            if audit.target_id == proposition_id
        )
        if len(audits) != 1:
            raise ValueError(
                f"proposition {proposition_id} must have exactly one semantic audit"
            )
        if derive_semantic_audit_disposition(audits[0]).value != "PASS":
            raise ValueError(
                f"proposition {proposition_id} does not have a PASS semantic audit"
            )
        audit_ids.add(audits[0].audit_id)
    return _ordered_unique_dependencies(
        proposition_keys,
        _typed_dependency_keys(SemanticAuditResult, audit_ids),
    )


def _render_prose_dependencies(
    invocation: RenderProseInvocation, snapshot: RepositorySnapshot
) -> tuple[str, ...]:
    return _passing_proposition_dependencies(
        invocation.proposition_ids, snapshot
    )


def _audit_rendered_sentence_dependencies(
    invocation: AuditRenderedSentenceInvocation, snapshot: RepositorySnapshot
) -> tuple[str, ...]:
    return _passing_proposition_dependencies(
        invocation.source_proposition_ids, snapshot
    )


def _no_resources(
    _invocation, _snapshot: RepositorySnapshot, _context: ProjectContext
) -> tuple[TaskResourceRequest, ...]:
    return ()


def _assess_evidence_resources(
    invocation: AssessEvidenceInvocation,
    _snapshot: RepositorySnapshot,
    _context: ProjectContext,
) -> tuple[TaskResourceRequest, ...]:
    return (
        TaskResourceRequest(
            resource_id="RES0001",
            logical_name="retrieval_ledger.json",
            source_path=invocation.retrieval_ledger_path,
            media_type="application/json",
        ),
    )


def _audit_proposition_resources(
    invocation: AuditPropositionInvocation,
    _snapshot: RepositorySnapshot,
    _context: ProjectContext,
) -> tuple[TaskResourceRequest, ...]:
    return (
        TaskResourceRequest(
            resource_id="RES0001",
            logical_name="accepted_proposition_draft.json",
            source_path=invocation.draft_artifact_path,
            media_type="application/json",
        ),
    )


def _audit_rendered_sentence_resources(
    invocation: AuditRenderedSentenceInvocation,
    _snapshot: RepositorySnapshot,
    _context: ProjectContext,
) -> tuple[TaskResourceRequest, ...]:
    return (
        TaskResourceRequest(
            resource_id="RES0001",
            logical_name="accepted_rendered_sentence_draft.json",
            source_path=invocation.draft_artifact_path,
            media_type="application/json",
        ),
    )


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


def _corpus_challenger_resources(
    invocation: CorpusChallengerInvocation,
    _snapshot: RepositorySnapshot,
    _context: ProjectContext,
) -> tuple[TaskResourceRequest, ...]:
    if invocation.discovery_artifact is None:
        if invocation.paper_resource_paths or invocation.paper_source_hashes:
            raise ValueError("legacy corpus challenger cannot carry partial strict resources")
        return ()
    requests = [
        TaskResourceRequest(
            resource_id="RES0001",
            logical_name="accepted_discovery_parse.json",
            source_path=invocation.discovery_artifact.artifact_path,
            media_type="application/json",
        )
    ]
    requests.extend(
        TaskResourceRequest(
            resource_id=f"RES{ordinal:04d}",
            logical_name=f"{paper_id}.md",
            source_path=source_path,
            media_type="text/markdown",
        )
        for ordinal, (paper_id, source_path) in enumerate(
            zip(
                invocation.paper_ids,
                invocation.paper_resource_paths,
                strict=True,
            ),
            start=2,
        )
    )
    return tuple(requests)


def _artifact_input(
    artifact,
    resource: SnapshottedResource,
) -> AcceptedDiscoveryArtifactInput:
    return AcceptedDiscoveryArtifactInput(
        artifact_kind=artifact.artifact_kind,
        owner_generation=artifact.owner_generation,
        source_generation=artifact.source_generation,
        task_id=artifact.task_id,
        semantic_task_key=artifact.semantic_task_key,
        artifact_hash=artifact.artifact_hash,
        resource_id=resource.resource_id,
    )


def _corpus_challenger_engine_input(
    invocation: CorpusChallengerInvocation,
    resources: tuple[SnapshottedResource, ...],
) -> CorpusChallengerInput:
    if invocation.discovery_artifact is None:
        if resources:
            raise ValueError("legacy corpus challenger received unexpected resources")
        return CorpusChallengerInput(topic=invocation.topic, paper_ids=invocation.paper_ids)
    if len(resources) != len(invocation.paper_ids) + 1:
        raise ValueError("strict corpus challenger requires one parse artifact and every paper")
    parse_resource, *paper_resources = resources
    if (
        parse_resource.resource_id != "RES0001"
        or parse_resource.media_type != "application/json"
        or any(item.media_type != "text/markdown" for item in paper_resources)
    ):
        raise ValueError("corpus challenger resource layout is invalid")
    return CorpusChallengerInput(
        topic=invocation.topic,
        paper_ids=invocation.paper_ids,
        discovery_artifact=_artifact_input(
            invocation.discovery_artifact, parse_resource
        ),
        paper_resource_ids=[item.resource_id for item in paper_resources],
        paper_source_hashes=invocation.paper_source_hashes,
    )


def _generate_candidate_claims_resources(
    invocation: GenerateCandidateClaimsInvocation,
    _snapshot: RepositorySnapshot,
    _context: ProjectContext,
) -> tuple[TaskResourceRequest, ...]:
    if invocation.discovery_artifact is None:
        return ()
    assert invocation.challenge_artifact is not None
    return (
        TaskResourceRequest(
            resource_id="RES0001",
            logical_name="accepted_discovery_parse.json",
            source_path=invocation.discovery_artifact.artifact_path,
            media_type="application/json",
        ),
        TaskResourceRequest(
            resource_id="RES0002",
            logical_name="accepted_corpus_challenge.json",
            source_path=invocation.challenge_artifact.artifact_path,
            media_type="application/json",
        ),
    )


def _generate_candidate_claims_engine_input(
    invocation: GenerateCandidateClaimsInvocation,
    resources: tuple[SnapshottedResource, ...],
) -> GenerateCandidateClaimsInput:
    if invocation.discovery_artifact is None:
        if resources:
            raise ValueError("legacy candidate generation received unexpected resources")
        return GenerateCandidateClaimsInput(
            topic=invocation.topic,
            existing_theme_ids=invocation.existing_theme_ids,
        )
    assert invocation.challenge_artifact is not None
    if (
        len(resources) != 2
        or tuple(item.resource_id for item in resources) != ("RES0001", "RES0002")
        or any(item.media_type != "application/json" for item in resources)
    ):
        raise ValueError("candidate generation requires the exact parse and challenge resources")
    return GenerateCandidateClaimsInput(
        topic=invocation.topic,
        existing_theme_ids=invocation.existing_theme_ids,
        discovery_artifact=_artifact_input(invocation.discovery_artifact, resources[0]),
        challenge_artifact=_artifact_input(invocation.challenge_artifact, resources[1]),
    )


def _assess_evidence_engine_input(
    invocation: AssessEvidenceInvocation,
    resources: tuple[SnapshottedResource, ...],
) -> AssessEvidenceInput:
    if (
        len(resources) != 1
        or resources[0].resource_id != "RES0001"
        or resources[0].media_type != "application/json"
    ):
        raise ValueError(
            "AssessEvidenceInput requires exactly the RES0001 JSON retrieval ledger"
        )
    return AssessEvidenceInput(
        source_generation=invocation.source_generation,
        claim_id=invocation.claim_id,
        query_id=invocation.query_id,
        candidate_refs=list(invocation.candidate_refs),
        canonical_span_refs=list(invocation.canonical_span_refs),
        retrieval_ledger_resource_id=resources[0].resource_id,
    )


def _require_draft_resource(
    invocation: AuditPropositionInvocation | AuditRenderedSentenceInvocation,
    resources: tuple[SnapshottedResource, ...],
    *,
    logical_name: str,
) -> SnapshottedResource:
    if (
        len(resources) != 1
        or resources[0].resource_id != "RES0001"
        or resources[0].logical_name != logical_name
        or resources[0].media_type != "application/json"
    ):
        raise ValueError(
            f"draft input requires exactly RES0001 {logical_name} JSON resource"
        )
    if resources[0].content_hash != invocation.draft_artifact_hash:
        raise ValueError("draft resource hash differs from draft_artifact_hash")
    return resources[0]


def _audit_proposition_engine_input(
    invocation: AuditPropositionInvocation,
    resources: tuple[SnapshottedResource, ...],
) -> AuditPropositionInput:
    resource = _require_draft_resource(
        invocation,
        resources,
        logical_name="accepted_proposition_draft.json",
    )
    return AuditPropositionInput(
        draft_kind=invocation.draft_kind,
        draft_owner_generation=invocation.draft_owner_generation,
        draft_source_generation=invocation.draft_source_generation,
        draft_task_id=invocation.draft_task_id,
        draft_artifact_hash=invocation.draft_artifact_hash,
        draft_local_ref=invocation.draft_local_ref,
        claim_packet_ids=list(invocation.claim_packet_ids),
        corpus_fact_ids=list(invocation.corpus_fact_ids),
        process_fact_ids=list(invocation.process_fact_ids),
        draft_resource_id=resource.resource_id,
    )


def _audit_rendered_sentence_engine_input(
    invocation: AuditRenderedSentenceInvocation,
    resources: tuple[SnapshottedResource, ...],
) -> AuditRenderedSentenceInput:
    resource = _require_draft_resource(
        invocation,
        resources,
        logical_name="accepted_rendered_sentence_draft.json",
    )
    return AuditRenderedSentenceInput(
        draft_kind=invocation.draft_kind,
        draft_owner_generation=invocation.draft_owner_generation,
        draft_source_generation=invocation.draft_source_generation,
        draft_task_id=invocation.draft_task_id,
        draft_artifact_hash=invocation.draft_artifact_hash,
        draft_local_ref=invocation.draft_local_ref,
        source_proposition_ids=list(invocation.source_proposition_ids),
        draft_resource_id=resource.resource_id,
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
    version: str = "1",
    prompt_version: str = "1",
) -> TaskSpec:
    return TaskSpec(
        task_type=task_type,
        version=version,
        invocation_model=invocation_model,
        engine_input_model=engine_input_model,
        proposal_model=proposal_model,
        dependency_builder=dependency_builder,
        resource_builder=resource_builder,
        engine_input_builder=engine_input_builder,
        prompt_path=PROMPT_ROOT / f"{task_type.value}.md",
        prompt_version=prompt_version,
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
        version="2",
        prompt_version="2",
    ),
    TaskType.CORPUS_CHALLENGER: _spec(
        TaskType.CORPUS_CHALLENGER,
        invocation_model=CorpusChallengerInvocation,
        engine_input_model=CorpusChallengerInput,
        proposal_model=DiscoveryProposalBundle,
        dependency_builder=_corpus_challenger_dependencies,
        resource_builder=_corpus_challenger_resources,
        engine_input_builder=_corpus_challenger_engine_input,
        promotion_handler="promote_discovery",
        disposition_handler="interpret_positive",
        version="2",
        prompt_version="2",
    ),
    TaskType.GENERATE_CANDIDATE_CLAIMS: _spec(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        invocation_model=GenerateCandidateClaimsInvocation,
        engine_input_model=GenerateCandidateClaimsInput,
        proposal_model=DiscoveryProposalBundle,
        dependency_builder=_generate_candidate_claims_dependencies,
        resource_builder=_generate_candidate_claims_resources,
        engine_input_builder=_generate_candidate_claims_engine_input,
        promotion_handler="promote_discovery",
        disposition_handler="interpret_positive",
        version="2",
        prompt_version="2",
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
        proposal_model=AssessEvidenceProposalBundle,
        dependency_builder=_assess_evidence_dependencies,
        resource_builder=_assess_evidence_resources,
        engine_input_builder=_assess_evidence_engine_input,
        promotion_handler="promote_evidence",
        disposition_handler="interpret_positive",
        version="2",
        prompt_version="2",
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
        dependency_builder=_revise_claim_dependencies,
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
        dependency_builder=_validate_final_claim_dependencies,
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
        dependency_builder=_generate_propositions_dependencies,
        resource_builder=_no_resources,
        engine_input_builder=_mirror_engine_input(GeneratePropositionsInput),
        promotion_handler="accept_proposition_drafts",
        disposition_handler="interpret_positive",
        version="2",
        prompt_version="2",
    ),
    TaskType.AUDIT_PROPOSITION: _spec(
        TaskType.AUDIT_PROPOSITION,
        invocation_model=AuditPropositionInvocation,
        engine_input_model=AuditPropositionInput,
        proposal_model=SemanticAuditProposal,
        dependency_builder=_audit_proposition_dependencies,
        resource_builder=_audit_proposition_resources,
        engine_input_builder=_audit_proposition_engine_input,
        promotion_handler="promote_proposition_with_audit",
        disposition_handler="interpret_semantic_audit",
        version="2",
        prompt_version="2",
    ),
    TaskType.RENDER_PROSE: _spec(
        TaskType.RENDER_PROSE,
        invocation_model=RenderProseInvocation,
        engine_input_model=RenderProseInput,
        proposal_model=RenderedSentenceProposalBundle,
        dependency_builder=_render_prose_dependencies,
        resource_builder=_no_resources,
        engine_input_builder=_mirror_engine_input(RenderProseInput),
        promotion_handler="accept_rendered_sentence_drafts",
        disposition_handler="interpret_positive",
        version="2",
        prompt_version="2",
    ),
    TaskType.AUDIT_RENDERED_SENTENCE: _spec(
        TaskType.AUDIT_RENDERED_SENTENCE,
        invocation_model=AuditRenderedSentenceInvocation,
        engine_input_model=AuditRenderedSentenceInput,
        proposal_model=RenderedSentenceAuditProposal,
        dependency_builder=_audit_rendered_sentence_dependencies,
        resource_builder=_audit_rendered_sentence_resources,
        engine_input_builder=_audit_rendered_sentence_engine_input,
        promotion_handler="promote_rendered_sentence_with_audit",
        disposition_handler="interpret_rendered_sentence_audit",
        version="2",
        prompt_version="2",
    ),
}


assert set(TASK_SPECS) == set(TaskType)


def _resolve_local_ref(schema: dict, ref: str) -> dict | None:
    if not ref.startswith("#/"):
        return None
    target: object = schema
    for part in ref[2:].split("/"):
        if not isinstance(target, dict) or part not in target:
            return None
        target = target[part]
    return target if isinstance(target, dict) else None


def _proposal_object_root_problems(proposal_model: type[BaseModel]) -> list[str]:
    """goal.md §6.11: every engine proposal must be a JSON object at the root."""

    name = getattr(proposal_model, "__name__", repr(proposal_model))
    if not isinstance(proposal_model, type) or not issubclass(
        proposal_model, BaseModel
    ):
        return [f"proposal model {name} is not a Pydantic BaseModel subclass"]
    if issubclass(proposal_model, RootModel):
        return [
            f"proposal model {name} is a RootModel; "
            "engine proposals must be named object models"
        ]
    schema = proposal_model.model_json_schema()
    root = schema
    seen_refs: set[str] = set()
    while isinstance(root.get("$ref"), str):
        ref = root["$ref"]
        if ref in seen_refs:
            break
        seen_refs.add(ref)
        resolved = _resolve_local_ref(schema, ref)
        if resolved is None:
            return [
                f"proposal model {name} has an unresolvable top-level $ref {ref!r}"
            ]
        root = resolved
    if root.get("type") != "object":
        return [
            f"proposal model {name} schema root must be type 'object', "
            f"got {root.get('type')!r}"
        ]
    return []


def validate_task_spec_executable(
    spec: TaskSpec,
    *,
    additional_promotion_handlers: frozenset[str] = frozenset(),
) -> None:
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
    else:
        problems.extend(_proposal_object_root_problems(spec.proposal_model))
    if not callable(spec.dependency_builder):
        problems.append("dependency builder is not registered")
    if not callable(spec.resource_builder):
        problems.append("resource builder is not registered")
    if not callable(spec.engine_input_builder):
        problems.append("engine-input builder is not registered")
    if (
        spec.promotion_handler not in PROMOTION_HANDLERS
        and spec.promotion_handler not in additional_promotion_handlers
    ):
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
