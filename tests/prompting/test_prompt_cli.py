"""CLI tests for the prompt-infrastructure commands (goal.md §49 subset)."""

from __future__ import annotations

from pathlib import Path

import pytest

from vibereview.prompting.cli import main as cli_main
from vibereview.prompting.compiler import compile_prompt, write_compiled
from vibereview.prompting.project import init_project, write_versioned_artifact
from vibereview.prompting.registry import load_registry

from prompt_registry_fixtures import build_registry_root, make_focus_body, make_prompt_body


@pytest.fixture
def registry_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    regroot = build_registry_root(
        tmp_path / "regroot",
        prompts={"T200@1.0.0": make_prompt_body()},
        focus_modules={"edm@1.0.0": make_focus_body()},
        protocols={
            "test-protocol-1": {
                "status": "released",
                "prompts": {"section_research": "T200@1.0.0"},
            }
        },
    )
    monkeypatch.setenv("VIBEREVIEW_PROMPT_ROOT", str(regroot))
    project = tmp_path / "AI_NCM_Review"
    init_project(project, project_id="AI_NCM_2026", working_topic="topic")
    write_versioned_artifact(project / "project", "project_brief", "# Brief\n")
    write_versioned_artifact(project / "project", "protocol", "# Protocol\n")
    write_versioned_artifact(
        project / "outline",
        "outline",
        "# Outline\n\n# S03 EDM\n\n## S03.1 prediction\n\nBody.\n",
    )
    return project, regroot


def _registry(regroot: Path):
    return load_registry(regroot / "registry.yaml")


def _compile(project: Path, regroot: Path) -> Path:
    registry = _registry(regroot)
    result = compile_prompt(
        registry=registry,
        registry_root=regroot,
        prompt_ref="T200@1.0.0",
        compiled_prompt_id="DR002_S03",
        inputs={
            "project_brief": project / "project" / "project_brief_v001.md",
            "protocol": project / "project" / "protocol_v001.md",
            "outline": project / "outline" / "outline_v001.md",
        },
        focus_modules=["edm@1.0.0"],
        section_id="S03.1",
    )
    md_path, _ = write_compiled(result, project / "deep_research" / "compiled")
    return md_path


def test_prompts_list_verify_and_show(registry_env, capsys: pytest.CaptureFixture) -> None:
    project, regroot = registry_env
    assert cli_main(["prompts", "list"]) == 0
    out = capsys.readouterr().out
    assert "T200@1.0.0" in out and "RELEASED" in out
    assert cli_main(["prompts", "show", "T200@1.0.0"]) == 0
    out = capsys.readouterr().out
    assert "## Output Contract" in out
    assert cli_main(["prompts", "verify"]) == 0
    assert "ok" in capsys.readouterr().out
    assert cli_main(["prompts", "status"]) == 0
    out = capsys.readouterr().out
    assert "test-protocol-1" in out


def test_prompts_verify_detects_tampering(registry_env, capsys: pytest.CaptureFixture) -> None:
    project, regroot = registry_env
    prompt = regroot / "deep_research" / "T200" / "1.0.0.md"
    prompt.write_text(prompt.read_text(encoding="utf-8") + "edit\n", encoding="utf-8")
    assert cli_main(["prompts", "verify"]) == 1
    captured = capsys.readouterr()
    assert "PROMPT_HASH_MISMATCH" in captured.out + captured.err


def test_prompts_compile_with_current_inputs(registry_env, capsys: pytest.CaptureFixture) -> None:
    project, regroot = registry_env
    rc = cli_main(
        [
            "prompts",
            "compile",
            "T200@1.0.0",
            "--project",
            str(project),
            "--id",
            "DR002_S03",
            "--input",
            "project_brief=@current",
            "--input",
            "protocol=@current",
            "--input",
            "outline=@current",
            "--focus",
            "edm@1.0.0",
            "--section",
            "S03.1",
        ]
    )
    assert rc == 0
    compiled = project / "deep_research" / "compiled" / "DR002_S03.md"
    manifest = project / "deep_research" / "compiled" / "DR002_S03.manifest.json"
    assert compiled.is_file() and manifest.is_file()
    assert 'role="outline-section"' in compiled.read_text(encoding="utf-8")


def test_project_init_and_status(registry_env, capsys: pytest.CaptureFixture) -> None:
    project, regroot = registry_env
    assert cli_main(["project", "status", "--project", str(project)]) == 0
    out = capsys.readouterr().out
    assert "AI_NCM_2026" in out
    new_root = project.parent / "Other_Review"
    assert cli_main(["project", "init", str(new_root), "--project-id", "X", "--topic", "t"]) == 0
    assert (new_root / "project.yaml").is_file()


def test_research_register_output(registry_env, capsys: pytest.CaptureFixture) -> None:
    project, regroot = registry_env
    compiled = _compile(project, regroot)
    raw = project / "deep_research" / "raw" / "DR002_S03.md"
    raw.write_text("# Raw report\n", encoding="utf-8")
    rc = cli_main(
        [
            "research",
            "register-output",
            "--project",
            str(project),
            "--run",
            "DR002",
            "--prompt",
            str(compiled),
            "--output",
            str(raw),
            "--operator",
            "operator-1",
            "--provider",
            "ChatGPT",
            "--mode",
            "Deep Research",
        ]
    )
    assert rc == 0
    run_json = project / "deep_research" / "runs" / "DR002" / "run.json"
    assert run_json.is_file()
    assert "unknown" in run_json.read_text(encoding="utf-8")


def test_research_register_output_requires_operator(registry_env) -> None:
    project, regroot = registry_env
    compiled = _compile(project, regroot)
    raw = project / "deep_research" / "raw" / "DR002_S03.md"
    raw.write_text("# Raw report\n", encoding="utf-8")
    rc = cli_main(
        [
            "research",
            "register-output",
            "--project",
            str(project),
            "--run",
            "DR002",
            "--prompt",
            str(compiled),
            "--output",
            str(raw),
        ]
    )
    assert rc == 1
