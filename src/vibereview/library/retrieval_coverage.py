"""Project-level retrieval planning, coverage diagnostics, and benchmark harness.

These are operator planning and diagnostic artifacts only.  Canonical
``RetrievalQuery`` IDs remain allocated exclusively through the
GENERATE_RETRIEVAL_QUERIES task promotion; nothing here enters a canonical
scientific model or the public repository.  The coverage status is an
operator-recorded diagnostic judgment, never a computed verdict and never
proof of corpus-wide recall.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path
import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from vibereview.enums import RetrievalIntent
from vibereview.ids import (
    QUERY_INTENT_CODES,
    ClaimId,
    PaperId,
    QueryId,
    Sha256,
    parse_query_id,
)
from vibereview.runtime.dto import (
    MAX_RETRIEVAL_QUERY_CHARS,
    MAX_RETRIEVAL_QUERY_UTF8_BYTES,
)
from vibereview.runtime.hashing import hash_bytes, hash_json
from vibereview.runtime.records import RuntimeModel
from vibereview.runtime.repository import (
    atomic_write_text,
    read_contained_regular_file,
)

from .models import CorpusIntegrityError
from .retrieval import (
    MAX_CANDIDATE_CHARS,
    MAX_QUERY_TERMS,
    MAX_TOP_K,
    DeterministicTextRetriever,
    GraphAssistedRetriever,
    RawCandidateHit,
    RetrievalLedger,
    UnifiedRetrievalCoordinator,
    VerifiedCorpus,
)


QUERY_PLAN_FORMAT_VERSION = "vibereview-retrieval-query-plan-1"
COVERAGE_FORMAT_VERSION = "vibereview-retrieval-coverage-1"
BENCHMARK_FORMAT_VERSION = "vibereview-retrieval-benchmark-1"
BENCHMARK_REPORT_FORMAT_VERSION = "vibereview-retrieval-benchmark-report-1"

INTENT_COVERAGE_ORDER: tuple[RetrievalIntent, ...] = tuple(RetrievalIntent)
REQUIRED_RETRIEVAL_INTENTS = frozenset(INTENT_COVERAGE_ORDER[:4])
_OPTIONAL_PLAN_INTENTS = INTENT_COVERAGE_ORDER[4:]

MAX_PLAN_VARIANTS = 16
MAX_CLAIM_REFERENCE_CHARS = 256
MAX_COVERAGE_ENTRIES = 600
MAX_BENCHMARK_CASES = 256
MAX_CASE_QUERY_TEXTS = 16
MAX_BENCHMARK_DOCUMENT_BYTES = 1024 * 1024

# Keep this tokenization policy aligned with library.retrieval._query_terms:
# only terms longer than two Unicode word characters are usable by the
# deterministic retrievers.
_TERM_PATTERN = re.compile(r"[^\W_]+", flags=re.UNICODE)

# Fixed deterministic per-intent expansion cues.  Terminology variation is a
# property of query text, never of the frozen intent enum.
_INTENT_QUERY_SUFFIXES: dict[RetrievalIntent, str] = {
    RetrievalIntent.SUPPORT: "",
    RetrievalIntent.CONTRADICTION: "no not opposite",
    RetrievalIntent.BOUNDARY: "only within range conditions limits",
    RetrievalIntent.ALTERNATIVE: "alternative instead other mechanisms context",
    RetrievalIntent.METHOD_CHALLENGE: "method measurement protocol bias artifact",
    RetrievalIntent.NULL_RESULT: "no significant effect",
}

_INTENT_QUERY_CODES = {value: code for code, value in QUERY_INTENT_CODES.items()}


def normalized_query_terms(text: str) -> frozenset[str]:
    """Return the deterministic retrieval term set of a query text."""

    return frozenset(
        term
        for term in (match.casefold() for match in _TERM_PATTERN.findall(text))
        if len(term) > 2
    )


def _expand_intent_queries(
    core: str, suffix: str, variants: list[str]
) -> list[str]:
    bases = [core, *[f"{core} {variant}" for variant in variants]]
    return [f"{base} {suffix}" if suffix else base for base in bases]


class QueryPlanIntentEntry(RuntimeModel):
    intent: RetrievalIntent
    query_texts: list[str] = Field(min_length=1, max_length=MAX_PLAN_VARIANTS + 1)

    @model_validator(mode="after")
    def _texts_are_usable(self) -> "QueryPlanIntentEntry":
        for text in self.query_texts:
            if (
                len(text) > MAX_RETRIEVAL_QUERY_CHARS
                or len(text.encode("utf-8")) > MAX_RETRIEVAL_QUERY_UTF8_BYTES
            ):
                raise ValueError("query-plan text exceeds the retrieval text budget")
            if not normalized_query_terms(text):
                raise ValueError(
                    "query-plan text has no usable deterministic retrieval terms"
                )
        if len(set(self.query_texts)) != len(self.query_texts):
            raise ValueError("query-plan texts must be unique within an intent")
        return self


class RetrievalQueryPlan(RuntimeModel):
    """Deterministic operator query plan; allocates no canonical identifiers."""

    format_version: Literal["vibereview-retrieval-query-plan-1"] = (
        QUERY_PLAN_FORMAT_VERSION
    )
    claim_reference: str = Field(min_length=1, max_length=MAX_CLAIM_REFERENCE_CHARS)
    claim_text: str = Field(min_length=1, max_length=MAX_RETRIEVAL_QUERY_CHARS)
    terminology_variants: list[str] = Field(
        default_factory=list, max_length=MAX_PLAN_VARIANTS
    )
    intents: list[QueryPlanIntentEntry] = Field(min_length=4, max_length=6)
    plan_hash: Sha256

    @model_validator(mode="after")
    def _plan_is_canonical(self) -> "RetrievalQueryPlan":
        variants = self.terminology_variants
        if len(set(variants)) != len(variants) or any(
            not variant or variant != " ".join(variant.split())
            for variant in variants
        ):
            raise ValueError("terminology variants must be unique normalized text")
        order = [entry.intent for entry in self.intents]
        if order[:4] != list(INTENT_COVERAGE_ORDER[:4]):
            raise ValueError(
                "query plan must lead with the four required intents in canonical order"
            )
        tail = order[4:]
        if tail != [intent for intent in _OPTIONAL_PLAN_INTENTS if intent in tail]:
            raise ValueError(
                "optional intents must be a unique canonically ordered subset of "
                "method_challenge and null_result"
            )
        all_texts = [text for entry in self.intents for text in entry.query_texts]
        if len(set(all_texts)) != len(all_texts):
            raise ValueError("query-plan texts must be unique across the plan")
        if self.plan_hash != _plan_content_hash(self):
            raise ValueError("query-plan hash does not match the plan content")
        return self


def _plan_content_hash(plan: RetrievalQueryPlan) -> str:
    return hash_json(
        {
            "format_version": plan.format_version,
            "claim_reference": plan.claim_reference,
            "claim_text": plan.claim_text,
            "terminology_variants": list(plan.terminology_variants),
            "intents": [
                {"intent": entry.intent.value, "query_texts": list(entry.query_texts)}
                for entry in plan.intents
            ],
        }
    )


def build_retrieval_query_plan(
    claim_reference: str,
    claim_text: str,
    *,
    terminology_variants: Iterable[str] = (),
    include_method_challenge: bool = True,
    include_null_result: bool = True,
) -> RetrievalQueryPlan:
    """Expand a claim into a deterministic multi-intent operator query plan.

    The plan is planning input for operators and benchmark fixtures.  It
    allocates no canonical identifiers: canonical ``RetrievalQuery`` IDs still
    flow exclusively through the GENERATE_RETRIEVAL_QUERIES task promotion.
    Identical inputs produce byte-identical serialized plans.
    """

    core = " ".join(claim_text.split())
    if not core:
        raise ValueError("claim text must not be empty")
    variants = [" ".join(variant.split()) for variant in terminology_variants]
    if len(variants) > MAX_PLAN_VARIANTS:
        raise ValueError("query plan exceeds the terminology-variant budget")
    if any(not variant for variant in variants):
        raise ValueError("terminology variants must not be empty")
    if not normalized_query_terms(core):
        raise ValueError("claim text yields no usable deterministic retrieval terms")
    selected = list(INTENT_COVERAGE_ORDER[:4])
    if include_method_challenge:
        selected.append(RetrievalIntent.METHOD_CHALLENGE)
    if include_null_result:
        selected.append(RetrievalIntent.NULL_RESULT)
    content = {
        "format_version": QUERY_PLAN_FORMAT_VERSION,
        "claim_reference": claim_reference,
        "claim_text": core,
        "terminology_variants": variants,
        "intents": [
            {
                "intent": intent.value,
                "query_texts": _expand_intent_queries(
                    core, _INTENT_QUERY_SUFFIXES[intent], variants
                ),
            }
            for intent in selected
        ],
    }
    return RetrievalQueryPlan(**content, plan_hash=hash_json(content))


class RetrievalCoverageStatus(StrEnum):
    """Operator-recorded diagnostic judgment; never a computed verdict."""

    ADEQUATE_FOR_SYNTHESIS = "ADEQUATE_FOR_SYNTHESIS"
    PARTIAL = "PARTIAL"
    INADEQUATE = "INADEQUATE"


class ExecutedRetrievalQuery(RuntimeModel):
    """One executed query bound to its ledger by ID, intent, and text hash."""

    query_id: QueryId
    query_text: str = Field(min_length=1, max_length=MAX_RETRIEVAL_QUERY_CHARS)
    intent: RetrievalIntent
    ledger: RetrievalLedger

    @model_validator(mode="after")
    def _entry_matches_ledger(self) -> "ExecutedRetrievalQuery":
        _, encoded_intent, _ = parse_query_id(self.query_id)
        if encoded_intent != self.intent.value:
            raise ValueError("query ID intent and entry intent disagree")
        if self.ledger.query_id != self.query_id:
            raise ValueError("entry query ID does not match its ledger")
        if self.ledger.query_text_hash != hash_bytes(self.query_text.encode("utf-8")):
            raise ValueError("entry query text does not match its ledger text hash")
        return self


class IntentCoverage(RuntimeModel):
    intent: RetrievalIntent
    query_count: int = Field(ge=1)
    selected_candidate_count: int = Field(ge=0)
    distinct_paper_count: int = Field(ge=0)


class ClaimRetrievalCoverage(RuntimeModel):
    claim_id: ClaimId
    executed_intents: list[RetrievalIntent] = Field(min_length=1)
    missing_required_intents: list[RetrievalIntent]
    distinct_query_count: int = Field(ge=1)
    selected_candidate_count: int = Field(ge=0)
    distinct_paper_count: int = Field(ge=0)
    terminology_diversity: int = Field(ge=1)
    per_intent: list[IntentCoverage] = Field(min_length=1)
    duplicate_result_rate: float = Field(ge=0.0, le=1.0)
    invalid_candidate_count: int = Field(ge=0)
    truncated_candidate_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _claim_coverage_is_consistent(self) -> "ClaimRetrievalCoverage":
        if self.executed_intents != sorted(
            set(self.executed_intents), key=INTENT_COVERAGE_ORDER.index
        ):
            raise ValueError("executed intents must be unique and canonically ordered")
        expected_missing = [
            intent
            for intent in INTENT_COVERAGE_ORDER[:4]
            if intent not in self.executed_intents
        ]
        if self.missing_required_intents != expected_missing:
            raise ValueError("missing required intents do not match executed intents")
        if [entry.intent for entry in self.per_intent] != self.executed_intents:
            raise ValueError("per-intent coverage must exactly follow executed intents")
        if self.distinct_query_count != sum(
            entry.query_count for entry in self.per_intent
        ):
            raise ValueError("distinct query count does not match per-intent queries")
        if self.selected_candidate_count != sum(
            entry.selected_candidate_count for entry in self.per_intent
        ):
            raise ValueError(
                "selected candidate count does not match per-intent selection"
            )
        per_intent_papers = [entry.distinct_paper_count for entry in self.per_intent]
        if not (max(per_intent_papers) <= self.distinct_paper_count <= sum(per_intent_papers)):
            raise ValueError("distinct paper count is inconsistent with per-intent coverage")
        if self.terminology_diversity > self.distinct_query_count:
            raise ValueError("terminology diversity cannot exceed the query count")
        return self


class AggregateRetrievalCoverage(RuntimeModel):
    claim_count: int = Field(ge=1)
    executed_query_count: int = Field(ge=1)
    selected_candidate_count: int = Field(ge=0)
    distinct_paper_count: int = Field(ge=0)
    terminology_diversity: int = Field(ge=1)
    per_intent: list[IntentCoverage]
    duplicate_result_rate: float = Field(ge=0.0, le=1.0)
    invalid_candidate_count: int = Field(ge=0)
    truncated_candidate_count: int = Field(ge=0)
    claims_missing_required_intents: list[ClaimId]


def _selected_hits(ledger: RetrievalLedger) -> list[RawCandidateHit]:
    selected = set(ledger.selected_candidate_keys)
    return [hit for hit in ledger.raw_hits if hit.candidate_key in selected]


def _span_identity(hit: RawCandidateHit) -> tuple[str, int, int]:
    if hit.start_offset is None or hit.end_offset is None:  # pragma: no cover
        raise ValueError("selected candidate lacks source offsets")
    return (hit.paper_id, hit.start_offset, hit.end_offset)


def _duplicate_result_rate(identities: list[tuple[str, int, int]]) -> float:
    """Fraction of selected hits repeating an already-seen (paper, span) identity."""

    if not identities:
        return 0.0
    return 1.0 - len(set(identities)) / len(identities)


def _truncated_candidates(ledger: RetrievalLedger) -> int:
    return (
        ledger.text_truncated_valid_candidates
        + ledger.text_truncated_invalid_candidates
        + ledger.graph_truncated_valid_candidates
        + ledger.graph_truncated_invalid_candidates
    )


def _claim_coverage(
    claim_id: str, entries: list[ExecutedRetrievalQuery]
) -> ClaimRetrievalCoverage:
    executed = sorted(
        {entry.intent for entry in entries}, key=INTENT_COVERAGE_ORDER.index
    )
    per_intent: list[IntentCoverage] = []
    for intent in executed:
        intent_entries = [entry for entry in entries if entry.intent is intent]
        intent_hits = [
            hit
            for entry in intent_entries
            for hit in _selected_hits(entry.ledger)
        ]
        per_intent.append(
            IntentCoverage(
                intent=intent,
                query_count=len(intent_entries),
                selected_candidate_count=len(intent_hits),
                distinct_paper_count=len({hit.paper_id for hit in intent_hits}),
            )
        )
    selected = [hit for entry in entries for hit in _selected_hits(entry.ledger)]
    return ClaimRetrievalCoverage(
        claim_id=claim_id,
        executed_intents=executed,
        missing_required_intents=[
            intent for intent in INTENT_COVERAGE_ORDER[:4] if intent not in executed
        ],
        distinct_query_count=len(entries),
        selected_candidate_count=len(selected),
        distinct_paper_count=len({hit.paper_id for hit in selected}),
        terminology_diversity=len(
            {normalized_query_terms(entry.query_text) for entry in entries}
        ),
        per_intent=per_intent,
        duplicate_result_rate=_duplicate_result_rate(
            [_span_identity(hit) for hit in selected]
        ),
        invalid_candidate_count=sum(
            entry.ledger.invalid_candidates for entry in entries
        ),
        truncated_candidate_count=sum(
            _truncated_candidates(entry.ledger) for entry in entries
        ),
    )


class RetrievalCoverageReport(RuntimeModel):
    """Versioned operator diagnostic; the status is recorded, never computed."""

    format_version: Literal["vibereview-retrieval-coverage-1"] = (
        COVERAGE_FORMAT_VERSION
    )
    status: RetrievalCoverageStatus
    corpus_lock_hash: Sha256
    source_generation: int = Field(ge=0)
    assessor: str | None = Field(default=None, min_length=1, max_length=256)
    assessed_at: str | None = None
    claims: list[ClaimRetrievalCoverage] = Field(min_length=1)
    aggregate: AggregateRetrievalCoverage

    @field_validator("assessed_at")
    @classmethod
    def _iso8601_timestamp(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                datetime.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("assessed_at must be an ISO-8601 timestamp") from exc
        return value

    @model_validator(mode="after")
    def _report_is_consistent(self) -> "RetrievalCoverageReport":
        claim_ids = [claim.claim_id for claim in self.claims]
        if claim_ids != sorted(set(claim_ids)):
            raise ValueError("claim coverage entries must be unique and sorted")
        aggregate = self.aggregate
        if aggregate.claim_count != len(self.claims):
            raise ValueError("aggregate claim count does not match claims")
        if aggregate.executed_query_count != sum(
            claim.distinct_query_count for claim in self.claims
        ):
            raise ValueError("aggregate query count does not match claims")
        if aggregate.selected_candidate_count != sum(
            claim.selected_candidate_count for claim in self.claims
        ):
            raise ValueError("aggregate selection count does not match claims")
        if aggregate.invalid_candidate_count != sum(
            claim.invalid_candidate_count for claim in self.claims
        ):
            raise ValueError("aggregate invalid count does not match claims")
        if aggregate.truncated_candidate_count != sum(
            claim.truncated_candidate_count for claim in self.claims
        ):
            raise ValueError("aggregate truncation count does not match claims")
        if aggregate.claims_missing_required_intents != [
            claim.claim_id for claim in self.claims if claim.missing_required_intents
        ]:
            raise ValueError("aggregate missing-intent claims do not match claims")
        expected_per_intent = []
        for intent in INTENT_COVERAGE_ORDER:
            entries = [
                entry
                for claim in self.claims
                for entry in claim.per_intent
                if entry.intent is intent
            ]
            if not entries:
                continue
            expected_per_intent.append(
                IntentCoverage(
                    intent=intent,
                    query_count=sum(entry.query_count for entry in entries),
                    selected_candidate_count=sum(
                        entry.selected_candidate_count for entry in entries
                    ),
                    distinct_paper_count=0,
                )
            )
        actual = {entry.intent: entry for entry in aggregate.per_intent}
        if [entry.intent for entry in aggregate.per_intent] != [
            entry.intent for entry in expected_per_intent
        ]:
            raise ValueError("aggregate per-intent coverage does not match claims")
        for expected in expected_per_intent:
            observed = actual[expected.intent]
            if (
                observed.query_count != expected.query_count
                or observed.selected_candidate_count
                != expected.selected_candidate_count
            ):
                raise ValueError("aggregate per-intent counts do not match claims")
            claim_papers = [
                entry.distinct_paper_count
                for claim in self.claims
                for entry in claim.per_intent
                if entry.intent is expected.intent
            ]
            if not (
                max(claim_papers)
                <= observed.distinct_paper_count
                <= sum(claim_papers)
            ):
                raise ValueError(
                    "aggregate per-intent paper diversity is inconsistent with claims"
                )
        claim_paper_counts = [claim.distinct_paper_count for claim in self.claims]
        if not (
            max(claim_paper_counts)
            <= aggregate.distinct_paper_count
            <= sum(claim_paper_counts)
        ):
            raise ValueError("aggregate paper diversity is inconsistent with claims")
        claim_terminology = [claim.terminology_diversity for claim in self.claims]
        if not (
            max(claim_terminology)
            <= aggregate.terminology_diversity
            <= sum(claim_terminology)
        ):
            raise ValueError("aggregate terminology diversity is inconsistent")
        if aggregate.terminology_diversity > aggregate.executed_query_count:
            raise ValueError("aggregate terminology diversity exceeds query count")
        return self


def compute_retrieval_coverage(
    entries: Sequence[ExecutedRetrievalQuery],
    *,
    status: RetrievalCoverageStatus,
    assessor: str | None = None,
    assessed_at: str | None = None,
) -> RetrievalCoverageReport:
    """Compute per-claim and aggregate retrieval-coverage diagnostics.

    The coverage status is supplied by the operator; it is a diagnostic
    judgment, never a computed verdict and never proof of exhaustive recall.
    """

    if not entries:
        raise ValueError("retrieval coverage requires at least one executed query")
    if len(entries) > MAX_COVERAGE_ENTRIES:
        raise ValueError("retrieval coverage exceeds the executed-query budget")
    query_ids = [entry.query_id for entry in entries]
    if len(set(query_ids)) != len(query_ids):
        raise ValueError("executed retrieval queries must have unique query IDs")
    lock_hashes = {entry.ledger.corpus_lock_hash for entry in entries}
    if len(lock_hashes) != 1:
        raise ValueError("retrieval coverage requires a single corpus lock hash")
    generations = {entry.ledger.source_generation for entry in entries}
    if len(generations) != 1:
        raise ValueError("retrieval coverage requires a single source generation")

    by_claim: dict[str, list[ExecutedRetrievalQuery]] = {}
    for entry in entries:
        claim_id, _, _ = parse_query_id(entry.query_id)
        by_claim.setdefault(claim_id, []).append(entry)
    claims = [
        _claim_coverage(claim_id, by_claim[claim_id]) for claim_id in sorted(by_claim)
    ]
    all_selected = [
        hit for entry in entries for hit in _selected_hits(entry.ledger)
    ]
    aggregate_per_intent = []
    for intent in INTENT_COVERAGE_ORDER:
        intent_entries = [entry for entry in entries if entry.intent is intent]
        if not intent_entries:
            continue
        intent_hits = [
            hit
            for entry in intent_entries
            for hit in _selected_hits(entry.ledger)
        ]
        aggregate_per_intent.append(
            IntentCoverage(
                intent=intent,
                query_count=len(intent_entries),
                selected_candidate_count=len(intent_hits),
                distinct_paper_count=len({hit.paper_id for hit in intent_hits}),
            )
        )
    aggregate = AggregateRetrievalCoverage(
        claim_count=len(claims),
        executed_query_count=len(entries),
        selected_candidate_count=len(all_selected),
        distinct_paper_count=len({hit.paper_id for hit in all_selected}),
        terminology_diversity=len(
            {normalized_query_terms(entry.query_text) for entry in entries}
        ),
        per_intent=aggregate_per_intent,
        duplicate_result_rate=_duplicate_result_rate(
            [_span_identity(hit) for hit in all_selected]
        ),
        invalid_candidate_count=sum(
            entry.ledger.invalid_candidates for entry in entries
        ),
        truncated_candidate_count=sum(
            _truncated_candidates(entry.ledger) for entry in entries
        ),
        claims_missing_required_intents=[
            claim.claim_id for claim in claims if claim.missing_required_intents
        ],
    )
    return RetrievalCoverageReport(
        status=status,
        corpus_lock_hash=next(iter(lock_hashes)),
        source_generation=next(iter(generations)),
        assessor=assessor,
        assessed_at=assessed_at,
        claims=claims,
        aggregate=aggregate,
    )


class RetrievalBenchmarkCategory(StrEnum):
    SUPPORT = "support"
    CONTRADICTION = "contradiction"
    QUALIFICATION = "qualification"
    NULL_RESULT = "null_result"
    BOUNDARY = "boundary"
    TERMINOLOGY_VARIANT = "terminology_variant"
    MECHANISM = "mechanism"
    METHOD_SPECIFIC = "method_specific"
    FULL_TEXT_ONLY = "full_text_only"


class RetrievalBenchmarkCase(RuntimeModel):
    """One curated case; expected papers are pinned by source content hash."""

    case_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    category: RetrievalBenchmarkCategory
    intent: RetrievalIntent
    query_texts: list[str] = Field(min_length=1, max_length=MAX_CASE_QUERY_TEXTS)
    expected_source_sha256: Sha256
    expected_passage: str | None = Field(
        default=None, min_length=1, max_length=MAX_CANDIDATE_CHARS
    )

    @model_validator(mode="after")
    def _queries_are_bounded(self) -> "RetrievalBenchmarkCase":
        for text in self.query_texts:
            if (
                not text
                or len(text) > MAX_RETRIEVAL_QUERY_CHARS
                or len(text.encode("utf-8")) > MAX_RETRIEVAL_QUERY_UTF8_BYTES
            ):
                raise ValueError("benchmark query text exceeds the retrieval budget")
        return self


class RetrievalBenchmarkDocument(RuntimeModel):
    """Versioned curated case file; real cases live outside the repository."""

    format_version: Literal["vibereview-retrieval-benchmark-1"] = (
        BENCHMARK_FORMAT_VERSION
    )
    cases: list[RetrievalBenchmarkCase] = Field(
        min_length=1, max_length=MAX_BENCHMARK_CASES
    )

    @model_validator(mode="after")
    def _case_ids_are_unique(self) -> "RetrievalBenchmarkDocument":
        case_ids = [case.case_id for case in self.cases]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("benchmark case IDs must be unique")
        return self


def load_retrieval_benchmark(path: Path) -> RetrievalBenchmarkDocument:
    """Load one bounded, strictly validated benchmark case file."""

    if not path.exists():
        raise FileNotFoundError(f"retrieval benchmark not found: {path}")
    content, _ = read_contained_regular_file(
        path.parent, path.name, max_bytes=MAX_BENCHMARK_DOCUMENT_BYTES
    )
    return RetrievalBenchmarkDocument.model_validate_json(content)


def save_retrieval_benchmark(
    document: RetrievalBenchmarkDocument, path: Path
) -> None:
    atomic_write_text(path, document.model_dump_json(indent=2) + "\n")


class RetrievalBenchmarkCaseResult(RuntimeModel):
    case_id: str
    category: RetrievalBenchmarkCategory
    intent: RetrievalIntent
    expected_paper_id: PaperId
    query_ids: list[QueryId] = Field(min_length=1)
    selected_candidate_count: int = Field(ge=0)
    selected_paper_ids: list[PaperId]
    paper_hit: bool
    passage_hit: bool | None

    @model_validator(mode="after")
    def _result_is_consistent(self) -> "RetrievalBenchmarkCaseResult":
        if self.selected_paper_ids != sorted(set(self.selected_paper_ids)):
            raise ValueError("selected paper IDs must be unique and sorted")
        if len(self.selected_paper_ids) > self.selected_candidate_count:
            raise ValueError("selected papers cannot exceed selected candidates")
        if self.paper_hit and self.expected_paper_id not in self.selected_paper_ids:
            raise ValueError("a paper hit must name the expected paper")
        if not self.paper_hit and self.expected_paper_id in self.selected_paper_ids:
            raise ValueError("a paper miss cannot name the expected paper")
        return self


class BenchmarkCategoryMetrics(RuntimeModel):
    category: RetrievalBenchmarkCategory
    case_count: int = Field(ge=1)
    paper_hit_count: int = Field(ge=0)
    paper_recall: float = Field(ge=0.0, le=1.0)
    passage_case_count: int = Field(ge=0)
    passage_hit_count: int = Field(ge=0)
    passage_recall: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _metrics_are_consistent(self) -> "BenchmarkCategoryMetrics":
        if self.paper_hit_count > self.case_count:
            raise ValueError("paper hits cannot exceed cases")
        if self.paper_recall != self.paper_hit_count / self.case_count:
            raise ValueError("paper recall does not match its counts")
        if self.passage_hit_count > self.passage_case_count:
            raise ValueError("passage hits cannot exceed passage cases")
        expected = (
            self.passage_hit_count / self.passage_case_count
            if self.passage_case_count
            else None
        )
        if self.passage_recall != expected:
            raise ValueError("passage recall does not match its counts")
        return self


class RetrievalBenchmarkReport(RuntimeModel):
    """Versioned benchmark outcome over one verified corpus generation."""

    format_version: Literal["vibereview-retrieval-benchmark-report-1"] = (
        BENCHMARK_REPORT_FORMAT_VERSION
    )
    corpus_lock_hash: Sha256
    source_generation: int = Field(ge=0)
    top_k: int = Field(ge=1, le=MAX_TOP_K)
    max_query_terms: int = Field(ge=1, le=MAX_QUERY_TERMS)
    case_count: int = Field(ge=1)
    paper_hit_count: int = Field(ge=0)
    known_paper_recall: float = Field(ge=0.0, le=1.0)
    passage_case_count: int = Field(ge=0)
    passage_hit_count: int = Field(ge=0)
    known_passage_recall: float | None = Field(default=None, ge=0.0, le=1.0)
    per_category: list[BenchmarkCategoryMetrics] = Field(min_length=1)
    cases: list[RetrievalBenchmarkCaseResult] = Field(min_length=1)

    @model_validator(mode="after")
    def _report_matches_cases(self) -> "RetrievalBenchmarkReport":
        if self.case_count != len(self.cases):
            raise ValueError("benchmark case count does not match case results")
        if self.paper_hit_count != sum(case.paper_hit for case in self.cases):
            raise ValueError("benchmark paper hits do not match case results")
        if self.known_paper_recall != self.paper_hit_count / self.case_count:
            raise ValueError("known-paper recall does not match its counts")
        passage_cases = [case for case in self.cases if case.passage_hit is not None]
        if self.passage_case_count != len(passage_cases):
            raise ValueError("passage case count does not match case results")
        if self.passage_hit_count != sum(
            case.passage_hit for case in passage_cases
        ):
            raise ValueError("passage hits do not match case results")
        expected_passage_recall = (
            self.passage_hit_count / self.passage_case_count
            if self.passage_case_count
            else None
        )
        if self.known_passage_recall != expected_passage_recall:
            raise ValueError("known-passage recall does not match its counts")
        expected_categories = sorted(
            {case.category for case in self.cases}, key=lambda category: category.value
        )
        if [metrics.category for metrics in self.per_category] != expected_categories:
            raise ValueError("per-category metrics must cover each case category once")
        for metrics in self.per_category:
            category_cases = [
                case for case in self.cases if case.category is metrics.category
            ]
            if metrics.case_count != len(category_cases) or (
                metrics.paper_hit_count
                != sum(case.paper_hit for case in category_cases)
            ):
                raise ValueError("per-category counts do not match case results")
            category_passage = [
                case for case in category_cases if case.passage_hit is not None
            ]
            if metrics.passage_case_count != len(category_passage) or (
                metrics.passage_hit_count
                != sum(case.passage_hit for case in category_passage)
            ):
                raise ValueError("per-category passage counts do not match results")
        return self


def run_retrieval_benchmark(
    document: RetrievalBenchmarkDocument,
    corpus: VerifiedCorpus,
    *,
    coordinator: UnifiedRetrievalCoordinator | None = None,
    top_k: int = 10,
    max_query_terms: int = MAX_QUERY_TERMS,
) -> RetrievalBenchmarkReport:
    """Execute curated cases against the verified corpus and measure recall.

    Query IDs built here are deterministic runtime handles scoped to the
    benchmark run; they are never persisted and allocate no canonical IDs.
    """

    if not 1 <= top_k <= MAX_TOP_K:
        raise ValueError(f"top_k must be between 1 and {MAX_TOP_K}")
    if not 1 <= max_query_terms <= MAX_QUERY_TERMS:
        raise ValueError(f"max_query_terms must be between 1 and {MAX_QUERY_TERMS}")
    if coordinator is None:
        text = DeterministicTextRetriever._from_verified_corpus(corpus)
        graph = GraphAssistedRetriever._from_verified_corpus(corpus)
        coordinator = UnifiedRetrievalCoordinator(text, graph)
    elif coordinator.text_retriever.corpus.lock_hash != corpus.lock_hash:
        raise CorpusIntegrityError(
            "benchmark coordinator does not use the verified corpus lock"
        )

    papers_by_source_hash: dict[str, str] = {}
    for paper in corpus.lock.papers:
        if paper.source_hash in papers_by_source_hash:
            raise CorpusIntegrityError(
                "corpus lock maps one source hash to multiple papers"
            )
        papers_by_source_hash[paper.source_hash] = paper.paper_id

    results: list[RetrievalBenchmarkCaseResult] = []
    for case_position, case in enumerate(document.cases, start=1):
        expected_paper_id = papers_by_source_hash.get(case.expected_source_sha256)
        if expected_paper_id is None:
            raise ValueError(
                f"benchmark case {case.case_id} names a source hash absent "
                "from the corpus lock"
            )
        claim_component = f"C{case_position:04d}"
        ordinals: dict[RetrievalIntent, int] = {}
        selected: list[RawCandidateHit] = []
        query_ids: list[str] = []
        for query_text in case.query_texts:
            ordinal = ordinals.get(case.intent, 0) + 1
            if ordinal > 99:
                raise ValueError("benchmark case exceeds the query ordinal capacity")
            ordinals[case.intent] = ordinal
            query_id = (
                f"Q-{claim_component}-{_INTENT_QUERY_CODES[case.intent]}-"
                f"{ordinal:02d}"
            )
            hits, _ = coordinator.retrieve(
                query_text,
                query_id,
                case.intent,
                top_k=top_k,
                max_query_terms=max_query_terms,
            )
            selected.extend(hits)
            query_ids.append(query_id)
        paper_hit = any(hit.paper_id == expected_paper_id for hit in selected)
        passage_hit = (
            any(
                case.expected_passage in hit.source_text for hit in selected
            )
            if case.expected_passage is not None
            else None
        )
        results.append(
            RetrievalBenchmarkCaseResult(
                case_id=case.case_id,
                category=case.category,
                intent=case.intent,
                expected_paper_id=expected_paper_id,
                query_ids=query_ids,
                selected_candidate_count=len(selected),
                selected_paper_ids=sorted({hit.paper_id for hit in selected}),
                paper_hit=paper_hit,
                passage_hit=passage_hit,
            )
        )

    per_category = []
    for category in sorted(
        {case.category for case in document.cases}, key=lambda category: category.value
    ):
        category_results = [
            result for result in results if result.category is category
        ]
        passage_results = [
            result for result in category_results if result.passage_hit is not None
        ]
        per_category.append(
            BenchmarkCategoryMetrics(
                category=category,
                case_count=len(category_results),
                paper_hit_count=sum(result.paper_hit for result in category_results),
                paper_recall=(
                    sum(result.paper_hit for result in category_results)
                    / len(category_results)
                ),
                passage_case_count=len(passage_results),
                passage_hit_count=sum(
                    result.passage_hit for result in passage_results
                ),
                passage_recall=(
                    sum(result.passage_hit for result in passage_results)
                    / len(passage_results)
                    if passage_results
                    else None
                ),
            )
        )
    passage_cases = [result for result in results if result.passage_hit is not None]
    paper_hits = sum(result.paper_hit for result in results)
    passage_hits = sum(result.passage_hit for result in passage_cases)
    return RetrievalBenchmarkReport(
        corpus_lock_hash=corpus.lock_hash,
        source_generation=corpus.generation,
        top_k=top_k,
        max_query_terms=max_query_terms,
        case_count=len(results),
        paper_hit_count=paper_hits,
        known_paper_recall=paper_hits / len(results),
        passage_case_count=len(passage_cases),
        passage_hit_count=passage_hits,
        known_passage_recall=(
            passage_hits / len(passage_cases) if passage_cases else None
        ),
        per_category=per_category,
        cases=results,
    )
