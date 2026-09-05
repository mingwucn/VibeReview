from __future__ import annotations

import pytest
from pydantic import ValidationError

from vibereview.errors import RepositoryValidationError
from vibereview.ids import candidate_claim_hash
from vibereview.models import Paper, RetrievalDisposition, RetrievalQuery
from vibereview.validators import validate_retrieval_bundle


def _validate(bundle):
    return validate_retrieval_bundle(
        bundle["candidate_claims"],
        bundle["retrieval_queries"],
        bundle["retrieved_spans"],
        bundle["retrieval_dispositions"],
        bundle["papers"],
    )


@pytest.mark.parametrize(
    "query_id",
    [
        "Q-C0001-SUP-01",
        "Q-C0001-CON-09",
        "Q-C0001-BND-10",
        "Q-C0001-ALT-99",
    ],
)
def test_valid_query_ordinals(query_id, bundle_factory):
    data = bundle_factory()["retrieval_queries"][0].model_dump()
    data["query_id"] = query_id
    data["intent"] = {
        "SUP": "support",
        "CON": "contradiction",
        "BND": "boundary",
        "ALT": "alternative",
    }[query_id.split("-")[2]]
    RetrievalQuery.model_validate(data)


@pytest.mark.parametrize(
    "query_id",
    ["Q-C0001-SUP-00", "Q-C0001-CON-100", "Q-C0001-XYZ-01"],
)
def test_invalid_query_ordinals_and_codes(query_id, bundle_factory):
    data = bundle_factory()["retrieval_queries"][0].model_dump()
    data["query_id"] = query_id
    with pytest.raises(ValidationError):
        RetrievalQuery.model_validate(data)


def test_query_encoded_claim_mismatch(bundle_factory):
    data = bundle_factory()["retrieval_queries"][0].model_dump()
    data["claim_id"] = "C0002"
    with pytest.raises(ValidationError):
        RetrievalQuery.model_validate(data)


def test_query_encoded_intent_mismatch(bundle_factory):
    data = bundle_factory()["retrieval_queries"][0].model_dump()
    data["intent"] = "contradiction"
    with pytest.raises(ValidationError):
        RetrievalQuery.model_validate(data)


def test_candidate_hash_matches(bundle_factory):
    bundle = bundle_factory()
    report = _validate(bundle)
    assert report.ok


def test_candidate_mutation_after_retrieval_has_frozen_error(bundle_factory):
    bundle = bundle_factory()
    original = bundle["candidate_claims"][0]
    bundle["candidate_claims"][0] = original.model_copy(
        update={"candidate_claim": "Preheating eliminates residual stress."}
    )
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "CANDIDATE_CLAIM_MUTATED_AFTER_RETRIEVAL" in exc_info.value.report.codes()


def test_wrong_candidate_hash_fails(bundle_factory):
    bundle = bundle_factory()
    query = bundle["retrieval_queries"][0]
    bundle["retrieval_queries"][0] = query.model_copy(
        update={"candidate_claim_hash": "sha256:" + "f" * 64}
    )
    with pytest.raises(RepositoryValidationError):
        _validate(bundle)


def test_retrieval_query_missing_candidate(bundle_factory):
    bundle = bundle_factory()
    bundle["candidate_claims"] = []
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "INVALID_REFERENCE" in exc_info.value.report.codes()


def test_retrieved_span_missing_query(bundle_factory):
    bundle = bundle_factory()
    bundle["retrieval_queries"] = []
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "INVALID_REFERENCE" in exc_info.value.report.codes()


def test_retrieved_span_intent_must_match_query(bundle_factory):
    bundle = bundle_factory()
    span = bundle["retrieved_spans"][0]
    retrieval = span.retrieval.model_copy(update={"intent": "contradiction"})
    bundle["retrieved_spans"][0] = span.model_copy(update={"retrieval": retrieval})
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "RETRIEVAL_INTENT_MISMATCH" in exc_info.value.report.codes()


def test_retrieved_span_missing_paper(bundle_factory):
    bundle = bundle_factory()
    bundle["papers"] = []
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "INVALID_REFERENCE" in exc_info.value.report.codes()


def test_missing_retrieval_disposition(bundle_factory):
    bundle = bundle_factory()
    bundle["retrieval_dispositions"] = []
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "CARDINALITY_VIOLATION" in exc_info.value.report.codes()


def test_duplicate_retrieval_disposition(bundle_factory):
    bundle = bundle_factory()
    bundle["retrieval_dispositions"].append(bundle["retrieval_dispositions"][0].model_copy())
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "CARDINALITY_VIOLATION" in exc_info.value.report.codes()


@pytest.mark.parametrize("status", ["duplicate", "redundant"])
def test_canonical_span_cannot_self_reference(status, bundle_factory):
    bundle = bundle_factory()
    bundle["retrieval_dispositions"][0] = RetrievalDisposition(
        span_id="R0001",
        status=status,
        reason=None,
        canonical_span_id="R0001",
    )
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "CANONICAL_SPAN_SELF_REFERENCE" in exc_info.value.report.codes()


def test_canonical_span_cannot_cross_papers(bundle_factory):
    bundle = bundle_factory()
    paper = bundle["papers"][0]
    second_paper = Paper.model_validate(
        {
            **paper.model_dump(),
            "paper_id": "P0002",
            "doi": "10.9999/other",
            "identity_keys": ["doi:10.9999/other"],
            "raw_md_path": "papers/P0002/raw.md",
            "source_hash": "sha256:" + "d" * 64,
            "raw_md_hash": "sha256:" + "e" * 64,
        }
    )
    span = bundle["retrieved_spans"][0]
    second_span = span.model_copy(update={"span_id": "R0002", "paper_id": "P0002"})
    bundle["papers"].append(second_paper)
    bundle["retrieved_spans"].append(second_span)
    bundle["retrieval_dispositions"] = [
        RetrievalDisposition(
            span_id="R0001",
            status="duplicate",
            reason=None,
            canonical_span_id="R0002",
        ),
        RetrievalDisposition(
            span_id="R0002",
            status="assessed",
            reason=None,
            canonical_span_id=None,
        ),
    ]
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "CANONICAL_SPAN_CROSS_PAPER" in exc_info.value.report.codes()


def test_canonical_span_references_cannot_cycle(bundle_factory):
    bundle = bundle_factory()
    span = bundle["retrieved_spans"][0]
    bundle["retrieved_spans"].append(span.model_copy(update={"span_id": "R0002"}))
    bundle["retrieval_dispositions"] = [
        RetrievalDisposition(
            span_id="R0001",
            status="duplicate",
            reason=None,
            canonical_span_id="R0002",
        ),
        RetrievalDisposition(
            span_id="R0002",
            status="duplicate",
            reason=None,
            canonical_span_id="R0001",
        ),
    ]
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "CANONICAL_SPAN_CYCLE" in exc_info.value.report.codes()


def test_hash_helper_uses_exact_candidate_string(bundle_factory):
    candidate = bundle_factory()["candidate_claims"][0]
    assert candidate_claim_hash(candidate.candidate_claim + " ") != candidate_claim_hash(
        candidate.candidate_claim
    )
