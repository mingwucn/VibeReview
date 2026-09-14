"""Adversarial evidence-closure regression (goal.md §38, Milestone 3).

Each test attempts to omit canonical evidence of one of the five frozen
``EvidenceRelation`` categories at one of the four closure transitions:

1. retrieved span -> EvidenceRecord (coupled ASSESS_EVIDENCE task path);
2. EvidenceRecord -> ClaimPaperEvidence (AGGREGATE_PAPER_EVIDENCE);
3. ClaimPaperEvidence -> FinalClaimValidation (VALIDATE_FINAL_CLAIM);
4. FinalClaimValidation -> ClaimPacket (deterministic packet derivation).

The module also probes the frozen repository validator directly with
hand-built snapshots.  The normative contract is the Milestone 3 section of
``docs/operations/mylib_operational_review.md``.  Negative and uncertain
categories are canonical scientific state: these tests assert that omission
is rejected, never that the categories themselves are rejected.

The module lives under ``tests/library/`` because transition 1 requires the
``synthetic_library`` pinned-Git fixture from ``tests/library/conftest.py``;
the claim-side transitions reuse the root ``bundle_factory`` fixture.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from vibereview.enums import AggregateRelation, EvidenceRelation, RetrievalIntent
from vibereview.errors import RepositoryValidationError
from vibereview.ids import candidate_claim_hash
from vibereview.library.evidence_task import run_assess_evidence_task
from vibereview.library.git_source import PinnedGitSource
from vibereview.library.inventory import build_library_inventory
from vibereview.library.models import (
    CorpusSelectionDocument,
    CorpusSelectionManifest,
    DocumentKind,
    LibraryConfig,
)
from vibereview.library.retrieval import RetrievalLedger, UnifiedRetrievalCoordinator
from vibereview.library.selection import import_selected_corpus
from vibereview.models import (
    CandidateClaim,
    ClaimPacket,
    ComponentRelations,
    FinalClaimValidation,
    FinalPaperRelation,
    RetrievalQuery,
    ThemeRecord,
)
from vibereview.runtime import (
    AggregatePaperEvidenceInvocation,
    AssessClaimInvocation,
    AttemptOutcome,
    MockEngine,
    MockResponse,
    ProjectRuntime,
    RepositorySnapshot,
    TaskType,
    ValidateFinalClaimInvocation,
)
from vibereview.runtime.dto import (
    AssessEvidenceProposalBundle,
    ClaimPaperEvidenceProposal,
    EvidenceAssessmentProposal,
    EvidenceCandidateDecisionProposal,
    FinalClaimValidationProposal,
    FinalPaperRelationProposal,
)
from vibereview.runtime.registry import IdKind
from vibereview.runtime.repository import GenerationStore, PromotionPayload


EVIDENCE_CATEGORIES = tuple(relation.value for relation in EvidenceRelation)
_NON_SUPPORT_CATEGORIES = tuple(
    category for category in EVIDENCE_CATEGORIES if category != "supports"
)
_SUBSTANTIVE_CATEGORIES = frozenset({"supports", "contradicts", "qualifies"})
_DOWNSTREAM_COLLECTIONS = (
    "claim_assessments",
    "final_claim_validations",
    "claim_packets",
    "proposition_records",
    "semantic_audits",
    "rendered_sentences",
    "rendered_sentence_audits",
)


def _components(mapping: dict[str, list[str]]) -> ComponentRelations:
    values = {category: [] for category in EVIDENCE_CATEGORIES}
    values.update(mapping)
    return ComponentRelations(**values)


def _aggregate_relation(categories: list[str]) -> str:
    substantive = [
        category for category in categories if category in _SUBSTANTIVE_CATEGORIES
    ]
    if len(set(substantive)) >= 2:
        return "mixed"
    if substantive:
        return substantive[0]
    return categories[0]


def _snapshot(bundle: dict[str, list[object]]) -> RepositorySnapshot:
    value = RepositorySnapshot.model_validate(bundle)
    value.validate_repository()
    return value


def _runtime(tmp_path: Path, snapshot: RepositorySnapshot) -> ProjectRuntime:
    return ProjectRuntime.create(
        tmp_path / "project",
        project_name="synthetic-evidence-closure-regression",
        initial_snapshot=snapshot,
    )


# --- transition 1 fixture: two selected retrieval candidates on two papers ---


def _selection(config: LibraryConfig) -> CorpusSelectionManifest:
    inventory, _ = build_library_inventory(PinnedGitSource.open(config), config)
    return CorpusSelectionManifest(
        library_id=config.library_id,
        source_commit=config.expected_commit,
        documents=[
            CorpusSelectionDocument(
                source_relative_path=item.source_relative_path,
                content_sha256=item.content_sha256,
                decision="include",
                role="synthetic_test",
                accepted_by="synthetic-fixture",
                accepted_at="2030-01-01T00:00:00Z",
                reason="evidence-closure regression fixture",
            )
            for item in inventory
            if item.document_kind is DocumentKind.CANDIDATE_PAPER_MARKDOWN
        ],
    )


@dataclass(frozen=True)
class TwoCandidateCase:
    review_root: Path
    runtime: ProjectRuntime
    ledger: RetrievalLedger
    claim_id: str


@pytest.fixture
def two_candidate_case(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
) -> TwoCandidateCase:
    _, config, _ = synthetic_library
    review_root = tmp_path / "review"
    import_selected_corpus(
        review_root,
        _selection(config),
        config,
        public_repository_root=tmp_path / "public-repository",
    )
    store = GenerationStore(review_root)
    base_generation, _, _ = store.load_current()

    def add_query(snapshot, registry):
        theme_ids, registry = registry.allocate(IdKind.THEME)
        claim_ids, registry = registry.allocate(IdKind.CLAIM)
        query_id, registry = registry.allocate_query(
            claim_ids[0], RetrievalIntent.SUPPORT
        )
        theme = ThemeRecord(
            theme_id=theme_ids[0],
            title="Synthetic closure regression",
            description="Fictitious evidence-closure regression fixture.",
            origin="human",
            parent_theme_id=None,
        )
        claim = CandidateClaim(
            claim_id=claim_ids[0],
            theme_id=theme.theme_id,
            candidate_claim="A synthetic lattice signal changes under load.",
            origin="human",
            origin_refs=[],
        )
        query = RetrievalQuery(
            query_id=query_id,
            claim_id=claim.claim_id,
            candidate_claim_hash=candidate_claim_hash(claim.candidate_claim),
            intent=RetrievalIntent.SUPPORT,
            query_text="Alpha lattice overflow",
        )
        return PromotionPayload(
            snapshot.model_copy(
                update={
                    "themes": snapshot.themes + (theme,),
                    "candidate_claims": snapshot.candidate_claims + (claim,),
                    "retrieval_queries": snapshot.retrieval_queries + (query,),
                }
            ),
            registry,
            {},
        )

    query_commit = store.commit(
        base_generation=base_generation,
        dependencies={},
        promotion=add_query,
    )
    snapshot, _ = store.load_generation(query_commit.generation)
    query = snapshot.retrieval_queries[0]
    candidates, ledger = UnifiedRetrievalCoordinator.from_generation(
        review_root, generation=query_commit.generation
    ).retrieve(query.query_text, query.query_id, query.intent, top_k=2)
    assert len(candidates) == 2
    assert len(ledger.selected_candidate_keys) == 2
    runtime = ProjectRuntime(review_root, allowed_source_roots=(review_root,))
    return TwoCandidateCase(
        review_root=review_root,
        runtime=runtime,
        ledger=ledger,
        claim_id=query.claim_id,
    )


def _assessed_decision(
    candidate_ref: str, relation: str
) -> EvidenceCandidateDecisionProposal:
    return EvidenceCandidateDecisionProposal(
        candidate_ref=candidate_ref,
        status="assessed",
        evidence=EvidenceAssessmentProposal(
            relation_to_candidate=EvidenceRelation(relation),
            evidence_summary=(
                f"Fictitious {relation} assessment of the synthetic source."
            ),
            quality={
                "directness": "direct",
                "methodological_relevance": "moderate",
                "strength": "low",
                "assessability": "partial",
                "limitations": ["Synthetic fixture only."],
            },
            assessment_note="Synthetic evidence-closure regression assessment.",
        ),
    )


# --- claim-side snapshot builders (root bundle_factory patterns) ---


def _two_evidence_bundle(
    bundle_factory, category: str
) -> dict[str, list[object]]:
    """Two assessed EvidenceRecords for C0001/P0001; nothing downstream."""

    bundle = bundle_factory()
    span = bundle["retrieved_spans"][0]
    second_span = span.model_copy(
        update={
            "span_id": "R0002",
            "locator": span.locator.model_copy(
                update={
                    "start_offset": 30,
                    "end_offset": 46,
                    "source_span_hash": "sha256:" + "d" * 64,
                }
            ),
            "source_text": "Stress also fell",
        }
    )
    second_disposition = bundle["retrieval_dispositions"][0].model_copy(
        update={"span_id": "R0002"}
    )
    second_evidence = bundle["evidence_records"][0].model_copy(
        update={
            "evidence_id": "E0002",
            "retrieved_span_id": "R0002",
            "relation_to_candidate": EvidenceRelation(category),
            "evidence_summary": (
                f"A second fictitious measurement classified {category}."
            ),
        }
    )
    bundle["retrieved_spans"].append(second_span)
    bundle["retrieval_dispositions"].append(second_disposition)
    bundle["evidence_records"].append(second_evidence)
    for collection in ("claim_paper_evidence", *_DOWNSTREAM_COLLECTIONS):
        bundle[collection] = []
    return bundle


def _two_cpe_bundle(bundle_factory, category: str) -> dict[str, list[object]]:
    """CPE-C0001-P0001 (supports) plus CPE-C0001-P0002 of `category`."""

    bundle = bundle_factory()
    paper = bundle["papers"][0]
    second_paper = paper.model_copy(
        update={
            "paper_id": "P0002",
            "title": "Independent synthetic replication",
            "doi": "10.1234/SECOND",
            "identity_keys": [
                "doi:10.1234/second",
                "bib:researcher 2026 replication",
            ],
            "raw_md_path": "papers/P0002/raw.md",
            "source_hash": "sha256:" + "d" * 64,
            "raw_md_hash": "sha256:" + "e" * 64,
        }
    )
    span = bundle["retrieved_spans"][0]
    second_span = span.model_copy(
        update={
            "span_id": "R0002",
            "paper_id": "P0002",
            "locator": span.locator.model_copy(
                update={
                    "raw_md_path": "papers/P0002/raw.md",
                    "start_offset": 30,
                    "end_offset": 46,
                    "source_span_hash": "sha256:" + "f" * 64,
                }
            ),
            "source_text": "Stress also fell",
        }
    )
    second_disposition = bundle["retrieval_dispositions"][0].model_copy(
        update={"span_id": "R0002"}
    )
    second_evidence = bundle["evidence_records"][0].model_copy(
        update={
            "evidence_id": "E0002",
            "retrieved_span_id": "R0002",
            "paper_id": "P0002",
            "relation_to_candidate": EvidenceRelation(category),
            "evidence_summary": (
                f"The fictitious independent result is {category} evidence."
            ),
        }
    )
    second_cpe = bundle["claim_paper_evidence"][0].model_copy(
        update={
            "claim_paper_evidence_id": "CPE-C0001-P0002",
            "paper_id": "P0002",
            "evidence_ids": ["E0002"],
            "relation_to_candidate": AggregateRelation(category),
            "component_relations": _components({category: ["E0002"]}),
            "assessment_note": (
                f"The fictitious second publication is {category} evidence."
            ),
        }
    )
    bundle["papers"].append(second_paper)
    bundle["retrieved_spans"].append(second_span)
    bundle["retrieval_dispositions"].append(second_disposition)
    bundle["evidence_records"].append(second_evidence)
    bundle["claim_paper_evidence"].append(second_cpe)
    for collection in _DOWNSTREAM_COLLECTIONS[1:]:
        bundle[collection] = []
    return bundle


def _closed_bundle(bundle_factory, category: str) -> dict[str, list[object]]:
    """Fully closed two-CPE chain: assessment, VALID final, matching packet."""

    bundle = _two_cpe_bundle(bundle_factory, category)
    final = FinalClaimValidation(
        claim_id="C0001",
        final_claim="Synthetic final claim covering both fictitious papers.",
        status="VALID",
        paper_relations=[
            FinalPaperRelation(
                claim_paper_evidence_id="CPE-C0001-P0001",
                paper_id="P0001",
                relation_to_final_claim="supports",
            ),
            FinalPaperRelation(
                claim_paper_evidence_id="CPE-C0001-P0002",
                paper_id="P0002",
                relation_to_final_claim=AggregateRelation(category),
            ),
        ],
        scope_check="pass",
        certainty_check="pass",
        causal_language_check="pass",
        numerical_claim_check="not_applicable",
        notes="Synthetic closure regression validation.",
    )
    packet = ClaimPacket(
        claim_id="C0001",
        theme_id="T0001",
        candidate_claim=bundle["candidate_claims"][0].candidate_claim,
        final_claim=final.final_claim,
        aggregate_strength=bundle["claim_assessments"][0].aggregate_strength,
        claim_paper_evidence_ids=["CPE-C0001-P0001", "CPE-C0001-P0002"],
    )
    bundle["final_claim_validations"] = [final]
    bundle["claim_packets"] = [packet]
    return bundle


def _cpe_proposal(
    evidence_refs: list[str], category_groups: dict[str, list[str]]
) -> ClaimPaperEvidenceProposal:
    return ClaimPaperEvidenceProposal(
        claim_ref="C0001",
        paper_ref="P0001",
        evidence_refs=evidence_refs,
        relation_to_candidate=AggregateRelation(
            _aggregate_relation(
                [category for category, refs in category_groups.items() if refs]
            )
        ),
        component_relations=_components(category_groups),
        strength="high",
        within_paper_consistency="consistent",
        assessment_note="Synthetic closure regression aggregate.",
    )


def _final_proposal(
    final_claim: str, relations: list[tuple[str, str, str]]
) -> FinalClaimValidationProposal:
    return FinalClaimValidationProposal(
        claim_ref="C0001",
        final_claim=final_claim,
        status="VALID",
        paper_relations=[
            FinalPaperRelationProposal(
                claim_paper_evidence_ref=cpe_id,
                paper_ref=paper_id,
                relation_to_final_claim=AggregateRelation(relation),
            )
            for cpe_id, paper_id, relation in relations
        ],
        scope_check="pass",
        certainty_check="pass",
        causal_language_check="pass",
        numerical_claim_check="not_applicable",
        notes="Checked against the complete synthetic CPE set.",
    )


# --- transition 1: retrieved span -> EvidenceRecord ---


@pytest.mark.parametrize("omitted_category", EVIDENCE_CATEGORIES)
def test_span_to_evidence_rejects_dropped_category_decision(
    two_candidate_case: TwoCandidateCase, omitted_category: str
) -> None:
    case = two_candidate_case
    first_ref, second_ref = case.ledger.selected_candidate_keys
    source_generation = case.ledger.source_generation
    omitted = AssessEvidenceProposalBundle(
        decisions=[_assessed_decision(first_ref, "supports")]
    )
    omitting_engine = MockEngine(
        [MockResponse(proposal=omitted.model_dump(mode="json"))],
        name="closure-omission",
    )

    result = run_assess_evidence_task(case.runtime, case.ledger, [omitting_engine])

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert not result.commit_performed
    assert omitting_engine.calls == 1
    assert "ordered invocation candidate refs" in (
        result.attempt_records[0].validation_errors[0]
    )
    generation, snapshot, _ = case.runtime.store.load_current()
    assert generation == source_generation
    assert snapshot.retrieved_spans == ()
    assert snapshot.evidence_records == ()

    # Positive control: the complete decision set canonicalizes every category.
    complete = AssessEvidenceProposalBundle(
        decisions=[
            _assessed_decision(first_ref, "supports"),
            _assessed_decision(second_ref, omitted_category),
        ]
    )
    accepted = run_assess_evidence_task(
        case.runtime,
        case.ledger,
        [
            MockEngine(
                [MockResponse(proposal=complete.model_dump(mode="json"))],
                name="closure-complete",
            )
        ],
    )
    assert accepted.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    _, current, _ = case.runtime.store.load_current()
    assert [item.relation_to_candidate.value for item in current.evidence_records] == [
        "supports",
        omitted_category,
    ]
    current.validate_repository()


def test_span_to_evidence_assessed_decision_requires_evidence_payload(
    two_candidate_case: TwoCandidateCase,
) -> None:
    with pytest.raises(ValidationError, match="exactly one evidence proposal"):
        EvidenceCandidateDecisionProposal(
            candidate_ref=two_candidate_case.ledger.selected_candidate_keys[0],
            status="assessed",
        )


# --- transition 2: EvidenceRecord -> ClaimPaperEvidence ---


@pytest.mark.parametrize("category", EVIDENCE_CATEGORIES)
def test_evidence_to_cpe_invocation_cannot_drop_category(
    tmp_path: Path, bundle_factory, category: str
) -> None:
    runtime = _runtime(
        tmp_path, _snapshot(_two_evidence_bundle(bundle_factory, category))
    )
    engine = MockEngine([], name="unused")

    with pytest.raises(ValueError, match="exactly equal all current EvidenceRecord"):
        runtime.run(
            TaskType.AGGREGATE_PAPER_EVIDENCE,
            AggregatePaperEvidenceInvocation(
                claim_id="C0001", paper_id="P0001", evidence_ids=["E0001"]
            ),
            engines=[engine],
        )

    assert engine.calls == 0
    assert runtime.store.current_generation() == 0


@pytest.mark.parametrize("category", EVIDENCE_CATEGORIES)
def test_evidence_to_cpe_proposal_cannot_drop_category(
    tmp_path: Path, bundle_factory, category: str
) -> None:
    runtime = _runtime(
        tmp_path, _snapshot(_two_evidence_bundle(bundle_factory, category))
    )
    proposal = _cpe_proposal(["E0001"], {"supports": ["E0001"]})

    result = runtime.run(
        TaskType.AGGREGATE_PAPER_EVIDENCE,
        AggregatePaperEvidenceInvocation(
            claim_id="C0001", paper_id="P0001", evidence_ids=["E0001", "E0002"]
        ),
        engines=[MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])],
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert "exactly equal all current EvidenceRecord" in (
        result.attempt_records[0].validation_errors[0]
    )
    assert runtime.store.current_generation() == 0


@pytest.mark.parametrize("category", _NON_SUPPORT_CATEGORIES)
def test_evidence_to_cpe_cannot_relabel_category_as_supports(
    tmp_path: Path, bundle_factory, category: str
) -> None:
    runtime = _runtime(
        tmp_path, _snapshot(_two_evidence_bundle(bundle_factory, category))
    )
    proposal = _cpe_proposal(
        ["E0001", "E0002"], {"supports": ["E0001", "E0002"]}
    )

    result = runtime.run(
        TaskType.AGGREGATE_PAPER_EVIDENCE,
        AggregatePaperEvidenceInvocation(
            claim_id="C0001", paper_id="P0001", evidence_ids=["E0001", "E0002"]
        ),
        engines=[MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])],
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert f"EvidenceRecord relation is {category}" in (
        result.attempt_records[0].validation_errors[0]
    )
    assert runtime.store.current_generation() == 0


@pytest.mark.parametrize("category", EVIDENCE_CATEGORIES)
def test_evidence_to_cpe_complete_aggregation_preserves_category(
    tmp_path: Path, bundle_factory, category: str
) -> None:
    runtime = _runtime(
        tmp_path, _snapshot(_two_evidence_bundle(bundle_factory, category))
    )
    groups: dict[str, list[str]] = {"supports": ["E0001"]}
    groups.setdefault(category, []).append("E0002")
    proposal = _cpe_proposal(["E0001", "E0002"], groups)

    result = runtime.run(
        TaskType.AGGREGATE_PAPER_EVIDENCE,
        AggregatePaperEvidenceInvocation(
            claim_id="C0001", paper_id="P0001", evidence_ids=["E0001", "E0002"]
        ),
        engines=[MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])],
    )

    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    _, current, _ = runtime.store.load_current()
    assert len(current.claim_paper_evidence) == 1
    aggregate = current.claim_paper_evidence[0]
    assert set(aggregate.evidence_ids) == {"E0001", "E0002"}
    grouped = aggregate.component_relations.grouped()
    assert grouped[EvidenceRelation(category)] == groups[category]
    current.validate_repository()


# --- transition 3: ClaimPaperEvidence -> FinalClaimValidation ---


@pytest.mark.parametrize("category", EVIDENCE_CATEGORIES)
def test_assess_claim_invocation_cannot_drop_category_cpe(
    tmp_path: Path, bundle_factory, category: str
) -> None:
    runtime = _runtime(tmp_path, _snapshot(_two_cpe_bundle(bundle_factory, category)))
    engine = MockEngine([], name="unused")

    with pytest.raises(ValueError, match="exactly equal all current CPE"):
        runtime.run(
            TaskType.ASSESS_CLAIM,
            AssessClaimInvocation(
                claim_id="C0001",
                claim_paper_evidence_ids=["CPE-C0001-P0001"],
            ),
            engines=[engine],
        )

    assert engine.calls == 0
    assert runtime.store.current_generation() == 0


@pytest.mark.parametrize("omitted_category", EVIDENCE_CATEGORIES)
def test_cpe_to_final_validation_cannot_drop_category(
    tmp_path: Path, bundle_factory, omitted_category: str
) -> None:
    runtime = _runtime(
        tmp_path, _snapshot(_two_cpe_bundle(bundle_factory, omitted_category))
    )
    wording = "A bounded synthetic final claim."
    proposal = _final_proposal(
        wording, [("CPE-C0001-P0001", "P0001", "supports")]
    )

    result = runtime.run(
        TaskType.VALIDATE_FINAL_CLAIM,
        ValidateFinalClaimInvocation(claim_id="C0001", proposed_final_claim=wording),
        engines=[MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])],
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert "exactly cover every current CPE" in (
        result.attempt_records[0].validation_errors[0]
    )
    assert runtime.store.current_generation() == 0


# --- transition 4: FinalClaimValidation -> ClaimPacket ---


@pytest.mark.parametrize("category", EVIDENCE_CATEGORIES)
def test_final_validation_and_packet_retain_category_cpe(
    tmp_path: Path, bundle_factory, category: str
) -> None:
    runtime = _runtime(tmp_path, _snapshot(_two_cpe_bundle(bundle_factory, category)))
    wording = "Synthetic final claim covering both fictitious papers."
    proposal = _final_proposal(
        wording,
        [
            ("CPE-C0001-P0001", "P0001", "supports"),
            ("CPE-C0001-P0002", "P0002", category),
        ],
    )

    result = runtime.run(
        TaskType.VALIDATE_FINAL_CLAIM,
        ValidateFinalClaimInvocation(claim_id="C0001", proposed_final_claim=wording),
        engines=[MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])],
    )

    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    _, current, _ = runtime.store.load_current()
    assert len(current.final_claim_validations) == 1
    assert len(current.claim_packets) == 1
    packet = current.claim_packets[0]
    # The packet is derived from the complete snapshot CPE set; a proposal
    # cannot omit the category CPE from it.
    assert packet.claim_paper_evidence_ids == [
        "CPE-C0001-P0001",
        "CPE-C0001-P0002",
    ]
    final = current.final_claim_validations[0]
    relations = {
        relation.claim_paper_evidence_id: relation.relation_to_final_claim.value
        for relation in final.paper_relations
    }
    assert relations == {
        "CPE-C0001-P0001": "supports",
        "CPE-C0001-P0002": category,
    }
    current.validate_repository()


# --- frozen repository validator probes (hand-built snapshots) ---


@pytest.mark.parametrize("category", EVIDENCE_CATEGORIES)
def test_validator_accepts_unaggregated_category_evidence_in_intermediate_state(
    bundle_factory, category: str
) -> None:
    # Legitimate intermediate: evidence is assessed but no ClaimPaperEvidence
    # or any downstream record exists yet.  Closure is a transition invariant;
    # this state must stay canonical.
    bundle = _two_cpe_bundle(bundle_factory, category)
    bundle["claim_paper_evidence"] = []
    for collection in _DOWNSTREAM_COLLECTIONS:
        bundle[collection] = []
    _snapshot(bundle)


@pytest.mark.parametrize("category", EVIDENCE_CATEGORIES)
def test_validator_accepts_partial_aggregation_before_any_packet(
    bundle_factory, category: str
) -> None:
    # Legitimate intermediate: one paper aggregated, the category evidence for
    # the second paper not yet aggregated, and no ClaimPacket anywhere.
    bundle = _two_cpe_bundle(bundle_factory, category)
    bundle["claim_paper_evidence"] = bundle["claim_paper_evidence"][:1]
    for collection in _DOWNSTREAM_COLLECTIONS:
        bundle[collection] = []
    _snapshot(bundle)


@pytest.mark.parametrize("category", EVIDENCE_CATEGORIES)
def test_validator_accepts_closed_chain_with_category_cpe(
    bundle_factory, category: str
) -> None:
    _snapshot(_closed_bundle(bundle_factory, category))


@pytest.mark.parametrize("omitted_category", EVIDENCE_CATEGORIES)
def test_validator_rejects_packet_that_drops_category_cpe_covered_by_final(
    bundle_factory, omitted_category: str
) -> None:
    bundle = _closed_bundle(bundle_factory, omitted_category)
    packet = bundle["claim_packets"][0]
    bundle["claim_packets"] = [
        packet.model_copy(
            update={"claim_paper_evidence_ids": ["CPE-C0001-P0001"]}
        )
    ]

    with pytest.raises(RepositoryValidationError) as exc_info:
        _snapshot(bundle)
    assert "CPE_SET_CONTINUITY_MISMATCH" in exc_info.value.report.codes()


@pytest.mark.parametrize("omitted_category", EVIDENCE_CATEGORIES)
def test_validator_rejects_closed_chain_whose_final_omits_category_cpe(
    bundle_factory, omitted_category: str
) -> None:
    # Demonstrated defect fixed in Milestone 3: the VALID final validation and
    # the ClaimPacket agree with each other but both omit the category CPE,
    # making canonical evidence invisible to the validated chain.
    bundle = _closed_bundle(bundle_factory, omitted_category)
    final = bundle["final_claim_validations"][0]
    bundle["final_claim_validations"] = [
        final.model_copy(update={"paper_relations": final.paper_relations[:1]})
    ]
    packet = bundle["claim_packets"][0]
    bundle["claim_packets"] = [
        packet.model_copy(
            update={"claim_paper_evidence_ids": ["CPE-C0001-P0001"]}
        )
    ]

    with pytest.raises(RepositoryValidationError) as exc_info:
        _snapshot(bundle)
    assert "FINAL_CPE_COVERAGE_MISMATCH" in exc_info.value.report.codes()


@pytest.mark.parametrize("orphan_category", EVIDENCE_CATEGORIES)
def test_validator_rejects_unaggregated_category_evidence_once_packet_exists(
    bundle_factory, orphan_category: str
) -> None:
    # Same defect class: the packet-closed chain is internally consistent, but
    # an assessed EvidenceRecord of the orphan category belongs to no CPE.
    bundle = _two_cpe_bundle(bundle_factory, orphan_category)
    bundle["claim_paper_evidence"] = bundle["claim_paper_evidence"][:1]
    final = FinalClaimValidation(
        claim_id="C0001",
        final_claim="Synthetic final claim over the first fictitious paper.",
        status="VALID",
        paper_relations=[
            FinalPaperRelation(
                claim_paper_evidence_id="CPE-C0001-P0001",
                paper_id="P0001",
                relation_to_final_claim="supports",
            )
        ],
        scope_check="pass",
        certainty_check="pass",
        causal_language_check="pass",
        numerical_claim_check="not_applicable",
        notes="Synthetic closure regression validation.",
    )
    bundle["final_claim_validations"] = [final]
    bundle["claim_packets"] = [
        ClaimPacket(
            claim_id="C0001",
            theme_id="T0001",
            candidate_claim=bundle["candidate_claims"][0].candidate_claim,
            final_claim=final.final_claim,
            aggregate_strength=bundle["claim_assessments"][0].aggregate_strength,
            claim_paper_evidence_ids=["CPE-C0001-P0001"],
        )
    ]

    with pytest.raises(RepositoryValidationError) as exc_info:
        _snapshot(bundle)
    assert "UNAGGREGATED_EVIDENCE_AFTER_PACKET" in exc_info.value.report.codes()
