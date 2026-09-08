"""Runtime-only contracts for pinned, read-only external libraries."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from vibereview.ids import Sha256
from vibereview.runtime.records import RuntimeModel


class LibraryError(Exception):
    """Base exception for external-library operations."""


class LibraryNotFoundError(LibraryError):
    """The configured external Git repository does not exist."""


class LibraryNotInitializedError(LibraryError):
    """The configured path is not a usable Git repository."""


class CommitMismatchError(LibraryError):
    """The working checkout does not equal the required immutable commit."""


class DirtyWorkingTreeError(LibraryError):
    """The configured checkout contains tracked or untracked changes."""


class GitlinkModeError(LibraryError):
    """The configured superproject entry is absent or not a stage-0 gitlink."""


class PathSecurityError(LibraryError):
    """A configured or Git path is unsafe for object access or local storage."""


class UnsupportedGitObjectError(LibraryError):
    """A selected tree entry is not a regular Git blob."""


class UnsupportedGraphSchemaError(LibraryError):
    """A graph export does not conform to the supported inspection schema."""


class CorpusSelectionError(LibraryError):
    """A selection or import operation failed closed."""


class CorpusIntegrityError(LibraryError):
    """A stored lock or immutable raw object failed verification."""


class CorpusNotImportedError(CorpusIntegrityError):
    """No generation-owned corpus import exists at the requested generation."""


class DocumentKind(StrEnum):
    CANDIDATE_PAPER_MARKDOWN = "candidate_paper_markdown"
    BIBLIOGRAPHY = "bibliography"
    GRAPH_EXPORT = "graph_export"
    ADMINISTRATIVE_OR_INSTRUCTION = "administrative_or_instruction"
    TOOLING_OR_SCRIPT = "tooling_or_script"
    UNKNOWN = "unknown"


class MetadataStatus(StrEnum):
    OBSERVED = "observed"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    NOT_CHECKED = "not_checked"


class SourceStatus(StrEnum):
    OBSERVED = "observed"
    CANDIDATE = "candidate"
    EXCLUDED = "excluded"


class GraphSchemaStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED_GRAPH_SCHEMA = "UNSUPPORTED_GRAPH_SCHEMA"


def validate_relative_git_path(value: str, *, allow_none: bool = False) -> str:
    """Return a canonical relative Git path or raise before invoking Git."""

    if allow_none and value == "":
        return value
    if not value or "\\" in value or "\x00" in value:
        raise ValueError("Git paths must be non-empty POSIX relative paths")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or parsed.as_posix() != value or ".." in parsed.parts:
        raise ValueError("Git paths must be canonical POSIX relative paths")
    if any(part in {"", ".", ".git"} for part in parsed.parts):
        raise ValueError("Git paths must not contain dot or Git-internal components")
    return value


class LibraryConfig(RuntimeModel):
    """Operator-supplied configuration; no repository or corpus is built in."""

    library_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    library_path: Path
    superproject_path: Path
    gitlink_path: str
    expected_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    markdown_root: str
    bibliography: str | None = None
    graph_path: str | None = None
    read_only: Literal[True] = True
    allow_network: Literal[False] = False
    require_clean_worktree: bool = True
    max_blob_bytes: int = Field(default=64 * 1024 * 1024, ge=1)
    max_selected_documents: int = Field(default=1_000, ge=1, le=10_000)
    max_selected_bytes: int = Field(default=1024 * 1024 * 1024, ge=1, le=1024 * 1024 * 1024)

    @field_validator("markdown_root")
    @classmethod
    def _safe_markdown_root(cls, value: str) -> str:
        return validate_relative_git_path(value.rstrip("/"))

    @field_validator("gitlink_path")
    @classmethod
    def _safe_gitlink_path(cls, value: str) -> str:
        return validate_relative_git_path(value)

    @field_validator("bibliography", "graph_path")
    @classmethod
    def _safe_optional_path(cls, value: str | None) -> str | None:
        return None if value is None else validate_relative_git_path(value)


class LibraryDocumentRecord(RuntimeModel):
    library_id: str
    source_commit: str
    source_relative_path: str
    git_blob_id: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    content_sha256: Sha256
    size_bytes: int = Field(ge=0)
    document_kind: DocumentKind
    title_candidate: str | None = None
    bibliography_key: str | None = None
    metadata_status: MetadataStatus
    source_status: SourceStatus


class ExcludedEntryRecord(RuntimeModel):
    source_relative_path: str
    document_kind: DocumentKind
    exclusion_reason: str


class BibEntryRecord(RuntimeModel):
    key: str
    entry_type: str
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    journal: str | None = None
    file_path: str | None = None
    raw_fields: dict[str, str] = Field(default_factory=dict)


class GraphSchemaReport(RuntimeModel):
    graph_path: str
    content_sha256: Sha256
    root_type: str
    container_keys: list[str]
    node_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)
    node_id_field: str
    edge_source_field: str
    edge_target_field: str
    node_fields_observed: list[str]
    edge_fields_observed: list[str]
    relation_labels: list[str]
    extraction_classes: list[str]
    duplicate_node_ids: list[str] = Field(default_factory=list)
    dangling_endpoints: list[dict[str, Any]] = Field(default_factory=list)
    has_nested_evidence: bool = False
    schema_status: GraphSchemaStatus


class SourceMappingEntry(RuntimeModel):
    paper_path: str | None = None
    content_sha256: Sha256 | None = None
    bib_key: str | None = None
    graph_node_id: str | None = None
    doi: str | None = None
    title: str | None = None
    resolution_method: str
    status: MetadataStatus


class SourceMappingReport(RuntimeModel):
    total_candidate_papers: int
    total_bib_entries: int
    total_graph_paper_nodes: int
    mapped_papers: list[SourceMappingEntry]
    unmapped_sources: list[str]
    unmapped_bib_keys: list[str]
    unmapped_graph_nodes: list[str]


class MetadataConflictReport(RuntimeModel):
    duplicate_bib_keys: list[str] = Field(default_factory=list)
    conflicting_dois: list[dict[str, Any]] = Field(default_factory=list)
    inconsistent_titles: list[dict[str, Any]] = Field(default_factory=list)
    conflicts_found: int = Field(default=0, ge=0)


class UpstreamIntegrityReport(RuntimeModel):
    library_id: str
    library_path: str
    superproject_path: str
    superproject_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    gitlink_path: str
    expected_commit: str
    checked_out_commit: str
    commit_match: bool
    gitlink_mode: Literal["160000"]
    gitlink_object_id: str
    gitlink_valid: Literal[True]
    is_clean: bool
    untracked_files_count: int
    modified_files_count: int
    total_tracked_blobs: int
    integrity_status: Literal["VERIFIED", "PINNED_OBJECTS_VERIFIED_DIRTY"]


class LibraryStateSnapshot(RuntimeModel):
    """Read-only before/after oracle for an operator-owned Git source."""

    library_head: str
    superproject_head: str
    library_index_hash: Sha256
    library_status_hash: Sha256
    library_worktree_hash: Sha256
    pinned_tree_hash: Sha256
    superproject_index_hash: Sha256
    superproject_status_hash: Sha256
    superproject_worktree_hash: Sha256
    superproject_gitlink_hash: Sha256


class CorpusSelectionDocument(RuntimeModel):
    source_relative_path: str
    content_sha256: Sha256
    decision: Literal["include", "exclude"]
    role: str = Field(min_length=1)
    accepted_by: str = Field(min_length=1)
    accepted_at: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @field_validator("source_relative_path")
    @classmethod
    def _safe_source_path(cls, value: str) -> str:
        return validate_relative_git_path(value)


class CorpusSelectionManifest(RuntimeModel):
    library_id: str
    source_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    documents: list[CorpusSelectionDocument] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_paths(self) -> "CorpusSelectionManifest":
        paths = [item.source_relative_path for item in self.documents]
        if len(paths) != len(set(paths)):
            raise ValueError("selection document paths must be unique")
        return self


class CorpusLockPaper(RuntimeModel):
    paper_id: str = Field(pattern=r"^P[0-9]{4}$")
    source_relative_path: str
    git_blob_id: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    source_hash: Sha256
    raw_md_path: str
    raw_md_hash: Sha256
    title: str | None = None
    doi: str | None = None
    bibliography_key: str | None = None


class LibrarySourceObject(RuntimeModel):
    role: Literal["paper", "bibliography", "graph"]
    source_relative_path: str
    git_blob_id: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    content_sha256: Sha256


class CorpusImportSource(RuntimeModel):
    library_id: str
    source_commit: str
    superproject_commit: str
    gitlink_path: str
    markdown_root: str
    bibliography_path: str | None
    graph_path: str | None
    selection_manifest_hash: Sha256
    objects: list[LibrarySourceObject]

    @model_validator(mode="after")
    def _unique_objects(self) -> "CorpusImportSource":
        keys = [(item.role, item.source_relative_path) for item in self.objects]
        if len(keys) != len(set(keys)):
            raise ValueError("import-source objects must have unique role/path pairs")
        if not any(item.role == "paper" for item in self.objects):
            raise ValueError("import source must contain at least one selected paper")
        return self


class CorpusLockManifest(RuntimeModel):
    contract_version: Literal["1.5.1b"] = "1.5.1b"
    library_id: str
    source_commit: str
    selection_manifest_path: Literal["library/selection_manifest.json"] = (
        "library/selection_manifest.json"
    )
    selection_manifest_hash: Sha256
    import_source: CorpusImportSource
    import_source_hash: Sha256
    committed_generation: int = Field(ge=0)
    papers: list[CorpusLockPaper]

    @model_validator(mode="after")
    def _source_fields_agree(self) -> "CorpusLockManifest":
        source = self.import_source
        if (
            source.library_id != self.library_id
            or source.source_commit != self.source_commit
            or source.selection_manifest_hash != self.selection_manifest_hash
        ):
            raise ValueError("corpus lock and import-source identity fields disagree")
        locked = {
            (paper.source_relative_path, paper.git_blob_id, paper.source_hash)
            for paper in self.papers
        }
        described = {
            (item.source_relative_path, item.git_blob_id, item.content_sha256)
            for item in source.objects
            if item.role == "paper"
        }
        if locked != described:
            raise ValueError("corpus lock papers disagree with import-source objects")
        return self


class GenerationLibraryManifest(RuntimeModel):
    """Generation-owned integrity index for one corpus import."""

    generation: int = Field(ge=0)
    corpus_lock_path: Literal["library/corpus.lock.json"] = "library/corpus.lock.json"
    corpus_lock_hash: Sha256
    selection_manifest_path: Literal["library/selection_manifest.json"] = (
        "library/selection_manifest.json"
    )
    selection_manifest_hash: Sha256
    import_source_hash: Sha256
    resource_hashes: dict[str, Sha256]


class CorpusImportResult(RuntimeModel):
    generation: int = Field(ge=0)
    allocated_ids: dict[str, str] = Field(default_factory=dict)
    corpus_lock_hash: Sha256
    reused: bool = False
