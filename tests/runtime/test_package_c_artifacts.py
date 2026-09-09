from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from vibereview.models import RenderedSentence, RenderedSentenceAudit
from vibereview.runtime import artifacts as artifact_module
from vibereview.runtime.artifacts import (
    AssemblyBudget,
    PackageCArtifactError,
    assemble_exact_section,
    verify_exact_assembly,
    verify_synthetic_review_packet,
    write_synthetic_review_packet,
)
from vibereview.runtime.hashing import hash_bytes
from vibereview.runtime.repository import GenerationStore, PromotionPayload
from vibereview.runtime.state import RepositorySnapshot


def _snapshot(bundle_factory) -> RepositorySnapshot:
    return RepositorySnapshot.model_validate(bundle_factory())


def _budget(**updates) -> AssemblyBudget:
    values = {
        "max_sentences": 18,
        "max_citations": 30,
        "max_body_utf8_bytes": 64 * 1024,
    }
    values.update(updates)
    return AssemblyBudget(**values)


def _review_root(tmp_path: Path, snapshot: RepositorySnapshot) -> Path:
    root = tmp_path / "review"
    GenerationStore(root).initialize(snapshot)
    return root


def _public_root(tmp_path: Path) -> Path:
    root = tmp_path / "public-repository"
    root.mkdir(exist_ok=True)
    return root


def test_exact_assembly_uses_canonical_numeric_order_and_utf8_offsets(bundle_factory):
    snapshot = _snapshot(bundle_factory)
    first = snapshot.rendered_sentences[0].model_copy(
        update={"text": "Café evidence remains bounded."}
    )
    second = RenderedSentence(
        sentence_id="RS0002",
        text="A second audited sentence.",
        source_proposition_ids=["PR0001"],
    )
    second_audit = RenderedSentenceAudit(
        audit_id="RSA0002",
        sentence_id="RS0002",
        verdict="ENTAILED",
        reason="Exact synthetic fixture entailment.",
    )
    snapshot = snapshot.model_copy(
        update={
            "rendered_sentences": (first, second),
            "rendered_sentence_audits": (
                snapshot.rendered_sentence_audits[0],
                second_audit,
            ),
        }
    )

    body, record = assemble_exact_section(
        snapshot,
        source_generation=7,
        budget=_budget(),
    )

    assert body == (
        "Café evidence remains bounded.\n"
        "A second audited sentence.\n"
    ).encode("utf-8")
    assert [item.sentence_id for item in record.sentences] == ["RS0001", "RS0002"]
    assert record.sentences[0].start_codepoint == 0
    assert record.sentences[0].end_codepoint == len(first.text)
    assert record.sentences[0].end_utf8_byte == len(first.text.encode("utf-8"))
    assert record.sentences[1].start_codepoint == len(first.text) + 1
    assert record.sentences[1].start_utf8_byte == len(first.text.encode("utf-8")) + 1
    assert record.body_hash == hash_bytes(body)
    verify_exact_assembly(snapshot, body, record)


def test_exact_assembly_flattens_resolved_citations(bundle_factory):
    snapshot = _snapshot(bundle_factory)
    body, record = assemble_exact_section(
        snapshot, source_generation=3, budget=_budget()
    )

    assert body.endswith(b"\n")
    assert [item.model_dump(mode="json") for item in record.citations] == [
        {
            "sentence_id": "RS0001",
            "proposition_id": "PR0001",
            "paper_id": "P0001",
            "claim_id": "C0001",
            "claim_paper_evidence_id": "CPE-C0001-P0001",
        }
    ]
    assert record.repository_hash == snapshot.canonical_hash()


def test_verification_rejects_any_post_audit_text_change(bundle_factory):
    snapshot = _snapshot(bundle_factory)
    body, record = assemble_exact_section(
        snapshot, source_generation=1, budget=_budget()
    )

    with pytest.raises(PackageCArtifactError, match="does not match"):
        verify_exact_assembly(snapshot, body + b"extra", record)


@pytest.mark.parametrize("marker", ["\n", "\r", "\x00"])
def test_assembly_rejects_embedded_line_or_nul_markers(bundle_factory, marker):
    snapshot = _snapshot(bundle_factory)
    sentence = snapshot.rendered_sentences[0].model_copy(
        update={"text": f"before{marker}after"}
    )
    snapshot = snapshot.model_copy(update={"rendered_sentences": (sentence,)})

    with pytest.raises(PackageCArtifactError, match="forbidden line marker"):
        assemble_exact_section(snapshot, source_generation=1, budget=_budget())


def test_synthetic_review_packet_is_nonpublication_and_exactly_idempotent(
    tmp_path: Path, bundle_factory
):
    snapshot = _snapshot(bundle_factory)
    review_root = _review_root(tmp_path, snapshot)
    body, assembly = assemble_exact_section(
        snapshot, source_generation=0, budget=_budget()
    )
    destination = tmp_path / "packet"

    manifest = write_synthetic_review_packet(
        review_root,
        destination,
        public_repository_root=_public_root(tmp_path),
        body=body,
        assembly=assembly,
    )

    assert (destination / "section.md").read_bytes() == body
    assert json.loads((destination / "assembly.json").read_text(encoding="utf-8")) == (
        assembly.model_dump(mode="json")
    )
    recorded = json.loads(
        (destination / "review_packet.json").read_text(encoding="utf-8")
    )
    assert recorded == manifest.model_dump(mode="json")
    assert recorded["synthetic_fixture"] is True
    assert recorded["human_review"] == "NOT_PERFORMED"
    assert recorded["publication_eligible"] is False
    assert recorded["source_generation"] == 0
    assert (destination / "repository.json").is_file()
    assert verify_synthetic_review_packet(review_root, destination) == manifest
    assert manifest.assembly_artifact_hash == hash_bytes(
        (destination / "assembly.json").read_bytes()
    )

    before = {
        path.name: (path.stat().st_dev, path.stat().st_ino, path.read_bytes())
        for path in destination.iterdir()
    }
    reused = write_synthetic_review_packet(
        review_root,
        destination,
        public_repository_root=_public_root(tmp_path),
        body=body,
        assembly=assembly,
    )
    after = {
        path.name: (path.stat().st_dev, path.stat().st_ino, path.read_bytes())
        for path in destination.iterdir()
    }
    assert reused == manifest
    assert after == before


def test_packet_writer_rejects_body_digest_mismatch(tmp_path: Path, bundle_factory):
    snapshot = _snapshot(bundle_factory)
    review_root = _review_root(tmp_path, snapshot)
    body, assembly = assemble_exact_section(
        snapshot, source_generation=0, budget=_budget()
    )

    with pytest.raises(PackageCArtifactError, match="does not match"):
        write_synthetic_review_packet(
            review_root,
            tmp_path / "packet",
            public_repository_root=_public_root(tmp_path),
            body=body + b"changed",
            assembly=assembly,
        )
    assert not (tmp_path / "packet").exists()


@pytest.mark.parametrize(
    "budget",
    [
        _budget(max_sentences=1),
        _budget(max_citations=1),
        _budget(max_body_utf8_bytes=1),
    ],
)
def test_explicit_assembly_budgets_fail_closed(tmp_path, bundle_factory, budget):
    snapshot = _snapshot(bundle_factory)
    second = RenderedSentence(
        sentence_id="RS0002",
        text="A second synthetic sentence.",
        source_proposition_ids=["PR0001"],
    )
    second_audit = RenderedSentenceAudit(
        audit_id="RSA0002",
        sentence_id="RS0002",
        verdict="ENTAILED",
        reason="Exact synthetic fixture entailment.",
    )
    snapshot = snapshot.model_copy(
        update={
            "rendered_sentences": snapshot.rendered_sentences + (second,),
            "rendered_sentence_audits": snapshot.rendered_sentence_audits
            + (second_audit,),
        }
    )

    with pytest.raises(PackageCArtifactError, match="exceeds"):
        assemble_exact_section(snapshot, source_generation=2, budget=budget)


def test_packet_writer_reloads_claimed_generation(tmp_path: Path, bundle_factory):
    snapshot = _snapshot(bundle_factory)
    review_root = _review_root(tmp_path, snapshot)
    body, assembly = assemble_exact_section(
        snapshot, source_generation=0, budget=_budget()
    )
    forged = assembly.model_copy(update={"source_generation": 999})

    with pytest.raises(PackageCArtifactError, match="source generation"):
        write_synthetic_review_packet(
            review_root,
            tmp_path / "packet",
            public_repository_root=_public_root(tmp_path),
            body=body,
            assembly=forged,
        )


def test_packet_writer_rejects_a_noncurrent_source_generation(
    tmp_path: Path, bundle_factory
):
    snapshot = _snapshot(bundle_factory)
    review_root = _review_root(tmp_path, snapshot)
    body, assembly = assemble_exact_section(
        snapshot, source_generation=0, budget=_budget()
    )
    store = GenerationStore(review_root)
    store.commit(
        base_generation=0,
        dependencies={},
        promotion=lambda current, registry: PromotionPayload(current, registry, {}),
    )

    with pytest.raises(PackageCArtifactError, match="not CURRENT"):
        write_synthetic_review_packet(
            review_root,
            tmp_path / "packet",
            public_repository_root=_public_root(tmp_path),
            body=body,
            assembly=assembly,
        )
    assert not (tmp_path / "packet").exists()


def test_packet_verifier_rejects_extra_files(tmp_path: Path, bundle_factory):
    snapshot = _snapshot(bundle_factory)
    review_root = _review_root(tmp_path, snapshot)
    body, assembly = assemble_exact_section(
        snapshot, source_generation=0, budget=_budget()
    )
    destination = tmp_path / "packet"
    write_synthetic_review_packet(
        review_root,
        destination,
        public_repository_root=_public_root(tmp_path),
        body=body,
        assembly=assembly,
    )
    (destination / "extra.txt").write_text("unexpected", encoding="utf-8")

    with pytest.raises(PackageCArtifactError, match="file set differs"):
        verify_synthetic_review_packet(review_root, destination)


@pytest.mark.parametrize(
    ("filename", "operation"),
    [
        ("section.md", "change"),
        ("assembly.json", "change"),
        ("repository.json", "change"),
        ("review_packet.json", "change"),
        ("section.md", "remove"),
    ],
)
def test_packet_verifier_rejects_changed_or_missing_files(
    tmp_path: Path, bundle_factory, filename: str, operation: str
):
    snapshot = _snapshot(bundle_factory)
    review_root = _review_root(tmp_path, snapshot)
    body, assembly = assemble_exact_section(
        snapshot, source_generation=0, budget=_budget()
    )
    destination = tmp_path / "packet"
    write_synthetic_review_packet(
        review_root,
        destination,
        public_repository_root=_public_root(tmp_path),
        body=body,
        assembly=assembly,
    )
    target = destination / filename
    if operation == "remove":
        target.unlink()
    else:
        target.chmod(0o644)
        target.write_bytes(target.read_bytes() + b"changed")

    with pytest.raises(PackageCArtifactError):
        verify_synthetic_review_packet(review_root, destination)


def test_atomic_publication_never_replaces_racing_destination(
    tmp_path: Path, bundle_factory, monkeypatch
):
    snapshot = _snapshot(bundle_factory)
    review_root = _review_root(tmp_path, snapshot)
    body, assembly = assemble_exact_section(
        snapshot, source_generation=0, budget=_budget()
    )
    destination = tmp_path / "packet"
    real_rename = artifact_module._rename_directory_noreplace_at

    def race(parent_descriptor: int, source_name: str, target_name: str) -> None:
        target = Path(f"/proc/self/fd/{parent_descriptor}") / target_name
        target.mkdir()
        (target / "owner.txt").write_text(
            "belongs to another writer", encoding="utf-8"
        )
        real_rename(parent_descriptor, source_name, target_name)

    monkeypatch.setattr(artifact_module, "_rename_directory_noreplace_at", race)
    with pytest.raises(FileExistsError):
        write_synthetic_review_packet(
            review_root,
            destination,
            public_repository_root=_public_root(tmp_path),
            body=body,
            assembly=assembly,
        )
    assert (destination / "owner.txt").read_text(encoding="utf-8") == (
        "belongs to another writer"
    )


def test_packet_publish_cannot_follow_swapped_parent_into_public_repository(
    tmp_path: Path, bundle_factory, monkeypatch
):
    snapshot = _snapshot(bundle_factory)
    review_root = _review_root(tmp_path, snapshot)
    body, assembly = assemble_exact_section(
        snapshot, source_generation=0, budget=_budget()
    )
    public_root = _public_root(tmp_path)
    output_parent = tmp_path / "output-parent"
    output_parent.mkdir()
    retained_parent = tmp_path / "retained-output-parent"
    destination = output_parent / "packet"
    real_rename = artifact_module._rename_directory_noreplace_at

    def swap_parent(
        parent_descriptor: int, source_name: str, target_name: str
    ) -> None:
        output_parent.rename(retained_parent)
        output_parent.symlink_to(public_root, target_is_directory=True)
        real_rename(parent_descriptor, source_name, target_name)

    monkeypatch.setattr(
        artifact_module,
        "_rename_directory_noreplace_at",
        swap_parent,
    )
    try:
        with pytest.raises(PackageCArtifactError, match="parent directory changed"):
            write_synthetic_review_packet(
                review_root,
                destination,
                public_repository_root=public_root,
                body=body,
                assembly=assembly,
            )
        assert not (public_root / "packet").exists()
        assert not (retained_parent / "packet").exists()
        assert not list(retained_parent.glob(".package-c-staging-*"))
    finally:
        if output_parent.is_symlink():
            output_parent.unlink()
        if retained_parent.exists():
            retained_parent.rename(output_parent)


@pytest.mark.parametrize(
    ("moved_root", "expected_error"),
    [
        ("output", "parent directory changed"),
        ("review", "review project root changed"),
        ("public", "public repository root changed"),
    ],
)
def test_packet_publish_rejects_relocated_intermediate_ancestor(
    tmp_path: Path,
    bundle_factory,
    monkeypatch,
    moved_root: str,
    expected_error: str,
):
    snapshot = _snapshot(bundle_factory)
    review_ancestor = tmp_path / "review-area"
    review_root = review_ancestor / "review"
    GenerationStore(review_root).initialize(snapshot)
    public_ancestor = tmp_path / "public-area"
    public_root = public_ancestor / "public-repository"
    public_root.mkdir(parents=True)
    output_ancestor = tmp_path / "output-area"
    output_parent = output_ancestor / "leaf"
    output_parent.mkdir(parents=True)
    destination = output_parent / "packet"
    body, assembly = assemble_exact_section(
        snapshot, source_generation=0, budget=_budget()
    )
    ancestors = {
        "output": output_ancestor,
        "review": review_ancestor,
        "public": public_ancestor,
    }
    ancestor = ancestors[moved_root]
    retained = tmp_path / f"retained-{moved_root}-area"
    real_rename = artifact_module._rename_directory_noreplace_at

    def relocate_ancestor(
        parent_descriptor: int, source_name: str, target_name: str
    ) -> None:
        ancestor.rename(retained)
        ancestor.symlink_to(retained, target_is_directory=True)
        real_rename(parent_descriptor, source_name, target_name)

    monkeypatch.setattr(
        artifact_module,
        "_rename_directory_noreplace_at",
        relocate_ancestor,
    )
    try:
        with pytest.raises(PackageCArtifactError, match=expected_error):
            write_synthetic_review_packet(
                review_root,
                destination,
                public_repository_root=public_root,
                body=body,
                assembly=assembly,
            )
        assert not destination.exists()
        assert not list(retained.rglob("packet"))
        assert not list(retained.rglob(".package-c-staging-*"))
    finally:
        if ancestor.is_symlink():
            ancestor.unlink()
        if retained.exists():
            retained.rename(ancestor)


def test_packet_publish_rejects_swapped_public_repository_root(
    tmp_path: Path, bundle_factory, monkeypatch
):
    snapshot = _snapshot(bundle_factory)
    review_root = _review_root(tmp_path, snapshot)
    body, assembly = assemble_exact_section(
        snapshot, source_generation=0, budget=_budget()
    )
    public_root = _public_root(tmp_path)
    retained_public_root = tmp_path / "retained-public-repository"
    destination = tmp_path / "packet"
    real_rename = artifact_module._rename_directory_noreplace_at

    def swap_public_root(
        parent_descriptor: int, source_name: str, target_name: str
    ) -> None:
        public_root.rename(retained_public_root)
        public_root.mkdir()
        (public_root / "owner.txt").write_text(
            "belongs to the replacement public root", encoding="utf-8"
        )
        real_rename(parent_descriptor, source_name, target_name)

    monkeypatch.setattr(
        artifact_module,
        "_rename_directory_noreplace_at",
        swap_public_root,
    )

    with pytest.raises(PackageCArtifactError, match="public repository root changed"):
        write_synthetic_review_packet(
            review_root,
            destination,
            public_repository_root=public_root,
            body=body,
            assembly=assembly,
        )

    assert not destination.exists()
    assert (public_root / "owner.txt").read_text(encoding="utf-8") == (
        "belongs to the replacement public root"
    )


def test_packet_publish_rejects_swapped_review_root_even_with_valid_clone(
    tmp_path: Path, bundle_factory, monkeypatch
):
    snapshot = _snapshot(bundle_factory)
    review_root = _review_root(tmp_path, snapshot)
    retained_review_root = tmp_path / "retained-review"
    output_parent = tmp_path / "output-parent"
    GenerationStore(output_parent).initialize(snapshot)
    destination = output_parent / "packet"
    body, assembly = assemble_exact_section(
        snapshot, source_generation=0, budget=_budget()
    )
    real_rename = artifact_module._rename_directory_noreplace_at

    def swap_review_root(
        parent_descriptor: int, source_name: str, target_name: str
    ) -> None:
        review_root.rename(retained_review_root)
        review_root.symlink_to(output_parent, target_is_directory=True)
        real_rename(parent_descriptor, source_name, target_name)

    monkeypatch.setattr(
        artifact_module,
        "_rename_directory_noreplace_at",
        swap_review_root,
    )
    try:
        with pytest.raises(PackageCArtifactError, match="review project root changed"):
            write_synthetic_review_packet(
                review_root,
                destination,
                public_repository_root=_public_root(tmp_path),
                body=body,
                assembly=assembly,
            )

        assert not destination.exists()
        loaded, _ = GenerationStore(retained_review_root).load_generation(0)
        assert loaded == snapshot
    finally:
        if review_root.is_symlink():
            review_root.unlink()
        if retained_review_root.exists():
            retained_review_root.rename(review_root)


def test_packet_publish_does_not_delete_swapped_destination(
    tmp_path: Path, bundle_factory, monkeypatch
):
    snapshot = _snapshot(bundle_factory)
    review_root = _review_root(tmp_path, snapshot)
    body, assembly = assemble_exact_section(
        snapshot, source_generation=0, budget=_budget()
    )
    destination = tmp_path / "packet"
    retained_packet = tmp_path / "retained-packet"
    real_rename = artifact_module._rename_directory_noreplace_at

    def publish_then_swap(
        parent_descriptor: int, source_name: str, target_name: str
    ) -> None:
        real_rename(parent_descriptor, source_name, target_name)
        destination.rename(retained_packet)
        destination.mkdir()
        (destination / "owner.txt").write_text(
            "belongs to the replacement destination", encoding="utf-8"
        )

    monkeypatch.setattr(
        artifact_module,
        "_rename_directory_noreplace_at",
        publish_then_swap,
    )

    with pytest.raises(
        PackageCArtifactError, match="published review packet directory changed"
    ):
        write_synthetic_review_packet(
            review_root,
            destination,
            public_repository_root=_public_root(tmp_path),
            body=body,
            assembly=assembly,
        )

    assert (destination / "owner.txt").read_text(encoding="utf-8") == (
        "belongs to the replacement destination"
    )
    assert set(path.name for path in retained_packet.iterdir()) == {
        "assembly.json",
        "repository.json",
        "review_packet.json",
        "section.md",
    }


def test_staging_relocation_is_detected_before_any_file_bytes_are_written(
    tmp_path: Path, bundle_factory, monkeypatch
):
    snapshot = _snapshot(bundle_factory)
    review_root = _review_root(tmp_path, snapshot)
    public_root = _public_root(tmp_path)
    destination = tmp_path / "packet"
    stolen_staging = public_root / "stolen-staging"
    body, assembly = assemble_exact_section(
        snapshot, source_generation=0, budget=_budget()
    )
    real_write = artifact_module._write_packet_file_at
    moved = False

    def relocate_then_write(
        directory_descriptor: int,
        name: str,
        content: bytes,
        **kwargs,
    ) -> None:
        nonlocal moved
        if not moved:
            Path(os.readlink(f"/proc/self/fd/{directory_descriptor}")).rename(
                stolen_staging
            )
            moved = True
        real_write(directory_descriptor, name, content, **kwargs)

    monkeypatch.setattr(
        artifact_module,
        "_write_packet_file_at",
        relocate_then_write,
    )
    with pytest.raises(PackageCArtifactError, match="staging directory changed"):
        write_synthetic_review_packet(
            review_root,
            destination,
            public_repository_root=public_root,
            body=body,
            assembly=assembly,
        )

    assert moved
    assert not destination.exists()
    assert stolen_staging.is_dir()
    assert list(stolen_staging.iterdir()) == []


def test_staging_open_failure_cleans_the_owned_new_directory(
    tmp_path: Path, bundle_factory, monkeypatch
):
    snapshot = _snapshot(bundle_factory)
    review_root = _review_root(tmp_path, snapshot)
    body, assembly = assemble_exact_section(
        snapshot, source_generation=0, budget=_budget()
    )
    real_open = artifact_module._open_directory_at

    def fail_staging_open(
        parent_descriptor: int, name: str, *, description: str
    ):
        if description == "review packet staging directory":
            raise PackageCArtifactError("injected staging open failure")
        return real_open(parent_descriptor, name, description=description)

    monkeypatch.setattr(artifact_module, "_open_directory_at", fail_staging_open)
    with pytest.raises(PackageCArtifactError, match="injected staging open failure"):
        write_synthetic_review_packet(
            review_root,
            tmp_path / "packet",
            public_repository_root=_public_root(tmp_path),
            body=body,
            assembly=assembly,
        )

    assert not list(tmp_path.glob(".package-c-staging-*"))


def test_restrictive_umask_does_not_break_or_leak_staging(
    tmp_path: Path, bundle_factory
):
    snapshot = _snapshot(bundle_factory)
    review_root = _review_root(tmp_path, snapshot)
    public_root = _public_root(tmp_path)
    destination = tmp_path / "packet"
    body, assembly = assemble_exact_section(
        snapshot, source_generation=0, budget=_budget()
    )

    previous_umask = os.umask(0o777)
    try:
        write_synthetic_review_packet(
            review_root,
            destination,
            public_repository_root=public_root,
            body=body,
            assembly=assembly,
        )
    finally:
        os.umask(previous_umask)

    assert destination.is_dir()
    assert destination.stat().st_mode & 0o777 == 0o700
    assert not list(tmp_path.glob(".package-c-staging-*"))


def test_collision_winner_reuse_fsyncs_the_parent_directory(
    tmp_path: Path, bundle_factory, monkeypatch
):
    snapshot = _snapshot(bundle_factory)
    review_root = _review_root(tmp_path, snapshot)
    destination = tmp_path / "packet"
    body, assembly = assemble_exact_section(
        snapshot, source_generation=0, budget=_budget()
    )
    parent_descriptor: int | None = None
    fsynced: list[int] = []
    real_fsync = artifact_module.os.fsync

    def publish_as_competing_winner(
        descriptor: int, source_name: str, target_name: str
    ) -> None:
        nonlocal parent_descriptor
        parent_descriptor = descriptor
        os.rename(
            source_name,
            target_name,
            src_dir_fd=descriptor,
            dst_dir_fd=descriptor,
        )
        raise FileExistsError(target_name)

    def record_fsync(descriptor: int) -> None:
        fsynced.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(
        artifact_module,
        "_rename_directory_noreplace_at",
        publish_as_competing_winner,
    )
    monkeypatch.setattr(artifact_module.os, "fsync", record_fsync)

    write_synthetic_review_packet(
        review_root,
        destination,
        public_repository_root=_public_root(tmp_path),
        body=body,
        assembly=assembly,
    )

    assert parent_descriptor is not None
    assert parent_descriptor in fsynced
    verify_synthetic_review_packet(review_root, destination)


@pytest.mark.parametrize("swap_timing", ["before", "after"])
def test_packet_verifier_rejects_destination_swap_around_file_reads(
    tmp_path: Path, bundle_factory, monkeypatch, swap_timing: str
):
    snapshot = _snapshot(bundle_factory)
    review_root = _review_root(tmp_path, snapshot)
    body, assembly = assemble_exact_section(
        snapshot, source_generation=0, budget=_budget()
    )
    destination = tmp_path / "packet"
    retained_packet = tmp_path / "retained-packet"
    write_synthetic_review_packet(
        review_root,
        destination,
        public_repository_root=_public_root(tmp_path),
        body=body,
        assembly=assembly,
    )
    original = artifact_module._read_packet_file
    call_count = 0

    def swap_destination() -> None:
        destination.rename(retained_packet)
        destination.mkdir()
        (destination / "owner.txt").write_text(
            "belongs to another directory", encoding="utf-8"
        )

    def read_while_swapping(
        directory_descriptor: int, name: str
    ) -> artifact_module._RetainedPacketFile:
        nonlocal call_count
        if swap_timing == "before" and call_count == 0:
            swap_destination()
        retained = original(directory_descriptor, name)
        call_count += 1
        if (
            swap_timing == "after"
            and call_count == len(artifact_module._PACKET_FILES)
        ):
            swap_destination()
        return retained

    monkeypatch.setattr(artifact_module, "_read_packet_file", read_while_swapping)

    with pytest.raises(PackageCArtifactError, match="directory changed"):
        verify_synthetic_review_packet(review_root, destination)

    assert (destination / "owner.txt").read_text(encoding="utf-8") == (
        "belongs to another directory"
    )
    assert retained_packet.is_dir()


def test_packet_verifier_rechecks_closed_file_set_after_reads(
    tmp_path: Path, bundle_factory, monkeypatch
):
    snapshot = _snapshot(bundle_factory)
    review_root = _review_root(tmp_path, snapshot)
    body, assembly = assemble_exact_section(
        snapshot, source_generation=0, budget=_budget()
    )
    destination = tmp_path / "packet"
    write_synthetic_review_packet(
        review_root,
        destination,
        public_repository_root=_public_root(tmp_path),
        body=body,
        assembly=assembly,
    )
    original = artifact_module._read_packet_file
    injected = False

    def inject_extra(
        directory_descriptor: int, name: str
    ) -> artifact_module._RetainedPacketFile:
        nonlocal injected
        retained = original(directory_descriptor, name)
        if not injected:
            descriptor = os.open(
                "extra.txt",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
                dir_fd=directory_descriptor,
            )
            os.close(descriptor)
            injected = True
        return retained

    monkeypatch.setattr(artifact_module, "_read_packet_file", inject_extra)
    with pytest.raises(PackageCArtifactError, match="file set changed"):
        verify_synthetic_review_packet(review_root, destination)


def test_packet_inventory_failure_is_bounded(
    tmp_path: Path, bundle_factory
):
    snapshot = _snapshot(bundle_factory)
    review_root = _review_root(tmp_path, snapshot)
    destination = tmp_path / "packet"
    body, assembly = assemble_exact_section(
        snapshot, source_generation=0, budget=_budget()
    )
    write_synthetic_review_packet(
        review_root,
        destination,
        public_repository_root=_public_root(tmp_path),
        body=body,
        assembly=assembly,
    )
    for ordinal in range(1_000):
        (destination / f"extra-{ordinal:04d}").touch()

    with pytest.raises(PackageCArtifactError, match="additional entries omitted") as caught:
        verify_synthetic_review_packet(review_root, destination)
    assert len(str(caught.value)) < 2_048


def test_packet_verifier_rejects_same_name_file_replacement_after_read(
    tmp_path: Path, bundle_factory, monkeypatch
):
    snapshot = _snapshot(bundle_factory)
    review_root = _review_root(tmp_path, snapshot)
    body, assembly = assemble_exact_section(
        snapshot, source_generation=0, budget=_budget()
    )
    destination = tmp_path / "packet"
    write_synthetic_review_packet(
        review_root,
        destination,
        public_repository_root=_public_root(tmp_path),
        body=body,
        assembly=assembly,
    )
    original = artifact_module._read_packet_file
    replaced = False

    def replace_same_name(
        directory_descriptor: int, name: str
    ) -> artifact_module._RetainedPacketFile:
        nonlocal replaced
        retained = original(directory_descriptor, name)
        if name == "assembly.json" and not replaced:
            os.unlink(name, dir_fd=directory_descriptor)
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o444,
                dir_fd=directory_descriptor,
            )
            try:
                os.write(descriptor, retained.content)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            replaced = True
        return retained

    monkeypatch.setattr(artifact_module, "_read_packet_file", replace_same_name)
    with pytest.raises(
        PackageCArtifactError,
        match="file changed after read: assembly.json",
    ):
        verify_synthetic_review_packet(review_root, destination)

    assert set(path.name for path in destination.iterdir()) == {
        "assembly.json",
        "repository.json",
        "review_packet.json",
        "section.md",
    }


def test_packet_writer_rejects_per_file_overflow_before_publication(
    tmp_path: Path, bundle_factory, monkeypatch
):
    snapshot = _snapshot(bundle_factory)
    review_root = _review_root(tmp_path, snapshot)
    body, assembly = assemble_exact_section(
        snapshot, source_generation=0, budget=_budget()
    )
    destination = tmp_path / "packet"
    repository_size = len(artifact_module._model_file_bytes(snapshot))
    monkeypatch.setattr(
        artifact_module,
        "_MAX_PACKET_FILE_BYTES",
        repository_size - 1,
    )

    with pytest.raises(PackageCArtifactError, match="repository.json"):
        write_synthetic_review_packet(
            review_root,
            destination,
            public_repository_root=_public_root(tmp_path),
            body=body,
            assembly=assembly,
        )
    assert not destination.exists()


def test_empty_repository_cannot_be_silently_exported():
    with pytest.raises(PackageCArtifactError, match="at least one sentence"):
        assemble_exact_section(
            RepositorySnapshot(), source_generation=0, budget=_budget()
        )
