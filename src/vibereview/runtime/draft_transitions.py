"""Fixed coupled adapters for accepted drafts and their semantic audits.

These adapters are runtime plumbing only.  Draft-generating tasks commit an
accepted, content-addressed auxiliary artifact without changing canonical
scientific state.  Audit tasks recover that exact artifact, commit the required
canonical transition when eligible, and always retain a receipt/source-indexed
audit witness in the same generation transaction.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path, PurePosixPath
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vibereview.enums import AuditDisposition, AuditProvenanceVerdict
from vibereview.ids import Sha256
from vibereview.models import (
    CitationBinding,
    PropositionRecord,
    RenderedSentence,
    RenderedSentenceAudit,
    SemanticAuditResult,
)
from vibereview.validators import derive_semantic_audit_disposition

from .coupled import CoupledCommitPlan, CoupledProposalValidationError
from .drafts import (
    DRAFT_ARTIFACT_ROOT,
    MAX_DRAFT_ARTIFACT_BYTES,
    AcceptedDraftArtifact,
    DraftArtifactAnchor,
    DraftArtifactError,
    DraftArtifactKind,
    DraftArtifactReference,
    build_draft_artifact,
    load_draft_artifact,
    load_draft_artifact_for_anchor,
    load_draft_artifact_for_receipt,
    make_draft_artifact_materializer,
)
from .dto import (
    AuditPropositionInvocation,
    AuditRenderedSentenceInvocation,
    GeneratePropositionsInvocation,
    PropositionProposal,
    PropositionProposalBundle,
    RenderProseInvocation,
    RenderedSentenceAuditProposal,
    RenderedSentenceProposal,
    RenderedSentenceProposalBundle,
    RevisedClaimProposal,
    ReviseClaimInvocation,
    SemanticAuditProposal,
)
from .hashing import (
    canonical_json_bytes,
    code_fingerprint,
    hash_bytes,
    hash_json,
)
from .receipts import compute_semantic_task_key, verify_receipt_canonical_objects
from .records import (
    AppliedTaskReceipt,
    RuntimeModel,
    TaskManifest,
    TaskProvenance,
    TaskSemanticFingerprint,
    TaskSpec,
    TaskType,
    resource_destination,
)
from .registry import CanonicalIdRegistry, IdKind
from .repository import (
    AuxiliaryStagingWriter,
    GenerationStore,
    PromotionPayload,
    StaleSnapshotError,
    read_contained_regular_file,
)
from .state import RepositorySnapshot


DRAFT_TRANSITION_ADAPTER_VERSION = "1"
DRAFT_AUDIT_ARTIFACT_VERSION = "1"
DRAFT_AUDIT_ARTIFACT_ROOT = "draft-audits/v1"
MAX_DRAFT_AUDIT_INDEX_BYTES = 16 * 1024
MAX_DRAFT_AUDIT_GENERATION_SCAN = 4_096


class DraftAuditArtifactKind(StrEnum):
    PROPOSITION = "proposition"
    RENDERED_SENTENCE = "rendered_sentence"


def _is_canonical_bundle_path(value: str) -> bool:
    parsed = PurePosixPath(value)
    return (
        bool(value)
        and not parsed.is_absolute()
        and parsed.as_posix() == value
        and "\\" not in value
        and all(component not in {"", ".", ".."} for component in parsed.parts)
    )


class _DraftAuditResourceSourceDependencyWitness(RuntimeModel):
    """Hash-only source freshness anchor safe for a generation artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["external_file"] = "external_file"
    source_hash_at_snapshot: Sha256


class _DraftAuditResourceProvenanceWitness(RuntimeModel):
    """Authenticated resource metadata with the operator path removed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resource_id: str = Field(pattern=r"^RES[0-9]{4,}$")
    logical_name: str
    media_type: str
    bundle_relative_path: Path
    source_dependency: _DraftAuditResourceSourceDependencyWitness
    snapshot_hash: Sha256
    size_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def _bundle_path_is_safe(self) -> "_DraftAuditResourceProvenanceWitness":
        if not _is_canonical_bundle_path(self.bundle_relative_path.as_posix()):
            raise ValueError("draft audit resource bundle path is unsafe")
        return self


class _DraftAuditTaskProvenanceWitness(RuntimeModel):
    """Path-free copy of the immutable task trust anchor."""

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
    resources: tuple[_DraftAuditResourceProvenanceWitness, ...] = ()

    @classmethod
    def from_private(
        cls,
        provenance: TaskProvenance | "_DraftAuditTaskProvenanceWitness",
    ) -> "_DraftAuditTaskProvenanceWitness":
        """Copy receipt-relevant provenance without persisting source paths."""

        if isinstance(provenance, cls):
            return cls.model_validate(provenance.model_dump(mode="json"))
        return cls(
            task_id=provenance.task_id,
            task_type=provenance.task_type,
            task_spec_version=provenance.task_spec_version,
            prompt_version=provenance.prompt_version,
            base_generation=provenance.base_generation,
            dependencies=dict(sorted(provenance.dependencies.items())),
            instructions_hash=provenance.instructions_hash,
            input_snapshot_hash=provenance.input_snapshot_hash,
            engine_input_hash=provenance.engine_input_hash,
            input_schema_hash=provenance.input_schema_hash,
            proposal_schema_hash=provenance.proposal_schema_hash,
            expected_bundle_manifest_hash=provenance.expected_bundle_manifest_hash,
            expected_immutable_files=dict(
                sorted(provenance.expected_immutable_files.items())
            ),
            resources=tuple(
                _DraftAuditResourceProvenanceWitness(
                    resource_id=resource.resource_id,
                    logical_name=resource.logical_name,
                    media_type=resource.media_type,
                    bundle_relative_path=resource.bundle_relative_path,
                    source_dependency=(
                        _DraftAuditResourceSourceDependencyWitness(
                            source_hash_at_snapshot=(
                                resource.source_dependency.source_hash_at_snapshot
                            )
                        )
                    ),
                    snapshot_hash=resource.snapshot_hash,
                    size_bytes=resource.size_bytes,
                )
                for resource in provenance.resources
            ),
        )

    @model_validator(mode="after")
    def _paths_and_resources_are_safe(self) -> "_DraftAuditTaskProvenanceWitness":
        if any(
            not _is_canonical_bundle_path(path)
            for path in self.expected_immutable_files
        ):
            raise ValueError("draft audit immutable bundle path is unsafe")
        resource_ids = tuple(resource.resource_id for resource in self.resources)
        if len(resource_ids) != len(set(resource_ids)):
            raise ValueError("draft audit resource identifiers must be unique")
        return self


def _same_draft_audit_replay_inputs(
    accepted: _DraftAuditTaskProvenanceWitness,
    current: TaskProvenance,
) -> bool:
    """Compare every immutable task input while allowing replay task lineage."""

    replay = _DraftAuditTaskProvenanceWitness.from_private(current)
    excluded = {"task_id", "base_generation"}
    return accepted.model_dump(mode="json", exclude=excluded) == replay.model_dump(
        mode="json", exclude=excluded
    )


def _adapter_fingerprint(handler_name: str) -> str:
    return code_fingerprint(
        [Path(__file__), Path(__file__).with_name("drafts.py")],
        f"draft-transition:{DRAFT_TRANSITION_ADAPTER_VERSION}:{handler_name}",
    )


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


def _require_current_generation(
    project_root: Path, manifest: TaskManifest, provenance: TaskProvenance
) -> None:
    if provenance.task_id != manifest.task_id:
        raise ValueError("task provenance and manifest identifiers differ")
    if provenance.base_generation != manifest.base_generation:
        raise ValueError("task provenance and manifest generations differ")
    current = GenerationStore(project_root).current_generation()
    if current != manifest.base_generation:
        raise StaleSnapshotError(
            f"task generation {manifest.base_generation} is stale; current is {current}"
        )


def _require_empty_allocations(
    accepted_allocations: Mapping[str, str] | None,
) -> None:
    if accepted_allocations is not None and dict(accepted_allocations):
        raise CoupledProposalValidationError(
            "a pre-canonical draft receipt cannot contain canonical allocations"
        )


def _typed_ids(provenance: TaskProvenance, model_name: str) -> tuple[str, ...]:
    prefix = f"{model_name}:"
    return tuple(
        sorted(
            dependency.removeprefix(prefix)
            for dependency in provenance.dependencies
            if dependency.startswith(prefix)
        )
    )


def _same_allowlist(actual: list[str], expected: tuple[str, ...], label: str) -> None:
    if tuple(sorted(actual)) != expected:
        raise CoupledProposalValidationError(
            f"{label} must exactly match the accepted draft source allowlist"
        )


def _require_source_dependencies(
    source_artifact: AcceptedDraftArtifact,
    audit_dependencies: Mapping[str, str],
) -> None:
    if dict(sorted(audit_dependencies.items())) != source_artifact.dependency_hashes:
        raise DraftArtifactError(
            "audit dependency hashes differ from the accepted draft dependencies"
        )


def _validate_citation_authorization(
    proposition: PropositionProposal,
    *,
    snapshot: RepositorySnapshot,
    allowed_claim_ids: set[str],
    allowed_corpus_fact_ids: set[str],
    allowed_process_fact_ids: set[str],
) -> None:
    if not set(proposition.claim_refs) <= allowed_claim_ids:
        raise CoupledProposalValidationError(
            "proposition claim_refs exceed the exact ClaimPacket allowlist"
        )
    if not set(proposition.corpus_fact_refs) <= allowed_corpus_fact_ids:
        raise CoupledProposalValidationError(
            "proposition corpus_fact_refs exceed the exact corpus-fact allowlist"
        )
    if not set(proposition.process_fact_refs) <= allowed_process_fact_ids:
        raise CoupledProposalValidationError(
            "proposition process_fact_refs exceed the exact process-fact allowlist"
        )

    packet_by_claim = {packet.claim_id: packet for packet in snapshot.claim_packets}
    cpe_by_id = {
        item.claim_paper_evidence_id: item
        for item in snapshot.claim_paper_evidence
    }
    for claim_id in allowed_claim_ids:
        if claim_id not in packet_by_claim:
            raise CoupledProposalValidationError(
                f"authorized ClaimPacket {claim_id} does not exist"
            )
    for binding in proposition.citation_bindings:
        if binding.claim_ref not in allowed_claim_ids:
            raise CoupledProposalValidationError(
                "citation claim is outside the exact ClaimPacket allowlist"
            )
        cpe = cpe_by_id.get(binding.claim_paper_evidence_ref)
        if cpe is None:
            raise CoupledProposalValidationError(
                f"citation CPE {binding.claim_paper_evidence_ref} does not exist"
            )
        if cpe.claim_id != binding.claim_ref or cpe.paper_id != binding.paper_ref:
            raise CoupledProposalValidationError(
                "citation binding does not match its canonical CPE"
            )
        packet = packet_by_claim[binding.claim_ref]
        if binding.claim_paper_evidence_ref not in packet.claim_paper_evidence_ids:
            raise CoupledProposalValidationError(
                "citation CPE is not authorized by the selected ClaimPacket"
            )


def _validate_proposition_bundle(
    proposal: PropositionProposalBundle,
    invocation: GeneratePropositionsInvocation,
    snapshot: RepositorySnapshot,
) -> None:
    allowed_claims = set(invocation.claim_packet_ids)
    allowed_corpus = set(invocation.corpus_fact_ids)
    allowed_process = set(invocation.process_fact_ids)
    for item in proposal.propositions:
        _validate_citation_authorization(
            item,
            snapshot=snapshot,
            allowed_claim_ids=allowed_claims,
            allowed_corpus_fact_ids=allowed_corpus,
            allowed_process_fact_ids=allowed_process,
        )
    covered_claims = {
        source
        for item in proposal.propositions
        for source in item.claim_refs
    }
    covered_corpus = {
        source
        for item in proposal.propositions
        for source in item.corpus_fact_refs
    }
    covered_process = {
        source
        for item in proposal.propositions
        for source in item.process_fact_refs
    }
    if (
        covered_claims != allowed_claims
        or covered_corpus != allowed_corpus
        or covered_process != allowed_process
    ):
        raise CoupledProposalValidationError(
            "proposition draft source union must exactly cover every invoked source"
        )


_CITATION_MARKER_PATTERN = re.compile(r"\[(?P<body>@[^\]\r\n]+)\]")
_CITATION_TOKEN_PATTERN = re.compile(r"@([A-Za-z0-9_-]+)")


def _validate_sentence_citation_markers(
    sentence: RenderedSentenceProposal,
    snapshot: RepositorySnapshot,
) -> None:
    proposition_by_id = {
        item.proposition_id: item for item in snapshot.proposition_records
    }
    licensed_papers = {
        binding.paper_id
        for proposition_id in sentence.source_proposition_refs
        for binding in proposition_by_id[proposition_id].citation_bindings
    }
    markers = tuple(_CITATION_MARKER_PATTERN.finditer(sentence.text))
    if sentence.text.count("[@") != len(markers):
        raise CoupledProposalValidationError(
            "rendered sentence contains a malformed citation marker"
        )
    observed: set[str] = set()
    for marker in markers:
        body = marker.group("body")
        tokens = _CITATION_TOKEN_PATTERN.findall(body)
        if body.count("@") != len(tokens) or any(
            re.fullmatch(r"P[0-9]{4}", token) is None for token in tokens
        ):
            raise CoupledProposalValidationError(
                "rendered sentence contains a malformed citation identifier"
            )
        observed.update(tokens)
    if observed != licensed_papers:
        raise CoupledProposalValidationError(
            "rendered sentence citation markers do not exactly match licensed papers"
        )


def _validate_rendered_bundle(
    proposal: RenderedSentenceProposalBundle,
    invocation: RenderProseInvocation,
    snapshot: RepositorySnapshot,
) -> None:
    allowed = set(invocation.proposition_ids)
    canonical = {item.proposition_id for item in snapshot.proposition_records}
    if not allowed <= canonical:
        raise CoupledProposalValidationError(
            "render invocation contains an unknown canonical proposition"
        )
    for proposition_id in allowed:
        audits = [
            item
            for item in snapshot.semantic_audits
            if item.target_id == proposition_id
        ]
        if (
            len(audits) != 1
            or derive_semantic_audit_disposition(audits[0])
            is not AuditDisposition.PASS
        ):
            raise CoupledProposalValidationError(
                f"render proposition {proposition_id} is not audited PASS"
            )
    for sentence in proposal.sentences:
        if not set(sentence.source_proposition_refs) <= allowed:
            raise CoupledProposalValidationError(
                "rendered sentence sources exceed the invocation allowlist"
            )
        _validate_sentence_citation_markers(sentence, snapshot)
    covered = {
        source
        for sentence in proposal.sentences
        for source in sentence.source_proposition_refs
    }
    if covered != allowed:
        raise CoupledProposalValidationError(
            "rendered sentence source union must exactly cover every invoked proposition"
        )


class _DraftAcceptanceAdapter:
    task_type: TaskType
    promotion_handler: str
    artifact_kind: DraftArtifactKind
    proposal_model: type[BaseModel]
    invocation_model: type[BaseModel]
    rebuild_on_stale = False

    def __init__(self) -> None:
        self.promotion_fingerprint = _adapter_fingerprint(self.promotion_handler)

    def validate_pre_execution(
        self,
        *,
        project_root: Path,
        spec: TaskSpec,
        invocation: BaseModel,
        manifest: TaskManifest,
        provenance: TaskProvenance,
    ) -> None:
        self._require_types(spec, proposal=None, invocation=invocation)
        _require_current_generation(project_root, manifest, provenance)

    def _require_types(
        self,
        spec: TaskSpec,
        *,
        proposal: BaseModel | None,
        invocation: BaseModel,
    ) -> None:
        if spec.task_type is not self.task_type:
            raise ValueError("draft adapter received the wrong TaskSpec")
        if type(invocation) is not self.invocation_model:
            raise ValueError("draft adapter received the wrong invocation model")
        if proposal is not None and type(proposal) is not self.proposal_model:
            raise CoupledProposalValidationError(
                "draft adapter received the wrong proposal model"
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
        del dependency_keys, registry
        self._require_types(spec, proposal=proposal, invocation=invocation)
        _require_empty_allocations(accepted_allocations)
        if isinstance(proposal, RevisedClaimProposal):
            assert isinstance(invocation, ReviseClaimInvocation)
            if proposal.claim_ref != invocation.claim_id:
                raise CoupledProposalValidationError(
                    "revised claim target differs from the invocation claim"
                )
            claim = next(
                (
                    item
                    for item in snapshot.candidate_claims
                    if item.claim_id == invocation.claim_id
                ),
                None,
            )
            if claim is None or claim.candidate_claim != invocation.current_candidate_claim:
                raise CoupledProposalValidationError(
                    "revise-claim invocation does not match canonical claim text"
                )
        elif isinstance(proposal, PropositionProposalBundle):
            assert isinstance(invocation, GeneratePropositionsInvocation)
            _validate_proposition_bundle(proposal, invocation, snapshot)
        elif isinstance(proposal, RenderedSentenceProposalBundle):
            assert isinstance(invocation, RenderProseInvocation)
            _validate_rendered_bundle(proposal, invocation, snapshot)
        else:  # pragma: no cover - guarded by exact type check above
            raise CoupledProposalValidationError("unsupported draft proposal")

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
        del project_root
        self._require_types(spec, proposal=proposal, invocation=invocation)
        materialize = make_draft_artifact_materializer(
            artifact_kind=self.artifact_kind,
            task_provenance=provenance,
            proposal=proposal,
        )

        def promote(
            snapshot: RepositorySnapshot, registry: CanonicalIdRegistry
        ) -> PromotionPayload:
            self.validate_proposal(
                spec=spec,
                proposal=proposal,
                snapshot=snapshot,
                dependency_keys=manifest.dependencies,
                invocation=invocation,
                registry=registry,
            )
            return PromotionPayload(snapshot, registry, {})

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
        del manifest, provenance, current_snapshot, current_registry
        try:
            self._require_types(spec, proposal=proposal, invocation=invocation)
            artifact, reference = load_draft_artifact_for_receipt(
                project_root, receipt
            )
            expected = build_draft_artifact(
                artifact_kind=self.artifact_kind,
                task_provenance=artifact.task_provenance,
                acceptance_receipt=receipt,
                proposal=proposal,
            )
            if (
                reference != expected.reference
                or artifact != expected.artifact
                or reference.owner_generation > current_generation
            ):
                return False, "accepted draft artifact differs from its receipt"
        except Exception as exc:
            return False, f"accepted draft artifact verification failed:{exc}"
        return True, None


class RevisedClaimDraftAdapter(_DraftAcceptanceAdapter):
    task_type = TaskType.REVISE_CLAIM
    promotion_handler = "promote_revised_claim"
    artifact_kind = DraftArtifactKind.REVISED_CLAIM
    proposal_model = RevisedClaimProposal
    invocation_model = ReviseClaimInvocation


class PropositionDraftAcceptanceAdapter(_DraftAcceptanceAdapter):
    task_type = TaskType.GENERATE_PROPOSITIONS
    promotion_handler = "accept_proposition_drafts"
    artifact_kind = DraftArtifactKind.PROPOSITION_DRAFT
    proposal_model = PropositionProposalBundle
    invocation_model = GeneratePropositionsInvocation


class RenderedSentenceDraftAcceptanceAdapter(_DraftAcceptanceAdapter):
    task_type = TaskType.RENDER_PROSE
    promotion_handler = "accept_rendered_sentence_drafts"
    artifact_kind = DraftArtifactKind.RENDERED_SENTENCE_DRAFT
    proposal_model = RenderedSentenceProposalBundle
    invocation_model = RenderProseInvocation


def _anchor_from_invocation(invocation: BaseModel) -> DraftArtifactAnchor:
    return DraftArtifactAnchor(
        draft_kind=getattr(invocation, "draft_kind"),
        draft_owner_generation=getattr(invocation, "draft_owner_generation"),
        draft_source_generation=getattr(invocation, "draft_source_generation"),
        draft_task_id=getattr(invocation, "draft_task_id"),
        draft_artifact_hash=getattr(invocation, "draft_artifact_hash"),
        draft_artifact_path=getattr(invocation, "draft_artifact_path"),
        draft_local_ref=getattr(invocation, "draft_local_ref"),
    )


def _load_source_draft(
    project_root: Path,
    invocation: BaseModel,
    expected_kind: DraftArtifactKind,
) -> tuple[AcceptedDraftArtifact, DraftArtifactReference]:
    artifact, reference = load_draft_artifact_for_anchor(
        project_root, _anchor_from_invocation(invocation)
    )
    if artifact.artifact_kind is not expected_kind:
        raise DraftArtifactError("audit invocation names the wrong draft kind")
    return artifact, reference


def _project_root_from_invocation(invocation: BaseModel) -> Path:
    path = Path(getattr(invocation, "draft_artifact_path"))
    kind = DraftArtifactKind(getattr(invocation, "draft_kind"))
    generation = GenerationStore.generation_name(
        getattr(invocation, "draft_owner_generation")
    )
    digest = getattr(invocation, "draft_artifact_hash").removeprefix("sha256:")
    suffix = Path(
        "state",
        "generations",
        generation,
        "auxiliary",
        DRAFT_ARTIFACT_ROOT,
        kind.value,
        f"{digest}.json",
    )
    if not path.is_absolute() or path.parts[-len(suffix.parts) :] != suffix.parts:
        raise DraftArtifactError("draft invocation path has no valid project owner")
    root_parts = path.parts[: -len(suffix.parts)]
    if not root_parts:
        raise DraftArtifactError("draft invocation path has no project root")
    return Path(*root_parts)


def _validate_audit_resource(
    provenance: TaskProvenance, invocation: BaseModel
) -> None:
    if len(provenance.resources) != 1:
        raise ValueError("draft audit requires exactly one snapshotted resource")
    resource = provenance.resources[0]
    if (
        resource.resource_id != "RES0001"
        or resource.media_type != "application/json"
        or resource.snapshot_hash != getattr(invocation, "draft_artifact_hash")
        or resource.source_dependency.source_hash_at_snapshot
        != resource.snapshot_hash
        or resource.source_path != getattr(invocation, "draft_artifact_path")
    ):
        raise ValueError("draft audit resource provenance is inconsistent")


def _proposition_context(
    project_root: Path,
    invocation: AuditPropositionInvocation,
) -> tuple[
    AcceptedDraftArtifact,
    DraftArtifactReference,
    PropositionProposal,
]:
    artifact, reference = _load_source_draft(
        project_root, invocation, DraftArtifactKind.PROPOSITION_DRAFT
    )
    if artifact.task_type is not TaskType.GENERATE_PROPOSITIONS:
        raise DraftArtifactError("proposition draft has the wrong originating task")
    bundle = PropositionProposalBundle.model_validate(artifact.proposal_payload)
    target = next(
        (
            item
            for item in bundle.propositions
            if item.local_ref == invocation.draft_local_ref
        ),
        None,
    )
    if target is None:
        raise DraftArtifactError("proposition draft target does not exist")
    _same_allowlist(
        invocation.claim_packet_ids,
        _typed_ids(artifact.task_provenance, "ClaimPacket"),
        "claim_packet_ids",
    )
    _same_allowlist(
        invocation.corpus_fact_ids,
        _typed_ids(artifact.task_provenance, "CorpusFact"),
        "corpus_fact_ids",
    )
    _same_allowlist(
        invocation.process_fact_ids,
        _typed_ids(artifact.task_provenance, "ReviewProcessFact"),
        "process_fact_ids",
    )
    return artifact, reference, target


def _proposition_mapping(local_ref: str, proposition_id: str, audit_id: str) -> dict[str, str]:
    return {local_ref: proposition_id, f"audit:{local_ref}": audit_id}


def _proposition_pair(
    target: PropositionProposal,
    proposal: Any,
    proposition_id: str,
    audit_id: str,
) -> tuple[PropositionRecord, SemanticAuditResult]:
    proposition = PropositionRecord(
        proposition_id=proposition_id,
        text=target.text,
        content_class=target.content_class,
        claim_ids=list(target.claim_refs),
        citation_bindings=[
            CitationBinding(
                paper_id=item.paper_ref,
                claim_id=item.claim_ref,
                claim_paper_evidence_id=item.claim_paper_evidence_ref,
            )
            for item in target.citation_bindings
        ],
        corpus_fact_ids=list(target.corpus_fact_refs),
        process_fact_ids=list(target.process_fact_refs),
    )
    audit = SemanticAuditResult(
        audit_id=audit_id,
        target_type="PropositionRecord",
        target_id=proposition_id,
        class_verdict=proposal.class_verdict,
        provenance_verdict=proposal.provenance_verdict,
        reason=proposal.reason,
        referenced_claim_ids=list(proposal.referenced_claim_refs),
        referenced_corpus_fact_ids=list(proposal.referenced_corpus_fact_refs),
        referenced_process_fact_ids=list(proposal.referenced_process_fact_refs),
    )
    return proposition, audit


class PropositionAuditAdapter:
    task_type = TaskType.AUDIT_PROPOSITION
    promotion_handler = "promote_proposition_with_audit"
    rebuild_on_stale = False

    def __init__(self) -> None:
        self.promotion_fingerprint = _adapter_fingerprint(self.promotion_handler)

    def validate_pre_execution(
        self,
        *,
        project_root: Path,
        spec: TaskSpec,
        invocation: BaseModel,
        manifest: TaskManifest,
        provenance: TaskProvenance,
    ) -> None:
        if (
            spec.task_type is not self.task_type
            or type(invocation) is not AuditPropositionInvocation
        ):
            raise ValueError("proposition-audit adapter received the wrong task")
        _require_current_generation(project_root, manifest, provenance)
        _validate_audit_resource(provenance, invocation)
        source_artifact, source_reference, target = _proposition_context(
            project_root, invocation
        )
        _require_source_dependencies(source_artifact, provenance.dependencies)
        _require_source_not_audited(
            project_root,
            audit_kind=DraftAuditArtifactKind.PROPOSITION,
            source_artifact=source_artifact,
            source_item=target,
            current_generation=manifest.base_generation,
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
        del dependency_keys, registry
        if (
            spec.task_type is not self.task_type
            or type(invocation) is not AuditPropositionInvocation
            or type(proposal) is not SemanticAuditProposal
        ):
            raise CoupledProposalValidationError(
                "proposition-audit adapter received the wrong contract"
            )
        root = _project_root_from_invocation(invocation)
        _, _, target = _proposition_context(root, invocation)
        if proposal.target_ref != invocation.draft_local_ref:
            raise CoupledProposalValidationError(
                "semantic audit target differs from the selected draft local ref"
            )
        if (
            set(proposal.referenced_claim_refs) != set(target.claim_refs)
            or set(proposal.referenced_corpus_fact_refs)
            != set(target.corpus_fact_refs)
            or set(proposal.referenced_process_fact_refs)
            != set(target.process_fact_refs)
        ):
            raise CoupledProposalValidationError(
                "semantic audit references do not exactly equal draft provenance"
            )
        _validate_citation_authorization(
            target,
            snapshot=snapshot,
            allowed_claim_ids=set(invocation.claim_packet_ids),
            allowed_corpus_fact_ids=set(invocation.corpus_fact_ids),
            allowed_process_fact_ids=set(invocation.process_fact_ids),
        )
        if accepted_allocations is not None:
            mapping = dict(accepted_allocations)
            expected_keys = {
                invocation.draft_local_ref,
                f"audit:{invocation.draft_local_ref}",
            }
            if set(mapping) != expected_keys:
                raise CoupledProposalValidationError(
                    "proposition receipt local-ref mapping is incomplete"
                )
            expected_pair = _proposition_pair(
                target,
                proposal,
                mapping[invocation.draft_local_ref],
                mapping[f"audit:{invocation.draft_local_ref}"],
            )
            if (
                expected_pair[0] not in snapshot.proposition_records
                or expected_pair[1] not in snapshot.semantic_audits
            ):
                raise CoupledProposalValidationError(
                    "proposition receipt does not resolve to its exact canonical pair"
                )

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
        if type(invocation) is not AuditPropositionInvocation:
            raise ValueError("proposition audit invocation type mismatch")
        if type(proposal) is not SemanticAuditProposal:
            raise ValueError("proposition audit proposal type mismatch")
        source_artifact, source_reference, target = _proposition_context(
            project_root, invocation
        )
        _require_source_dependencies(source_artifact, manifest.dependencies)

        def promote(
            snapshot: RepositorySnapshot, registry: CanonicalIdRegistry
        ) -> PromotionPayload:
            _require_source_not_audited(
                project_root,
                audit_kind=DraftAuditArtifactKind.PROPOSITION,
                source_artifact=source_artifact,
                source_item=target,
                current_generation=manifest.base_generation,
            )
            self.validate_proposal(
                spec=spec,
                proposal=proposal,
                snapshot=snapshot,
                dependency_keys=manifest.dependencies,
                invocation=invocation,
                registry=registry,
            )
            proposition_ids, registry = registry.allocate(IdKind.PROPOSITION)
            audit_ids, registry = registry.allocate(IdKind.SEMANTIC_AUDIT)
            pair = _proposition_pair(
                target, proposal, proposition_ids[0], audit_ids[0]
            )
            promoted = snapshot.model_copy(
                update={
                    "proposition_records": snapshot.proposition_records + (pair[0],),
                    "semantic_audits": snapshot.semantic_audits + (pair[1],),
                }
            )
            return PromotionPayload(
                promoted,
                registry,
                _proposition_mapping(
                    invocation.draft_local_ref,
                    proposition_ids[0],
                    audit_ids[0],
                ),
            )

        return CoupledCommitPlan(
            promote,
            _make_draft_audit_materializer(
                project_root=project_root,
                audit_kind=DraftAuditArtifactKind.PROPOSITION,
                task_provenance=provenance,
                source_draft=source_reference,
                source_artifact=source_artifact,
                source_item=target,
                proposal=proposal,
            ),
        )

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
        del manifest
        try:
            self.validate_proposal(
                spec=spec,
                proposal=proposal,
                snapshot=current_snapshot,
                dependency_keys={},
                invocation=invocation,
                registry=current_registry,
                accepted_allocations=receipt.local_ref_map,
            )
            artifact, reference = load_proposition_audit_artifact(
                project_root, receipt
            )
            source_artifact, source_reference, target = _proposition_context(
                project_root, invocation
            )
            _require_source_dependencies(
                source_artifact, provenance.dependencies
            )
            if (
                not _same_draft_audit_replay_inputs(
                    artifact.task_provenance, provenance
                )
                or artifact.source_draft != source_reference
                or artifact.proposition != target
                or artifact.audit_proposal != proposal
                or artifact.canonical_id_mapping != receipt.local_ref_map
                or reference.owner_generation > current_generation
            ):
                return False, "accepted proposition audit artifact differs"
        except Exception as exc:
            return False, f"proposition audit receipt verification failed:{exc}"
        return True, None


_AUDIT_TASK_TYPE: dict[DraftAuditArtifactKind, TaskType] = {
    DraftAuditArtifactKind.PROPOSITION: TaskType.AUDIT_PROPOSITION,
    DraftAuditArtifactKind.RENDERED_SENTENCE: TaskType.AUDIT_RENDERED_SENTENCE,
}

_AUDIT_SOURCE_KIND: dict[DraftAuditArtifactKind, DraftArtifactKind] = {
    DraftAuditArtifactKind.PROPOSITION: DraftArtifactKind.PROPOSITION_DRAFT,
    DraftAuditArtifactKind.RENDERED_SENTENCE: (
        DraftArtifactKind.RENDERED_SENTENCE_DRAFT
    ),
}

_AUDIT_RESOURCE_NAME: dict[DraftAuditArtifactKind, str] = {
    DraftAuditArtifactKind.PROPOSITION: "accepted_proposition_draft.json",
    DraftAuditArtifactKind.RENDERED_SENTENCE: (
        "accepted_rendered_sentence_draft.json"
    ),
}


def _canonical_audit_payload(
    kind: DraftAuditArtifactKind,
    source_item: BaseModel | Mapping[str, Any],
    audit_proposal: BaseModel | Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_model: type[BaseModel]
    audit_model: type[BaseModel]
    if kind is DraftAuditArtifactKind.PROPOSITION:
        source_model = PropositionProposal
        audit_model = SemanticAuditProposal
    else:
        source_model = RenderedSentenceProposal
        audit_model = RenderedSentenceAuditProposal
    source_raw = (
        source_item.model_dump(mode="json")
        if isinstance(source_item, BaseModel)
        else dict(source_item)
    )
    audit_raw = (
        audit_proposal.model_dump(mode="json")
        if isinstance(audit_proposal, BaseModel)
        else dict(audit_proposal)
    )
    try:
        source = source_model.model_validate(source_raw).model_dump(mode="json")
        audit = audit_model.model_validate(audit_raw).model_dump(mode="json")
    except Exception as exc:
        raise DraftArtifactError("draft audit payload is invalid") from exc
    if (
        canonical_json_bytes(source_raw) != canonical_json_bytes(source)
        or canonical_json_bytes(audit_raw) != canonical_json_bytes(audit)
    ):
        raise DraftArtifactError("draft audit payload is not canonical")
    return source, audit


def _normalized_source_item_payload(
    kind: DraftAuditArtifactKind,
    source_item: PropositionProposal | RenderedSentenceProposal,
) -> dict[str, Any]:
    payload = source_item.model_dump(mode="json")
    payload.pop("local_ref")
    if kind is DraftAuditArtifactKind.PROPOSITION:
        assert isinstance(source_item, PropositionProposal)
        payload["claim_refs"] = sorted(source_item.claim_refs)
        payload["corpus_fact_refs"] = sorted(source_item.corpus_fact_refs)
        payload["process_fact_refs"] = sorted(source_item.process_fact_refs)
        payload["citation_bindings"] = sorted(
            (
                binding.model_dump(mode="json")
                for binding in source_item.citation_bindings
            ),
            key=lambda item: (
                item["claim_ref"],
                item["paper_ref"],
                item["claim_paper_evidence_ref"],
            ),
        )
    else:
        assert isinstance(source_item, RenderedSentenceProposal)
        payload["source_proposition_refs"] = sorted(
            source_item.source_proposition_refs
        )
    return payload


def _source_item_identity_bindings(
    project_root: Path,
    source_artifact: AcceptedDraftArtifact,
    source_item: PropositionProposal | RenderedSentenceProposal,
) -> tuple[str, dict[str, str], dict[str, str]]:
    """Project a draft bundle's provenance down to one semantic source item."""

    source_snapshot = GenerationStore(project_root).load_generation(
        source_artifact.owner_generation
    )[0]
    # The TaskSpec dependency builder is the authoritative transitive closure.
    # Use a target-only invocation so unrelated siblings in a multi-item draft
    # cannot reset audit finality for an unchanged item.
    from .specs import TASK_SPECS

    if isinstance(source_item, PropositionProposal):
        kind = DraftAuditArtifactKind.PROPOSITION
        if source_item.claim_refs:
            invocation: BaseModel = GeneratePropositionsInvocation(
                claim_packet_ids=sorted(source_item.claim_refs),
                corpus_fact_ids=[],
                process_fact_ids=[],
            )
            dependency_keys = TASK_SPECS[
                TaskType.GENERATE_PROPOSITIONS
            ].dependency_builder(invocation, source_snapshot)
        elif source_item.corpus_fact_refs:
            selected = set(source_item.corpus_fact_refs)
            paper_ids = {
                paper_id
                for fact in source_snapshot.corpus_facts
                if fact.corpus_fact_id in selected
                for paper_id in fact.source_paper_ids
            }
            dependency_keys = tuple(
                [f"CorpusFact:{identifier}" for identifier in sorted(selected)]
                + [f"Paper:{identifier}" for identifier in sorted(paper_ids)]
            )
        elif source_item.process_fact_refs:
            dependency_keys = tuple(
                f"ReviewProcessFact:{identifier}"
                for identifier in sorted(source_item.process_fact_refs)
            )
        else:
            dependency_keys = ()
    else:
        invocation = RenderProseInvocation(
            proposition_ids=sorted(source_item.source_proposition_refs)
        )
        kind = DraftAuditArtifactKind.RENDERED_SENTENCE
        dependency_keys = TASK_SPECS[
            TaskType.RENDER_PROSE
        ].dependency_builder(invocation, source_snapshot)
    try:
        dependencies = {
            key: source_artifact.dependency_hashes[key]
            for key in dependency_keys
        }
    except KeyError as exc:
        raise DraftArtifactError(
            "source draft lacks a target-projected dependency hash"
        ) from exc
    resources = dict(sorted(source_artifact.resource_hashes.items()))
    identity_key = hash_json(
        {
            "audit_kind": kind.value,
            "source_item": _normalized_source_item_payload(kind, source_item),
            "dependency_hashes": dict(sorted(dependencies.items())),
            "resource_hashes": resources,
        }
    )
    return identity_key, dict(sorted(dependencies.items())), resources


def _expected_audit_transition(
    kind: DraftAuditArtifactKind,
    proposal: SemanticAuditProposal | RenderedSentenceAuditProposal,
) -> tuple[str, bool, bool]:
    if kind is DraftAuditArtifactKind.PROPOSITION:
        assert isinstance(proposal, SemanticAuditProposal)
        disposition = (
            proposal.class_verdict.value
            if proposal.class_verdict.value != "CORRECT"
            else proposal.provenance_verdict.value
        )
        eligible = (
            proposal.class_verdict.value == "CORRECT"
            and proposal.provenance_verdict is AuditProvenanceVerdict.ENTAILED
        )
        human_review = (
            proposal.class_verdict.value == "UNCLEAR"
            or proposal.provenance_verdict is AuditProvenanceVerdict.UNCLEAR
        )
        return disposition, eligible, human_review
    assert isinstance(proposal, RenderedSentenceAuditProposal)
    return (
        proposal.verdict.value,
        proposal.verdict is AuditProvenanceVerdict.ENTAILED,
        proposal.verdict is AuditProvenanceVerdict.UNCLEAR,
    )


def _expected_audit_mapping(
    kind: DraftAuditArtifactKind,
    local_ref: str,
    proposal: SemanticAuditProposal | RenderedSentenceAuditProposal,
    mapping: Mapping[str, str],
) -> tuple[set[str], set[str]]:
    values = dict(mapping)
    if (
        kind is DraftAuditArtifactKind.RENDERED_SENTENCE
        and isinstance(proposal, RenderedSentenceAuditProposal)
        and proposal.verdict is not AuditProvenanceVerdict.ENTAILED
    ):
        if values:
            raise ValueError(
                "non-ENTAILED sentence audit cannot map canonical identifiers"
            )
        return set(), set()
    expected_keys = {local_ref, f"audit:{local_ref}"}
    if set(values) != expected_keys:
        raise ValueError("draft audit canonical mapping is incomplete")
    primary = values[local_ref]
    audit = values[f"audit:{local_ref}"]
    if kind is DraftAuditArtifactKind.PROPOSITION:
        if re.fullmatch(r"PR[0-9]{4,}", primary) is None or re.fullmatch(
            r"SA[0-9]{4,}", audit
        ) is None:
            raise ValueError("proposition audit mapping contains invalid identifiers")
        qualified = {
            f"PropositionRecord:{primary}",
            f"SemanticAuditResult:{audit}",
        }
    else:
        if re.fullmatch(r"RS[0-9]{4,}", primary) is None or re.fullmatch(
            r"RSA[0-9]{4,}", audit
        ) is None:
            raise ValueError("sentence audit mapping contains invalid identifiers")
        qualified = {
            f"RenderedSentence:{primary}",
            f"RenderedSentenceAudit:{audit}",
        }
    return expected_keys, qualified


class AcceptedDraftAuditArtifact(RuntimeModel):
    """Atomic witness for an accepted audit of one exact draft item."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: Literal["1"] = DRAFT_AUDIT_ARTIFACT_VERSION
    audit_kind: DraftAuditArtifactKind
    owner_generation: int = Field(ge=1)
    source_generation: int = Field(ge=0)
    task_id: str = Field(pattern=r"^TASK[0-9]{4,}$")
    task_type: TaskType
    task_spec_version: str
    task_provenance_hash: Sha256
    task_provenance: _DraftAuditTaskProvenanceWitness
    source_draft: DraftArtifactReference
    draft_local_ref: str = Field(min_length=1)
    source_item_payload: dict[str, Any]
    source_item_identity_key: Sha256
    source_dependency_hashes: dict[str, Sha256]
    source_resource_hashes: dict[str, Sha256]
    audit_proposal_payload: dict[str, Any]
    proposal_hash: Sha256
    semantic_fingerprint: TaskSemanticFingerprint
    input_identity_key: Sha256
    semantic_task_key: Sha256
    dependency_hashes: dict[str, Sha256]
    resource_hashes: dict[str, Sha256]
    canonical_id_mapping: dict[str, str]
    acceptance_receipt: AppliedTaskReceipt

    @property
    def source_item(self) -> PropositionProposal | RenderedSentenceProposal:
        if self.audit_kind is DraftAuditArtifactKind.PROPOSITION:
            return PropositionProposal.model_validate(self.source_item_payload)
        return RenderedSentenceProposal.model_validate(self.source_item_payload)

    @property
    def proposition(self) -> PropositionProposal:
        if self.audit_kind is not DraftAuditArtifactKind.PROPOSITION:
            raise AttributeError("sentence audit artifact has no proposition")
        return PropositionProposal.model_validate(self.source_item_payload)

    @property
    def rendered_sentence(self) -> RenderedSentenceProposal:
        if self.audit_kind is not DraftAuditArtifactKind.RENDERED_SENTENCE:
            raise AttributeError("proposition audit artifact has no sentence")
        return RenderedSentenceProposal.model_validate(self.source_item_payload)

    @property
    def audit_proposal(
        self,
    ) -> SemanticAuditProposal | RenderedSentenceAuditProposal:
        if self.audit_kind is DraftAuditArtifactKind.PROPOSITION:
            return SemanticAuditProposal.model_validate(self.audit_proposal_payload)
        return RenderedSentenceAuditProposal.model_validate(
            self.audit_proposal_payload
        )

    @model_validator(mode="after")
    def _bindings_are_exact(self) -> "AcceptedDraftAuditArtifact":
        expected_task = _AUDIT_TASK_TYPE[self.audit_kind]
        expected_source_kind = _AUDIT_SOURCE_KIND[self.audit_kind]
        provenance = self.task_provenance
        receipt = self.acceptance_receipt
        if not (
            self.task_type is expected_task
            and provenance.task_type is expected_task
            and receipt.task_type is expected_task
        ):
            raise ValueError("draft audit artifact has the wrong task type")
        if not (
            self.task_id == provenance.task_id
            and receipt.accepted_attempt_id.startswith(f"{self.task_id}/")
            and receipt.accepted_attempt_id != f"{self.task_id}/"
        ):
            raise ValueError("draft audit artifact task identity mismatch")
        if not (
            self.task_spec_version
            == provenance.task_spec_version
            == receipt.task_spec_version
        ):
            raise ValueError("draft audit task-spec versions differ")
        if not (
            self.source_generation
            == provenance.base_generation
            == receipt.source_generation
            and self.owner_generation == receipt.committed_generation
            and self.owner_generation == self.source_generation + 1
        ):
            raise ValueError("draft audit artifact generation mismatch")
        if self.task_provenance_hash != hash_json(
            provenance.model_dump(mode="json")
        ):
            raise ValueError("draft audit task provenance hash mismatch")
        if (
            self.source_draft.artifact_kind is not expected_source_kind
            or self.draft_local_ref not in self.source_draft.draft_local_refs
            or self.source_draft.owner_generation > self.source_generation
        ):
            raise ValueError("draft audit source reference is inconsistent")

        source_payload, audit_payload = _canonical_audit_payload(
            self.audit_kind,
            self.source_item_payload,
            self.audit_proposal_payload,
        )
        source_item = self.source_item
        audit_proposal = self.audit_proposal
        source_local_ref = getattr(source_item, "local_ref")
        audit_target = (
            audit_proposal.target_ref
            if isinstance(audit_proposal, SemanticAuditProposal)
            else audit_proposal.sentence_ref
        )
        if source_local_ref != self.draft_local_ref or audit_target != self.draft_local_ref:
            raise ValueError("draft audit target differs from its source item")
        if self.source_item_payload != source_payload:
            raise ValueError("draft audit source item is not canonical")
        if self.audit_proposal_payload != audit_payload:
            raise ValueError("draft audit proposal is not canonical")
        expected_source_identity = hash_json(
            {
                "audit_kind": self.audit_kind.value,
                "source_item": _normalized_source_item_payload(
                    self.audit_kind, source_item
                ),
                "dependency_hashes": dict(
                    sorted(self.source_dependency_hashes.items())
                ),
                "resource_hashes": dict(
                    sorted(self.source_resource_hashes.items())
                ),
            }
        )
        if self.source_item_identity_key != expected_source_identity:
            raise ValueError("draft audit source-item identity is inconsistent")

        if canonical_json_bytes(receipt.proposal_payload) != canonical_json_bytes(
            audit_payload
        ):
            raise ValueError("draft audit proposal differs from its receipt")
        if not (
            self.proposal_hash
            == receipt.proposal_hash
            == hash_json(audit_payload)
        ):
            raise ValueError("draft audit proposal hash mismatch")
        if self.semantic_fingerprint != receipt.semantic_fingerprint:
            raise ValueError("draft audit semantic fingerprint mismatch")
        if (
            self.semantic_fingerprint.combined_fingerprint
            != _semantic_fingerprint_hash(self.semantic_fingerprint)
        ):
            raise ValueError("draft audit semantic fingerprint is inconsistent")
        if self.input_identity_key != receipt.input_identity_key:
            raise ValueError("draft audit input identity mismatch")
        expected_semantic_key = compute_semantic_task_key(
            input_identity_key=self.input_identity_key,
            semantic_fingerprint=self.semantic_fingerprint,
        )
        if not (
            self.semantic_task_key
            == receipt.semantic_task_key
            == expected_semantic_key
        ):
            raise ValueError("draft audit semantic key mismatch")

        if self.dependency_hashes != dict(sorted(provenance.dependencies.items())):
            raise ValueError("draft audit dependency hashes differ from provenance")
        if len(provenance.resources) != 1:
            raise ValueError("draft audit must bind exactly one source resource")
        resource = provenance.resources[0]
        expected_resource_hashes = {resource.resource_id: resource.snapshot_hash}
        if self.resource_hashes != expected_resource_hashes:
            raise ValueError("draft audit resource hashes differ from provenance")
        expected_bundle_path = resource_destination(
            resource.resource_id, resource.media_type
        )
        if not (
            resource.resource_id == "RES0001"
            and resource.logical_name == _AUDIT_RESOURCE_NAME[self.audit_kind]
            and resource.media_type == "application/json"
            and resource.bundle_relative_path == expected_bundle_path
            and resource.snapshot_hash == self.source_draft.artifact_hash
            and resource.source_dependency.source_hash_at_snapshot
            == self.source_draft.artifact_hash
            and provenance.expected_immutable_files.get(
                resource.bundle_relative_path.as_posix()
            )
            == resource.snapshot_hash
        ):
            raise ValueError("draft audit source resource binding is inconsistent")

        _, expected_qualified = _expected_audit_mapping(
            self.audit_kind,
            self.draft_local_ref,
            audit_proposal,
            self.canonical_id_mapping,
        )
        if self.canonical_id_mapping != receipt.local_ref_map:
            raise ValueError("draft audit canonical mapping differs from receipt")
        actual_qualified = {
            item.qualified_id for item in receipt.canonical_objects
        }
        if (
            actual_qualified != expected_qualified
            or len(actual_qualified) != len(receipt.canonical_objects)
        ):
            raise ValueError("draft audit canonical object set is incomplete")
        disposition, eligible, human_review = _expected_audit_transition(
            self.audit_kind, audit_proposal
        )
        transition = receipt.recorded_transition
        expected_canonicalized = (
            self.audit_kind is DraftAuditArtifactKind.RENDERED_SENTENCE
            or bool(expected_qualified)
        )
        if (
            transition.scientific_disposition != disposition
            or transition.canonicalized is not expected_canonicalized
            or transition.downstream_eligible != eligible
            or transition.human_review_required != human_review
        ):
            raise ValueError("draft audit receipt transition mismatch")
        return self


def _draft_audit_path(kind: DraftAuditArtifactKind, artifact_hash: str) -> str:
    return (
        f"{DRAFT_AUDIT_ARTIFACT_ROOT}/{kind.value}/artifacts/"
        f"{artifact_hash.removeprefix('sha256:')}.json"
    )


def _draft_audit_receipt_index_path(
    kind: DraftAuditArtifactKind, semantic_task_key: str
) -> str:
    return (
        f"{DRAFT_AUDIT_ARTIFACT_ROOT}/{kind.value}/by-receipt/"
        f"{semantic_task_key.removeprefix('sha256:')}.json"
    )


def _draft_audit_source_index_path(
    kind: DraftAuditArtifactKind,
    source_item_identity_key: str,
) -> str:
    return (
        f"{DRAFT_AUDIT_ARTIFACT_ROOT}/{kind.value}/by-source/"
        f"{source_item_identity_key.removeprefix('sha256:')}.json"
    )


class DraftAuditArtifactReference(RuntimeModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    audit_kind: DraftAuditArtifactKind
    artifact_hash: Sha256
    relative_path: str
    owner_generation: int = Field(ge=1)
    source_generation: int = Field(ge=0)
    task_id: str = Field(pattern=r"^TASK[0-9]{4,}$")
    semantic_task_key: Sha256
    source_draft_artifact_hash: Sha256
    source_item_identity_key: Sha256
    draft_local_ref: str = Field(min_length=1)

    @model_validator(mode="after")
    def _content_addressed(self) -> "DraftAuditArtifactReference":
        if self.relative_path != _draft_audit_path(
            self.audit_kind, self.artifact_hash
        ):
            raise ValueError("draft audit artifact path is not content addressed")
        if self.owner_generation != self.source_generation + 1:
            raise ValueError("draft audit artifact lineage mismatch")
        return self


class DraftAuditReceiptIndex(RuntimeModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: Literal["1"] = DRAFT_AUDIT_ARTIFACT_VERSION
    semantic_task_key: Sha256
    reference: DraftAuditArtifactReference

    @model_validator(mode="after")
    def _reference_is_exact(self) -> "DraftAuditReceiptIndex":
        if self.reference.semantic_task_key != self.semantic_task_key:
            raise ValueError("draft audit receipt index key mismatch")
        return self


class DraftAuditSourceIndex(RuntimeModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: Literal["1"] = DRAFT_AUDIT_ARTIFACT_VERSION
    audit_kind: DraftAuditArtifactKind
    source_draft_artifact_hash: Sha256
    source_item_identity_key: Sha256
    draft_local_ref: str = Field(min_length=1)
    semantic_task_key: Sha256
    reference: DraftAuditArtifactReference

    @model_validator(mode="after")
    def _reference_is_exact(self) -> "DraftAuditSourceIndex":
        reference = self.reference
        if not (
            reference.audit_kind is self.audit_kind
            and reference.source_draft_artifact_hash
            == self.source_draft_artifact_hash
            and reference.source_item_identity_key
            == self.source_item_identity_key
            and reference.draft_local_ref == self.draft_local_ref
            and reference.semantic_task_key == self.semantic_task_key
        ):
            raise ValueError("draft audit source index differs from its reference")
        return self


def _draft_audit_bytes(artifact: AcceptedDraftAuditArtifact) -> bytes:
    content = canonical_json_bytes(artifact.model_dump(mode="json")) + b"\n"
    if len(content) > MAX_DRAFT_ARTIFACT_BYTES:
        raise DraftArtifactError("draft audit artifact exceeds its byte limit")
    return content


def _build_draft_audit_artifact(
    *,
    audit_kind: DraftAuditArtifactKind,
    task_provenance: TaskProvenance,
    receipt: AppliedTaskReceipt,
    source_draft: DraftArtifactReference,
    source_item: BaseModel | Mapping[str, Any],
    source_item_identity_key: str,
    source_dependency_hashes: Mapping[str, str],
    source_resource_hashes: Mapping[str, str],
    proposal: BaseModel | Mapping[str, Any],
) -> tuple[AcceptedDraftAuditArtifact, DraftAuditArtifactReference, bytes]:
    source_payload, proposal_payload = _canonical_audit_payload(
        audit_kind, source_item, proposal
    )
    provenance_witness = _DraftAuditTaskProvenanceWitness.from_private(
        task_provenance
    )
    resource_hashes = {
        resource.resource_id: resource.snapshot_hash
        for resource in provenance_witness.resources
    }
    try:
        artifact = AcceptedDraftAuditArtifact(
            audit_kind=audit_kind,
            owner_generation=receipt.committed_generation,
            source_generation=receipt.source_generation,
            task_id=provenance_witness.task_id,
            task_type=receipt.task_type,
            task_spec_version=receipt.task_spec_version,
            task_provenance_hash=hash_json(
                provenance_witness.model_dump(mode="json")
            ),
            task_provenance=provenance_witness,
            source_draft=source_draft,
            draft_local_ref=getattr(source_item, "local_ref", None)
            or source_payload["local_ref"],
            source_item_payload=source_payload,
            source_item_identity_key=source_item_identity_key,
            source_dependency_hashes=dict(
                sorted(source_dependency_hashes.items())
            ),
            source_resource_hashes=dict(sorted(source_resource_hashes.items())),
            audit_proposal_payload=proposal_payload,
            proposal_hash=receipt.proposal_hash,
            semantic_fingerprint=receipt.semantic_fingerprint,
            input_identity_key=receipt.input_identity_key,
            semantic_task_key=receipt.semantic_task_key,
            dependency_hashes=dict(
                sorted(provenance_witness.dependencies.items())
            ),
            resource_hashes=dict(sorted(resource_hashes.items())),
            canonical_id_mapping=dict(receipt.local_ref_map),
            acceptance_receipt=receipt,
        )
    except DraftArtifactError:
        raise
    except Exception as exc:
        raise DraftArtifactError(
            f"accepted draft audit bindings are invalid:{exc}"
        ) from exc
    content = _draft_audit_bytes(artifact)
    artifact_hash = hash_bytes(content)
    reference = DraftAuditArtifactReference(
        audit_kind=audit_kind,
        artifact_hash=artifact_hash,
        relative_path=_draft_audit_path(audit_kind, artifact_hash),
        owner_generation=artifact.owner_generation,
        source_generation=artifact.source_generation,
        task_id=artifact.task_id,
        semantic_task_key=artifact.semantic_task_key,
        source_draft_artifact_hash=source_draft.artifact_hash,
        source_item_identity_key=artifact.source_item_identity_key,
        draft_local_ref=artifact.draft_local_ref,
    )
    return artifact, reference, content


def _read_exact_auxiliary(
    project_root: Path,
    generation: int,
    relative_paths: set[str],
) -> tuple[RepositorySnapshot, dict[str, bytes]]:
    snapshot, _, selected = GenerationStore(project_root).load_generation_auxiliary(
        generation, relative_paths
    )
    generation_name = GenerationStore.generation_name(generation)
    for relative_path, selected_content in selected.items():
        exact, _ = read_contained_regular_file(
            project_root,
            f"state/generations/{generation_name}/auxiliary/{relative_path}",
            max_bytes=MAX_DRAFT_ARTIFACT_BYTES,
        )
        if exact != selected_content:
            raise DraftArtifactError(
                "draft audit auxiliary file changed during verification"
            )
    return snapshot, selected


def _load_draft_audit_reference(
    project_root: Path,
    reference: DraftAuditArtifactReference,
    *,
    expected_receipt: AppliedTaskReceipt | None = None,
) -> AcceptedDraftAuditArtifact:
    receipt_index_path = _draft_audit_receipt_index_path(
        reference.audit_kind, reference.semantic_task_key
    )
    source_index_path = _draft_audit_source_index_path(
        reference.audit_kind, reference.source_item_identity_key
    )
    snapshot, selected = _read_exact_auxiliary(
        project_root,
        reference.owner_generation,
        {reference.relative_path, receipt_index_path, source_index_path},
    )
    content = selected[reference.relative_path]
    if hash_bytes(content) != reference.artifact_hash:
        raise DraftArtifactError("draft audit artifact content hash mismatch")
    try:
        artifact = AcceptedDraftAuditArtifact.model_validate_json(content)
    except Exception as exc:
        raise DraftArtifactError("draft audit artifact is invalid") from exc
    if _draft_audit_bytes(artifact) != content:
        raise DraftArtifactError("draft audit artifact bytes are not canonical")
    if not (
        artifact.audit_kind is reference.audit_kind
        and artifact.owner_generation == reference.owner_generation
        and artifact.source_generation == reference.source_generation
        and artifact.task_id == reference.task_id
        and artifact.semantic_task_key == reference.semantic_task_key
        and artifact.source_draft.artifact_hash
        == reference.source_draft_artifact_hash
        and artifact.source_item_identity_key
        == reference.source_item_identity_key
        and artifact.draft_local_ref == reference.draft_local_ref
    ):
        raise DraftArtifactError("draft audit artifact differs from its reference")
    if expected_receipt is not None and artifact.acceptance_receipt != expected_receipt:
        raise DraftArtifactError("draft audit artifact receipt mismatch")
    receipt_index_content = selected[receipt_index_path]
    source_index_content = selected[source_index_path]
    if (
        len(receipt_index_content) > MAX_DRAFT_AUDIT_INDEX_BYTES
        or len(source_index_content) > MAX_DRAFT_AUDIT_INDEX_BYTES
    ):
        raise DraftArtifactError("draft audit index exceeds its byte limit")
    try:
        receipt_index = DraftAuditReceiptIndex.model_validate_json(
            receipt_index_content
        )
        source_index = DraftAuditSourceIndex.model_validate_json(
            source_index_content
        )
    except Exception as exc:
        raise DraftArtifactError("draft audit index is invalid") from exc
    if not (
        canonical_json_bytes(receipt_index.model_dump(mode="json")) + b"\n"
        == receipt_index_content
        and canonical_json_bytes(source_index.model_dump(mode="json")) + b"\n"
        == source_index_content
        and receipt_index.reference == reference
        and source_index.reference == reference
    ):
        raise DraftArtifactError("draft audit indexes differ from the artifact")
    canonical_ok, canonical_reason = verify_receipt_canonical_objects(
        artifact.acceptance_receipt, snapshot
    )
    if not canonical_ok:
        raise DraftArtifactError(
            f"draft audit canonical objects are invalid:{canonical_reason}"
        )
    source_artifact = load_draft_artifact(project_root, artifact.source_draft)
    source_content, _ = read_contained_regular_file(
        Path(project_root),
        (
            "state/generations/"
            f"{GenerationStore.generation_name(artifact.source_draft.owner_generation)}"
            f"/auxiliary/{artifact.source_draft.relative_path}"
        ),
        max_bytes=MAX_DRAFT_ARTIFACT_BYTES,
    )
    resource = artifact.task_provenance.resources[0]
    if not (
        hash_bytes(source_content) == artifact.source_draft.artifact_hash
        and len(source_content) == resource.size_bytes
    ):
        raise DraftArtifactError(
            "draft audit source resource bytes differ from its witness"
        )
    if artifact.audit_kind is DraftAuditArtifactKind.PROPOSITION:
        source_bundle = PropositionProposalBundle.model_validate(
            source_artifact.proposal_payload
        )
        candidates: tuple[BaseModel, ...] = tuple(source_bundle.propositions)
    else:
        source_bundle = RenderedSentenceProposalBundle.model_validate(
            source_artifact.proposal_payload
        )
        candidates = tuple(source_bundle.sentences)
    source_matches = [
        item for item in candidates if item.local_ref == artifact.draft_local_ref
    ]
    if len(source_matches) != 1 or source_matches[0] != artifact.source_item:
        raise DraftArtifactError("draft audit source item is not exact")
    _require_source_dependencies(source_artifact, artifact.dependency_hashes)
    identity_key, dependency_hashes, resource_hashes = (
        _source_item_identity_bindings(
            project_root, source_artifact, artifact.source_item
        )
    )
    if not (
        artifact.source_item_identity_key == identity_key
        and artifact.source_dependency_hashes == dependency_hashes
        and artifact.source_resource_hashes == resource_hashes
    ):
        raise DraftArtifactError(
            "draft audit target-projected source identity is inconsistent"
        )
    return artifact


def _load_draft_audit_artifact(
    project_root: Path,
    receipt: AppliedTaskReceipt,
    audit_kind: DraftAuditArtifactKind,
) -> tuple[AcceptedDraftAuditArtifact, DraftAuditArtifactReference]:
    exact_receipt = AppliedTaskReceipt.model_validate(
        receipt.model_dump(mode="json")
    )
    index_path = _draft_audit_receipt_index_path(
        audit_kind, exact_receipt.semantic_task_key
    )
    _, selected = _read_exact_auxiliary(
        project_root, exact_receipt.committed_generation, {index_path}
    )
    content = selected[index_path]
    if len(content) > MAX_DRAFT_AUDIT_INDEX_BYTES:
        raise DraftArtifactError("draft audit receipt index exceeds its byte limit")
    try:
        index = DraftAuditReceiptIndex.model_validate_json(content)
    except Exception as exc:
        raise DraftArtifactError("draft audit receipt index is invalid") from exc
    if canonical_json_bytes(index.model_dump(mode="json")) + b"\n" != content:
        raise DraftArtifactError("draft audit receipt index bytes are not canonical")
    reference = index.reference
    if not (
        reference.audit_kind is audit_kind
        and reference.owner_generation == exact_receipt.committed_generation
        and reference.semantic_task_key == exact_receipt.semantic_task_key
    ):
        raise DraftArtifactError("draft audit receipt index binding mismatch")
    artifact = _load_draft_audit_reference(
        project_root, reference, expected_receipt=exact_receipt
    )
    source_index_path = _draft_audit_source_index_path(
        audit_kind,
        reference.source_item_identity_key,
    )
    _, source_selected = _read_exact_auxiliary(
        project_root, reference.owner_generation, {source_index_path}
    )
    source_content = source_selected[source_index_path]
    if len(source_content) > MAX_DRAFT_AUDIT_INDEX_BYTES:
        raise DraftArtifactError("draft audit source index exceeds its byte limit")
    try:
        source_index = DraftAuditSourceIndex.model_validate_json(source_content)
    except Exception as exc:
        raise DraftArtifactError("draft audit source index is invalid") from exc
    if (
        canonical_json_bytes(source_index.model_dump(mode="json")) + b"\n"
        != source_content
        or source_index.reference != reference
    ):
        raise DraftArtifactError("draft audit source index binding mismatch")
    return artifact, reference


def load_draft_audit_artifact(
    project_root: Path,
    receipt: AppliedTaskReceipt,
    audit_kind: DraftAuditArtifactKind,
) -> tuple[AcceptedDraftAuditArtifact, DraftAuditArtifactReference]:
    """Resolve and exactly verify one receipt/source-indexed audit witness."""

    try:
        return _load_draft_audit_artifact(project_root, receipt, audit_kind)
    except DraftArtifactError:
        raise
    except Exception as exc:
        raise DraftArtifactError(
            "draft audit artifact could not be verified"
        ) from exc


def load_proposition_audit_artifact(
    project_root: Path,
    receipt: AppliedTaskReceipt,
) -> tuple[AcceptedDraftAuditArtifact, DraftAuditArtifactReference]:
    return load_draft_audit_artifact(
        project_root, receipt, DraftAuditArtifactKind.PROPOSITION
    )


def load_sentence_audit_artifact(
    project_root: Path,
    receipt: AppliedTaskReceipt,
) -> tuple[AcceptedDraftAuditArtifact, DraftAuditArtifactReference]:
    return load_draft_audit_artifact(
        project_root, receipt, DraftAuditArtifactKind.RENDERED_SENTENCE
    )


# Compatibility aliases for callers that imported the original sentence-only
# names while this Package C slice was under development.
AcceptedSentenceAuditArtifact = AcceptedDraftAuditArtifact
SentenceAuditArtifactReference = DraftAuditArtifactReference


def _missing_auxiliary_path(exc: ValueError, generation: int) -> bool:
    return str(exc) in {
        "requested generation auxiliary file is absent",
        f"generation {generation} has no requested auxiliary data",
    }


def _find_prior_draft_audit(
    project_root: Path,
    *,
    audit_kind: DraftAuditArtifactKind,
    source_artifact: AcceptedDraftArtifact,
    source_item: PropositionProposal | RenderedSentenceProposal,
    current_generation: int,
) -> AcceptedDraftAuditArtifact | None:
    if current_generation < 1:
        return None
    if current_generation > MAX_DRAFT_AUDIT_GENERATION_SCAN:
        raise DraftArtifactError(
            "draft audit history exceeds the bounded source-index scan"
        )
    source_item_identity_key, _, _ = _source_item_identity_bindings(
        project_root, source_artifact, source_item
    )
    source_index_path = _draft_audit_source_index_path(
        audit_kind, source_item_identity_key
    )
    for generation in range(1, current_generation + 1):
        try:
            _, selected = _read_exact_auxiliary(
                project_root, generation, {source_index_path}
            )
        except ValueError as exc:
            if _missing_auxiliary_path(exc, generation):
                continue
            raise
        content = selected[source_index_path]
        if len(content) > MAX_DRAFT_AUDIT_INDEX_BYTES:
            raise DraftArtifactError("draft audit source index exceeds its byte limit")
        try:
            source_index = DraftAuditSourceIndex.model_validate_json(content)
        except Exception as exc:
            raise DraftArtifactError("draft audit source index is invalid") from exc
        if canonical_json_bytes(source_index.model_dump(mode="json")) + b"\n" != content:
            raise DraftArtifactError("draft audit source index bytes are not canonical")
        if not (
            source_index.audit_kind is audit_kind
            and source_index.source_item_identity_key
            == source_item_identity_key
            and source_index.reference.owner_generation == generation
        ):
            raise DraftArtifactError("draft audit source index binding mismatch")
        return _load_draft_audit_reference(
            project_root, source_index.reference
        )
    return None


def _require_source_not_audited(
    project_root: Path,
    *,
    audit_kind: DraftAuditArtifactKind,
    source_artifact: AcceptedDraftArtifact,
    source_item: PropositionProposal | RenderedSentenceProposal,
    current_generation: int,
) -> None:
    prior = _find_prior_draft_audit(
        project_root,
        audit_kind=audit_kind,
        source_artifact=source_artifact,
        source_item=source_item,
        current_generation=current_generation,
    )
    if prior is not None:
        raise DraftArtifactError(
            "this exact draft item already has an accepted audit; "
            "submit a new repaired draft artifact before re-auditing"
        )


def _make_draft_audit_materializer(
    *,
    project_root: Path,
    audit_kind: DraftAuditArtifactKind,
    task_provenance: TaskProvenance,
    source_draft: DraftArtifactReference,
    source_artifact: AcceptedDraftArtifact,
    source_item: BaseModel,
    proposal: BaseModel,
):
    frozen_provenance = TaskProvenance.model_validate(
        task_provenance.model_dump(mode="json")
    )
    frozen_source = type(source_item).model_validate(
        source_item.model_dump(mode="json")
    )
    frozen_proposal = type(proposal).model_validate(
        proposal.model_dump(mode="json")
    )
    source_item_identity_key, source_dependencies, source_resources = (
        _source_item_identity_bindings(
            project_root, source_artifact, frozen_source
        )
    )

    def materialize(
        writer: AuxiliaryStagingWriter,
        next_generation: int,
        payload: PromotionPayload,
        receipt: AppliedTaskReceipt,
    ) -> None:
        if dict(payload.allocated_ids) != receipt.local_ref_map:
            raise DraftArtifactError(
                "draft audit payload allocations differ from its receipt"
            )
        canonical_ok, canonical_reason = verify_receipt_canonical_objects(
            receipt, payload.snapshot
        )
        if not canonical_ok:
            raise DraftArtifactError(
                f"draft audit receipt canonical objects are invalid:{canonical_reason}"
            )
        artifact, reference, content = _build_draft_audit_artifact(
            audit_kind=audit_kind,
            task_provenance=frozen_provenance,
            receipt=receipt,
            source_draft=source_draft,
            source_item=frozen_source,
            source_item_identity_key=source_item_identity_key,
            source_dependency_hashes=source_dependencies,
            source_resource_hashes=source_resources,
            proposal=frozen_proposal,
        )
        if reference.owner_generation != next_generation:
            raise DraftArtifactError(
                "draft audit artifact is owned by another generation"
            )
        writer.write_bytes(reference.relative_path, content)
        receipt_index = DraftAuditReceiptIndex(
            semantic_task_key=receipt.semantic_task_key,
            reference=reference,
        )
        writer.write_bytes(
            _draft_audit_receipt_index_path(
                audit_kind, receipt.semantic_task_key
            ),
            canonical_json_bytes(receipt_index.model_dump(mode="json")) + b"\n",
        )
        source_index = DraftAuditSourceIndex(
            audit_kind=audit_kind,
            source_draft_artifact_hash=source_draft.artifact_hash,
            source_item_identity_key=reference.source_item_identity_key,
            draft_local_ref=reference.draft_local_ref,
            semantic_task_key=receipt.semantic_task_key,
            reference=reference,
        )
        writer.write_bytes(
            _draft_audit_source_index_path(
                audit_kind,
                reference.source_item_identity_key,
            ),
            canonical_json_bytes(source_index.model_dump(mode="json")) + b"\n",
        )

    return materialize


def _sentence_context(
    project_root: Path,
    invocation: Any,
) -> tuple[
    AcceptedDraftArtifact,
    DraftArtifactReference,
    RenderedSentenceProposal,
]:
    artifact, reference = _load_source_draft(
        project_root, invocation, DraftArtifactKind.RENDERED_SENTENCE_DRAFT
    )
    if artifact.task_type is not TaskType.RENDER_PROSE:
        raise DraftArtifactError("sentence draft has the wrong originating task")
    bundle = RenderedSentenceProposalBundle.model_validate(artifact.proposal_payload)
    target = next(
        (
            item
            for item in bundle.sentences
            if item.local_ref == invocation.draft_local_ref
        ),
        None,
    )
    if target is None:
        raise DraftArtifactError("rendered sentence draft target does not exist")
    if list(target.source_proposition_refs) != list(invocation.source_proposition_ids):
        raise CoupledProposalValidationError(
            "sentence source_proposition_ids differ from the exact draft sources"
        )
    allowed = set(_typed_ids(artifact.task_provenance, "PropositionRecord"))
    if not set(target.source_proposition_refs) <= allowed:
        raise CoupledProposalValidationError(
            "sentence draft sources exceed the original render allowlist"
        )
    return artifact, reference, target


def _sentence_mapping(local_ref: str, sentence_id: str, audit_id: str) -> dict[str, str]:
    return {local_ref: sentence_id, f"audit:{local_ref}": audit_id}


def _sentence_pair(
    target: RenderedSentenceProposal,
    proposal: RenderedSentenceAuditProposal,
    sentence_id: str,
    audit_id: str,
) -> tuple[RenderedSentence, RenderedSentenceAudit]:
    return (
        RenderedSentence(
            sentence_id=sentence_id,
            text=target.text,
            source_proposition_ids=list(target.source_proposition_refs),
        ),
        RenderedSentenceAudit(
            audit_id=audit_id,
            sentence_id=sentence_id,
            verdict=proposal.verdict,
            reason=proposal.reason,
        ),
    )


class RenderedSentenceAuditAdapter:
    task_type = TaskType.AUDIT_RENDERED_SENTENCE
    promotion_handler = "promote_rendered_sentence_with_audit"
    rebuild_on_stale = False
    canonicalizes_accepted_artifact = True

    def __init__(self) -> None:
        self.promotion_fingerprint = _adapter_fingerprint(self.promotion_handler)

    def validate_pre_execution(
        self,
        *,
        project_root: Path,
        spec: TaskSpec,
        invocation: BaseModel,
        manifest: TaskManifest,
        provenance: TaskProvenance,
    ) -> None:
        if (
            spec.task_type is not self.task_type
            or type(invocation) is not AuditRenderedSentenceInvocation
        ):
            raise ValueError("sentence-audit adapter received the wrong task")
        _require_current_generation(project_root, manifest, provenance)
        _validate_audit_resource(provenance, invocation)
        source_artifact, source_reference, target = _sentence_context(
            project_root, invocation
        )
        _require_source_dependencies(source_artifact, provenance.dependencies)
        _require_source_not_audited(
            project_root,
            audit_kind=DraftAuditArtifactKind.RENDERED_SENTENCE,
            source_artifact=source_artifact,
            source_item=target,
            current_generation=manifest.base_generation,
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
        del dependency_keys, registry
        if (
            spec.task_type is not self.task_type
            or type(proposal) is not RenderedSentenceAuditProposal
            or type(invocation) is not AuditRenderedSentenceInvocation
        ):
            raise CoupledProposalValidationError(
                "sentence-audit adapter received the wrong contract"
            )
        root = _project_root_from_invocation(invocation)
        _, _, target = _sentence_context(root, invocation)
        if proposal.sentence_ref != invocation.draft_local_ref:
            raise CoupledProposalValidationError(
                "sentence audit target differs from selected draft local ref"
            )
        proposition_by_id = {
            item.proposition_id: item for item in snapshot.proposition_records
        }
        for proposition_id in target.source_proposition_refs:
            if proposition_id not in proposition_by_id:
                raise CoupledProposalValidationError(
                    f"sentence source proposition {proposition_id} does not exist"
                )
            audits = [
                item
                for item in snapshot.semantic_audits
                if item.target_id == proposition_id
            ]
            if (
                len(audits) != 1
                or derive_semantic_audit_disposition(audits[0])
                is not AuditDisposition.PASS
            ):
                raise CoupledProposalValidationError(
                    f"sentence source proposition {proposition_id} is not audited PASS"
                )
        _validate_sentence_citation_markers(target, snapshot)
        if accepted_allocations is not None:
            mapping = dict(accepted_allocations)
            if proposal.verdict is AuditProvenanceVerdict.ENTAILED:
                expected_keys = {
                    invocation.draft_local_ref,
                    f"audit:{invocation.draft_local_ref}",
                }
                if set(mapping) != expected_keys:
                    raise CoupledProposalValidationError(
                        "sentence receipt local-ref mapping is incomplete"
                    )
                pair = _sentence_pair(
                    target,
                    proposal,
                    mapping[invocation.draft_local_ref],
                    mapping[f"audit:{invocation.draft_local_ref}"],
                )
                if (
                    pair[0] not in snapshot.rendered_sentences
                    or pair[1] not in snapshot.rendered_sentence_audits
                ):
                    raise CoupledProposalValidationError(
                        "sentence receipt does not resolve to its exact canonical pair"
                    )
            elif mapping:
                raise CoupledProposalValidationError(
                    "non-ENTAILED sentence audit cannot map canonical identifiers"
                )

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
        if type(proposal) is not RenderedSentenceAuditProposal:
            raise ValueError("sentence audit proposal type mismatch")
        source_artifact, source_reference, target = _sentence_context(
            project_root, invocation
        )
        _require_source_dependencies(source_artifact, manifest.dependencies)

        def promote(
            snapshot: RepositorySnapshot, registry: CanonicalIdRegistry
        ) -> PromotionPayload:
            _require_source_not_audited(
                project_root,
                audit_kind=DraftAuditArtifactKind.RENDERED_SENTENCE,
                source_artifact=source_artifact,
                source_item=target,
                current_generation=manifest.base_generation,
            )
            self.validate_proposal(
                spec=spec,
                proposal=proposal,
                snapshot=snapshot,
                dependency_keys=manifest.dependencies,
                invocation=invocation,
                registry=registry,
            )
            if proposal.verdict is not AuditProvenanceVerdict.ENTAILED:
                return PromotionPayload(snapshot, registry, {})
            sentence_ids, registry = registry.allocate(IdKind.RENDERED_SENTENCE)
            audit_ids, registry = registry.allocate(IdKind.RENDERED_SENTENCE_AUDIT)
            pair = _sentence_pair(
                target, proposal, sentence_ids[0], audit_ids[0]
            )
            promoted = snapshot.model_copy(
                update={
                    "rendered_sentences": snapshot.rendered_sentences + (pair[0],),
                    "rendered_sentence_audits": (
                        snapshot.rendered_sentence_audits + (pair[1],)
                    ),
                }
            )
            return PromotionPayload(
                promoted,
                registry,
                _sentence_mapping(
                    invocation.draft_local_ref, sentence_ids[0], audit_ids[0]
                ),
            )

        return CoupledCommitPlan(
            promote,
            _make_draft_audit_materializer(
                project_root=project_root,
                audit_kind=DraftAuditArtifactKind.RENDERED_SENTENCE,
                task_provenance=provenance,
                source_draft=source_reference,
                source_artifact=source_artifact,
                source_item=target,
                proposal=proposal,
            ),
        )

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
        del manifest
        try:
            self.validate_proposal(
                spec=spec,
                proposal=proposal,
                snapshot=current_snapshot,
                dependency_keys={},
                invocation=invocation,
                registry=current_registry,
                accepted_allocations=receipt.local_ref_map,
            )
            artifact, reference = load_sentence_audit_artifact(
                project_root, receipt
            )
            source_artifact, source_reference, target = _sentence_context(
                project_root, invocation
            )
            _require_source_dependencies(
                source_artifact, provenance.dependencies
            )
            if (
                not _same_draft_audit_replay_inputs(
                    artifact.task_provenance, provenance
                )
                or artifact.source_draft != source_reference
                or artifact.rendered_sentence != target
                or artifact.audit_proposal != proposal
                or artifact.canonical_id_mapping != receipt.local_ref_map
                or reference.owner_generation > current_generation
            ):
                return False, "accepted sentence audit artifact differs"
        except Exception as exc:
            return False, f"sentence audit receipt verification failed:{exc}"
        return True, None
