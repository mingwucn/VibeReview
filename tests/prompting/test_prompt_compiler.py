"""P2 deterministic compilation tests (goal.md §50 compilation tests)."""

from __future__ import annotations

from pathlib import Path

import pytest

from vibereview.prompting import COMPILER_VERSION, PROMPT_ROOT
from vibereview.prompting.compiler import (
    CANONICAL_ROLE_ORDER,
    compile_prompt,
    extract_outline_section,
    load_manifest,
    revalidate_manifest_inputs,
    validate_compiled_against_manifest,
    write_compiled,
)
from vibereview.prompting.models import PromptingError
from vibereview.prompting.registry import load_registry
from vibereview.runtime.hashing import hash_text

from prompt_registry_fixtures import build_registry_root, make_focus_body, make_prompt_body


def _registry(tmp_path: Path):
    root = build_registry_root(
        tmp_path / "regroot",
        prompts={"T200@1.0.0": make_prompt_body()},
        focus_modules={"edm@1.0.0": make_focus_body(), "ecm@1.0.0": make_focus_body()},
    )
    return load_registry(root / "registry.yaml"), root


def test_same_inputs_produce_byte_identical_prompts(
    tmp_path: Path, project_files: dict[str, Path]
) -> None:
    registry, root = _registry(tmp_path)
    first = compile_prompt(
        registry=registry,
        registry_root=root,
        prompt_ref="T200@1.0.0",
        compiled_prompt_id="T001",
        inputs=project_files,
        focus_modules=["edm@1.0.0"],
        section_id="S03.2",
    )
    second = compile_prompt(
        registry=registry,
        registry_root=root,
        prompt_ref="T200@1.0.0",
        compiled_prompt_id="T001",
        inputs=project_files,
        focus_modules=["edm@1.0.0"],
        section_id="S03.2",
    )
    assert first.compiled_text == second.compiled_text
    assert first.manifest.compiled_sha256 == second.manifest.compiled_sha256
    assert first.manifest.compiled_sha256 == hash_text(first.compiled_text)


def test_input_ordering_is_canonical_not_caller_order(
    tmp_path: Path, project_files: dict[str, Path]
) -> None:
    registry, root = _registry(tmp_path)
    reversed_inputs = dict(reversed(list(project_files.items())))
    baseline = compile_prompt(
        registry=registry,
        registry_root=root,
        prompt_ref="T200@1.0.0",
        compiled_prompt_id="T001",
        inputs=project_files,
    )
    shuffled = compile_prompt(
        registry=registry,
        registry_root=root,
        prompt_ref="T200@1.0.0",
        compiled_prompt_id="T001",
        inputs=reversed_inputs,
    )
    assert baseline.compiled_text == shuffled.compiled_text
    roles = [entry.role for entry in shuffled.manifest.inputs]
    assert roles == sorted(roles, key=CANONICAL_ROLE_ORDER.index)


def test_project_config_role_sorts_before_project_brief(
    tmp_path: Path, project_files: dict[str, Path]
) -> None:
    registry, root = _registry(tmp_path)
    config = project_files["project_brief"].parent / "project.yaml"
    config.write_text("project_id: X\nworking_topic: widgets\n", encoding="utf-8")
    result = compile_prompt(
        registry=registry,
        registry_root=root,
        prompt_ref="T200@1.0.0",
        compiled_prompt_id="T001",
        inputs={"project_brief": project_files["project_brief"], "project_config": config},
    )
    roles = [entry.role for entry in result.manifest.inputs]
    assert roles == ["project_config", "project_brief"]
    assert 'role="project-config"' in result.compiled_text


def test_focus_module_order_is_declared_order(tmp_path: Path) -> None:
    registry, root = _registry(tmp_path)
    forward = compile_prompt(
        registry=registry,
        registry_root=root,
        prompt_ref="T200@1.0.0",
        compiled_prompt_id="T001",
        focus_modules=["edm@1.0.0", "ecm@1.0.0"],
    )
    backward = compile_prompt(
        registry=registry,
        registry_root=root,
        prompt_ref="T200@1.0.0",
        compiled_prompt_id="T001",
        focus_modules=["ecm@1.0.0", "edm@1.0.0"],
    )
    assert forward.compiled_text != backward.compiled_text
    assert [m.id for m in forward.manifest.modules] == ["edm", "ecm"]
    assert [m.id for m in backward.manifest.modules] == ["ecm", "edm"]


def test_exact_section_extraction_is_deterministic(project_files: dict[str, Path]) -> None:
    outline = project_files["outline"].read_text(encoding="utf-8")
    section = extract_outline_section(outline, "S03.2")
    assert section.startswith("## S03.2 Optimization")
    assert "Optimization content." in section
    assert "Gadget content." not in section
    assert "Prediction content." not in section
    parent = extract_outline_section(outline, "S03")
    assert "Prediction content." in parent
    assert "Gadget content." not in parent
    with pytest.raises(PromptingError) as excinfo:
        extract_outline_section(outline, "S09")
    assert excinfo.value.code == "SECTION_NOT_FOUND"


def test_frozen_component_order(tmp_path: Path, project_files: dict[str, Path]) -> None:
    registry, root = _registry(tmp_path)
    supplement = project_files["project_brief"].parent / "supplement.md"
    supplement.write_text("# Supplement\nExtra instruction.\n", encoding="utf-8")
    result = compile_prompt(
        registry=registry,
        registry_root=root,
        prompt_ref="T200@1.0.0",
        compiled_prompt_id="T001",
        inputs=project_files,
        focus_modules=["edm@1.0.0"],
        section_id="S03.2",
        supplement=supplement,
    )
    text = result.compiled_text
    markers = [
        'role="core-prompt"',
        'role="project-brief"',
        'role="protocol"',
        'role="outline-section"',
        'role="focus-module"',
        'role="supplement"',
        'role="output-contract"',
    ]
    positions = [text.index(marker) for marker in markers]
    assert positions == sorted(positions)
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_supplement_is_represented_in_manifest(
    tmp_path: Path, project_files: dict[str, Path]
) -> None:
    registry, root = _registry(tmp_path)
    supplement = project_files["outline"].parent / "supplements" / "S03.2_v001.md"
    supplement.parent.mkdir(parents=True)
    supplement.write_text("# Supplement\n", encoding="utf-8")
    result = compile_prompt(
        registry=registry,
        registry_root=root,
        prompt_ref="T200@1.0.0",
        compiled_prompt_id="T001",
        inputs=project_files,
        supplement=supplement,
    )
    assert result.manifest.supplement is not None
    assert result.manifest.supplement.sha256 == hash_text("# Supplement\n")


def test_unknown_and_unreleased_prompt_fails_closed(
    tmp_path: Path, project_files: dict[str, Path]
) -> None:
    registry, root = _registry(tmp_path)
    with pytest.raises(PromptingError) as excinfo:
        compile_prompt(
            registry=registry,
            registry_root=root,
            prompt_ref="T999@1.0.0",
            compiled_prompt_id="T001",
            inputs=project_files,
        )
    assert excinfo.value.code == "PROMPT_NOT_FOUND"


def test_unknown_input_role_fails_closed(tmp_path: Path, project_files: dict[str, Path]) -> None:
    registry, root = _registry(tmp_path)
    inputs = dict(project_files)
    inputs["bogus_role"] = project_files["protocol"]
    with pytest.raises(PromptingError) as excinfo:
        compile_prompt(
            registry=registry,
            registry_root=root,
            prompt_ref="T200@1.0.0",
            compiled_prompt_id="T001",
            inputs=inputs,
        )
    assert excinfo.value.code == "UNKNOWN_INPUT_ROLE"


def test_write_compiled_roundtrip_and_tamper_detection(
    tmp_path: Path, project_files: dict[str, Path]
) -> None:
    registry, root = _registry(tmp_path)
    result = compile_prompt(
        registry=registry,
        registry_root=root,
        prompt_ref="T200@1.0.0",
        compiled_prompt_id="T001",
        inputs=project_files,
        section_id="S03.1",
    )
    out_dir = tmp_path / "compiled"
    md_path, manifest_path = write_compiled(result, out_dir)
    assert md_path.name == "T001.md"
    assert manifest_path.name == "T001.manifest.json"
    manifest = load_manifest(manifest_path)
    validate_compiled_against_manifest(manifest, md_path)
    # Byte-identical recompilation is idempotent.
    write_compiled(result, out_dir)
    assert md_path.read_text(encoding="utf-8") == result.compiled_text
    md_path.write_text(md_path.read_text(encoding="utf-8") + "manual edit\n", encoding="utf-8")
    with pytest.raises(PromptingError) as excinfo:
        validate_compiled_against_manifest(manifest, md_path)
    assert excinfo.value.code == "COMPILED_PROMPT_TAMPERED"


def test_post_compilation_input_drift_fails_closed(
    tmp_path: Path, project_files: dict[str, Path]
) -> None:
    registry, root = _registry(tmp_path)
    result = compile_prompt(
        registry=registry,
        registry_root=root,
        prompt_ref="T200@1.0.0",
        compiled_prompt_id="T001",
        inputs=project_files,
    )
    project_files["protocol"].write_text("# Protocol\nchanged after compile\n", encoding="utf-8")
    with pytest.raises(PromptingError) as excinfo:
        revalidate_manifest_inputs(result.manifest, tmp_path / "project")
    assert excinfo.value.code == "INPUT_HASH_CHANGED"


def test_manifest_records_compiler_version_and_no_timestamps(
    tmp_path: Path, project_files: dict[str, Path]
) -> None:
    registry, root = _registry(tmp_path)
    result = compile_prompt(
        registry=registry,
        registry_root=root,
        prompt_ref="T200@1.0.0",
        compiled_prompt_id="T001",
        inputs=project_files,
    )
    assert result.manifest.compiler_version == COMPILER_VERSION
    assert result.manifest.format_version == "vibereview-compiled-prompt-manifest-1"
    dumped = result.manifest.model_dump(mode="json")
    assert not any("time" in key or "date" in key for key in dumped)


def test_compiled_manifest_matches_goal_contract(
    tmp_path: Path, project_files: dict[str, Path]
) -> None:
    registry, root = _registry(tmp_path)
    result = compile_prompt(
        registry=registry,
        registry_root=root,
        prompt_ref="T200@1.0.0",
        compiled_prompt_id="DR007",
        inputs=project_files,
        focus_modules=["edm@1.0.0"],
        section_id="S03.2",
    )
    manifest = result.manifest.model_dump(mode="json")
    assert manifest["compiled_prompt_id"] == "DR007"
    assert manifest["prompt"]["id"] == "T200"
    assert manifest["prompt"]["version"] == "1.0.0"
    assert manifest["prompt"]["sha256"].startswith("sha256:")
    assert manifest["modules"][0]["id"] == "edm"
    assert manifest["section_id"] == "S03.2"
    assert manifest["supplement"] is None
    assert {entry["role"] for entry in manifest["inputs"]} == set(project_files)


def test_committed_library_compiles_end_to_end(tmp_path: Path) -> None:
    """Compile DR110@1.0.0 from the committed registry against a synthetic outline."""
    registry = load_registry(PROMPT_ROOT / "registry.yaml")
    outline = tmp_path / "outline_v001.md"
    outline.write_text(
        "# Outline\n\n# S03 EDM\n\n## S03.1 prediction\n\nBody.\n\n# S04 ECM\n\nOther.\n",
        encoding="utf-8",
    )
    brief = tmp_path / "project_brief_v001.md"
    brief.write_text("# Brief\n\nTopic.\n", encoding="utf-8")
    protocol = tmp_path / "protocol_v001.md"
    protocol.write_text("# Protocol\n\nRules.\n", encoding="utf-8")
    result = compile_prompt(
        registry=registry,
        registry_root=PROMPT_ROOT,
        prompt_ref="DR110@1.0.0",
        compiled_prompt_id="DR002_S03",
        inputs={"project_brief": brief, "protocol": protocol, "outline": outline},
        focus_modules=["edm@1.0.0"],
        section_id="S03.1",
    )
    text = result.compiled_text
    for phrase in (
        "null findings",
        "contradictions",
        "boundary conditions",
        "generalization",
    ):
        assert phrase in text
    assert 'role="outline-section"' in text
    assert text.count("outline_v001.md#S03.1") == 1
