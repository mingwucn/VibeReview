"""Guards for data that must never be tracked in the public repository."""

from __future__ import annotations

import subprocess
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_EXACT = {
    ".gitmodules",
    "src/vibereview/library/pilot.py",
    "tests/library/test_pilot_p2.py",
}
FORBIDDEN_PREFIXES = (
    "external/",
    "hand-off/",
)


def _tracked_paths() -> tuple[str, ...]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    return tuple(
        path.decode("utf-8")
        for path in completed.stdout.split(b"\0")
        if path
    )


def _reachable_paths() -> tuple[str, ...]:
    completed = subprocess.run(
        ["git", "rev-list", "--objects", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return tuple(
        line.split(" ", 1)[1]
        for line in completed.stdout.splitlines()
        if " " in line
    )


def _path_violations(paths: tuple[str, ...]) -> list[str]:
    return [
        path
        for path in paths
        if path in FORBIDDEN_EXACT
        or path.startswith(FORBIDDEN_PREFIXES)
        or (path.startswith("reviews/") and path != "reviews/.gitkeep")
        or (path.startswith("configs/libraries/") and path.endswith(".toml"))
        or path.endswith("/.writer.lock")
    ]


def test_public_tree_excludes_live_or_retired_project_data() -> None:
    assert _path_violations(_tracked_paths()) == []


def test_public_history_excludes_live_or_retired_project_paths() -> None:
    assert _path_violations(_reachable_paths()) == []


def test_retired_fixture_acceptance_phrases_are_absent() -> None:
    forbidden = (
        b"100% BYTE-EXACT " + b"VERIFICATION PASSED",
        b"complete scientific " + b"pilot",
        b"fully " + b"audited",
    )

    violations: list[str] = []
    for relative_path in _tracked_paths():
        path = REPOSITORY_ROOT / relative_path
        if not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        if any(phrase.lower() in data.lower() for phrase in forbidden):
            violations.append(relative_path)

    assert violations == []
