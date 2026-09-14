"""Operator source-quality records and deterministic structural diagnostics."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from vibereview.ids import Sha256
from vibereview.runtime.records import RuntimeModel
from vibereview.runtime.repository import atomic_write_text, read_contained_regular_file

from .git_source import PinnedGitSource, compute_content_sha256
from .models import CorpusIntegrityError, CorpusSelectionManifest, LibraryDocumentRecord

SOURCE_QUALITY_FORMAT_VERSION = "vibereview-source-quality-1"
_MAX_QUALITY_DOCUMENT_BYTES = 1024 * 1024

_HEADING_RE = re.compile(r"^#{1,6}\s+\S")
_MARKER_RE = re.compile(r"(\\begin\{|\\end\{|\$\$|\\\(|\\\[|\\frac|\\sqrt)")
_MIN_BODY_CHARACTERS = 200
_REPLACEMENT_RATIO_WARNING = 0.001
_MARKER_RATIO_WARNING = 0.05


class SourceQualityClassification(StrEnum):
    READABLE = "READABLE"
    READABLE_WITH_ARTIFACTS = "READABLE_WITH_ARTIFACTS"
    MATERIAL_EXTRACTION_PROBLEM = "MATERIAL_EXTRACTION_PROBLEM"
    UNUSABLE = "UNUSABLE"


BLOCKING_CLASSIFICATIONS = frozenset(
    {
        SourceQualityClassification.MATERIAL_EXTRACTION_PROBLEM,
        SourceQualityClassification.UNUSABLE,
    }
)


class SourceQualityRecord(RuntimeModel):
    """One operator assessment of one pinned source blob."""

    content_sha256: Sha256
    classification: SourceQualityClassification
    assessor: str = Field(min_length=1)
    assessed_at: str = Field(min_length=1)
    rationale: str = Field(min_length=1)

    @field_validator("assessed_at")
    @classmethod
    def _iso8601_timestamp(cls, value: str) -> str:
        try:
            datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("assessed_at must be an ISO-8601 timestamp") from exc
        return value


class SourceQualityDocument(RuntimeModel):
    """Versioned operator artifact; it lives outside the public repository."""

    format_version: Literal["vibereview-source-quality-1"] = (
        SOURCE_QUALITY_FORMAT_VERSION
    )
    records: list[SourceQualityRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_hashes(self) -> "SourceQualityDocument":
        hashes = [record.content_sha256 for record in self.records]
        if len(hashes) != len(set(hashes)):
            raise ValueError("source-quality records must have unique content hashes")
        return self

    def by_hash(self) -> dict[str, SourceQualityRecord]:
        return {record.content_sha256: record for record in self.records}


def load_source_quality_document(path: Path) -> SourceQualityDocument:
    """Load one bounded, strictly validated quality document from a regular file."""

    if not path.exists():
        raise FileNotFoundError(f"source-quality document not found: {path}")
    content, _ = read_contained_regular_file(
        path.parent, path.name, max_bytes=_MAX_QUALITY_DOCUMENT_BYTES
    )
    return SourceQualityDocument.model_validate_json(content)


def save_source_quality_document(document: SourceQualityDocument, path: Path) -> None:
    atomic_write_text(path, document.model_dump_json(indent=2) + "\n")


def validate_selection_source_quality(
    manifest: CorpusSelectionManifest, document: SourceQualityDocument
) -> list[str]:
    """Return blocking problems; an empty list means the selection passes."""

    records = document.by_hash()
    problems: list[str] = []
    for selected in manifest.documents:
        if selected.decision != "include":
            continue
        record = records.get(selected.content_sha256)
        if record is None:
            problems.append(
                "included source lacks a source-quality record: "
                f"{selected.source_relative_path}"
            )
        elif record.classification in BLOCKING_CLASSIFICATIONS:
            problems.append(
                f"included source has blocking classification "
                f"{record.classification.value}: {selected.source_relative_path}"
            )
    return problems


class SourceQualityDiagnostics(RuntimeModel):
    """Advisory structural diagnostics; never an authoritative classification."""

    source_relative_path: str
    content_sha256: Sha256
    size_bytes: int = Field(ge=0)
    decodable_utf8: bool
    line_count: int = Field(ge=0)
    has_title_line: bool
    heading_count: int = Field(ge=0)
    body_character_count: int = Field(ge=0)
    replacement_character_count: int = Field(ge=0)
    replacement_character_ratio: float = Field(ge=0)
    marker_count: int = Field(ge=0)
    marker_ratio: float = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)


def diagnose_source_bytes(
    source_relative_path: str, content_sha256: Sha256, content: bytes
) -> SourceQualityDiagnostics:
    """Compute deterministic structural heuristics over pinned Markdown bytes."""

    try:
        text = content.decode("utf-8", errors="strict")
        decodable = True
    except UnicodeDecodeError:
        text = content.decode("utf-8", errors="replace")
        decodable = False
    lines = text.splitlines()
    headings = [line for line in lines if _HEADING_RE.match(line)]
    body_character_count = sum(
        len(line.strip()) for line in lines if line.strip() and not _HEADING_RE.match(line)
    )
    replacement_count = text.count("\ufffd")
    marker_count = len(_MARKER_RE.findall(text))
    replacement_ratio = replacement_count / max(len(text), 1)
    marker_ratio = marker_count / max(len(lines), 1)
    has_title_line = any(line.startswith("# ") for line in lines)

    warnings: list[str] = []
    if not decodable:
        warnings.append("content is not valid UTF-8")
    if replacement_ratio > _REPLACEMENT_RATIO_WARNING:
        warnings.append("elevated replacement-character ratio")
    if not has_title_line:
        warnings.append("no recognizable title line")
    if not headings:
        warnings.append("no heading structure")
    if marker_ratio > _MARKER_RATIO_WARNING:
        warnings.append("elevated math/table extraction-marker density")
    if body_character_count < _MIN_BODY_CHARACTERS:
        warnings.append("body appears empty or truncated")
    return SourceQualityDiagnostics(
        source_relative_path=source_relative_path,
        content_sha256=content_sha256,
        size_bytes=len(content),
        decodable_utf8=decodable,
        line_count=len(lines),
        has_title_line=has_title_line,
        heading_count=len(headings),
        body_character_count=body_character_count,
        replacement_character_count=replacement_count,
        replacement_character_ratio=replacement_ratio,
        marker_count=marker_count,
        marker_ratio=marker_ratio,
        warnings=warnings,
    )


def assess_source_structure(
    source: PinnedGitSource, record: LibraryDocumentRecord
) -> SourceQualityDiagnostics:
    """Read the pinned blob (never the working tree) and diagnose its structure."""

    content = source.read_blob(record.git_blob_id)
    if compute_content_sha256(content) != record.content_sha256:
        raise CorpusIntegrityError("pinned object changed since the inventory record")
    return diagnose_source_bytes(
        record.source_relative_path, record.content_sha256, content
    )
