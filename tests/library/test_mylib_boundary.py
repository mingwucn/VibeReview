from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tomllib
from typing import Literal

import pytest

from vibereview.library.bibliography import load_bibliography
from vibereview.library.git_source import (
    PinnedGitSource,
    compute_content_sha256,
    snapshot_library_state,
)
from vibereview.library.graph import ReadOnlyGraphAdapter
from vibereview.library.inventory import build_library_inventory
from vibereview.library.models import (
    BibEntryRecord,
    CommitMismatchError,
    CorpusSelectionDocument,
    CorpusSelectionManifest,
    DirtyWorkingTreeError,
    DocumentKind,
    ExcludedEntryRecord,
    GraphSchemaReport,
    LibraryConfig,
    LibraryDocumentRecord,
    LibraryStateSnapshot,
    UnsupportedGitObjectError,
    UpstreamIntegrityReport,
)
from vibereview.library.selection import validate_selection_manifest


pytestmark = [
    pytest.mark.external_corpus,
    pytest.mark.skipif(
        os.environ.get("VIBEREVIEW_RUN_EXTERNAL_CORPUS") != "1",
        reason="requires explicit VIBEREVIEW_RUN_EXTERNAL_CORPUS=1 opt-in",
    ),
]

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_PIN_RECORD = _REPOSITORY_ROOT / "configs" / "libraries" / "mylib.toml"
_REVIEWER = "vibereview-boundary-verification"
_REVIEWED_AT = "2026-09-14T00:00:00Z"
_EXCLUSION_REASON = "software-prepopulated exclusion pending operator review"
_INCLUSION_REASON = "boundary verification inclusion probe"


@dataclass(frozen=True)
class _BoundaryRun:
    config: LibraryConfig
    before: LibraryStateSnapshot
    after: LibraryStateSnapshot
    source: PinnedGitSource
    inventory: list[LibraryDocumentRecord]
    excluded: list[ExcludedEntryRecord]
    bibliography: list[BibEntryRecord]
    duplicate_bib_keys: list[str]
    graph_report: GraphSchemaReport | None


def _load_pinned_config() -> LibraryConfig:
    pin = tomllib.loads(_PIN_RECORD.read_text(encoding="utf-8"))
    library = pin["library"]
    sources = pin["sources"]
    graph = pin.get("graph", {})
    library_path = (_REPOSITORY_ROOT / library["path"]).resolve()
    if not (library_path / ".git").exists():
        pytest.skip("the pinned library submodule is not initialized")
    return LibraryConfig(
        library_id=library["id"],
        library_path=library_path,
        superproject_path=_REPOSITORY_ROOT,
        gitlink_path=library["path"],
        expected_commit=library["expected_commit"],
        markdown_root=sources["markdown_root"],
        bibliography=sources["bibliography"],
        graph_path=graph.get("path"),
        require_clean_worktree=bool(library["require_clean_tracked_files"]),
    )


@pytest.fixture(scope="module")
def pinned_config() -> LibraryConfig:
    return _load_pinned_config()


@pytest.fixture(scope="module")
def boundary_run(pinned_config: LibraryConfig) -> _BoundaryRun:
    before = snapshot_library_state(pinned_config)
    try:
        source = PinnedGitSource.open(pinned_config)
    except DirtyWorkingTreeError as exc:
        pytest.skip(f"operator checkout is not clean and must be restored: {exc}")
    except CommitMismatchError as exc:
        pytest.skip(f"operator checkout does not match the pin record: {exc}")
    inventory, excluded = build_library_inventory(source, pinned_config)
    bibliography, duplicate_bib_keys = load_bibliography(source, pinned_config)
    graph_report = (
        ReadOnlyGraphAdapter.from_source(source, pinned_config.graph_path).inspect()
        if pinned_config.graph_path is not None
        else None
    )
    after = snapshot_library_state(pinned_config)
    return _BoundaryRun(
        config=pinned_config,
        before=before,
        after=after,
        source=source,
        inventory=inventory,
        excluded=excluded,
        bibliography=bibliography,
        duplicate_bib_keys=duplicate_bib_keys,
        graph_report=graph_report,
    )


def _candidates(boundary_run: _BoundaryRun) -> list[LibraryDocumentRecord]:
    return sorted(
        (
            record
            for record in boundary_run.inventory
            if record.document_kind is DocumentKind.CANDIDATE_PAPER_MARKDOWN
        ),
        key=lambda record: record.source_relative_path,
    )


def _decision(
    record: LibraryDocumentRecord, decision: Literal["include", "exclude"]
) -> CorpusSelectionDocument:
    return CorpusSelectionDocument(
        source_relative_path=record.source_relative_path,
        content_sha256=record.content_sha256,
        decision=decision,
        role="boundary verification",
        accepted_by=_REVIEWER,
        accepted_at=_REVIEWED_AT,
        reason=_INCLUSION_REASON if decision == "include" else _EXCLUSION_REASON,
    )


def _manifest(
    config: LibraryConfig, documents: list[CorpusSelectionDocument]
) -> CorpusSelectionManifest:
    return CorpusSelectionManifest(
        library_id=config.library_id,
        source_commit=config.expected_commit,
        documents=documents,
    )


def test_pin_record_gitlink_and_library_head_agree(boundary_run: _BoundaryRun) -> None:
    report: UpstreamIntegrityReport = boundary_run.source.integrity
    expected = boundary_run.config.expected_commit
    assert report.expected_commit == expected
    assert report.checked_out_commit == expected
    assert report.gitlink_object_id == expected
    assert report.commit_match is True
    assert report.gitlink_mode == "160000"
    assert report.gitlink_valid is True
    assert report.is_clean is True
    assert report.untracked_files_count == 0
    assert report.modified_files_count == 0
    assert report.integrity_status == "VERIFIED"


def test_reads_are_served_from_verified_pinned_objects(
    boundary_run: _BoundaryRun,
) -> None:
    source = boundary_run.source
    for record in boundary_run.inventory:
        content = source.read_path(record.source_relative_path)
        assert compute_content_sha256(content) == record.content_sha256
    with pytest.raises(UnsupportedGitObjectError):
        source.read_blob("0" * 40)
    with pytest.raises(FileNotFoundError):
        source.read_path("not/a/pinned/path.md")


def test_inventory_bibliography_and_graph(boundary_run: _BoundaryRun) -> None:
    assert _candidates(boundary_run)
    assert boundary_run.bibliography
    report = boundary_run.graph_report
    assert report is not None
    assert report.graph_path == boundary_run.config.graph_path
    assert report.root_type == "dict"
    assert report.node_count > 0


def test_selection_completeness_and_budgets(boundary_run: _BoundaryRun) -> None:
    config = boundary_run.config
    candidates = _candidates(boundary_run)
    assert len(candidates) >= 2

    all_excluded = _manifest(
        config, [_decision(record, "exclude") for record in candidates]
    )
    # The frozen validator requires at least one explicit inclusion.  An
    # all-exclusion prepopulation failing with exactly this problem proves
    # every candidate carries a hash-agreeing decision and nothing is omitted.
    assert validate_selection_manifest(all_excluded, boundary_run.inventory, config=config) == [
        "selection must include at least one paper"
    ]

    one_included = _manifest(
        config,
        [_decision(candidates[0], "include")]
        + [_decision(record, "exclude") for record in candidates[1:]],
    )
    assert (
        validate_selection_manifest(one_included, boundary_run.inventory, config=config)
        == []
    )

    budget_config = config.model_copy(update={"max_selected_documents": 1})
    over_budget = _manifest(
        config,
        [_decision(record, "include") for record in candidates[:2]]
        + [_decision(record, "exclude") for record in candidates[2:]],
    )
    problems = validate_selection_manifest(
        over_budget, boundary_run.inventory, config=budget_config
    )
    assert any("exceeds max_selected_documents" in problem for problem in problems)


def test_library_state_snapshot_unchanged(boundary_run: _BoundaryRun) -> None:
    assert boundary_run.after == boundary_run.before
