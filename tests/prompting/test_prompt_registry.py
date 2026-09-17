"""P1 registry tests (goal.md §50 registry tests)."""

from __future__ import annotations

from pathlib import Path

import pytest

from vibereview.prompting import PROMPT_ROOT
from vibereview.prompting.models import PromptingError
from vibereview.prompting.registry import (
    load_registry,
    resolve_focus_module,
    resolve_prompt,
    verify_registry,
)

from prompt_registry_fixtures import build_registry_root, make_focus_body, make_prompt_body


def test_released_prompt_resolves_and_hash_matches(registry_root: Path) -> None:
    registry = load_registry(registry_root / "registry.yaml")
    resolved = resolve_prompt(registry, registry_root, "T100@1.0.0")
    assert "## Output Contract" in resolved.bytes.decode("utf-8")
    assert resolved.body.rstrip().endswith("2. Report results.")
    assert resolved.output_contract.startswith("## Output Contract")
    assert verify_registry(registry_root / "registry.yaml").ok


def test_changed_released_prompt_fails_verification_and_resolution(
    registry_root: Path,
) -> None:
    prompt_path = registry_root / "project" / "T100" / "1.0.0.md"
    prompt_path.write_bytes(prompt_path.read_bytes() + b"\nmanual edit\n")
    report = verify_registry(registry_root / "registry.yaml")
    assert not report.ok
    assert any(issue.code == "PROMPT_HASH_MISMATCH" for issue in report.issues)
    registry = load_registry(registry_root / "registry.yaml")
    with pytest.raises(PromptingError) as excinfo:
        resolve_prompt(registry, registry_root, "T100@1.0.0")
    assert excinfo.value.code == "PROMPT_HASH_MISMATCH"


def test_missing_prompt_file_fails_closed(registry_root: Path) -> None:
    (registry_root / "project" / "T100" / "1.0.0.md").unlink()
    report = verify_registry(registry_root / "registry.yaml")
    assert any(issue.code == "PROMPT_FILE_MISSING" for issue in report.issues)
    registry = load_registry(registry_root / "registry.yaml")
    with pytest.raises(PromptingError) as excinfo:
        resolve_prompt(registry, registry_root, "T100@1.0.0")
    assert excinfo.value.code == "PROMPT_FILE_MISSING"


def test_duplicate_prompt_version_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "regroot"
    body = make_prompt_body()
    build_registry_root(root, prompts={"T100@1.0.0": body})
    text = (root / "registry.yaml").read_text(encoding="utf-8")
    (root / "registry.yaml").write_text(
        text + "\n" + text.split("prompts:", 1)[1].replace(
            "T100@1.0.0:", "T100@1.0.0:"
        ),
        encoding="utf-8",
    )
    with pytest.raises(PromptingError) as excinfo:
        load_registry(root / "registry.yaml")
    assert excinfo.value.code in {"DUPLICATE_PROMPT_REF", "REGISTRY_PARSE_ERROR"}


def test_deprecated_prompt_remains_resolvable_and_replayable(
    tmp_path: Path,
) -> None:
    root = build_registry_root(tmp_path / "regroot", prompts={"T100@1.0.0": make_prompt_body()})
    import yaml

    registry_path = root / "registry.yaml"
    data = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    data["prompts"]["T100@1.0.0"]["status"] = "DEPRECATED"
    registry_path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
    registry = load_registry(registry_path)
    resolved = resolve_prompt(registry, root, "T100@1.0.0")
    assert resolved.entry.status == "DEPRECATED"
    assert verify_registry(registry_path).ok


def test_draft_prompt_is_rejected_for_compilation(tmp_path: Path) -> None:
    root = build_registry_root(tmp_path / "regroot", prompts={"T100@1.0.0": make_prompt_body()})
    import yaml

    registry_path = root / "registry.yaml"
    data = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    data["prompts"]["T100@1.0.0"]["status"] = "DRAFT"
    registry_path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
    registry = load_registry(registry_path)
    with pytest.raises(PromptingError) as excinfo:
        resolve_prompt(registry, root, "T100@1.0.0")
    assert excinfo.value.code == "PROMPT_NOT_RELEASED"


def test_unknown_prompt_and_unknown_version_fail_closed(registry_root: Path) -> None:
    registry = load_registry(registry_root / "registry.yaml")
    with pytest.raises(PromptingError) as excinfo:
        resolve_prompt(registry, registry_root, "T999@1.0.0")
    assert excinfo.value.code == "PROMPT_NOT_FOUND"
    with pytest.raises(PromptingError) as excinfo:
        resolve_prompt(registry, registry_root, "T100@9.9.9")
    assert excinfo.value.code == "PROMPT_NOT_FOUND"
    with pytest.raises(PromptingError) as excinfo:
        resolve_prompt(registry, registry_root, "not-a-ref")
    assert excinfo.value.code == "INVALID_PROMPT_REF"


def test_registry_key_must_match_entry_identity(tmp_path: Path) -> None:
    root = build_registry_root(tmp_path / "regroot", prompts={"T100@1.0.0": make_prompt_body()})
    import yaml

    registry_path = root / "registry.yaml"
    data = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    entry = data["prompts"].pop("T100@1.0.0")
    data["prompts"]["T100@2.0.0"] = entry
    registry_path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
    with pytest.raises(PromptingError) as excinfo:
        load_registry(registry_path)
    assert excinfo.value.code == "INVALID_REGISTRY_ENTRY"


def test_protocol_pin_must_resolve_to_released_prompt(tmp_path: Path) -> None:
    root = build_registry_root(
        tmp_path / "regroot",
        prompts={"T100@1.0.0": make_prompt_body()},
        protocols={
            "test-protocol-1": {
                "status": "released",
                "prompts": {"project_brief": "T999@1.0.0"},
            }
        },
    )
    report = verify_registry(root / "registry.yaml")
    assert any(issue.code == "PROTOCOL_PIN_UNRESOLVED" for issue in report.issues)


def test_focus_module_resolution_and_hash_check(
    tmp_path: Path,
) -> None:
    root = build_registry_root(
        tmp_path / "regroot",
        prompts={"T100@1.0.0": make_prompt_body()},
        focus_modules={"edm@1.0.0": make_focus_body()},
    )
    registry = load_registry(root / "registry.yaml")
    resolved = resolve_focus_module(registry, root, "edm@1.0.0")
    assert "Domain emphasis" in resolved.bytes.decode("utf-8")
    (root / "focus" / "edm" / "1.0.0.md").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(PromptingError) as excinfo:
        resolve_focus_module(registry, root, "edm@1.0.0")
    assert excinfo.value.code == "PROMPT_HASH_MISMATCH"


def test_missing_registry_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(PromptingError) as excinfo:
        load_registry(tmp_path / "nope" / "registry.yaml")
    assert excinfo.value.code == "REGISTRY_NOT_FOUND"


def test_committed_prompt_library_verifies() -> None:
    """The real released library under src/vibereview/prompts must verify."""
    registry_path = PROMPT_ROOT / "registry.yaml"
    assert registry_path.is_file()
    report = verify_registry(registry_path)
    assert report.ok, [f"{i.path}: {i.code}" for i in report.issues]
    registry = load_registry(registry_path)
    assert len(registry.prompts) == 20
    assert len(registry.focus_modules) == 10
