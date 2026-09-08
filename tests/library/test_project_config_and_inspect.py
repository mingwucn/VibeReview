from __future__ import annotations

import os
from pathlib import Path

import pytest

from vibereview.library.inspect import run_inspection
from vibereview.library.models import LibraryConfig, PathSecurityError
from vibereview.library.project_config import load_review_config


def write_config(
    path: Path,
    library_path: str,
    superproject_path: str,
    commit: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""
[project]
name = "synthetic-review"
topic = "A synthetic signal"
scope = "Fixture-only Markdown"

[library]
id = "synthetic"
path = "{library_path}"
superproject_path = "{superproject_path}"
gitlink_path = "library"
expected_commit = "{commit}"
markdown_root = "papers"
bibliography = "references.bib"
graph_path = "graph.json"
selection_manifest = "selection.json"

[retrieval]
max_candidates = 12
max_query_terms = 16
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_operator_config_is_external_and_paths_resolve_from_config(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    repository, library_config, _ = synthetic_library
    public_root = tmp_path / "public"
    public_root.mkdir()
    config_path = tmp_path / "operator" / "review.toml"
    relative_library = os.path.relpath(repository, config_path.parent)
    relative_superproject = os.path.relpath(
        library_config.superproject_path, config_path.parent
    )
    write_config(
        config_path,
        Path(relative_library).as_posix(),
        Path(relative_superproject).as_posix(),
        library_config.expected_commit,
    )

    config = load_review_config(config_path, public_repository_root=public_root)
    assert config.library.path == repository.resolve()
    assert config.library.id == "synthetic"
    assert config.retrieval.max_candidates == 12
    assert config.library.as_library_config().library_path == repository.resolve()


def test_operator_config_can_explicitly_disable_only_cleanliness_enforcement(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    repository, library_config, _ = synthetic_library
    public_root = tmp_path / "public"
    public_root.mkdir()
    config_path = tmp_path / "operator" / "review.toml"
    write_config(
        config_path,
        str(repository),
        str(library_config.superproject_path),
        library_config.expected_commit,
    )
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'selection_manifest = "selection.json"',
            'selection_manifest = "selection.json"\nrequire_clean_worktree = false',
        ),
        encoding="utf-8",
    )
    loaded = load_review_config(config_path, public_repository_root=public_root)
    assert loaded.library.require_clean_worktree is False
    assert loaded.library.as_library_config().require_clean_worktree is False


def test_checked_in_config_is_rejected(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    repository, library_config, _ = synthetic_library
    public_root = tmp_path / "public"
    config_path = public_root / "config.toml"
    write_config(
        config_path,
        str(repository),
        str(library_config.superproject_path),
        library_config.expected_commit,
    )
    with pytest.raises(PathSecurityError, match="outside the public"):
        load_review_config(config_path, public_repository_root=public_root)


def test_operator_config_symlink_is_rejected_without_following_it(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    repository, library_config, _ = synthetic_library
    public_root = tmp_path / "public"
    public_root.mkdir()
    target = tmp_path / "operator" / "actual.toml"
    write_config(
        target,
        str(repository),
        str(library_config.superproject_path),
        library_config.expected_commit,
    )
    link = target.with_name("review.toml")
    link.symlink_to(target)
    with pytest.raises(PathSecurityError, match="regular file"):
        load_review_config(link, public_repository_root=public_root)


def test_checked_in_selection_manifest_is_rejected(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    repository, library_config, _ = synthetic_library
    public_root = tmp_path / "public"
    public_root.mkdir()
    config_path = tmp_path / "operator" / "review.toml"
    write_config(
        config_path,
        str(repository),
        str(library_config.superproject_path),
        library_config.expected_commit,
    )
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        text.replace(
            'selection_manifest = "selection.json"',
            f'selection_manifest = "{public_root / "selection.json"}"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(PathSecurityError, match="selection manifest"):
        load_review_config(config_path, public_repository_root=public_root)


@pytest.mark.parametrize("location", ["library", "superproject"])
def test_selection_manifest_cannot_be_inside_source_checkout(
    location: str,
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
) -> None:
    repository, library_config, _ = synthetic_library
    public_root = tmp_path / "public"
    public_root.mkdir()
    config_path = tmp_path / "operator" / "review.toml"
    write_config(
        config_path,
        str(repository),
        str(library_config.superproject_path),
        library_config.expected_commit,
    )
    selection_root = (
        repository if location == "library" else library_config.superproject_path
    )
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        text.replace(
            'selection_manifest = "selection.json"',
            f'selection_manifest = "{selection_root / "selection.json"}"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(PathSecurityError, match="library and its superproject"):
        load_review_config(config_path, public_repository_root=public_root)


def test_offline_inspection_writes_only_to_external_output(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    repository, library_config, _ = synthetic_library
    public_root = tmp_path / "public"
    public_root.mkdir()
    config_path = tmp_path / "operator" / "review.toml"
    write_config(
        config_path,
        str(repository),
        str(library_config.superproject_path),
        library_config.expected_commit,
    )
    config = load_review_config(config_path, public_repository_root=public_root)
    output = tmp_path / "local-audit"
    summary = run_inspection(
        config, output, public_repository_root=public_root
    )
    assert summary["integrity_status"] == "VERIFIED"
    assert summary["candidate_papers"] == 2
    assert (output / "upstream_integrity.json").is_file()
    assert not list(public_root.rglob("*.json"))

    with pytest.raises(PathSecurityError):
        run_inspection(
            config,
            public_root / "audit",
            public_repository_root=public_root,
        )
    with pytest.raises(PathSecurityError, match="superproject"):
        run_inspection(
            config,
            library_config.superproject_path / "local-audit",
            public_repository_root=public_root,
        )
