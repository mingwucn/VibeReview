"""Bounded, provider-neutral records for the fixed synthetic Package C pilot.

These records describe runtime execution and validation evidence.  They are
deliberately separate from the frozen scientific models and never make a
publication claim.  Live engines, operator corpora, and human sign-off remain
outside this synthetic implementation boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from vibereview.ids import Sha256

from .hashing import hash_json
from .records import RuntimeModel, TaskType


PILOT_RUN_ID_PATTERN = r"^RUN-[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
PILOT_TASK_TYPES = frozenset(TaskType)
PILOT_CORPUS_FACT_TEXT = (
    "The synthetic pilot corpus contains exactly five selected papers."
)
PILOT_SCOPE_FACT_TEXT = (
    "This run is a bounded five-paper pilot and is not an exhaustive review."
)
PILOT_HUMAN_REVIEW_FACT_TEXT = (
    "No authorized human scientific review or publication sign-off was "
    "performed for this synthetic run."
)


class PilotRecordModel(RuntimeModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PilotStage(StrEnum):
    PREREQUISITES = "prerequisites"
    DISCOVERY = "discovery"
    CORPUS_CHALLENGE = "corpus_challenge"
    QUERY_GENERATION = "query_generation"
    RETRIEVAL = "retrieval"
    EVIDENCE_ASSESSMENT = "evidence_assessment"
    CLAIM_AGGREGATION = "claim_aggregation"
    CLAIM_VALIDATION = "claim_validation"
    PROPOSITION_AUDIT = "proposition_audit"
    SENTENCE_AUDIT = "sentence_audit"
    EXACT_ASSEMBLY = "exact_assembly"
    VALIDATION_REPORT = "validation_report"


class PilotStageStatus(StrEnum):
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class ValidationStatus(StrEnum):
    NOT_EXECUTED = "NOT_EXECUTED"
    PASSED = "PASSED"
    FAILED = "FAILED"


class HumanReviewStatus(StrEnum):
    NOT_PERFORMED = "NOT_PERFORMED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class FivePaperPilotBudget(PilotRecordModel):
    """Finite engineering ceilings; none is an evidence-quality threshold."""

    paper_count: Literal[5] = 5
    min_discovery_documents: Literal[2] = 2
    max_discovery_documents: Literal[3] = 3
    max_discovery_document_bytes: int = Field(default=192 * 1024, ge=1, le=512 * 1024)
    max_discovery_total_bytes: int = Field(default=512 * 1024, ge=1, le=2 * 1024 * 1024)
    max_paper_bytes: int = Field(default=2 * 1024 * 1024, ge=1, le=8 * 1024 * 1024)
    max_corpus_bytes: int = Field(default=8 * 1024 * 1024, ge=1, le=32 * 1024 * 1024)
    max_themes: int = Field(default=10, ge=1, le=100)
    max_discovery_leads: int = Field(default=20, ge=1, le=200)
    max_claims: int = Field(default=5, ge=1, le=50)
    required_queries_per_claim: Literal[4] = 4
    max_queries: int = Field(default=20, ge=4, le=200)
    retrieval_top_k_per_backend: int = Field(default=4, ge=1, le=100)
    max_raw_candidates: int = Field(default=160, ge=1, le=1_000)
    max_assessed_candidates: int = Field(default=60, ge=1, le=500)
    max_evidence_records: int = Field(default=60, ge=1, le=500)
    max_claim_paper_evidence: int = Field(default=25, ge=1, le=250)
    max_initial_propositions: int = Field(default=12, ge=1, le=100)
    max_proposition_repairs: int = Field(default=6, ge=0, le=100)
    max_initial_sentences: int = Field(default=18, ge=1, le=200)
    max_sentence_repairs: int = Field(default=6, ge=0, le=100)
    max_semantic_engine_invocations: int = Field(default=128, ge=1, le=128)
    max_task_seconds: int = Field(default=180, ge=1, le=3_600)
    max_run_seconds: int = Field(default=4 * 60 * 60, ge=1, le=24 * 60 * 60)
    max_request_bytes: int = Field(default=512 * 1024, ge=1, le=2 * 1024 * 1024)
    max_proposal_bytes: int = Field(default=1024 * 1024, ge=1, le=4 * 1024 * 1024)
    max_stdout_bytes_per_attempt: int = Field(
        default=64 * 1024, ge=1, le=1024 * 1024
    )
    max_stderr_bytes_per_attempt: int = Field(
        default=64 * 1024, ge=1, le=1024 * 1024
    )
    max_retained_diagnostic_bytes_per_attempt: int = Field(
        default=128 * 1024, ge=1, le=4 * 1024 * 1024
    )
    max_writable_entries_per_attempt: int = Field(default=256, ge=1, le=4_096)
    max_writable_tree_bytes_per_attempt: int = Field(
        default=16 * 1024 * 1024, ge=1, le=256 * 1024 * 1024
    )
    max_cumulative_stdout_bytes: int = Field(
        default=8 * 1024 * 1024, ge=1, le=128 * 1024 * 1024
    )
    max_cumulative_stderr_bytes: int = Field(
        default=8 * 1024 * 1024, ge=1, le=128 * 1024 * 1024
    )
    max_cumulative_retained_diagnostic_bytes: int = Field(
        default=16 * 1024 * 1024, ge=1, le=512 * 1024 * 1024
    )
    max_cumulative_writable_entries: int = Field(
        default=8_192, ge=1, le=262_144
    )
    max_cumulative_writable_tree_bytes: int = Field(
        default=256 * 1024 * 1024, ge=1, le=8 * 1024 * 1024 * 1024
    )

    @model_validator(mode="after")
    def _cross_field_limits(self) -> "FivePaperPilotBudget":
        if self.max_discovery_total_bytes < self.max_discovery_document_bytes:
            raise ValueError("discovery total budget must cover one document")
        if self.max_corpus_bytes < self.max_paper_bytes:
            raise ValueError("corpus budget must cover one paper")
        if self.max_queries < self.max_claims * self.required_queries_per_claim:
            raise ValueError("query budget must cover every required claim intent")
        if self.max_raw_candidates < self.max_assessed_candidates:
            raise ValueError("raw-candidate budget must cover assessed candidates")
        if self.max_assessed_candidates < self.max_evidence_records:
            raise ValueError("assessed-candidate budget must cover evidence records")
        if (
            self.max_retained_diagnostic_bytes_per_attempt
            < self.max_stdout_bytes_per_attempt + self.max_stderr_bytes_per_attempt
        ):
            raise ValueError(
                "per-attempt diagnostic budget must cover stdout and stderr"
            )
        if (
            self.max_writable_tree_bytes_per_attempt
            < self.max_proposal_bytes
        ):
            raise ValueError("per-attempt writable-tree budget must cover a proposal")
        cumulative_pairs = (
            (
                self.max_cumulative_stdout_bytes,
                self.max_stdout_bytes_per_attempt,
                "stdout",
            ),
            (
                self.max_cumulative_stderr_bytes,
                self.max_stderr_bytes_per_attempt,
                "stderr",
            ),
            (
                self.max_cumulative_retained_diagnostic_bytes,
                self.max_retained_diagnostic_bytes_per_attempt,
                "retained-diagnostic",
            ),
            (
                self.max_cumulative_writable_entries,
                self.max_writable_entries_per_attempt,
                "writable-entry",
            ),
            (
                self.max_cumulative_writable_tree_bytes,
                self.max_writable_tree_bytes_per_attempt,
                "writable-tree",
            ),
        )
        for cumulative, per_attempt, label in cumulative_pairs:
            if cumulative < per_attempt:
                raise ValueError(
                    f"cumulative {label} budget must cover one attempt"
                )
        if (
            self.max_cumulative_retained_diagnostic_bytes
            < self.max_cumulative_stdout_bytes
            + self.max_cumulative_stderr_bytes
        ):
            raise ValueError(
                "cumulative diagnostic budget must cover stdout and stderr"
            )
        return self


class PilotEngineRoleBinding(PilotRecordModel):
    """Safe, predeclared engine identity for one semantic task role."""

    engine: Annotated[str, Field(min_length=1, max_length=128)]
    engine_version: Annotated[str | None, Field(default=None, max_length=128)]
    safe_configuration_hash: Sha256
    qualification_fingerprint: Sha256 | None = None
    synthetic: Literal[True] = True


def compute_pilot_engine_role_plan_hash(
    plan: Mapping[TaskType, PilotEngineRoleBinding],
) -> Sha256:
    """Hash the exact ordered, provider-neutral role assignment."""

    return hash_json(
        {
            task_type.value: binding.model_dump(mode="json")
            for task_type, binding in sorted(
                plan.items(), key=lambda item: item[0].value
            )
        }
    )


def compute_pilot_schema_fingerprint(
    *, input_schema_hash: Sha256, proposal_schema_hash: Sha256
) -> Sha256:
    return hash_json(
        {
            "input_schema": input_schema_hash,
            "proposal_schema": proposal_schema_hash,
        }
    )


class PilotRunManifest(PilotRecordModel):
    schema_version: Literal["package-c-pilot-run-1"] = "package-c-pilot-run-1"
    run_id: Annotated[str, Field(pattern=PILOT_RUN_ID_PATTERN)]
    created_at: Annotated[str, Field(min_length=1, max_length=64)]
    topic: Annotated[str, Field(min_length=1, max_length=4_096)]
    source_generation: int = Field(ge=0)
    corpus_lock_hash: Sha256
    selection_manifest_hash: Sha256
    paper_source_hashes: Annotated[tuple[Sha256, ...], Field(min_length=5, max_length=5)]
    discovery_resource_hashes: Annotated[
        tuple[Sha256, ...], Field(min_length=2, max_length=3)
    ]
    engine_role_plan: dict[TaskType, PilotEngineRoleBinding]
    engine_role_plan_hash: Sha256
    prompt_fingerprints: dict[TaskType, Sha256]
    schema_fingerprints: dict[TaskType, Sha256]
    validator_fingerprint: Sha256
    package_c_implementation_fingerprint: Sha256
    budget: FivePaperPilotBudget
    synthetic_only: Literal[True] = True
    human_review_status: Literal[HumanReviewStatus.NOT_PERFORMED] = (
        HumanReviewStatus.NOT_PERFORMED
    )
    publication_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _closed_identity(self) -> "PilotRunManifest":
        if not self.topic.strip():
            raise ValueError("pilot topic cannot be blank")
        if len(set(self.paper_source_hashes)) != 5:
            raise ValueError("pilot requires five distinct paper source hashes")
        if len(set(self.discovery_resource_hashes)) != len(
            self.discovery_resource_hashes
        ):
            raise ValueError("discovery resource hashes must be unique")
        if set(self.engine_role_plan) != PILOT_TASK_TYPES:
            raise ValueError("pilot engine-role plan must cover every TaskType exactly")
        expected_plan_hash = compute_pilot_engine_role_plan_hash(
            self.engine_role_plan
        )
        if self.engine_role_plan_hash != expected_plan_hash:
            raise ValueError("pilot engine-role-plan hash mismatch")
        if set(self.prompt_fingerprints) != PILOT_TASK_TYPES:
            raise ValueError("pilot prompt fingerprints must cover every TaskType")
        if set(self.schema_fingerprints) != PILOT_TASK_TYPES:
            raise ValueError("pilot schema fingerprints must cover every TaskType")
        return self

    @property
    def manifest_hash(self) -> Sha256:
        return hash_json(self.model_dump(mode="json"))


class PilotRunRegistrationArtifact(PilotRecordModel):
    """Generation-owned proof that setup registered this exact run policy."""

    schema_version: Literal["package-c-pilot-registration-1"] = (
        "package-c-pilot-registration-1"
    )
    run_id: Annotated[str, Field(pattern=PILOT_RUN_ID_PATTERN)]
    source_generation: int = Field(ge=0)
    owner_generation: int = Field(ge=1)
    run_manifest_hash: Sha256
    run_manifest_content_hash: Sha256
    run_manifest_relative_path: str
    corpus_fact_id: Annotated[str, Field(pattern=r"^CF[0-9]{4,}$")]
    scope_process_fact_id: Annotated[str, Field(pattern=r"^PF[0-9]{4,}$")]
    human_review_process_fact_id: Annotated[str, Field(pattern=r"^PF[0-9]{4,}$")]

    @model_validator(mode="after")
    def _registration_lineage(self) -> "PilotRunRegistrationArtifact":
        if self.owner_generation != self.source_generation + 1:
            raise ValueError("pilot registration must own the next generation")
        expected_manifest_path = f"pilot/runs/{self.run_id}/run_manifest.json"
        if self.run_manifest_relative_path != expected_manifest_path:
            raise ValueError("pilot registration manifest path is not canonical")
        if self.scope_process_fact_id == self.human_review_process_fact_id:
            raise ValueError("pilot process facts must have distinct identifiers")
        return self


class PilotRunRegistrationReference(PilotRecordModel):
    """Content-addressed locator for a registered pilot setup."""

    run_id: Annotated[str, Field(pattern=PILOT_RUN_ID_PATTERN)]
    owner_generation: int = Field(ge=1)
    artifact_hash: Sha256
    relative_path: str

    @model_validator(mode="after")
    def _registration_path(self) -> "PilotRunRegistrationReference":
        digest = self.artifact_hash.removeprefix("sha256:")
        expected = f"pilot/runs/{self.run_id}/registration-{digest}.json"
        if self.relative_path != expected:
            raise ValueError("pilot registration path is not content addressed")
        return self


class PilotStagePredecessor(PilotRecordModel):
    """Exact previous journal event; hashes alone are not a chain."""

    run_id: Annotated[str, Field(pattern=PILOT_RUN_ID_PATTERN)]
    ordinal: int = Field(ge=1)
    stage: PilotStage
    owner_generation: int = Field(ge=1)
    stage_record_hash: Sha256
    artifact_hash: Sha256
    relative_path: str


class PilotStageRecord(PilotRecordModel):
    schema_version: Literal["package-c-stage-1"] = "package-c-stage-1"
    run_id: Annotated[str, Field(pattern=PILOT_RUN_ID_PATTERN)]
    ordinal: int = Field(ge=1)
    stage: PilotStage
    status: PilotStageStatus
    source_generation: int = Field(ge=0)
    committed_generation: int = Field(ge=1)
    input_hashes: dict[str, Sha256]
    task_ids: tuple[str, ...] = ()
    artifact_hashes: dict[str, Sha256] = Field(default_factory=dict)
    budget_consumed: dict[str, int] = Field(default_factory=dict)
    previous_stage: PilotStagePredecessor | None = None
    reason: Annotated[str | None, Field(default=None, max_length=4_096)]

    @model_validator(mode="after")
    def _status_fields(self) -> "PilotStageRecord":
        if any(value < 0 for value in self.budget_consumed.values()):
            raise ValueError("stage budget consumption cannot be negative")
        if self.committed_generation != self.source_generation + 1:
            raise ValueError("journal event must own the next generation")
        if len(self.task_ids) > 1 or len(self.task_ids) != len(set(self.task_ids)):
            raise ValueError("journal event may name at most one unique task")
        if self.status is PilotStageStatus.COMPLETED:
            if self.reason is not None:
                raise ValueError("completed stage cannot carry a failure reason")
        elif not self.reason:
            raise ValueError("blocked or failed stage requires a reason")
        if self.ordinal == 1 and self.previous_stage is not None:
            raise ValueError("first stage record cannot name a predecessor")
        if self.ordinal > 1 and self.previous_stage is None:
            raise ValueError("later stage record requires an exact predecessor")
        if self.previous_stage is not None:
            if self.previous_stage.run_id != self.run_id:
                raise ValueError("journal predecessor belongs to another run")
            if self.previous_stage.ordinal + 1 != self.ordinal:
                raise ValueError("journal predecessor ordinal is not consecutive")
            if self.previous_stage.owner_generation != self.source_generation:
                raise ValueError("journal predecessor is not the source generation")
        return self

    @property
    def record_hash(self) -> Sha256:
        return hash_json(self.model_dump(mode="json"))


class EngineUsageRecord(PilotRecordModel):
    task_id: str
    attempt_id: str
    engine: str
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=16)
    price_source_hash: Sha256 | None = None
    unavailable_reason: str | None = Field(default=None, max_length=1_024)

    @model_validator(mode="after")
    def _availability_is_explicit(self) -> "EngineUsageRecord":
        values = (self.input_tokens, self.output_tokens, self.cached_tokens, self.cost)
        if all(value is None for value in values):
            if not self.unavailable_reason:
                raise ValueError("unavailable usage requires an explicit reason")
        elif self.unavailable_reason is not None:
            raise ValueError("available usage cannot carry unavailable_reason")
        if self.cost is not None and (not self.currency or self.price_source_hash is None):
            raise ValueError("reported cost requires currency and price-source hash")
        return self


class PilotValidationReport(PilotRecordModel):
    schema_version: Literal["package-c-validation-1"] = "package-c-validation-1"
    run_id: Annotated[str, Field(pattern=PILOT_RUN_ID_PATTERN)]
    source_generation: int = Field(ge=0)
    repository_hash: Sha256
    structural_validation: ValidationStatus
    locator_verification: ValidationStatus
    semantic_audits_executed: ValidationStatus
    citation_authorization: ValidationStatus
    exact_assembly: ValidationStatus
    artifact_integrity: ValidationStatus
    human_review_status: Literal[HumanReviewStatus.NOT_PERFORMED]
    synthetic_only: Literal[True] = True
    publication_eligible: Literal[False] = False


__all__ = [
    "EngineUsageRecord",
    "FivePaperPilotBudget",
    "HumanReviewStatus",
    "PilotEngineRoleBinding",
    "PilotRunManifest",
    "PilotRunRegistrationArtifact",
    "PilotRunRegistrationReference",
    "PilotStage",
    "PilotStagePredecessor",
    "PilotStageRecord",
    "PilotStageStatus",
    "PilotValidationReport",
    "ValidationStatus",
    "compute_pilot_engine_role_plan_hash",
    "compute_pilot_schema_fingerprint",
]
