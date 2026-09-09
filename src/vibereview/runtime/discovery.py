"""Strict synthetic Package C discovery transitions and immutable artifacts.

The three historical TaskSpecs remain usable by the B2 runner tests.  The
entry points in this module are the operational Package C path: discovery and
challenge proposals become generation-owned, receipt-bound runtime artifacts,
and candidate claims are promoted only after both exact artifacts have been
validated together.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vibereview.enums import Origin
from vibereview.ids import PaperId, Sha256

from .coupled import CoupledCommitPlan, CoupledProposalValidationError
from .dto import (
    AcceptedDiscoveryArtifactInvocation,
    CorpusChallengerInvocation,
    DiscoveryProposalBundle,
    GenerateCandidateClaimsInvocation,
    MAX_DISCOVERY_RESOURCES,
    ParseDeepResearchInvocation,
    ResourceTextLocator,
)
from .engine import AgentEngine, MockEngine
from .hashing import canonical_json_bytes, code_fingerprint, hash_bytes, hash_json, hash_text
from .kernel import ProjectRuntime
from .promotion import promote_discovery
from .receipts import compute_semantic_task_key, verify_receipt_canonical_objects
from .records import (
    AppliedTaskReceipt,
    RuntimeModel,
    RuntimeResult,
    TaskManifest,
    TaskProvenance,
    TaskSpec,
    TaskType,
)
from .registry import CanonicalIdRegistry
from .repository import (
    AuxiliaryStagingWriter,
    GenerationStore,
    PromotionPayload,
    StaleSnapshotError,
    read_contained_regular_file,
)
from .state import RepositorySnapshot
from .tasks import TaskWorkspace


DISCOVERY_ADAPTER_VERSION = "1"
DISCOVERY_ARTIFACT_VERSION = "1"
DISCOVERY_ARTIFACT_ROOT = "discovery/v1"
MIN_STRICT_DISCOVERY_DOCUMENTS = 2
MAX_STRICT_DISCOVERY_DOCUMENTS = 3
STRICT_PAPER_COUNT = 5
MAX_DISCOVERY_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_DISCOVERY_RESOURCE_BYTES = 2 * 1024 * 1024
MAX_DISCOVERY_TOTAL_RESOURCE_BYTES = 16 * 1024 * 1024


class StructuredDiscoveryError(ValueError):
    """A strict discovery input, artifact, or transition is not exact."""


class DiscoveryArtifactKind(StrEnum):
    PARSE = "parse"
    CHALLENGE = "challenge"
    CANDIDATE_GENERATION = "candidate_generation"


_TASK_BY_KIND: dict[DiscoveryArtifactKind, TaskType] = {
    DiscoveryArtifactKind.PARSE: TaskType.PARSE_DEEP_RESEARCH,
    DiscoveryArtifactKind.CHALLENGE: TaskType.CORPUS_CHALLENGER,
    DiscoveryArtifactKind.CANDIDATE_GENERATION: TaskType.GENERATE_CANDIDATE_CLAIMS,
}


class _FrozenRuntimeModel(RuntimeModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


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


class AcceptedDiscoveryArtifact(_FrozenRuntimeModel):
    """Sanitized immutable witness for one accepted structured discovery task."""

    artifact_version: Literal["1"] = DISCOVERY_ARTIFACT_VERSION
    artifact_kind: DiscoveryArtifactKind
    task_id: str = Field(pattern=r"^TASK[0-9]{4,}$")
    source_generation: int = Field(ge=0)
    committed_generation: int = Field(ge=1)
    topic_hash: Sha256
    paper_ids: Annotated[tuple[PaperId, ...], Field(max_length=STRICT_PAPER_COUNT)] = ()
    upstream_artifact_hashes: Annotated[
        dict[str, Sha256], Field(max_length=2)
    ] = Field(default_factory=dict)
    task_provenance_hash: Sha256
    dependency_hashes: Annotated[dict[str, Sha256], Field(max_length=100)]
    resource_hashes: Annotated[
        dict[str, Sha256], Field(max_length=MAX_DISCOVERY_RESOURCES)
    ]
    receipt_hash: Sha256
    accepted_receipt: AppliedTaskReceipt
    synthetic_only: Literal[True] = True

    @model_validator(mode="after")
    def _receipt_and_stage_are_exact(self) -> "AcceptedDiscoveryArtifact":
        receipt = self.accepted_receipt
        expected_task = _TASK_BY_KIND[self.artifact_kind]
        attempt_prefix = f"{self.task_id}/"
        attempt_suffix = receipt.accepted_attempt_id.removeprefix(attempt_prefix)
        if (
            not receipt.accepted_attempt_id.startswith(attempt_prefix)
            or not attempt_suffix
            or "/" in attempt_suffix
        ):
            raise ValueError("discovery artifact receipt belongs to another task")
        if receipt.task_type is not expected_task:
            raise ValueError("discovery artifact task type disagrees with its kind")
        if not (
            self.source_generation == receipt.source_generation
            and self.committed_generation == receipt.committed_generation
            and self.committed_generation == self.source_generation + 1
        ):
            raise ValueError("discovery artifact generation lineage is invalid")
        if self.receipt_hash != hash_json(receipt.model_dump(mode="json")):
            raise ValueError("discovery artifact receipt hash mismatch")
        if receipt.proposal_hash != hash_json(receipt.proposal_payload):
            raise ValueError("discovery artifact proposal hash mismatch")
        if receipt.semantic_task_key != compute_semantic_task_key(
            input_identity_key=receipt.input_identity_key,
            semantic_fingerprint=receipt.semantic_fingerprint,
        ):
            raise ValueError("discovery artifact semantic task key mismatch")
        if (
            receipt.semantic_fingerprint.combined_fingerprint
            != _semantic_fingerprint_hash(receipt)
        ):
            raise ValueError("discovery artifact semantic fingerprint mismatch")
        try:
            DiscoveryProposalBundle.model_validate(receipt.proposal_payload)
        except Exception as exc:
            raise ValueError("discovery artifact proposal payload is invalid") from exc
        transition = receipt.recorded_transition
        if (
            transition.scientific_disposition != "VALID"
            or not transition.downstream_eligible
            or transition.human_review_required
        ):
            raise ValueError("discovery artifact receipt is not an accepted transition")
        pre_canonical = self.artifact_kind in {
            DiscoveryArtifactKind.PARSE,
            DiscoveryArtifactKind.CHALLENGE,
        }
        if pre_canonical and (
            transition.canonicalized
            or receipt.canonical_objects
            or receipt.local_ref_map
        ):
            raise ValueError("pre-canonical discovery artifacts cannot claim promotion")
        if not pre_canonical and (
            not transition.canonicalized
            or not receipt.canonical_objects
            or not receipt.local_ref_map
        ):
            raise ValueError(
                "candidate-generation artifact requires an exact canonical promotion"
            )
        if len(self.paper_ids) != len(set(self.paper_ids)):
            raise ValueError("discovery artifact paper identifiers must be unique")
        expected_upstream = {
            DiscoveryArtifactKind.PARSE: set(),
            DiscoveryArtifactKind.CHALLENGE: {"parse"},
            DiscoveryArtifactKind.CANDIDATE_GENERATION: {"parse", "challenge"},
        }[self.artifact_kind]
        if set(self.upstream_artifact_hashes) != expected_upstream:
            raise ValueError("discovery artifact upstream hash set is incomplete")
        if self.artifact_kind is DiscoveryArtifactKind.PARSE and self.paper_ids:
            raise ValueError("parse artifact cannot name corpus papers")
        if (
            self.artifact_kind is not DiscoveryArtifactKind.PARSE
            and len(self.paper_ids) != STRICT_PAPER_COUNT
        ):
            raise ValueError("challenge-derived artifacts require exactly five papers")
        return self


class DiscoveryArtifactReference(_FrozenRuntimeModel):
    artifact_kind: DiscoveryArtifactKind
    owner_generation: int = Field(ge=1)
    source_generation: int = Field(ge=0)
    task_id: str = Field(pattern=r"^TASK[0-9]{4,}$")
    semantic_task_key: Sha256
    artifact_hash: Sha256
    relative_path: str

    @model_validator(mode="after")
    def _reference_is_canonical(self) -> "DiscoveryArtifactReference":
        if self.owner_generation != self.source_generation + 1:
            raise ValueError("discovery reference generation lineage is invalid")
        if self.relative_path != _artifact_relative_path(
            self.artifact_kind, self.semantic_task_key
        ):
            raise ValueError("discovery reference path is not canonical")
        return self

    def invocation(self, project_root: Path) -> AcceptedDiscoveryArtifactInvocation:
        if self.artifact_kind not in {
            DiscoveryArtifactKind.PARSE,
            DiscoveryArtifactKind.CHALLENGE,
        }:
            raise StructuredDiscoveryError(
                "candidate-generation artifacts are not discovery inputs"
            )
        relative = (
            f"state/generations/{self.owner_generation:06d}/auxiliary/"
            f"{self.relative_path}"
        )
        # Validate the generated relative path before exposing an absolute,
        # private source path to a TaskResourceRequest.
        parsed = PurePosixPath(relative)
        if any(part in {"", ".", ".."} for part in parsed.parts):
            raise StructuredDiscoveryError("discovery artifact path is unsafe")
        return AcceptedDiscoveryArtifactInvocation(
            artifact_kind=self.artifact_kind.value,
            owner_generation=self.owner_generation,
            source_generation=self.source_generation,
            task_id=self.task_id,
            semantic_task_key=self.semantic_task_key,
            artifact_hash=self.artifact_hash,
            artifact_path=(project_root.resolve() / relative),
        )


@dataclass(frozen=True, slots=True)
class PreparedDiscoveryArtifact:
    artifact: AcceptedDiscoveryArtifact
    reference: DiscoveryArtifactReference
    content: bytes


@dataclass(frozen=True, slots=True)
class StructuredDiscoveryRunResult:
    result: RuntimeResult
    artifact_reference: DiscoveryArtifactReference | None


def _artifact_relative_path(
    kind: DiscoveryArtifactKind, semantic_task_key: Sha256
) -> str:
    digest = semantic_task_key.removeprefix("sha256:")
    return f"{DISCOVERY_ARTIFACT_ROOT}/{kind.value}/{digest}.json"


def _resource_hashes(provenance: TaskProvenance) -> dict[str, Sha256]:
    return {
        item.resource_id: item.snapshot_hash
        for item in sorted(provenance.resources, key=lambda value: value.resource_id)
    }


def build_discovery_artifact(
    *,
    kind: DiscoveryArtifactKind,
    topic: str,
    paper_ids: Sequence[str],
    upstream_artifact_hashes: Mapping[str, Sha256],
    provenance: TaskProvenance,
    receipt: AppliedTaskReceipt,
) -> PreparedDiscoveryArtifact:
    """Build one deterministic artifact without mutating repository state."""

    try:
        artifact = AcceptedDiscoveryArtifact(
            artifact_kind=kind,
            task_id=receipt.accepted_attempt_id.rsplit("/", 1)[0],
            source_generation=receipt.source_generation,
            committed_generation=receipt.committed_generation,
            topic_hash=hash_text(topic),
            paper_ids=tuple(paper_ids),
            upstream_artifact_hashes=dict(sorted(upstream_artifact_hashes.items())),
            task_provenance_hash=hash_json(provenance.model_dump(mode="json")),
            dependency_hashes=dict(sorted(provenance.dependencies.items())),
            resource_hashes=_resource_hashes(provenance),
            receipt_hash=hash_json(receipt.model_dump(mode="json")),
            accepted_receipt=receipt,
        )
    except Exception as exc:
        raise StructuredDiscoveryError(
            f"discovery artifact bindings are invalid: {exc}"
        ) from exc
    if provenance.task_id != artifact.task_id:
        raise StructuredDiscoveryError("task provenance belongs to another task")
    if (
        provenance.task_type is not receipt.task_type
        or provenance.base_generation != receipt.source_generation
    ):
        raise StructuredDiscoveryError("task provenance and receipt differ")
    if (
        artifact.dependency_hashes != dict(provenance.dependencies)
        or artifact.resource_hashes != _resource_hashes(provenance)
    ):
        raise StructuredDiscoveryError(
            "discovery artifact omits task dependency or resource provenance"
        )
    content = canonical_json_bytes(artifact.model_dump(mode="json")) + b"\n"
    if len(content) > MAX_DISCOVERY_ARTIFACT_BYTES:
        raise StructuredDiscoveryError("discovery artifact exceeds its byte limit")
    artifact_hash = hash_bytes(content)
    reference = DiscoveryArtifactReference(
        artifact_kind=kind,
        owner_generation=receipt.committed_generation,
        source_generation=receipt.source_generation,
        task_id=artifact.task_id,
        semantic_task_key=receipt.semantic_task_key,
        artifact_hash=artifact_hash,
        relative_path=_artifact_relative_path(kind, receipt.semantic_task_key),
    )
    return PreparedDiscoveryArtifact(artifact, reference, content)


def load_discovery_artifact(
    project_root: Path, reference: DiscoveryArtifactReference
) -> AcceptedDiscoveryArtifact:
    store = GenerationStore(project_root)
    snapshot, _, auxiliary = store.load_generation_auxiliary(
        reference.owner_generation, {reference.relative_path}
    )
    content = auxiliary[reference.relative_path]
    if len(content) > MAX_DISCOVERY_ARTIFACT_BYTES:
        raise StructuredDiscoveryError("discovery artifact exceeds its byte limit")
    if hash_bytes(content) != reference.artifact_hash:
        raise StructuredDiscoveryError("discovery artifact content hash mismatch")
    try:
        artifact = AcceptedDiscoveryArtifact.model_validate_json(content)
    except Exception as exc:
        raise StructuredDiscoveryError("discovery artifact is invalid") from exc
    if canonical_json_bytes(artifact.model_dump(mode="json")) + b"\n" != content:
        raise StructuredDiscoveryError("discovery artifact serialization is not canonical")
    receipt = artifact.accepted_receipt
    if (
        artifact.artifact_kind is not reference.artifact_kind
        or artifact.task_id != reference.task_id
        or artifact.source_generation != reference.source_generation
        or artifact.committed_generation != reference.owner_generation
        or receipt.semantic_task_key != reference.semantic_task_key
    ):
        raise StructuredDiscoveryError("discovery artifact reference differs from content")
    matching = tuple(
        item
        for item in store.load_receipts(reference.owner_generation)
        if item.semantic_task_key == reference.semantic_task_key
    )
    if matching != (receipt,):
        raise StructuredDiscoveryError(
            "accepted discovery receipt is absent or ambiguous in its generation"
        )
    task_dir = project_root.resolve() / "work" / "tasks" / artifact.task_id
    try:
        provenance = TaskWorkspace.load_provenance(task_dir)
        TaskWorkspace(project_root).verify_bundle(task_dir, provenance)
    except Exception as exc:
        raise StructuredDiscoveryError(
            "accepted discovery task provenance is absent or invalid"
        ) from exc
    if (
        hash_json(provenance.model_dump(mode="json"))
        != artifact.task_provenance_hash
        or provenance.task_type is not receipt.task_type
        or provenance.base_generation != artifact.source_generation
        or dict(provenance.dependencies) != artifact.dependency_hashes
        or _resource_hashes(provenance) != artifact.resource_hashes
    ):
        raise StructuredDiscoveryError(
            "accepted discovery artifact differs from its private task provenance"
        )
    canonical_ok, reason = verify_receipt_canonical_objects(receipt, snapshot)
    if not canonical_ok:
        raise StructuredDiscoveryError(reason or "discovery receipt objects differ")
    return artifact


def load_discovery_artifact_for_receipt(
    project_root: Path,
    kind: DiscoveryArtifactKind,
    receipt: AppliedTaskReceipt,
) -> tuple[AcceptedDiscoveryArtifact, DiscoveryArtifactReference]:
    relative = _artifact_relative_path(kind, receipt.semantic_task_key)
    store = GenerationStore(project_root)
    _, _, auxiliary = store.load_generation_auxiliary(
        receipt.committed_generation, {relative}
    )
    content = auxiliary[relative]
    reference = DiscoveryArtifactReference(
        artifact_kind=kind,
        owner_generation=receipt.committed_generation,
        source_generation=receipt.source_generation,
        task_id=receipt.accepted_attempt_id.rsplit("/", 1)[0],
        semantic_task_key=receipt.semantic_task_key,
        artifact_hash=hash_bytes(content),
        relative_path=relative,
    )
    return load_discovery_artifact(project_root, reference), reference


def _reference_from_invocation(
    project_root: Path,
    value: AcceptedDiscoveryArtifactInvocation,
) -> DiscoveryArtifactReference:
    kind = DiscoveryArtifactKind(value.artifact_kind)
    expected_relative = _artifact_relative_path(kind, value.semantic_task_key)
    expected_path = (
        project_root.resolve()
        / "state"
        / "generations"
        / f"{value.owner_generation:06d}"
        / "auxiliary"
        / expected_relative
    )
    if value.artifact_path.resolve(strict=False) != expected_path:
        raise StructuredDiscoveryError(
            "discovery artifact source path differs from its generation owner"
        )
    return DiscoveryArtifactReference(
        artifact_kind=kind,
        owner_generation=value.owner_generation,
        source_generation=value.source_generation,
        task_id=value.task_id,
        semantic_task_key=value.semantic_task_key,
        artifact_hash=value.artifact_hash,
        relative_path=expected_relative,
    )


def _proposal(artifact: AcceptedDiscoveryArtifact) -> DiscoveryProposalBundle:
    return DiscoveryProposalBundle.model_validate(
        artifact.accepted_receipt.proposal_payload
    )


def _parse_item_refs(proposal: DiscoveryProposalBundle) -> set[str]:
    return {
        *(item.local_ref for item in proposal.themes),
        *(item.local_ref for item in proposal.claims),
        *(item.local_ref for item in proposal.terminology),
        *(item.local_ref for item in proposal.paper_candidates),
        *(item.local_ref for item in proposal.controversies),
        *(item.local_ref for item in proposal.gaps),
    }


def _all_parse_locators(
    proposal: DiscoveryProposalBundle,
) -> tuple[ResourceTextLocator, ...]:
    locator_groups = [
        *(item.locators for item in proposal.source_bindings),
        *(item.locators for item in proposal.terminology),
        *(item.locators for item in proposal.paper_candidates),
        *(item.locators for item in proposal.controversies),
        *(item.locators for item in proposal.gaps),
    ]
    return tuple(locator for group in locator_groups for locator in group)


def _adapter_fingerprint(kind: DiscoveryArtifactKind) -> Sha256:
    return code_fingerprint(
        [Path(__file__)],
        f"structured-discovery:{DISCOVERY_ADAPTER_VERSION}:{kind.value}",
    )


class _StructuredDiscoveryAdapter:
    promotion_handler = "promote_discovery"
    rebuild_on_stale = False
    kind: DiscoveryArtifactKind
    task_type: TaskType
    invocation_model: type[BaseModel]

    def __init__(self, project_root: Path, invocation: BaseModel) -> None:
        self.project_root = project_root.resolve()
        self.invocation = invocation
        self.promotion_fingerprint = _adapter_fingerprint(self.kind)
        self.artifact_reference: DiscoveryArtifactReference | None = None
        self._resource_bytes: dict[str, bytes] | None = None
        self._resource_hashes: dict[str, Sha256] | None = None

    def _require_types(
        self,
        spec: TaskSpec,
        invocation: BaseModel,
        proposal: BaseModel | None = None,
    ) -> None:
        if spec.task_type is not self.task_type:
            raise ValueError("structured discovery adapter received another TaskSpec")
        if type(invocation) is not self.invocation_model or invocation != self.invocation:
            raise ValueError("structured discovery adapter invocation identity differs")
        if proposal is not None and type(proposal) is not DiscoveryProposalBundle:
            raise CoupledProposalValidationError(
                "structured discovery adapter requires DiscoveryProposalBundle"
            )

    def _load_bundle_resources(self, provenance: TaskProvenance) -> None:
        resource_bytes: dict[str, bytes] = {}
        resource_hashes: dict[str, Sha256] = {}
        total = 0
        for resource in provenance.resources:
            relative = (
                f"work/tasks/{provenance.task_id}/bundle/"
                f"{resource.bundle_relative_path.as_posix()}"
            )
            try:
                content, _ = read_contained_regular_file(
                    self.project_root,
                    relative,
                    max_bytes=MAX_DISCOVERY_ARTIFACT_BYTES,
                )
            except Exception as exc:
                raise StructuredDiscoveryError(
                    f"task resource {resource.resource_id} is not safely readable"
                ) from exc
            total += len(content)
            if total > MAX_DISCOVERY_TOTAL_RESOURCE_BYTES:
                raise StructuredDiscoveryError(
                    "structured discovery task resources exceed their total budget"
                )
            if hash_bytes(content) != resource.snapshot_hash:
                raise StructuredDiscoveryError(
                    f"task resource {resource.resource_id} differs from provenance"
                )
            resource_bytes[resource.resource_id] = content
            resource_hashes[resource.resource_id] = resource.snapshot_hash
        self._resource_bytes = resource_bytes
        self._resource_hashes = resource_hashes

    def validate_pre_execution(
        self,
        *,
        project_root: Path,
        spec: TaskSpec,
        invocation: BaseModel,
        manifest: TaskManifest,
        provenance: TaskProvenance,
    ) -> None:
        self._require_types(spec, invocation)
        if project_root.resolve() != self.project_root:
            raise StructuredDiscoveryError("structured discovery project root differs")
        if (
            manifest.task_id != provenance.task_id
            or manifest.base_generation != provenance.base_generation
            or provenance.task_type is not self.task_type
        ):
            raise StructuredDiscoveryError("task manifest and provenance differ")
        current = GenerationStore(self.project_root).current_generation()
        if current != manifest.base_generation:
            raise StaleSnapshotError(
                f"task generation {manifest.base_generation} is stale; current is {current}"
            )
        self._load_bundle_resources(provenance)
        snapshot, _ = GenerationStore(self.project_root).load_generation(
            manifest.base_generation
        )
        self._validate_resource_contract(provenance, snapshot)

    def _validate_resource_contract(
        self, provenance: TaskProvenance, snapshot: RepositorySnapshot
    ) -> None:
        raise NotImplementedError

    def _validate_locator(
        self,
        locator: ResourceTextLocator,
        *,
        allowed_resource_ids: set[str],
    ) -> None:
        if locator.resource_id not in allowed_resource_ids:
            raise CoupledProposalValidationError(
                f"locator resource {locator.resource_id} is outside the exact RES scope"
            )
        if self._resource_hashes is None or self._resource_bytes is None:
            # Accepted-receipt reuse is verified against the immutable artifact
            # below; fresh execution always populates these maps in preflight.
            return
        expected_hash = self._resource_hashes.get(locator.resource_id)
        if locator.resource_hash != expected_hash:
            raise CoupledProposalValidationError(
                f"locator resource hash differs for {locator.resource_id}"
            )
        content = self._resource_bytes[locator.resource_id]
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CoupledProposalValidationError(
                f"resource {locator.resource_id} is not UTF-8"
            ) from exc
        if locator.end_offset > len(text):
            raise CoupledProposalValidationError(
                f"locator exceeds resource {locator.resource_id}"
            )
        observed = text[locator.start_offset : locator.end_offset]
        if hash_text(observed) != locator.source_span_hash:
            raise CoupledProposalValidationError(
                f"locator span hash differs for {locator.resource_id}"
            )

    def _artifact_inputs(self) -> tuple[Mapping[str, Sha256], tuple[str, ...]]:
        return {}, ()

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
        del registry
        self._require_types(spec, invocation, proposal)
        assert isinstance(proposal, DiscoveryProposalBundle)
        self._validate_proposal(
            proposal,
            snapshot,
            dependency_keys,
            accepted_allocations,
        )

    def _validate_proposal(
        self,
        proposal: DiscoveryProposalBundle,
        snapshot: RepositorySnapshot,
        dependency_keys: Mapping[str, str],
        accepted_allocations: Mapping[str, str] | None,
    ) -> None:
        raise NotImplementedError

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
        self._require_types(spec, invocation, proposal)
        assert isinstance(proposal, DiscoveryProposalBundle)
        upstream, paper_ids = self._artifact_inputs()

        def promote(
            snapshot: RepositorySnapshot, registry: CanonicalIdRegistry
        ) -> PromotionPayload:
            self._validate_proposal(
                proposal,
                snapshot,
                manifest.dependencies,
                None,
            )
            if self.kind is DiscoveryArtifactKind.CANDIDATE_GENERATION:
                return promote_discovery(snapshot, registry, proposal)
            return PromotionPayload(snapshot, registry, {})

        def materialize(
            writer: AuxiliaryStagingWriter,
            next_generation: int,
            _payload: PromotionPayload,
            receipt: AppliedTaskReceipt,
        ) -> None:
            if (
                next_generation != receipt.committed_generation
                or next_generation != receipt.source_generation + 1
            ):
                raise StructuredDiscoveryError(
                    "discovery receipt does not own the staged generation"
                )
            prepared = build_discovery_artifact(
                kind=self.kind,
                topic=getattr(invocation, "topic"),
                paper_ids=paper_ids,
                upstream_artifact_hashes=upstream,
                provenance=provenance,
                receipt=receipt,
            )
            writer.write_bytes(prepared.reference.relative_path, prepared.content)
            self.artifact_reference = prepared.reference

        return CoupledCommitPlan(promote, materialize)

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
        del manifest, provenance, current_registry
        try:
            self._require_types(spec, invocation, proposal)
            artifact, reference = load_discovery_artifact_for_receipt(
                project_root, self.kind, receipt
            )
            upstream, paper_ids = self._artifact_inputs()
            if (
                artifact.topic_hash != hash_text(getattr(invocation, "topic"))
                or artifact.paper_ids != paper_ids
                or artifact.upstream_artifact_hashes != dict(upstream)
                or reference.owner_generation > current_generation
            ):
                return False, "accepted discovery artifact input identity differs"
            canonical_ok, reason = verify_receipt_canonical_objects(
                receipt, current_snapshot
            )
            if not canonical_ok:
                return False, reason
            self.artifact_reference = reference
        except Exception as exc:
            return False, f"accepted discovery artifact verification failed:{exc}"
        return True, None


class StructuredDiscoveryParseAdapter(_StructuredDiscoveryAdapter):
    kind = DiscoveryArtifactKind.PARSE
    task_type = TaskType.PARSE_DEEP_RESEARCH
    invocation_model = ParseDeepResearchInvocation

    def _validate_resource_contract(
        self, provenance: TaskProvenance, snapshot: RepositorySnapshot
    ) -> None:
        del snapshot
        invocation = self.invocation
        assert isinstance(invocation, ParseDeepResearchInvocation)
        resources = provenance.resources
        if not (
            MIN_STRICT_DISCOVERY_DOCUMENTS
            <= len(resources)
            <= MAX_STRICT_DISCOVERY_DOCUMENTS
            and len(resources) == len(invocation.document_paths)
        ):
            raise StructuredDiscoveryError(
                "strict discovery parse requires two or three exact documents"
            )
        expected_ids = tuple(f"RES{index:04d}" for index in range(1, len(resources) + 1))
        if tuple(item.resource_id for item in resources) != expected_ids:
            raise StructuredDiscoveryError("discovery document RES ordering differs")
        if any(item.media_type != "text/markdown" for item in resources):
            raise StructuredDiscoveryError("discovery documents must be Markdown")
        if len({path.resolve(strict=False) for path in invocation.document_paths}) != len(
            invocation.document_paths
        ):
            raise StructuredDiscoveryError("discovery document paths must be unique")
        if tuple(item.source_path.resolve(strict=False) for item in resources) != tuple(
            path.resolve(strict=False) for path in invocation.document_paths
        ):
            raise StructuredDiscoveryError(
                "discovery resources differ from the private document selection"
            )

    def _validate_proposal(
        self,
        proposal: DiscoveryProposalBundle,
        snapshot: RepositorySnapshot,
        dependency_keys: Mapping[str, str],
        accepted_allocations: Mapping[str, str] | None,
    ) -> None:
        del snapshot
        if dependency_keys:
            raise CoupledProposalValidationError(
                "discovery parse cannot depend on canonical scientific state"
            )
        if accepted_allocations:
            raise CoupledProposalValidationError(
                "discovery parse receipt cannot contain canonical allocations"
            )
        sections = (
            proposal.themes,
            proposal.claims,
            proposal.terminology,
            proposal.paper_candidates,
            proposal.controversies,
            proposal.gaps,
        )
        if any(not section for section in sections):
            raise CoupledProposalValidationError(
                "structured discovery must retain themes, claims, terminology, "
                "paper candidates, controversies, and gaps"
            )
        if proposal.paper_concept_sketches or proposal.coverage_findings:
            raise CoupledProposalValidationError(
                "parse proposal cannot contain corpus-challenger output"
            )
        if proposal.input_dispositions:
            raise CoupledProposalValidationError(
                "parse proposal cannot contain candidate input dispositions"
            )
        refs = [
            *(item.local_ref for item in proposal.themes),
            *(item.local_ref for item in proposal.claims),
            *(item.local_ref for item in proposal.terminology),
            *(item.local_ref for item in proposal.paper_candidates),
            *(item.local_ref for item in proposal.controversies),
            *(item.local_ref for item in proposal.gaps),
        ]
        if len(refs) != len(set(refs)):
            raise CoupledProposalValidationError(
                "structured discovery local references must be globally unique"
            )
        expected_bindings = {
            *(item.local_ref for item in proposal.themes),
            *(item.local_ref for item in proposal.claims),
        }
        binding_refs = [item.target_ref for item in proposal.source_bindings]
        if set(binding_refs) != expected_bindings or len(binding_refs) != len(
            expected_bindings
        ):
            raise CoupledProposalValidationError(
                "every discovery theme and claim requires one exact source binding"
            )
        allowed = {
            f"RES{index:04d}"
            for index in range(1, len(self.invocation.document_paths) + 1)
        }
        bindings = {item.target_ref: item for item in proposal.source_bindings}
        for item in proposal.themes:
            if item.origin is not Origin.DEEP_RESEARCH:
                raise CoupledProposalValidationError(
                    "parsed discovery themes must retain deep-research origin"
                )
        for item in proposal.claims:
            binding_resources = {
                locator.resource_id for locator in bindings[item.local_ref].locators
            }
            if (
                item.origin is not Origin.DEEP_RESEARCH
                or set(item.origin_refs) != binding_resources
            ):
                raise CoupledProposalValidationError(
                    "parsed claim origin_refs must equal its exact RES sources"
                )
        for locator in _all_parse_locators(proposal):
            self._validate_locator(locator, allowed_resource_ids=allowed)


class StructuredCorpusChallengeAdapter(_StructuredDiscoveryAdapter):
    kind = DiscoveryArtifactKind.CHALLENGE
    task_type = TaskType.CORPUS_CHALLENGER
    invocation_model = CorpusChallengerInvocation

    def __init__(self, project_root: Path, invocation: CorpusChallengerInvocation) -> None:
        super().__init__(project_root, invocation)
        if invocation.discovery_artifact is None:
            raise StructuredDiscoveryError(
                "strict corpus challenger requires an accepted parse artifact"
            )
        self.parse_reference = _reference_from_invocation(
            self.project_root, invocation.discovery_artifact
        )
        self.parse_artifact = load_discovery_artifact(
            self.project_root, self.parse_reference
        )
        if self.parse_artifact.topic_hash != hash_text(invocation.topic):
            raise StructuredDiscoveryError(
                "corpus challenge topic differs from the accepted parse artifact"
            )

    def _artifact_inputs(self) -> tuple[Mapping[str, Sha256], tuple[str, ...]]:
        invocation = self.invocation
        assert isinstance(invocation, CorpusChallengerInvocation)
        return {"parse": self.parse_reference.artifact_hash}, tuple(invocation.paper_ids)

    def _validate_resource_contract(
        self, provenance: TaskProvenance, snapshot: RepositorySnapshot
    ) -> None:
        invocation = self.invocation
        assert isinstance(invocation, CorpusChallengerInvocation)
        if len(invocation.paper_ids) != STRICT_PAPER_COUNT:
            raise StructuredDiscoveryError(
                "strict corpus challenger requires exactly five papers"
            )
        if provenance.base_generation != self.parse_reference.owner_generation:
            raise StructuredDiscoveryError(
                "corpus challenge must consume the current parse artifact generation"
            )
        if (
            len(invocation.paper_resource_paths) != STRICT_PAPER_COUNT
            or len(invocation.paper_source_hashes) != STRICT_PAPER_COUNT
        ):
            raise StructuredDiscoveryError(
                "corpus challenge requires exact paths and source hashes for five papers"
            )
        expected_dependencies = {
            f"Paper:{paper_id}" for paper_id in invocation.paper_ids
        }
        if set(provenance.dependencies) != expected_dependencies:
            raise StructuredDiscoveryError(
                "corpus challenger dependencies must be the exact five Papers"
            )
        if len(provenance.resources) != STRICT_PAPER_COUNT + 1:
            raise StructuredDiscoveryError(
                "corpus challenger requires parse artifact plus five raw papers"
            )
        resources = provenance.resources
        if resources[0].resource_id != "RES0001" or resources[0].snapshot_hash != self.parse_reference.artifact_hash:
            raise StructuredDiscoveryError("corpus challenger parse resource differs")
        papers = {paper.paper_id: paper for paper in snapshot.papers}
        if set(papers) != set(invocation.paper_ids) or len(papers) != STRICT_PAPER_COUNT:
            raise StructuredDiscoveryError(
                "corpus challenge paper selection differs from the locked corpus"
            )
        if (
            resources[0].media_type != "application/json"
            or resources[0].logical_name != "accepted_discovery_parse.json"
        ):
            raise StructuredDiscoveryError("corpus challenger parse resource layout differs")
        for index, paper_id in enumerate(invocation.paper_ids, start=2):
            paper = papers.get(paper_id)
            resource = resources[index - 1]
            if paper is None:
                raise StructuredDiscoveryError(f"corpus paper {paper_id} is absent")
            expected_path = (self.project_root / paper.raw_md_path).resolve(strict=False)
            if (
                resource.resource_id != f"RES{index:04d}"
                or resource.media_type != "text/markdown"
                or resource.snapshot_hash != paper.raw_md_hash
                or invocation.paper_source_hashes[index - 2] != paper.source_hash
                or invocation.paper_resource_paths[index - 2].resolve(strict=False)
                != expected_path
                or resource.source_path.resolve(strict=False) != expected_path
            ):
                raise StructuredDiscoveryError(
                    f"corpus challenger raw resource differs for {paper_id}"
                )

    def _validate_proposal(
        self,
        proposal: DiscoveryProposalBundle,
        snapshot: RepositorySnapshot,
        dependency_keys: Mapping[str, str],
        accepted_allocations: Mapping[str, str] | None,
    ) -> None:
        del snapshot
        invocation = self.invocation
        assert isinstance(invocation, CorpusChallengerInvocation)
        if accepted_allocations:
            raise CoupledProposalValidationError(
                "corpus challenge receipt cannot contain canonical allocations"
            )
        if set(dependency_keys) != {
            f"Paper:{paper_id}" for paper_id in invocation.paper_ids
        }:
            raise CoupledProposalValidationError(
                "corpus challenge dependency scope differs from the five Papers"
            )
        if any(
            (
                proposal.themes,
                proposal.claims,
                proposal.source_bindings,
                proposal.terminology,
                proposal.paper_candidates,
                proposal.controversies,
                proposal.gaps,
                proposal.input_dispositions,
            )
        ):
            raise CoupledProposalValidationError(
                "corpus challenge may contain only sketches and coverage findings"
            )
        sketch_papers = [item.paper_ref for item in proposal.paper_concept_sketches]
        if (
            len(sketch_papers) != STRICT_PAPER_COUNT
            or set(sketch_papers) != set(invocation.paper_ids)
        ):
            raise CoupledProposalValidationError(
                "corpus challenge requires exactly one concept sketch per paper"
            )
        parse_refs = _parse_item_refs(_proposal(self.parse_artifact))
        finding_refs = [item.discovery_ref for item in proposal.coverage_findings]
        if set(finding_refs) != parse_refs or len(finding_refs) != len(parse_refs):
            raise CoupledProposalValidationError(
                "corpus challenge must cover every exact discovery concept once"
            )
        local_refs = [
            *(item.local_ref for item in proposal.paper_concept_sketches),
            *(item.local_ref for item in proposal.coverage_findings),
        ]
        if len(local_refs) != len(set(local_refs)):
            raise CoupledProposalValidationError(
                "corpus challenge local references must be unique"
            )
        paper_resource = {
            paper_id: f"RES{index:04d}"
            for index, paper_id in enumerate(invocation.paper_ids, start=2)
        }
        for sketch in proposal.paper_concept_sketches:
            expected_resource = paper_resource[sketch.paper_ref]
            for locator in sketch.locators:
                if locator.resource_id != expected_resource:
                    raise CoupledProposalValidationError(
                        "paper sketch locator belongs to another paper"
                    )
                self._validate_locator(
                    locator, allowed_resource_ids={expected_resource}
                )
        for finding in proposal.coverage_findings:
            if not set(finding.paper_refs) <= set(invocation.paper_ids):
                raise CoupledProposalValidationError(
                    "coverage finding references a paper outside the five-paper corpus"
                )
            locator_papers = {
                paper_id
                for paper_id, resource_id in paper_resource.items()
                if any(
                    locator.resource_id == resource_id
                    for locator in finding.locators
                )
            }
            if locator_papers != set(finding.paper_refs):
                raise CoupledProposalValidationError(
                    "coverage finding paper refs must exactly match its RES locators"
                )
            for locator in finding.locators:
                self._validate_locator(
                    locator,
                    allowed_resource_ids=set(paper_resource.values()),
                )


class StructuredCandidateClaimsAdapter(_StructuredDiscoveryAdapter):
    kind = DiscoveryArtifactKind.CANDIDATE_GENERATION
    task_type = TaskType.GENERATE_CANDIDATE_CLAIMS
    invocation_model = GenerateCandidateClaimsInvocation

    def __init__(
        self, project_root: Path, invocation: GenerateCandidateClaimsInvocation
    ) -> None:
        super().__init__(project_root, invocation)
        if invocation.discovery_artifact is None or invocation.challenge_artifact is None:
            raise StructuredDiscoveryError(
                "strict candidate generation requires parse and challenge artifacts"
            )
        self.parse_reference = _reference_from_invocation(
            self.project_root, invocation.discovery_artifact
        )
        self.challenge_reference = _reference_from_invocation(
            self.project_root, invocation.challenge_artifact
        )
        self.parse_artifact = load_discovery_artifact(
            self.project_root, self.parse_reference
        )
        self.challenge_artifact = load_discovery_artifact(
            self.project_root, self.challenge_reference
        )
        if self.challenge_artifact.upstream_artifact_hashes != {
            "parse": self.parse_reference.artifact_hash
        }:
            raise StructuredDiscoveryError(
                "challenge artifact is not derived from the selected parse artifact"
            )
        if (
            self.challenge_artifact.source_generation
            != self.parse_reference.owner_generation
            or self.parse_artifact.topic_hash != hash_text(invocation.topic)
            or self.challenge_artifact.topic_hash != hash_text(invocation.topic)
        ):
            raise StructuredDiscoveryError(
                "candidate artifacts do not form one topic-bound generation chain"
            )

    def _artifact_inputs(self) -> tuple[Mapping[str, Sha256], tuple[str, ...]]:
        return (
            {
                "challenge": self.challenge_reference.artifact_hash,
                "parse": self.parse_reference.artifact_hash,
            },
            self.challenge_artifact.paper_ids,
        )

    def _validate_resource_contract(
        self, provenance: TaskProvenance, snapshot: RepositorySnapshot
    ) -> None:
        invocation = self.invocation
        assert isinstance(invocation, GenerateCandidateClaimsInvocation)
        if provenance.base_generation != self.challenge_reference.owner_generation:
            raise StructuredDiscoveryError(
                "candidate generation must consume the current challenge generation"
            )
        if {paper.paper_id for paper in snapshot.papers} != set(
            self.challenge_artifact.paper_ids
        ):
            raise StructuredDiscoveryError(
                "candidate generation corpus differs from the accepted challenge"
            )
        expected_dependencies = {
            f"ThemeRecord:{theme_id}" for theme_id in invocation.existing_theme_ids
        }
        if set(provenance.dependencies) != expected_dependencies:
            raise StructuredDiscoveryError(
                "candidate generation dependencies differ from selected themes"
            )
        if len(provenance.resources) != 2:
            raise StructuredDiscoveryError(
                "candidate generation requires exactly two accepted artifacts"
            )
        expected = (
            ("RES0001", self.parse_reference.artifact_hash),
            ("RES0002", self.challenge_reference.artifact_hash),
        )
        if tuple(
            (item.resource_id, item.snapshot_hash) for item in provenance.resources
        ) != expected or any(
            item.media_type != "application/json" for item in provenance.resources
        ):
            raise StructuredDiscoveryError(
                "candidate generation artifact resources differ"
            )

    def _validate_proposal(
        self,
        proposal: DiscoveryProposalBundle,
        snapshot: RepositorySnapshot,
        dependency_keys: Mapping[str, str],
        accepted_allocations: Mapping[str, str] | None,
    ) -> None:
        invocation = self.invocation
        assert isinstance(invocation, GenerateCandidateClaimsInvocation)
        if any(
            (
                proposal.source_bindings,
                proposal.terminology,
                proposal.paper_candidates,
                proposal.controversies,
                proposal.gaps,
                proposal.paper_concept_sketches,
                proposal.coverage_findings,
            )
        ):
            raise CoupledProposalValidationError(
                "candidate generation may promote only themes and combined claims"
            )
        if not proposal.themes or not proposal.claims:
            raise CoupledProposalValidationError(
                "candidate generation requires bounded themes and claims"
            )
        refs = [
            *(item.local_ref for item in proposal.themes),
            *(item.local_ref for item in proposal.claims),
        ]
        if len(refs) != len(set(refs)):
            raise CoupledProposalValidationError(
                "candidate generation local references must be unique"
            )
        local_themes = {item.local_ref for item in proposal.themes}
        canonical_themes = {item.theme_id for item in snapshot.themes}
        invoked_themes = set(invocation.existing_theme_ids)
        if set(dependency_keys) != {
            f"ThemeRecord:{theme_id}" for theme_id in invoked_themes
        } or not invoked_themes <= canonical_themes:
            raise CoupledProposalValidationError(
                "candidate generation theme dependency scope differs"
            )
        allowed_themes = local_themes | invoked_themes
        for theme in proposal.themes:
            if theme.origin is not Origin.GENERATED:
                raise CoupledProposalValidationError(
                    "combined candidate themes must have generated origin"
                )
            if theme.parent_ref is not None and theme.parent_ref not in allowed_themes:
                raise CoupledProposalValidationError(
                    f"unknown candidate theme parent {theme.parent_ref}"
                )
        parent_by_ref = {
            item.local_ref: item.parent_ref
            for item in proposal.themes
            if item.parent_ref in local_themes
        }
        for start in parent_by_ref:
            seen: set[str] = set()
            current: str | None = start
            while current in parent_by_ref:
                if current in seen:
                    raise CoupledProposalValidationError(
                        "candidate theme hierarchy is cyclic"
                    )
                seen.add(current)
                current = parent_by_ref[current]
        parse_refs = _parse_item_refs(_proposal(self.parse_artifact))
        challenge_refs = {
            *(item.local_ref for item in _proposal(self.challenge_artifact).coverage_findings),
            *(item.local_ref for item in _proposal(self.challenge_artifact).paper_concept_sketches),
        }
        expected_inputs = {
            *(('parse', value) for value in parse_refs),
            *(('challenge', value) for value in challenge_refs),
        }
        dispositions = {
            (item.source_kind, item.source_ref): item
            for item in proposal.input_dispositions
        }
        if (
            len(dispositions) != len(proposal.input_dispositions)
            or set(dispositions) != expected_inputs
        ):
            raise CoupledProposalValidationError(
                "candidate generation must explicitly include or exclude every "
                "parse and challenge input exactly once"
            )
        claims_by_ref = {item.local_ref: item for item in proposal.claims}
        for key, disposition in dispositions.items():
            target_refs = set(disposition.candidate_claim_refs)
            if not target_refs <= set(claims_by_ref):
                raise CoupledProposalValidationError(
                    "discovery input disposition names an unknown candidate claim"
                )
            expected_origin = f"{key[0]}:{key[1]}"
            actual_targets = {
                claim.local_ref
                for claim in proposal.claims
                if expected_origin in claim.origin_refs
            }
            if disposition.disposition == "included":
                if actual_targets != target_refs:
                    raise CoupledProposalValidationError(
                        "included discovery input targets must exactly match claim origins"
                    )
            elif actual_targets:
                raise CoupledProposalValidationError(
                    "excluded discovery input cannot remain in a claim origin"
                )
        for claim in proposal.claims:
            if claim.theme_ref not in allowed_themes:
                raise CoupledProposalValidationError(
                    f"unknown combined claim theme {claim.theme_ref}"
                )
            parse_origins = {
                value.removeprefix("parse:")
                for value in claim.origin_refs
                if value.startswith("parse:")
            }
            challenge_origins = {
                value.removeprefix("challenge:")
                for value in claim.origin_refs
                if value.startswith("challenge:")
            }
            expected_count = len(parse_origins) + len(challenge_origins)
            if (
                claim.origin is not Origin.GENERATED
                or expected_count != len(claim.origin_refs)
                or not parse_origins
                or not challenge_origins
                or not parse_origins <= parse_refs
                or not challenge_origins <= challenge_refs
            ):
                raise CoupledProposalValidationError(
                    "every combined claim requires exact parse and challenge origins"
                )
        if accepted_allocations is not None and set(accepted_allocations) != set(refs):
            raise CoupledProposalValidationError(
                "candidate generation receipt allocations are incomplete"
            )


def _require_synthetic_engines(engines: Sequence[AgentEngine]) -> list[AgentEngine]:
    values = list(engines)
    if not values or any(not isinstance(engine, MockEngine) for engine in values):
        raise StructuredDiscoveryError(
            "structured discovery wrappers are synthetic-only and require MockEngine"
        )
    return values


def run_structured_discovery_parse(
    runtime: ProjectRuntime,
    *,
    topic: str,
    document_paths: Sequence[Path],
    engines: Sequence[AgentEngine],
) -> StructuredDiscoveryRunResult:
    paths = list(document_paths)
    if not MIN_STRICT_DISCOVERY_DOCUMENTS <= len(paths) <= MAX_STRICT_DISCOVERY_DOCUMENTS:
        raise StructuredDiscoveryError(
            "structured discovery requires two or three documents"
        )
    invocation = ParseDeepResearchInvocation(topic=topic, document_paths=paths)
    adapter = StructuredDiscoveryParseAdapter(runtime.project_root, invocation)
    result = runtime.run(
        TaskType.PARSE_DEEP_RESEARCH,
        invocation,
        engines=_require_synthetic_engines(engines),
        promotion_adapter=adapter,
    )
    return StructuredDiscoveryRunResult(result, adapter.artifact_reference)


def run_structured_corpus_challenge(
    runtime: ProjectRuntime,
    *,
    topic: str,
    discovery_artifact: DiscoveryArtifactReference,
    engines: Sequence[AgentEngine],
) -> StructuredDiscoveryRunResult:
    if discovery_artifact.artifact_kind is not DiscoveryArtifactKind.PARSE:
        raise StructuredDiscoveryError("corpus challenge requires a parse artifact")
    load_discovery_artifact(runtime.project_root, discovery_artifact)
    generation, snapshot, _ = runtime.store.load_current()
    del generation
    papers = tuple(sorted(snapshot.papers, key=lambda item: item.paper_id))
    if len(papers) != STRICT_PAPER_COUNT:
        raise StructuredDiscoveryError(
            "structured corpus challenge requires exactly five canonical papers"
        )
    paths: list[Path] = []
    source_hashes: list[Sha256] = []
    for paper in papers:
        content, _ = read_contained_regular_file(
            runtime.project_root,
            paper.raw_md_path,
            max_bytes=MAX_DISCOVERY_RESOURCE_BYTES,
        )
        if hash_bytes(content) != paper.raw_md_hash:
            raise StructuredDiscoveryError(
                f"canonical raw Markdown differs for {paper.paper_id}"
            )
        paths.append(runtime.project_root / paper.raw_md_path)
        source_hashes.append(paper.source_hash)
    invocation = CorpusChallengerInvocation(
        topic=topic,
        paper_ids=[paper.paper_id for paper in papers],
        discovery_artifact=discovery_artifact.invocation(runtime.project_root),
        paper_resource_paths=paths,
        paper_source_hashes=source_hashes,
    )
    adapter = StructuredCorpusChallengeAdapter(runtime.project_root, invocation)
    result = runtime.run(
        TaskType.CORPUS_CHALLENGER,
        invocation,
        engines=_require_synthetic_engines(engines),
        promotion_adapter=adapter,
    )
    return StructuredDiscoveryRunResult(result, adapter.artifact_reference)


def run_structured_candidate_claims(
    runtime: ProjectRuntime,
    *,
    topic: str,
    discovery_artifact: DiscoveryArtifactReference,
    challenge_artifact: DiscoveryArtifactReference,
    engines: Sequence[AgentEngine],
    existing_theme_ids: Sequence[str] = (),
) -> StructuredDiscoveryRunResult:
    if (
        discovery_artifact.artifact_kind is not DiscoveryArtifactKind.PARSE
        or challenge_artifact.artifact_kind is not DiscoveryArtifactKind.CHALLENGE
    ):
        raise StructuredDiscoveryError(
            "candidate generation requires parse and challenge artifacts"
        )
    invocation = GenerateCandidateClaimsInvocation(
        topic=topic,
        existing_theme_ids=list(existing_theme_ids),
        discovery_artifact=discovery_artifact.invocation(runtime.project_root),
        challenge_artifact=challenge_artifact.invocation(runtime.project_root),
    )
    adapter = StructuredCandidateClaimsAdapter(runtime.project_root, invocation)
    result = runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        invocation,
        engines=_require_synthetic_engines(engines),
        promotion_adapter=adapter,
    )
    return StructuredDiscoveryRunResult(result, adapter.artifact_reference)


__all__ = [
    "AcceptedDiscoveryArtifact",
    "DISCOVERY_ADAPTER_VERSION",
    "DISCOVERY_ARTIFACT_ROOT",
    "DISCOVERY_ARTIFACT_VERSION",
    "DiscoveryArtifactKind",
    "DiscoveryArtifactReference",
    "PreparedDiscoveryArtifact",
    "StructuredCandidateClaimsAdapter",
    "StructuredCorpusChallengeAdapter",
    "StructuredDiscoveryError",
    "StructuredDiscoveryParseAdapter",
    "StructuredDiscoveryRunResult",
    "build_discovery_artifact",
    "load_discovery_artifact",
    "load_discovery_artifact_for_receipt",
    "run_structured_candidate_claims",
    "run_structured_corpus_challenge",
    "run_structured_discovery_parse",
]
