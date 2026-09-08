from __future__ import annotations

from pathlib import Path

from vibereview.library.bibliography import load_bibliography
from vibereview.library.git_source import PinnedGitSource
from vibereview.library.graph import ReadOnlyGraphAdapter
from vibereview.library.inventory import (
    build_library_inventory,
    extract_citekey_candidate,
)
from vibereview.library.models import DocumentKind, GraphSchemaStatus, LibraryConfig


def test_inventory_and_metadata_use_pinned_objects(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    _, config, _ = synthetic_library
    source = PinnedGitSource.open(config)
    records, excluded = build_library_inventory(source, config)
    candidates = [
        record
        for record in records
        if record.document_kind is DocumentKind.CANDIDATE_PAPER_MARKDOWN
    ]
    assert [item.source_relative_path for item in candidates] == [
        "papers/Writer - 2023 - Beta.md",
        "papers/Writer - 2024 - Alpha.md",
    ]
    assert candidates[1].bibliography_key == "Alpha2024"
    assert any(item.source_relative_path == "README.md" for item in excluded)

    entries, duplicates = load_bibliography(source, config)
    assert duplicates == []
    assert entries[0].key == "Alpha2024"
    assert entries[0].doi == "doi:10.5555/synthetic.alpha"


def test_graph_adapter_uses_supplied_pinned_bytes(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    _, config, _ = synthetic_library
    source = PinnedGitSource.open(config)
    adapter = ReadOnlyGraphAdapter.from_source(source, config.graph_path or "")
    report = adapter.inspect()
    assert report.schema_status is GraphSchemaStatus.SUPPORTED
    assert report.node_count == 1
    assert [hit["id"] for hit in adapter.find_candidates("lattice signal")] == [
        "alpha-node"
    ]


def test_invalid_graph_remains_an_unsupported_runtime_input() -> None:
    adapter = ReadOnlyGraphAdapter(b'{"nodes": []}', "graph.json")
    assert adapter.inspect().schema_status is GraphSchemaStatus.UNSUPPORTED_GRAPH_SCHEMA


def test_citekey_prefix_is_measured_after_complete_utf8_decode() -> None:
    content = (("é" * 2047) + "€\\cite{BoundaryKey}\n").encode("utf-8")
    assert content[4094:4097] == "€".encode("utf-8")
    assert extract_citekey_candidate(content) == "BoundaryKey"
