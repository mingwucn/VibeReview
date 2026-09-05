"""Python workflow authority for TaskSpec execution through MockEngine."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from .cache import SemanticCache
from .engine import AgentEngine
from .hashing import code_fingerprint, hash_json, hash_text
from .promotion import (
    ContractImplementationError,
    ProposalValidationError,
    interpret_disposition,
    prepare_promotion,
    validate_proposal,
)
from .records import (
    AgentResult,
    AttemptOutcome,
    CacheSignature,
    ProjectManifest,
    RuntimeConfig,
    RuntimeResult,
    TaskAttemptRecord,
    TaskManifest,
    TaskSpec,
    TaskType,
    fallback_allowed,
)
from .repository import (
    GenerationStore,
    InjectedCrash,
    StagedRepositoryValidationError,
    StaleSnapshotError,
    atomic_write_text,
)
from .specs import TASK_SPECS
from .state import RepositorySnapshot
from .tasks import TaskWorkspace


SCIENTIFIC_CONTRACT_VERSION = "V1.5.1b"
RUNTIME_VERSION = "1.5"


class ProjectRuntime:
    def __init__(
        self,
        project_root: Path,
        *,
        config: RuntimeConfig | None = None,
        validator_fingerprint: str | None = None,
    ):
        self.project_root = project_root.resolve()
        self.config = config or RuntimeConfig()
        self.store = GenerationStore(
            self.project_root, self.config.writer_lock_timeout_seconds
        )
        self.tasks = TaskWorkspace(
            self.project_root, self.config.writer_lock_timeout_seconds
        )
        self.cache = SemanticCache(self.project_root / "work" / "cache")
        runtime_dir = Path(__file__).resolve().parent
        package_dir = runtime_dir.parent
        self.validator_fingerprint = validator_fingerprint or code_fingerprint(
            [
                package_dir / "models.py",
                package_dir / "validators.py",
                runtime_dir / "dto.py",
                runtime_dir / "promotion.py",
                runtime_dir / "records.py",
                runtime_dir / "specs.py",
            ],
            SCIENTIFIC_CONTRACT_VERSION,
        )

    @classmethod
    def create(
        cls,
        project_root: Path,
        *,
        project_name: str,
        initial_snapshot: RepositorySnapshot | None = None,
        config: RuntimeConfig | None = None,
        validator_fingerprint: str | None = None,
    ) -> "ProjectRuntime":
        runtime = cls(
            project_root,
            config=config,
            validator_fingerprint=validator_fingerprint,
        )
        runtime.project_root.mkdir(parents=True, exist_ok=True)
        manifest_path = runtime.project_root / "project_manifest.json"
        if manifest_path.exists():
            manifest = ProjectManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
            if manifest.scientific_contract_version != SCIENTIFIC_CONTRACT_VERSION:
                raise ValueError("project scientific-contract version mismatch")
        else:
            manifest = ProjectManifest(
                project_name=project_name,
                scientific_contract_version=SCIENTIFIC_CONTRACT_VERSION,
                runtime_version=RUNTIME_VERSION,
                created_at=datetime.now(UTC).isoformat(),
                runtime=runtime.config,
            )
            atomic_write_text(
                manifest_path, manifest.model_dump_json(indent=2) + "\n"
            )
        runtime.store.initialize(initial_snapshot)
        return runtime

    def run(
        self,
        task_type: TaskType,
        input_dto: BaseModel | dict[str, Any],
        *,
        dependency_ids: list[str],
        engines: list[AgentEngine],
    ) -> RuntimeResult:
        spec = TASK_SPECS[task_type]
        if isinstance(input_dto, dict):
            input_dto = spec.input_model.model_validate(input_dto)
        elif type(input_dto) is not spec.input_model:
            raise TypeError(
                f"{task_type.value} requires {spec.input_model.__name__}, "
                f"not {type(input_dto).__name__}"
            )
        if not engines:
            raise ValueError("at least one engine is required")

        all_records: list[TaskAttemptRecord] = []
        stale_rebuilds = 0
        while True:
            task_dir, manifest = self._create_task(spec, input_dto, dependency_ids)
            result = self._run_task_once(spec, task_dir, manifest, engines)
            all_records.extend(result.attempt_records)
            result = result.model_copy(
                update={
                    "attempt_records": all_records,
                    "stale_rebuilds": stale_rebuilds,
                }
            )
            if result.outcome is not AttemptOutcome.STALE_SNAPSHOT:
                return result
            if stale_rebuilds >= self.config.max_stale_rebuilds:
                return result
            stale_rebuilds += 1

    def _create_task(
        self, spec: TaskSpec, input_dto: BaseModel, dependency_ids: list[str]
    ) -> tuple[Path, TaskManifest]:
        generation, snapshot, _ = self.store.load_current()
        dependencies = {
            identifier: snapshot.dependency_hash(identifier)
            for identifier in dependency_ids
        }
        return self.tasks.create(
            spec=spec,
            input_dto=input_dto,
            base_generation=generation,
            dependencies=dependencies,
            snapshot=snapshot,
        )

    def _cache_signature(
        self, spec: TaskSpec, manifest: TaskManifest, engine: AgentEngine
    ) -> CacheSignature:
        return CacheSignature(
            task_type=spec.task_type,
            task_spec_version=spec.version,
            prompt_hash=manifest.instructions_hash,
            input_snapshot_hash=manifest.input_snapshot_hash,
            engine=engine.name,
            engine_version=engine.version,
            safe_engine_configuration_hash=hash_json(dict(engine.safe_configuration())),
            scientific_contract_version=SCIENTIFIC_CONTRACT_VERSION,
            validator_fingerprint=self.validator_fingerprint,
        )

    def _run_task_once(
        self,
        spec: TaskSpec,
        task_dir: Path,
        manifest: TaskManifest,
        engines: list[AgentEngine],
    ) -> RuntimeResult:
        base_snapshot, _ = self.store.load_generation(manifest.base_generation)
        records: list[TaskAttemptRecord] = []
        allowed_engines = engines[: 1 + self.config.max_fallback_engines]
        last_outcome = AttemptOutcome.ENGINE_EXECUTION_FAILURE
        last_engine: str | None = None

        for engine in allowed_engines:
            last_engine = engine.name
            signature = self._cache_signature(spec, manifest, engine)
            for _technical_attempt in range(self.config.technical_attempts_per_engine):
                attempt_dir, agent_task, attempt_id = self.tasks.next_attempt(
                    task_dir, engine.name, spec.task_type
                )
                cached = self.cache.get(signature)
                raw_proposal: dict[str, Any] | None = None
                proposal_model: BaseModel | None = None
                result: AgentResult

                if cached is not None:
                    try:
                        candidate = spec.proposal_model.model_validate(cached)
                        validate_proposal(spec, candidate, base_snapshot)
                    except (ValidationError, ProposalValidationError, AssertionError):
                        atomic_write_text(
                            attempt_dir / "cache_rejected.txt",
                            "cached proposal failed current schema or proposal validation\n",
                        )
                    else:
                        raw_proposal = cached
                        proposal_model = candidate

                if proposal_model is None:
                    try:
                        result = engine.execute(agent_task)
                    except Exception as exc:
                        result = AgentResult(
                            engine=engine.name,
                            engine_version=engine.version,
                            execution_succeeded=False,
                            output_text="",
                            stderr=str(exc),
                            execution_error=str(exc),
                        )
                else:
                    result = AgentResult(
                        engine=engine.name,
                        engine_version=engine.version,
                        execution_succeeded=True,
                        output_text=json.dumps(
                            raw_proposal, ensure_ascii=False, sort_keys=True
                        ),
                        stdout="cache hit\n",
                    )

                agent_result_path = self.tasks.write_agent_result(attempt_dir, result)
                relative_result_path = agent_result_path.relative_to(self.project_root)
                output_hash = hash_text(result.output_text) if result.output_text else None

                if not result.execution_succeeded:
                    last_outcome = AttemptOutcome.ENGINE_EXECUTION_FAILURE
                    record = self._record(
                        manifest,
                        attempt_id,
                        engine,
                        last_outcome,
                        False,
                        False,
                        None,
                        False,
                        [result.execution_error or "engine execution failed"],
                        output_hash,
                        relative_result_path,
                    )
                    self.tasks.write_attempt_record(attempt_dir, record)
                    records.append(record)
                    continue

                if raw_proposal is None:
                    try:
                        parsed = json.loads(result.output_text)
                        if not isinstance(parsed, dict):
                            raise ValueError("proposal root must be a JSON object")
                        raw_proposal = parsed
                    except (json.JSONDecodeError, ValueError) as exc:
                        last_outcome = AttemptOutcome.ENGINE_FORMAT_FAILURE
                        record = self._record(
                            manifest,
                            attempt_id,
                            engine,
                            last_outcome,
                            False,
                            False,
                            None,
                            False,
                            [str(exc)],
                            output_hash,
                            relative_result_path,
                        )
                        self.tasks.write_attempt_record(attempt_dir, record)
                        records.append(record)
                        continue
                    try:
                        proposal_model = spec.proposal_model.model_validate(raw_proposal)
                    except ValidationError as exc:
                        last_outcome = AttemptOutcome.ENGINE_SCHEMA_FAILURE
                        self.tasks.write_proposal(attempt_dir, raw_proposal)
                        record = self._record(
                            manifest,
                            attempt_id,
                            engine,
                            last_outcome,
                            True,
                            False,
                            None,
                            False,
                            [str(exc)],
                            output_hash,
                            relative_result_path,
                        )
                        self.tasks.write_attempt_record(attempt_dir, record)
                        records.append(record)
                        continue
                    try:
                        validate_proposal(spec, proposal_model, base_snapshot)
                    except (ProposalValidationError, AssertionError) as exc:
                        last_outcome = AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
                        self.tasks.write_proposal(attempt_dir, raw_proposal)
                        record = self._record(
                            manifest,
                            attempt_id,
                            engine,
                            last_outcome,
                            True,
                            True,
                            False,
                            False,
                            [str(exc)],
                            output_hash,
                            relative_result_path,
                        )
                        self.tasks.write_attempt_record(attempt_dir, record)
                        records.append(record)
                        continue

                assert raw_proposal is not None and proposal_model is not None
                self.tasks.write_proposal(attempt_dir, raw_proposal)
                try:
                    transition = interpret_disposition(spec, proposal_model)
                    promotion = prepare_promotion(spec, proposal_model)
                except ContractImplementationError as exc:
                    return self._terminal_failure(
                        manifest,
                        attempt_dir,
                        attempt_id,
                        engine,
                        AttemptOutcome.CONTRACT_IMPLEMENTATION_FAILURE,
                        str(exc),
                        output_hash,
                        relative_result_path,
                        records,
                    )
                except Exception as exc:
                    return self._terminal_failure(
                        manifest,
                        attempt_dir,
                        attempt_id,
                        engine,
                        AttemptOutcome.INTERNAL_RUNTIME_FAILURE,
                        str(exc),
                        output_hash,
                        relative_result_path,
                        records,
                    )

                try:
                    commit = self.store.commit(
                        base_generation=manifest.base_generation,
                        dependencies=dict(manifest.dependencies),
                        promotion=promotion,
                    )
                except StaleSnapshotError as exc:
                    return self._terminal_failure(
                        manifest,
                        attempt_dir,
                        attempt_id,
                        engine,
                        AttemptOutcome.STALE_SNAPSHOT,
                        str(exc),
                        output_hash,
                        relative_result_path,
                        records,
                    )
                except StagedRepositoryValidationError as exc:
                    return self._terminal_failure(
                        manifest,
                        attempt_dir,
                        attempt_id,
                        engine,
                        AttemptOutcome.CONTRACT_IMPLEMENTATION_FAILURE,
                        str(exc),
                        output_hash,
                        relative_result_path,
                        records,
                    )
                except (InjectedCrash, OSError) as exc:
                    return self._terminal_failure(
                        manifest,
                        attempt_dir,
                        attempt_id,
                        engine,
                        AttemptOutcome.TRANSACTION_FAILURE,
                        str(exc),
                        output_hash,
                        relative_result_path,
                        records,
                    )
                except Exception as exc:
                    return self._terminal_failure(
                        manifest,
                        attempt_dir,
                        attempt_id,
                        engine,
                        AttemptOutcome.INTERNAL_RUNTIME_FAILURE,
                        str(exc),
                        output_hash,
                        relative_result_path,
                        records,
                    )

                transition = transition.model_copy(update={"canonicalized": True})
                record = self._record(
                    manifest,
                    attempt_id,
                    engine,
                    AttemptOutcome.VALID_SCIENTIFIC_RESULT,
                    True,
                    True,
                    True,
                    True,
                    [],
                    output_hash,
                    relative_result_path,
                )
                self.tasks.write_attempt_record(attempt_dir, record)
                records.append(record)
                self.tasks.accept(
                    task_dir,
                    raw_proposal,
                    commit.generation,
                    commit.allocated_ids,
                    transition,
                )
                self.cache.put(signature, raw_proposal)
                return RuntimeResult(
                    outcome=AttemptOutcome.VALID_SCIENTIFIC_RESULT,
                    task_id=manifest.task_id,
                    generation=commit.generation,
                    engine=engine.name,
                    transition=transition,
                    attempt_records=records,
                    allocated_ids=commit.allocated_ids,
                )

            if not fallback_allowed(last_outcome):
                break

        return RuntimeResult(
            outcome=last_outcome,
            task_id=manifest.task_id,
            generation=None,
            engine=last_engine,
            transition=None,
            attempt_records=records,
        )

    def _terminal_failure(
        self,
        manifest: TaskManifest,
        attempt_dir: Path,
        attempt_id: str,
        engine: AgentEngine,
        outcome: AttemptOutcome,
        message: str,
        output_hash: str | None,
        agent_result_path: Path,
        previous_records: list[TaskAttemptRecord],
    ) -> RuntimeResult:
        record = self._record(
            manifest,
            attempt_id,
            engine,
            outcome,
            True,
            True,
            True,
            False,
            [message],
            output_hash,
            agent_result_path,
        )
        self.tasks.write_attempt_record(attempt_dir, record)
        return RuntimeResult(
            outcome=outcome,
            task_id=manifest.task_id,
            generation=None,
            engine=engine.name,
            transition=None,
            attempt_records=[*previous_records, record],
        )

    @staticmethod
    def _record(
        manifest: TaskManifest,
        attempt_id: str,
        engine: AgentEngine,
        outcome: AttemptOutcome,
        format_valid: bool,
        schema_valid: bool,
        proposal_validation_valid: bool | None,
        accepted_attempt: bool,
        validation_errors: list[str],
        output_hash: str | None,
        agent_result_path: Path,
    ) -> TaskAttemptRecord:
        return TaskAttemptRecord(
            task_id=manifest.task_id,
            attempt_id=attempt_id,
            engine=engine.name,
            engine_version=engine.version,
            outcome=outcome,
            format_valid=format_valid,
            schema_valid=schema_valid,
            proposal_validation_valid=proposal_validation_valid,
            accepted_attempt=accepted_attempt,
            validation_errors=validation_errors,
            output_hash=output_hash,
            agent_result_path=agent_result_path,
        )
