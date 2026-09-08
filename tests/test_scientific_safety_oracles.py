from __future__ import annotations

import pytest

from vibereview.enums import AuditDisposition
from vibereview.errors import RepositoryValidationError
from vibereview.models import RenderedSentence
from vibereview.validators import (
    derive_semantic_audit_disposition,
    validate_proposition_bundle,
    validate_rendered_prose_bundle,
    validate_retrieval_bundle,
)


def _validate_retrieval(bundle):
    return validate_retrieval_bundle(
        bundle["candidate_claims"],
        bundle["retrieval_queries"],
        bundle["retrieved_spans"],
        bundle["retrieval_dispositions"],
        bundle["papers"],
    )


def _validate_propositions(bundle):
    return validate_proposition_bundle(
        bundle["claim_packets"],
        bundle["claim_paper_evidence"],
        bundle["corpus_facts"],
        bundle["process_facts"],
        bundle["proposition_records"],
        bundle["semantic_audits"],
    )


def _validate_rendering(bundle):
    return validate_rendered_prose_bundle(
        bundle["proposition_records"],
        bundle["semantic_audits"],
        bundle["rendered_sentences"],
        bundle["rendered_sentence_audits"],
    )


@pytest.mark.parametrize(
    ("oracle_label", "located_text", "proposition_text", "audit_verdict"),
    [
        (
            "title_only_cannot_support_broad_claim",
            "A Synthetic Evaluation of Thermal Treatment",
            "Thermal treatment eliminates failures in every operating condition.",
            "UNSUPPORTED",
        ),
        (
            "scope_or_negation_truncation_is_overstated",
            "reduced failures in the observed sample",
            "Thermal treatment reduced failures outside the observed sample.",
            "OVERSTATED",
        ),
    ],
)
def test_structural_locator_success_never_overrides_negative_semantic_oracle(
    oracle_label,
    located_text,
    proposition_text,
    audit_verdict,
    bundle_factory,
):
    """These labels are frozen test oracles, not an automated semantic classifier."""

    bundle = bundle_factory()
    span = bundle["retrieved_spans"][0]
    bundle["retrieved_spans"][0] = span.model_copy(
        update={"source_text": located_text}
    )
    assert _validate_retrieval(bundle).ok, oracle_label

    proposition = bundle["proposition_records"][0]
    bundle["proposition_records"][0] = proposition.model_copy(
        update={"text": proposition_text}
    )
    audit = bundle["semantic_audits"][0]
    bundle["semantic_audits"][0] = audit.model_copy(
        update={
            "provenance_verdict": audit_verdict,
            "reason": f"synthetic negative oracle: {oracle_label}",
        }
    )

    # Negative audits are valid canonical scientific state.
    assert _validate_propositions(bundle).ok
    assert (
        derive_semantic_audit_disposition(bundle["semantic_audits"][0])
        is AuditDisposition.REPAIR_BLOCKING
    )

    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate_rendering(bundle)
    assert "RENDERED_FROM_NONPASSING_PROPOSITION" in exc_info.value.report.codes()


def test_corpus_count_cannot_license_mechanism_prose(bundle_factory):
    """A registry count is provenance for a count, never for a mechanism."""

    bundle = bundle_factory()
    proposition = bundle["proposition_records"][0]
    bundle["proposition_records"][0] = proposition.model_copy(
        update={
            "text": "The one-paper corpus proves the treatment prevents cracking.",
            "content_class": "CorpusFact",
            "claim_ids": [],
            "citation_bindings": [],
            "corpus_fact_ids": ["CF0001"],
        }
    )
    audit = bundle["semantic_audits"][0]
    bundle["semantic_audits"][0] = audit.model_copy(
        update={
            "class_verdict": "MISCLASSIFIED",
            "provenance_verdict": "UNSUPPORTED",
            "reason": "synthetic oracle: corpus cardinality cannot entail mechanism",
            "referenced_claim_ids": [],
            "referenced_corpus_fact_ids": ["CF0001"],
        }
    )

    assert _validate_propositions(bundle).ok
    assert (
        derive_semantic_audit_disposition(bundle["semantic_audits"][0])
        is AuditDisposition.REPAIR_BLOCKING
    )
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate_rendering(bundle)
    assert "RENDERED_FROM_NONPASSING_PROPOSITION" in exc_info.value.report.codes()


@pytest.mark.parametrize(
    ("oracle_label", "strengthened_sentence", "verdict"),
    [
        (
            "absolute_completeness",
            "Preheating completely eliminated residual stress.",
            "OVERSTATED",
        ),
        (
            "unsupported_guarantee",
            "Preheating guarantees elimination of residual stress.",
            "UNSUPPORTED",
        ),
        (
            "unsupported_causation",
            "Preheating caused all residual stress to disappear.",
            "OVERSTATED",
        ),
        (
            "unsupported_numeric_accuracy",
            "Preheating reduced residual stress with 99.9 percent accuracy.",
            "UNSUPPORTED",
        ),
        (
            "unsupported_gpu_memory_claim",
            "Preheating reduced residual stress while using 2 GB of GPU memory.",
            "UNSUPPORTED",
        ),
    ],
)
def test_strengthened_sentence_oracles_are_never_renderable(
    oracle_label,
    strengthened_sentence,
    verdict,
    bundle_factory,
):
    bundle = bundle_factory()
    sentence = bundle["rendered_sentences"][0]
    bundle["rendered_sentences"][0] = sentence.model_copy(
        update={"text": strengthened_sentence}
    )
    audit = bundle["rendered_sentence_audits"][0]
    bundle["rendered_sentence_audits"][0] = audit.model_copy(
        update={
            "verdict": verdict,
            "reason": f"synthetic negative oracle: {oracle_label}",
        }
    )

    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate_rendering(bundle)
    assert "RENDERED_SENTENCE_NOT_ENTAILED" in exc_info.value.report.codes()


def test_extra_body_sentence_without_audit_is_rejected(bundle_factory):
    bundle = bundle_factory()
    bundle["rendered_sentences"].append(
        RenderedSentence(
            sentence_id="RS0002",
            text="An unaudited extra body sentence must never reach export.",
            source_proposition_ids=["PR0001"],
        )
    )

    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate_rendering(bundle)
    assert "CARDINALITY_VIOLATION" in exc_info.value.report.codes()
