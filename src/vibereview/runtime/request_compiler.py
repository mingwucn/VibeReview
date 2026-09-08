"""Bounded, deterministic compilation of an immutable task bundle.

The compiler consumes only paths explicitly named by the engine-readable
``bundle_manifest.json``.  It never receives the private task provenance or
source paths.  The resulting request is runtime-only and provider neutral so
REST and CLI adapters can share exactly the same engine-visible bytes.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Literal

from pydantic import ConfigDict, Field

from .hashing import canonical_json_bytes, hash_bytes
from .records import RuntimeModel, Sha256, TaskType

SourceKind = Literal[
    "manifest", "instructions", "input_schema", "proposal_schema", "engine_input",
    "dependency", "resource"
]


class RequestCompilationError(RuntimeError):
    """The engine-facing bundle cannot be compiled safely and completely."""


class RequestCompilationPolicy(RuntimeModel):
    """Independent input bounds applied before any external engine is started."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_manifest_bytes: int = Field(default=1_048_576, gt=0)
    max_instructions_bytes: int = Field(default=1_048_576, gt=0)
    max_schema_bytes: int = Field(default=1_048_576, gt=0)
    max_engine_input_bytes: int = Field(default=4_194_304, gt=0)
    max_dependency_count: int = Field(default=512, ge=0)
    max_dependency_bytes: int = Field(default=4_194_304, gt=0)
    max_resource_count: int = Field(default=128, ge=0)
    max_resource_bytes: int = Field(default=16_777_216, gt=0)
    max_total_source_bytes: int = Field(default=524_288, gt=0)
    max_compiled_request_bytes: int = Field(default=524_288, gt=0)


class RequestSourceRecord(RuntimeModel):
    """Nonsecret provenance for one source embedded in a compiled request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SourceKind
    identifier: str
    relative_path: Path
    media_type: str
    size_bytes: int = Field(ge=0)
    sha256: Sha256


class CompiledEngineRequest(RuntimeModel):
    """Complete provider-neutral prompt plus its deterministic provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_type: TaskType
    system_prompt: str
    user_prompt: str
    proposal_schema: dict[str, Any]
    sources: tuple[RequestSourceRecord, ...]
    source_bytes: int = Field(ge=0)
    compiled_bytes: int = Field(ge=0)
    request_digest: Sha256

    def cli_prompt(self) -> str:
        return f"{self.system_prompt}\n\n{self.user_prompt}"

    def worker_payload(self) -> bytes:
        return (self.model_dump_json(exclude_none=True) + "\n").encode("utf-8")


_TOP_LEVEL_KEYS = {
    "task_type",
    "task_spec_version",
    "prompt_version",
    "instructions",
    "schemas",
    "engine_input",
    "dependencies",
    "resources",
}
_ENTRY_KEYS = {"path", "sha256"}
_RESOURCE_KEYS = {
    "resource_id",
    "logical_name",
    "path",
    "media_type",
    "size_bytes",
    "sha256",
}
_TEXT_MEDIA_TYPES = {"text/markdown", "text/plain", "application/json"}
_RESERVED_FRAMING_TOKEN = "VIBEREVIEW_"


class _Budget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def add(self, size: int, label: str) -> None:
        if size < 0 or self.used + size > self.limit:
            raise RequestCompilationError(
                f"total source-byte bound exceeded while reading {label}"
            )
        self.used += size

    @property
    def remaining(self) -> int:
        return self.limit - self.used


def _require_exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RequestCompilationError(f"{label} must be a JSON object")
    keys = set(value)
    if keys != expected:
        raise RequestCompilationError(
            f"{label} keys mismatch: missing={sorted(expected - keys)} "
            f"extra={sorted(keys - expected)}"
        )
    return value


def _safe_relative_path(raw: Any, label: str) -> Path:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise RequestCompilationError(f"{label} path must be a non-empty POSIX path")
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise RequestCompilationError(f"unsafe {label} path {raw!r}")
    if path.as_posix() != raw:
        raise RequestCompilationError(f"non-canonical {label} path {raw!r}")
    return path


def _read_regular_bounded(
    bundle_dir: Path,
    relative_path: Path,
    *,
    per_file_limit: int,
    budget: _Budget,
    label: str,
) -> bytes:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    directory_descriptors: list[int] = []
    descriptor: int | None = None
    try:
        root_stat = os.lstat(bundle_dir)
    except OSError as exc:
        raise RequestCompilationError(f"task bundle is unavailable: {exc}") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise RequestCompilationError("task bundle must be a non-symlink directory")
    try:
        root_descriptor = os.open(bundle_dir, directory_flags)
    except OSError as exc:
        raise RequestCompilationError(f"task bundle cannot be opened safely: {exc}") from exc
    directory_descriptors.append(root_descriptor)
    try:
        opened_root = os.fstat(root_descriptor)
        if (opened_root.st_dev, opened_root.st_ino) != (
            root_stat.st_dev,
            root_stat.st_ino,
        ):
            raise RequestCompilationError("task bundle changed while opening")
        parent_descriptor = root_descriptor
        for component in relative_path.parts[:-1]:
            try:
                component_stat = os.stat(
                    component, dir_fd=parent_descriptor, follow_symlinks=False
                )
            except OSError as exc:
                raise RequestCompilationError(
                    f"{label} parent is unavailable: {exc}"
                ) from exc
            if stat.S_ISLNK(component_stat.st_mode) or not stat.S_ISDIR(
                component_stat.st_mode
            ):
                raise RequestCompilationError(
                    f"{label} contains a non-directory or symlinked ancestor"
                )
            try:
                next_descriptor = os.open(
                    component, directory_flags, dir_fd=parent_descriptor
                )
            except OSError as exc:
                raise RequestCompilationError(
                    f"{label} parent cannot be opened safely: {exc}"
                ) from exc
            directory_descriptors.append(next_descriptor)
            opened_component = os.fstat(next_descriptor)
            if (
                opened_component.st_dev,
                opened_component.st_ino,
                opened_component.st_mtime_ns,
            ) != (
                component_stat.st_dev,
                component_stat.st_ino,
                component_stat.st_mtime_ns,
            ):
                raise RequestCompilationError(
                    f"{label} ancestor changed while opening"
                )
            parent_descriptor = next_descriptor

        try:
            item_stat = os.stat(
                relative_path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise RequestCompilationError(f"{label} is unavailable: {exc}") from exc
        if (
            stat.S_ISLNK(item_stat.st_mode)
            or not stat.S_ISREG(item_stat.st_mode)
            or item_stat.st_nlink != 1
        ):
            raise RequestCompilationError(
                f"{label} must be a single-link regular file without symlinks"
            )
        if item_stat.st_size > per_file_limit:
            raise RequestCompilationError(f"{label} exceeds its byte bound")
        try:
            descriptor = os.open(
                relative_path.name, file_flags, dir_fd=parent_descriptor
            )
        except OSError as exc:
            raise RequestCompilationError(
                f"{label} cannot be opened safely: {exc}"
            ) from exc
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise RequestCompilationError(f"{label} changed file type while opening")
        if (
            (
                opened_stat.st_dev,
                opened_stat.st_ino,
                opened_stat.st_size,
                opened_stat.st_mtime_ns,
                opened_stat.st_nlink,
            )
            != (
                item_stat.st_dev,
                item_stat.st_ino,
                item_stat.st_size,
                item_stat.st_mtime_ns,
                item_stat.st_nlink,
            )
        ):
            raise RequestCompilationError(f"{label} changed while opening")
        if opened_stat.st_size > budget.remaining:
            raise RequestCompilationError(
                f"total source-byte bound exceeded while reading {label}"
            )
        read_limit = min(per_file_limit, budget.remaining)
        chunks: list[bytes] = []
        observed = 0
        while True:
            chunk = os.read(descriptor, min(65_536, read_limit + 1 - observed))
            if not chunk:
                break
            observed += len(chunk)
            if observed > read_limit:
                if observed > per_file_limit:
                    raise RequestCompilationError(f"{label} exceeds its byte bound")
                raise RequestCompilationError(
                    f"total source-byte bound exceeded while reading {label}"
                )
            chunks.append(chunk)
        after_stat = os.fstat(descriptor)
        if (
            observed != opened_stat.st_size
            or (
                after_stat.st_dev,
                after_stat.st_ino,
                after_stat.st_size,
                after_stat.st_mtime_ns,
                after_stat.st_nlink,
            )
            != (
                opened_stat.st_dev,
                opened_stat.st_ino,
                opened_stat.st_size,
                opened_stat.st_mtime_ns,
                opened_stat.st_nlink,
            )
        ):
            raise RequestCompilationError(f"{label} changed while reading")
        budget.add(observed, label)
        return b"".join(chunks)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        for directory_descriptor in reversed(directory_descriptors):
            os.close(directory_descriptor)


def _decode_utf8(raw: bytes, label: str) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RequestCompilationError(f"{label} is not valid UTF-8") from exc
    if _RESERVED_FRAMING_TOKEN in text:
        raise RequestCompilationError(f"{label} contains a reserved framing token")
    return text


def _parse_object(text: str, label: str) -> dict[str, Any]:
    value = _parse_json(text, label)
    if not isinstance(value, dict):
        raise RequestCompilationError(f"{label} JSON root must be an object")
    return value


def _parse_json(text: str, label: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise RequestCompilationError(f"{label} is not valid JSON: {exc}") from exc


def _expected_sha(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != 71
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise RequestCompilationError(f"{label} has an invalid sha256")
    return value


def _read_manifest_entry(
    bundle_dir: Path,
    entry: Any,
    *,
    kind: SourceKind,
    identifier: str,
    media_type: str,
    limit: int,
    budget: _Budget,
    seen_paths: set[Path],
) -> tuple[str, RequestSourceRecord]:
    item = _require_exact_keys(entry, _ENTRY_KEYS, identifier)
    relative_path = _safe_relative_path(item["path"], identifier)
    if relative_path in seen_paths:
        raise RequestCompilationError(f"duplicate manifest path {relative_path.as_posix()}")
    seen_paths.add(relative_path)
    raw = _read_regular_bounded(
        bundle_dir,
        relative_path,
        per_file_limit=limit,
        budget=budget,
        label=identifier,
    )
    actual = hash_bytes(raw)
    expected = _expected_sha(item["sha256"], identifier)
    if actual != expected:
        raise RequestCompilationError(f"{identifier} content hash mismatch")
    text = _decode_utf8(raw, identifier)
    return text, RequestSourceRecord(
        kind=kind,
        identifier=identifier,
        relative_path=relative_path,
        media_type=media_type,
        size_bytes=len(raw),
        sha256=actual,
    )


def compile_engine_request(
    bundle_dir: Path,
    task_type: TaskType,
    expected_manifest_hash: Sha256,
    policy: RequestCompilationPolicy | None = None,
) -> CompiledEngineRequest:
    """Compile every manifest-listed engine input under deterministic bounds."""

    limits = policy or RequestCompilationPolicy()
    bundle_dir = Path(os.path.abspath(os.fspath(bundle_dir)))
    budget = _Budget(limits.max_total_source_bytes)
    manifest_raw = _read_regular_bounded(
        bundle_dir,
        Path("bundle_manifest.json"),
        per_file_limit=limits.max_manifest_bytes,
        budget=budget,
        label="bundle manifest",
    )
    if hash_bytes(manifest_raw) != expected_manifest_hash:
        raise RequestCompilationError(
            "bundle manifest does not match the private task trust anchor"
        )
    manifest = _require_exact_keys(
        _parse_object(_decode_utf8(manifest_raw, "bundle manifest"), "bundle manifest"),
        _TOP_LEVEL_KEYS,
        "bundle manifest",
    )
    if manifest["task_type"] != task_type.value:
        raise RequestCompilationError("bundle task_type does not match AgentTask")
    if not isinstance(manifest["task_spec_version"], str) or not manifest["task_spec_version"]:
        raise RequestCompilationError("task_spec_version must be non-empty")
    if not isinstance(manifest["prompt_version"], str) or not manifest["prompt_version"]:
        raise RequestCompilationError("prompt_version must be non-empty")

    sources: list[RequestSourceRecord] = [
        RequestSourceRecord(
            kind="manifest",
            identifier="bundle manifest",
            relative_path=Path("bundle_manifest.json"),
            media_type="application/json",
            size_bytes=len(manifest_raw),
            sha256=hash_bytes(manifest_raw),
        )
    ]
    seen_paths: set[Path] = {Path("bundle_manifest.json")}
    instructions, record = _read_manifest_entry(
        bundle_dir, manifest["instructions"], kind="instructions",
        identifier="instructions", media_type="text/markdown",
        limit=limits.max_instructions_bytes, budget=budget, seen_paths=seen_paths,
    )
    sources.append(record)
    schemas = _require_exact_keys(manifest["schemas"], {"input", "proposal"}, "schemas")
    input_schema_text, record = _read_manifest_entry(
        bundle_dir, schemas["input"], kind="input_schema",
        identifier="input schema", media_type="application/json",
        limit=limits.max_schema_bytes, budget=budget, seen_paths=seen_paths,
    )
    input_schema = _parse_object(input_schema_text, "input schema")
    sources.append(record)
    proposal_schema_text, record = _read_manifest_entry(
        bundle_dir, schemas["proposal"], kind="proposal_schema",
        identifier="proposal schema", media_type="application/json",
        limit=limits.max_schema_bytes, budget=budget, seen_paths=seen_paths,
    )
    proposal_schema = _parse_object(proposal_schema_text, "proposal schema")
    sources.append(record)
    engine_input_text, record = _read_manifest_entry(
        bundle_dir, manifest["engine_input"], kind="engine_input",
        identifier="engine input", media_type="application/json",
        limit=limits.max_engine_input_bytes, budget=budget, seen_paths=seen_paths,
    )
    _parse_object(engine_input_text, "engine input")
    sources.append(record)

    dependencies = manifest["dependencies"]
    if not isinstance(dependencies, list):
        raise RequestCompilationError("dependencies must be a JSON array")
    if len(dependencies) > limits.max_dependency_count:
        raise RequestCompilationError("dependency-count bound exceeded")
    dependency_sections: list[str] = []
    dependency_keys: set[str] = set()
    for ordinal, raw_entry in enumerate(dependencies):
        entry = _require_exact_keys(raw_entry, {"key", "path", "sha256"}, f"dependency {ordinal}")
        key = entry["key"]
        if not isinstance(key, str) or not key or key in dependency_keys:
            raise RequestCompilationError(f"invalid or duplicate dependency key {key!r}")
        dependency_keys.add(key)
        content, record = _read_manifest_entry(
            bundle_dir, {"path": entry["path"], "sha256": entry["sha256"]},
            kind="dependency", identifier=f"dependency:{key}",
            media_type="application/json", limit=limits.max_dependency_bytes,
            budget=budget, seen_paths=seen_paths,
        )
        _parse_object(content, f"dependency:{key}")
        sources.append(record)
        dependency_sections.append(
            "<VIBEREVIEW_DEPENDENCY_META>\n"
            + json.dumps(
                {"key": key, "sha256": record.sha256},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n</VIBEREVIEW_DEPENDENCY_META>\n"
            + "<VIBEREVIEW_DEPENDENCY_CONTENT>\n"
            + content
            + "\n</VIBEREVIEW_DEPENDENCY_CONTENT>"
        )

    resources = manifest["resources"]
    if not isinstance(resources, list):
        raise RequestCompilationError("resources must be a JSON array")
    if len(resources) > limits.max_resource_count:
        raise RequestCompilationError("resource-count bound exceeded")
    resource_sections: list[str] = []
    resource_ids: set[str] = set()
    for ordinal, raw_entry in enumerate(resources):
        entry = _require_exact_keys(raw_entry, _RESOURCE_KEYS, f"resource {ordinal}")
        resource_id = entry["resource_id"]
        logical_name = entry["logical_name"]
        media_type = entry["media_type"]
        declared_size = entry["size_bytes"]
        if not isinstance(resource_id, str) or not resource_id or resource_id in resource_ids:
            raise RequestCompilationError(f"invalid or duplicate resource_id {resource_id!r}")
        if not isinstance(logical_name, str) or not logical_name:
            raise RequestCompilationError(f"resource {resource_id} has invalid logical_name")
        if media_type not in _TEXT_MEDIA_TYPES:
            raise RequestCompilationError(f"resource {resource_id} has unsupported media_type")
        if not isinstance(declared_size, int) or isinstance(declared_size, bool) or declared_size < 0:
            raise RequestCompilationError(f"resource {resource_id} has invalid size_bytes")
        resource_ids.add(resource_id)
        content, record = _read_manifest_entry(
            bundle_dir, {"path": entry["path"], "sha256": entry["sha256"]},
            kind="resource", identifier=f"resource:{resource_id}", media_type=media_type,
            limit=limits.max_resource_bytes, budget=budget, seen_paths=seen_paths,
        )
        if record.size_bytes != declared_size:
            raise RequestCompilationError(f"resource {resource_id} size mismatch")
        if media_type == "application/json":
            _parse_json(content, f"resource:{resource_id}")
        sources.append(record)
        resource_sections.append(
            "<VIBEREVIEW_RESOURCE_META>\n"
            + json.dumps(
                {"resource_id": resource_id, "logical_name": logical_name,
                 "media_type": media_type, "size_bytes": record.size_bytes,
                 "sha256": record.sha256},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            + "\n</VIBEREVIEW_RESOURCE_META>\n"
            + "<VIBEREVIEW_RESOURCE_CONTENT>\n"
            + content
            + "\n</VIBEREVIEW_RESOURCE_CONTENT>"
        )

    system_prompt = (
        "Return exactly one JSON object conforming to the proposal schema; "
        "do not add surrounding text.\n"
        + "<VIBEREVIEW_INSTRUCTIONS>\n" + instructions
        + "\n</VIBEREVIEW_INSTRUCTIONS>\n"
        + "<VIBEREVIEW_INPUT_SCHEMA>\n" + input_schema_text
        + "\n</VIBEREVIEW_INPUT_SCHEMA>\n"
        + "<VIBEREVIEW_PROPOSAL_SCHEMA>\n" + proposal_schema_text
        + "\n</VIBEREVIEW_PROPOSAL_SCHEMA>"
    )
    user_prompt = (
        "<VIBEREVIEW_ENGINE_INPUT>\n" + engine_input_text
        + "\n</VIBEREVIEW_ENGINE_INPUT>\n"
        + "<VIBEREVIEW_DEPENDENCIES>\n"
        + ("\n".join(dependency_sections) if dependency_sections else "[]")
        + "\n</VIBEREVIEW_DEPENDENCIES>\n"
        + "<VIBEREVIEW_RESOURCES>\n"
        + ("\n".join(resource_sections) if resource_sections else "[]")
        + "\n</VIBEREVIEW_RESOURCES>"
    )
    digest_payload = {
        "task_type": task_type.value,
        "task_spec_version": manifest["task_spec_version"],
        "prompt_version": manifest["prompt_version"],
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "proposal_schema": proposal_schema,
        "sources": [item.model_dump(mode="json") for item in sources],
    }
    rendered = canonical_json_bytes(digest_payload)
    if len(rendered) > limits.max_compiled_request_bytes:
        raise RequestCompilationError("compiled-request byte bound exceeded")
    compiled = CompiledEngineRequest(
        task_type=task_type,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        proposal_schema=proposal_schema,
        sources=tuple(sources),
        source_bytes=budget.used,
        compiled_bytes=len(rendered),
        request_digest=hash_bytes(rendered),
    )
    if len(compiled.worker_payload()) > limits.max_compiled_request_bytes:
        raise RequestCompilationError("serialized worker-payload byte bound exceeded")
    return compiled
