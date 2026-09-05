from __future__ import annotations

import pytest

from vibereview.errors import RepositoryValidationError
from vibereview.validators import validate_rendered_prose_bundle


def _validate(bundle):
    return validate_rendered_prose_bundle(
        bundle["proposition_records"],
        bundle["semantic_audits"],
        bundle["rendered_sentences"],
        bundle["rendered_sentence_audits"],
    )


def test_valid_rendered_prose_bundle(bundle_factory):
    assert _validate(bundle_factory()).ok


def test_rendered_sentence_missing_proposition(bundle_factory):
    bundle = bundle_factory()
    sentence = bundle["rendered_sentences"][0]
    bundle["rendered_sentences"][0] = sentence.model_copy(
        update={"source_proposition_ids": ["PR9999"]}
    )
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "INVALID_REFERENCE" in exc_info.value.report.codes()


@pytest.mark.parametrize(
    ("class_verdict", "provenance_verdict"),
    [
        ("MISCLASSIFIED", "ENTAILED"),
        ("CORRECT", "PARTIALLY_SUPPORTED"),
        ("CORRECT", "OVERSTATED"),
        ("CORRECT", "UNSUPPORTED"),
        ("CORRECT", "UNCLEAR"),
    ],
)
def test_rendered_sentence_based_on_failed_proposition(
    class_verdict, provenance_verdict, bundle_factory
):
    bundle = bundle_factory()
    audit = bundle["semantic_audits"][0]
    bundle["semantic_audits"][0] = audit.model_copy(
        update={
            "class_verdict": class_verdict,
            "provenance_verdict": provenance_verdict,
        }
    )
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "RENDERED_FROM_NONPASSING_PROPOSITION" in exc_info.value.report.codes()


def test_rendered_sentence_audit_missing_sentence(bundle_factory):
    bundle = bundle_factory()
    audit = bundle["rendered_sentence_audits"][0]
    bundle["rendered_sentence_audits"][0] = audit.model_copy(
        update={"sentence_id": "RS9999"}
    )
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "INVALID_REFERENCE" in exc_info.value.report.codes()


def test_rendered_sentence_requires_current_audit(bundle_factory):
    bundle = bundle_factory()
    bundle["rendered_sentence_audits"] = []
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "CARDINALITY_VIOLATION" in exc_info.value.report.codes()


def test_rendered_sentence_requires_entailed_audit(bundle_factory):
    bundle = bundle_factory()
    audit = bundle["rendered_sentence_audits"][0]
    bundle["rendered_sentence_audits"][0] = audit.model_copy(
        update={"verdict": "PARTIALLY_SUPPORTED"}
    )
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "RENDERED_SENTENCE_NOT_ENTAILED" in exc_info.value.report.codes()


def test_rendered_sentence_requires_exactly_one_audit(bundle_factory):
    bundle = bundle_factory()
    duplicate = bundle["rendered_sentence_audits"][0].model_copy(
        update={"audit_id": "RSA0002"}
    )
    bundle["rendered_sentence_audits"].append(duplicate)
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate(bundle)
    assert "CARDINALITY_VIOLATION" in exc_info.value.report.codes()

