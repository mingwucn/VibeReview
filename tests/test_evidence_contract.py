from __future__ import annotations

import pytest

from vibereview.errors import RepositoryValidationError
from vibereview.validators import validate_evidence_bundle


def _validate(bundle):
    return validate_evidence_bundle(
        bundle["candidate_claims"],
        bundle["retrieval_queries"],
        bundle["retrieved_spans"],
        bundle["retrieval_dispositions"],
        bundle["evidence_records"],
    )


def test_valid_evidence_continuity(bundle_factory):
    assert _validate(bundle_factory()).ok


def test_assessed_span_without_evidence(bundle_factory):
    bundle = bundle_factory()
    bundle["evidence_records"] = []
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "ASSESSED_SPAN_EVIDENCE_CARDINALITY" in exc_info.value.report.codes()


@pytest.mark.parametrize(
    "status",
    ["duplicate", "redundant", "excluded_by_budget", "invalid_locator"],
)
def test_discarded_span_with_evidence(status, bundle_factory):
    bundle = bundle_factory()
    disposition = bundle["retrieval_dispositions"][0]
    update = {"status": status, "reason": "discarded"}
    if status == "duplicate":
        update["canonical_span_id"] = "R0002"
    bundle["retrieval_dispositions"][0] = disposition.model_copy(update=update)
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "DISCARDED_SPAN_HAS_EVIDENCE" in exc_info.value.report.codes()


def test_evidence_claim_query_mismatch(bundle_factory):
    bundle = bundle_factory()
    evidence = bundle["evidence_records"][0]
    bundle["evidence_records"][0] = evidence.model_copy(update={"claim_id": "C0002"})
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "EVIDENCE_CLAIM_MISMATCH" in exc_info.value.report.codes()


def test_evidence_paper_span_mismatch(bundle_factory):
    bundle = bundle_factory()
    evidence = bundle["evidence_records"][0]
    bundle["evidence_records"][0] = evidence.model_copy(update={"paper_id": "P0002"})
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "EVIDENCE_PAPER_MISMATCH" in exc_info.value.report.codes()


def test_assessed_span_with_two_evidence_records(bundle_factory):
    bundle = bundle_factory()
    second = bundle["evidence_records"][0].model_copy(update={"evidence_id": "E0002"})
    bundle["evidence_records"].append(second)
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "ASSESSED_SPAN_EVIDENCE_CARDINALITY" in exc_info.value.report.codes()


def test_evidence_missing_span_is_orphan(bundle_factory):
    bundle = bundle_factory()
    evidence = bundle["evidence_records"][0]
    bundle["evidence_records"][0] = evidence.model_copy(update={"retrieved_span_id": "R9999"})
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "INVALID_REFERENCE" in exc_info.value.report.codes()
