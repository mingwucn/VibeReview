"""Deterministic prompt compiler with per-compile manifests."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Mapping, Sequence

from vibereview.runtime.hashing import canonical_json_bytes, hash_file, hash_text

from . import COMPILER_VERSION
from .models import (
    CompiledPromptManifest,
    CompileResult,
    ComponentRef,
    InputRef,
    PromptRegistry,
    ResolvedPrompt,
    SupplementRef,
    PromptingError,
    parse_prompt_ref,
)
from .registry import resolve_focus_module, resolve_prompt

CANONICAL_ROLE_ORDER: tuple[str, ...] = (
    "project_config",
    "project_brief",
    "protocol",
    "structure",
    "outline",
    "discovery",
    "reports",
    "claims",
    "packets",
    "other",
)

_BANNER_ROLE_BY_INPUT_ROLE = {role: role.replace("_", "-") for role in CANONICAL_ROLE_ORDER}
_OUTLINE_HEADING = re.compile(r"^(#{1,6})[ \t]+", re.MULTILINE)
_SECTION_BOUNDARY = re.compile(r"[A-Za-z0-9_.\-]")


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n")


def _banner_ref(path: Path) -> str:
    """Relative paths are recorded as passed; absolute paths as their name."""

    return str(path) if not path.is_absolute() else path.name


def extract_outline_section(outline_text: str, section_id: str) -> str:
    """Return the exact slice of ``outline_text`` owned by ``section_id``.

    The slice runs from the heading line whose first token is exactly the
    section ID to the next heading of equal or higher level (or EOF).
    """

    token = re.compile(
        r"^(#{1,6})[ \t]+" + re.escape(section_id) + r"(?!" + _SECTION_BOUNDARY.pattern + r")",
        re.MULTILINE,
    )
    matches = list(token.finditer(outline_text))
    if not matches:
        raise PromptingError(
            "SECTION_NOT_FOUND", f"section {section_id!r} has no heading in the outline"
        )
    if len(matches) > 1:
        raise PromptingError(
            "AMBIGUOUS_SECTION",
            f"section {section_id!r} matches {len(matches)} outline headings",
        )
    match = matches[0]
    level = len(match.group(1))
    end = len(outline_text)
    for heading in _OUTLINE_HEADING.finditer(outline_text, match.end()):
        if len(heading.group(1)) <= level:
            end = heading.start()
            break
    return outline_text[match.start() : end].rstrip()


def _read_nonempty(path: Path, label: str) -> tuple[bytes, str]:
    if not path.is_file():
        raise PromptingError(
            "PROMPT_FILE_MISSING", f"{label} file {str(path)!r} does not exist"
        )
    data = path.read_bytes()
    if not data:
        raise PromptingError("PROMPT_FILE_MISSING", f"{label} file {str(path)!r} is empty")
    return data, hash_file(path)


def compile_prompt(
    *,
    registry: PromptRegistry,
    registry_root: Path,
    prompt_ref: str,
    compiled_prompt_id: str,
    inputs: Mapping[str, Path] | None = None,
    focus_modules: Sequence[str] = (),
    section_id: str | None = None,
    supplement: Path | None = None,
) -> CompileResult:
    """Compile one released prompt and its inputs into deterministic bytes."""

    resolved = resolve_prompt(registry, registry_root, prompt_ref)

    module_resolved: list[tuple[str, ResolvedPrompt]] = []
    seen_modules: set[str] = set()
    for ref in focus_modules:
        parse_prompt_ref(ref)
        if ref in seen_modules:
            raise PromptingError(
                "INVALID_PROMPT_REF", f"duplicate focus module ref {ref!r}"
            )
        seen_modules.add(ref)
        module_resolved.append((ref, resolve_focus_module(registry, registry_root, ref)))

    input_paths = dict(inputs or {})
    unknown_roles = sorted(role for role in input_paths if role not in CANONICAL_ROLE_ORDER)
    if unknown_roles:
        raise PromptingError(
            "UNKNOWN_INPUT_ROLE",
            f"unknown input role(s) {', '.join(unknown_roles)}; canonical roles are "
            + ", ".join(CANONICAL_ROLE_ORDER),
        )
    if section_id is not None and "outline" not in input_paths:
        raise PromptingError(
            "SECTION_NOT_FOUND",
            "outline section extraction requires an 'outline' role input",
        )

    input_records: dict[str, tuple[Path, bytes, str]] = {}
    for role in CANONICAL_ROLE_ORDER:
        if role not in input_paths:
            continue
        path = Path(input_paths[role])
        data, file_hash = _read_nonempty(path, f"input {role!r}")
        input_records[role] = (path, data, file_hash)

    outline_section_text: str | None = None
    if section_id is not None:
        outline_bytes = input_records["outline"][1]
        outline_section_text = extract_outline_section(
            _lf(outline_bytes.decode("utf-8")), section_id
        )

    supplement_record: tuple[Path, bytes, str] | None = None
    if supplement is not None:
        supplement_path = Path(supplement)
        supplement_data, supplement_hash = _read_nonempty(supplement_path, "supplement")
        supplement_record = (supplement_path, supplement_data, supplement_hash)

    prompt_hash = resolved.entry.sha256
    components: list[tuple[str, str, str, str]] = [
        ("core-prompt", prompt_ref, prompt_hash, _lf(resolved.body))
    ]
    ordered_roles = [role for role in CANONICAL_ROLE_ORDER if role in input_records]
    for role in ordered_roles:
        path, data, file_hash = input_records[role]
        ref = _banner_ref(path)
        banner_role = _BANNER_ROLE_BY_INPUT_ROLE[role]
        if role == "outline" and section_id is not None:
            ref = f"{ref}#{section_id}"
            banner_role = "outline-section"
            text = outline_section_text or ""
        else:
            text = _lf(data.decode("utf-8"))
        components.append((banner_role, ref, file_hash, text))
    for ref, module in module_resolved:
        components.append(
            (
                "focus-module",
                ref,
                module.entry.sha256,
                _lf(module.bytes.decode("utf-8")),
            )
        )
    if supplement_record is not None:
        supplement_path, supplement_data, supplement_hash = supplement_record
        components.append(
            (
                "supplement",
                _banner_ref(supplement_path),
                supplement_hash,
                _lf(supplement_data.decode("utf-8")),
            )
        )
    components.append(("output-contract", prompt_ref, prompt_hash, _lf(resolved.output_contract)))

    lines = [
        f'<!-- vibereview:compiled-prompt id="{compiled_prompt_id}" '
        f'compiler="{COMPILER_VERSION}" -->'
    ]
    for banner_role, ref, sha256, text in components:
        lines.append(
            f'<!-- vibereview:component role="{banner_role}" ref="{ref}" '
            f'sha256="{sha256}" -->'
        )
        lines.append(text.rstrip())
    compiled_text = "\n".join(lines) + "\n"

    manifest = CompiledPromptManifest(
        compiled_prompt_id=compiled_prompt_id,
        prompt=ComponentRef(
            id=resolved.entry.id, version=resolved.entry.version, sha256=prompt_hash
        ),
        modules=[
            ComponentRef(id=module.entry.id, version=module.entry.version, sha256=module.entry.sha256)
            for _, module in module_resolved
        ],
        inputs=[
            InputRef(role=role, path=str(input_records[role][0]), sha256=input_records[role][2])
            for role in ordered_roles
        ],
        section_id=section_id,
        supplement=(
            SupplementRef(path=str(supplement_record[0]), sha256=supplement_record[2])
            if supplement_record is not None
            else None
        ),
        compiled_sha256=hash_text(compiled_text),
        compiler_version=COMPILER_VERSION,
    )
    return CompileResult(compiled_text=compiled_text, manifest=manifest)


def write_compiled(
    result: CompileResult, out_dir: Path, *, overwrite_identical: bool = True
) -> tuple[Path, Path]:
    """Persist ``<id>.md`` and ``<id>.manifest.json``; refuse manual edits."""

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{result.manifest.compiled_prompt_id}.md"
    manifest_path = out_dir / f"{result.manifest.compiled_prompt_id}.manifest.json"
    data = result.compiled_text.encode("utf-8")
    if md_path.exists():
        if md_path.read_bytes() != data:
            raise PromptingError(
                "COMPILED_PROMPT_TAMPERED",
                f"{md_path} already exists with different bytes",
            )
        if not overwrite_identical:
            raise PromptingError(
                "COMPILED_PROMPT_TAMPERED", f"{md_path} already exists"
            )
    else:
        md_path.write_bytes(data)
    manifest_bytes = (
        canonical_json_bytes(result.manifest.model_dump(mode="json")) + b"\n"
    )
    if not manifest_path.exists() or manifest_path.read_bytes() != manifest_bytes:
        manifest_path.write_bytes(manifest_bytes)
    return md_path, manifest_path


def load_manifest(path: Path) -> CompiledPromptManifest:
    manifest_path = Path(path)
    if not manifest_path.is_file():
        raise PromptingError("MANIFEST_NOT_FOUND", f"compiled manifest not found at {manifest_path}")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return CompiledPromptManifest.model_validate(data)
    except PromptingError:
        raise
    except (OSError, ValueError) as exc:
        raise PromptingError("MANIFEST_INVALID", f"{manifest_path}: {exc}") from exc


def validate_compiled_against_manifest(
    manifest: CompiledPromptManifest, compiled_path: Path
) -> None:
    """Detect manual edits of the compiled prompt or its manifest."""

    compiled_path = Path(compiled_path)
    if not compiled_path.is_file():
        raise PromptingError("PROMPT_FILE_MISSING", f"compiled prompt not found at {compiled_path}")
    manifest_path = compiled_path.with_suffix(".manifest.json")
    disk_manifest = load_manifest(manifest_path)
    actual = hash_file(compiled_path)
    if actual != disk_manifest.compiled_sha256:
        raise PromptingError(
            "COMPILED_PROMPT_TAMPERED",
            f"{compiled_path} hash {actual} does not match the recorded "
            f"compiled_sha256 {disk_manifest.compiled_sha256}",
        )
    if disk_manifest != manifest:
        raise PromptingError(
            "MANIFEST_INVALID",
            "the manifest does not match the manifest serialized next to the compiled prompt",
        )
    if actual != manifest.compiled_sha256:
        raise PromptingError(
            "MANIFEST_INVALID",
            "manifest compiled_sha256 does not match the recomputed hash of the compiled prompt",
        )


def revalidate_manifest_inputs(
    manifest: CompiledPromptManifest, project_root: Path
) -> None:
    """Detect input drift after compilation, relative to ``project_root``."""

    root = Path(project_root)
    for input_ref in manifest.inputs:
        path = root / input_ref.path
        if not path.is_file() or hash_file(path) != input_ref.sha256:
            raise PromptingError(
                "INPUT_HASH_CHANGED",
                f"input {input_ref.role!r} ({input_ref.path}) changed since compilation",
            )
    if manifest.supplement is not None:
        path = root / manifest.supplement.path
        if not path.is_file() or hash_file(path) != manifest.supplement.sha256:
            raise PromptingError(
                "INPUT_HASH_CHANGED",
                f"supplement ({manifest.supplement.path}) changed since compilation",
            )
