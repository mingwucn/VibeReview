from __future__ import annotations

from pathlib import Path

from vibereview.enums import RetrievalIntent
from vibereview.library.git_source import snapshot_library_state
from vibereview.library.inspect import run_inspection
from vibereview.library.models import LibraryConfig
from vibereview.library.project_config import (
    ProjectLibraryConfig,
    ProjectMetadata,
    ReviewProjectConfig,
)
from vibereview.library.retrieval import DeterministicTextRetriever
from vibereview.library.selection import import_selected_corpus

from test_corpus_import import make_selection


def test_inspect_import_and_retrieval_never_mutate_source_or_gitlink(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    _, config, _ = synthetic_library
    before = snapshot_library_state(config)
    operator_config = ReviewProjectConfig(
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
            selection_manifest=tmp_path / "operator-selection.json",
        ),
    )
    public_root = tmp_path / "public"
    public_root.mkdir()
    run_inspection(
        operator_config,
        tmp_path / "audit",
        public_repository_root=public_root,
    )
    review_root = tmp_path / "review"
    import_selected_corpus(
        review_root,
        make_selection(config),
        config,
        public_repository_root=tmp_path / "public-repository",
    )
    DeterministicTextRetriever(review_root).search(
        "lattice signal",
        "Q-C0001-SUP-01",
        RetrievalIntent.SUPPORT,
    )
    assert snapshot_library_state(config) == before
