"""Review-project layout, append-only artifact versioning, and prompt profiles."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from .models import PromptingError

REQUIRED_DIRECTORIES: tuple[str, ...] = (
    "project",
    "outline",
    "supplements",
    "deep_research/compiled",
    "deep_research/runs",
    "deep_research/raw",
    "deep_research/parsed",
    "claims/candidate",
    "claims/normalized",
    "claims/synthesis",
    "corpus/selection",
    "corpus/source_quality",
    "corpus/aliases",
    "corpus/coverage",
    "benchmarks/retrieval",
    "benchmarks/semantic",
    "analysis/evidence",
    "analysis/claim_packets",
    "analysis/matrices",
    "analysis/audits",
    "manuscript/outline",
    "manuscript/section_contracts",
    "manuscript/propositions",
    "manuscript/sections",
    "run_manifests",
)

DEFAULT_PROTOCOL = "vibereview-review-protocol-1.0"

_FAMILY_PATTERN_CACHE: dict[tuple[str, str], re.Pattern[str]] = {}


def _family_pattern(family: str, extension: str) -> re.Pattern[str]:
    key = (family, extension)
    pattern = _FAMILY_PATTERN_CACHE.get(key)
    if pattern is None:
        pattern = re.compile(
            rf"^{re.escape(family)}_v([0-9]{{3}})\.{re.escape(extension)}$"
        )
        _FAMILY_PATTERN_CACHE[key] = pattern
    return pattern


def _list_ordinals(directory: Path, family: str, extension: str) -> list[int]:
    if not directory.is_dir():
        return []
    pattern = _family_pattern(family, extension)
    ordinals = [
        int(match.group(1))
        for entry in sorted(directory.iterdir())
        if entry.is_file() and (match := pattern.match(entry.name)) is not None
    ]
    return sorted(ordinals)


def list_versions(directory: Path, family: str, *, extension: str = "md") -> list[Path]:
    """Sorted versioned artifact paths ``<family>_vNNN.<extension>``."""

    directory = Path(directory)
    return [
        directory / f"{family}_v{ordinal:03d}.{extension}"
        for ordinal in _list_ordinals(directory, family, extension)
    ]


def resolve_current(directory: Path, family: str, *, extension: str = "md") -> Path | None:
    """The current pointer target, else the highest version, else ``None``."""

    directory = Path(directory)
    if family == "outline":
        pointer = directory / "current"
        if pointer.is_file():
            name = pointer.read_text(encoding="utf-8").strip()
            if name and _family_pattern(family, extension).match(name):
                candidate = directory / name
                if candidate.is_file():
                    return candidate
    versions = list_versions(directory, family, extension=extension)
    if versions:
        return versions[-1]
    return None


def _declared_ordinal(family: str, content: str) -> int | None:
    first_line = content.split("\n", 1)[0]
    family_pattern = re.escape(family).replace("_", "[_ ]")
    match = re.search(rf"{family_pattern}[ _]v([0-9]{{1,3}})\b", first_line, re.IGNORECASE)
    if match is None:
        match = re.search(r"_v([0-9]{3})\b", first_line)
    return int(match.group(1)) if match is not None else None


def write_versioned_artifact(
    directory: Path, family: str, content: str, *, extension: str = "md"
) -> Path:
    """Append one ``<family>_vNNN.<extension>`` version; never overwrite.

    The ordinal is declared by a ``<family> vN`` (or ``<family>_vNNN``) marker
    on the content's first line; without a marker the next gap-free ordinal is
    used. Existing ordinals must be consecutive from 001.
    """

    directory = Path(directory)
    ordinals = _list_ordinals(directory, family, extension)
    if ordinals != list(range(1, len(ordinals) + 1)):
        raise PromptingError(
            "ARTIFACT_ORDINAL_GAP",
            f"{directory}: {family}_v*.{extension} ordinals are not gap-free: "
            f"{ordinals}",
        )
    next_ordinal = ordinals[-1] + 1 if ordinals else 1
    ordinal = _declared_ordinal(family, content)
    if ordinal is None:
        ordinal = next_ordinal
    path = directory / f"{family}_v{ordinal:03d}.{extension}"
    if path.exists():
        raise PromptingError("ARTIFACT_EXISTS", f"{path} already exists")
    if ordinal != next_ordinal:
        raise PromptingError(
            "ARTIFACT_ORDINAL_GAP",
            f"{path}: only the next ordinal v{next_ordinal:03d} may be appended",
        )
    directory.mkdir(parents=True, exist_ok=True)
    text = content.replace("\r\n", "\n")
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")
    if family == "outline":
        (directory / "current").write_text(path.name, encoding="utf-8")
    return path


def _write_if_absent(path: Path, content: str) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


PACKAGE_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "review-project"

_TEMPLATE_TOKEN_PATTERN = re.compile(r"\{\{([A-Z_]+)\}\}")


def _flow_list(values: Sequence[str]) -> str:
    return json.dumps(list(values))


def render_project_template(
    template_dir: Path, values: Mapping[str, str]
) -> dict[str, str]:
    """Render every template file; fail closed on leftover placeholders."""

    template_dir = Path(template_dir)
    if not template_dir.is_dir():
        raise PromptingError(
            "TEMPLATE_NOT_FOUND", f"template directory {template_dir} does not exist"
        )
    rendered: dict[str, str] = {}
    for path in sorted(template_dir.rglob("*")):
        if not path.is_file() or path.name == "README.md":
            continue
        relative = path.relative_to(template_dir).as_posix()
        text = path.read_text(encoding="utf-8")
        text = _TEMPLATE_TOKEN_PATTERN.sub(
            lambda match: values.get(match.group(1), match.group(0)), text
        )
        leftover = sorted(set(_TEMPLATE_TOKEN_PATTERN.findall(text)))
        if leftover:
            raise PromptingError(
                "PLACEHOLDER_UNFILLED",
                f"template {relative} has unfilled placeholder(s): "
                + ", ".join(leftover),
            )
        rendered[relative] = text
    return rendered


def init_project(
    root: Path,
    *,
    project_id: str,
    working_topic: str,
    protocol: str = DEFAULT_PROTOCOL,
    core_processes: Sequence[str] = (),
    secondary_processes: Sequence[str] = (),
    future_outlook: Sequence[str] = (),
    cutoff: str | None = None,
    template_dir: Path | None = None,
) -> Path:
    """Create the goal.md §11 layout from the placeholder template."""

    root = Path(root)
    if (root / "project.yaml").exists():
        raise PromptingError(
            "ARTIFACT_EXISTS", f"{root / 'project.yaml'} already exists"
        )
    values = {
        "PROJECT_ID": project_id,
        "WORKING_TOPIC": working_topic,
        "REVIEW_PROTOCOL": protocol,
        "CORE_PROCESSES": _flow_list(core_processes),
        "SECONDARY_PROCESSES": _flow_list(secondary_processes),
        "FUTURE_OUTLOOK": _flow_list(future_outlook),
        "PUBLICATION_CUTOFF": json.dumps(cutoff),
    }
    rendered = render_project_template(template_dir or PACKAGE_TEMPLATE_DIR, values)
    try:
        config_check = yaml.safe_load(rendered["project.yaml"])
    except (yaml.YAMLError, KeyError) as exc:
        raise PromptingError(
            "TEMPLATE_INVALID", f"rendered project.yaml is unusable: {exc}"
        ) from exc
    if not isinstance(config_check, dict) or config_check.get("project_id") != project_id:
        raise PromptingError(
            "TEMPLATE_INVALID", "rendered project.yaml does not carry the requested project_id"
        )
    for relative in REQUIRED_DIRECTORIES:
        (root / relative).mkdir(parents=True, exist_ok=True)
    for relative, text in rendered.items():
        _write_if_absent(root / relative, text)
    return root


def _require_project(project_root: Path) -> Path:
    root = Path(project_root)
    if not (root / "project.yaml").is_file():
        raise PromptingError(
            "PROJECT_NOT_INITIALIZED", f"{root} has no project.yaml"
        )
    return root


def load_prompt_profile(project_root: Path) -> tuple[Path, dict]:
    """Return the current (highest-version) prompt profile path and content."""

    root = _require_project(project_root)
    profile_dir = root / "project"
    versions = list_versions(profile_dir, "prompt_profile", extension="yaml")
    if not versions:
        raise PromptingError(
            "PROFILE_NOT_FOUND", f"{profile_dir} has no prompt_profile_vNNN.yaml"
        )
    path = versions[-1]
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PromptingError(
            "PROFILE_NOT_FOUND", f"prompt profile at {path} is unparseable: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise PromptingError(
            "PROFILE_NOT_FOUND", f"prompt profile at {path} is not a mapping"
        )
    return path, data


def migrate_prompt_profile(
    project_root: Path,
    *,
    protocol: str | None = None,
    focus_modules: Mapping[str, Sequence[str]] | None = None,
    reason: str,
) -> Path:
    """Write the next prompt profile version; never edit an older one."""

    root = _require_project(project_root)
    path, data = load_prompt_profile(root)
    old_protocol = data.get("protocol")
    old_protocol_id = old_protocol.get("id") if isinstance(old_protocol, dict) else None
    old_modules = data.get("focus_modules")
    if not isinstance(old_modules, dict):
        old_modules = {}
    new_protocol_id = protocol if protocol is not None else old_protocol_id
    new_modules = (
        {section: list(refs) for section, refs in focus_modules.items()}
        if focus_modules is not None
        else dict(old_modules)
    )
    if new_protocol_id == old_protocol_id and new_modules == old_modules:
        raise PromptingError(
            "PROFILE_MIGRATION_UNCHANGED",
            f"current profile {path.name} already pins protocol "
            f"{new_protocol_id!r} with the same focus modules",
        )
    profile_version = data.get("prompt_profile_version", 1)
    migrated = {
        "prompt_profile_version": profile_version,
        "protocol": {"id": new_protocol_id},
        "focus_modules": new_modules,
    }
    text = yaml.safe_dump(migrated, sort_keys=False, allow_unicode=True)
    if reason:
        text = f"# migration reason: {reason}\n" + text
    return write_versioned_artifact(
        root / "project", "prompt_profile", text, extension="yaml"
    )


@dataclass(frozen=True, slots=True)
class ProjectStatus:
    project_id: str
    protocol: str
    artifact_versions: dict[str, tuple[Path, ...]]
    registered_runs: tuple[str, ...]
    compiled_prompts: tuple[str, ...]


def _load_project_yaml(root: Path) -> dict[str, Any]:
    path = root / "project.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PromptingError(
            "PROJECT_NOT_INITIALIZED", f"{path} is unparseable: {exc}"
        ) from exc
    return data if isinstance(data, dict) else {}


def project_status(project_root: Path) -> ProjectStatus:
    """Summarize project identity, artifact versions, runs, and compiles."""

    root = _require_project(project_root)
    config = _load_project_yaml(root)
    artifact_versions: dict[str, tuple[Path, ...]] = {
        "project_brief": tuple(list_versions(root / "project", "project_brief")),
        "protocol": tuple(list_versions(root / "project", "protocol")),
        "structure": tuple(list_versions(root / "project", "structure")),
        "outline": tuple(list_versions(root / "outline", "outline")),
        "prompt_profile": tuple(
            list_versions(root / "project", "prompt_profile", extension="yaml")
        ),
    }
    runs_root = root / "deep_research" / "runs"
    registered_runs = (
        tuple(sorted(entry.name for entry in runs_root.iterdir() if entry.is_dir()))
        if runs_root.is_dir()
        else ()
    )
    compiled_root = root / "deep_research" / "compiled"
    compiled = (
        tuple(sorted(path.name for path in compiled_root.glob("*.md") if path.is_file()))
        if compiled_root.is_dir()
        else ()
    )
    review_protocol = config.get("review_protocol")
    protocol = (
        review_protocol.get("id", "")
        if isinstance(review_protocol, dict)
        else ""
    )
    return ProjectStatus(
        project_id=str(config.get("project_id", "")),
        protocol=protocol,
        artifact_versions=artifact_versions,
        registered_runs=registered_runs,
        compiled_prompts=compiled,
    )
