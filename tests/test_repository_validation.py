from __future__ import annotations

import pytest

from vibereview.errors import RepositoryValidationError
from vibereview.models import Paper, ThemeRecord
from vibereview.validators import (
    validate_identity_registry,
    validate_repository,
    validate_theme_hierarchy,
)


def _new_paper(base: Paper, *, paper_id: str, doi: str, source_char: str, keys=None):
    data = base.model_dump()
    data.update(
        paper_id=paper_id,
        doi=doi,
        identity_keys=keys or [doi],
        source_hash="sha256:" + source_char * 64,
        raw_md_hash="sha256:" + source_char * 64,
        raw_md_path=f"papers/{paper_id}/raw.md",
    )
    return Paper.model_validate(data)


def test_complete_repository_is_valid(bundle_factory):
    report = validate_repository(**bundle_factory())
    assert report.ok
    assert report.warnings == ()


def test_duplicate_doi_conflict(bundle_factory):
    bundle = bundle_factory()
    base = bundle["papers"][0]
    second = _new_paper(
        base,
        paper_id="P0002",
        doi="https://doi.org/10.1234/EXAMPLE",
        source_char="d",
    )
    with pytest.raises(RepositoryValidationError) as exc_info:
        validate_identity_registry([base, second])
    assert "DUPLICATE_IDENTITY_CONFLICT" in exc_info.value.report.codes()


def test_duplicate_source_sha_conflict(bundle_factory):
    bundle = bundle_factory()
    base = bundle["papers"][0]
    data = base.model_dump()
    data.update(
        paper_id="P0002",
        doi="10.5678/other",
        identity_keys=["doi:10.5678/other"],
        raw_md_hash="sha256:" + "d" * 64,
        raw_md_path="papers/P0002/raw.md",
    )
    second = Paper.model_validate(data)
    with pytest.raises(RepositoryValidationError) as exc_info:
        validate_identity_registry([base, second])
    assert "DUPLICATE_IDENTITY_CONFLICT" in exc_info.value.report.codes()


def test_weak_fingerprint_warning_only(bundle_factory):
    bundle = bundle_factory()
    base = bundle["papers"][0]
    second = _new_paper(
        base,
        paper_id="P0002",
        doi="10.5678/other",
        source_char="d",
        keys=["bib:  Researcher 2025 Preheating  ", "doi:10.5678/other"],
    )
    report = validate_identity_registry([base, second])
    assert report.ok
    assert [issue.code for issue in report.warnings] == ["POSSIBLE_DUPLICATE_WARNING"]


def test_related_publication_must_resolve(bundle_factory):
    base = bundle_factory()["papers"][0]
    paper = Paper.model_validate(
        {**base.model_dump(), "related_publications": ["P9999"]}
    )
    with pytest.raises(RepositoryValidationError) as exc_info:
        validate_identity_registry([paper])
    assert "INVALID_REFERENCE" in exc_info.value.report.codes()


def test_theme_parent_must_exist():
    theme = ThemeRecord(
        theme_id="T0001",
        title="child",
        description="child",
        origin="human",
        parent_theme_id="T9999",
    )
    with pytest.raises(RepositoryValidationError) as exc_info:
        validate_theme_hierarchy([theme])
    assert "INVALID_REFERENCE" in exc_info.value.report.codes()


def test_theme_cannot_parent_itself():
    theme = ThemeRecord(
        theme_id="T0001",
        title="self",
        description="self",
        origin="human",
        parent_theme_id="T0001",
    )
    with pytest.raises(RepositoryValidationError) as exc_info:
        validate_theme_hierarchy([theme])
    assert "THEME_SELF_PARENT" in exc_info.value.report.codes()


def test_theme_hierarchy_cycle():
    first = ThemeRecord(
        theme_id="T0001",
        title="first",
        description="first",
        origin="human",
        parent_theme_id="T0002",
    )
    second = ThemeRecord(
        theme_id="T0002",
        title="second",
        description="second",
        origin="human",
        parent_theme_id="T0001",
    )
    with pytest.raises(RepositoryValidationError) as exc_info:
        validate_theme_hierarchy([first, second])
    assert "THEME_CYCLE" in exc_info.value.report.codes()


def test_repository_orphan_candidate_theme(bundle_factory):
    bundle = bundle_factory()
    bundle["themes"] = []
    with pytest.raises(RepositoryValidationError) as exc_info:
        validate_repository(**bundle)
    assert "INVALID_REFERENCE" in exc_info.value.report.codes()


def test_repository_orphan_corpus_paper(bundle_factory):
    bundle = bundle_factory()
    fact = bundle["corpus_facts"][0]
    bundle["corpus_facts"][0] = fact.model_copy(
        update={"source_paper_ids": ["P9999"]}
    )
    with pytest.raises(RepositoryValidationError) as exc_info:
        validate_repository(**bundle)
    assert "INVALID_REFERENCE" in exc_info.value.report.codes()


def test_repository_aggregates_independent_failures(bundle_factory):
    bundle = bundle_factory()
    bundle["themes"] = []
    bundle["retrieval_dispositions"] = []
    with pytest.raises(RepositoryValidationError) as exc_info:
        validate_repository(**bundle)
    codes = exc_info.value.report.codes()
    assert "INVALID_REFERENCE" in codes
    assert "CARDINALITY_VIOLATION" in codes
    assert "DISCARDED_SPAN_HAS_EVIDENCE" in codes
