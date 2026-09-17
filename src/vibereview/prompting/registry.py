"""Immutable prompt-registry loading, verification, and fail-closed resolution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from vibereview.runtime.hashing import hash_file

from .models import (
    FocusModuleEntry,
    PromptRegistry,
    PromptRegistryEntry,
    PromptStatus,
    PromptingError,
    RegistryIssue,
    ResolvedPrompt,
    VerificationReport,
    parse_prompt_ref,
)

__all__ = [
    "load_registry",
    "verify_registry",
    "resolve_prompt",
    "resolve_focus_module",
    "parse_prompt_ref",
]


def _load_yaml_strict(path: Path) -> Any:
    """Parse YAML, failing closed on duplicate mapping keys."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptingError("REGISTRY_PARSE_ERROR", f"{path}: {exc}") from exc

    class _StrictLoader(yaml.SafeLoader):
        pass

    def _construct_mapping(loader: yaml.SafeLoader, node: yaml.Node, deep: bool = False) -> dict:
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=True)
            if key in mapping:
                raise yaml.constructor.ConstructorError(
                    "mapping",
                    node.start_mark,
                    f"duplicate key {key!r}",
                    key_node.start_mark,
                )
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    _StrictLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
    )
    try:
        return yaml.load(text, Loader=_StrictLoader)
    except yaml.YAMLError as exc:
        raise PromptingError("REGISTRY_PARSE_ERROR", f"{path}: {exc}") from exc


def _default_registry_path(path: Path | None) -> Path:
    if path is not None:
        return Path(path)
    from . import get_prompt_root

    return get_prompt_root() / "registry.yaml"


def _unwrap_validation_error(exc: ValidationError, registry_path: Path) -> PromptingError:
    for error in exc.errors():
        ctx = error.get("ctx") or {}
        inner = ctx.get("error")
        if isinstance(inner, PromptingError):
            return inner
    return PromptingError("REGISTRY_PARSE_ERROR", f"{registry_path}: {exc}")


def _parse_registry(data: Any, registry_path: Path) -> PromptRegistry:
    if not isinstance(data, dict):
        raise PromptingError(
            "REGISTRY_PARSE_ERROR",
            f"{registry_path}: top-level YAML mapping expected",
        )
    try:
        return PromptRegistry.model_validate(data)
    except ValidationError as exc:
        raise _unwrap_validation_error(exc, registry_path) from exc


def load_registry(path: Path | None = None) -> PromptRegistry:
    """Load and validate the prompt registry, failing closed on any defect."""

    registry_path = _default_registry_path(path)
    if not registry_path.is_file():
        raise PromptingError("REGISTRY_NOT_FOUND", f"prompt registry not found at {registry_path}")
    data = _load_yaml_strict(registry_path)
    return _parse_registry(data, registry_path)


def _issues_from_validation_error(
    exc: ValidationError, registry_path: Path
) -> list[RegistryIssue]:
    issues: list[RegistryIssue] = []
    for error in exc.errors():
        ctx = error.get("ctx") or {}
        inner = ctx.get("error")
        loc = ".".join(str(part) for part in error.get("loc", ()))
        prefix = f"{loc}: " if loc else ""
        if isinstance(inner, PromptingError):
            issues.append(
                RegistryIssue(inner.code, str(registry_path), f"{prefix}{inner.details}")
            )
        else:
            issues.append(
                RegistryIssue(
                    "REGISTRY_PARSE_ERROR",
                    str(registry_path),
                    f"{prefix}{error.get('msg', 'invalid registry content')}",
                )
            )
    return issues


def _verify_entry_files(
    key: str, entry: PromptRegistryEntry | FocusModuleEntry, root: Path
) -> list[RegistryIssue]:
    path = root / entry.path
    if not path.is_file():
        return [
            RegistryIssue(
                "PROMPT_FILE_MISSING",
                str(path),
                f"{key}: recorded file {entry.path!r} is missing",
            )
        ]
    actual = hash_file(path)
    if actual != entry.sha256:
        return [
            RegistryIssue(
                "PROMPT_HASH_MISMATCH",
                str(path),
                f"{key}: recorded sha256 {entry.sha256} does not match "
                f"file hash {actual}",
            )
        ]
    return []


def _verify_protocol_pins(registry: PromptRegistry) -> list[RegistryIssue]:
    issues: list[RegistryIssue] = []
    for name, bundle in sorted(registry.protocols.items()):
        for slot, pin in sorted(bundle.prompts.items()):
            try:
                parse_prompt_ref(pin)
            except PromptingError:
                issues.append(
                    RegistryIssue(
                        "PROTOCOL_PIN_UNRESOLVED",
                        pin,
                        f"protocol {name!r} slot {slot!r} pin {pin!r} is malformed",
                    )
                )
                continue
            entry = registry.prompts.get(pin)
            if entry is None:
                issues.append(
                    RegistryIssue(
                        "PROTOCOL_PIN_UNRESOLVED",
                        pin,
                        f"protocol {name!r} slot {slot!r} pin {pin!r} is not a "
                        "registered prompt",
                    )
                )
            elif entry.status is not PromptStatus.RELEASED:
                issues.append(
                    RegistryIssue(
                        "PROTOCOL_PIN_UNRESOLVED",
                        pin,
                        f"protocol {name!r} slot {slot!r} pin {pin!r} has status "
                        f"{entry.status.value}, not RELEASED",
                    )
                )
    return issues


def verify_registry(path: Path | None = None) -> VerificationReport:
    """Verify every recorded byte and pin; content problems become issues."""

    registry_path = _default_registry_path(path)
    if not registry_path.is_file():
        raise PromptingError("REGISTRY_NOT_FOUND", f"prompt registry not found at {registry_path}")
    data = _load_yaml_strict(registry_path)
    issues: list[RegistryIssue] = []
    registry: PromptRegistry | None = None
    if not isinstance(data, dict):
        issues.append(
            RegistryIssue(
                "REGISTRY_PARSE_ERROR",
                str(registry_path),
                "top-level YAML mapping expected",
            )
        )
    else:
        try:
            registry = PromptRegistry.model_validate(data)
        except ValidationError as exc:
            issues.extend(_issues_from_validation_error(exc, registry_path))
    if registry is not None:
        root = registry_path.parent
        for key, entry in sorted(registry.prompts.items()):
            issues.extend(_verify_entry_files(key, entry, root))
        for key, entry in sorted(registry.focus_modules.items()):
            issues.extend(_verify_entry_files(key, entry, root))
        issues.extend(_verify_protocol_pins(registry))
    return VerificationReport(issues=tuple(issues))


def _resolve_entry(
    entry: PromptRegistryEntry | FocusModuleEntry,
    root: Path,
    ref: str,
    *,
    require_output_contract: bool,
) -> ResolvedPrompt:
    if entry.status is PromptStatus.DRAFT:
        raise PromptingError(
            "PROMPT_NOT_RELEASED",
            f"{ref} has status DRAFT; DRAFT entries may not be compiled",
        )
    path = root / entry.path
    if not path.is_file():
        raise PromptingError(
            "PROMPT_FILE_MISSING",
            f"{ref}: recorded file {entry.path!r} is missing under {root}",
        )
    actual = hash_file(path)
    if actual != entry.sha256:
        raise PromptingError(
            "PROMPT_HASH_MISMATCH",
            f"{ref}: recorded sha256 {entry.sha256} does not match file hash {actual}",
        )
    return ResolvedPrompt(
        entry=entry,
        path=path,
        bytes=path.read_bytes(),
        require_output_contract=require_output_contract,
    )


def resolve_prompt(registry: PromptRegistry, root: Path, ref: str) -> ResolvedPrompt:
    """Resolve a released core prompt by exact ``<id>@<version>`` ref."""

    parse_prompt_ref(ref)
    entry = registry.prompts.get(ref)
    if entry is None:
        raise PromptingError("PROMPT_NOT_FOUND", f"prompt {ref!r} is not registered")
    return _resolve_entry(entry, Path(root), ref, require_output_contract=True)


def resolve_focus_module(
    registry: PromptRegistry, root: Path, ref: str
) -> ResolvedPrompt:
    """Resolve a released focus module by exact ``<name>@<version>`` ref."""

    parse_prompt_ref(ref)
    entry = registry.focus_modules.get(ref)
    if entry is None:
        raise PromptingError("PROMPT_NOT_FOUND", f"focus module {ref!r} is not registered")
    return _resolve_entry(entry, Path(root), ref, require_output_contract=False)
