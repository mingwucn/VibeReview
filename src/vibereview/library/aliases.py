"""Operator-adjudicated source aliases keyed by pinned source content hash."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from vibereview.ids import Sha256
from vibereview.runtime.records import RuntimeModel
from vibereview.runtime.repository import read_contained_regular_file

SOURCE_ALIASES_FORMAT_VERSION = "vibereview-source-aliases-1"
_MAX_ALIAS_DOCUMENT_BYTES = 1024 * 1024


class SourceAliasEntry(RuntimeModel):
    """One operator adjudication binding a pinned blob to a BibTeX citekey."""

    content_sha256: Sha256
    bib_key: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class SourceAliasDocument(RuntimeModel):
    """Versioned operator artifact; it lives outside the public repository."""

    format_version: Literal["vibereview-source-aliases-1"] = (
        SOURCE_ALIASES_FORMAT_VERSION
    )
    entries: list[SourceAliasEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_hashes(self) -> "SourceAliasDocument":
        hashes = [entry.content_sha256 for entry in self.entries]
        if len(hashes) != len(set(hashes)):
            raise ValueError("source alias entries must have unique content hashes")
        return self

    def as_mapping(self) -> dict[str, str]:
        return {entry.content_sha256: entry.bib_key for entry in self.entries}


def load_source_aliases(path: Path) -> SourceAliasDocument:
    """Load one bounded, strictly validated alias document from a regular file."""

    if not path.exists():
        raise FileNotFoundError(f"source alias document not found: {path}")
    content, _ = read_contained_regular_file(
        path.parent, path.name, max_bytes=_MAX_ALIAS_DOCUMENT_BYTES
    )
    return SourceAliasDocument.model_validate_json(content)
