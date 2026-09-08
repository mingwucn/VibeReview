from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from vibereview.enums import RetrievalIntent
from vibereview.library.git_source import PinnedGitSource
from vibereview.library.graph import ReadOnlyGraphAdapter
from vibereview.library.inventory import build_library_inventory
from vibereview.library.models import (
    CorpusIntegrityError,
    CorpusSelectionDocument,
    CorpusSelectionManifest,
    LibraryConfig,
)
from vibereview.library.retrieval import (
    CandidateHitOrigin,
    DeterministicTextRetriever,
    GraphAssistedRetriever,
    RetrievalLedger,
    UnifiedRetrievalCoordinator,
)
from vibereview.library.selection import (
    import_selected_corpus as _import_selected_corpus,
    load_corpus_lock,
)


def import_selected_corpus(
    review_root: Path,
    manifest: CorpusSelectionManifest,
    config: LibraryConfig,
) -> object:
    return _import_selected_corpus(
        review_root,
        manifest,
        config,
        public_repository_root=review_root.parent / "public-repository",
    )



def make_selection(config: LibraryConfig) -> CorpusSelectionManifest:
    inventory, _ = build_library_inventory(PinnedGitSource.open(config), config)
    return CorpusSelectionManifest(
        library_id=config.library_id,
        source_commit=config.expected_commit,
        documents=[
            CorpusSelectionDocument(
                source_relative_path=record.source_relative_path,
                content_sha256=record.content_sha256,
                decision="include",
                role="primary",
                accepted_by="synthetic-test",
                accepted_at="2030-01-01T00:00:00Z",
                reason="fixture coverage",
            )
            for record in inventory
            if record.document_kind.value == "candidate_paper_markdown"
        ],
    )


def imported_review(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> tuple[Path, LibraryConfig, PinnedGitSource]:
    _, config, _ = synthetic_library
    review_root = tmp_path / "review"
    import_selected_corpus(review_root, make_selection(config), config)
    return review_root, config, PinnedGitSource.open(config)


def imported_review_with_graph(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
    graph: dict[str, object],
) -> tuple[Path, LibraryConfig, PinnedGitSource]:
    repository, config, _ = synthetic_library
    (repository / (config.graph_path or "graph.json")).write_text(
        json.dumps(graph, sort_keys=True), encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "synthetic graph case"],
        check=True,
        capture_output=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        [
            "git",
            "-C",
            str(config.superproject_path),
            "update-index",
            "--cacheinfo",
            f"160000,{commit},{config.gitlink_path}",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(config.superproject_path),
            "commit",
            "-m",
            "advance synthetic graph pin",
        ],
        check=True,
        capture_output=True,
    )
    revised = config.model_copy(update={"expected_commit": commit})
    review_root = tmp_path / "review"
    import_selected_corpus(review_root, make_selection(revised), revised)
    return review_root, revised, PinnedGitSource.open(revised)


def test_ensemble_returns_only_runtime_candidates(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    review_root, config, source = imported_review(synthetic_library, tmp_path)
    text = DeterministicTextRetriever(review_root)
    graph = GraphAssistedRetriever(
        ReadOnlyGraphAdapter.from_source(source, config.graph_path or ""), review_root
    )
    candidates, ledger = UnifiedRetrievalCoordinator(text, graph).retrieve(
        "lattice signal",
        "Q-C0001-SUP-01",
        RetrievalIntent.SUPPORT,
    )

    assert candidates
    assert candidates[0].origin is CandidateHitOrigin.BOTH
    assert candidates[0].upstream_node_id == "alpha-node"
    assert candidates[0].upstream_relation == "supports"
    payload = candidates[0].model_dump(mode="json")
    assert "candidate_key" in payload
    assert payload["offset_unit"] == "unicode_code_points"
    assert "span_id" not in payload
    assert "disposition" not in payload
    assert ledger.valid_candidates == len(candidates)
    raw_text = (review_root / candidates[0].raw_md_path).read_text(encoding="utf-8")
    assert raw_text[candidates[0].start_offset : candidates[0].end_offset] == candidates[0].source_text


def test_invalid_graph_locator_stays_in_runtime_ledger(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    graph_data = {
        "nodes": [
            {
                "id": "missing",
                "citekey": "Alpha2024",
                "label": "missing phrase",
                "attributes": {"support": [{"evidence": "not present in raw bytes"}]},
            }
        ],
        "edges": [],
    }
    review_root, config, source = imported_review_with_graph(
        synthetic_library, tmp_path, graph_data
    )
    adapter = ReadOnlyGraphAdapter.from_source(source, config.graph_path or "")
    text = DeterministicTextRetriever(review_root)
    graph = GraphAssistedRetriever(adapter, review_root)
    candidates, ledger = UnifiedRetrievalCoordinator(text, graph).retrieve(
        "missing phrase",
        "Q-C0001-CON-01",
        RetrievalIntent.CONTRADICTION,
    )
    assert candidates == []
    assert ledger.invalid_candidates == 1
    assert ledger.raw_hits[0].is_valid is False
    assert ledger.raw_hits[0].start_offset is None
    assert "exact raw Markdown slice" in (ledger.raw_hits[0].validation_error or "")


def test_lock_and_raw_bytes_are_rehashed_before_retrieval(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    review_root, _, _ = imported_review(synthetic_library, tmp_path)
    lock, _ = load_corpus_lock(review_root)
    raw_path = review_root / lock.papers[0].raw_md_path
    raw_path.chmod(0o644)
    raw_path.write_text("corrupt\n", encoding="utf-8")
    with pytest.raises(CorpusIntegrityError, match="hash mismatch"):
        DeterministicTextRetriever(review_root)


@pytest.mark.parametrize("replacement", ["symlink", "fifo"])
def test_retrieval_never_follows_or_blocks_on_replaced_raw_object(
    replacement: str,
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
) -> None:
    review_root, _, _ = imported_review(synthetic_library, tmp_path)
    lock, _ = load_corpus_lock(review_root)
    raw_path = review_root / lock.papers[0].raw_md_path
    content = raw_path.read_bytes()
    raw_path.parent.chmod(0o755)
    raw_path.unlink()
    if replacement == "symlink":
        outside = tmp_path / "same-content.md"
        outside.write_bytes(content)
        raw_path.symlink_to(outside)
    else:
        os.mkfifo(raw_path)

    with pytest.raises(CorpusIntegrityError, match="auxiliary hash"):
        DeterministicTextRetriever(review_root)


def test_graph_provenance_must_match_active_lock(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    review_root, config, source = imported_review(synthetic_library, tmp_path)
    adapter = ReadOnlyGraphAdapter(
        source.read_path(config.graph_path or ""),
        config.graph_path or "",
        library_id=config.library_id,
        source_commit="0" * 40,
    )
    with pytest.raises(CorpusIntegrityError, match="does not come from"):
        GraphAssistedRetriever(adapter, review_root)


def test_graph_content_and_path_must_match_unique_locked_object(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    review_root, config, _ = imported_review(synthetic_library, tmp_path)
    altered = ReadOnlyGraphAdapter(
        b'{"nodes": [], "edges": []}',
        config.graph_path or "",
        library_id=config.library_id,
        source_commit=config.expected_commit,
    )
    with pytest.raises(CorpusIntegrityError, match="path or content"):
        GraphAssistedRetriever(altered, review_root)


def test_mutating_exported_graph_nodes_cannot_change_retrieval_bytes(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    review_root, config, source = imported_review(synthetic_library, tmp_path)
    adapter = ReadOnlyGraphAdapter.from_source(source, config.graph_path or "")
    exported = adapter.graph_nodes()
    exported[0]["attributes"]["supports"][0]["evidence"] = "# Alpha"
    graph = GraphAssistedRetriever(adapter, review_root)
    hits = graph.search(
        "lattice signal", "Q-C0001-SUP-01", RetrievalIntent.SUPPORT
    )
    assert hits[0].source_text == "A lattice signal changes under load."


def test_mutating_inspection_report_cannot_spoof_forged_graph_hash(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    review_root, config, source = imported_review(synthetic_library, tmp_path)
    locked_adapter = ReadOnlyGraphAdapter.from_source(source, config.graph_path or "")
    forged = ReadOnlyGraphAdapter(
        b'{"nodes": [], "edges": []}',
        config.graph_path or "",
        library_id=config.library_id,
        source_commit=config.expected_commit,
    )
    report = forged.inspect()
    report.content_sha256 = locked_adapter.content_sha256
    with pytest.raises(CorpusIntegrityError, match="path or content"):
        GraphAssistedRetriever(forged, review_root)


def test_verified_corpus_views_cannot_be_mutated_to_forge_text_hits(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    review_root, _, _ = imported_review(synthetic_library, tmp_path)
    retriever = DeterministicTextRetriever(review_root)
    paper_id = next(iter(retriever.corpus.texts))
    with pytest.raises(AttributeError):
        retriever._corpus = object()  # type: ignore[assignment]
    with pytest.raises(TypeError):
        retriever.corpus.texts[paper_id] = (  # type: ignore[index]
            "forged/path.md",
            "sha256:" + "0" * 64,
            "forged lattice signal",
        )
    detached_lock = retriever.corpus.lock
    detached_lock.papers[0].raw_md_path = "forged/path.md"

    hits = retriever.search(
        "lattice signal", "Q-C0001-SUP-01", RetrievalIntent.SUPPORT
    )
    assert hits
    assert all(hit.raw_md_path != "forged/path.md" for hit in hits)
    assert all("forged" not in hit.source_text for hit in hits)


def test_graph_retrieval_rejects_subclasses_and_ignores_later_adapter_mutation(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    review_root, config, source = imported_review(synthetic_library, tmp_path)

    class ForgedAdapter(ReadOnlyGraphAdapter):
        def iter_candidates(self, query: str):
            yield {
                "id": "forged",
                "citekey": "Alpha2024",
                "label": query,
                "attributes": {"supports": [{"evidence": "# Alpha"}]},
            }

    with pytest.raises(CorpusIntegrityError, match="exact concrete"):
        GraphAssistedRetriever(
            ForgedAdapter(
                source.read_path(config.graph_path or ""),
                config.graph_path or "",
                library_id=config.library_id,
                source_commit=config.expected_commit,
            ),
            review_root,
        )

    adapter = ReadOnlyGraphAdapter.from_source(source, config.graph_path or "")
    retriever = GraphAssistedRetriever(adapter, review_root)
    with pytest.raises(AttributeError):
        retriever._graph_content = b"forged"  # type: ignore[misc]
    adapter._content = b'{"nodes": [], "edges": []}'
    adapter.iter_candidates = lambda query: iter(  # type: ignore[method-assign]
        [
            {
                "id": "forged",
                "citekey": "Alpha2024",
                "label": query,
                "attributes": {"supports": [{"evidence": "# Alpha"}]},
            }
        ]
    )
    hits = retriever.search(
        "lattice signal", "Q-C0001-SUP-01", RetrievalIntent.SUPPORT
    )
    assert hits[0].source_text == "A lattice signal changes under load."


def test_graph_retrieval_requires_graph_object_in_import_provenance(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    _, config, _ = synthetic_library
    without_graph = config.model_copy(update={"graph_path": None})
    review_root = tmp_path / "review-without-graph"
    import_selected_corpus(review_root, make_selection(without_graph), without_graph)
    source = PinnedGitSource.open(without_graph)
    adapter = ReadOnlyGraphAdapter.from_source(source, "graph.json")
    with pytest.raises(CorpusIntegrityError, match="exactly one graph"):
        GraphAssistedRetriever(adapter, review_root)


def test_repeated_exact_graph_text_is_ambiguous_and_ledger_only(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    graph_data = {
        "nodes": [
            {
                "id": "ambiguous",
                "citekey": "Alpha2024",
                "label": "ambiguous token",
                "attributes": {"support": [{"evidence": "A"}]},
            }
        ],
        "edges": [],
    }
    review_root, config, source = imported_review_with_graph(
        synthetic_library, tmp_path, graph_data
    )
    adapter = ReadOnlyGraphAdapter.from_source(source, config.graph_path or "")
    text = DeterministicTextRetriever(review_root)
    graph = GraphAssistedRetriever(adapter, review_root)
    candidates, ledger = UnifiedRetrievalCoordinator(text, graph).retrieve(
        "ambiguous token",
        "Q-C0001-SUP-01",
        RetrievalIntent.SUPPORT,
    )
    assert candidates == []
    assert ledger.invalid_candidates == 1
    assert "multiple times" in (ledger.raw_hits[0].validation_error or "")


def test_over_limit_text_candidate_is_bounded_and_ledger_only(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    review_root, _, _ = imported_review(synthetic_library, tmp_path)
    text = DeterministicTextRetriever(review_root)
    candidates, ledger = UnifiedRetrievalCoordinator(text).retrieve(
        "overflow",
        "Q-C0001-BND-01",
        RetrievalIntent.BOUNDARY,
    )
    assert candidates == []
    assert ledger.invalid_candidates == 1
    assert len(ledger.raw_hits[0].source_text) == 16_384
    assert ledger.raw_hits[0].start_offset is None


def test_invalid_diagnostic_does_not_consume_valid_top_k_capacity(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    review_root, _, _ = imported_review(synthetic_library, tmp_path)
    candidates, ledger = UnifiedRetrievalCoordinator(
        DeterministicTextRetriever(review_root)
    ).retrieve(
        "lattice overflow",
        "Q-C0001-SUP-01",
        RetrievalIntent.SUPPORT,
        top_k=1,
    )
    assert len(candidates) == 1
    assert "lattice signal" in candidates[0].source_text
    assert ledger.invalid_candidates == 1


def test_retrieval_ledger_rejects_tampered_counts_and_query_membership(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    review_root, _, _ = imported_review(synthetic_library, tmp_path)
    _, ledger = UnifiedRetrievalCoordinator(
        DeterministicTextRetriever(review_root)
    ).retrieve(
        "lattice signal",
        "Q-C0001-SUP-01",
        RetrievalIntent.SUPPORT,
    )
    payload = ledger.model_dump(mode="json")
    payload["total_candidates"] += 1
    with pytest.raises(ValueError, match="total"):
        RetrievalLedger.model_validate(payload)

    payload = ledger.model_dump(mode="json")
    payload["raw_hits"][0]["query_id"] = "Q-C0002-SUP-01"
    with pytest.raises(ValueError, match="another query"):
        RetrievalLedger.model_validate(payload)

    payload = ledger.model_dump(mode="json")
    payload["raw_hits"][0]["end_offset"] += 1
    with pytest.raises(ValueError, match="span exactly"):
        RetrievalLedger.model_validate(payload)

    payload = ledger.model_dump(mode="json")
    payload["raw_hits"][0]["source_span_hash"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="hash must match"):
        RetrievalLedger.model_validate(payload)


def test_retrieval_query_text_has_a_finite_input_budget(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    review_root, _, _ = imported_review(synthetic_library, tmp_path)
    retriever = DeterministicTextRetriever(review_root)
    with pytest.raises(ValueError, match="text budget"):
        retriever.search(
            "x" * 4_097,
            "Q-C0001-SUP-01",
            RetrievalIntent.SUPPORT,
        )


def test_crlf_paragraph_offsets_are_exact_unicode_code_point_slices(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    repository, config, _ = synthetic_library
    target = repository / "papers" / "Writer - 2024 - Alpha.md"
    target.write_bytes(
        b"\\cite{Alpha2024}\r\n# Alpha\r\n\r\n"
        b"A lattice signal changes under load.\r\n"
    )
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "CRLF paper fixture"],
        check=True,
        capture_output=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        [
            "git",
            "-C",
            str(config.superproject_path),
            "update-index",
            "--cacheinfo",
            f"160000,{commit},{config.gitlink_path}",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(config.superproject_path),
            "commit",
            "-m",
            "advance CRLF fixture pin",
        ],
        check=True,
        capture_output=True,
    )
    revised = config.model_copy(update={"expected_commit": commit})
    review_root = tmp_path / "review-crlf"
    import_selected_corpus(review_root, make_selection(revised), revised)
    hits = DeterministicTextRetriever(review_root).search(
        "lattice signal", "Q-C0001-SUP-01", RetrievalIntent.SUPPORT
    )
    hit = next(item for item in hits if item.is_valid)
    lock, _ = load_corpus_lock(review_root)
    raw_path = next(
        review_root / paper.raw_md_path
        for paper in lock.papers
        if paper.paper_id == hit.paper_id
    )
    with raw_path.open("r", encoding="utf-8", newline="") as handle:
        raw = handle.read()
    assert hit.start_offset is not None and hit.end_offset is not None
    assert raw[hit.start_offset : hit.end_offset] == hit.source_text
