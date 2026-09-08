"""Immutable task-bundle creation, resource snapshotting, and attempt history.

Every task directory follows the Milestone-A layout:

```text
work/tasks/TASKxxxx/
├── private/                     # never exposed to an engine
│   ├── invocation.json
│   ├── resource_sources.json
│   └── task_provenance.json     # authoritative integrity trust anchor
├── bundle/                      # immutable engine-facing template
│   ├── instructions.md
│   ├── bundle_manifest.json     # engine-readable, non-authoritative
│   ├── contracts/{input,proposal}.schema.json
│   └── input/{input.json, dependencies/, resources/RESxxxx/content<ext>}
├── attempts/NN-engine/workspace/  # fresh per-attempt copy of bundle/
└── accepted/
```
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from .hashing import hash_file, hash_tree
from .locking import AdvisoryFileLock
from .records import (
    AgentResult,
    AgentTask,
    ProjectContext,
    ResourceProvenance,
    ResourceSourceDependency,
    ResourceValidationError,
    SnapshottedResource,
    SnapshotSourceChangedError,
    TaskAttemptRecord,
    TaskManifest,
    TaskProvenance,
    TaskResourceRequest,
    TaskSpec,
    resource_destination,
    validate_resource_requests,
)
from .repository import atomic_write_text
from .state import RepositorySnapshot


BUNDLE_PROTOCOL_SECTION = (
    "\n---\n\n# Bundle protocol\n\n"
    "- Read `input/input.json`.\n"
    "- Read resources only through the resource entries in `bundle_manifest.json`.\n"
    "- Return exactly one JSON object conforming to `contracts/proposal.schema.json`.\n"
    "- Do not emit Markdown fences or explanatory text.\n"
    "- Do not allocate canonical scientific IDs.\n"
)


class BundleIntegrityError(RuntimeError):
    """A9: a bundle tree does not match the private integrity trust anchor."""


def bundle_file_path(bundle_dir: Path, relative_path: Path) -> Path:
    """Resolve a bundle-relative destination, rejecting escapes.

    Absolute paths and ``..`` components are forbidden, and the resolved
    candidate must stay inside the resolved bundle directory.
    """

    if relative_path.is_absolute():
        raise ResourceValidationError(
            f"bundle destination {relative_path} must be relative"
        )
    if ".." in relative_path.parts:
        raise ResourceValidationError(
            f"bundle destination {relative_path} must not contain '..' parts"
        )
    bundle_root = bundle_dir.resolve()
    candidate = (bundle_root / relative_path).resolve()
    if candidate != bundle_root and bundle_root not in candidate.parents:
        raise ResourceValidationError(
            f"bundle destination {relative_path} escapes the bundle"
        )
    return candidate


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stat_stamp(result: os.stat_result) -> tuple[int, int, int, int]:
    return (
        result.st_dev,
        result.st_ino,
        result.st_size,
        getattr(result, "st_mtime_ns", int(result.st_mtime * 1e9)),
    )


class TaskWorkspace:
    def __init__(
        self,
        project_root: Path,
        lock_timeout_seconds: float = 30.0,
        *,
        snapshot_chunk_size: int = 1024 * 1024,
        snapshot_hook: Callable[[Path, int], None] | None = None,
    ):
        if snapshot_chunk_size < 1:
            raise ValueError("snapshot_chunk_size must be >= 1")
        self.project_root = project_root.resolve()
        self.tasks_root = self.project_root / "work" / "tasks"
        self.task_lock_path = self.project_root / "work" / ".task-id.lock"
        self.lock_timeout_seconds = lock_timeout_seconds
        # Testing seam for A7: small chunks plus a hook called after each
        # written chunk make mid-copy source mutation deterministic.
        self.snapshot_chunk_size = snapshot_chunk_size
        self.snapshot_hook = snapshot_hook

    def create(
        self,
        *,
        spec: TaskSpec,
        invocation: BaseModel,
        base_generation: int,
        dependencies: dict[str, str],
        resource_requests: tuple[TaskResourceRequest, ...] = (),
        snapshot: RepositorySnapshot,
        context: ProjectContext,
    ) -> tuple[Path, TaskManifest, TaskProvenance]:
        if type(invocation) is not spec.invocation_model:
            raise TypeError(
                f"{spec.task_type.value} requires {spec.invocation_model.__name__}, "
                f"not {type(invocation).__name__}"
            )
        validate_resource_requests(resource_requests)
        task_dir, task_id = self._allocate_task_dir()
        try:
            private_dir = task_dir / "private"
            bundle_dir = task_dir / "bundle"
            contracts_dir = bundle_dir / "contracts"
            input_dir = bundle_dir / "input"
            dependency_dir = input_dir / "dependencies"
            for directory in (
                private_dir,
                contracts_dir,
                dependency_dir,
                input_dir / "resources",
            ):
                directory.mkdir(parents=True, exist_ok=True)

            atomic_write_text(
                private_dir / "invocation.json",
                invocation.model_dump_json(indent=2) + "\n",
            )
            atomic_write_text(
                private_dir / "resource_sources.json",
                json.dumps(
                    [request.model_dump(mode="json") for request in resource_requests],
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
            )

            snapshotted: list[SnapshottedResource] = []
            resource_provenance: list[ResourceProvenance] = []
            for request in resource_requests:
                resource, provenance_entry = self._snapshot_resource(
                    request, bundle_dir, context
                )
                snapshotted.append(resource)
                resource_provenance.append(provenance_entry)

            engine_input = spec.engine_input_builder(invocation, tuple(snapshotted))
            atomic_write_text(
                input_dir / "input.json", engine_input.model_dump_json(indent=2) + "\n"
            )

            instructions = (
                spec.prompt_path.read_text(encoding="utf-8") + BUNDLE_PROTOCOL_SECTION
            )
            atomic_write_text(bundle_dir / "instructions.md", instructions)
            atomic_write_text(
                contracts_dir / "input.schema.json",
                json.dumps(
                    spec.engine_input_model.model_json_schema(),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
            )
            atomic_write_text(
                contracts_dir / "proposal.schema.json",
                json.dumps(
                    spec.proposal_model.model_json_schema(),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
            )

            index = snapshot.object_index()
            dependency_entries: list[dict[str, str]] = []
            for identifier in dependencies:
                if not re.fullmatch(r"[A-Za-z0-9_:-]+", identifier):
                    raise ValueError(f"unsafe dependency identifier {identifier!r}")
                value = index.get(identifier)
                if value is None:
                    raise KeyError(f"canonical dependency {identifier} does not exist")
                safe_name = identifier.replace(":", "__")
                relative = f"input/dependencies/{safe_name}.json"
                atomic_write_text(
                    dependency_dir / f"{safe_name}.json",
                    value.model_dump_json(indent=2) + "\n",
                )
                dependency_entries.append(
                    {
                        "key": identifier,
                        "path": relative,
                        "sha256": hash_file(bundle_dir / relative),
                    }
                )

            bundle_manifest = {
                "task_type": spec.task_type.value,
                "task_spec_version": spec.version,
                "prompt_version": spec.prompt_version,
                "instructions": {
                    "path": "instructions.md",
                    "sha256": hash_file(bundle_dir / "instructions.md"),
                },
                "schemas": {
                    "input": {
                        "path": "contracts/input.schema.json",
                        "sha256": hash_file(contracts_dir / "input.schema.json"),
                    },
                    "proposal": {
                        "path": "contracts/proposal.schema.json",
                        "sha256": hash_file(contracts_dir / "proposal.schema.json"),
                    },
                },
                "engine_input": {
                    "path": "input/input.json",
                    "sha256": hash_file(input_dir / "input.json"),
                },
                "dependencies": dependency_entries,
                "resources": [
                    {
                        "resource_id": resource.resource_id,
                        "logical_name": resource.logical_name,
                        "path": resource.snapshot_relative_path.as_posix(),
                        "media_type": resource.media_type,
                        "size_bytes": resource.size_bytes,
                        "sha256": resource.content_hash,
                    }
                    for resource in snapshotted
                ],
            }
            atomic_write_text(
                bundle_dir / "bundle_manifest.json",
                json.dumps(bundle_manifest, ensure_ascii=False, sort_keys=True, indent=2)
                + "\n",
            )

            expected_immutable = {
                path.relative_to(bundle_dir).as_posix(): hash_file(path)
                for path in sorted(bundle_dir.rglob("*"))
                if path.is_file() and path.name != "bundle_manifest.json"
            }
            provenance = TaskProvenance(
                task_id=task_id,
                task_type=spec.task_type,
                task_spec_version=spec.version,
                prompt_version=spec.prompt_version,
                base_generation=base_generation,
                dependencies=dict(dependencies),
                instructions_hash=expected_immutable["instructions.md"],
                input_snapshot_hash=hash_tree(input_dir),
                engine_input_hash=expected_immutable["input/input.json"],
                input_schema_hash=expected_immutable["contracts/input.schema.json"],
                proposal_schema_hash=expected_immutable[
                    "contracts/proposal.schema.json"
                ],
                expected_bundle_manifest_hash=hash_file(
                    bundle_dir / "bundle_manifest.json"
                ),
                expected_immutable_files=expected_immutable,
                resources=tuple(resource_provenance),
            )
            atomic_write_text(
                private_dir / "task_provenance.json",
                provenance.model_dump_json(indent=2) + "\n",
            )
            (task_dir / "attempts").mkdir()
            (task_dir / "accepted").mkdir()
            for path in (*private_dir.rglob("*"), *bundle_dir.rglob("*")):
                if path.is_file():
                    path.chmod(0o444)
            return task_dir, provenance.task_manifest(), provenance
        except BaseException:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise

    def _allocate_task_dir(self) -> tuple[Path, str]:
        self.tasks_root.mkdir(parents=True, exist_ok=True)
        with AdvisoryFileLock(self.task_lock_path, self.lock_timeout_seconds):
            maximum = 0
            for path in self.tasks_root.glob("TASK*"):
                match = re.fullmatch(r"TASK([0-9]+)", path.name)
                if match:
                    maximum = max(maximum, int(match.group(1)))
            task_id = f"TASK{maximum + 1:04d}"
            task_dir = self.tasks_root / task_id
            task_dir.mkdir()
            return task_dir, task_id

    @staticmethod
    def _resolve_source(
        request: TaskResourceRequest, context: ProjectContext
    ) -> Path:
        try:
            resolved = request.source_path.resolve(strict=True)
        except OSError as exc:
            raise ResourceValidationError(
                f"resource source {request.source_path} for {request.resource_id} "
                f"does not resolve to an existing path: {exc}"
            ) from exc
        if not resolved.is_file():
            raise ResourceValidationError(
                f"resource source {resolved} for {request.resource_id} "
                "is not a regular file"
            )
        roots = tuple(root.resolve() for root in context.allowed_source_roots)
        if not any(resolved == root or root in resolved.parents for root in roots):
            raise ResourceValidationError(
                f"resource source {resolved} for {request.resource_id} "
                "escapes the allowed source roots"
            )
        return resolved

    def _snapshot_resource(
        self,
        request: TaskResourceRequest,
        bundle_dir: Path,
        context: ProjectContext,
    ) -> tuple[SnapshottedResource, ResourceProvenance]:
        """A7 copy-while-hashing snapshot of one resource into the bundle."""

        destination_relative = resource_destination(
            request.resource_id, request.media_type
        )
        destination = bundle_file_path(bundle_dir, destination_relative)
        resolved_source = self._resolve_source(request, context)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            f".{destination.name}.tmp-{uuid.uuid4().hex}"
        )
        digest = hashlib.sha256()
        size = 0
        try:
            with resolved_source.open("rb") as source_handle:
                before = os.fstat(source_handle.fileno())
                with temporary.open("wb") as target_handle:
                    chunk_index = 0
                    while True:
                        chunk = source_handle.read(self.snapshot_chunk_size)
                        if not chunk:
                            break
                        target_handle.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                        if self.snapshot_hook is not None:
                            self.snapshot_hook(resolved_source, chunk_index)
                        chunk_index += 1
                    after = os.fstat(source_handle.fileno())
                    target_handle.flush()
                    os.fsync(target_handle.fileno())
            self._verify_source_stability(request, resolved_source, before, after)
            temporary.chmod(0o444)
            os.replace(temporary, destination)
            _fsync_directory(destination.parent)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        content_hash = f"sha256:{digest.hexdigest()}"
        resource = SnapshottedResource(
            resource_id=request.resource_id,
            logical_name=request.logical_name,
            snapshot_relative_path=destination_relative,
            media_type=request.media_type,
            size_bytes=size,
            content_hash=content_hash,
        )
        provenance = ResourceProvenance(
            resource_id=request.resource_id,
            logical_name=request.logical_name,
            media_type=request.media_type,
            bundle_relative_path=destination_relative,
            source_path=resolved_source,
            source_dependency=ResourceSourceDependency(
                source_hash_at_snapshot=content_hash
            ),
            snapshot_hash=content_hash,
            size_bytes=size,
        )
        return resource, provenance

    @staticmethod
    def _verify_source_stability(
        request: TaskResourceRequest,
        resolved_source: Path,
        before: os.stat_result,
        after: os.stat_result,
    ) -> None:
        # fstat anchors the opened inode; the path stat additionally catches a
        # rename-replacement that swapped the path to a different inode.
        if _stat_stamp(after) != _stat_stamp(before):
            raise SnapshotSourceChangedError(request.resource_id, resolved_source)
        try:
            current = resolved_source.stat()
        except OSError as exc:
            raise SnapshotSourceChangedError(request.resource_id, resolved_source) from exc
        if (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino):
            raise SnapshotSourceChangedError(request.resource_id, resolved_source)

    def next_attempt(
        self, task_dir: Path, engine_name: str, task_type
    ) -> tuple[Path, AgentTask, str]:
        attempts = task_dir / "attempts"
        existing = [path for path in attempts.iterdir() if path.is_dir()]
        number = len(existing) + 1
        safe_engine = re.sub(r"[^A-Za-z0-9_-]", "_", engine_name).lower()
        attempt_id = f"{number:02d}-{safe_engine}"
        attempt_dir = attempts / attempt_id
        attempt_dir.mkdir()
        workspace_dir = attempt_dir / "workspace"
        shutil.copytree(task_dir / "bundle", workspace_dir)
        task = AgentTask(
            task_id=task_dir.name,
            task_type=task_type,
            workspace_dir=workspace_dir,
            instructions_path=workspace_dir / "instructions.md",
            input_dir=workspace_dir / "input",
            attempt_dir=attempt_dir,
            expected_bundle_manifest_hash=self.load_provenance(
                task_dir
            ).expected_bundle_manifest_hash,
        )
        return attempt_dir, task, attempt_id

    def write_agent_result(self, attempt_dir: Path, result: AgentResult) -> Path:
        atomic_write_text(attempt_dir / "stdout.txt", result.stdout)
        atomic_write_text(attempt_dir / "stderr.txt", result.stderr)
        path = attempt_dir / "agent_result.json"
        atomic_write_text(path, result.model_dump_json(indent=2) + "\n")
        return path

    def write_proposal(self, attempt_dir: Path, proposal: dict) -> Path:
        path = attempt_dir / "proposal.json"
        atomic_write_text(
            path,
            json.dumps(proposal, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        return path

    def write_attempt_record(
        self, attempt_dir: Path, record: TaskAttemptRecord
    ) -> None:
        atomic_write_text(
            attempt_dir / "attempt_record.json",
            record.model_dump_json(indent=2) + "\n",
        )

    def accept(
        self,
        task_dir: Path,
        proposal: dict,
        generation: int,
        allocated_ids: dict[str, str],
        transition: BaseModel,
    ) -> None:
        accepted = task_dir / "accepted"
        atomic_write_text(
            accepted / "proposal.json",
            json.dumps(proposal, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        atomic_write_text(
            accepted / "promotion.json",
            json.dumps(
                {
                    "generation": generation,
                    "allocated_ids": allocated_ids,
                    "transition": transition.model_dump(mode="json"),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
        )

    @staticmethod
    def load_provenance(task_dir: Path) -> TaskProvenance:
        return TaskProvenance.model_validate_json(
            (task_dir / "private" / "task_provenance.json").read_text(encoding="utf-8")
        )

    @staticmethod
    def load_manifest(task_dir: Path) -> TaskManifest:
        return TaskWorkspace.load_provenance(task_dir).task_manifest()

    @staticmethod
    def expected_immutable_map(provenance: TaskProvenance) -> dict[str, str]:
        expected = dict(provenance.expected_immutable_files)
        expected["bundle_manifest.json"] = provenance.expected_bundle_manifest_hash
        return expected

    def verify_bundle(
        self, task_dir: Path, provenance: TaskProvenance | None = None
    ) -> None:
        """A9: compare the master bundle against the private trust anchor."""

        if provenance is None:
            provenance = self.load_provenance(task_dir)
        self._verify_immutable_tree(
            task_dir / "bundle", self.expected_immutable_map(provenance)
        )

    def verify_workspace(
        self,
        task_dir: Path,
        workspace_dir: Path,
        provenance: TaskProvenance | None = None,
    ) -> None:
        """A9: compare an attempt workspace against the private trust anchor."""

        if provenance is None:
            provenance = self.load_provenance(task_dir)
        self._verify_immutable_tree(
            workspace_dir, self.expected_immutable_map(provenance)
        )

    @staticmethod
    def _verify_immutable_tree(root: Path, expected: dict[str, str]) -> None:
        actual: dict[str, str] = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise BundleIntegrityError(f"unexpected symlink in bundle: {relative}")
            if path.is_file():
                actual[relative] = hash_file(path)
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        changed = sorted(
            key
            for key in expected.keys() & actual.keys()
            if expected[key] != actual[key]
        )
        if missing or extra or changed:
            raise BundleIntegrityError(
                f"bundle integrity mismatch under {root}: "
                f"missing={missing} extra={extra} changed={changed}"
            )
