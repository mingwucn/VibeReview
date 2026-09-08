"""Public-history path guard with an optional operator-private content denylist."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess

from .git_source import sanitized_git_environment
from vibereview.runtime.repository import read_contained_regular_file


ALLOWED_REVIEW_PATHS = frozenset({"reviews/.gitkeep"})
FORBIDDEN_EXACT_PATHS = frozenset(
    {
        ".gitmodules",
        "configs/libraries",
        "reviews",
        "src/vibereview/library/pilot.py",
        "tests/library/test_pilot_p2.py",
    }
)
FORBIDDEN_PREFIXES = (
    "external/",
    "local-corpora/",
    "hand-off/",
    "docs/integrations/",
    "work/",
    "state/",
    "output/",
    ".vibereview-private/",
)
FORBIDDEN_ROOT_NAMES = frozenset(
    prefix.removesuffix("/") for prefix in FORBIDDEN_PREFIXES
)
FORBIDDEN_MODES = frozenset({"120000", "160000"})


def _git(repository: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(
        [
            "git",
            "--no-replace-objects",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(repository),
            *args,
        ],
        input=input_bytes,
        check=True,
        capture_output=True,
        env=sanitized_git_environment(),
    ).stdout


def _git_optional(repository: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            "git",
            "--no-replace-objects",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(repository),
            *args,
        ],
        check=False,
        capture_output=True,
        env=sanitized_git_environment(),
    )


def path_violation(path: str) -> str | None:
    normalized = path.casefold()
    parsed = PurePosixPath(normalized)
    basename = parsed.name
    if normalized in FORBIDDEN_EXACT_PATHS:
        return "forbidden exact path"
    if normalized.startswith("reviews/") and normalized not in ALLOWED_REVIEW_PATHS:
        return "review artifacts are excluded from public history"
    if normalized.startswith("configs/libraries/") and normalized.endswith(".toml"):
        return "operator library configuration is excluded from public history"
    if normalized in FORBIDDEN_ROOT_NAMES or normalized.startswith(
        FORBIDDEN_PREFIXES
    ):
        return "forbidden path prefix"
    if basename == ".writer.lock":
        return "writer lock files are excluded from public history"
    if basename.endswith(".secret"):
        return "secret files are excluded from public history"
    if "__pycache__" in parsed.parts or basename.endswith((".pyc", ".pyo")):
        return "generated Python bytecode is excluded from public history"
    return None


def _parse_tree_entries(output: bytes) -> set[tuple[str, str]]:
    entries: set[tuple[str, str]] = set()
    for raw in output.split(b"\0"):
        if not raw:
            continue
        metadata, path_bytes = raw.split(b"\t", 1)
        mode = metadata.split(b" ", 1)[0].decode("ascii")
        path = path_bytes.decode("utf-8", errors="strict")
        entries.add((mode, path))
    return entries


def _parse_index_entries(output: bytes) -> list[tuple[str, str, str, str]]:
    entries: list[tuple[str, str, str, str]] = []
    for raw in output.split(b"\0"):
        if not raw:
            continue
        metadata, path_bytes = raw.split(b"\t", 1)
        mode_bytes, object_bytes, stage_bytes = metadata.split(b" ", 2)
        entries.append(
            (
                mode_bytes.decode("ascii"),
                object_bytes.decode("ascii"),
                stage_bytes.decode("ascii"),
                path_bytes.decode("utf-8", errors="strict"),
            )
        )
    return entries


def history_entries(repository: Path, revision: str = "HEAD") -> set[tuple[str, str]]:
    commits = _git(repository, "rev-list", revision).decode("ascii").splitlines()
    entries: set[tuple[str, str]] = set()
    for commit in commits:
        output = _git(repository, "ls-tree", "-r", "-z", "--full-tree", commit)
        entries.update(_parse_tree_entries(output))
    return entries


def history_paths(repository: Path, revision: str = "HEAD") -> set[str]:
    return {path for _, path in history_entries(repository, revision)}


def worktree_entries(repository: Path) -> set[tuple[str, str]]:
    indexed = _git(
        repository,
        "ls-files",
        "-z",
        "--cached",
        "--stage",
    )
    entries = _parse_tree_entries(indexed)
    paths = {path for _, path in entries}
    untracked = _git(
        repository,
        "ls-files",
        "-z",
        "--others",
        "--exclude-standard",
    )
    for raw in untracked.split(b"\0"):
        if not raw:
            continue
        path = raw.decode("utf-8", errors="strict")
        paths.add(path)
    for path in paths:
        candidate = repository / path
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            mode = "120000"
        elif stat.S_ISREG(info.st_mode):
            mode = "100755" if info.st_mode & stat.S_IXUSR else "100644"
        elif stat.S_ISDIR(info.st_mode):
            mode = "040000"
        else:
            mode = "special"
        entries.add((mode, path))
    return entries


def worktree_paths(repository: Path) -> set[str]:
    return {path for _, path in worktree_entries(repository)}


def _full_clone_violation(repository: Path) -> str | None:
    shallow = _git(repository, "rev-parse", "--is-shallow-repository").strip()
    if shallow != b"false":
        return "public history scan requires a full, non-shallow clone"
    partial = _git_optional(repository, "config", "--get", "extensions.partialclone")
    if partial.returncode == 0 and partial.stdout.strip():
        return "public history scan requires a non-partial clone"
    promisor = _git_optional(
        repository, "config", "--get-regexp", r"^remote\..*\.promisor$"
    )
    if promisor.returncode == 0 and b"true" in promisor.stdout.casefold():
        return "public history scan requires a non-promisor clone"
    return None


def _load_private_denylist(path: Path) -> tuple[set[str], set[str], list[bytes]]:
    git_oids: set[str] = set()
    sha256s: set[str] = set()
    tokens: list[bytes] = []
    if not path.is_file():
        raise FileNotFoundError("private denylist does not exist")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        kind, separator, value = line.partition(":")
        if not separator or not value:
            raise ValueError("denylist lines require a typed prefix")
        if kind == "git-oid" and re.fullmatch(r"[0-9a-f]{40,64}", value):
            git_oids.add(value)
        elif kind == "blob-sha256" and re.fullmatch(r"[0-9a-f]{64}", value):
            sha256s.add(value)
        elif kind == "text":
            tokens.append(value.encode("utf-8"))
        else:
            raise ValueError("unsupported or malformed private denylist entry")
    return git_oids, sha256s, tokens


def scan_private_content_denylist(
    repository: Path,
    denylist_path: Path,
    *,
    revision: str = "HEAD",
    max_blob_bytes: int = 16 * 1024 * 1024,
    include_worktree: bool = True,
) -> list[str]:
    """Scan reachable objects without placing private fingerprints in this repository."""

    denied_oids, denied_sha256s, denied_tokens = _load_private_denylist(denylist_path)
    object_lines = _git(repository, "rev-list", "--objects", revision).splitlines()
    object_ids = sorted({line.split(b" ", 1)[0].decode("ascii") for line in object_lines})
    violations: list[str] = []
    for object_id in object_ids:
        if object_id in denied_oids:
            violations.append("reachable object matches a private Git object fingerprint")
        object_type = _git(repository, "cat-file", "-t", object_id).decode("ascii").strip()
        if object_type != "blob":
            continue
        size = int(_git(repository, "cat-file", "-s", object_id).decode("ascii"))
        if size > max_blob_bytes:
            violations.append("reachable blob exceeds the configured content-scan budget")
            continue
        content = _git(repository, "cat-file", "blob", object_id)
        digest = hashlib.sha256(content).hexdigest()
        if digest in denied_sha256s:
            violations.append("reachable blob matches a private SHA-256 fingerprint")
        if any(token in content for token in denied_tokens):
            violations.append("reachable blob contains a private denylist token")

    if include_worktree:
        index = _git(repository, "ls-files", "--stage", "-z")
        for mode, object_id, stage, _ in _parse_index_entries(index):
            if stage != "0" or mode not in {"100644", "100755"}:
                continue
            if object_id in denied_oids:
                violations.append("staged blob matches a private Git object fingerprint")
            size = int(_git(repository, "cat-file", "-s", object_id).decode("ascii"))
            if size > max_blob_bytes:
                violations.append("staged blob exceeds the configured content-scan budget")
                continue
            content = _git(repository, "cat-file", "blob", object_id)
            if hashlib.sha256(content).hexdigest() in denied_sha256s:
                violations.append("staged blob matches a private SHA-256 fingerprint")
            if any(token in content for token in denied_tokens):
                violations.append("staged blob contains a private denylist token")

        for mode, path in sorted(worktree_entries(repository)):
            if mode not in {"100644", "100755"}:
                continue
            try:
                content, _ = read_contained_regular_file(
                    repository, path, max_bytes=max_blob_bytes
                )
            except ValueError:
                violations.append("worktree entry is not a safe regular file")
                continue
            object_id = _git(
                repository, "hash-object", "--stdin", input_bytes=content
            ).decode("ascii").strip()
            if object_id in denied_oids:
                violations.append("worktree file matches a private Git object fingerprint")
            if hashlib.sha256(content).hexdigest() in denied_sha256s:
                violations.append("worktree file matches a private SHA-256 fingerprint")
            if any(token in content for token in denied_tokens):
                violations.append("worktree file contains a private denylist token")
    return sorted(set(violations))


def scan_public_boundary(
    repository: Path,
    *,
    revision: str = "HEAD",
    denylist_path: Path | None = None,
    include_worktree: bool = True,
) -> list[str]:
    if (clone_error := _full_clone_violation(repository)) is not None:
        return [clone_error]
    history = history_entries(repository, revision)
    violations = [
        f"{path}: {reason}"
        for _, path in sorted(history)
        if (reason := path_violation(path)) is not None
    ]
    violations.extend(
        f"{path}: forbidden Git mode {mode}"
        for mode, path in sorted(history)
        if mode in FORBIDDEN_MODES
    )
    if include_worktree:
        current_entries = worktree_entries(repository)
        violations.extend(
            f"{path}: {reason}"
            for _, path in sorted(current_entries)
            if (reason := path_violation(path)) is not None
        )
        violations.extend(
            f"{path}: forbidden Git mode {mode}"
            for mode, path in sorted(current_entries)
            if mode in FORBIDDEN_MODES
        )
        violations.extend(
            f"{path}: unsafe worktree mode {mode}"
            for mode, path in sorted(current_entries)
            if mode not in {"100644", "100755", *FORBIDDEN_MODES}
        )
    if denylist_path is not None:
        violations.extend(
            scan_private_content_denylist(
                repository,
                denylist_path,
                revision=revision,
                include_worktree=include_worktree,
            )
        )
    return sorted(set(violations))


def main() -> None:
    parser = argparse.ArgumentParser(description="Check the public library boundary")
    parser.add_argument("repository", type=Path)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--private-denylist", type=Path)
    args = parser.parse_args()
    violations = scan_public_boundary(
        args.repository,
        revision=args.revision,
        denylist_path=args.private_denylist,
    )
    if violations:
        for violation in violations:
            print(violation)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
