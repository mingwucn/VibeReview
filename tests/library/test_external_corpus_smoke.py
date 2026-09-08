from __future__ import annotations

import os
from pathlib import Path

import pytest

from vibereview.library.bibliography import load_bibliography
from vibereview.library.git_source import PinnedGitSource, snapshot_library_state
from vibereview.library.graph import ReadOnlyGraphAdapter
from vibereview.library.inventory import build_library_inventory
from vibereview.library.project_config import load_review_config
from vibereview.library.selection import (
    load_selection_manifest,
    validate_selection_manifest,
)


pytestmark = [
    pytest.mark.external_corpus,
    pytest.mark.skipif(
        os.environ.get("VIBEREVIEW_RUN_EXTERNAL_CORPUS") != "1",
        reason="requires explicit VIBEREVIEW_RUN_EXTERNAL_CORPUS=1 opt-in",
    ),
]


def test_operator_supplied_external_corpus_read_only_smoke() -> None:
    """No repository identity or location is embedded in this opt-in smoke test."""

    raw_config = os.environ.get("VIBEREVIEW_EXTERNAL_CORPUS_CONFIG")
    raw_public_root = os.environ.get("VIBEREVIEW_PUBLIC_REPOSITORY_ROOT")
    if not raw_config or not raw_public_root:
        pytest.fail(
            "opt-in requires VIBEREVIEW_EXTERNAL_CORPUS_CONFIG and "
            "VIBEREVIEW_PUBLIC_REPOSITORY_ROOT"
        )
    config = load_review_config(
        Path(raw_config), public_repository_root=Path(raw_public_root)
    )
    library_config = config.library.as_library_config()
    before = snapshot_library_state(library_config)
    source = PinnedGitSource.open(library_config)
    inventory, _ = build_library_inventory(source, library_config)
    assert inventory
    selection = load_selection_manifest(config.library.selection_manifest)
    assert validate_selection_manifest(
        selection, inventory, config=library_config
    ) == []
    load_bibliography(source, library_config)
    if library_config.graph_path is not None:
        ReadOnlyGraphAdapter.from_source(
            source, library_config.graph_path
        ).inspect()
    assert snapshot_library_state(library_config) == before
