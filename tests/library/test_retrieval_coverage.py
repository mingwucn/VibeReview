from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vibereview.enums import RetrievalIntent
from vibereview.library.git_source import PinnedGitSource, compute_content_sha256
from vibereview.library.inventory import build_library_inventory
from vibereview.library.models import (
    CorpusSelectionDocument,
    CorpusSelectionManifest,
    LibraryConfig,
)
from vibereview.library.retrieval import (
    DeterministicTextRetriever,
    UnifiedRetrievalCoordinator,
    VerifiedCorpus,
)
from vibereview.library.retrieval_coverage import (
    ExecutedRetrievalQuery,
    RetrievalBenchmarkCase,
    RetrievalBenchmarkCategory,
    RetrievalBenchmarkDocument,
    RetrievalCoverageStatus,
    RetrievalCoverageReport,
    RetrievalQueryPlan,
    RetrievalBenchmarkReport,
    build_retrieval_query_plan,
    compute_retrieval_coverage,
    load_retrieval_benchmark,
    run_retrieval_benchmark,
    save_retrieval_benchmark,
)
from vibereview.library.selection import (
    import_selected_corpus as _import_selected_corpus,
)


ALPHA_TEXT = (
    "\\cite{Alpha2024}\n"
    "# Alpha\n"
    "\n"
    "Preheating reduces residual stress in synthetic welded joints.\n"
    "\n"
    "## Methods\n"
    "\n"
    "The thermal conditioning protocol lowered residual stress measurements "
    "across all coupons.\n"
)
BETA_TEXT = (
    "# Beta\n"
    "\n"
    "Preheating does not reduce residual stress; no significant effect was "
    "observed in synthetic coupons.\n"
)
GAMMA_TEXT = (
    "# Gamma\n"
    "\n"
    "Preheating reduces residual stress only within the 200 to 300 degree "
    "Celsius range in synthetic joints.\n"
)


def make_selection(config: LibraryConfig) -> CorpusSelectionManifest:
    inventory, _ = build_library_inventory(PinnedGitSource.open(config), config)
    return CorpusSelectionManifest(
        library_id=config.library_id,
        source_commit=config.expected_commit,
        documents=[
            CorpusSelectionDocument(
                source_relative_path=record.source_relative_path,
                content_sha256=record.content_sha256,
                decision="include",
                role="primary",
                accepted_by="synthetic-test",
                accepted_at="2030-01-01T00:00:00Z",
                reason="fixture coverage",
            )
            for record in inventory
            if record.document_kind.value == "candidate_paper_markdown"
        ],
    )


def import_review(review_root: Path, config: LibraryConfig) -> Path:
    _import_selected_corpus(
        review_root,
        make_selection(config),
        config,
        public_repository_root=review_root.parent / "public-repository",
    )
    return review_root


def repin_library(
    repository: Path, config: LibraryConfig, files: dict[str, bytes]
) -> LibraryConfig:
    for relative_path, content in files.items():
        (repository / relative_path).write_bytes(content)
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "benchmark corpus"],
        check=True,
        capture_output=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        [
            "git",
            "-C",
            str(config.superproject_path),
            "update-index",
            "--cacheinfo",
            f"160000,{commit},{config.gitlink_path}",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(config.superproject_path),
            "commit",
            "-m",
            "advance benchmark corpus pin",
        ],
        check=True,
        capture_output=True,
    )
    return config.model_copy(update={"expected_commit": commit})


@pytest.fixture
def benchmark_review(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> tuple[Path, dict[str, str]]:
    repository, config, _ = synthetic_library
    revised = repin_library(
        repository,
        config,
        {
            "papers/Writer - 2024 - Alpha.md": ALPHA_TEXT.encode("utf-8"),
            "papers/Writer - 2023 - Beta.md": BETA_TEXT.encode("utf-8"),
            "papers/Writer - 2022 - Gamma.md": GAMMA_TEXT.encode("utf-8"),
        },
    )
    review_root = import_review(tmp_path / "review", revised)
    return review_root, {
        "alpha": compute_content_sha256(ALPHA_TEXT.encode("utf-8")),
        "beta": compute_content_sha256(BETA_TEXT.encode("utf-8")),
        "gamma": compute_content_sha256(GAMMA_TEXT.encode("utf-8")),
    }


def benchmark_document(hashes: dict[str, str]) -> RetrievalBenchmarkDocument:
    return RetrievalBenchmarkDocument(
        cases=[
            RetrievalBenchmarkCase(
                case_id="support-1",
                category=RetrievalBenchmarkCategory.SUPPORT,
                intent=RetrievalIntent.SUPPORT,
                query_texts=["preheating reduces residual stress"],
                expected_source_sha256=hashes["alpha"],
                expected_passage=(
                    "Preheating reduces residual stress in synthetic welded joints."
                ),
            ),
            RetrievalBenchmarkCase(
                case_id="contradiction-1",
                category=RetrievalBenchmarkCategory.CONTRADICTION,
                intent=RetrievalIntent.CONTRADICTION,
                query_texts=["preheating does not reduce residual stress"],
                expected_source_sha256=hashes["beta"],
                expected_passage="no significant effect was observed",
            ),
            RetrievalBenchmarkCase(
                case_id="null-1",
                category=RetrievalBenchmarkCategory.NULL_RESULT,
                intent=RetrievalIntent.NULL_RESULT,
                query_texts=["preheating residual stress no significant effect"],
                expected_source_sha256=hashes["beta"],
                expected_passage="no significant effect was observed",
            ),
            RetrievalBenchmarkCase(
                case_id="boundary-1",
                category=RetrievalBenchmarkCategory.BOUNDARY,
                intent=RetrievalIntent.BOUNDARY,
                query_texts=["preheating residual stress range"],
                expected_source_sha256=hashes["gamma"],
                expected_passage="only within the 200 to 300 degree Celsius range",
            ),
            RetrievalBenchmarkCase(
                case_id="variant-1",
                category=RetrievalBenchmarkCategory.TERMINOLOGY_VARIANT,
                intent=RetrievalIntent.SUPPORT,
                query_texts=["thermal conditioning residual stress"],
                expected_source_sha256=hashes["alpha"],
                expected_passage="thermal conditioning protocol lowered residual stress",
            ),
            RetrievalBenchmarkCase(
                case_id="fulltext-1",
                category=RetrievalBenchmarkCategory.FULL_TEXT_ONLY,
                intent=RetrievalIntent.SUPPORT,
                query_texts=["residual stress measurements coupons"],
                expected_source_sha256=hashes["alpha"],
                expected_passage="residual stress measurements across all coupons",
            ),
            RetrievalBenchmarkCase(
                case_id="miss-1",
                category=RetrievalBenchmarkCategory.MECHANISM,
                intent=RetrievalIntent.SUPPORT,
                query_texts=["cryogenic treatment fatigue life"],
                expected_source_sha256=hashes["alpha"],
            ),
        ]
    )


def execute(
    coordinator: UnifiedRetrievalCoordinator,
    query_id: str,
    query_text: str,
    intent: RetrievalIntent,
) -> ExecutedRetrievalQuery:
    _, ledger = coordinator.retrieve(query_text, query_id, intent)
    return ExecutedRetrievalQuery(
        query_id=query_id,
        query_text=query_text,
        intent=intent,
        ledger=ledger,
    )


def test_query_plan_is_deterministic_and_covers_required_intents() -> None:
    plan = build_retrieval_query_plan(
        "operator-claim-1",
        "Preheating reduces residual stress.",
        terminology_variants=["thermal conditioning"],
    )
    repeated = build_retrieval_query_plan(
        "operator-claim-1",
        "Preheating reduces   residual stress.",
        terminology_variants=["thermal conditioning"],
    )

    assert plan.model_dump_json() == repeated.model_dump_json()
    assert [entry.intent for entry in plan.intents] == list(RetrievalIntent)
    for entry in plan.intents:
        assert len(entry.query_texts) == 2
        assert "thermal conditioning" in entry.query_texts[1]
    support = plan.intents[0]
    assert support.query_texts == [
        "Preheating reduces residual stress.",
        "Preheating reduces residual stress. thermal conditioning",
    ]
    assert any(
        "no significant effect" in text
        for text in plan.intents[-1].query_texts
    )
    payload = plan.model_dump(mode="json")
    payload["intents"][0]["query_texts"][0] = "tampered query text"
    with pytest.raises(ValueError, match="hash does not match"):
        RetrievalQueryPlan.model_validate(payload)


def test_query_plan_optional_intents_and_variant_bounds() -> None:
    minimal = build_retrieval_query_plan(
        "operator-claim-2",
        "Preheating reduces residual stress.",
        include_method_challenge=False,
        include_null_result=False,
    )
    assert [entry.intent for entry in minimal.intents] == [
        RetrievalIntent.SUPPORT,
        RetrievalIntent.CONTRADICTION,
        RetrievalIntent.BOUNDARY,
        RetrievalIntent.ALTERNATIVE,
    ]
    assert all(len(entry.query_texts) == 1 for entry in minimal.intents)

    with pytest.raises(ValueError, match="no usable deterministic retrieval terms"):
        build_retrieval_query_plan("operator-claim-3", "a an of to")
    with pytest.raises(ValueError, match="terminology-variant budget"):
        build_retrieval_query_plan(
            "operator-claim-4",
            "Preheating reduces residual stress.",
            terminology_variants=[f"variant {index}" for index in range(17)],
        )
    with pytest.raises(ValueError, match="unique"):
        build_retrieval_query_plan(
            "operator-claim-5",
            "Preheating reduces residual stress.",
            terminology_variants=["dup", "dup"],
        )


def test_query_plan_rejects_wrong_format_version() -> None:
    payload = build_retrieval_query_plan(
        "operator-claim-6", "Preheating reduces residual stress."
    ).model_dump(mode="json")
    payload["format_version"] = "vibereview-retrieval-query-plan-0"
    with pytest.raises(ValueError):
        RetrievalQueryPlan.model_validate(payload)


def test_coverage_report_full_required_intents(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    _, config, _ = synthetic_library
    review_root = import_review(tmp_path / "review", config)
    coordinator = UnifiedRetrievalCoordinator.from_generation(review_root)
    entries = [
        execute(
            coordinator, "Q-C0001-SUP-01", "lattice signal", RetrievalIntent.SUPPORT
        ),
        execute(
            coordinator,
            "Q-C0001-CON-01",
            "lattice signal contradicts",
            RetrievalIntent.CONTRADICTION,
        ),
        execute(
            coordinator,
            "Q-C0001-BND-01",
            "lattice signal range",
            RetrievalIntent.BOUNDARY,
        ),
        execute(
            coordinator,
            "Q-C0001-ALT-01",
            "lattice signal alternative",
            RetrievalIntent.ALTERNATIVE,
        ),
    ]

    report = compute_retrieval_coverage(
        entries,
        status=RetrievalCoverageStatus.ADEQUATE_FOR_SYNTHESIS,
        assessor="synthetic-operator",
        assessed_at="2030-01-01T00:00:00Z",
    )

    assert report.status is RetrievalCoverageStatus.ADEQUATE_FOR_SYNTHESIS
    assert report.format_version == "vibereview-retrieval-coverage-1"
    claim = report.claims[0]
    assert claim.claim_id == "C0001"
    assert claim.executed_intents == [
        RetrievalIntent.SUPPORT,
        RetrievalIntent.CONTRADICTION,
        RetrievalIntent.BOUNDARY,
        RetrievalIntent.ALTERNATIVE,
    ]
    assert claim.missing_required_intents == []
    assert claim.distinct_query_count == 4
    assert claim.terminology_diversity == 4
    assert claim.selected_candidate_count == 4
    assert claim.distinct_paper_count == 1
    # All four queries select the same Alpha paragraph.
    assert claim.duplicate_result_rate == 0.75
    assert report.aggregate.claim_count == 1
    assert report.aggregate.executed_query_count == 4
    assert report.aggregate.duplicate_result_rate == 0.75
    assert report.aggregate.claims_missing_required_intents == []
    assert [entry.intent for entry in report.aggregate.per_intent] == list(
        claim.executed_intents
    )

    payload = report.model_dump(mode="json")
    payload["aggregate"]["executed_query_count"] += 1
    with pytest.raises(ValueError, match="aggregate query count"):
        RetrievalCoverageReport.model_validate(payload)


def test_coverage_report_flags_missing_required_intents(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    _, config, _ = synthetic_library
    review_root = import_review(tmp_path / "review", config)
    coordinator = UnifiedRetrievalCoordinator.from_generation(review_root)
    entries = [
        execute(
            coordinator, "Q-C0001-SUP-01", "lattice signal", RetrievalIntent.SUPPORT
        )
    ]

    report = compute_retrieval_coverage(
        entries, status=RetrievalCoverageStatus.INADEQUATE
    )

    claim = report.claims[0]
    assert claim.missing_required_intents == [
        RetrievalIntent.CONTRADICTION,
        RetrievalIntent.BOUNDARY,
        RetrievalIntent.ALTERNATIVE,
    ]
    assert report.aggregate.claims_missing_required_intents == ["C0001"]
    assert report.status is RetrievalCoverageStatus.INADEQUATE


def test_coverage_report_duplicate_heavy_multi_claim(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    _, config, _ = synthetic_library
    review_root = import_review(tmp_path / "review", config)
    coordinator = UnifiedRetrievalCoordinator.from_generation(review_root)
    entries = [
        execute(
            coordinator, "Q-C0001-SUP-01", "lattice signal", RetrievalIntent.SUPPORT
        ),
        execute(
            coordinator,
            "Q-C0001-SUP-02",
            "signal changes load",
            RetrievalIntent.SUPPORT,
        ),
        execute(
            coordinator, "Q-C0002-SUP-01", "lattice signal", RetrievalIntent.SUPPORT
        ),
    ]

    report = compute_retrieval_coverage(
        entries, status=RetrievalCoverageStatus.PARTIAL
    )

    first, second = report.claims
    assert first.claim_id == "C0001"
    # Both C0001 queries select the identical Alpha paragraph.
    assert first.selected_candidate_count == 2
    assert first.distinct_paper_count == 1
    assert first.duplicate_result_rate == 0.5
    assert first.terminology_diversity == 2
    assert second.claim_id == "C0002"
    assert second.duplicate_result_rate == 0.0
    # All three selected hits across both claims are the same paragraph.
    assert report.aggregate.selected_candidate_count == 3
    assert report.aggregate.distinct_paper_count == 1
    assert report.aggregate.duplicate_result_rate == pytest.approx(2 / 3)
    assert report.aggregate.executed_query_count == 3
    assert report.aggregate.claims_missing_required_intents == ["C0001", "C0002"]


def test_coverage_rejects_inconsistent_entries(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    _, config, _ = synthetic_library
    review_root = import_review(tmp_path / "review", config)
    coordinator = UnifiedRetrievalCoordinator.from_generation(review_root)
    _, ledger = coordinator.retrieve(
        "lattice signal", "Q-C0001-SUP-01", RetrievalIntent.SUPPORT
    )
    _, boundary_ledger = coordinator.retrieve(
        "lattice signal range", "Q-C0001-BND-01", RetrievalIntent.BOUNDARY
    )

    with pytest.raises(ValueError, match="text hash"):
        ExecutedRetrievalQuery(
            query_id="Q-C0001-SUP-01",
            query_text="different text",
            intent=RetrievalIntent.SUPPORT,
            ledger=ledger,
        )
    with pytest.raises(ValueError, match="intent"):
        ExecutedRetrievalQuery(
            query_id="Q-C0001-SUP-01",
            query_text="lattice signal",
            intent=RetrievalIntent.CONTRADICTION,
            ledger=ledger,
        )

    entry = ExecutedRetrievalQuery(
        query_id="Q-C0001-SUP-01",
        query_text="lattice signal",
        intent=RetrievalIntent.SUPPORT,
        ledger=ledger,
    )
    boundary_entry = ExecutedRetrievalQuery(
        query_id="Q-C0001-BND-01",
        query_text="lattice signal range",
        intent=RetrievalIntent.BOUNDARY,
        ledger=boundary_ledger,
    )
    with pytest.raises(ValueError, match="unique query IDs"):
        compute_retrieval_coverage(
            [entry, entry], status=RetrievalCoverageStatus.PARTIAL
        )
    tampered = boundary_entry.model_copy(
        update={
            "ledger": boundary_ledger.model_copy(
                update={"corpus_lock_hash": "sha256:" + "1" * 64}
            )
        }
    )
    with pytest.raises(ValueError, match="single corpus lock hash"):
        compute_retrieval_coverage(
            [entry, tampered], status=RetrievalCoverageStatus.PARTIAL
        )
    with pytest.raises(ValueError, match="at least one"):
        compute_retrieval_coverage([], status=RetrievalCoverageStatus.PARTIAL)


def test_benchmark_measures_perfect_recall_and_one_miss(
    benchmark_review: tuple[Path, dict[str, str]],
) -> None:
    review_root, hashes = benchmark_review
    document = benchmark_document(hashes)
    corpus = VerifiedCorpus(review_root)

    report = run_retrieval_benchmark(document, corpus)

    assert report.format_version == "vibereview-retrieval-benchmark-report-1"
    assert report.case_count == 7
    assert report.paper_hit_count == 6
    assert report.known_paper_recall == pytest.approx(6 / 7)
    assert report.passage_case_count == 6
    assert report.passage_hit_count == 6
    assert report.known_passage_recall == 1.0
    by_case = {case.case_id: case for case in report.cases}
    assert by_case["miss-1"].paper_hit is False
    assert by_case["miss-1"].selected_candidate_count == 0
    assert by_case["miss-1"].passage_hit is None
    for case_id in (
        "support-1",
        "contradiction-1",
        "null-1",
        "boundary-1",
        "variant-1",
        "fulltext-1",
    ):
        assert by_case[case_id].paper_hit is True
        assert by_case[case_id].passage_hit is True
    by_category = {metrics.category: metrics for metrics in report.per_category}
    assert by_category[RetrievalBenchmarkCategory.MECHANISM].paper_recall == 0.0
    assert (
        by_category[RetrievalBenchmarkCategory.TERMINOLOGY_VARIANT].paper_recall
        == 1.0
    )
    assert (
        by_category[RetrievalBenchmarkCategory.FULL_TEXT_ONLY].passage_recall == 1.0
    )
    assert (
        by_category[RetrievalBenchmarkCategory.MECHANISM].passage_recall is None
    )

    # A caller-supplied coordinator over the same corpus yields identical bytes.
    coordinator = UnifiedRetrievalCoordinator(DeterministicTextRetriever(review_root))
    repeated = run_retrieval_benchmark(document, corpus, coordinator=coordinator)
    assert repeated.model_dump_json() == report.model_dump_json()


def test_benchmark_measures_passage_misses(
    benchmark_review: tuple[Path, dict[str, str]],
) -> None:
    review_root, hashes = benchmark_review
    document = RetrievalBenchmarkDocument(
        cases=[
            RetrievalBenchmarkCase(
                case_id="passage-hit",
                category=RetrievalBenchmarkCategory.SUPPORT,
                intent=RetrievalIntent.SUPPORT,
                query_texts=["preheating reduces residual stress"],
                expected_source_sha256=hashes["alpha"],
                expected_passage="residual stress in synthetic welded joints",
            ),
            RetrievalBenchmarkCase(
                case_id="passage-miss",
                category=RetrievalBenchmarkCategory.SUPPORT,
                intent=RetrievalIntent.SUPPORT,
                query_texts=["preheating reduces residual stress"],
                expected_source_sha256=hashes["alpha"],
                expected_passage="residual stress vanished entirely",
            ),
        ]
    )

    report = run_retrieval_benchmark(document, VerifiedCorpus(review_root))

    assert report.known_paper_recall == 1.0
    assert report.passage_case_count == 2
    assert report.passage_hit_count == 1
    assert report.known_passage_recall == 0.5
    by_case = {case.case_id: case for case in report.cases}
    assert by_case["passage-miss"].paper_hit is True
    assert by_case["passage-miss"].passage_hit is False


def test_benchmark_round_trips_and_fails_closed(
    benchmark_review: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    review_root, hashes = benchmark_review
    document = benchmark_document(hashes)
    path = tmp_path / "benchmark.json"
    save_retrieval_benchmark(document, path)
    assert load_retrieval_benchmark(path) == document

    unknown = RetrievalBenchmarkDocument(
        cases=[
            RetrievalBenchmarkCase(
                case_id="unknown-paper",
                category=RetrievalBenchmarkCategory.SUPPORT,
                intent=RetrievalIntent.SUPPORT,
                query_texts=["lattice signal"],
                expected_source_sha256="sha256:" + "0" * 64,
            )
        ]
    )
    with pytest.raises(ValueError, match="absent from the corpus lock"):
        run_retrieval_benchmark(unknown, VerifiedCorpus(review_root))

    with pytest.raises(ValueError, match="unique"):
        RetrievalBenchmarkDocument(
            cases=[
                RetrievalBenchmarkCase(
                    case_id="dup",
                    category=RetrievalBenchmarkCategory.SUPPORT,
                    intent=RetrievalIntent.SUPPORT,
                    query_texts=["lattice signal"],
                    expected_source_sha256=hashes["alpha"],
                ),
                RetrievalBenchmarkCase(
                    case_id="dup",
                    category=RetrievalBenchmarkCategory.SUPPORT,
                    intent=RetrievalIntent.SUPPORT,
                    query_texts=["lattice signal"],
                    expected_source_sha256=hashes["alpha"],
                ),
            ]
        )

    payload = document.model_dump(mode="json")
    payload["format_version"] = "vibereview-retrieval-benchmark-0"
    with pytest.raises(ValueError):
        RetrievalBenchmarkDocument.model_validate(payload)

    report = run_retrieval_benchmark(document, VerifiedCorpus(review_root))
    report_payload = report.model_dump(mode="json")
    report_payload["known_paper_recall"] = 1.0
    with pytest.raises(ValueError, match="known-paper recall"):
        RetrievalBenchmarkReport.model_validate(report_payload)
