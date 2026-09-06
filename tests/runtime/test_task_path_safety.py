"""Milestone-A path safety: destinations stay Python-controlled inside the bundle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vibereview.runtime import (
    MEDIA_EXTENSIONS,
    ParseDeepResearchInvocation,
    ProjectContext,
    ResourceValidationError,
    TaskResourceRequest,
    TaskType,
    resource_destination,
    validate_resource_requests,
)
from vibereview.runtime.specs import TASK_SPECS
from vibereview.runtime.state import RepositorySnapshot
from vibereview.runtime.tasks import TaskWorkspace, bundle_file_path

SPEC = TASK_SPECS[TaskType.PARSE_DEEP_RESEARCH]


def _create_with_requests(
    project: Path,
    context: ProjectContext,
    requests: tuple[TaskResourceRequest, ...],
):
    invocation = ParseDeepResearchInvocation(
        topic="residual stress", document_paths=[r.source_path for r in requests]
    )
    return TaskWorkspace(project).create(
        spec=SPEC,
        invocation=invocation,
        base_generation=0,
        dependencies={},
        resource_requests=requests,
        snapshot=RepositorySnapshot(),
        context=context,
    )


def test_generated_destinations_are_never_absolute(tmp_path):
    for media_type in MEDIA_EXTENSIONS:
        destination = resource_destination("RES0001", media_type)
        assert not destination.is_absolute()
        assert ".." not in destination.parts


@pytest.mark.parametrize("absolute", ["/etc/passwd", "/tmp/evil/content.md"])
def test_absolute_destination_is_rejected(tmp_path, absolute):
    with pytest.raises(ResourceValidationError, match="relative"):
        bundle_file_path(tmp_path / "bundle", Path(absolute))


@pytest.mark.parametrize(
    "relative",
    [
        "../escape.md",
        "input/../../escape.md",
        "input/resources/../../../escape.md",
    ],
)
def test_traversal_destination_is_rejected(tmp_path, relative):
    with pytest.raises(ResourceValidationError, match=r"\.\."):
        bundle_file_path(tmp_path / "bundle", Path(relative))


def test_resolved_destination_stays_inside_the_bundle(tmp_path):
    bundle = tmp_path / "task" / "bundle"
    bundle.mkdir(parents=True)
    destination = bundle_file_path(
        bundle, Path("input/resources/RES0001/content.md")
    )
    assert bundle.resolve() in destination.parents
    assert (
        destination
        == bundle.resolve() / "input" / "resources" / "RES0001" / "content.md"
    )


def test_symlinked_bundle_component_cannot_escape(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (bundle / "input").symlink_to(outside)
    with pytest.raises(ResourceValidationError, match="escapes the bundle"):
        bundle_file_path(bundle, Path("input/resources/RES0001/content.md"))


def test_symlink_source_escape_outside_allowed_roots_is_rejected(tmp_path):
    project = tmp_path / "project"
    documents = project / "docs"
    documents.mkdir(parents=True)
    secret = tmp_path / "secret.md"
    secret.write_text("secret\n", encoding="utf-8")
    link = documents / "innocent.md"
    link.symlink_to(secret)
    requests = (
        TaskResourceRequest(
            resource_id="RES0001",
            logical_name="innocent.md",
            source_path=link,
            media_type="text/markdown",
        ),
    )
    with pytest.raises(ResourceValidationError, match="escapes the allowed source roots"):
        _create_with_requests(project, ProjectContext(project_root=project), requests)
    assert not list((project / "work" / "tasks").glob("TASK*"))


def test_symlink_source_inside_allowed_roots_is_accepted(tmp_path):
    project = tmp_path / "project"
    documents = project / "docs"
    documents.mkdir(parents=True)
    real = documents / "real.md"
    real.write_text("real content\n", encoding="utf-8")
    link = documents / "link.md"
    link.symlink_to(real.name)
    requests = (
        TaskResourceRequest(
            resource_id="RES0001",
            logical_name="link.md",
            source_path=link,
            media_type="text/markdown",
        ),
    )
    task_dir, _, provenance = _create_with_requests(
        project, ProjectContext(project_root=project), requests
    )
    copied = task_dir / "bundle" / "input" / "resources" / "RES0001" / "content.md"
    assert copied.read_text(encoding="utf-8") == "real content\n"
    assert provenance.resources[0].source_path == real.resolve()


def test_explicit_allowed_source_roots_permit_outside_sources(tmp_path):
    project = tmp_path / "project"
    external = tmp_path / "external"
    external.mkdir()
    document = external / "survey.md"
    document.write_text("external content\n", encoding="utf-8")
    requests = (
        TaskResourceRequest(
            resource_id="RES0001",
            logical_name="survey.md",
            source_path=document,
            media_type="text/markdown",
        ),
    )
    task_dir, _, _ = _create_with_requests(
        project,
        ProjectContext(
            project_root=project, allowed_source_roots=(project, external)
        ),
        requests,
    )
    copied = task_dir / "bundle" / "input" / "resources" / "RES0001" / "content.md"
    assert copied.read_text(encoding="utf-8") == "external content\n"


@pytest.mark.parametrize("name", ["../evil.md", "a/b.md", "a\\b.md", "..", ".", ""])
def test_path_like_logical_names_are_rejected(name):
    request = TaskResourceRequest(
        resource_id="RES0001",
        logical_name=name,
        source_path=Path("/documents/source.md"),
        media_type="text/markdown",
    )
    with pytest.raises(ResourceValidationError, match="logical_name"):
        validate_resource_requests([request])


def test_logical_name_never_influences_the_destination(tmp_path):
    project = tmp_path / "project"
    documents = project / "docs"
    documents.mkdir(parents=True)
    source = documents / "source.md"
    source.write_text("content\n", encoding="utf-8")
    requests = (
        TaskResourceRequest(
            resource_id="RES0001",
            logical_name="bundle_manifest.json",
            source_path=source,
            media_type="text/markdown",
        ),
    )
    task_dir, _, provenance = _create_with_requests(
        project, ProjectContext(project_root=project), requests
    )
    # The destination derives only from the resource ID and the media-type map.
    copied = task_dir / "bundle" / "input" / "resources" / "RES0001" / "content.md"
    assert copied.is_file()
    assert provenance.resources[0].bundle_relative_path == Path(
        "input/resources/RES0001/content.md"
    )
    manifest = json.loads(
        (task_dir / "bundle" / "bundle_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["resources"][0]["path"] == "input/resources/RES0001/content.md"
    assert manifest["resources"][0]["logical_name"] == "bundle_manifest.json"
    assert (task_dir / "bundle" / "bundle_manifest.json").is_file()
