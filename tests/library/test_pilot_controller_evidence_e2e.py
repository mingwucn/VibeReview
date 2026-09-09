from __future__ import annotations

import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from vibereview.enums import RetrievalIntent
from vibereview.library.git_source import PinnedGitSource
from vibereview.library.inventory import build_library_inventory
from vibereview.library.models import (
    CorpusSelectionDocument,
    CorpusSelectionManifest,
    DocumentKind,
    LibraryConfig,
)
from vibereview.library.pilot_controller import SyntheticPilotController
from vibereview.library.pilot_setup import register_synthetic_pilot_setup
from vibereview.library.retrieval import RetrievalLedger, UnifiedRetrievalCoordinator
from vibereview.library.selection import import_selected_corpus
from vibereview.models import ComponentRelations
from vibereview.runtime.discovery import DiscoveryArtifactReference
from vibereview.runtime.drafts import DraftArtifactReference
from vibereview.runtime.dto import (
    AssessEvidenceProposalBundle,
    CandidateClaimProposal,
    ClaimAssessmentProposal,
    ClaimPaperEvidenceProposal,
    DiscoveryCoverageFindingProposal,
    DiscoveryFindingProposal,
    DiscoveryInputDispositionProposal,
    DiscoveryPaperCandidateProposal,
    DiscoveryProposalBundle,
    DiscoverySourceBinding,
    DiscoveryTerminologyProposal,
    EvidenceAssessmentProposal,
    EvidenceCandidateDecisionProposal,
    FinalClaimValidationProposal,
    FinalPaperRelationProposal,
    PaperConceptSketchProposal,
    PropositionProposal,
    PropositionProposalBundle,
    RenderedSentenceAuditProposal,
    RenderedSentenceProposal,
    RenderedSentenceProposalBundle,
    ResourceTextLocator,
    RetrievalQueryProposal,
    RetrievalQueryProposalBundle,
    RevisedClaimProposal,
    SemanticAuditProposal,
    ThemeProposal,
)
from vibereview.runtime.engine import MockEngine, MockResponse
from vibereview.runtime.hashing import hash_bytes, hash_text
from vibereview.runtime.kernel import ProjectRuntime
from vibereview.runtime.pilot_manifest import build_synthetic_pilot_run_manifest
from vibereview.runtime.pilot_packet import (
    verify_synthetic_pilot_packet,
    write_synthetic_pilot_packet,
)
from vibereview.runtime.pilot_records import (
    FivePaperPilotBudget,
    PilotStageStatus,
    ValidationStatus,
)
from vibereview.runtime.pilot_reproduction import (
    compare_synthetic_pilot_reproductions,
)
from vibereview.runtime.pilot_sequence import (
    load_fixed_pilot_sequence,
    validate_fixed_pilot_sequence,
)
from vibereview.runtime.records import RuntimeConfig, TaskType


TOPIC = "fictional thermal-window residual stress"
SUPPORT_QUERY = "supportsynthmarker"
CONTRADICTION_QUERY = "contradictsynthmarker"
SUPPORT_SPAN = (
    "supportsynthmarker: In the fictional low-temperature window, preheating "
    "decreased residual stress."
)
CONTRADICTION_SPAN = (
    "contradictsynthmarker: In the fictional high-temperature window, preheating "
    "increased residual stress."
)
CANDIDATE_CLAIM = (
    "Preheating may change residual stress across synthetic process windows."
)
FINAL_CLAIM = (
    "In the tested low-temperature window, preheating decreased residual stress, "
    "while the high-temperature result limits broader generalization."
)
PARSE_REFS = (
    "parsed_theme",
    "parsed_claim",
    "term_stress",
    "candidate_paper",
    "controversy_scale",
    "gap_boundary",
)


@dataclass(frozen=True, slots=True)
class _RunWitness:
    packet_dir: Path
    repository_hash: str
    section: bytes


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
    papers = {
        "Aster - 2030 - Support.md": f"# Support\n\n{SUPPORT_SPAN}\n",
        "Beryl - 2030 - Contradiction.md": (
            f"# Contradiction\n\n{CONTRADICTION_SPAN}\n"
        ),
        "Cobalt - 2030 - Neutral.md": (
            "# Neutral\n\nA third fictional observation has no retrieval marker.\n"
        ),
    }
    for name, text in papers.items():
        (repository / "papers" / name).write_text(text, encoding="utf-8")
    _git(repository, "add", "papers")
    _git(repository, "commit", "-m", "add fictional positive evidence fixture")
    commit = _git(repository, "rev-parse", "HEAD")
    _git(config.superproject_path, "add", config.gitlink_path)
    _git(config.superproject_path, "commit", "-m", "advance fictional fixture")
    return config.model_copy(update={"expected_commit": commit})


def _selection(config: LibraryConfig) -> CorpusSelectionManifest:
    inventory, _ = build_library_inventory(PinnedGitSource.open(config), config)
    documents = [
        CorpusSelectionDocument(
            source_relative_path=item.source_relative_path,
            content_sha256=item.content_sha256,
            decision="include",
            role="synthetic_positive_evidence_e2e",
            accepted_by="fictional-controller-fixture",
            accepted_at="2030-01-01T00:00:00Z",
            reason="Exercise exact support and contradiction closure.",
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


def _parse_proposal(document_texts: tuple[str, str]) -> DiscoveryProposalBundle:
    primary = _locator("RES0001", document_texts[0], "Residual stress")
    secondary = _locator("RES0002", document_texts[1], "controversial")
    return DiscoveryProposalBundle(
        themes=[
            ThemeProposal(
                local_ref="parsed_theme",
                title="Synthetic thermal processing",
                description="Fictional processing effects on residual stress.",
                origin="deep_research",
            )
        ],
        claims=[
            CandidateClaimProposal(
                local_ref="parsed_claim",
                theme_ref="parsed_theme",
                candidate_claim="Preheating changes residual stress.",
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
                meaning="A retained fictional discovery definition.",
                locators=[primary],
            )
        ],
        paper_candidates=[
            DiscoveryPaperCandidateProposal(
                local_ref="candidate_paper",
                citation="A fictional candidate publication",
                relevance="Reports a synthetic thermal-window result.",
                locators=[primary],
            )
        ],
        controversies=[
            DiscoveryFindingProposal(
                local_ref="controversy_scale",
                summary="The fictional direction of effect is controversial.",
                locators=[secondary],
            )
        ],
        gaps=[
            DiscoveryFindingProposal(
                local_ref="gap_boundary",
                summary="Synthetic process-window boundaries require checking.",
                locators=[secondary],
            )
        ],
    )


def _challenge_proposal(paper_texts: dict[str, str]) -> DiscoveryProposalBundle:
    sketches = [
        PaperConceptSketchProposal(
            local_ref=f"sketch_{paper_id.lower()}",
            paper_ref=paper_id,
            summary=f"Fictional concept sketch for {paper_id}.",
            concepts=["residual stress"],
            populations=["synthetic process window"],
            methods=["bounded fictional comparison"],
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
            rationale="The exact fictional input requires corpus qualification.",
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
                title="Synthetic thermal-window evidence",
                description="Discovery reconciled with the locked fictional corpus.",
                origin="generated",
            )
        ],
        claims=[
            CandidateClaimProposal(
                local_ref="combined_claim",
                theme_ref="combined_theme",
                candidate_claim=CANDIDATE_CLAIM,
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
                    "included"
                    if (source_kind, source_ref) in included
                    else "excluded"
                ),
                candidate_claim_refs=(
                    ["combined_claim"]
                    if (source_kind, source_ref) in included
                    else []
                ),
                reason=(
                    "Used by the bounded synthetic candidate."
                    if (source_kind, source_ref) in included
                    else "Excluded from the one bounded synthetic candidate."
                ),
            )
            for source_kind, source_ref in source_refs
        ],
    )


def _query_proposal() -> RetrievalQueryProposalBundle:
    texts = {
        "support": SUPPORT_QUERY,
        "contradiction": CONTRADICTION_QUERY,
        "boundary": "boundarysynthmarker",
        "alternative": "alternativesynthmarker",
    }
    return RetrievalQueryProposalBundle(
        queries=[
            RetrievalQueryProposal(
                local_ref=f"query_{intent}",
                claim_ref="C0001",
                intent=intent,
                query_text=texts[intent],
            )
            for intent in ("support", "contradiction", "boundary", "alternative")
        ]
    )


def _response(proposal) -> MockResponse:
    return MockResponse(proposal=proposal.model_dump(mode="json"))


def _engines(
    *,
    document_texts: tuple[str, str],
    paper_texts: dict[str, str],
    support_candidate_key: str,
    support_paper_id: str,
    contradiction_candidate_key: str,
    contradiction_paper_id: str,
) -> dict[TaskType, MockEngine]:
    support_cpe_id = f"CPE-C0001-{support_paper_id}"
    contradiction_cpe_id = f"CPE-C0001-{contradiction_paper_id}"
    cited_papers = sorted((support_paper_id, contradiction_paper_id))
    rendered_text = f"{FINAL_CLAIM} [@{' @'.join(cited_papers)}]"
    scripts = {
        TaskType.PARSE_DEEP_RESEARCH: [_response(_parse_proposal(document_texts))],
        TaskType.CORPUS_CHALLENGER: [_response(_challenge_proposal(paper_texts))],
        TaskType.GENERATE_CANDIDATE_CLAIMS: [_response(_candidate_proposal())],
        TaskType.GENERATE_RETRIEVAL_QUERIES: [_response(_query_proposal())],
        TaskType.ASSESS_EVIDENCE: [
            _response(
                AssessEvidenceProposalBundle(
                    decisions=[
                        EvidenceCandidateDecisionProposal(
                            candidate_ref=support_candidate_key,
                            status="assessed",
                            evidence=EvidenceAssessmentProposal(
                                relation_to_candidate="supports",
                                evidence_summary=(
                                    "The exact fictional low-temperature span "
                                    "supports a decrease."
                                ),
                                quality={
                                    "directness": "direct",
                                    "methodological_relevance": "high",
                                    "strength": "high",
                                    "assessability": "full",
                                    "limitations": ["Fictional fixture only."],
                                },
                                assessment_note="Exact raw support span assessed.",
                            ),
                        )
                    ]
                )
            ),
            _response(
                AssessEvidenceProposalBundle(
                    decisions=[
                        EvidenceCandidateDecisionProposal(
                            candidate_ref=contradiction_candidate_key,
                            status="assessed",
                            evidence=EvidenceAssessmentProposal(
                                relation_to_candidate="contradicts",
                                evidence_summary=(
                                    "The exact fictional high-temperature span "
                                    "contradicts a universal decrease."
                                ),
                                quality={
                                    "directness": "direct",
                                    "methodological_relevance": "high",
                                    "strength": "high",
                                    "assessability": "full",
                                    "limitations": ["Fictional fixture only."],
                                },
                                assessment_note="Exact raw contradiction span assessed.",
                            ),
                        )
                    ]
                )
            ),
        ],
        TaskType.AGGREGATE_PAPER_EVIDENCE: [
            _response(
                ClaimPaperEvidenceProposal(
                    claim_ref="C0001",
                    paper_ref=support_paper_id,
                    evidence_refs=["E0001"],
                    relation_to_candidate="supports",
                    component_relations=ComponentRelations(supports=["E0001"]),
                    strength="high",
                    within_paper_consistency="consistent",
                    assessment_note="The complete support-paper evidence supports.",
                )
            ),
            _response(
                ClaimPaperEvidenceProposal(
                    claim_ref="C0001",
                    paper_ref=contradiction_paper_id,
                    evidence_refs=["E0002"],
                    relation_to_candidate="contradicts",
                    component_relations=ComponentRelations(
                        contradicts=["E0002"]
                    ),
                    strength="high",
                    within_paper_consistency="consistent",
                    assessment_note=(
                        "The complete contradiction-paper evidence contradicts."
                    ),
                )
            ),
        ],
        TaskType.ASSESS_CLAIM: [
            _response(
                ClaimAssessmentProposal(
                    claim_ref="C0001",
                    aggregate_strength="high",
                    evidence_sufficiency="sufficient",
                    decision="NARROW",
                    rejection_basis=None,
                    support_summary="The low-temperature fictional paper supports.",
                    contradiction_summary=(
                        "The high-temperature fictional paper contradicts."
                    ),
                    qualification_summary="Limit the claim to the tested window.",
                    reason="Opposite exact results require a bounded claim.",
                )
            )
        ],
        TaskType.REVISE_CLAIM: [
            _response(
                RevisedClaimProposal(claim_ref="C0001", final_claim=FINAL_CLAIM)
            )
        ],
        TaskType.VALIDATE_FINAL_CLAIM: [
            _response(
                FinalClaimValidationProposal(
                    claim_ref="C0001",
                    final_claim=FINAL_CLAIM,
                    status="VALID",
                    paper_relations=[
                        FinalPaperRelationProposal(
                            claim_paper_evidence_ref=support_cpe_id,
                            paper_ref=support_paper_id,
                            relation_to_final_claim="supports",
                        ),
                        FinalPaperRelationProposal(
                            claim_paper_evidence_ref=contradiction_cpe_id,
                            paper_ref=contradiction_paper_id,
                            relation_to_final_claim="qualifies",
                        ),
                    ],
                    scope_check="pass",
                    certainty_check="pass",
                    causal_language_check="pass",
                    numerical_claim_check="not_applicable",
                    notes="Validated against both complete fictional CPE records.",
                )
            )
        ],
        TaskType.GENERATE_PROPOSITIONS: [
            _response(
                PropositionProposalBundle(
                    propositions=[
                        PropositionProposal(
                            local_ref="bounded_result",
                            text=FINAL_CLAIM,
                            content_class="ScientificClaim",
                            claim_refs=["C0001"],
                            citation_bindings=[
                                {
                                    "paper_ref": support_paper_id,
                                    "claim_ref": "C0001",
                                    "claim_paper_evidence_ref": support_cpe_id,
                                },
                                {
                                    "paper_ref": contradiction_paper_id,
                                    "claim_ref": "C0001",
                                    "claim_paper_evidence_ref": (
                                        contradiction_cpe_id
                                    ),
                                },
                            ],
                            corpus_fact_refs=[],
                            process_fact_refs=[],
                        )
                    ]
                )
            )
        ],
        TaskType.AUDIT_PROPOSITION: [
            _response(
                SemanticAuditProposal(
                    target_ref="bounded_result",
                    class_verdict="CORRECT",
                    provenance_verdict="ENTAILED",
                    reason="The bounded proposition is licensed by both CPEs.",
                    referenced_claim_refs=["C0001"],
                    referenced_corpus_fact_refs=[],
                    referenced_process_fact_refs=[],
                )
            )
        ],
        TaskType.RENDER_PROSE: [
            _response(
                RenderedSentenceProposalBundle(
                    sentences=[
                        RenderedSentenceProposal(
                            local_ref="bounded_sentence",
                            text=rendered_text,
                            source_proposition_refs=["PR0001"],
                        )
                    ]
                )
            )
        ],
        TaskType.AUDIT_RENDERED_SENTENCE: [
            _response(
                RenderedSentenceAuditProposal(
                    sentence_ref="bounded_sentence",
                    verdict="ENTAILED",
                    reason="The sentence exactly renders the audited proposition.",
                )
            )
        ],
    }
    assert set(scripts) == set(TaskType)
    return {
        task_type: MockEngine(
            responses,
            name=f"positive-e2e-{task_type.value}",
            version="1",
        )
        for task_type, responses in scripts.items()
    }


def _run_once(config: LibraryConfig, tmp_path: Path, ordinal: int) -> _RunWitness:
    run_root = tmp_path / f"run-{ordinal}"
    run_root.mkdir()
    review_root = run_root / "review"
    public_root = run_root / "public"
    public_root.mkdir()
    imported = import_selected_corpus(
        review_root,
        _selection(config),
        config,
        public_repository_root=public_root,
    )

    resources = run_root / "discovery"
    resources.mkdir()
    document_texts = (
        "Residual stress is a defined fictional outcome.",
        "Thermal-window direction is controversial in this fictional review.",
    )
    documents = tuple(
        resources / f"source-{number}.md" for number in range(1, 3)
    )
    for path, text in zip(documents, document_texts, strict=True):
        path.write_text(text, encoding="utf-8")

    runtime = ProjectRuntime(
        review_root,
        config=RuntimeConfig(
            max_fallback_engines=0,
            technical_attempts_per_engine=1,
        ),
        allowed_source_roots=(review_root, resources),
    )
    generation, snapshot, _ = runtime.store.load_current()
    assert generation == imported.generation
    paper_texts = {
        paper.paper_id: (review_root / paper.raw_md_path).read_text(
            encoding="utf-8"
        )
        for paper in snapshot.papers
    }
    budget = FivePaperPilotBudget()
    planning_retrieval = UnifiedRetrievalCoordinator.from_generation(
        review_root, generation=generation
    )
    support_hits, _ = planning_retrieval.retrieve(
        SUPPORT_QUERY,
        "Q-C0001-SUP-01",
        RetrievalIntent.SUPPORT,
        top_k=budget.retrieval_top_k_per_backend,
    )
    contradiction_hits, _ = planning_retrieval.retrieve(
        CONTRADICTION_QUERY,
        "Q-C0001-CON-01",
        RetrievalIntent.CONTRADICTION,
        top_k=budget.retrieval_top_k_per_backend,
    )
    assert len(support_hits) == len(contradiction_hits) == 1
    support_hit = support_hits[0]
    contradiction_hit = contradiction_hits[0]
    assert support_hit.source_text == SUPPORT_SPAN
    assert contradiction_hit.source_text == CONTRADICTION_SPAN
    assert support_hit.paper_id != contradiction_hit.paper_id

    engines = _engines(
        document_texts=document_texts,
        paper_texts=paper_texts,
        support_candidate_key=support_hit.candidate_key,
        support_paper_id=support_hit.paper_id,
        contradiction_candidate_key=contradiction_hit.candidate_key,
        contradiction_paper_id=contradiction_hit.paper_id,
    )
    manifest = build_synthetic_pilot_run_manifest(
        runtime,
        run_id="RUN-positive-evidence-e2e",
        topic=TOPIC,
        discovery_document_paths=documents,
        engines=engines,
        budget=budget,
        created_at="2030-01-01T00:00:00+00:00",
    )
    setup = register_synthetic_pilot_setup(
        review_root,
        manifest,
        public_repository_root=public_root,
    )
    controller = SyntheticPilotController(
        runtime, manifest, setup.registration, engines
    )

    controller.record_prerequisites()
    parsed = controller.parse_deep_research(
        topic=TOPIC, document_paths=documents
    )
    assert isinstance(parsed.artifact_reference, DiscoveryArtifactReference)
    challenged = controller.corpus_challenger(
        topic=TOPIC,
        discovery_artifact=parsed.artifact_reference,
    )
    assert isinstance(challenged.artifact_reference, DiscoveryArtifactReference)
    candidate = controller.generate_candidate_claims(
        topic=TOPIC,
        discovery_artifact=parsed.artifact_reference,
        challenge_artifact=challenged.artifact_reference,
    )
    assert candidate.runtime_result is not None
    assert candidate.runtime_result.allocated_ids == {
        "combined_theme": "T0001",
        "combined_claim": "C0001",
    }
    controller.generate_retrieval_queries(claim_ids=["C0001"])

    retrieval_generation, retrieval_snapshot, _ = runtime.store.load_current()
    retrieval = UnifiedRetrievalCoordinator.from_generation(
        review_root, generation=retrieval_generation
    )
    ledgers: list[RetrievalLedger] = []
    for query in sorted(
        retrieval_snapshot.retrieval_queries, key=lambda item: item.query_id
    ):
        _, ledger = retrieval.retrieve(
            query.query_text,
            query.query_id,
            query.intent,
            top_k=budget.retrieval_top_k_per_backend,
        )
        ledgers.append(ledger)
    ledgers_by_intent = {
        next(
            query.intent
            for query in retrieval_snapshot.retrieval_queries
            if query.query_id == ledger.query_id
        ): ledger
        for ledger in ledgers
    }
    support_ledger = ledgers_by_intent[RetrievalIntent.SUPPORT]
    contradiction_ledger = ledgers_by_intent[RetrievalIntent.CONTRADICTION]
    assert support_ledger.selected_candidate_keys == [support_hit.candidate_key]
    assert contradiction_ledger.selected_candidate_keys == [
        contradiction_hit.candidate_key
    ]
    assert (
        ledgers_by_intent[RetrievalIntent.BOUNDARY].selected_candidate_keys
        == []
    )
    assert (
        ledgers_by_intent[RetrievalIntent.ALTERNATIVE].selected_candidate_keys
        == []
    )
    controller.record_retrieval(ledgers)

    support = controller.assess_evidence(support_ledger)
    contradiction = controller.assess_evidence(contradiction_ledger)
    assert support.runtime_result is not None
    assert contradiction.runtime_result is not None
    assert support.runtime_result.allocated_ids == {
        f"span:{support_hit.candidate_key}": "R0001",
        f"evidence:{support_hit.candidate_key}": "E0001",
    }
    assert contradiction.runtime_result.allocated_ids == {
        f"span:{contradiction_hit.candidate_key}": "R0002",
        f"evidence:{contradiction_hit.candidate_key}": "E0002",
    }

    support_cpe_id = f"CPE-C0001-{support_hit.paper_id}"
    contradiction_cpe_id = f"CPE-C0001-{contradiction_hit.paper_id}"
    controller.aggregate_paper_evidence(
        claim_id="C0001",
        paper_id=support_hit.paper_id,
        evidence_ids=["E0001"],
    )
    controller.aggregate_paper_evidence(
        claim_id="C0001",
        paper_id=contradiction_hit.paper_id,
        evidence_ids=["E0002"],
    )
    controller.assess_claim(
        claim_id="C0001",
        claim_paper_evidence_ids=sorted(
            (support_cpe_id, contradiction_cpe_id)
        ),
    )
    revised = controller.revise_claim(claim_id="C0001")
    assert isinstance(revised.artifact_reference, DraftArtifactReference)
    controller.validate_final_claim(
        claim_id="C0001", revised_claim=revised.artifact_reference
    )

    proposition = controller.generate_propositions(
        claim_packet_ids=["C0001"],
        corpus_fact_ids=[],
        process_fact_ids=[],
    )
    assert isinstance(proposition.artifact_reference, DraftArtifactReference)
    audited_proposition = controller.audit_proposition(
        draft=proposition.artifact_reference,
        local_ref="bounded_result",
    )
    assert audited_proposition.runtime_result is not None
    proposition_id = audited_proposition.runtime_result.allocated_ids[
        "bounded_result"
    ]
    rendered = controller.render_prose(proposition_ids=[proposition_id])
    assert isinstance(rendered.artifact_reference, DraftArtifactReference)
    audited_sentence = controller.audit_rendered_sentence(
        draft=rendered.artifact_reference,
        local_ref="bounded_sentence",
    )
    assert audited_sentence.runtime_result is not None
    assert audited_sentence.runtime_result.allocated_ids["bounded_sentence"] == (
        "RS0001"
    )

    assembled = controller.exact_assembly()
    validated = controller.validation_report()
    assert assembled.body is not None and assembled.assembly is not None
    assert validated.body == assembled.body
    assert validated.assembly == assembled.assembly
    assert validated.validation is not None
    cited_papers = sorted((support_hit.paper_id, contradiction_hit.paper_id))
    expected_sentence = f"{FINAL_CLAIM} [@{' @'.join(cited_papers)}]"
    assert assembled.body == (expected_sentence + "\n").encode("utf-8")

    report = validated.validation.report
    assert report.structural_validation is ValidationStatus.PASSED
    assert report.locator_verification is ValidationStatus.PASSED
    assert report.semantic_audits_executed is ValidationStatus.PASSED
    assert report.citation_authorization is ValidationStatus.PASSED
    assert report.exact_assembly is ValidationStatus.PASSED
    assert report.artifact_integrity is ValidationStatus.PASSED
    assert report.publication_eligible is False

    final_generation, final_snapshot, _ = runtime.store.load_current()
    final_snapshot.validate_repository()
    paper_by_id = {paper.paper_id: paper for paper in final_snapshot.papers}
    span_by_id = {span.span_id: span for span in final_snapshot.retrieved_spans}
    assert set(span_by_id) == {"R0001", "R0002"}
    for span in span_by_id.values():
        raw_text = (
            review_root / paper_by_id[span.paper_id].raw_md_path
        ).read_text(encoding="utf-8")
        locator = span.locator
        observed = raw_text[locator.start_offset : locator.end_offset]
        assert observed == span.source_text
        assert locator.source_span_hash == hash_text(observed)
    assert span_by_id["R0001"].source_text == SUPPORT_SPAN
    assert span_by_id["R0002"].source_text == CONTRADICTION_SPAN

    evidence_by_id = {
        evidence.evidence_id: evidence for evidence in final_snapshot.evidence_records
    }
    assert evidence_by_id["E0001"].retrieved_span_id == "R0001"
    assert evidence_by_id["E0001"].relation_to_candidate.value == "supports"
    assert evidence_by_id["E0002"].retrieved_span_id == "R0002"
    assert evidence_by_id["E0002"].relation_to_candidate.value == "contradicts"
    cpe_by_id = {
        cpe.claim_paper_evidence_id: cpe
        for cpe in final_snapshot.claim_paper_evidence
    }
    assert cpe_by_id[support_cpe_id].evidence_ids == ["E0001"]
    assert cpe_by_id[support_cpe_id].component_relations.supports == ["E0001"]
    assert cpe_by_id[contradiction_cpe_id].evidence_ids == ["E0002"]
    assert cpe_by_id[contradiction_cpe_id].component_relations.contradicts == [
        "E0002"
    ]

    assessment = final_snapshot.claim_assessments[0]
    assert assessment.decision.value == "NARROW"
    final = final_snapshot.final_claim_validations[0]
    assert final.status.value == "VALID"
    assert final.final_claim == FINAL_CLAIM
    assert {item.claim_paper_evidence_id for item in final.paper_relations} == {
        support_cpe_id,
        contradiction_cpe_id,
    }
    packet = final_snapshot.claim_packets[0]
    assert set(packet.claim_paper_evidence_ids) == {
        support_cpe_id,
        contradiction_cpe_id,
    }
    canonical_proposition = final_snapshot.proposition_records[0]
    assert canonical_proposition.claim_ids == ["C0001"]
    assert {
        (
            binding.paper_id,
            binding.claim_id,
            binding.claim_paper_evidence_id,
        )
        for binding in canonical_proposition.citation_bindings
    } == {
        (support_hit.paper_id, "C0001", support_cpe_id),
        (contradiction_hit.paper_id, "C0001", contradiction_cpe_id),
    }
    assert final_snapshot.semantic_audits[0].provenance_verdict.value == "ENTAILED"
    assert final_snapshot.rendered_sentences[0].text == expected_sentence
    assert final_snapshot.rendered_sentence_audits[0].verdict.value == "ENTAILED"

    sequence = validate_fixed_pilot_sequence(review_root, validated.head)
    events = load_fixed_pilot_sequence(review_root, validated.head)
    assert sequence.closure_complete
    assert sequence.event_count == len(events) == 19
    assert sequence.semantic_task_count == 15
    assert all(
        event.stage_record.status is PilotStageStatus.COMPLETED for event in events
    )
    task_counts = Counter(
        event.task_provenance.task_type
        for event in events
        if event.task_provenance is not None
    )
    assert task_counts == Counter(
        {
            **{task_type: 1 for task_type in TaskType},
            TaskType.ASSESS_EVIDENCE: 2,
            TaskType.AGGREGATE_PAPER_EVIDENCE: 2,
        }
    )
    assert {
        task_type: engine.calls for task_type, engine in engines.items()
    } == dict(task_counts)

    packet_parent = tmp_path / "packets"
    packet_parent.mkdir(exist_ok=True)
    packet_dir = packet_parent / f"positive-{ordinal}"
    packet_manifest = write_synthetic_pilot_packet(
        review_root,
        packet_dir,
        public_repository_root=public_root,
        registration=controller.registration,
        journal_head=validated.head,
        body=validated.body,
        assembly=validated.assembly,
        validation=validated.validation,
    )
    assert packet_manifest.source_generation == final_generation
    assert packet_manifest.accepted_receipt_count == 15
    assert len(packet_manifest.corpus_sources) == 5
    assert len(packet_manifest.journal) == 19
    assert verify_synthetic_pilot_packet(packet_dir) == packet_manifest

    detached_review = run_root / "detached-review"
    review_root.rename(detached_review)
    assert verify_synthetic_pilot_packet(packet_dir) == packet_manifest
    return _RunWitness(
        packet_dir=packet_dir,
        repository_hash=final_snapshot.canonical_hash(),
        section=assembled.body,
    )


def test_positive_evidence_controller_closes_offline_and_reproduces(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
) -> None:
    config = _five_paper_library(synthetic_library)

    first = _run_once(config, tmp_path, 1)
    second = _run_once(config, tmp_path, 2)
    comparison = compare_synthetic_pilot_reproductions(
        (first.packet_dir, second.packet_dir)
    )

    assert first.packet_dir != second.packet_dir
    assert first.repository_hash == second.repository_hash
    assert first.section == second.section
    assert comparison.packet_count == 2
    assert comparison.differences == ()
    assert comparison.publication_eligible is False
    assert comparison.human_review == "NOT_PERFORMED"
    assert {item.subject for item in comparison.agreements} >= {
        "repository.retrieved_spans",
        "repository.evidence_records",
        "repository.claim_paper_evidence",
        "repository.claim_packets",
        "repository.proposition_records",
        "assembled_section",
        "validation.locator_verification",
        "validation.citation_authorization",
    }
