from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from vibereview.enums import RetrievalIntent
from vibereview.ids import candidate_claim_hash
from vibereview.library.retrieval import (
    CandidateHitOrigin,
    RawCandidateHit,
    RetrievalLedger,
)
from vibereview.models import (
    CandidateClaim,
    ClaimAssessment,
    CorpusFact,
    CorpusFactDerivation,
    FinalClaimValidation,
    EvidenceQuality,
    EvidenceRecord,
    Paper,
    PropositionRecord,
    RenderedSentence,
    RenderedSentenceAudit,
    ReviewProcessFact,
    SemanticAuditResult,
    ThemeRecord,
    RetrievalQuery,
    RetrievalDisposition,
    RetrievalMetadata,
    RetrievedSpan,
    SpanLocator,
)
from vibereview.runtime.artifacts import (
    AssemblyBudget,
    AssemblyRecord,
    AssemblySentenceRecord,
)
from vibereview.runtime.hashing import hash_bytes, hash_json, hash_text
from vibereview.runtime.pilot_journal import (
    PilotControlArtifact,
    PilotExactAssemblyControlPayload,
    PilotRetrievalControlPayload,
    PilotStageArtifact,
    PilotStageArtifactReference,
    PilotValidationControlPayload,
    _prepare_pilot_control_event,
    build_pilot_stage_artifact,
)
from vibereview.runtime.pilot_manifest import (
    compute_package_c_implementation_fingerprint,
)
from vibereview.runtime.pilot_records import (
    FivePaperPilotBudget,
    PilotEngineRoleBinding,
    PilotRunManifest,
    PilotRunRegistrationReference,
    PilotStage,
    PilotStageRecord,
    PilotStageStatus,
    PilotValidationReport,
    ValidationStatus,
    compute_pilot_engine_role_plan_hash,
    compute_pilot_schema_fingerprint,
)
from vibereview.runtime.receipts import (
    compute_input_identity_key,
    compute_semantic_task_key,
)
from vibereview.runtime.records import (
    AppliedTaskReceipt,
    CanonicalObjectReceipt,
    ResourceProvenance,
    ResourceSourceDependency,
    TaskProvenance,
    TaskSemanticFingerprint,
    TaskType,
    TransitionDecision,
)
from vibereview.runtime.state import RepositorySnapshot
from vibereview.runtime.pilot_sequence import (
    PilotSequenceError,
    validate_fixed_pilot_artifact_sequence,
)


_RUN_ID = "RUN-synthetic-sequence"


def _sha(character: str) -> str:
    return "sha256:" + character * 64


def _snapshot() -> RepositorySnapshot:
    papers = tuple(
        Paper(
            paper_id=f"P{ordinal:04d}",
            title=f"Synthetic paper {ordinal}",
            authors=["Fixture Author"],
            year=2030,
            doi=None,
            journal="Synthetic Journal",
            identity_keys=[f"fixture:paper-{ordinal}"],
            study_group_id=None,
            related_publications=[],
            independence_status="unknown",
            raw_md_path=f"papers/P{ordinal:04d}/raw.md",
            source_hash=_sha(str(ordinal)),
            raw_md_hash=_sha(chr(ord("a") + ordinal)),
        )
        for ordinal in range(1, 6)
    )
    corpus = CorpusFact(
        corpus_fact_id="CF0001",
        text="The synthetic corpus contains five papers.",
        derivation_type="registry_arithmetic",
        source_paper_ids=[item.paper_id for item in papers],
        derivation=CorpusFactDerivation(
            task_version=None,
            model_signature=None,
            input_hash=None,
            output_hash=None,
        ),
    )
    process = ReviewProcessFact(
        process_fact_id="PF0001",
        text="This is a bounded synthetic pilot.",
        source_type="project_config",
        source_key="pilot.scope",
    )
    proposition = PropositionRecord(
        proposition_id="PR0001",
        text=process.text,
        content_class="ReviewProcessStatement",
        claim_ids=[],
        citation_bindings=[],
        corpus_fact_ids=[],
        process_fact_ids=[process.process_fact_id],
    )
    semantic_audit = SemanticAuditResult(
        audit_id="SA0001",
        target_type="PropositionRecord",
        target_id=proposition.proposition_id,
        class_verdict="CORRECT",
        provenance_verdict="ENTAILED",
        reason="The process statement exactly reflects the registered fact.",
        referenced_claim_ids=[],
        referenced_corpus_fact_ids=[],
        referenced_process_fact_ids=[process.process_fact_id],
    )
    sentence = RenderedSentence(
        sentence_id="RS0001",
        text=process.text,
        source_proposition_ids=[proposition.proposition_id],
    )
    sentence_audit = RenderedSentenceAudit(
        audit_id="RSA0001",
        sentence_id=sentence.sentence_id,
        verdict="ENTAILED",
        reason="The sentence exactly renders the audited proposition.",
    )
    snapshot = RepositorySnapshot(
        papers=papers,
        corpus_facts=(corpus,),
        process_facts=(process,),
        proposition_records=(proposition,),
        semantic_audits=(semantic_audit,),
        rendered_sentences=(sentence,),
        rendered_sentence_audits=(sentence_audit,),
    )
    snapshot.validate_repository()
    return snapshot


def _snapshot_with_queries(
    intents: tuple[RetrievalIntent, ...],
) -> RepositorySnapshot:
    snapshot = _snapshot()
    text = "The synthetic intervention changes the measured outcome."
    theme = ThemeRecord(
        theme_id="T0001",
        title="Synthetic theme",
        description="A bounded synthetic theme.",
        origin="generated",
        parent_theme_id=None,
    )
    claim = CandidateClaim(
        claim_id="C0001",
        theme_id=theme.theme_id,
        candidate_claim=text,
        origin="generated",
        origin_refs=[],
    )
    codes = {
        RetrievalIntent.SUPPORT: "SUP",
        RetrievalIntent.CONTRADICTION: "CON",
        RetrievalIntent.BOUNDARY: "BND",
        RetrievalIntent.ALTERNATIVE: "ALT",
        RetrievalIntent.METHOD_CHALLENGE: "MTH",
        RetrievalIntent.NULL_RESULT: "NUL",
    }
    queries = tuple(
        RetrievalQuery(
            query_id=f"Q-C0001-{codes[intent]}-01",
            claim_id=claim.claim_id,
            candidate_claim_hash=candidate_claim_hash(text),
            intent=intent,
            query_text=f"synthetic {intent.value} evidence",
        )
        for intent in intents
    )
    result = snapshot.model_copy(
        update={
            "themes": (theme,),
            "candidate_claims": (claim,),
            "retrieval_queries": queries,
        }
    )
    result.validate_repository()
    return result


def _query_task(snapshot: RepositorySnapshot) -> _Task:
    return _Task(
        TaskType.GENERATE_RETRIEVAL_QUERIES,
        {
            "queries": [
                {
                    "local_ref": f"query_{index}",
                    "claim_ref": query.claim_id,
                    "intent": query.intent.value,
                    "query_text": query.query_text,
                }
                for index, query in enumerate(
                    snapshot.retrieval_queries, start=1
                )
            ]
        },
        dependencies=tuple(
            f"CandidateClaim:{claim.claim_id}"
            for claim in snapshot.candidate_claims
        ),
        canonical=tuple(
            (
                f"RetrievalQuery:{query.query_id}",
                f"RetrievalQuery:{query.query_id}",
            )
            for query in snapshot.retrieval_queries
        ),
    )


def _fingerprint() -> TaskSemanticFingerprint:
    values = {
        "validator_fingerprint": hash_text("validator"),
        "promotion_handler_fingerprint": hash_text("promotion"),
        "disposition_handler_fingerprint": hash_text("disposition"),
        "scientific_contract_version": "V1.5.1b",
        "runtime_contract_version": "1.6",
    }
    return TaskSemanticFingerprint(
        **values, combined_fingerprint=hash_json(values)
    )


def _manifest(snapshot: RepositorySnapshot) -> PilotRunManifest:
    binding = PilotEngineRoleBinding(
        engine="synthetic-mock",
        engine_version="1",
        safe_configuration_hash=hash_text("engine plan"),
    )
    plan = {task_type: binding for task_type in TaskType}
    schema_hash = compute_pilot_schema_fingerprint(
        input_schema_hash=hash_text("input schema"),
        proposal_schema_hash=hash_text("proposal schema"),
    )
    return PilotRunManifest(
        run_id=_RUN_ID,
        created_at="2030-01-01T00:00:00+00:00",
        topic="Synthetic fixed-sequence topic.",
        source_generation=0,
        corpus_lock_hash=_sha("a"),
        selection_manifest_hash=_sha("b"),
        paper_source_hashes=tuple(item.source_hash for item in snapshot.papers),
        discovery_resource_hashes=(_sha("6"), _sha("7")),
        engine_role_plan=plan,
        engine_role_plan_hash=compute_pilot_engine_role_plan_hash(plan),
        prompt_fingerprints={task_type: hash_text("instructions") for task_type in TaskType},
        schema_fingerprints={task_type: schema_hash for task_type in TaskType},
        validator_fingerprint=hash_text("validator"),
        package_c_implementation_fingerprint=(
            compute_package_c_implementation_fingerprint()
        ),
        budget=FivePaperPilotBudget(),
    )


def _registration() -> PilotRunRegistrationReference:
    artifact_hash = _sha("f")
    return PilotRunRegistrationReference(
        run_id=_RUN_ID,
        owner_generation=1,
        artifact_hash=artifact_hash,
        relative_path=(
            f"pilot/runs/{_RUN_ID}/registration-"
            f"{artifact_hash.removeprefix('sha256:')}.json"
        ),
    )


_TASK_STAGE = {
    TaskType.PARSE_DEEP_RESEARCH: PilotStage.DISCOVERY,
    TaskType.CORPUS_CHALLENGER: PilotStage.CORPUS_CHALLENGE,
    TaskType.GENERATE_CANDIDATE_CLAIMS: PilotStage.DISCOVERY,
    TaskType.GENERATE_RETRIEVAL_QUERIES: PilotStage.QUERY_GENERATION,
    TaskType.ASSESS_EVIDENCE: PilotStage.EVIDENCE_ASSESSMENT,
    TaskType.AGGREGATE_PAPER_EVIDENCE: PilotStage.CLAIM_AGGREGATION,
    TaskType.ASSESS_CLAIM: PilotStage.CLAIM_AGGREGATION,
    TaskType.REVISE_CLAIM: PilotStage.CLAIM_VALIDATION,
    TaskType.VALIDATE_FINAL_CLAIM: PilotStage.CLAIM_VALIDATION,
    TaskType.GENERATE_PROPOSITIONS: PilotStage.PROPOSITION_AUDIT,
    TaskType.AUDIT_PROPOSITION: PilotStage.PROPOSITION_AUDIT,
    TaskType.RENDER_PROSE: PilotStage.SENTENCE_AUDIT,
    TaskType.AUDIT_RENDERED_SENTENCE: PilotStage.SENTENCE_AUDIT,
}


@dataclass(frozen=True)
class _Task:
    task_type: TaskType
    proposal: dict
    canonical: tuple[tuple[str, str], ...] = ()
    downstream_eligible: bool = True
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Control:
    stage: PilotStage
    status: PilotStageStatus = PilotStageStatus.COMPLETED
    reason: str | None = None
    budget: dict[str, int] | None = None
    selected_candidate_keys: tuple[str, ...] = ()
    validation_statuses: dict[str, ValidationStatus] | None = None


def _default_operations():
    return [
        _Control(PilotStage.PREREQUISITES),
        _Task(TaskType.PARSE_DEEP_RESEARCH, {}),
        _Task(TaskType.CORPUS_CHALLENGER, {}),
        _Task(TaskType.GENERATE_CANDIDATE_CLAIMS, {"claims": []}),
        _Task(TaskType.GENERATE_RETRIEVAL_QUERIES, {"queries": []}),
        _Control(PilotStage.RETRIEVAL),
        _Task(
            TaskType.GENERATE_PROPOSITIONS,
            {
                "propositions": [
                    {
                        "local_ref": "process_statement",
                        "text": "This is a bounded synthetic pilot.",
                        "content_class": "ReviewProcessStatement",
                        "claim_refs": [],
                        "citation_bindings": [],
                        "corpus_fact_refs": [],
                        "process_fact_refs": ["PF0001"],
                    }
                ]
            },
            dependencies=("ReviewProcessFact:PF0001",),
        ),
        _Task(
            TaskType.AUDIT_PROPOSITION,
            {
                "target_ref": "process_statement",
                "class_verdict": "CORRECT",
                "provenance_verdict": "ENTAILED",
                "reason": "Exact process fact.",
                "referenced_claim_refs": [],
                "referenced_corpus_fact_refs": [],
                "referenced_process_fact_refs": ["PF0001"],
            },
            canonical=(
                ("PropositionRecord:PR0001", "PropositionRecord:PR0001"),
                ("SemanticAuditResult:SA0001", "SemanticAuditResult:SA0001"),
            ),
        ),
        _Task(
            TaskType.RENDER_PROSE,
            {
                "sentences": [
                    {
                        "local_ref": "sentence_one",
                        "text": "This is a bounded synthetic pilot.",
                        "source_proposition_refs": ["PR0001"],
                    }
                ]
            },
            dependencies=(
                "PropositionRecord:PR0001",
                "SemanticAuditResult:SA0001",
            ),
        ),
        _Task(
            TaskType.AUDIT_RENDERED_SENTENCE,
            {
                "sentence_ref": "sentence_one",
                "verdict": "ENTAILED",
                "reason": "Exact rendering.",
            },
            canonical=(
                ("RenderedSentence:RS0001", "RenderedSentence:RS0001"),
                (
                    "RenderedSentenceAudit:RSA0001",
                    "RenderedSentenceAudit:RSA0001",
                ),
            ),
        ),
        _Control(PilotStage.EXACT_ASSEMBLY),
        _Control(PilotStage.VALIDATION_REPORT),
    ]


def _assembly(snapshot: RepositorySnapshot, generation: int) -> tuple[bytes, AssemblyRecord]:
    sentence = snapshot.rendered_sentences[0]
    body = (sentence.text + "\n").encode("utf-8")
    encoded = sentence.text.encode("utf-8")
    return body, AssemblyRecord(
        source_generation=generation,
        repository_hash=snapshot.canonical_hash(),
        budget=AssemblyBudget(
            max_sentences=18,
            max_citations=30,
            max_body_utf8_bytes=64 * 1024,
        ),
        sentences=(
            AssemblySentenceRecord(
                sentence_id=sentence.sentence_id,
                text_hash=hash_bytes(encoded),
                source_proposition_ids=tuple(sentence.source_proposition_ids),
                start_codepoint=0,
                end_codepoint=len(sentence.text),
                start_utf8_byte=0,
                end_utf8_byte=len(encoded),
            ),
        ),
        citations=(),
        body_hash=hash_bytes(body),
        body_size_bytes=len(body),
    )


def _chain(operations, *, snapshot: RepositorySnapshot | None = None):
    snapshot = snapshot or _snapshot()
    manifest = _manifest(snapshot)
    registration = _registration()
    events: list[PilotStageArtifact] = []
    controls: list[PilotControlArtifact] = []
    previous: PilotStageArtifactReference | None = None
    fingerprint = _fingerprint()
    cumulative_budget: dict[str, int] = {}

    for ordinal, operation in enumerate(operations, start=1):
        source = previous.owner_generation if previous is not None else 1
        if isinstance(operation, _Control):
            budget = dict(cumulative_budget)
            budget.update(operation.budget or {})
            if operation.stage is PilotStage.RETRIEVAL:
                ordered_queries = tuple(
                    sorted(snapshot.retrieval_queries, key=lambda item: item.query_id)
                )
                ledgers = tuple(
                    RetrievalLedger(
                        source_generation=source,
                        query_id=query.query_id,
                        query_text_hash=hash_bytes(
                            query.query_text.encode("utf-8")
                        ),
                        corpus_lock_hash=manifest.corpus_lock_hash,
                        requested_top_k=1,
                        max_query_terms=64,
                        total_candidates=(
                            len(operation.selected_candidate_keys)
                            if query == ordered_queries[0]
                            else 0
                        ),
                        valid_candidates=(
                            len(operation.selected_candidate_keys)
                            if query == ordered_queries[0]
                            else 0
                        ),
                        invalid_candidates=0,
                        selected_candidate_keys=(
                            list(operation.selected_candidate_keys)
                            if query == ordered_queries[0]
                            else []
                        ),
                        excluded_by_budget_candidate_keys=[],
                        raw_hits=(
                            [
                                RawCandidateHit(
                                    candidate_key=key,
                                    paper_id=snapshot.papers[0].paper_id,
                                    raw_md_path=snapshot.papers[0].raw_md_path,
                                    query_id=query.query_id,
                                    query_text_hash=hash_bytes(
                                        query.query_text.encode("utf-8")
                                    ),
                                    intent=query.intent,
                                    origin=CandidateHitOrigin.TEXT_BASELINE,
                                    source_text="Synthetic evidence",
                                    start_offset=0,
                                    end_offset=len("Synthetic evidence"),
                                    source_span_hash=hash_bytes(
                                        b"Synthetic evidence"
                                    ),
                                    context_text="Synthetic evidence",
                                    context_start_offset=0,
                                    context_end_offset=len("Synthetic evidence"),
                                    context_utf8_hash=hash_bytes(
                                        b"Synthetic evidence"
                                    ),
                                    score=1.0 - index / 100.0,
                                    is_valid=True,
                                )
                                for index, key in enumerate(
                                    operation.selected_candidate_keys
                                )
                            ]
                            if query == ordered_queries[0]
                            else []
                        ),
                    )
                    for query in ordered_queries
                )
                payload = PilotRetrievalControlPayload.from_ledgers(
                    source_generation=source,
                    corpus_lock_hash=manifest.corpus_lock_hash,
                    ledgers=ledgers,
                ).model_dump(mode="json")
                budget.setdefault("queries", len(ledgers))
                budget.setdefault(
                    "raw_candidates", len(operation.selected_candidate_keys)
                )
                budget.setdefault("assessed_candidates", 0)
            elif operation.stage is PilotStage.EXACT_ASSEMBLY:
                body, assembly = _assembly(snapshot, source + 1)
                payload = PilotExactAssemblyControlPayload.from_assembly(
                    body, assembly
                ).model_dump(mode="json")
            elif operation.stage is PilotStage.VALIDATION_REPORT:
                report = PilotValidationReport(
                    run_id=_RUN_ID,
                    source_generation=source,
                    repository_hash=snapshot.canonical_hash(),
                    structural_validation=ValidationStatus.PASSED,
                    locator_verification=(
                        (operation.validation_statuses or {}).get(
                            "locator_verification",
                            ValidationStatus.NOT_EXECUTED,
                        )
                    ),
                    semantic_audits_executed=ValidationStatus.PASSED,
                    citation_authorization=(
                        (operation.validation_statuses or {}).get(
                            "citation_authorization",
                            ValidationStatus.NOT_EXECUTED,
                        )
                    ),
                    exact_assembly=ValidationStatus.PASSED,
                    artifact_integrity=ValidationStatus.PASSED,
                    human_review_status="NOT_PERFORMED",
                )
                payload = PilotValidationControlPayload.from_report(
                    report=report,
                    diagnostics={
                        "structural_validation": (),
                        "locator_verification": (
                            ()
                            if report.locator_verification
                            is ValidationStatus.PASSED
                            else ("No retrieved spans or locator check failed.",)
                        ),
                        "semantic_audits_executed": (),
                        "citation_authorization": (
                            ()
                            if report.citation_authorization
                            is ValidationStatus.PASSED
                            else ("No citation bindings or authorization failed.",)
                        ),
                        "exact_assembly": (),
                        "artifact_integrity": (),
                    },
                ).model_dump(mode="json")
            else:
                payload = {"checks": ["registered", "bounded"]}
            prepared = _prepare_pilot_control_event(
                registration=registration,
                run_manifest=manifest,
                ordinal=ordinal,
                stage=operation.stage,
                status=operation.status,
                source_generation=source,
                input_hashes={
                    "pilot_run_manifest": manifest.manifest_hash,
                    "pilot_run_registration": registration.artifact_hash,
                },
                budget_consumed=budget,
                previous_stage=previous,
                reason=operation.reason,
                artifact_payload=payload,
            )
            events.append(prepared.stage.artifact)
            controls.append(prepared.control_artifact)
            previous = prepared.stage.reference
            cumulative_budget = budget
            continue

        task_id = f"TASK{ordinal:04d}"
        dependencies = {
            key: snapshot.dependency_hash(key) for key in operation.dependencies
        }
        provenance = TaskProvenance(
            task_id=task_id,
            task_type=operation.task_type,
            task_spec_version="1",
            prompt_version="1",
            base_generation=source,
            dependencies=dependencies,
            instructions_hash=hash_text("instructions"),
            input_snapshot_hash=hash_text(f"input:{ordinal}"),
            engine_input_hash=hash_text(f"engine:{ordinal}"),
            input_schema_hash=hash_text("input schema"),
            proposal_schema_hash=hash_text("proposal schema"),
            expected_bundle_manifest_hash=hash_text(f"bundle:{ordinal}"),
            expected_immutable_files={"instructions.md": hash_text("instructions")},
            resources=(
                tuple(
                    ResourceProvenance(
                        resource_id=f"RES{resource_ordinal:04d}",
                        logical_name=f"discovery-{resource_ordinal}.md",
                        media_type="text/markdown",
                        bundle_relative_path=Path(
                            "input/resources/"
                            f"RES{resource_ordinal:04d}/content.md"
                        ),
                        source_path=Path(
                            f"/synthetic/discovery-{resource_ordinal}.md"
                        ),
                        source_dependency=ResourceSourceDependency(
                            source_hash_at_snapshot=digest
                        ),
                        snapshot_hash=digest,
                        size_bytes=1,
                    )
                    for resource_ordinal, digest in enumerate(
                        manifest.discovery_resource_hashes, start=1
                    )
                )
                if operation.task_type is TaskType.PARSE_DEEP_RESEARCH
                else ()
            ),
        )
        canonical = tuple(
            CanonicalObjectReceipt(
                qualified_id=qualified,
                object_hash=snapshot.dependency_hash(snapshot_key),
            )
            for qualified, snapshot_key in operation.canonical
        )
        input_identity_key = compute_input_identity_key(
            task_type=operation.task_type,
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
            engine_version=fingerprint.runtime_contract_version,
            safe_engine_configuration_hash=manifest.engine_role_plan[
                operation.task_type
            ].safe_configuration_hash,
            scientific_contract_version=(
                fingerprint.scientific_contract_version
            ),
        )
        receipt = AppliedTaskReceipt(
            input_identity_key=input_identity_key,
            semantic_task_key=compute_semantic_task_key(
                input_identity_key=input_identity_key,
                semantic_fingerprint=fingerprint,
            ),
            task_type=operation.task_type,
            task_spec_version="1",
            proposal_hash=hash_json(operation.proposal),
            proposal_payload=operation.proposal,
            semantic_fingerprint=fingerprint,
            source_generation=source,
            committed_generation=source + 1,
            canonical_objects=canonical,
            local_ref_map={},
            recorded_transition=TransitionDecision(
                scientific_disposition=(
                    operation.proposal["verdict"]
                    if operation.task_type is TaskType.AUDIT_RENDERED_SENTENCE
                    else "VALID"
                ),
                canonicalized=(
                    bool(canonical)
                    or operation.task_type is TaskType.AUDIT_RENDERED_SENTENCE
                ),
                downstream_eligible=operation.downstream_eligible,
            ),
            engine="synthetic-mock",
            engine_version="1",
            accepted_attempt_id=f"{task_id}/01-synthetic",
        )
        stage = _TASK_STAGE[operation.task_type]
        record = PilotStageRecord(
            run_id=_RUN_ID,
            ordinal=ordinal,
            stage=stage,
            status=PilotStageStatus.COMPLETED,
            source_generation=source,
            committed_generation=source + 1,
            input_hashes={"pilot_run_manifest": manifest.manifest_hash},
            task_ids=(task_id,),
            budget_consumed=dict(cumulative_budget),
            artifact_hashes={
                f"domain/{ordinal:04d}.json": hash_text(f"domain:{ordinal}")
            },
            previous_stage=previous.predecessor() if previous is not None else None,
        )
        prepared = build_pilot_stage_artifact(
            registration=registration,
            run_manifest=manifest,
            stage_record=record,
            task_provenance=provenance,
            accepted_receipt=receipt,
            engine_plan_hash=manifest.engine_role_plan[
                operation.task_type
            ].safe_configuration_hash,
        )
        events.append(prepared.artifact)
        previous = prepared.reference
    return tuple(events), tuple(controls), snapshot


def _validate(
    operations,
    *,
    controls_transform=lambda values: values,
    snapshot: RepositorySnapshot | None = None,
):
    events, controls, snapshot = _chain(operations, snapshot=snapshot)
    return validate_fixed_pilot_artifact_sequence(
        events,
        control_artifacts=controls_transform(controls),
        snapshot=snapshot,
    )


def test_fixed_sequence_accepts_complete_honest_no_evidence_run() -> None:
    summary = _validate(_default_operations())

    assert summary.event_count == 12
    assert summary.semantic_task_count == 8
    assert summary.closure_complete is True
    assert summary.terminal is True


def test_fixed_sequence_rejects_reordered_discovery() -> None:
    operations = _default_operations()
    operations[1], operations[2] = operations[2], operations[1]
    with pytest.raises(PilotSequenceError, match="order regressed|prefix"):
        _validate(operations)


def test_fixed_sequence_rejects_omitted_or_reversed_process_audits() -> None:
    omitted = [
        item
        for item in _default_operations()
        if not (
            isinstance(item, _Task)
            and item.task_type is TaskType.AUDIT_PROPOSITION
        )
    ]
    with pytest.raises(PilotSequenceError, match="audit|passing"):
        _validate(omitted)

    reversed_operations = _default_operations()
    reversed_operations[6], reversed_operations[7] = (
        reversed_operations[7],
        reversed_operations[6],
    )
    with pytest.raises(PilotSequenceError, match="precedes draft generation"):
        _validate(reversed_operations)


def test_fixed_sequence_requires_exact_taskless_control_coverage() -> None:
    with pytest.raises(PilotSequenceError, match="control coverage"):
        _validate(_default_operations(), controls_transform=lambda values: values[:-1])


def test_fixed_sequence_rejects_work_after_terminal_event() -> None:
    operations = [
        _Control(PilotStage.PREREQUISITES),
        _Task(TaskType.PARSE_DEEP_RESEARCH, {}),
        _Control(
            PilotStage.CORPUS_CHALLENGE,
            status=PilotStageStatus.BLOCKED,
            reason="synthetic blocker",
        ),
        _Control(PilotStage.RETRIEVAL),
    ]
    with pytest.raises(PilotSequenceError, match="terminal event|journal head"):
        _validate(operations)


def test_fixed_sequence_accepts_terminal_blocked_head() -> None:
    operations = [
        _Control(PilotStage.PREREQUISITES),
        _Task(TaskType.PARSE_DEEP_RESEARCH, {}),
        _Control(
            PilotStage.CORPUS_CHALLENGE,
            status=PilotStageStatus.BLOCKED,
            reason="synthetic blocker",
        ),
    ]
    summary = _validate(operations)
    assert summary.terminal is True
    assert summary.closure_complete is False


def test_fixed_sequence_rejects_incomplete_closure_and_duplicate_singletons() -> None:
    incomplete = [
        _Control(PilotStage.PREREQUISITES),
        _Control(PilotStage.VALIDATION_REPORT),
    ]
    with pytest.raises(PilotSequenceError, match="discovery/query prefix"):
        _validate(incomplete)

    duplicate = _default_operations()
    duplicate.insert(5, _Task(TaskType.GENERATE_RETRIEVAL_QUERIES, {"queries": []}))
    with pytest.raises(PilotSequenceError, match="only once"):
        _validate(duplicate)


def test_fixed_sequence_derives_final_assessed_candidate_budget() -> None:
    operations = _default_operations()
    operations[-1] = _Control(
        PilotStage.VALIDATION_REPORT,
        budget={"assessed_candidates": 1},
    )
    with pytest.raises(PilotSequenceError, match="assessed-candidate budget"):
        _validate(operations)


def test_fixed_sequence_rejects_disabled_or_missing_query_intents() -> None:
    enabled = (
        RetrievalIntent.SUPPORT,
        RetrievalIntent.CONTRADICTION,
        RetrievalIntent.BOUNDARY,
        RetrievalIntent.ALTERNATIVE,
    )
    snapshot = _snapshot_with_queries(
        (*enabled, RetrievalIntent.METHOD_CHALLENGE)
    )
    operations = _default_operations()
    operations[4] = _query_task(snapshot)
    with pytest.raises(PilotSequenceError, match="four enabled query intents"):
        _validate(operations, snapshot=snapshot)


def test_rejected_claim_is_terminal_and_cannot_have_final_validation() -> None:
    enabled = (
        RetrievalIntent.SUPPORT,
        RetrievalIntent.CONTRADICTION,
        RetrievalIntent.BOUNDARY,
        RetrievalIntent.ALTERNATIVE,
    )
    base = _snapshot_with_queries(enabled)
    assessment = ClaimAssessment(
        claim_id="C0001",
        aggregate_strength="unknown",
        evidence_sufficiency="insufficient",
        decision="REJECT",
        rejection_basis="insufficient_evidence",
        support_summary="No supporting evidence was retrieved.",
        contradiction_summary="No contradictory evidence was retrieved.",
        qualification_summary="The bounded search returned no evidence.",
        reason="The claim lacks evidence in the bounded corpus.",
    )
    rejected = base.model_copy(update={"claim_assessments": (assessment,)})
    rejected.validate_repository()
    assessment_proposal = {
        "claim_ref": "C0001",
        "aggregate_strength": "unknown",
        "evidence_sufficiency": "insufficient",
        "decision": "REJECT",
        "rejection_basis": "insufficient_evidence",
        "support_summary": "No supporting evidence was retrieved.",
        "contradiction_summary": "No contradictory evidence was retrieved.",
        "qualification_summary": "The bounded search returned no evidence.",
        "reason": "The claim lacks evidence in the bounded corpus.",
    }
    assessment_task = _Task(
        TaskType.ASSESS_CLAIM,
        assessment_proposal,
        dependencies=("CandidateClaim:C0001",),
        canonical=(("ClaimAssessment:C0001", "ClaimAssessment:C0001"),),
    )
    operations = _default_operations()
    operations[4] = _query_task(rejected)
    operations.insert(6, assessment_task)
    assert _validate(operations, snapshot=rejected).closure_complete is True

    final = FinalClaimValidation(
        claim_id="C0001",
        final_claim=rejected.candidate_claims[0].candidate_claim,
        status="REJECT",
        paper_relations=[],
        scope_check="fail",
        certainty_check="fail",
        causal_language_check="not_applicable",
        numerical_claim_check="not_applicable",
        notes="The upstream claim was rejected.",
    )
    invalid = rejected.model_copy(update={"final_claim_validations": (final,)})
    invalid.validate_repository()
    operations = _default_operations()
    operations[4] = _query_task(invalid)
    operations.insert(6, assessment_task)
    operations.insert(
        7,
        _Task(
            TaskType.VALIDATE_FINAL_CLAIM,
            {
                "claim_ref": "C0001",
                "final_claim": final.final_claim,
                "status": "REJECT",
                "paper_relations": [],
                "scope_check": "fail",
                "certainty_check": "fail",
                "causal_language_check": "not_applicable",
                "numerical_claim_check": "not_applicable",
                "notes": final.notes,
            },
            dependencies=(
                "CandidateClaim:C0001",
                "ClaimAssessment:C0001",
            ),
            canonical=(
                (
                    "FinalClaimValidation:C0001",
                    "FinalClaimValidation:C0001",
                ),
            ),
        ),
    )
    with pytest.raises(PilotSequenceError, match="rejected ClaimAssessment"):
        _validate(operations, snapshot=invalid)


def test_claim_aggregation_cannot_follow_assessment_in_partial_phase() -> None:
    enabled = (
        RetrievalIntent.SUPPORT,
        RetrievalIntent.CONTRADICTION,
        RetrievalIntent.BOUNDARY,
        RetrievalIntent.ALTERNATIVE,
    )
    base = _snapshot_with_queries(enabled)
    assessment = ClaimAssessment(
        claim_id="C0001",
        aggregate_strength="unknown",
        evidence_sufficiency="insufficient",
        decision="REJECT",
        rejection_basis="insufficient_evidence",
        support_summary="No supporting evidence was retrieved.",
        contradiction_summary="No contradictory evidence was retrieved.",
        qualification_summary="The bounded search returned no evidence.",
        reason="The claim lacks evidence in the bounded corpus.",
    )
    snapshot = base.model_copy(update={"claim_assessments": (assessment,)})
    snapshot.validate_repository()
    operations = _default_operations()[:5]
    operations[4] = _query_task(snapshot)
    operations.extend(
        [
            _Control(PilotStage.RETRIEVAL),
            _Task(
                TaskType.ASSESS_CLAIM,
                {
                    "claim_ref": "C0001",
                    "aggregate_strength": "unknown",
                    "evidence_sufficiency": "insufficient",
                    "decision": "REJECT",
                    "rejection_basis": "insufficient_evidence",
                    "support_summary": assessment.support_summary,
                    "contradiction_summary": assessment.contradiction_summary,
                    "qualification_summary": assessment.qualification_summary,
                    "reason": assessment.reason,
                },
                dependencies=("CandidateClaim:C0001",),
                canonical=(("ClaimAssessment:C0001", "ClaimAssessment:C0001"),),
            ),
            _Task(
                TaskType.AGGREGATE_PAPER_EVIDENCE,
                {"claim_ref": "C0001"},
            ),
        ]
    )
    with pytest.raises(
        PilotSequenceError,
        match="aggregation occurs after claim assessment",
    ):
        _validate(operations, snapshot=snapshot)


def test_proposition_repair_cannot_start_before_prior_batch_is_fully_audited() -> None:
    default = _default_operations()
    first_batch = _Task(
        TaskType.GENERATE_PROPOSITIONS,
        {
            "propositions": [
                *default[6].proposal["propositions"],
                {
                    "local_ref": "second_process_statement",
                    "text": "This is another bounded synthetic statement.",
                    "content_class": "ReviewProcessStatement",
                    "claim_refs": [],
                    "citation_bindings": [],
                    "corpus_fact_refs": [],
                    "process_fact_refs": ["PF0001"],
                },
            ]
        },
        dependencies=("ReviewProcessFact:PF0001",),
    )
    operations = [*default[:6], first_batch, default[7], default[6]]

    with pytest.raises(PilotSequenceError, match="repeats before every draft"):
        _validate(operations)


def test_sentence_repair_cannot_start_before_prior_batch_is_fully_audited() -> None:
    default = _default_operations()
    first_batch = _Task(
        TaskType.RENDER_PROSE,
        {
            "sentences": [
                *default[8].proposal["sentences"],
                {
                    "local_ref": "sentence_two",
                    "text": "This is another bounded synthetic pilot sentence.",
                    "source_proposition_refs": ["PR0001"],
                },
            ]
        },
        dependencies=(
            "PropositionRecord:PR0001",
            "SemanticAuditResult:SA0001",
        ),
    )
    operations = [*default[:8], first_batch, default[9], default[8]]

    with pytest.raises(PilotSequenceError, match="repeats before every draft"):
        _validate(operations)


def test_later_phase_cannot_start_before_selected_candidates_are_assessed() -> None:
    enabled = (
        RetrievalIntent.SUPPORT,
        RetrievalIntent.CONTRADICTION,
        RetrievalIntent.BOUNDARY,
        RetrievalIntent.ALTERNATIVE,
    )
    snapshot = _snapshot_with_queries(enabled)
    candidate_key = _sha("d")
    operations = _default_operations()[:5]
    operations[4] = _query_task(snapshot)
    operations.extend(
        [
            _Control(
                PilotStage.RETRIEVAL,
                selected_candidate_keys=(candidate_key,),
            ),
            _Task(
                TaskType.AGGREGATE_PAPER_EVIDENCE,
                {"claim_ref": "C0001"},
            ),
        ]
    )
    with pytest.raises(PilotSequenceError, match="exactly cover selected"):
        _validate(operations, snapshot=snapshot)


def test_claim_validation_phase_requires_every_claim_assessment() -> None:
    enabled = (
        RetrievalIntent.SUPPORT,
        RetrievalIntent.CONTRADICTION,
        RetrievalIntent.BOUNDARY,
        RetrievalIntent.ALTERNATIVE,
    )
    snapshot = _snapshot_with_queries(enabled)
    operations = _default_operations()[:5]
    operations[4] = _query_task(snapshot)
    operations.extend(
        [
            _Control(PilotStage.RETRIEVAL),
            _Task(
                TaskType.REVISE_CLAIM,
                {
                    "claim_ref": "C0001",
                    "final_claim": "A prematurely revised synthetic claim.",
                },
                dependencies=("CandidateClaim:C0001",),
            ),
        ]
    )
    with pytest.raises(PilotSequenceError, match="lacks assessment|every CandidateClaim"):
        _validate(operations, snapshot=snapshot)


def test_claim_validation_phase_requires_complete_evidence_partition() -> None:
    enabled = (
        RetrievalIntent.SUPPORT,
        RetrievalIntent.CONTRADICTION,
        RetrievalIntent.BOUNDARY,
        RetrievalIntent.ALTERNATIVE,
    )
    base = _snapshot_with_queries(enabled)
    query = next(
        item
        for item in base.retrieval_queries
        if item.intent is RetrievalIntent.SUPPORT
    )
    source_text = "Synthetic evidence"
    span = RetrievedSpan(
        span_id="R0001",
        paper_id="P0001",
        locator=SpanLocator(
            raw_md_path=base.papers[0].raw_md_path,
            page=None,
            section="Synthetic results",
            start_offset=0,
            end_offset=len(source_text),
            source_span_hash=hash_bytes(source_text.encode("utf-8")),
        ),
        source_text=source_text,
        retrieval=RetrievalMetadata(
            query_id=query.query_id,
            intent=query.intent,
            retrieval_score=1.0,
        ),
    )
    disposition = RetrievalDisposition(
        span_id=span.span_id,
        status="assessed",
        reason=None,
        canonical_span_id=None,
    )
    evidence = EvidenceRecord(
        evidence_id="E0001",
        claim_id="C0001",
        retrieved_span_id=span.span_id,
        paper_id=span.paper_id,
        relation_to_candidate="supports",
        evidence_summary="The synthetic result supports the candidate.",
        quality=EvidenceQuality(
            directness="direct",
            methodological_relevance="high",
            strength="moderate",
            assessability="full",
            limitations=[],
        ),
        assessment_note="Bounded synthetic evidence.",
    )
    snapshot = base.model_copy(
        update={
            "retrieved_spans": (span,),
            "retrieval_dispositions": (disposition,),
            "evidence_records": (evidence,),
        }
    )
    snapshot.validate_repository()
    candidate_key = _sha("d")
    operations = _default_operations()[:5]
    operations[4] = _query_task(snapshot)
    operations.extend(
        [
            _Control(
                PilotStage.RETRIEVAL,
                selected_candidate_keys=(candidate_key,),
                budget={"assessed_candidates": 1},
            ),
            _Task(
                TaskType.ASSESS_EVIDENCE,
                {"decisions": [{"candidate_ref": candidate_key}]},
                canonical=(("EvidenceRecord:E0001", "EvidenceRecord:E0001"),),
            ),
            _Task(
                TaskType.REVISE_CLAIM,
                {
                    "claim_ref": "C0001",
                    "final_claim": "A prematurely revised synthetic claim.",
                },
                dependencies=("CandidateClaim:C0001",),
            ),
        ]
    )
    with pytest.raises(PilotSequenceError, match="exactly one CPE"):
        _validate(operations, snapshot=snapshot)


def test_proposition_phase_requires_terminal_eligible_claims() -> None:
    enabled = (
        RetrievalIntent.SUPPORT,
        RetrievalIntent.CONTRADICTION,
        RetrievalIntent.BOUNDARY,
        RetrievalIntent.ALTERNATIVE,
    )
    base = _snapshot_with_queries(enabled)
    assessment = ClaimAssessment(
        claim_id="C0001",
        aggregate_strength="moderate",
        evidence_sufficiency="sufficient",
        decision="RETAIN",
        rejection_basis=None,
        support_summary="Synthetic retained assessment.",
        contradiction_summary="No contradiction in this bounded fixture.",
        qualification_summary="The fixture is intentionally bounded.",
        reason="The synthetic assessment is eligible for final validation.",
    )
    snapshot = base.model_copy(update={"claim_assessments": (assessment,)})
    snapshot.validate_repository()
    assessment_task = _Task(
        TaskType.ASSESS_CLAIM,
        {
            "claim_ref": "C0001",
            "aggregate_strength": "moderate",
            "evidence_sufficiency": "sufficient",
            "decision": "RETAIN",
            "rejection_basis": None,
            "support_summary": assessment.support_summary,
            "contradiction_summary": assessment.contradiction_summary,
            "qualification_summary": assessment.qualification_summary,
            "reason": assessment.reason,
        },
        dependencies=("CandidateClaim:C0001",),
        canonical=(("ClaimAssessment:C0001", "ClaimAssessment:C0001"),),
    )
    operations = _default_operations()[:5]
    operations[4] = _query_task(snapshot)
    operations.extend(
        [
            _Control(PilotStage.RETRIEVAL),
            assessment_task,
            _default_operations()[6],
        ]
    )
    with pytest.raises(PilotSequenceError, match="lacks final validation"):
        _validate(operations, snapshot=snapshot)


def test_render_and_assembly_require_complete_draft_audit_coverage() -> None:
    operations = _default_operations()
    render_before_proposition_audit = [*operations[:7], operations[8]]
    with pytest.raises(PilotSequenceError, match="audit coverage is incomplete"):
        _validate(render_before_proposition_audit)

    assembly_before_sentence_audit = [*operations[:9], operations[10]]
    with pytest.raises(PilotSequenceError, match="audit coverage is incomplete"):
        _validate(assembly_before_sentence_audit)


def test_nonentailed_sentence_audit_is_authoritative_without_rs_rsa_pair() -> None:
    snapshot = _snapshot().model_copy(
        update={"rendered_sentences": (), "rendered_sentence_audits": ()}
    )
    snapshot.validate_repository()
    nonentailed = _Task(
        TaskType.AUDIT_RENDERED_SENTENCE,
        {
            "sentence_ref": "sentence_one",
            "verdict": "UNSUPPORTED",
            "reason": "The proposed sentence exceeds its audited proposition.",
        },
        canonical=(),
        downstream_eligible=False,
    )
    operations = [*_default_operations()[:9], nonentailed]
    summary = _validate(operations, snapshot=snapshot)
    assert summary.closure_complete is False
    assert summary.semantic_task_count == 8

    forged_pair = nonentailed.__class__(
        task_type=nonentailed.task_type,
        proposal=nonentailed.proposal,
        canonical=(
            ("RenderedSentence:RS0001", "RenderedSentence:RS0001"),
            (
                "RenderedSentenceAudit:RSA0001",
                "RenderedSentenceAudit:RSA0001",
            ),
        ),
        downstream_eligible=False,
    )
    with pytest.raises(
        PilotSequenceError,
        match="non-ENTAILED sentence audit cannot create an RS/RSA pair",
    ):
        _validate([*_default_operations()[:9], forged_pair])


@pytest.mark.parametrize(
    "field",
    ["locator_verification", "citation_authorization"],
)
def test_terminal_validation_rejects_failed_optional_checks(field: str) -> None:
    operations = _default_operations()
    operations[-1] = _Control(
        PilotStage.VALIDATION_REPORT,
        validation_statuses={field: ValidationStatus.FAILED},
    )
    with pytest.raises(PilotSequenceError, match="does not close"):
        _validate(operations)
