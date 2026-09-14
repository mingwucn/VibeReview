"""Opt-in MyLib execution of the deterministic operational pilot harness.

This test never commits corpus content.  The operator supplies a directory
(via ``VIBEREVIEW_OPERATIONAL_PILOT_DIR``) holding two files:

- ``selection.json``: a ``vibereview-corpus-selection-1`` manifest covering
  every inventory candidate of the pinned MyLib library, and
- ``proposals.json``: a ``vibereview-operational-pilot-1`` bundle keyed by
  content hashes and exact corpus text from that same pinned commit.

Both artifacts stay outside the repository; the test only reads them.
"""

from __future__ import annotations

import os
from pathlib import Path
import tomllib

import pytest

from vibereview.library.git_source import PinnedGitSource
from vibereview.library.inventory import build_library_inventory
from vibereview.library.models import LibraryConfig
from vibereview.library.operational_pilot import (
    load_operational_pilot_bundle,
    run_operational_pilot,
)
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

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_PIN_RECORD = _REPOSITORY_ROOT / "configs" / "libraries" / "mylib.toml"
_OPERATOR_DIR_ENV = "VIBEREVIEW_OPERATIONAL_PILOT_DIR"


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


def _operator_dir() -> Path:
    raw = os.environ.get(_OPERATOR_DIR_ENV)
    if not raw:
        pytest.skip(
            f"{_OPERATOR_DIR_ENV} is not set; no operator proposal bundle supplied"
        )
    directory = Path(raw)
    if not (directory / "selection.json").is_file():
        pytest.skip(f"{directory}/selection.json is missing")
    if not (directory / "proposals.json").is_file():
        pytest.skip(f"{directory}/proposals.json is missing")
    return directory


def test_operational_pilot_executes_against_pinned_mylib(tmp_path: Path) -> None:
    config = _load_pinned_config()
    operator_dir = _operator_dir()
    selection = load_selection_manifest(operator_dir / "selection.json")
    bundle = load_operational_pilot_bundle(operator_dir / "proposals.json")
    inventory, _ = build_library_inventory(PinnedGitSource.open(config), config)
    errors = validate_selection_manifest(selection, inventory, config=config)
    assert errors == [], f"selection does not cover the pinned inventory: {errors}"

    result = run_operational_pilot(
        config,
        selection,
        bundle,
        tmp_path / "review",
        tmp_path / "public",
    )

    assert result.library_before == result.library_after
    assert result.tasks, "the harness recorded no runtime tasks"
    assert {task.outcome for task in result.tasks} == {"valid_scientific_result"}
    assert result.snapshot.retrieved_spans, "no spans were promoted"
    assert result.snapshot.evidence_records, "no evidence records were promoted"
    assert result.snapshot.claim_paper_evidence, "no claim-paper evidence aggregated"
    assert result.snapshot.claim_packets, "no claim packets were validated"
