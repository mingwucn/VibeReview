"""P3 review-project artifact versioning tests (goal.md §50 versioning tests)."""

from __future__ import annotations

from pathlib import Path

import pytest

from vibereview.prompting.models import PromptingError
from vibereview.prompting.project import (
    REQUIRED_DIRECTORIES,
    init_project,
    list_versions,
    load_prompt_profile,
    migrate_prompt_profile,
    project_status,
    resolve_current,
    write_versioned_artifact,
)


def test_project_init_creates_layout_and_configs(tmp_path: Path) -> None:
    root = tmp_path / "AI_NCM_Review"
    init_project(
        root,
        project_id="AI_NCM_2026",
        working_topic="AI-assisted precision non-conventional manufacturing",
    )
    for rel in REQUIRED_DIRECTORIES:
        assert (root / rel).is_dir(), rel
    assert (root / "project.yaml").is_file()
    assert (root / "project" / "prompt_profile_v001.yaml").is_file()
    text = (root / "project.yaml").read_text(encoding="utf-8")
    assert "AI_NCM_2026" in text
    assert "systematic_search_claim: false" in text
    assert "gap_claims_allowed: false" in text
    assert (root / "outline" / "current").is_file() is False


def test_project_init_fills_template_placeholders(tmp_path: Path) -> None:
    root = tmp_path / "AI_NCM_Review"
    init_project(
        root,
        project_id="AI_NCM_2026",
        working_topic="AI-assisted precision non-conventional manufacturing",
        core_processes=("edm", "ecm", "laser_precision"),
        secondary_processes=("ultrasonic", "abrasive_jet", "waterjet", "ecdm", "hybrid"),
        future_outlook=("atomic_manufacturing",),
        cutoff="2026-09-14",
    )
    import yaml

    config = yaml.safe_load((root / "project.yaml").read_text(encoding="utf-8"))
    assert config["project_id"] == "AI_NCM_2026"
    assert config["core_processes"] == ["edm", "ecm", "laser_precision"]
    assert config["secondary_processes"] == ["ultrasonic", "abrasive_jet", "waterjet", "ecdm", "hybrid"]
    assert config["future_outlook"] == ["atomic_manufacturing"]
    assert config["publication_window"]["cutoff"] == "2026-09-14"
    profile = yaml.safe_load(
        (root / "project" / "prompt_profile_v001.yaml").read_text(encoding="utf-8")
    )
    assert profile["protocol"]["id"] == "vibereview-review-protocol-1.0"
    assert profile["focus_modules"] == {}
    assert "{{" not in (root / "project.yaml").read_text(encoding="utf-8")


def test_render_project_template_fails_on_unfilled_placeholder(tmp_path: Path) -> None:
    from vibereview.prompting.project import render_project_template

    template = tmp_path / "template"
    template.mkdir()
    (template / "project.yaml").write_text('project_id: "{{PROJECT_ID}}"\nlabel: "{{UNKNOWN_TOKEN}}"\n', encoding="utf-8")
    with pytest.raises(PromptingError) as excinfo:
        render_project_template(template, {"PROJECT_ID": "X"})
    assert excinfo.value.code == "PLACEHOLDER_UNFILLED"
    with pytest.raises(PromptingError) as excinfo:
        render_project_template(tmp_path / "missing", {"PROJECT_ID": "X"})
    assert excinfo.value.code == "TEMPLATE_NOT_FOUND"


def test_project_init_refuses_to_overwrite(tmp_path: Path) -> None:
    root = tmp_path / "review"
    init_project(root, project_id="X", working_topic="topic")
    with pytest.raises(PromptingError) as excinfo:
        init_project(root, project_id="X", working_topic="topic")
    assert excinfo.value.code == "ARTIFACT_EXISTS"


def test_versioned_artifacts_are_append_only(tmp_path: Path) -> None:
    outline_dir = tmp_path / "outline"
    first = write_versioned_artifact(outline_dir, "outline", "# Outline v1\n")
    assert first.name == "outline_v001.md"
    second = write_versioned_artifact(outline_dir, "outline", "# Outline v2\n")
    assert second.name == "outline_v002.md"
    assert first.read_text(encoding="utf-8") == "# Outline v1\n"
    with pytest.raises(PromptingError) as excinfo:
        write_versioned_artifact(outline_dir, "outline", "# Outline v1 rewritten\n")
    assert excinfo.value.code == "ARTIFACT_EXISTS"
    # Pre-seeding a gap must fail closed rather than silently filling it.
    (outline_dir / "outline_v004.md").write_text("# seeded\n", encoding="utf-8")
    with pytest.raises(PromptingError) as excinfo:
        write_versioned_artifact(outline_dir, "outline", "# Outline v3\n")
    assert excinfo.value.code == "ARTIFACT_ORDINAL_GAP"


def test_outline_current_pointer_tracks_latest(tmp_path: Path) -> None:
    outline_dir = tmp_path / "outline"
    write_versioned_artifact(outline_dir, "outline", "# v1\n")
    assert (outline_dir / "current").read_text(encoding="utf-8") == "outline_v001.md"
    write_versioned_artifact(outline_dir, "outline", "# v2\n")
    assert (outline_dir / "current").read_text(encoding="utf-8") == "outline_v002.md"
    assert resolve_current(outline_dir, "outline").name == "outline_v002.md"
    assert [p.name for p in list_versions(outline_dir, "outline")] == [
        "outline_v001.md",
        "outline_v002.md",
    ]


def test_prompt_profile_migration_writes_new_version_and_preserves_old(
    tmp_path: Path,
) -> None:
    root = tmp_path / "review"
    init_project(root, project_id="X", working_topic="topic")
    profile_path, profile = load_prompt_profile(root)
    assert profile_path.name == "prompt_profile_v001.yaml"
    before = profile_path.read_bytes()
    new_path = migrate_prompt_profile(
        root,
        protocol="vibereview-review-protocol-1.0",
        focus_modules={"S03": ["edm@1.0.0"]},
        reason="pin section focus modules",
    )
    assert new_path.name == "prompt_profile_v002.yaml"
    assert profile_path.read_bytes() == before
    _, migrated = load_prompt_profile(root)
    assert migrated["prompt_profile_version"] == 1
    assert migrated["focus_modules"]["S03"] == ["edm@1.0.0"]
    # Old profile remains loadable by explicit path (replayability).
    import yaml

    old = yaml.safe_load(before.decode("utf-8"))
    assert old["focus_modules"] == {}


def test_profile_migration_without_change_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "review"
    init_project(root, project_id="X", working_topic="topic")
    with pytest.raises(PromptingError) as excinfo:
        migrate_prompt_profile(root, reason="nothing changes")
    assert excinfo.value.code == "PROFILE_MIGRATION_UNCHANGED"


def test_project_status_reports_versions_and_runs(tmp_path: Path) -> None:
    root = tmp_path / "review"
    init_project(root, project_id="X", working_topic="topic")
    write_versioned_artifact(root / "project", "project_brief", "# brief\n")
    write_versioned_artifact(root / "outline", "outline", "# outline\n")
    runs_dir = root / "deep_research" / "runs" / "DR001_scoping"
    runs_dir.mkdir(parents=True)
    status = project_status(root)
    assert status.project_id == "X"
    names = [v.name for v in status.artifact_versions["project_brief"]]
    assert names == ["project_brief_v001.md"]
    assert status.registered_runs == ("DR001_scoping",)
