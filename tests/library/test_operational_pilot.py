from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vibereview.enums import RetrievalIntent
from vibereview.library.git_source import PinnedGitSource
from vibereview.library.inventory import build_library_inventory
from vibereview.library.models import (
    CorpusSelectionDocument,
    CorpusSelectionManifest,
    DocumentKind,
    LibraryConfig,
)
from vibereview.library.operational_pilot import (
    OPERATIONAL_PILOT_FORMAT_VERSION,
    OperationalAggregationStep,
    OperationalClaimAssessmentStep,
    OperationalEvidenceStep,
    OperationalFinalRelation,
    OperationalFinalValidationStep,
    OperationalPilotBundle,
    OperationalPilotError,
    OperationalQueryProposal,
    OperationalSpanAnchor,
    OperationalSpanDecision,
    load_operational_pilot_bundle,
    run_operational_pilot,
    save_operational_pilot_bundle,
)
from vibereview.models import EvidenceQuality
from vibereview.runtime.dto import (
    CandidateClaimProposal,
    DiscoveryProposalBundle,
    EvidenceAssessmentProposal,
    ThemeProposal,
)
from vibereview.runtime.hashing import hash_bytes
from vibereview.runtime.repository import GenerationStore


SUPPORT_SPAN = (
    "supportsynthmarker: In the fictional low-temperature window, preheating "
    "decreased residual stress."
)
CONTRADICTION_SPAN = (
    "contradictsynthmarker: In the fictional high-temperature window, "
    "preheating increased residual stress."
)
CANDIDATE_CLAIM = (
    "Preheating changes residual stress in fictional process windows."
)
FINAL_CLAIM = (
    "In the fictional low-temperature window, preheating decreased residual "
    "stress, while the high-temperature result limits broader generalization."
)

SUPPORT_PAPER = f"# Support\n\n{SUPPORT_SPAN}\n"
CONTRADICTION_PAPER = f"# Contradiction\n\n{CONTRADICTION_SPAN}\n"


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
        "Aster - 2030 - Support.md": SUPPORT_PAPER,
        "Beryl - 2030 - Contradiction.md": CONTRADICTION_PAPER,
        "Cobalt - 2030 - Neutral.md": (
            "# Neutral\n\nA third fictional observation has no retrieval marker.\n"
        ),
    }
    for name, text in papers.items():
        (repository / "papers" / name).write_text(text, encoding="utf-8")
    _git(repository, "add", "papers")
    _git(repository, "commit", "-m", "add fictional operational pilot fixture")
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
            role="synthetic_operational_pilot",
            accepted_by="fictional-operator-fixture",
            accepted_at="2030-01-01T00:00:00Z",
            reason="Exercise the deterministic operator pilot chain.",
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


def _anchor(text: str, needle: str) -> OperationalSpanAnchor:
    start = text.index(needle)
    return OperationalSpanAnchor(
        source_sha256=hash_bytes(text.encode("utf-8")),
        start_offset=start,
        end_offset=start + len(needle),
    )


def _evidence(relation: str, summary: str) -> EvidenceAssessmentProposal:
    return EvidenceAssessmentProposal(
        relation_to_candidate=relation,
        evidence_summary=summary,
        quality=EvidenceQuality(
            directness="direct",
            methodological_relevance="high",
            strength="high",
            assessability="full",
            limitations=["Fictional fixture only."],
        ),
        assessment_note="Exact fictional span assessed by the operator.",
    )


def _bundle() -> OperationalPilotBundle:
    support_hash = hash_bytes(SUPPORT_PAPER.encode("utf-8"))
    contradiction_hash = hash_bytes(CONTRADICTION_PAPER.encode("utf-8"))
    return OperationalPilotBundle(
        operator="fictional-operator",
        created_at="2030-01-01T00:00:00+00:00",
        topic="fictional thermal-window residual stress",
        discovery=DiscoveryProposalBundle(
            themes=[
                ThemeProposal(
                    local_ref="theme_window",
                    title="Synthetic thermal processing",
                    description="Fictional processing effects on residual stress.",
                    origin="human",
                )
            ],
            claims=[
                CandidateClaimProposal(
                    local_ref="claim_window",
                    theme_ref="theme_window",
                    candidate_claim=CANDIDATE_CLAIM,
                    origin="human",
                    origin_refs=[],
                )
            ],
        ),
        queries=[
            OperationalQueryProposal(
                local_ref=f"query_{intent}",
                claim=CANDIDATE_CLAIM,
                intent=intent,
                query_text=query_text,
            )
            for intent, query_text in (
                ("support", "supportsynthmarker"),
                ("contradiction", "contradictsynthmarker"),
                ("boundary", "boundarysynthmarker"),
                ("alternative", "alternativesynthmarker"),
            )
        ],
        evidence=[
            OperationalEvidenceStep(
                step=1,
                claim=CANDIDATE_CLAIM,
                query_text="supportsynthmarker",
                decisions=[
                    OperationalSpanDecision(
                        span=_anchor(SUPPORT_PAPER, SUPPORT_SPAN),
                        status="assessed",
                        evidence=_evidence(
                            "supports",
                            "The exact fictional low-temperature span supports "
                            "a decrease.",
                        ),
                    )
                ],
            ),
            OperationalEvidenceStep(
                step=2,
                claim=CANDIDATE_CLAIM,
                query_text="contradictsynthmarker",
                decisions=[
                    OperationalSpanDecision(
                        span=_anchor(CONTRADICTION_PAPER, CONTRADICTION_SPAN),
                        status="assessed",
                        evidence=_evidence(
                            "contradicts",
                            "The exact fictional high-temperature span "
                            "contradicts a universal decrease.",
                        ),
                    )
                ],
            ),
        ],
        aggregations=[
            OperationalAggregationStep(
                step=3,
                claim=CANDIDATE_CLAIM,
                paper_sha256=support_hash,
                relation_to_candidate="supports",
                strength="high",
                within_paper_consistency="consistent",
                assessment_note="The complete support-paper evidence supports.",
            ),
            OperationalAggregationStep(
                step=4,
                claim=CANDIDATE_CLAIM,
                paper_sha256=contradiction_hash,
                relation_to_candidate="contradicts",
                strength="high",
                within_paper_consistency="consistent",
                assessment_note=(
                    "The complete contradiction-paper evidence contradicts."
                ),
            ),
        ],
        claim_assessments=[
            OperationalClaimAssessmentStep(
                step=5,
                claim=CANDIDATE_CLAIM,
                aggregate_strength="high",
                evidence_sufficiency="sufficient",
                decision="RETAIN",
                support_summary="The low-temperature fictional paper supports.",
                contradiction_summary=(
                    "The high-temperature fictional paper contradicts."
                ),
                qualification_summary="Limit the claim to the tested window.",
                reason="The bounded wording already reflects both directions.",
            )
        ],
        final_validations=[
            OperationalFinalValidationStep(
                step=6,
                claim=CANDIDATE_CLAIM,
                final_claim=FINAL_CLAIM,
                status="VALID",
                paper_relations=[
                    OperationalFinalRelation(
                        paper_sha256=support_hash,
                        relation_to_final_claim="supports",
                    ),
                    OperationalFinalRelation(
                        paper_sha256=contradiction_hash,
                        relation_to_final_claim="qualifies",
                    ),
                ],
                scope_check="pass",
                certainty_check="pass",
                causal_language_check="pass",
                numerical_claim_check="not_applicable",
                notes="Validated against both complete fictional CPE records.",
            )
        ],
    )


def test_operational_pilot_closes_the_complete_chain(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
) -> None:
    config = _five_paper_library(synthetic_library)
    bundle = _bundle()
    assert bundle.format_version == OPERATIONAL_PILOT_FORMAT_VERSION
    bundle_path = tmp_path / "operator" / "proposals.json"
    bundle_path.parent.mkdir()
    save_operational_pilot_bundle(bundle, bundle_path)
    assert load_operational_pilot_bundle(bundle_path) == bundle

    result = run_operational_pilot(
        config,
        _selection(config),
        load_operational_pilot_bundle(bundle_path),
        tmp_path / "review",
        tmp_path / "public",
    )

    assert result.library_before == result.library_after
    assert result.claim_ids == {CANDIDATE_CLAIM: "C0001"}
    assert set(result.query_ids) == {
        "query_support",
        "query_contradiction",
        "query_boundary",
        "query_alternative",
    }
    assert all(
        task.outcome == "valid_scientific_result" for task in result.tasks
    )
    assert [task.label for task in result.tasks] == [
        "candidate-claims",
        "retrieval-queries",
        "evidence:Q-C0001-CON-01",
        "evidence:Q-C0001-SUP-01",
        "aggregate:C0001:P0001",
        "aggregate:C0001:P0002",
        "assess-claim:C0001",
        "validate-final:C0001",
    ]

    snapshot = result.snapshot
    snapshot.validate_repository()
    assert len(snapshot.papers) == 5
    assert len(snapshot.retrieval_queries) == 4
    assert len(snapshot.retrieved_spans) == 2
    assert len(snapshot.evidence_records) == 2
    assert len(snapshot.claim_paper_evidence) == 2
    assert len(snapshot.claim_assessments) == 1
    assert len(snapshot.final_claim_validations) == 1
    assert len(snapshot.claim_packets) == 1

    span_texts = {span.source_text for span in snapshot.retrieved_spans}
    assert span_texts == {SUPPORT_SPAN, CONTRADICTION_SPAN}
    relations = {
        evidence.relation_to_candidate.value
        for evidence in snapshot.evidence_records
    }
    assert relations == {"supports", "contradicts"}

    assessment = snapshot.claim_assessments[0]
    assert assessment.decision.value == "RETAIN"
    final = snapshot.final_claim_validations[0]
    assert final.status.value == "VALID"
    assert final.final_claim == FINAL_CLAIM
    packet = snapshot.claim_packets[0]
    assert packet.final_claim == FINAL_CLAIM
    assert set(packet.claim_paper_evidence_ids) == {
        item.claim_paper_evidence_id for item in snapshot.claim_paper_evidence
    }

    ledger_by_intent = {
        query.intent: ledger
        for query in snapshot.retrieval_queries
        for ledger in result.ledgers
        if ledger.query_id == query.query_id
    }
    assert (
        len(ledger_by_intent[RetrievalIntent.SUPPORT].selected_candidate_keys)
        == 1
    )
    assert (
        len(
            ledger_by_intent[RetrievalIntent.CONTRADICTION].selected_candidate_keys
        )
        == 1
    )
    assert (
        ledger_by_intent[RetrievalIntent.BOUNDARY].selected_candidate_keys == []
    )
    assert (
        ledger_by_intent[RetrievalIntent.ALTERNATIVE].selected_candidate_keys
        == []
    )


def _run_failing(
    config: LibraryConfig, tmp_path: Path, bundle: OperationalPilotBundle
) -> GenerationStore:
    review_root = tmp_path / "review"
    with pytest.raises(OperationalPilotError):
        run_operational_pilot(
            config,
            _selection(config),
            bundle,
            review_root,
            tmp_path / "public",
        )
    return GenerationStore(review_root)


def test_missing_evidence_decision_fails_closed_before_commit(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
) -> None:
    config = _five_paper_library(synthetic_library)
    bundle = _bundle().model_copy(
        update={
            "evidence": [
                step
                for step in _bundle().evidence
                if step.query_text != "contradictsynthmarker"
            ]
        }
    )

    store = _run_failing(config, tmp_path, bundle)

    _, snapshot, _ = store.load_current()
    assert snapshot.retrieved_spans == ()
    assert snapshot.evidence_records == ()
    assert snapshot.claim_paper_evidence == ()


def test_tampered_span_anchor_fails_closed_before_commit(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
) -> None:
    config = _five_paper_library(synthetic_library)
    bundle = _bundle()
    tampered_step = bundle.evidence[1].model_copy(
        update={
            "decisions": [
                bundle.evidence[1].decisions[0].model_copy(
                    update={
                        "span": bundle.evidence[1].decisions[0].span.model_copy(
                            update={"start_offset": 0}
                        )
                    }
                )
            ]
        }
    )
    bundle = bundle.model_copy(
        update={"evidence": [bundle.evidence[0], tampered_step]}
    )

    store = _run_failing(config, tmp_path, bundle)

    _, snapshot, _ = store.load_current()
    assert snapshot.retrieved_spans == ()
    assert snapshot.evidence_records == ()
    assert snapshot.claim_paper_evidence == ()
