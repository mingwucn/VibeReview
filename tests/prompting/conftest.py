"""Shared fixtures for the prompt-infrastructure tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml


def sha_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha_file(path: Path) -> str:
    return sha_bytes(path.read_bytes())


PROMPT_TEMPLATE = (
    "# {ref} — {title}\n\n"
    "## Purpose\n\n{purpose}\n\n"
    "## Procedure\n\n{procedure}\n\n"
    "## Output Contract\n\n```text\n{contract}\n```\n"
)

FOCUS_TEMPLATE = (
    "# focus: {ref}\n\n## Domain emphasis\n\n{body}\n\n"
    "## Terminology pointers\n\n- example term\n\n"
    "## Boundaries\n\nNo project-specific conclusions.\n"
)


def build_registry_root(
    root: Path,
    *,
    prompts: dict[str, dict[str, str]],
    focus_modules: dict[str, dict[str, str]] | None = None,
    protocols: dict[str, dict] | None = None,
) -> Path:
    """Write a synthetic registry tree; values are file bodies by ref."""

    root.mkdir(parents=True, exist_ok=True)
    classes = {
        "T100": "project",
        "T200": "deep_research",
        "T300": "claims",
        "T400": "synthesis",
        "T500": "manuscript",
    }
    entries: dict[str, dict] = {}
    for ref, body in prompts.items():
        prompt_id, version = ref.split("@")
        if prompt_id.startswith("T"):
            rel = f"{classes[prompt_id]}/{prompt_id}/{version}.md"
        else:
            rel = f"project/{prompt_id}/{version}.md"
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body.encode("utf-8"))
        entries[ref] = {
            "class": classes.get(prompt_id, "project"),
            "path": rel,
            "sha256": sha_file(path),
            "status": "RELEASED",
            "scientific_role": "test prompt",
            "canonical_evidence": False,
            "release_date": "2026-09-17",
            "change_note": "Initial release.",
        }
    focus_entries: dict[str, dict] = {}
    for ref, body in (focus_modules or {}).items():
        name, version = ref.split("@")
        rel = f"focus/{name}/{version}.md"
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body.encode("utf-8"))
        focus_entries[ref] = {
            "path": rel,
            "sha256": sha_file(path),
            "status": "RELEASED",
            "release_date": "2026-09-17",
            "change_note": "Initial release.",
        }
    registry = {
        "registry_version": 1,
        "protocols": protocols or {},
        "prompts": entries,
        "focus_modules": focus_entries,
    }
    (root / "registry.yaml").write_text(
        yaml.safe_dump(registry, sort_keys=True), encoding="utf-8"
    )
    return root


def make_prompt_body(title: str = "Test Prompt") -> str:
    return PROMPT_TEMPLATE.format(
        ref="T100@1.0.0",
        title=title,
        purpose="Test purpose.",
        procedure="1. Follow the components.\n2. Report results.",
        contract="result: structured text",
    )


def make_focus_body() -> str:
    return FOCUS_TEMPLATE.format(ref="edm@1.0.0", body="Domain emphasis text.")


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
