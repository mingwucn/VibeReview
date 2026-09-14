from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from vibereview.library.git_source import PinnedGitSource
from vibereview.library.inventory import build_library_inventory
from vibereview.library.models import (
    CorpusIntegrityError,
    CorpusSelectionDocument,
    LibraryConfig,
)
from vibereview.library.source_quality import (
    SOURCE_QUALITY_FORMAT_VERSION,
    SourceQualityClassification,
    SourceQualityDocument,
    SourceQualityRecord,
    assess_source_structure,
    diagnose_source_bytes,
    load_source_quality_document,
    save_source_quality_document,
    validate_selection_source_quality,
)

from test_corpus_import import make_selection


def make_record(
    sha: str,
    classification: SourceQualityClassification = SourceQualityClassification.READABLE,
) -> SourceQualityRecord:
    return SourceQualityRecord(
        content_sha256=sha,
        classification=classification,
        assessor="synthetic-reviewer",
        assessed_at="2030-01-01T00:00:00Z",
        rationale="synthetic rationale",
    )


def test_quality_document_round_trip_and_strict_parsing(tmp_path: Path) -> None:
    document = SourceQualityDocument(records=[make_record("sha256:" + "a" * 64)])
    assert document.format_version == SOURCE_QUALITY_FORMAT_VERSION
    path = tmp_path / "quality.json"
    save_source_quality_document(document, path)
    assert load_source_quality_document(path) == document

    with pytest.raises(ValidationError):
        SourceQualityDocument(
            records=[
                make_record("sha256:" + "a" * 64),
                make_record(
                    "sha256:" + "a" * 64,
                    SourceQualityClassification.UNUSABLE,
                ),
            ]
        )
    with pytest.raises(ValidationError):
        SourceQualityDocument.model_validate(
            {"format_version": "vibereview-source-quality-0", "records": []}
        )
    with pytest.raises(ValidationError):
        SourceQualityRecord(
            content_sha256="sha256:" + "b" * 64,
            classification="SOMEWHAT_READABLE",
            assessor="synthetic-reviewer",
            assessed_at="2030-01-01T00:00:00Z",
            rationale="synthetic rationale",
        )
    with pytest.raises(ValidationError):
        SourceQualityRecord(
            content_sha256="sha256:" + "b" * 64,
            classification=SourceQualityClassification.READABLE,
            assessor="synthetic-reviewer",
            assessed_at="not-a-timestamp",
            rationale="synthetic rationale",
        )
    with pytest.raises(ValidationError):
        SourceQualityRecord(
            content_sha256="sha256:" + "c" * 64,
            classification=SourceQualityClassification.READABLE,
            assessor="synthetic-reviewer",
            assessed_at="2030-01-01T00:00:00Z",
            rationale="synthetic rationale",
            unexpected="field",
        )
    with pytest.raises(FileNotFoundError):
        load_source_quality_document(tmp_path / "missing.json")


def test_selection_quality_validation_passes_complete_acceptable_coverage(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    _, config, _ = synthetic_library
    manifest = make_selection(config)
    document = SourceQualityDocument(
        records=[
            make_record(selected.content_sha256) for selected in manifest.documents
        ]
    )
    assert validate_selection_source_quality(manifest, document) == []


def test_selection_quality_validation_requires_a_record_per_included_source(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    _, config, _ = synthetic_library
    manifest = make_selection(config)
    included = [item for item in manifest.documents if item.decision == "include"]
    document = SourceQualityDocument(
        records=[make_record(included[0].content_sha256)]
    )
    problems = validate_selection_source_quality(manifest, document)
    assert len(problems) == 1
    assert "lacks a source-quality record" in problems[0]
    assert included[1].source_relative_path in problems[0]


@pytest.mark.parametrize(
    "classification",
    [
        SourceQualityClassification.MATERIAL_EXTRACTION_PROBLEM,
        SourceQualityClassification.UNUSABLE,
    ],
)
def test_blocking_classification_on_included_source_is_a_blocking_problem(
    classification: SourceQualityClassification,
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    _, config, _ = synthetic_library
    manifest = make_selection(config)
    included = [item for item in manifest.documents if item.decision == "include"]
    document = SourceQualityDocument(
        records=[
            make_record(included[0].content_sha256, classification),
            make_record(included[1].content_sha256),
        ]
    )
    problems = validate_selection_source_quality(manifest, document)
    assert len(problems) == 1
    assert classification.value in problems[0]
    assert included[0].source_relative_path in problems[0]


def test_excluded_sources_need_no_quality_record(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    _, config, _ = synthetic_library
    manifest = make_selection(config)
    documents = [
        manifest.documents[0].model_copy(update={"decision": "exclude"}),
        manifest.documents[1],
    ]
    manifest = manifest.model_copy(update={"documents": documents})
    document = SourceQualityDocument(
        records=[make_record(manifest.documents[1].content_sha256)]
    )
    assert validate_selection_source_quality(manifest, document) == []


def test_diagnostics_flag_garbled_and_marker_dense_content() -> None:
    sha = "sha256:" + "d" * 64
    clean = (
        "# Synthetic Title\n\n## Section\n\n" + "A clear synthetic sentence. " * 40
    ).encode()
    diagnostics = diagnose_source_bytes("papers/Clean.md", sha, clean)
    assert diagnostics.decodable_utf8 is True
    assert diagnostics.has_title_line is True
    assert diagnostics.heading_count == 2
    assert diagnostics.replacement_character_count == 0
    assert diagnostics.warnings == []

    garbled = ("# T\n\n" + "garbled " * 10 + "\ufffd" * 20 + "\n").encode()
    diagnostics = diagnose_source_bytes("papers/Garbled.md", sha, garbled)
    assert diagnostics.decodable_utf8 is True
    assert diagnostics.replacement_character_count == 20
    assert "elevated replacement-character ratio" in diagnostics.warnings

    invalid = b"# T\n\ninvalid bytes: \xff\xfe\n" + b"body " * 100
    diagnostics = diagnose_source_bytes("papers/Invalid.md", sha, invalid)
    assert diagnostics.decodable_utf8 is False
    assert "content is not valid UTF-8" in diagnostics.warnings

    markers = "\n".join(
        ["# T"] + ["\\begin{equation} x \\end{equation} $$ y $$" for _ in range(10)]
    ).encode()
    diagnostics = diagnose_source_bytes("papers/Markers.md", sha, markers)
    assert "elevated math/table extraction-marker density" in diagnostics.warnings


def test_diagnostics_flag_empty_or_structureless_body() -> None:
    sha = "sha256:" + "e" * 64
    diagnostics = diagnose_source_bytes("papers/Empty.md", sha, b"")
    assert diagnostics.has_title_line is False
    assert diagnostics.heading_count == 0
    assert "no recognizable title line" in diagnostics.warnings
    assert "no heading structure" in diagnostics.warnings
    assert "body appears empty or truncated" in diagnostics.warnings

    diagnostics = diagnose_source_bytes("papers/Short.md", sha, b"# T\n\nTiny.\n")
    assert diagnostics.has_title_line is True
    assert "body appears empty or truncated" in diagnostics.warnings


def test_structural_assessment_reads_pinned_blob_and_verifies_hash(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    _, config, expected = synthetic_library
    source = PinnedGitSource.open(config)
    inventory, _ = build_library_inventory(source, config)
    beta = next(
        record
        for record in inventory
        if record.source_relative_path.endswith("Beta.md")
    )
    diagnostics = assess_source_structure(source, beta)
    expected_diagnostics = diagnose_source_bytes(
        beta.source_relative_path,
        beta.content_sha256,
        expected["papers/Writer - 2023 - Beta.md"],
    )
    assert diagnostics == expected_diagnostics
    assert diagnostics.has_title_line is True
    assert diagnostics.body_character_count > 200

    tampered = beta.model_copy(update={"content_sha256": "sha256:" + "0" * 64})
    with pytest.raises(CorpusIntegrityError, match="changed since the inventory"):
        assess_source_structure(source, tampered)


def test_validate_selection_source_quality_ignores_duplicate_and_extra_records(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    _, config, _ = synthetic_library
    manifest = make_selection(config)
    extra = make_record("sha256:" + "f" * 64, SourceQualityClassification.UNUSABLE)
    document = SourceQualityDocument(
        records=[
            *[make_record(item.content_sha256) for item in manifest.documents],
            extra,
        ]
    )
    # Records for sources outside the selection never block it.
    assert validate_selection_source_quality(manifest, document) == []
