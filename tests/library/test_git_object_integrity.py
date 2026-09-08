from __future__ import annotations

from pathlib import Path
import subprocess
import zlib

import pytest

import vibereview.library.git_source as git_source
from vibereview.library.git_source import PinnedGitSource
from vibereview.library.inventory import build_library_inventory
from vibereview.library.models import (
    CorpusSelectionDocument,
    CorpusSelectionManifest,
    LibraryConfig,
    UnsupportedGitObjectError,
)
from vibereview.library.selection import import_selected_corpus


def _git_bytes(repository: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "--no-replace-objects", "-C", str(repository), *args],
        check=True,
        capture_output=True,
    ).stdout


def _git_text(repository: Path, *args: str) -> str:
    return _git_bytes(repository, *args).decode("ascii").strip()


def _mutate_same_length(payload: bytes) -> bytes:
    assert payload
    return payload[:-1] + bytes([payload[-1] ^ 1])


def _replace_loose_object(
    repository: Path, object_id: str, object_type: str, payload: bytes
) -> None:
    objects = Path(
        _git_text(
            repository,
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "objects",
        )
    )
    target = objects / object_id[:2] / object_id[2:]
    assert target.is_file(), "synthetic fixture objects must remain loose"
    target.chmod(0o600)
    canonical = f"{object_type} {len(payload)}\0".encode("ascii") + payload
    target.write_bytes(zlib.compress(canonical))


def _corrupt_object(
    repository: Path, object_id: str, object_type: str
) -> None:
    payload = _git_bytes(repository, "cat-file", object_type, object_id)
    _replace_loose_object(
        repository, object_id, object_type, _mutate_same_length(payload)
    )


def _target_blob(config: LibraryConfig) -> tuple[str, str]:
    path = "papers/Writer - 2024 - Alpha.md"
    return path, _git_text(
        config.library_path,
        "rev-parse",
        f"{config.expected_commit}:{path}",
    )


def test_corrupted_library_commit_is_rejected(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    repository, config, _ = synthetic_library
    _corrupt_object(repository, config.expected_commit, "commit")

    with pytest.raises(UnsupportedGitObjectError, match="canonical object ID"):
        PinnedGitSource.open(config)


def test_corrupted_library_tree_is_rejected(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    repository, config, _ = synthetic_library
    tree_id = _git_text(repository, "rev-parse", f"{config.expected_commit}^{{tree}}")
    _corrupt_object(repository, tree_id, "tree")

    with pytest.raises(UnsupportedGitObjectError, match="canonical object ID"):
        PinnedGitSource.open(config)


def test_corrupted_library_blob_is_rejected_during_snapshot_creation(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    repository, config, _ = synthetic_library
    _, blob_id = _target_blob(config)
    _corrupt_object(repository, blob_id, "blob")

    with pytest.raises(UnsupportedGitObjectError, match="canonical object ID"):
        PinnedGitSource.open(config)


def test_every_post_open_blob_read_rehashes_the_object(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    repository, config, expected = synthetic_library
    path, blob_id = _target_blob(config)
    source = PinnedGitSource.open(config)
    assert source.read_path(path) == expected[path]

    _corrupt_object(repository, blob_id, "blob")

    with pytest.raises(UnsupportedGitObjectError, match="canonical object ID"):
        source.read_path(path)
    with pytest.raises(UnsupportedGitObjectError, match="canonical object ID"):
        source.read_blob(blob_id)


@pytest.mark.parametrize("object_type", ["commit", "tree"])
def test_corrupted_superproject_anchor_is_rejected(
    object_type: str,
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    _, config, _ = synthetic_library
    superproject = config.superproject_path
    object_id = _git_text(superproject, "rev-parse", "HEAD")
    if object_type == "tree":
        object_id = _git_text(superproject, "rev-parse", "HEAD^{tree}")
    _corrupt_object(superproject, object_id, object_type)

    with pytest.raises(UnsupportedGitObjectError, match="canonical object ID"):
        PinnedGitSource.open(config)


def test_open_builds_one_detached_library_tree_snapshot(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, config, _ = synthetic_library
    original = git_source._build_verified_commit_snapshot
    calls = 0

    def counted(*args: object, **kwargs: object):
        nonlocal calls
        if Path(args[0]).resolve() == repository.resolve():
            calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(git_source, "_build_verified_commit_snapshot", counted)
    source = PinnedGitSource.open(config)
    first = source.enumerate_blobs()
    first[0]["git_blob_id"] = "0" * 40

    assert calls == 1
    assert source.enumerate_blobs()[0]["git_blob_id"] != "0" * 40


def test_nested_superproject_gitlink_path_uses_verified_intermediate_trees(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    repository, config, expected = synthetic_library
    superproject = config.superproject_path
    nested_parent = superproject / "dependencies"
    nested_parent.mkdir()
    nested_repository = nested_parent / "library"
    repository.rename(nested_repository)
    _git_bytes(superproject, "update-index", "--force-remove", "library")
    _git_bytes(
        superproject,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{config.expected_commit},dependencies/library",
    )
    _git_bytes(superproject, "commit", "-m", "use nested synthetic gitlink")
    nested = config.model_copy(
        update={
            "library_path": nested_repository,
            "gitlink_path": "dependencies/library",
        }
    )

    source = PinnedGitSource.open(nested)

    assert source.read_path("papers/Writer - 2024 - Alpha.md") == expected[
        "papers/Writer - 2024 - Alpha.md"
    ]


@pytest.mark.parametrize(
    ("limit_name", "expected_type"),
    [("_MAX_COMMIT_BYTES", "commit"), ("_MAX_TREE_BYTES", "tree")],
)
def test_commit_and_tree_reads_are_bounded(
    limit_name: str,
    expected_type: str,
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, config, _ = synthetic_library
    monkeypatch.setattr(git_source, limit_name, 1)

    with pytest.raises(
        UnsupportedGitObjectError,
        match=rf"pinned {expected_type} exceeds its byte limit",
    ):
        PinnedGitSource.open(config)


def test_blob_reads_are_bounded_by_configuration(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    _, config, _ = synthetic_library
    bounded = config.model_copy(update={"max_blob_bytes": 1})

    with pytest.raises(
        UnsupportedGitObjectError, match="pinned blob exceeds its byte limit"
    ):
        PinnedGitSource.open(bounded)


def test_verified_reader_supports_packed_objects(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    repository, config, expected = synthetic_library
    _git_bytes(repository, "gc", "--prune=now")
    _git_bytes(config.superproject_path, "gc", "--prune=now")

    source = PinnedGitSource.open(config)

    assert source.read_path("papers/Writer - 2024 - Alpha.md") == expected[
        "papers/Writer - 2024 - Alpha.md"
    ]


def test_corruption_fails_before_import_destination_mutation(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
) -> None:
    repository, config, _ = synthetic_library
    source = PinnedGitSource.open(config)
    inventory, _ = build_library_inventory(source, config)
    candidates = [
        item
        for item in inventory
        if item.document_kind.value == "candidate_paper_markdown"
    ]
    manifest = CorpusSelectionManifest(
        library_id=config.library_id,
        source_commit=config.expected_commit,
        documents=[
            CorpusSelectionDocument(
                source_relative_path=item.source_relative_path,
                content_sha256=item.content_sha256,
                decision="include",
                role="primary",
                accepted_by="synthetic-test",
                accepted_at="2030-01-01T00:00:00Z",
                reason="object-integrity regression",
            )
            for item in candidates
        ],
    )
    _, blob_id = _target_blob(config)
    _corrupt_object(repository, blob_id, "blob")
    review_root = tmp_path / "must-remain-absent"

    with pytest.raises(UnsupportedGitObjectError, match="canonical object ID"):
        import_selected_corpus(
            review_root,
            manifest,
            config,
            public_repository_root=tmp_path / "public-repository",
        )
    assert not review_root.exists()


def test_sha256_repository_and_superproject_object_chains(tmp_path: Path) -> None:
    probe = tmp_path / "sha256-probe"
    capability = subprocess.run(
        ["git", "init", "--object-format=sha256", str(probe)],
        capture_output=True,
    )
    if capability.returncode != 0:
        pytest.skip("installed Git does not support SHA-256 repositories")

    superproject = tmp_path / "sha256-superproject"
    subprocess.run(
        [
            "git",
            "init",
            "--object-format=sha256",
            "-b",
            "main",
            str(superproject),
        ],
        check=True,
        capture_output=True,
    )
    repository = superproject / "library"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "--object-format=sha256", "-b", "main", str(repository)],
        check=True,
        capture_output=True,
    )
    for current in (superproject, repository):
        _git_bytes(current, "config", "user.name", "Synthetic Fixture")
        _git_bytes(
            current,
            "config",
            "user.email",
            "fixture.invalid@example.invalid",
        )
    papers = repository / "papers"
    papers.mkdir()
    expected = b"# SHA-256 fixture\n"
    (papers / "Sample.md").write_bytes(expected)
    _git_bytes(repository, "add", ".")
    _git_bytes(repository, "commit", "-m", "synthetic SHA-256 corpus")
    commit_id = _git_text(repository, "rev-parse", "HEAD")
    _git_bytes(
        superproject,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{commit_id},library",
    )
    _git_bytes(superproject, "commit", "-m", "pin synthetic SHA-256 corpus")
    config = LibraryConfig(
        library_id="synthetic-sha256",
        library_path=repository,
        superproject_path=superproject,
        gitlink_path="library",
        expected_commit=commit_id,
        markdown_root="papers",
    )

    source = PinnedGitSource.open(config)

    assert len(commit_id) == 64
    assert len(source.integrity.superproject_commit) == 64
    assert source.read_path("papers/Sample.md") == expected
