"""Python workflow authority for TaskSpec execution through AgentEngine."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from .cache import SemanticCache
from .engine import AgentEngine
from .hashing import code_fingerprint, hash_file, hash_json, hash_text
from .promotion import (
    ContractImplementationError,
    ProposalValidationError,
    interpret_disposition,
    prepare_promotion,
    validate_proposal,
)
from .records import (
    AgentResult,
    AttemptFailure,
    AttemptOutcome,
    CacheSignature,
    ProjectContext,
    ProjectManifest,
    RuntimeConfig,
    RuntimeResult,
    TaskAttemptRecord,
    TaskManifest,
    TaskProvenance,
    TaskSpec,
    TaskSpecNotExecutableError,
    TaskType,
    fallback_allowed,
    validate_resource_requests,
)
from .repository import (
    GenerationStore,
    InjectedCrash,
    StagedRepositoryValidationError,
    StaleSnapshotError,
    atomic_write_text,
)
from .specs import TASK_SPECS, validate_task_spec_executable
from .state import RepositorySnapshot
from .tasks import BundleIntegrityError, TaskWorkspace


SCIENTIFIC_CONTRACT_VERSION = "V1.5.1b"
RUNTIME_VERSION = "1.6"


class ProjectRuntime:
    def __init__(
        self,
        project_root: Path,
        *,
        config: RuntimeConfig | None = None,
        validator_fingerprint: str | None = None,
        allowed_source_roots: tuple[Path, ...] = (),
        snapshot_chunk_size: int = 1024 * 1024,
        snapshot_hook: Callable[[Path, int], None] | None = None,
    ):
        self.project_root = project_root.resolve()
        self.allowed_source_roots = tuple(allowed_source_roots)
        self.config = config or RuntimeConfig()
        self.store = GenerationStore(
            self.project_root, self.config.writer_lock_timeout_seconds
        )
        self.tasks = TaskWorkspace(
            self.project_root,
            self.config.writer_lock_timeout_seconds,
            snapshot_chunk_size=snapshot_chunk_size,
            snapshot_hook=snapshot_hook,
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
        allowed_source_roots: tuple[Path, ...] = (),
        snapshot_chunk_size: int = 1024 * 1024,
        snapshot_hook: Callable[[Path, int], None] | None = None,
    ) -> "ProjectRuntime":
        runtime = cls(
            project_root,
            config=config,
            validator_fingerprint=validator_fingerprint,
            allowed_source_roots=allowed_source_roots,
            snapshot_chunk_size=snapshot_chunk_size,
            snapshot_hook=snapshot_hook,
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
        invocation: BaseModel | dict[str, Any],
        *,
        engines: list[AgentEngine],
    ) -> RuntimeResult:
        spec = TASK_SPECS[task_type]
        try:
            validate_task_spec_executable(spec)
        except TaskSpecNotExecutableError:
            return RuntimeResult(
                outcome=AttemptOutcome.TASK_TYPE_NOT_IMPLEMENTED,
                task_id="",
                generation=None,
                engine=None,
                transition=None,
                attempt_records=[],
            )
        if isinstance(invocation, dict):
            invocation = spec.invocation_model.model_validate(invocation)
        elif type(invocation) is not spec.invocation_model:
            raise TypeError(
                f"{task_type.value} requires {spec.invocation_model.__name__}, "
                f"not {type(invocation).__name__}"
            )
        if not engines:
            raise ValueError("at least one engine is required")

        all_records: list[TaskAttemptRecord] = []
        stale_rebuilds = 0
        while True:
            task_dir, manifest, provenance = self._create_task(spec, invocation)
            result = self._run_task_once(spec, task_dir, manifest, provenance, engines)
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
        self, spec: TaskSpec, invocation: BaseModel
    ) -> tuple[Path, TaskManifest, TaskProvenance]:
        generation, snapshot, _ = self.store.load_current()
        dependency_keys = spec.dependency_builder(invocation, snapshot)
        dependencies = {
            identifier: snapshot.dependency_hash(identifier)
            for identifier in dependency_keys
        }
        context = ProjectContext(
            project_root=self.project_root,
            allowed_source_roots=self.allowed_source_roots,
        )
        resource_requests = spec.resource_builder(invocation, snapshot, context)
        validate_resource_requests(resource_requests)
        return self.tasks.create(
            spec=spec,
            invocation=invocation,
            base_generation=generation,
            dependencies=dependencies,
            resource_requests=resource_requests,
            snapshot=snapshot,
            context=context,
        )

    def _cache_signature(
        self,
        spec: TaskSpec,
        manifest: TaskManifest,
        provenance: TaskProvenance,
        engine: AgentEngine,
    ) -> CacheSignature:
        return CacheSignature(
            task_type=spec.task_type,
            task_spec_version=spec.version,
            prompt_hash=manifest.instructions_hash,
            input_schema_hash=provenance.input_schema_hash,
            proposal_schema_hash=provenance.proposal_schema_hash,
            dependency_hashes=dict(manifest.dependencies),
            resource_hashes={
                resource.resource_id: resource.snapshot_hash
                for resource in provenance.resources
            },
            engine_input_hash=provenance.engine_input_hash,
            bundle_manifest_hash=provenance.expected_bundle_manifest_hash,
            engine=engine.name,
            engine_version=engine.version,
            safe_engine_configuration_hash=hash_json(dict(engine.safe_configuration())),
            scientific_contract_version=SCIENTIFIC_CONTRACT_VERSION,
            validator_fingerprint=self.validator_fingerprint,
        )

    @staticmethod
    def _require_fresh_resources(provenance: TaskProvenance) -> None:
        """A11: re-hash external-file sources inside the writer-locked commit."""

        for resource in provenance.resources:
            dependency = resource.source_dependency
            if dependency.type != "external_file":
                continue
            try:
                actual = hash_file(resource.source_path)
            except OSError as exc:
                raise StaleSnapshotError(
                    f"resource {resource.resource_id} source "
                    f"{resource.source_path} is no longer readable"
                ) from exc
            if actual != dependency.source_hash_at_snapshot:
                raise StaleSnapshotError(
                    f"resource {resource.resource_id} source changed since snapshot"
                )

    def _run_task_once(
        self,
        spec: TaskSpec,
        task_dir: Path,
        manifest: TaskManifest,
        provenance: TaskProvenance,
        engines: list[AgentEngine],
    ) -> RuntimeResult:
        try:
            self.tasks.verify_bundle(task_dir, provenance)
        except BundleIntegrityError:
            return RuntimeResult(
                outcome=AttemptOutcome.INTERNAL_RUNTIME_FAILURE,
                task_id=manifest.task_id,
                generation=None,
                engine=None,
                transition=None,
                attempt_records=[],
            )
        base_snapshot, _ = self.store.load_generation(manifest.base_generation)
        records: list[TaskAttemptRecord] = []
        allowed_engines = engines[: 1 + self.config.max_fallback_engines]
        last_outcome = AttemptOutcome.ENGINE_EXECUTION_FAILURE
        last_engine: str | None = None

        for engine in allowed_engines:
            last_engine = engine.name
            signature = self._cache_signature(spec, manifest, provenance, engine)
            for _technical_attempt in range(self.config.technical_attempts_per_engine):
                attempt_dir, agent_task, attempt_id = self.tasks.next_attempt(
                    task_dir, engine.name, spec.task_type
                )
                try:
                    self.tasks.verify_workspace(
                        task_dir, agent_task.workspace_dir, provenance
                    )
                except BundleIntegrityError:
                    return RuntimeResult(
                        outcome=AttemptOutcome.INTERNAL_RUNTIME_FAILURE,
                        task_id=manifest.task_id,
                        generation=None,
                        engine=None,
                        transition=None,
                        attempt_records=records,
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

                # B2: a subprocess engine pre-classifies the §6.6 priority 1-5
                # conditions it owns (resource limit, execution, workspace
                # integrity, output policy, proposal import) through the frozen
                # primary_attempt_outcome precedence and attaches the report.
                # A reported failure is recorded directly; otherwise the result
                # falls through to the existing parse → schema →
                # proposal-validation → commit stages (priorities 5-8), whose
                # staged order matches the same frozen precedence.
                execution_report = getattr(result, "execution_report", None)
                if (
                    execution_report is not None
                    and execution_report.primary_outcome
                    is not AttemptOutcome.VALID_SCIENTIFIC_RESULT
                ):
                    last_outcome = execution_report.primary_outcome
                    record = self._record(
                        manifest,
                        attempt_id,
                        engine,
                        last_outcome,
                        False,
                        False,
                        None,
                        False,
                        [
                            failure.message
                            for failure in execution_report.detected_failures
                        ]
                        or [last_outcome.value],
                        output_hash,
                        relative_result_path,
                        detected_failures=execution_report.detected_failures,
                    )
                    self.tasks.write_attempt_record(attempt_dir, record)
                    records.append(record)
                    continue

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

                try:
                    self.tasks.verify_workspace(
                        task_dir, agent_task.workspace_dir, provenance
                    )
                except BundleIntegrityError as exc:
                    last_outcome = AttemptOutcome.ENGINE_WORKSPACE_INTEGRITY_FAILURE
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

                def locked_promotion(snapshot, registry):
                    # Runs inside the writer lock, after generation-level and
                    # structured-dependency freshness, before materialization.
                    self._require_fresh_resources(provenance)
                    return promotion(snapshot, registry)

                try:
                    commit = self.store.commit(
                        base_generation=manifest.base_generation,
                        dependencies=dict(manifest.dependencies),
                        promotion=locked_promotion,
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
        detected_failures: tuple[AttemptFailure, ...] = (),
    ) -> TaskAttemptRecord:
        return TaskAttemptRecord(
            task_id=manifest.task_id,
            attempt_id=attempt_id,
            engine=engine.name,
            engine_version=engine.version,
            outcome=outcome,
            detected_failures=detected_failures,
            format_valid=format_valid,
            schema_valid=schema_valid,
            proposal_validation_valid=proposal_validation_valid,
            accepted_attempt=accepted_attempt,
            validation_errors=validation_errors,
            output_hash=output_hash,
            agent_result_path=agent_result_path,
        )
