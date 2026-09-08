from __future__ import annotations

from copy import deepcopy

import pytest

from vibereview.ids import candidate_claim_hash
from vibereview.models import (
    CandidateClaim,
    CitationBinding,
    ClaimAssessment,
    ClaimPacket,
    ClaimPaperEvidence,
    ComponentRelations,
    CorpusFact,
    CorpusFactDerivation,
    EvidenceQuality,
    EvidenceRecord,
    FinalClaimValidation,
    FinalPaperRelation,
    Paper,
    PropositionRecord,
    RenderedSentence,
    RenderedSentenceAudit,
    RetrievalDisposition,
    RetrievalMetadata,
    RetrievalQuery,
    RetrievedSpan,
    ReviewProcessFact,
    SemanticAuditResult,
    SpanLocator,
    ThemeRecord,
)


def make_valid_bundle() -> dict[str, list[object]]:
    theme = ThemeRecord(
        theme_id="T0001",
        title="Thermal management",
        description="Effects of process heating",
        origin="human",
        parent_theme_id=None,
    )
    paper = Paper(
        paper_id="P0001",
        title="Preheating and residual stress",
        authors=["A. Researcher"],
        year=2025,
        doi="10.1234/EXAMPLE",
        journal="Journal of Examples",
        identity_keys=["doi:10.1234/example", "bib:researcher 2025 preheating"],
        study_group_id=None,
        related_publications=[],
        independence_status="independent",
        raw_md_path="papers/P0001/raw.md",
        source_hash="sha256:" + "a" * 64,
        raw_md_hash="sha256:" + "b" * 64,
    )
    candidate = CandidateClaim(
        claim_id="C0001",
        theme_id="T0001",
        candidate_claim="Preheating reduces residual stress.",
        origin="human",
        origin_refs=[],
    )
    query = RetrievalQuery(
        query_id="Q-C0001-SUP-01",
        claim_id="C0001",
        candidate_claim_hash=candidate_claim_hash(candidate.candidate_claim),
        intent="support",
        query_text="preheating residual stress reduction",
    )
    span = RetrievedSpan(
        span_id="R0001",
        paper_id="P0001",
        locator=SpanLocator(
            raw_md_path="papers/P0001/raw.md",
            page=3,
            section="Results",
            start_offset=10,
            end_offset=25,
            source_span_hash="sha256:" + "c" * 64,
        ),
        source_text="Stress declined",
        retrieval=RetrievalMetadata(
            query_id="Q-C0001-SUP-01",
            intent="support",
            retrieval_score=0.9,
        ),
    )
    disposition = RetrievalDisposition(
        span_id="R0001",
        status="assessed",
        reason=None,
        canonical_span_id=None,
    )
    evidence = EvidenceRecord(
        evidence_id="E0001",
        claim_id="C0001",
        retrieved_span_id="R0001",
        paper_id="P0001",
        relation_to_candidate="supports",
        evidence_summary="The measured stress declined after preheating.",
        quality=EvidenceQuality(
            directness="direct",
            methodological_relevance="high",
            strength="high",
            assessability="full",
            limitations=[],
        ),
        assessment_note="Direct experimental comparison.",
    )
    cpe = ClaimPaperEvidence(
        claim_paper_evidence_id="CPE-C0001-P0001",
        claim_id="C0001",
        paper_id="P0001",
        evidence_ids=["E0001"],
        relation_to_candidate="supports",
        component_relations=ComponentRelations(supports=["E0001"]),
        strength="high",
        within_paper_consistency="consistent",
        assessment_note="Publication supports the candidate.",
    )
    assessment = ClaimAssessment(
        claim_id="C0001",
        aggregate_strength="high",
        evidence_sufficiency="sufficient",
        decision="RETAIN",
        rejection_basis=None,
        support_summary="One direct publication-level result.",
        contradiction_summary="None in the supplied corpus.",
        qualification_summary="Applies to the tested process window.",
        reason="Evidence is sufficient for retention.",
    )
    final = FinalClaimValidation(
        claim_id="C0001",
        final_claim="Preheating reduced residual stress in the tested process window.",
        status="VALID",
        paper_relations=[
            FinalPaperRelation(
                claim_paper_evidence_id="CPE-C0001-P0001",
                relation_to_final_claim="supports",
                paper_id="P0001",
            )
        ],
        scope_check="pass",
        certainty_check="pass",
        causal_language_check="pass",
        numerical_claim_check="not_applicable",
        notes="Validated against current publication evidence.",
    )
    packet = ClaimPacket(
        claim_id="C0001",
        theme_id="T0001",
        candidate_claim=candidate.candidate_claim,
        final_claim=final.final_claim,
        aggregate_strength="high",
        claim_paper_evidence_ids=["CPE-C0001-P0001"],
    )
    binding = CitationBinding(
        paper_id="P0001",
        claim_id="C0001",
        claim_paper_evidence_id="CPE-C0001-P0001",
    )
    proposition = PropositionRecord(
        proposition_id="PR0001",
        text=final.final_claim,
        content_class="ScientificClaim",
        claim_ids=["C0001"],
        citation_bindings=[binding],
        corpus_fact_ids=[],
        process_fact_ids=[],
    )
    semantic_audit = SemanticAuditResult(
        audit_id="SA0001",
        target_type="PropositionRecord",
        target_id="PR0001",
        class_verdict="CORRECT",
        provenance_verdict="ENTAILED",
        reason="The proposition matches the approved claim.",
        referenced_claim_ids=["C0001"],
        referenced_corpus_fact_ids=[],
        referenced_process_fact_ids=[],
    )
    sentence = RenderedSentence(
        sentence_id="RS0001",
        text="Preheating reduced residual stress in the tested process window.",
        source_proposition_ids=["PR0001"],
    )
    sentence_audit = RenderedSentenceAudit(
        audit_id="RSA0001",
        sentence_id="RS0001",
        verdict="ENTAILED",
        reason="The sentence is entailed by its proposition.",
    )
    corpus_fact = CorpusFact(
        corpus_fact_id="CF0001",
        text="The supplied corpus contains one paper.",
        derivation_type="registry_arithmetic",
        source_paper_ids=["P0001"],
        derivation=CorpusFactDerivation(
            task_version=None,
            model_signature=None,
            input_hash=None,
            output_hash=None,
        ),
    )
    process_fact = ReviewProcessFact(
        process_fact_id="PF0001",
        text="The project used the default validation policy.",
        source_type="project_config",
        source_key="validation.policy",
    )
    return {
        "themes": [theme],
        "papers": [paper],
        "candidate_claims": [candidate],
        "retrieval_queries": [query],
        "retrieved_spans": [span],
        "retrieval_dispositions": [disposition],
        "evidence_records": [evidence],
        "claim_paper_evidence": [cpe],
        "claim_assessments": [assessment],
        "final_claim_validations": [final],
        "claim_packets": [packet],
        "corpus_facts": [corpus_fact],
        "process_facts": [process_fact],
        "proposition_records": [proposition],
        "semantic_audits": [semantic_audit],
        "rendered_sentences": [sentence],
        "rendered_sentence_audits": [sentence_audit],
    }


@pytest.fixture
def bundle_factory():
    def factory() -> dict[str, list[object]]:
        return deepcopy(make_valid_bundle())

    return factory

