"""Immutable, generation-owned artifacts for accepted pre-canonical drafts.

The scientific repository deliberately has no persistent model for revised
claim, proposition, or rendered-sentence drafts.  This module keeps those
accepted task results on the runtime side of the boundary.  A record is
content addressed, written only through :class:`AuxiliaryStagingWriter`, and
therefore crosses the same generation transaction as its accepted-task
receipt without allocating a canonical scientific identifier.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vibereview.ids import Sha256

from .dto import (
    PropositionProposalBundle,
    RenderedSentenceProposalBundle,
    RevisedClaimProposal,
)
from .hashing import canonical_json_bytes, hash_bytes, hash_json
from .receipts import compute_semantic_task_key
from .records import (
    AppliedTaskReceipt,
    RuntimeModel,
    TaskProvenance,
    TaskSemanticFingerprint,
    TaskType,
)
from .repository import (
    AuxiliaryStagingWriter,
    GenerationStore,
    PromotionPayload,
    read_contained_regular_file,
)


DRAFT_ARTIFACT_FORMAT_VERSION = "1"
DRAFT_ARTIFACT_ROOT = "drafts/v1"
MAX_DRAFT_PROPOSAL_BYTES = 4 * 1024 * 1024
MAX_DRAFT_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_DRAFT_JSON_NODES = 50_000
MAX_DRAFT_JSON_DEPTH = 32
MAX_DRAFT_DEPENDENCIES = 4_096
MAX_DRAFT_RESOURCES = 1_024


class DraftArtifactError(ValueError):
    """An accepted draft is malformed, unbound, or not exactly recoverable."""


class DraftArtifactKind(StrEnum):
    REVISED_CLAIM = "revised_claim"
    PROPOSITION_DRAFT = "proposition_draft"
    RENDERED_SENTENCE_DRAFT = "rendered_sentence_draft"


_TASK_TYPE_BY_KIND: dict[DraftArtifactKind, TaskType] = {
    DraftArtifactKind.REVISED_CLAIM: TaskType.REVISE_CLAIM,
    DraftArtifactKind.PROPOSITION_DRAFT: TaskType.GENERATE_PROPOSITIONS,
    DraftArtifactKind.RENDERED_SENTENCE_DRAFT: TaskType.RENDER_PROSE,
}

_PROPOSAL_MODEL_BY_KIND: dict[DraftArtifactKind, type[BaseModel]] = {
    DraftArtifactKind.REVISED_CLAIM: RevisedClaimProposal,
    DraftArtifactKind.PROPOSITION_DRAFT: PropositionProposalBundle,
    DraftArtifactKind.RENDERED_SENTENCE_DRAFT: RenderedSentenceProposalBundle,
}


def _json_tree_budget(value: Any, *, label: str) -> None:
    """Bound JSON structure before hashing or model reconstruction."""

    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_DRAFT_JSON_NODES:
            raise DraftArtifactError(f"{label} exceeds its JSON node limit")
        if depth > MAX_DRAFT_JSON_DEPTH:
            raise DraftArtifactError(f"{label} exceeds its JSON depth limit")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise DraftArtifactError(f"{label} contains a non-string JSON key")
            nodes += len(item)
            if nodes > MAX_DRAFT_JSON_NODES:
                raise DraftArtifactError(f"{label} exceeds its JSON node limit")
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise DraftArtifactError(f"{label} contains a non-finite number")
        elif not isinstance(item, (str, int, float, bool, type(None))):
            raise DraftArtifactError(f"{label} contains a non-JSON value")


def _canonical_proposal_payload(
    artifact_kind: DraftArtifactKind,
    proposal: BaseModel | Mapping[str, Any],
) -> dict[str, Any]:
    raw: Any
    if isinstance(proposal, BaseModel):
        raw = proposal.model_dump(mode="json")
    else:
        raw = dict(proposal)
    _json_tree_budget(raw, label="draft proposal")
    raw_bytes = canonical_json_bytes(raw)
    if len(raw_bytes) > MAX_DRAFT_PROPOSAL_BYTES:
        raise DraftArtifactError("draft proposal exceeds its byte limit")
    try:
        validated = _PROPOSAL_MODEL_BY_KIND[artifact_kind].model_validate(raw)
    except Exception as exc:
        raise DraftArtifactError(
            f"proposal does not match {artifact_kind.value}"
        ) from exc
    canonical = validated.model_dump(mode="json")
    canonical_bytes = canonical_json_bytes(canonical)
    if raw_bytes != canonical_bytes:
        raise DraftArtifactError("draft proposal payload is not canonical")
    return json.loads(canonical_bytes)


def _draft_local_refs(
    artifact_kind: DraftArtifactKind, proposal_payload: Mapping[str, Any]
) -> tuple[str, ...]:
    if artifact_kind is DraftArtifactKind.REVISED_CLAIM:
        local_refs = (str(proposal_payload["claim_ref"]),)
    elif artifact_kind is DraftArtifactKind.PROPOSITION_DRAFT:
        local_refs = tuple(
            str(item["local_ref"]) for item in proposal_payload["propositions"]
        )
    else:
        local_refs = tuple(
            str(item["local_ref"]) for item in proposal_payload["sentences"]
        )
    if len(local_refs) != len(set(local_refs)):
        raise DraftArtifactError("draft local references must be unique")
    return local_refs


def _semantic_fingerprint_hash(
    fingerprint: TaskSemanticFingerprint,
) -> str:
    return hash_json(
        {
            "validator_fingerprint": fingerprint.validator_fingerprint,
            "promotion_handler_fingerprint": (
                fingerprint.promotion_handler_fingerprint
            ),
            "disposition_handler_fingerprint": (
                fingerprint.disposition_handler_fingerprint
            ),
            "scientific_contract_version": (
                fingerprint.scientific_contract_version
            ),
            "runtime_contract_version": fingerprint.runtime_contract_version,
        }
    )


def _resource_hashes(provenance: TaskProvenance) -> dict[str, str]:
    resource_ids = [resource.resource_id for resource in provenance.resources]
    if len(resource_ids) != len(set(resource_ids)):
        raise DraftArtifactError("task provenance has duplicate resource identifiers")
    for resource in provenance.resources:
        if resource.source_dependency.source_hash_at_snapshot != resource.snapshot_hash:
            raise DraftArtifactError(
                f"resource {resource.resource_id} source and snapshot hashes differ"
            )
    return dict(
        sorted(
            (resource.resource_id, resource.snapshot_hash)
            for resource in provenance.resources
        )
    )


class AcceptedDraftArtifact(RuntimeModel):
    """Self-verifying acceptance record stored outside scientific state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: Literal["1"] = DRAFT_ARTIFACT_FORMAT_VERSION
    artifact_kind: DraftArtifactKind
    owner_generation: int = Field(ge=1)
    source_generation: int = Field(ge=0)
    task_id: str = Field(pattern=r"^TASK[0-9]{4,}$")
    task_type: TaskType
    task_spec_version: str
    task_provenance_hash: Sha256
    task_provenance: TaskProvenance
    semantic_fingerprint: TaskSemanticFingerprint
    input_identity_key: Sha256
    semantic_task_key: Sha256
    proposal_hash: Sha256
    proposal_payload: dict[str, Any]
    draft_local_refs: tuple[str, ...]
    dependency_hashes: dict[str, Sha256]
    resource_hashes: dict[str, Sha256]
    acceptance_receipt: AppliedTaskReceipt

    @model_validator(mode="after")
    def _bindings_are_exact(self) -> "AcceptedDraftArtifact":
        expected_task_type = _TASK_TYPE_BY_KIND[self.artifact_kind]
        provenance = self.task_provenance
        receipt = self.acceptance_receipt

        if self.task_type is not expected_task_type:
            raise ValueError("draft kind and task type differ")
        if provenance.task_type is not expected_task_type:
            raise ValueError("draft kind and task provenance type differ")
        if receipt.task_type is not expected_task_type:
            raise ValueError("draft kind and accepted receipt type differ")
        if self.task_id != provenance.task_id:
            raise ValueError("draft task identifier differs from task provenance")
        if not receipt.accepted_attempt_id.startswith(f"{self.task_id}/"):
            raise ValueError("accepted attempt is not owned by the draft task")
        if receipt.accepted_attempt_id == f"{self.task_id}/":
            raise ValueError("accepted attempt identifier has an empty suffix")
        if not (
            self.task_spec_version
            == provenance.task_spec_version
            == receipt.task_spec_version
        ):
            raise ValueError("draft task-spec versions differ")
        if not (
            self.source_generation
            == provenance.base_generation
            == receipt.source_generation
        ):
            raise ValueError("draft source generations differ")
        if self.owner_generation != receipt.committed_generation:
            raise ValueError("draft owner differs from receipt commit generation")
        if self.owner_generation != self.source_generation + 1:
            raise ValueError("draft is not owned by the next immutable generation")

        expected_provenance_hash = hash_json(provenance.model_dump(mode="json"))
        if self.task_provenance_hash != expected_provenance_hash:
            raise ValueError("draft task provenance hash mismatch")
        if self.semantic_fingerprint != receipt.semantic_fingerprint:
            raise ValueError("draft semantic fingerprints differ")
        if (
            self.semantic_fingerprint.combined_fingerprint
            != _semantic_fingerprint_hash(self.semantic_fingerprint)
        ):
            raise ValueError("draft semantic fingerprint is internally inconsistent")
        if self.input_identity_key != receipt.input_identity_key:
            raise ValueError("draft input identity differs from accepted receipt")
        expected_semantic_key = compute_semantic_task_key(
            input_identity_key=self.input_identity_key,
            semantic_fingerprint=self.semantic_fingerprint,
        )
        if not (
            self.semantic_task_key
            == receipt.semantic_task_key
            == expected_semantic_key
        ):
            raise ValueError("draft semantic task key mismatch")

        canonical_proposal = _canonical_proposal_payload(
            self.artifact_kind, self.proposal_payload
        )
        expected_proposal_hash = hash_json(canonical_proposal)
        if not (
            self.proposal_hash
            == receipt.proposal_hash
            == expected_proposal_hash
        ):
            raise ValueError("draft proposal hash mismatch")
        if canonical_json_bytes(canonical_proposal) != canonical_json_bytes(
            receipt.proposal_payload
        ):
            raise ValueError("draft proposal differs from accepted receipt")
        if self.draft_local_refs != _draft_local_refs(
            self.artifact_kind, canonical_proposal
        ):
            raise ValueError("draft local-reference index differs from its proposal")

        expected_dependencies = dict(sorted(provenance.dependencies.items()))
        if self.dependency_hashes != expected_dependencies:
            raise ValueError("draft dependency hashes differ from task provenance")
        expected_resources = _resource_hashes(provenance)
        if self.resource_hashes != expected_resources:
            raise ValueError("draft resource hashes differ from task provenance")
        if len(self.dependency_hashes) > MAX_DRAFT_DEPENDENCIES:
            raise ValueError("draft exceeds its dependency limit")
        if len(self.resource_hashes) > MAX_DRAFT_RESOURCES:
            raise ValueError("draft exceeds its resource limit")

        transition = receipt.recorded_transition
        if (
            transition.scientific_disposition != "VALID"
            or transition.canonicalized
            or not transition.downstream_eligible
            or transition.human_review_required
        ):
            raise ValueError("receipt is not an accepted pre-canonical draft")
        if receipt.canonical_objects or receipt.local_ref_map:
            raise ValueError("a draft receipt cannot claim canonical promotion")
        return self


def _draft_relative_path(
    artifact_kind: DraftArtifactKind, artifact_hash: str
) -> str:
    digest = artifact_hash.removeprefix("sha256:")
    return f"{DRAFT_ARTIFACT_ROOT}/{artifact_kind.value}/{digest}.json"


class DraftArtifactReference(RuntimeModel):
    """Caller-retained exact anchor for a generation-owned draft."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_kind: DraftArtifactKind
    artifact_hash: Sha256
    relative_path: str
    owner_generation: int = Field(ge=1)
    source_generation: int = Field(ge=0)
    task_id: str = Field(pattern=r"^TASK[0-9]{4,}$")
    draft_local_refs: tuple[str, ...]

    @model_validator(mode="after")
    def _path_is_content_addressed(self) -> "DraftArtifactReference":
        if self.relative_path != _draft_relative_path(
            self.artifact_kind, self.artifact_hash
        ):
            raise ValueError("draft reference path is not content addressed")
        if self.owner_generation != self.source_generation + 1:
            raise ValueError("draft reference generation lineage is invalid")
        if len(self.draft_local_refs) != len(set(self.draft_local_refs)):
            raise ValueError("draft reference local identifiers are not unique")
        return self

    def anchor(
        self, project_root: Path, draft_local_ref: str
    ) -> "DraftArtifactAnchor":
        """Select one exact item without allowing a caller-selected file path."""

        return draft_artifact_anchor(project_root, self, draft_local_ref)


class DraftArtifactAnchor(RuntimeModel):
    """Private invocation fields identifying one item in one exact draft."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    draft_kind: DraftArtifactKind
    draft_owner_generation: int = Field(ge=1)
    draft_source_generation: int = Field(ge=0)
    draft_task_id: str = Field(pattern=r"^TASK[0-9]{4,}$")
    draft_artifact_hash: Sha256
    draft_artifact_path: Path
    draft_local_ref: str = Field(min_length=1)

    @model_validator(mode="after")
    def _path_is_derived_from_content(self) -> "DraftArtifactAnchor":
        if self.draft_owner_generation != self.draft_source_generation + 1:
            raise ValueError("draft invocation generation lineage is invalid")
        if not self.draft_artifact_path.is_absolute():
            raise ValueError("draft invocation resource path must be absolute")
        if ".." in self.draft_artifact_path.parts:
            raise ValueError("draft invocation resource path contains '..'")
        expected_suffix = Path(
            "state",
            "generations",
            GenerationStore.generation_name(self.draft_owner_generation),
            "auxiliary",
            _draft_relative_path(
                self.draft_kind, self.draft_artifact_hash
            ),
        )
        suffix_parts = expected_suffix.parts
        if self.draft_artifact_path.parts[-len(suffix_parts) :] != suffix_parts:
            raise ValueError("draft invocation path is not content addressed")
        return self


def draft_resource_path(
    project_root: Path, reference: DraftArtifactReference
) -> Path:
    """Return the absolute immutable source path used by TaskResourceRequest."""

    root = Path(project_root)
    if not root.is_absolute():
        raise DraftArtifactError("draft resource project root must be absolute")
    try:
        exact_reference = DraftArtifactReference.model_validate(
            reference.model_dump(mode="json")
        )
    except Exception as exc:
        raise DraftArtifactError("draft reference is invalid") from exc
    return (
        root
        / "state"
        / "generations"
        / GenerationStore.generation_name(exact_reference.owner_generation)
        / "auxiliary"
        / exact_reference.relative_path
    )


def draft_artifact_anchor(
    project_root: Path,
    reference: DraftArtifactReference,
    draft_local_ref: str,
) -> DraftArtifactAnchor:
    """Derive TaskSpec private-invocation anchors from a validated reference."""

    try:
        exact_reference = DraftArtifactReference.model_validate(
            reference.model_dump(mode="json")
        )
    except Exception as exc:
        raise DraftArtifactError("draft reference is invalid") from exc
    if draft_local_ref not in exact_reference.draft_local_refs:
        raise DraftArtifactError("draft local reference is not present in the artifact")
    return DraftArtifactAnchor(
        draft_kind=exact_reference.artifact_kind,
        draft_owner_generation=exact_reference.owner_generation,
        draft_source_generation=exact_reference.source_generation,
        draft_task_id=exact_reference.task_id,
        draft_artifact_hash=exact_reference.artifact_hash,
        draft_artifact_path=draft_resource_path(project_root, exact_reference),
        draft_local_ref=draft_local_ref,
    )


@dataclass(frozen=True, slots=True)
class PreparedDraftArtifact:
    artifact: AcceptedDraftArtifact
    reference: DraftArtifactReference
    content: bytes


class DraftArtifactReceiptIndex(RuntimeModel):
    """Deterministic receipt-key lookup for zero-cost artifact reuse."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: Literal["1"] = DRAFT_ARTIFACT_FORMAT_VERSION
    semantic_task_key: Sha256
    reference: DraftArtifactReference


def _draft_receipt_index_path(semantic_task_key: str) -> str:
    return (
        f"{DRAFT_ARTIFACT_ROOT}/by-receipt/"
        f"{semantic_task_key.removeprefix('sha256:')}.json"
    )


def _draft_receipt_index_bytes(
    prepared: PreparedDraftArtifact,
) -> tuple[str, bytes]:
    index = DraftArtifactReceiptIndex(
        semantic_task_key=prepared.artifact.semantic_task_key,
        reference=prepared.reference,
    )
    content = canonical_json_bytes(index.model_dump(mode="json")) + b"\n"
    return _draft_receipt_index_path(index.semantic_task_key), content


def _serialize_draft_artifact(artifact: AcceptedDraftArtifact) -> bytes:
    payload = artifact.model_dump(mode="json")
    _json_tree_budget(payload, label="draft artifact")
    content = canonical_json_bytes(payload) + b"\n"
    if len(content) > MAX_DRAFT_ARTIFACT_BYTES:
        raise DraftArtifactError("draft artifact exceeds its byte limit")
    return content


def build_draft_artifact(
    *,
    artifact_kind: DraftArtifactKind | str,
    task_provenance: TaskProvenance,
    acceptance_receipt: AppliedTaskReceipt,
    proposal: BaseModel | Mapping[str, Any] | None = None,
) -> PreparedDraftArtifact:
    """Build deterministic bytes and their exact generation-owned reference."""

    try:
        kind = DraftArtifactKind(artifact_kind)
    except ValueError as exc:
        raise DraftArtifactError(f"unsupported draft artifact kind {artifact_kind!r}") from exc
    provenance = TaskProvenance.model_validate(
        task_provenance.model_dump(mode="json")
    )
    receipt = AppliedTaskReceipt.model_validate(
        acceptance_receipt.model_dump(mode="json")
    )
    proposal_payload = _canonical_proposal_payload(
        kind, proposal if proposal is not None else receipt.proposal_payload
    )
    if canonical_json_bytes(proposal_payload) != canonical_json_bytes(
        receipt.proposal_payload
    ):
        raise DraftArtifactError("materialized proposal differs from accepted receipt")

    try:
        artifact = AcceptedDraftArtifact(
            artifact_kind=kind,
            owner_generation=receipt.committed_generation,
            source_generation=receipt.source_generation,
            task_id=provenance.task_id,
            task_type=receipt.task_type,
            task_spec_version=receipt.task_spec_version,
            task_provenance_hash=hash_json(provenance.model_dump(mode="json")),
            task_provenance=provenance,
            semantic_fingerprint=receipt.semantic_fingerprint,
            input_identity_key=receipt.input_identity_key,
            semantic_task_key=receipt.semantic_task_key,
            proposal_hash=receipt.proposal_hash,
            proposal_payload=proposal_payload,
            draft_local_refs=_draft_local_refs(kind, proposal_payload),
            dependency_hashes=dict(sorted(provenance.dependencies.items())),
            resource_hashes=_resource_hashes(provenance),
            acceptance_receipt=receipt,
        )
    except DraftArtifactError:
        raise
    except Exception as exc:
        raise DraftArtifactError("accepted draft bindings are invalid") from exc
    content = _serialize_draft_artifact(artifact)
    artifact_hash = hash_bytes(content)
    try:
        reference = DraftArtifactReference(
            artifact_kind=kind,
            artifact_hash=artifact_hash,
            relative_path=_draft_relative_path(kind, artifact_hash),
            owner_generation=artifact.owner_generation,
            source_generation=artifact.source_generation,
            task_id=artifact.task_id,
            draft_local_refs=artifact.draft_local_refs,
        )
    except Exception as exc:
        raise DraftArtifactError("accepted draft reference is invalid") from exc
    return PreparedDraftArtifact(artifact, reference, content)


def materialize_draft_artifact(
    writer: AuxiliaryStagingWriter,
    next_generation: int,
    promotion_payload: PromotionPayload,
    acceptance_receipt: AppliedTaskReceipt,
    *,
    artifact_kind: DraftArtifactKind | str,
    task_provenance: TaskProvenance,
    proposal: BaseModel | Mapping[str, Any] | None = None,
) -> DraftArtifactReference:
    """Write one draft through the confined generation staging writer."""

    if promotion_payload.allocated_ids:
        raise DraftArtifactError("pre-canonical draft materialization allocated identifiers")
    prepared = build_draft_artifact(
        artifact_kind=artifact_kind,
        task_provenance=task_provenance,
        acceptance_receipt=acceptance_receipt,
        proposal=proposal,
    )
    if prepared.reference.owner_generation != next_generation:
        raise DraftArtifactError("draft receipt is not owned by the staging generation")
    writer.write_bytes(prepared.reference.relative_path, prepared.content)
    index_path, index_content = _draft_receipt_index_bytes(prepared)
    writer.write_bytes(index_path, index_content)
    return prepared.reference


AcceptedDraftMaterializer = Callable[
    [AuxiliaryStagingWriter, int, PromotionPayload, AppliedTaskReceipt], None
]


def make_draft_artifact_materializer(
    *,
    artifact_kind: DraftArtifactKind | str,
    task_provenance: TaskProvenance,
    proposal: BaseModel | Mapping[str, Any],
) -> AcceptedDraftMaterializer:
    """Return an ``AuxiliaryStagingWriter``/coupled-plan compatible callback."""

    try:
        kind = DraftArtifactKind(artifact_kind)
    except ValueError as exc:
        raise DraftArtifactError(f"unsupported draft artifact kind {artifact_kind!r}") from exc
    provenance = TaskProvenance.model_validate(
        task_provenance.model_dump(mode="json")
    )
    if provenance.task_type is not _TASK_TYPE_BY_KIND[kind]:
        raise DraftArtifactError("draft kind and task provenance type differ")
    frozen_provenance = canonical_json_bytes(provenance.model_dump(mode="json"))
    frozen_proposal = canonical_json_bytes(_canonical_proposal_payload(kind, proposal))

    def materialize(
        writer: AuxiliaryStagingWriter,
        next_generation: int,
        promotion_payload: PromotionPayload,
        acceptance_receipt: AppliedTaskReceipt,
    ) -> None:
        materialize_draft_artifact(
            writer,
            next_generation,
            promotion_payload,
            acceptance_receipt,
            artifact_kind=kind,
            task_provenance=TaskProvenance.model_validate_json(frozen_provenance),
            proposal=json.loads(frozen_proposal),
        )

    return materialize


def _decode_draft_artifact(
    content: bytes, reference: DraftArtifactReference
) -> AcceptedDraftArtifact:
    if len(content) > MAX_DRAFT_ARTIFACT_BYTES:
        raise DraftArtifactError("draft artifact exceeds its byte limit")
    if hash_bytes(content) != reference.artifact_hash:
        raise DraftArtifactError("draft artifact content hash mismatch")
    try:
        artifact = AcceptedDraftArtifact.model_validate_json(content)
    except Exception as exc:
        raise DraftArtifactError("draft artifact is invalid") from exc
    if _serialize_draft_artifact(artifact) != content:
        raise DraftArtifactError("draft artifact bytes are not canonical")
    if (
        artifact.artifact_kind is not reference.artifact_kind
        or artifact.owner_generation != reference.owner_generation
        or artifact.source_generation != reference.source_generation
        or artifact.task_id != reference.task_id
        or artifact.draft_local_refs != reference.draft_local_refs
    ):
        raise DraftArtifactError("draft artifact differs from its exact reference")
    return artifact


def load_draft_artifact(
    project_root: Path,
    reference: DraftArtifactReference,
    *,
    expected_task_provenance: TaskProvenance | None = None,
    expected_receipt: AppliedTaskReceipt | None = None,
) -> AcceptedDraftArtifact:
    """Load and verify one exact artifact through anchored no-follow reads."""

    try:
        anchor = DraftArtifactReference.model_validate(
            reference.model_dump(mode="json")
        )
        store = GenerationStore(project_root)
        _, _, selected = store.load_generation_auxiliary(
            anchor.owner_generation, {anchor.relative_path}
        )
        try:
            selected_content = selected[anchor.relative_path]
        except KeyError as exc:
            raise DraftArtifactError(
                "generation does not contain the referenced draft"
            ) from exc

        generation_name = GenerationStore.generation_name(anchor.owner_generation)
        exact_content, _ = read_contained_regular_file(
            Path(project_root),
            (
                f"state/generations/{generation_name}/auxiliary/"
                f"{anchor.relative_path}"
            ),
            max_bytes=MAX_DRAFT_ARTIFACT_BYTES,
        )
        if exact_content != selected_content:
            raise DraftArtifactError("draft changed during anchored verification")
        artifact = _decode_draft_artifact(exact_content, anchor)
    except DraftArtifactError:
        raise
    except Exception as exc:
        raise DraftArtifactError("draft artifact could not be verified") from exc

    if expected_task_provenance is not None:
        expected = TaskProvenance.model_validate(
            expected_task_provenance.model_dump(mode="json")
        )
        if canonical_json_bytes(expected.model_dump(mode="json")) != canonical_json_bytes(
            artifact.task_provenance.model_dump(mode="json")
        ):
            raise DraftArtifactError("draft does not match expected task provenance")
    if expected_receipt is not None:
        expected = AppliedTaskReceipt.model_validate(
            expected_receipt.model_dump(mode="json")
        )
        if canonical_json_bytes(expected.model_dump(mode="json")) != canonical_json_bytes(
            artifact.acceptance_receipt.model_dump(mode="json")
        ):
            raise DraftArtifactError("draft does not match expected accepted receipt")
    return artifact


def verify_draft_artifact(
    project_root: Path,
    reference: DraftArtifactReference,
    *,
    expected_task_provenance: TaskProvenance | None = None,
    expected_receipt: AppliedTaskReceipt | None = None,
) -> AcceptedDraftArtifact:
    """Verification alias emphasizing that every load is exact and fail closed."""

    return load_draft_artifact(
        project_root,
        reference,
        expected_task_provenance=expected_task_provenance,
        expected_receipt=expected_receipt,
    )


def load_draft_artifact_for_receipt(
    project_root: Path,
    receipt: AppliedTaskReceipt,
) -> tuple[AcceptedDraftArtifact, DraftArtifactReference]:
    """Resolve an accepted artifact by its immutable semantic receipt key."""

    exact_receipt = AppliedTaskReceipt.model_validate(
        receipt.model_dump(mode="json")
    )
    index_path = _draft_receipt_index_path(exact_receipt.semantic_task_key)
    try:
        _, _, selected = GenerationStore(project_root).load_generation_auxiliary(
            exact_receipt.committed_generation, {index_path}
        )
        content = selected[index_path]
        index = DraftArtifactReceiptIndex.model_validate_json(content)
    except Exception as exc:
        raise DraftArtifactError("draft receipt index could not be verified") from exc
    if canonical_json_bytes(index.model_dump(mode="json")) + b"\n" != content:
        raise DraftArtifactError("draft receipt index bytes are not canonical")
    if index.semantic_task_key != exact_receipt.semantic_task_key:
        raise DraftArtifactError("draft receipt index semantic key mismatch")
    if index.reference.owner_generation != exact_receipt.committed_generation:
        raise DraftArtifactError("draft receipt index generation mismatch")
    artifact = load_draft_artifact(
        Path(project_root),
        index.reference,
        expected_receipt=exact_receipt,
    )
    return artifact, index.reference


def load_draft_artifact_for_anchor(
    project_root: Path,
    anchor: DraftArtifactAnchor,
) -> tuple[AcceptedDraftArtifact, DraftArtifactReference]:
    """Recover and verify the complete reference behind a private task anchor."""

    root = Path(project_root)
    if not root.is_absolute():
        raise DraftArtifactError("draft resource project root must be absolute")
    try:
        exact_anchor = DraftArtifactAnchor.model_validate(
            anchor.model_dump(mode="json")
        )
    except Exception as exc:
        raise DraftArtifactError("draft invocation anchor is invalid") from exc
    relative_path = _draft_relative_path(
        exact_anchor.draft_kind, exact_anchor.draft_artifact_hash
    )
    expected_path = (
        root
        / "state"
        / "generations"
        / GenerationStore.generation_name(exact_anchor.draft_owner_generation)
        / "auxiliary"
        / relative_path
    )
    if exact_anchor.draft_artifact_path != expected_path:
        raise DraftArtifactError("draft invocation path belongs to another project")
    generation_name = GenerationStore.generation_name(
        exact_anchor.draft_owner_generation
    )
    try:
        content, _ = read_contained_regular_file(
            root,
            f"state/generations/{generation_name}/auxiliary/{relative_path}",
            max_bytes=MAX_DRAFT_ARTIFACT_BYTES,
        )
        if hash_bytes(content) != exact_anchor.draft_artifact_hash:
            raise DraftArtifactError("draft artifact content hash mismatch")
        artifact = AcceptedDraftArtifact.model_validate_json(content)
        if _serialize_draft_artifact(artifact) != content:
            raise DraftArtifactError("draft artifact bytes are not canonical")
        reference = DraftArtifactReference(
            artifact_kind=artifact.artifact_kind,
            artifact_hash=exact_anchor.draft_artifact_hash,
            relative_path=relative_path,
            owner_generation=artifact.owner_generation,
            source_generation=artifact.source_generation,
            task_id=artifact.task_id,
            draft_local_refs=artifact.draft_local_refs,
        )
    except DraftArtifactError:
        raise
    except Exception as exc:
        raise DraftArtifactError("draft artifact anchor could not be read") from exc
    if (
        reference.artifact_kind is not exact_anchor.draft_kind
        or reference.owner_generation != exact_anchor.draft_owner_generation
        or reference.source_generation != exact_anchor.draft_source_generation
        or reference.task_id != exact_anchor.draft_task_id
        or exact_anchor.draft_local_ref not in reference.draft_local_refs
    ):
        raise DraftArtifactError("draft artifact differs from its private task anchor")
    verified = load_draft_artifact(
        root,
        reference,
        expected_task_provenance=artifact.task_provenance,
        expected_receipt=artifact.acceptance_receipt,
    )
    return verified, reference


class DraftArtifactStore:
    """Small project-root-bound facade for exact draft artifact reads."""

    __slots__ = ("project_root",)

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root)

    def load(
        self,
        reference: DraftArtifactReference,
        *,
        expected_task_provenance: TaskProvenance | None = None,
        expected_receipt: AppliedTaskReceipt | None = None,
    ) -> AcceptedDraftArtifact:
        return load_draft_artifact(
            self.project_root,
            reference,
            expected_task_provenance=expected_task_provenance,
            expected_receipt=expected_receipt,
        )

    def verify(
        self,
        reference: DraftArtifactReference,
        *,
        expected_task_provenance: TaskProvenance | None = None,
        expected_receipt: AppliedTaskReceipt | None = None,
    ) -> AcceptedDraftArtifact:
        return self.load(
            reference,
            expected_task_provenance=expected_task_provenance,
            expected_receipt=expected_receipt,
        )
