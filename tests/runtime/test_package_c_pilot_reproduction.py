from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.runtime import test_package_c_pilot_packet as packet_fixture_module
from tests.runtime.test_package_c_pilot_packet import _sha, _write_fixture
from vibereview.models import RenderedSentence, RenderedSentenceAudit
from vibereview.runtime import pilot_packet as packet_module
from vibereview.runtime import pilot_reproduction as reproduction_module
from vibereview.runtime.pilot_packet import PilotPacketError
from vibereview.runtime.pilot_reproduction import (
    PilotReproductionError,
    compare_synthetic_pilot_reproductions,
)


def _write_packet(root: Path, bundle_factory) -> Path:
    root.mkdir()
    _, packet, _ = _write_fixture(root, bundle_factory)
    return packet


def _replace_ids(value: Any, replacements: dict[str, str]) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _replace_ids(child, replacements) for key, child in value.items()}
    if isinstance(value, list):
        return [_replace_ids(child, replacements) for child in value]
    if isinstance(value, str):
        result = value
        for source in sorted(replacements, key=len, reverse=True):
            result = result.replace(source, replacements[source])
        return result
    return value


def _reidentified_factory(bundle_factory):
    replacements = {
        "CPE-C0001-P0001": "CPE-C0042-P0001",
        "Q-C0001-SUP-01": "Q-C0042-SUP-01",
        "RSA0001": "RSA0042",
        "RS0001": "RS0042",
        "SA0001": "SA0042",
        "PR0001": "PR0042",
        "CF0001": "CF0042",
        "PF0001": "PF0042",
        "T0001": "T0042",
        "C0001": "C0042",
        "R0001": "R0042",
        "E0001": "E0042",
    }

    def factory():
        return _replace_ids(bundle_factory(), replacements)

    return factory


def _changed_sentence_factory(bundle_factory):
    def factory():
        bundle = bundle_factory()
        sentence = bundle["rendered_sentences"][0]
        bundle["rendered_sentences"][0] = sentence.model_copy(
            update={
                "text": "A deliberately different but still audited pilot sentence."
            }
        )
        return bundle

    return factory


def _changed_source_factory(bundle_factory):
    def factory():
        bundle = bundle_factory()
        paper = bundle["papers"][0]
        bundle["papers"][0] = paper.model_copy(
            update={"title": "A deliberately different synthetic source"}
        )
        return bundle

    return factory


def _literal_identifier_factory(bundle_factory, *, reidentified: bool):
    def factory():
        bundle = bundle_factory()
        if reidentified:
            bundle = _replace_ids(bundle, {
                "RSA0001": "RSA0042",
                "RS0001": "RS0042",
                "SA0001": "SA0042",
                "PR0001": "PR0042",
            })
        sentence = bundle["rendered_sentences"][0]
        text = "The literal scientific label PR0001 is retained as prose."
        if isinstance(sentence, dict):
            sentence["text"] = text
        else:
            bundle["rendered_sentences"][0] = sentence.model_copy(
                update={"text": text}
            )
        return bundle

    return factory


def _two_sentence_factory(bundle_factory, *, reverse_text: bool):
    def factory():
        bundle = bundle_factory()
        first = bundle["rendered_sentences"][0]
        first_text = first.text
        second_text = "A second audited synthetic pilot sentence."
        if reverse_text:
            first_text, second_text = second_text, first_text
        bundle["rendered_sentences"] = [
            first.model_copy(update={"text": first_text}),
            RenderedSentence(
                sentence_id="RS0002",
                text=second_text,
                source_proposition_ids=["PR0001"],
            ),
        ]
        reason = bundle["rendered_sentence_audits"][0].reason
        bundle["rendered_sentence_audits"] = [
            bundle["rendered_sentence_audits"][0],
            RenderedSentenceAudit(
                audit_id="RSA0002",
                sentence_id="RS0002",
                verdict="ENTAILED",
                reason=reason,
            ),
        ]
        return bundle

    return factory


def test_reproduction_ignores_canonical_id_allocation_but_preserves_semantics(
    tmp_path: Path, bundle_factory
) -> None:
    first = _write_packet(tmp_path / "first", bundle_factory)
    second = _write_packet(
        tmp_path / "second", _reidentified_factory(bundle_factory)
    )

    report = compare_synthetic_pilot_reproductions((first, second))

    assert report.packet_count == 2
    assert report.publication_eligible is False
    assert report.human_review == "NOT_PERFORMED"
    assert report.differences == ()
    assert {item.subject for item in report.agreements} >= {
        "repository.papers",
        "repository.retrieved_spans",
        "repository.proposition_records",
        "assembled_section",
    }
    assert (
        report.packets[0].repository_hash
        != report.packets[1].repository_hash
    )
    assert (
        report.packets[0].repository_semantic_hash
        == report.packets[1].repository_semantic_hash
    )


def test_reproduction_ignores_run_label_and_timestamp_only(
    tmp_path: Path, bundle_factory, monkeypatch
) -> None:
    first = _write_packet(tmp_path / "first", bundle_factory)
    original = packet_fixture_module._run_manifest

    def relabeled_run_manifest(snapshot):
        return original(snapshot).model_copy(
            update={
                "run_id": "RUN-synthetic-packet-second",
                "created_at": "2031-02-03T04:05:06+00:00",
            }
        )

    monkeypatch.setattr(
        packet_fixture_module,
        "_run_manifest",
        relabeled_run_manifest,
    )
    second = _write_packet(tmp_path / "second", bundle_factory)

    report = compare_synthetic_pilot_reproductions((first, second))

    assert report.differences == ()
    assert report.packets[0].run_id != report.packets[1].run_id


def test_reproduction_rejects_different_package_c_implementation_fingerprints(
    tmp_path: Path, bundle_factory, monkeypatch
) -> None:
    first = _write_packet(tmp_path / "first", bundle_factory)
    original = packet_fixture_module._run_manifest

    def changed_implementation_manifest(snapshot):
        return original(snapshot).model_copy(
            update={"package_c_implementation_fingerprint": _sha("f")}
        )

    monkeypatch.setattr(
        packet_fixture_module,
        "_run_manifest",
        changed_implementation_manifest,
    )
    second = _write_packet(tmp_path / "second", bundle_factory)

    with pytest.raises(PilotReproductionError, match="run identity"):
        compare_synthetic_pilot_reproductions((first, second))


def test_reproduction_rejects_a_different_review_topic(
    tmp_path: Path, bundle_factory, monkeypatch
) -> None:
    first = _write_packet(tmp_path / "first", bundle_factory)
    original = packet_fixture_module._run_manifest

    def changed_topic_manifest(snapshot):
        return original(snapshot).model_copy(
            update={"topic": "A materially different synthetic review topic."}
        )

    monkeypatch.setattr(
        packet_fixture_module,
        "_run_manifest",
        changed_topic_manifest,
    )
    second = _write_packet(tmp_path / "second", bundle_factory)

    with pytest.raises(PilotReproductionError, match="run identity"):
        compare_synthetic_pilot_reproductions((first, second))


def test_reproduction_describes_text_differences_without_a_verdict(
    tmp_path: Path, bundle_factory
) -> None:
    first = _write_packet(tmp_path / "first", bundle_factory)
    second = _write_packet(
        tmp_path / "second", _changed_sentence_factory(bundle_factory)
    )

    report = compare_synthetic_pilot_reproductions([first, second])
    subjects = {item.subject for item in report.differences}

    assert "repository.rendered_sentences" in subjects
    assert "assembled_section" in subjects
    assert not (
        set(report.model_dump())
        & {"winner", "threshold", "acceptance", "score", "verdict"}
    )
    assert report.publication_eligible is False


def test_reproduction_fails_closed_for_different_source_identity(
    tmp_path: Path, bundle_factory
) -> None:
    first = _write_packet(tmp_path / "first", bundle_factory)
    second = _write_packet(
        tmp_path / "second", _changed_source_factory(bundle_factory)
    )

    with pytest.raises(PilotReproductionError, match="run identity"):
        compare_synthetic_pilot_reproductions((first, second))


def test_reproduction_preserves_ordered_discovery_resource_identity(
    tmp_path: Path, bundle_factory, monkeypatch
) -> None:
    first = _write_packet(tmp_path / "first", bundle_factory)
    original = packet_fixture_module._run_manifest

    def reordered_run_manifest(snapshot):
        manifest = original(snapshot)
        return manifest.model_copy(
            update={
                "discovery_resource_hashes": tuple(
                    reversed(manifest.discovery_resource_hashes)
                )
            }
        )

    monkeypatch.setattr(
        packet_fixture_module,
        "_run_manifest",
        reordered_run_manifest,
    )
    second = _write_packet(tmp_path / "second", bundle_factory)

    with pytest.raises(PilotReproductionError, match="run identity"):
        compare_synthetic_pilot_reproductions((first, second))


def test_literal_canonical_id_in_scientific_prose_is_not_rewritten(
    tmp_path: Path, bundle_factory
) -> None:
    first = _write_packet(
        tmp_path / "first",
        _literal_identifier_factory(bundle_factory, reidentified=False),
    )
    second = _write_packet(
        tmp_path / "second",
        _literal_identifier_factory(bundle_factory, reidentified=True),
    )

    report = compare_synthetic_pilot_reproductions((first, second))

    assert report.differences == ()


def test_reproduction_preserves_assembled_sentence_order(
    tmp_path: Path, bundle_factory
) -> None:
    first = _write_packet(
        tmp_path / "first",
        _two_sentence_factory(bundle_factory, reverse_text=False),
    )
    second = _write_packet(
        tmp_path / "second",
        _two_sentence_factory(bundle_factory, reverse_text=True),
    )

    report = compare_synthetic_pilot_reproductions((first, second))
    subjects = {item.subject for item in report.differences}

    assert "assembled_section" in subjects
    assert "repository.rendered_sentences" not in subjects


def test_section_summary_binds_exact_trailing_newline() -> None:
    with_newline = reproduction_module._ordered_text_summary(b"same text\n")
    without_newline = reproduction_module._ordered_text_summary(b"same text")

    assert with_newline.semantic_set_hash != without_newline.semantic_set_hash


def test_semantic_identity_retains_exact_source_graph(bundle_factory) -> None:
    snapshot = reproduction_module.RepositorySnapshot.model_validate(bundle_factory())
    paper = snapshot.papers[0].model_copy(
        update={
            "paper_id": "P0002",
            "title": "A distinct source paper",
            "authors": ["Distinct Author"],
            "doi": None,
            "identity_keys": ["fixture:distinct-source"],
            "raw_md_path": "papers/P0002/raw.md",
            "source_hash": _sha("2"),
            "raw_md_hash": _sha("3"),
        }
    )
    claim = snapshot.candidate_claims[0].model_copy(
        update={"claim_id": "C0002", "candidate_claim": "A distinct claim."}
    )
    query = snapshot.retrieval_queries[0].model_copy(
        update={"query_id": "Q-C0002-SUP-01", "claim_id": "C0002"}
    )
    span = snapshot.retrieved_spans[0].model_copy(
        update={
            "span_id": "R0002",
            "paper_id": "P0002",
            "locator": snapshot.retrieved_spans[0].locator.model_copy(
                update={"raw_md_path": "papers/P0002/raw.md"}
            ),
            "retrieval": snapshot.retrieved_spans[0].retrieval.model_copy(
                update={"query_id": "Q-C0002-SUP-01"}
            ),
        }
    )
    evidence = snapshot.evidence_records[0].model_copy(
        update={
            "evidence_id": "E0002",
            "claim_id": "C0002",
            "retrieved_span_id": "R0002",
            "paper_id": "P0002",
        }
    )
    expanded = snapshot.model_copy(
        update={
            "papers": snapshot.papers + (paper,),
            "candidate_claims": snapshot.candidate_claims + (claim,),
            "retrieval_queries": snapshot.retrieval_queries + (query,),
            "retrieved_spans": snapshot.retrieved_spans + (span,),
            "evidence_records": snapshot.evidence_records + (evidence,),
        }
    )

    identities = reproduction_module._canonical_semantic_identities(expanded)

    assert identities["E0001"] != identities["E0002"]


def test_forward_paper_reference_does_not_bind_allocation_order(
    bundle_factory,
) -> None:
    snapshot = reproduction_module.RepositorySnapshot.model_validate(bundle_factory())
    first = snapshot.papers[0].model_copy(
        update={"related_publications": ["P0002"]}
    )
    second = snapshot.papers[0].model_copy(
        update={
            "paper_id": "P0002",
            "title": "A forward-referenced source paper",
            "authors": ["Forward Author"],
            "identity_keys": ["fixture:forward-source"],
            "raw_md_path": "papers/P0002/raw.md",
            "source_hash": _sha("2"),
            "raw_md_hash": _sha("3"),
        }
    )
    original = snapshot.model_copy(update={"papers": (first, second)})
    renamed_payload = _replace_ids(
        original,
        {"P0001": "P0042", "P0002": "P0043"},
    )
    renamed_payload["papers"] = list(reversed(renamed_payload["papers"]))
    renamed = reproduction_module.RepositorySnapshot.model_validate(renamed_payload)

    original_identities = reproduction_module._canonical_semantic_identities(
        original
    )
    renamed_identities = reproduction_module._canonical_semantic_identities(renamed)

    assert original_identities["P0001"] == renamed_identities["P0042"]
    assert original_identities["P0002"] == renamed_identities["P0043"]


def test_retained_packet_load_rejects_directory_rebinding(
    tmp_path: Path, bundle_factory, monkeypatch
) -> None:
    first = _write_packet(tmp_path / "first", bundle_factory)
    second = _write_packet(tmp_path / "second", bundle_factory)
    moved = first.with_name("first-moved")
    original_verify = packet_module._verify_packet_values
    swapped = False

    def swap_after_semantic_verification(values):
        nonlocal swapped
        result = original_verify(values)
        if not swapped:
            swapped = True
            first.rename(moved)
            shutil.copytree(moved, first)
        return result

    monkeypatch.setattr(
        packet_module,
        "_verify_packet_values",
        swap_after_semantic_verification,
    )
    with pytest.raises((PilotPacketError, PilotReproductionError)):
        compare_synthetic_pilot_reproductions((first, second))


@pytest.mark.parametrize("count", [1, 5])
def test_reproduction_requires_two_to_four_packets(
    tmp_path: Path, bundle_factory, count: int
) -> None:
    packet = _write_packet(tmp_path / "only", bundle_factory)

    with pytest.raises(PilotReproductionError, match="two to four"):
        compare_synthetic_pilot_reproductions(tuple(packet for _ in range(count)))
