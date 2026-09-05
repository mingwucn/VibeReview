"""Immutable generation store with freshness-checked atomic transactions."""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .hashing import hash_json
from .locking import AdvisoryFileLock
from .records import GenerationManifest
from .registry import CanonicalIdRegistry
from .state import RepositorySnapshot


REPOSITORY_FILE = "repository.json"
REGISTRY_FILE = "registry.json"
GENERATION_MANIFEST_FILE = "generation_manifest.json"
COMPLETION_MARKER = "COMPLETE"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


class CrashPoint(StrEnum):
    BEFORE_STAGED_VALIDATION = "before_staged_validation"
    DURING_GENERATION_MATERIALIZATION = "during_generation_materialization"
    BEFORE_COMPLETION_MARKER = "before_completion_marker"
    AFTER_COMPLETION_MARKER_BEFORE_CURRENT = "after_completion_marker_before_current"
    AFTER_CURRENT = "after_current"


class InjectedCrash(RuntimeError):
    pass


class StaleSnapshotError(RuntimeError):
    pass


class StagedRepositoryValidationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PromotionPayload:
    snapshot: RepositorySnapshot
    registry: CanonicalIdRegistry
    allocated_ids: dict[str, str]


@dataclass(frozen=True, slots=True)
class CommitResult:
    generation: int
    allocated_ids: dict[str, str]


Promotion = Callable[[RepositorySnapshot, CanonicalIdRegistry], PromotionPayload]


class GenerationStore:
    def __init__(self, project_root: Path, lock_timeout_seconds: float = 30.0):
        self.project_root = project_root.resolve()
        self.state_dir = self.project_root / "state"
        self.generations_dir = self.state_dir / "generations"
        self.current_path = self.state_dir / "CURRENT"
        self.writer_lock_path = self.state_dir / ".writer.lock"
        self.lock_timeout_seconds = lock_timeout_seconds

    def writer_lock(self) -> AdvisoryFileLock:
        return AdvisoryFileLock(self.writer_lock_path, self.lock_timeout_seconds)

    @staticmethod
    def generation_name(generation: int) -> str:
        return f"{generation:06d}"

    def generation_path(self, generation: int) -> Path:
        return self.generations_dir / self.generation_name(generation)

    def initialize(self, snapshot: RepositorySnapshot | None = None) -> int:
        self.generations_dir.mkdir(parents=True, exist_ok=True)
        with self.writer_lock():
            self._recover_locked()
            if self.current_path.exists():
                return self.current_generation()
            initial = snapshot or RepositorySnapshot()
            initial.validate_repository()
            registry = CanonicalIdRegistry.from_identifiers(initial.all_identifiers())
            generation_dir = self.generation_path(0)
            if generation_dir.exists():
                shutil.rmtree(generation_dir)
            generation_dir.mkdir(parents=True)
            self._write_generation_files(generation_dir, 0, None, initial, registry)
            atomic_write_text(generation_dir / COMPLETION_MARKER, "complete\n")
            self._seal_generation(generation_dir)
            _fsync_directory(generation_dir)
            atomic_write_text(self.current_path, f"{self.generation_name(0)}\n")
            return 0

    def current_generation(self) -> int:
        if not self.current_path.is_file():
            raise FileNotFoundError("repository CURRENT pointer does not exist")
        value = self.current_path.read_text(encoding="utf-8").strip()
        if len(value) != 6 or not value.isdigit():
            raise ValueError(f"invalid CURRENT pointer {value!r}")
        generation = int(value)
        self._require_complete_generation(generation)
        return generation

    def _require_complete_generation(self, generation: int) -> Path:
        path = self.generation_path(generation)
        if not path.is_dir() or not (path / COMPLETION_MARKER).is_file():
            raise ValueError(f"generation {generation} is not complete")
        for filename in (REPOSITORY_FILE, REGISTRY_FILE, GENERATION_MANIFEST_FILE):
            if not (path / filename).is_file():
                raise ValueError(f"generation {generation} lacks {filename}")
        return path

    def load_generation(
        self, generation: int
    ) -> tuple[RepositorySnapshot, CanonicalIdRegistry]:
        path = self._require_complete_generation(generation)
        snapshot = RepositorySnapshot.model_validate_json(
            (path / REPOSITORY_FILE).read_text(encoding="utf-8")
        )
        registry = CanonicalIdRegistry.model_validate_json(
            (path / REGISTRY_FILE).read_text(encoding="utf-8")
        )
        manifest = GenerationManifest.model_validate_json(
            (path / GENERATION_MANIFEST_FILE).read_text(encoding="utf-8")
        )
        if manifest.repository_hash != snapshot.canonical_hash():
            raise ValueError(f"generation {generation} repository hash mismatch")
        if manifest.registry_hash != hash_json(registry.model_dump(mode="json")):
            raise ValueError(f"generation {generation} registry hash mismatch")
        return snapshot, registry

    def load_current(self) -> tuple[int, RepositorySnapshot, CanonicalIdRegistry]:
        generation = self.current_generation()
        snapshot, registry = self.load_generation(generation)
        return generation, snapshot, registry

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
        crash_at: CrashPoint | None = None,
    ) -> CommitResult:
        self.generations_dir.mkdir(parents=True, exist_ok=True)
        with self.writer_lock():
            self._recover_locked()
            current = self.current_generation()
            if current != base_generation:
                raise StaleSnapshotError(
                    f"task generation {base_generation} is stale; current is {current}"
                )
            snapshot, registry = self.load_generation(current)
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
            payload = promotion(snapshot, registry)
            next_generation = current + 1
            final_path = self.generation_path(next_generation)
            if final_path.exists():
                raise FileExistsError(f"generation {next_generation} already exists")
            staging = self.generations_dir / (
                f".{self.generation_name(next_generation)}.staging-{uuid.uuid4().hex}"
            )
            staging.mkdir(parents=True)
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
            if crash_at is CrashPoint.BEFORE_STAGED_VALIDATION:
                raise InjectedCrash(crash_at.value)
            try:
                payload.snapshot.validate_repository()
            except Exception as exc:
                raise StagedRepositoryValidationError(str(exc)) from exc

            manifest = GenerationManifest(
                generation=next_generation,
                previous_generation=current,
                created_at=utc_now(),
                repository_hash=payload.snapshot.canonical_hash(),
                registry_hash=hash_json(payload.registry.model_dump(mode="json")),
            )
            atomic_write_text(
                staging / GENERATION_MANIFEST_FILE,
                manifest.model_dump_json(indent=2) + "\n",
            )
            if crash_at is CrashPoint.BEFORE_COMPLETION_MARKER:
                raise InjectedCrash(crash_at.value)
            atomic_write_text(staging / COMPLETION_MARKER, "complete\n")
            self._seal_generation(staging)
            _fsync_directory(staging)
            os.replace(staging, final_path)
            _fsync_directory(self.generations_dir)
            if crash_at is CrashPoint.AFTER_COMPLETION_MARKER_BEFORE_CURRENT:
                raise InjectedCrash(crash_at.value)
            atomic_write_text(
                self.current_path, f"{self.generation_name(next_generation)}\n"
            )
            if crash_at is CrashPoint.AFTER_CURRENT:
                raise InjectedCrash(crash_at.value)
            return CommitResult(next_generation, dict(payload.allocated_ids))

    def recover(self) -> int | None:
        self.generations_dir.mkdir(parents=True, exist_ok=True)
        with self.writer_lock():
            return self._recover_locked()

    def _recover_locked(self) -> int | None:
        for path in self.generations_dir.glob(".*.staging-*"):
            if path.is_dir():
                shutil.rmtree(path)

        complete: list[int] = []
        for path in self.generations_dir.iterdir():
            if not path.is_dir() or len(path.name) != 6 or not path.name.isdigit():
                continue
            generation = int(path.name)
            if (path / COMPLETION_MARKER).is_file():
                complete.append(generation)
            else:
                shutil.rmtree(path)

        current: int | None = None
        if self.current_path.exists():
            try:
                candidate = int(self.current_path.read_text(encoding="utf-8").strip())
                self._require_complete_generation(candidate)
                current = candidate
            except (ValueError, FileNotFoundError):
                current = None

        if current is None and complete:
            current = max(complete)
            atomic_write_text(self.current_path, f"{self.generation_name(current)}\n")
        elif current is not None:
            # Complete generations newer than CURRENT never crossed the atomic commit
            # point and are abandoned transaction products.
            for generation in complete:
                if generation > current:
                    shutil.rmtree(self.generation_path(generation))
        return current

    def _write_generation_files(
        self,
        generation_dir: Path,
        generation: int,
        previous_generation: int | None,
        snapshot: RepositorySnapshot,
        registry: CanonicalIdRegistry,
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

    @staticmethod
    def _seal_generation(generation_dir: Path) -> None:
        for path in generation_dir.rglob("*"):
            if path.is_file():
                path.chmod(0o444)
