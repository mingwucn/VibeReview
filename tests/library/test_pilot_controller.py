from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import vibereview.library.pilot_controller as controller_module
from vibereview.library.git_source import PinnedGitSource
from vibereview.library.inventory import build_library_inventory
from vibereview.library.models import (
    CorpusSelectionDocument,
    CorpusSelectionManifest,
    DocumentKind,
    LibraryConfig,
)
from vibereview.library.pilot_controller import (
    MOCK_USAGE_UNAVAILABLE_REASON,
    PilotBudgetError,
    SyntheticPilotController,
    SyntheticPilotControllerError,
)
from vibereview.library.pilot_setup import (
    PILOT_SCOPE_FACT_TEXT,
    register_synthetic_pilot_setup,
)
from vibereview.library.retrieval import UnifiedRetrievalCoordinator
from vibereview.library.selection import import_selected_corpus
from vibereview.runtime.discovery import DiscoveryArtifactReference
from vibereview.runtime.drafts import DraftArtifactReference
from vibereview.runtime.dto import (
    CandidateClaimProposal,
    ClaimAssessmentProposal,
    DiscoveryCoverageFindingProposal,
    DiscoveryFindingProposal,
    DiscoveryInputDispositionProposal,
    DiscoveryPaperCandidateProposal,
    DiscoveryProposalBundle,
    DiscoverySourceBinding,
    DiscoveryTerminologyProposal,
    PaperConceptSketchProposal,
    PropositionProposalBundle,
    RenderedSentenceAuditProposal,
    RenderedSentenceProposalBundle,
    ResourceTextLocator,
    RetrievalQueryProposal,
    RetrievalQueryProposalBundle,
    SemanticAuditProposal,
    ThemeProposal,
)
from vibereview.runtime.engine import MockEngine, MockResponse
from vibereview.runtime.hashing import hash_bytes, hash_text
from vibereview.runtime.kernel import ProjectRuntime
from vibereview.runtime.pilot_journal import (
    PilotJournalError,
    load_pilot_control_artifact,
    load_pilot_stage_artifact,
)
from vibereview.runtime.pilot_manifest import build_synthetic_pilot_run_manifest
from vibereview.runtime.pilot_records import (
    EngineUsageRecord,
    FivePaperPilotBudget,
    PilotStage,
    PilotStageStatus,
)
from vibereview.runtime.pilot_sequence import (
    load_fixed_pilot_sequence,
    validate_fixed_pilot_sequence,
)
from vibereview.runtime.pilot_usage import PilotTaskUsageArtifact
from vibereview.runtime.records import (
    AttemptOutcome,
    RuntimeConfig,
    TaskType,
)
from vibereview.runtime.repository import (
    GenerationStore,
    PromotionPayload,
    StaleSnapshotError,
)


TOPIC = "synthetic residual stress"
PARSE_REFS = (
    "parsed_theme",
    "parsed_claim",
    "term_stress",
    "candidate_paper",
    "controversy_scale",
    "gap_boundary",
)


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _five_paper_library(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> LibraryConfig:
    repository, config, _ = synthetic_library
    for ordinal in range(3, 6):
        (repository / "papers" / f"Fixture - 2030 - Paper {ordinal}.md").write_text(
            f"# Synthetic Paper {ordinal}\n\n"
            f"A fictitious observation numbered {ordinal}.\n",
            encoding="utf-8",
        )
    _git(repository, "add", "papers")
    _git(repository, "commit", "-m", "add pilot-controller fixture papers")
    commit = _git(repository, "rev-parse", "HEAD")
    _git(config.superproject_path, "add", config.gitlink_path)
    _git(config.superproject_path, "commit", "-m", "advance pilot fixture")
    return config.model_copy(update={"expected_commit": commit})


def _selection(config: LibraryConfig) -> CorpusSelectionManifest:
    inventory, _ = build_library_inventory(PinnedGitSource.open(config), config)
    documents = [
        CorpusSelectionDocument(
            source_relative_path=item.source_relative_path,
            content_sha256=item.content_sha256,
            decision="include",
            role="synthetic_five_paper_pilot",
            accepted_by="controller-fixture",
            accepted_at="2030-01-01T00:00:00Z",
            reason="Exercise the deterministic synthetic controller.",
        )
        for item in inventory
        if item.document_kind is DocumentKind.CANDIDATE_PAPER_MARKDOWN
    ]
    assert len(documents) == 5
    return CorpusSelectionManifest(
        library_id=config.library_id,
        source_commit=config.expected_commit,
        documents=documents,
    )


def _locator(resource_id: str, text: str, needle: str) -> ResourceTextLocator:
    start = text.index(needle)
    return ResourceTextLocator(
        resource_id=resource_id,
        resource_hash=hash_bytes(text.encode("utf-8")),
        start_offset=start,
        end_offset=start + len(needle),
        source_span_hash=hash_text(needle),
    )


def _parse_proposal(texts: tuple[str, str]) -> DiscoveryProposalBundle:
    primary = _locator("RES0001", texts[0], "Residual stress")
    secondary = _locator("RES0002", texts[1], "controversial")
    return DiscoveryProposalBundle(
        themes=[
            ThemeProposal(
                local_ref="parsed_theme",
                title="Thermal processing",
                description="Processing effects on residual stress.",
                origin="deep_research",
            )
        ],
        claims=[
            CandidateClaimProposal(
                local_ref="parsed_claim",
                theme_ref="parsed_theme",
                candidate_claim="Thermal processing changes residual stress.",
                origin="deep_research",
                origin_refs=["RES0001"],
            )
        ],
        source_bindings=[
            DiscoverySourceBinding(target_ref="parsed_theme", locators=[primary]),
            DiscoverySourceBinding(target_ref="parsed_claim", locators=[primary]),
        ],
        terminology=[
            DiscoveryTerminologyProposal(
                local_ref="term_stress",
                term="Residual stress",
                meaning="A retained synthetic discovery definition.",
                locators=[primary],
            )
        ],
        paper_candidates=[
            DiscoveryPaperCandidateProposal(
                local_ref="candidate_paper",
                citation="A candidate synthetic paper",
                relevance="Reports a synthetic disagreement.",
                locators=[primary],
            )
        ],
        controversies=[
            DiscoveryFindingProposal(
                local_ref="controversy_scale",
                summary="The synthetic effect is controversial.",
                locators=[secondary],
            )
        ],
        gaps=[
            DiscoveryFindingProposal(
                local_ref="gap_boundary",
                summary="Synthetic boundary conditions remain absent.",
                locators=[secondary],
            )
        ],
    )


def _challenge_proposal(paper_texts: dict[str, str]) -> DiscoveryProposalBundle:
    sketches = [
        PaperConceptSketchProposal(
            local_ref=f"sketch_{paper_id.lower()}",
            paper_ref=paper_id,
            summary=f"Concept sketch for {paper_id}.",
            concepts=["synthetic concept"],
            populations=["synthetic population"],
            methods=["bounded synthetic method"],
            locators=[_locator(f"RES{ordinal:04d}", text, text.splitlines()[0])],
        )
        for ordinal, (paper_id, text) in enumerate(
            sorted(paper_texts.items()), start=2
        )
    ]
    findings = [
        DiscoveryCoverageFindingProposal(
            local_ref=f"coverage_{reference}",
            discovery_ref=reference,
            status="missing",
            missing_dimensions=["boundary_condition"],
            rationale="The locked corpus does not resolve this discovery item.",
        )
        for reference in PARSE_REFS
    ]
    return DiscoveryProposalBundle(
        paper_concept_sketches=sketches,
        coverage_findings=findings,
    )


def _candidate_proposal() -> DiscoveryProposalBundle:
    included = {
        ("parse", "parsed_claim"),
        ("challenge", "coverage_parsed_claim"),
    }
    source_refs = [
        *(("parse", reference) for reference in PARSE_REFS),
        *(("challenge", f"coverage_{reference}") for reference in PARSE_REFS),
        *(("challenge", f"sketch_p{ordinal:04d}") for ordinal in range(1, 6)),
    ]
    return DiscoveryProposalBundle(
        themes=[
            ThemeProposal(
                local_ref="combined_theme",
                title="Combined thermal evidence",
                description="Discovery reconciled with the locked corpus.",
                origin="generated",
            )
        ],
        claims=[
            CandidateClaimProposal(
                local_ref="combined_claim",
                theme_ref="combined_theme",
                candidate_claim=(
                    "Thermal processing may change residual stress within tested "
                    "conditions."
                ),
                origin="generated",
                origin_refs=[
                    "parse:parsed_claim",
                    "challenge:coverage_parsed_claim",
                ],
            )
        ],
        input_dispositions=[
            DiscoveryInputDispositionProposal(
                source_kind=source_kind,
                source_ref=source_ref,
                disposition=(
                    "included" if (source_kind, source_ref) in included else "excluded"
                ),
                candidate_claim_refs=(
                    ["combined_claim"]
                    if (source_kind, source_ref) in included
                    else []
                ),
                reason=(
                    "Used in the bounded combined claim."
                    if (source_kind, source_ref) in included
                    else "Not selected for this bounded combined claim."
                ),
            )
            for source_kind, source_ref in source_refs
        ],
    )


def _query_proposal() -> RetrievalQueryProposalBundle:
    return RetrievalQueryProposalBundle(
        queries=[
            RetrievalQueryProposal(
                local_ref=f"query_{intent}",
                claim_ref="C0001",
                intent=intent,
                query_text=f"xylophonicquasar {intent} unfindabletoken",
            )
            for intent in ("support", "contradiction", "boundary", "alternative")
        ]
    )


def _response_engines(
    document_texts: tuple[str, str],
    paper_texts: dict[str, str],
    *,
    parse_failure: bool = False,
    parse_stdout: str = "",
    rendered_sentence_count: int = 1,
) -> dict[TaskType, MockEngine]:
    proposals = {
        TaskType.PARSE_DEEP_RESEARCH: _parse_proposal(document_texts),
        TaskType.CORPUS_CHALLENGER: _challenge_proposal(paper_texts),
        TaskType.GENERATE_CANDIDATE_CLAIMS: _candidate_proposal(),
        TaskType.GENERATE_RETRIEVAL_QUERIES: _query_proposal(),
        TaskType.ASSESS_CLAIM: ClaimAssessmentProposal(
            claim_ref="C0001",
            aggregate_strength="low",
            evidence_sufficiency="insufficient",
            decision="REJECT",
            rejection_basis="insufficient_evidence",
            support_summary="No selected evidence candidates.",
            contradiction_summary="No selected evidence candidates.",
            qualification_summary="No scientific claim can be retained.",
            reason="The deterministic retrieval returned no assessable evidence.",
        ),
        TaskType.GENERATE_PROPOSITIONS: PropositionProposalBundle(
            propositions=[
                {
                    "local_ref": "process_report",
                    "text": PILOT_SCOPE_FACT_TEXT,
                    "content_class": "ReviewProcessStatement",
                    "claim_refs": [],
                    "citation_bindings": [],
                    "corpus_fact_refs": [],
                    "process_fact_refs": ["PF0001"],
                }
            ]
        ),
        TaskType.AUDIT_PROPOSITION: SemanticAuditProposal(
            target_ref="process_report",
            class_verdict="CORRECT",
            provenance_verdict="ENTAILED",
            reason="Exact synthetic process-fact restatement.",
            referenced_claim_refs=[],
            referenced_corpus_fact_refs=[],
            referenced_process_fact_refs=["PF0001"],
        ),
        TaskType.RENDER_PROSE: RenderedSentenceProposalBundle(
            sentences=[
                {
                    "local_ref": (
                        "process_sentence"
                        if ordinal == 1
                        else f"process_sentence_{ordinal}"
                    ),
                    "text": PILOT_SCOPE_FACT_TEXT,
                    "source_proposition_refs": ["PR0001"],
                }
                for ordinal in range(1, rendered_sentence_count + 1)
            ]
        ),
        TaskType.AUDIT_RENDERED_SENTENCE: RenderedSentenceAuditProposal(
            sentence_ref="process_sentence",
            verdict="ENTAILED",
            reason="Exact synthetic process-fact restatement.",
        ),
    }
    engines: dict[TaskType, MockEngine] = {}
    for task_type in TaskType:
        if parse_failure and task_type is TaskType.PARSE_DEEP_RESEARCH:
            response = MockResponse(execution_error="synthetic technical failure")
        elif task_type in proposals:
            response = MockResponse(
                proposal=proposals[task_type].model_dump(mode="json"),
                stdout=(
                    parse_stdout
                    if task_type is TaskType.PARSE_DEEP_RESEARCH
                    else ""
                ),
            )
        else:
            response = MockResponse(execution_error="unused pilot role")
        engines[task_type] = MockEngine(
            [response],
            name=f"synthetic-{task_type.value}",
            version="1",
        )
    return engines


def _prepared_controller(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
    *,
    parse_failure: bool = False,
    budget: FivePaperPilotBudget | None = None,
    technical_attempts_per_engine: int = 1,
    rendered_sentence_count: int = 1,
    parse_stdout: str = "",
):
    config = _five_paper_library(synthetic_library)
    review_root = tmp_path / "review"
    imported = import_selected_corpus(
        review_root,
        _selection(config),
        config,
        public_repository_root=tmp_path / "public",
    )
    resources = tmp_path / "discovery"
    resources.mkdir()
    document_texts = (
        "Residual stress is a defined synthetic outcome.",
        "Thermal processing is controversial in the synthetic review.",
    )
    documents = tuple(
        resources / f"source-{ordinal}.md" for ordinal in range(1, 3)
    )
    for path, text in zip(documents, document_texts, strict=True):
        path.write_text(text, encoding="utf-8")
    runtime = ProjectRuntime(
        review_root,
        config=RuntimeConfig(
            max_fallback_engines=0,
            technical_attempts_per_engine=technical_attempts_per_engine,
        ),
        allowed_source_roots=(review_root, resources),
    )
    _, snapshot, _ = runtime.store.load_current()
    assert imported.generation == runtime.store.current_generation()
    paper_texts = {
        paper.paper_id: (review_root / paper.raw_md_path).read_text(encoding="utf-8")
        for paper in snapshot.papers
    }
    engines = _response_engines(
        document_texts,
        paper_texts,
        parse_failure=parse_failure,
        parse_stdout=parse_stdout,
        rendered_sentence_count=rendered_sentence_count,
    )
    manifest = build_synthetic_pilot_run_manifest(
        runtime,
        run_id="RUN-controller-process-only",
        topic=TOPIC,
        discovery_document_paths=documents,
        engines=engines,
        budget=budget,
        created_at="2030-01-01T00:00:00+00:00",
    )
    setup = register_synthetic_pilot_setup(
        review_root,
        manifest,
        public_repository_root=tmp_path / "public",
    )
    controller = SyntheticPilotController(
        runtime, manifest, setup.registration, engines
    )
    return controller, documents, engines


def _run_prefix_and_retrieval(
    controller: SyntheticPilotController,
    documents: tuple[Path, Path],
) -> str:
    controller.record_prerequisites()
    parsed = controller.parse_deep_research(topic=TOPIC, document_paths=documents)
    assert isinstance(parsed.artifact_reference, DiscoveryArtifactReference)
    challenged = controller.corpus_challenger(
        topic=TOPIC,
        discovery_artifact=parsed.artifact_reference,
    )
    assert isinstance(challenged.artifact_reference, DiscoveryArtifactReference)
    generated = controller.generate_candidate_claims(
        topic=TOPIC,
        discovery_artifact=parsed.artifact_reference,
        challenge_artifact=challenged.artifact_reference,
    )
    assert generated.runtime_result is not None
    assert generated.runtime_result.allocated_ids == {
        "combined_theme": "T0001",
        "combined_claim": "C0001",
    }
    controller.generate_retrieval_queries(claim_ids=["C0001"])
    generation, snapshot, _ = controller.runtime.store.load_current()
    coordinator = UnifiedRetrievalCoordinator.from_generation(
        controller.runtime.project_root, generation=generation
    )
    ledgers = []
    for query in sorted(snapshot.retrieval_queries, key=lambda item: item.query_id):
        _, ledger = coordinator.retrieve(
            query.query_text,
            query.query_id,
            query.intent,
            top_k=controller.manifest.budget.retrieval_top_k_per_backend,
        )
        assert ledger.selected_candidate_keys == []
        ledgers.append(ledger)
    controller.record_retrieval(ledgers)
    assert parsed.runtime_result is not None
    return parsed.runtime_result.task_id


def test_process_only_controller_closes_exact_atomic_journal(
    synthetic_library, tmp_path
) -> None:
    controller, documents, engines = _prepared_controller(
        synthetic_library, tmp_path
    )
    assert all(engine.calls == 0 for engine in engines.values())

    parse_task_id = _run_prefix_and_retrieval(controller, documents)
    parse_input = json.loads(
        (
            controller.runtime.project_root
            / "work"
            / "tasks"
            / parse_task_id
            / "bundle"
            / "input"
            / "input.json"
        ).read_text(encoding="utf-8")
    )
    assert parse_input == {
        "topic": TOPIC,
        "document_resource_ids": ["RES0001", "RES0002"],
    }
    recovered = SyntheticPilotController(
        controller.runtime,
        controller.manifest,
        controller.registration,
        engines,
    )
    assert recovered.head == controller.head
    assert recovered.head is not None
    assert load_fixed_pilot_sequence(
        recovered.runtime.project_root, recovered.head
    ) == load_fixed_pilot_sequence(
        controller.runtime.project_root, controller.head
    )
    controller = recovered
    rejected = controller.assess_claim(
        claim_id="C0001", claim_paper_evidence_ids=[]
    )
    assert rejected.runtime_result is not None
    assert rejected.runtime_result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert rejected.runtime_result.transition is not None
    assert rejected.runtime_result.transition.scientific_disposition == "REJECT"
    assert engines[TaskType.ASSESS_CLAIM].calls == 1

    proposition = controller.generate_propositions(
        claim_packet_ids=[], corpus_fact_ids=[], process_fact_ids=["PF0001"]
    )
    assert isinstance(proposition.artifact_reference, DraftArtifactReference)
    audited = controller.audit_proposition(
        draft=proposition.artifact_reference, local_ref="process_report"
    )
    assert audited.runtime_result is not None
    proposition_id = audited.runtime_result.allocated_ids["process_report"]
    sentence = controller.render_prose(proposition_ids=[proposition_id])
    assert isinstance(sentence.artifact_reference, DraftArtifactReference)
    sentence_audit = controller.audit_rendered_sentence(
        draft=sentence.artifact_reference, local_ref="process_sentence"
    )
    assert sentence_audit.runtime_result is not None
    assert sentence_audit.runtime_result.allocated_ids["process_sentence"] == "RS0001"

    assembled = controller.exact_assembly()
    validated = controller.validation_report()
    assert assembled.assembly is not None and validated.assembly is not None
    assert assembled.body == validated.body == (PILOT_SCOPE_FACT_TEXT + "\n").encode()
    assert validated.validation is not None
    assert validated.validation.report.source_generation == assembled.assembly.source_generation
    assert validated.head.owner_generation == assembled.assembly.source_generation + 1
    assert validated.validation.report.repository_hash == assembled.assembly.repository_hash

    summary = validate_fixed_pilot_sequence(
        controller.runtime.project_root, validated.head
    )
    assert summary.closure_complete
    events = load_fixed_pilot_sequence(
        controller.runtime.project_root, validated.head
    )
    task_events = [item for item in events if item.task_provenance is not None]
    assert len(task_events) == 9
    assert all(
        any("/usage/" in path for path in event.stage_record.artifact_hashes)
        for event in task_events
    )
    assert all(
        any(
            "/attempt-usage/" in path
            for path in event.stage_record.artifact_hashes
        )
        for event in task_events
    )
    for event in task_events:
        usage_path = next(
            path for path in event.stage_record.artifact_hashes if "/usage/" in path
        )
        _, _, selected = GenerationStore(
            controller.runtime.project_root
        ).load_generation_auxiliary(event.stage_record.committed_generation, {usage_path})
        usage = EngineUsageRecord.model_validate_json(selected[usage_path])
        assert usage.unavailable_reason == MOCK_USAGE_UNAVAILABLE_REASON
        attempt_usage_path = next(
            path
            for path in event.stage_record.artifact_hashes
            if "/attempt-usage/" in path
        )
        _, _, selected = GenerationStore(
            controller.runtime.project_root
        ).load_generation_auxiliary(
            event.stage_record.committed_generation, {attempt_usage_path}
        )
        attempt_usage = PilotTaskUsageArtifact.model_validate_json(
            selected[attempt_usage_path]
        )
        assert attempt_usage.totals.attempt_count == 1
        assert len(attempt_usage.attempts) == 1
        assert attempt_usage.attempts[0].accepted_attempt

    final_artifact = load_pilot_stage_artifact(
        controller.runtime.project_root, validated.head
    )
    budget = final_artifact.stage_record.budget_consumed
    assert budget["semantic_engine_invocations"] == 9
    assert budget["queries"] == 4
    assert budget["raw_candidates"] == 0
    assert budget.get("assessed_candidates", 0) == 0
    assert budget["propositions"] == budget["sentences"] == 1
    assert budget["request_bytes"] > 0
    assert budget["proposal_bytes"] > 0
    _, snapshot, _ = controller.runtime.store.load_current()
    assert len(snapshot.claim_assessments) == 1
    assert snapshot.claim_assessments[0].decision.value == "REJECT"
    assert snapshot.claim_packets == ()

    closed_generation = controller.runtime.store.current_generation()
    closed_head = controller.head
    calls_before_replay = engines[TaskType.ASSESS_CLAIM].calls
    replay = controller.assess_claim(
        claim_id="C0001", claim_paper_evidence_ids=[]
    )
    assert replay.runtime_result is not None
    assert replay.runtime_result.receipt_reused
    assert not replay.runtime_result.commit_performed
    assert replay.runtime_result.generation == closed_generation
    assert replay.runtime_result.reused_generation == rejected.head.owner_generation
    assert replay.head == closed_head
    assert controller.runtime.store.current_generation() == closed_generation
    assert engines[TaskType.ASSESS_CLAIM].calls == calls_before_replay
    assert load_fixed_pilot_sequence(
        controller.runtime.project_root, replay.head
    ) == events


def test_constructor_and_parse_binding_fail_before_any_engine_call(
    synthetic_library, tmp_path
) -> None:
    controller, documents, engines = _prepared_controller(
        synthetic_library, tmp_path
    )
    mismatched = dict(engines)
    mismatched[TaskType.PARSE_DEEP_RESEARCH] = MockEngine(
        [MockResponse(proposal={})], name="wrong-role", version="1"
    )
    with pytest.raises(SyntheticPilotControllerError, match="engine role"):
        SyntheticPilotController(
            controller.runtime,
            controller.manifest,
            controller.registration,
            mismatched,
        )
    callback_engines = dict(engines)
    callback_engines[TaskType.PARSE_DEEP_RESEARCH] = MockEngine(
        [MockResponse(execution_error="must not run")],
        name=engines[TaskType.PARSE_DEEP_RESEARCH].name,
        version=engines[TaskType.PARSE_DEEP_RESEARCH].version,
        on_execute=lambda _task, _call: None,
    )
    with pytest.raises(SyntheticPilotControllerError, match="callbacks"):
        SyntheticPilotController(
            controller.runtime,
            controller.manifest,
            controller.registration,
            callback_engines,
        )
    assert all(engine.calls == 0 for engine in engines.values())

    controller.record_prerequisites()
    before = controller.runtime.store.current_generation()
    with pytest.raises(SyntheticPilotControllerError, match="hashes/order"):
        controller.parse_deep_research(
            topic=TOPIC, document_paths=tuple(reversed(documents))
        )
    assert engines[TaskType.PARSE_DEEP_RESEARCH].calls == 0
    assert controller.runtime.store.current_generation() == before

    GenerationStore(controller.runtime.project_root).commit(
        base_generation=before,
        dependencies={},
        promotion=lambda snapshot, registry: PromotionPayload(snapshot, registry, {}),
    )
    with pytest.raises(StaleSnapshotError, match="head is not CURRENT"):
        controller.parse_deep_research(topic=TOPIC, document_paths=documents)
    assert engines[TaskType.PARSE_DEEP_RESEARCH].calls == 0


def test_controller_recovers_the_authenticated_current_journal_head(
    synthetic_library, tmp_path
) -> None:
    controller, documents, engines = _prepared_controller(
        synthetic_library, tmp_path
    )
    controller.record_prerequisites()
    parsed = controller.parse_deep_research(
        topic=TOPIC, document_paths=documents
    )
    fresh_engines = {
        task_type: MockEngine(
            list(engine._responses),  # noqa: SLF001 - exact synthetic restart
            name=engine.name,
            version=engine.version,
        )
        for task_type, engine in engines.items()
    }
    recovered = SyntheticPilotController(
        controller.runtime,
        controller.manifest,
        controller.registration,
        fresh_engines,
    )
    assert recovered.head == parsed.head
    assert recovered.runtime.store.current_generation() == parsed.head.owner_generation
    assert fresh_engines[TaskType.PARSE_DEEP_RESEARCH].calls == 1
    replay = recovered.parse_deep_research(
        topic=TOPIC, document_paths=documents
    )
    assert replay.runtime_result is not None
    assert replay.runtime_result.receipt_reused
    assert replay.head == parsed.head
    assert fresh_engines[TaskType.PARSE_DEEP_RESEARCH].calls == 1
    calls_before = fresh_engines[TaskType.CORPUS_CHALLENGER].calls
    challenged = recovered.corpus_challenger(
        topic=TOPIC,
        discovery_artifact=parsed.artifact_reference,
    )
    assert challenged.runtime_result is not None
    assert challenged.runtime_result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert fresh_engines[TaskType.CORPUS_CHALLENGER].calls == calls_before + 1


def test_controller_rejects_mock_engine_cursor_drift(
    synthetic_library, tmp_path
) -> None:
    controller, documents, engines = _prepared_controller(
        synthetic_library, tmp_path
    )
    controller.record_prerequisites()
    engines[TaskType.PARSE_DEEP_RESEARCH].execute(None)  # type: ignore[arg-type]

    with pytest.raises(
        SyntheticPilotControllerError,
        match="MockEngine cursor differs from journal",
    ):
        controller.parse_deep_research(topic=TOPIC, document_paths=documents)
    assert controller.head is not None
    assert (
        controller.runtime.store.current_generation()
        == controller.head.owner_generation
    )


def test_technical_failure_is_one_terminal_control_event(
    synthetic_library, tmp_path
) -> None:
    controller, documents, engines = _prepared_controller(
        synthetic_library, tmp_path, parse_failure=True
    )
    first = controller.record_prerequisites()
    failed = controller.parse_deep_research(topic=TOPIC, document_paths=documents)
    assert failed.runtime_result is not None
    assert failed.runtime_result.outcome is AttemptOutcome.ENGINE_EXECUTION_FAILURE
    assert failed.control_result is not None
    assert failed.control_result.reference.ordinal == first.head.ordinal + 1
    artifact = load_pilot_stage_artifact(
        controller.runtime.project_root, failed.head
    )
    assert artifact.task_provenance is None
    assert artifact.stage_record.stage is PilotStage.DISCOVERY
    assert artifact.stage_record.status is PilotStageStatus.FAILED
    assert engines[TaskType.PARSE_DEEP_RESEARCH].calls == 1
    with pytest.raises(SyntheticPilotControllerError, match="terminal"):
        controller.parse_deep_research(topic=TOPIC, document_paths=documents)


def test_attempt_budget_overrun_is_one_bounded_terminal_event(
    synthetic_library, tmp_path
) -> None:
    controller, documents, engines = _prepared_controller(
        synthetic_library,
        tmp_path,
        budget=FivePaperPilotBudget(max_stdout_bytes_per_attempt=1),
        parse_stdout="XX",
    )
    controller.record_prerequisites()

    failed = controller.parse_deep_research(
        topic=TOPIC, document_paths=documents
    )

    assert failed.runtime_result is not None
    assert failed.runtime_result.outcome is AttemptOutcome.INTERNAL_RUNTIME_FAILURE
    assert failed.control_result is not None
    assert engines[TaskType.PARSE_DEEP_RESEARCH].calls == 1
    control = load_pilot_control_artifact(
        controller.runtime.project_root,
        load_pilot_stage_artifact(
            controller.runtime.project_root, failed.head
        ),
    )
    assert control.payload["budget_overruns"] == {
        "per_attempt_stdout_bytes": 1
    }
    usage = PilotTaskUsageArtifact.model_validate(
        control.payload["attempt_usage"]
    )
    assert usage.totals.stdout_bytes == 2
    assert usage.attempts[0].stdout_bytes == 2
    with pytest.raises(SyntheticPilotControllerError, match="terminal"):
        controller.parse_deep_research(topic=TOPIC, document_paths=documents)


def test_engine_call_budget_is_enforced_from_recorded_usage(
    synthetic_library, tmp_path
) -> None:
    controller, documents, engines = _prepared_controller(
        synthetic_library,
        tmp_path,
        budget=FivePaperPilotBudget(max_semantic_engine_invocations=1),
    )
    controller.record_prerequisites()
    parsed = controller.parse_deep_research(topic=TOPIC, document_paths=documents)
    assert parsed.runtime_result is not None
    assert engines[TaskType.PARSE_DEEP_RESEARCH].calls == 1
    before = controller.runtime.store.current_generation()
    with pytest.raises(PilotBudgetError, match="invocation budget"):
        controller.corpus_challenger(
            topic=TOPIC,
            discovery_artifact=parsed.artifact_reference,
        )
    assert engines[TaskType.CORPUS_CHALLENGER].calls == 0
    assert controller.runtime.store.current_generation() == before


def test_remaining_engine_budget_reserves_the_full_technical_retry_ceiling(
    synthetic_library, tmp_path
) -> None:
    controller, documents, engines = _prepared_controller(
        synthetic_library,
        tmp_path,
        budget=FivePaperPilotBudget(max_semantic_engine_invocations=1),
        technical_attempts_per_engine=2,
    )
    controller.record_prerequisites()
    before = controller.runtime.store.current_generation()
    with pytest.raises(PilotBudgetError, match="all technical attempts"):
        controller.parse_deep_research(topic=TOPIC, document_paths=documents)
    assert engines[TaskType.PARSE_DEEP_RESEARCH].calls == 0
    assert controller.runtime.store.current_generation() == before


def test_task_elapsed_budget_aborts_semantic_publication(
    synthetic_library, tmp_path, monkeypatch
) -> None:
    controller, documents, engines = _prepared_controller(
        synthetic_library,
        tmp_path,
        budget=FivePaperPilotBudget(max_task_seconds=1),
    )
    controller.record_prerequisites()
    ticks = iter((0.0, 2.0, 2.0))
    monkeypatch.setattr(
        controller_module, "_monotonic", lambda: next(ticks, 2.0)
    )

    failed = controller.parse_deep_research(
        topic=TOPIC, document_paths=documents
    )

    assert failed.runtime_result is not None
    assert failed.runtime_result.outcome is AttemptOutcome.INTERNAL_RUNTIME_FAILURE
    assert failed.control_result is not None
    assert engines[TaskType.PARSE_DEEP_RESEARCH].calls == 1
    events = load_fixed_pilot_sequence(
        controller.runtime.project_root, failed.head
    )
    assert all(item.task_provenance is None for item in events)
    assert controller.runtime.store.load_receipts(failed.head.owner_generation) == ()


def test_exact_assembly_is_rejected_before_commit_when_a_draft_audit_is_missing(
    synthetic_library, tmp_path
) -> None:
    controller, documents, _ = _prepared_controller(
        synthetic_library,
        tmp_path,
        rendered_sentence_count=2,
    )
    _run_prefix_and_retrieval(controller, documents)
    controller.assess_claim(claim_id="C0001", claim_paper_evidence_ids=[])
    proposition = controller.generate_propositions(
        claim_packet_ids=[], corpus_fact_ids=[], process_fact_ids=["PF0001"]
    )
    assert isinstance(proposition.artifact_reference, DraftArtifactReference)
    audited = controller.audit_proposition(
        draft=proposition.artifact_reference, local_ref="process_report"
    )
    assert audited.runtime_result is not None
    rendered = controller.render_prose(
        proposition_ids=[audited.runtime_result.allocated_ids["process_report"]]
    )
    assert isinstance(rendered.artifact_reference, DraftArtifactReference)
    controller.audit_rendered_sentence(
        draft=rendered.artifact_reference, local_ref="process_sentence"
    )
    before = controller.runtime.store.current_generation()
    with pytest.raises(PilotJournalError, match="pre-commit validation"):
        controller.exact_assembly()
    assert controller.runtime.store.current_generation() == before
    assert controller.head is not None
    assert controller.head.owner_generation == before
