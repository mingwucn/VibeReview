"""Pydantic models, error codes, and small value records for the prompt slice."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from vibereview.ids import Sha256
from vibereview.runtime.records import RuntimeModel

PROMPT_ID_PATTERN = r"^[A-Z]{1,4}[0-9]{3}$"
FOCUS_ID_PATTERN = r"^[a-z][a-z0-9_]*$"
SEMVER_PATTERN = r"^[0-9]+\.[0-9]+\.[0-9]+$"
COMPILED_PROMPT_ID_PATTERN = r"^[A-Z][A-Za-z0-9_.-]{2,64}$"
RUN_ID_PATTERN = r"^[A-Z]{2}[0-9]{3}(_[A-Za-z0-9.]+)?$"
RELEASE_DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
_OUTPUT_CONTRACT_LINE = re.compile(r"^##[ \t]+Output Contract[ \t]*$")


class PromptingError(ValueError):
    """Fail-closed prompt-infrastructure failure carrying a stable machine code."""

    def __init__(self, code: str, details: str) -> None:
        self.code = code
        self.details = details
        super().__init__(f"{code}: {details}")


class PromptStatus(StrEnum):
    DRAFT = "DRAFT"
    RELEASED = "RELEASED"
    DEPRECATED = "DEPRECATED"


class PromptClass(StrEnum):
    PROJECT = "project"
    DEEP_RESEARCH = "deep_research"
    CLAIMS = "claims"
    SYNTHESIS = "synthesis"
    MANUSCRIPT = "manuscript"


def parse_prompt_ref(ref: str) -> tuple[str, str]:
    """Split a ``<id>@<version>`` prompt reference into its two parts."""

    if not isinstance(ref, str):
        raise PromptingError(
            "INVALID_PROMPT_REF",
            f"prompt ref must be a string, got {type(ref).__name__}",
        )
    parts = ref.split("@")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise PromptingError(
            "INVALID_PROMPT_REF",
            f"malformed prompt ref {ref!r}; expected <id>@<version>",
        )
    return parts[0], parts[1]


def _validate_relative_entry_path(value: str) -> str:
    if not value or "\\" in value:
        raise PromptingError(
            "INVALID_REGISTRY_ENTRY",
            f"entry path {value!r} must be a relative POSIX path",
        )
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts:
        raise PromptingError(
            "INVALID_REGISTRY_ENTRY",
            f"entry path {value!r} must be relative and must not contain '..'",
        )
    return value


class ComponentRef(RuntimeModel):
    """Registry-bound identity and hash of one compiled component."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    version: str
    sha256: Sha256


class InputRef(RuntimeModel):
    """One project input bound into a compiled prompt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str
    path: str
    sha256: Sha256


class SupplementRef(RuntimeModel):
    """One project supplement bound into a compiled prompt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sha256: Sha256


class CompiledPromptManifest(RuntimeModel):
    """Deterministic per-compile manifest; never contains timestamps."""

    format_version: Literal["vibereview-compiled-prompt-manifest-1"] = (
        "vibereview-compiled-prompt-manifest-1"
    )
    compiled_prompt_id: str = Field(pattern=COMPILED_PROMPT_ID_PATTERN)
    prompt: ComponentRef
    modules: list[ComponentRef] = Field(default_factory=list)
    inputs: list[InputRef] = Field(default_factory=list)
    section_id: str | None = None
    supplement: SupplementRef | None = None
    compiled_sha256: Sha256
    compiler_version: str


class DeepResearchRunManifest(RuntimeModel):
    """Immutable triple record for one manually executed Deep Research run."""

    format_version: Literal["vibereview-deep-research-run-1"] = (
        "vibereview-deep-research-run-1"
    )
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    compiled_prompt_path: str
    compiled_prompt_sha256: Sha256
    raw_output_path: str
    raw_output_sha256: Sha256
    manifest_sha256: Sha256
    provider: str = "unknown"
    mode: str = "unknown"
    model: str = "unknown"
    model_version: str = "unknown"
    started_at: str = "unknown"
    completed_at: str = "unknown"
    operator: str = Field(min_length=1)
    manual_ui_boundary: Literal[True] = True


@dataclass(frozen=True, slots=True)
class ResolvedPrompt:
    """Hash-verified prompt file bytes plus its body/contract split.

    Core prompts must contain exactly one ``## Output Contract`` heading.
    Focus modules may omit it; their whole text becomes the body.
    """

    entry: PromptRegistryEntry | FocusModuleEntry
    path: Path
    bytes: bytes
    require_output_contract: bool = field(default=True, kw_only=True)
    body: str = field(init=False)
    output_contract: str = field(init=False)

    def __post_init__(self) -> None:
        text = self.bytes.decode("utf-8").replace("\r\n", "\n")
        heading_start: int | None = None
        offset = 0
        for line in text.split("\n"):
            if _OUTPUT_CONTRACT_LINE.match(line):
                if heading_start is not None:
                    raise PromptingError(
                        "INVALID_PROMPT_ENTRY",
                        f"{self.path}: expected exactly one "
                        "'## Output Contract' heading, found more than one",
                    )
                heading_start = offset
            offset += len(line) + 1
        if heading_start is None:
            if self.require_output_contract:
                raise PromptingError(
                    "INVALID_PROMPT_ENTRY",
                    f"{self.path}: expected exactly one '## Output Contract' "
                    "heading, found none",
                )
            heading_start = len(text)
        body = text[:heading_start].rstrip()
        object.__setattr__(self, "body", body + "\n" if body else "")
        object.__setattr__(self, "output_contract", text[heading_start:])


@dataclass(frozen=True, slots=True)
class RegistryIssue:
    code: str
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class VerificationReport:
    issues: tuple[RegistryIssue, ...] = ()

    @property
    def errors(self) -> tuple[RegistryIssue, ...]:
        return self.issues

    @property
    def ok(self) -> bool:
        return not self.issues

    def codes(self) -> set[str]:
        return {issue.code for issue in self.issues}


class PromptRegistryEntry(RuntimeModel):
    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, populate_by_name=True
    )

    id: str = Field(pattern=PROMPT_ID_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    class_: PromptClass = Field(validation_alias="class", serialization_alias="class")
    path: str
    sha256: Sha256
    status: PromptStatus
    scientific_role: str = Field(min_length=1)
    canonical_evidence: Literal[False] = False
    release_date: str = Field(pattern=RELEASE_DATE_PATTERN)
    change_note: str = Field(min_length=1)

    _path_is_relative = field_validator("path")(_validate_relative_entry_path)


class FocusModuleEntry(RuntimeModel):
    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, populate_by_name=True
    )

    id: str = Field(pattern=FOCUS_ID_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    path: str
    sha256: Sha256
    status: PromptStatus
    release_date: str = Field(pattern=RELEASE_DATE_PATTERN)
    change_note: str = Field(min_length=1)

    _path_is_relative = field_validator("path")(_validate_relative_entry_path)


class ProtocolBundle(RuntimeModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9.-]*$")
    status: Literal["released", "draft"]
    prompts: dict[str, str] = Field(default_factory=dict)

    @field_validator("prompts")
    @classmethod
    def _pins_must_be_prompt_refs(cls, value: dict[str, str]) -> dict[str, str]:
        for slot, ref in value.items():
            prompt_id, version = parse_prompt_ref(ref)
            if (
                re.fullmatch(PROMPT_ID_PATTERN, prompt_id) is None
                or re.fullmatch(SEMVER_PATTERN, version) is None
            ):
                raise PromptingError(
                    "INVALID_PROMPT_REF",
                    f"protocol pin {slot!r}={ref!r} is not a valid released prompt ref",
                )
        return value


class PromptRegistry(RuntimeModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    registry_version: Literal[1] = 1
    protocols: dict[str, ProtocolBundle] = Field(default_factory=dict)
    prompts: dict[str, PromptRegistryEntry] = Field(default_factory=dict)
    focus_modules: dict[str, FocusModuleEntry] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _keys_supply_identity(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        protocols = data.get("protocols")
        if isinstance(protocols, dict):
            normalized_protocols = dict(protocols)
            for key, bundle in protocols.items():
                if isinstance(bundle, dict) and "name" not in bundle:
                    normalized_bundle = dict(bundle)
                    normalized_bundle["name"] = key
                    normalized_protocols[key] = normalized_bundle
            normalized["protocols"] = normalized_protocols
        for section in ("prompts", "focus_modules"):
            entries = data.get(section)
            if not isinstance(entries, dict):
                continue
            normalized_entries = dict(entries)
            for key, entry in entries.items():
                if not isinstance(entry, dict):
                    continue
                try:
                    entry_id, version = parse_prompt_ref(key)
                except PromptingError as exc:
                    raise PromptingError(
                        "INVALID_REGISTRY_ENTRY",
                        f"{section} key {key!r} is not a valid <id>@<version> ref",
                    ) from exc
                normalized_entry = dict(entry)
                normalized_entry.setdefault("id", entry_id)
                normalized_entry.setdefault("version", version)
                normalized_entries[key] = normalized_entry
            normalized[section] = normalized_entries
        return normalized

    @model_validator(mode="after")
    def _keys_match_entry_ids(self) -> PromptRegistry:
        for section, entries in (
            ("prompts", self.prompts),
            ("focus_modules", self.focus_modules),
        ):
            for key, entry in entries.items():
                expected = f"{entry.id}@{entry.version}"
                if key != expected:
                    raise PromptingError(
                        "INVALID_REGISTRY_ENTRY",
                        f"{section} key {key!r} does not match entry "
                        f"id/version {expected!r}",
                    )
                recorded = PurePosixPath(entry.path)
                if recorded.parent.name != entry.id or recorded.stem != entry.version:
                    raise PromptingError(
                        "INVALID_REGISTRY_ENTRY",
                        f"{section} key {key!r} does not match recorded path "
                        f"{entry.path!r} (expected <dir>/{entry.id}/"
                        f"{entry.version}.md)",
                    )
        return self


@dataclass(frozen=True, slots=True)
class CompileResult:
    compiled_text: str
    manifest: CompiledPromptManifest
