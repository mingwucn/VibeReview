"""Immutable task-snapshot creation and attempt-history persistence."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from pydantic import BaseModel

from .hashing import hash_file, hash_tree
from .locking import AdvisoryFileLock
from .records import AgentResult, AgentTask, TaskAttemptRecord, TaskManifest, TaskSpec
from .repository import atomic_write_text
from .state import RepositorySnapshot


class TaskWorkspace:
    def __init__(self, project_root: Path, lock_timeout_seconds: float = 30.0):
        self.project_root = project_root.resolve()
        self.tasks_root = self.project_root / "work" / "tasks"
        self.task_lock_path = self.project_root / "work" / ".task-id.lock"
        self.lock_timeout_seconds = lock_timeout_seconds

    def create(
        self,
        *,
        spec: TaskSpec,
        input_dto: BaseModel,
        base_generation: int,
        dependencies: dict[str, str],
        snapshot: RepositorySnapshot,
    ) -> tuple[Path, TaskManifest]:
        if type(input_dto) is not spec.input_model:
            raise TypeError(
                f"{spec.task_type.value} requires {spec.input_model.__name__}, "
                f"not {type(input_dto).__name__}"
            )
        task_dir, task_id = self._allocate_task_dir()
        try:
            input_dir = task_dir / "input"
            dependency_dir = input_dir / "dependencies"
            dependency_dir.mkdir(parents=True)
            instructions = spec.prompt_path.read_text(encoding="utf-8")
            atomic_write_text(task_dir / "instructions.md", instructions)
            atomic_write_text(
                input_dir / "input.json", input_dto.model_dump_json(indent=2) + "\n"
            )
            index = snapshot.object_index()
            for identifier in dependencies:
                if not re.fullmatch(r"[A-Za-z0-9_:-]+", identifier):
                    raise ValueError(f"unsafe dependency identifier {identifier!r}")
                value = index.get(identifier)
                if value is None:
                    raise KeyError(f"canonical dependency {identifier} does not exist")
                safe_name = identifier.replace(":", "__")
                atomic_write_text(
                    dependency_dir / f"{safe_name}.json",
                    value.model_dump_json(indent=2) + "\n",
                )
            manifest = TaskManifest(
                task_id=task_id,
                task_type=spec.task_type,
                task_spec_version=spec.version,
                base_generation=base_generation,
                dependencies=dependencies,
                instructions_hash=hash_file(task_dir / "instructions.md"),
                input_snapshot_hash=hash_tree(input_dir),
            )
            atomic_write_text(
                task_dir / "task_manifest.json",
                manifest.model_dump_json(indent=2) + "\n",
            )
            (task_dir / "attempts").mkdir()
            (task_dir / "accepted").mkdir()
            for path in [
                task_dir / "instructions.md",
                task_dir / "task_manifest.json",
                *input_dir.rglob("*.json"),
            ]:
                path.chmod(0o444)
            return task_dir, manifest
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
        task = AgentTask(
            task_id=task_dir.name,
            task_type=task_type,
            task_dir=task_dir,
            instructions_path=task_dir / "instructions.md",
            input_dir=task_dir / "input",
            attempt_dir=attempt_dir,
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
    def load_manifest(task_dir: Path) -> TaskManifest:
        return TaskManifest.model_validate_json(
            (task_dir / "task_manifest.json").read_text(encoding="utf-8")
        )
