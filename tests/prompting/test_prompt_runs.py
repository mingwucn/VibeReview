"""P4 Deep Research run-registration tests (goal.md §50 run-registration tests)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vibereview.prompting.compiler import compile_prompt, write_compiled
from vibereview.prompting.models import PromptingError
from vibereview.prompting.project import init_project, write_versioned_artifact
from vibereview.prompting.registry import load_registry
from vibereview.prompting.runs import register_run
from vibereview.runtime.hashing import hash_file, hash_text

from conftest import build_registry_root, make_focus_body, make_prompt_body


def _project_with_compiled_prompt(tmp_path: Path):
    registry_root = build_registry_root(
        tmp_path / "regroot",
        prompts={"T200@1.0.0": make_prompt_body()},
        focus_modules={"edm@1.0.0": make_focus_body()},
    )
    registry = load_registry(registry_root / "registry.yaml")
    project = tmp_path / "AI_NCM_Review"
    init_project(project, project_id="AI_NCM_2026", working_topic="topic")
    brief = write_versioned_artifact(project / "project", "project_brief", "# Brief\n")
    protocol = write_versioned_artifact(project / "project", "protocol", "# Protocol\n")
    result = compile_prompt(
        registry=registry,
        registry_root=registry_root,
        prompt_ref="T200@1.0.0",
        compiled_prompt_id="DR002",
        inputs={"project_brief": brief, "protocol": protocol},
        focus_modules=["edm@1.0.0"],
    )
    md_path, manifest_path = write_compiled(result, project / "deep_research" / "compiled")
    raw = project / "deep_research" / "raw" / "DR002_S03.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("# Raw Deep Research report\n\nUnchanged operator output.\n", encoding="utf-8")
    return project, md_path, raw


def test_register_run_creates_immutable_provenance_triple(tmp_path: Path) -> None:
    project, md_path, raw = _project_with_compiled_prompt(tmp_path)
    manifest = register_run(
        project_root=project,
        run_id="DR002",
        compiled_prompt_path=md_path,
        raw_output_path=raw,
        operator="operator-1",
        provider="ChatGPT",
        mode="Deep Research",
    )
    run_dir = project / "deep_research" / "runs" / "DR002"
    stored_prompt = run_dir / "compiled_prompt.md"
    stored_raw = run_dir / "raw_output.md"
    run_json = run_dir / "run.json"
    assert stored_prompt.is_file() and stored_raw.is_file() and run_json.is_file()
    assert stored_prompt.read_bytes() == md_path.read_bytes()
    assert stored_raw.read_bytes() == raw.read_bytes()
    assert manifest.compiled_prompt_sha256 == hash_file(md_path)
    assert manifest.raw_output_sha256 == hash_file(raw)
    assert manifest.manifest_sha256.startswith("sha256:")
    assert manifest.manual_ui_boundary is True
    recorded = json.loads(run_json.read_text(encoding="utf-8"))
    assert recorded["format_version"] == "vibereview-deep-research-run-1"
    assert recorded["run_id"] == "DR002"
    assert recorded["provider"] == "ChatGPT"
    assert recorded["mode"] == "Deep Research"


def test_unknown_provider_metadata_are_explicit(tmp_path: Path) -> None:
    project, md_path, raw = _project_with_compiled_prompt(tmp_path)
    manifest = register_run(
        project_root=project,
        run_id="DR003",
        compiled_prompt_path=md_path,
        raw_output_path=raw,
        operator="operator-1",
    )
    assert manifest.model == "unknown"
    assert manifest.model_version == "unknown"
    assert manifest.started_at == "unknown"
    assert manifest.completed_at == "unknown"
    assert manifest.provider == "unknown"


def test_existing_run_cannot_be_overwritten(tmp_path: Path) -> None:
    project, md_path, raw = _project_with_compiled_prompt(tmp_path)
    register_run(
        project_root=project,
        run_id="DR002",
        compiled_prompt_path=md_path,
        raw_output_path=raw,
        operator="operator-1",
    )
    with pytest.raises(PromptingError) as excinfo:
        register_run(
            project_root=project,
            run_id="DR002",
            compiled_prompt_path=md_path,
            raw_output_path=raw,
            operator="operator-1",
        )
    assert excinfo.value.code == "RUN_EXISTS"


def test_manual_prompt_modification_fails_registration(tmp_path: Path) -> None:
    project, md_path, raw = _project_with_compiled_prompt(tmp_path)
    md_path.write_text(md_path.read_text(encoding="utf-8") + "\noperator edit\n", encoding="utf-8")
    with pytest.raises(PromptingError) as excinfo:
        register_run(
            project_root=project,
            run_id="DR002",
            compiled_prompt_path=md_path,
            raw_output_path=raw,
            operator="operator-1",
        )
    assert excinfo.value.code == "COMPILED_PROMPT_TAMPERED"


def test_changed_input_since_compilation_fails_registration(tmp_path: Path) -> None:
    project, md_path, raw = _project_with_compiled_prompt(tmp_path)
    brief = project / "project" / "project_brief_v001.md"
    brief.write_text("# Brief\nedited after compilation\n", encoding="utf-8")
    with pytest.raises(PromptingError) as excinfo:
        register_run(
            project_root=project,
            run_id="DR002",
            compiled_prompt_path=md_path,
            raw_output_path=raw,
            operator="operator-1",
        )
    assert excinfo.value.code == "INPUT_HASH_CHANGED"


def test_missing_manifest_and_bad_raw_fail_closed(tmp_path: Path) -> None:
    project, md_path, raw = _project_with_compiled_prompt(tmp_path)
    md_path.with_suffix(".manifest.json").unlink()
    with pytest.raises(PromptingError) as excinfo:
        register_run(
            project_root=project,
            run_id="DR002",
            compiled_prompt_path=md_path,
            raw_output_path=raw,
            operator="operator-1",
        )
    assert excinfo.value.code == "MANIFEST_NOT_FOUND"

    registry_root = build_registry_root(
        tmp_path / "regroot2", prompts={"T200@1.0.0": make_prompt_body()}
    )
    registry = load_registry(registry_root / "registry.yaml")
    result = compile_prompt(
        registry=registry,
        registry_root=registry_root,
        prompt_ref="T200@1.0.0",
        compiled_prompt_id="DR004",
        inputs={},
    )
    md2, _ = write_compiled(result, project / "deep_research" / "compiled")
    empty_raw = project / "deep_research" / "raw" / "DR004.md"
    empty_raw.write_text("", encoding="utf-8")
    with pytest.raises(PromptingError) as excinfo:
        register_run(
            project_root=project,
            run_id="DR004",
            compiled_prompt_path=md2,
            raw_output_path=empty_raw,
            operator="operator-1",
        )
    assert excinfo.value.code == "RAW_OUTPUT_INVALID"


def test_operator_is_required_and_run_id_is_validated(tmp_path: Path) -> None:
    project, md_path, raw = _project_with_compiled_prompt(tmp_path)
    with pytest.raises(PromptingError) as excinfo:
        register_run(
            project_root=project,
            run_id="DR002",
            compiled_prompt_path=md_path,
            raw_output_path=raw,
            operator="",
        )
    assert excinfo.value.code == "OPERATOR_REQUIRED"
    with pytest.raises(PromptingError) as excinfo:
        register_run(
            project_root=project,
            run_id="not a run id",
            compiled_prompt_path=md_path,
            raw_output_path=raw,
            operator="operator-1",
        )
    assert excinfo.value.code == "RUN_ID_INVALID"
