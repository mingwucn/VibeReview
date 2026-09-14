"""Crosswalk resolver between paper Markdown, bibliography, and graph nodes (L0)."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any
import unicodedata

from .models import (
    BibEntryRecord,
    DocumentKind,
    LibraryDocumentRecord,
    MetadataConflictReport,
    MetadataStatus,
    SourceMappingEntry,
    SourceMappingReport,
)


def _normalize_title_for_comparison(title: str) -> str:
    """Normalize title for conflict/consistency checking."""

    cleaned = re.sub(r"[^\w\s]", "", title.lower())
    return " ".join(cleaned.split())


def _normalize_title_key(title: str) -> str:
    """Case-folded, punctuation/diacritic-insensitive, whitespace-collapsed key."""

    decomposed = unicodedata.normalize("NFKD", title)
    folded = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    ).casefold()
    cleaned = re.sub(r"[^\w\s]", " ", folded)
    return " ".join(cleaned.split())


def resolve_source_mappings(
    candidate_papers: list[LibraryDocumentRecord],
    bib_entries: list[BibEntryRecord],
    duplicate_bib_keys: list[str],
    graph_nodes: list[dict[str, Any]],
    aliases: dict[str, str] | None = None,
) -> tuple[SourceMappingReport, MetadataConflictReport]:
    """Crosswalk candidate papers, bibliography records, and graph nodes strictly by contract."""
    aliases = aliases or {}

    bib_by_key = {b.key: b for b in bib_entries}
    bib_by_stem: dict[str, list[BibEntryRecord]] = {}
    doi_to_bib: dict[str, list[BibEntryRecord]] = {}
    bib_by_title_key: dict[str, list[BibEntryRecord]] = {}

    for b in bib_entries:
        if b.file_path:
            raw_fname = b.file_path.replace("\\", "/").split("/")[-1]
            stem = Path(raw_fname).stem.lower()
            bib_by_stem.setdefault(stem, []).append(b)

        if b.doi:
            doi_to_bib.setdefault(b.doi.lower(), []).append(b)

        if b.title:
            title_key = _normalize_title_key(b.title)
            if title_key:
                bib_by_title_key.setdefault(title_key, []).append(b)

    graph_by_id: dict[str, dict[str, Any]] = {}
    graph_by_citekey: dict[str, dict[str, Any]] = {}
    for n in graph_nodes:
        nid = str(n.get("id", ""))
        graph_by_id[nid] = n
        citekey = n.get("citekey")
        if citekey:
            graph_by_citekey[str(citekey)] = n

    # 1. Detect metadata conflicts
    conflicting_dois: list[dict[str, Any]] = []
    for doi, entries in doi_to_bib.items():
        if len(entries) > 1:
            titles = {
                _normalize_title_for_comparison(e.title)
                for e in entries
                if e.title
            }
            if len(titles) > 1:
                conflicting_dois.append(
                    {
                        "doi": doi,
                        "keys": [e.key for e in entries],
                        "titles": [e.title for e in entries],
                    }
                )

    inconsistent_titles: list[dict[str, Any]] = []
    alias_conflicts: list[dict[str, Any]] = []
    mapped_entries: list[SourceMappingEntry] = []
    mapped_sources: set[str] = set()
    mapped_bib_keys: set[str] = set()
    mapped_graph_node_ids: set[str] = set()

    # 2. Resolve each candidate paper
    for paper in candidate_papers:
        if paper.document_kind != DocumentKind.CANDIDATE_PAPER_MARKDOWN:
            continue

        paper_stem = Path(paper.source_relative_path).stem.lower()
        content_hash = paper.content_sha256

        matched_bib: BibEntryRecord | None = None
        matched_graph: dict[str, Any] | None = None
        resolution_method = "unresolved"
        match_tier = "none"
        status = MetadataStatus.MISSING

        # Human-reviewed alias adjudication first.  An alias never silently
        # overrides a conflicting exact citekey match; the pair is reported as
        # an operator-visible conflict and left unresolved instead.
        if content_hash in aliases:
            alias_target = aliases[content_hash]
            exact_key = paper.bibliography_key
            if (
                exact_key is not None
                and exact_key in bib_by_key
                and alias_target in bib_by_key
                and exact_key != alias_target
            ):
                alias_conflicts.append(
                    {
                        "source_path": paper.source_relative_path,
                        "content_sha256": content_hash,
                        "alias_bib_key": alias_target,
                        "exact_bib_key": exact_key,
                        "reason": "alias conflicts with the exact citekey match",
                    }
                )
                resolution_method = "alias_conflict"
                status = MetadataStatus.AMBIGUOUS
            else:
                if alias_target in bib_by_key:
                    matched_bib = bib_by_key[alias_target]
                    resolution_method = "alias"
                    match_tier = "alias"
                    status = MetadataStatus.OBSERVED
                if alias_target in graph_by_id:
                    matched_graph = graph_by_id[alias_target]
                    resolution_method = "alias"
                    match_tier = "alias"
                    status = MetadataStatus.OBSERVED
                if matched_bib is None and matched_graph is None:
                    alias_conflicts.append(
                        {
                            "source_path": paper.source_relative_path,
                            "content_sha256": content_hash,
                            "alias_bib_key": alias_target,
                            "reason": "alias target is absent from the pinned bibliography and graph",
                        }
                    )

        # Check Step 1: Exact stable citekey
        if (
            matched_bib is None
            and status is not MetadataStatus.AMBIGUOUS
            and paper.bibliography_key
        ):
            citekey = paper.bibliography_key
            if citekey in bib_by_key:
                matched_bib = bib_by_key[citekey]
                resolution_method = "exact_citekey"
                match_tier = "exact"
                status = MetadataStatus.OBSERVED
            if citekey in graph_by_id:
                matched_graph = graph_by_id[citekey]
            elif citekey in graph_by_citekey:
                matched_graph = graph_by_citekey[citekey]

        # Check Step 3: Exact stored relative source path / stem
        if (
            matched_bib is None
            and status is not MetadataStatus.AMBIGUOUS
            and paper_stem in bib_by_stem
        ):
            candidates = bib_by_stem[paper_stem]
            if len(candidates) == 1:
                matched_bib = candidates[0]
                resolution_method = "exact_path"
                match_tier = "exact"
                status = MetadataStatus.OBSERVED
            elif len(candidates) > 1:
                resolution_method = "ambiguous_path"
                status = MetadataStatus.AMBIGUOUS

        # Advisory normalized tier: unique, non-conflicting normalized DOI or
        # title equivalence.  Normalized matches are decision support for the
        # operator; they do not authorize import metadata on their own.
        if matched_bib is None and status is MetadataStatus.MISSING:
            doi_matches: dict[str, BibEntryRecord] = {}
            if paper.doi_candidate:
                for entry in doi_to_bib.get(paper.doi_candidate.lower(), []):
                    doi_matches[entry.key] = entry
            title_matches: dict[str, BibEntryRecord] = {}
            if paper.title_candidate:
                title_key = _normalize_title_key(paper.title_candidate)
                if title_key:
                    for entry in bib_by_title_key.get(title_key, []):
                        title_matches[entry.key] = entry
            if doi_matches:
                if len(doi_matches) > 1 or (
                    title_matches and set(title_matches) != set(doi_matches)
                ):
                    resolution_method = "normalized_conflict"
                    status = MetadataStatus.AMBIGUOUS
                else:
                    matched_bib = next(iter(doi_matches.values()))
                    resolution_method = "normalized_doi"
                    match_tier = "normalized"
                    status = MetadataStatus.OBSERVED
            elif len(title_matches) > 1:
                resolution_method = "normalized_ambiguous"
                status = MetadataStatus.AMBIGUOUS
            elif len(title_matches) == 1:
                entry = next(iter(title_matches.values()))
                if (
                    paper.doi_candidate
                    and entry.doi
                    and entry.doi.lower() != paper.doi_candidate.lower()
                ):
                    resolution_method = "normalized_conflict"
                    status = MetadataStatus.AMBIGUOUS
                else:
                    matched_bib = entry
                    resolution_method = "normalized_title"
                    match_tier = "normalized"
                    status = MetadataStatus.OBSERVED

        # Cross-reference graph node if not yet resolved
        if matched_graph is None and matched_bib is not None:
            if matched_bib.key in graph_by_id:
                matched_graph = graph_by_id[matched_bib.key]
            elif matched_bib.key in graph_by_citekey:
                matched_graph = graph_by_citekey[matched_bib.key]

        # Title consistency check if both titles available
        if matched_bib and matched_bib.title and paper.title_candidate:
            norm_cand = _normalize_title_for_comparison(paper.title_candidate)
            norm_bib = _normalize_title_for_comparison(matched_bib.title)
            # If one is not a substring or significant match with the other
            if norm_cand not in norm_bib and norm_bib not in norm_cand:
                inconsistent_titles.append(
                    {
                        "source_path": paper.source_relative_path,
                        "bib_key": matched_bib.key,
                        "title_candidate": paper.title_candidate,
                        "bib_title": matched_bib.title,
                    }
                )

        bib_key = matched_bib.key if matched_bib else None
        graph_node_id = str(matched_graph["id"]) if matched_graph and "id" in matched_graph else None
        doi = matched_bib.doi if matched_bib else None
        final_title = matched_bib.title if matched_bib else paper.title_candidate

        if matched_bib:
            mapped_bib_keys.add(matched_bib.key)
        if matched_graph and "id" in matched_graph:
            mapped_graph_node_ids.add(str(matched_graph["id"]))
        if matched_bib or matched_graph:
            mapped_sources.add(paper.source_relative_path)

        mapped_entries.append(
            SourceMappingEntry(
                paper_path=paper.source_relative_path,
                content_sha256=paper.content_sha256,
                bib_key=bib_key,
                graph_node_id=graph_node_id,
                doi=doi,
                title=final_title,
                resolution_method=resolution_method,
                match_tier=match_tier,
                status=status,
            )
        )

    # 3. Gather unmapped sources, bib keys, and graph nodes
    unmapped_sources = sorted(
        [
            p.source_relative_path
            for p in candidate_papers
            if p.source_relative_path not in mapped_sources
        ]
    )

    unmapped_bib_keys = sorted(
        [b.key for b in bib_entries if b.key not in mapped_bib_keys]
    )

    unmapped_graph_nodes = sorted(
        [str(n["id"]) for n in graph_nodes if "id" in n and str(n["id"]) not in mapped_graph_node_ids]
    )

    mapping_report = SourceMappingReport(
        total_candidate_papers=len(candidate_papers),
        total_bib_entries=len(bib_entries),
        total_graph_paper_nodes=len(graph_nodes),
        mapped_papers=mapped_entries,
        unmapped_sources=unmapped_sources,
        unmapped_bib_keys=unmapped_bib_keys,
        unmapped_graph_nodes=unmapped_graph_nodes,
    )

    conflicts_count = (
        len(duplicate_bib_keys)
        + len(conflicting_dois)
        + len(inconsistent_titles)
        + len(alias_conflicts)
    )

    conflict_report = MetadataConflictReport(
        duplicate_bib_keys=sorted(list(set(duplicate_bib_keys))),
        conflicting_dois=conflicting_dois,
        inconsistent_titles=inconsistent_titles,
        alias_conflicts=alias_conflicts,
        conflicts_found=conflicts_count,
    )

    return mapping_report, conflict_report
