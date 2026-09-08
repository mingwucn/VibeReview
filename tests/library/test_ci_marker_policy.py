from pathlib import Path


def test_ordinary_ci_excludes_operator_corpus_and_fetches_complete_history() -> None:
    repository = Path(__file__).resolve().parents[2]
    workflow = (repository / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    assert 'not external_engine and not requires_bwrap and not external_corpus' in workflow
    first_checkout = workflow.index("uses: actions/checkout@v4")
    first_test = workflow.index("name: Run deterministic tests")
    assert "fetch-depth: 0" in workflow[first_checkout:first_test]
