"""Project-level semantic-benchmark machinery for evidence assessment.

These are operator evaluation artifacts only.  The adjudicated label
vocabulary is the frozen ``EvidenceRelation`` enum used by the
ASSESS_EVIDENCE contract (``supports``, ``contradicts``, ``qualifies``,
``contextual``, ``unclear``); this module adds no canonical model, enum
value, or state transition.  Real adjudicated claim--passage pairs are human
work product held outside the repository; nothing here fabricates them.

Category tags describe the design intent of a case only.  Adjudicated and
predicted labels always use the frozen ``EvidenceRelation`` vocabulary; the
category-to-label mapping is the adjudicator's decision and is never
enforced.  Typical mappings: ``irrelevant`` passages adjudicate as
``contextual`` or ``unclear``; ``negation`` and ``null_result`` cases as
``contradicts`` (or ``unclear`` when the passage does not address the
claim); ``partial_support`` as ``qualifies`` or ``supports``; and
``boundary``, ``material_mismatch``, ``method_dependent``, and
``causal_mismatch`` cases as ``qualifies`` or ``contextual``.

Metric definitions (each computed per split partition and overall):

- per-class precision for label L: true positives / cases predicted as L
  (``None`` when nothing is predicted as L);
- per-class recall for label L: true positives / cases adjudicated as L
  (``None`` when no case is adjudicated as L);
- false-support rate: among cases whose adjudicated label is not
  ``supports``, the fraction predicted as ``supports``; failed predictions
  (the ``unpredicted`` bucket) count in the denominator only, and the rate
  is ``None`` when no non-support case exists;
- contradiction recall and qualification recall are the recalls of the
  ``contradicts`` and ``qualifies`` classes, surfaced as first-class fields
  because they are the safety-critical metrics.

A classifier failure (an exception or a non-``EvidenceRelation`` return
value) is recorded under the explicit ``unpredicted`` prediction column; a
failed prediction is never silently dropped.  Reviewer disagreement is
retained per case and surfaced in every report; it is never erased.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from vibereview.enums import EvidenceRelation
from vibereview.runtime.records import RuntimeModel
from vibereview.runtime.repository import (
    atomic_write_text,
    read_contained_regular_file,
)


SEMANTIC_BENCHMARK_FORMAT_VERSION = "vibereview-semantic-benchmark-1"
SEMANTIC_BENCHMARK_REPORT_FORMAT_VERSION = "vibereview-semantic-benchmark-report-1"

UNPREDICTED_LABEL = "unpredicted"

MAX_BENCHMARK_CASES = 512
MAX_CASE_CLAIM_CHARS = 4_096
MAX_CASE_PASSAGE_CHARS = 32_768
MAX_CASE_SOURCE_KEY_CHARS = 256
MAX_REVIEWER_LABELS = 16
MAX_REVIEWER_CHARS = 128
MAX_CLASSIFIER_REFERENCE_CHARS = 256
MAX_BENCHMARK_DOCUMENT_BYTES = 4 * 1024 * 1024

_LABEL_ORDER: tuple[EvidenceRelation, ...] = tuple(EvidenceRelation)
_PREDICTION_ORDER: tuple[str, ...] = tuple(
    label.value for label in _LABEL_ORDER
) + (UNPREDICTED_LABEL,)

PredictedRelation = EvidenceRelation | Literal["unpredicted"]
SemanticBenchmarkSplit = Literal["development", "evaluation"]
SemanticClassifier = Callable[[str, str], EvidenceRelation | str]

FALSE_SUPPORT_RATE_DEFINITION = (
    "Among cases whose adjudicated label is not 'supports', the fraction "
    "predicted as 'supports'; unpredicted classifier failures count in the "
    "denominator only; null when no non-support case exists."
)


class SemanticBenchmarkCategory(StrEnum):
    """Descriptive case-design tag; never an adjudicated label."""

    SUPPORT = "support"
    CONTRADICTION = "contradiction"
    QUALIFICATION = "qualification"
    CONTEXT = "context"
    UNCLEAR = "unclear"
    IRRELEVANT = "irrelevant"
    NEGATION = "negation"
    NULL_RESULT = "null_result"
    PARTIAL_SUPPORT = "partial_support"
    BOUNDARY = "boundary"
    MATERIAL_MISMATCH = "material_mismatch"
    METHOD_DEPENDENT = "method_dependent"
    CAUSAL_MISMATCH = "causal_mismatch"


class ReviewerLabel(RuntimeModel):
    """One individual reviewer label retained for disagreement visibility."""

    reviewer: str = Field(min_length=1, max_length=MAX_REVIEWER_CHARS)
    label: EvidenceRelation


class SemanticBenchmarkCase(RuntimeModel):
    """One adjudicated claim--passage pair; real cases live outside the repo."""

    case_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    claim_text: str = Field(min_length=1, max_length=MAX_CASE_CLAIM_CHARS)
    passage_text: str = Field(min_length=1, max_length=MAX_CASE_PASSAGE_CHARS)
    source_key: str | None = Field(
        default=None, min_length=1, max_length=MAX_CASE_SOURCE_KEY_CHARS
    )
    adjudicated_label: EvidenceRelation
    reviewer_labels: list[ReviewerLabel] = Field(
        default_factory=list, max_length=MAX_REVIEWER_LABELS
    )
    retained_disagreement: bool
    split: SemanticBenchmarkSplit
    category: SemanticBenchmarkCategory | None = None

    @model_validator(mode="after")
    def _disagreement_flag_is_derived(self) -> "SemanticBenchmarkCase":
        reviewers = [entry.reviewer for entry in self.reviewer_labels]
        if len(reviewers) != len(set(reviewers)):
            raise ValueError("reviewer labels must name unique reviewers")
        labels = [entry.label for entry in self.reviewer_labels]
        expected = any(label != self.adjudicated_label for label in labels) or (
            len(set(labels)) > 1
        )
        if self.retained_disagreement != expected:
            raise ValueError(
                "retained_disagreement must equal its derivation: true exactly "
                "when a reviewer label differs from the adjudicated label or "
                "two reviewer labels differ"
            )
        return self


class SemanticBenchmarkDocument(RuntimeModel):
    """Versioned adjudicated case file; an operator artifact, never canonical."""

    format_version: Literal["vibereview-semantic-benchmark-1"] = (
        SEMANTIC_BENCHMARK_FORMAT_VERSION
    )
    cases: list[SemanticBenchmarkCase] = Field(
        min_length=1, max_length=MAX_BENCHMARK_CASES
    )

    @model_validator(mode="after")
    def _case_ids_are_unique(self) -> "SemanticBenchmarkDocument":
        case_ids = [case.case_id for case in self.cases]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("semantic benchmark case IDs must be unique")
        return self


def load_semantic_benchmark(path: Path) -> SemanticBenchmarkDocument:
    """Load one bounded, strictly validated adjudicated case file."""

    if not path.exists():
        raise FileNotFoundError(f"semantic benchmark not found: {path}")
    content, _ = read_contained_regular_file(
        path.parent, path.name, max_bytes=MAX_BENCHMARK_DOCUMENT_BYTES
    )
    return SemanticBenchmarkDocument.model_validate_json(content)


def save_semantic_benchmark(
    document: SemanticBenchmarkDocument, path: Path
) -> None:
    atomic_write_text(path, document.model_dump_json(indent=2) + "\n")


class SemanticBenchmarkCaseOutcome(RuntimeModel):
    """One scored case; ``unpredicted`` records a classifier failure."""

    case_id: str
    split: SemanticBenchmarkSplit
    category: SemanticBenchmarkCategory | None
    adjudicated_label: EvidenceRelation
    predicted_label: PredictedRelation
    retained_disagreement: bool
    correct: bool

    @model_validator(mode="after")
    def _correctness_is_derived(self) -> "SemanticBenchmarkCaseOutcome":
        if self.correct != (self.predicted_label == self.adjudicated_label):
            raise ValueError(
                "outcome correctness must equal adjudicated == predicted"
            )
        return self


def _predicted_key(predicted: PredictedRelation) -> str:
    return predicted.value if isinstance(predicted, EvidenceRelation) else predicted


class SemanticConfusionCell(RuntimeModel):
    predicted: PredictedRelation
    count: int = Field(ge=0)


class SemanticConfusionRow(RuntimeModel):
    """One adjudicated-label row over every prediction column, in order."""

    adjudicated: EvidenceRelation
    cells: list[SemanticConfusionCell] = Field(
        min_length=len(_PREDICTION_ORDER), max_length=len(_PREDICTION_ORDER)
    )

    @model_validator(mode="after")
    def _cells_follow_canonical_order(self) -> "SemanticConfusionRow":
        if [_predicted_key(cell.predicted) for cell in self.cells] != list(
            _PREDICTION_ORDER
        ):
            raise ValueError(
                "confusion row cells must cover every prediction column once "
                "in canonical order"
            )
        return self


class SemanticClassMetrics(RuntimeModel):
    """Per-class counts; precision/recall are derived, never asserted."""

    label: EvidenceRelation
    adjudicated_count: int = Field(ge=0)
    predicted_count: int = Field(ge=0)
    true_positive_count: int = Field(ge=0)
    precision: float | None = Field(default=None, ge=0.0, le=1.0)
    recall: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _metrics_match_counts(self) -> "SemanticClassMetrics":
        if self.true_positive_count > min(self.adjudicated_count, self.predicted_count):
            raise ValueError("true positives cannot exceed class counts")
        expected_precision = (
            self.true_positive_count / self.predicted_count
            if self.predicted_count
            else None
        )
        if self.precision != expected_precision:
            raise ValueError("per-class precision does not match its counts")
        expected_recall = (
            self.true_positive_count / self.adjudicated_count
            if self.adjudicated_count
            else None
        )
        if self.recall != expected_recall:
            raise ValueError("per-class recall does not match its counts")
        return self


def _false_support_rate(
    confusion: list[SemanticConfusionRow],
) -> float | None:
    non_support_rows = [
        row for row in confusion if row.adjudicated is not EvidenceRelation.SUPPORTS
    ]
    denominator = sum(cell.count for row in non_support_rows for cell in row.cells)
    if not denominator:
        return None
    numerator = sum(
        cell.count
        for row in non_support_rows
        for cell in row.cells
        if cell.predicted is EvidenceRelation.SUPPORTS
    )
    return numerator / denominator


class SemanticMetrics(RuntimeModel):
    """Metrics over one case partition; every aggregate is recomputed."""

    case_count: int = Field(ge=0)
    unpredicted_count: int = Field(ge=0)
    confusion: list[SemanticConfusionRow] = Field(
        min_length=len(_LABEL_ORDER), max_length=len(_LABEL_ORDER)
    )
    per_class: list[SemanticClassMetrics] = Field(
        min_length=len(_LABEL_ORDER), max_length=len(_LABEL_ORDER)
    )
    false_support_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    contradiction_recall: float | None = Field(default=None, ge=0.0, le=1.0)
    qualification_recall: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _aggregates_match_confusion(self) -> "SemanticMetrics":
        if [row.adjudicated for row in self.confusion] != list(_LABEL_ORDER):
            raise ValueError(
                "confusion rows must cover every adjudicated label once in "
                "canonical order"
            )
        if [entry.label for entry in self.per_class] != list(_LABEL_ORDER):
            raise ValueError(
                "per-class metrics must cover every label once in canonical order"
            )
        per_class = {entry.label: entry for entry in self.per_class}
        for row in self.confusion:
            entry = per_class[row.adjudicated]
            if sum(cell.count for cell in row.cells) != entry.adjudicated_count:
                raise ValueError("confusion row does not match per-class counts")
        for column, predicted in enumerate(_PREDICTION_ORDER):
            column_total = sum(row.cells[column].count for row in self.confusion)
            if predicted == UNPREDICTED_LABEL:
                if column_total != self.unpredicted_count:
                    raise ValueError(
                        "confusion unpredicted column does not match its count"
                    )
            elif column_total != per_class[EvidenceRelation(predicted)].predicted_count:
                raise ValueError("confusion column does not match per-class counts")
        if sum(entry.adjudicated_count for entry in self.per_class) != self.case_count:
            raise ValueError("confusion totals do not match the case count")
        for label, recall in (
            (EvidenceRelation.CONTRADICTS, self.contradiction_recall),
            (EvidenceRelation.QUALIFIES, self.qualification_recall),
        ):
            if recall != per_class[label].recall:
                raise ValueError(
                    f"{label.value} recall does not match per-class metrics"
                )
        if self.false_support_rate != _false_support_rate(self.confusion):
            raise ValueError("false-support rate does not match the confusion matrix")
        return self


def _compute_metrics(
    outcomes: Sequence[SemanticBenchmarkCaseOutcome],
) -> SemanticMetrics:
    counts = {
        label: {predicted: 0 for predicted in _PREDICTION_ORDER}
        for label in _LABEL_ORDER
    }
    for outcome in outcomes:
        counts[outcome.adjudicated_label][_predicted_key(outcome.predicted_label)] += 1
    confusion = [
        SemanticConfusionRow(
            adjudicated=label,
            cells=[
                SemanticConfusionCell(
                    predicted=(
                        EvidenceRelation(predicted)
                        if predicted != UNPREDICTED_LABEL
                        else UNPREDICTED_LABEL
                    ),
                    count=counts[label][predicted],
                )
                for predicted in _PREDICTION_ORDER
            ],
        )
        for label in _LABEL_ORDER
    ]
    per_class = []
    for label in _LABEL_ORDER:
        true_positives = counts[label][label.value]
        adjudicated_count = sum(counts[label].values())
        predicted_count = sum(counts[other][label.value] for other in _LABEL_ORDER)
        per_class.append(
            SemanticClassMetrics(
                label=label,
                adjudicated_count=adjudicated_count,
                predicted_count=predicted_count,
                true_positive_count=true_positives,
                precision=(
                    true_positives / predicted_count if predicted_count else None
                ),
                recall=(
                    true_positives / adjudicated_count if adjudicated_count else None
                ),
            )
        )
    recalls = {entry.label: entry.recall for entry in per_class}
    return SemanticMetrics(
        case_count=len(outcomes),
        unpredicted_count=sum(
            outcome.predicted_label == UNPREDICTED_LABEL for outcome in outcomes
        ),
        confusion=confusion,
        per_class=per_class,
        false_support_rate=_false_support_rate(confusion),
        contradiction_recall=recalls[EvidenceRelation.CONTRADICTS],
        qualification_recall=recalls[EvidenceRelation.QUALIFIES],
    )


class SemanticBenchmarkReport(RuntimeModel):
    """Versioned semantic-benchmark outcome; recomputed from case outcomes."""

    format_version: Literal["vibereview-semantic-benchmark-report-1"] = (
        SEMANTIC_BENCHMARK_REPORT_FORMAT_VERSION
    )
    classifier_reference: str | None = Field(
        default=None, min_length=1, max_length=MAX_CLASSIFIER_REFERENCE_CHARS
    )
    false_support_rate_definition: Literal[
        "Among cases whose adjudicated label is not 'supports', the fraction "
        "predicted as 'supports'; unpredicted classifier failures count in the "
        "denominator only; null when no non-support case exists."
    ] = FALSE_SUPPORT_RATE_DEFINITION
    case_count: int = Field(ge=1)
    disagreement_count: int = Field(ge=0)
    disagreement_case_ids: list[str]
    overall: SemanticMetrics
    development: SemanticMetrics
    evaluation: SemanticMetrics
    outcomes: list[SemanticBenchmarkCaseOutcome] = Field(min_length=1)

    @model_validator(mode="after")
    def _report_matches_outcomes(self) -> "SemanticBenchmarkReport":
        case_ids = [outcome.case_id for outcome in self.outcomes]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("report outcomes must have unique case IDs")
        if self.case_count != len(self.outcomes):
            raise ValueError("report case count does not match outcomes")
        disagreement_ids = [
            outcome.case_id
            for outcome in self.outcomes
            if outcome.retained_disagreement
        ]
        if self.disagreement_case_ids != disagreement_ids:
            raise ValueError("disagreement case IDs do not match outcomes")
        if self.disagreement_count != len(disagreement_ids):
            raise ValueError("disagreement count does not match outcomes")
        partitions = {
            "overall": list(self.outcomes),
            "development": [
                outcome
                for outcome in self.outcomes
                if outcome.split == "development"
            ],
            "evaluation": [
                outcome
                for outcome in self.outcomes
                if outcome.split == "evaluation"
            ],
        }
        for name, metrics in (
            ("overall", self.overall),
            ("development", self.development),
            ("evaluation", self.evaluation),
        ):
            if metrics != _compute_metrics(partitions[name]):
                raise ValueError(
                    f"{name} metrics are not recomputed from the case outcomes"
                )
        return self


def run_semantic_benchmark(
    document: SemanticBenchmarkDocument,
    classifier: SemanticClassifier,
    *,
    classifier_reference: str | None = None,
) -> SemanticBenchmarkReport:
    """Score every case with a (claim text, passage text) -> label classifier.

    The runner is engine-agnostic: callers supply the adapter, whether a
    deterministic synthetic classifier in tests or an ASSESS_EVIDENCE engine
    invocation.  A classifier exception or a return value outside the frozen
    ``EvidenceRelation`` vocabulary is recorded as ``unpredicted``; a failed
    prediction is never dropped.  Identical documents and classifiers produce
    byte-identical serialized reports.
    """

    outcomes: list[SemanticBenchmarkCaseOutcome] = []
    for case in document.cases:
        predicted: PredictedRelation = UNPREDICTED_LABEL
        try:
            predicted = EvidenceRelation(classifier(case.claim_text, case.passage_text))
        except Exception:
            predicted = UNPREDICTED_LABEL
        outcomes.append(
            SemanticBenchmarkCaseOutcome(
                case_id=case.case_id,
                split=case.split,
                category=case.category,
                adjudicated_label=case.adjudicated_label,
                predicted_label=predicted,
                retained_disagreement=case.retained_disagreement,
                correct=predicted == case.adjudicated_label,
            )
        )
    disagreement_ids = [
        outcome.case_id for outcome in outcomes if outcome.retained_disagreement
    ]
    return SemanticBenchmarkReport(
        classifier_reference=classifier_reference,
        case_count=len(outcomes),
        disagreement_count=len(disagreement_ids),
        disagreement_case_ids=disagreement_ids,
        overall=_compute_metrics(outcomes),
        development=_compute_metrics(
            [outcome for outcome in outcomes if outcome.split == "development"]
        ),
        evaluation=_compute_metrics(
            [outcome for outcome in outcomes if outcome.split == "evaluation"]
        ),
        outcomes=outcomes,
    )


__all__ = [
    "FALSE_SUPPORT_RATE_DEFINITION",
    "MAX_BENCHMARK_CASES",
    "PredictedRelation",
    "ReviewerLabel",
    "SEMANTIC_BENCHMARK_FORMAT_VERSION",
    "SEMANTIC_BENCHMARK_REPORT_FORMAT_VERSION",
    "SemanticBenchmarkCase",
    "SemanticBenchmarkCaseOutcome",
    "SemanticBenchmarkCategory",
    "SemanticBenchmarkDocument",
    "SemanticBenchmarkReport",
    "SemanticClassMetrics",
    "SemanticClassifier",
    "SemanticConfusionCell",
    "SemanticConfusionRow",
    "SemanticMetrics",
    "UNPREDICTED_LABEL",
    "load_semantic_benchmark",
    "run_semantic_benchmark",
    "save_semantic_benchmark",
]
