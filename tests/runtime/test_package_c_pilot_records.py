from __future__ import annotations

import pytest
from pydantic import ValidationError

from vibereview.runtime.pilot_records import (
    EngineUsageRecord,
    FivePaperPilotBudget,
    HumanReviewStatus,
    PilotEngineRoleBinding,
    PilotRunManifest,
    PilotStage,
    PilotStageRecord,
    PilotStageStatus,
    PilotValidationReport,
    ValidationStatus,
    compute_pilot_engine_role_plan_hash,
)
from vibereview.runtime.records import TaskType


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _manifest() -> PilotRunManifest:
    binding = PilotEngineRoleBinding(
        engine="synthetic-mock",
        engine_version="1",
        safe_configuration_hash=_hash("8"),
    )
    plan = {task_type: binding for task_type in TaskType}
    return PilotRunManifest(
        run_id="RUN-synthetic-01",
        created_at="2030-01-01T00:00:00+00:00",
        topic="Synthetic pilot-record topic.",
        source_generation=3,
        corpus_lock_hash=_hash("a"),
        selection_manifest_hash=_hash("b"),
        paper_source_hashes=tuple(_hash(str(value)) for value in range(1, 6)),
        discovery_resource_hashes=(_hash("6"), _hash("7")),
        engine_role_plan=plan,
        engine_role_plan_hash=compute_pilot_engine_role_plan_hash(plan),
        prompt_fingerprints={task_type: _hash("9") for task_type in TaskType},
        schema_fingerprints={task_type: _hash("a") for task_type in TaskType},
        validator_fingerprint=_hash("b"),
        package_c_implementation_fingerprint=_hash("c"),
        budget=FivePaperPilotBudget(),
    )


def test_run_manifest_is_closed_five_paper_nonpublication_record() -> None:
    manifest = _manifest()

    assert manifest.budget.paper_count == 5
    assert len(manifest.paper_source_hashes) == 5
    assert manifest.human_review_status is HumanReviewStatus.NOT_PERFORMED
    assert manifest.publication_eligible is False
    assert manifest.manifest_hash.startswith("sha256:")
    with pytest.raises(ValidationError):
        PilotRunManifest.model_validate(
            {**manifest.model_dump(mode="json"), "publication_eligible": True}
        )


def test_run_manifest_rejects_duplicate_inputs_and_missing_fingerprints() -> None:
    values = _manifest().model_dump(mode="json")
    values["paper_source_hashes"] = [_hash("1")] * 5
    with pytest.raises(ValidationError, match="five distinct"):
        PilotRunManifest.model_validate(values)

    values = _manifest().model_dump(mode="json")
    values["prompt_fingerprints"] = {}
    with pytest.raises(ValidationError, match="cover every TaskType"):
        PilotRunManifest.model_validate(values)

    values = _manifest().model_dump(mode="json")
    values["engine_role_plan_hash"] = _hash("f")
    with pytest.raises(ValidationError, match="role-plan hash mismatch"):
        PilotRunManifest.model_validate(values)

    values = _manifest().model_dump(mode="json")
    values["schema_fingerprints"].pop(TaskType.AUDIT_RENDERED_SENTENCE.value)
    with pytest.raises(ValidationError, match="cover every TaskType"):
        PilotRunManifest.model_validate(values)

    values = _manifest().model_dump(mode="json")
    del values["package_c_implementation_fingerprint"]
    with pytest.raises(ValidationError, match="package_c_implementation_fingerprint"):
        PilotRunManifest.model_validate(values)


def test_run_budget_has_finite_per_attempt_and_cumulative_resource_caps() -> None:
    budget = FivePaperPilotBudget()

    assert budget.max_stdout_bytes_per_attempt == 64 * 1024
    assert budget.max_stderr_bytes_per_attempt == 64 * 1024
    assert budget.max_retained_diagnostic_bytes_per_attempt == 128 * 1024
    assert budget.max_writable_entries_per_attempt == 256
    assert budget.max_writable_tree_bytes_per_attempt == 16 * 1024 * 1024
    assert budget.max_cumulative_stdout_bytes == 8 * 1024 * 1024
    assert budget.max_cumulative_stderr_bytes == 8 * 1024 * 1024
    assert budget.max_cumulative_retained_diagnostic_bytes == 16 * 1024 * 1024
    assert budget.max_cumulative_writable_entries == 8_192
    assert budget.max_cumulative_writable_tree_bytes == 256 * 1024 * 1024


@pytest.mark.parametrize(
    "updates",
    [
        {"max_discovery_total_bytes": 9, "max_discovery_document_bytes": 10},
        {"max_corpus_bytes": 9, "max_paper_bytes": 10},
        {"max_queries": 4, "max_claims": 2},
        {"max_raw_candidates": 5, "max_assessed_candidates": 6},
        {"max_assessed_candidates": 5, "max_evidence_records": 6},
        {"max_retained_diagnostic_bytes_per_attempt": (128 * 1024) - 1},
        {"max_writable_tree_bytes_per_attempt": (1024 * 1024) - 1},
        {"max_cumulative_stdout_bytes": (64 * 1024) - 1},
        {"max_cumulative_writable_entries": 255},
        {"max_cumulative_writable_tree_bytes": (16 * 1024 * 1024) - 1},
        {"max_cumulative_retained_diagnostic_bytes": (16 * 1024 * 1024) - 1},
    ],
)
def test_cross_budget_inconsistencies_fail_closed(updates) -> None:
    with pytest.raises(ValidationError):
        FivePaperPilotBudget(**updates)


def test_stage_records_are_hash_chained_and_status_complete() -> None:
    first = PilotStageRecord(
        run_id="RUN-synthetic-01",
        ordinal=1,
        stage=PilotStage.PREREQUISITES,
        status=PilotStageStatus.COMPLETED,
        source_generation=3,
        committed_generation=4,
        input_hashes={"manifest": _hash("1")},
    )
    second = PilotStageRecord(
        run_id=first.run_id,
        ordinal=2,
        stage=PilotStage.DISCOVERY,
        status=PilotStageStatus.BLOCKED,
        source_generation=4,
        committed_generation=5,
        input_hashes={"discovery": _hash("2")},
        previous_stage={
            "run_id": first.run_id,
            "ordinal": first.ordinal,
            "stage": first.stage,
            "owner_generation": first.committed_generation,
            "stage_record_hash": first.record_hash,
            "artifact_hash": _hash("3"),
            "relative_path": "pilot/runs/RUN-synthetic-01/stages/first.json",
        },
        reason="Synthetic budget was exhausted.",
    )

    assert first.record_hash.startswith("sha256:")
    assert second.previous_stage is not None
    assert second.previous_stage.stage_record_hash == first.record_hash
    with pytest.raises(ValidationError, match="requires an exact predecessor"):
        PilotStageRecord.model_validate(
            {**second.model_dump(mode="json"), "previous_stage": None}
        )


def test_usage_is_explicit_when_provider_metrics_are_unavailable() -> None:
    unavailable = EngineUsageRecord(
        task_id="TASK0001",
        attempt_id="01-mock",
        engine="mock",
        unavailable_reason="Mock engine does not report token usage.",
    )
    assert unavailable.input_tokens is None
    with pytest.raises(ValidationError, match="explicit reason"):
        EngineUsageRecord.model_validate(
            {**unavailable.model_dump(mode="json"), "unavailable_reason": None}
        )
    with pytest.raises(ValidationError, match="currency"):
        EngineUsageRecord(
            task_id="TASK0001",
            attempt_id="01-mock",
            engine="mock",
            input_tokens=1,
            output_tokens=1,
            cost=0.5,
        )


def test_validation_report_keeps_independent_checks_and_blocks_approval() -> None:
    report = PilotValidationReport(
        run_id="RUN-synthetic-01",
        source_generation=9,
        repository_hash=_hash("a"),
        structural_validation=ValidationStatus.PASSED,
        locator_verification=ValidationStatus.FAILED,
        semantic_audits_executed=ValidationStatus.PASSED,
        citation_authorization=ValidationStatus.PASSED,
        exact_assembly=ValidationStatus.NOT_EXECUTED,
        artifact_integrity=ValidationStatus.PASSED,
        human_review_status=HumanReviewStatus.NOT_PERFORMED,
    )
    assert report.publication_eligible is False
    assert report.locator_verification is ValidationStatus.FAILED
    with pytest.raises(ValidationError):
        PilotValidationReport.model_validate(
            {
                **report.model_dump(mode="json"),
                "human_review_status": HumanReviewStatus.APPROVED,
            }
        )
