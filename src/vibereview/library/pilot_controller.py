"""Fixed synthetic-only controller for the bounded Package C pilot.

The controller deliberately spells out the one supported sequence.  It is not
a workflow abstraction: every semantic method builds one existing TaskSpec,
uses its predeclared :class:`MockEngine`, and decorates the task's promotion so
the accepted receipt, all domain artifacts, usage record, and pilot journal
event cross one generation transaction.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel

from vibereview.enums import ClaimDecision
from vibereview.runtime.artifacts import (
    AssemblyBudget,
    AssemblyRecord,
    PackageCArtifactError,
    assemble_exact_section,
    verify_exact_assembly,
)
from vibereview.runtime.coupled import (
    CoupledCommitPlan,
    CoupledProposalValidationError,
)
from vibereview.runtime.discovery import (
    DiscoveryArtifactReference,
    StructuredCandidateClaimsAdapter,
    StructuredCorpusChallengeAdapter,
    StructuredDiscoveryParseAdapter,
    load_discovery_artifact,
)
from vibereview.runtime.draft_transitions import (
    DraftAuditArtifactReference,
    PropositionAuditAdapter,
    PropositionDraftAcceptanceAdapter,
    RenderedSentenceAuditAdapter,
    RenderedSentenceDraftAcceptanceAdapter,
    RevisedClaimDraftAdapter,
    load_proposition_audit_artifact,
    load_sentence_audit_artifact,
)
from vibereview.runtime.drafts import (
    DraftArtifactReference,
    DraftArtifactKind,
    load_draft_artifact,
    load_draft_artifact_for_receipt,
)
from vibereview.runtime.dto import (
    AssessClaimInvocation,
    AssessEvidenceInvocation,
    AuditPropositionInvocation,
    AuditRenderedSentenceInvocation,
    CorpusChallengerInvocation,
    GenerateCandidateClaimsInvocation,
    GeneratePropositionsInvocation,
    GenerateRetrievalQueriesInvocation,
    ParseDeepResearchInvocation,
    RenderProseInvocation,
    ReviseClaimInvocation,
    ValidateFinalClaimInvocation,
)
from vibereview.runtime.engine import MockEngine
from vibereview.runtime.hashing import (
    canonical_json_bytes,
    code_fingerprint,
    hash_bytes,
    hash_json,
)
from vibereview.runtime.kernel import ProjectRuntime
from vibereview.runtime.pilot_journal import (
    PilotControlEventResult,
    PilotExactAssemblyControlPayload,
    PilotRetrievalControlPayload,
    PilotStageArtifactReference,
    PilotStageMaterializer,
    PilotValidationControlPayload,
    discover_current_pilot_head,
    load_pilot_control_artifact,
    load_pilot_run_registration,
    load_pilot_stage_artifact,
    record_pilot_control_event,
    validate_pilot_control_artifact_binding,
)
from vibereview.runtime.pilot_manifest import (
    _engine_plan_hash,
    _read_allowed_resource,
    _schema_file_hash,
    _task_prompt_hash,
    compute_package_c_implementation_fingerprint,
)
from vibereview.runtime.pilot_records import (
    EngineUsageRecord,
    PilotRunManifest,
    PilotRunRegistrationReference,
    PilotStage,
    PilotStageStatus,
)
from vibereview.runtime.pilot_usage import (
    PilotTaskUsageArtifact,
    PilotTaskUsageTotals,
    canonical_pilot_task_usage_bytes,
    collect_pilot_task_usage,
    pilot_task_usage_budget_overruns,
)
from vibereview.runtime.pilot_sequence import (
    load_fixed_pilot_sequence,
    validate_fixed_pilot_artifact_sequence,
    validate_fixed_pilot_sequence,
)
from vibereview.runtime.pilot_validation import (
    SyntheticPilotValidationResult,
    validate_synthetic_pilot_run,
)
from vibereview.runtime.promotion import prepare_promotion, validate_proposal
from vibereview.runtime.records import (
    AppliedTaskReceipt,
    AttemptOutcome,
    RuntimeResult,
    TaskManifest,
    TaskProvenance,
    TaskSpec,
    TaskType,
)
from vibereview.runtime.registry import CanonicalIdRegistry
from vibereview.runtime.repository import (
    AuxiliaryStagingWriter,
    PromotionPayload,
    StaleSnapshotError,
    read_contained_regular_file,
)
from vibereview.runtime.specs import TASK_SPECS, validate_task_spec_executable
from vibereview.runtime.state import RepositorySnapshot

from .evidence_task import (
    AssessEvidencePromotionAdapter,
    _canonical_span_allowlist,
    _stage_retrieval_ledger_resource,
)
from .models import CorpusIntegrityError
from .retrieval import RetrievalLedger, UnifiedRetrievalCoordinator, VerifiedCorpus


PILOT_CONTROLLER_VERSION = "1"
MOCK_USAGE_UNAVAILABLE_REASON = (
    "Deterministic MockEngine does not report token, cache, price, or cost usage."
)


def _monotonic() -> float:
    """Single local clock seam for deterministic budget verification."""

    return time.monotonic()


class SyntheticPilotControllerError(ValueError):
    """The requested operation is outside the one fixed synthetic pilot."""


class PilotBudgetError(
    SyntheticPilotControllerError, CoupledProposalValidationError
):
    """An actual or unavoidable pilot cost exceeds its frozen run budget."""


@dataclass(frozen=True, slots=True)
class PilotDomainArtifacts:
    """Exact generation-owned files observed inside one accepted transaction."""

    owner_generation: int
    hashes: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class SyntheticPilotControllerResult:
    """One controller transition and its new exact journal head."""

    head: PilotStageArtifactReference
    runtime_result: RuntimeResult | None = None
    control_result: PilotControlEventResult | None = None
    artifact_reference: object | None = None
    domain_artifacts: PilotDomainArtifacts | None = None
    engine_usage: EngineUsageRecord | None = None
    attempt_usage: PilotTaskUsageArtifact | None = None
    body: bytes | None = None
    assembly: AssemblyRecord | None = None
    validation: SyntheticPilotValidationResult | None = None

    @property
    def result(self) -> RuntimeResult | PilotControlEventResult:
        value = self.runtime_result or self.control_result
        if value is None:  # pragma: no cover - construction invariant
            raise AssertionError("controller result has no transition result")
        return value


_TASK_STAGE: dict[TaskType, PilotStage] = {
    TaskType.PARSE_DEEP_RESEARCH: PilotStage.DISCOVERY,
    TaskType.CORPUS_CHALLENGER: PilotStage.CORPUS_CHALLENGE,
    TaskType.GENERATE_CANDIDATE_CLAIMS: PilotStage.DISCOVERY,
    TaskType.GENERATE_RETRIEVAL_QUERIES: PilotStage.QUERY_GENERATION,
    TaskType.ASSESS_EVIDENCE: PilotStage.EVIDENCE_ASSESSMENT,
    TaskType.AGGREGATE_PAPER_EVIDENCE: PilotStage.CLAIM_AGGREGATION,
    TaskType.ASSESS_CLAIM: PilotStage.CLAIM_AGGREGATION,
    TaskType.REVISE_CLAIM: PilotStage.CLAIM_VALIDATION,
    TaskType.VALIDATE_FINAL_CLAIM: PilotStage.CLAIM_VALIDATION,
    TaskType.GENERATE_PROPOSITIONS: PilotStage.PROPOSITION_AUDIT,
    TaskType.AUDIT_PROPOSITION: PilotStage.PROPOSITION_AUDIT,
    TaskType.RENDER_PROSE: PilotStage.SENTENCE_AUDIT,
    TaskType.AUDIT_RENDERED_SENTENCE: PilotStage.SENTENCE_AUDIT,
}

_TASK_PHASE: dict[TaskType, int] = {
    TaskType.PARSE_DEEP_RESEARCH: 1,
    TaskType.CORPUS_CHALLENGER: 2,
    TaskType.GENERATE_CANDIDATE_CLAIMS: 3,
    TaskType.GENERATE_RETRIEVAL_QUERIES: 4,
    TaskType.ASSESS_EVIDENCE: 6,
    TaskType.AGGREGATE_PAPER_EVIDENCE: 7,
    TaskType.ASSESS_CLAIM: 7,
    TaskType.REVISE_CLAIM: 8,
    TaskType.VALIDATE_FINAL_CLAIM: 8,
    TaskType.GENERATE_PROPOSITIONS: 9,
    TaskType.AUDIT_PROPOSITION: 9,
    TaskType.RENDER_PROSE: 10,
    TaskType.AUDIT_RENDERED_SENTENCE: 10,
}

_CONTROL_PHASE = {
    PilotStage.PREREQUISITES: 0,
    PilotStage.RETRIEVAL: 5,
    PilotStage.EXACT_ASSEMBLY: 11,
    PilotStage.VALIDATION_REPORT: 12,
}

_PREFIX = (
    TaskType.PARSE_DEEP_RESEARCH,
    TaskType.CORPUS_CHALLENGER,
    TaskType.GENERATE_CANDIDATE_CLAIMS,
    TaskType.GENERATE_RETRIEVAL_QUERIES,
)

_BUDGET_LIMITS = {
    "semantic_engine_invocations": "max_semantic_engine_invocations",
    "queries": "max_queries",
    "raw_candidates": "max_raw_candidates",
    "assessed_candidates": "max_assessed_candidates",
    "evidence_records": "max_evidence_records",
    "claim_paper_evidence": "max_claim_paper_evidence",
    "propositions": "max_initial_propositions",
    "proposition_repairs": "max_proposition_repairs",
    "sentences": "max_initial_sentences",
    "sentence_repairs": "max_sentence_repairs",
    "elapsed_seconds": "max_run_seconds",
    "request_bytes": "max_request_bytes",
    "proposal_bytes": "max_proposal_bytes",
    "stdout_bytes": "max_cumulative_stdout_bytes",
    "stderr_bytes": "max_cumulative_stderr_bytes",
    "retained_diagnostic_bytes": "max_cumulative_retained_diagnostic_bytes",
    "writable_entries": "max_cumulative_writable_entries",
    "writable_tree_bytes": "max_cumulative_writable_tree_bytes",
}


class _HashObservingWriter:
    """Observe exact bytes while preserving the confined writer interface."""

    __slots__ = ("_delegate", "hashes")

    def __init__(self, delegate: AuxiliaryStagingWriter) -> None:
        self._delegate = delegate
        self.hashes: dict[str, str] = {}

    def write_bytes(self, relative_path: str, content: bytes) -> None:
        if relative_path in self.hashes:
            raise SyntheticPilotControllerError(
                f"pilot domain artifact path was written twice: {relative_path}"
            )
        self._delegate.write_bytes(relative_path, content)
        self.hashes[relative_path] = hash_bytes(content)


class _JournaledPromotionAdapter:
    """Decorate one existing default/coupled promotion with the pilot journal."""

    rebuild_on_stale = False

    def __init__(
        self,
        controller: "SyntheticPilotController",
        *,
        task_type: TaskType,
        delegate: Any | None,
        calls_before: int,
        stage_started_at: float,
        position_error: SyntheticPilotControllerError | None = None,
    ) -> None:
        self.controller = controller
        self.task_type = task_type
        self.stage = _TASK_STAGE[task_type]
        self.delegate = delegate
        self.promotion_handler = TASK_SPECS[task_type].promotion_handler
        inner_fingerprint = getattr(delegate, "promotion_fingerprint", "default")
        # Coupled adapters authenticate their own receipt fingerprint inside
        # their atomic materializer.  Preserve that exact identity at the
        # kernel boundary; the enclosing journal/controller implementation is
        # independently bound by the run manifest's Package C fingerprint.
        self.promotion_fingerprint = (
            inner_fingerprint
            if delegate is not None
            else code_fingerprint(
                [Path(__file__)],
                (
                    f"synthetic-pilot-controller:{PILOT_CONTROLLER_VERSION}:"
                    f"{task_type.value}:default"
                ),
            )
        )
        self.calls_before = calls_before
        self.stage_started_at = stage_started_at
        self.position_error = position_error
        self.request_bytes = 0
        self.preflight_budget_error: PilotBudgetError | None = None
        self.reference: PilotStageArtifactReference | None = None
        self.usage: EngineUsageRecord | None = None
        self.attempt_usage: PilotTaskUsageArtifact | None = None
        self.artifact_hashes: dict[str, str] = {}

    def validate_pre_execution(
        self,
        *,
        project_root: Path,
        spec: TaskSpec,
        invocation: BaseModel,
        manifest: TaskManifest,
        provenance: TaskProvenance,
    ) -> None:
        self.controller._require_live_head()
        if self.position_error is not None:
            raise self.position_error
        self.controller._validate_task_provenance(spec, provenance)
        self.request_bytes = self.controller._request_size(provenance)
        previous = self.controller._budget_value("request_bytes")
        attempts = self.controller.runtime.config.technical_attempts_per_engine
        if (
            self.controller._budget_value("semantic_engine_invocations") + attempts
            > self.controller.manifest.budget.max_semantic_engine_invocations
        ):
            self.preflight_budget_error = PilotBudgetError(
                "pilot semantic-engine invocation budget cannot cover all technical attempts"
            )
            raise self.preflight_budget_error
        if previous + self.request_bytes * attempts > self.controller.manifest.budget.max_request_bytes:
            self.preflight_budget_error = PilotBudgetError(
                "pilot cumulative request-byte budget would be exceeded"
            )
            raise self.preflight_budget_error
        if self.delegate is not None:
            self.delegate.validate_pre_execution(
                project_root=project_root,
                spec=spec,
                invocation=invocation,
                manifest=manifest,
                provenance=provenance,
            )

    def validate_proposal(
        self,
        *,
        spec: TaskSpec,
        proposal: BaseModel,
        snapshot: RepositorySnapshot,
        dependency_keys: Mapping[str, str],
        invocation: BaseModel,
        registry: CanonicalIdRegistry,
        accepted_allocations: Mapping[str, str] | None = None,
    ) -> None:
        if self.delegate is None:
            validate_proposal(
                spec,
                proposal,
                snapshot,
                dependency_keys=dependency_keys,
                invocation=invocation,
                registry=registry,
                accepted_allocations=accepted_allocations,
            )
        else:
            self.delegate.validate_proposal(
                spec=spec,
                proposal=proposal,
                snapshot=snapshot,
                dependency_keys=dependency_keys,
                invocation=invocation,
                registry=registry,
                accepted_allocations=accepted_allocations,
            )
        self.controller._validate_proposal_budget(self.task_type, proposal)

    def prepare_commit(
        self,
        *,
        project_root: Path,
        spec: TaskSpec,
        proposal: BaseModel,
        invocation: BaseModel,
        manifest: TaskManifest,
        provenance: TaskProvenance,
    ) -> CoupledCommitPlan:
        if self.delegate is None:
            plan = CoupledCommitPlan(prepare_promotion(spec, proposal))
        else:
            plan = self.delegate.prepare_commit(
                project_root=project_root,
                spec=spec,
                proposal=proposal,
                invocation=invocation,
                manifest=manifest,
                provenance=provenance,
            )

        def materialize(
            writer: AuxiliaryStagingWriter,
            next_generation: int,
            payload: PromotionPayload,
            receipt: AppliedTaskReceipt,
        ) -> None:
            engine = self.controller.engines[self.task_type]
            calls_used = engine.calls - self.calls_before
            attempt_usage = collect_pilot_task_usage(
                self.controller.runtime.project_root,
                task_id=provenance.task_id,
                budget=self.controller.manifest.budget,
                accepted_attempt_id=receipt.accepted_attempt_id,
            )
            if attempt_usage.totals.attempt_count != calls_used:
                raise SyntheticPilotControllerError(
                    "pilot engine calls and retained attempts differ"
                )
            budget = self.controller._accepted_budget(
                task_type=self.task_type,
                proposal=proposal,
                payload=payload,
                usage_totals=attempt_usage.totals,
                request_bytes=self.request_bytes,
                started_at=self.stage_started_at,
            )
            usage = EngineUsageRecord(
                task_id=provenance.task_id,
                attempt_id=receipt.accepted_attempt_id,
                engine=receipt.engine,
                unavailable_reason=MOCK_USAGE_UNAVAILABLE_REASON,
            )
            usage_content = canonical_json_bytes(usage.model_dump(mode="json")) + b"\n"
            usage_path = self.controller._usage_path(
                self.task_type, receipt, self.controller._next_ordinal
            )
            attempt_usage_content = canonical_pilot_task_usage_bytes(attempt_usage)
            attempt_usage_path = self.controller._attempt_usage_path(
                self.task_type, receipt, self.controller._next_ordinal
            )
            observed: _HashObservingWriter | None = None

            def inner(
                stage_writer: AuxiliaryStagingWriter,
                generation: int,
                stage_payload: PromotionPayload,
                stage_receipt: AppliedTaskReceipt,
            ) -> None:
                nonlocal observed
                observed = _HashObservingWriter(stage_writer)
                if plan.staging_materializer is not None:
                    plan.staging_materializer(
                        observed, generation, stage_payload, stage_receipt
                    )
                observed.write_bytes(usage_path, usage_content)
                observed.write_bytes(attempt_usage_path, attempt_usage_content)

            def artifact_hashes(_receipt: AppliedTaskReceipt) -> Mapping[str, str]:
                if observed is None:  # pragma: no cover - materializer order invariant
                    raise SyntheticPilotControllerError(
                        "pilot domain artifacts were not observed"
                    )
                return observed.hashes

            stage_materializer = PilotStageMaterializer(
                project_root=self.controller.runtime.project_root,
                registration=self.controller.registration,
                run_manifest=self.controller.manifest,
                task_provenance=provenance,
                engine_plan_hash=self.controller.manifest.engine_role_plan[
                    self.task_type
                ].safe_configuration_hash,
                ordinal=self.controller._next_ordinal,
                stage=self.stage,
                input_hashes=self.controller._task_input_hashes(provenance),
                artifact_hash_provider=artifact_hashes,
                budget_consumed=budget,
                previous_stage=self.controller._head,
                inner=inner,
                precommit_validator=self.controller._validate_prospective_event,
            )
            stage_materializer(writer, next_generation, payload, receipt)
            if observed is None:  # pragma: no cover - materializer order invariant
                raise SyntheticPilotControllerError("pilot artifacts were not staged")
            prepared = stage_materializer.prepared_for(
                receipt, artifact_hashes=observed.hashes
            )
            self.reference = prepared.reference
            self.usage = usage
            self.attempt_usage = attempt_usage
            self.artifact_hashes = dict(observed.hashes)

        return CoupledCommitPlan(plan.promotion, materialize)

    def verify_receipt_reuse(
        self,
        *,
        project_root: Path,
        spec: TaskSpec,
        proposal: BaseModel,
        invocation: BaseModel,
        manifest: TaskManifest,
        provenance: TaskProvenance,
        receipt: AppliedTaskReceipt,
        current_generation: int,
        current_snapshot: RepositorySnapshot,
        current_registry: CanonicalIdRegistry,
    ) -> tuple[bool, str | None]:
        if self.delegate is not None:
            try:
                delegate_ok, delegate_reason = self.delegate.verify_receipt_reuse(
                    project_root=project_root,
                    spec=spec,
                    proposal=proposal,
                    invocation=invocation,
                    manifest=manifest,
                    provenance=provenance,
                    receipt=receipt,
                    current_generation=current_generation,
                    current_snapshot=current_snapshot,
                    current_registry=current_registry,
                )
            except Exception as exc:
                return False, f"pilot delegate receipt verification failed:{exc}"
            if not delegate_ok:
                return False, delegate_reason or "pilot delegate receipt differs"

        matches = [
            (index, event)
            for index, event in enumerate(self.controller._events)
            if event.accepted_receipt == receipt
        ]
        if len(matches) != 1:
            return False, "accepted pilot receipt has no unique journal event"
        index, event = matches[0]
        if (
            event.stage_record.stage is not self.stage
            or event.stage_record.status is not PilotStageStatus.COMPLETED
            or event.stage_record.committed_generation != receipt.committed_generation
        ):
            return False, "accepted pilot receipt journal binding differs"
        self.reference = self.controller._reference_for_event_index(index)
        self.artifact_hashes = dict(event.stage_record.artifact_hashes)
        self.usage = self.controller._load_event_engine_usage(event)
        self.attempt_usage = self.controller._load_event_attempt_usage(event)
        if self.attempt_usage is None:
            return False, "accepted pilot receipt lacks attempt accounting"
        return True, None


class SyntheticPilotController:
    """Execute only the registered deterministic five-paper synthetic pilot."""

    def __init__(
        self,
        runtime: ProjectRuntime,
        manifest: PilotRunManifest,
        registration: PilotRunRegistrationReference,
        engines: Mapping[TaskType, MockEngine],
        *,
        journal_head: PilotStageArtifactReference | None = None,
    ) -> None:
        self.runtime = runtime
        self.manifest = PilotRunManifest.model_validate(
            manifest.model_dump(mode="json")
        )
        self.registration = PilotRunRegistrationReference.model_validate(
            registration.model_dump(mode="json")
        )
        if runtime.config.max_fallback_engines != 0:
            raise SyntheticPilotControllerError(
                "synthetic pilot runtime must disable engine fallback"
            )
        if not runtime.config.enable_receipts:
            raise SyntheticPilotControllerError(
                "synthetic pilot requires accepted-task receipts"
            )
        if set(engines) != set(TaskType) or any(
            type(engine) is not MockEngine for engine in engines.values()
        ):
            raise SyntheticPilotControllerError(
                "pilot requires exactly one concrete MockEngine for every TaskType"
            )
        if any(engine.has_execution_callback for engine in engines.values()):
            raise SyntheticPilotControllerError(
                "pilot MockEngine roles cannot execute arbitrary callbacks"
            )
        self.engines: Mapping[TaskType, MockEngine] = MappingProxyType(dict(engines))
        self._validate_registered_identity()
        self._validate_contract_fingerprints()
        source_snapshot, _ = runtime.store.load_generation(
            self.manifest.source_generation
        )
        self._baseline_counts = {
            "queries": len(source_snapshot.retrieval_queries),
            "evidence_records": len(source_snapshot.evidence_records),
            "claim_paper_evidence": len(source_snapshot.claim_paper_evidence),
        }
        supplied_head = (
            PilotStageArtifactReference.model_validate(
                journal_head.model_dump(mode="json")
            )
            if journal_head is not None
            else None
        )
        discovered_head = discover_current_pilot_head(
            self.runtime.project_root,
            self.registration,
            self.manifest,
        )
        if supplied_head is not None and supplied_head != discovered_head:
            raise SyntheticPilotControllerError(
                "supplied pilot journal head is not the authenticated CURRENT head"
            )
        self._head = discovered_head
        self._assembly_body: bytes | None = None
        self._assembly: AssemblyRecord | None = None
        self._events = ()
        self._budget: dict[str, int] = {}
        self._refresh_sequence(allow_engine_restore=True)

    @property
    def journal_head(self) -> PilotStageArtifactReference | None:
        return self._head

    @property
    def head(self) -> PilotStageArtifactReference | None:
        return self._head

    @property
    def _next_ordinal(self) -> int:
        return 1 if self._head is None else self._head.ordinal + 1

    def _validate_registered_identity(self) -> None:
        if self.registration.run_id != self.manifest.run_id:
            raise SyntheticPilotControllerError(
                "pilot registration and manifest run IDs differ"
            )
        try:
            _, registered = load_pilot_run_registration(
                self.runtime.project_root,
                self.registration,
                expected_manifest=self.manifest,
            )
        except Exception as exc:
            raise SyntheticPilotControllerError(
                "registered pilot run could not be authenticated"
            ) from exc
        if registered != self.manifest:
            raise SyntheticPilotControllerError(
                "registered pilot manifest differs from controller manifest"
            )

    def _validate_contract_fingerprints(self) -> None:
        if self.runtime.validator_fingerprint != self.manifest.validator_fingerprint:
            raise SyntheticPilotControllerError(
                "runtime validator fingerprint differs from the run manifest"
            )
        if (
            self.manifest.package_c_implementation_fingerprint
            != compute_package_c_implementation_fingerprint()
        ):
            raise SyntheticPilotControllerError(
                "Package C implementation fingerprint differs from the run manifest"
            )
        for task_type in TaskType:
            spec = TASK_SPECS[task_type]
            try:
                validate_task_spec_executable(
                    spec,
                    additional_promotion_handlers=frozenset(
                        {spec.promotion_handler}
                    ),
                )
            except Exception as exc:
                raise SyntheticPilotControllerError(
                    f"pilot TaskSpec is not executable: {task_type.value}"
                ) from exc
            if self.manifest.prompt_fingerprints[task_type] != _task_prompt_hash(
                task_type
            ):
                raise SyntheticPilotControllerError(
                    f"pilot prompt fingerprint drifted: {task_type.value}"
                )
            expected_schema = hash_json(
                {
                    "input_schema": _schema_file_hash(spec.engine_input_model),
                    "proposal_schema": _schema_file_hash(spec.proposal_model),
                }
            )
            if self.manifest.schema_fingerprints[task_type] != expected_schema:
                raise SyntheticPilotControllerError(
                    f"pilot schema fingerprint drifted: {task_type.value}"
                )
            engine = self.engines[task_type]
            binding = self.manifest.engine_role_plan[task_type]
            if (
                binding.engine != engine.name
                or binding.engine_version != engine.version
                or binding.safe_configuration_hash
                != _engine_plan_hash(self.runtime, engine)
            ):
                raise SyntheticPilotControllerError(
                    f"pilot engine role differs from manifest: {task_type.value}"
                )

    def _refresh_sequence(self, *, allow_engine_restore: bool = False) -> None:
        current = self.runtime.store.current_generation()
        if self._head is None:
            if current != self.registration.owner_generation:
                raise StaleSnapshotError(
                    "pilot CURRENT advanced without an exact journal head"
                )
            self._events = ()
            self._budget = {}
            self._validate_engine_cursors(allow_restore=allow_engine_restore)
            return
        artifact = load_pilot_stage_artifact(
            self.runtime.project_root,
            self._head,
            expected_registration=self.registration,
            expected_run_manifest_hash=self.manifest.manifest_hash,
        )
        if current != self._head.owner_generation:
            raise StaleSnapshotError("pilot journal head is not CURRENT")
        validate_fixed_pilot_sequence(self.runtime.project_root, self._head)
        self._events = load_fixed_pilot_sequence(
            self.runtime.project_root, self._head
        )
        self._budget = dict(artifact.stage_record.budget_consumed)
        self._validate_engine_cursors(allow_restore=allow_engine_restore)

    def _validate_engine_cursors(self, *, allow_restore: bool = False) -> None:
        """Bind each mutable MockEngine cursor to authenticated run evidence."""

        expected = {task_type: 0 for task_type in TaskType}
        for event in self._events:
            provenance = event.task_provenance
            if provenance is not None:
                usage = self._load_event_attempt_usage(event)
                if usage is None or usage.task_id != provenance.task_id:
                    raise SyntheticPilotControllerError(
                        "accepted pilot event lacks exact attempt accounting"
                    )
                expected[provenance.task_type] += usage.totals.attempt_count
                continue
            if event.stage_record.status is not PilotStageStatus.FAILED:
                continue
            control = load_pilot_control_artifact(
                self.runtime.project_root, event
            )
            raw_usage = control.payload.get("attempt_usage")
            raw_task_type = control.payload.get("task_type")
            if raw_usage is None or not isinstance(raw_task_type, str):
                raise SyntheticPilotControllerError(
                    "failed pilot event lacks exact attempt accounting"
                )
            try:
                usage = PilotTaskUsageArtifact.model_validate(raw_usage)
                task_type = TaskType(raw_task_type)
            except Exception as exc:
                raise SyntheticPilotControllerError(
                    "failed pilot attempt accounting is invalid"
                ) from exc
            if usage.task_id != control.payload.get("task_id"):
                raise SyntheticPilotControllerError(
                    "failed pilot attempt accounting names another task"
                )
            expected[task_type] += usage.totals.attempt_count

        for task_type, engine in self.engines.items():
            if allow_restore and engine.calls == 0 and expected[task_type] > 0:
                try:
                    engine.restore_calls(expected[task_type])
                except ValueError as exc:
                    raise SyntheticPilotControllerError(
                        "pilot MockEngine cursor cannot be restored: "
                        f"{task_type.value}"
                    ) from exc
            if engine.calls != expected[task_type]:
                raise SyntheticPilotControllerError(
                    f"pilot MockEngine cursor differs from journal: {task_type.value}"
                )

    def _require_live_head(self) -> None:
        self._validate_registered_identity()
        self._refresh_sequence()
        if self._events and self._events[-1].stage_record.status in {
            PilotStageStatus.BLOCKED,
            PilotStageStatus.FAILED,
        }:
            raise SyntheticPilotControllerError(
                "pilot journal is terminal after a blocked or failed event"
            )
        if self._events and (
            self._events[-1].stage_record.stage is PilotStage.VALIDATION_REPORT
        ):
            raise SyntheticPilotControllerError("pilot run is already closed")

    def _task_types(self) -> tuple[TaskType, ...]:
        return tuple(
            event.task_provenance.task_type
            for event in self._events
            if event.task_provenance is not None
        )

    def _reference_for_event_index(self, index: int) -> PilotStageArtifactReference:
        if not 0 <= index < len(self._events):
            raise SyntheticPilotControllerError("pilot event index is invalid")
        if index == len(self._events) - 1:
            if self._head is None:  # pragma: no cover - internal invariant
                raise SyntheticPilotControllerError("pilot head is absent")
            return self._head
        predecessor = self._events[index + 1].stage_record.previous_stage
        if predecessor is None:  # pragma: no cover - authenticated-chain invariant
            raise SyntheticPilotControllerError("pilot event successor lost predecessor")
        return PilotStageArtifactReference.model_validate(
            predecessor.model_dump(mode="json")
        )

    def _load_event_engine_usage(self, event) -> EngineUsageRecord | None:
        candidates = [
            path
            for path in event.stage_record.artifact_hashes
            if "/usage/" in path
        ]
        if len(candidates) != 1:
            return None
        path = candidates[0]
        try:
            _, _, selected = self.runtime.store.load_generation_auxiliary(
                event.stage_record.committed_generation, {path}
            )
            return EngineUsageRecord.model_validate_json(selected[path])
        except Exception as exc:
            raise SyntheticPilotControllerError(
                "accepted pilot usage artifact is invalid"
            ) from exc

    def _load_event_attempt_usage(
        self, event
    ) -> PilotTaskUsageArtifact | None:
        candidates = [
            path
            for path in event.stage_record.artifact_hashes
            if "/attempt-usage/" in path
        ]
        if len(candidates) != 1:
            return None
        path = candidates[0]
        try:
            _, _, selected = self.runtime.store.load_generation_auxiliary(
                event.stage_record.committed_generation, {path}
            )
            usage = PilotTaskUsageArtifact.model_validate_json(selected[path])
        except Exception as exc:
            raise SyntheticPilotControllerError(
                "accepted pilot attempt-usage artifact is invalid"
            ) from exc
        if usage.artifact_hash != event.stage_record.artifact_hashes[path]:
            raise SyntheticPilotControllerError(
                "accepted pilot attempt-usage hash differs from the journal"
            )
        return usage

    def _validate_prospective_event(
        self,
        event,
        control,
        snapshot: RepositorySnapshot,
    ) -> None:
        """Reject an invalid fixed-sequence transition before CURRENT changes."""

        prospective_events = (*self._events, event)
        controls = [
            load_pilot_control_artifact(self.runtime.project_root, existing)
            for existing in self._events
            if existing.task_provenance is None
        ]
        if control is not None:
            controls.append(control)
        validate_fixed_pilot_artifact_sequence(
            prospective_events,
            control_artifacts=tuple(controls),
            snapshot=snapshot,
        )

    def _last_phase(self) -> int:
        if not self._events:
            return -1
        event = self._events[-1]
        if event.task_provenance is not None:
            return _TASK_PHASE[event.task_provenance.task_type]
        return _CONTROL_PHASE.get(event.stage_record.stage, 0)

    def _require_task_position(self, task_type: TaskType) -> None:
        self._require_live_head()
        if not self._events:
            raise SyntheticPilotControllerError(
                "record prerequisites before any semantic stage"
            )
        tasks = self._task_types()
        if task_type in _PREFIX:
            expected_index = _PREFIX.index(task_type)
            if tasks != _PREFIX[:expected_index]:
                raise SyntheticPilotControllerError(
                    f"{task_type.value} is outside the fixed pilot prefix"
                )
            return
        if tasks[:4] != _PREFIX:
            raise SyntheticPilotControllerError(
                "pilot discovery/query prefix is incomplete"
            )
        stages = tuple(event.stage_record.stage for event in self._events)
        if PilotStage.RETRIEVAL not in stages:
            raise SyntheticPilotControllerError(
                "record deterministic retrieval before downstream semantic work"
            )
        phase = _TASK_PHASE[task_type]
        if phase < self._last_phase():
            raise SyntheticPilotControllerError(
                f"{task_type.value} would regress the fixed pilot sequence"
            )
        if task_type is TaskType.AUDIT_PROPOSITION and (
            TaskType.GENERATE_PROPOSITIONS not in tasks
        ):
            raise SyntheticPilotControllerError(
                "proposition audit requires an accepted proposition draft"
            )
        if task_type is TaskType.RENDER_PROSE and (
            TaskType.AUDIT_PROPOSITION not in tasks
        ):
            raise SyntheticPilotControllerError(
                "rendering requires at least one proposition audit"
            )
        if task_type is TaskType.AUDIT_RENDERED_SENTENCE and (
            TaskType.RENDER_PROSE not in tasks
        ):
            raise SyntheticPilotControllerError(
                "sentence audit requires an accepted rendered draft"
            )

    def _validate_task_provenance(
        self, spec: TaskSpec, provenance: TaskProvenance
    ) -> None:
        if provenance.base_generation != (
            self._head.owner_generation
            if self._head is not None
            else self.registration.owner_generation
        ):
            raise StaleSnapshotError("pilot task was not built from the journal head")
        if provenance.instructions_hash != self.manifest.prompt_fingerprints[
            spec.task_type
        ]:
            raise SyntheticPilotControllerError(
                "task prompt differs from the registered pilot manifest"
            )
        schema = hash_json(
            {
                "input_schema": provenance.input_schema_hash,
                "proposal_schema": provenance.proposal_schema_hash,
            }
        )
        if schema != self.manifest.schema_fingerprints[spec.task_type]:
            raise SyntheticPilotControllerError(
                "task schema differs from the registered pilot manifest"
            )
        if spec.task_type is TaskType.PARSE_DEEP_RESEARCH:
            observed = tuple(item.snapshot_hash for item in provenance.resources)
            expected_ids = tuple(
                f"RES{index:04d}" for index in range(1, len(observed) + 1)
            )
            if (
                observed != self.manifest.discovery_resource_hashes
                or tuple(item.resource_id for item in provenance.resources)
                != expected_ids
            ):
                raise SyntheticPilotControllerError(
                    "parse resources differ in hash or order from the run manifest"
                )

    def _request_size(self, provenance: TaskProvenance) -> int:
        bundle_root = (
            self.runtime.project_root
            / "work"
            / "tasks"
            / provenance.task_id
            / "bundle"
        )
        total = 0
        ceiling = self.manifest.budget.max_request_bytes
        for relative, expected_hash in sorted(
            provenance.expected_immutable_files.items()
        ):
            try:
                content, _ = read_contained_regular_file(
                    bundle_root,
                    relative,
                    max_bytes=ceiling + 1,
                )
            except Exception as exc:
                raise SyntheticPilotControllerError(
                    f"pilot task request file is unreadable: {relative}"
                ) from exc
            if hash_bytes(content) != expected_hash:
                raise SyntheticPilotControllerError(
                    f"pilot task request file hash differs: {relative}"
                )
            total += len(content)
            if total > ceiling:
                raise PilotBudgetError("pilot request exceeds its byte budget")
        return total

    def _validate_proposal_budget(
        self, task_type: TaskType, proposal: BaseModel
    ) -> None:
        size = len(canonical_json_bytes(proposal.model_dump(mode="json")))
        if self._budget_value("proposal_bytes") + size > self.manifest.budget.max_proposal_bytes:
            raise PilotBudgetError("pilot cumulative proposal-byte budget exceeded")
        budget = self.manifest.budget
        if task_type in {
            TaskType.PARSE_DEEP_RESEARCH,
            TaskType.GENERATE_CANDIDATE_CLAIMS,
        }:
            themes = len(getattr(proposal, "themes", ()))
            claims = len(getattr(proposal, "claims", ()))
            if themes > budget.max_themes or claims > budget.max_claims:
                raise PilotBudgetError("pilot discovery theme/claim budget exceeded")
            if task_type is TaskType.PARSE_DEEP_RESEARCH:
                leads = sum(
                    len(getattr(proposal, field, ()))
                    for field in (
                        "paper_candidates",
                        "controversies",
                        "gaps",
                    )
                )
                if leads > budget.max_discovery_leads:
                    raise PilotBudgetError("pilot discovery-lead budget exceeded")
        if task_type is TaskType.GENERATE_RETRIEVAL_QUERIES and len(
            getattr(proposal, "queries", ())
        ) > budget.max_queries:
            raise PilotBudgetError("pilot retrieval-query budget exceeded")

    def _budget_value(self, name: str) -> int:
        return self._budget.get(name, 0)

    def _actual_state_counts(
        self, snapshot: RepositorySnapshot
    ) -> dict[str, int]:
        return {
            "queries": max(
                0,
                len(snapshot.retrieval_queries)
                - self._baseline_counts["queries"],
            ),
            "evidence_records": max(
                0,
                len(snapshot.evidence_records)
                - self._baseline_counts["evidence_records"],
            ),
            "claim_paper_evidence": max(
                0,
                len(snapshot.claim_paper_evidence)
                - self._baseline_counts["claim_paper_evidence"],
            ),
        }

    def _check_budget(self, values: Mapping[str, int]) -> dict[str, int]:
        exact = dict(sorted(values.items()))
        for name, value in exact.items():
            if value < self._budget_value(name):
                raise PilotBudgetError(f"pilot cumulative budget regressed: {name}")
            field = _BUDGET_LIMITS.get(name)
            if field is None:
                raise PilotBudgetError(f"unknown pilot budget counter: {name}")
            if value > getattr(self.manifest.budget, field):
                raise PilotBudgetError(f"pilot budget exceeded: {name}")
        return exact

    def _accepted_budget(
        self,
        *,
        task_type: TaskType,
        proposal: BaseModel,
        payload: PromotionPayload,
        usage_totals: PilotTaskUsageTotals,
        request_bytes: int,
        started_at: float,
    ) -> dict[str, int]:
        task_elapsed = max(1, math.ceil(_monotonic() - started_at))
        if task_elapsed > self.manifest.budget.max_task_seconds:
            raise PilotBudgetError("pilot task exceeded its elapsed-time budget")
        values = dict(self._budget)
        values.update(self._actual_state_counts(payload.snapshot))
        values["semantic_engine_invocations"] = self._budget_value(
            "semantic_engine_invocations"
        ) + usage_totals.attempt_count
        values["request_bytes"] = self._budget_value("request_bytes") + (
            request_bytes * usage_totals.attempt_count
        )
        values["proposal_bytes"] = (
            self._budget_value("proposal_bytes") + usage_totals.output_bytes
        )
        values["stdout_bytes"] = (
            self._budget_value("stdout_bytes") + usage_totals.stdout_bytes
        )
        values["stderr_bytes"] = (
            self._budget_value("stderr_bytes") + usage_totals.stderr_bytes
        )
        values["retained_diagnostic_bytes"] = (
            self._budget_value("retained_diagnostic_bytes")
            + usage_totals.retained_diagnostic_bytes
        )
        values["writable_entries"] = (
            self._budget_value("writable_entries")
            + usage_totals.writable_entry_count
        )
        values["writable_tree_bytes"] = (
            self._budget_value("writable_tree_bytes")
            + usage_totals.writable_tree_bytes
        )
        values["elapsed_seconds"] = self._budget_value(
            "elapsed_seconds"
        ) + task_elapsed
        if task_type is TaskType.ASSESS_EVIDENCE:
            values["assessed_candidates"] = self._budget_value(
                "assessed_candidates"
            ) + len(getattr(proposal, "decisions"))
        if task_type is TaskType.GENERATE_PROPOSITIONS:
            count = len(getattr(proposal, "propositions"))
            if TaskType.GENERATE_PROPOSITIONS in self._task_types():
                values["proposition_repairs"] = self._budget_value(
                    "proposition_repairs"
                ) + count
            else:
                values["propositions"] = self._budget_value("propositions") + count
        if task_type is TaskType.RENDER_PROSE:
            count = len(getattr(proposal, "sentences"))
            if TaskType.RENDER_PROSE in self._task_types():
                values["sentence_repairs"] = self._budget_value(
                    "sentence_repairs"
                ) + count
            else:
                values["sentences"] = self._budget_value("sentences") + count
        return self._check_budget(values)

    def _control_budget(
        self,
        *,
        started_at: float,
        snapshot: RepositorySnapshot,
        counters: Mapping[str, int] | None = None,
    ) -> dict[str, int]:
        """Advance trusted cumulative counters for one Python-only stage."""

        task_elapsed = max(1, math.ceil(_monotonic() - started_at))
        if task_elapsed > self.manifest.budget.max_task_seconds:
            raise PilotBudgetError("pilot control exceeded its elapsed-time budget")
        values = dict(self._budget)
        values.update(self._actual_state_counts(snapshot))
        values["elapsed_seconds"] = self._budget_value(
            "elapsed_seconds"
        ) + task_elapsed
        if counters is not None:
            values.update(counters)
        return self._check_budget(values)

    def _failed_budget(
        self,
        *,
        usage_totals: PilotTaskUsageTotals,
        request_bytes: int,
        started_at: float,
    ) -> tuple[dict[str, int], dict[str, int]]:
        task_elapsed = max(1, math.ceil(_monotonic() - started_at))
        # A task-time overrun is itself a terminal technical outcome.  Keep its
        # bounded failure witness in the journal, while never publishing the
        # rejected scientific proposal.  Successful materialization enforces
        # the per-task ceiling before CURRENT changes.
        values = dict(self._budget)
        values["semantic_engine_invocations"] = self._budget_value(
            "semantic_engine_invocations"
        ) + usage_totals.attempt_count
        values["request_bytes"] = self._budget_value("request_bytes") + (
            request_bytes * usage_totals.attempt_count
        )
        values["proposal_bytes"] = (
            self._budget_value("proposal_bytes") + usage_totals.output_bytes
        )
        values["stdout_bytes"] = (
            self._budget_value("stdout_bytes") + usage_totals.stdout_bytes
        )
        values["stderr_bytes"] = (
            self._budget_value("stderr_bytes") + usage_totals.stderr_bytes
        )
        values["retained_diagnostic_bytes"] = (
            self._budget_value("retained_diagnostic_bytes")
            + usage_totals.retained_diagnostic_bytes
        )
        values["writable_entries"] = (
            self._budget_value("writable_entries")
            + usage_totals.writable_entry_count
        )
        values["writable_tree_bytes"] = (
            self._budget_value("writable_tree_bytes")
            + usage_totals.writable_tree_bytes
        )
        values["elapsed_seconds"] = self._budget_value(
            "elapsed_seconds"
        ) + task_elapsed
        consumed: dict[str, int] = {}
        overruns: dict[str, int] = {}
        for name, value in sorted(values.items()):
            field = _BUDGET_LIMITS.get(name)
            if field is None:
                raise PilotBudgetError(f"unknown pilot budget counter: {name}")
            limit = getattr(self.manifest.budget, field)
            if value > limit:
                overruns[name] = value - limit
                value = limit
            if value < self._budget_value(name):
                raise PilotBudgetError(f"pilot cumulative budget regressed: {name}")
            consumed[name] = value
        return consumed, overruns

    def _attempt_budget_overruns(
        self, usage: PilotTaskUsageArtifact
    ) -> dict[str, int]:
        return pilot_task_usage_budget_overruns(usage, self.manifest.budget)

    def _task_input_hashes(
        self, provenance: TaskProvenance
    ) -> dict[str, str]:
        values = {
            "task_provenance": hash_json(provenance.model_dump(mode="json")),
            "engine_input": provenance.engine_input_hash,
        }
        values.update(
            {
                f"dependency:{name}": digest
                for name, digest in sorted(provenance.dependencies.items())
            }
        )
        values.update(
            {
                f"resource:{item.resource_id}": item.snapshot_hash
                for item in provenance.resources
            }
        )
        return dict(sorted(values.items()))

    def _usage_path(
        self,
        task_type: TaskType,
        receipt: AppliedTaskReceipt,
        ordinal: int,
    ) -> str:
        digest = receipt.semantic_task_key.removeprefix("sha256:")
        return (
            f"pilot/runs/{self.manifest.run_id}/usage/"
            f"{ordinal:04d}-{task_type.value}-{digest}.json"
        )

    def _attempt_usage_path(
        self,
        task_type: TaskType,
        receipt: AppliedTaskReceipt,
        ordinal: int,
    ) -> str:
        digest = receipt.semantic_task_key.removeprefix("sha256:")
        return (
            f"pilot/runs/{self.manifest.run_id}/attempt-usage/"
            f"{ordinal:04d}-{task_type.value}-{digest}.json"
        )

    def _primary_artifact_reference(
        self,
        task_type: TaskType,
        delegate: Any | None,
        receipt: AppliedTaskReceipt,
    ) -> object | None:
        reference = getattr(delegate, "artifact_reference", None)
        if reference is not None:
            return reference
        if task_type in {
            TaskType.REVISE_CLAIM,
            TaskType.GENERATE_PROPOSITIONS,
            TaskType.RENDER_PROSE,
        }:
            return load_draft_artifact_for_receipt(
                self.runtime.project_root, receipt
            )[1]
        if task_type is TaskType.AUDIT_PROPOSITION:
            return load_proposition_audit_artifact(
                self.runtime.project_root, receipt
            )[1]
        if task_type is TaskType.AUDIT_RENDERED_SENTENCE:
            return load_sentence_audit_artifact(
                self.runtime.project_root, receipt
            )[1]
        return None

    def _run_task(
        self,
        task_type: TaskType,
        invocation: BaseModel,
        *,
        delegate: Any | None = None,
    ) -> SyntheticPilotControllerResult:
        position_error: SyntheticPilotControllerError | None = None
        try:
            self._require_task_position(task_type)
        except SyntheticPilotControllerError as exc:
            # Receipt lookup precedes coupled preflight in the kernel.  Defer a
            # phase error so an exact accepted request can reuse its immutable
            # receipt and journal event without executing an engine or creating
            # another generation.  A novel request still fails at preflight.
            position_error = exc
            self._validate_registered_identity()
            self._refresh_sequence()
        engine = self.engines[task_type]
        calls_before = engine.calls
        started_at = _monotonic()
        wrapped = _JournaledPromotionAdapter(
            self,
            task_type=task_type,
            delegate=delegate,
            calls_before=calls_before,
            stage_started_at=started_at,
            position_error=position_error,
        )
        result = self.runtime.run(
            task_type,
            invocation,
            engines=[engine],
            promotion_adapter=wrapped,
        )
        if result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT:
            if result.receipt_reused:
                if self._head is None or wrapped.reference is None:
                    raise SyntheticPilotControllerError(
                        "accepted pilot replay lacks its authenticated journal event"
                    )
                self._refresh_sequence()
                replay_artifact = load_pilot_stage_artifact(
                    self.runtime.project_root,
                    wrapped.reference,
                    expected_registration=self.registration,
                    expected_run_manifest_hash=self.manifest.manifest_hash,
                )
                receipt = replay_artifact.accepted_receipt
                if receipt is None:  # pragma: no cover - model invariant
                    raise SyntheticPilotControllerError(
                        "accepted pilot replay receipt is absent"
                    )
                return SyntheticPilotControllerResult(
                    head=self._head,
                    runtime_result=result,
                    artifact_reference=self._primary_artifact_reference(
                        task_type, delegate, receipt
                    ),
                    domain_artifacts=PilotDomainArtifacts(
                        owner_generation=wrapped.reference.owner_generation,
                        hashes=MappingProxyType(dict(wrapped.artifact_hashes)),
                    ),
                    engine_usage=wrapped.usage,
                    attempt_usage=wrapped.attempt_usage,
                )
            if wrapped.reference is None or result.generation != wrapped.reference.owner_generation:
                raise SyntheticPilotControllerError(
                    "accepted pilot task did not atomically materialize its journal event"
                )
            self._head = wrapped.reference
            self._refresh_sequence()
            artifact = load_pilot_stage_artifact(
                self.runtime.project_root,
                self._head,
                expected_registration=self.registration,
                expected_run_manifest_hash=self.manifest.manifest_hash,
            )
            receipt = artifact.accepted_receipt
            if receipt is None:  # pragma: no cover - stage model invariant
                raise SyntheticPilotControllerError("accepted pilot receipt is absent")
            primary = self._primary_artifact_reference(
                task_type, delegate, receipt
            )
            return SyntheticPilotControllerResult(
                head=self._head,
                runtime_result=result,
                artifact_reference=primary,
                domain_artifacts=PilotDomainArtifacts(
                    owner_generation=self._head.owner_generation,
                    hashes=MappingProxyType(dict(wrapped.artifact_hashes)),
                ),
                engine_usage=wrapped.usage,
                attempt_usage=wrapped.attempt_usage,
            )

        if wrapped.preflight_budget_error is not None:
            # The kernel correctly converts coupled preflight exceptions into
            # a runtime result.  A controller budget refusal is different: no
            # engine ran and no terminal pilot event should be published.
            raise wrapped.preflight_budget_error
        if position_error is not None and engine.calls == calls_before:
            raise position_error

        # All non-VALID outcomes are technical/runtime failures here.  Valid
        # negative scientific dispositions arrive through the VALID result
        # branch above and are therefore committed without fallback.
        attempt_usage = collect_pilot_task_usage(
            self.runtime.project_root,
            task_id=result.task_id,
            budget=self.manifest.budget,
            enforce_budget=False,
        )
        if attempt_usage.totals.attempt_count != engine.calls - calls_before:
            raise SyntheticPilotControllerError(
                "pilot engine calls and retained failed attempts differ"
            )
        observed_attempts = tuple(
            (
                item.attempt_id,
                item.engine,
                item.engine_version,
                item.outcome,
                item.accepted_attempt,
                item.attempt_record_semantic_hash,
            )
            for item in attempt_usage.attempts
        )
        runtime_attempts = tuple(sorted(
            (
                item.attempt_id,
                item.engine,
                item.engine_version,
                item.outcome,
                item.accepted_attempt,
                hash_json(item.model_dump(mode="json")),
            )
            for item in result.attempt_records
        ))
        terminal_attempt_id = result.attempt_records[-1].attempt_id
        terminal_usage = next(
            (
                item
                for item in attempt_usage.attempts
                if item.attempt_id == terminal_attempt_id
            ),
            None,
        )
        if (
            attempt_usage.accepted_attempt_id is not None
            or observed_attempts != runtime_attempts
            or not observed_attempts
            or terminal_usage is None
            or terminal_usage.outcome is not result.outcome
        ):
            raise SyntheticPilotControllerError(
                "pilot failed-attempt accounting differs from runtime result"
            )
        failed_budget, budget_overruns = self._failed_budget(
            usage_totals=attempt_usage.totals,
            request_bytes=wrapped.request_bytes,
            started_at=started_at,
        )
        budget_overruns = {
            **self._attempt_budget_overruns(attempt_usage),
            **budget_overruns,
        }
        budget_overruns.update(
            self._attempt_budget_overruns(attempt_usage)
        )
        control = self._record_terminal_task_failure(
            task_type,
            result,
            failed_budget,
            attempt_usage,
            budget_overruns,
        )
        return SyntheticPilotControllerResult(
            head=control.reference,
            runtime_result=result,
            control_result=control,
        )

    def _record_terminal_task_failure(
        self,
        task_type: TaskType,
        result: RuntimeResult,
        budget: Mapping[str, int],
        attempt_usage: PilotTaskUsageArtifact,
        budget_overruns: Mapping[str, int],
    ) -> PilotControlEventResult:
        input_hashes = {
            "failed_runtime_result": hash_json(result.model_dump(mode="json"))
        }
        payload: dict[str, Any] = {
            "task_type": task_type.value,
            "task_id": result.task_id,
            "outcome": result.outcome.value,
            "attempts": [
                EngineUsageRecord(
                    task_id=item.task_id,
                    attempt_id=f"{item.task_id}/{item.attempt_id}",
                    engine=item.engine,
                    unavailable_reason=MOCK_USAGE_UNAVAILABLE_REASON,
                ).model_dump(mode="json")
                for item in result.attempt_records
            ],
            "attempt_usage": attempt_usage.model_dump(mode="json"),
            "budget_overruns": dict(sorted(budget_overruns.items())),
        }
        control = record_pilot_control_event(
            self.runtime.project_root,
            self.registration,
            self.manifest,
            ordinal=self._next_ordinal,
            stage=_TASK_STAGE[task_type],
            status=PilotStageStatus.FAILED,
            artifact_payload=payload,
            input_hashes=input_hashes,
            budget_consumed=budget,
            previous_stage=self._head,
            reason=f"eligible technical failure: {result.outcome.value}",
            precommit_validator=self._validate_prospective_event,
        )
        self._head = control.reference
        self._refresh_sequence()
        return control

    def record_prerequisites(self) -> SyntheticPilotControllerResult:
        started_at = _monotonic()
        self._require_live_head()
        if self._head is not None:
            raise SyntheticPilotControllerError(
                "prerequisites must be the first pilot journal event"
            )
        registration_artifact, _ = load_pilot_run_registration(
            self.runtime.project_root,
            self.registration,
            expected_manifest=self.manifest,
        )
        _, snapshot, _ = self.runtime.store.load_current()
        budget = self._control_budget(
            started_at=started_at, snapshot=snapshot
        )
        control = record_pilot_control_event(
            self.runtime.project_root,
            self.registration,
            self.manifest,
            ordinal=1,
            stage=PilotStage.PREREQUISITES,
            status=PilotStageStatus.COMPLETED,
            artifact_payload={
                "registered_owner_generation": registration_artifact.owner_generation,
                "corpus_fact_id": registration_artifact.corpus_fact_id,
                "scope_process_fact_id": registration_artifact.scope_process_fact_id,
                "human_review_process_fact_id": (
                    registration_artifact.human_review_process_fact_id
                ),
            },
            input_hashes={},
            budget_consumed=budget,
            precommit_validator=self._validate_prospective_event,
        )
        self._head = control.reference
        self._refresh_sequence()
        return SyntheticPilotControllerResult(
            head=control.reference, control_result=control
        )

    prerequisites = record_prerequisites

    def parse_deep_research(
        self, *, topic: str, document_paths: Sequence[Path]
    ) -> SyntheticPilotControllerResult:
        if topic != self.manifest.topic:
            raise SyntheticPilotControllerError(
                "parse topic differs from the run manifest"
            )
        paths = [Path(value) for value in document_paths]
        if not 2 <= len(paths) <= self.manifest.budget.max_discovery_documents:
            raise PilotBudgetError(
                "pilot discovery document count is outside its fixed budget"
            )
        hashes: list[str] = []
        total_bytes = 0
        for path in paths:
            content = _read_allowed_resource(
                self.runtime,
                path,
                max_bytes=self.manifest.budget.max_discovery_document_bytes,
            )
            total_bytes += len(content)
            if total_bytes > self.manifest.budget.max_discovery_total_bytes:
                raise PilotBudgetError(
                    "pilot discovery resources exceed their total-byte budget"
                )
            hashes.append(hash_bytes(content))
        if tuple(hashes) != self.manifest.discovery_resource_hashes:
            raise SyntheticPilotControllerError(
                "parse document hashes/order differ from the run manifest"
            )
        invocation = ParseDeepResearchInvocation(topic=topic, document_paths=paths)
        return self._run_task(
            TaskType.PARSE_DEEP_RESEARCH,
            invocation,
            delegate=StructuredDiscoveryParseAdapter(
                self.runtime.project_root, invocation
            ),
        )

    run_discovery_parse = parse_deep_research

    def corpus_challenger(
        self,
        *,
        topic: str,
        discovery_artifact: DiscoveryArtifactReference,
    ) -> SyntheticPilotControllerResult:
        if topic != self.manifest.topic:
            raise SyntheticPilotControllerError(
                "challenger topic differs from the run manifest"
            )
        if discovery_artifact.owner_generation != self.runtime.store.current_generation():
            raise StaleSnapshotError("corpus challenger parse artifact is not CURRENT")
        load_discovery_artifact(self.runtime.project_root, discovery_artifact)
        _, snapshot, _ = self.runtime.store.load_current()
        papers = tuple(sorted(snapshot.papers, key=lambda item: item.paper_id))
        if len(papers) != self.manifest.budget.paper_count:
            raise SyntheticPilotControllerError(
                "corpus challenger requires the registered five-paper corpus"
            )
        paths: list[Path] = []
        hashes: list[str] = []
        for paper in papers:
            content, _ = read_contained_regular_file(
                self.runtime.project_root,
                paper.raw_md_path,
                max_bytes=self.manifest.budget.max_paper_bytes,
            )
            if hash_bytes(content) != paper.raw_md_hash:
                raise CorpusIntegrityError(
                    f"canonical paper bytes changed: {paper.paper_id}"
                )
            paths.append(self.runtime.project_root / paper.raw_md_path)
            hashes.append(paper.source_hash)
        invocation = CorpusChallengerInvocation(
            topic=topic,
            paper_ids=[item.paper_id for item in papers],
            discovery_artifact=discovery_artifact.invocation(
                self.runtime.project_root
            ),
            paper_resource_paths=paths,
            paper_source_hashes=hashes,
        )
        return self._run_task(
            TaskType.CORPUS_CHALLENGER,
            invocation,
            delegate=StructuredCorpusChallengeAdapter(
                self.runtime.project_root, invocation
            ),
        )

    run_corpus_challenge = corpus_challenger

    def generate_candidate_claims(
        self,
        *,
        topic: str,
        discovery_artifact: DiscoveryArtifactReference,
        challenge_artifact: DiscoveryArtifactReference,
        existing_theme_ids: Sequence[str] = (),
    ) -> SyntheticPilotControllerResult:
        if topic != self.manifest.topic:
            raise SyntheticPilotControllerError(
                "candidate-claim topic differs from the run manifest"
            )
        invocation = GenerateCandidateClaimsInvocation(
            topic=topic,
            existing_theme_ids=list(existing_theme_ids),
            discovery_artifact=discovery_artifact.invocation(
                self.runtime.project_root
            ),
            challenge_artifact=challenge_artifact.invocation(
                self.runtime.project_root
            ),
        )
        return self._run_task(
            TaskType.GENERATE_CANDIDATE_CLAIMS,
            invocation,
            delegate=StructuredCandidateClaimsAdapter(
                self.runtime.project_root, invocation
            ),
        )

    def generate_retrieval_queries(
        self, *, claim_ids: Sequence[str]
    ) -> SyntheticPilotControllerResult:
        _, snapshot, _ = self.runtime.store.load_current()
        exact_claim_ids = tuple(
            item.claim_id
            for item in sorted(
                snapshot.candidate_claims, key=lambda item: item.claim_id
            )
        )
        supplied = tuple(claim_ids)
        if supplied != exact_claim_ids:
            raise SyntheticPilotControllerError(
                "retrieval-query claims must equal CURRENT in canonical order"
            )
        if len(supplied) > self.manifest.budget.max_claims:
            raise PilotBudgetError("pilot claim selection exceeds its budget")
        return self._run_task(
            TaskType.GENERATE_RETRIEVAL_QUERIES,
            GenerateRetrievalQueriesInvocation(claim_ids=list(claim_ids)),
        )

    def record_retrieval(
        self, ledgers: Sequence[RetrievalLedger]
    ) -> SyntheticPilotControllerResult:
        started_at = _monotonic()
        self._require_live_head()
        if self._task_types() != _PREFIX or self._last_phase() != 4:
            raise SyntheticPilotControllerError(
                "retrieval must immediately follow the fixed discovery/query prefix"
            )
        generation, snapshot, _ = self.runtime.store.load_current()
        exact = tuple(
            RetrievalLedger.model_validate(item.model_dump(mode="json"))
            for item in ledgers
        )
        queries = tuple(sorted(snapshot.retrieval_queries, key=lambda item: item.query_id))
        if tuple(item.query_id for item in exact) != tuple(
            item.query_id for item in queries
        ):
            raise SyntheticPilotControllerError(
                "retrieval ledgers must exactly follow canonical query order"
            )
        corpus = VerifiedCorpus(self.runtime.project_root, generation=generation)
        if corpus.lock_hash != self.manifest.corpus_lock_hash:
            raise CorpusIntegrityError("retrieval corpus differs from run manifest")
        coordinator = UnifiedRetrievalCoordinator.from_generation(
            self.runtime.project_root, generation=generation
        )
        for query, ledger in zip(queries, exact, strict=True):
            if ledger.requested_top_k > self.manifest.budget.retrieval_top_k_per_backend:
                raise PilotBudgetError("retrieval top-k exceeds the pilot budget")
            _, rebuilt = coordinator.retrieve(
                query.query_text,
                query.query_id,
                query.intent,
                top_k=ledger.requested_top_k,
                max_query_terms=ledger.max_query_terms,
            )
            if rebuilt != ledger:
                raise SyntheticPilotControllerError(
                    f"retrieval ledger is not canonical: {query.query_id}"
                )
        payload = PilotRetrievalControlPayload.from_ledgers(
            source_generation=generation,
            corpus_lock_hash=self.manifest.corpus_lock_hash,
            ledgers=exact,
        )
        if payload.total_candidates > self.manifest.budget.max_raw_candidates:
            raise PilotBudgetError("pilot raw-candidate budget exceeded")
        if payload.selected_candidate_count > self.manifest.budget.max_assessed_candidates:
            raise PilotBudgetError("pilot assessed-candidate budget exceeded")
        budget = self._control_budget(
            started_at=started_at,
            snapshot=snapshot,
            counters={
                "raw_candidates": payload.total_candidates,
            },
        )
        control = record_pilot_control_event(
            self.runtime.project_root,
            self.registration,
            self.manifest,
            ordinal=self._next_ordinal,
            stage=PilotStage.RETRIEVAL,
            status=PilotStageStatus.COMPLETED,
            artifact_payload=payload.model_dump(mode="json"),
            input_hashes={
                f"ledger:{item.query_id}": digest
                for item, digest in zip(exact, payload.ledger_hashes, strict=True)
            },
            budget_consumed=budget,
            previous_stage=self._head,
            precommit_validator=self._validate_prospective_event,
        )
        self._head = control.reference
        self._refresh_sequence()
        return SyntheticPilotControllerResult(
            head=control.reference,
            control_result=control,
            artifact_reference=payload,
        )

    def assess_evidence(
        self, ledger: RetrievalLedger
    ) -> SyntheticPilotControllerResult:
        exact = RetrievalLedger.model_validate(ledger.model_dump(mode="json"))
        self._require_live_head()
        retrieval_event = next(
            (
                event
                for event in reversed(self._events)
                if event.task_provenance is None
                and event.stage_record.stage is PilotStage.RETRIEVAL
                and event.stage_record.status is PilotStageStatus.COMPLETED
            ),
            None,
        )
        if retrieval_event is None:
            raise SyntheticPilotControllerError(
                "evidence assessment lacks an authenticated retrieval control"
            )
        retrieval_control = load_pilot_control_artifact(
            self.runtime.project_root, retrieval_event
        )
        parsed = validate_pilot_control_artifact_binding(
            retrieval_event, retrieval_control
        )
        if not isinstance(parsed, PilotRetrievalControlPayload):
            raise SyntheticPilotControllerError(
                "retrieval control payload has the wrong type"
            )
        recorded = {
            item.query_id: item for item in parsed.parsed_ledgers()
        }.get(exact.query_id)
        if recorded != exact:
            raise SyntheticPilotControllerError(
                "evidence ledger differs from the authenticated retrieval control"
            )

        # RETRIEVAL is an authenticated no-op commit.  Re-run the deterministic
        # coordinator on its unchanged CURRENT snapshot so the existing
        # assessment adapter receives a fresh generation-owned ledger.  Every
        # substantive field must remain byte-for-byte equivalent.
        generation, source_snapshot, _ = self.runtime.store.load_current()
        query = next(
            (
                item
                for item in source_snapshot.retrieval_queries
                if item.query_id == exact.query_id
            ),
            None,
        )
        if query is None:
            raise SyntheticPilotControllerError(
                "retrieval control query is absent from CURRENT"
            )
        _, current_ledger = UnifiedRetrievalCoordinator.from_generation(
            self.runtime.project_root, generation=generation
        ).retrieve(
            query.query_text,
            query.query_id,
            query.intent,
            top_k=exact.requested_top_k,
            max_query_terms=exact.max_query_terms,
        )
        expected_current = exact.model_copy(
            update={"source_generation": generation}
        )
        if current_ledger != expected_current:
            raise SyntheticPilotControllerError(
                "retrieval results changed across the no-op control commit"
            )
        adapter = AssessEvidencePromotionAdapter(
            self.runtime.project_root, current_ledger
        )
        ledger_path = _stage_retrieval_ledger_resource(
            self.runtime.project_root, current_ledger
        )
        invocation = AssessEvidenceInvocation(
            source_generation=generation,
            claim_id=adapter._source_query.claim_id,
            query_id=current_ledger.query_id,
            candidate_refs=list(current_ledger.selected_candidate_keys),
            canonical_span_refs=_canonical_span_allowlist(
                source_snapshot, current_ledger
            ),
            retrieval_ledger_path=ledger_path.resolve(strict=True),
        )
        return self._run_task(
            TaskType.ASSESS_EVIDENCE, invocation, delegate=adapter
        )

    def aggregate_paper_evidence(
        self,
        *,
        claim_id: str,
        paper_id: str,
        evidence_ids: Sequence[str],
    ) -> SyntheticPilotControllerResult:
        from vibereview.runtime.dto import AggregatePaperEvidenceInvocation

        return self._run_task(
            TaskType.AGGREGATE_PAPER_EVIDENCE,
            AggregatePaperEvidenceInvocation(
                claim_id=claim_id,
                paper_id=paper_id,
                evidence_ids=list(evidence_ids),
            ),
        )

    def assess_claim(
        self, *, claim_id: str, claim_paper_evidence_ids: Sequence[str]
    ) -> SyntheticPilotControllerResult:
        _, snapshot, _ = self.runtime.store.load_current()
        expected = tuple(
            item.claim_paper_evidence_id
            for item in sorted(
                (
                    item
                    for item in snapshot.claim_paper_evidence
                    if item.claim_id == claim_id
                ),
                key=lambda item: item.claim_paper_evidence_id,
            )
        )
        if tuple(claim_paper_evidence_ids) != expected:
            raise SyntheticPilotControllerError(
                "claim assessment must bind the complete CURRENT CPE set"
            )
        return self._run_task(
            TaskType.ASSESS_CLAIM,
            AssessClaimInvocation(
                claim_id=claim_id,
                claim_paper_evidence_ids=list(claim_paper_evidence_ids),
            ),
        )

    def revise_claim(self, *, claim_id: str) -> SyntheticPilotControllerResult:
        _, snapshot, _ = self.runtime.store.load_current()
        claim = next(
            (item for item in snapshot.candidate_claims if item.claim_id == claim_id),
            None,
        )
        if claim is None:
            raise SyntheticPilotControllerError(f"unknown candidate claim {claim_id}")
        assessment = next(
            (
                item
                for item in snapshot.claim_assessments
                if item.claim_id == claim_id
            ),
            None,
        )
        if assessment is None or assessment.decision not in {
            ClaimDecision.WEAKEN,
            ClaimDecision.NARROW,
            ClaimDecision.REFORMULATE,
        }:
            raise SyntheticPilotControllerError(
                "claim revision requires a modification assessment"
            )
        invocation = ReviseClaimInvocation(
            claim_id=claim_id, current_candidate_claim=claim.candidate_claim
        )
        return self._run_task(
            TaskType.REVISE_CLAIM,
            invocation,
            delegate=RevisedClaimDraftAdapter(),
        )

    def validate_final_claim(
        self,
        *,
        claim_id: str,
        revised_claim: DraftArtifactReference | None = None,
    ) -> SyntheticPilotControllerResult:
        _, snapshot, _ = self.runtime.store.load_current()
        assessment = next(
            (
                item
                for item in snapshot.claim_assessments
                if item.claim_id == claim_id
            ),
            None,
        )
        if assessment is None:
            raise SyntheticPilotControllerError(
                "final claim validation requires an accepted claim assessment"
            )
        if assessment.decision is ClaimDecision.REJECT:
            raise SyntheticPilotControllerError(
                "a rejected claim cannot be final-validated"
            )
        modified = assessment.decision in {
            ClaimDecision.WEAKEN,
            ClaimDecision.NARROW,
            ClaimDecision.REFORMULATE,
        }
        if modified != (revised_claim is not None):
            raise SyntheticPilotControllerError(
                "final claim validation does not match its assessment path"
            )
        if revised_claim is None:
            claim = next(
                (item for item in snapshot.candidate_claims if item.claim_id == claim_id),
                None,
            )
            if claim is None:
                raise SyntheticPilotControllerError(f"unknown candidate claim {claim_id}")
            proposed = claim.candidate_claim
        else:
            artifact = load_draft_artifact(
                self.runtime.project_root, revised_claim
            )
            if artifact.artifact_kind is not DraftArtifactKind.REVISED_CLAIM:
                raise SyntheticPilotControllerError(
                    "final claim validation requires a revised-claim artifact"
                )
            proposal = TASK_SPECS[TaskType.REVISE_CLAIM].proposal_model.model_validate(
                artifact.proposal_payload
            )
            if proposal.claim_ref != claim_id:
                raise SyntheticPilotControllerError(
                    "revised claim artifact belongs to another claim"
                )
            proposed = proposal.final_claim
        return self._run_task(
            TaskType.VALIDATE_FINAL_CLAIM,
            ValidateFinalClaimInvocation(
                claim_id=claim_id, proposed_final_claim=proposed
            ),
        )

    def generate_propositions(
        self,
        *,
        claim_packet_ids: Sequence[str] = (),
        corpus_fact_ids: Sequence[str] | None = None,
        process_fact_ids: Sequence[str] | None = None,
    ) -> SyntheticPilotControllerResult:
        _, snapshot, _ = self.runtime.store.load_current()
        candidate_ids = {item.claim_id for item in snapshot.candidate_claims}
        assessed_ids = {item.claim_id for item in snapshot.claim_assessments}
        if assessed_ids != candidate_ids:
            raise SyntheticPilotControllerError(
                "proposition generation requires every candidate claim assessment"
            )
        exact_packets = tuple(
            item.claim_id
            for item in sorted(snapshot.claim_packets, key=lambda item: item.claim_id)
        )
        if tuple(claim_packet_ids) != exact_packets:
            raise SyntheticPilotControllerError(
                "proposition ClaimPacket selection must equal CURRENT"
            )
        registration_artifact, _ = load_pilot_run_registration(
            self.runtime.project_root,
            self.registration,
            expected_manifest=self.manifest,
        )
        corpus = (
            [registration_artifact.corpus_fact_id]
            if corpus_fact_ids is None
            else list(corpus_fact_ids)
        )
        process = (
            [
                registration_artifact.scope_process_fact_id,
                registration_artifact.human_review_process_fact_id,
            ]
            if process_fact_ids is None
            else list(process_fact_ids)
        )
        invocation = GeneratePropositionsInvocation(
            claim_packet_ids=list(claim_packet_ids),
            corpus_fact_ids=corpus,
            process_fact_ids=process,
        )
        return self._run_task(
            TaskType.GENERATE_PROPOSITIONS,
            invocation,
            delegate=PropositionDraftAcceptanceAdapter(),
        )

    def audit_proposition(
        self,
        *,
        draft: DraftArtifactReference,
        local_ref: str,
    ) -> SyntheticPilotControllerResult:
        artifact = load_draft_artifact(self.runtime.project_root, draft)
        anchor = draft.anchor(self.runtime.project_root, local_ref)
        dependencies = artifact.task_provenance.dependencies
        invocation = AuditPropositionInvocation(
            **anchor.model_dump(mode="json"),
            claim_packet_ids=sorted(
                key.removeprefix("ClaimPacket:")
                for key in dependencies
                if key.startswith("ClaimPacket:")
            ),
            corpus_fact_ids=sorted(
                key.removeprefix("CorpusFact:")
                for key in dependencies
                if key.startswith("CorpusFact:")
            ),
            process_fact_ids=sorted(
                key.removeprefix("ReviewProcessFact:")
                for key in dependencies
                if key.startswith("ReviewProcessFact:")
            ),
        )
        return self._run_task(
            TaskType.AUDIT_PROPOSITION,
            invocation,
            delegate=PropositionAuditAdapter(),
        )

    def render_prose(
        self, *, proposition_ids: Sequence[str]
    ) -> SyntheticPilotControllerResult:
        invocation = RenderProseInvocation(proposition_ids=list(proposition_ids))
        return self._run_task(
            TaskType.RENDER_PROSE,
            invocation,
            delegate=RenderedSentenceDraftAcceptanceAdapter(),
        )

    def audit_rendered_sentence(
        self,
        *,
        draft: DraftArtifactReference,
        local_ref: str,
    ) -> SyntheticPilotControllerResult:
        artifact = load_draft_artifact(self.runtime.project_root, draft)
        proposal = TASK_SPECS[TaskType.RENDER_PROSE].proposal_model.model_validate(
            artifact.proposal_payload
        )
        sentence = next(
            (item for item in proposal.sentences if item.local_ref == local_ref),
            None,
        )
        if sentence is None:
            raise SyntheticPilotControllerError(
                "rendered sentence local ref is absent from the draft"
            )
        anchor = draft.anchor(self.runtime.project_root, local_ref)
        invocation = AuditRenderedSentenceInvocation(
            **anchor.model_dump(mode="json"),
            source_proposition_ids=list(sentence.source_proposition_refs),
        )
        return self._run_task(
            TaskType.AUDIT_RENDERED_SENTENCE,
            invocation,
            delegate=RenderedSentenceAuditAdapter(),
        )

    def exact_assembly(self) -> SyntheticPilotControllerResult:
        started_at = _monotonic()
        self._require_live_head()
        if self._last_phase() != 10:
            raise SyntheticPilotControllerError(
                "exact assembly must follow the sentence-audit stage"
            )
        generation, snapshot, _ = self.runtime.store.load_current()
        owner_generation = generation + 1
        assembly_budget = AssemblyBudget(
            max_sentences=self.manifest.budget.max_initial_sentences,
            max_citations=10_000,
            max_body_utf8_bytes=min(
                8 * 1024 * 1024,
                self.manifest.budget.max_proposal_bytes,
            ),
        )
        try:
            body, assembly = assemble_exact_section(
                snapshot,
                source_generation=owner_generation,
                budget=assembly_budget,
            )
        except PackageCArtifactError as exc:
            blocked_budget = self._control_budget(
                started_at=started_at, snapshot=snapshot
            )
            control = record_pilot_control_event(
                self.runtime.project_root,
                self.registration,
                self.manifest,
                ordinal=self._next_ordinal,
                stage=PilotStage.EXACT_ASSEMBLY,
                status=PilotStageStatus.BLOCKED,
                artifact_payload={"error": str(exc)},
                input_hashes={"repository": snapshot.canonical_hash()},
                budget_consumed=blocked_budget,
                previous_stage=self._head,
                reason="canonical sentences are not eligible for exact assembly",
                precommit_validator=self._validate_prospective_event,
            )
            self._head = control.reference
            self._refresh_sequence()
            return SyntheticPilotControllerResult(
                head=control.reference, control_result=control
            )
        payload = PilotExactAssemblyControlPayload.from_assembly(body, assembly)
        budget = self._control_budget(
            started_at=started_at, snapshot=snapshot
        )
        control = record_pilot_control_event(
            self.runtime.project_root,
            self.registration,
            self.manifest,
            ordinal=self._next_ordinal,
            stage=PilotStage.EXACT_ASSEMBLY,
            status=PilotStageStatus.COMPLETED,
            artifact_payload=payload.model_dump(mode="json"),
            input_hashes={"repository": snapshot.canonical_hash()},
            budget_consumed=budget,
            previous_stage=self._head,
            precommit_validator=self._validate_prospective_event,
        )
        self._head = control.reference
        self._refresh_sequence()
        committed_snapshot, _ = self.runtime.store.load_generation(
            control.generation
        )
        verify_exact_assembly(committed_snapshot, body, assembly)
        self._assembly_body = body
        self._assembly = assembly
        return SyntheticPilotControllerResult(
            head=control.reference,
            control_result=control,
            artifact_reference=payload,
            body=body,
            assembly=assembly,
        )

    def validation_report(self) -> SyntheticPilotControllerResult:
        started_at = _monotonic()
        self._require_live_head()
        if (
            not self._events
            or self._events[-1].stage_record.stage is not PilotStage.EXACT_ASSEMBLY
        ):
            raise SyntheticPilotControllerError(
                "validation report must immediately follow exact assembly"
            )
        generation, snapshot, _ = self.runtime.store.load_current()
        if self._assembly_body is None or self._assembly is None:
            budget = AssemblyBudget(
                max_sentences=self.manifest.budget.max_initial_sentences,
                max_citations=10_000,
                max_body_utf8_bytes=min(
                    8 * 1024 * 1024,
                    self.manifest.budget.max_proposal_bytes,
                ),
            )
            self._assembly_body, self._assembly = assemble_exact_section(
                snapshot, source_generation=generation, budget=budget
            )
        validation = validate_synthetic_pilot_run(
            self.runtime.project_root,
            registration=self.registration,
            journal_head=self._head,
            body=self._assembly_body,
            assembly=self._assembly,
        )
        payload = PilotValidationControlPayload.from_report(
            report=validation.report,
            diagnostics=validation.diagnostics,
        )
        budget = self._control_budget(
            started_at=started_at, snapshot=snapshot
        )
        control = record_pilot_control_event(
            self.runtime.project_root,
            self.registration,
            self.manifest,
            ordinal=self._next_ordinal,
            stage=PilotStage.VALIDATION_REPORT,
            status=PilotStageStatus.COMPLETED,
            artifact_payload=payload.model_dump(mode="json"),
            input_hashes={
                "repository": snapshot.canonical_hash(),
                "validation_report": payload.validation_report_hash,
            },
            budget_consumed=budget,
            previous_stage=self._head,
            precommit_validator=self._validate_prospective_event,
        )
        self._head = control.reference
        self._refresh_sequence()
        # Validation is a no-op commit.  The assembly and report intentionally
        # retain the G+1 generation that was actually assembled and validated;
        # the control event itself is the G+2 journal head.
        final_generation, final_snapshot, _ = self.runtime.store.load_current()
        if (
            final_generation != generation + 1
            or final_snapshot.canonical_hash() != snapshot.canonical_hash()
        ):
            raise SyntheticPilotControllerError(
                "validation control commit changed canonical repository state"
            )
        verify_exact_assembly(
            final_snapshot, self._assembly_body, self._assembly
        )
        return SyntheticPilotControllerResult(
            head=control.reference,
            control_result=control,
            artifact_reference=payload,
            body=self._assembly_body,
            assembly=self._assembly,
            validation=validation,
        )


__all__ = [
    "MOCK_USAGE_UNAVAILABLE_REASON",
    "PILOT_CONTROLLER_VERSION",
    "PilotBudgetError",
    "PilotDomainArtifacts",
    "SyntheticPilotController",
    "SyntheticPilotControllerError",
    "SyntheticPilotControllerResult",
]
