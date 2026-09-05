from __future__ import annotations

import pytest
from pydantic import ValidationError

from vibereview.ids import candidate_claim_hash, normalize_doi
from vibereview.models import (
    CandidateClaim,
    ClaimAssessment,
    CorpusFact,
    CorpusFactDerivation,
    EvidenceQuality,
    FinalClaimValidation,
    Paper,
    RetrievalDisposition,
    SpanLocator,
)


@pytest.mark.parametrize(
    ("model", "field", "valid", "invalid"),
    [
        ("ThemeRecord", "theme_id", "T0001", "T001"),
        ("Paper", "paper_id", "P0001", "P00001"),
        ("CandidateClaim", "claim_id", "C0001", "C1"),
        ("RetrievedSpan", "span_id", "R0001", "R001"),
        ("EvidenceRecord", "evidence_id", "E0001", "E000"),
        ("ClaimPaperEvidence", "claim_paper_evidence_id", "CPE-C0001-P0001", "CPE-C1-P1"),
        ("CorpusFact", "corpus_fact_id", "CF0001", "CF001"),
        ("ReviewProcessFact", "process_fact_id", "PF0001", "PF001"),
        ("PropositionRecord", "proposition_id", "PR0001", "PR001"),
        ("SemanticAuditResult", "audit_id", "SA0001", "SA001"),
        ("RenderedSentence", "sentence_id", "RS0001", "RS001"),
        ("RenderedSentenceAudit", "audit_id", "RSA0001", "RSA001"),
    ],
)
def test_identifier_regexes(bundle_factory, model, field, valid, invalid):
    bundle = bundle_factory()
    objects = [item for values in bundle.values() for item in values]
    instance = next(item for item in objects if type(item).__name__ == model)
    data = instance.model_dump()
    data[field] = valid
    type(instance).model_validate(data)
    data[field] = invalid
    with pytest.raises(ValidationError):
        type(instance).model_validate(data)


def test_canonical_sha256_and_exact_utf8_hash():
    claim = " Preheating reduces stress. "
    assert candidate_claim_hash(claim) == (
        "sha256:ba5bd54ca64511f12d3cc04e355ed56e81660151f202c42c150971940f4dfa56"
    )
    assert candidate_claim_hash(claim) != candidate_claim_hash(claim.strip())


@pytest.mark.parametrize(
    "value",
    ["https://doi.org/10.1234/ABC", "doi:10.1234/abc", "10.1234/ABC"],
)
def test_doi_normalization(value):
    assert normalize_doi(value) == "doi:10.1234/abc"


def test_paper_normalizes_identity_keys_and_forbids_extra_fields(bundle_factory):
    paper = bundle_factory()["papers"][0]
    data = paper.model_dump()
    data["doi"] = "https://doi.org/10.9999/ABC"
    data["identity_keys"] = [" DOI:10.9999/ABC ", "bib:  Smith   2024  Study "]
    normalized = Paper.model_validate(data)
    assert normalized.doi == "doi:10.9999/abc"
    assert normalized.identity_keys == ["doi:10.9999/abc", "bib:smith 2024 study"]
    data["unexpected"] = True
    with pytest.raises(ValidationError):
        Paper.model_validate(data)


@pytest.mark.parametrize(
    ("start", "end", "page"),
    [(0, 0, 1), (5, 4, 1), (-1, 2, 1), (0, 2, 0)],
)
def test_invalid_offsets_and_page(start, end, page):
    with pytest.raises(ValidationError):
        SpanLocator(
            raw_md_path="paper/raw.md",
            page=page,
            section=None,
            start_offset=start,
            end_offset=end,
            source_span_hash="sha256:" + "a" * 64,
        )


def test_invalid_span_hash():
    with pytest.raises(ValidationError):
        SpanLocator(
            raw_md_path="paper/raw.md",
            page=None,
            section=None,
            start_offset=0,
            end_offset=2,
            source_span_hash="A" * 64,
        )


@pytest.mark.parametrize(
    "data",
    [
        dict(status="assessed", reason=None, canonical_span_id="R0002"),
        dict(status="duplicate", reason=None, canonical_span_id=None),
        dict(status="redundant", reason=None, canonical_span_id=None),
        dict(status="excluded_by_budget", reason=None, canonical_span_id=None),
        dict(status="invalid_locator", reason=None, canonical_span_id=None),
    ],
)
def test_invalid_disposition_combinations(data):
    with pytest.raises(ValidationError):
        RetrievalDisposition(span_id="R0001", **data)


@pytest.mark.parametrize(
    ("strength", "relevance"),
    [("high", "low"), ("moderate", "not_assessable"), ("unknown", "high")],
)
def test_quality_invalid_not_assessable_combinations(strength, relevance):
    with pytest.raises(ValidationError):
        EvidenceQuality(
            directness="not_assessable",
            methodological_relevance=relevance,
            strength=strength,
            assessability="not_assessable",
            limitations=[],
        )


def test_reject_requires_rejection_basis(bundle_factory):
    data = bundle_factory()["claim_assessments"][0].model_dump()
    data.update(decision="REJECT", rejection_basis=None)
    with pytest.raises(ValidationError):
        ClaimAssessment.model_validate(data)


def test_non_reject_forbids_rejection_basis(bundle_factory):
    data = bundle_factory()["claim_assessments"][0].model_dump()
    data["rejection_basis"] = "contradicted"
    with pytest.raises(ValidationError):
        ClaimAssessment.model_validate(data)


@pytest.mark.parametrize("check", ["fail", "unclear"])
def test_valid_final_claim_rejects_failed_or_unclear_check(bundle_factory, check):
    data = bundle_factory()["final_claim_validations"][0].model_dump()
    data["scope_check"] = check
    with pytest.raises(ValidationError):
        FinalClaimValidation.model_validate(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scope_check", "not_applicable"),
        ("certainty_check", "not_applicable"),
    ],
)
def test_valid_final_claim_requires_scope_and_certainty_pass(
    field, value, bundle_factory
):
    data = bundle_factory()["final_claim_validations"][0].model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        FinalClaimValidation.model_validate(data)


def test_valid_final_claim_requires_paper_relations(bundle_factory):
    data = bundle_factory()["final_claim_validations"][0].model_dump()
    data["paper_relations"] = []
    with pytest.raises(ValidationError):
        FinalClaimValidation.model_validate(data)


def test_semantic_corpus_fact_requires_complete_provenance():
    with pytest.raises(ValidationError):
        CorpusFact(
            corpus_fact_id="CF0001",
            text="A semantic corpus fact.",
            derivation_type="semantic_classification",
            source_paper_ids=["P0001"],
            derivation=CorpusFactDerivation(
                task_version="v1",
                model_signature=None,
                input_hash="sha256:" + "a" * 64,
                output_hash="sha256:" + "b" * 64,
            ),
        )


def test_all_persistent_models_round_trip_json(bundle_factory):
    bundle = bundle_factory()
    objects = [item for values in bundle.values() for item in values]
    citation = bundle["proposition_records"][0].citation_bindings[0]
    objects.append(citation)
    names = {type(item).__name__ for item in objects}
    assert names == {
        "ThemeRecord",
        "Paper",
        "CandidateClaim",
        "RetrievalQuery",
        "RetrievedSpan",
        "RetrievalDisposition",
        "EvidenceRecord",
        "ClaimPaperEvidence",
        "ClaimAssessment",
        "FinalClaimValidation",
        "ClaimPacket",
        "CitationBinding",
        "CorpusFact",
        "ReviewProcessFact",
        "PropositionRecord",
        "SemanticAuditResult",
        "RenderedSentence",
        "RenderedSentenceAudit",
    }
    for item in objects:
        assert type(item).model_validate_json(item.model_dump_json()) == item


def test_assignment_validation_preserves_contract(bundle_factory):
    candidate: CandidateClaim = bundle_factory()["candidate_claims"][0]
    with pytest.raises(ValidationError):
        candidate.claim_id = "C01"
