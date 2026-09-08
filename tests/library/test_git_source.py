from __future__ import annotations

from pathlib import Path
import subprocess

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
    CommitMismatchError,
    DirtyWorkingTreeError,
    GitlinkModeError,
    LibraryConfig,
    UnsupportedGitObjectError,
)


def test_reads_are_from_expected_commit_objects(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    repository, config, expected = synthetic_library
    source = PinnedGitSource.open(config)
    assert source.integrity.gitlink_mode == "160000"
    assert source.integrity.gitlink_object_id == config.expected_commit
    target = "papers/Writer - 2024 - Alpha.md"
    assert source.read_path(target) == expected[target]

    (repository / target).write_text("mutated working copy\n", encoding="utf-8")
    assert source.read_path(target) == expected[target]
    with pytest.raises(DirtyWorkingTreeError):
        PinnedGitSource.open(config)


def test_pin_and_untracked_content_fail_closed(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    repository, config, _ = synthetic_library
    wrong = config.model_copy(update={"expected_commit": "0" * 40})
    with pytest.raises(Exception):
        PinnedGitSource.open(wrong)

    (repository / "local-only.txt").write_text("not committed\n", encoding="utf-8")
    with pytest.raises(DirtyWorkingTreeError):
        PinnedGitSource.open(config)


def test_head_must_equal_pin(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    repository, config, _ = synthetic_library
    (repository / "README.md").write_text("# Next revision\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "next revision"],
        check=True,
        capture_output=True,
    )
    relaxed = config.model_copy(update={"require_clean_worktree": False})
    with pytest.raises(CommitMismatchError):
        PinnedGitSource.open(relaxed)


def test_only_reachable_regular_blobs_can_be_read(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    _, config, _ = synthetic_library
    source = PinnedGitSource.open(config)
    with pytest.raises(UnsupportedGitObjectError):
        source.read_blob(config.expected_commit)
    with pytest.raises(ValueError):
        source.read_path("../escape.md")
    assert compute_content_sha256(b"abc").startswith("sha256:")


def test_gitlink_object_must_equal_configured_pin(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    _, config, _ = synthetic_library
    other_commit = subprocess.run(
        ["git", "-C", str(config.superproject_path), "rev-parse", "HEAD"],
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
            f"160000,{other_commit},{config.gitlink_path}",
        ],
        check=True,
    )
    with pytest.raises(CommitMismatchError, match="gitlink object"):
        PinnedGitSource.open(config)


def test_configured_entry_must_be_mode_160000(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    _, config, _ = synthetic_library
    blob_id = subprocess.run(
        ["git", "-C", str(config.superproject_path), "hash-object", "-w", "--stdin"],
        input=b"ordinary file\n",
        check=True,
        capture_output=True,
    ).stdout.decode().strip()
    subprocess.run(
        [
            "git",
            "-C",
            str(config.superproject_path),
            "update-index",
            "--cacheinfo",
            f"100644,{blob_id},{config.gitlink_path}",
        ],
        check=True,
    )
    with pytest.raises(GitlinkModeError, match="mode-160000"):
        PinnedGitSource.open(config)


def test_staged_gitlink_cannot_mask_different_committed_superproject_pin(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    repository, config, _ = synthetic_library
    (repository / "README.md").write_text("# Later source\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "later source"],
        check=True,
        capture_output=True,
    )
    later_commit = subprocess.run(
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
            f"160000,{later_commit},{config.gitlink_path}",
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
            "commit later pin",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "reset", "--hard", config.expected_commit],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(config.superproject_path),
            "update-index",
            "--cacheinfo",
            f"160000,{config.expected_commit},{config.gitlink_path}",
        ],
        check=True,
    )

    relaxed = config.model_copy(update={"require_clean_worktree": False})
    with pytest.raises(CommitMismatchError, match="committed superproject"):
        PinnedGitSource.open(relaxed)


def test_blob_replacement_ref_cannot_change_pinned_bytes(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    repository, config, expected = synthetic_library
    target = "papers/Writer - 2024 - Alpha.md"
    source = PinnedGitSource.open(config)
    blob_id = next(
        item["git_blob_id"]
        for item in source.enumerate_blobs()
        if item["source_relative_path"] == target
    )
    replacement = b"replacement bytes must remain invisible\n"
    replacement_id = subprocess.run(
        ["git", "-C", str(repository), "hash-object", "-w", "--stdin"],
        input=replacement,
        check=True,
        capture_output=True,
    ).stdout.decode("ascii").strip()
    subprocess.run(
        ["git", "-C", str(repository), "replace", blob_id, replacement_id],
        check=True,
        capture_output=True,
    )

    unsanitized = subprocess.run(
        ["git", "-C", str(repository), "cat-file", "blob", blob_id],
        check=True,
        capture_output=True,
    ).stdout
    assert unsanitized == replacement
    assert PinnedGitSource.open(config).read_path(target) == expected[target]


def test_commit_replacement_ref_cannot_change_pinned_tree(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    repository, config, expected = synthetic_library
    target = "papers/Writer - 2024 - Alpha.md"
    original_commit = config.expected_commit
    replacement = b"replacement commit bytes must remain invisible\n"
    (repository / target).write_bytes(replacement)
    subprocess.run(["git", "-C", str(repository), "add", target], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "replacement commit"],
        check=True,
        capture_output=True,
    )
    replacement_commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(repository), "reset", "--hard", original_commit],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "replace",
            original_commit,
            replacement_commit,
        ],
        check=True,
        capture_output=True,
    )

    unsanitized = subprocess.run(
        ["git", "-C", str(repository), "show", f"{original_commit}:{target}"],
        check=True,
        capture_output=True,
    ).stdout
    assert unsanitized == replacement
    assert PinnedGitSource.open(config).read_path(target) == expected[target]


def test_inherited_git_repository_and_object_environment_is_ignored(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, config, expected = synthetic_library
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    subprocess.run(["git", "-C", str(hostile), "init"], check=True, capture_output=True)
    monkeypatch.setenv("GIT_DIR", str(hostile / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(hostile))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(tmp_path / "missing-objects"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "hostile-command")

    target = "papers/Writer - 2024 - Alpha.md"
    assert PinnedGitSource.open(config).read_path(target) == expected[target]


def test_assume_unchanged_cannot_hide_modified_tracked_bytes(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    repository, config, _ = synthetic_library
    target = "papers/Writer - 2024 - Alpha.md"
    subprocess.run(
        ["git", "-C", str(repository), "update-index", "--assume-unchanged", target],
        check=True,
    )
    (repository / target).write_text("hidden mutation\n", encoding="utf-8")
    assert subprocess.run(
        ["git", "-C", str(repository), "status", "--porcelain"],
        check=True,
        capture_output=True,
    ).stdout == b""
    with pytest.raises(DirtyWorkingTreeError):
        PinnedGitSource.open(config)


def test_ignored_untracked_bytes_are_not_considered_clean(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    repository, config, _ = synthetic_library
    (repository / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "ignore fixture path"],
        check=True,
        capture_output=True,
    )
    new_commit = subprocess.run(
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
            f"160000,{new_commit},{config.gitlink_path}",
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
            "advance ignore fixture pin",
        ],
        check=True,
        capture_output=True,
    )
    (repository / "ignored.txt").write_text("ignored but present\n", encoding="utf-8")
    revised = config.model_copy(update={"expected_commit": new_commit})
    with pytest.raises(DirtyWorkingTreeError):
        PinnedGitSource.open(revised)


def test_disabled_clean_enforcement_still_reads_only_exact_pinned_objects(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    repository, config, expected = synthetic_library
    (repository / ".git" / "info" / "exclude").write_text(
        "ignored-local.txt\n", encoding="utf-8"
    )
    for path in (
        "papers/Writer - 2024 - Alpha.md",
        "references.bib",
        "graph.json",
    ):
        (repository / path).write_text("dirty worktree bytes\n", encoding="utf-8")
    (repository / "ignored-local.txt").write_text(
        "ignored local bytes\n", encoding="utf-8"
    )
    before = snapshot_library_state(config)

    with pytest.raises(DirtyWorkingTreeError):
        PinnedGitSource.open(config)

    relaxed = config.model_copy(update={"require_clean_worktree": False})
    source = PinnedGitSource.open(relaxed)
    assert source.integrity.is_clean is False
    assert source.integrity.integrity_status == "PINNED_OBJECTS_VERIFIED_DIRTY"
    assert source.read_path("papers/Writer - 2024 - Alpha.md") == expected[
        "papers/Writer - 2024 - Alpha.md"
    ]
    assert source.read_path("references.bib") == expected["references.bib"]
    assert source.read_path("graph.json") == expected["graph.json"]
    inventory, _ = build_library_inventory(source, relaxed)
    bibliography, duplicates = load_bibliography(source, relaxed)
    graph = ReadOnlyGraphAdapter.from_source(source, relaxed.graph_path or "")
    assert len(inventory) > 3
    assert bibliography[0].key == "Alpha2024"
    assert duplicates == []
    assert graph.inspect().node_count == 1
    assert snapshot_library_state(relaxed) == before


def test_snapshot_covers_actual_superproject_bytes_and_ignored_status(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    _, config, _ = synthetic_library
    superproject = config.superproject_path
    tracked = superproject / "operator-note.txt"
    tracked.write_text("first bytes\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(superproject), "add", "operator-note.txt"], check=True
    )
    subprocess.run(
        ["git", "-C", str(superproject), "commit", "-m", "snapshot fixture"],
        check=True,
        capture_output=True,
    )
    before = snapshot_library_state(config)
    subprocess.run(
        [
            "git",
            "-C",
            str(superproject),
            "update-index",
            "--assume-unchanged",
            "operator-note.txt",
        ],
        check=True,
    )
    tracked.write_text("hidden replacement bytes\n", encoding="utf-8")
    hidden = snapshot_library_state(config)
    assert hidden.superproject_index_hash == before.superproject_index_hash
    assert hidden.superproject_status_hash == before.superproject_status_hash
    assert hidden.superproject_worktree_hash != before.superproject_worktree_hash

    (superproject / ".git" / "info" / "exclude").write_text(
        "ignored-local.txt\n", encoding="utf-8"
    )
    (superproject / "ignored-local.txt").write_text(
        "ignored bytes\n", encoding="utf-8"
    )
    ignored = snapshot_library_state(config)
    assert ignored.superproject_status_hash != hidden.superproject_status_hash
