"""Runtime-only records; none are persistent scientific objects."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vibereview.ids import Sha256

if TYPE_CHECKING:
    from .state import RepositorySnapshot


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
    ENGINE_WORKSPACE_INTEGRITY_FAILURE = "engine_workspace_integrity_failure"
    VALID_SCIENTIFIC_RESULT = "valid_scientific_result"
    STALE_SNAPSHOT = "stale_snapshot"
    TASK_TYPE_NOT_IMPLEMENTED = "task_type_not_implemented"
    INTERNAL_RUNTIME_FAILURE = "internal_runtime_failure"
    TRANSACTION_FAILURE = "transaction_failure"
    CONTRACT_IMPLEMENTATION_FAILURE = "contract_implementation_failure"


FALLBACK_OUTCOMES = frozenset(
    {
        AttemptOutcome.ENGINE_EXECUTION_FAILURE,
        AttemptOutcome.ENGINE_FORMAT_FAILURE,
        AttemptOutcome.ENGINE_SCHEMA_FAILURE,
        AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE,
        AttemptOutcome.ENGINE_WORKSPACE_INTEGRITY_FAILURE,
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
    """Engine-facing task handle; never exposes private/ or the master bundle."""

    task_id: str = Field(pattern=r"^TASK[0-9]{4,}$")
    task_type: TaskType
    workspace_dir: Path
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
    """Semantic cache key covering every engine-visible byte of a task bundle."""

    task_type: TaskType
    task_spec_version: str
    prompt_hash: Sha256
    input_schema_hash: Sha256
    proposal_schema_hash: Sha256
    dependency_hashes: dict[str, Sha256]
    resource_hashes: dict[str, Sha256]
    engine_input_hash: Sha256
    bundle_manifest_hash: Sha256
    engine: str
    engine_version: str | None
    safe_engine_configuration_hash: Sha256
    scientific_contract_version: str
    validator_fingerprint: Sha256


RESOURCE_ID_PATTERN = r"^RES[0-9]{4,}$"

MEDIA_EXTENSIONS: dict[str, str] = {
    "text/markdown": ".md",
    "text/plain": ".txt",
    "application/json": ".json",
}


class ResourceValidationError(ValueError):
    pass


class SnapshotSourceChangedError(RuntimeError):
    """A7: a mutable source changed while its bytes were being copied.

    Raised during task construction, before any engine invocation. It is an
    internal snapshot-construction failure, never a fallback condition.
    """

    code = "SNAPSHOT_SOURCE_CHANGED"

    def __init__(self, resource_id: str, source_path: Path) -> None:
        self.resource_id = resource_id
        self.source_path = source_path
        super().__init__(
            f"{self.code}: {source_path} changed while snapshotting {resource_id}"
        )


class TaskSpecNotExecutableError(ValueError):
    """A12 preflight failure, raised before task-ID allocation or engine use."""

    code = "TASK_TYPE_NOT_IMPLEMENTED"

    def __init__(self, task_type: TaskType, problems: Iterable[str]) -> None:
        self.task_type = task_type
        self.problems = tuple(problems)
        details = "; ".join(self.problems)
        super().__init__(f"{self.code}: {task_type.value} is not executable: {details}")


class TaskResourceRequest(RuntimeModel):
    """Private runtime request telling Python where to obtain resource bytes."""

    resource_id: str = Field(pattern=RESOURCE_ID_PATTERN)
    logical_name: str
    source_path: Path
    media_type: str


class SnapshottedResource(RuntimeModel):
    """Engine-visible record of bytes copied into the task bundle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resource_id: str = Field(pattern=RESOURCE_ID_PATTERN)
    logical_name: str
    snapshot_relative_path: Path
    media_type: str
    size_bytes: int = Field(ge=0)
    content_hash: Sha256

    @field_validator("snapshot_relative_path")
    @classmethod
    def _snapshot_path_is_safe(cls, value: Path) -> Path:
        if value.is_absolute():
            raise ValueError("snapshot_relative_path must be relative")
        if ".." in value.parts:
            raise ValueError("snapshot_relative_path must not contain '..' parts")
        return value


class ProjectContext(RuntimeModel):
    project_root: Path
    allowed_source_roots: tuple[Path, ...] = ()

    @model_validator(mode="after")
    def _default_allowed_source_roots(self) -> "ProjectContext":
        if not self.allowed_source_roots:
            self.allowed_source_roots = (self.project_root,)
        return self


class ResourceSourceDependency(RuntimeModel):
    """A11 freshness anchor for a snapshotted resource's origin."""

    type: Literal["external_file"] = "external_file"
    source_hash_at_snapshot: Sha256


class ResourceProvenance(RuntimeModel):
    """Private per-resource provenance retained under private/task_provenance.json."""

    resource_id: str = Field(pattern=RESOURCE_ID_PATTERN)
    logical_name: str
    media_type: str
    bundle_relative_path: Path
    source_path: Path
    source_dependency: ResourceSourceDependency
    snapshot_hash: Sha256
    size_bytes: int = Field(ge=0)


class TaskProvenance(RuntimeModel):
    """Private trust anchor: authoritative expected hashes for the task bundle."""

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
    resources: tuple[ResourceProvenance, ...] = ()

    def task_manifest(self) -> TaskManifest:
        return TaskManifest(
            task_id=self.task_id,
            task_type=self.task_type,
            task_spec_version=self.task_spec_version,
            base_generation=self.base_generation,
            dependencies=dict(self.dependencies),
            instructions_hash=self.instructions_hash,
            input_snapshot_hash=self.input_snapshot_hash,
        )


def allocate_resource_id(ordinal: int) -> str:
    if ordinal < 1:
        raise ResourceValidationError("resource ordinal must be >= 1")
    return f"RES{ordinal:04d}"


def validate_resource_requests(requests: Iterable[TaskResourceRequest]) -> None:
    seen: set[str] = set()
    for request in requests:
        if request.resource_id in seen:
            raise ResourceValidationError(
                f"duplicate resource_id {request.resource_id}"
            )
        seen.add(request.resource_id)
        if request.media_type not in MEDIA_EXTENSIONS:
            raise ResourceValidationError(
                f"unsupported media type {request.media_type!r} "
                f"for {request.resource_id}"
            )
        name = request.logical_name
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ResourceValidationError(
                f"logical_name {name!r} of {request.resource_id} "
                "is metadata and must not look like a path"
            )


def build_resource_requests(
    source_paths: Iterable[Path], *, media_type: str
) -> tuple[TaskResourceRequest, ...]:
    requests = tuple(
        TaskResourceRequest(
            resource_id=allocate_resource_id(ordinal),
            logical_name=source_path.name,
            source_path=source_path,
            media_type=media_type,
        )
        for ordinal, source_path in enumerate(source_paths, start=1)
    )
    validate_resource_requests(requests)
    return requests


def resource_destination(resource_id: str, media_type: str) -> Path:
    """Python-controlled bundle-relative destination for a resource snapshot.

    The destination derives only from the resource ID and the controlled
    media-type map; logical_name is never interpreted as a path.
    """

    if re.fullmatch(RESOURCE_ID_PATTERN, resource_id) is None:
        raise ResourceValidationError(f"invalid resource_id {resource_id!r}")
    extension = MEDIA_EXTENSIONS.get(media_type)
    if extension is None:
        raise ResourceValidationError(f"unsupported media type {media_type!r}")
    return Path("input") / "resources" / resource_id / f"content{extension}"


InvocationT = TypeVar("InvocationT", bound=BaseModel)
EngineInputT = TypeVar("EngineInputT", bound=BaseModel)
ProposalT = TypeVar("ProposalT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class TaskSpec(Generic[InvocationT, EngineInputT, ProposalT]):
    task_type: TaskType
    version: str

    invocation_model: type[InvocationT]
    engine_input_model: type[EngineInputT]
    proposal_model: type[ProposalT]

    dependency_builder: Callable[[InvocationT, RepositorySnapshot], tuple[str, ...]]
    resource_builder: Callable[
        [InvocationT, RepositorySnapshot, ProjectContext],
        tuple[TaskResourceRequest, ...],
    ]
    engine_input_builder: Callable[
        [InvocationT, tuple[SnapshottedResource, ...]],
        EngineInputT,
    ]

    prompt_path: Path
    prompt_version: str

    promotion_handler: str
    disposition_handler: str

    @property
    def input_model(self) -> type[EngineInputT]:
        """Pre-Milestone-A alias kept source-compatible with kernel.py/tasks.py."""
        return self.engine_input_model

