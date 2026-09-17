"""Shared fixtures for the prompt-infrastructure tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from prompt_registry_fixtures import (
    build_registry_root,
    make_focus_body,
    make_prompt_body,
)

__all__ = ["build_registry_root", "make_focus_body", "make_prompt_body", "registry_root", "project_files"]


@pytest.fixture
def registry_root(tmp_path: Path) -> Path:
    return build_registry_root(
        tmp_path / "regroot",
        prompts={"T100@1.0.0": make_prompt_body()},
    )


@pytest.fixture
def project_files(tmp_path: Path) -> dict[str, Path]:
    project = tmp_path / "project"
    files = {
        "project_brief": "# Project Brief\n\nTopic: widgets.\n",
        "protocol": "# Protocol\n\nCritical narrative.\n",
        "outline": (
            "# Outline v001\n\n"
            "# S03 Widgets\n\nIntro paragraph.\n\n"
            "## S03.1 Prediction\n\nPrediction content.\n\n"
            "## S03.2 Optimization\n\nOptimization content.\n\n"
            "# S04 Gadgets\n\nGadget content.\n"
        ),
    }
    paths: dict[str, Path] = {}
    for role, text in files.items():
        path = project / f"{role}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        paths[role] = path
    return paths
