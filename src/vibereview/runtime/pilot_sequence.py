"""Fixed stage-order validation for the bounded synthetic Package C pilot.

This is intentionally a single finite sequence policy, not a general workflow
or DAG engine.  The journal proves event integrity; this module additionally
proves that those authentic events occur in the only supported pilot order.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from vibereview.enums import ClaimDecision, FinalClaimStatus, RetrievalIntent

from .pilot_journal import (
    PilotControlArtifact,
    PilotExactAssemblyControlPayload,
    PilotRetrievalControlPayload,
    PilotStageArtifact,
    PilotStageArtifactReference,
    PilotValidationControlPayload,
    build_pilot_stage_artifact,
    load_pilot_control_artifact,
    load_pilot_stage_artifact,
    validate_pilot_control_artifact_binding,
)
from .pilot_records import PilotStage, PilotStageStatus, ValidationStatus
from .records import AppliedTaskReceipt, TaskType
from .repository import GenerationStore
from .state import RepositorySnapshot


class PilotSequenceError(ValueError):
    """An authentic journal chain violates the fixed pilot sequence."""


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

_CONTROL_PHASE: dict[PilotStage, int] = {
    PilotStage.PREREQUISITES: 0,
    PilotStage.RETRIEVAL: 5,
    PilotStage.EXACT_ASSEMBLY: 11,
    PilotStage.VALIDATION_REPORT: 12,
}

_SINGLE_TASKS = frozenset(
    {
        TaskType.PARSE_DEEP_RESEARCH,
        TaskType.CORPUS_CHALLENGER,
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        TaskType.GENERATE_RETRIEVAL_QUERIES,
    }
)

_REQUIRED_PREFIX_TASKS = (
    TaskType.PARSE_DEEP_RESEARCH,
    TaskType.CORPUS_CHALLENGER,
    TaskType.GENERATE_CANDIDATE_CLAIMS,
    TaskType.GENERATE_RETRIEVAL_QUERIES,
)

_REQUIRED_RETRIEVAL_INTENTS = frozenset(
    {
        RetrievalIntent.SUPPORT,
        RetrievalIntent.CONTRADICTION,
        RetrievalIntent.BOUNDARY,
        RetrievalIntent.ALTERNATIVE,
    }
)


@dataclass(frozen=True, slots=True)
class PilotSequenceSummary:
    run_id: str
    head: PilotStageArtifactReference
    event_count: int
    semantic_task_count: int
    terminal: bool
    closure_complete: bool
    stages: tuple[PilotStage, ...]


def _reference_from_artifact(
    artifact: PilotStageArtifact,
) -> PilotStageArtifactReference:
    try:
        prepared = build_pilot_stage_artifact(
            registration=artifact.registration,
            run_manifest=artifact.run_manifest,
            stage_record=artifact.stage_record,
            task_provenance=artifact.task_provenance,
            accepted_receipt=artifact.accepted_receipt,
            engine_plan_hash=artifact.engine_plan_hash,
        )
    except Exception as exc:
        raise PilotSequenceError("pilot sequence contains an invalid artifact") from exc
    if prepared.artifact != artifact:
        raise PilotSequenceError("pilot sequence artifact is not canonical")
    return prepared.reference


def _reference_from_predecessor(predecessor) -> PilotStageArtifactReference:
    return PilotStageArtifactReference(
        run_id=predecessor.run_id,
        ordinal=predecessor.ordinal,
        stage=predecessor.stage,
        owner_generation=predecessor.owner_generation,
        stage_record_hash=predecessor.stage_record_hash,
        artifact_hash=predecessor.artifact_hash,
        relative_path=predecessor.relative_path,
    )


def load_fixed_pilot_sequence(
    project_root: Path,
    head: PilotStageArtifactReference,
) -> tuple[PilotStageArtifact, ...]:
    """Load the authentic chain and return it in execution order."""

    reverse: list[PilotStageArtifact] = []
    reference = PilotStageArtifactReference.model_validate(
        head.model_dump(mode="json")
    )
    while True:
        artifact = load_pilot_stage_artifact(project_root, reference)
        reverse.append(artifact)
        predecessor = artifact.stage_record.previous_stage
        if predecessor is None:
            break
        reference = _reference_from_predecessor(predecessor)
    return tuple(reversed(reverse))


def _receipt(artifact: PilotStageArtifact) -> AppliedTaskReceipt:
    if artifact.accepted_receipt is None:  # pragma: no cover - model invariant
        raise PilotSequenceError("semantic pilot event lacks its receipt")
    return artifact.accepted_receipt


def _qualified_ids(receipt: AppliedTaskReceipt, object_type: str) -> tuple[str, ...]:
    prefix = f"{object_type}:"
    return tuple(
        item.qualified_id.removeprefix(prefix)
        for item in receipt.canonical_objects
        if item.qualified_id.startswith(prefix)
    )


def _task_claim_id(artifact: PilotStageArtifact) -> str:
    proposal = _receipt(artifact).proposal_payload
    value = proposal.get("claim_ref")
    if not isinstance(value, str):
        raise PilotSequenceError("claim task receipt lacks an exact claim_ref")
    return value


def _validate_claim_pairing(
    task_events: Sequence[PilotStageArtifact],
    snapshot: RepositorySnapshot,
    *,
    require_terminal: bool = True,
) -> None:
    assessments: dict[str, tuple[int, PilotStageArtifact]] = {}
    revisions: dict[str, tuple[int, PilotStageArtifact]] = {}
    validations: dict[str, tuple[int, PilotStageArtifact]] = {}
    aggregates: dict[str, list[int]] = {}

    for index, artifact in enumerate(task_events):
        task_type = artifact.task_provenance.task_type  # type: ignore[union-attr]
        if task_type not in {
            TaskType.AGGREGATE_PAPER_EVIDENCE,
            TaskType.ASSESS_CLAIM,
            TaskType.REVISE_CLAIM,
            TaskType.VALIDATE_FINAL_CLAIM,
        }:
            continue
        claim_id = _task_claim_id(artifact)
        if task_type is TaskType.AGGREGATE_PAPER_EVIDENCE:
            aggregates.setdefault(claim_id, []).append(index)
            continue
        target = {
            TaskType.ASSESS_CLAIM: assessments,
            TaskType.REVISE_CLAIM: revisions,
            TaskType.VALIDATE_FINAL_CLAIM: validations,
        }[task_type]
        if claim_id in target:
            raise PilotSequenceError(
                f"pilot repeats {task_type.value} for claim {claim_id}"
            )
        target[claim_id] = (index, artifact)

    if set(revisions) - set(assessments) or set(validations) - set(assessments):
        raise PilotSequenceError("claim revision or validation lacks assessment")
    candidate_ids = {item.claim_id for item in snapshot.candidate_claims}
    snapshot_assessments = {
        item.claim_id: item for item in snapshot.claim_assessments
    }
    if set(assessments) != candidate_ids or set(snapshot_assessments) != candidate_ids:
        raise PilotSequenceError(
            "every CandidateClaim must have exactly one accepted ClaimAssessment"
        )
    snapshot_validations = {
        item.claim_id: item for item in snapshot.final_claim_validations
    }
    if set(validations) != set(snapshot_validations):
        raise PilotSequenceError(
            "final-validation tasks and canonical results differ"
        )

    for claim_id, (assessment_index, assessment_artifact) in assessments.items():
        if any(index > assessment_index for index in aggregates.get(claim_id, ())):
            raise PilotSequenceError(
                "claim-paper aggregation occurs after claim assessment"
            )
        receipt = _receipt(assessment_artifact)
        proposal = receipt.proposal_payload
        try:
            decision = ClaimDecision(proposal["decision"])
        except Exception as exc:
            raise PilotSequenceError("claim assessment decision is invalid") from exc

        assessment_ids = _qualified_ids(receipt, "ClaimAssessment")
        if assessment_ids != (claim_id,):
            raise PilotSequenceError(
                "claim assessment receipt lacks its exact canonical witness"
            )
        candidate_hashes = {
            item.object_hash
            for item in receipt.canonical_objects
            if item.qualified_id == f"ClaimAssessment:{claim_id}"
        }
        if len(candidate_hashes) != 1:
            raise PilotSequenceError("claim assessment witness is ambiguous")
        assessment_hash = next(iter(candidate_hashes))
        expected_cpe_ids = {
            item.claim_paper_evidence_id
            for item in snapshot.claim_paper_evidence
            if item.claim_id == claim_id
        }
        observed_cpe_dependencies = {
            key.removeprefix("ClaimPaperEvidence:")
            for key in assessment_artifact.task_provenance.dependencies  # type: ignore[union-attr]
            if key.startswith("ClaimPaperEvidence:")
        }
        if observed_cpe_dependencies != expected_cpe_ids:
            raise PilotSequenceError(
                "claim assessment does not bind the complete CPE set"
            )

        revision_entry = revisions.get(claim_id)
        validation_entry = validations.get(claim_id)
        if decision is ClaimDecision.REJECT:
            if revision_entry is not None:
                raise PilotSequenceError("rejected claim cannot be revised")
            if validation_entry is not None or claim_id in snapshot_validations:
                raise PilotSequenceError(
                    "rejected ClaimAssessment cannot yield final validation"
                )
            continue
        elif validation_entry is None:
            if require_terminal:
                raise PilotSequenceError(
                    f"downstream-eligible claim {claim_id} lacks final validation"
                )
            continue

        assert validation_entry is not None
        validation_index, validation_artifact = validation_entry
        if validation_index <= assessment_index:
            raise PilotSequenceError("final claim validation precedes assessment")
        validation_receipt = _receipt(validation_artifact)
        final_payload = validation_receipt.proposal_payload
        final_text = final_payload.get("final_claim")
        if not isinstance(final_text, str) or not final_text:
            raise PilotSequenceError("final claim receipt lacks exact claim text")

        if decision is ClaimDecision.RETAIN:
            if revision_entry is not None:
                raise PilotSequenceError("retained claim cannot use a revision task")
            claim_text = next(
                item.candidate_claim
                for item in snapshot.candidate_claims
                if item.claim_id == claim_id
            )
            if final_text != claim_text:
                raise PilotSequenceError(
                    "retained final claim differs from CandidateClaim text"
                )
            expected_dependencies = dict(assessment_artifact.task_provenance.dependencies)  # type: ignore[union-attr]
            expected_dependencies[f"ClaimAssessment:{claim_id}"] = assessment_hash
            if validation_artifact.task_provenance.dependencies != dict(  # type: ignore[union-attr]
                sorted(expected_dependencies.items())
            ):
                raise PilotSequenceError(
                    "retained final validation evidence context drifted"
                )
        elif decision in {
            ClaimDecision.WEAKEN,
            ClaimDecision.NARROW,
            ClaimDecision.REFORMULATE,
        }:
            if revision_entry is None:
                raise PilotSequenceError(
                    "modified claim lacks its accepted revision task"
                )
            revision_index, revision_artifact = revision_entry
            if not assessment_index < revision_index < validation_index:
                raise PilotSequenceError("claim revision is outside its claim pair")
            revised_text = _receipt(revision_artifact).proposal_payload.get(
                "final_claim"
            )
            if revised_text != final_text:
                raise PilotSequenceError(
                    "final validation text differs from accepted revision"
                )
            if (
                revision_artifact.task_provenance.dependencies  # type: ignore[union-attr]
                != validation_artifact.task_provenance.dependencies  # type: ignore[union-attr]
            ):
                raise PilotSequenceError(
                    "revision and final validation evidence contexts differ"
                )

        try:
            status = FinalClaimStatus(final_payload["status"])
        except Exception as exc:
            raise PilotSequenceError("final claim status is invalid") from exc
        packet_ids = _qualified_ids(validation_receipt, "ClaimPacket")
        snapshot_validation = snapshot_validations[claim_id]
        if snapshot_validation.status is not status:
            raise PilotSequenceError(
                "final validation receipt and canonical status differ"
            )
        if status is FinalClaimStatus.VALID:
            if packet_ids != (claim_id,):
                raise PilotSequenceError(
                    "VALID final claim lacks its exact ClaimPacket witness"
                )
        elif packet_ids:
            raise PilotSequenceError(
                "non-VALID final claim cannot yield a ClaimPacket"
            )


def _validate_partial_claim_order(
    task_events: Sequence[PilotStageArtifact],
) -> None:
    """Reject irreversible same-phase claim ordering as soon as it appears.

    Full claim coverage is intentionally checked only at the later phase gate,
    but a new CPE aggregate can never be accepted after the claim assessment it
    would invalidate.  Running this partial check for every prefix prevents an
    invalid phase-7 journal from becoming CURRENT first.
    """

    assessed_claims: set[str] = set()
    for artifact in task_events:
        provenance = artifact.task_provenance
        if provenance is None:
            continue
        task_type = provenance.task_type
        if task_type not in {
            TaskType.AGGREGATE_PAPER_EVIDENCE,
            TaskType.ASSESS_CLAIM,
        }:
            continue
        claim_id = _task_claim_id(artifact)
        if task_type is TaskType.AGGREGATE_PAPER_EVIDENCE:
            if claim_id in assessed_claims:
                raise PilotSequenceError(
                    "claim-paper aggregation occurs after claim assessment"
                )
        else:
            assessed_claims.add(claim_id)


def _validate_evidence_partition(
    snapshot: RepositorySnapshot,
    task_events: Sequence[PilotStageArtifact],
) -> None:
    evidence_ids = {item.evidence_id for item in snapshot.evidence_records}
    evidence_owners = Counter(
        evidence_id
        for item in snapshot.claim_paper_evidence
        for evidence_id in item.evidence_ids
    )
    if set(evidence_owners) != evidence_ids or any(
        count != 1 for count in evidence_owners.values()
    ):
        raise PilotSequenceError(
            "every EvidenceRecord must belong to exactly one CPE"
        )
    evidence_by_group: dict[tuple[str, str], set[str]] = {}
    for item in snapshot.evidence_records:
        evidence_by_group.setdefault((item.claim_id, item.paper_id), set()).add(
            item.evidence_id
        )
    cpe_by_group = {
        (item.claim_id, item.paper_id): item
        for item in snapshot.claim_paper_evidence
    }
    if set(cpe_by_group) != set(evidence_by_group):
        raise PilotSequenceError(
            "CPE records do not cover every nonempty claim-paper evidence group"
        )
    for key, identifiers in evidence_by_group.items():
        if set(cpe_by_group[key].evidence_ids) != identifiers:
            raise PilotSequenceError("CPE evidence membership is incomplete")

    evidence_receipt_ids = [
        identifier
        for artifact in task_events
        if artifact.task_provenance.task_type is TaskType.ASSESS_EVIDENCE  # type: ignore[union-attr]
        for identifier in _qualified_ids(_receipt(artifact), "EvidenceRecord")
    ]
    aggregate_outputs = [
        _qualified_ids(_receipt(artifact), "ClaimPaperEvidence")
        for artifact in task_events
        if artifact.task_provenance.task_type  # type: ignore[union-attr]
        is TaskType.AGGREGATE_PAPER_EVIDENCE
    ]
    if Counter(evidence_receipt_ids) != Counter(
        {identifier: 1 for identifier in evidence_ids}
    ):
        raise PilotSequenceError(
            "evidence task receipts do not cover canonical EvidenceRecords exactly"
        )
    canonical_cpe_ids = {
        item.claim_paper_evidence_id for item in snapshot.claim_paper_evidence
    }
    if (
        any(len(outputs) != 1 for outputs in aggregate_outputs)
        or Counter(
            identifier
            for outputs in aggregate_outputs
            for identifier in outputs
        )
        != Counter({identifier: 1 for identifier in canonical_cpe_ids})
    ):
        raise PilotSequenceError(
            "each canonical CPE requires exactly one aggregate task"
        )


def _validate_claim_packet_closure(snapshot: RepositorySnapshot) -> None:
    """Require final-validation and ClaimPacket state to form an exact partition."""

    validation_by_claim = {
        item.claim_id: item for item in snapshot.final_claim_validations
    }
    packet_by_claim = {item.claim_id: item for item in snapshot.claim_packets}
    valid_claims = {
        claim_id
        for claim_id, item in validation_by_claim.items()
        if item.status is FinalClaimStatus.VALID
    }
    if set(packet_by_claim) != valid_claims:
        raise PilotSequenceError(
            "ClaimPacket presence must exactly equal VALID final claims"
        )
    for claim_id, packet in packet_by_claim.items():
        validation = validation_by_claim[claim_id]
        final_cpe_ids = {
            item.claim_paper_evidence_id
            for item in snapshot.claim_paper_evidence
            if item.claim_id == claim_id
        }
        relation_ids = {
            item.claim_paper_evidence_id for item in validation.paper_relations
        }
        if set(packet.claim_paper_evidence_ids) != final_cpe_ids or relation_ids != final_cpe_ids:
            raise PilotSequenceError(
                "ClaimPacket and final validation do not cover the full CPE set"
            )


def _validate_snapshot_closure(
    snapshot: RepositorySnapshot,
    task_events: Sequence[PilotStageArtifact],
) -> None:
    _validate_evidence_partition(snapshot, task_events)
    _validate_claim_packet_closure(snapshot)

    packet_by_claim = {item.claim_id: item for item in snapshot.claim_packets}
    packet_ids = set(packet_by_claim)
    proposition_claim_ids = {
        claim_id
        for item in snapshot.proposition_records
        for claim_id in item.claim_ids
    }
    proposition_source_ids = {
        key.removeprefix("ClaimPacket:")
        for artifact in task_events
        if artifact.task_provenance.task_type is TaskType.GENERATE_PROPOSITIONS  # type: ignore[union-attr]
        for key in artifact.task_provenance.dependencies  # type: ignore[union-attr]
        if key.startswith("ClaimPacket:")
    }
    if proposition_claim_ids != packet_ids or proposition_source_ids != packet_ids:
        raise PilotSequenceError(
            "proposition sources do not cover every ClaimPacket exactly"
        )
    if not packet_ids and not any(
        item.corpus_fact_ids or item.process_fact_ids
        for item in snapshot.proposition_records
    ):
        raise PilotSequenceError(
            "zero-claim closure requires a corpus/process-sourced proposition"
        )

def _validate_sentence_audit_receipt(artifact: PilotStageArtifact) -> None:
    """Bind a sentence verdict to its immutable receipt without inventing state."""

    receipt = _receipt(artifact)
    transition = receipt.recorded_transition
    verdict = receipt.proposal_payload.get("verdict")
    eligible = verdict == "ENTAILED"
    sentence_ids = _qualified_ids(receipt, "RenderedSentence")
    audit_ids = _qualified_ids(receipt, "RenderedSentenceAudit")
    if (
        not isinstance(verdict, str)
        or transition.scientific_disposition != verdict
        or transition.downstream_eligible is not eligible
        or transition.canonicalized is not True
    ):
        raise PilotSequenceError(
            "rendered-sentence audit receipt transition is inconsistent"
        )
    if eligible:
        if len(sentence_ids) != 1 or len(audit_ids) != 1:
            raise PilotSequenceError(
                "passing sentence audit lacks its exact RS/RSA pair"
            )
    elif sentence_ids or audit_ids:
        raise PilotSequenceError(
            "non-ENTAILED sentence audit cannot create an RS/RSA pair"
        )


def _validate_draft_audit_phase(
    events: Sequence[PilotStageArtifact],
    *,
    producer: TaskType,
    auditor: TaskType,
    items_field: str,
    local_ref_field: str,
    audit_ref_field: str,
    require_complete: bool = True,
) -> tuple[str, ...]:
    phase_events = tuple(
        artifact
        for artifact in events
        if artifact.task_provenance.task_type in {producer, auditor}  # type: ignore[union-attr]
    )
    if not phase_events:
        if not require_complete:
            return ()
        raise PilotSequenceError(
            f"completed pilot closure lacks {producer.value} and {auditor.value}"
        )
    outstanding: Counter[str] = Counter()
    produced = audited = 0
    passing_ids: list[str] = []
    for artifact in phase_events:
        task_type = artifact.task_provenance.task_type  # type: ignore[union-attr]
        receipt = _receipt(artifact)
        if task_type is producer:
            if outstanding:
                raise PilotSequenceError(
                    f"{producer.value} repeats before every draft is audited"
                )
            items = receipt.proposal_payload.get(items_field)
            if not isinstance(items, list) or not items:
                raise PilotSequenceError(f"{producer.value} produced no drafts")
            refs: list[str] = []
            for item in items:
                if not isinstance(item, dict) or not isinstance(
                    item.get(local_ref_field), str
                ):
                    raise PilotSequenceError(
                        f"{producer.value} draft identity is malformed"
                    )
                refs.append(item[local_ref_field])
            outstanding.update(refs)
            produced += len(refs)
            continue
        if not outstanding:
            raise PilotSequenceError(f"{auditor.value} precedes draft generation")
        target = receipt.proposal_payload.get(audit_ref_field)
        if not isinstance(target, str) or outstanding[target] != 1:
            raise PilotSequenceError(
                f"{auditor.value} target is absent, duplicate, or stale"
            )
        del outstanding[target]
        audited += 1
        transition = receipt.recorded_transition
        if auditor is TaskType.AUDIT_RENDERED_SENTENCE:
            _validate_sentence_audit_receipt(artifact)

        if transition.downstream_eligible:
            object_type = (
                "PropositionRecord"
                if auditor is TaskType.AUDIT_PROPOSITION
                else "RenderedSentence"
            )
            identifiers = _qualified_ids(receipt, object_type)
            if len(identifiers) != 1:
                raise PilotSequenceError(
                    f"passing {auditor.value} lacks one canonical output"
                )
            passing_ids.extend(identifiers)
    if require_complete and (outstanding or not produced or audited != produced):
        raise PilotSequenceError("draft generation and audit coverage is incomplete")
    if require_complete and not passing_ids:
        raise PilotSequenceError(
            f"completed pilot closure has no passing {auditor.value} output"
        )
    if len(passing_ids) != len(set(passing_ids)):
        raise PilotSequenceError("pilot audit outputs repeat canonical identifiers")
    return tuple(passing_ids)


def _validate_retrieval_progress(
    *,
    parsed_controls: dict[PilotStage, object],
    task_events: Sequence[PilotStageArtifact],
    snapshot: RepositorySnapshot,
    budget_consumed: dict[str, int],
    require_complete_assessment: bool,
) -> tuple[PilotRetrievalControlPayload, set[str], tuple[str, ...]]:
    """Validate the query/retrieval boundary and its accepted decisions."""

    retrieval = parsed_controls.get(PilotStage.RETRIEVAL)
    if not isinstance(retrieval, PilotRetrievalControlPayload):
        raise PilotSequenceError(
            "evidence work requires its completed typed retrieval control"
        )
    query_events = tuple(
        item
        for item in task_events
        if item.task_provenance.task_type  # type: ignore[union-attr]
        is TaskType.GENERATE_RETRIEVAL_QUERIES
    )
    if len(query_events) != 1:
        raise PilotSequenceError(
            "retrieval progress requires one query-generation event"
        )
    query_event = query_events[0]
    candidate_ids = {item.claim_id for item in snapshot.candidate_claims}
    expected_dependencies = {
        f"CandidateClaim:{claim_id}" for claim_id in candidate_ids
    }
    if set(query_event.task_provenance.dependencies) != expected_dependencies:  # type: ignore[union-attr]
        raise PilotSequenceError(
            "query-generation scope does not cover every CandidateClaim exactly"
        )

    observed_queries = {
        (item.claim_id, item.intent) for item in snapshot.retrieval_queries
    }
    expected_queries = {
        (claim_id, intent)
        for claim_id in candidate_ids
        for intent in _REQUIRED_RETRIEVAL_INTENTS
    }
    if (
        observed_queries != expected_queries
        or len(snapshot.retrieval_queries)
        != len(candidate_ids)
        * query_event.run_manifest.budget.required_queries_per_claim
    ):
        raise PilotSequenceError(
            "every CandidateClaim must have exactly the four enabled query intents"
        )
    ledgers = retrieval.parsed_ledgers()
    query_ids = set(_qualified_ids(_receipt(query_event), "RetrievalQuery"))
    if (
        query_ids != {item.query_id for item in snapshot.retrieval_queries}
        or query_ids != {item.query_id for item in ledgers}
    ):
        raise PilotSequenceError(
            "retrieval control does not cover query-generation outputs exactly"
        )

    selected_keys = {
        key for ledger in ledgers for key in ledger.selected_candidate_keys
    }
    decision_keys: list[str] = []
    for artifact in task_events:
        if artifact.task_provenance.task_type is not TaskType.ASSESS_EVIDENCE:  # type: ignore[union-attr]
            continue
        decisions = _receipt(artifact).proposal_payload.get("decisions")
        if not isinstance(decisions, list):
            raise PilotSequenceError("evidence assessment receipt is malformed")
        for decision in decisions:
            if not isinstance(decision, dict) or not isinstance(
                decision.get("candidate_ref"), str
            ):
                raise PilotSequenceError(
                    "evidence assessment candidate identity is malformed"
                )
            decision_keys.append(decision["candidate_ref"])
    if len(decision_keys) != len(set(decision_keys)):
        raise PilotSequenceError("evidence assessments repeat a selected candidate")
    if not set(decision_keys) <= selected_keys:
        raise PilotSequenceError("evidence assessment names an unselected candidate")
    if require_complete_assessment and set(decision_keys) != selected_keys:
        raise PilotSequenceError(
            "evidence assessments do not exactly cover selected candidates"
        )
    if budget_consumed.get("assessed_candidates", 0) != len(decision_keys):
        raise PilotSequenceError(
            "current assessed-candidate budget differs from evidence decisions"
        )
    return retrieval, selected_keys, tuple(decision_keys)


def validate_fixed_pilot_artifact_sequence(
    events: Sequence[PilotStageArtifact],
    *,
    control_artifacts: Sequence[PilotControlArtifact],
    snapshot: RepositorySnapshot,
) -> PilotSequenceSummary:
    """Validate fixed closure using only already authenticated in-memory values."""

    exact_events = tuple(events)
    try:
        exact_snapshot = RepositorySnapshot.model_validate(
            snapshot.model_dump(mode="json")
        )
        exact_snapshot.validate_repository()
    except Exception as exc:
        raise PilotSequenceError(
            "pilot sequence final repository snapshot is invalid"
        ) from exc
    if not exact_events:
        raise PilotSequenceError("pilot sequence is empty")
    if len(exact_events) > 128:
        raise PilotSequenceError("pilot sequence exceeds its event bound")

    references = tuple(_reference_from_artifact(item) for item in exact_events)
    first = exact_events[0]
    first_record = first.stage_record
    if (
        first_record.stage is not PilotStage.PREREQUISITES
        or first.task_provenance is not None
        or first_record.status is not PilotStageStatus.COMPLETED
    ):
        raise PilotSequenceError(
            "pilot sequence must begin with completed prerequisites"
        )

    registration = first.registration
    manifest_hash = first.run_manifest_hash
    prior_budget: dict[str, int] = {}
    for index, (artifact, reference) in enumerate(
        zip(exact_events, references, strict=True), start=1
    ):
        record = artifact.stage_record
        if (
            record.ordinal != index
            or artifact.registration != registration
            or artifact.run_manifest_hash != manifest_hash
        ):
            raise PilotSequenceError(
                "pilot sequence identity or ordinal changed within the chain"
            )
        expected_predecessor = (
            None if index == 1 else references[index - 2].predecessor()
        )
        if record.previous_stage != expected_predecessor:
            raise PilotSequenceError("pilot sequence predecessor is not exact")
        if any(record.budget_consumed.get(name, 0) < value for name, value in prior_budget.items()):
            raise PilotSequenceError("pilot sequence cumulative budget regressed")
        prior_budget = dict(record.budget_consumed)

    controls_by_ordinal: dict[int, PilotControlArtifact] = {}
    for control in control_artifacts:
        if control.ordinal in controls_by_ordinal:
            raise PilotSequenceError("pilot sequence repeats a control artifact")
        controls_by_ordinal[control.ordinal] = control
    taskless_ordinals = {
        item.stage_record.ordinal
        for item in exact_events
        if item.task_provenance is None
    }
    if set(controls_by_ordinal) != taskless_ordinals:
        raise PilotSequenceError(
            "pilot sequence control coverage is not exact for taskless events"
        )
    parsed_controls: dict[PilotStage, object] = {}
    for artifact in exact_events:
        if artifact.task_provenance is not None:
            continue
        try:
            parsed = validate_pilot_control_artifact_binding(
                artifact,
                controls_by_ordinal[artifact.stage_record.ordinal],
                snapshot=exact_snapshot,
            )
        except Exception as exc:
            raise PilotSequenceError(
                "pilot sequence control artifact is not exactly bound"
            ) from exc
        if parsed is not None:
            parsed_controls[artifact.stage_record.stage] = parsed

    phases: list[int] = []
    task_events: list[PilotStageArtifact] = []
    task_types: list[TaskType] = []
    stage_counts: dict[PilotStage, int] = {}
    task_counts: dict[TaskType, int] = {}
    semantic_keys: set[str] = set()
    terminal_seen = False

    for index, artifact in enumerate(exact_events):
        record = artifact.stage_record
        stage_counts[record.stage] = stage_counts.get(record.stage, 0) + 1
        if terminal_seen:
            raise PilotSequenceError("pilot sequence continues after a terminal event")
        provenance = artifact.task_provenance
        if provenance is None:
            try:
                phase = _CONTROL_PHASE[record.stage]
            except KeyError as exc:
                if record.status is PilotStageStatus.COMPLETED:
                    raise PilotSequenceError(
                        "completed taskless event is outside the fixed control stages"
                    ) from exc
                phase = phases[-1] if phases else 0
        else:
            task_type = provenance.task_type
            task_events.append(artifact)
            task_types.append(task_type)
            task_counts[task_type] = task_counts.get(task_type, 0) + 1
            phase = _TASK_PHASE[task_type]
            semantic_key = _receipt(artifact).semantic_task_key
            if semantic_key in semantic_keys:
                raise PilotSequenceError("pilot sequence repeats a semantic receipt")
            semantic_keys.add(semantic_key)
        if phases and phase < phases[-1]:
            raise PilotSequenceError("pilot stage order regressed")
        phases.append(phase)
        if record.status in {PilotStageStatus.BLOCKED, PilotStageStatus.FAILED}:
            terminal_seen = True
            if index != len(exact_events) - 1:
                raise PilotSequenceError(
                    "blocked or failed pilot event must be the journal head"
                )

    for task_type in _SINGLE_TASKS:
        if task_counts.get(task_type, 0) > 1:
            raise PilotSequenceError(
                f"pilot task may occur only once: {task_type.value}"
            )
    for stage in _CONTROL_PHASE:
        if stage_counts.get(stage, 0) > 1:
            raise PilotSequenceError(
                f"pilot control stage may occur only once: {stage.value}"
            )

    observed_prefix = tuple(task_types[: len(_REQUIRED_PREFIX_TASKS)])
    expected_prefix = _REQUIRED_PREFIX_TASKS[: len(observed_prefix)]
    if observed_prefix != expected_prefix:
        raise PilotSequenceError("pilot discovery/query task prefix is incomplete")

    _validate_partial_claim_order(task_events)
    _validate_draft_audit_phase(
        task_events,
        producer=TaskType.GENERATE_PROPOSITIONS,
        auditor=TaskType.AUDIT_PROPOSITION,
        items_field="propositions",
        local_ref_field="local_ref",
        audit_ref_field="target_ref",
        require_complete=False,
    )
    _validate_draft_audit_phase(
        task_events,
        producer=TaskType.RENDER_PROSE,
        auditor=TaskType.AUDIT_RENDERED_SENTENCE,
        items_field="sentences",
        local_ref_field="local_ref",
        audit_ref_field="sentence_ref",
        require_complete=False,
    )

    for artifact in task_events:
        if artifact.task_provenance.task_type is TaskType.AUDIT_RENDERED_SENTENCE:  # type: ignore[union-attr]
            _validate_sentence_audit_receipt(artifact)

    completed_phase = max(
        (
            phase
            for artifact, phase in zip(exact_events, phases, strict=True)
            if artifact.stage_record.status is PilotStageStatus.COMPLETED
        ),
        default=0,
    )
    retrieval_progress: tuple[
        PilotRetrievalControlPayload, set[str], tuple[str, ...]
    ] | None = None
    if completed_phase >= _TASK_PHASE[TaskType.ASSESS_EVIDENCE]:
        if tuple(task_types[:4]) != _REQUIRED_PREFIX_TASKS:
            raise PilotSequenceError(
                "evidence work requires the complete discovery/query prefix"
            )
        retrieval_progress = _validate_retrieval_progress(
            parsed_controls=parsed_controls,
            task_events=task_events,
            snapshot=exact_snapshot,
            budget_consumed=exact_events[-1].stage_record.budget_consumed,
            require_complete_assessment=(
                completed_phase
                >= _TASK_PHASE[TaskType.AGGREGATE_PAPER_EVIDENCE]
            ),
        )

    if completed_phase >= _TASK_PHASE[TaskType.REVISE_CLAIM]:
        _validate_evidence_partition(exact_snapshot, task_events)
        _validate_claim_pairing(
            task_events, exact_snapshot, require_terminal=False
        )

    if completed_phase >= _TASK_PHASE[TaskType.GENERATE_PROPOSITIONS]:
        _validate_claim_pairing(task_events, exact_snapshot)
        _validate_claim_packet_closure(exact_snapshot)

    passing_propositions: tuple[str, ...] | None = None
    if completed_phase >= _TASK_PHASE[TaskType.RENDER_PROSE]:
        passing_propositions = _validate_draft_audit_phase(
            task_events,
            producer=TaskType.GENERATE_PROPOSITIONS,
            auditor=TaskType.AUDIT_PROPOSITION,
            items_field="propositions",
            local_ref_field="local_ref",
            audit_ref_field="target_ref",
        )
        passing_proposition_set = set(passing_propositions)
        for artifact in task_events:
            if artifact.task_provenance.task_type is not TaskType.RENDER_PROSE:  # type: ignore[union-attr]
                continue
            for item in _receipt(artifact).proposal_payload.get("sentences", ()):
                if not isinstance(item, dict) or not set(
                    item.get("source_proposition_refs", ())
                ) <= passing_proposition_set:
                    raise PilotSequenceError(
                        "rendered sentence cites a nonpassing proposition"
                    )

    passing_sentences: tuple[str, ...] | None = None
    if completed_phase >= _CONTROL_PHASE[PilotStage.EXACT_ASSEMBLY]:
        passing_sentences = _validate_draft_audit_phase(
            task_events,
            producer=TaskType.RENDER_PROSE,
            auditor=TaskType.AUDIT_RENDERED_SENTENCE,
            items_field="sentences",
            local_ref_field="local_ref",
            audit_ref_field="sentence_ref",
        )
        assembly_progress = parsed_controls.get(PilotStage.EXACT_ASSEMBLY)
        if not isinstance(assembly_progress, PilotExactAssemblyControlPayload):
            raise PilotSequenceError(
                "exact assembly requires its completed typed control"
            )
        if {item.sentence_id for item in assembly_progress.assembly.sentences} != set(
            passing_sentences
        ):
            raise PilotSequenceError(
                "exact assembly does not cover passing sentence audits exactly"
            )

    closure_complete = (
        exact_events[-1].stage_record.stage is PilotStage.VALIDATION_REPORT
        and exact_events[-1].stage_record.status is PilotStageStatus.COMPLETED
    )
    if closure_complete:
        if tuple(task_types[:4]) != _REQUIRED_PREFIX_TASKS:
            raise PilotSequenceError(
                "completed pilot closure lacks its discovery/query prefix"
            )
        required_controls = set(_CONTROL_PHASE)
        if set(parsed_controls) != required_controls - {PilotStage.PREREQUISITES}:
            raise PilotSequenceError(
                "completed pilot closure lacks a required typed control event"
            )

        retrieval = parsed_controls[PilotStage.RETRIEVAL]
        assembly = parsed_controls[PilotStage.EXACT_ASSEMBLY]
        validation = parsed_controls[PilotStage.VALIDATION_REPORT]
        if not isinstance(retrieval, PilotRetrievalControlPayload):
            raise PilotSequenceError("retrieval control payload has the wrong type")
        if not isinstance(assembly, PilotExactAssemblyControlPayload):
            raise PilotSequenceError("exact-assembly control payload has the wrong type")
        if not isinstance(validation, PilotValidationControlPayload):
            raise PilotSequenceError("validation control payload has the wrong type")

        query_event = next(
            item
            for item in task_events
            if item.task_provenance.task_type  # type: ignore[union-attr]
            is TaskType.GENERATE_RETRIEVAL_QUERIES
        )
        candidate_ids = {item.claim_id for item in exact_snapshot.candidate_claims}
        scoped_claim_ids = {
            key.removeprefix("CandidateClaim:")
            for key in query_event.task_provenance.dependencies  # type: ignore[union-attr]
            if key.startswith("CandidateClaim:")
        }
        if (
            set(query_event.task_provenance.dependencies)  # type: ignore[union-attr]
            != {f"CandidateClaim:{claim_id}" for claim_id in candidate_ids}
            or scoped_claim_ids != candidate_ids
        ):
            raise PilotSequenceError(
                "query-generation scope does not cover every CandidateClaim exactly"
            )
        required_intents = {
            RetrievalIntent.SUPPORT,
            RetrievalIntent.CONTRADICTION,
            RetrievalIntent.BOUNDARY,
            RetrievalIntent.ALTERNATIVE,
        }
        observed_queries = {
            (item.claim_id, item.intent) for item in exact_snapshot.retrieval_queries
        }
        expected_queries = {
            (claim_id, intent)
            for claim_id in candidate_ids
            for intent in required_intents
        }
        if (
            observed_queries != expected_queries
            or len(exact_snapshot.retrieval_queries)
            != len(candidate_ids)
            * exact_events[-1].run_manifest.budget.required_queries_per_claim
        ):
            raise PilotSequenceError(
                "every CandidateClaim must have exactly the four enabled query intents"
            )
        query_ids = set(_qualified_ids(_receipt(query_event), "RetrievalQuery"))
        if query_ids != {item.query_id for item in retrieval.parsed_ledgers()}:
            raise PilotSequenceError(
                "retrieval control does not cover query-generation outputs exactly"
            )
        candidate_ids = {item.claim_id for item in exact_snapshot.candidate_claims}
        query_scope = {
            key.removeprefix("CandidateClaim:")
            for key in query_event.task_provenance.dependencies  # type: ignore[union-attr]
            if key.startswith("CandidateClaim:")
        }
        if query_scope != candidate_ids:
            raise PilotSequenceError(
                "query-generation task does not cover every CandidateClaim exactly"
            )
        intents_by_claim: dict[str, set[RetrievalIntent]] = {
            claim_id: set() for claim_id in candidate_ids
        }
        for query in exact_snapshot.retrieval_queries:
            try:
                intents_by_claim[query.claim_id].add(query.intent)
            except KeyError as exc:  # pragma: no cover - repository validator
                raise PilotSequenceError(
                    "retrieval query belongs to an unknown CandidateClaim"
                ) from exc
        if any(
            not _REQUIRED_RETRIEVAL_INTENTS <= intents
            for intents in intents_by_claim.values()
        ):
            raise PilotSequenceError(
                "every CandidateClaim requires support, contradiction, boundary, "
                "and alternative retrieval queries"
            )

        selected_keys = {
            key
            for ledger in retrieval.parsed_ledgers()
            for key in ledger.selected_candidate_keys
        }
        decision_keys: list[str] = []
        for artifact in task_events:
            if artifact.task_provenance.task_type is not TaskType.ASSESS_EVIDENCE:  # type: ignore[union-attr]
                continue
            decisions = _receipt(artifact).proposal_payload.get("decisions")
            if not isinstance(decisions, list):
                raise PilotSequenceError("evidence assessment receipt is malformed")
            for decision in decisions:
                if not isinstance(decision, dict) or not isinstance(
                    decision.get("candidate_ref"), str
                ):
                    raise PilotSequenceError(
                        "evidence assessment candidate identity is malformed"
                    )
                decision_keys.append(decision["candidate_ref"])
        if len(decision_keys) != len(set(decision_keys)) or set(decision_keys) != selected_keys:
            raise PilotSequenceError(
                "evidence assessments do not exactly cover selected candidates"
            )
        if (
            exact_events[-1].stage_record.budget_consumed.get(
                "assessed_candidates", 0
            )
            != len(decision_keys)
        ):
            raise PilotSequenceError(
                "final assessed-candidate budget differs from evidence decisions"
            )
        if selected_keys and (
            task_counts.get(TaskType.AGGREGATE_PAPER_EVIDENCE, 0) == 0
            or task_counts.get(TaskType.ASSESS_CLAIM, 0) == 0
        ):
            raise PilotSequenceError(
                "nonzero selected candidates require CPE and claim assessment"
            )

        _validate_snapshot_closure(exact_snapshot, task_events)
        _validate_claim_pairing(task_events, exact_snapshot)
        passing_propositions = _validate_draft_audit_phase(
            task_events,
            producer=TaskType.GENERATE_PROPOSITIONS,
            auditor=TaskType.AUDIT_PROPOSITION,
            items_field="propositions",
            local_ref_field="local_ref",
            audit_ref_field="target_ref",
        )
        passing_sentences = _validate_draft_audit_phase(
            task_events,
            producer=TaskType.RENDER_PROSE,
            auditor=TaskType.AUDIT_RENDERED_SENTENCE,
            items_field="sentences",
            local_ref_field="local_ref",
            audit_ref_field="sentence_ref",
        )
        passing_proposition_set = set(passing_propositions)
        for artifact in task_events:
            if artifact.task_provenance.task_type is not TaskType.RENDER_PROSE:  # type: ignore[union-attr]
                continue
            for item in _receipt(artifact).proposal_payload.get("sentences", ()):
                if not isinstance(item, dict) or not set(
                    item.get("source_proposition_refs", ())
                ) <= passing_proposition_set:
                    raise PilotSequenceError(
                        "rendered sentence cites a nonpassing proposition"
                    )
        if {item.sentence_id for item in assembly.assembly.sentences} != set(
            passing_sentences
        ):
            raise PilotSequenceError(
                "exact assembly does not cover passing sentence audits exactly"
            )
        report = validation.report
        expected_locator_status = (
            ValidationStatus.PASSED
            if exact_snapshot.retrieved_spans
            else ValidationStatus.NOT_EXECUTED
        )
        has_citation_bindings = any(
            item.citation_bindings for item in exact_snapshot.proposition_records
        )
        expected_citation_status = (
            ValidationStatus.PASSED
            if has_citation_bindings
            else ValidationStatus.NOT_EXECUTED
        )
        if (
            validation.repository_hash != assembly.repository_hash
            or validation.source_generation != assembly.source_generation
            or validation.source_generation
            != exact_events[-1].stage_record.source_generation
            or report.structural_validation is not ValidationStatus.PASSED
            or report.locator_verification is not expected_locator_status
            or report.semantic_audits_executed is not ValidationStatus.PASSED
            or report.citation_authorization is not expected_citation_status
            or report.exact_assembly is not ValidationStatus.PASSED
            or report.artifact_integrity is not ValidationStatus.PASSED
        ):
            raise PilotSequenceError(
                "validation control does not close the exact assembled run"
            )

    return PilotSequenceSummary(
        run_id=exact_events[-1].run_manifest.run_id,
        head=references[-1],
        event_count=len(exact_events),
        semantic_task_count=len(task_types),
        terminal=terminal_seen or closure_complete,
        closure_complete=closure_complete,
        stages=tuple(item.stage_record.stage for item in exact_events),
    )


def validate_fixed_pilot_sequence(
    project_root: Path,
    head: PilotStageArtifactReference,
) -> PilotSequenceSummary:
    """Load authentic live values and delegate to the pure fixed validator."""

    events = load_fixed_pilot_sequence(project_root, head)
    snapshot, _ = GenerationStore(project_root).load_generation(
        head.owner_generation
    )
    controls = tuple(
        load_pilot_control_artifact(project_root, artifact)
        for artifact in events
        if artifact.task_provenance is None
    )
    summary = validate_fixed_pilot_artifact_sequence(
        events, control_artifacts=controls, snapshot=snapshot
    )
    if summary.head != head:
        raise PilotSequenceError("pilot sequence head differs from the request")
    return summary


__all__ = [
    "PilotSequenceError",
    "PilotSequenceSummary",
    "load_fixed_pilot_sequence",
    "validate_fixed_pilot_artifact_sequence",
    "validate_fixed_pilot_sequence",
]
