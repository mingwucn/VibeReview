"""Fail-closed access to regular blobs in one exact external Git commit."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
from types import MappingProxyType
from typing import Any, BinaryIO

from vibereview.ids import Sha256
from vibereview.runtime.hashing import hash_bytes, hash_json
from vibereview.runtime.repository import read_contained_regular_file

from .models import (
    CommitMismatchError,
    DirtyWorkingTreeError,
    GitlinkModeError,
    LibraryConfig,
    LibraryNotFoundError,
    LibraryNotInitializedError,
    LibraryStateSnapshot,
    PathSecurityError,
    UnsupportedGitObjectError,
    UpstreamIntegrityReport,
    validate_relative_git_path,
)


_OBJECT_FORMATS = {"sha1": (20, 40), "sha256": (32, 64)}
_OBJECT_ID_RE = re.compile(r"^[0-9a-f]+$")
_MAX_BATCH_HEADER_BYTES = 256
_MAX_COMMIT_BYTES = 16 * 1024 * 1024
_MAX_TREE_BYTES = 64 * 1024 * 1024
_MAX_TREE_DEPTH = 128
_MAX_TREE_OBJECTS = 100_000
_MAX_TREE_ENTRIES = 1_000_000


@dataclass(frozen=True)
class _TreeEntry:
    mode: str
    name: str
    object_id: str


@dataclass(frozen=True)
class _BlobEntry:
    mode: str
    object_id: str
    path: str

    def as_public_dict(self) -> dict[str, str]:
        return {
            "mode": self.mode,
            "type": "blob",
            "git_blob_id": self.object_id,
            "source_relative_path": self.path,
        }


@dataclass(frozen=True)
class _CommitSnapshot:
    object_format: str
    commit_id: str
    root_tree_id: str
    blobs: tuple[_BlobEntry, ...]


def compute_content_sha256(content: bytes) -> Sha256:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def sanitized_git_environment() -> dict[str, str]:
    """Return a fixed read-only Git environment without object/config overrides."""

    # Construct a small allowlist instead of inheriting any GIT_* setting.  In
    # particular, object directories, replacement namespaces, config injection,
    # and a caller-selected repository must never alter a pinned-object read.
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git_argv(repo: Path, args: list[str]) -> list[str]:
    return [
        "git",
        "--no-replace-objects",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.quotepath=false",
        "-c",
        "core.untrackedCache=false",
        "-C",
        str(repo),
        *args,
    ]


def _git(repo: Path, args: list[str], *, text: bool = False) -> subprocess.CompletedProcess[Any]:
    try:
        return subprocess.run(
            _git_argv(repo, args),
            check=True,
            capture_output=True,
            text=text,
            env=sanitized_git_environment(),
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        stderr = getattr(exc, "stderr", b"")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        raise LibraryNotInitializedError(
            f"Git object operation failed for configured library: {str(stderr).strip()}"
        ) from exc


def _detect_object_format(repository: Path) -> str:
    if not repository.exists():
        raise LibraryNotFoundError(f"external library does not exist: {repository}")
    if not repository.is_dir():
        raise LibraryNotInitializedError("external library path is not a directory")
    output = _git(
        repository,
        ["rev-parse", "--show-object-format=storage"],
        text=True,
    ).stdout.strip()
    if output not in _OBJECT_FORMATS:
        raise UnsupportedGitObjectError(
            f"unsupported Git object format: {output or '<empty>'}"
        )
    return output


def _validate_object_id(object_id: str, object_format: str) -> None:
    _, hex_length = _OBJECT_FORMATS[object_format]
    if len(object_id) != hex_length or _OBJECT_ID_RE.fullmatch(object_id) is None:
        raise UnsupportedGitObjectError(
            f"object ID does not match repository {object_format} format"
        )


def _canonical_object_id(object_format: str, object_type: str, payload: bytes) -> str:
    digest = hashlib.new(object_format)
    digest.update(f"{object_type} {len(payload)}\0".encode("ascii"))
    digest.update(payload)
    return digest.hexdigest()


def _readline_bounded(stream: BinaryIO, *, max_bytes: int) -> bytes:
    line = stream.readline(max_bytes + 1)
    if len(line) > max_bytes or not line.endswith(b"\n"):
        raise UnsupportedGitObjectError("Git object response header is malformed")
    return line


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise UnsupportedGitObjectError("Git object payload ended before declared size")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_verified_object(
    repository: Path,
    object_id: str,
    *,
    object_format: str,
    expected_type: str,
    max_bytes: int,
) -> bytes:
    """Read one object payload with a hard bound and verify its canonical ID."""

    _validate_object_id(object_id, object_format)
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            _git_argv(repository, ["cat-file", "--batch"]),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=sanitized_git_environment(),
        )
        if process.stdin is None or process.stdout is None:  # pragma: no cover - Popen contract
            raise OSError("Git object pipes were not created")
        process.stdin.write(object_id.encode("ascii") + b"\n")
        process.stdin.close()
        header = _readline_bounded(process.stdout, max_bytes=_MAX_BATCH_HEADER_BYTES)
        fields = header[:-1].split(b" ")
        if len(fields) == 2 and fields[1] in {b"missing", b"ambiguous"}:
            raise UnsupportedGitObjectError("required Git object is unavailable")
        if len(fields) != 3:
            raise UnsupportedGitObjectError("Git object response header is malformed")
        response_id_bytes, object_type_bytes, size_bytes = fields
        try:
            response_id = response_id_bytes.decode("ascii")
            object_type = object_type_bytes.decode("ascii")
            size = int(size_bytes.decode("ascii"))
        except (UnicodeError, ValueError) as exc:
            raise UnsupportedGitObjectError(
                "Git object response header is malformed"
            ) from exc
        if response_id != object_id or object_type != expected_type or size < 0:
            raise UnsupportedGitObjectError(
                "Git object response does not match the requested object"
            )
        if size > max_bytes:
            raise UnsupportedGitObjectError(
                f"pinned {expected_type} exceeds its byte limit ({size} > {max_bytes})"
            )
        payload = _read_exact(process.stdout, size)
        if process.stdout.read(1) != b"\n" or process.stdout.read(1) != b"":
            raise UnsupportedGitObjectError("Git object response framing is malformed")
        if process.wait() != 0:
            raise UnsupportedGitObjectError("Git failed while reading a required object")
    except OSError as exc:
        raise LibraryNotInitializedError(
            "Git object operation failed for configured library"
        ) from exc
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
    if _canonical_object_id(object_format, expected_type, payload) != object_id:
        raise UnsupportedGitObjectError(
            "Git object payload does not match its canonical object ID"
        )
    return payload


def _parse_commit_root_tree(payload: bytes, *, object_format: str) -> str:
    first_line = payload.partition(b"\n")[0]
    if not first_line.startswith(b"tree ") or first_line.count(b" ") != 1:
        raise UnsupportedGitObjectError("Git commit has no canonical root tree header")
    try:
        tree_id = first_line[5:].decode("ascii")
    except UnicodeError as exc:
        raise UnsupportedGitObjectError("Git commit root tree ID is malformed") from exc
    _validate_object_id(tree_id, object_format)
    return tree_id


def _parse_tree(payload: bytes, *, object_format: str) -> tuple[_TreeEntry, ...]:
    digest_bytes, _ = _OBJECT_FORMATS[object_format]
    entries: list[_TreeEntry] = []
    names: set[str] = set()
    cursor = 0
    while cursor < len(payload):
        separator = payload.find(b" ", cursor)
        terminator = payload.find(b"\0", separator + 1)
        if separator <= cursor or terminator < 0:
            raise UnsupportedGitObjectError("Git tree entry framing is malformed")
        mode_bytes = payload[cursor:separator]
        name_bytes = payload[separator + 1 : terminator]
        oid_start = terminator + 1
        oid_end = oid_start + digest_bytes
        if oid_end > len(payload):
            raise UnsupportedGitObjectError("Git tree entry object ID is truncated")
        try:
            mode = mode_bytes.decode("ascii")
            name = name_bytes.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise PathSecurityError("invalid UTF-8 in pinned Git tree") from exc
        if (
            not name
            or "/" in name
            or "\x00" in name
            or name in {".", "..", ".git"}
            or mode not in {"40000", "100644", "100755", "120000", "160000"}
        ):
            raise PathSecurityError("invalid path or mode in pinned Git tree")
        if name in names:
            raise PathSecurityError("duplicate path component in pinned Git tree")
        names.add(name)
        entries.append(
            _TreeEntry(
                mode=mode,
                name=name,
                object_id=payload[oid_start:oid_end].hex(),
            )
        )
        cursor = oid_end
    return tuple(entries)


def _read_verified_commit_root(
    repository: Path, commit_id: str, *, object_format: str
) -> str:
    payload = _read_verified_object(
        repository,
        commit_id,
        object_format=object_format,
        expected_type="commit",
        max_bytes=_MAX_COMMIT_BYTES,
    )
    return _parse_commit_root_tree(payload, object_format=object_format)


def _build_verified_commit_snapshot(
    repository: Path,
    commit_id: str,
    *,
    object_format: str,
    max_blob_bytes: int,
) -> _CommitSnapshot:
    """Verify commit -> trees -> regular blobs and return one immutable view."""

    root_tree_id = _read_verified_commit_root(
        repository, commit_id, object_format=object_format
    )
    tree_cache: dict[str, tuple[_TreeEntry, ...]] = {}
    verified_blobs: set[str] = set()
    blobs: list[_BlobEntry] = []
    observed_paths: set[str] = set()
    traversed_entries = 0

    def load_tree(tree_id: str) -> tuple[_TreeEntry, ...]:
        if tree_id not in tree_cache:
            if len(tree_cache) >= _MAX_TREE_OBJECTS:
                raise UnsupportedGitObjectError("pinned Git tree contains too many trees")
            tree_cache[tree_id] = _parse_tree(
                _read_verified_object(
                    repository,
                    tree_id,
                    object_format=object_format,
                    expected_type="tree",
                    max_bytes=_MAX_TREE_BYTES,
                ),
                object_format=object_format,
            )
        return tree_cache[tree_id]

    def visit(tree_id: str, prefix: str, depth: int) -> None:
        nonlocal traversed_entries
        if depth > _MAX_TREE_DEPTH:
            raise UnsupportedGitObjectError("pinned Git tree exceeds maximum depth")
        for entry in load_tree(tree_id):
            traversed_entries += 1
            if traversed_entries > _MAX_TREE_ENTRIES:
                raise UnsupportedGitObjectError("pinned Git tree contains too many entries")
            path = f"{prefix}/{entry.name}" if prefix else entry.name
            try:
                validate_relative_git_path(path)
            except ValueError as exc:
                raise PathSecurityError("invalid path in pinned Git tree") from exc
            if path in observed_paths:
                raise PathSecurityError("duplicate path in pinned Git tree")
            observed_paths.add(path)
            if entry.mode == "40000":
                visit(entry.object_id, path, depth + 1)
                continue
            if entry.mode not in {"100644", "100755"}:
                raise UnsupportedGitObjectError(
                    f"unsupported Git tree entry at {path!r}: mode={entry.mode}"
                )
            if entry.object_id not in verified_blobs:
                _read_verified_object(
                    repository,
                    entry.object_id,
                    object_format=object_format,
                    expected_type="blob",
                    max_bytes=max_blob_bytes,
                )
                verified_blobs.add(entry.object_id)
            blobs.append(_BlobEntry(entry.mode, entry.object_id, path))

    visit(root_tree_id, "", 0)
    ordered = tuple(sorted(blobs, key=lambda item: item.path))
    return _CommitSnapshot(object_format, commit_id, root_tree_id, ordered)


def _inspect_repository_head_oid(repository: Path) -> str:
    if not repository.exists():
        raise LibraryNotFoundError(f"external library does not exist: {repository}")
    if not repository.is_dir():
        raise LibraryNotInitializedError("external library path is not a directory")
    # Resolve the ref name only.  Peeling with ``^{commit}`` asks Git to parse
    # the object before our bounded reader can independently authenticate it.
    return _git(repository, ["rev-parse", "--verify", "HEAD"], text=True).stdout.strip()


def inspect_repository_commit(repository: Path) -> str:
    object_format = _detect_object_format(repository)
    commit_id = _inspect_repository_head_oid(repository)
    _validate_object_id(commit_id, object_format)
    _read_verified_commit_root(repository, commit_id, object_format=object_format)
    return commit_id


def check_repository_clean(
    repository: Path, *, max_blob_bytes: int = 64 * 1024 * 1024
) -> tuple[bool, list[str], list[str]]:
    object_format = _detect_object_format(repository)
    output = _git(
        repository,
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored=matching",
        ],
    ).stdout
    modified: list[str] = []
    untracked: list[str] = []
    for entry in output.split(b"\0"):
        if not entry:
            continue
        decoded = entry.decode("utf-8", errors="strict")
        if len(decoded) < 3:
            raise DirtyWorkingTreeError("malformed Git status output")
        path = decoded[3:]
        if decoded[:2] in {"??", "!!"}:
            untracked.append(path)
        else:
            modified.append(path)

    flag_output = _git(repository, ["ls-files", "-v", "-z"]).stdout
    for raw in flag_output.split(b"\0"):
        if not raw:
            continue
        try:
            tag, path_bytes = raw.split(b" ", 1)
            path = path_bytes.decode("utf-8", errors="strict")
        except (UnicodeError, ValueError) as exc:
            raise DirtyWorkingTreeError("malformed Git index flag output") from exc
        if tag == b"S" or tag.islower():
            modified.append(path)

    # Git status deliberately honors assume-unchanged/skip-worktree.  Compare
    # every stage-0 index blob with the actual regular worktree bytes as an
    # independent strict cleanliness oracle.
    index_output = _git(repository, ["ls-files", "--stage", "-z"]).stdout
    for raw in index_output.split(b"\0"):
        if not raw:
            continue
        try:
            metadata, path_bytes = raw.split(b"\t", 1)
            mode_bytes, object_id, stage = metadata.split(b" ", 2)
            path = path_bytes.decode("utf-8", errors="strict")
            mode = mode_bytes.decode("ascii")
        except (UnicodeError, ValueError) as exc:
            raise DirtyWorkingTreeError("malformed Git index entry") from exc
        if stage != b"0" or mode not in {"100644", "100755"}:
            modified.append(path)
            continue
        try:
            worktree_bytes, _ = read_contained_regular_file(
                repository, path, max_bytes=max_blob_bytes
            )
            index_bytes = _read_verified_object(
                repository,
                object_id.decode("ascii"),
                object_format=object_format,
                expected_type="blob",
                max_bytes=max_blob_bytes,
            )
            worktree_mode = (repository / path).lstat().st_mode
            expected_executable = mode == "100755"
            actual_executable = bool(worktree_mode & stat.S_IXUSR)
            if worktree_bytes != index_bytes or expected_executable != actual_executable:
                modified.append(path)
        except (OSError, UnicodeError, ValueError):
            modified.append(path)
    return not modified and not untracked, sorted(modified), sorted(untracked)


def _verified_tree_entry_at_path(
    repository: Path,
    root_tree_id: str,
    relative_path: str,
    *,
    object_format: str,
) -> _TreeEntry:
    parts = relative_path.split("/")
    if len(parts) > _MAX_TREE_DEPTH:
        raise GitlinkModeError("configured gitlink path exceeds maximum depth")
    tree_id = root_tree_id
    for ordinal, component in enumerate(parts):
        entries = _parse_tree(
            _read_verified_object(
                repository,
                tree_id,
                object_format=object_format,
                expected_type="tree",
                max_bytes=_MAX_TREE_BYTES,
            ),
            object_format=object_format,
        )
        matches = [entry for entry in entries if entry.name == component]
        if len(matches) != 1:
            raise GitlinkModeError("configured superproject HEAD has no unique gitlink")
        selected = matches[0]
        if ordinal != len(parts) - 1:
            if selected.mode != "40000":
                raise GitlinkModeError(
                    "configured superproject gitlink parent is not a tree"
                )
            tree_id = selected.object_id
            continue
        return selected
    raise GitlinkModeError("configured gitlink path is empty")  # pragma: no cover


def inspect_configured_gitlink(config: LibraryConfig) -> tuple[str, str, str]:
    """Validate the committed and staged gitlink, returning its superproject HEAD."""

    superproject = config.superproject_path.resolve(strict=False)
    checkout = (superproject / config.gitlink_path).resolve(strict=False)
    if checkout != config.library_path.resolve(strict=False):
        raise GitlinkModeError("configured library_path does not equal the gitlink checkout path")
    object_format = _detect_object_format(superproject)
    superproject_commit = _inspect_repository_head_oid(superproject)
    _validate_object_id(superproject_commit, object_format)
    root_tree_id = _read_verified_commit_root(
        superproject, superproject_commit, object_format=object_format
    )
    tree_entry = _verified_tree_entry_at_path(
        superproject,
        root_tree_id,
        config.gitlink_path,
        object_format=object_format,
    )
    if tree_entry.mode != "160000":
        raise GitlinkModeError(
            "configured superproject HEAD entry must be a mode-160000 gitlink"
        )
    if tree_entry.object_id != config.expected_commit:
        raise CommitMismatchError(
            "committed superproject gitlink object does not equal expected_commit"
        )

    output = _git(
        superproject,
        ["ls-files", "--stage", "--", config.gitlink_path],
    ).stdout
    lines = [line for line in output.splitlines() if line]
    if len(lines) != 1:
        raise GitlinkModeError("configured superproject path has no unique index entry")
    try:
        metadata, indexed_path = lines[0].split(b"\t", 1)
        mode_bytes, oid_bytes, stage_bytes = metadata.split(b" ", 2)
        mode = mode_bytes.decode("ascii")
        object_id = oid_bytes.decode("ascii")
        stage = stage_bytes.decode("ascii")
        path = indexed_path.decode("utf-8", errors="strict")
    except (UnicodeError, ValueError) as exc:
        raise GitlinkModeError("configured gitlink index entry is malformed") from exc
    if path != config.gitlink_path or mode != "160000" or stage != "0":
        raise GitlinkModeError("configured entry must be a stage-0 mode-160000 gitlink")
    if object_id != config.expected_commit:
        raise CommitMismatchError("configured gitlink object does not equal expected_commit")
    return mode, object_id, superproject_commit


def _indexed_worktree_fingerprint(
    repository: Path, *, max_blob_bytes: int
) -> list[dict[str, Any]]:
    """Describe actual tracked entries without following links or Git hints."""

    output = _git(repository, ["ls-files", "--stage", "-z"]).stdout
    observed: list[dict[str, Any]] = []
    for raw in output.split(b"\0"):
        if not raw:
            continue
        try:
            metadata, path_bytes = raw.split(b"\t", 1)
            mode_bytes, object_id_bytes, stage_bytes = metadata.split(b" ", 2)
            path = path_bytes.decode("utf-8", errors="strict")
            mode = mode_bytes.decode("ascii")
            object_id = object_id_bytes.decode("ascii")
            stage = stage_bytes.decode("ascii")
        except (UnicodeError, ValueError) as exc:
            raise LibraryNotInitializedError("malformed Git index entry") from exc
        entry: dict[str, Any] = {
            "path": path,
            "index_mode": mode,
            "index_object_id": object_id,
            "stage": stage,
        }
        candidate = repository / path
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            entry["worktree_kind"] = "missing"
        else:
            entry["worktree_mode"] = stat.S_IMODE(info.st_mode)
            if stat.S_ISREG(info.st_mode):
                try:
                    content, _ = read_contained_regular_file(
                        repository, path, max_bytes=max_blob_bytes
                    )
                except (OSError, ValueError):
                    entry["worktree_kind"] = "unsafe_regular"
                else:
                    entry["worktree_kind"] = "regular"
                    entry["worktree_size"] = len(content)
                    entry["worktree_sha256"] = hash_bytes(content)
            elif stat.S_ISDIR(info.st_mode):
                entry["worktree_kind"] = "directory"
            elif stat.S_ISLNK(info.st_mode):
                entry["worktree_kind"] = "symlink"
                entry["symlink_target"] = os.readlink(candidate)
            else:
                entry["worktree_kind"] = "special"
        observed.append(entry)
    return sorted(observed, key=lambda item: (item["path"], item["stage"]))


def enumerate_commit_blobs(
    repository: Path,
    expected_commit: str,
    *,
    max_blob_bytes: int = 64 * 1024 * 1024,
) -> list[dict[str, Any]]:
    object_format = _detect_object_format(repository)
    _validate_object_id(expected_commit, object_format)
    snapshot = _build_verified_commit_snapshot(
        repository,
        expected_commit,
        object_format=object_format,
        max_blob_bytes=max_blob_bytes,
    )
    return [entry.as_public_dict() for entry in snapshot.blobs]


class PinnedGitSource:
    """Verified handle whose reads are resolved from ``expected_commit``, never files."""

    def __init__(self, config: LibraryConfig) -> None:
        self.config = config
        self.repository = config.library_path.resolve(strict=False)
        self.expected_commit = config.expected_commit
        self.integrity, snapshot = self._verify_and_snapshot()
        self._object_format = snapshot.object_format
        self._entries = MappingProxyType(
            {entry.path: entry for entry in snapshot.blobs}
        )
        self._blob_ids = frozenset(entry.object_id for entry in snapshot.blobs)

    @classmethod
    def open(cls, config: LibraryConfig) -> "PinnedGitSource":
        return cls(config)

    def _verify_and_snapshot(self) -> tuple[UpstreamIntegrityReport, _CommitSnapshot]:
        (
            gitlink_mode,
            gitlink_object_id,
            superproject_commit,
        ) = inspect_configured_gitlink(self.config)
        object_format = _detect_object_format(self.repository)
        checked_out = _inspect_repository_head_oid(self.repository)
        _validate_object_id(checked_out, object_format)
        _validate_object_id(self.expected_commit, object_format)
        if checked_out != self.expected_commit:
            raise CommitMismatchError("external library HEAD does not match expected_commit")
        snapshot = _build_verified_commit_snapshot(
            self.repository,
            self.expected_commit,
            object_format=object_format,
            max_blob_bytes=self.config.max_blob_bytes,
        )
        clean, modified, untracked = check_repository_clean(
            self.repository, max_blob_bytes=self.config.max_blob_bytes
        )
        if not clean and self.config.require_clean_worktree:
            raise DirtyWorkingTreeError(
                "external library checkout must be completely clean before object access"
            )
        report = UpstreamIntegrityReport(
            library_id=self.config.library_id,
            library_path=str(self.repository),
            superproject_path=str(self.config.superproject_path.resolve(strict=False)),
            superproject_commit=superproject_commit,
            gitlink_path=self.config.gitlink_path,
            expected_commit=self.expected_commit,
            checked_out_commit=checked_out,
            commit_match=True,
            gitlink_mode=gitlink_mode,
            gitlink_object_id=gitlink_object_id,
            gitlink_valid=True,
            is_clean=clean,
            untracked_files_count=len(untracked),
            modified_files_count=len(modified),
            total_tracked_blobs=len(snapshot.blobs),
            integrity_status=(
                "VERIFIED" if clean else "PINNED_OBJECTS_VERIFIED_DIRTY"
            ),
        )
        return report, snapshot

    def verify(self) -> UpstreamIntegrityReport:
        report, _ = self._verify_and_snapshot()
        return report

    def enumerate_blobs(self) -> list[dict[str, Any]]:
        return [entry.as_public_dict() for entry in self._entries.values()]

    def read_blob(self, blob_id: str) -> bytes:
        if blob_id not in self._blob_ids:
            raise UnsupportedGitObjectError("blob is not reachable from expected_commit")
        return _read_verified_object(
            self.repository,
            blob_id,
            object_format=self._object_format,
            expected_type="blob",
            max_bytes=self.config.max_blob_bytes,
        )

    def read_path(self, source_relative_path: str) -> bytes:
        path = validate_relative_git_path(source_relative_path)
        try:
            entry = self._entries[path]
        except KeyError as exc:
            raise FileNotFoundError(
                f"path is not a regular blob in expected_commit: {path}"
            ) from exc
        return self.read_blob(entry.object_id)

    def read_blobs(self, blob_ids: list[str]) -> dict[str, bytes]:
        return {blob_id: self.read_blob(blob_id) for blob_id in sorted(set(blob_ids))}


def verify_library_integrity(config: LibraryConfig) -> UpstreamIntegrityReport:
    return PinnedGitSource.open(config).integrity


def snapshot_library_state(config: LibraryConfig) -> LibraryStateSnapshot:
    """Capture HEAD, index, status, tree, and configured gitlink without mutation."""

    mode, object_id, superproject_commit = inspect_configured_gitlink(config)
    head = inspect_repository_commit(config.library_path)
    tree = enumerate_commit_blobs(
        config.library_path,
        config.expected_commit,
        max_blob_bytes=config.max_blob_bytes,
    )
    library_index = _git(
        config.library_path,
        ["ls-files", "--stage", "-z"],
    ).stdout
    library_status = _git(
        config.library_path,
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored=matching",
        ],
    ).stdout
    superproject_index = _git(
        config.superproject_path,
        ["ls-files", "--stage", "-z"],
    ).stdout
    superproject_status = _git(
        config.superproject_path,
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored=matching",
        ],
    ).stdout
    gitlink_entry = {
        "superproject": str(config.superproject_path.resolve(strict=False)),
        "superproject_commit": superproject_commit,
        "path": config.gitlink_path,
        "mode": mode,
        "object_id": object_id,
    }
    return LibraryStateSnapshot(
        library_head=head,
        superproject_head=superproject_commit,
        library_index_hash=hash_bytes(library_index),
        library_status_hash=hash_bytes(library_status),
        library_worktree_hash=hash_json(
            _indexed_worktree_fingerprint(
                config.library_path, max_blob_bytes=config.max_blob_bytes
            )
        ),
        pinned_tree_hash=hash_json(tree),
        superproject_index_hash=hash_bytes(superproject_index),
        superproject_status_hash=hash_bytes(superproject_status),
        superproject_worktree_hash=hash_json(
            _indexed_worktree_fingerprint(
                config.superproject_path, max_blob_bytes=config.max_blob_bytes
            )
        ),
        superproject_gitlink_hash=hash_json(gitlink_entry),
    )
