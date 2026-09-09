"""Generation-owned, hash-chained events for the fixed Package C pilot.

This is deliberately not a workflow engine.  It authenticates one accepted
task event or Python-owned control event at a time against an already
registered five-paper run, its immutable runtime artifacts, and the
immediately preceding event.  Semantic events additionally bind exact task
provenance, accepted receipt, and declared engine plan.  The fixed controller
owns sequencing.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import ConfigDict, Field, JsonValue, model_validator

from vibereview.enums import CorpusFactDerivationType, ReviewProcessSourceType
from vibereview.ids import Sha256
from vibereview.models import CorpusFactDerivation

from .artifacts import AssemblyRecord, verify_exact_assembly
from .coupled import CoupledStagingMaterializer
from .hashing import canonical_json_bytes, hash_bytes, hash_json
from .pilot_records import (
    PILOT_CORPUS_FACT_TEXT,
    PILOT_HUMAN_REVIEW_FACT_TEXT,
    PILOT_RUN_ID_PATTERN,
    PILOT_SCOPE_FACT_TEXT,
    PilotRecordModel,
    PilotRunManifest,
    PilotRunRegistrationArtifact,
    PilotRunRegistrationReference,
    PilotStage,
    PilotStagePredecessor,
    PilotStageRecord,
    PilotStageStatus,
    PilotValidationReport,
    ValidationStatus,
)
from .receipts import (
    compute_input_identity_key,
    compute_semantic_task_key,
    verify_receipt_canonical_objects,
)
from .records import (
    AppliedTaskReceipt,
    TaskProvenance,
    TaskType,
)
from .repository import (
    AuxiliaryStagingWriter,
    CrashPoint,
    GenerationStore,
    PromotionPayload,
    StaleSnapshotError,
)
from .state import RepositorySnapshot


PILOT_JOURNAL_VERSION = "2"
PILOT_JOURNAL_ROOT = "pilot/runs"
PILOT_CURRENT_HEAD_FILENAME = "current_head.json"
MAX_STAGE_HASHES = 512
MAX_STAGE_BUDGET_COUNTERS = 32
MAX_STAGE_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_STAGE_CHAIN_EVENTS = 128
MAX_CONTROL_PAYLOAD_DEPTH = 64
MAX_CONTROL_PAYLOAD_NODES = 100_000

_STAGE_TASK_TYPES: dict[PilotStage, frozenset[TaskType]] = {
    PilotStage.DISCOVERY: frozenset(
        {TaskType.PARSE_DEEP_RESEARCH, TaskType.GENERATE_CANDIDATE_CLAIMS}
    ),
    PilotStage.CORPUS_CHALLENGE: frozenset({TaskType.CORPUS_CHALLENGER}),
    PilotStage.QUERY_GENERATION: frozenset(
        {TaskType.GENERATE_RETRIEVAL_QUERIES}
    ),
    PilotStage.EVIDENCE_ASSESSMENT: frozenset({TaskType.ASSESS_EVIDENCE}),
    PilotStage.CLAIM_AGGREGATION: frozenset(
        {TaskType.AGGREGATE_PAPER_EVIDENCE, TaskType.ASSESS_CLAIM}
    ),
    PilotStage.CLAIM_VALIDATION: frozenset(
        {TaskType.REVISE_CLAIM, TaskType.VALIDATE_FINAL_CLAIM}
    ),
    PilotStage.PROPOSITION_AUDIT: frozenset(
        {TaskType.GENERATE_PROPOSITIONS, TaskType.AUDIT_PROPOSITION}
    ),
    PilotStage.SENTENCE_AUDIT: frozenset(
        {TaskType.RENDER_PROSE, TaskType.AUDIT_RENDERED_SENTENCE}
    ),
}

_TASKLESS_COMPLETED_STAGES = frozenset(
    {
        PilotStage.PREREQUISITES,
        PilotStage.RETRIEVAL,
        PilotStage.EXACT_ASSEMBLY,
        PilotStage.VALIDATION_REPORT,
    }
)

_TASKS_REQUIRING_DOMAIN_ARTIFACTS = frozenset(
    {
        TaskType.PARSE_DEEP_RESEARCH,
        TaskType.CORPUS_CHALLENGER,
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        TaskType.ASSESS_EVIDENCE,
        TaskType.REVISE_CLAIM,
        TaskType.GENERATE_PROPOSITIONS,
        TaskType.AUDIT_PROPOSITION,
        TaskType.RENDER_PROSE,
        TaskType.AUDIT_RENDERED_SENTENCE,
    }
)

_BUDGET_FIELDS = {
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


class PilotJournalError(ValueError):
    """A pilot event is malformed or not bound to its transaction."""


def _canonical_model_bytes(model: PilotRecordModel) -> bytes:
    return canonical_json_bytes(model.model_dump(mode="json")) + b"\n"


class PilotResourceSourceDependencyWitness(PilotRecordModel):
    """Frozen hash-only freshness anchor for one sanitized resource."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["external_file"] = "external_file"
    source_hash_at_snapshot: Sha256


class PilotResourceProvenanceWitness(PilotRecordModel):
    """Public-safe resource witness with the operator source path removed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resource_id: str = Field(pattern=r"^RES[0-9]{4,}$")
    logical_name: str
    media_type: str
    bundle_relative_path: Path
    source_dependency: PilotResourceSourceDependencyWitness
    snapshot_hash: Sha256
    size_bytes: int = Field(ge=0)


class PilotTaskProvenanceWitness(PilotRecordModel):
    """Sanitized immutable task identity embedded in the pilot journal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(pattern=r"^TASK[0-9]{4,}$")
    task_type: TaskType
    task_spec_version: str
    prompt_version: str
    base_generation: int = Field(ge=0)
    dependencies: dict[str, Sha256]
    instructions_hash: Sha256
    input_snapshot_hash: Sha256
    engine_input_hash: Sha256
    input_schema_hash: Sha256
    proposal_schema_hash: Sha256
    expected_bundle_manifest_hash: Sha256
    expected_immutable_files: dict[str, Sha256]
    resources: tuple[PilotResourceProvenanceWitness, ...] = ()

    @classmethod
    def from_private(
        cls, provenance: TaskProvenance | "PilotTaskProvenanceWitness"
    ) -> "PilotTaskProvenanceWitness":
        """Detach the receipt-relevant fields while dropping every source path."""

        if isinstance(provenance, cls):
            return cls.model_validate(provenance.model_dump(mode="json"))
        return cls(
            task_id=provenance.task_id,
            task_type=provenance.task_type,
            task_spec_version=provenance.task_spec_version,
            prompt_version=provenance.prompt_version,
            base_generation=provenance.base_generation,
            dependencies=dict(provenance.dependencies),
            instructions_hash=provenance.instructions_hash,
            input_snapshot_hash=provenance.input_snapshot_hash,
            engine_input_hash=provenance.engine_input_hash,
            input_schema_hash=provenance.input_schema_hash,
            proposal_schema_hash=provenance.proposal_schema_hash,
            expected_bundle_manifest_hash=provenance.expected_bundle_manifest_hash,
            expected_immutable_files=dict(provenance.expected_immutable_files),
            resources=tuple(
                PilotResourceProvenanceWitness(
                    resource_id=resource.resource_id,
                    logical_name=resource.logical_name,
                    media_type=resource.media_type,
                    bundle_relative_path=resource.bundle_relative_path,
                    source_dependency=PilotResourceSourceDependencyWitness(
                        source_hash_at_snapshot=(
                            resource.source_dependency.source_hash_at_snapshot
                        )
                    ),
                    snapshot_hash=resource.snapshot_hash,
                    size_bytes=resource.size_bytes,
                )
                for resource in provenance.resources
            ),
        )


def _validated_retrieval_ledgers(
    values: tuple[dict[str, JsonValue], ...],
):
    # Validate lazily through the exact library contract without making the
    # provider-neutral runtime import graph depend on the library at import
    # time.  The control artifact itself contains only canonical JSON values.
    from vibereview.library.retrieval import RetrievalLedger

    try:
        return tuple(RetrievalLedger.model_validate(value) for value in values)
    except Exception as exc:
        raise ValueError("retrieval control contains an invalid ledger") from exc


class PilotRetrievalControlPayload(PilotRecordModel):
    """Closed aggregate witness for deterministic retrieval execution."""

    schema_version: Literal["package-c-pilot-retrieval-control-1"] = (
        "package-c-pilot-retrieval-control-1"
    )
    source_generation: int = Field(ge=0)
    corpus_lock_hash: Sha256
    ledgers: tuple[dict[str, JsonValue], ...] = Field(max_length=200)
    ledger_hashes: tuple[Sha256, ...] = Field(max_length=200)
    query_count: int = Field(ge=0, le=200)
    total_candidates: int = Field(ge=0, le=1_000)
    valid_candidates: int = Field(ge=0, le=1_000)
    invalid_candidates: int = Field(ge=0, le=1_000)
    truncated_valid_candidates: int = Field(ge=0, le=1_000)
    truncated_invalid_candidates: int = Field(ge=0, le=1_000)
    selected_candidate_count: int = Field(ge=0, le=500)
    excluded_by_budget_candidate_count: int = Field(ge=0, le=1_000)

    @model_validator(mode="after")
    def _counts_are_closed(self) -> "PilotRetrievalControlPayload":
        ledgers = _validated_retrieval_ledgers(self.ledgers)
        if len(ledgers) != self.query_count:
            raise ValueError("retrieval control query count differs from its ledgers")
        if len(self.ledger_hashes) != self.query_count:
            raise ValueError("retrieval control requires one ledger hash per query")
        query_ids = tuple(ledger.query_id for ledger in ledgers)
        if query_ids != tuple(sorted(query_ids)) or len(query_ids) != len(
            set(query_ids)
        ):
            raise ValueError(
                "retrieval control ledgers must be query-sorted and unique"
            )
        if any(
            ledger.source_generation != self.source_generation
            or ledger.corpus_lock_hash != self.corpus_lock_hash
            for ledger in ledgers
        ):
            raise ValueError(
                "retrieval control ledgers differ in generation or corpus lock"
            )
        expected_hashes = tuple(
            hash_bytes(
                canonical_json_bytes(ledger.model_dump(mode="json")) + b"\n"
            )
            for ledger in ledgers
        )
        if self.ledger_hashes != expected_hashes:
            raise ValueError("retrieval control ledger hashes differ from content")
        if len(self.ledger_hashes) != len(set(self.ledger_hashes)):
            raise ValueError("retrieval control ledger hashes must be unique")
        ledgered_valid = sum(ledger.valid_candidates for ledger in ledgers)
        ledgered_invalid = sum(ledger.invalid_candidates for ledger in ledgers)
        truncated_valid = sum(
            ledger.text_truncated_valid_candidates
            + ledger.graph_truncated_valid_candidates
            for ledger in ledgers
        )
        truncated_invalid = sum(
            ledger.text_truncated_invalid_candidates
            + ledger.graph_truncated_invalid_candidates
            for ledger in ledgers
        )
        if (
            self.truncated_valid_candidates != truncated_valid
            or self.truncated_invalid_candidates != truncated_invalid
        ):
            raise ValueError("retrieval control truncated counts differ from ledgers")
        if self.valid_candidates != ledgered_valid + truncated_valid:
            raise ValueError("retrieval control valid count differs from ledgers")
        if self.invalid_candidates != ledgered_invalid + truncated_invalid:
            raise ValueError("retrieval control invalid count differs from ledgers")
        if self.valid_candidates + self.invalid_candidates != self.total_candidates:
            raise ValueError("retrieval control candidate counts do not sum")
        if (
            self.selected_candidate_count
            + self.excluded_by_budget_candidate_count
            + self.truncated_valid_candidates
            != self.valid_candidates
        ):
            raise ValueError("retrieval control valid-candidate accounting is incomplete")
        if self.selected_candidate_count != sum(
            len(ledger.selected_candidate_keys) for ledger in ledgers
        ):
            raise ValueError("retrieval selected count differs from its ledgers")
        if self.excluded_by_budget_candidate_count != sum(
            len(ledger.excluded_by_budget_candidate_keys) for ledger in ledgers
        ):
            raise ValueError("retrieval excluded count differs from its ledgers")
        return self

    def parsed_ledgers(self):
        """Return detached instances of the exact library ledger model."""

        return _validated_retrieval_ledgers(self.ledgers)

    @classmethod
    def from_ledgers(
        cls,
        *,
        source_generation: int,
        corpus_lock_hash: Sha256,
        ledgers,
    ) -> "PilotRetrievalControlPayload":
        """Derive every redundant count and hash from exact ledger objects."""

        exact = tuple(sorted(ledgers, key=lambda item: item.query_id))
        serialized = tuple(item.model_dump(mode="json") for item in exact)
        truncated_valid = sum(
            item.text_truncated_valid_candidates
            + item.graph_truncated_valid_candidates
            for item in exact
        )
        truncated_invalid = sum(
            item.text_truncated_invalid_candidates
            + item.graph_truncated_invalid_candidates
            for item in exact
        )
        ledgered_valid = sum(item.valid_candidates for item in exact)
        ledgered_invalid = sum(item.invalid_candidates for item in exact)
        return cls(
            source_generation=source_generation,
            corpus_lock_hash=corpus_lock_hash,
            ledgers=serialized,
            ledger_hashes=tuple(
                hash_bytes(canonical_json_bytes(value) + b"\n")
                for value in serialized
            ),
            query_count=len(exact),
            total_candidates=(
                ledgered_valid
                + ledgered_invalid
                + truncated_valid
                + truncated_invalid
            ),
            valid_candidates=ledgered_valid + truncated_valid,
            invalid_candidates=ledgered_invalid + truncated_invalid,
            truncated_valid_candidates=truncated_valid,
            truncated_invalid_candidates=truncated_invalid,
            selected_candidate_count=sum(
                len(item.selected_candidate_keys) for item in exact
            ),
            excluded_by_budget_candidate_count=sum(
                len(item.excluded_by_budget_candidate_keys) for item in exact
            ),
        )


class PilotExactAssemblyControlPayload(PilotRecordModel):
    """Exact hashes and bounded counts for deterministic section assembly."""

    schema_version: Literal["package-c-pilot-exact-assembly-control-1"] = (
        "package-c-pilot-exact-assembly-control-1"
    )
    source_generation: int = Field(ge=1)
    repository_hash: Sha256
    section_body_hash: Sha256
    assembly_artifact_hash: Sha256
    sentence_count: int = Field(ge=0, le=1_000)
    citation_count: int = Field(ge=0, le=10_000)
    body_size_bytes: int = Field(ge=0, le=8 * 1024 * 1024)
    section_body_utf8: str = Field(max_length=8 * 1024 * 1024)
    assembly: AssemblyRecord

    @model_validator(mode="after")
    def _assembly_is_exact(self) -> "PilotExactAssemblyControlPayload":
        body = self.section_body_utf8.encode("utf-8")
        assembly = self.assembly
        if len(body) > MAX_STAGE_ARTIFACT_BYTES:
            raise ValueError("exact-assembly body exceeds the journal byte limit")
        if (
            self.source_generation != assembly.source_generation
            or self.repository_hash != assembly.repository_hash
            or self.section_body_hash != assembly.body_hash
            or self.section_body_hash != hash_bytes(body)
            or self.assembly_artifact_hash != assembly.artifact_hash
            or self.sentence_count != len(assembly.sentences)
            or self.citation_count != len(assembly.citations)
            or self.body_size_bytes != assembly.body_size_bytes
            or self.body_size_bytes != len(body)
        ):
            raise ValueError("exact-assembly control fields differ from embedded bytes")
        return self

    @classmethod
    def from_assembly(
        cls, body: bytes, assembly: AssemblyRecord
    ) -> "PilotExactAssemblyControlPayload":
        """Build the canonical self-contained control from exact assembly bytes."""

        try:
            text = bytes(body).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("exact-assembly body is not UTF-8") from exc
        return cls(
            source_generation=assembly.source_generation,
            repository_hash=assembly.repository_hash,
            section_body_hash=assembly.body_hash,
            assembly_artifact_hash=assembly.artifact_hash,
            sentence_count=len(assembly.sentences),
            citation_count=len(assembly.citations),
            body_size_bytes=assembly.body_size_bytes,
            section_body_utf8=text,
            assembly=assembly,
        )


class PilotValidationDiagnostics(PilotRecordModel):
    """Closed, bounded diagnostics accompanying one validation report."""

    schema_version: Literal["package-c-pilot-diagnostics-1"] = (
        "package-c-pilot-diagnostics-1"
    )
    checks: dict[str, tuple[str, ...]]

    @model_validator(mode="after")
    def _checks_are_closed(self) -> "PilotValidationDiagnostics":
        expected = {
            "structural_validation",
            "locator_verification",
            "semantic_audits_executed",
            "citation_authorization",
            "exact_assembly",
            "artifact_integrity",
        }
        if set(self.checks) != expected:
            raise ValueError("pilot validation diagnostics check set is incomplete")
        for messages in self.checks.values():
            if len(messages) > 32:
                raise ValueError("pilot validation diagnostics exceed their count limit")
            if any(not message or len(message) > 4_096 for message in messages):
                raise ValueError("pilot validation diagnostic is empty or oversized")
        return self


class PilotValidationControlPayload(PilotRecordModel):
    """Immutable identity and outcomes of one completed validation pass."""

    schema_version: Literal["package-c-pilot-validation-control-1"] = (
        "package-c-pilot-validation-control-1"
    )
    source_generation: int = Field(ge=1)
    repository_hash: Sha256
    validation_report_hash: Sha256
    validation_diagnostics_hash: Sha256
    report: PilotValidationReport
    diagnostics: PilotValidationDiagnostics

    @model_validator(mode="after")
    def _report_is_exact(self) -> "PilotValidationControlPayload":
        report = self.report
        if (
            self.source_generation != report.source_generation
            or self.repository_hash != report.repository_hash
            or self.validation_report_hash != hash_bytes(_canonical_model_bytes(report))
            or self.validation_diagnostics_hash
            != hash_bytes(_canonical_model_bytes(self.diagnostics))
        ):
            raise ValueError("validation control fields differ from embedded report")
        for name, messages in self.diagnostics.checks.items():
            status = getattr(report, name)
            if (status is ValidationStatus.PASSED) != (not messages):
                raise ValueError(
                    "validation report status and diagnostics disagree"
                )
        return self

    @classmethod
    def from_report(
        cls,
        *,
        report: PilotValidationReport,
        diagnostics: Mapping[str, tuple[str, ...]],
    ) -> "PilotValidationControlPayload":
        """Build a self-contained control from an exact CURRENT report."""

        exact_report = PilotValidationReport.model_validate(
            report.model_dump(mode="json")
        )
        exact_diagnostics = PilotValidationDiagnostics(
            checks={name: tuple(messages) for name, messages in diagnostics.items()}
        )
        return cls(
            source_generation=exact_report.source_generation,
            repository_hash=exact_report.repository_hash,
            validation_report_hash=hash_bytes(
                _canonical_model_bytes(exact_report)
            ),
            validation_diagnostics_hash=hash_bytes(
                _canonical_model_bytes(exact_diagnostics)
            ),
            report=exact_report,
            diagnostics=exact_diagnostics,
        )


PilotCompletedControlPayload = (
    PilotRetrievalControlPayload
    | PilotExactAssemblyControlPayload
    | PilotValidationControlPayload
)


_COMPLETED_CONTROL_PAYLOAD_MODELS: dict[
    PilotStage, type[PilotCompletedControlPayload]
] = {
    PilotStage.RETRIEVAL: PilotRetrievalControlPayload,
    PilotStage.EXACT_ASSEMBLY: PilotExactAssemblyControlPayload,
    PilotStage.VALIDATION_REPORT: PilotValidationControlPayload,
}


def validate_completed_pilot_control_payload(
    stage: PilotStage,
    payload: Mapping[str, JsonValue],
) -> PilotCompletedControlPayload | None:
    """Parse a completed control payload through its fixed stage contract."""

    model = _COMPLETED_CONTROL_PAYLOAD_MODELS.get(stage)
    if model is None:
        return None
    try:
        return model.model_validate(dict(payload))
    except Exception as exc:
        raise PilotJournalError(
            f"completed {stage.value} control payload is invalid"
        ) from exc


def _semantic_fingerprint_hash(receipt: AppliedTaskReceipt) -> Sha256:
    fingerprint = receipt.semantic_fingerprint
    return hash_json(
        {
            "validator_fingerprint": fingerprint.validator_fingerprint,
            "promotion_handler_fingerprint": (
                fingerprint.promotion_handler_fingerprint
            ),
            "disposition_handler_fingerprint": (
                fingerprint.disposition_handler_fingerprint
            ),
            "scientific_contract_version": fingerprint.scientific_contract_version,
            "runtime_contract_version": fingerprint.runtime_contract_version,
        }
    )


def _schema_fingerprint(
    provenance: TaskProvenance | PilotTaskProvenanceWitness,
) -> Sha256:
    return hash_json(
        {
            "input_schema": provenance.input_schema_hash,
            "proposal_schema": provenance.proposal_schema_hash,
        }
    )


def _process_source_key(manifest: PilotRunManifest, suffix: str) -> str:
    return f"pilot-run:{manifest.manifest_hash.removeprefix('sha256:')}:{suffix}"


def _canonical_auxiliary_path(path: str) -> bool:
    parsed = PurePosixPath(path)
    return (
        bool(path)
        and not parsed.is_absolute()
        and parsed.as_posix() == path
        and all(component not in {"", ".", ".."} for component in parsed.parts)
    )


def load_pilot_run_registration(
    project_root: Path,
    reference: PilotRunRegistrationReference,
    *,
    expected_manifest: PilotRunManifest | None = None,
) -> tuple[PilotRunRegistrationArtifact, PilotRunManifest]:
    """Authenticate setup, run policy, corpus identity, and current fact uniqueness."""

    try:
        exact_reference = PilotRunRegistrationReference.model_validate(
            reference.model_dump(mode="json")
        )
        store = GenerationStore(project_root)
        _, owner_snapshot, _ = store.load_current()
        registered_snapshot, _, selected = store.load_generation_auxiliary(
            exact_reference.owner_generation,
            {exact_reference.relative_path},
        )
        content = selected[exact_reference.relative_path]
        if hash_bytes(content) != exact_reference.artifact_hash:
            raise PilotJournalError("pilot registration content hash mismatch")
        registration = PilotRunRegistrationArtifact.model_validate_json(content)
        if (
            canonical_json_bytes(registration.model_dump(mode="json")) + b"\n"
            != content
        ):
            raise PilotJournalError("pilot registration bytes are not canonical")
        if (
            registration.run_id != exact_reference.run_id
            or registration.owner_generation != exact_reference.owner_generation
        ):
            raise PilotJournalError("pilot registration and reference differ")

        _, _, manifest_selected = store.load_generation_auxiliary(
            registration.owner_generation,
            {registration.run_manifest_relative_path},
        )
        manifest_content = manifest_selected[registration.run_manifest_relative_path]
        if hash_bytes(manifest_content) != registration.run_manifest_content_hash:
            raise PilotJournalError("registered run-manifest content hash mismatch")
        manifest = PilotRunManifest.model_validate_json(manifest_content)
        if (
            canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n"
            != manifest_content
        ):
            raise PilotJournalError("registered run-manifest bytes are not canonical")
        if (
            manifest.run_id != registration.run_id
            or manifest.source_generation != registration.source_generation
            or manifest.manifest_hash != registration.run_manifest_hash
        ):
            raise PilotJournalError("pilot registration names another run manifest")
        if expected_manifest is not None and manifest != expected_manifest:
            raise PilotJournalError("registered run manifest differs from expectation")

        source_snapshot, _ = store.load_generation(registration.source_generation)
        source_papers = tuple(
            sorted(source_snapshot.papers, key=lambda item: item.paper_id)
        )
        if (
            len(source_papers) != 5
            or tuple(item.source_hash for item in source_papers)
            != manifest.paper_source_hashes
        ):
            raise PilotJournalError("registered five-paper source identity differs")
        paper_ids = tuple(item.paper_id for item in source_papers)

        corpus_matches = tuple(
            item
            for item in registered_snapshot.corpus_facts
            if item.corpus_fact_id == registration.corpus_fact_id
            and item.text == PILOT_CORPUS_FACT_TEXT
            and item.derivation_type
            is CorpusFactDerivationType.REGISTRY_ARITHMETIC
            and tuple(item.source_paper_ids) == paper_ids
            and item.derivation
            == CorpusFactDerivation(
                task_version=None,
                model_signature=None,
                input_hash=None,
                output_hash=None,
            )
        )
        scope_key = _process_source_key(manifest, "scope")
        human_key = _process_source_key(manifest, "human-review")
        scope_matches = tuple(
            item
            for item in registered_snapshot.process_facts
            if item.process_fact_id == registration.scope_process_fact_id
            and item.text == PILOT_SCOPE_FACT_TEXT
            and item.source_type is ReviewProcessSourceType.RUN_MANIFEST
            and item.source_key == scope_key
        )
        human_matches = tuple(
            item
            for item in registered_snapshot.process_facts
            if item.process_fact_id == registration.human_review_process_fact_id
            and item.text == PILOT_HUMAN_REVIEW_FACT_TEXT
            and item.source_type is ReviewProcessSourceType.RUN_MANIFEST
            and item.source_key == human_key
        )
        if not (
            len(corpus_matches) == len(scope_matches) == len(human_matches) == 1
        ):
            raise PilotJournalError(
                "registered pilot setup facts are absent or changed"
            )

        current_scope = tuple(
            item
            for item in owner_snapshot.process_facts
            if item.source_key == scope_key
        )
        current_human = tuple(
            item
            for item in owner_snapshot.process_facts
            if item.source_key == human_key
        )
        current_corpus = tuple(
            item
            for item in owner_snapshot.corpus_facts
            if item.corpus_fact_id == registration.corpus_fact_id
        )
        current_papers = tuple(
            sorted(owner_snapshot.papers, key=lambda item: item.paper_id)
        )
        if (
            current_corpus != corpus_matches
            or current_scope != scope_matches
            or current_human != human_matches
            or current_papers != source_papers
        ):
            raise PilotJournalError(
                "CURRENT does not contain one unique exact registered pilot fact set"
            )
        return registration, manifest
    except PilotJournalError:
        raise
    except Exception as exc:
        raise PilotJournalError("pilot run registration could not be verified") from exc


def _validate_control_payload(value: JsonValue) -> None:
    """Reject non-finite or pathologically nested JSON before serialization."""

    pending: list[tuple[JsonValue, int]] = [(value, 0)]
    nodes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > MAX_CONTROL_PAYLOAD_NODES:
            raise ValueError("pilot control payload exceeds its node budget")
        if depth > MAX_CONTROL_PAYLOAD_DEPTH:
            raise ValueError("pilot control payload exceeds its nesting budget")
        if isinstance(current, dict):
            for key, child in current.items():
                if not key or len(key) > 256:
                    raise ValueError(
                        "pilot control payload keys must be 1..256 characters"
                    )
                pending.append((child, depth + 1))
        elif isinstance(current, list):
            pending.extend((child, depth + 1) for child in current)
        elif isinstance(current, float) and not math.isfinite(current):
            raise ValueError("pilot control payload contains a non-finite number")


class PilotControlArtifact(PilotRecordModel):
    """Canonical taskless output bound to one journal transition."""

    schema_version: Literal["package-c-pilot-control-1"] = (
        "package-c-pilot-control-1"
    )
    registration: PilotRunRegistrationReference
    run_manifest_hash: Sha256
    run_id: str = Field(pattern=PILOT_RUN_ID_PATTERN)
    ordinal: int = Field(ge=1, le=MAX_STAGE_CHAIN_EVENTS)
    stage: PilotStage
    status: PilotStageStatus
    source_generation: int = Field(ge=0)
    committed_generation: int = Field(ge=1)
    input_hashes: dict[str, Sha256]
    budget_consumed: dict[str, int]
    previous_stage: PilotStagePredecessor | None = None
    reason: str | None = Field(default=None, max_length=4_096)
    payload: dict[str, JsonValue] = Field(
        default_factory=dict, max_length=MAX_STAGE_HASHES
    )

    @model_validator(mode="after")
    def _control_binding(self) -> "PilotControlArtifact":
        if self.registration.run_id != self.run_id:
            raise ValueError("pilot control artifact belongs to another registration")
        if self.committed_generation != self.source_generation + 1:
            raise ValueError("pilot control artifact must own the next generation")
        if len(self.input_hashes) > MAX_STAGE_HASHES:
            raise ValueError("pilot control artifact exceeds its input-hash budget")
        if len(self.budget_consumed) > MAX_STAGE_BUDGET_COUNTERS:
            raise ValueError("pilot control artifact exceeds its budget-counter limit")
        if any(value < 0 for value in self.budget_consumed.values()):
            raise ValueError("pilot control budget consumption cannot be negative")
        if self.input_hashes.get("pilot_run_manifest") != self.run_manifest_hash:
            raise ValueError("pilot control artifact does not bind its run manifest")
        if (
            self.input_hashes.get("pilot_run_registration")
            != self.registration.artifact_hash
        ):
            raise ValueError("pilot control artifact does not bind its registration")
        if self.status is PilotStageStatus.COMPLETED:
            if self.stage not in _TASKLESS_COMPLETED_STAGES:
                raise ValueError("completed control artifact is invalid for this stage")
            if self.reason is not None:
                raise ValueError("completed control artifact cannot carry a reason")
            parsed_payload = validate_completed_pilot_control_payload(
                self.stage, self.payload
            )
            if isinstance(parsed_payload, PilotRetrievalControlPayload):
                if parsed_payload.source_generation != self.source_generation:
                    raise ValueError(
                        "retrieval control payload names another source generation"
                    )
            elif isinstance(parsed_payload, PilotExactAssemblyControlPayload):
                if parsed_payload.source_generation != self.committed_generation:
                    raise ValueError(
                        "exact-assembly payload must name its no-op owner generation"
                    )
            elif isinstance(parsed_payload, PilotValidationControlPayload):
                if parsed_payload.source_generation != self.source_generation:
                    raise ValueError(
                        "validation payload must name the generation it validated"
                    )
        elif not self.reason:
            raise ValueError("blocked or failed control artifact requires a reason")
        if self.ordinal == 1:
            if self.previous_stage is not None:
                raise ValueError("first control artifact cannot name a predecessor")
        elif self.previous_stage is None:
            raise ValueError("later control artifact requires a predecessor")
        if self.previous_stage is not None:
            if (
                self.previous_stage.run_id != self.run_id
                or self.previous_stage.ordinal + 1 != self.ordinal
                or self.previous_stage.owner_generation != self.source_generation
            ):
                raise ValueError(
                    "pilot control predecessor is not exact and consecutive"
                )
        _validate_control_payload(self.payload)
        return self


def _validate_receipt_integrity(
    receipt: AppliedTaskReceipt,
    provenance: TaskProvenance | PilotTaskProvenanceWitness,
    snapshot,
    *,
    engine_plan_hash: Sha256,
) -> None:
    from .specs import TASK_SPECS

    try:
        TASK_SPECS[receipt.task_type].proposal_model.model_validate(
            receipt.proposal_payload
        )
    except Exception as exc:
        raise ValueError("pilot receipt proposal does not match its TaskSpec") from exc
    if receipt.proposal_hash != hash_json(receipt.proposal_payload):
        raise ValueError("pilot receipt proposal hash mismatch")
    if (
        receipt.semantic_fingerprint.combined_fingerprint
        != _semantic_fingerprint_hash(receipt)
    ):
        raise ValueError("pilot receipt semantic fingerprint is inconsistent")
    if receipt.semantic_task_key != compute_semantic_task_key(
        input_identity_key=receipt.input_identity_key,
        semantic_fingerprint=receipt.semantic_fingerprint,
    ):
        raise ValueError("pilot receipt semantic task key mismatch")
    if receipt.task_type is not provenance.task_type:
        raise ValueError("pilot receipt task type differs from provenance")
    if receipt.task_spec_version != provenance.task_spec_version:
        raise ValueError("pilot receipt task-spec version differs from provenance")
    if receipt.source_generation != provenance.base_generation:
        raise ValueError("pilot receipt generation differs from provenance")
    expected_input_identity = compute_input_identity_key(
        task_type=provenance.task_type,
        task_spec_version=provenance.task_spec_version,
        prompt_hash=provenance.instructions_hash,
        input_schema_hash=provenance.input_schema_hash,
        proposal_schema_hash=provenance.proposal_schema_hash,
        dependency_hashes=provenance.dependencies,
        resource_hashes={
            resource.resource_id: resource.snapshot_hash
            for resource in provenance.resources
        },
        engine_input_hash=provenance.engine_input_hash,
        engine="ordered-engine-plan",
        engine_version=receipt.semantic_fingerprint.runtime_contract_version,
        safe_engine_configuration_hash=engine_plan_hash,
        scientific_contract_version=(
            receipt.semantic_fingerprint.scientific_contract_version
        ),
    )
    if receipt.input_identity_key != expected_input_identity:
        raise ValueError("pilot receipt input-identity key is inconsistent")
    attempt_prefix = f"{provenance.task_id}/"
    attempt_suffix = receipt.accepted_attempt_id.removeprefix(attempt_prefix)
    if (
        not receipt.accepted_attempt_id.startswith(attempt_prefix)
        or not attempt_suffix
        or "/" in attempt_suffix
    ):
        raise ValueError("pilot receipt attempt does not belong to its task")
    qualified_ids = [item.qualified_id for item in receipt.canonical_objects]
    if len(qualified_ids) != len(set(qualified_ids)):
        raise ValueError("pilot receipt has duplicate canonical object witnesses")
    canonical_raw_ids = {
        qualified_id.rsplit(":", 1)[-1] for qualified_id in qualified_ids
    }
    if any(value not in canonical_raw_ids for value in receipt.local_ref_map.values()):
        raise ValueError(
            "pilot receipt local map is not covered by canonical witnesses"
        )
    canonical_ok, canonical_reason = verify_receipt_canonical_objects(
        receipt, snapshot
    )
    if not canonical_ok:
        raise ValueError(canonical_reason or "pilot canonical receipt changed")


class PilotStageArtifact(PilotRecordModel):
    """One accepted semantic task event and every immutable trust anchor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["package-c-stage-artifact-3"] = (
        "package-c-stage-artifact-3"
    )
    registration: PilotRunRegistrationReference
    run_manifest_hash: Sha256
    run_manifest: PilotRunManifest
    stage_record_hash: Sha256
    stage_record: PilotStageRecord
    task_provenance_hash: Sha256 | None = None
    task_provenance: PilotTaskProvenanceWitness | None = None
    accepted_receipt_hash: Sha256 | None = None
    accepted_receipt: AppliedTaskReceipt | None = None
    engine_plan_hash: Sha256 | None = None

    @model_validator(mode="after")
    def _bindings_are_exact(self) -> "PilotStageArtifact":
        manifest = self.run_manifest
        record = self.stage_record
        receipt = self.accepted_receipt
        provenance = self.task_provenance

        if self.registration.run_id != manifest.run_id:
            raise ValueError("pilot registration and run identifiers differ")
        if self.run_manifest_hash != manifest.manifest_hash:
            raise ValueError("pilot stage run-manifest hash mismatch")
        if self.stage_record_hash != record.record_hash:
            raise ValueError("pilot stage record hash mismatch")
        if record.run_id != manifest.run_id:
            raise ValueError("pilot stage and run identifiers differ")
        if record.ordinal == 1:
            if record.source_generation != self.registration.owner_generation:
                raise ValueError("first pilot event does not follow registered setup")
        if len(record.input_hashes) > MAX_STAGE_HASHES:
            raise ValueError("pilot stage exceeds its input-hash budget")
        if len(record.artifact_hashes) > MAX_STAGE_HASHES:
            raise ValueError("pilot stage exceeds its artifact-hash budget")
        if len(record.budget_consumed) > MAX_STAGE_BUDGET_COUNTERS:
            raise ValueError("pilot stage exceeds its budget-counter limit")
        if record.input_hashes.get("pilot_run_manifest") != manifest.manifest_hash:
            raise ValueError("pilot stage does not bind the exact run manifest")
        for path in record.artifact_hashes:
            if not _canonical_auxiliary_path(path):
                raise ValueError("pilot stage artifact path is unsafe")
        task_fields = (
            self.task_provenance_hash,
            provenance,
            self.accepted_receipt_hash,
            receipt,
            self.engine_plan_hash,
        )
        if any(value is not None for value in task_fields) and not all(
            value is not None for value in task_fields
        ):
            raise ValueError("pilot task witness fields must be present together")
        if provenance is None or receipt is None:
            if record.task_ids:
                raise ValueError("taskless pilot event cannot name a semantic task")
            if (
                record.status is PilotStageStatus.COMPLETED
                and record.stage not in _TASKLESS_COMPLETED_STAGES
            ):
                raise ValueError("completed taskless event is invalid for this stage")
            if not record.artifact_hashes:
                raise ValueError("taskless pilot event requires an immutable artifact")
        else:
            task_id = provenance.task_id
            if self.task_provenance_hash != hash_json(
                provenance.model_dump(mode="json")
            ):
                raise ValueError("pilot stage task-provenance hash mismatch")
            if self.accepted_receipt_hash != hash_json(
                receipt.model_dump(mode="json")
            ):
                raise ValueError("pilot stage accepted-receipt hash mismatch")
            if record.status is not PilotStageStatus.COMPLETED:
                raise ValueError("accepted task event must be COMPLETED")
            if record.task_ids != (task_id,):
                raise ValueError("pilot event must name exactly its accepted task")
            allowed_tasks = _STAGE_TASK_TYPES.get(record.stage, frozenset())
            if provenance.task_type not in allowed_tasks:
                raise ValueError(
                    "pilot stage is not compatible with its accepted task type"
                )
            if not (
                record.source_generation
                == provenance.base_generation
                == receipt.source_generation
                and record.committed_generation == receipt.committed_generation
                and receipt.committed_generation == receipt.source_generation + 1
            ):
                raise ValueError(
                    "pilot stage, provenance, and receipt generations differ"
                )
            if (
                provenance.task_type in _TASKS_REQUIRING_DOMAIN_ARTIFACTS
                and not record.artifact_hashes
            ):
                raise ValueError(
                    "pilot task requires a generation-owned domain artifact"
                )
            if (
                manifest.prompt_fingerprints[provenance.task_type]
                != provenance.instructions_hash
            ):
                raise ValueError("pilot task prompt differs from the run manifest")
            if manifest.schema_fingerprints[
                provenance.task_type
            ] != _schema_fingerprint(provenance):
                raise ValueError("pilot task schemas differ from the run manifest")
            if (
                manifest.validator_fingerprint
                != receipt.semantic_fingerprint.validator_fingerprint
            ):
                raise ValueError("pilot task validator differs from the run manifest")
            binding = manifest.engine_role_plan[provenance.task_type]
            if (
                receipt.engine != binding.engine
                or receipt.engine_version != binding.engine_version
            ):
                raise ValueError(
                    "pilot task engine differs from the predeclared role plan"
                )
            if self.engine_plan_hash != binding.safe_configuration_hash:
                raise ValueError(
                    "pilot task execution plan differs from the run manifest"
                )
            if provenance.task_type is TaskType.PARSE_DEEP_RESEARCH and tuple(
                resource.snapshot_hash for resource in provenance.resources
            ) != tuple(manifest.discovery_resource_hashes):
                raise ValueError(
                    "pilot parse resources differ from the run manifest"
                )
        for name, value in record.budget_consumed.items():
            field_name = _BUDGET_FIELDS.get(name)
            if field_name is None:
                raise ValueError(f"unknown pilot budget counter {name}")
            if value > getattr(manifest.budget, field_name):
                raise ValueError(f"pilot budget exceeded for {name}")
        return self


class PilotStageArtifactReference(PilotRecordModel):
    """Content-addressed locator for one generation-owned journal event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(pattern=PILOT_RUN_ID_PATTERN)
    ordinal: int = Field(ge=1, le=MAX_STAGE_CHAIN_EVENTS)
    stage: PilotStage
    owner_generation: int = Field(ge=1)
    stage_record_hash: Sha256
    artifact_hash: Sha256
    relative_path: str

    @model_validator(mode="after")
    def _path_is_canonical(self) -> "PilotStageArtifactReference":
        expected = _stage_relative_path(
            self.run_id, self.ordinal, self.stage, self.artifact_hash
        )
        if self.relative_path != expected:
            raise ValueError("pilot stage artifact path is not content addressed")
        return self

    def predecessor(self) -> PilotStagePredecessor:
        return PilotStagePredecessor(
            run_id=self.run_id,
            ordinal=self.ordinal,
            stage=self.stage,
            owner_generation=self.owner_generation,
            stage_record_hash=self.stage_record_hash,
            artifact_hash=self.artifact_hash,
            relative_path=self.relative_path,
        )


class PilotCurrentHeadIndex(PilotRecordModel):
    """Fixed-path CURRENT pointer authenticated by its generation manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["package-c-pilot-current-head-1"] = (
        "package-c-pilot-current-head-1"
    )
    registration: PilotRunRegistrationReference
    run_manifest_hash: Sha256
    run_id: str = Field(pattern=PILOT_RUN_ID_PATTERN)
    owner_generation: int = Field(ge=1)
    event_count: int = Field(ge=1, le=MAX_STAGE_CHAIN_EVENTS)
    head_reference_hash: Sha256
    head: PilotStageArtifactReference

    @model_validator(mode="after")
    def _head_is_exact(self) -> "PilotCurrentHeadIndex":
        if (
            self.registration.run_id != self.run_id
            or self.head.run_id != self.run_id
            or self.head.owner_generation != self.owner_generation
            or self.head.ordinal != self.event_count
            or self.head_reference_hash
            != hash_json(self.head.model_dump(mode="json"))
        ):
            raise ValueError("pilot CURRENT-head index bindings differ")
        return self


@dataclass(frozen=True, slots=True)
class PreparedPilotStageArtifact:
    artifact: PilotStageArtifact
    reference: PilotStageArtifactReference
    content: bytes


PilotPrecommitValidator = Callable[
    [PilotStageArtifact, PilotControlArtifact | None, RepositorySnapshot], None
]


def _stage_relative_path(
    run_id: str,
    ordinal: int,
    stage: PilotStage,
    artifact_hash: Sha256,
) -> str:
    digest = artifact_hash.removeprefix("sha256:")
    return (
        f"{PILOT_JOURNAL_ROOT}/{run_id}/stages/"
        f"{ordinal:04d}-{stage.value}-{digest}.json"
    )


def _current_head_relative_path(run_id: str) -> str:
    return f"{PILOT_JOURNAL_ROOT}/{run_id}/{PILOT_CURRENT_HEAD_FILENAME}"


def _current_head_content(
    *,
    registration: PilotRunRegistrationReference,
    run_manifest: PilotRunManifest,
    head: PilotStageArtifactReference,
) -> tuple[str, bytes]:
    try:
        index = PilotCurrentHeadIndex(
            registration=registration,
            run_manifest_hash=run_manifest.manifest_hash,
            run_id=run_manifest.run_id,
            owner_generation=head.owner_generation,
            event_count=head.ordinal,
            head_reference_hash=hash_json(head.model_dump(mode="json")),
            head=head,
        )
    except Exception as exc:
        raise PilotJournalError("pilot CURRENT-head index is invalid") from exc
    content = _canonical_model_bytes(index)
    if len(content) > MAX_STAGE_ARTIFACT_BYTES:
        raise PilotJournalError("pilot CURRENT-head index exceeds its byte limit")
    return _current_head_relative_path(run_manifest.run_id), content


def _control_relative_path(
    run_id: str,
    ordinal: int,
    stage: PilotStage,
    status: PilotStageStatus,
    artifact_hash: Sha256,
) -> str:
    digest = artifact_hash.removeprefix("sha256:")
    return (
        f"{PILOT_JOURNAL_ROOT}/{run_id}/control/"
        f"{ordinal:04d}-{stage.value}-{status.value.lower()}-{digest}.json"
    )


def _reference_from_predecessor(
    predecessor: PilotStagePredecessor,
) -> PilotStageArtifactReference:
    return PilotStageArtifactReference(
        run_id=predecessor.run_id,
        ordinal=predecessor.ordinal,
        stage=predecessor.stage,
        owner_generation=predecessor.owner_generation,
        stage_record_hash=predecessor.stage_record_hash,
        artifact_hash=predecessor.artifact_hash,
        relative_path=predecessor.relative_path,
    )


def build_pilot_stage_artifact(
    *,
    registration: PilotRunRegistrationReference,
    run_manifest: PilotRunManifest,
    stage_record: PilotStageRecord,
    task_provenance: TaskProvenance | PilotTaskProvenanceWitness | None = None,
    accepted_receipt: AppliedTaskReceipt | None = None,
    engine_plan_hash: Sha256 | None = None,
) -> PreparedPilotStageArtifact:
    """Build canonical stage bytes without writing filesystem state."""

    sanitized_provenance = (
        PilotTaskProvenanceWitness.from_private(task_provenance)
        if task_provenance is not None
        else None
    )
    try:
        artifact = PilotStageArtifact(
            registration=registration,
            run_manifest_hash=run_manifest.manifest_hash,
            run_manifest=run_manifest,
            stage_record_hash=stage_record.record_hash,
            stage_record=stage_record,
            task_provenance_hash=(
                hash_json(sanitized_provenance.model_dump(mode="json"))
                if sanitized_provenance is not None
                else None
            ),
            task_provenance=sanitized_provenance,
            accepted_receipt_hash=(
                hash_json(accepted_receipt.model_dump(mode="json"))
                if accepted_receipt is not None
                else None
            ),
            accepted_receipt=accepted_receipt,
            engine_plan_hash=engine_plan_hash,
        )
    except Exception as exc:
        raise PilotJournalError("pilot stage artifact bindings are invalid") from exc
    content = canonical_json_bytes(artifact.model_dump(mode="json")) + b"\n"
    if len(content) > MAX_STAGE_ARTIFACT_BYTES:
        raise PilotJournalError("pilot stage artifact exceeds its byte limit")
    artifact_hash = hash_bytes(content)
    record = artifact.stage_record
    reference = PilotStageArtifactReference(
        run_id=artifact.run_manifest.run_id,
        ordinal=record.ordinal,
        stage=record.stage,
        owner_generation=record.committed_generation,
        stage_record_hash=artifact.stage_record_hash,
        artifact_hash=artifact_hash,
        relative_path=_stage_relative_path(
            artifact.run_manifest.run_id,
            record.ordinal,
            record.stage,
            artifact_hash,
        ),
    )
    return PreparedPilotStageArtifact(artifact, reference, content)


@dataclass(frozen=True, slots=True)
class PreparedPilotControlEvent:
    control_artifact: PilotControlArtifact
    control_relative_path: str
    control_hash: Sha256
    control_content: bytes
    stage: PreparedPilotStageArtifact


class PilotControlEventResult(PilotRecordModel):
    """Accounting result for a newly committed or exactly reused event."""

    schema_version: Literal["package-c-pilot-control-result-1"] = (
        "package-c-pilot-control-result-1"
    )
    generation: int = Field(ge=1)
    commit_performed: bool
    reused_generation: int | None = Field(default=None, ge=1)
    control_artifact_relative_path: str
    control_artifact_hash: Sha256
    reference: PilotStageArtifactReference

    @model_validator(mode="after")
    def _reuse_accounting(self) -> "PilotControlEventResult":
        if self.commit_performed:
            if self.reused_generation is not None:
                raise ValueError("new pilot control commit cannot report reuse")
            if self.generation != self.reference.owner_generation:
                raise ValueError("new pilot control result reports another generation")
        else:
            if self.reused_generation != self.reference.owner_generation:
                raise ValueError("pilot control reuse must name its owner generation")
            if self.generation < self.reused_generation:
                raise ValueError("pilot control reuse is newer than CURRENT")
        if not _canonical_auxiliary_path(self.control_artifact_relative_path):
            raise ValueError("pilot control artifact path is unsafe")
        return self


def _prepare_pilot_control_event(
    *,
    registration: PilotRunRegistrationReference,
    run_manifest: PilotRunManifest,
    ordinal: int,
    stage: PilotStage,
    status: PilotStageStatus,
    source_generation: int,
    input_hashes: Mapping[str, Sha256],
    budget_consumed: Mapping[str, int],
    previous_stage: PilotStageArtifactReference | None,
    reason: str | None,
    artifact_payload: Mapping[str, JsonValue],
) -> PreparedPilotControlEvent:
    predecessor = previous_stage.predecessor() if previous_stage is not None else None
    try:
        control_artifact = PilotControlArtifact(
            registration=registration,
            run_manifest_hash=run_manifest.manifest_hash,
            run_id=run_manifest.run_id,
            ordinal=ordinal,
            stage=stage,
            status=status,
            source_generation=source_generation,
            committed_generation=source_generation + 1,
            input_hashes=dict(input_hashes),
            budget_consumed=dict(budget_consumed),
            previous_stage=predecessor,
            reason=reason,
            payload=dict(artifact_payload),
        )
        _validate_control_payload(control_artifact.payload)
    except Exception as exc:
        raise PilotJournalError(
            f"pilot control artifact bindings are invalid: {exc}"
        ) from exc

    try:
        control_content = (
            canonical_json_bytes(control_artifact.model_dump(mode="json")) + b"\n"
        )
    except Exception as exc:
        raise PilotJournalError(
            "pilot control artifact is not canonical UTF-8 JSON"
        ) from exc
    if len(control_content) > MAX_STAGE_ARTIFACT_BYTES:
        raise PilotJournalError("pilot control artifact exceeds its byte limit")
    control_hash = hash_bytes(control_content)
    control_relative_path = _control_relative_path(
        run_manifest.run_id, ordinal, stage, status, control_hash
    )
    try:
        stage_record = PilotStageRecord(
            run_id=run_manifest.run_id,
            ordinal=ordinal,
            stage=stage,
            status=status,
            source_generation=source_generation,
            committed_generation=source_generation + 1,
            input_hashes=dict(input_hashes),
            task_ids=(),
            artifact_hashes={control_relative_path: control_hash},
            budget_consumed=dict(budget_consumed),
            previous_stage=predecessor,
            reason=reason,
        )
    except Exception as exc:
        raise PilotJournalError("pilot control stage record is invalid") from exc
    stage_artifact = build_pilot_stage_artifact(
        registration=registration,
        run_manifest=run_manifest,
        stage_record=stage_record,
    )
    return PreparedPilotControlEvent(
        control_artifact=control_artifact,
        control_relative_path=control_relative_path,
        control_hash=control_hash,
        control_content=control_content,
        stage=stage_artifact,
    )


class PilotControlEventMaterializer:
    """Write one prevalidated control artifact and journal event atomically."""

    def __init__(
        self,
        *,
        prepared: PreparedPilotControlEvent,
        source_snapshot_hash: Sha256,
        source_registry_hash: Sha256,
        precommit_validator: PilotPrecommitValidator | None = None,
    ) -> None:
        if len(prepared.control_content) > MAX_STAGE_ARTIFACT_BYTES:
            raise PilotJournalError("pilot control artifact exceeds its byte limit")
        if hash_bytes(prepared.control_content) != prepared.control_hash:
            raise PilotJournalError("prepared pilot control content hash mismatch")
        expected_control_path = _control_relative_path(
            prepared.control_artifact.run_id,
            prepared.control_artifact.ordinal,
            prepared.control_artifact.stage,
            prepared.control_artifact.status,
            prepared.control_hash,
        )
        if prepared.control_relative_path != expected_control_path:
            raise PilotJournalError("prepared pilot control path is not canonical")
        if hash_bytes(prepared.stage.content) != prepared.stage.reference.artifact_hash:
            raise PilotJournalError("prepared pilot journal content hash mismatch")
        try:
            parsed_stage = PilotStageArtifact.model_validate_json(
                prepared.stage.content
            )
        except Exception as exc:
            raise PilotJournalError(
                "prepared pilot journal content is invalid"
            ) from exc
        if parsed_stage != prepared.stage.artifact:
            raise PilotJournalError("prepared pilot journal model differs from bytes")
        rebuilt_stage = build_pilot_stage_artifact(
            registration=parsed_stage.registration,
            run_manifest=parsed_stage.run_manifest,
            stage_record=parsed_stage.stage_record,
        )
        if rebuilt_stage != prepared.stage:
            raise PilotJournalError("prepared pilot journal is not canonical and exact")
        _validate_taskless_control_artifact(
            prepared.stage.artifact,
            {prepared.control_relative_path: prepared.control_content},
        )
        self.prepared = prepared
        self.source_snapshot_hash = source_snapshot_hash
        self.source_registry_hash = source_registry_hash
        self.precommit_validator = precommit_validator

    def __call__(
        self,
        writer: AuxiliaryStagingWriter,
        next_generation: int,
        payload: PromotionPayload,
    ) -> None:
        expected_generation = self.prepared.stage.reference.owner_generation
        if next_generation != expected_generation:
            raise PilotJournalError("pilot control event owns another generation")
        if payload.allocated_ids:
            raise PilotJournalError("pilot control event cannot allocate canonical IDs")
        if payload.snapshot.canonical_hash() != self.source_snapshot_hash:
            raise PilotJournalError(
                "pilot control event cannot change scientific state"
            )
        if (
            hash_json(payload.registry.model_dump(mode="json"))
            != self.source_registry_hash
        ):
            raise PilotJournalError("pilot control event cannot change the ID registry")
        validate_pilot_control_artifact_binding(
            self.prepared.stage.artifact,
            self.prepared.control_artifact,
            snapshot=payload.snapshot,
        )
        if self.precommit_validator is not None:
            try:
                self.precommit_validator(
                    self.prepared.stage.artifact,
                    self.prepared.control_artifact,
                    RepositorySnapshot.model_validate(
                        payload.snapshot.model_dump(mode="json")
                    ),
                )
            except Exception as exc:
                raise PilotJournalError(
                    "pilot prospective control event failed pre-commit validation"
                ) from exc
        head_path, head_content = _current_head_content(
            registration=self.prepared.stage.artifact.registration,
            run_manifest=self.prepared.stage.artifact.run_manifest,
            head=self.prepared.stage.reference,
        )
        writer.write_bytes(
            self.prepared.control_relative_path,
            self.prepared.control_content,
        )
        writer.write_bytes(
            self.prepared.stage.reference.relative_path,
            self.prepared.stage.content,
        )
        writer.write_bytes(head_path, head_content)


def _control_result(
    prepared: PreparedPilotControlEvent,
    *,
    generation: int,
    commit_performed: bool,
) -> PilotControlEventResult:
    return PilotControlEventResult(
        generation=generation,
        commit_performed=commit_performed,
        reused_generation=(
            None if commit_performed else prepared.stage.reference.owner_generation
        ),
        control_artifact_relative_path=prepared.control_relative_path,
        control_artifact_hash=prepared.control_hash,
        reference=prepared.stage.reference,
    )


def _find_exact_control_event(
    project_root: Path,
    prepared: PreparedPilotControlEvent,
) -> int | None:
    """Return CURRENT when the deterministic owner generation is exact."""

    store = GenerationStore(project_root)
    owner_generation = prepared.stage.reference.owner_generation
    with store.writer_lock():
        current_generation = store.current_generation()
        if current_generation < owner_generation:
            return None
        try:
            store.load_generation_auxiliary(owner_generation, set())
        except FileNotFoundError as exc:
            raise PilotJournalError(
                "pilot control owner generation is absent"
            ) from exc
        candidate = (
            store.generation_path(owner_generation)
            / "auxiliary"
            / prepared.stage.reference.relative_path
        )
        try:
            candidate.lstat()
        except FileNotFoundError:
            return None
        loaded = load_pilot_stage_artifact(
            project_root,
            prepared.stage.reference,
            expected_registration=prepared.stage.artifact.registration,
            expected_run_manifest_hash=prepared.stage.artifact.run_manifest_hash,
        )
        if loaded != prepared.stage.artifact:
            raise PilotJournalError("stored pilot control event differs from request")
        _, _, control_files = store.load_generation_auxiliary(
            owner_generation,
            {prepared.control_relative_path},
        )
        if control_files[prepared.control_relative_path] != prepared.control_content:
            raise PilotJournalError(
                "stored pilot control artifact differs from request"
            )
        if store.current_generation() != current_generation:
            raise PilotJournalError(
                "CURRENT changed while verifying pilot control reuse"
            )
        return current_generation


def record_pilot_control_event(
    project_root: Path,
    registration: PilotRunRegistrationReference,
    run_manifest: PilotRunManifest,
    *,
    ordinal: int,
    stage: PilotStage,
    status: PilotStageStatus,
    artifact_payload: Mapping[str, JsonValue],
    input_hashes: Mapping[str, Sha256] | None = None,
    budget_consumed: Mapping[str, int] | None = None,
    previous_stage: PilotStageArtifactReference | None = None,
    reason: str | None = None,
    precommit_validator: PilotPrecommitValidator | None = None,
    crash_at: CrashPoint | None = None,
) -> PilotControlEventResult:
    """Atomically commit or exactly reuse one taskless pilot event."""

    try:
        exact_registration = PilotRunRegistrationReference.model_validate(
            registration.model_dump(mode="json")
        )
        exact_manifest = PilotRunManifest.model_validate(
            run_manifest.model_dump(mode="json")
        )
        exact_stage = PilotStage(stage)
        exact_status = PilotStageStatus(status)
    except Exception as exc:
        raise PilotJournalError("pilot control event identity is invalid") from exc
    registration_artifact, registered_manifest = load_pilot_run_registration(
        project_root,
        exact_registration,
        expected_manifest=exact_manifest,
    )
    if registered_manifest != exact_manifest:
        raise PilotJournalError("pilot control event names another run manifest")

    if ordinal < 1 or ordinal > MAX_STAGE_CHAIN_EVENTS:
        raise PilotJournalError("pilot control ordinal is outside its finite budget")
    if (ordinal == 1) != (previous_stage is None):
        raise PilotJournalError(
            "only the first pilot control event may omit its exact predecessor"
        )

    exact_previous: PilotStageArtifactReference | None = None
    previous_artifact: PilotStageArtifact | None = None
    if previous_stage is None:
        source_generation = registration_artifact.owner_generation
    else:
        exact_previous = PilotStageArtifactReference.model_validate(
            previous_stage.model_dump(mode="json")
        )
        previous_artifact = load_pilot_stage_artifact(
            project_root,
            exact_previous,
            expected_registration=exact_registration,
            expected_run_manifest_hash=exact_manifest.manifest_hash,
        )
        if previous_artifact.stage_record.ordinal + 1 != ordinal:
            raise PilotJournalError("pilot control predecessor is not consecutive")
        source_generation = exact_previous.owner_generation

    normalized_inputs = dict(input_hashes or {})
    reserved_inputs = {
        "pilot_run_manifest": exact_manifest.manifest_hash,
        "pilot_run_registration": exact_registration.artifact_hash,
    }
    for name, expected_hash in reserved_inputs.items():
        observed = normalized_inputs.get(name)
        if observed not in {None, expected_hash}:
            raise PilotJournalError(f"input hash {name} differs from registered setup")
        normalized_inputs[name] = expected_hash
    normalized_inputs = dict(sorted(normalized_inputs.items()))

    normalized_budget = dict(sorted((budget_consumed or {}).items()))
    if previous_artifact is not None:
        for name, previous_value in (
            previous_artifact.stage_record.budget_consumed.items()
        ):
            if normalized_budget.get(name, 0) < previous_value:
                raise PilotJournalError(
                    f"pilot cumulative budget counter regressed: {name}"
                )

    prepared = _prepare_pilot_control_event(
        registration=exact_registration,
        run_manifest=exact_manifest,
        ordinal=ordinal,
        stage=exact_stage,
        status=exact_status,
        source_generation=source_generation,
        input_hashes=normalized_inputs,
        budget_consumed=normalized_budget,
        previous_stage=exact_previous,
        reason=reason,
        artifact_payload=artifact_payload,
    )

    store = GenerationStore(project_root)
    current_generation = store.current_generation()
    if current_generation != source_generation:
        reused_generation = _find_exact_control_event(project_root, prepared)
        if reused_generation is not None:
            return _control_result(
                prepared,
                generation=reused_generation,
                commit_performed=False,
            )
        raise StaleSnapshotError(
            f"pilot control source generation {source_generation} is stale; "
            f"current is {current_generation}"
        )

    source_snapshot, source_registry = store.load_generation(source_generation)
    source_snapshot_hash = source_snapshot.canonical_hash()
    source_registry_hash = hash_json(source_registry.model_dump(mode="json"))
    setup_identifiers = [
        *(f"Paper:{item.paper_id}" for item in source_snapshot.papers),
        f"CorpusFact:{registration_artifact.corpus_fact_id}",
        f"ReviewProcessFact:{registration_artifact.scope_process_fact_id}",
        (
            "ReviewProcessFact:"
            f"{registration_artifact.human_review_process_fact_id}"
        ),
    ]
    dependencies = store.dependency_hashes(source_generation, setup_identifiers)
    materializer = PilotControlEventMaterializer(
        prepared=prepared,
        source_snapshot_hash=source_snapshot_hash,
        source_registry_hash=source_registry_hash,
        precommit_validator=precommit_validator,
    )

    def promote(snapshot, registry):
        if snapshot.canonical_hash() != source_snapshot_hash:
            raise PilotJournalError("pilot control source snapshot changed")
        if hash_json(registry.model_dump(mode="json")) != source_registry_hash:
            raise PilotJournalError("pilot control source registry changed")
        return PromotionPayload(snapshot, registry, {})

    try:
        commit = store.commit(
            base_generation=source_generation,
            dependencies=dependencies,
            promotion=promote,
            staging_materializer=materializer,
            crash_at=crash_at,
        )
    except StaleSnapshotError:
        reused_generation = _find_exact_control_event(project_root, prepared)
        if reused_generation is not None:
            return _control_result(
                prepared,
                generation=reused_generation,
                commit_performed=False,
            )
        raise
    except BaseException:
        store.recover()
        raise

    observed_generation = _find_exact_control_event(project_root, prepared)
    if observed_generation != commit.generation:
        raise PilotJournalError("committed pilot control event could not be verified")
    return _control_result(
        prepared,
        generation=commit.generation,
        commit_performed=True,
    )


class PilotStageMaterializer:
    """Receipt-aware materializer for one completed semantic-task event."""

    def __init__(
        self,
        *,
        project_root: Path,
        registration: PilotRunRegistrationReference,
        run_manifest: PilotRunManifest,
        task_provenance: TaskProvenance,
        engine_plan_hash: Sha256,
        ordinal: int,
        stage: PilotStage,
        input_hashes: Mapping[str, Sha256],
        artifact_hashes: Mapping[str, Sha256] | None = None,
        artifact_hash_provider: (
            Callable[[AppliedTaskReceipt], Mapping[str, Sha256]] | None
        ) = None,
        budget_consumed: Mapping[str, int] | None = None,
        previous_stage: PilotStageArtifactReference | None = None,
        inner: CoupledStagingMaterializer | None = None,
        precommit_validator: PilotPrecommitValidator | None = None,
    ) -> None:
        if ordinal < 1 or ordinal > MAX_STAGE_CHAIN_EVENTS:
            raise PilotJournalError("pilot stage ordinal is outside its finite budget")
        if artifact_hashes is not None and artifact_hash_provider is not None:
            raise PilotJournalError(
                "static and dynamic pilot artifact hashes are mutually exclusive"
            )
        _, registered_manifest = load_pilot_run_registration(
            project_root, registration, expected_manifest=run_manifest
        )
        if registered_manifest != run_manifest:
            raise PilotJournalError("pilot stage names another registered manifest")
        if (ordinal == 1) != (previous_stage is None):
            raise PilotJournalError(
                "only the first pilot event may omit its exact predecessor"
            )
        previous_budget: Mapping[str, int] = {}
        if previous_stage is not None:
            previous = load_pilot_stage_artifact(
                project_root,
                previous_stage,
                expected_registration=registration,
                expected_run_manifest_hash=run_manifest.manifest_hash,
            )
            if previous.stage_record.ordinal + 1 != ordinal:
                raise PilotJournalError("pilot predecessor ordinal is not consecutive")
            previous_budget = previous.stage_record.budget_consumed
        normalized_inputs = dict(sorted(input_hashes.items()))
        existing_manifest_hash = normalized_inputs.get("pilot_run_manifest")
        if existing_manifest_hash not in {None, run_manifest.manifest_hash}:
            raise PilotJournalError("input hashes name another run manifest")
        normalized_inputs["pilot_run_manifest"] = run_manifest.manifest_hash
        normalized_budget = dict(sorted((budget_consumed or {}).items()))
        for name, previous_value in previous_budget.items():
            if normalized_budget.get(name, 0) < previous_value:
                raise PilotJournalError(
                    f"pilot cumulative budget counter regressed: {name}"
                )
        self.registration = registration
        self.run_manifest = run_manifest
        private_provenance = TaskProvenance.model_validate(
            task_provenance.model_dump(mode="json")
        )
        self.task_provenance = PilotTaskProvenanceWitness.from_private(
            private_provenance
        )
        self.engine_plan_hash = engine_plan_hash
        self.ordinal = ordinal
        self.stage = stage
        self.input_hashes = normalized_inputs
        self.artifact_hashes = dict(sorted((artifact_hashes or {}).items()))
        self.artifact_hash_provider = artifact_hash_provider
        self.budget_consumed = normalized_budget
        self.previous_stage = previous_stage
        self.inner = inner
        self.precommit_validator = precommit_validator

    def _artifact_hashes_for(
        self, receipt: AppliedTaskReceipt
    ) -> dict[str, Sha256]:
        if self.artifact_hash_provider is None:
            return dict(self.artifact_hashes)
        try:
            return dict(sorted(self.artifact_hash_provider(receipt).items()))
        except Exception as exc:
            raise PilotJournalError(
                "dynamic pilot artifact hashes could not be resolved"
            ) from exc

    def stage_record_for(
        self,
        receipt: AppliedTaskReceipt,
        *,
        artifact_hashes: Mapping[str, Sha256] | None = None,
    ) -> PilotStageRecord:
        task_id = receipt.accepted_attempt_id.rsplit("/", 1)[0]
        resolved_artifacts = (
            self._artifact_hashes_for(receipt)
            if artifact_hashes is None
            else dict(sorted(artifact_hashes.items()))
        )
        return PilotStageRecord(
            run_id=self.run_manifest.run_id,
            ordinal=self.ordinal,
            stage=self.stage,
            status=PilotStageStatus.COMPLETED,
            source_generation=receipt.source_generation,
            committed_generation=receipt.committed_generation,
            input_hashes=self.input_hashes,
            task_ids=(task_id,),
            artifact_hashes=resolved_artifacts,
            budget_consumed=self.budget_consumed,
            previous_stage=(
                self.previous_stage.predecessor()
                if self.previous_stage is not None
                else None
            ),
        )

    def prepared_for(
        self,
        receipt: AppliedTaskReceipt,
        *,
        artifact_hashes: Mapping[str, Sha256] | None = None,
    ) -> PreparedPilotStageArtifact:
        return build_pilot_stage_artifact(
            registration=self.registration,
            run_manifest=self.run_manifest,
            stage_record=self.stage_record_for(
                receipt, artifact_hashes=artifact_hashes
            ),
            task_provenance=self.task_provenance,
            accepted_receipt=receipt,
            engine_plan_hash=self.engine_plan_hash,
        )

    def __call__(
        self,
        writer: AuxiliaryStagingWriter,
        next_generation: int,
        payload: PromotionPayload,
        receipt: AppliedTaskReceipt,
    ) -> None:
        if (
            receipt.committed_generation != next_generation
            or receipt.source_generation + 1 != next_generation
            or receipt.source_generation != self.task_provenance.base_generation
        ):
            raise PilotJournalError(
                "pilot stage receipt does not own the staged generation"
            )
        if self.previous_stage is None:
            if receipt.source_generation != self.registration.owner_generation:
                raise PilotJournalError("first pilot event does not follow setup")
        elif receipt.source_generation != self.previous_stage.owner_generation:
            raise PilotJournalError(
                "pilot event does not immediately follow predecessor"
            )
        try:
            _validate_receipt_integrity(
                receipt,
                self.task_provenance,
                payload.snapshot,
                engine_plan_hash=self.engine_plan_hash,
            )
        except Exception as exc:
            raise PilotJournalError(
                "pilot stage receipt failed pre-commit validation"
            ) from exc

        staged_domain_hashes: dict[str, Sha256] = {}

        class _ObservedAuxiliaryWriter:
            """Record the exact domain bytes written by the journal wrapper."""

            __slots__ = ("_delegate",)

            def __init__(self, delegate: AuxiliaryStagingWriter) -> None:
                self._delegate = delegate

            def write_bytes(self, relative_path: str, content: bytes) -> None:
                self._delegate.write_bytes(relative_path, content)
                staged_domain_hashes[relative_path] = hash_bytes(content)

        observed_writer = _ObservedAuxiliaryWriter(writer)
        if self.inner is not None:
            self.inner(observed_writer, next_generation, payload, receipt)  # type: ignore[arg-type]
        resolved_artifacts = self._artifact_hashes_for(receipt)
        if staged_domain_hashes != resolved_artifacts:
            raise PilotJournalError(
                "pilot stage domain artifact declarations differ from staged bytes"
            )
        prepared = self.prepared_for(
            receipt, artifact_hashes=resolved_artifacts
        )
        if self.precommit_validator is not None:
            try:
                self.precommit_validator(
                    prepared.artifact,
                    None,
                    RepositorySnapshot.model_validate(
                        payload.snapshot.model_dump(mode="json")
                    ),
                )
            except Exception as exc:
                raise PilotJournalError(
                    "pilot prospective semantic event failed pre-commit validation"
                ) from exc
        head_path, head_content = _current_head_content(
            registration=self.registration,
            run_manifest=self.run_manifest,
            head=prepared.reference,
        )
        writer.write_bytes(prepared.reference.relative_path, prepared.content)
        writer.write_bytes(head_path, head_content)


def validate_pilot_control_artifact_binding(
    stage_artifact: PilotStageArtifact,
    control: PilotControlArtifact,
    *,
    snapshot: RepositorySnapshot | None = None,
) -> PilotCompletedControlPayload | None:
    """Purely bind one authenticated taskless stage to its typed control."""

    record = stage_artifact.stage_record
    if stage_artifact.accepted_receipt is not None:
        raise PilotJournalError("semantic pilot event cannot carry a control artifact")
    content = _canonical_model_bytes(control)
    control_hash = hash_bytes(content)
    expected_path = _control_relative_path(
        record.run_id,
        record.ordinal,
        record.stage,
        record.status,
        control_hash,
    )
    if record.artifact_hashes != {expected_path: control_hash}:
        raise PilotJournalError(
            "taskless pilot event does not bind its exact control artifact"
        )
    if not (
        control.registration == stage_artifact.registration
        and control.run_manifest_hash == stage_artifact.run_manifest_hash
        and control.run_id == record.run_id
        and control.ordinal == record.ordinal
        and control.stage is record.stage
        and control.status is record.status
        and control.source_generation == record.source_generation
        and control.committed_generation == record.committed_generation
        and control.input_hashes == record.input_hashes
        and control.budget_consumed == record.budget_consumed
        and control.previous_stage == record.previous_stage
        and control.reason == record.reason
    ):
        raise PilotJournalError("pilot control artifact and stage record differ")

    parsed: PilotCompletedControlPayload | None = None
    if record.status is PilotStageStatus.COMPLETED:
        parsed = validate_completed_pilot_control_payload(
            record.stage, control.payload
        )
    if isinstance(parsed, PilotRetrievalControlPayload):
        if parsed.corpus_lock_hash != stage_artifact.run_manifest.corpus_lock_hash:
            raise PilotJournalError(
                "retrieval control corpus lock differs from the run manifest"
            )
        expected_budget = {
            "queries": parsed.query_count,
            "raw_candidates": parsed.total_candidates,
        }
        for name, expected in expected_budget.items():
            if record.budget_consumed.get(name) != expected:
                raise PilotJournalError(
                    f"retrieval control budget counter differs: {name}"
                )
        if snapshot is not None:
            queries = {
                item.query_id: item for item in snapshot.retrieval_queries
            }
            ledgers = parsed.parsed_ledgers()
            if set(queries) != {item.query_id for item in ledgers}:
                raise PilotJournalError(
                    "retrieval control does not cover the exact canonical query set"
                )
            for ledger in ledgers:
                query = queries[ledger.query_id]
                if hash_bytes(query.query_text.encode("utf-8")) != (
                    ledger.query_text_hash
                ):
                    raise PilotJournalError(
                        "retrieval control query text differs from canonical state"
                    )
    elif isinstance(parsed, PilotExactAssemblyControlPayload):
        if parsed.source_generation != record.committed_generation:
            raise PilotJournalError(
                "exact assembly does not name its no-op owner generation"
            )
        if snapshot is not None:
            if parsed.repository_hash != snapshot.canonical_hash():
                raise PilotJournalError(
                    "exact assembly repository hash differs from canonical state"
                )
            try:
                verify_exact_assembly(
                    snapshot,
                    parsed.section_body_utf8.encode("utf-8"),
                    parsed.assembly,
                )
            except Exception as exc:
                raise PilotJournalError(
                    "exact assembly control does not verify against canonical state"
                ) from exc
    elif isinstance(parsed, PilotValidationControlPayload):
        if (
            parsed.source_generation != record.source_generation
            or parsed.report.run_id != stage_artifact.run_manifest.run_id
        ):
            raise PilotJournalError(
                "validation control does not name its run and validated generation"
            )
        if snapshot is not None and parsed.repository_hash != snapshot.canonical_hash():
            raise PilotJournalError(
                "validation report repository hash differs from canonical state"
            )
    return parsed


def _validate_taskless_control_artifact(
    stage_artifact: PilotStageArtifact,
    domain_files: Mapping[str, bytes],
    *,
    snapshot: RepositorySnapshot | None = None,
) -> PilotControlArtifact:
    record = stage_artifact.stage_record
    if len(record.artifact_hashes) != 1:
        raise PilotJournalError(
            "taskless pilot event must own exactly one control artifact"
        )
    relative_path, expected_hash = next(iter(record.artifact_hashes.items()))
    content = domain_files[relative_path]
    if len(content) > MAX_STAGE_ARTIFACT_BYTES:
        raise PilotJournalError("pilot control artifact exceeds its byte limit")
    if hash_bytes(content) != expected_hash:
        raise PilotJournalError("pilot control artifact hash mismatch")
    expected_path = _control_relative_path(
        record.run_id,
        record.ordinal,
        record.stage,
        record.status,
        expected_hash,
    )
    if relative_path != expected_path:
        raise PilotJournalError("pilot control artifact path is not canonical")
    try:
        control = PilotControlArtifact.model_validate_json(content)
    except Exception as exc:
        raise PilotJournalError("pilot control artifact is invalid") from exc
    if canonical_json_bytes(control.model_dump(mode="json")) + b"\n" != content:
        raise PilotJournalError("pilot control artifact bytes are not canonical")
    validate_pilot_control_artifact_binding(
        stage_artifact, control, snapshot=snapshot
    )
    return control


def load_pilot_control_artifact(
    project_root: Path,
    stage_artifact: PilotStageArtifact,
) -> PilotControlArtifact:
    """Load the exact generation-owned control for an authenticated stage."""

    if stage_artifact.accepted_receipt is not None:
        raise PilotJournalError("semantic pilot event has no control artifact")
    record = stage_artifact.stage_record
    try:
        snapshot, _, domain_files = GenerationStore(
            project_root
        ).load_generation_auxiliary(
            record.committed_generation, set(record.artifact_hashes)
        )
        return _validate_taskless_control_artifact(
            stage_artifact, domain_files, snapshot=snapshot
        )
    except PilotJournalError:
        raise
    except Exception as exc:
        raise PilotJournalError(
            "pilot control artifact could not be authenticated"
        ) from exc


def _load_single_stage_impl(
    project_root: Path,
    reference: PilotStageArtifactReference,
    *,
    expected_registration: PilotRunRegistrationReference | None,
    expected_run_manifest_hash: Sha256 | None,
) -> PilotStageArtifact:
    store = GenerationStore(project_root)
    snapshot, _, auxiliary = store.load_generation_auxiliary(
        reference.owner_generation,
        {reference.relative_path},
    )
    content = auxiliary[reference.relative_path]
    if len(content) > MAX_STAGE_ARTIFACT_BYTES:
        raise PilotJournalError("pilot stage artifact exceeds its byte limit")
    if hash_bytes(content) != reference.artifact_hash:
        raise PilotJournalError("pilot stage artifact content hash mismatch")
    try:
        artifact = PilotStageArtifact.model_validate_json(content)
    except Exception as exc:
        raise PilotJournalError("pilot stage artifact is invalid") from exc
    if canonical_json_bytes(artifact.model_dump(mode="json")) + b"\n" != content:
        raise PilotJournalError("pilot stage artifact bytes are not canonical")
    record = artifact.stage_record
    if (
        artifact.run_manifest.run_id != reference.run_id
        or record.ordinal != reference.ordinal
        or record.stage is not reference.stage
        or record.committed_generation != reference.owner_generation
        or artifact.stage_record_hash != reference.stage_record_hash
    ):
        raise PilotJournalError("pilot stage reference and artifact differ")
    if (
        expected_registration is not None
        and artifact.registration != expected_registration
    ):
        raise PilotJournalError("pilot stage belongs to another registration")
    if (
        expected_run_manifest_hash is not None
        and artifact.run_manifest_hash != expected_run_manifest_hash
    ):
        raise PilotJournalError("pilot stage belongs to another run manifest")
    _, registered_manifest = load_pilot_run_registration(
        project_root,
        artifact.registration,
        expected_manifest=artifact.run_manifest,
    )
    if registered_manifest.manifest_hash != artifact.run_manifest_hash:
        raise PilotJournalError("pilot stage is not bound to registered setup")

    if artifact.accepted_receipt is not None:
        if artifact.task_provenance is None:  # pragma: no cover - model invariant
            raise PilotJournalError("pilot task provenance is absent")
        _validate_receipt_integrity(
            artifact.accepted_receipt,
            artifact.task_provenance,
            snapshot,
            engine_plan_hash=artifact.engine_plan_hash,
        )
        matching_receipts = tuple(
            receipt
            for receipt in store.load_receipts(reference.owner_generation)
            if receipt.semantic_task_key
            == artifact.accepted_receipt.semantic_task_key
        )
        if matching_receipts != (artifact.accepted_receipt,):
            raise PilotJournalError(
                "pilot stage accepted receipt is absent or ambiguous in its generation"
            )
    if record.artifact_hashes:
        _, _, domain_files = store.load_generation_auxiliary(
            reference.owner_generation, set(record.artifact_hashes)
        )
        for path, expected_hash in record.artifact_hashes.items():
            if hash_bytes(domain_files[path]) != expected_hash:
                raise PilotJournalError(
                    f"pilot stage domain artifact hash mismatch: {path}"
                )
        if artifact.accepted_receipt is None:
            _validate_taskless_control_artifact(
                artifact, domain_files, snapshot=snapshot
            )
    return artifact


def _load_single_stage(
    project_root: Path,
    reference: PilotStageArtifactReference,
    *,
    expected_registration: PilotRunRegistrationReference | None,
    expected_run_manifest_hash: Sha256 | None,
) -> PilotStageArtifact:
    try:
        return _load_single_stage_impl(
            project_root,
            reference,
            expected_registration=expected_registration,
            expected_run_manifest_hash=expected_run_manifest_hash,
        )
    except PilotJournalError:
        raise
    except Exception as exc:
        raise PilotJournalError(str(exc)) from exc


def load_pilot_stage_artifact(
    project_root: Path,
    reference: PilotStageArtifactReference,
    *,
    expected_registration: PilotRunRegistrationReference | None = None,
    expected_run_manifest_hash: Sha256 | None = None,
    expected_previous_stage: PilotStageArtifactReference | None = None,
) -> PilotStageArtifact:
    """Load one event and prove its entire chain reaches registered setup."""

    exact_reference = PilotStageArtifactReference.model_validate(
        reference.model_dump(mode="json")
    )
    head = _load_single_stage(
        project_root,
        exact_reference,
        expected_registration=expected_registration,
        expected_run_manifest_hash=expected_run_manifest_hash,
    )
    if expected_previous_stage is not None:
        observed = head.stage_record.previous_stage
        if observed != expected_previous_stage.predecessor():
            raise PilotJournalError("pilot stage predecessor differs from expectation")

    current_reference = exact_reference
    current = head
    seen: set[tuple[int, Sha256]] = set()
    while current.stage_record.previous_stage is not None:
        key = (current_reference.owner_generation, current_reference.artifact_hash)
        if key in seen or len(seen) >= MAX_STAGE_CHAIN_EVENTS:
            raise PilotJournalError("pilot stage chain is cyclic or over budget")
        seen.add(key)
        previous_reference = _reference_from_predecessor(
            current.stage_record.previous_stage
        )
        previous = _load_single_stage(
            project_root,
            previous_reference,
            expected_registration=head.registration,
            expected_run_manifest_hash=head.run_manifest_hash,
        )
        if (
            previous_reference.ordinal + 1 != current_reference.ordinal
            or previous_reference.owner_generation
            != current.stage_record.source_generation
        ):
            raise PilotJournalError("pilot stage chain is not consecutive")
        for name, previous_value in previous.stage_record.budget_consumed.items():
            if current.stage_record.budget_consumed.get(name, 0) < previous_value:
                raise PilotJournalError(
                    f"pilot cumulative budget counter regressed: {name}"
                )
        current_reference = previous_reference
        current = previous
    if current_reference.ordinal != 1:
        raise PilotJournalError("pilot stage chain does not reach its first event")
    if current.stage_record.source_generation != head.registration.owner_generation:
        raise PilotJournalError("pilot stage chain does not reach registered setup")
    return head


def discover_current_pilot_head(
    project_root: Path,
    registration: PilotRunRegistrationReference,
    expected_manifest: PilotRunManifest,
) -> PilotStageArtifactReference | None:
    """Discover and fully authenticate the fixed journal head stored by CURRENT.

    The registered setup generation predates journal events and therefore has no
    index.  Every later CURRENT generation must contain exactly the canonical
    fixed-path index for its generation; absence, drift, or any chain/sequence
    failure is an error rather than an empty result.
    """

    try:
        exact_registration = PilotRunRegistrationReference.model_validate(
            registration.model_dump(mode="json")
        )
        exact_manifest = PilotRunManifest.model_validate(
            expected_manifest.model_dump(mode="json")
        )
        _, registered_manifest = load_pilot_run_registration(
            project_root,
            exact_registration,
            expected_manifest=exact_manifest,
        )
        if registered_manifest != exact_manifest:
            raise PilotJournalError(
                "pilot CURRENT-head manifest differs from registration"
            )
        store = GenerationStore(project_root)
        current_generation = store.current_generation()
        if current_generation < exact_registration.owner_generation:
            raise PilotJournalError(
                "CURRENT predates the registered pilot setup generation"
            )
        if current_generation == exact_registration.owner_generation:
            return None

        relative_path = _current_head_relative_path(exact_manifest.run_id)
        _, _, selected = store.load_generation_auxiliary(
            current_generation, {relative_path}
        )
        content = selected[relative_path]
        if len(content) > MAX_STAGE_ARTIFACT_BYTES:
            raise PilotJournalError("pilot CURRENT-head index exceeds its byte limit")
        index = PilotCurrentHeadIndex.model_validate_json(content)
        if _canonical_model_bytes(index) != content:
            raise PilotJournalError("pilot CURRENT-head index bytes are not canonical")
        if not (
            index.registration == exact_registration
            and index.run_manifest_hash == exact_manifest.manifest_hash
            and index.run_id == exact_manifest.run_id
            and index.owner_generation == current_generation
        ):
            raise PilotJournalError(
                "pilot CURRENT-head index belongs to another run or generation"
            )
        head = PilotStageArtifactReference.model_validate(
            index.head.model_dump(mode="json")
        )
        artifact = load_pilot_stage_artifact(
            project_root,
            head,
            expected_registration=exact_registration,
            expected_run_manifest_hash=exact_manifest.manifest_hash,
        )
        if artifact.stage_record.committed_generation != current_generation:
            raise PilotJournalError("pilot CURRENT-head event is not owned by CURRENT")

        # Lazy import avoids a module cycle: sequence validation itself uses the
        # journal's authenticated loaders.
        from .pilot_sequence import validate_fixed_pilot_sequence

        summary = validate_fixed_pilot_sequence(project_root, head)
        if summary.head != head or summary.event_count != index.event_count:
            raise PilotJournalError(
                "pilot CURRENT-head index differs from its authentic sequence"
            )
        return head
    except PilotJournalError:
        raise
    except Exception as exc:
        raise PilotJournalError(
            "pilot CURRENT-head index could not be authenticated"
        ) from exc


__all__ = [
    "PILOT_CURRENT_HEAD_FILENAME",
    "PILOT_JOURNAL_ROOT",
    "PILOT_JOURNAL_VERSION",
    "PilotControlArtifact",
    "PilotControlEventMaterializer",
    "PilotControlEventResult",
    "PilotCurrentHeadIndex",
    "PilotExactAssemblyControlPayload",
    "PilotJournalError",
    "PilotPrecommitValidator",
    "PilotRetrievalControlPayload",
    "PilotResourceProvenanceWitness",
    "PilotResourceSourceDependencyWitness",
    "PilotStageArtifact",
    "PilotStageArtifactReference",
    "PilotStageMaterializer",
    "PilotTaskProvenanceWitness",
    "PilotValidationControlPayload",
    "PilotValidationDiagnostics",
    "PreparedPilotControlEvent",
    "PreparedPilotStageArtifact",
    "build_pilot_stage_artifact",
    "discover_current_pilot_head",
    "load_pilot_control_artifact",
    "load_pilot_run_registration",
    "load_pilot_stage_artifact",
    "record_pilot_control_event",
    "validate_completed_pilot_control_payload",
    "validate_pilot_control_artifact_binding",
]
