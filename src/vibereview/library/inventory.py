"""Deterministic inventory of blobs reachable from the configured commit."""

from __future__ import annotations

from pathlib import PurePosixPath
import re

from .git_source import PinnedGitSource, compute_content_sha256
from .models import (
    DocumentKind,
    ExcludedEntryRecord,
    LibraryConfig,
    LibraryDocumentRecord,
    MetadataStatus,
    SourceStatus,
)


_CITE_RE = re.compile(r"\\cite\{([^}]+)\}")
_ADMIN_NAMES = frozenset(
    {"README.md", "AGENTS.md", "LICENSE", "COPYING", ".gitignore", ".gitattributes"}
)
_ADMIN_PARTS = frozenset({".github", ".vscode", ".agent", ".agents"})


def extract_title_candidate(path: str) -> str | None:
    stem = PurePosixPath(path).stem
    parts = stem.split(" - ")
    if len(parts) >= 3:
        return " - ".join(parts[2:]).strip() or None
    if len(parts) == 2:
        return parts[1].strip() or None
    return stem.strip() or None


def extract_citekey_candidate(content: bytes) -> str | None:
    # The enclosing pinned source has already bounded the blob.  Decode once
    # before taking a character prefix so a multibyte code point straddling a
    # byte offset cannot turn otherwise-valid UTF-8 into a false failure.
    sample = content.decode("utf-8", errors="strict")[:4096]
    for line in sample.splitlines()[:20]:
        match = _CITE_RE.search(line)
        if match:
            return match.group(1).strip() or None
    return None


def classify_document(
    source_relative_path: str, config: LibraryConfig
) -> tuple[DocumentKind, str | None]:
    path = PurePosixPath(source_relative_path)
    if source_relative_path == config.bibliography:
        return DocumentKind.BIBLIOGRAPHY, None
    if source_relative_path == config.graph_path:
        return DocumentKind.GRAPH_EXPORT, None
    if path.name in _ADMIN_NAMES or any(part in _ADMIN_PARTS for part in path.parts):
        return DocumentKind.ADMINISTRATIVE_OR_INSTRUCTION, "administrative metadata"
    root = PurePosixPath(config.markdown_root)
    try:
        within_markdown = path.is_relative_to(root)
    except AttributeError:  # pragma: no cover - Python 3.11+ has is_relative_to
        within_markdown = path.parts[: len(root.parts)] == root.parts
    if within_markdown and path.suffix.casefold() == ".md":
        return DocumentKind.CANDIDATE_PAPER_MARKDOWN, None
    if within_markdown:
        return DocumentKind.UNKNOWN, "non-Markdown entry under markdown_root"
    if path.suffix.casefold() in {".py", ".sh", ".ps1", ".js", ".ts"}:
        return DocumentKind.TOOLING_OR_SCRIPT, "tooling is never a paper candidate"
    return DocumentKind.UNKNOWN, "entry is outside configured source paths"


def build_library_inventory(
    source: PinnedGitSource, config: LibraryConfig
) -> tuple[list[LibraryDocumentRecord], list[ExcludedEntryRecord]]:
    if source.expected_commit != config.expected_commit:
        raise ValueError("pinned source and library configuration disagree")
    records: list[LibraryDocumentRecord] = []
    excluded: list[ExcludedEntryRecord] = []
    for blob in source.enumerate_blobs():
        path = blob["source_relative_path"]
        content = source.read_blob(blob["git_blob_id"])
        kind, reason = classify_document(path, config)
        citekey = None
        title = None
        metadata = MetadataStatus.NOT_CHECKED
        status = SourceStatus.EXCLUDED
        if kind is DocumentKind.CANDIDATE_PAPER_MARKDOWN:
            citekey = extract_citekey_candidate(content)
            title = extract_title_candidate(path)
            metadata = MetadataStatus.OBSERVED if citekey else MetadataStatus.MISSING
            status = SourceStatus.CANDIDATE
        elif kind in {DocumentKind.BIBLIOGRAPHY, DocumentKind.GRAPH_EXPORT}:
            metadata = MetadataStatus.OBSERVED
            status = SourceStatus.OBSERVED
        record = LibraryDocumentRecord(
            library_id=config.library_id,
            source_commit=config.expected_commit,
            source_relative_path=path,
            git_blob_id=blob["git_blob_id"],
            content_sha256=compute_content_sha256(content),
            size_bytes=len(content),
            document_kind=kind,
            title_candidate=title,
            bibliography_key=citekey,
            metadata_status=metadata,
            source_status=status,
        )
        records.append(record)
        if reason is not None:
            excluded.append(
                ExcludedEntryRecord(
                    source_relative_path=path,
                    document_kind=kind,
                    exclusion_reason=reason,
                )
            )
    return records, sorted(excluded, key=lambda item: item.source_relative_path)


def generate_inventory_markdown(
    records: list[LibraryDocumentRecord], config: LibraryConfig
) -> str:
    counts: dict[DocumentKind, int] = {}
    sizes: dict[DocumentKind, int] = {}
    for record in records:
        counts[record.document_kind] = counts.get(record.document_kind, 0) + 1
        sizes[record.document_kind] = sizes.get(record.document_kind, 0) + record.size_bytes
    lines = [
        f"# Library inventory: `{config.library_id}`",
        "",
        f"- Expected commit: `{config.expected_commit}`",
        f"- Tracked regular blobs: {len(records)}",
        "",
        "| Kind | Count | Bytes |",
        "|---|---:|---:|",
    ]
    for kind in sorted(counts, key=str):
        lines.append(f"| `{kind.value}` | {counts[kind]} | {sizes[kind]} |")
    return "\n".join(lines) + "\n"
