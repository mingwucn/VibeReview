"""Offline inspection over a fully pinned external Git repository."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from vibereview.runtime.repository import atomic_write_text

from .bibliography import load_bibliography
from .git_source import PinnedGitSource
from .graph import ReadOnlyGraphAdapter
from .inventory import build_library_inventory, generate_inventory_markdown
from .models import DocumentKind, PathSecurityError
from .project_config import ReviewProjectConfig, load_review_config
from .resolver import resolve_source_mappings


def _inside(path: Path, root: Path) -> bool:
    resolved = path.resolve(strict=False)
    resolved_root = root.resolve(strict=False)
    return resolved == resolved_root or resolved_root in resolved.parents


def run_inspection(
    config: ReviewProjectConfig,
    output_dir: Path,
    *,
    public_repository_root: Path,
) -> dict[str, Any]:
    """Write local audit reports without reading mutable upstream files."""

    output = output_dir.resolve(strict=False)
    library_config = config.library.as_library_config()
    if (
        _inside(output, public_repository_root)
        or _inside(output, library_config.library_path)
        or _inside(output, library_config.superproject_path)
    ):
        raise PathSecurityError(
            "inspection output must be outside the public repository, library, and superproject"
        )
    source = PinnedGitSource.open(library_config)
    records, excluded = build_library_inventory(source, library_config)
    bibliography, duplicate_keys = load_bibliography(source, library_config)
    graph_nodes: list[dict[str, Any]] = []
    graph_report = None
    if library_config.graph_path is not None:
        adapter = ReadOnlyGraphAdapter.from_source(source, library_config.graph_path)
        graph_report = adapter.inspect()
        graph_nodes = adapter.graph_nodes()
    candidates = [
        record
        for record in records
        if record.document_kind is DocumentKind.CANDIDATE_PAPER_MARKDOWN
    ]
    mapping, conflicts = resolve_source_mappings(
        candidates, bibliography, duplicate_keys, graph_nodes
    )

    output.mkdir(parents=True)
    atomic_write_text(
        output / "upstream_integrity.json",
        source.integrity.model_dump_json(indent=2) + "\n",
    )
    atomic_write_text(
        output / "inventory.json",
        json.dumps(
            [record.model_dump(mode="json") for record in records],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    atomic_write_text(output / "inventory.md", generate_inventory_markdown(records, library_config))
    atomic_write_text(
        output / "excluded_entries.json",
        json.dumps(
            [record.model_dump(mode="json") for record in excluded],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    atomic_write_text(output / "source_mapping.json", mapping.model_dump_json(indent=2) + "\n")
    atomic_write_text(output / "metadata_conflicts.json", conflicts.model_dump_json(indent=2) + "\n")
    if graph_report is not None:
        atomic_write_text(output / "graph_schema.json", graph_report.model_dump_json(indent=2) + "\n")
    summary = {
        "library_id": library_config.library_id,
        "source_commit": library_config.expected_commit,
        "integrity_status": source.integrity.integrity_status,
        "tracked_blobs": len(records),
        "candidate_papers": len(candidates),
        "bibliography_entries": len(bibliography),
        "graph_nodes": len(graph_nodes),
        "metadata_conflicts": conflicts.conflicts_found,
    }
    atomic_write_text(
        output / "summary.json",
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect an operator-supplied pinned library")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--public-repository-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = load_review_config(
            args.config, public_repository_root=args.public_repository_root
        )
        run_inspection(
            config,
            args.output,
            public_repository_root=args.public_repository_root,
        )
    except Exception as exc:
        print(f"ERROR: inspection failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
