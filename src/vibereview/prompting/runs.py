"""Append-only, tamper-evident Deep Research run registration."""

from __future__ import annotations

import re
from pathlib import Path

from vibereview.runtime.hashing import canonical_json_bytes, hash_bytes, hash_file

from .compiler import (
    load_manifest,
    revalidate_manifest_inputs,
    validate_compiled_against_manifest,
)
from .models import DeepResearchRunManifest, PromptingError, RUN_ID_PATTERN


def _resolve_inside(project_root: Path, value: Path) -> Path | None:
    path = Path(value)
    if not path.is_absolute():
        path = project_root / path
    try:
        path.resolve().relative_to(project_root.resolve())
    except ValueError:
        return None
    return path


def register_run(
    *,
    project_root: Path,
    run_id: str,
    compiled_prompt_path: Path,
    raw_output_path: Path,
    operator: str,
    provider: str = "unknown",
    mode: str = "unknown",
    model: str = "unknown",
    model_version: str = "unknown",
    started_at: str = "unknown",
    completed_at: str = "unknown",
) -> DeepResearchRunManifest:
    """Register the immutable compiled-prompt/raw-output/manifest triple."""

    root = Path(project_root)
    if re.fullmatch(RUN_ID_PATTERN, run_id) is None:
        raise PromptingError(
            "RUN_ID_INVALID", f"run id {run_id!r} does not match {RUN_ID_PATTERN}"
        )
    if not operator:
        raise PromptingError(
            "OPERATOR_REQUIRED", "an operator name is required to register a run"
        )
    run_dir = root / "deep_research" / "runs" / run_id
    if run_dir.exists():
        raise PromptingError("RUN_EXISTS", f"run {run_id!r} is already registered")

    compiled = _resolve_inside(root, compiled_prompt_path)
    if compiled is None or not compiled.is_file():
        raise PromptingError(
            "MANIFEST_NOT_FOUND",
            f"compiled prompt {str(compiled_prompt_path)!r} does not exist inside {root}",
        )
    manifest_path = compiled.with_suffix(".manifest.json")
    if not manifest_path.is_file():
        raise PromptingError(
            "MANIFEST_NOT_FOUND",
            f"compiled prompt {compiled} has no sibling .manifest.json",
        )
    manifest = load_manifest(manifest_path)
    validate_compiled_against_manifest(manifest, compiled)
    revalidate_manifest_inputs(manifest, root)

    raw = _resolve_inside(root, raw_output_path)
    if raw is None or not raw.is_file() or raw.stat().st_size == 0:
        raise PromptingError(
            "RAW_OUTPUT_INVALID",
            f"raw output {str(raw_output_path)!r} is missing or empty",
        )

    compiled_bytes = compiled.read_bytes()
    raw_bytes = raw.read_bytes()
    run_dir.mkdir(parents=True)
    (run_dir / "compiled_prompt.md").write_bytes(compiled_bytes)
    (run_dir / "raw_output.md").write_bytes(raw_bytes)
    run_manifest = DeepResearchRunManifest(
        run_id=run_id,
        compiled_prompt_path=compiled.relative_to(root).as_posix(),
        compiled_prompt_sha256=hash_bytes(compiled_bytes),
        raw_output_path=raw.relative_to(root).as_posix(),
        raw_output_sha256=hash_bytes(raw_bytes),
        manifest_sha256=hash_file(manifest_path),
        provider=provider,
        mode=mode,
        model=model,
        model_version=model_version,
        started_at=started_at,
        completed_at=completed_at,
        operator=operator,
    )
    (run_dir / "run.json").write_bytes(
        canonical_json_bytes(run_manifest.model_dump(mode="json")) + b"\n"
    )
    return run_manifest
