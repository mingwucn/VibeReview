from __future__ import annotations

from pathlib import Path

import pytest

from vibereview.enums import EvidenceRelation
from vibereview.library.semantic_benchmark import (
    SEMANTIC_BENCHMARK_FORMAT_VERSION,
    SEMANTIC_BENCHMARK_REPORT_FORMAT_VERSION,
    UNPREDICTED_LABEL,
    ReviewerLabel,
    SemanticBenchmarkCase,
    SemanticBenchmarkCategory,
    SemanticBenchmarkDocument,
    SemanticBenchmarkReport,
    load_semantic_benchmark,
    run_semantic_benchmark,
    save_semantic_benchmark,
)


def make_case(
    case_id: str,
    adjudicated: EvidenceRelation,
    predicted_marker: str,
    split: str,
    category: SemanticBenchmarkCategory,
    *,
    reviewer_labels: list[ReviewerLabel] | None = None,
    retained_disagreement: bool = False,
    source_key: str | None = None,
) -> SemanticBenchmarkCase:
    return SemanticBenchmarkCase(
        case_id=case_id,
        claim_text=f"Synthetic claim underlying case {case_id}.",
        passage_text=(
            f"[{predicted_marker}] Synthetic passage adjudicated as "
            f"{adjudicated.value} for case {case_id}."
        ),
        source_key=source_key,
        adjudicated_label=adjudicated,
        reviewer_labels=reviewer_labels or [],
        retained_disagreement=retained_disagreement,
        split=split,
        category=category,
    )


def marker_classifier(claim_text: str, passage_text: str) -> str:
    """Deterministic synthetic classifier reading a leading [marker] tag."""

    marker = passage_text.split("]", 1)[0].removeprefix("[")
    if marker == "error":
        raise RuntimeError("synthetic classifier failure")
    return marker


@pytest.fixture
def benchmark_document() -> SemanticBenchmarkDocument:
    """Hand-computed fixture covering every label and both splits.

    Development partition (4 cases):
      dev-support-correct     supports    -> supports
      dev-support-missed      supports    -> contextual
      dev-contradiction-flag  contradicts -> supports   (reviewer disagreement)
      dev-qualify-correct     qualifies   -> qualifies
    Evaluation partition (6 cases):
      eval-contradict-correct contradicts -> contradicts
      eval-qualify-missed     qualifies   -> contradicts
      eval-context-false-sup  contextual  -> supports
      eval-unclear-correct    unclear     -> unclear
      eval-null-unpredicted   unclear     -> classifier raises
      eval-support-correct    supports    -> supports
    """

    return SemanticBenchmarkDocument(
        cases=[
            make_case(
                "dev-support-correct",
                EvidenceRelation.SUPPORTS,
                "supports",
                "development",
                SemanticBenchmarkCategory.SUPPORT,
                reviewer_labels=[
                    ReviewerLabel(reviewer="reviewer-a", label=EvidenceRelation.SUPPORTS),
                    ReviewerLabel(reviewer="reviewer-b", label=EvidenceRelation.SUPPORTS),
                ],
                source_key="synthetic-paper-alpha",
            ),
            make_case(
                "dev-support-missed",
                EvidenceRelation.SUPPORTS,
                "contextual",
                "development",
                SemanticBenchmarkCategory.PARTIAL_SUPPORT,
            ),
            make_case(
                "dev-contradiction-flag",
                EvidenceRelation.CONTRADICTS,
                "supports",
                "development",
                SemanticBenchmarkCategory.NEGATION,
                reviewer_labels=[
                    ReviewerLabel(reviewer="reviewer-a", label=EvidenceRelation.SUPPORTS),
                    ReviewerLabel(
                        reviewer="reviewer-b", label=EvidenceRelation.CONTRADICTS
                    ),
                ],
                retained_disagreement=True,
            ),
            make_case(
                "dev-qualify-correct",
                EvidenceRelation.QUALIFIES,
                "qualifies",
                "development",
                SemanticBenchmarkCategory.QUALIFICATION,
            ),
            make_case(
                "eval-contradict-correct",
                EvidenceRelation.CONTRADICTS,
                "contradicts",
                "evaluation",
                SemanticBenchmarkCategory.CONTRADICTION,
            ),
            make_case(
                "eval-qualify-missed",
                EvidenceRelation.QUALIFIES,
                "contradicts",
                "evaluation",
                SemanticBenchmarkCategory.BOUNDARY,
            ),
            make_case(
                "eval-context-false-sup",
                EvidenceRelation.CONTEXTUAL,
                "supports",
                "evaluation",
                SemanticBenchmarkCategory.CONTEXT,
            ),
            make_case(
                "eval-unclear-correct",
                EvidenceRelation.UNCLEAR,
                "unclear",
                "evaluation",
                SemanticBenchmarkCategory.UNCLEAR,
            ),
            make_case(
                "eval-null-unpredicted",
                EvidenceRelation.UNCLEAR,
                "error",
                "evaluation",
                SemanticBenchmarkCategory.NULL_RESULT,
            ),
            make_case(
                "eval-support-correct",
                EvidenceRelation.SUPPORTS,
                "supports",
                "evaluation",
                SemanticBenchmarkCategory.METHOD_DEPENDENT,
            ),
        ]
    )


def test_report_computes_hand_computed_metrics(
    benchmark_document: SemanticBenchmarkDocument,
) -> None:
    report = run_semantic_benchmark(
        benchmark_document,
        marker_classifier,
        classifier_reference="synthetic-marker-classifier",
    )

    assert report.format_version == SEMANTIC_BENCHMARK_REPORT_FORMAT_VERSION
    assert report.classifier_reference == "synthetic-marker-classifier"
    assert report.case_count == 10

    overall = report.overall
    assert overall.case_count == 10
    assert overall.unpredicted_count == 1

    confusion = {
        (row.adjudicated, cell.predicted): cell.count
        for row in overall.confusion
        for cell in row.cells
    }
    # supports row: dev-support-correct, eval-support-correct -> supports;
    # dev-support-missed -> contextual.
    assert confusion[(EvidenceRelation.SUPPORTS, EvidenceRelation.SUPPORTS)] == 2
    assert confusion[(EvidenceRelation.SUPPORTS, EvidenceRelation.CONTEXTUAL)] == 1
    # contradicts row: dev-contradiction-flag -> supports;
    # eval-contradict-correct -> contradicts.
    assert confusion[(EvidenceRelation.CONTRADICTS, EvidenceRelation.SUPPORTS)] == 1
    assert confusion[(EvidenceRelation.CONTRADICTS, EvidenceRelation.CONTRADICTS)] == 1
    # qualifies row: dev-qualify-correct -> qualifies;
    # eval-qualify-missed -> contradicts.
    assert confusion[(EvidenceRelation.QUALIFIES, EvidenceRelation.QUALIFIES)] == 1
    assert confusion[(EvidenceRelation.QUALIFIES, EvidenceRelation.CONTRADICTS)] == 1
    # contextual row: eval-context-false-sup -> supports.
    assert confusion[(EvidenceRelation.CONTEXTUAL, EvidenceRelation.SUPPORTS)] == 1
    # unclear row: eval-unclear-correct -> unclear;
    # eval-null-unpredicted -> unpredicted.
    assert confusion[(EvidenceRelation.UNCLEAR, EvidenceRelation.UNCLEAR)] == 1
    assert confusion[(EvidenceRelation.UNCLEAR, UNPREDICTED_LABEL)] == 1
    assert sum(confusion.values()) == 10

    per_class = {entry.label: entry for entry in overall.per_class}
    supports = per_class[EvidenceRelation.SUPPORTS]
    assert supports.adjudicated_count == 3
    assert supports.predicted_count == 4
    assert supports.true_positive_count == 2
    assert supports.precision == 0.5
    assert supports.recall == pytest.approx(2 / 3)
    contradicts = per_class[EvidenceRelation.CONTRADICTS]
    assert contradicts.adjudicated_count == 2
    assert contradicts.predicted_count == 2
    assert contradicts.true_positive_count == 1
    assert contradicts.precision == 0.5
    assert contradicts.recall == 0.5
    qualifies = per_class[EvidenceRelation.QUALIFIES]
    assert qualifies.precision == 1.0
    assert qualifies.recall == 0.5
    contextual = per_class[EvidenceRelation.CONTEXTUAL]
    assert contextual.precision == 0.0
    assert contextual.recall == 0.0
    unclear = per_class[EvidenceRelation.UNCLEAR]
    assert unclear.precision == 1.0
    assert unclear.recall == 0.5

    # False-support rate: 7 non-support cases, 2 predicted as supports.
    assert overall.false_support_rate == pytest.approx(2 / 7)
    assert overall.contradiction_recall == 0.5
    assert overall.qualification_recall == 0.5


def test_report_is_split_aware(
    benchmark_document: SemanticBenchmarkDocument,
) -> None:
    report = run_semantic_benchmark(benchmark_document, marker_classifier)

    development = report.development
    assert development.case_count == 4
    assert development.unpredicted_count == 0
    # Non-support development cases: dev-contradiction-flag (predicted
    # supports) and dev-qualify-correct (predicted qualifies).
    assert development.false_support_rate == 0.5
    assert development.contradiction_recall == 0.0
    assert development.qualification_recall == 1.0

    evaluation = report.evaluation
    assert evaluation.case_count == 6
    assert evaluation.unpredicted_count == 1
    # Non-support evaluation cases: five; only eval-context-false-sup is
    # predicted as supports.
    assert evaluation.false_support_rate == 0.2
    assert evaluation.contradiction_recall == 1.0
    assert evaluation.qualification_recall == 0.0

    assert (
        development.case_count + evaluation.case_count == report.overall.case_count
    )


def test_classifier_failures_fall_into_unpredicted_bucket(
    benchmark_document: SemanticBenchmarkDocument,
) -> None:
    def flaky_classifier(claim_text: str, passage_text: str) -> str:
        if passage_text.startswith("[error]"):
            raise RuntimeError("synthetic failure")
        if passage_text.startswith("[unclear]"):
            return "not-a-relation"
        return passage_text.split("]", 1)[0].removeprefix("[")

    report = run_semantic_benchmark(benchmark_document, flaky_classifier)

    by_case = {outcome.case_id: outcome for outcome in report.outcomes}
    raised = by_case["eval-null-unpredicted"]
    assert raised.predicted_label == UNPREDICTED_LABEL
    assert raised.correct is False
    invalid = by_case["eval-unclear-correct"]
    assert invalid.predicted_label == UNPREDICTED_LABEL
    assert invalid.correct is False
    assert report.overall.unpredicted_count == 2
    unclear_row = next(
        row
        for row in report.overall.confusion
        if row.adjudicated is EvidenceRelation.UNCLEAR
    )
    unpredicted_cell = next(
        cell for cell in unclear_row.cells if cell.predicted == UNPREDICTED_LABEL
    )
    assert unpredicted_cell.count == 2


def test_disagreement_is_visible_and_never_erased(
    benchmark_document: SemanticBenchmarkDocument,
) -> None:
    report = run_semantic_benchmark(benchmark_document, marker_classifier)

    assert report.disagreement_count == 1
    assert report.disagreement_case_ids == ["dev-contradiction-flag"]
    outcome = next(
        outcome
        for outcome in report.outcomes
        if outcome.case_id == "dev-contradiction-flag"
    )
    assert outcome.retained_disagreement is True


def test_runner_is_deterministic(
    benchmark_document: SemanticBenchmarkDocument,
) -> None:
    first = run_semantic_benchmark(benchmark_document, marker_classifier)
    second = run_semantic_benchmark(benchmark_document, marker_classifier)
    assert first.model_dump_json() == second.model_dump_json()


def test_document_load_save_round_trip(
    benchmark_document: SemanticBenchmarkDocument, tmp_path: Path
) -> None:
    path = tmp_path / "semantic-benchmark.json"
    save_semantic_benchmark(benchmark_document, path)
    assert load_semantic_benchmark(path) == benchmark_document
    save_semantic_benchmark(benchmark_document, path)
    assert path.read_bytes() == (
        benchmark_document.model_dump_json(indent=2) + "\n"
    ).encode("utf-8")


def test_load_rejects_missing_and_oversized_files(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_semantic_benchmark(tmp_path / "absent.json")


def test_duplicate_case_ids_are_rejected() -> None:
    cases = [
        make_case(
            "duplicate",
            EvidenceRelation.SUPPORTS,
            "supports",
            "development",
            SemanticBenchmarkCategory.SUPPORT,
        ),
        make_case(
            "duplicate",
            EvidenceRelation.CONTRADICTS,
            "contradicts",
            "evaluation",
            SemanticBenchmarkCategory.CONTRADICTION,
        ),
    ]
    with pytest.raises(ValueError, match="unique"):
        SemanticBenchmarkDocument(cases=cases)


def test_document_requires_at_least_one_case_and_a_split() -> None:
    with pytest.raises(ValueError):
        SemanticBenchmarkDocument(cases=[])
    payload = make_case(
        "no-split",
        EvidenceRelation.SUPPORTS,
        "supports",
        "development",
        SemanticBenchmarkCategory.SUPPORT,
    ).model_dump(mode="json")
    del payload["split"]
    with pytest.raises(ValueError):
        SemanticBenchmarkCase.model_validate(payload)


def test_wrong_format_version_is_rejected(
    benchmark_document: SemanticBenchmarkDocument,
) -> None:
    payload = benchmark_document.model_dump(mode="json")
    payload["format_version"] = "vibereview-semantic-benchmark-0"
    with pytest.raises(ValueError):
        SemanticBenchmarkDocument.model_validate(payload)
    assert benchmark_document.format_version == SEMANTIC_BENCHMARK_FORMAT_VERSION


def test_disagreement_flag_must_match_its_derivation() -> None:
    base = make_case(
        "disagreement",
        EvidenceRelation.CONTRADICTS,
        "contradicts",
        "evaluation",
        SemanticBenchmarkCategory.NEGATION,
    ).model_dump(mode="json")

    flagged_without_cause = dict(
        base,
        retained_disagreement=True,
        reviewer_labels=[{"reviewer": "reviewer-a", "label": "contradicts"}],
    )
    with pytest.raises(ValueError, match="retained_disagreement"):
        SemanticBenchmarkCase.model_validate(flagged_without_cause)

    unflagged_with_cause = dict(
        base,
        retained_disagreement=False,
        reviewer_labels=[{"reviewer": "reviewer-a", "label": "supports"}],
    )
    with pytest.raises(ValueError, match="retained_disagreement"):
        SemanticBenchmarkCase.model_validate(unflagged_with_cause)

    reviewers_disagree = dict(
        base,
        retained_disagreement=True,
        reviewer_labels=[
            {"reviewer": "reviewer-a", "label": "contradicts"},
            {"reviewer": "reviewer-b", "label": "unclear"},
        ],
    )
    case = SemanticBenchmarkCase.model_validate(reviewers_disagree)
    assert case.retained_disagreement is True

    duplicate_reviewers = dict(
        base,
        reviewer_labels=[
            {"reviewer": "reviewer-a", "label": "contradicts"},
            {"reviewer": "reviewer-a", "label": "unclear"},
        ],
        retained_disagreement=True,
    )
    with pytest.raises(ValueError, match="unique reviewers"):
        SemanticBenchmarkCase.model_validate(duplicate_reviewers)


def test_report_rejects_tampered_aggregates(
    benchmark_document: SemanticBenchmarkDocument,
) -> None:
    report = run_semantic_benchmark(benchmark_document, marker_classifier)

    false_support = report.model_dump(mode="json")
    false_support["overall"]["false_support_rate"] = 0.0
    with pytest.raises(ValueError, match="false-support rate"):
        SemanticBenchmarkReport.model_validate(false_support)

    confusion = report.model_dump(mode="json")
    confusion["overall"]["confusion"][0]["cells"][0]["count"] += 1
    with pytest.raises(ValueError, match="confusion"):
        SemanticBenchmarkReport.model_validate(confusion)

    recall = report.model_dump(mode="json")
    recall["overall"]["contradiction_recall"] = 1.0
    with pytest.raises(ValueError, match="contradicts recall"):
        SemanticBenchmarkReport.model_validate(recall)

    disagreement = report.model_dump(mode="json")
    disagreement["disagreement_count"] = 0
    with pytest.raises(ValueError, match="disagreement count"):
        SemanticBenchmarkReport.model_validate(disagreement)

    outcome = report.model_dump(mode="json")
    outcome["outcomes"][0]["predicted_label"] = "unclear"
    with pytest.raises(ValueError, match="correctness"):
        SemanticBenchmarkReport.model_validate(outcome)

    recomputed = report.model_dump(mode="json")
    recomputed["outcomes"][0]["predicted_label"] = "unclear"
    recomputed["outcomes"][0]["correct"] = False
    with pytest.raises(ValueError, match="not recomputed"):
        SemanticBenchmarkReport.model_validate(recomputed)

    split = report.model_dump(mode="json")
    split["development"]["case_count"] = 3
    with pytest.raises(ValueError, match="confusion totals"):
        SemanticBenchmarkReport.model_validate(split)


def test_report_round_trips_through_json(
    benchmark_document: SemanticBenchmarkDocument,
) -> None:
    report = run_semantic_benchmark(benchmark_document, marker_classifier)
    restored = SemanticBenchmarkReport.model_validate_json(report.model_dump_json())
    assert restored == report


def test_single_split_document_yields_empty_other_partition() -> None:
    document = SemanticBenchmarkDocument(
        cases=[
            make_case(
                "dev-only",
                EvidenceRelation.SUPPORTS,
                "supports",
                "development",
                SemanticBenchmarkCategory.SUPPORT,
            )
        ]
    )
    report = run_semantic_benchmark(document, marker_classifier)
    assert report.development.case_count == 1
    assert report.development.false_support_rate is None
    assert report.evaluation.case_count == 0
    assert report.evaluation.false_support_rate is None
    assert report.evaluation.contradiction_recall is None
    assert report.overall.contradiction_recall is None
