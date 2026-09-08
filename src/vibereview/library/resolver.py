"""Crosswalk resolver between paper Markdown, bibliography, and graph nodes (L0)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from vibereview.ids import Sha256
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
    import re

    cleaned = re.sub(r"[^\w\s]", "", title.lower())
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

    for b in bib_entries:
        if b.file_path:
            raw_fname = b.file_path.replace("\\", "/").split("/")[-1]
            stem = Path(raw_fname).stem.lower()
            bib_by_stem.setdefault(stem, []).append(b)

        if b.doi:
            doi_to_bib.setdefault(b.doi.lower(), []).append(b)

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
        status = MetadataStatus.MISSING

        # Check Step 4: Human-reviewed alias first if present
        if content_hash in aliases:
            alias_target = aliases[content_hash]
            if alias_target in bib_by_key:
                matched_bib = bib_by_key[alias_target]
                resolution_method = "alias"
                status = MetadataStatus.OBSERVED
            if alias_target in graph_by_id:
                matched_graph = graph_by_id[alias_target]
                resolution_method = "alias"
                status = MetadataStatus.OBSERVED

        # Check Step 1: Exact stable citekey
        if matched_bib is None and paper.bibliography_key:
            citekey = paper.bibliography_key
            if citekey in bib_by_key:
                matched_bib = bib_by_key[citekey]
                resolution_method = "exact_citekey"
                status = MetadataStatus.OBSERVED
            if citekey in graph_by_id:
                matched_graph = graph_by_id[citekey]
            elif citekey in graph_by_citekey:
                matched_graph = graph_by_citekey[citekey]

        # Check Step 3: Exact stored relative source path / stem
        if matched_bib is None and paper_stem in bib_by_stem:
            candidates = bib_by_stem[paper_stem]
            if len(candidates) == 1:
                matched_bib = candidates[0]
                resolution_method = "exact_path"
                status = MetadataStatus.OBSERVED
            elif len(candidates) > 1:
                resolution_method = "ambiguous_path"
                status = MetadataStatus.AMBIGUOUS

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
    )

    conflict_report = MetadataConflictReport(
        duplicate_bib_keys=sorted(list(set(duplicate_bib_keys))),
        conflicting_dois=conflicting_dois,
        inconsistent_titles=inconsistent_titles,
        conflicts_found=conflicts_count,
    )

    return mapping_report, conflict_report
