"""Deterministic operator proposal bundles and the Milestone 6 pilot harness.

This module implements the Milestone 6 contract of
``docs/operations/mylib_operational_review.md``: an operator harness that
composes the existing boundaries — pinned import, deterministic retrieval,
coupled evidence promotion, claim aggregation, claim assessment, and final
claim validation — against a fixed operator corpus selection.  Semantic
proposals are deterministic operator artifacts supplied as one versioned
proposal-bundle document held outside the repository.  Every proposal is
served through the explicit offline fixture engine
(``vibereview.runtime.external_engines.build_offline_engine``), one engine
instance per task invocation.  No live provider is constructed, and no
synthetic-pilot journal, packet, or manifest machinery is used.

Bundle format ``vibereview-operational-pilot-1`` keying and resolution rules
(canonical IDs are never authored by the operator):

- Claims are anchored by their exact candidate-claim text.  The harness
  resolves the anchor to the canonical claim ID allocated by the accepted
  ``generate_candidate_claims`` promotion.
- Retrieval queries are anchored by (claim text, query text); the pair must
  identify exactly one canonical query.  The ``claim_ref`` carried to the
  runtime is the resolved canonical claim ID.
- Evidence steps are anchored by (claim text, query text).  Each decision is
  anchored by one span anchor: the source content SHA-256 of the paper's
  pinned Markdown blob plus the candidate's Unicode code-point
  ``[start_offset, end_offset)``.  After retrieval, the anchor resolves to the
  ledger's selected candidate whose paper carries that content hash and whose
  offsets match.  Every selected candidate of an executed ledger requires
  exactly one operator decision, and every operator decision must resolve to a
  selected candidate; any miss fails closed before an engine is invoked.
  ``duplicate``/``redundant`` canonical span targets use the same anchor,
  resolved against already-canonical retrieved spans.
- Aggregation steps are anchored by (claim text, paper content SHA-256).  The
  closure-exact fields (``evidence_refs`` and ``component_relations``) are
  derived by the harness from canonical state — the promotion contract already
  requires them to equal the complete current evidence set partitioned by
  canonical relation — while the operator supplies the semantic judgment
  fields.
- Claim assessments and final validations are anchored by claim text.  Final
  paper relations are anchored by paper content SHA-256 and resolved to the
  canonical claim-paper-evidence records of the current snapshot.

Each step carries a stable, bundle-unique positive integer ``step`` index.
Phases execute in the fixed contract order (candidate claims → retrieval
queries → retrieval + coupled evidence promotion → aggregation → claim
assessment → final validation); steps within one phase execute in ascending
step order.  ``revise_claim`` is intentionally outside this harness: the final
claim wording is itself an operator artifact carried by the final-validation
step.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from vibereview.enums import (
    AggregateRelation,
    AggregateStrength,
    ClaimDecision,
    EvidenceStrength,
    EvidenceSufficiency,
    FinalClaimStatus,
    RejectionBasis,
    RetrievalIntent,
    ValidationCheckResult,
    WithinPaperConsistency,
)
from vibereview.ids import Sha256
from vibereview.models import ComponentRelations
from vibereview.runtime.dto import (
    MAX_DISCOVERY_TEXT_CHARS,
    MAX_EVIDENCE_PROPOSAL_TEXT_CHARS,
    MAX_RETRIEVAL_QUERY_CHARS,
    MAX_RETRIEVAL_QUERY_UTF8_BYTES,
    AggregatePaperEvidenceInvocation,
    AssessClaimInvocation,
    AssessEvidenceProposalBundle,
    ClaimAssessmentProposal,
    ClaimPaperEvidenceProposal,
    ClaimTaskText,
    DiscoveryProposalBundle,
    EvidenceAssessmentProposal,
    EvidenceCandidateDecisionProposal,
    FinalClaimValidationProposal,
    FinalPaperRelationProposal,
    GenerateCandidateClaimsInvocation,
    GenerateRetrievalQueriesInvocation,
    LocalRef,
    NonEmptyClaimTaskText,
    RetrievalQueryProposal,
    RetrievalQueryProposalBundle,
    ValidateFinalClaimInvocation,
)
from vibereview.runtime.external_engines import build_offline_engine
from vibereview.runtime.hashing import hash_bytes
from vibereview.runtime.kernel import ProjectRuntime
from vibereview.runtime.records import (
    AttemptOutcome,
    RuntimeConfig,
    RuntimeModel,
    TaskType,
)
from vibereview.runtime.repository import atomic_write_text, read_contained_regular_file
from vibereview.runtime.state import RepositorySnapshot

from .evidence_task import run_assess_evidence_task
from .git_source import (
    PinnedGitSource,
    compute_content_sha256,
    snapshot_library_state,
)
from .models import (
    CorpusLockManifest,
    CorpusSelectionManifest,
    LibraryConfig,
    LibraryStateSnapshot,
)
from .retrieval import (
    MAX_TOP_K,
    RetrievalLedger,
    UnifiedRetrievalCoordinator,
    VerifiedCorpus,
)
from .selection import import_selected_corpus, load_corpus_lock


OPERATIONAL_PILOT_FORMAT_VERSION = "vibereview-operational-pilot-1"
_MAX_BUNDLE_BYTES = 4 * 1024 * 1024


class OperationalPilotError(RuntimeError):
    """The operator bundle or run state failed a fail-closed pilot check."""


class OperationalSpanAnchor(RuntimeModel):
    """Pinned-blob content hash plus a Unicode code-point slice of that blob."""

    source_sha256: Sha256
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)

    @model_validator(mode="after")
    def _offsets_are_ordered(self) -> "OperationalSpanAnchor":
        if self.end_offset <= self.start_offset:
            raise ValueError("span anchor end_offset must exceed start_offset")
        return self


class OperationalQueryProposal(RuntimeModel):
    """One retrieval query anchored by claim text instead of a canonical ID."""

    local_ref: LocalRef
    claim: str = Field(min_length=1, max_length=MAX_DISCOVERY_TEXT_CHARS)
    intent: RetrievalIntent
    query_text: str = Field(min_length=1, max_length=MAX_RETRIEVAL_QUERY_CHARS)

    @model_validator(mode="after")
    def _query_text_budget(self) -> "OperationalQueryProposal":
        if len(self.query_text.encode("utf-8")) > MAX_RETRIEVAL_QUERY_UTF8_BYTES:
            raise ValueError("retrieval query exceeds its deterministic UTF-8 byte budget")
        return self


class OperationalSpanDecision(RuntimeModel):
    """One disposition decision anchored by a span anchor, not a candidate key."""

    span: OperationalSpanAnchor
    status: Literal["assessed", "duplicate", "redundant"]
    reason: str | None = Field(default=None, max_length=MAX_EVIDENCE_PROPOSAL_TEXT_CHARS)
    canonical_span: OperationalSpanAnchor | None = None
    evidence: EvidenceAssessmentProposal | None = None

    @model_validator(mode="after")
    def _evidence_is_coupled_exactly_once(self) -> "OperationalSpanDecision":
        if self.status == "assessed":
            if self.canonical_span is not None:
                raise ValueError("assessed candidate cannot name a canonical span")
            if self.evidence is None:
                raise ValueError(
                    "assessed candidate requires exactly one evidence proposal"
                )
        elif self.status == "duplicate":
            if self.canonical_span is None:
                raise ValueError("duplicate candidate requires a canonical span")
            if self.evidence is not None:
                raise ValueError("duplicate candidate cannot carry evidence")
        else:
            if self.canonical_span is None and not self.reason:
                raise ValueError(
                    "redundant candidate requires a canonical span or reason"
                )
            if self.evidence is not None:
                raise ValueError("redundant candidate cannot carry evidence")
        return self


class OperationalEvidenceStep(RuntimeModel):
    """Operator decisions for every selected candidate of one anchored query."""

    step: int = Field(ge=1)
    claim: str = Field(min_length=1, max_length=MAX_DISCOVERY_TEXT_CHARS)
    query_text: str = Field(min_length=1, max_length=MAX_RETRIEVAL_QUERY_CHARS)
    decisions: list[OperationalSpanDecision] = Field(min_length=1)

    @model_validator(mode="after")
    def _span_anchors_are_unique(self) -> "OperationalEvidenceStep":
        anchors = [
            (item.span.source_sha256, item.span.start_offset, item.span.end_offset)
            for item in self.decisions
        ]
        if len(anchors) != len(set(anchors)):
            raise ValueError("evidence step span anchors must be unique")
        return self


class OperationalAggregationStep(RuntimeModel):
    """Semantic aggregation judgment for one claim and one anchored paper."""

    step: int = Field(ge=1)
    claim: str = Field(min_length=1, max_length=MAX_DISCOVERY_TEXT_CHARS)
    paper_sha256: Sha256
    relation_to_candidate: AggregateRelation
    strength: EvidenceStrength
    within_paper_consistency: WithinPaperConsistency
    assessment_note: ClaimTaskText


class OperationalClaimAssessmentStep(RuntimeModel):
    """Semantic claim assessment anchored by claim text."""

    step: int = Field(ge=1)
    claim: str = Field(min_length=1, max_length=MAX_DISCOVERY_TEXT_CHARS)
    aggregate_strength: AggregateStrength
    evidence_sufficiency: EvidenceSufficiency
    decision: ClaimDecision
    rejection_basis: RejectionBasis | None = None
    support_summary: ClaimTaskText
    contradiction_summary: ClaimTaskText
    qualification_summary: ClaimTaskText
    reason: ClaimTaskText

    @model_validator(mode="after")
    def _rejection_basis(self) -> "OperationalClaimAssessmentStep":
        if self.decision is ClaimDecision.REJECT and self.rejection_basis is None:
            raise ValueError("REJECT requires rejection_basis")
        if self.decision is not ClaimDecision.REJECT and self.rejection_basis is not None:
            raise ValueError("non-REJECT cannot have rejection_basis")
        return self


class OperationalFinalRelation(RuntimeModel):
    """One final paper relation anchored by paper content hash."""

    paper_sha256: Sha256
    relation_to_final_claim: AggregateRelation


class OperationalFinalValidationStep(RuntimeModel):
    """Final claim validation anchored by claim text and paper content hashes."""

    step: int = Field(ge=1)
    claim: str = Field(min_length=1, max_length=MAX_DISCOVERY_TEXT_CHARS)
    final_claim: NonEmptyClaimTaskText
    status: FinalClaimStatus
    paper_relations: list[OperationalFinalRelation] = Field(default_factory=list)
    scope_check: ValidationCheckResult
    certainty_check: ValidationCheckResult
    causal_language_check: ValidationCheckResult
    numerical_claim_check: ValidationCheckResult
    notes: ClaimTaskText

    @model_validator(mode="after")
    def _valid_status_checks(self) -> "OperationalFinalValidationStep":
        if self.status is not FinalClaimStatus.VALID:
            return self
        if not self.paper_relations:
            raise ValueError("VALID final claim requires at least one paper relation")
        if self.scope_check is not ValidationCheckResult.PASS:
            raise ValueError("VALID final claim requires scope_check=pass")
        if self.certainty_check is not ValidationCheckResult.PASS:
            raise ValueError("VALID final claim requires certainty_check=pass")
        if self.causal_language_check not in {
            ValidationCheckResult.PASS,
            ValidationCheckResult.NOT_APPLICABLE,
        }:
            raise ValueError(
                "VALID final claim requires causal_language_check=pass or not_applicable"
            )
        if self.numerical_claim_check not in {
            ValidationCheckResult.PASS,
            ValidationCheckResult.NOT_APPLICABLE,
        }:
            raise ValueError(
                "VALID final claim requires numerical_claim_check=pass or not_applicable"
            )
        return self


class OperationalPilotBundle(RuntimeModel):
    """Versioned operator proposal bundle; it lives outside the repository."""

    format_version: Literal["vibereview-operational-pilot-1"] = (
        OPERATIONAL_PILOT_FORMAT_VERSION
    )
    operator: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    topic: str = Field(min_length=1, max_length=MAX_DISCOVERY_TEXT_CHARS)
    retrieval_top_k: int = Field(default=10, ge=1, le=MAX_TOP_K)
    discovery: DiscoveryProposalBundle
    queries: list[OperationalQueryProposal] = Field(default_factory=list)
    evidence: list[OperationalEvidenceStep] = Field(default_factory=list)
    aggregations: list[OperationalAggregationStep] = Field(default_factory=list)
    claim_assessments: list[OperationalClaimAssessmentStep] = Field(default_factory=list)
    final_validations: list[OperationalFinalValidationStep] = Field(default_factory=list)

    @model_validator(mode="after")
    def _bundle_is_self_consistent(self) -> "OperationalPilotBundle":
        try:
            datetime.fromisoformat(self.created_at)
        except ValueError as exc:
            raise ValueError("created_at must be an ISO-8601 timestamp") from exc
        claim_texts = [claim.candidate_claim for claim in self.discovery.claims]
        if len(claim_texts) != len(set(claim_texts)):
            raise ValueError("discovery claim texts must be unique anchors")
        anchors = set(claim_texts)
        steps = [
            item.step
            for collection in (
                self.evidence,
                self.aggregations,
                self.claim_assessments,
                self.final_validations,
            )
            for item in collection
        ]
        if len(steps) != len(set(steps)):
            raise ValueError("operator step indexes must be unique within the bundle")
        for collection in (
            self.queries,
            self.evidence,
            self.aggregations,
            self.claim_assessments,
            self.final_validations,
        ):
            for item in collection:
                if item.claim not in anchors:
                    raise ValueError(f"unknown claim anchor: {item.claim!r}")
        local_refs = [item.local_ref for item in self.queries]
        if len(local_refs) != len(set(local_refs)):
            raise ValueError("query local_refs must be unique")
        query_keys = [(item.claim, item.query_text) for item in self.queries]
        if len(query_keys) != len(set(query_keys)):
            raise ValueError("query (claim, query_text) anchors must be unique")
        query_anchor_set = set(query_keys)
        evidence_keys = [(item.claim, item.query_text) for item in self.evidence]
        if len(evidence_keys) != len(set(evidence_keys)):
            raise ValueError("evidence step (claim, query_text) anchors must be unique")
        for key in evidence_keys:
            if key not in query_anchor_set:
                raise ValueError(f"evidence step names an unknown query anchor: {key!r}")
        aggregation_keys = [
            (item.claim, item.paper_sha256) for item in self.aggregations
        ]
        if len(aggregation_keys) != len(set(aggregation_keys)):
            raise ValueError("aggregation (claim, paper) anchors must be unique")
        assessment_claims = [item.claim for item in self.claim_assessments]
        if len(assessment_claims) != len(set(assessment_claims)):
            raise ValueError("each claim may have at most one claim assessment")
        validation_claims = [item.claim for item in self.final_validations]
        if len(validation_claims) != len(set(validation_claims)):
            raise ValueError("each claim may have at most one final validation")
        for claim in validation_claims:
            if claim not in assessment_claims:
                raise ValueError(
                    f"final validation for {claim!r} requires a claim assessment"
                )
        return self


def load_operational_pilot_bundle(path: Path) -> OperationalPilotBundle:
    """Load one bounded, strictly validated proposal bundle from a regular file."""

    if not path.exists():
        raise FileNotFoundError(f"operational pilot bundle not found: {path}")
    content, _ = read_contained_regular_file(
        path.parent, path.name, max_bytes=_MAX_BUNDLE_BYTES
    )
    return OperationalPilotBundle.model_validate_json(content)


def save_operational_pilot_bundle(bundle: OperationalPilotBundle, path: Path) -> None:
    atomic_write_text(path, bundle.model_dump_json(indent=2) + "\n")


@dataclass(frozen=True, slots=True)
class OperationalPilotTaskRecord:
    """One executed runtime task (or one retrieval) in execution order."""

    label: str
    task_type: str | None
    task_id: str
    outcome: str
    generation: int | None
    allocated_ids: dict[str, str]


@dataclass(frozen=True, slots=True)
class OperationalPilotResult:
    """Final repository snapshot plus bounded run diagnostics."""

    review_root: Path
    final_generation: int
    repository_hash: str
    corpus_lock_hash: str
    claim_ids: dict[str, str]
    query_ids: dict[str, str]
    snapshot: RepositorySnapshot
    ledgers: tuple[RetrievalLedger, ...]
    tasks: tuple[OperationalPilotTaskRecord, ...]
    library_before: LibraryStateSnapshot
    library_after: LibraryStateSnapshot


def _anchor_of(anchor: OperationalSpanAnchor) -> tuple[str, int, int]:
    return (anchor.source_sha256, anchor.start_offset, anchor.end_offset)


def _claim_ids(
    snapshot: RepositorySnapshot, bundle: OperationalPilotBundle
) -> dict[str, str]:
    claim_ids: dict[str, str] = {}
    for claim in snapshot.candidate_claims:
        if claim.candidate_claim in claim_ids:
            raise OperationalPilotError(
                f"canonical claim text is not a unique anchor: {claim.candidate_claim!r}"
            )
        claim_ids[claim.candidate_claim] = claim.claim_id
    for anchor in (claim.candidate_claim for claim in bundle.discovery.claims):
        if anchor not in claim_ids:
            raise OperationalPilotError(
                f"bundle claim anchor was not promoted: {anchor!r}"
            )
    return claim_ids


def _paper_ids_by_hash(snapshot: RepositorySnapshot) -> dict[str, str]:
    by_hash: dict[str, str] = {}
    for paper in snapshot.papers:
        if paper.source_hash in by_hash:
            raise OperationalPilotError(
                "canonical paper content hashes are not unique anchors"
            )
        by_hash[paper.source_hash] = paper.paper_id
    return by_hash


def _resolve_paper(by_hash: dict[str, str], anchor: Sha256, label: str) -> str:
    paper_id = by_hash.get(anchor)
    if paper_id is None:
        raise OperationalPilotError(f"{label}: unknown paper content hash {anchor}")
    return paper_id


def _verify_span_locators(
    review_root: Path,
    generation: int,
    snapshot: RepositorySnapshot,
    expected_lock_hash: str,
) -> None:
    """Re-resolve every canonical span against the verified corpus lock."""

    corpus = VerifiedCorpus(review_root, generation=generation)
    if corpus.lock_hash != expected_lock_hash:
        raise OperationalPilotError("verified corpus lock differs from the import")
    papers = {paper.paper_id: paper for paper in snapshot.papers}
    for span in snapshot.retrieved_spans:
        try:
            raw_path, raw_hash, text = corpus.texts[span.paper_id]
        except KeyError as exc:
            raise OperationalPilotError(
                f"retrieved span {span.span_id} names an unlocked paper"
            ) from exc
        locator = span.locator
        if locator.raw_md_path != raw_path:
            raise OperationalPilotError(
                f"retrieved span {span.span_id} raw path differs from the corpus lock"
            )
        paper = papers.get(span.paper_id)
        if paper is None or paper.raw_md_hash != raw_hash:
            raise OperationalPilotError(
                f"retrieved span {span.span_id} paper hash differs from the corpus lock"
            )
        observed = text[locator.start_offset : locator.end_offset]
        if observed != span.source_text:
            raise OperationalPilotError(
                f"retrieved span {span.span_id} source slice differs"
            )
        if hash_bytes(observed.encode("utf-8")) != locator.source_span_hash:
            raise OperationalPilotError(
                f"retrieved span {span.span_id} source hash differs"
            )


def _verify_packet_provenance(
    source: PinnedGitSource,
    snapshot: RepositorySnapshot,
    lock: CorpusLockManifest,
    config: LibraryConfig,
) -> None:
    """Walk ClaimPacket → validation → CPE → evidence → span → pinned blob."""

    if lock.source_commit != config.expected_commit:
        raise OperationalPilotError("corpus lock commit differs from the pin record")
    locked_papers = {paper.paper_id: paper for paper in lock.papers}
    papers = {paper.paper_id: paper for paper in snapshot.papers}
    spans = {span.span_id: span for span in snapshot.retrieved_spans}
    evidence = {item.evidence_id: item for item in snapshot.evidence_records}
    cpes = {item.claim_paper_evidence_id: item for item in snapshot.claim_paper_evidence}
    validations = {item.claim_id: item for item in snapshot.final_claim_validations}
    verified_blobs: dict[str, str] = {}
    for packet in snapshot.claim_packets:
        validation = validations.get(packet.claim_id)
        if validation is None or validation.final_claim != packet.final_claim:
            raise OperationalPilotError(
                f"ClaimPacket {packet.claim_id} lacks its final claim validation"
            )
        if {
            relation.claim_paper_evidence_id for relation in validation.paper_relations
        } != set(packet.claim_paper_evidence_ids):
            raise OperationalPilotError(
                f"ClaimPacket {packet.claim_id} CPE set differs from its validation"
            )
        for cpe_id in packet.claim_paper_evidence_ids:
            cpe = cpes.get(cpe_id)
            if cpe is None or cpe.claim_id != packet.claim_id:
                raise OperationalPilotError(
                    f"ClaimPacket {packet.claim_id} names an unknown CPE {cpe_id}"
                )
            for evidence_id in cpe.evidence_ids:
                record = evidence.get(evidence_id)
                if record is None or record.claim_id != packet.claim_id:
                    raise OperationalPilotError(
                        f"CPE {cpe_id} names an unknown EvidenceRecord {evidence_id}"
                    )
                span = spans.get(record.retrieved_span_id)
                if span is None or span.paper_id != record.paper_id:
                    raise OperationalPilotError(
                        f"EvidenceRecord {evidence_id} names an unknown retrieved span"
                    )
                locked = locked_papers.get(span.paper_id)
                paper = papers.get(span.paper_id)
                if (
                    locked is None
                    or paper is None
                    or paper.source_hash != locked.source_hash
                    or paper.raw_md_hash != locked.raw_md_hash
                ):
                    raise OperationalPilotError(
                        f"retrieved span {span.span_id} is outside the corpus lock"
                    )
                if locked.git_blob_id not in verified_blobs:
                    blob = source.read_blob(locked.git_blob_id)
                    verified_blobs[locked.git_blob_id] = compute_content_sha256(blob)
                if verified_blobs[locked.git_blob_id] != locked.source_hash:
                    raise OperationalPilotError(
                        f"pinned blob {locked.git_blob_id} differs from the corpus lock"
                    )


def run_operational_pilot(
    config: LibraryConfig,
    selection: CorpusSelectionManifest,
    bundle: OperationalPilotBundle,
    review_root: Path,
    public_repository_root: Path,
) -> OperationalPilotResult:
    """Run the deterministic operator pilot chain against one pinned library."""

    before = snapshot_library_state(config)
    source = PinnedGitSource.open(config)
    integrity = source.integrity
    if (
        integrity.expected_commit != config.expected_commit
        or integrity.checked_out_commit != config.expected_commit
        or not integrity.commit_match
    ):
        raise OperationalPilotError(
            "the pinned library does not match the configured commit"
        )

    imported = import_selected_corpus(
        review_root,
        selection,
        config,
        public_repository_root=public_repository_root,
    )
    runtime = ProjectRuntime(
        review_root,
        config=RuntimeConfig(max_fallback_engines=0, technical_attempts_per_engine=1),
        allowed_source_roots=(review_root,),
    )
    tasks: list[OperationalPilotTaskRecord] = []
    ledgers: list[RetrievalLedger] = []

    def run_task(
        label: str,
        task_type: TaskType,
        invocation,
        proposal: RuntimeModel,
    ):
        engine = build_offline_engine(proposal.model_dump(mode="json"))
        result = runtime.run(task_type, invocation, engines=[engine])
        tasks.append(
            OperationalPilotTaskRecord(
                label=label,
                task_type=task_type.value,
                task_id=result.task_id,
                outcome=result.outcome.value,
                generation=result.generation,
                allocated_ids=dict(result.allocated_ids),
            )
        )
        if result.outcome is not AttemptOutcome.VALID_SCIENTIFIC_RESULT:
            detail = "; ".join(
                error
                for record in result.attempt_records
                for error in record.validation_errors
            )
            raise OperationalPilotError(
                f"{label} failed with {result.outcome.value}"
                + (f": {detail}" if detail else "")
            )
        return result

    def current_snapshot() -> RepositorySnapshot:
        _, snapshot, _ = runtime.store.load_current()
        return snapshot

    # Phase 1: candidate claims.
    run_task(
        "candidate-claims",
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        GenerateCandidateClaimsInvocation(topic=bundle.topic, existing_theme_ids=[]),
        bundle.discovery,
    )
    claim_ids = _claim_ids(current_snapshot(), bundle)

    # Phase 2: retrieval queries.
    query_ids: dict[str, str] = {}
    if bundle.queries:
        proposal = RetrievalQueryProposalBundle(
            queries=[
                RetrievalQueryProposal(
                    local_ref=item.local_ref,
                    claim_ref=claim_ids[item.claim],
                    intent=item.intent,
                    query_text=item.query_text,
                )
                for item in bundle.queries
            ]
        )
        query_result = run_task(
            "retrieval-queries",
            TaskType.GENERATE_RETRIEVAL_QUERIES,
            GenerateRetrievalQueriesInvocation(
                claim_ids=sorted(set(claim_ids.values()))
            ),
            proposal,
        )
        query_ids = dict(query_result.allocated_ids)

    # Phase 3: deterministic retrieval plus coupled evidence promotion.
    remaining_evidence = {(item.claim, item.query_text): item for item in bundle.evidence}
    for query in sorted(
        current_snapshot().retrieval_queries, key=lambda item: item.query_id
    ):
        coordinator = UnifiedRetrievalCoordinator.from_generation(review_root)
        _, ledger = coordinator.retrieve(
            query.query_text,
            query.query_id,
            query.intent,
            top_k=bundle.retrieval_top_k,
        )
        ledgers.append(ledger)
        claim_anchor = next(
            text for text, claim_id in claim_ids.items() if claim_id == query.claim_id
        )
        step = remaining_evidence.pop((claim_anchor, query.query_text), None)
        if not ledger.selected_candidate_keys:
            if step is not None:
                raise OperationalPilotError(
                    f"step {step.step}: the query selected no candidates but the "
                    "bundle carries decisions for it"
                )
            continue
        if step is None:
            raise OperationalPilotError(
                f"retrieval for {query.query_id} selected "
                f"{len(ledger.selected_candidate_keys)} candidates without operator "
                "decisions; refusing to continue"
            )
        snapshot = current_snapshot()
        by_hash = _paper_ids_by_hash(snapshot)
        hash_by_paper = {paper_id: source_hash for source_hash, paper_id in by_hash.items()}
        decisions_by_anchor = {
            _anchor_of(item.span): item for item in step.decisions
        }
        hits = {hit.candidate_key: hit for hit in ledger.raw_hits if hit.is_valid}
        resolved: list[EvidenceCandidateDecisionProposal] = []
        for candidate_key in ledger.selected_candidate_keys:
            hit = hits[candidate_key]
            anchor = (hash_by_paper[hit.paper_id], hit.start_offset, hit.end_offset)
            decision = decisions_by_anchor.pop(anchor, None)
            if decision is None:
                raise OperationalPilotError(
                    f"step {step.step}: no operator decision for retrieved candidate "
                    f"{anchor!r}; refusing to continue"
                )
            canonical_span_ref = None
            if decision.canonical_span is not None:
                target = _resolve_paper(
                    by_hash, decision.canonical_span.source_sha256, f"step {step.step}"
                )
                matches = [
                    span
                    for span in snapshot.retrieved_spans
                    if span.paper_id == target
                    and span.locator.start_offset == decision.canonical_span.start_offset
                    and span.locator.end_offset == decision.canonical_span.end_offset
                ]
                if len(matches) != 1:
                    raise OperationalPilotError(
                        f"step {step.step}: canonical span anchor does not resolve "
                        "to exactly one existing retrieved span"
                    )
                canonical_span_ref = matches[0].span_id
            resolved.append(
                EvidenceCandidateDecisionProposal(
                    candidate_ref=candidate_key,
                    status=decision.status,
                    reason=decision.reason,
                    canonical_span_ref=canonical_span_ref,
                    evidence=decision.evidence,
                )
            )
        if decisions_by_anchor:
            raise OperationalPilotError(
                f"step {step.step}: operator decisions did not resolve to retrieved "
                f"candidates: {sorted(decisions_by_anchor)!r}"
            )
        engine = build_offline_engine(
            AssessEvidenceProposalBundle(decisions=resolved).model_dump(mode="json")
        )
        result = run_assess_evidence_task(runtime, ledger, [engine])
        tasks.append(
            OperationalPilotTaskRecord(
                label=f"evidence:{query.query_id}",
                task_type=TaskType.ASSESS_EVIDENCE.value,
                task_id=result.task_id,
                outcome=result.outcome.value,
                generation=result.generation,
                allocated_ids=dict(result.allocated_ids),
            )
        )
        if result.outcome is not AttemptOutcome.VALID_SCIENTIFIC_RESULT:
            detail = "; ".join(
                error
                for record in result.attempt_records
                for error in record.validation_errors
            )
            raise OperationalPilotError(
                f"evidence:{query.query_id} failed with {result.outcome.value}"
                + (f": {detail}" if detail else "")
            )
    if remaining_evidence:
        raise OperationalPilotError(
            "operator evidence steps were not consumed by any executed query: "
            f"{sorted(remaining_evidence)!r}"
        )

    # Phase 4: per claim+paper aggregation.
    aggregated: set[tuple[str, str]] = set()
    for step in sorted(bundle.aggregations, key=lambda item: item.step):
        snapshot = current_snapshot()
        claim_id = claim_ids[step.claim]
        paper_id = _resolve_paper(
            _paper_ids_by_hash(snapshot), step.paper_sha256, f"step {step.step}"
        )
        records = [
            item
            for item in snapshot.evidence_records
            if item.claim_id == claim_id and item.paper_id == paper_id
        ]
        if not records:
            raise OperationalPilotError(
                f"step {step.step}: no canonical evidence for the anchored "
                "claim and paper"
            )
        grouped: dict[str, list[str]] = {
            field: [] for field in ComponentRelations.model_fields
        }
        for record in records:
            grouped[record.relation_to_candidate.value].append(record.evidence_id)
        proposal = ClaimPaperEvidenceProposal(
            claim_ref=claim_id,
            paper_ref=paper_id,
            evidence_refs=[record.evidence_id for record in records],
            relation_to_candidate=step.relation_to_candidate,
            component_relations=ComponentRelations(**grouped),
            strength=step.strength,
            within_paper_consistency=step.within_paper_consistency,
            assessment_note=step.assessment_note,
        )
        run_task(
            f"aggregate:{claim_id}:{paper_id}",
            TaskType.AGGREGATE_PAPER_EVIDENCE,
            AggregatePaperEvidenceInvocation(
                claim_id=claim_id,
                paper_id=paper_id,
                evidence_ids=[record.evidence_id for record in records],
            ),
            proposal,
        )
        aggregated.add((claim_id, paper_id))
    for record in current_snapshot().evidence_records:
        if (record.claim_id, record.paper_id) not in aggregated:
            raise OperationalPilotError(
                f"EvidenceRecord {record.evidence_id} has no operator aggregation step"
            )

    # Phase 5: claim assessments.
    for step in sorted(bundle.claim_assessments, key=lambda item: item.step):
        snapshot = current_snapshot()
        claim_id = claim_ids[step.claim]
        cpe_ids = sorted(
            item.claim_paper_evidence_id
            for item in snapshot.claim_paper_evidence
            if item.claim_id == claim_id
        )
        proposal = ClaimAssessmentProposal(
            claim_ref=claim_id,
            aggregate_strength=step.aggregate_strength,
            evidence_sufficiency=step.evidence_sufficiency,
            decision=step.decision,
            rejection_basis=step.rejection_basis,
            support_summary=step.support_summary,
            contradiction_summary=step.contradiction_summary,
            qualification_summary=step.qualification_summary,
            reason=step.reason,
        )
        run_task(
            f"assess-claim:{claim_id}",
            TaskType.ASSESS_CLAIM,
            AssessClaimInvocation(claim_id=claim_id, claim_paper_evidence_ids=cpe_ids),
            proposal,
        )

    # Phase 6: final claim validations.
    for step in sorted(bundle.final_validations, key=lambda item: item.step):
        snapshot = current_snapshot()
        claim_id = claim_ids[step.claim]
        by_hash = _paper_ids_by_hash(snapshot)
        cpe_ids = {
            (item.claim_id, item.paper_id): item.claim_paper_evidence_id
            for item in snapshot.claim_paper_evidence
        }
        relations: list[FinalPaperRelationProposal] = []
        for relation in step.paper_relations:
            paper_id = _resolve_paper(
                by_hash, relation.paper_sha256, f"step {step.step}"
            )
            cpe_id = cpe_ids.get((claim_id, paper_id))
            if cpe_id is None:
                raise OperationalPilotError(
                    f"step {step.step}: no canonical claim-paper evidence for the "
                    "anchored claim and paper"
                )
            relations.append(
                FinalPaperRelationProposal(
                    claim_paper_evidence_ref=cpe_id,
                    paper_ref=paper_id,
                    relation_to_final_claim=relation.relation_to_final_claim,
                )
            )
        proposal = FinalClaimValidationProposal(
            claim_ref=claim_id,
            final_claim=step.final_claim,
            status=step.status,
            paper_relations=relations,
            scope_check=step.scope_check,
            certainty_check=step.certainty_check,
            causal_language_check=step.causal_language_check,
            numerical_claim_check=step.numerical_claim_check,
            notes=step.notes,
        )
        run_task(
            f"validate-final:{claim_id}",
            TaskType.VALIDATE_FINAL_CLAIM,
            ValidateFinalClaimInvocation(
                claim_id=claim_id, proposed_final_claim=step.final_claim
            ),
            proposal,
        )

    # Provenance assertions over the final committed snapshot.
    generation, snapshot, _ = runtime.store.load_current()
    snapshot.validate_repository()
    _verify_span_locators(
        review_root, generation, snapshot, imported.corpus_lock_hash
    )
    lock, _ = load_corpus_lock(review_root, generation=generation)
    _verify_packet_provenance(source, snapshot, lock, config)
    after = snapshot_library_state(config)
    if after != before:
        raise OperationalPilotError("the library state snapshot changed during the run")

    return OperationalPilotResult(
        review_root=review_root,
        final_generation=generation,
        repository_hash=snapshot.canonical_hash(),
        corpus_lock_hash=imported.corpus_lock_hash,
        claim_ids=claim_ids,
        query_ids=query_ids,
        snapshot=snapshot,
        ledgers=tuple(ledgers),
        tasks=tuple(tasks),
        library_before=before,
        library_after=after,
    )


__all__ = [
    "OPERATIONAL_PILOT_FORMAT_VERSION",
    "OperationalAggregationStep",
    "OperationalClaimAssessmentStep",
    "OperationalEvidenceStep",
    "OperationalFinalRelation",
    "OperationalFinalValidationStep",
    "OperationalPilotBundle",
    "OperationalPilotError",
    "OperationalPilotResult",
    "OperationalPilotTaskRecord",
    "OperationalQueryProposal",
    "OperationalSpanAnchor",
    "OperationalSpanDecision",
    "load_operational_pilot_bundle",
    "run_operational_pilot",
    "save_operational_pilot_bundle",
]
