"""Deterministic BibTeX parsing over bytes read from a pinned Git object."""

from __future__ import annotations

import re

from vibereview.ids import normalize_doi

from .git_source import PinnedGitSource
from .models import BibEntryRecord, LibraryConfig


def _clean_latex_braces(text: str) -> str:
    return re.sub(r"[{}]", "", text).strip()


def parse_bibtex_text(text: str) -> tuple[list[BibEntryRecord], list[str]]:
    """Parse a bounded BibTeX subset without filesystem or network access."""

    entries: list[BibEntryRecord] = []
    duplicates: list[str] = []
    seen: set[str] = set()
    position = 0
    while position < len(text):
        start = text.find("@", position)
        if start < 0:
            break
        opening = text.find("{", start)
        if opening < 0:
            break
        entry_type = text[start + 1 : opening].strip().lower()
        if entry_type in {"comment", "string", "preamble"}:
            position = opening + 1
            continue
        comma = text.find(",", opening)
        if comma < 0:
            break
        key = text[opening + 1 : comma].strip()
        if not key:
            position = comma + 1
            continue
        if key in seen:
            duplicates.append(key)
        seen.add(key)

        fields: dict[str, str] = {}
        cursor = comma + 1
        depth = 1
        field_start = cursor
        while cursor < len(text) and depth:
            character = text[cursor]
            if character == "{" :
                depth += 1
                cursor += 1
            elif character == "}":
                depth -= 1
                cursor += 1
                if depth == 0:
                    break
            elif character == "=" and depth == 1:
                raw_name = text[field_start:cursor].strip().lstrip(",").strip()
                field_name = raw_name.split()[-1].lower() if raw_name else ""
                cursor += 1
                while cursor < len(text) and text[cursor].isspace():
                    cursor += 1
                if cursor >= len(text):
                    break
                if text[cursor] == "{":
                    value_start = cursor + 1
                    value_depth = 1
                    cursor += 1
                    while cursor < len(text) and value_depth:
                        value_depth += (text[cursor] == "{") - (text[cursor] == "}")
                        cursor += 1
                    value = text[value_start : cursor - 1]
                elif text[cursor] == '"':
                    value_start = cursor + 1
                    cursor += 1
                    while cursor < len(text):
                        if text[cursor] == '"' and text[cursor - 1] != "\\":
                            break
                        cursor += 1
                    value = text[value_start:cursor]
                    cursor += 1
                else:
                    value_start = cursor
                    while cursor < len(text) and text[cursor] not in ",}\r\n":
                        cursor += 1
                    value = text[value_start:cursor].strip()
                if field_name:
                    fields[field_name] = value.strip()
                field_start = cursor
            else:
                cursor += 1
        position = cursor

        title = _clean_latex_braces(fields["title"]) if "title" in fields else None
        authors = [
            item.strip()
            for item in re.split(
                r"\s+and\s+",
                _clean_latex_braces(fields.get("author", "")),
                flags=re.IGNORECASE,
            )
            if item.strip()
        ]
        year_match = re.search(r"\b(?:19|20)\d{2}\b", fields.get("year", ""))
        year = int(year_match.group()) if year_match else None
        doi = None
        if fields.get("doi", "").strip():
            doi = normalize_doi(_clean_latex_braces(fields["doi"]))
        journal = _clean_latex_braces(fields["journal"]) if "journal" in fields else None
        entries.append(
            BibEntryRecord(
                key=key,
                entry_type=entry_type,
                title=title,
                authors=authors,
                year=year,
                doi=doi,
                journal=journal,
                file_path=fields.get("file"),
                raw_fields=fields,
            )
        )
    return entries, sorted(set(duplicates))


def parse_bibtex_bytes(content: bytes) -> tuple[list[BibEntryRecord], list[str]]:
    return parse_bibtex_text(content.decode("utf-8", errors="strict"))


def load_bibliography(
    source: PinnedGitSource, config: LibraryConfig
) -> tuple[list[BibEntryRecord], list[str]]:
    if config.bibliography is None:
        return [], []
    return parse_bibtex_bytes(source.read_path(config.bibliography))
