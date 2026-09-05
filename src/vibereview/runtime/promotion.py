"""Proposal validation, scientific interpretation, and locked promotion handlers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from vibereview.enums import AuditClassVerdict, AuditProvenanceVerdict, ClaimDecision
from vibereview.models import (
    CandidateClaim,
    ClaimAssessment,
    SemanticAuditResult,
    ThemeRecord,
)
from vibereview.validators import derive_semantic_audit_disposition

from .dto import (
    ClaimAssessmentProposal,
    DiscoveryProposalBundle,
    SemanticAuditProposal,
)
from .records import TaskSpec, TransitionDecision
from .registry import CanonicalIdRegistry, IdKind
from .repository import PromotionPayload
from .state import RepositorySnapshot


class ProposalValidationError(ValueError):
    pass


class ContractImplementationError(RuntimeError):
    pass


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


def validate_proposal(
    spec: TaskSpec, proposal: BaseModel, snapshot: RepositorySnapshot
) -> None:
    if spec.promotion_handler == "promote_discovery":
        _validate_discovery(proposal, snapshot)  # type: ignore[arg-type]
    elif spec.promotion_handler == "promote_claim_assessment":
        assert isinstance(proposal, ClaimAssessmentProposal)
        if proposal.claim_ref not in {claim.claim_id for claim in snapshot.candidate_claims}:
            raise ProposalValidationError(f"unknown claim_ref {proposal.claim_ref}")
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


def promote_claim_assessment(
    snapshot: RepositorySnapshot,
    registry: CanonicalIdRegistry,
    proposal: ClaimAssessmentProposal,
) -> PromotionPayload:
    assessment = ClaimAssessment(
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
    values = tuple(
        item for item in snapshot.claim_assessments if item.claim_id != assessment.claim_id
    ) + (assessment,)
    return PromotionPayload(
        snapshot.model_copy(update={"claim_assessments": values}), registry, {}
    )


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
    "promote_claim_assessment": promote_claim_assessment,
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


def interpret_semantic_audit(proposal: SemanticAuditProposal) -> TransitionDecision:
    temporary = SemanticAuditResult(
        audit_id="SA0000",
        target_type="PropositionRecord",
        target_id=proposal.target_ref,
        class_verdict=proposal.class_verdict,
        provenance_verdict=proposal.provenance_verdict,
        reason=proposal.reason,
        referenced_claim_ids=proposal.referenced_claim_refs,
        referenced_corpus_fact_ids=proposal.referenced_corpus_fact_refs,
        referenced_process_fact_ids=proposal.referenced_process_fact_refs,
    )
    disposition = derive_semantic_audit_disposition(temporary)
    scientific_disposition = (
        proposal.class_verdict.value
        if proposal.class_verdict is not AuditClassVerdict.CORRECT
        else proposal.provenance_verdict.value
    )
    return TransitionDecision(
        scientific_disposition=scientific_disposition,
        canonicalized=False,
        downstream_eligible=disposition.value == "PASS",
        human_review_required=(
            proposal.class_verdict is AuditClassVerdict.UNCLEAR
            or proposal.provenance_verdict is AuditProvenanceVerdict.UNCLEAR
        ),
    )


DISPOSITION_HANDLERS: dict[str, Callable[[Any], TransitionDecision]] = {
    "interpret_positive": interpret_positive,
    "interpret_claim_assessment": interpret_claim_assessment,
    "interpret_semantic_audit": interpret_semantic_audit,
}


def interpret_disposition(spec: TaskSpec, proposal: BaseModel) -> TransitionDecision:
    handler = DISPOSITION_HANDLERS.get(spec.disposition_handler)
    if handler is None:
        raise ContractImplementationError(
            f"disposition handler {spec.disposition_handler} is not implemented in this milestone"
        )
    return handler(proposal)
