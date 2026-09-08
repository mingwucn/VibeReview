"""Immutable generation store with freshness-checked atomic transactions."""

from __future__ import annotations

import json
import os
import stat
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath

from .hashing import hash_bytes, hash_json
from .locking import AdvisoryFileLock
from .records import AppliedTaskReceipt, GenerationManifest
from .registry import CanonicalIdRegistry
from .state import RepositorySnapshot


REPOSITORY_FILE = "repository.json"
REGISTRY_FILE = "registry.json"
GENERATION_MANIFEST_FILE = "generation_manifest.json"
APPLIED_TASKS_FILE = "applied_tasks.json"
COMPLETION_MARKER = "COMPLETE"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory_descriptor(descriptor: int, display_path: Path) -> None:
    """Fsync a directory already opened without following its public path."""

    del display_path
    os.fsync(descriptor)


def _fsync_tree_directories(root: Path) -> None:
    """Persist nested directory entries from leaves through the staging root."""

    for directory, directories, _ in os.walk(root, topdown=False, followlinks=False):
        directory_path = Path(directory)
        for name in directories:
            info = (directory_path / name).lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise UnsafeRepositoryEntryError(
                    "cannot fsync an auxiliary symlink or special directory"
                )
        _fsync_directory(directory_path)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _atomic_write_text_at(
    directory_descriptor: int,
    name: str,
    display_path: Path,
    content: str,
) -> None:
    """Atomically write one control file relative to an already-open directory."""

    if not name or "/" in name or name in {".", ".."}:
        raise UnsafeRepositoryEntryError(
            f"repository control filename is unsafe: {display_path}"
        )
    temporary = f".{name}.tmp-{uuid.uuid4().hex}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | os.O_CLOEXEC,
            0o600,
            dir_fd=directory_descriptor,
        )
        pending = memoryview(content.encode("utf-8"))
        while pending:
            written = os.write(descriptor, pending)
            if written <= 0:
                raise OSError(f"short write to repository control file: {display_path}")
            pending = pending[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary,
            name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        _fsync_directory_descriptor(directory_descriptor, display_path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass


class CrashPoint(StrEnum):
    BEFORE_STAGED_VALIDATION = "before_staged_validation"
    DURING_GENERATION_MATERIALIZATION = "during_generation_materialization"
    BEFORE_COMPLETION_MARKER = "before_completion_marker"
    AFTER_COMPLETION_MARKER_BEFORE_CURRENT = "after_completion_marker_before_current"
    AFTER_CURRENT = "after_current"
    BEFORE_RECEIPT_CONSTRUCTION = "before_receipt_construction"
    AFTER_SCIENTIFIC_STAGING_BEFORE_RECEIPT_STAGING = "after_scientific_staging_before_receipt_staging"


class InjectedCrash(RuntimeError):
    pass


class StaleSnapshotError(RuntimeError):
    pass


class StagedRepositoryValidationError(RuntimeError):
    pass


class UnsafeRepositoryEntryError(ValueError):
    """A repository control file is not one private, regular filesystem object."""


@dataclass(frozen=True, slots=True)
class _RegularFileSnapshot:
    device: int
    inode: int
    size: int
    content_hash: str


MAX_GENERATION_CONTROL_BYTES = 64 * 1024 * 1024
MAX_AUXILIARY_FILE_BYTES = 256 * 1024 * 1024
MAX_AUXILIARY_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_AUXILIARY_ENTRIES = 10_000


def _read_regular_at(
    directory_descriptor: int,
    name: str,
    display_path: Path,
    *,
    max_bytes: int,
) -> tuple[bytes, _RegularFileSnapshot]:
    file_flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    file_descriptor: int | None = None
    try:
        try:
            file_descriptor = os.open(
                name, file_flags, dir_fd=directory_descriptor
            )
        except OSError as exc:
            raise UnsafeRepositoryEntryError(
                f"repository control file is unsafe: {display_path}"
            ) from exc
        info = os.fstat(file_descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise UnsafeRepositoryEntryError(
                f"repository control file must be regular and single-linked: {display_path}"
            )
        if info.st_size > max_bytes:
            raise UnsafeRepositoryEntryError(
                f"repository control file exceeds its byte limit: {display_path}"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(file_descriptor, min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise UnsafeRepositoryEntryError(
                    f"repository control file exceeds its byte limit: {display_path}"
                )
        content = b"".join(chunks)
        current = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        final_info = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
            or (final_info.st_dev, final_info.st_ino) != (info.st_dev, info.st_ino)
            or final_info.st_nlink != 1
            or final_info.st_size != len(content)
        ):
            raise UnsafeRepositoryEntryError(
                f"repository control file changed while being read: {display_path}"
            )
        return content, _RegularFileSnapshot(
            device=info.st_dev,
            inode=info.st_ino,
            size=len(content),
            content_hash=hash_bytes(content),
        )
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)


def read_contained_regular_file(
    root: Path,
    relative_path: str,
    *,
    max_bytes: int = MAX_GENERATION_CONTROL_BYTES,
) -> tuple[bytes, _RegularFileSnapshot]:
    """Read a bounded regular file through no-follow descriptors at every level."""

    parsed = PurePosixPath(relative_path)
    if (
        not relative_path
        or parsed.is_absolute()
        or parsed.as_posix() != relative_path
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        raise UnsafeRepositoryEntryError("repository path must be canonical and relative")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptors: list[int] = []
    ancestry: list[tuple[int, str, int, int]] = []
    try:
        try:
            root_descriptor = os.open(root, directory_flags)
        except OSError as exc:
            raise UnsafeRepositoryEntryError(
                f"repository control root is unsafe: {root}"
            ) from exc
        descriptors.append(root_descriptor)
        root_info = os.fstat(root_descriptor)
        for component in parsed.parts[:-1]:
            parent_descriptor = descriptors[-1]
            try:
                descriptor = os.open(
                    component, directory_flags, dir_fd=parent_descriptor
                )
            except OSError as exc:
                raise UnsafeRepositoryEntryError(
                    f"repository path contains an unsafe directory: {relative_path}"
                ) from exc
            info = os.fstat(descriptor)
            ancestry.append((parent_descriptor, component, info.st_dev, info.st_ino))
            descriptors.append(descriptor)
        result = _read_regular_at(
            descriptors[-1],
            parsed.name,
            root.joinpath(*parsed.parts),
            max_bytes=max_bytes,
        )
        for parent_descriptor, component, device, inode in reversed(ancestry):
            current = os.stat(
                component, dir_fd=parent_descriptor, follow_symlinks=False
            )
            if (
                not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino) != (device, inode)
            ):
                raise UnsafeRepositoryEntryError(
                    f"repository directory changed while being read: {relative_path}"
                )
        current_root = root.lstat()
        if (
            not stat.S_ISDIR(current_root.st_mode)
            or (current_root.st_dev, current_root.st_ino)
            != (root_info.st_dev, root_info.st_ino)
        ):
            raise UnsafeRepositoryEntryError(
                f"repository control root changed while being read: {root}"
            )
        return result
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_regular_file(
    path: Path, *, max_bytes: int = MAX_GENERATION_CONTROL_BYTES
) -> tuple[bytes, _RegularFileSnapshot]:
    """Read one file without following its final path component.

    The parent directory and file descriptors remain open for the complete read,
    and the directory entry is rechecked afterward.  This rejects symlinks,
    special files, hardlinks, and replace-while-reading attacks on generation
    control files.
    """

    return read_contained_regular_file(
        path.parent, path.name, max_bytes=max_bytes
    )


def _require_real_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise UnsafeRepositoryEntryError(f"repository directory is absent: {path}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafeRepositoryEntryError(
            f"repository directory must not be a symlink or special file: {path}"
        )


def _open_or_create_directory_at(
    parent_descriptor: int,
    name: str,
    display_path: Path,
) -> int:
    """Create/open one private directory without following its directory entry."""

    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
    except FileExistsError:
        pass
    except OSError as exc:
        raise UnsafeRepositoryEntryError(
            f"repository directory cannot be created safely: {display_path}"
        ) from exc
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_descriptor,
        )
    except OSError as exc:
        raise UnsafeRepositoryEntryError(
            f"repository directory is unsafe: {display_path}"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise UnsafeRepositoryEntryError(
                f"repository directory changed while being opened: {display_path}"
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@dataclass(slots=True)
class _GenerationLayout:
    project_root: Path
    generations_path_display: Path
    root_descriptor: int
    state_descriptor: int
    generations_descriptor: int

    @property
    def generations_path(self) -> Path:
        # Linux and WSL2 expose an open directory descriptor as a stable path.
        # Children reached through this path remain under the opened inode even
        # if an attacker renames and replaces the public directory entry.
        return Path(f"/proc/self/fd/{self.generations_descriptor}")

    def verify(self) -> None:
        """Require public layout entries still to name the held directories."""

        try:
            opened_root = os.fstat(self.root_descriptor)
            current_root = self.project_root.lstat()
            opened_state = os.fstat(self.state_descriptor)
            current_state = os.stat(
                "state", dir_fd=self.root_descriptor, follow_symlinks=False
            )
            opened_generations = os.fstat(self.generations_descriptor)
            current_generations = os.stat(
                "generations",
                dir_fd=self.state_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise UnsafeRepositoryEntryError(
                "repository state layout changed while writer lock was held"
            ) from exc
        checks = (
            (opened_root, current_root),
            (opened_state, current_state),
            (opened_generations, current_generations),
        )
        if any(
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            for opened, current in checks
        ):
            raise UnsafeRepositoryEntryError(
                "repository state layout changed while writer lock was held"
            )

    def close(self) -> None:
        for descriptor in (
            self.generations_descriptor,
            self.state_descriptor,
            self.root_descriptor,
        ):
            os.close(descriptor)


class _GenerationWriterLock:
    """Writer lock coupled to one stable, descriptor-opened repository layout."""

    def __init__(self, store: "GenerationStore") -> None:
        self._store = store
        self._advisory: AdvisoryFileLock | None = None
        self.layout: _GenerationLayout | None = None

    def __enter__(self) -> "_GenerationWriterLock":
        layout = self._store._open_layout()
        advisory = AdvisoryFileLock(
            self._store.writer_lock_path,
            self._store.lock_timeout_seconds,
            directory_opener=lambda: os.dup(layout.state_descriptor),
        )
        entered = False
        try:
            advisory.__enter__()
            entered = True
            layout.verify()
        except BaseException:
            if entered:
                advisory.__exit__(None, None, None)
            layout.close()
            raise
        self._advisory = advisory
        self.layout = layout
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._advisory is None or self.layout is None:
            raise RuntimeError("generation writer lock was not entered")
        layout_error: BaseException | None = None
        try:
            self.layout.verify()
        except BaseException as error:
            layout_error = error
        try:
            self._advisory.__exit__(exc_type, exc, traceback)
        finally:
            self.layout.close()
            self._advisory = None
            self.layout = None
        if exc_type is None and layout_error is not None:
            raise layout_error


def _entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _entry_exists_at(directory_descriptor: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _remove_repository_tree(path: Path) -> None:
    """Remove one validated transaction tree, reopening sealed directories only."""

    _require_real_directory(path)
    for directory, directories, _ in os.walk(path, topdown=True, followlinks=False):
        directory_path = Path(directory)
        os.chmod(directory_path, 0o700, follow_symlinks=False)
        for name in list(directories):
            info = (directory_path / name).lstat()
            if not stat.S_ISDIR(info.st_mode):
                (directory_path / name).unlink()
                directories.remove(name)
    shutil.rmtree(path)


@dataclass(frozen=True, slots=True)
class PromotionPayload:
    snapshot: RepositorySnapshot
    registry: CanonicalIdRegistry
    allocated_ids: dict[str, str]


@dataclass(frozen=True, slots=True)
class CommitResult:
    generation: int
    allocated_ids: dict[str, str]
    receipt: AppliedTaskReceipt | None = None


Promotion = Callable[[RepositorySnapshot, CanonicalIdRegistry], PromotionPayload]


def _copy_promotion_payload(payload: PromotionPayload) -> PromotionPayload:
    """Detach mutable nested models and mappings at a transaction hook boundary."""

    return PromotionPayload(
        snapshot=RepositorySnapshot.model_validate(
            payload.snapshot.model_dump(mode="json")
        ),
        registry=CanonicalIdRegistry.model_validate(
            payload.registry.model_dump(mode="json")
        ),
        allocated_ids=dict(payload.allocated_ids),
    )


class AuxiliaryStagingWriter:
    """Narrow no-overwrite writer for trusted generation auxiliary files."""

    __slots__ = (
        "__before_mutation",
        "__directories",
        "__root",
        "__total_bytes",
        "__written",
    )

    def __init__(
        self,
        root: Path,
        *,
        before_mutation: Callable[[], None] | None = None,
    ) -> None:
        self.__root = root
        self.__before_mutation = before_mutation
        self.__written: dict[str, tuple[str, int, int]] = {}
        self.__directories: set[str] = set()
        self.__total_bytes = 0

    def write_bytes(self, relative_path: str, content: bytes) -> None:
        parsed = PurePosixPath(relative_path)
        if (
            not relative_path
            or parsed.is_absolute()
            or parsed.as_posix() != relative_path
            or any(part in {"", ".", ".."} for part in parsed.parts)
        ):
            raise ValueError("auxiliary paths must be canonical POSIX relative paths")
        if len(content) > MAX_AUXILIARY_FILE_BYTES:
            raise ValueError("auxiliary file exceeds its materialization byte limit")
        if self.__total_bytes + len(content) > MAX_AUXILIARY_TOTAL_BYTES:
            raise ValueError("auxiliary tree exceeds its materialization byte limit")
        directories = {
            PurePosixPath(*parsed.parts[:index]).as_posix()
            for index in range(1, len(parsed.parts))
        }
        if len(self.__written) + 1 + len(self.__directories | directories) > MAX_AUXILIARY_ENTRIES:
            raise ValueError("auxiliary tree exceeds its materialization entry limit")
        if self.__before_mutation is not None:
            self.__before_mutation()
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptors = [os.open(self.__root, directory_flags)]
        file_descriptor: int | None = None
        try:
            for component in parsed.parts[:-1]:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptors[-1])
                except FileExistsError:
                    pass
                descriptors.append(
                    os.open(component, directory_flags, dir_fd=descriptors[-1])
                )
            file_descriptor = os.open(
                parsed.name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | os.O_CLOEXEC,
                0o600,
                dir_fd=descriptors[-1],
            )
            view = memoryview(content)
            while view:
                written = os.write(file_descriptor, view)
                if written <= 0:
                    raise OSError("short write while staging auxiliary content")
                view = view[written:]
            os.fsync(file_descriptor)
            info = os.fstat(file_descriptor)
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            for descriptor in reversed(descriptors):
                os.close(descriptor)
        self.__written[relative_path] = (hash_bytes(content), info.st_dev, info.st_ino)
        self.__directories.update(directories)
        self.__total_bytes += len(content)

    def validate(self) -> None:
        actual: set[str] = set()
        actual_directories: set[str] = set()
        allowed_directories = {
            PurePosixPath(*PurePosixPath(path).parts[:index]).as_posix()
            for path in self.__written
            for index in range(1, len(PurePosixPath(path).parts))
        }
        for directory, directories, filenames in os.walk(
            self.__root, topdown=True, followlinks=False
        ):
            directory_path = Path(directory)
            for name in directories:
                path = directory_path / name
                info = path.lstat()
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError("auxiliary tree contains a symlink or special directory")
                actual_directories.add(path.relative_to(self.__root).as_posix())
            for name in filenames:
                path = directory_path / name
                relative = path.relative_to(self.__root).as_posix()
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("auxiliary tree contains a symlink, hardlink, or special file")
                actual.add(relative)
                expected = self.__written.get(relative)
                if expected is None:
                    raise ValueError("auxiliary callback created an unregistered file")
                expected_hash, expected_device, expected_inode = expected
                if (info.st_dev, info.st_ino) != (expected_device, expected_inode):
                    raise ValueError("auxiliary callback replaced a staged file")
                content, secure_info = read_contained_regular_file(
                    self.__root,
                    relative,
                    max_bytes=MAX_AUXILIARY_FILE_BYTES,
                )
                if (
                    (secure_info.device, secure_info.inode)
                    != (expected_device, expected_inode)
                    or hash_bytes(content) != expected_hash
                ):
                    raise ValueError("auxiliary callback changed staged file bytes")
        if actual != set(self.__written):
            raise ValueError("auxiliary callback removed a staged file")
        if actual_directories != allowed_directories:
            raise ValueError("auxiliary callback created an unregistered directory")


def _validated_auxiliary_snapshot(
    root: Path, requested_paths: set[str] | None = None
) -> tuple[str, dict[str, bytes]]:
    requested = requested_paths or set()
    for relative in requested:
        parsed = PurePosixPath(relative)
        if (
            not relative
            or parsed.is_absolute()
            or parsed.as_posix() != relative
            or any(part in {"", ".", ".."} for part in parsed.parts)
        ):
            raise ValueError("requested auxiliary path is not canonical")

    def inventory() -> dict[str, tuple[str, int, int, int]]:
        _require_real_directory(root)
        observed: dict[str, tuple[str, int, int, int]] = {}
        for directory, directories, filenames in os.walk(
            root, topdown=True, followlinks=False
        ):
            directory_path = Path(directory)
            for name in directories:
                path = directory_path / name
                info = path.lstat()
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError(
                        "generation auxiliary tree contains an unsafe directory"
                    )
                relative = path.relative_to(root).as_posix()
                observed[relative] = ("directory", info.st_dev, info.st_ino, 0)
                if len(observed) > MAX_AUXILIARY_ENTRIES:
                    raise ValueError("generation auxiliary tree exceeds its entry limit")
            for name in filenames:
                path = directory_path / name
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("generation auxiliary tree contains an unsafe file")
                relative = path.relative_to(root).as_posix()
                observed[relative] = ("file", info.st_dev, info.st_ino, info.st_size)
                if len(observed) > MAX_AUXILIARY_ENTRIES:
                    raise ValueError("generation auxiliary tree exceeds its entry limit")
        return observed

    before = inventory()
    file_sizes = [entry[3] for entry in before.values() if entry[0] == "file"]
    if sum(file_sizes) > MAX_AUXILIARY_TOTAL_BYTES:
        raise ValueError("generation auxiliary tree exceeds its total byte limit")
    entries: list[dict[str, str]] = []
    requested_bytes: dict[str, bytes] = {}
    for relative, (kind, device, inode, _) in sorted(before.items()):
        if kind == "directory":
            entries.append({"path": relative, "type": "directory"})
            continue
        content, observed = read_contained_regular_file(
            root,
            relative,
            max_bytes=MAX_AUXILIARY_FILE_BYTES,
        )
        if (observed.device, observed.inode) != (device, inode):
            raise ValueError("generation auxiliary file changed during hashing")
        entries.append(
            {"path": relative, "type": "file", "hash": hash_bytes(content)}
        )
        if relative in requested:
            requested_bytes[relative] = content
    if inventory() != before:
        raise ValueError("generation auxiliary tree changed during hashing")
    missing = requested - set(requested_bytes)
    if missing:
        raise ValueError("requested generation auxiliary file is absent")
    return hash_json(entries), requested_bytes


def _validated_auxiliary_hash(root: Path) -> str:
    return _validated_auxiliary_snapshot(root)[0]


StagingMaterializer = Callable[
    [AuxiliaryStagingWriter, int, PromotionPayload], None
]


class GenerationStore:
    def __init__(self, project_root: Path, lock_timeout_seconds: float = 30.0):
        self.project_root = project_root.resolve()
        self.state_dir = self.project_root / "state"
        self.generations_dir = self.state_dir / "generations"
        self.current_path = self.state_dir / "CURRENT"
        self.writer_lock_path = self.state_dir / ".writer.lock"
        self.lock_timeout_seconds = lock_timeout_seconds

    def _open_layout(self) -> _GenerationLayout:
        """Validate/create the internal layout before the lock can mutate state.

        The state and generations entries are opened relative to no-follow
        descriptors. A pre-existing symlink or special file therefore fails
        before `.writer.lock`, CURRENT, or a generation can be created through
        it.
        """

        self.project_root.mkdir(parents=True, exist_ok=True)
        root_descriptor: int | None = None
        state_descriptor: int | None = None
        generations_descriptor: int | None = None
        try:
            root_descriptor = os.open(
                self.project_root,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            state_descriptor = _open_or_create_directory_at(
                root_descriptor, "state", self.state_dir
            )
            generations_descriptor = _open_or_create_directory_at(
                state_descriptor, "generations", self.generations_dir
            )
            result = _GenerationLayout(
                project_root=self.project_root,
                generations_path_display=self.generations_dir,
                root_descriptor=root_descriptor,
                state_descriptor=state_descriptor,
                generations_descriptor=generations_descriptor,
            )
            result.verify()
            root_descriptor = None
            state_descriptor = None
            generations_descriptor = None
            return result
        finally:
            if generations_descriptor is not None:
                os.close(generations_descriptor)
            if state_descriptor is not None:
                os.close(state_descriptor)
            if root_descriptor is not None:
                os.close(root_descriptor)

    def writer_lock(self) -> _GenerationWriterLock:
        return _GenerationWriterLock(self)

    @staticmethod
    def generation_name(generation: int) -> str:
        return f"{generation:06d}"

    def generation_path(self, generation: int) -> Path:
        return self.generations_dir / self.generation_name(generation)

    def _generation_path_at(
        self, layout: _GenerationLayout, generation: int
    ) -> Path:
        return layout.generations_path / self.generation_name(generation)

    def _require_complete_generation_at(
        self, layout: _GenerationLayout, generation: int
    ) -> Path:
        path = self._generation_path_at(layout, generation)
        try:
            _require_real_directory(path)
        except UnsafeRepositoryEntryError as exc:
            raise ValueError(f"generation {generation} is not complete") from exc
        if not _entry_exists(path / COMPLETION_MARKER):
            raise ValueError(f"generation {generation} is not complete")
        marker, _ = _read_regular_file(path / COMPLETION_MARKER, max_bytes=32)
        if marker != b"complete\n":
            raise ValueError(f"generation {generation} has an invalid completion marker")
        core_bytes: dict[str, bytes] = {}
        for filename in (
            REPOSITORY_FILE,
            REGISTRY_FILE,
            GENERATION_MANIFEST_FILE,
            APPLIED_TASKS_FILE,
        ):
            if not _entry_exists(path / filename):
                raise ValueError(f"generation {generation} lacks {filename}")
            core_bytes[filename] = _read_regular_file(path / filename)[0]
        try:
            manifest = GenerationManifest.model_validate_json(
                core_bytes[GENERATION_MANIFEST_FILE]
            )
        except Exception as exc:
            raise ValueError(f"generation {generation} manifest is invalid") from exc
        expected_previous = generation - 1 if generation > 0 else None
        if (
            manifest.generation != generation
            or manifest.previous_generation != expected_previous
        ):
            raise ValueError(
                f"generation {generation} manifest owner or lineage mismatch"
            )
        return path

    def _load_generation_at(
        self,
        layout: _GenerationLayout,
        generation: int,
    ) -> tuple[RepositorySnapshot, CanonicalIdRegistry]:
        path = self._require_complete_generation_at(layout, generation)
        repository_bytes, _ = _read_regular_file(path / REPOSITORY_FILE)
        registry_bytes, _ = _read_regular_file(path / REGISTRY_FILE)
        manifest_bytes, _ = _read_regular_file(path / GENERATION_MANIFEST_FILE)
        snapshot = RepositorySnapshot.model_validate_json(repository_bytes)
        registry = CanonicalIdRegistry.model_validate_json(registry_bytes)
        manifest = GenerationManifest.model_validate_json(manifest_bytes)
        if manifest.repository_hash != snapshot.canonical_hash():
            raise ValueError(f"generation {generation} repository hash mismatch")
        if manifest.registry_hash != hash_json(registry.model_dump(mode="json")):
            raise ValueError(f"generation {generation} registry hash mismatch")
        registry.validate_covers_identifiers(snapshot.all_identifiers())
        auxiliary = path / "auxiliary"
        if manifest.auxiliary_hash is None:
            if auxiliary.exists() or auxiliary.is_symlink():
                raise ValueError(f"generation {generation} has unanchored auxiliary data")
        elif _validated_auxiliary_hash(auxiliary) != manifest.auxiliary_hash:
            raise ValueError(f"generation {generation} auxiliary hash mismatch")
        return snapshot, registry

    def _current_generation_at(self, layout: _GenerationLayout) -> int:
        if not _entry_exists_at(layout.state_descriptor, "CURRENT"):
            raise FileNotFoundError("repository CURRENT pointer does not exist")
        raw, _ = _read_regular_at(
            layout.state_descriptor,
            "CURRENT",
            self.current_path,
            max_bytes=64,
        )
        try:
            value = raw.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise ValueError("repository CURRENT pointer is not UTF-8") from exc
        if len(value) != 6 or not value.isdigit():
            raise ValueError(f"invalid CURRENT pointer {value!r}")
        generation = int(value)
        self._require_complete_generation_at(layout, generation)
        return generation

    def _load_receipts_at(
        self, layout: _GenerationLayout, generation: int
    ) -> tuple[AppliedTaskReceipt, ...]:
        path = self._require_complete_generation_at(layout, generation)
        receipts_bytes, _ = _read_regular_file(path / APPLIED_TASKS_FILE)
        raw = json.loads(receipts_bytes)
        return tuple(AppliedTaskReceipt.model_validate(item) for item in raw)

    def initialize(self, snapshot: RepositorySnapshot | None = None) -> int:
        with self.writer_lock() as locked:
            assert locked.layout is not None
            layout = locked.layout
            self._recover_locked(layout)
            layout.verify()
            if _entry_exists_at(layout.state_descriptor, "CURRENT"):
                return self._current_generation_at(layout)
            supplied = snapshot or RepositorySnapshot()
            initial = RepositorySnapshot.model_validate(
                supplied.model_dump(mode="json")
            )
            layout.verify()
            initial.validate_repository()
            registry = CanonicalIdRegistry.from_identifiers(initial.all_identifiers())
            generation_dir = self._generation_path_at(layout, 0)
            layout.verify()
            if _entry_exists(generation_dir):
                _remove_repository_tree(generation_dir)
            layout.verify()
            os.mkdir(self.generation_name(0), dir_fd=layout.generations_descriptor)
            self._write_generation_files(generation_dir, 0, None, initial, registry)
            atomic_write_text(generation_dir / COMPLETION_MARKER, "complete\n")
            self._seal_generation(generation_dir)
            _fsync_directory(generation_dir)
            _fsync_directory_descriptor(
                layout.generations_descriptor, layout.generations_path_display
            )
            layout.verify()
            _atomic_write_text_at(
                layout.state_descriptor,
                "CURRENT",
                self.current_path,
                f"{self.generation_name(0)}\n",
            )
            return 0

    def current_generation(self) -> int:
        if not _entry_exists(self.current_path):
            raise FileNotFoundError("repository CURRENT pointer does not exist")
        raw, _ = read_contained_regular_file(
            self.project_root, "state/CURRENT", max_bytes=64
        )
        try:
            value = raw.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise ValueError("repository CURRENT pointer is not UTF-8") from exc
        if len(value) != 6 or not value.isdigit():
            raise ValueError(f"invalid CURRENT pointer {value!r}")
        generation = int(value)
        self._require_complete_generation(generation)
        return generation

    def _require_complete_generation(self, generation: int) -> Path:
        path = self.generation_path(generation)
        try:
            _require_real_directory(path)
        except UnsafeRepositoryEntryError as exc:
            raise ValueError(f"generation {generation} is not complete") from exc
        if not _entry_exists(path / COMPLETION_MARKER):
            raise ValueError(f"generation {generation} is not complete")
        marker, _ = read_contained_regular_file(
            self.project_root,
            f"state/generations/{self.generation_name(generation)}/{COMPLETION_MARKER}",
            max_bytes=32,
        )
        if marker != b"complete\n":
            raise ValueError(f"generation {generation} has an invalid completion marker")
        core_bytes: dict[str, bytes] = {}
        for filename in (
            REPOSITORY_FILE,
            REGISTRY_FILE,
            GENERATION_MANIFEST_FILE,
            APPLIED_TASKS_FILE,
        ):
            if not _entry_exists(path / filename):
                raise ValueError(f"generation {generation} lacks {filename}")
            core_bytes[filename] = read_contained_regular_file(
                self.project_root,
                f"state/generations/{self.generation_name(generation)}/{filename}",
            )[0]
        try:
            manifest = GenerationManifest.model_validate_json(
                core_bytes[GENERATION_MANIFEST_FILE]
            )
        except Exception as exc:
            raise ValueError(f"generation {generation} manifest is invalid") from exc
        expected_previous = generation - 1 if generation > 0 else None
        if (
            manifest.generation != generation
            or manifest.previous_generation != expected_previous
        ):
            raise ValueError(
                f"generation {generation} manifest owner or lineage mismatch"
            )
        return path

    def load_generation(
        self, generation: int
    ) -> tuple[RepositorySnapshot, CanonicalIdRegistry]:
        snapshot, registry, _ = self.load_generation_auxiliary(generation, set())
        return snapshot, registry

    def load_generation_auxiliary(
        self,
        generation: int,
        relative_paths: set[str],
    ) -> tuple[RepositorySnapshot, CanonicalIdRegistry, dict[str, bytes]]:
        """Load core state and selected auxiliary bytes from one anchored tree scan."""

        path = self._require_complete_generation(generation)
        prefix = f"state/generations/{self.generation_name(generation)}"
        repository_bytes, _ = read_contained_regular_file(
            self.project_root, f"{prefix}/{REPOSITORY_FILE}"
        )
        registry_bytes, _ = read_contained_regular_file(
            self.project_root, f"{prefix}/{REGISTRY_FILE}"
        )
        manifest_bytes, _ = read_contained_regular_file(
            self.project_root, f"{prefix}/{GENERATION_MANIFEST_FILE}"
        )
        snapshot = RepositorySnapshot.model_validate_json(repository_bytes)
        registry = CanonicalIdRegistry.model_validate_json(registry_bytes)
        manifest = GenerationManifest.model_validate_json(manifest_bytes)
        expected_previous = generation - 1 if generation > 0 else None
        if (
            manifest.generation != generation
            or manifest.previous_generation != expected_previous
        ):
            raise ValueError(
                f"generation {generation} manifest owner or lineage mismatch"
            )
        if manifest.repository_hash != snapshot.canonical_hash():
            raise ValueError(f"generation {generation} repository hash mismatch")
        if manifest.registry_hash != hash_json(registry.model_dump(mode="json")):
            raise ValueError(f"generation {generation} registry hash mismatch")
        registry.validate_covers_identifiers(snapshot.all_identifiers())
        auxiliary = path / "auxiliary"
        requested_bytes: dict[str, bytes] = {}
        if manifest.auxiliary_hash is None:
            if auxiliary.exists() or auxiliary.is_symlink():
                raise ValueError(f"generation {generation} has unanchored auxiliary data")
            if relative_paths:
                raise ValueError(
                    f"generation {generation} has no requested auxiliary data"
                )
        else:
            auxiliary_hash, requested_bytes = _validated_auxiliary_snapshot(
                auxiliary, relative_paths
            )
            if auxiliary_hash != manifest.auxiliary_hash:
                raise ValueError(f"generation {generation} auxiliary hash mismatch")
        return snapshot, registry, requested_bytes

    def load_current(self) -> tuple[int, RepositorySnapshot, CanonicalIdRegistry]:
        generation = self.current_generation()
        snapshot, registry = self.load_generation(generation)
        return generation, snapshot, registry

    def load_receipts(
        self, generation: int
    ) -> tuple[AppliedTaskReceipt, ...]:
        self._require_complete_generation(generation)
        receipts_bytes, _ = read_contained_regular_file(
            self.project_root,
            f"state/generations/{self.generation_name(generation)}/{APPLIED_TASKS_FILE}",
        )
        raw = json.loads(receipts_bytes)
        return tuple(AppliedTaskReceipt.model_validate(item) for item in raw)

    def load_current_receipts(self) -> tuple[AppliedTaskReceipt, ...]:
        generation = self.current_generation()
        return self.load_receipts(generation)

    def dependency_hashes(
        self, generation: int, identifiers: list[str]
    ) -> dict[str, str]:
        snapshot, _ = self.load_generation(generation)
        return {identifier: snapshot.dependency_hash(identifier) for identifier in identifiers}

    def commit(
        self,
        *,
        base_generation: int,
        dependencies: dict[str, str],
        promotion: Promotion,
        receipt_factory: Callable[[int, RepositorySnapshot, PromotionPayload], AppliedTaskReceipt] | None = None,
        staging_materializer: StagingMaterializer | None = None,
        crash_at: CrashPoint | None = None,
    ) -> CommitResult:
        with self.writer_lock() as locked:
            assert locked.layout is not None
            layout = locked.layout
            self._recover_locked(layout)
            layout.verify()
            current = self._current_generation_at(layout)
            if current != base_generation:
                raise StaleSnapshotError(
                    f"task generation {base_generation} is stale; current is {current}"
                )
            snapshot, registry = self._load_generation_at(layout, current)
            for identifier, expected_hash in dependencies.items():
                try:
                    actual_hash = snapshot.dependency_hash(identifier)
                except KeyError as exc:
                    raise StaleSnapshotError(str(exc)) from exc
                if actual_hash != expected_hash:
                    raise StaleSnapshotError(
                        f"dependency {identifier} changed since task creation"
                    )

            # Canonical ID allocation occurs inside this callback, after both lock and
            # freshness checks have succeeded.
            payload = _copy_promotion_payload(promotion(snapshot, registry))
            layout.verify()
            try:
                payload.snapshot.validate_repository()
                payload.registry.validate_covers_identifiers(
                    payload.snapshot.all_identifiers()
                )
            except Exception as exc:
                raise StagedRepositoryValidationError(str(exc)) from exc
            next_generation = current + 1
            final_path = self._generation_path_at(layout, next_generation)
            if _entry_exists(final_path):
                raise FileExistsError(f"generation {next_generation} already exists")

            if crash_at is CrashPoint.BEFORE_RECEIPT_CONSTRUCTION:
                raise InjectedCrash(crash_at.value)

            receipt: AppliedTaskReceipt | None = None
            if receipt_factory is not None:
                proposed_receipt = receipt_factory(
                    next_generation, snapshot, _copy_promotion_payload(payload)
                )
                receipt = AppliedTaskReceipt.model_validate(
                    proposed_receipt.model_dump(mode="json")
                )
            layout.verify()

            staging_name = (
                f".{self.generation_name(next_generation)}.staging-{uuid.uuid4().hex}"
            )
            staging = layout.generations_path / staging_name
            os.mkdir(staging_name, dir_fd=layout.generations_descriptor)
            if crash_at is CrashPoint.DURING_GENERATION_MATERIALIZATION:
                atomic_write_text(staging / ".partial", "incomplete\n")
                raise InjectedCrash(crash_at.value)

            # The snapshot is complete state, so writing it materializes unchanged and
            # changed canonical objects without ever editing generation N.
            atomic_write_text(
                staging / REPOSITORY_FILE,
                payload.snapshot.model_dump_json(indent=2) + "\n",
            )
            atomic_write_text(
                staging / REGISTRY_FILE,
                payload.registry.model_dump_json(indent=2) + "\n",
            )

            if crash_at is CrashPoint.AFTER_SCIENTIFIC_STAGING_BEFORE_RECEIPT_STAGING:
                raise InjectedCrash(crash_at.value)

            inherited_receipts = self._load_receipts_at(layout, current)
            new_receipts = list(inherited_receipts)
            if receipt is not None:
                new_receipts.append(receipt)
            atomic_write_text(
                staging / APPLIED_TASKS_FILE,
                json.dumps(
                    [r.model_dump(mode="json") for r in new_receipts],
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
            )

            # Trusted runtime extensions may materialize files that must cross the
            # same atomic boundary as the promoted snapshot. The callback receives a
            # path-confined, no-overwrite writer for auxiliary/ only; it never sees
            # the generation staging path. Any exception leaves CURRENT unchanged.
            auxiliary_hash: str | None = None
            if staging_materializer is not None:
                core_names = {
                    REPOSITORY_FILE,
                    REGISTRY_FILE,
                    APPLIED_TASKS_FILE,
                }
                core_snapshots = {
                    name: _read_regular_file(staging / name)[1]
                    for name in core_names
                }
                auxiliary = staging / "auxiliary"
                auxiliary.mkdir()
                writer = AuxiliaryStagingWriter(
                    auxiliary, before_mutation=layout.verify
                )
                staging_materializer(
                    writer, next_generation, _copy_promotion_payload(payload)
                )
                layout.verify()
                writer.validate()
                try:
                    current_core = {
                        name: _read_regular_file(staging / name)[1]
                        for name in core_names
                    }
                except UnsafeRepositoryEntryError as exc:
                    raise ValueError(
                        "staging materializer changed core generation files"
                    ) from exc
                if current_core != core_snapshots:
                    raise ValueError("staging materializer changed core generation files")
                unexpected = {
                    path.name for path in staging.iterdir()
                    if path.name not in {*core_names, "auxiliary"}
                }
                if unexpected:
                    raise ValueError("staging materializer created an unexpected top-level entry")
                auxiliary_hash = _validated_auxiliary_hash(auxiliary)
                _fsync_tree_directories(auxiliary)

            if crash_at is CrashPoint.BEFORE_STAGED_VALIDATION:
                raise InjectedCrash(crash_at.value)
            try:
                staged_repository_bytes, _ = _read_regular_file(
                    staging / REPOSITORY_FILE
                )
                staged_registry_bytes, _ = _read_regular_file(
                    staging / REGISTRY_FILE
                )
                staged_snapshot = RepositorySnapshot.model_validate_json(
                    staged_repository_bytes
                )
                staged_registry = CanonicalIdRegistry.model_validate_json(
                    staged_registry_bytes
                )
                staged_snapshot.validate_repository()
                staged_registry.validate_covers_identifiers(
                    staged_snapshot.all_identifiers()
                )
            except Exception as exc:
                raise StagedRepositoryValidationError(str(exc)) from exc

            manifest = GenerationManifest(
                generation=next_generation,
                previous_generation=current,
                created_at=utc_now(),
                repository_hash=staged_snapshot.canonical_hash(),
                registry_hash=hash_json(staged_registry.model_dump(mode="json")),
                auxiliary_hash=auxiliary_hash,
            )
            atomic_write_text(
                staging / GENERATION_MANIFEST_FILE,
                manifest.model_dump_json(indent=2) + "\n",
            )
            if crash_at is CrashPoint.BEFORE_COMPLETION_MARKER:
                raise InjectedCrash(crash_at.value)
            atomic_write_text(staging / COMPLETION_MARKER, "complete\n")
            layout.verify()
            self._seal_generation(staging)
            _fsync_directory(staging)
            layout.verify()
            os.replace(
                staging_name,
                self.generation_name(next_generation),
                src_dir_fd=layout.generations_descriptor,
                dst_dir_fd=layout.generations_descriptor,
            )
            _fsync_directory_descriptor(
                layout.generations_descriptor, layout.generations_path_display
            )
            if crash_at is CrashPoint.AFTER_COMPLETION_MARKER_BEFORE_CURRENT:
                raise InjectedCrash(crash_at.value)
            layout.verify()
            _atomic_write_text_at(
                layout.state_descriptor,
                "CURRENT",
                self.current_path,
                f"{self.generation_name(next_generation)}\n",
            )
            if crash_at is CrashPoint.AFTER_CURRENT:
                raise InjectedCrash(crash_at.value)
            return CommitResult(
                next_generation, dict(payload.allocated_ids), receipt=receipt
            )

    def recover(self) -> int | None:
        with self.writer_lock() as locked:
            assert locked.layout is not None
            return self._recover_locked(locked.layout)

    def _recover_locked(self, layout: _GenerationLayout) -> int | None:
        layout.verify()
        generations_path = layout.generations_path
        for path in generations_path.glob(".*.staging-*"):
            layout.verify()
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                _remove_repository_tree(path)
            else:
                path.unlink()

        complete: list[int] = []
        for path in generations_path.iterdir():
            if len(path.name) != 6 or not path.name.isdigit():
                continue
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise UnsafeRepositoryEntryError(
                    f"generation path is a symlink or special file: {path}"
                )
            generation = int(path.name)
            marker_path = path / COMPLETION_MARKER
            if _entry_exists(marker_path):
                marker, _ = _read_regular_file(marker_path, max_bytes=32)
                if marker != b"complete\n":
                    raise ValueError(
                        f"generation {generation} has an invalid completion marker"
                    )
                self._require_complete_generation_at(layout, generation)
                complete.append(generation)
            else:
                layout.verify()
                _remove_repository_tree(path)

        current: int | None = None
        if _entry_exists_at(layout.state_descriptor, "CURRENT"):
            try:
                current_bytes, _ = _read_regular_at(
                    layout.state_descriptor,
                    "CURRENT",
                    self.current_path,
                    max_bytes=64,
                )
                candidate = int(current_bytes.decode("utf-8", errors="strict").strip())
                self._require_complete_generation_at(layout, candidate)
                current = candidate
            except UnsafeRepositoryEntryError:
                raise
            except (UnicodeDecodeError, ValueError, FileNotFoundError):
                current = None

        if current is None and complete:
            current = max(complete)
            layout.verify()
            _atomic_write_text_at(
                layout.state_descriptor,
                "CURRENT",
                self.current_path,
                f"{self.generation_name(current)}\n",
            )
        elif current is not None:
            # Complete generations newer than CURRENT never crossed the atomic commit
            # point and are abandoned transaction products.
            for generation in complete:
                if generation > current:
                    layout.verify()
                    _remove_repository_tree(
                        self._generation_path_at(layout, generation)
                    )
        return current

    def _write_generation_files(
        self,
        generation_dir: Path,
        generation: int,
        previous_generation: int | None,
        snapshot: RepositorySnapshot,
        registry: CanonicalIdRegistry,
        receipts: tuple[AppliedTaskReceipt, ...] = (),
    ) -> None:
        repository_hash = snapshot.canonical_hash()
        registry_hash = hash_json(registry.model_dump(mode="json"))
        manifest = GenerationManifest(
            generation=generation,
            previous_generation=previous_generation,
            created_at=utc_now(),
            repository_hash=repository_hash,
            registry_hash=registry_hash,
        )
        atomic_write_text(
            generation_dir / REPOSITORY_FILE,
            snapshot.model_dump_json(indent=2) + "\n",
        )
        atomic_write_text(
            generation_dir / REGISTRY_FILE,
            registry.model_dump_json(indent=2) + "\n",
        )
        atomic_write_text(
            generation_dir / GENERATION_MANIFEST_FILE,
            manifest.model_dump_json(indent=2) + "\n",
        )
        atomic_write_text(
            generation_dir / APPLIED_TASKS_FILE,
            json.dumps(
                [r.model_dump(mode="json") for r in receipts],
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
        )

    @staticmethod
    def _seal_generation(generation_dir: Path) -> None:
        _require_real_directory(generation_dir)
        directory_paths: list[Path] = []
        for directory, directories, filenames in os.walk(
            generation_dir, topdown=True, followlinks=False
        ):
            directory_path = Path(directory)
            directory_paths.append(directory_path)
            for name in directories:
                info = (directory_path / name).lstat()
                if not stat.S_ISDIR(info.st_mode):
                    raise UnsafeRepositoryEntryError(
                        "generation tree contains a symlink or special directory"
                    )
            for name in filenames:
                path = directory_path / name
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise UnsafeRepositoryEntryError(
                        "generation tree contains a symlink, hardlink, or special file"
                    )
                os.chmod(path, 0o444, follow_symlinks=False)
        for directory_path in reversed(directory_paths):
            os.chmod(directory_path, 0o555, follow_symlinks=False)
