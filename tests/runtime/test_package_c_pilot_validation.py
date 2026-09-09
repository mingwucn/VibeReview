from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from vibereview.runtime import pilot_validation as validation_module
from vibereview.runtime.artifacts import AssemblyBudget, assemble_exact_section
from vibereview.runtime.hashing import hash_bytes
from vibereview.runtime.pilot_journal import PilotStageArtifactReference
from vibereview.runtime.pilot_records import (
    FivePaperPilotBudget,
    PilotEngineRoleBinding,
    PilotRunManifest,
    PilotRunRegistrationReference,
    PilotStage,
    ValidationStatus,
    compute_pilot_engine_role_plan_hash,
)
from vibereview.runtime.records import TaskType
from vibereview.runtime.repository import GenerationStore, PromotionPayload
from vibereview.runtime.state import RepositorySnapshot


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _manifest() -> PilotRunManifest:
    binding = PilotEngineRoleBinding(
        engine="synthetic-mock",
        engine_version="1",
        safe_configuration_hash=_hash("a"),
    )
    plan = {task_type: binding for task_type in TaskType}
    return PilotRunManifest(
        run_id="RUN-synthetic-validation",
        created_at="2030-01-01T00:00:00+00:00",
        topic="Synthetic validation topic.",
        source_generation=0,
        corpus_lock_hash=_hash("b"),
        selection_manifest_hash=_hash("c"),
        paper_source_hashes=tuple(_hash(str(value)) for value in range(1, 6)),
        discovery_resource_hashes=(_hash("6"), _hash("7")),
        engine_role_plan=plan,
        engine_role_plan_hash=compute_pilot_engine_role_plan_hash(plan),
        prompt_fingerprints={task_type: _hash("d") for task_type in TaskType},
        schema_fingerprints={task_type: _hash("e") for task_type in TaskType},
        validator_fingerprint=_hash("f"),
        package_c_implementation_fingerprint=_hash("0"),
        budget=FivePaperPilotBudget(),
    )


def _registration(manifest: PilotRunManifest) -> PilotRunRegistrationReference:
    artifact_hash = _hash("8")
    return PilotRunRegistrationReference(
        run_id=manifest.run_id,
        owner_generation=1,
        artifact_hash=artifact_hash,
        relative_path=(
            f"pilot/runs/{manifest.run_id}/"
            f"registration-{artifact_hash.removeprefix('sha256:')}.json"
        ),
    )


def _journal_head(manifest: PilotRunManifest) -> PilotStageArtifactReference:
    artifact_hash = _hash("9")
    return PilotStageArtifactReference(
        run_id=manifest.run_id,
        ordinal=1,
        stage=PilotStage.PREREQUISITES,
        owner_generation=1,
        stage_record_hash=_hash("a"),
        artifact_hash=artifact_hash,
        relative_path=(
            f"pilot/runs/{manifest.run_id}/stages/"
            f"0001-prerequisites-{artifact_hash.removeprefix('sha256:')}.json"
        ),
    )


def _generation_one(root: Path, snapshot: RepositorySnapshot) -> None:
    store = GenerationStore(root)
    store.initialize(snapshot)
    store.commit(
        base_generation=0,
        dependencies={},
        promotion=lambda current, registry: PromotionPayload(current, registry, {}),
    )


def _patch_run_anchors(monkeypatch, manifest, registration) -> None:
    monkeypatch.setattr(
        validation_module,
        "load_pilot_run_registration",
        lambda *_args, **_kwargs: (SimpleNamespace(), manifest),
    )
    monkeypatch.setattr(
        validation_module,
        "load_pilot_stage_artifact",
        lambda *_args, **_kwargs: SimpleNamespace(
            stage_record=SimpleNamespace(committed_generation=1)
        ),
    )


def test_validation_reports_each_check_independently(
    tmp_path: Path, bundle_factory, monkeypatch
) -> None:
    snapshot = RepositorySnapshot.model_validate(bundle_factory())
    original_span = snapshot.retrieved_spans[0]
    exact_locator = original_span.locator.model_copy(
        update={
            "source_span_hash": hash_bytes(original_span.source_text.encode("utf-8"))
        }
    )
    snapshot = snapshot.model_copy(
        update={
            "retrieved_spans": (
                original_span.model_copy(update={"locator": exact_locator}),
            )
        }
    )
    root = tmp_path / "review"
    _generation_one(root, snapshot)
    manifest = _manifest()
    registration = _registration(manifest)
    _patch_run_anchors(monkeypatch, manifest, registration)

    span = snapshot.retrieved_spans[0]
    paper = snapshot.papers[0]
    locator = span.locator
    text = " " * locator.start_offset + span.source_text + " trailing context"
    corpus = SimpleNamespace(
        lock_hash=manifest.corpus_lock_hash,
        texts={paper.paper_id: (paper.raw_md_path, paper.raw_md_hash, text)},
    )
    monkeypatch.setattr(validation_module, "_verified_corpus", lambda *_args: corpus)
    body, assembly = assemble_exact_section(
        snapshot,
        source_generation=1,
        budget=AssemblyBudget(
            max_sentences=18,
            max_citations=30,
            max_body_utf8_bytes=64 * 1024,
        ),
    )

    result = validation_module.validate_synthetic_pilot_run(
        root,
        registration=registration,
        journal_head=_journal_head(manifest),
        body=body,
        assembly=assembly,
    )

    report = result.report
    assert report.structural_validation is ValidationStatus.PASSED
    assert report.locator_verification is ValidationStatus.PASSED
    assert report.semantic_audits_executed is ValidationStatus.PASSED
    assert report.citation_authorization is ValidationStatus.PASSED
    assert report.exact_assembly is ValidationStatus.PASSED
    assert report.artifact_integrity is ValidationStatus.PASSED
    assert report.human_review_status.value == "NOT_PERFORMED"
    assert report.publication_eligible is False
    assert all(not messages for messages in result.diagnostics.values())


def test_validation_does_not_turn_failed_or_unexecuted_checks_into_pass(
    tmp_path: Path, bundle_factory, monkeypatch
) -> None:
    snapshot = RepositorySnapshot.model_validate(bundle_factory())
    root = tmp_path / "review"
    _generation_one(root, snapshot)
    manifest = _manifest()
    registration = _registration(manifest)
    _patch_run_anchors(monkeypatch, manifest, registration)
    paper = snapshot.papers[0]
    corpus = SimpleNamespace(
        lock_hash=manifest.corpus_lock_hash,
        texts={paper.paper_id: (paper.raw_md_path, paper.raw_md_hash, "wrong")},
    )
    monkeypatch.setattr(validation_module, "_verified_corpus", lambda *_args: corpus)

    result = validation_module.validate_synthetic_pilot_run(
        root,
        registration=registration,
        journal_head=None,
        body=b"partial",
        assembly=None,
    )

    assert result.report.structural_validation is ValidationStatus.PASSED
    assert result.report.locator_verification is ValidationStatus.FAILED
    assert result.report.exact_assembly is ValidationStatus.FAILED
    assert result.report.artifact_integrity is ValidationStatus.NOT_EXECUTED
    assert result.diagnostics["locator_verification"]
    assert result.report.publication_eligible is False


def test_no_scientific_objects_are_explicitly_not_executed(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "review"
    _generation_one(root, RepositorySnapshot())
    manifest = _manifest()
    registration = _registration(manifest)
    _patch_run_anchors(monkeypatch, manifest, registration)

    result = validation_module.validate_synthetic_pilot_run(
        root,
        registration=registration,
        journal_head=None,
    )

    assert result.report.structural_validation is ValidationStatus.PASSED
    assert result.report.locator_verification is ValidationStatus.NOT_EXECUTED
    assert result.report.semantic_audits_executed is ValidationStatus.NOT_EXECUTED
    assert result.report.citation_authorization is ValidationStatus.NOT_EXECUTED
    assert result.report.exact_assembly is ValidationStatus.NOT_EXECUTED
    assert result.report.artifact_integrity is ValidationStatus.NOT_EXECUTED
    assert result.report.repository_hash == RepositorySnapshot().canonical_hash()


def test_validation_rejects_current_change_during_checks(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "review"
    _generation_one(root, RepositorySnapshot())
    manifest = _manifest()
    registration = _registration(manifest)
    _patch_run_anchors(monkeypatch, manifest, registration)
    original = GenerationStore.load_current
    calls = 0

    def changing_current(store):
        nonlocal calls
        calls += 1
        generation, snapshot, registry = original(store)
        if calls == 2:
            generation += 1
        return generation, snapshot, registry

    monkeypatch.setattr(GenerationStore, "load_current", changing_current)
    with pytest.raises(
        validation_module.PilotValidationError,
        match="changed during validation",
    ):
        validation_module.validate_synthetic_pilot_run(
            root,
            registration=registration,
            journal_head=None,
        )


def test_validation_reauthenticates_registration_after_checks(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "review"
    _generation_one(root, RepositorySnapshot())
    manifest = _manifest()
    registration = _registration(manifest)
    artifacts = iter(
        (SimpleNamespace(version=1), SimpleNamespace(version=2))
    )
    monkeypatch.setattr(
        validation_module,
        "load_pilot_run_registration",
        lambda *_args, **_kwargs: (next(artifacts), manifest),
    )

    with pytest.raises(
        validation_module.PilotValidationError,
        match="changed during validation",
    ):
        validation_module.validate_synthetic_pilot_run(
            root,
            registration=registration,
            journal_head=None,
        )
