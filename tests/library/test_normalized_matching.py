from __future__ import annotations

from pathlib import Path

import pytest

from vibereview.library.bibliography import parse_bibtex_bytes
from vibereview.library.git_source import PinnedGitSource
from vibereview.library.inventory import build_library_inventory, extract_doi_candidate
from vibereview.library.models import (
    CorpusSelectionError,
    LibraryConfig,
    MetadataStatus,
)
from vibereview.library.resolver import resolve_source_mappings
from vibereview.library.selection import import_selected_corpus
from vibereview.runtime.repository import GenerationStore

from test_corpus_import import advance_source_pin, make_selection
from test_source_aliases import make_bib, make_paper


def test_extract_doi_candidate_normalizes_url_and_prefix_forms() -> None:
    assert (
        extract_doi_candidate(b"# T\n\nhttps://doi.org/10.5555/Synthetic.Alpha.\n")
        == "doi:10.5555/synthetic.alpha"
    )
    assert (
        extract_doi_candidate("DOI: 10.5555/synthetic.beta\n".encode())
        == "doi:10.5555/synthetic.beta"
    )
    assert extract_doi_candidate(b"# No identifier here\n") is None


def test_normalized_title_match_is_recorded_as_advisory() -> None:
    paper = make_paper("papers/Beta.md", "b" * 64, title="Beta:  A STUDY")
    bib = make_bib("Beta2023", title="Beta a study")
    mapping, conflicts = resolve_source_mappings([paper], [bib], [], [])
    entry = mapping.mapped_papers[0]
    assert entry.bib_key == "Beta2023"
    assert entry.resolution_method == "normalized_title"
    assert entry.match_tier == "normalized"
    assert entry.status is MetadataStatus.OBSERVED
    assert conflicts.conflicts_found == 0


def test_normalized_title_match_is_diacritic_insensitive_but_stays_visible() -> None:
    paper = make_paper("papers/Beta.md", "b" * 64, title="Béta Study")
    bib = make_bib("Beta2023", title="Beta Study")
    mapping, conflicts = resolve_source_mappings([paper], [bib], [], [])
    entry = mapping.mapped_papers[0]
    assert entry.bib_key == "Beta2023"
    assert entry.match_tier == "normalized"
    # The legacy surface-title consistency check still records the visible
    # difference; the advisory tier never hides it.
    assert len(conflicts.inconsistent_titles) == 1


def test_normalized_doi_match() -> None:
    paper = make_paper("papers/Beta.md", "b" * 64, doi="doi:10.5555/synthetic.beta")
    bib = make_bib("Beta2023", doi="doi:10.5555/synthetic.beta")
    mapping, _ = resolve_source_mappings([paper], [bib], [], [])
    entry = mapping.mapped_papers[0]
    assert entry.bib_key == "Beta2023"
    assert entry.resolution_method == "normalized_doi"
    assert entry.match_tier == "normalized"


def test_matching_title_with_conflicting_doi_is_a_conflict_not_a_match() -> None:
    paper = make_paper(
        "papers/Beta.md", "b" * 64, title="Beta", doi="doi:10.5555/synthetic.x"
    )
    bib = make_bib("Beta2023", title="Beta", doi="doi:10.5555/synthetic.y")
    mapping, _ = resolve_source_mappings([paper], [bib], [], [])
    entry = mapping.mapped_papers[0]
    assert entry.bib_key is None
    assert entry.resolution_method == "normalized_conflict"
    assert entry.match_tier == "none"
    assert entry.status is MetadataStatus.AMBIGUOUS
    assert mapping.unmapped_sources == ["papers/Beta.md"]


def test_doi_and_title_pointing_at_different_entries_is_a_conflict() -> None:
    paper = make_paper(
        "papers/Beta.md", "b" * 64, title="Beta", doi="doi:10.5555/synthetic.a"
    )
    bibs = [
        make_bib("A2020", doi="doi:10.5555/synthetic.a"),
        make_bib("B2021", title="Beta"),
    ]
    mapping, _ = resolve_source_mappings([paper], bibs, [], [])
    entry = mapping.mapped_papers[0]
    assert entry.bib_key is None
    assert entry.resolution_method == "normalized_conflict"
    assert entry.status is MetadataStatus.AMBIGUOUS


def test_multiple_normalized_title_matches_are_ambiguous() -> None:
    paper = make_paper("papers/Beta.md", "b" * 64, title="Beta")
    bibs = [
        make_bib("Beta2023a", title="Beta", doi="doi:10.5555/synthetic.a"),
        make_bib("Beta2023b", title="BETA", doi="doi:10.5555/synthetic.b"),
    ]
    mapping, _ = resolve_source_mappings([paper], bibs, [], [])
    entry = mapping.mapped_papers[0]
    assert entry.bib_key is None
    assert entry.resolution_method == "normalized_ambiguous"
    assert entry.status is MetadataStatus.AMBIGUOUS


def test_normalized_tier_never_overrides_exact_or_alias_tiers() -> None:
    paper = make_paper(
        "papers/Alpha.md",
        "a" * 64,
        citekey="Alpha2024",
        title="Beta",
        doi="doi:10.5555/synthetic.beta",
    )
    bibs = [
        make_bib("Alpha2024", title="Synthetic Alpha Study"),
        make_bib("Beta2023", title="Beta", doi="doi:10.5555/synthetic.beta"),
    ]
    mapping, _ = resolve_source_mappings([paper], bibs, [], [])
    entry = mapping.mapped_papers[0]
    assert entry.bib_key == "Alpha2024"
    assert entry.match_tier == "exact"

    mapping, _ = resolve_source_mappings(
        [paper], bibs, [], [], aliases={paper.content_sha256: "Beta2023"}
    )
    entry = mapping.mapped_papers[0]
    # Alias conflicts with the exact citekey and is reported, not applied.
    assert entry.bib_key is None
    assert entry.resolution_method == "alias_conflict"


def test_normalized_match_does_not_authorize_import_metadata(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    repository, config, _ = synthetic_library
    bibliography = repository / "references.bib"
    bibliography.write_text(
        bibliography.read_text(encoding="utf-8")
        + (
            "@article{Beta2023,\n"
            " title={Beta},\n"
            " year={2023},\n"
            " doi={10.5555/synthetic.beta}\n"
            "}\n"
        ),
        encoding="utf-8",
    )
    revised = advance_source_pin(repository, config, "normalized-title fixture")
    source = PinnedGitSource.open(revised)
    inventory, _ = build_library_inventory(source, revised)
    candidates = [
        record
        for record in inventory
        if record.document_kind.value == "candidate_paper_markdown"
    ]
    beta = next(
        record
        for record in candidates
        if record.source_relative_path.endswith("Beta.md")
    )
    bibs, duplicates = parse_bibtex_bytes(source.read_path(revised.bibliography or ""))
    mapping, _ = resolve_source_mappings(candidates, bibs, duplicates, [])
    beta_entry = next(
        entry
        for entry in mapping.mapped_papers
        if entry.paper_path == beta.source_relative_path
    )
    assert beta_entry.match_tier == "normalized"
    assert beta_entry.bib_key == "Beta2023"

    review_root = tmp_path / "review-normalized"
    result = import_selected_corpus(
        review_root,
        make_selection(revised),
        revised,
        public_repository_root=tmp_path / "public-repository",
    )
    _, snapshot, _ = GenerationStore(review_root).load_current()
    beta_paper = next(paper for paper in snapshot.papers if paper.title == "Beta")
    assert result.generation == 1
    # The advisory normalized match must not enrich canonical metadata.
    assert beta_paper.doi is None
    assert beta_paper.title == "Beta"


def test_normalized_ambiguity_on_selected_paper_fails_import_closed(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    repository, config, _ = synthetic_library
    bibliography = repository / "references.bib"
    bibliography.write_text(
        bibliography.read_text(encoding="utf-8")
        + (
            "@article{Beta2023a,\n"
            " title={Beta},\n"
            " year={2023},\n"
            " doi={10.5555/synthetic.beta.a}\n"
            "}\n"
            "@article{Beta2023b,\n"
            " title={Beta},\n"
            " year={2023},\n"
            " doi={10.5555/synthetic.beta.b}\n"
            "}\n"
        ),
        encoding="utf-8",
    )
    revised = advance_source_pin(repository, config, "ambiguous normalized fixture")
    review_root = tmp_path / "must-not-exist"
    with pytest.raises(CorpusSelectionError, match="conflicting or ambiguous"):
        import_selected_corpus(
            review_root,
            make_selection(revised),
            revised,
            public_repository_root=tmp_path / "public-repository",
        )
    assert not review_root.exists()
