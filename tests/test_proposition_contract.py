from __future__ import annotations

import pytest
from pydantic import ValidationError

from vibereview.enums import AuditDisposition
from vibereview.errors import RepositoryValidationError
from vibereview.models import CitationBinding, PropositionRecord, SemanticAuditResult
from vibereview.validators import (
    derive_semantic_audit_disposition,
    validate_proposition_bundle,
)


def _validate(bundle):
    return validate_proposition_bundle(
        bundle["claim_packets"],
        bundle["claim_paper_evidence"],
        bundle["corpus_facts"],
        bundle["process_facts"],
        bundle["proposition_records"],
        bundle["semantic_audits"],
    )


def _base_proposition_data(bundle_factory):
    return bundle_factory()["proposition_records"][0].model_dump()


def test_valid_proposition_bundle(bundle_factory):
    assert _validate(bundle_factory()).ok


def test_scientific_claim_without_citations(bundle_factory):
    data = _base_proposition_data(bundle_factory)
    data["citation_bindings"] = []
    with pytest.raises(ValidationError):
        PropositionRecord.model_validate(data)


def test_scientific_claim_missing_citation_coverage_for_one_claim(bundle_factory):
    data = _base_proposition_data(bundle_factory)
    data["claim_ids"] = ["C0001", "C0002"]
    with pytest.raises(ValidationError):
        PropositionRecord.model_validate(data)


def test_corpus_fact_with_claim_provenance(bundle_factory):
    data = _base_proposition_data(bundle_factory)
    data.update(
        content_class="CorpusFact",
        claim_ids=["C0001"],
        citation_bindings=[],
        corpus_fact_ids=["CF0001"],
    )
    with pytest.raises(ValidationError):
        PropositionRecord.model_validate(data)


def test_review_process_statement_with_citations(bundle_factory):
    data = _base_proposition_data(bundle_factory)
    data.update(
        content_class="ReviewProcessStatement",
        claim_ids=[],
        corpus_fact_ids=[],
        process_fact_ids=["PF0001"],
    )
    with pytest.raises(ValidationError):
        PropositionRecord.model_validate(data)


def test_rhetorical_with_provenance(bundle_factory):
    data = _base_proposition_data(bundle_factory)
    data.update(content_class="Rhetorical", citation_bindings=[], claim_ids=["C0001"])
    with pytest.raises(ValidationError):
        PropositionRecord.model_validate(data)


def test_citation_binding_claim_mismatch(bundle_factory):
    bundle = bundle_factory()
    proposition = bundle["proposition_records"][0]
    binding = CitationBinding(
        paper_id="P0001",
        claim_id="C0002",
        claim_paper_evidence_id="CPE-C0001-P0001",
    )
    bundle["proposition_records"][0] = proposition.model_copy(
        update={"claim_ids": ["C0002"], "citation_bindings": [binding]}
    )
    audit = bundle["semantic_audits"][0]
    bundle["semantic_audits"][0] = audit.model_copy(
        update={"referenced_claim_ids": ["C0002"]}
    )
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "CITATION_BINDING_CLAIM_MISMATCH" in exc_info.value.report.codes()


def test_citation_binding_paper_mismatch(bundle_factory):
    bundle = bundle_factory()
    proposition = bundle["proposition_records"][0]
    binding = proposition.citation_bindings[0].model_copy(update={"paper_id": "P0002"})
    bundle["proposition_records"][0] = proposition.model_copy(
        update={"citation_bindings": [binding]}
    )
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "CITATION_BINDING_PAPER_MISMATCH" in exc_info.value.report.codes()


def test_citation_binding_cpe_must_be_licensed_by_claim_packet(bundle_factory):
    bundle = bundle_factory()
    packet = bundle["claim_packets"][0]
    bundle["claim_packets"][0] = packet.model_copy(
        update={"claim_paper_evidence_ids": ["CPE-C0001-P9999"]}
    )
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "CITATION_BINDING_UNLICENSED_CPE" in exc_info.value.report.codes()


def test_semantic_audit_incomplete_provenance(bundle_factory):
    bundle = bundle_factory()
    audit = bundle["semantic_audits"][0]
    bundle["semantic_audits"][0] = audit.model_copy(
        update={"referenced_claim_ids": []}
    )
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "SEMANTIC_AUDIT_INCOMPLETE_PROVENANCE" in exc_info.value.report.codes()


def test_proposition_requires_exactly_one_current_audit(bundle_factory):
    bundle = bundle_factory()
    bundle["semantic_audits"] = []
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "CARDINALITY_VIOLATION" in exc_info.value.report.codes()


@pytest.mark.parametrize(
    ("class_verdict", "provenance_verdict", "expected"),
    [
        ("CORRECT", "ENTAILED", AuditDisposition.PASS),
        ("CORRECT", "PARTIALLY_SUPPORTED", AuditDisposition.REPAIR),
        ("CORRECT", "OVERSTATED", AuditDisposition.REPAIR_BLOCKING),
        ("CORRECT", "UNSUPPORTED", AuditDisposition.REPAIR_BLOCKING),
        ("MISCLASSIFIED", "ENTAILED", AuditDisposition.REPAIR_BLOCKING),
        ("UNCLEAR", "ENTAILED", AuditDisposition.HUMAN_REVIEW),
        ("MISCLASSIFIED", "UNCLEAR", AuditDisposition.HUMAN_REVIEW),
    ],
)
def test_semantic_audit_disposition_mapping(
    class_verdict, provenance_verdict, expected
):
    audit = SemanticAuditResult(
        audit_id="SA0001",
        target_type="PropositionRecord",
        target_id="PR0001",
        class_verdict=class_verdict,
        provenance_verdict=provenance_verdict,
        reason="test",
        referenced_claim_ids=[],
        referenced_corpus_fact_ids=[],
        referenced_process_fact_ids=[],
    )
    assert derive_semantic_audit_disposition(audit) is expected
