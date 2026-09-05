"""Runtime-only records; none are persistent scientific objects."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from vibereview.ids import Sha256


class RuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class TaskType(StrEnum):
    PARSE_DEEP_RESEARCH = "parse_deep_research"
    CORPUS_CHALLENGER = "corpus_challenger"
    GENERATE_CANDIDATE_CLAIMS = "generate_candidate_claims"
    GENERATE_RETRIEVAL_QUERIES = "generate_retrieval_queries"
    ASSESS_EVIDENCE = "assess_evidence"
    AGGREGATE_PAPER_EVIDENCE = "aggregate_paper_evidence"
    ASSESS_CLAIM = "assess_claim"
    REVISE_CLAIM = "revise_claim"
    VALIDATE_FINAL_CLAIM = "validate_final_claim"
    GENERATE_PROPOSITIONS = "generate_propositions"
    AUDIT_PROPOSITION = "audit_proposition"
    RENDER_PROSE = "render_prose"
    AUDIT_RENDERED_SENTENCE = "audit_rendered_sentence"


class AttemptOutcome(StrEnum):
    ENGINE_EXECUTION_FAILURE = "engine_execution_failure"
    ENGINE_FORMAT_FAILURE = "engine_format_failure"
    ENGINE_SCHEMA_FAILURE = "engine_schema_failure"
    ENGINE_PROPOSAL_VALIDATION_FAILURE = "engine_proposal_validation_failure"
    VALID_SCIENTIFIC_RESULT = "valid_scientific_result"
    STALE_SNAPSHOT = "stale_snapshot"
    INTERNAL_RUNTIME_FAILURE = "internal_runtime_failure"
    TRANSACTION_FAILURE = "transaction_failure"
    CONTRACT_IMPLEMENTATION_FAILURE = "contract_implementation_failure"


FALLBACK_OUTCOMES = frozenset(
    {
        AttemptOutcome.ENGINE_EXECUTION_FAILURE,
        AttemptOutcome.ENGINE_FORMAT_FAILURE,
        AttemptOutcome.ENGINE_SCHEMA_FAILURE,
        AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE,
    }
)


def fallback_allowed(outcome: AttemptOutcome) -> bool:
    return outcome in FALLBACK_OUTCOMES


class RuntimeConfig(RuntimeModel):
    max_stale_rebuilds: int = Field(default=3, ge=0)
    technical_attempts_per_engine: int = Field(default=1, ge=1)
    max_fallback_engines: int = Field(default=2, ge=0)
    writer_lock_timeout_seconds: float = Field(default=30.0, gt=0)


class ProjectManifest(RuntimeModel):
    project_name: str
    scientific_contract_version: str
    runtime_version: str
    created_at: str
    runtime: RuntimeConfig


class TaskManifest(RuntimeModel):
    task_id: str = Field(pattern=r"^TASK[0-9]{4,}$")
    task_type: TaskType
    task_spec_version: str
    base_generation: int = Field(ge=0)
    dependencies: dict[str, Sha256]
    instructions_hash: Sha256
    input_snapshot_hash: Sha256


class TaskAttemptRecord(RuntimeModel):
    task_id: str = Field(pattern=r"^TASK[0-9]{4,}$")
    attempt_id: str
    engine: str
    engine_version: str | None
    outcome: AttemptOutcome
    format_valid: bool
    schema_valid: bool
    proposal_validation_valid: bool | None
    accepted_attempt: bool
    validation_errors: list[str]
    output_hash: Sha256 | None
    agent_result_path: Path


class AgentTask(RuntimeModel):
    task_id: str = Field(pattern=r"^TASK[0-9]{4,}$")
    task_type: TaskType
    task_dir: Path
    instructions_path: Path
    input_dir: Path
    attempt_dir: Path


class AgentResult(RuntimeModel):
    engine: str
    engine_version: str | None
    execution_succeeded: bool
    output_text: str
    stdout: str = ""
    stderr: str = ""
    execution_error: str | None = None


class TransitionDecision(RuntimeModel):
    scientific_disposition: str
    canonicalized: bool
    downstream_eligible: bool
    human_review_required: bool = False


class RuntimeResult(RuntimeModel):
    outcome: AttemptOutcome
    task_id: str
    generation: int | None
    engine: str | None
    transition: TransitionDecision | None
    stale_rebuilds: int = 0
    attempt_records: list[TaskAttemptRecord]
    allocated_ids: dict[str, str] = Field(default_factory=dict)


class GenerationManifest(RuntimeModel):
    generation: int = Field(ge=0)
    previous_generation: int | None
    created_at: str
    repository_hash: Sha256
    registry_hash: Sha256


class CacheSignature(RuntimeModel):
    task_type: TaskType
    task_spec_version: str
    prompt_hash: Sha256
    input_snapshot_hash: Sha256
    engine: str
    engine_version: str | None
    safe_engine_configuration_hash: Sha256
    scientific_contract_version: str
    validator_fingerprint: Sha256


InputT = TypeVar("InputT", bound=BaseModel)
ProposalT = TypeVar("ProposalT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class TaskSpec(Generic[InputT, ProposalT]):
    task_type: TaskType
    version: str
    input_model: type[InputT]
    proposal_model: type[ProposalT]
    prompt_path: Path
    prompt_version: str
    promotion_handler: str
    disposition_handler: str

