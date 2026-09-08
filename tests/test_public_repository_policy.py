"""Guards for data that must never be tracked in the public repository."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_EXACT = {
    ".gitmodules",
    "src/vibereview/library/pilot.py",
    "tests/library/test_pilot_p2.py",
}
FORBIDDEN_PREFIXES = (
    "external/",
    "hand-off/",
    "work/",
    "output/",
    ".vibereview-private/",
)


class TreeEntry(NamedTuple):
    mode: str
    object_id: str
    path: str


def _git(*args: str, text: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=text,
        encoding="utf-8" if text else None,
    )


def _require_full_clone() -> None:
    shallow = _git("rev-parse", "--is-shallow-repository", text=True).stdout.strip()
    assert shallow == "false", "public-history policy requires a full, non-shallow clone"


def _tracked_entries() -> tuple[TreeEntry, ...]:
    completed = subprocess.run(
        ["git", "ls-files", "--stage", "-z"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    entries: list[TreeEntry] = []
    for record in completed.stdout.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, object_id, stage = metadata.decode("ascii").split(" ")
        assert stage == "0", "public tree contains an unresolved index entry"
        entries.append(TreeEntry(mode, object_id, raw_path.decode("utf-8")))
    return tuple(entries)


def _reachable_entries() -> tuple[TreeEntry, ...]:
    _require_full_clone()
    commits = _git("rev-list", "HEAD", text=True).stdout.splitlines()
    entries: set[TreeEntry] = set()
    seen_trees: set[str] = set()
    for commit in commits:
        tree = _git("rev-parse", f"{commit}^{{tree}}", text=True).stdout.strip()
        if tree in seen_trees:
            continue
        seen_trees.add(tree)
        raw = _git("ls-tree", "-r", "-z", "--full-tree", tree).stdout
        for record in raw.split(b"\0"):
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            mode, _kind, object_id = metadata.decode("ascii").split(" ")
            entries.add(TreeEntry(mode, object_id, raw_path.decode("utf-8")))
    return tuple(sorted(entries))


def _entry_violations(entries: tuple[TreeEntry, ...]) -> list[str]:
    violations: list[str] = []
    exact = {item.casefold() for item in FORBIDDEN_EXACT}
    prefixes = tuple(item.casefold() for item in FORBIDDEN_PREFIXES)
    for entry in entries:
        path = entry.path.casefold()
        if (
            entry.mode in {"120000", "160000"}
            or path in exact
            or path.startswith(prefixes)
            or (path.startswith("reviews/") and path != "reviews/.gitkeep")
            or (path.startswith("configs/libraries/") and path.endswith(".toml"))
            or Path(path).name == ".writer.lock"
            or path.endswith(".secret")
        ):
            violations.append(f"{entry.mode} {entry.path}")
    return violations


def test_public_tree_excludes_live_or_retired_project_data() -> None:
    assert _entry_violations(_tracked_entries()) == []


def test_public_history_excludes_live_or_retired_project_paths() -> None:
    assert _entry_violations(_reachable_entries()) == []


@pytest.mark.parametrize(
    "entry",
    [
        TreeEntry("160000", "0" * 40, "vendor/library"),
        TreeEntry("120000", "0" * 40, "docs/link"),
        TreeEntry("100644", "0" * 40, "Reviews/live/state.json"),
        TreeEntry("100644", "0" * 40, "EXTERNAL/source.txt"),
        TreeEntry("100644", "0" * 40, "WORK/cache.json"),
        TreeEntry("100644", "0" * 40, "output/result.md"),
        TreeEntry("100644", "0" * 40, "configs/Libraries/live.TOML"),
        TreeEntry("100644", "0" * 40, ".writer.lock"),
        TreeEntry("100644", "0" * 40, "credentials/API.SECRET"),
    ],
)
def test_reserved_paths_modes_and_case_variants_are_rejected(entry: TreeEntry) -> None:
    assert _entry_violations((entry,))


def test_synthetic_fixture_and_reviews_placeholder_are_allowed() -> None:
    entries = (
        TreeEntry("100644", "0" * 40, "reviews/.gitkeep"),
        TreeEntry("100644", "0" * 40, "tests/fixtures/synthetic/paper.md"),
    )
    assert _entry_violations(entries) == []


def test_retired_fixture_acceptance_phrases_are_absent() -> None:
    forbidden = (
        b"100% BYTE-EXACT " + b"VERIFICATION PASSED",
        b"complete scientific " + b"pilot",
        b"fully " + b"audited",
    )

    violations: list[str] = []
    blob_ids = {
        entry.object_id
        for entry in _reachable_entries()
        if entry.mode in {"100644", "100755"}
    }
    for object_id in sorted(blob_ids):
        data = _git("cat-file", "blob", object_id).stdout
        if any(phrase.lower() in data.lower() for phrase in forbidden):
            violations.append(object_id)

    assert violations == []
