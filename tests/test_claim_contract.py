from __future__ import annotations

import pytest
from pydantic import ValidationError

from vibereview.errors import RepositoryValidationError
from vibereview.models import ClaimPaperEvidence, ComponentRelations
from vibereview.validators import validate_claim_bundle, validate_claim_evidence_bundle


def _validate_cpe(bundle):
    return validate_claim_evidence_bundle(
        bundle["candidate_claims"],
        bundle["papers"],
        bundle["evidence_records"],
        bundle["claim_paper_evidence"],
    )


def _validate_claim(bundle):
    return validate_claim_bundle(
        bundle["candidate_claims"],
        bundle["claim_paper_evidence"],
        bundle["claim_assessments"],
        bundle["final_claim_validations"],
        bundle["claim_packets"],
    )


def test_valid_claim_paper_evidence(bundle_factory):
    assert _validate_cpe(bundle_factory()).ok


def test_cpe_component_set_mismatch(bundle_factory):
    data = bundle_factory()["claim_paper_evidence"][0].model_dump()
    data["component_relations"] = ComponentRelations().model_dump()
    with pytest.raises(ValidationError):
        ClaimPaperEvidence.model_validate(data)


def test_cpe_duplicate_evidence(bundle_factory):
    data = bundle_factory()["claim_paper_evidence"][0].model_dump()
    data["evidence_ids"] = ["E0001", "E0001"]
    data["component_relations"] = ComponentRelations(
        supports=["E0001", "E0001"]
    ).model_dump()
    with pytest.raises(ValidationError):
        ClaimPaperEvidence.model_validate(data)


def test_cpe_relation_must_match_evidence_record(bundle_factory):
    bundle = bundle_factory()
    evidence = bundle["evidence_records"][0]
    bundle["evidence_records"][0] = evidence.model_copy(
        update={"relation_to_candidate": "qualifies"}
    )
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate_cpe(bundle)
    assert "CPE_RELATION_MISMATCH" in exc_info.value.report.codes()


def test_invalid_mixed_cpe(bundle_factory):
    data = bundle_factory()["claim_paper_evidence"][0].model_dump()
    data["relation_to_candidate"] = "mixed"
    with pytest.raises(ValidationError):
        ClaimPaperEvidence.model_validate(data)


def test_cpe_evidence_claim_mismatch(bundle_factory):
    bundle = bundle_factory()
    evidence = bundle["evidence_records"][0]
    bundle["evidence_records"][0] = evidence.model_copy(update={"claim_id": "C0002"})
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate_cpe(bundle)
    assert "CPE_CLAIM_MISMATCH" in exc_info.value.report.codes()


def test_cpe_evidence_paper_mismatch(bundle_factory):
    bundle = bundle_factory()
    evidence = bundle["evidence_records"][0]
    bundle["evidence_records"][0] = evidence.model_copy(update={"paper_id": "P0002"})
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate_cpe(bundle)
    assert "CPE_PAPER_MISMATCH" in exc_info.value.report.codes()


def test_valid_claim_bundle(bundle_factory):
    assert _validate_claim(bundle_factory()).ok


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate_claim", "Different candidate text."),
        ("theme_id", "T0002"),
        ("aggregate_strength", "moderate"),
        ("final_claim", "Different final claim."),
    ],
)
def test_claim_packet_upstream_field_mismatch(field, value, bundle_factory):
    bundle = bundle_factory()
    packet = bundle["claim_packets"][0]
    bundle["claim_packets"][0] = packet.model_copy(update={field: value})
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate_claim(bundle)
    assert "CLAIM_PACKET_UPSTREAM_MISMATCH" in exc_info.value.report.codes()


def test_claim_packet_requires_valid_final_validation(bundle_factory):
    bundle = bundle_factory()
    final = bundle["final_claim_validations"][0]
    bundle["final_claim_validations"][0] = final.model_copy(
        update={"status": "REVISE_AGAIN"}
    )
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate_claim(bundle)
    assert "CLAIM_PACKET_REQUIRES_VALID_FINAL_CLAIM" in exc_info.value.report.codes()


def test_claim_packet_requires_final_validation_object(bundle_factory):
    bundle = bundle_factory()
    bundle["final_claim_validations"] = []
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate_claim(bundle)
    assert "INVALID_REFERENCE" in exc_info.value.report.codes()


def test_cpe_set_discontinuity(bundle_factory):
    bundle = bundle_factory()
    packet = bundle["claim_packets"][0]
    bundle["claim_packets"][0] = packet.model_copy(
        update={"claim_paper_evidence_ids": []}
    )
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate_claim(bundle)
    assert "CPE_SET_CONTINUITY_MISMATCH" in exc_info.value.report.codes()


def test_final_relation_paper_must_match_cpe(bundle_factory):
    bundle = bundle_factory()
    final = bundle["final_claim_validations"][0]
    relation = final.paper_relations[0].model_copy(update={"paper_id": "P0002"})
    bundle["final_claim_validations"][0] = final.model_copy(
        update={"paper_relations": [relation]}
    )
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate_claim(bundle)
    assert "FINAL_RELATION_PAPER_MISMATCH" in exc_info.value.report.codes()


def test_claim_packet_cpe_must_resolve(bundle_factory):
    bundle = bundle_factory()
    bundle["claim_paper_evidence"] = []
    with pytest.raises(RepositoryValidationError) as exc_info:
        _validate_claim(bundle)
    assert "INVALID_REFERENCE" in exc_info.value.report.codes()

