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
from .receipts import (
    compute_input_identity_key,
    compute_semantic_fingerprint,
    compute_semantic_task_key,
    extract_canonical_object_receipts,
    verify_receipt_canonical_objects,
)
from .records import (
    AgentResult,
    AppliedTaskReceipt,
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
    PromotionPayload,
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

        # Receipt identity covers the complete ordered technical execution
        # plan.  This preserves zero-cost reuse when a fallback engine produced
        # the accepted result, while any engine/order/configuration drift
        # invalidates the receipt before preflight.
        engine_plan = {
            "technical_attempts_per_engine": self.config.technical_attempts_per_engine,
            "engines": [
                {
                    "name": item.name,
                    "version": item.version,
                    "safe_configuration": dict(item.safe_configuration()),
                }
                for item in allowed_engines
            ],
        }
        semantic_fingerprint = compute_semantic_fingerprint(
            validator_fingerprint=self.validator_fingerprint,
            promotion_handler_name=spec.promotion_handler,
            disposition_handler_name=spec.disposition_handler,
            scientific_contract_version=SCIENTIFIC_CONTRACT_VERSION,
            runtime_contract_version=RUNTIME_VERSION,
        )
        input_identity_key = compute_input_identity_key(
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
            engine="ordered-engine-plan",
            engine_version=RUNTIME_VERSION,
            safe_engine_configuration_hash=hash_json(engine_plan),
            scientific_contract_version=SCIENTIFIC_CONTRACT_VERSION,
        )
        semantic_task_key = compute_semantic_task_key(
            input_identity_key=input_identity_key,
            semantic_fingerprint=semantic_fingerprint,
        )

        candidate_receipt = None
        if self.config.enable_receipts:
            receipts = self.store.load_current_receipts()
            receipt_map = {r.semantic_task_key: r for r in receipts}
            candidate_receipt = receipt_map.get(semantic_task_key)
            if candidate_receipt is None:
                # Exact semantic key absent but same input identity: the
                # evaluation semantics changed since that receipt committed.
                # The locked validation block below evaluates this receipt and
                # engages the reevaluation-required path. Receipts with a
                # different input identity are unrelated tasks; their
                # transitions are never inspected here.
                for r in reversed(receipts):
                    if r.input_identity_key == input_identity_key:
                        candidate_receipt = r
                        break

        if candidate_receipt is not None:
            with self.store.writer_lock():
                current_gen, current_snapshot, current_registry = (
                    self.store.load_current()
                )
                locked_receipts = self.store.load_receipts(current_gen)
                locked_map = {r.semantic_task_key: r for r in locked_receipts}
                receipt = locked_map.get(candidate_receipt.semantic_task_key)

                receipt_valid = False
                rejection_reason: str | None = None

                if receipt is None:
                    rejection_reason = "RECEIPT_NOT_IN_CURRENT_GENERATION"
                elif hash_json(receipt.proposal_payload) != receipt.proposal_hash:
                    # Payload integrity is checked before the payload is
                    # trusted for validation or disposition interpretation.
                    rejection_reason = "RECEIPT_PROPOSAL_HASH_MISMATCH"
                elif receipt.task_type != spec.task_type:
                    rejection_reason = "RECEIPT_TASK_TYPE_MISMATCH"
                elif receipt.input_identity_key != input_identity_key:
                    rejection_reason = "RECEIPT_INPUT_IDENTITY_MISMATCH"
                else:
                    proposal_model: BaseModel | None = None
                    current_transition = None
                    try:
                        proposal_model = spec.proposal_model.model_validate(
                            receipt.proposal_payload
                        )
                        current_transition = interpret_disposition(
                            spec, proposal_model
                        )
                    except Exception as exc:
                        rejection_reason = f"PROPOSAL_VALIDATION_FAILED:{exc}"

                    if current_transition is not None and (
                        current_transition.scientific_disposition
                        != receipt.recorded_transition.scientific_disposition
                        or current_transition.downstream_eligible
                        != receipt.recorded_transition.downstream_eligible
                    ):
                        return RuntimeResult(
                            outcome=AttemptOutcome.CONTRACT_IMPLEMENTATION_FAILURE,
                            task_id=manifest.task_id,
                            generation=None,
                            engine=receipt.engine,
                            transition=None,
                            stale_rebuilds=0,
                            attempt_records=records,
                            allocated_ids={},
                            cache_reused=False,
                            commit_performed=False,
                            reused_generation=None,
                            receipt_reused=False,
                            receipt_rejection_reason="ACCEPTED_RECEIPT_REEVALUATION_REQUIRED",
                        )
                    elif (
                        receipt.semantic_fingerprint.combined_fingerprint
                        != semantic_fingerprint.combined_fingerprint
                    ):
                        rejection_reason = "SEMANTIC_FINGERPRINT_MISMATCH"
                    else:
                        canonical_raw_ids = {
                            obj_receipt.qualified_id.rsplit(":", 1)[-1]
                            for obj_receipt in receipt.canonical_objects
                        }
                        unrecorded_ids = sorted(
                            raw_id
                            for raw_id in receipt.local_ref_map.values()
                            if raw_id not in canonical_raw_ids
                        )
                        if unrecorded_ids:
                            rejection_reason = (
                                "RECEIPT_LOCAL_REF_NOT_CANONICAL:"
                                + ",".join(unrecorded_ids)
                            )

                        dep_ok = rejection_reason is None
                        if dep_ok:
                            for dep_id, exp_hash in manifest.dependencies.items():
                                try:
                                    act_hash = current_snapshot.dependency_hash(dep_id)
                                except KeyError:
                                    dep_ok = False
                                    rejection_reason = f"DEPENDENCY_MISSING:{dep_id}"
                                    break
                                if act_hash != exp_hash:
                                    dep_ok = False
                                    rejection_reason = f"DEPENDENCY_HASH_MISMATCH:{dep_id}"
                                    break

                        if dep_ok:
                            try:
                                self._require_fresh_resources(provenance)
                            except StaleSnapshotError as exc:
                                dep_ok = False
                                rejection_reason = f"RESOURCE_STALE:{exc}"

                        if dep_ok:
                            canon_ok, canon_reason = verify_receipt_canonical_objects(
                                receipt, current_snapshot
                            )
                            if not canon_ok:
                                rejection_reason = canon_reason
                            else:
                                try:
                                    validate_proposal(
                                        spec, proposal_model, current_snapshot
                                    )
                                    current_snapshot.validate_repository()
                                except Exception as exc:
                                    rejection_reason = (
                                        f"PROPOSAL_OR_REPO_VALIDATION_FAILED:{exc}"
                                    )
                                else:
                                    receipt_valid = True

                if receipt_valid and receipt is not None:
                    transition = current_transition.model_copy(
                        update={"canonicalized": True}
                    )
                    atomic_write_text(
                        task_dir / "receipt_reused.json",
                        receipt.model_dump_json(indent=2) + "\n",
                    )
                    # The result operates against the CURRENT canonical view
                    # (current_gen, loaded under the writer lock); the accepted
                    # effect originated in receipt.committed_generation, which
                    # is reported separately as reused_generation (goal.md §7.3).
                    return RuntimeResult(
                        outcome=AttemptOutcome.VALID_SCIENTIFIC_RESULT,
                        task_id=manifest.task_id,
                        generation=current_gen,
                        engine=receipt.engine,
                        transition=transition,
                        stale_rebuilds=0,
                        attempt_records=[],
                        allocated_ids=dict(receipt.local_ref_map),
                        cache_reused=False,
                        commit_performed=False,
                        reused_generation=receipt.committed_generation,
                        receipt_reused=True,
                        receipt_rejection_reason=None,
                    )
                else:
                    atomic_write_text(
                        task_dir / "receipt_rejected.txt",
                        f"Receipt rejected: {rejection_reason}\n",
                    )

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
                    if not fallback_allowed(last_outcome):
                        return RuntimeResult(
                            outcome=last_outcome,
                            task_id=manifest.task_id,
                            generation=None,
                            engine=last_engine,
                            transition=None,
                            attempt_records=records,
                        )
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

                def receipt_builder(
                    next_gen: int,
                    locked_base_snapshot: RepositorySnapshot,
                    payload: PromotionPayload,
                ) -> AppliedTaskReceipt:
                    canon_objects = extract_canonical_object_receipts(
                        locked_base_snapshot, payload.snapshot, payload.allocated_ids
                    )
                    return AppliedTaskReceipt(
                        input_identity_key=input_identity_key,
                        semantic_task_key=semantic_task_key,
                        task_type=spec.task_type,
                        task_spec_version=spec.version,
                        proposal_hash=hash_json(raw_proposal),
                        proposal_payload=raw_proposal,
                        semantic_fingerprint=semantic_fingerprint,
                        source_generation=manifest.base_generation,
                        committed_generation=next_gen,
                        canonical_objects=canon_objects,
                        local_ref_map=dict(payload.allocated_ids),
                        recorded_transition=transition,
                        engine=engine.name,
                        engine_version=engine.version,
                        accepted_attempt_id=f"{manifest.task_id}/{attempt_id}",
                    )

                try:
                    commit = self.store.commit(
                        base_generation=manifest.base_generation,
                        dependencies=dict(manifest.dependencies),
                        promotion=locked_promotion,
                        receipt_factory=(
                            receipt_builder if self.config.enable_receipts else None
                        ),
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
                    cache_reused=cached is not None,
                    commit_performed=True,
                    reused_generation=None,
                    receipt_reused=False,
                    receipt_rejection_reason=None,
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
