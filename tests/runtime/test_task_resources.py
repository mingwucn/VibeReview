"""Milestone-A resource copying: byte fidelity, recorded hashes, mutation, collisions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vibereview.runtime import (
    MockEngine,
    MockResponse,
    ParseDeepResearchInvocation,
    ProjectContext,
    ProjectRuntime,
    ResourceValidationError,
    SnapshotSourceChangedError,
    TaskResourceRequest,
    TaskType,
)
from vibereview.runtime.hashing import hash_bytes, hash_file
from vibereview.runtime.records import TaskProvenance
from vibereview.runtime.specs import TASK_SPECS
from vibereview.runtime.state import RepositorySnapshot
from vibereview.runtime.tasks import TaskWorkspace

SPEC = TASK_SPECS[TaskType.PARSE_DEEP_RESEARCH]


def _write_documents(project: Path, documents: dict[str, str]) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for name, content in documents.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        paths[name] = path
    return paths


def _create_task(
    workspace: TaskWorkspace,
    context: ProjectContext,
    document_paths: list[Path],
    resource_requests=None,
) -> tuple[Path, object, TaskProvenance]:
    invocation = ParseDeepResearchInvocation(
        topic="residual stress", document_paths=document_paths
    )
    if resource_requests is None:
        resource_requests = SPEC.resource_builder(
            invocation, RepositorySnapshot(), context
        )
    return workspace.create(
        spec=SPEC,
        invocation=invocation,
        base_generation=0,
        dependencies={},
        resource_requests=resource_requests,
        snapshot=RepositorySnapshot(),
        context=context,
    )


def test_copied_bytes_match_source_bytes(tmp_path):
    project = tmp_path / "project"
    documents = _write_documents(
        project,
        {
            "docs/alpha.md": "# Alpha\n\nUnicode content α β γ\n",
            "docs/beta.md": "# Beta\n",
        },
    )
    task_dir, _, _ = _create_task(
        TaskWorkspace(project),
        ProjectContext(project_root=project),
        [documents["docs/alpha.md"], documents["docs/beta.md"]],
    )
    resources = task_dir / "bundle" / "input" / "resources"
    first = resources / "RES0001" / "content.md"
    second = resources / "RES0002" / "content.md"
    assert first.read_bytes() == documents["docs/alpha.md"].read_bytes()
    assert second.read_bytes() == documents["docs/beta.md"].read_bytes()
    assert not (first.stat().st_mode & 0o222)
    assert not (second.stat().st_mode & 0o222)


def test_recorded_hash_and_size_match_copied_bytes(tmp_path):
    project = tmp_path / "project"
    documents = _write_documents(
        project,
        {"docs/alpha.md": "# Alpha\n", "docs/beta.md": "# Beta\nmore bytes\n"},
    )
    task_dir, _, provenance = _create_task(
        TaskWorkspace(project),
        ProjectContext(project_root=project),
        [documents["docs/alpha.md"], documents["docs/beta.md"]],
    )
    assert [entry.resource_id for entry in provenance.resources] == [
        "RES0001",
        "RES0002",
    ]
    for entry in provenance.resources:
        destination = task_dir / "bundle" / entry.bundle_relative_path
        copied_hash = hash_bytes(destination.read_bytes())
        assert entry.snapshot_hash == copied_hash == hash_file(destination)
        assert entry.size_bytes == destination.stat().st_size
        assert entry.source_dependency.type == "external_file"
        assert entry.source_dependency.source_hash_at_snapshot == copied_hash
        # The recorded hash comes from the bytes written; the source was
        # verified stable during the copy, so both agree.
        assert hash_file(entry.source_path) == copied_hash

    engine_input = json.loads(
        (task_dir / "bundle" / "input" / "input.json").read_text(encoding="utf-8")
    )
    assert engine_input["document_resource_ids"] == ["RES0001", "RES0002"]
    manifest = json.loads(
        (task_dir / "bundle" / "bundle_manifest.json").read_text(encoding="utf-8")
    )
    entries = {entry["resource_id"]: entry for entry in manifest["resources"]}
    for entry in provenance.resources:
        assert entries[entry.resource_id]["sha256"] == entry.snapshot_hash
        assert entries[entry.resource_id]["size_bytes"] == entry.size_bytes
        assert entries[entry.resource_id]["logical_name"] == entry.logical_name
    # Engine-visible JSON never carries live source paths.
    assert str(project) not in json.dumps(engine_input)
    assert str(project) not in json.dumps(manifest)


def test_mutation_during_copy_aborts_task_construction(tmp_path):
    project = tmp_path / "project"
    documents = _write_documents(project, {"docs/survey.md": "x" * 4096})

    def mutate(source: Path, chunk_index: int) -> None:
        if chunk_index == 0:
            with source.open("ab") as handle:
                handle.write(b"y" * 128)

    workspace = TaskWorkspace(project, snapshot_chunk_size=64, snapshot_hook=mutate)
    with pytest.raises(SnapshotSourceChangedError) as excinfo:
        _create_task(
            workspace,
            ProjectContext(project_root=project),
            [documents["docs/survey.md"]],
        )
    assert excinfo.value.code == "SNAPSHOT_SOURCE_CHANGED"
    assert excinfo.value.resource_id == "RES0001"
    tasks_root = project / "work" / "tasks"
    assert not list(tasks_root.glob("TASK*"))


def test_snapshot_failure_propagates_from_run_without_engine_invocation(tmp_path):
    project = tmp_path / "project"
    documents = _write_documents(project, {"docs/survey.md": "x" * 4096})

    def mutate(source: Path, chunk_index: int) -> None:
        if chunk_index == 0:
            with source.open("ab") as handle:
                handle.write(b"y" * 128)

    runtime = ProjectRuntime.create(
        project,
        project_name="snapshot",
        snapshot_chunk_size=64,
        snapshot_hook=mutate,
    )
    engine = MockEngine([MockResponse(proposal={"themes": [], "claims": []})])
    with pytest.raises(SnapshotSourceChangedError):
        runtime.run(
            TaskType.PARSE_DEEP_RESEARCH,
            ParseDeepResearchInvocation(
                topic="residual stress",
                document_paths=[documents["docs/survey.md"]],
            ),
            engines=[engine],
        )
    assert engine.calls == 0
    assert not list((project / "work" / "tasks").glob("TASK*"))


def test_duplicate_source_filenames_coexist_as_distinct_resources(tmp_path):
    project = tmp_path / "project"
    documents = _write_documents(
        project,
        {"a/notes.md": "first notes\n", "b/notes.md": "second notes\n"},
    )
    task_dir, _, provenance = _create_task(
        TaskWorkspace(project),
        ProjectContext(project_root=project),
        [documents["a/notes.md"], documents["b/notes.md"]],
    )
    resources = task_dir / "bundle" / "input" / "resources"
    assert (resources / "RES0001" / "content.md").read_text(
        encoding="utf-8"
    ) == "first notes\n"
    assert (resources / "RES0002" / "content.md").read_text(
        encoding="utf-8"
    ) == "second notes\n"
    assert [entry.resource_id for entry in provenance.resources] == [
        "RES0001",
        "RES0002",
    ]
    assert [entry.logical_name for entry in provenance.resources] == [
        "notes.md",
        "notes.md",
    ]


def test_duplicate_resource_id_is_rejected(tmp_path):
    project = tmp_path / "project"
    documents = _write_documents(
        project, {"docs/a.md": "a\n", "docs/b.md": "b\n"}
    )
    requests = (
        TaskResourceRequest(
            resource_id="RES0001",
            logical_name="a.md",
            source_path=documents["docs/a.md"],
            media_type="text/markdown",
        ),
        TaskResourceRequest(
            resource_id="RES0001",
            logical_name="b.md",
            source_path=documents["docs/b.md"],
            media_type="text/markdown",
        ),
    )
    with pytest.raises(ResourceValidationError, match="duplicate resource_id"):
        _create_task(
            TaskWorkspace(project),
            ProjectContext(project_root=project),
            [documents["docs/a.md"], documents["docs/b.md"]],
            resource_requests=requests,
        )
    assert not list((project / "work" / "tasks").glob("TASK*"))


def test_unsupported_media_type_is_rejected(tmp_path):
    project = tmp_path / "project"
    documents = _write_documents(project, {"docs/slides.pdf": "pdf-bytes\n"})
    requests = (
        TaskResourceRequest(
            resource_id="RES0001",
            logical_name="slides.pdf",
            source_path=documents["docs/slides.pdf"],
            media_type="application/pdf",
        ),
    )
    with pytest.raises(ResourceValidationError, match="unsupported media type"):
        _create_task(
            TaskWorkspace(project),
            ProjectContext(project_root=project),
            [documents["docs/slides.pdf"]],
            resource_requests=requests,
        )
    assert not list((project / "work" / "tasks").glob("TASK*"))
