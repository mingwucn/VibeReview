"""Operator-owned configuration for an external library and review project."""

from __future__ import annotations

from pathlib import Path
import tomllib

from pydantic import Field, field_validator

from vibereview.runtime.records import RuntimeModel
from vibereview.runtime.repository import read_contained_regular_file

from .models import LibraryConfig, PathSecurityError, validate_relative_git_path


class ProjectMetadata(RuntimeModel):
    name: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    contract_version: str = "1.5.1b"


class ProjectLibraryConfig(RuntimeModel):
    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    path: Path
    superproject_path: Path
    gitlink_path: str
    expected_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    markdown_root: str
    bibliography: str | None = None
    graph_path: str | None = None
    selection_manifest: Path
    require_clean_worktree: bool = True
    max_blob_bytes: int = Field(default=64 * 1024 * 1024, ge=1)
    max_selected_documents: int = Field(default=1_000, ge=1, le=10_000)
    max_selected_bytes: int = Field(default=1024 * 1024 * 1024, ge=1, le=1024 * 1024 * 1024)

    @field_validator("markdown_root", "gitlink_path")
    @classmethod
    def _markdown_path(cls, value: str) -> str:
        return validate_relative_git_path(value.rstrip("/"))

    @field_validator("bibliography", "graph_path")
    @classmethod
    def _optional_git_path(cls, value: str | None) -> str | None:
        return None if value is None else validate_relative_git_path(value)

    def as_library_config(self) -> LibraryConfig:
        return LibraryConfig(
            library_id=self.id,
            library_path=self.path,
            superproject_path=self.superproject_path,
            gitlink_path=self.gitlink_path,
            expected_commit=self.expected_commit,
            markdown_root=self.markdown_root,
            bibliography=self.bibliography,
            graph_path=self.graph_path,
            require_clean_worktree=self.require_clean_worktree,
            max_blob_bytes=self.max_blob_bytes,
            max_selected_documents=self.max_selected_documents,
            max_selected_bytes=self.max_selected_bytes,
        )


class ProjectRetrievalConfig(RuntimeModel):
    max_candidates: int = Field(default=20, ge=1, le=100)
    max_query_terms: int = Field(default=32, ge=1, le=64)


class ReviewProjectConfig(RuntimeModel):
    project: ProjectMetadata
    library: ProjectLibraryConfig
    retrieval: ProjectRetrievalConfig = Field(default_factory=ProjectRetrievalConfig)


def _is_within(path: Path, root: Path) -> bool:
    resolved = path.resolve(strict=False)
    resolved_root = root.resolve(strict=False)
    return resolved == resolved_root or resolved_root in resolved.parents


def load_review_config(
    path: Path, *, public_repository_root: Path
) -> ReviewProjectConfig:
    """Load a TOML config that is deliberately kept outside the public repository."""

    supplied_path = path if path.is_absolute() else Path.cwd() / path
    config_path = supplied_path.parent.resolve(strict=False) / supplied_path.name
    try:
        config_path.lstat()
    except FileNotFoundError:
        raise FileNotFoundError(f"review config not found: {config_path}")
    if _is_within(config_path, public_repository_root):
        raise PathSecurityError("operator configuration must be outside the public repository")
    try:
        config_bytes, _ = read_contained_regular_file(
            config_path.parent, config_path.name, max_bytes=1024 * 1024
        )
        config_text = config_bytes.decode("utf-8", errors="strict")
    except (OSError, UnicodeError, ValueError) as exc:
        raise PathSecurityError(
            "operator configuration must be one bounded UTF-8 regular file"
        ) from exc
    raw = tomllib.loads(config_text)
    config = ReviewProjectConfig.model_validate(raw)
    base = config_path.parent
    library_path = config.library.path
    superproject_path = config.library.superproject_path
    selection_path = config.library.selection_manifest
    if not library_path.is_absolute():
        library_path = (base / library_path).resolve(strict=False)
    if not superproject_path.is_absolute():
        superproject_path = (base / superproject_path).resolve(strict=False)
    if not selection_path.is_absolute():
        selection_path = (base / selection_path).resolve(strict=False)
    if _is_within(library_path, public_repository_root):
        raise PathSecurityError("external library must be outside the public repository")
    if _is_within(superproject_path, public_repository_root):
        raise PathSecurityError("library superproject must be outside the public repository")
    if _is_within(selection_path, public_repository_root):
        raise PathSecurityError("selection manifest must be outside the public repository")
    if _is_within(selection_path, library_path) or _is_within(
        selection_path, superproject_path
    ):
        raise PathSecurityError(
            "selection manifest must be outside the library and its superproject"
        )
    return config.model_copy(
        update={
            "library": config.library.model_copy(
                update={
                    "path": library_path,
                    "superproject_path": superproject_path,
                    "selection_manifest": selection_path,
                }
            )
        }
    )
