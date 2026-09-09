"""Task-bound promotion of authenticated retrieval candidates.

The semantic engine proposes dispositions and evidence fields only.  This
adapter binds that proposal to the exact generation-owned corpus, canonical
query, complete retrieval ledger, task resource, and accepted-task receipt
before Python allocates any canonical identifier.
"""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from vibereview.enums import RetrievalDispositionStatus
from vibereview.ids import ClaimId, EvidenceId, QueryId, Sha256, SpanId
from vibereview.runtime.coupled import (
    CoupledCommitPlan,
    CoupledProposalValidationError,
)
from vibereview.runtime.dto import (
    MAX_ASSESS_EVIDENCE_CANDIDATES,
    AssessEvidenceInvocation,
    AssessEvidenceProposalBundle,
)
from vibereview.runtime.engine import AgentEngine
from vibereview.runtime.hashing import (
    canonical_json_bytes,
    code_fingerprint,
    hash_bytes,
    hash_json,
)
from vibereview.runtime.kernel import ProjectRuntime
from vibereview.runtime.locking import AdvisoryFileLock
from vibereview.runtime.receipts import (
    extract_canonical_object_receipts,
    verify_receipt_canonical_objects,
)
from vibereview.runtime.records import (
    AppliedTaskReceipt,
    RuntimeResult,
    RuntimeModel,
    TaskManifest,
    TaskProvenance,
    TaskSpec,
    TaskType,
)
from vibereview.runtime.registry import CanonicalIdRegistry
from vibereview.runtime.repository import (
    AuxiliaryStagingWriter,
    GenerationStore,
    PromotionPayload,
    StaleSnapshotError,
    UnsafeRepositoryEntryError,
    _open_or_create_directory_at,
    _read_regular_at,
    read_contained_regular_file,
)
from vibereview.runtime.state import RepositorySnapshot

from .models import CorpusIntegrityError
from .retrieval import RetrievalLedger
from .retrieval_promotion import (
    PROMOTION_RECORD_VERSION,
    CoupledEvidenceProposal,
    RetrievalPromotionDecision,
    RetrievalPromotionError,
    RetrievalPromotionRecord,
    RetrievalPromotionRequest,
    _allocated_maps,
    _build_locked_promotion,
    _candidate_source_identity,
    _canonical_request,
    _canonical_source_identity,
    _ledger_bytes,
    _record_bytes,
    _record_relative_path,
    _validate_decisions_against_snapshot,
    _validate_locked_request,
)


ASSESS_EVIDENCE_ADAPTER_CONTRACT_VERSION = "1"
EVIDENCE_TASK_ARTIFACT_VERSION = "1"
EVIDENCE_TASK_ARTIFACT_ROOT = "retrieval/task_promotions"
_LEDGER_RESOURCE_ID = "RES0001"
_LEDGER_LOGICAL_NAME = "retrieval_ledger.json"
_LEDGER_MEDIA_TYPE = "application/json"
_LEDGER_BUNDLE_PATH = Path("input/resources/RES0001/content.json")
_LEDGER_RESOURCE_ROOT = Path("work/task_resources/retrieval_ledgers/sha256")
_LEDGER_RESOURCE_LOCK = ".publish.lock"


class _FrozenRuntimeModel(RuntimeModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AssessEvidencePromotionArtifact(_FrozenRuntimeModel):
    """Immutable witness coupling a task receipt to its retrieval promotion."""

    artifact_version: Literal["1"] = EVIDENCE_TASK_ARTIFACT_VERSION
    promotion_record_version: Literal["1"] = PROMOTION_RECORD_VERSION
    task_id: str = Field(pattern=r"^TASK[0-9]{4,}$")
    accepted_attempt_id: str
    adapter_fingerprint: Sha256
    input_identity_key: Sha256
    semantic_task_key: Sha256
    proposal_hash: Sha256
    receipt_hash: Sha256
    source_generation: int = Field(ge=0)
    committed_generation: int = Field(ge=1)
    claim_id: ClaimId
    query_id: QueryId
    candidate_refs: tuple[Sha256, ...]
    assessed_candidate_refs: tuple[Sha256, ...]
    canonical_span_refs: tuple[SpanId, ...]
    corpus_lock_hash: Sha256
    ledger_hash: Sha256
    request_hash: Sha256
    promotion_record_path: str
    promotion_record_hash: Sha256
    span_ids: dict[Sha256, SpanId]
    evidence_ids: dict[Sha256, EvidenceId]

    @model_validator(mode="after")
    def _identity_is_self_consistent(self) -> "AssessEvidencePromotionArtifact":
        attempt_prefix = f"{self.task_id}/"
        attempt_suffix = self.accepted_attempt_id.removeprefix(attempt_prefix)
        if (
            not self.accepted_attempt_id.startswith(attempt_prefix)
            or not attempt_suffix
            or "/" in attempt_suffix
        ):
            raise ValueError("promotion artifact attempt does not belong to its task")
        if self.committed_generation != self.source_generation + 1:
            raise ValueError("promotion artifact generation lineage is not consecutive")
        if len(self.candidate_refs) != len(set(self.candidate_refs)):
            raise ValueError("promotion artifact candidate refs must be unique")
        if len(self.assessed_candidate_refs) != len(
            set(self.assessed_candidate_refs)
        ):
            raise ValueError("promotion artifact assessed candidate refs must be unique")
        if not set(self.assessed_candidate_refs).issubset(self.candidate_refs):
            raise ValueError(
                "promotion artifact assessed candidates are outside the task candidates"
            )
        if len(self.canonical_span_refs) != len(set(self.canonical_span_refs)):
            raise ValueError("promotion artifact canonical span refs must be unique")
        if set(self.span_ids) != set(self.candidate_refs):
            raise ValueError("promotion artifact span allocations are incomplete")
        if set(self.evidence_ids) != set(self.assessed_candidate_refs):
            raise ValueError(
                "promotion artifact evidence allocations disagree with assessed candidates"
            )
        if self.promotion_record_path != _record_relative_path(
            self.query_id, self.request_hash
        ):
            raise ValueError("promotion artifact record path is not canonical")
        return self


def _artifact_bytes(artifact: AssessEvidencePromotionArtifact) -> bytes:
    return canonical_json_bytes(artifact.model_dump(mode="json")) + b"\n"


def _artifact_relative_path(query_id: str, semantic_task_key: str) -> str:
    digest = semantic_task_key.removeprefix("sha256:")
    return f"{EVIDENCE_TASK_ARTIFACT_ROOT}/{query_id}/{digest}.json"


def _receipt_hash(receipt: AppliedTaskReceipt) -> str:
    return hash_json(receipt.model_dump(mode="json"))


def _adapter_code_fingerprint() -> str:
    return code_fingerprint(
        [
            Path(__file__),
            Path(__file__).with_name("git_source.py"),
            Path(__file__).with_name("graph.py"),
            Path(__file__).with_name("models.py"),
            Path(__file__).with_name("retrieval.py"),
            Path(__file__).with_name("retrieval_promotion.py"),
            Path(__file__).with_name("selection.py"),
        ],
        f"assess-evidence-promotion:{ASSESS_EVIDENCE_ADAPTER_CONTRACT_VERSION}",
    )


def _open_ledger_resource_directory(review_root: Path) -> int:
    """Open/create the private resource directory through no-follow dirfds."""

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptors: list[int] = []
    try:
        try:
            descriptors.append(os.open(review_root, directory_flags))
        except OSError as exc:
            raise UnsafeRepositoryEntryError(
                f"review root is unsafe: {review_root}"
            ) from exc
        display = review_root
        for component in _LEDGER_RESOURCE_ROOT.parts:
            display /= component
            descriptors.append(
                _open_or_create_directory_at(
                    descriptors[-1], component, display
                )
            )
        result = descriptors.pop()
        return result
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _write_once_at(
    directory_descriptor: int,
    name: str,
    display_path: Path,
    content: bytes,
) -> None:
    """Create one immutable resource without following a pathname component."""

    descriptor: int | None = None
    created = False
    try:
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | os.O_CLOEXEC,
                0o600,
                dir_fd=directory_descriptor,
            )
            created = True
        except FileExistsError:
            return
        pending = memoryview(content)
        while pending:
            written = os.write(descriptor, pending)
            if written <= 0:
                raise OSError(
                    f"short write while staging retrieval ledger: {display_path}"
                )
            pending = pending[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.fsync(directory_descriptor)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        if created:
            try:
                os.unlink(name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass
        raise


def _stage_retrieval_ledger_resource(
    review_root: Path, ledger: RetrievalLedger
) -> Path:
    """Publish canonical ledger bytes once under their content digest."""

    resolved_review_root = review_root.resolve(strict=True)
    content = _ledger_bytes(ledger)
    digest = hash_bytes(content).removeprefix("sha256:")
    resource_root = resolved_review_root / _LEDGER_RESOURCE_ROOT
    relative = f"{digest}.json"
    target = resource_root / relative
    resource_descriptor: int | None = None

    def open_resource_directory() -> int:
        nonlocal resource_descriptor
        resource_descriptor = _open_ledger_resource_directory(resolved_review_root)
        return resource_descriptor

    try:
        with AdvisoryFileLock(
            resource_root / _LEDGER_RESOURCE_LOCK,
            directory_opener=open_resource_directory,
        ):
            if resource_descriptor is None:  # pragma: no cover - lock invariant
                raise AssertionError("retrieval ledger lock has no directory handle")
            _write_once_at(resource_descriptor, relative, target, content)
            observed, _ = _read_regular_at(
                resource_descriptor,
                relative,
                target,
                max_bytes=len(content),
            )
            if observed != content:
                raise CorpusIntegrityError(
                    "content-addressed retrieval ledger path contains different bytes"
                )
    except UnsafeRepositoryEntryError as exc:
        raise CorpusIntegrityError(
            "private retrieval-ledger resource path is unsafe"
        ) from exc
    return target


def _canonical_span_allowlist(
    snapshot: RepositorySnapshot, ledger: RetrievalLedger
) -> list[SpanId]:
    """Return exact-source duplicate targets visible to the evidence task."""

    selected = set(ledger.selected_candidate_keys)
    identities = {
        _candidate_source_identity(hit)
        for hit in ledger.raw_hits
        if hit.is_valid and hit.candidate_key in selected
    }
    span_ids = sorted(
        span.span_id
        for span in snapshot.retrieved_spans
        if _canonical_source_identity(span) in identities
    )
    if len(span_ids) > MAX_ASSESS_EVIDENCE_CANDIDATES:
        raise RetrievalPromotionError(
            "exact duplicate-target allowlist exceeds the task candidate budget"
        )
    return span_ids


def _request_from_proposal(
    ledger: RetrievalLedger,
    proposal: AssessEvidenceProposalBundle,
) -> RetrievalPromotionRequest:
    decisions: list[RetrievalPromotionDecision] = []
    for item in proposal.decisions:
        evidence = (
            CoupledEvidenceProposal.model_validate(
                item.evidence.model_dump(mode="json")
            )
            if item.evidence is not None
            else None
        )
        decisions.append(
            RetrievalPromotionDecision(
                candidate_key=item.candidate_ref,
                status=item.status,
                reason=item.reason,
                canonical_span_id=item.canonical_span_ref,
                evidence=evidence,
            )
        )
    return _canonical_request(
        RetrievalPromotionRequest(ledger=ledger, decisions=tuple(decisions))
    )


def _expected_allocation_keys(
    request: RetrievalPromotionRequest,
) -> set[str]:
    keys = {
        f"span:{candidate_ref}"
        for candidate_ref in request.ledger.selected_candidate_keys
    }
    keys.update(
        f"evidence:{decision.candidate_key}"
        for decision in request.decisions
        if decision.status is RetrievalDispositionStatus.ASSESSED
    )
    return keys


class AssessEvidencePromotionAdapter:
    """Coupled adapter for the public ASSESS_EVIDENCE task path."""

    task_type = TaskType.ASSESS_EVIDENCE
    promotion_handler = "promote_evidence"
    rebuild_on_stale = False

    def __init__(self, review_root: Path, ledger: RetrievalLedger) -> None:
        self.review_root = review_root.resolve(strict=False)
        self.ledger = RetrievalLedger.model_validate(
            ledger.model_dump(mode="json")
        )
        if not self.ledger.selected_candidate_keys:
            raise RetrievalPromotionError(
                "ASSESS_EVIDENCE requires at least one selected candidate"
            )
        self.promotion_fingerprint = _adapter_code_fingerprint()

        # Authenticate the complete deterministic ledger before any provider
        # proposal can be requested.  Redundant placeholder decisions exercise
        # the same ledger/corpus/query validator without creating evidence.
        store = GenerationStore(self.review_root)
        source_snapshot, source_registry = store.load_generation(
            self.ledger.source_generation
        )
        selected_hits = {
            hit.candidate_key: hit
            for hit in self.ledger.raw_hits
            if hit.is_valid
        }
        source_spans = tuple(source_snapshot.retrieved_spans)

        def authentication_decision(
            candidate_ref: str,
        ) -> RetrievalPromotionDecision:
            hit = selected_hits[candidate_ref]
            exact_targets = sorted(
                span.span_id
                for span in source_spans
                if _canonical_source_identity(span)
                == _candidate_source_identity(hit)
            )
            if exact_targets:
                return RetrievalPromotionDecision(
                    candidate_key=candidate_ref,
                    status=RetrievalDispositionStatus.DUPLICATE,
                    canonical_span_id=exact_targets[0],
                )
            return RetrievalPromotionDecision(
                candidate_key=candidate_ref,
                status=RetrievalDispositionStatus.REDUNDANT,
                reason="task-adapter ledger authentication",
            )

        authentication_request = RetrievalPromotionRequest(
            ledger=self.ledger,
            decisions=tuple(
                authentication_decision(candidate_ref)
                for candidate_ref in self.ledger.selected_candidate_keys
            ),
        )
        _, source_query = _validate_locked_request(
            self.review_root, authentication_request, source_snapshot
        )
        self._source_snapshot = RepositorySnapshot.model_validate(
            source_snapshot.model_dump(mode="json")
        )
        self._source_registry = CanonicalIdRegistry.model_validate(
            source_registry.model_dump(mode="json")
        )
        self._source_query = source_query.model_copy(deep=True)

    def validate_pre_execution(
        self,
        *,
        project_root: Path,
        spec: TaskSpec,
        invocation,
        manifest: TaskManifest,
        provenance: TaskProvenance,
    ) -> None:
        """Stop an unmatched historical ledger before any engine is invoked."""

        if (
            project_root.resolve(strict=False) != self.review_root
            or spec.task_type is not self.task_type
            or spec.promotion_handler != self.promotion_handler
            or spec.invocation_model is not AssessEvidenceInvocation
            or type(invocation) is not AssessEvidenceInvocation
        ):
            raise CorpusIntegrityError(
                "ASSESS_EVIDENCE pre-execution contract identity disagrees"
            )
        self._require_resource_binding(
            manifest=manifest,
            provenance=provenance,
            invocation=invocation,
            require_source_base=False,
        )
        try:
            expected_dependencies = {
                name: self._source_snapshot.dependency_hash(name)
                for name in spec.dependency_builder(
                    invocation, self._source_snapshot
                )
            }
        except (KeyError, ValueError) as exc:
            raise CorpusIntegrityError(
                "ASSESS_EVIDENCE dependency closure is invalid"
            ) from exc
        if manifest.dependencies != expected_dependencies:
            raise CorpusIntegrityError(
                "task dependencies differ from the exact source-generation closure"
            )
        if manifest.base_generation != self.ledger.source_generation:
            raise StaleSnapshotError(
                "retrieval ledger source generation is no longer CURRENT"
            )

    def _require_contract_types(
        self,
        *,
        spec: TaskSpec,
        proposal: object,
        invocation: object,
    ) -> tuple[AssessEvidenceProposalBundle, AssessEvidenceInvocation]:
        if (
            spec.task_type is not self.task_type
            or spec.promotion_handler != self.promotion_handler
            or spec.proposal_model is not AssessEvidenceProposalBundle
            or spec.invocation_model is not AssessEvidenceInvocation
        ):
            raise CoupledProposalValidationError(
                "ASSESS_EVIDENCE adapter received a mismatched TaskSpec"
            )
        if type(proposal) is not AssessEvidenceProposalBundle:
            raise CoupledProposalValidationError(
                "ASSESS_EVIDENCE adapter received the wrong proposal model"
            )
        if type(invocation) is not AssessEvidenceInvocation:
            raise CoupledProposalValidationError(
                "ASSESS_EVIDENCE adapter received the wrong invocation model"
            )
        return proposal, invocation

    def _request_and_validate_identity(
        self,
        *,
        spec: TaskSpec,
        proposal: object,
        snapshot: RepositorySnapshot,
        dependency_keys: Mapping[str, str],
        invocation: object,
        registry: CanonicalIdRegistry,
        accepted_allocations: Mapping[str, str] | None,
    ) -> tuple[RetrievalPromotionRequest, AssessEvidenceInvocation]:
        proposal_model, invocation_model = self._require_contract_types(
            spec=spec, proposal=proposal, invocation=invocation
        )
        if invocation_model.source_generation != self.ledger.source_generation:
            raise CoupledProposalValidationError(
                "invocation source generation differs from the retrieval ledger"
            )
        if invocation_model.query_id != self.ledger.query_id:
            raise CoupledProposalValidationError(
                "invocation query differs from the retrieval ledger"
            )
        if invocation_model.claim_id != self._source_query.claim_id:
            raise CoupledProposalValidationError(
                "invocation claim differs from the canonical retrieval query"
            )
        if list(invocation_model.candidate_refs) != list(
            self.ledger.selected_candidate_keys
        ):
            raise CoupledProposalValidationError(
                "invocation candidate refs must equal the ordered ledger selection"
            )
        proposal_refs = [item.candidate_ref for item in proposal_model.decisions]
        if proposal_refs != list(invocation_model.candidate_refs):
            raise CoupledProposalValidationError(
                "proposal decisions must equal the ordered invocation candidate refs"
            )
        allowed_spans = set(invocation_model.canonical_span_refs)
        if any(
            item.canonical_span_ref is not None
            and item.canonical_span_ref not in allowed_spans
            for item in proposal_model.decisions
        ):
            raise CoupledProposalValidationError(
                "proposal canonical span target is outside the invocation allowlist"
            )

        try:
            expected_dependency_names = spec.dependency_builder(
                invocation_model, self._source_snapshot
            )
            expected_dependencies = {
                name: self._source_snapshot.dependency_hash(name)
                for name in expected_dependency_names
            }
        except (KeyError, ValueError) as exc:
            raise CoupledProposalValidationError(str(exc)) from exc
        if dict(dependency_keys) != expected_dependencies:
            raise CoupledProposalValidationError(
                "task dependencies differ from the exact source-generation closure"
            )

        if accepted_allocations is None:
            if (
                snapshot != self._source_snapshot
                or registry != self._source_registry
            ):
                raise CoupledProposalValidationError(
                    "retrieval ledger source generation is no longer the task base"
                )

        try:
            request = _request_from_proposal(self.ledger, proposal_model)
            _validate_decisions_against_snapshot(
                request, self._source_snapshot, self._source_query
            )
        except RetrievalPromotionError as exc:
            raise CoupledProposalValidationError(str(exc)) from exc
        except (AssertionError, ValueError) as exc:
            raise CoupledProposalValidationError(str(exc)) from exc

        if accepted_allocations is not None:
            allocated = dict(accepted_allocations)
            if set(allocated) != _expected_allocation_keys(request):
                raise CoupledProposalValidationError(
                    "accepted receipt allocations do not cover the exact decisions"
                )
            if len(allocated.values()) != len(set(allocated.values())):
                raise CoupledProposalValidationError(
                    "accepted receipt allocations repeat a canonical identifier"
                )
        return request, invocation_model

    def validate_proposal(
        self,
        *,
        spec: TaskSpec,
        proposal,
        snapshot: RepositorySnapshot,
        dependency_keys: Mapping[str, str],
        invocation,
        registry: CanonicalIdRegistry,
        accepted_allocations: Mapping[str, str] | None = None,
    ) -> None:
        self._request_and_validate_identity(
            spec=spec,
            proposal=proposal,
            snapshot=snapshot,
            dependency_keys=dependency_keys,
            invocation=invocation,
            registry=registry,
            accepted_allocations=accepted_allocations,
        )

    def _require_resource_binding(
        self,
        *,
        manifest: TaskManifest,
        provenance: TaskProvenance,
        invocation: AssessEvidenceInvocation,
        require_source_base: bool,
    ) -> None:
        if provenance.task_manifest() != manifest:
            raise CorpusIntegrityError("task provenance and manifest disagree")
        if (
            provenance.task_type is not self.task_type
            or provenance.task_spec_version != manifest.task_spec_version
        ):
            raise CorpusIntegrityError("task provenance contract identity disagrees")
        if require_source_base and manifest.base_generation != self.ledger.source_generation:
            raise CorpusIntegrityError(
                "task base generation differs from the retrieval ledger source"
            )
        if (
            invocation.source_generation != self.ledger.source_generation
            or invocation.query_id != self.ledger.query_id
            or invocation.claim_id != self._source_query.claim_id
            or list(invocation.candidate_refs)
            != list(self.ledger.selected_candidate_keys)
        ):
            raise CorpusIntegrityError("task invocation and retrieval ledger disagree")

        expected_content = _ledger_bytes(self.ledger)
        expected_hash = hash_bytes(expected_content)
        if len(provenance.resources) != 1:
            raise CorpusIntegrityError(
                "ASSESS_EVIDENCE requires exactly one retrieval-ledger resource"
            )
        resource = provenance.resources[0]
        invocation_source = invocation.retrieval_ledger_path.resolve(strict=False)
        if (
            resource.resource_id != _LEDGER_RESOURCE_ID
            or resource.logical_name != _LEDGER_LOGICAL_NAME
            or resource.media_type != _LEDGER_MEDIA_TYPE
            or resource.bundle_relative_path != _LEDGER_BUNDLE_PATH
            or resource.snapshot_hash != expected_hash
            or resource.source_dependency.source_hash_at_snapshot != expected_hash
            or resource.size_bytes != len(expected_content)
            or resource.source_path != invocation_source
            or provenance.expected_immutable_files.get(
                _LEDGER_BUNDLE_PATH.as_posix()
            )
            != expected_hash
        ):
            raise CorpusIntegrityError(
                "task resource is not the exact canonical retrieval ledger"
            )

    @staticmethod
    def _record_for_payload(
        request: RetrievalPromotionRequest,
        *,
        next_generation: int,
        payload: PromotionPayload,
    ) -> RetrievalPromotionRecord:
        span_ids, evidence_ids = _allocated_maps(payload.allocated_ids)
        return RetrievalPromotionRecord(
            request_hash=hash_json(request.model_dump(mode="json")),
            ledger_hash=hash_bytes(_ledger_bytes(request.ledger)),
            source_generation=request.ledger.source_generation,
            committed_generation=next_generation,
            query_id=request.ledger.query_id,
            corpus_lock_hash=request.ledger.corpus_lock_hash,
            ledger=request.ledger,
            decisions=request.decisions,
            span_ids=span_ids,
            evidence_ids=evidence_ids,
        )

    def _artifact_for_receipt(
        self,
        *,
        request: RetrievalPromotionRequest,
        invocation: AssessEvidenceInvocation,
        receipt: AppliedTaskReceipt,
        record: RetrievalPromotionRecord,
        record_content: bytes,
    ) -> AssessEvidencePromotionArtifact:
        task_id = receipt.accepted_attempt_id.rsplit("/", 1)[0]
        return AssessEvidencePromotionArtifact(
            task_id=task_id,
            accepted_attempt_id=receipt.accepted_attempt_id,
            adapter_fingerprint=self.promotion_fingerprint,
            input_identity_key=receipt.input_identity_key,
            semantic_task_key=receipt.semantic_task_key,
            proposal_hash=receipt.proposal_hash,
            receipt_hash=_receipt_hash(receipt),
            source_generation=record.source_generation,
            committed_generation=record.committed_generation,
            claim_id=invocation.claim_id,
            query_id=record.query_id,
            candidate_refs=tuple(invocation.candidate_refs),
            assessed_candidate_refs=tuple(
                decision.candidate_key
                for decision in request.decisions
                if decision.status is RetrievalDispositionStatus.ASSESSED
            ),
            canonical_span_refs=tuple(invocation.canonical_span_refs),
            corpus_lock_hash=record.corpus_lock_hash,
            ledger_hash=record.ledger_hash,
            request_hash=record.request_hash,
            promotion_record_path=_record_relative_path(
                record.query_id, record.request_hash
            ),
            promotion_record_hash=hash_bytes(record_content),
            span_ids=dict(record.span_ids),
            evidence_ids=dict(record.evidence_ids),
        )

    def prepare_commit(
        self,
        *,
        project_root: Path,
        spec: TaskSpec,
        proposal,
        invocation,
        manifest: TaskManifest,
        provenance: TaskProvenance,
    ) -> CoupledCommitPlan:
        proposal_model, invocation_model = self._require_contract_types(
            spec=spec, proposal=proposal, invocation=invocation
        )
        if project_root.resolve(strict=False) != self.review_root:
            raise CorpusIntegrityError(
                "ASSESS_EVIDENCE adapter belongs to a different review project"
            )
        self._require_resource_binding(
            manifest=manifest,
            provenance=provenance,
            invocation=invocation_model,
            require_source_base=True,
        )
        request = _request_from_proposal(self.ledger, proposal_model)
        promotion = _build_locked_promotion(self.review_root, request)

        def materialize(
            writer,
            next_generation: int,
            payload: PromotionPayload,
            receipt: AppliedTaskReceipt,
        ) -> None:
            if receipt.local_ref_map != payload.allocated_ids:
                raise CorpusIntegrityError(
                    "accepted receipt allocations differ from promotion payload"
                )
            if (
                receipt.task_type is not self.task_type
                or receipt.source_generation != request.ledger.source_generation
                or receipt.committed_generation != next_generation
                or not receipt.accepted_attempt_id.startswith(
                    f"{manifest.task_id}/"
                )
                or receipt.semantic_fingerprint.promotion_handler_fingerprint
                != self.promotion_fingerprint
            ):
                raise CorpusIntegrityError(
                    "accepted receipt identity differs from evidence promotion"
                )
            try:
                receipt_proposal = AssessEvidenceProposalBundle.model_validate(
                    receipt.proposal_payload
                )
            except ValueError as exc:
                raise CorpusIntegrityError(
                    "accepted receipt proposal payload is invalid"
                ) from exc
            if receipt_proposal != proposal_model:
                raise CorpusIntegrityError(
                    "accepted receipt proposal differs from evidence promotion"
                )
            expected_receipts = extract_canonical_object_receipts(
                self._source_snapshot, payload.snapshot, payload.allocated_ids
            )
            if receipt.canonical_objects != expected_receipts:
                raise CorpusIntegrityError(
                    "accepted receipt canonical objects differ from promotion"
                )

            record = self._record_for_payload(
                request, next_generation=next_generation, payload=payload
            )
            record_content = _record_bytes(record)
            record_relative = _record_relative_path(
                record.query_id, record.request_hash
            )
            artifact = self._artifact_for_receipt(
                request=request,
                invocation=invocation_model,
                receipt=receipt,
                record=record,
                record_content=record_content,
            )
            artifact_relative = _artifact_relative_path(
                record.query_id, receipt.semantic_task_key
            )
            writer.write_bytes(record_relative, record_content)
            writer.write_bytes(artifact_relative, _artifact_bytes(artifact))

        return CoupledCommitPlan(
            promotion=promotion,
            staging_materializer=materialize,
        )

    def verify_receipt_reuse(
        self,
        *,
        project_root: Path,
        spec: TaskSpec,
        proposal,
        invocation,
        manifest: TaskManifest,
        provenance: TaskProvenance,
        receipt: AppliedTaskReceipt,
        current_generation: int,
        current_snapshot: RepositorySnapshot,
        current_registry: CanonicalIdRegistry,
    ) -> tuple[bool, str | None]:
        try:
            proposal_model, invocation_model = self._require_contract_types(
                spec=spec, proposal=proposal, invocation=invocation
            )
            if project_root.resolve(strict=False) != self.review_root:
                raise CorpusIntegrityError(
                    "ASSESS_EVIDENCE adapter belongs to a different review project"
                )
            self._require_resource_binding(
                manifest=manifest,
                provenance=provenance,
                invocation=invocation_model,
                require_source_base=False,
            )
            request = _request_from_proposal(self.ledger, proposal_model)
            if (
                receipt.task_type is not self.task_type
                or receipt.task_spec_version != spec.version
                or receipt.source_generation != request.ledger.source_generation
                or receipt.committed_generation != receipt.source_generation + 1
                or receipt.semantic_fingerprint.promotion_handler_fingerprint
                != self.promotion_fingerprint
                or receipt.proposal_hash != hash_json(receipt.proposal_payload)
                or set(receipt.local_ref_map) != _expected_allocation_keys(request)
            ):
                raise CorpusIntegrityError(
                    "accepted receipt identity differs from the exact evidence request"
                )

            store = GenerationStore(self.review_root)
            source_snapshot, source_registry = store.load_generation(
                request.ledger.source_generation
            )
            expected_payload = _build_locked_promotion(
                self.review_root, request
            )(source_snapshot, source_registry)
            if receipt.local_ref_map != expected_payload.allocated_ids:
                raise CorpusIntegrityError(
                    "accepted receipt allocations differ from deterministic replay"
                )
            expected_canonical = extract_canonical_object_receipts(
                source_snapshot,
                expected_payload.snapshot,
                expected_payload.allocated_ids,
            )
            if receipt.canonical_objects != expected_canonical:
                raise CorpusIntegrityError(
                    "accepted receipt canonical objects differ from deterministic replay"
                )

            record = self._record_for_payload(
                request,
                next_generation=receipt.committed_generation,
                payload=expected_payload,
            )
            record_content = _record_bytes(record)
            record_relative = _record_relative_path(
                record.query_id, record.request_hash
            )
            artifact = self._artifact_for_receipt(
                request=request,
                invocation=invocation_model,
                receipt=receipt,
                record=record,
                record_content=record_content,
            )
            artifact_relative = _artifact_relative_path(
                record.query_id, receipt.semantic_task_key
            )
            committed_snapshot, committed_registry, auxiliary = (
                store.load_generation_auxiliary(
                    receipt.committed_generation,
                    {record_relative, artifact_relative},
                )
            )
            if (
                committed_snapshot != expected_payload.snapshot
                or committed_registry != expected_payload.registry
                or auxiliary[record_relative] != record_content
                or auxiliary[artifact_relative] != _artifact_bytes(artifact)
                or RetrievalPromotionRecord.model_validate_json(
                    auxiliary[record_relative]
                )
                != record
                or AssessEvidencePromotionArtifact.model_validate_json(
                    auxiliary[artifact_relative]
                )
                != artifact
            ):
                raise CorpusIntegrityError(
                    "generation-owned evidence promotion artifact changed"
                )

            committed_receipts = [
                item
                for item in store.load_receipts(receipt.committed_generation)
                if item.semantic_task_key == receipt.semantic_task_key
            ]
            if committed_receipts != [receipt]:
                raise CorpusIntegrityError(
                    "committed generation does not contain the exact accepted receipt"
                )
            if current_generation < receipt.committed_generation:
                raise CorpusIntegrityError(
                    "accepted receipt generation is newer than CURRENT"
                )
            canonical_ok, canonical_reason = verify_receipt_canonical_objects(
                receipt, current_snapshot
            )
            if not canonical_ok:
                raise CorpusIntegrityError(
                    canonical_reason or "accepted canonical objects changed"
                )
            current_registry.validate_covers_identifiers(
                current_snapshot.all_identifiers()
            )
        except Exception as exc:
            return False, f"ASSESS_EVIDENCE_RECEIPT_ARTIFACT_INVALID:{exc}"
        return True, None


def run_assess_evidence_task(
    runtime: ProjectRuntime,
    ledger: RetrievalLedger,
    engines: list[AgentEngine],
) -> RuntimeResult:
    """Run the only public semantic promotion path for retrieved candidates."""

    detached = RetrievalLedger.model_validate(ledger.model_dump(mode="json"))
    adapter = AssessEvidencePromotionAdapter(runtime.project_root, detached)
    source_snapshot, _ = runtime.store.load_generation(detached.source_generation)
    canonical_span_refs = _canonical_span_allowlist(source_snapshot, detached)
    ledger_path = _stage_retrieval_ledger_resource(runtime.project_root, detached)

    allowed_roots = tuple(
        root.resolve(strict=False)
        for root in (runtime.allowed_source_roots or (runtime.project_root,))
    )
    resolved_ledger_path = ledger_path.resolve(strict=True)
    if not any(
        resolved_ledger_path == root or root in resolved_ledger_path.parents
        for root in allowed_roots
    ):
        raise CorpusIntegrityError(
            "runtime allowed source roots exclude its private ledger resources"
        )
    return runtime.run(
        TaskType.ASSESS_EVIDENCE,
        AssessEvidenceInvocation(
            source_generation=detached.source_generation,
            claim_id=adapter._source_query.claim_id,
            query_id=detached.query_id,
            candidate_refs=list(detached.selected_candidate_keys),
            canonical_span_refs=canonical_span_refs,
            retrieval_ledger_path=resolved_ledger_path,
        ),
        engines=engines,
        promotion_adapter=adapter,
    )


__all__ = [
    "ASSESS_EVIDENCE_ADAPTER_CONTRACT_VERSION",
    "AssessEvidencePromotionAdapter",
    "AssessEvidencePromotionArtifact",
    "EVIDENCE_TASK_ARTIFACT_ROOT",
    "EVIDENCE_TASK_ARTIFACT_VERSION",
    "run_assess_evidence_task",
]
