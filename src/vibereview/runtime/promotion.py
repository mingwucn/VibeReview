"""Proposal validation, scientific interpretation, and locked promotion handlers."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from pydantic import BaseModel

from vibereview.enums import (
    AuditClassVerdict,
    AuditProvenanceVerdict,
    ClaimDecision,
    FinalClaimStatus,
    RetrievalIntent,
)
from vibereview.ids import QUERY_INTENT_CODES, candidate_claim_hash, parse_query_id
from vibereview.models import (
    CandidateClaim,
    ClaimAssessment,
    ClaimPacket,
    ClaimPaperEvidence,
    FinalClaimValidation,
    FinalPaperRelation,
    RetrievalQuery,
    SemanticAuditResult,
    ThemeRecord,
)
from .dto import (
    ClaimAssessmentProposal,
    ClaimPaperEvidenceProposal,
    DiscoveryProposalBundle,
    FinalClaimValidationProposal,
    GenerateRetrievalQueriesInvocation,
    MAX_RETRIEVAL_QUERY_CHARS,
    MAX_RETRIEVAL_QUERY_UTF8_BYTES,
    RenderedSentenceAuditProposal,
    RetrievalQueryProposalBundle,
    SemanticAuditProposal,
    ValidateFinalClaimInvocation,
)
from .records import TaskSpec, TransitionDecision
from .registry import CanonicalIdRegistry, IdKind
from .repository import PromotionPayload
from .state import RepositorySnapshot


class ProposalValidationError(ValueError):
    pass


class ContractImplementationError(RuntimeError):
    pass


_REQUIRED_RETRIEVAL_INTENTS = frozenset(
    {
        RetrievalIntent.SUPPORT,
        RetrievalIntent.CONTRADICTION,
        RetrievalIntent.BOUNDARY,
        RetrievalIntent.ALTERNATIVE,
    }
)

# Keep this tokenization policy aligned with library.retrieval._query_terms
# without making the provider-neutral runtime depend on the optional library
# package. Only terms longer than two Unicode word characters are usable by
# deterministic retrieval, and retrieval retains at most the first 64.
_MAX_DETERMINISTIC_RETRIEVAL_TERMS = 64
_DETERMINISTIC_RETRIEVAL_TERM_PATTERN = re.compile(
    r"[^\W_]+", flags=re.UNICODE
)


def _deterministic_retrieval_terms(query_text: str) -> list[str]:
    terms = [
        term.casefold()
        for term in _DETERMINISTIC_RETRIEVAL_TERM_PATTERN.findall(query_text)
    ]
    return list(dict.fromkeys(term for term in terms if len(term) > 2))[
        :_MAX_DETERMINISTIC_RETRIEVAL_TERMS
    ]


def _unique_local_refs(proposal: DiscoveryProposalBundle) -> set[str]:
    refs = [item.local_ref for item in (*proposal.themes, *proposal.claims)]
    if len(refs) != len(set(refs)):
        raise ProposalValidationError("local_ref must be unique within proposal bundle")
    return set(refs)


def _validate_discovery(
    proposal: DiscoveryProposalBundle, snapshot: RepositorySnapshot
) -> None:
    local_refs = _unique_local_refs(proposal)
    local_themes = {theme.local_ref for theme in proposal.themes}
    canonical_themes = {theme.theme_id for theme in snapshot.themes}
    allowed_themes = local_themes | canonical_themes
    for theme in proposal.themes:
        if theme.parent_ref is not None and theme.parent_ref not in allowed_themes:
            raise ProposalValidationError(
                f"unknown local or canonical parent_ref {theme.parent_ref}"
            )
    for claim in proposal.claims:
        if claim.theme_ref not in allowed_themes:
            raise ProposalValidationError(
                f"unknown local or canonical theme_ref {claim.theme_ref}"
            )

    parent_by_ref = {
        theme.local_ref: theme.parent_ref
        for theme in proposal.themes
        if theme.parent_ref in local_refs
    }
    for start in parent_by_ref:
        current = start
        seen: set[str] = set()
        while current in parent_by_ref:
            if current in seen:
                raise ProposalValidationError("proposal-local theme hierarchy is cyclic")
            seen.add(current)
            current = parent_by_ref[current]  # type: ignore[assignment]


def _validate_retrieval_queries(
    proposal: RetrievalQueryProposalBundle,
    snapshot: RepositorySnapshot,
    dependency_keys: Iterable[str] | None,
    registry: CanonicalIdRegistry | None,
    accepted_allocations: Mapping[str, str] | None,
    invocation: BaseModel | None,
) -> None:
    local_refs = [item.local_ref for item in proposal.queries]
    if len(local_refs) != len(set(local_refs)):
        raise ProposalValidationError(
            "local_ref must be unique within retrieval-query proposal bundle"
        )

    canonical_claims = {claim.claim_id for claim in snapshot.candidate_claims}
    scoped_claims = (
        {
            key.removeprefix("CandidateClaim:")
            for key in dependency_keys
            if key.startswith("CandidateClaim:")
        }
        if dependency_keys is not None
        else None
    )
    for query in proposal.queries:
        if (
            len(query.query_text) > MAX_RETRIEVAL_QUERY_CHARS
            or len(query.query_text.encode("utf-8"))
            > MAX_RETRIEVAL_QUERY_UTF8_BYTES
        ):
            raise ProposalValidationError(
                "retrieval query exceeds its deterministic text budget"
            )
        if not _deterministic_retrieval_terms(query.query_text):
            raise ProposalValidationError(
                "retrieval query has no usable deterministic retrieval terms"
            )
        if query.claim_ref not in canonical_claims:
            raise ProposalValidationError(f"unknown claim_ref {query.claim_ref}")
        if scoped_claims is not None and query.claim_ref not in scoped_claims:
            raise ProposalValidationError(
                f"claim_ref {query.claim_ref} is outside the task dependency scope"
            )

    accepted_allocations = accepted_allocations or {}
    canonical_queries = {query.query_id: query for query in snapshot.retrieval_queries}
    pending = []
    for query in proposal.queries:
        accepted_id = accepted_allocations.get(query.local_ref)
        if accepted_id is None:
            pending.append(query)
            continue
        canonical = canonical_queries.get(accepted_id)
        if canonical is None or (
            canonical.claim_id != query.claim_ref
            or canonical.intent != query.intent
            or canonical.query_text != query.query_text
        ):
            raise ProposalValidationError(
                f"accepted allocation for {query.local_ref} does not match its "
                "canonical RetrievalQuery"
            )

    current_ordinals: dict[tuple[str, str], int] = {}
    for existing in snapshot.retrieval_queries:
        claim_id, intent, ordinal = parse_query_id(existing.query_id)
        key = (claim_id, intent)
        current_ordinals[key] = max(current_ordinals.get(key, 0), ordinal)
    if registry is not None:
        for registry_key, ordinal in registry.query_ordinals.items():
            claim_id, intent_code = registry_key.split(":", 1)
            key = (claim_id, QUERY_INTENT_CODES[intent_code])
            current_ordinals[key] = max(current_ordinals.get(key, 0), ordinal)

    capacity_claims = scoped_claims if scoped_claims is not None else canonical_claims
    total_capacity = sum(
        99 - current_ordinals.get((claim_id, intent.value), 0)
        for claim_id in capacity_claims
        for intent in RetrievalIntent
    )
    if len(pending) > total_capacity:
        raise ProposalValidationError(
            "retrieval-query proposal exceeds the total remaining canonical "
            "ordinal capacity for its dependency scope"
        )

    pending_counts = Counter((item.claim_ref, item.intent.value) for item in pending)
    for (claim_id, intent), count in pending_counts.items():
        remaining = 99 - current_ordinals.get((claim_id, intent), 0)
        if count > remaining:
            raise ProposalValidationError(
                "retrieval-query proposal exceeds remaining canonical ordinal "
                f"capacity for {claim_id}/{intent}"
            )

    if invocation is not None:
        if not isinstance(invocation, GenerateRetrievalQueriesInvocation):
            raise ProposalValidationError(
                "retrieval-query validation received the wrong invocation context"
            )
        invoked_claims = set(invocation.claim_ids)
        intents_by_claim: dict[str, set[RetrievalIntent]] = {
            claim_id: set() for claim_id in invoked_claims
        }
        for query in proposal.queries:
            if query.claim_ref in intents_by_claim:
                intents_by_claim[query.claim_ref].add(query.intent)
        missing = {
            claim_id: sorted(
                intent.value
                for intent in _REQUIRED_RETRIEVAL_INTENTS - intents_by_claim[claim_id]
            )
            for claim_id in sorted(invoked_claims)
            if not _REQUIRED_RETRIEVAL_INTENTS <= intents_by_claim[claim_id]
        }
        if missing:
            detail = "; ".join(
                f"{claim_id}: {','.join(intents)}"
                for claim_id, intents in missing.items()
            )
            raise ProposalValidationError(
                "retrieval-query proposal is missing required intents for invoked "
                f"claims ({detail})"
            )


def _require_dependency_scope(
    dependency_keys: frozenset[str] | None,
    dependency: str,
    description: str,
) -> None:
    if dependency_keys is not None and dependency not in dependency_keys:
        raise ProposalValidationError(
            f"{description} is outside the task dependency scope"
        )


def _claim_paper_evidence_from_proposal(
    proposal: ClaimPaperEvidenceProposal,
) -> ClaimPaperEvidence:
    return ClaimPaperEvidence(
        claim_paper_evidence_id=CanonicalIdRegistry.claim_paper_evidence_id(
            proposal.claim_ref, proposal.paper_ref
        ),
        claim_id=proposal.claim_ref,
        paper_id=proposal.paper_ref,
        evidence_ids=proposal.evidence_refs,
        relation_to_candidate=proposal.relation_to_candidate,
        component_relations=proposal.component_relations,
        strength=proposal.strength,
        within_paper_consistency=proposal.within_paper_consistency,
        assessment_note=proposal.assessment_note,
    )


def _claim_assessment_from_proposal(
    proposal: ClaimAssessmentProposal,
) -> ClaimAssessment:
    return ClaimAssessment(
        claim_id=proposal.claim_ref,
        aggregate_strength=proposal.aggregate_strength,
        evidence_sufficiency=proposal.evidence_sufficiency,
        decision=proposal.decision,
        rejection_basis=proposal.rejection_basis,
        support_summary=proposal.support_summary,
        contradiction_summary=proposal.contradiction_summary,
        qualification_summary=proposal.qualification_summary,
        reason=proposal.reason,
    )


def _upsert_by_attribute(
    values: tuple[Any, ...],
    *,
    attribute: str,
    replacement: Any,
) -> tuple[Any, ...]:
    key = getattr(replacement, attribute)
    for position, existing in enumerate(values):
        if getattr(existing, attribute) != key:
            continue
        if existing == replacement:
            return values
        return values[:position] + (replacement,) + values[position + 1 :]
    return values + (replacement,)


def _final_validation_from_proposal(
    proposal: FinalClaimValidationProposal,
) -> FinalClaimValidation:
    return FinalClaimValidation(
        claim_id=proposal.claim_ref,
        final_claim=proposal.final_claim,
        status=proposal.status,
        paper_relations=[
            FinalPaperRelation(
                claim_paper_evidence_id=relation.claim_paper_evidence_ref,
                paper_id=relation.paper_ref,
                relation_to_final_claim=relation.relation_to_final_claim,
            )
            for relation in proposal.paper_relations
        ],
        scope_check=proposal.scope_check,
        certainty_check=proposal.certainty_check,
        causal_language_check=proposal.causal_language_check,
        numerical_claim_check=proposal.numerical_claim_check,
        notes=proposal.notes,
    )


def _claim_packet_for_validation(
    snapshot: RepositorySnapshot,
    validation: FinalClaimValidation,
) -> ClaimPacket | None:
    if validation.status is not FinalClaimStatus.VALID:
        return None
    claim = next(
        item
        for item in snapshot.candidate_claims
        if item.claim_id == validation.claim_id
    )
    assessment = next(
        item
        for item in snapshot.claim_assessments
        if item.claim_id == validation.claim_id
    )
    cpe_ids = sorted(
        cpe.claim_paper_evidence_id
        for cpe in snapshot.claim_paper_evidence
        if cpe.claim_id == validation.claim_id
    )
    return ClaimPacket(
        claim_id=claim.claim_id,
        theme_id=claim.theme_id,
        candidate_claim=claim.candidate_claim,
        final_claim=validation.final_claim,
        aggregate_strength=assessment.aggregate_strength,
        claim_paper_evidence_ids=cpe_ids,
    )


def _validate_claim_paper_evidence(
    proposal: ClaimPaperEvidenceProposal,
    snapshot: RepositorySnapshot,
    dependency_keys: frozenset[str] | None,
) -> None:
    if proposal.claim_ref not in {
        claim.claim_id for claim in snapshot.candidate_claims
    }:
        raise ProposalValidationError(f"unknown claim_ref {proposal.claim_ref}")
    if proposal.paper_ref not in {paper.paper_id for paper in snapshot.papers}:
        raise ProposalValidationError(f"unknown paper_ref {proposal.paper_ref}")
    _require_dependency_scope(
        dependency_keys,
        f"CandidateClaim:{proposal.claim_ref}",
        f"claim_ref {proposal.claim_ref}",
    )
    _require_dependency_scope(
        dependency_keys,
        f"Paper:{proposal.paper_ref}",
        f"paper_ref {proposal.paper_ref}",
    )

    evidence_refs = list(proposal.evidence_refs)
    if len(evidence_refs) != len(set(evidence_refs)):
        raise ProposalValidationError("evidence_refs must not contain duplicates")
    evidence_by_id = {
        evidence.evidence_id: evidence for evidence in snapshot.evidence_records
    }
    for evidence_ref in evidence_refs:
        if evidence_ref not in evidence_by_id:
            raise ProposalValidationError(f"unknown evidence_ref {evidence_ref}")
        _require_dependency_scope(
            dependency_keys,
            f"EvidenceRecord:{evidence_ref}",
            f"evidence_ref {evidence_ref}",
        )

    complete_refs = {
        evidence.evidence_id
        for evidence in snapshot.evidence_records
        if evidence.claim_id == proposal.claim_ref
        and evidence.paper_id == proposal.paper_ref
    }
    if set(evidence_refs) != complete_refs:
        raise ProposalValidationError(
            "evidence_refs must exactly equal all current EvidenceRecord IDs "
            "for the proposal claim and paper"
        )

    grouped = proposal.component_relations.grouped()
    grouped_refs = [
        evidence_ref
        for refs in grouped.values()
        for evidence_ref in refs
    ]
    if len(grouped_refs) != len(set(grouped_refs)):
        raise ProposalValidationError(
            "each evidence_ref must occur in exactly one component relation"
        )
    if set(grouped_refs) != set(evidence_refs):
        raise ProposalValidationError(
            "component relation union must exactly equal evidence_refs"
        )
    for relation, refs in grouped.items():
        for evidence_ref in refs:
            actual = evidence_by_id[evidence_ref].relation_to_candidate
            if actual != relation:
                raise ProposalValidationError(
                    f"component relation for {evidence_ref} is {relation.value}, "
                    f"but the EvidenceRecord relation is {actual.value}"
                )

    try:
        aggregate = _claim_paper_evidence_from_proposal(proposal)
    except ValueError as exc:
        raise ProposalValidationError(str(exc)) from exc

    existing = next(
        (
            item
            for item in snapshot.claim_paper_evidence
            if item.claim_paper_evidence_id
            == aggregate.claim_paper_evidence_id
        ),
        None,
    )
    assessed = any(
        item.claim_id == proposal.claim_ref
        for item in snapshot.claim_assessments
    )
    if assessed and existing != aggregate:
        raise ProposalValidationError(
            "cannot add or change claim-paper evidence after ClaimAssessment "
            "exists for the claim"
        )


def _validate_final_claim(
    proposal: FinalClaimValidationProposal,
    snapshot: RepositorySnapshot,
    dependency_keys: frozenset[str] | None,
    invocation: BaseModel | None,
) -> None:
    if invocation is not None:
        if not isinstance(invocation, ValidateFinalClaimInvocation):
            raise ProposalValidationError(
                "final-claim validation received the wrong invocation context"
            )
        if proposal.claim_ref != invocation.claim_id:
            raise ProposalValidationError(
                f"claim_ref {proposal.claim_ref} does not match invocation claim_id "
                f"{invocation.claim_id}"
            )
        if proposal.final_claim != invocation.proposed_final_claim:
            raise ProposalValidationError(
                "final_claim must exactly equal the invocation proposed_final_claim"
            )

    if proposal.claim_ref not in {
        claim.claim_id for claim in snapshot.candidate_claims
    }:
        raise ProposalValidationError(f"unknown claim_ref {proposal.claim_ref}")
    assessment = next(
        (
            item
            for item in snapshot.claim_assessments
            if item.claim_id == proposal.claim_ref
        ),
        None,
    )
    if assessment is None:
        raise ProposalValidationError(
            f"ClaimAssessment for {proposal.claim_ref} does not exist"
        )
    _require_dependency_scope(
        dependency_keys,
        f"CandidateClaim:{proposal.claim_ref}",
        f"claim_ref {proposal.claim_ref}",
    )
    _require_dependency_scope(
        dependency_keys,
        f"ClaimAssessment:{proposal.claim_ref}",
        f"ClaimAssessment for {proposal.claim_ref}",
    )

    relation_refs = [
        relation.claim_paper_evidence_ref for relation in proposal.paper_relations
    ]
    if len(relation_refs) != len(set(relation_refs)):
        raise ProposalValidationError(
            "each CPE may occur only once in final paper relations"
        )
    cpe_by_id = {
        cpe.claim_paper_evidence_id: cpe for cpe in snapshot.claim_paper_evidence
    }
    for relation in proposal.paper_relations:
        cpe = cpe_by_id.get(relation.claim_paper_evidence_ref)
        if cpe is None:
            raise ProposalValidationError(
                "unknown claim_paper_evidence_ref "
                f"{relation.claim_paper_evidence_ref}"
            )
        _require_dependency_scope(
            dependency_keys,
            f"ClaimPaperEvidence:{relation.claim_paper_evidence_ref}",
            f"claim_paper_evidence_ref {relation.claim_paper_evidence_ref}",
        )
        if cpe.claim_id != proposal.claim_ref:
            raise ProposalValidationError(
                f"CPE {cpe.claim_paper_evidence_id} belongs to {cpe.claim_id}, "
                f"not {proposal.claim_ref}"
            )
        if cpe.paper_id != relation.paper_ref:
            raise ProposalValidationError(
                f"paper_ref {relation.paper_ref} does not match CPE "
                f"{cpe.claim_paper_evidence_id} paper {cpe.paper_id}"
            )

    complete_refs = {
        cpe.claim_paper_evidence_id
        for cpe in snapshot.claim_paper_evidence
        if cpe.claim_id == proposal.claim_ref
    }
    if set(relation_refs) != complete_refs:
        raise ProposalValidationError(
            "paper_relations must exactly cover every current CPE for the claim"
        )
    if (
        proposal.status is FinalClaimStatus.VALID
        and assessment.decision is ClaimDecision.REJECT
    ):
        raise ProposalValidationError(
            "a rejected ClaimAssessment cannot yield a VALID final claim"
        )

    try:
        validation = _final_validation_from_proposal(proposal)
        packet = _claim_packet_for_validation(snapshot, validation)
    except ValueError as exc:
        raise ProposalValidationError(str(exc)) from exc

    has_descendants = any(
        proposal.claim_ref in proposition.claim_ids
        or any(
            binding.claim_id == proposal.claim_ref
            for binding in proposition.citation_bindings
        )
        for proposition in snapshot.proposition_records
    )
    if has_descendants:
        existing_validation = next(
            (
                item
                for item in snapshot.final_claim_validations
                if item.claim_id == proposal.claim_ref
            ),
            None,
        )
        existing_packet = next(
            (
                item
                for item in snapshot.claim_packets
                if item.claim_id == proposal.claim_ref
            ),
            None,
        )
        if existing_validation != validation or existing_packet != packet:
            raise ProposalValidationError(
                "cannot change final claim validation while canonical "
                "proposition descendants exist"
            )


def validate_proposal(
    spec: TaskSpec,
    proposal: BaseModel,
    snapshot: RepositorySnapshot,
    *,
    dependency_keys: Iterable[str] | None = None,
    invocation: BaseModel | None = None,
    registry: CanonicalIdRegistry | None = None,
    accepted_allocations: Mapping[str, str] | None = None,
) -> None:
    scoped_dependencies = (
        frozenset(dependency_keys) if dependency_keys is not None else None
    )
    if spec.promotion_handler == "promote_discovery":
        _validate_discovery(proposal, snapshot)  # type: ignore[arg-type]
    elif spec.promotion_handler == "promote_claim_assessment":
        assert isinstance(proposal, ClaimAssessmentProposal)
        if proposal.claim_ref not in {claim.claim_id for claim in snapshot.candidate_claims}:
            raise ProposalValidationError(f"unknown claim_ref {proposal.claim_ref}")
        if (
            scoped_dependencies is not None
            and f"CandidateClaim:{proposal.claim_ref}" not in scoped_dependencies
        ):
            raise ProposalValidationError(
                f"claim_ref {proposal.claim_ref} is outside the task dependency scope"
            )
        proposed_assessment = _claim_assessment_from_proposal(proposal)
        existing_assessment = next(
            (
                item
                for item in snapshot.claim_assessments
                if item.claim_id == proposal.claim_ref
            ),
            None,
        )
        has_claim_descendants = any(
            item.claim_id == proposal.claim_ref
            for item in (
                *snapshot.final_claim_validations,
                *snapshot.claim_packets,
            )
        ) or any(
            proposal.claim_ref in proposition.claim_ids
            or any(
                binding.claim_id == proposal.claim_ref
                for binding in proposition.citation_bindings
            )
            for proposition in snapshot.proposition_records
        )
        if has_claim_descendants and existing_assessment != proposed_assessment:
            raise ProposalValidationError(
                "cannot change ClaimAssessment while canonical claim descendants exist"
            )
    elif spec.promotion_handler == "promote_semantic_audit":
        assert isinstance(proposal, SemanticAuditProposal)
        target = next(
            (
                item
                for item in snapshot.proposition_records
                if item.proposition_id == proposal.target_ref
            ),
            None,
        )
        if target is None:
            raise ProposalValidationError(f"unknown proposition target {proposal.target_ref}")
        if (
            set(proposal.referenced_claim_refs) != set(target.claim_ids)
            or set(proposal.referenced_corpus_fact_refs) != set(target.corpus_fact_ids)
            or set(proposal.referenced_process_fact_refs) != set(target.process_fact_ids)
        ):
            raise ProposalValidationError(
                "semantic audit proposal does not cover complete target provenance"
            )
        if (
            scoped_dependencies is not None
            and f"PropositionRecord:{proposal.target_ref}" not in scoped_dependencies
        ):
            raise ProposalValidationError(
                f"proposition target {proposal.target_ref} is outside the task "
                "dependency scope"
            )
    elif spec.promotion_handler == "promote_retrieval_queries":
        assert isinstance(proposal, RetrievalQueryProposalBundle)
        _validate_retrieval_queries(
            proposal,
            snapshot,
            scoped_dependencies,
            registry,
            accepted_allocations,
            invocation,
        )
    elif spec.promotion_handler == "promote_claim_paper_evidence":
        assert isinstance(proposal, ClaimPaperEvidenceProposal)
        _validate_claim_paper_evidence(proposal, snapshot, scoped_dependencies)
    elif spec.promotion_handler == "promote_final_claim_validation":
        assert isinstance(proposal, FinalClaimValidationProposal)
        _validate_final_claim(
            proposal, snapshot, scoped_dependencies, invocation
        )


def promote_discovery(
    snapshot: RepositorySnapshot,
    registry: CanonicalIdRegistry,
    proposal: DiscoveryProposalBundle,
) -> PromotionPayload:
    theme_ids, registry = registry.allocate(IdKind.THEME, len(proposal.themes)) if proposal.themes else ([], registry)
    claim_ids, registry = registry.allocate(IdKind.CLAIM, len(proposal.claims)) if proposal.claims else ([], registry)
    local_map = {
        item.local_ref: identifier
        for item, identifier in zip(proposal.themes, theme_ids, strict=True)
    }
    local_map.update(
        {
            item.local_ref: identifier
            for item, identifier in zip(proposal.claims, claim_ids, strict=True)
        }
    )
    themes = tuple(
        ThemeRecord(
            theme_id=identifier,
            title=item.title,
            description=item.description,
            origin=item.origin,
            parent_theme_id=local_map.get(item.parent_ref, item.parent_ref),
        )
        for item, identifier in zip(proposal.themes, theme_ids, strict=True)
    )
    claims = tuple(
        CandidateClaim(
            claim_id=identifier,
            theme_id=local_map.get(item.theme_ref, item.theme_ref),
            candidate_claim=item.candidate_claim,
            origin=item.origin,
            origin_refs=item.origin_refs,
        )
        for item, identifier in zip(proposal.claims, claim_ids, strict=True)
    )
    promoted = snapshot.model_copy(
        update={
            "themes": snapshot.themes + themes,
            "candidate_claims": snapshot.candidate_claims + claims,
        }
    )
    return PromotionPayload(promoted, registry, local_map)


def promote_retrieval_queries(
    snapshot: RepositorySnapshot,
    registry: CanonicalIdRegistry,
    proposal: RetrievalQueryProposalBundle,
) -> PromotionPayload:
    claims_by_id = {claim.claim_id: claim for claim in snapshot.candidate_claims}
    queries: list[RetrievalQuery] = []
    local_map: dict[str, str] = {}
    for item in proposal.queries:
        query_id, registry = registry.allocate_query(item.claim_ref, item.intent)
        candidate = claims_by_id[item.claim_ref]
        queries.append(
            RetrievalQuery(
                query_id=query_id,
                claim_id=item.claim_ref,
                candidate_claim_hash=candidate_claim_hash(candidate.candidate_claim),
                intent=item.intent,
                query_text=item.query_text,
            )
        )
        local_map[item.local_ref] = query_id
    promoted = snapshot.model_copy(
        update={"retrieval_queries": snapshot.retrieval_queries + tuple(queries)}
    )
    return PromotionPayload(promoted, registry, local_map)


def promote_claim_assessment(
    snapshot: RepositorySnapshot,
    registry: CanonicalIdRegistry,
    proposal: ClaimAssessmentProposal,
) -> PromotionPayload:
    assessment = _claim_assessment_from_proposal(proposal)
    values = _upsert_by_attribute(
        snapshot.claim_assessments,
        attribute="claim_id",
        replacement=assessment,
    )
    return PromotionPayload(
        snapshot.model_copy(update={"claim_assessments": values}), registry, {}
    )


def promote_claim_paper_evidence(
    snapshot: RepositorySnapshot,
    registry: CanonicalIdRegistry,
    proposal: ClaimPaperEvidenceProposal,
) -> PromotionPayload:
    aggregate = _claim_paper_evidence_from_proposal(proposal)
    identifier = aggregate.claim_paper_evidence_id
    values = _upsert_by_attribute(
        snapshot.claim_paper_evidence,
        attribute="claim_paper_evidence_id",
        replacement=aggregate,
    )
    return PromotionPayload(
        snapshot.model_copy(update={"claim_paper_evidence": values}),
        registry,
        {"claim_paper_evidence": identifier},
    )


def promote_final_claim_validation(
    snapshot: RepositorySnapshot,
    registry: CanonicalIdRegistry,
    proposal: FinalClaimValidationProposal,
) -> PromotionPayload:
    validation = _final_validation_from_proposal(proposal)
    validations = _upsert_by_attribute(
        snapshot.final_claim_validations,
        attribute="claim_id",
        replacement=validation,
    )
    packet = _claim_packet_for_validation(snapshot, validation)
    if packet is None:
        packets = tuple(
            item
            for item in snapshot.claim_packets
            if item.claim_id != validation.claim_id
        )
    else:
        packets = _upsert_by_attribute(
            snapshot.claim_packets,
            attribute="claim_id",
            replacement=packet,
        )
    promoted = snapshot.model_copy(
        update={
            "final_claim_validations": validations,
            "claim_packets": packets,
        }
    )
    return PromotionPayload(promoted, registry, {})


def promote_semantic_audit(
    snapshot: RepositorySnapshot,
    registry: CanonicalIdRegistry,
    proposal: SemanticAuditProposal,
) -> PromotionPayload:
    identifiers, registry = registry.allocate(IdKind.SEMANTIC_AUDIT)
    audit = SemanticAuditResult(
        audit_id=identifiers[0],
        target_type="PropositionRecord",
        target_id=proposal.target_ref,
        class_verdict=proposal.class_verdict,
        provenance_verdict=proposal.provenance_verdict,
        reason=proposal.reason,
        referenced_claim_ids=proposal.referenced_claim_refs,
        referenced_corpus_fact_ids=proposal.referenced_corpus_fact_refs,
        referenced_process_fact_ids=proposal.referenced_process_fact_refs,
    )
    values = tuple(
        item for item in snapshot.semantic_audits if item.target_id != audit.target_id
    ) + (audit,)
    return PromotionPayload(
        snapshot.model_copy(update={"semantic_audits": values}),
        registry,
        {"audit": audit.audit_id},
    )


PROMOTION_HANDLERS: dict[
    str, Callable[[RepositorySnapshot, CanonicalIdRegistry, Any], PromotionPayload]
] = {
    "promote_discovery": promote_discovery,
    "promote_retrieval_queries": promote_retrieval_queries,
    "promote_claim_paper_evidence": promote_claim_paper_evidence,
    "promote_claim_assessment": promote_claim_assessment,
    "promote_final_claim_validation": promote_final_claim_validation,
    "promote_semantic_audit": promote_semantic_audit,
}


def prepare_promotion(spec: TaskSpec, proposal: BaseModel):
    handler = PROMOTION_HANDLERS.get(spec.promotion_handler)
    if handler is None:
        raise ContractImplementationError(
            f"promotion handler {spec.promotion_handler} is not implemented in this milestone"
        )

    def promote(
        snapshot: RepositorySnapshot, registry: CanonicalIdRegistry
    ) -> PromotionPayload:
        return handler(snapshot, registry, proposal)

    return promote


def interpret_positive(_: BaseModel) -> TransitionDecision:
    return TransitionDecision(
        scientific_disposition="VALID",
        canonicalized=False,
        downstream_eligible=True,
    )


def interpret_claim_assessment(proposal: ClaimAssessmentProposal) -> TransitionDecision:
    return TransitionDecision(
        scientific_disposition=proposal.decision.value,
        canonicalized=False,
        downstream_eligible=proposal.decision is not ClaimDecision.REJECT,
    )


def interpret_final_claim_validation(
    proposal: FinalClaimValidationProposal,
) -> TransitionDecision:
    return TransitionDecision(
        scientific_disposition=proposal.status.value,
        canonicalized=False,
        downstream_eligible=proposal.status is FinalClaimStatus.VALID,
        human_review_required=proposal.status is FinalClaimStatus.UNCLEAR,
    )


def interpret_semantic_audit(proposal: SemanticAuditProposal) -> TransitionDecision:
    # Derive the frozen validator's transition directly from the verdicts.
    # Package C audits target a generation-owned draft-local reference until
    # the coupled PR+SA promotion allocates both canonical identifiers.  A
    # temporary SemanticAuditResult would therefore require inventing an
    # invalid canonical PropositionId before the writer-locked transaction.
    downstream_eligible = (
        proposal.class_verdict is AuditClassVerdict.CORRECT
        and proposal.provenance_verdict is AuditProvenanceVerdict.ENTAILED
    )
    scientific_disposition = (
        proposal.class_verdict.value
        if proposal.class_verdict is not AuditClassVerdict.CORRECT
        else proposal.provenance_verdict.value
    )
    return TransitionDecision(
        scientific_disposition=scientific_disposition,
        canonicalized=False,
        downstream_eligible=downstream_eligible,
        human_review_required=(
            proposal.class_verdict is AuditClassVerdict.UNCLEAR
            or proposal.provenance_verdict is AuditProvenanceVerdict.UNCLEAR
        ),
    )


def interpret_rendered_sentence_audit(
    proposal: RenderedSentenceAuditProposal,
) -> TransitionDecision:
    """Preserve every sentence-audit verdict without triggering fallback."""

    return TransitionDecision(
        scientific_disposition=proposal.verdict.value,
        canonicalized=False,
        downstream_eligible=(
            proposal.verdict is AuditProvenanceVerdict.ENTAILED
        ),
        human_review_required=(
            proposal.verdict is AuditProvenanceVerdict.UNCLEAR
        ),
    )


DISPOSITION_HANDLERS: dict[str, Callable[[Any], TransitionDecision]] = {
    "interpret_positive": interpret_positive,
    "interpret_claim_assessment": interpret_claim_assessment,
    "interpret_final_claim_validation": interpret_final_claim_validation,
    "interpret_semantic_audit": interpret_semantic_audit,
    "interpret_rendered_sentence_audit": interpret_rendered_sentence_audit,
}


def interpret_disposition(spec: TaskSpec, proposal: BaseModel) -> TransitionDecision:
    handler = DISPOSITION_HANDLERS.get(spec.disposition_handler)
    if handler is None:
        raise ContractImplementationError(
            f"disposition handler {spec.disposition_handler} is not implemented in this milestone"
        )
    return handler(proposal)
