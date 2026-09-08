"""Pure repository-level validators for the frozen Phase-0 object graph."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Hashable, Sequence
from typing import Any, TypeVar

from .enums import (
    AggregateRelation,
    AuditClassVerdict,
    AuditDisposition,
    AuditProvenanceVerdict,
    ClaimDecision,
    EvidenceRelation,
    FinalClaimStatus,
    PropositionContentClass,
    RetrievalDispositionStatus,
)
from .errors import (
    IssueSeverity,
    RepositoryValidationError,
    ValidationIssue,
    ValidationReport,
)
from .ids import candidate_claim_hash, parse_query_id
from .models import (
    CandidateClaim,
    ClaimAssessment,
    ClaimPacket,
    ClaimPaperEvidence,
    CorpusFact,
    EvidenceRecord,
    FinalClaimValidation,
    Paper,
    PropositionRecord,
    RenderedSentence,
    RenderedSentenceAudit,
    RetrievalDisposition,
    RetrievalQuery,
    RetrievedSpan,
    ReviewProcessFact,
    SemanticAuditResult,
    ThemeRecord,
)

T = TypeVar("T")


def _enum_value(value: object) -> str:
    """Render validated enums and low-level snapshot values consistently."""

    return str(getattr(value, "value", value))


class _Collector:
    def __init__(self) -> None:
        self.issues: list[ValidationIssue] = []

    def error(self, code: str, path: str, message: str) -> None:
        self.issues.append(ValidationIssue(IssueSeverity.ERROR, code, path, message))

    def warning(self, code: str, path: str, message: str) -> None:
        self.issues.append(ValidationIssue(IssueSeverity.WARNING, code, path, message))

    def report(self) -> ValidationReport:
        # Cross-bundle checks can discover the same issue. Keep the public report stable.
        unique = tuple(dict.fromkeys(self.issues))
        return ValidationReport(unique)

    def finish(self) -> ValidationReport:
        report = self.report()
        if not report.ok:
            raise RepositoryValidationError(report)
        return report


def _index(
    objects: Sequence[T],
    attribute: str,
    label: str,
    collector: _Collector,
) -> dict[Hashable, T]:
    result: dict[Hashable, T] = {}
    for position, obj in enumerate(objects):
        key = getattr(obj, attribute)
        if key in result:
            collector.error(
                "DUPLICATE_OBJECT_ID",
                f"{label}[{position}].{attribute}",
                f"duplicate {label} identifier {key}",
            )
        else:
            result[key] = obj
    return result


def _finish(check: Callable[[_Collector], None]) -> ValidationReport:
    collector = _Collector()
    check(collector)
    return collector.finish()


def _collect_identity_registry(papers: Sequence[Paper], collector: _Collector) -> None:
    paper_by_id = _index(papers, "paper_id", "papers", collector)
    doi_owners: dict[str, str] = {}
    source_owners: dict[str, str] = {}
    bib_owners: dict[str, str] = {}

    for position, paper in enumerate(papers):
        for related_id in paper.related_publications:
            if related_id not in paper_by_id:
                collector.error(
                    "INVALID_REFERENCE",
                    f"papers[{position}].related_publications",
                    f"related Paper {related_id} does not exist",
                )
        doi_keys = {key for key in paper.identity_keys if key.startswith("doi:")}
        if paper.doi is not None:
            doi_keys.add(paper.doi)

        for doi in sorted(doi_keys):
            owner = doi_owners.setdefault(doi, paper.paper_id)
            if owner != paper.paper_id:
                collector.error(
                    "DUPLICATE_IDENTITY_CONFLICT",
                    f"papers[{position}].identity_keys",
                    f"DOI {doi} is shared by {owner} and {paper.paper_id}",
                )

        owner = source_owners.setdefault(paper.source_hash, paper.paper_id)
        if owner != paper.paper_id:
            collector.error(
                "DUPLICATE_IDENTITY_CONFLICT",
                f"papers[{position}].source_hash",
                f"source hash {paper.source_hash} is shared by {owner} and {paper.paper_id}",
            )

        for key in sorted(item for item in paper.identity_keys if item.startswith("bib:")):
            owner = bib_owners.setdefault(key, paper.paper_id)
            if owner != paper.paper_id:
                collector.warning(
                    "POSSIBLE_DUPLICATE_WARNING",
                    f"papers[{position}].identity_keys",
                    f"weak bibliographic key {key} is shared by {owner} and {paper.paper_id}",
                )


def validate_identity_registry(papers: Sequence[Paper]) -> ValidationReport:
    return _finish(lambda collector: _collect_identity_registry(papers, collector))


def _collect_theme_hierarchy(
    themes: Sequence[ThemeRecord], collector: _Collector
) -> None:
    theme_by_id = _index(themes, "theme_id", "themes", collector)
    for position, theme in enumerate(themes):
        parent_id = theme.parent_theme_id
        if parent_id is None:
            continue
        if parent_id == theme.theme_id:
            collector.error(
                "THEME_SELF_PARENT",
                f"themes[{position}].parent_theme_id",
                f"theme {theme.theme_id} cannot parent itself",
            )
        elif parent_id not in theme_by_id:
            collector.error(
                "INVALID_REFERENCE",
                f"themes[{position}].parent_theme_id",
                f"parent theme {parent_id} does not exist",
            )

    state: dict[str, int] = {}

    def visit(theme_id: str, trail: tuple[str, ...]) -> None:
        if state.get(theme_id) == 2:
            return
        if state.get(theme_id) == 1:
            cycle = trail[trail.index(theme_id) :] + (theme_id,)
            collector.error(
                "THEME_CYCLE",
                f"themes[{theme_id}]",
                f"theme hierarchy is cyclic: {' -> '.join(cycle)}",
            )
            return
        state[theme_id] = 1
        parent_id = theme_by_id[theme_id].parent_theme_id
        if parent_id in theme_by_id and parent_id != theme_id:
            visit(parent_id, trail + (theme_id,))
        state[theme_id] = 2

    for theme_id in theme_by_id:
        if state.get(theme_id) is None:
            visit(theme_id, ())


def validate_theme_hierarchy(themes: Sequence[ThemeRecord]) -> ValidationReport:
    return _finish(lambda collector: _collect_theme_hierarchy(themes, collector))


def _collect_retrieval_bundle(
    candidate_claims: Sequence[CandidateClaim],
    retrieval_queries: Sequence[RetrievalQuery],
    retrieved_spans: Sequence[RetrievedSpan],
    retrieval_dispositions: Sequence[RetrievalDisposition],
    papers: Sequence[Paper],
    collector: _Collector,
) -> None:
    claim_by_id = _index(candidate_claims, "claim_id", "candidate_claims", collector)
    query_by_id = _index(retrieval_queries, "query_id", "retrieval_queries", collector)
    span_by_id = _index(retrieved_spans, "span_id", "retrieved_spans", collector)
    paper_by_id = _index(papers, "paper_id", "papers", collector)

    for position, query in enumerate(retrieval_queries):
        claim = claim_by_id.get(query.claim_id)
        if claim is None:
            collector.error(
                "INVALID_REFERENCE",
                f"retrieval_queries[{position}].claim_id",
                f"candidate claim {query.claim_id} does not exist",
            )
        elif query.candidate_claim_hash != candidate_claim_hash(claim.candidate_claim):
            collector.error(
                "CANDIDATE_CLAIM_MUTATED_AFTER_RETRIEVAL",
                f"retrieval_queries[{position}].candidate_claim_hash",
                f"query hash does not match current text of {query.claim_id}",
            )
        try:
            encoded_claim, encoded_intent, ordinal = parse_query_id(query.query_id)
        except ValueError:
            collector.error(
                "QUERY_ID_MISMATCH",
                f"retrieval_queries[{position}].query_id",
                f"query ID {query.query_id} does not satisfy the canonical grammar",
            )
        else:
            if encoded_claim != query.claim_id:
                collector.error(
                    "QUERY_ID_MISMATCH",
                    f"retrieval_queries[{position}].query_id",
                    "query ID claim component does not equal claim_id",
                )
            if encoded_intent != _enum_value(query.intent):
                collector.error(
                    "QUERY_ID_MISMATCH",
                    f"retrieval_queries[{position}].query_id",
                    "query ID intent code does not equal intent",
                )
            if not 1 <= ordinal <= 99:
                collector.error(
                    "QUERY_ID_MISMATCH",
                    f"retrieval_queries[{position}].query_id",
                    "query ordinal must be between 01 and 99",
                )

    for position, span in enumerate(retrieved_spans):
        query = query_by_id.get(span.retrieval.query_id)
        if query is None:
            collector.error(
                "INVALID_REFERENCE",
                f"retrieved_spans[{position}].retrieval.query_id",
                f"retrieval query {span.retrieval.query_id} does not exist",
            )
        elif span.retrieval.intent != query.intent:
            collector.error(
                "RETRIEVAL_INTENT_MISMATCH",
                f"retrieved_spans[{position}].retrieval.intent",
                f"span intent does not match query {query.query_id}",
            )
        if span.paper_id not in paper_by_id:
            collector.error(
                "INVALID_REFERENCE",
                f"retrieved_spans[{position}].paper_id",
                f"paper {span.paper_id} does not exist",
            )

    dispositions_by_span: dict[str, list[RetrievalDisposition]] = defaultdict(list)
    canonical_edges: dict[str, str] = {}
    for position, disposition in enumerate(retrieval_dispositions):
        dispositions_by_span[disposition.span_id].append(disposition)
        if disposition.span_id not in span_by_id:
            collector.error(
                "INVALID_REFERENCE",
                f"retrieval_dispositions[{position}].span_id",
                f"retrieved span {disposition.span_id} does not exist",
            )
        if (
            disposition.canonical_span_id is not None
            and disposition.canonical_span_id not in span_by_id
        ):
            collector.error(
                "INVALID_REFERENCE",
                f"retrieval_dispositions[{position}].canonical_span_id",
                f"canonical span {disposition.canonical_span_id} does not exist",
            )
        if disposition.canonical_span_id is not None:
            canonical_edges[disposition.span_id] = disposition.canonical_span_id
            if disposition.canonical_span_id == disposition.span_id:
                collector.error(
                    "CANONICAL_SPAN_SELF_REFERENCE",
                    f"retrieval_dispositions[{position}].canonical_span_id",
                    "duplicate/redundant span cannot reference itself",
                )
            source = span_by_id.get(disposition.span_id)
            target = span_by_id.get(disposition.canonical_span_id)
            if source is not None and target is not None and source.paper_id != target.paper_id:
                collector.error(
                    "CANONICAL_SPAN_CROSS_PAPER",
                    f"retrieval_dispositions[{position}].canonical_span_id",
                    "duplicate/redundant canonicalization cannot cross papers",
                )

    for span_id in span_by_id:
        count = len(dispositions_by_span[span_id])
        if count != 1:
            collector.error(
                "CARDINALITY_VIOLATION",
                f"retrieved_spans[{span_id}].disposition",
                f"retrieved span {span_id} requires exactly one disposition; found {count}",
            )

    for start in canonical_edges:
        seen: set[str] = set()
        current = start
        while current in canonical_edges:
            if current in seen:
                collector.error(
                    "CANONICAL_SPAN_CYCLE",
                    f"retrieval_dispositions[{start}].canonical_span_id",
                    "duplicate/redundant canonical-span references form a cycle",
                )
                break
            seen.add(current)
            current = canonical_edges[current]


def validate_retrieval_bundle(
    candidate_claims: Sequence[CandidateClaim],
    retrieval_queries: Sequence[RetrievalQuery],
    retrieved_spans: Sequence[RetrievedSpan],
    retrieval_dispositions: Sequence[RetrievalDisposition],
    papers: Sequence[Paper],
) -> ValidationReport:
    return _finish(
        lambda collector: _collect_retrieval_bundle(
            candidate_claims,
            retrieval_queries,
            retrieved_spans,
            retrieval_dispositions,
            papers,
            collector,
        )
    )


def _collect_evidence_bundle(
    candidate_claims: Sequence[CandidateClaim],
    retrieval_queries: Sequence[RetrievalQuery],
    retrieved_spans: Sequence[RetrievedSpan],
    retrieval_dispositions: Sequence[RetrievalDisposition],
    evidence_records: Sequence[EvidenceRecord],
    collector: _Collector,
) -> None:
    claim_by_id = _index(candidate_claims, "claim_id", "candidate_claims", collector)
    query_by_id = _index(retrieval_queries, "query_id", "retrieval_queries", collector)
    span_by_id = _index(retrieved_spans, "span_id", "retrieved_spans", collector)
    _index(evidence_records, "evidence_id", "evidence_records", collector)

    disposition_by_span: dict[str, list[RetrievalDisposition]] = defaultdict(list)
    for disposition in retrieval_dispositions:
        disposition_by_span[disposition.span_id].append(disposition)

    evidence_by_span: dict[str, list[EvidenceRecord]] = defaultdict(list)
    for position, evidence in enumerate(evidence_records):
        evidence_by_span[evidence.retrieved_span_id].append(evidence)
        if evidence.claim_id not in claim_by_id:
            collector.error(
                "INVALID_REFERENCE",
                f"evidence_records[{position}].claim_id",
                f"candidate claim {evidence.claim_id} does not exist",
            )
        span = span_by_id.get(evidence.retrieved_span_id)
        if span is None:
            collector.error(
                "INVALID_REFERENCE",
                f"evidence_records[{position}].retrieved_span_id",
                f"retrieved span {evidence.retrieved_span_id} does not exist",
            )
            continue
        query = query_by_id.get(span.retrieval.query_id)
        if query is None:
            collector.error(
                "INVALID_REFERENCE",
                f"retrieved_spans[{span.span_id}].retrieval.query_id",
                f"retrieval query {span.retrieval.query_id} does not exist",
            )
        elif evidence.claim_id != query.claim_id:
            collector.error(
                "EVIDENCE_CLAIM_MISMATCH",
                f"evidence_records[{position}].claim_id",
                f"evidence claim does not match retrieval query {query.query_id}",
            )
        if evidence.paper_id != span.paper_id:
            collector.error(
                "EVIDENCE_PAPER_MISMATCH",
                f"evidence_records[{position}].paper_id",
                f"evidence paper does not match retrieved span {span.span_id}",
            )
        dispositions = disposition_by_span.get(span.span_id, [])
        if len(dispositions) != 1 or dispositions[0].status != RetrievalDispositionStatus.ASSESSED:
            collector.error(
                "DISCARDED_SPAN_HAS_EVIDENCE",
                f"evidence_records[{position}].retrieved_span_id",
                f"span {span.span_id} is not uniquely disposed as assessed",
            )

    for span in retrieved_spans:
        dispositions = disposition_by_span.get(span.span_id, [])
        if len(dispositions) != 1:
            continue
        records = evidence_by_span.get(span.span_id, [])
        if dispositions[0].status == RetrievalDispositionStatus.ASSESSED:
            query = query_by_id.get(span.retrieval.query_id)
            relevant = [
                record
                for record in records
                if query is not None and record.claim_id == query.claim_id
            ]
            if len(relevant) != 1:
                collector.error(
                    "ASSESSED_SPAN_EVIDENCE_CARDINALITY",
                    f"retrieved_spans[{span.span_id}].evidence",
                    f"assessed span requires exactly one relevant EvidenceRecord; found {len(relevant)}",
                )
        elif records:
            collector.error(
                "DISCARDED_SPAN_HAS_EVIDENCE",
                f"retrieved_spans[{span.span_id}].evidence",
                f"discarded span {span.span_id} cannot generate EvidenceRecords",
            )


def validate_evidence_bundle(
    candidate_claims: Sequence[CandidateClaim],
    retrieval_queries: Sequence[RetrievalQuery],
    retrieved_spans: Sequence[RetrievedSpan],
    retrieval_dispositions: Sequence[RetrievalDisposition],
    evidence_records: Sequence[EvidenceRecord],
) -> ValidationReport:
    return _finish(
        lambda collector: _collect_evidence_bundle(
            candidate_claims,
            retrieval_queries,
            retrieved_spans,
            retrieval_dispositions,
            evidence_records,
            collector,
        )
    )


def _collect_claim_evidence_bundle(
    candidate_claims: Sequence[CandidateClaim],
    papers: Sequence[Paper],
    evidence_records: Sequence[EvidenceRecord],
    claim_paper_evidence: Sequence[ClaimPaperEvidence],
    collector: _Collector,
) -> None:
    claim_by_id = _index(candidate_claims, "claim_id", "candidate_claims", collector)
    paper_by_id = _index(papers, "paper_id", "papers", collector)
    evidence_by_id = _index(evidence_records, "evidence_id", "evidence_records", collector)
    _index(
        claim_paper_evidence,
        "claim_paper_evidence_id",
        "claim_paper_evidence",
        collector,
    )

    for position, cpe in enumerate(claim_paper_evidence):
        if cpe.claim_id not in claim_by_id:
            collector.error(
                "INVALID_REFERENCE",
                f"claim_paper_evidence[{position}].claim_id",
                f"candidate claim {cpe.claim_id} does not exist",
            )
        if cpe.paper_id not in paper_by_id:
            collector.error(
                "INVALID_REFERENCE",
                f"claim_paper_evidence[{position}].paper_id",
                f"paper {cpe.paper_id} does not exist",
            )

        groups = cpe.component_relations.grouped()
        grouped_ids = [evidence_id for values in groups.values() for evidence_id in values]
        if len(cpe.evidence_ids) != len(set(cpe.evidence_ids)) or len(grouped_ids) != len(set(grouped_ids)):
            collector.error(
                "CPE_EVIDENCE_PARTITION_MISMATCH",
                f"claim_paper_evidence[{position}].component_relations",
                "CPE evidence IDs must occur exactly once",
            )
        if set(grouped_ids) != set(cpe.evidence_ids):
            collector.error(
                "CPE_EVIDENCE_PARTITION_MISMATCH",
                f"claim_paper_evidence[{position}].component_relations",
                "component relation union must equal evidence_ids",
            )

        for relation, evidence_ids in groups.items():
            for evidence_id in evidence_ids:
                evidence = evidence_by_id.get(evidence_id)
                if evidence is None:
                    collector.error(
                        "INVALID_REFERENCE",
                        f"claim_paper_evidence[{position}].component_relations.{relation.value}",
                        f"EvidenceRecord {evidence_id} does not exist",
                    )
                    continue
                if evidence.claim_id != cpe.claim_id:
                    collector.error(
                        "CPE_CLAIM_MISMATCH",
                        f"claim_paper_evidence[{position}].evidence_ids",
                        f"evidence {evidence_id} belongs to {evidence.claim_id}",
                    )
                if evidence.paper_id != cpe.paper_id:
                    collector.error(
                        "CPE_PAPER_MISMATCH",
                        f"claim_paper_evidence[{position}].evidence_ids",
                        f"evidence {evidence_id} belongs to {evidence.paper_id}",
                    )
                if evidence.relation_to_candidate != relation:
                    collector.error(
                        "CPE_RELATION_MISMATCH",
                        f"claim_paper_evidence[{position}].component_relations.{relation.value}",
                        f"evidence {evidence_id} has stance {_enum_value(evidence.relation_to_candidate)}",
                    )

        if cpe.relation_to_candidate == AggregateRelation.MIXED:
            substantive_count = sum(
                bool(groups[relation])
                for relation in (
                    EvidenceRelation.SUPPORTS,
                    EvidenceRelation.CONTRADICTS,
                    EvidenceRelation.QUALIFIES,
                )
            )
            if substantive_count < 2:
                collector.error(
                    "INVALID_MIXED_CPE",
                    f"claim_paper_evidence[{position}].relation_to_candidate",
                    "mixed CPE requires at least two substantive evidence categories",
                )


def validate_claim_evidence_bundle(
    candidate_claims: Sequence[CandidateClaim],
    papers: Sequence[Paper],
    evidence_records: Sequence[EvidenceRecord],
    claim_paper_evidence: Sequence[ClaimPaperEvidence],
) -> ValidationReport:
    return _finish(
        lambda collector: _collect_claim_evidence_bundle(
            candidate_claims,
            papers,
            evidence_records,
            claim_paper_evidence,
            collector,
        )
    )


def _collect_claim_bundle(
    candidate_claims: Sequence[CandidateClaim],
    claim_paper_evidence: Sequence[ClaimPaperEvidence],
    claim_assessments: Sequence[ClaimAssessment],
    final_claim_validations: Sequence[FinalClaimValidation],
    claim_packets: Sequence[ClaimPacket],
    collector: _Collector,
) -> None:
    claim_by_id = _index(candidate_claims, "claim_id", "candidate_claims", collector)
    cpe_by_id = _index(
        claim_paper_evidence,
        "claim_paper_evidence_id",
        "claim_paper_evidence",
        collector,
    )
    assessment_by_claim = _index(
        claim_assessments, "claim_id", "claim_assessments", collector
    )
    final_by_claim = _index(
        final_claim_validations, "claim_id", "final_claim_validations", collector
    )
    _index(claim_packets, "claim_id", "claim_packets", collector)

    for label, values in (
        ("claim_assessments", claim_assessments),
        ("final_claim_validations", final_claim_validations),
        ("claim_packets", claim_packets),
    ):
        for position, value in enumerate(values):
            if value.claim_id not in claim_by_id:
                collector.error(
                    "INVALID_REFERENCE",
                    f"{label}[{position}].claim_id",
                    f"candidate claim {value.claim_id} does not exist",
                )

    for position, final in enumerate(final_claim_validations):
        for relation_position, relation in enumerate(final.paper_relations):
            cpe = cpe_by_id.get(relation.claim_paper_evidence_id)
            path = f"final_claim_validations[{position}].paper_relations[{relation_position}]"
            if cpe is None:
                collector.error(
                    "INVALID_REFERENCE",
                    f"{path}.claim_paper_evidence_id",
                    f"CPE {relation.claim_paper_evidence_id} does not exist",
                )
                continue
            if cpe.claim_id != final.claim_id:
                collector.error(
                    "CPE_SET_CONTINUITY_MISMATCH",
                    f"{path}.claim_paper_evidence_id",
                    f"CPE belongs to {cpe.claim_id}, not {final.claim_id}",
                )
            if cpe.paper_id != relation.paper_id:
                collector.error(
                    "FINAL_RELATION_PAPER_MISMATCH",
                    f"{path}.paper_id",
                    f"paper relation does not match CPE {cpe.claim_paper_evidence_id}",
                )

    for position, packet in enumerate(claim_packets):
        claim = claim_by_id.get(packet.claim_id)
        assessment = assessment_by_claim.get(packet.claim_id)
        final = final_by_claim.get(packet.claim_id)
        path = f"claim_packets[{position}]"
        if claim is not None:
            if packet.candidate_claim != claim.candidate_claim:
                collector.error(
                    "CLAIM_PACKET_UPSTREAM_MISMATCH",
                    f"{path}.candidate_claim",
                    "candidate_claim differs from CandidateClaim",
                )
            if packet.theme_id != claim.theme_id:
                collector.error(
                    "CLAIM_PACKET_UPSTREAM_MISMATCH",
                    f"{path}.theme_id",
                    "theme_id differs from CandidateClaim",
                )
        if assessment is None:
            collector.error(
                "INVALID_REFERENCE",
                f"{path}.claim_id",
                f"ClaimAssessment for {packet.claim_id} does not exist",
            )
        elif packet.aggregate_strength != assessment.aggregate_strength:
            collector.error(
                "CLAIM_PACKET_UPSTREAM_MISMATCH",
                f"{path}.aggregate_strength",
                "aggregate_strength differs from ClaimAssessment",
            )
        if assessment is not None and assessment.decision == ClaimDecision.REJECT:
            collector.error(
                "REJECT_CANNOT_YIELD_CLAIM_PACKET",
                f"{path}.claim_id",
                "a rejected ClaimAssessment cannot yield a ClaimPacket",
            )
        if final is None:
            collector.error(
                "INVALID_REFERENCE",
                f"{path}.claim_id",
                f"FinalClaimValidation for {packet.claim_id} does not exist",
            )
        else:
            if final.status != FinalClaimStatus.VALID:
                collector.error(
                    "CLAIM_PACKET_REQUIRES_VALID_FINAL_CLAIM",
                    f"{path}.claim_id",
                    f"final validation status is {_enum_value(final.status)}",
                )
            if packet.final_claim != final.final_claim:
                collector.error(
                    "CLAIM_PACKET_UPSTREAM_MISMATCH",
                    f"{path}.final_claim",
                    "final_claim differs from FinalClaimValidation",
                )
            relation_ids = [item.claim_paper_evidence_id for item in final.paper_relations]
            packet_ids = list(packet.claim_paper_evidence_ids)
            if (
                len(packet_ids) != len(set(packet_ids))
                or len(relation_ids) != len(set(relation_ids))
                or set(packet_ids) != set(relation_ids)
            ):
                collector.error(
                    "CPE_SET_CONTINUITY_MISMATCH",
                    f"{path}.claim_paper_evidence_ids",
                    "ClaimPacket CPE set must exactly equal final paper-relation CPE set",
                )

        for cpe_id in packet.claim_paper_evidence_ids:
            cpe = cpe_by_id.get(cpe_id)
            if cpe is None:
                collector.error(
                    "INVALID_REFERENCE",
                    f"{path}.claim_paper_evidence_ids",
                    f"CPE {cpe_id} does not exist",
                )
            elif cpe.claim_id != packet.claim_id:
                collector.error(
                    "CPE_SET_CONTINUITY_MISMATCH",
                    f"{path}.claim_paper_evidence_ids",
                    f"CPE {cpe_id} belongs to {cpe.claim_id}",
                )

def validate_claim_bundle(
    candidate_claims: Sequence[CandidateClaim],
    claim_paper_evidence: Sequence[ClaimPaperEvidence],
    claim_assessments: Sequence[ClaimAssessment],
    final_claim_validations: Sequence[FinalClaimValidation],
    claim_packets: Sequence[ClaimPacket],
) -> ValidationReport:
    return _finish(
        lambda collector: _collect_claim_bundle(
            candidate_claims,
            claim_paper_evidence,
            claim_assessments,
            final_claim_validations,
            claim_packets,
            collector,
        )
    )


def derive_semantic_audit_disposition(audit: SemanticAuditResult) -> AuditDisposition:
    if (
        audit.class_verdict == AuditClassVerdict.UNCLEAR
        or audit.provenance_verdict == AuditProvenanceVerdict.UNCLEAR
    ):
        return AuditDisposition.HUMAN_REVIEW
    if audit.class_verdict == AuditClassVerdict.MISCLASSIFIED:
        return AuditDisposition.REPAIR_BLOCKING
    if audit.provenance_verdict in {
        AuditProvenanceVerdict.OVERSTATED,
        AuditProvenanceVerdict.UNSUPPORTED,
    }:
        return AuditDisposition.REPAIR_BLOCKING
    if audit.provenance_verdict == AuditProvenanceVerdict.PARTIALLY_SUPPORTED:
        return AuditDisposition.REPAIR
    return AuditDisposition.PASS


def _collect_proposition_bundle(
    claim_packets: Sequence[ClaimPacket],
    claim_paper_evidence: Sequence[ClaimPaperEvidence],
    corpus_facts: Sequence[CorpusFact],
    process_facts: Sequence[ReviewProcessFact],
    proposition_records: Sequence[PropositionRecord],
    semantic_audits: Sequence[SemanticAuditResult],
    collector: _Collector,
) -> None:
    packet_by_claim = _index(claim_packets, "claim_id", "claim_packets", collector)
    cpe_by_id = _index(
        claim_paper_evidence,
        "claim_paper_evidence_id",
        "claim_paper_evidence",
        collector,
    )
    corpus_by_id = _index(corpus_facts, "corpus_fact_id", "corpus_facts", collector)
    process_by_id = _index(process_facts, "process_fact_id", "process_facts", collector)
    proposition_by_id = _index(
        proposition_records, "proposition_id", "proposition_records", collector
    )
    _index(semantic_audits, "audit_id", "semantic_audits", collector)

    for position, proposition in enumerate(proposition_records):
        path = f"proposition_records[{position}]"
        for claim_id in proposition.claim_ids:
            if claim_id not in packet_by_claim:
                collector.error(
                    "INVALID_REFERENCE",
                    f"{path}.claim_ids",
                    f"approved ClaimPacket {claim_id} does not exist",
                )
        for fact_id in proposition.corpus_fact_ids:
            if fact_id not in corpus_by_id:
                collector.error(
                    "INVALID_REFERENCE",
                    f"{path}.corpus_fact_ids",
                    f"CorpusFact {fact_id} does not exist",
                )
        for fact_id in proposition.process_fact_ids:
            if fact_id not in process_by_id:
                collector.error(
                    "INVALID_REFERENCE",
                    f"{path}.process_fact_ids",
                    f"ReviewProcessFact {fact_id} does not exist",
                )
        for binding_position, binding in enumerate(proposition.citation_bindings):
            binding_path = f"{path}.citation_bindings[{binding_position}]"
            cpe = cpe_by_id.get(binding.claim_paper_evidence_id)
            if cpe is None:
                collector.error(
                    "INVALID_REFERENCE",
                    f"{binding_path}.claim_paper_evidence_id",
                    f"CPE {binding.claim_paper_evidence_id} does not exist",
                )
                continue
            if binding.claim_id != cpe.claim_id:
                collector.error(
                    "CITATION_BINDING_CLAIM_MISMATCH",
                    f"{binding_path}.claim_id",
                    f"citation claim does not match CPE {cpe.claim_paper_evidence_id}",
                )
            if binding.paper_id != cpe.paper_id:
                collector.error(
                    "CITATION_BINDING_PAPER_MISMATCH",
                    f"{binding_path}.paper_id",
                    f"citation paper does not match CPE {cpe.claim_paper_evidence_id}",
                )
            packet = packet_by_claim.get(binding.claim_id)
            if (
                packet is not None
                and binding.claim_paper_evidence_id
                not in packet.claim_paper_evidence_ids
            ):
                collector.error(
                    "CITATION_BINDING_UNLICENSED_CPE",
                    f"{binding_path}.claim_paper_evidence_id",
                    "citation binding CPE is not licensed by the claim's ClaimPacket",
                )
        if proposition.content_class == PropositionContentClass.SCIENTIFIC_CLAIM:
            binding_claims = {binding.claim_id for binding in proposition.citation_bindings}
            if binding_claims != set(proposition.claim_ids):
                collector.error(
                    "SCIENTIFIC_CLAIM_CITATION_COVERAGE",
                    f"{path}.citation_bindings",
                    "citation binding claims must exactly cover proposition claim_ids",
                )

    audits_by_target: dict[str, list[SemanticAuditResult]] = defaultdict(list)
    for position, audit in enumerate(semantic_audits):
        audits_by_target[audit.target_id].append(audit)
        proposition = proposition_by_id.get(audit.target_id)
        if proposition is None:
            collector.error(
                "INVALID_REFERENCE",
                f"semantic_audits[{position}].target_id",
                f"PropositionRecord {audit.target_id} does not exist",
            )
            continue
        expected_claims = set(proposition.claim_ids)
        expected_corpus = set(proposition.corpus_fact_ids)
        expected_process = set(proposition.process_fact_ids)
        if (
            set(audit.referenced_claim_ids) != expected_claims
            or set(audit.referenced_corpus_fact_ids) != expected_corpus
            or set(audit.referenced_process_fact_ids) != expected_process
        ):
            collector.error(
                "SEMANTIC_AUDIT_INCOMPLETE_PROVENANCE",
                f"semantic_audits[{position}]",
                "semantic audit references must exactly equal target provenance",
            )

    for proposition_id in proposition_by_id:
        count = len(audits_by_target[proposition_id])
        if count != 1:
            collector.error(
                "CARDINALITY_VIOLATION",
                f"proposition_records[{proposition_id}].audit",
                f"proposition requires exactly one current semantic audit; found {count}",
            )


def validate_proposition_bundle(
    claim_packets: Sequence[ClaimPacket],
    claim_paper_evidence: Sequence[ClaimPaperEvidence],
    corpus_facts: Sequence[CorpusFact],
    process_facts: Sequence[ReviewProcessFact],
    proposition_records: Sequence[PropositionRecord],
    semantic_audits: Sequence[SemanticAuditResult],
) -> ValidationReport:
    return _finish(
        lambda collector: _collect_proposition_bundle(
            claim_packets,
            claim_paper_evidence,
            corpus_facts,
            process_facts,
            proposition_records,
            semantic_audits,
            collector,
        )
    )


def _collect_rendered_prose_bundle(
    proposition_records: Sequence[PropositionRecord],
    semantic_audits: Sequence[SemanticAuditResult],
    rendered_sentences: Sequence[RenderedSentence],
    rendered_sentence_audits: Sequence[RenderedSentenceAudit],
    collector: _Collector,
) -> None:
    proposition_by_id = _index(
        proposition_records, "proposition_id", "proposition_records", collector
    )
    sentence_by_id = _index(
        rendered_sentences, "sentence_id", "rendered_sentences", collector
    )
    _index(
        rendered_sentence_audits,
        "audit_id",
        "rendered_sentence_audits",
        collector,
    )
    audits_by_proposition: dict[str, list[SemanticAuditResult]] = defaultdict(list)
    for audit in semantic_audits:
        audits_by_proposition[audit.target_id].append(audit)

    for position, sentence in enumerate(rendered_sentences):
        for proposition_id in sentence.source_proposition_ids:
            if proposition_id not in proposition_by_id:
                collector.error(
                    "INVALID_REFERENCE",
                    f"rendered_sentences[{position}].source_proposition_ids",
                    f"PropositionRecord {proposition_id} does not exist",
                )
                continue
            audits = audits_by_proposition.get(proposition_id, [])
            if len(audits) != 1 or derive_semantic_audit_disposition(audits[0]) != AuditDisposition.PASS:
                collector.error(
                    "RENDERED_FROM_NONPASSING_PROPOSITION",
                    f"rendered_sentences[{position}].source_proposition_ids",
                    f"proposition {proposition_id} lacks exactly one passing audit",
                )

    sentence_audits: dict[str, list[RenderedSentenceAudit]] = defaultdict(list)
    for position, audit in enumerate(rendered_sentence_audits):
        sentence_audits[audit.sentence_id].append(audit)
        if audit.sentence_id not in sentence_by_id:
            collector.error(
                "INVALID_REFERENCE",
                f"rendered_sentence_audits[{position}].sentence_id",
                f"RenderedSentence {audit.sentence_id} does not exist",
            )
        if audit.verdict != AuditProvenanceVerdict.ENTAILED:
            collector.error(
                "RENDERED_SENTENCE_NOT_ENTAILED",
                f"rendered_sentence_audits[{position}].verdict",
                "only ENTAILED rendered sentences qualify for deterministic rendering",
            )

    for sentence_id in sentence_by_id:
        count = len(sentence_audits[sentence_id])
        if count != 1:
            collector.error(
                "CARDINALITY_VIOLATION",
                f"rendered_sentences[{sentence_id}].audit",
                f"rendered sentence requires exactly one current audit; found {count}",
            )


def validate_rendered_prose_bundle(
    proposition_records: Sequence[PropositionRecord],
    semantic_audits: Sequence[SemanticAuditResult],
    rendered_sentences: Sequence[RenderedSentence],
    rendered_sentence_audits: Sequence[RenderedSentenceAudit],
) -> ValidationReport:
    return _finish(
        lambda collector: _collect_rendered_prose_bundle(
            proposition_records,
            semantic_audits,
            rendered_sentences,
            rendered_sentence_audits,
            collector,
        )
    )


def _collect_cross_repository_references(
    themes: Sequence[ThemeRecord],
    papers: Sequence[Paper],
    candidate_claims: Sequence[CandidateClaim],
    corpus_facts: Sequence[CorpusFact],
    collector: _Collector,
) -> None:
    theme_by_id = {theme.theme_id: theme for theme in themes}
    paper_by_id = {paper.paper_id: paper for paper in papers}
    for position, claim in enumerate(candidate_claims):
        if claim.theme_id not in theme_by_id:
            collector.error(
                "INVALID_REFERENCE",
                f"candidate_claims[{position}].theme_id",
                f"ThemeRecord {claim.theme_id} does not exist",
            )
    for position, fact in enumerate(corpus_facts):
        for paper_id in fact.source_paper_ids:
            if paper_id not in paper_by_id:
                collector.error(
                    "INVALID_REFERENCE",
                    f"corpus_facts[{position}].source_paper_ids",
                    f"Paper {paper_id} does not exist",
                )


def validate_repository(
    *,
    themes: Sequence[ThemeRecord] = (),
    papers: Sequence[Paper] = (),
    candidate_claims: Sequence[CandidateClaim] = (),
    retrieval_queries: Sequence[RetrievalQuery] = (),
    retrieved_spans: Sequence[RetrievedSpan] = (),
    retrieval_dispositions: Sequence[RetrievalDisposition] = (),
    evidence_records: Sequence[EvidenceRecord] = (),
    claim_paper_evidence: Sequence[ClaimPaperEvidence] = (),
    claim_assessments: Sequence[ClaimAssessment] = (),
    final_claim_validations: Sequence[FinalClaimValidation] = (),
    claim_packets: Sequence[ClaimPacket] = (),
    corpus_facts: Sequence[CorpusFact] = (),
    process_facts: Sequence[ReviewProcessFact] = (),
    proposition_records: Sequence[PropositionRecord] = (),
    semantic_audits: Sequence[SemanticAuditResult] = (),
    rendered_sentences: Sequence[RenderedSentence] = (),
    rendered_sentence_audits: Sequence[RenderedSentenceAudit] = (),
) -> ValidationReport:
    """Validate the entire current repository snapshot and aggregate all findings."""

    collector = _Collector()
    _collect_identity_registry(papers, collector)
    _collect_theme_hierarchy(themes, collector)
    _collect_cross_repository_references(
        themes, papers, candidate_claims, corpus_facts, collector
    )
    _collect_retrieval_bundle(
        candidate_claims,
        retrieval_queries,
        retrieved_spans,
        retrieval_dispositions,
        papers,
        collector,
    )
    _collect_evidence_bundle(
        candidate_claims,
        retrieval_queries,
        retrieved_spans,
        retrieval_dispositions,
        evidence_records,
        collector,
    )
    _collect_claim_evidence_bundle(
        candidate_claims,
        papers,
        evidence_records,
        claim_paper_evidence,
        collector,
    )
    _collect_claim_bundle(
        candidate_claims,
        claim_paper_evidence,
        claim_assessments,
        final_claim_validations,
        claim_packets,
        collector,
    )
    _collect_proposition_bundle(
        claim_packets,
        claim_paper_evidence,
        corpus_facts,
        process_facts,
        proposition_records,
        semantic_audits,
        collector,
    )
    _collect_rendered_prose_bundle(
        proposition_records,
        semantic_audits,
        rendered_sentences,
        rendered_sentence_audits,
        collector,
    )
    return collector.finish()
