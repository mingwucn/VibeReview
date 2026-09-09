from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import vibereview.runtime.pilot_manifest as pilot_manifest_module
from vibereview.models import Paper
from vibereview.runtime.dto import ParseDeepResearchInvocation
from vibereview.runtime.engine import MockEngine, MockResponse
from vibereview.runtime.kernel import ProjectRuntime
from vibereview.runtime.pilot_manifest import (
    PilotManifestError,
    build_synthetic_pilot_run_manifest,
    compute_package_c_implementation_fingerprint,
)
from vibereview.runtime.pilot_records import compute_pilot_schema_fingerprint
from vibereview.runtime.records import RuntimeConfig, TaskType
from vibereview.runtime.specs import TASK_SPECS
from vibereview.runtime.state import RepositorySnapshot


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _papers() -> tuple[Paper, ...]:
    return tuple(
        Paper(
            paper_id=f"P{ordinal:04d}",
            title=f"Synthetic paper {ordinal}",
            authors=["Fixture Author"],
            year=2030,
            doi=None,
            journal="Synthetic Journal",
            identity_keys=[f"fixture:paper-{ordinal}"],
            study_group_id=None,
            related_publications=[],
            independence_status="unknown",
            raw_md_path=f"papers/P{ordinal:04d}/raw.md",
            source_hash=_hash(str(ordinal)),
            raw_md_hash=_hash(chr(ord("a") + ordinal)),
        )
        for ordinal in range(1, 6)
    )


def _runtime(tmp_path: Path, *, fallback: int = 0) -> ProjectRuntime:
    resources = tmp_path / "resources"
    resources.mkdir(parents=True)
    return ProjectRuntime.create(
        tmp_path / "review",
        project_name="synthetic-manifest",
        initial_snapshot=RepositorySnapshot(papers=_papers()),
        config=RuntimeConfig(max_fallback_engines=fallback),
        allowed_source_roots=(resources,),
    )


def _resources(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "resources"
    first = root / "discovery-one.md"
    second = root / "discovery-two.md"
    first.write_text("# Synthetic discovery one\n", encoding="utf-8")
    second.write_text("# Synthetic discovery two\n", encoding="utf-8")
    return first, second


def _engines() -> dict[TaskType, MockEngine]:
    shared = MockEngine([MockResponse(proposal={})], name="synthetic", version="1")
    return {task_type: shared for task_type in TaskType}


def _patch_corpus(monkeypatch, runtime: ProjectRuntime) -> None:
    papers = _papers()
    corpus = SimpleNamespace(
        lock_hash=_hash("9"),
        lock=SimpleNamespace(
            selection_manifest_hash=_hash("8"),
            papers=tuple(SimpleNamespace(paper_id=item.paper_id) for item in papers),
        ),
        texts={
            item.paper_id: (
                item.raw_md_path,
                item.raw_md_hash,
                f"# Synthetic source {item.paper_id}\n",
            )
            for item in papers
        },
    )
    monkeypatch.setattr(
        "vibereview.runtime.pilot_manifest._verified_corpus",
        lambda root, generation: corpus,
    )


def test_manifest_binds_exact_task_contracts_and_engine_plan(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path)
    documents = _resources(tmp_path)
    engines = _engines()
    _patch_corpus(monkeypatch, runtime)

    manifest = build_synthetic_pilot_run_manifest(
        runtime,
        run_id="RUN-synthetic-manifest",
        topic="Synthetic five-paper review topic.",
        discovery_document_paths=documents,
        engines=engines,
        created_at="2030-01-01T00:00:00+00:00",
    )

    assert manifest.source_generation == 0
    assert manifest.paper_source_hashes == tuple(
        item.source_hash for item in _papers()
    )
    assert manifest.corpus_lock_hash == _hash("9")
    assert manifest.selection_manifest_hash == _hash("8")
    assert set(manifest.engine_role_plan) == set(TaskType)
    assert all(binding.synthetic for binding in manifest.engine_role_plan.values())
    assert manifest.package_c_implementation_fingerprint == (
        compute_package_c_implementation_fingerprint()
    )
    assert manifest.publication_eligible is False

    spec = TASK_SPECS[TaskType.PARSE_DEEP_RESEARCH]
    _, _, provenance = runtime._create_task(
        spec,
        ParseDeepResearchInvocation(
            topic="Synthetic topic", document_paths=list(documents)
        ),
    )
    assert manifest.prompt_fingerprints[spec.task_type] == provenance.instructions_hash
    assert manifest.schema_fingerprints[spec.task_type] == (
        compute_pilot_schema_fingerprint(
            input_schema_hash=provenance.input_schema_hash,
            proposal_schema_hash=provenance.proposal_schema_hash,
        )
    )


def test_package_c_implementation_fingerprint_is_checkout_path_independent(
    tmp_path: Path, monkeypatch
) -> None:
    expected = compute_package_c_implementation_fingerprint()

    monkeypatch.chdir(tmp_path)

    assert compute_package_c_implementation_fingerprint() == expected


def test_package_c_implementation_fingerprint_binds_top_level_id_code(
    monkeypatch,
) -> None:
    expected = compute_package_c_implementation_fingerprint()
    ids_path = Path(pilot_manifest_module.__file__).resolve().parents[1] / "ids.py"
    original_read_bytes = Path.read_bytes

    def changed_read_bytes(path: Path) -> bytes:
        content = original_read_bytes(path)
        if path.resolve() == ids_path:
            return content + b"\n# synthetic fingerprint drift\n"
        return content

    monkeypatch.setattr(Path, "read_bytes", changed_read_bytes)

    assert compute_package_c_implementation_fingerprint() != expected


def test_manifest_engine_identity_binds_exact_mock_response_script(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path)
    documents = _resources(tmp_path)
    _patch_corpus(monkeypatch, runtime)
    baseline_engines = _engines()
    changed_engines = _engines()
    changed_engines[TaskType.ASSESS_CLAIM] = MockEngine(
        [MockResponse(proposal={"different": True})],
        name="synthetic",
        version="1",
    )

    baseline = build_synthetic_pilot_run_manifest(
        runtime,
        run_id="RUN-script-baseline",
        topic="Synthetic five-paper review topic.",
        discovery_document_paths=documents,
        engines=baseline_engines,
        created_at="2030-01-01T00:00:00+00:00",
    )
    changed = build_synthetic_pilot_run_manifest(
        runtime,
        run_id="RUN-script-changed",
        topic="Synthetic five-paper review topic.",
        discovery_document_paths=documents,
        engines=changed_engines,
        created_at="2030-01-01T00:00:00+00:00",
    )

    assert (
        baseline.engine_role_plan[TaskType.ASSESS_CLAIM].safe_configuration_hash
        != changed.engine_role_plan[TaskType.ASSESS_CLAIM].safe_configuration_hash
    )
    assert baseline.engine_role_plan_hash != changed.engine_role_plan_hash


def test_manifest_rejects_fallback_nonmock_and_incomplete_roles(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path, fallback=1)
    documents = _resources(tmp_path)
    _patch_corpus(monkeypatch, runtime)
    with pytest.raises(PilotManifestError, match="forbids engine fallback"):
        build_synthetic_pilot_run_manifest(
            runtime,
            run_id="RUN-invalid-fallback",
            topic="Synthetic five-paper review topic.",
            discovery_document_paths=documents,
            engines=_engines(),
        )

    runtime = _runtime(tmp_path / "second")
    documents = _resources(tmp_path / "second")
    _patch_corpus(monkeypatch, runtime)
    incomplete = _engines()
    del incomplete[TaskType.AUDIT_RENDERED_SENTENCE]
    with pytest.raises(PilotManifestError, match="cover every TaskType"):
        build_synthetic_pilot_run_manifest(
            runtime,
            run_id="RUN-incomplete-roles",
            topic="Synthetic five-paper review topic.",
            discovery_document_paths=documents,
            engines=incomplete,
        )


def test_manifest_rejects_duplicate_or_outside_discovery_resources(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path)
    first, second = _resources(tmp_path)
    _patch_corpus(monkeypatch, runtime)
    second.write_bytes(first.read_bytes())
    with pytest.raises(PilotManifestError, match="distinct content"):
        build_synthetic_pilot_run_manifest(
            runtime,
            run_id="RUN-duplicate-discovery",
            topic="Synthetic five-paper review topic.",
            discovery_document_paths=(first, second),
            engines=_engines(),
        )

    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    with pytest.raises(PilotManifestError, match="outside"):
        build_synthetic_pilot_run_manifest(
            runtime,
            run_id="RUN-outside-discovery",
            topic="Synthetic five-paper review topic.",
            discovery_document_paths=(first, outside),
            engines=_engines(),
        )
