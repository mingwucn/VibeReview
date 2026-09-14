from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from vibereview.library.aliases import (
    SOURCE_ALIASES_FORMAT_VERSION,
    SourceAliasDocument,
    SourceAliasEntry,
    load_source_aliases,
)
from vibereview.library.git_source import PinnedGitSource
from vibereview.library.inspect import run_inspection
from vibereview.library.inventory import build_library_inventory
from vibereview.library.models import (
    BibEntryRecord,
    CorpusSelectionError,
    DocumentKind,
    LibraryConfig,
    LibraryDocumentRecord,
    MetadataStatus,
    PathSecurityError,
    SourceMappingReport,
    SourceStatus,
)
from vibereview.library.project_config import (
    ProjectLibraryConfig,
    ProjectMetadata,
    ReviewProjectConfig,
)
from vibereview.library.resolver import resolve_source_mappings
from vibereview.library.selection import import_selected_corpus
from vibereview.runtime.repository import GenerationStore

from test_corpus_import import advance_source_pin, make_selection


def make_paper(
    path: str,
    sha_hex: str,
    citekey: str | None = None,
    title: str | None = None,
    doi: str | None = None,
) -> LibraryDocumentRecord:
    return LibraryDocumentRecord(
        library_id="synthetic",
        source_commit="0" * 40,
        source_relative_path=path,
        git_blob_id="1" * 40,
        content_sha256=f"sha256:{sha_hex}",
        size_bytes=10,
        document_kind=DocumentKind.CANDIDATE_PAPER_MARKDOWN,
        title_candidate=title,
        bibliography_key=citekey,
        doi_candidate=doi,
        metadata_status=(
            MetadataStatus.OBSERVED if citekey else MetadataStatus.MISSING
        ),
        source_status=SourceStatus.CANDIDATE,
    )


def make_bib(
    key: str,
    title: str | None = None,
    doi: str | None = None,
    file_path: str | None = None,
) -> BibEntryRecord:
    return BibEntryRecord(
        key=key, entry_type="article", title=title, doi=doi, file_path=file_path
    )


def make_alias_entry(sha_hex: str, bib_key: str) -> SourceAliasEntry:
    return SourceAliasEntry(
        content_sha256=f"sha256:{sha_hex}",
        bib_key=bib_key,
        rationale="synthetic operator adjudication",
    )


def make_operator_config(
    config: LibraryConfig, selection_path: Path
) -> ReviewProjectConfig:
    return ReviewProjectConfig(
        project=ProjectMetadata(
            name="synthetic",
            topic="synthetic signal",
            scope="fixture Markdown only",
        ),
        library=ProjectLibraryConfig(
            id=config.library_id,
            path=config.library_path,
            superproject_path=config.superproject_path,
            gitlink_path=config.gitlink_path,
            expected_commit=config.expected_commit,
            markdown_root=config.markdown_root,
            bibliography=config.bibliography,
            graph_path=config.graph_path,
            selection_manifest=selection_path,
        ),
    )


def test_alias_document_round_trip_and_strict_parsing(tmp_path: Path) -> None:
    document = SourceAliasDocument(entries=[make_alias_entry("a" * 64, "Beta2023")])
    assert document.format_version == SOURCE_ALIASES_FORMAT_VERSION
    path = tmp_path / "aliases.json"
    path.write_text(document.model_dump_json(indent=2), encoding="utf-8")
    loaded = load_source_aliases(path)
    assert loaded == document
    assert loaded.as_mapping() == {f"sha256:{'a' * 64}": "Beta2023"}

    with pytest.raises(ValidationError):
        SourceAliasDocument(
            entries=[make_alias_entry("a" * 64, "K1"), make_alias_entry("a" * 64, "K2")]
        )
    with pytest.raises(ValidationError):
        SourceAliasDocument.model_validate(
            {"format_version": "vibereview-source-aliases-0", "entries": []}
        )
    with pytest.raises(ValidationError):
        SourceAliasEntry(
            content_sha256=f"sha256:{'b' * 64}",
            bib_key="K1",
            rationale="rationale",
            unexpected="field",
        )
    with pytest.raises(FileNotFoundError):
        load_source_aliases(tmp_path / "missing.json")


def test_alias_resolves_otherwise_unmatched_candidate() -> None:
    paper = make_paper("papers/Beta.md", "b" * 64)
    bib = make_bib("Beta2023", title="Synthetic Beta Study")
    mapping, conflicts = resolve_source_mappings(
        [paper], [bib], [], [], aliases={paper.content_sha256: "Beta2023"}
    )
    entry = mapping.mapped_papers[0]
    assert entry.bib_key == "Beta2023"
    assert entry.resolution_method == "alias"
    assert entry.match_tier == "alias"
    assert entry.status is MetadataStatus.OBSERVED
    assert conflicts.conflicts_found == 0
    assert mapping.unmapped_sources == []


def test_alias_agreeing_with_exact_citekey_is_alias_tier_without_conflict() -> None:
    paper = make_paper("papers/Alpha.md", "a" * 64, citekey="Alpha2024")
    bib = make_bib("Alpha2024", title="Synthetic Alpha Study")
    mapping, conflicts = resolve_source_mappings(
        [paper], [bib], [], [], aliases={paper.content_sha256: "Alpha2024"}
    )
    entry = mapping.mapped_papers[0]
    assert entry.bib_key == "Alpha2024"
    assert entry.match_tier == "alias"
    assert conflicts.alias_conflicts == []


def test_alias_conflicting_with_exact_citekey_is_reported_not_applied() -> None:
    paper = make_paper("papers/Alpha.md", "a" * 64, citekey="Alpha2024")
    bibs = [
        make_bib("Alpha2024", title="Synthetic Alpha Study"),
        make_bib("Other2020", title="Other Synthetic Study"),
    ]
    mapping, conflicts = resolve_source_mappings(
        [paper], bibs, [], [], aliases={paper.content_sha256: "Other2020"}
    )
    entry = mapping.mapped_papers[0]
    assert entry.bib_key is None
    assert entry.resolution_method == "alias_conflict"
    assert entry.match_tier == "none"
    assert entry.status is MetadataStatus.AMBIGUOUS
    assert len(conflicts.alias_conflicts) == 1
    assert conflicts.alias_conflicts[0]["exact_bib_key"] == "Alpha2024"
    assert conflicts.conflicts_found == 1
    assert mapping.unmapped_sources == ["papers/Alpha.md"]


def test_dangling_alias_is_reported_and_falls_through_to_other_tiers() -> None:
    paper = make_paper("papers/Beta.md", "b" * 64, title="Beta")
    bib = make_bib("Beta2023", title="Beta")
    mapping, conflicts = resolve_source_mappings(
        [paper], [bib], [], [], aliases={paper.content_sha256: "MissingKey"}
    )
    entry = mapping.mapped_papers[0]
    assert len(conflicts.alias_conflicts) == 1
    assert "absent from the pinned bibliography" in conflicts.alias_conflicts[0]["reason"]
    # The dangling alias must not block the advisory normalized tier.
    assert entry.bib_key == "Beta2023"
    assert entry.match_tier == "normalized"


def test_inspection_applies_alias_document_and_records_it(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    _, config, _ = synthetic_library
    source = PinnedGitSource.open(config)
    inventory, _ = build_library_inventory(source, config)
    beta = next(
        record
        for record in inventory
        if record.source_relative_path.endswith("Beta.md")
    )
    document = SourceAliasDocument(
        entries=[make_alias_entry(beta.content_sha256.removeprefix("sha256:"), "Alpha2024")]
    )
    aliases_path = tmp_path / "operator" / "aliases.json"
    aliases_path.parent.mkdir(parents=True)
    aliases_path.write_text(document.model_dump_json(indent=2), encoding="utf-8")
    public_root = tmp_path / "public"
    public_root.mkdir()
    output = tmp_path / "audit"

    summary = run_inspection(
        make_operator_config(config, tmp_path / "operator" / "selection.json"),
        output,
        public_repository_root=public_root,
        aliases_path=aliases_path,
    )

    assert summary["source_aliases_applied"] == 1
    recorded = SourceAliasDocument.model_validate_json(
        (output / "source_aliases_applied.json").read_bytes()
    )
    assert recorded == document
    mapping = SourceMappingReport.model_validate_json(
        (output / "source_mapping.json").read_bytes()
    )
    beta_entry = next(
        entry for entry in mapping.mapped_papers if entry.paper_path == beta.source_relative_path
    )
    assert beta_entry.bib_key == "Alpha2024"
    assert beta_entry.match_tier == "alias"
    assert not list(public_root.rglob("*.json"))


def test_inspection_rejects_alias_document_inside_public_repository(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    _, config, _ = synthetic_library
    public_root = tmp_path / "public"
    public_root.mkdir()
    aliases_path = public_root / "aliases.json"
    aliases_path.write_text(
        SourceAliasDocument().model_dump_json(indent=2), encoding="utf-8"
    )
    with pytest.raises(PathSecurityError, match="source aliases"):
        run_inspection(
            make_operator_config(config, tmp_path / "operator" / "selection.json"),
            tmp_path / "audit",
            public_repository_root=public_root,
            aliases_path=aliases_path,
        )


def test_import_threads_aliases_into_resolution(
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
    revised = advance_source_pin(repository, config, "alias target fixture")
    source = PinnedGitSource.open(revised)
    inventory, _ = build_library_inventory(source, revised)
    beta = next(
        record
        for record in inventory
        if record.source_relative_path.endswith("Beta.md")
    )
    review_root = tmp_path / "review-alias"
    import_selected_corpus(
        review_root,
        make_selection(revised),
        revised,
        public_repository_root=tmp_path / "public-repository",
        aliases={beta.content_sha256: "Beta2023"},
    )
    _, snapshot, _ = GenerationStore(review_root).load_current()
    assert len(snapshot.papers) == 2
    beta_paper = next(paper for paper in snapshot.papers if paper.title == "Beta")
    assert beta_paper.doi == "doi:10.5555/synthetic.beta"
    assert beta_paper.year == 2023


def test_import_fails_closed_on_alias_conflict_with_exact_citekey(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    repository, config, _ = synthetic_library
    bibliography = repository / "references.bib"
    bibliography.write_text(
        bibliography.read_text(encoding="utf-8")
        + "@article{Other2020,\n title={Other Synthetic Study},\n year={2020}\n}\n",
        encoding="utf-8",
    )
    revised = advance_source_pin(repository, config, "second bib entry fixture")
    source = PinnedGitSource.open(revised)
    inventory, _ = build_library_inventory(source, revised)
    alpha = next(
        record
        for record in inventory
        if record.source_relative_path.endswith("Alpha.md")
    )
    review_root = tmp_path / "must-not-exist"
    with pytest.raises(CorpusSelectionError, match="identity conflicts"):
        import_selected_corpus(
            review_root,
            make_selection(revised),
            revised,
            public_repository_root=tmp_path / "public-repository",
            aliases={alpha.content_sha256: "Other2020"},
        )
    assert not review_root.exists()
