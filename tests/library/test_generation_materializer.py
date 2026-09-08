from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

import pytest

import vibereview.runtime.repository as repository_module
from vibereview.enums import IndependenceStatus
from vibereview.models import Paper, ThemeRecord
from vibereview.runtime.hashing import hash_json
from vibereview.runtime.records import AppliedTaskReceipt
from vibereview.runtime.registry import CanonicalIdRegistry, IdKind
from vibereview.runtime.repository import (
    AuxiliaryStagingWriter,
    GenerationStore,
    PromotionPayload,
    UnsafeRepositoryEntryError,
)
from vibereview.runtime.state import RepositorySnapshot


def test_staging_materializer_failure_is_not_published(tmp_path: Path) -> None:
    store = GenerationStore(tmp_path / "review")
    store.initialize()
    _, snapshot, registry = store.load_current()

    def promote(current, current_registry):
        return PromotionPayload(current, current_registry, {})

    def fail(
        writer: AuxiliaryStagingWriter,
        generation: int,
        payload: PromotionPayload,
    ) -> None:
        writer.write_bytes("library/partial.txt", b"partial\n")
        raise RuntimeError("injected materializer failure")

    with pytest.raises(RuntimeError, match="injected materializer"):
        store.commit(
            base_generation=0,
            dependencies={},
            promotion=promote,
            staging_materializer=fail,
        )

    assert store.current_generation() == 0
    store.recover()
    assert store.current_generation() == 0
    assert not list(store.generations_dir.glob(".*.staging-*"))
    assert store.load_current()[1] == snapshot
    assert store.load_current()[2] == registry


def test_initialize_fsyncs_generation_parent_before_current_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = GenerationStore(tmp_path / "review")
    events: list[tuple[str, Path]] = []
    original_fsync = repository_module._fsync_directory_descriptor
    original_write = repository_module._atomic_write_text_at

    def recorded_fsync(descriptor: int, path: Path) -> None:
        events.append(("fsync", path))
        original_fsync(descriptor, path)

    def recorded_write(
        descriptor: int, name: str, path: Path, content: str
    ) -> None:
        if path == store.current_path:
            events.append(("current", path))
        original_write(descriptor, name, path, content)

    monkeypatch.setattr(
        repository_module, "_fsync_directory_descriptor", recorded_fsync
    )
    monkeypatch.setattr(repository_module, "_atomic_write_text_at", recorded_write)
    store.initialize()

    current_index = events.index(("current", store.current_path))
    generation_parent_indices = [
        index
        for index, event in enumerate(events)
        if event == ("fsync", store.generations_dir)
    ]
    assert generation_parent_indices
    assert max(generation_parent_indices) < current_index


def _tree_bytes(root: Path) -> tuple[tuple[str, str, bytes | str | None], ...]:
    entries: list[tuple[str, str, bytes | str | None]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if path.is_symlink():
            entries.append((relative, "symlink", os.readlink(path)))
        elif path.is_file():
            entries.append((relative, "file", path.read_bytes()))
        elif path.is_dir():
            entries.append((relative, "directory", None))
        else:
            entries.append((relative, f"mode:{info.st_mode}", None))
    return tuple(entries)


@pytest.mark.parametrize("component", ["state", "generations"])
@pytest.mark.parametrize(
    "operation", ["initialize", "writer_lock", "recover", "commit"]
)
def test_store_layout_symlink_fails_before_outside_mutation(
    component: str,
    operation: str,
    tmp_path: Path,
) -> None:
    project = tmp_path / f"review-{component}-{operation}"
    store = GenerationStore(project)
    outside = tmp_path / f"outside-{component}-{operation}"
    outside.mkdir()
    (outside / "sentinel.bin").write_bytes(b"outside must remain unchanged\x00")

    if operation == "initialize":
        project.mkdir()
    else:
        store.initialize()

    if component == "state":
        if store.state_dir.exists():
            store.state_dir.rename(project / "saved-state")
        store.state_dir.symlink_to(outside, target_is_directory=True)
    else:
        store.state_dir.mkdir(exist_ok=True)
        if store.generations_dir.exists():
            store.generations_dir.rename(project / "saved-generations")
        store.generations_dir.symlink_to(outside, target_is_directory=True)

    before = _tree_bytes(outside)

    def invoke() -> None:
        if operation == "initialize":
            store.initialize()
        elif operation == "writer_lock":
            with store.writer_lock():
                pass
        elif operation == "recover":
            store.recover()
        else:
            store.commit(
                base_generation=0,
                dependencies={},
                promotion=lambda snapshot, registry: PromotionPayload(
                    snapshot, registry, {}
                ),
            )

    with pytest.raises(UnsafeRepositoryEntryError, match="directory is unsafe"):
        invoke()

    assert _tree_bytes(outside) == before


def test_writer_lock_rejects_symlink_without_touching_target(tmp_path: Path) -> None:
    store = GenerationStore(tmp_path / "review-lock-link")
    store.initialize()
    outside = tmp_path / "outside-lock"
    outside.write_bytes(b"not a repository lock\n")
    store.writer_lock_path.unlink()
    store.writer_lock_path.symlink_to(outside)

    before = outside.read_bytes()
    with pytest.raises(OSError):
        with store.writer_lock():
            pass
    assert outside.read_bytes() == before


@pytest.mark.parametrize("component", ["state", "generations"])
def test_initialize_uses_anchored_layout_after_post_lock_swap(
    component: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GenerationStore(tmp_path / f"review-initialize-{component}-swap")
    outside = tmp_path / f"outside-initialize-{component}-swap"
    outside.mkdir()
    (outside / "sentinel.bin").write_bytes(b"outside remains unchanged\x00")
    outside_after_attack: list[tuple[tuple[str, str, bytes | str | None], ...]] = []
    original_recover = store._recover_locked

    def swap_during_recovery(layout) -> int | None:
        if component == "state":
            relocated = outside / "relocated-state"
            replacement = outside / "replacement-state"
            store.state_dir.rename(relocated)
            replacement.mkdir()
            (replacement / "generations").mkdir()
            store.state_dir.symlink_to(replacement, target_is_directory=True)
            relocated_generations = relocated / "generations"
        else:
            relocated_generations = outside / "relocated-generations"
            replacement = outside / "replacement-generations"
            store.generations_dir.rename(relocated_generations)
            replacement.mkdir()
            store.generations_dir.symlink_to(replacement, target_is_directory=True)
        injected = relocated_generations / ".000001.staging-injected"
        injected.mkdir()
        (injected / "sentinel.bin").write_bytes(b"recovery must not delete this\n")
        outside_after_attack.append(_tree_bytes(outside))
        return original_recover(layout)

    monkeypatch.setattr(store, "_recover_locked", swap_during_recovery)
    with pytest.raises(
        UnsafeRepositoryEntryError, match="state layout changed"
    ):
        store.initialize()

    assert _tree_bytes(outside) == outside_after_attack[0]


@pytest.mark.parametrize("component", ["state", "generations"])
def test_commit_uses_anchored_layout_after_promotion_swap(
    component: str,
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / f"review-commit-{component}-swap")
    store.initialize()
    outside = tmp_path / f"outside-commit-{component}-swap"
    outside.mkdir()
    (outside / "sentinel.bin").write_bytes(b"outside remains unchanged\x00")
    outside_after_attack: list[tuple[tuple[str, str, bytes | str | None], ...]] = []

    def swap_during_promotion(snapshot, registry):
        if component == "state":
            relocated = outside / "relocated-state"
            replacement = outside / "replacement-state"
            store.state_dir.rename(relocated)
            replacement.mkdir()
            (replacement / "generations").mkdir()
            store.state_dir.symlink_to(replacement, target_is_directory=True)
        else:
            relocated = outside / "relocated-generations"
            replacement = outside / "replacement-generations"
            store.generations_dir.rename(relocated)
            replacement.mkdir()
            store.generations_dir.symlink_to(replacement, target_is_directory=True)
        outside_after_attack.append(_tree_bytes(outside))
        return PromotionPayload(snapshot, registry, {})

    with pytest.raises(
        UnsafeRepositoryEntryError, match="state layout changed"
    ):
        store.commit(
            base_generation=0,
            dependencies={},
            promotion=swap_during_promotion,
        )

    assert _tree_bytes(outside) == outside_after_attack[0]


@pytest.mark.parametrize("component", ["state", "generations"])
def test_auxiliary_writer_rechecks_layout_before_each_write(
    component: str,
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / f"review-writer-{component}-swap")
    store.initialize()
    outside = tmp_path / f"outside-writer-{component}-swap"
    outside.mkdir()
    (outside / "sentinel.bin").write_bytes(b"outside remains unchanged\x00")
    outside_after_attack: list[tuple[tuple[str, str, bytes | str | None], ...]] = []

    def materialize(writer, generation, payload) -> None:
        del generation, payload
        if component == "state":
            relocated = outside / "relocated-state"
            replacement = outside / "replacement-state"
            store.state_dir.rename(relocated)
            replacement.mkdir()
            (replacement / "generations").mkdir()
            store.state_dir.symlink_to(replacement, target_is_directory=True)
        else:
            relocated = outside / "relocated-generations"
            replacement = outside / "replacement-generations"
            store.generations_dir.rename(relocated)
            replacement.mkdir()
            store.generations_dir.symlink_to(replacement, target_is_directory=True)
        outside_after_attack.append(_tree_bytes(outside))
        writer.write_bytes("library/must-not-write.bin", b"blocked\n")

    with pytest.raises(
        UnsafeRepositoryEntryError, match="state layout changed"
    ):
        store.commit(
            base_generation=0,
            dependencies={},
            promotion=lambda snapshot, registry: PromotionPayload(
                snapshot, registry, {}
            ),
            staging_materializer=materialize,
        )

    assert _tree_bytes(outside) == outside_after_attack[0]


def test_receipt_factory_result_is_normalized_before_staging(
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / "review-receipt-swap")
    store.initialize()
    outside = tmp_path / "outside-receipt-swap"
    outside.mkdir()
    (outside / "sentinel.bin").write_bytes(b"outside remains unchanged\x00")
    outside_after_attack: list[tuple[tuple[str, str, bytes | str | None], ...]] = []

    class RelocatingReceipt(AppliedTaskReceipt):
        def model_dump(self, *args, **kwargs):
            relocated = outside / "relocated-state"
            replacement = outside / "replacement-state"
            store.state_dir.rename(relocated)
            replacement.mkdir()
            (replacement / "generations").mkdir()
            store.state_dir.symlink_to(replacement, target_is_directory=True)
            outside_after_attack.append(_tree_bytes(outside))
            return super().model_dump(*args, **kwargs)

    def receipt(next_generation, snapshot, payload):
        del snapshot, payload
        return RelocatingReceipt.model_validate(
            {
                "input_identity_key": "sha256:" + "1" * 64,
                "semantic_task_key": "sha256:" + "2" * 64,
                "task_type": "generate_candidate_claims",
                "task_spec_version": "synthetic",
                "proposal_hash": "sha256:" + "3" * 64,
                "proposal_payload": {},
                "semantic_fingerprint": {
                    "validator_fingerprint": "sha256:" + "4" * 64,
                    "promotion_handler_fingerprint": "sha256:" + "5" * 64,
                    "disposition_handler_fingerprint": "sha256:" + "6" * 64,
                    "scientific_contract_version": "1.5.1b",
                    "runtime_contract_version": "synthetic",
                    "combined_fingerprint": "sha256:" + "7" * 64,
                },
                "source_generation": 0,
                "committed_generation": next_generation,
                "canonical_objects": [],
                "local_ref_map": {},
                "recorded_transition": {
                    "scientific_disposition": "VALID",
                    "canonicalized": True,
                    "downstream_eligible": True,
                },
                "engine": "synthetic",
                "engine_version": None,
                "accepted_attempt_id": "TASK0001/01-synthetic",
            }
        )

    with pytest.raises(
        UnsafeRepositoryEntryError, match="state layout changed"
    ):
        store.commit(
            base_generation=0,
            dependencies={},
            promotion=lambda snapshot, registry: PromotionPayload(
                snapshot, registry, {}
            ),
            receipt_factory=receipt,
        )

    assert _tree_bytes(outside) == outside_after_attack[0]


def test_initial_snapshot_is_normalized_before_generation_write(
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / "review-initial-snapshot-swap")
    outside = tmp_path / "outside-initial-snapshot-swap"
    outside.mkdir()
    (outside / "sentinel.bin").write_bytes(b"outside remains unchanged\x00")
    outside_after_attack: list[
        tuple[tuple[str, str, bytes | str | None], ...]
    ] = []

    class RelocatingSnapshot(RepositorySnapshot):
        def model_dump(self, *args, **kwargs):
            relocated = outside / "relocated-state"
            replacement = outside / "replacement-state"
            store.state_dir.rename(relocated)
            replacement.mkdir()
            (replacement / "generations").mkdir()
            store.state_dir.symlink_to(replacement, target_is_directory=True)
            outside_after_attack.append(_tree_bytes(outside))
            return super().model_dump(*args, **kwargs)

    with pytest.raises(
        UnsafeRepositoryEntryError, match="state layout changed"
    ):
        store.initialize(RelocatingSnapshot())

    assert _tree_bytes(outside) == outside_after_attack[0]


def test_staging_writer_rejects_escape_without_touching_outside_or_core(
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / "review")
    store.initialize()
    before_repository = (
        store.generation_path(0) / "repository.json"
    ).read_bytes()
    outside = tmp_path / "outside.txt"

    def promote(current, current_registry):
        return PromotionPayload(current, current_registry, {})

    def escape(
        writer: AuxiliaryStagingWriter,
        generation: int,
        payload: PromotionPayload,
    ) -> None:
        writer.write_bytes("../repository.json", b"replaced\n")

    with pytest.raises(ValueError, match="canonical POSIX"):
        store.commit(
            base_generation=0,
            dependencies={},
            promotion=promote,
            staging_materializer=escape,
        )
    store.recover()
    assert store.current_generation() == 0
    assert (store.generation_path(0) / "repository.json").read_bytes() == before_repository
    assert not outside.exists()


@pytest.mark.parametrize("attack", ["unregistered", "symlink", "hardlink", "fifo", "replace"])
def test_auxiliary_tree_audit_rejects_malicious_callback_entries(
    attack: str, tmp_path: Path
) -> None:
    store = GenerationStore(tmp_path / "review")
    store.initialize()
    outside = tmp_path / "outside.txt"
    outside.write_text("unchanged\n", encoding="utf-8")
    before = outside.read_bytes()

    def promote(current, current_registry):
        return PromotionPayload(current, current_registry, {})

    def malicious(
        writer: AuxiliaryStagingWriter,
        generation: int,
        payload: PromotionPayload,
    ) -> None:
        writer.write_bytes("registered.txt", b"registered\n")
        stage = next(store.generations_dir.glob(".*.staging-*"))
        auxiliary = stage / "auxiliary"
        if attack == "unregistered":
            (auxiliary / "rogue.txt").write_text("rogue\n", encoding="utf-8")
        elif attack == "symlink":
            (auxiliary / "rogue.txt").symlink_to(outside)
        elif attack == "hardlink":
            os.link(outside, auxiliary / "rogue.txt")
        elif attack == "fifo":
            os.mkfifo(auxiliary / "rogue.fifo")
        else:
            registered = auxiliary / "registered.txt"
            registered.unlink()
            registered.write_text("replacement\n", encoding="utf-8")

    with pytest.raises(ValueError):
        store.commit(
            base_generation=0,
            dependencies={},
            promotion=promote,
            staging_materializer=malicious,
        )
    assert store.current_generation() == 0
    store.recover()
    assert not list(store.generations_dir.glob(".*.staging-*"))
    assert outside.read_bytes() == before


def test_materializer_cannot_publish_modified_core_files(tmp_path: Path) -> None:
    store = GenerationStore(tmp_path / "review")
    store.initialize()
    active_repository = store.generation_path(0) / "repository.json"
    before = active_repository.read_bytes()

    def promote(current, current_registry):
        return PromotionPayload(current, current_registry, {})

    def modify_core(
        writer: AuxiliaryStagingWriter,
        generation: int,
        payload: PromotionPayload,
    ) -> None:
        stage = next(store.generations_dir.glob(".*.staging-*"))
        (stage / "repository.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="changed core"):
        store.commit(
            base_generation=0,
            dependencies={},
            promotion=promote,
            staging_materializer=modify_core,
        )
    store.recover()
    assert store.current_generation() == 0
    assert active_repository.read_bytes() == before


@pytest.mark.parametrize("attack", ["symlink", "hardlink"])
def test_materializer_cannot_replace_core_with_same_bytes_external_entry(
    attack: str, tmp_path: Path
) -> None:
    store = GenerationStore(tmp_path / "review")
    store.initialize()
    outside = tmp_path / "outside.json"

    def promote(current, current_registry):
        return PromotionPayload(current, current_registry, {})

    def replace_core(
        writer: AuxiliaryStagingWriter,
        generation: int,
        payload: PromotionPayload,
    ) -> None:
        stage = next(store.generations_dir.glob(".*.staging-*"))
        core = stage / "repository.json"
        outside.write_bytes(core.read_bytes())
        core.unlink()
        if attack == "symlink":
            core.symlink_to(outside)
        else:
            os.link(outside, core)

    with pytest.raises(ValueError, match="changed core"):
        store.commit(
            base_generation=0,
            dependencies={},
            promotion=promote,
            staging_materializer=replace_core,
        )
    assert store.current_generation() == 0
    assert outside.stat().st_mode & 0o200
    store.recover()
    assert not list(store.generations_dir.glob(".*.staging-*"))


def test_load_rejects_same_bytes_symlinked_core_file(tmp_path: Path) -> None:
    store = GenerationStore(tmp_path / "review")
    store.initialize()
    core = store.generation_path(0) / "repository.json"
    outside = tmp_path / "outside.json"
    outside.write_bytes(core.read_bytes())
    store.generation_path(0).chmod(0o755)
    core.unlink()
    core.symlink_to(outside)

    with pytest.raises(ValueError, match="control file is unsafe"):
        store.load_generation(0)
    assert outside.stat().st_mode & 0o200


def test_current_pointer_symlink_is_never_followed_or_repaired(tmp_path: Path) -> None:
    store = GenerationStore(tmp_path / "review")
    store.initialize()
    outside = tmp_path / "outside-current"
    outside.write_text("000000\n", encoding="utf-8")
    store.current_path.unlink()
    store.current_path.symlink_to(outside)

    with pytest.raises(ValueError, match="control file is unsafe"):
        store.current_generation()
    with pytest.raises(ValueError, match="control file is unsafe"):
        store.recover()
    assert outside.read_text(encoding="utf-8") == "000000\n"


def test_current_pointer_read_is_tightly_bounded(tmp_path: Path) -> None:
    store = GenerationStore(tmp_path / "review")
    store.initialize()
    store.current_path.write_bytes(b"0" * 65)
    with pytest.raises(ValueError, match="byte limit"):
        store.current_generation()


def test_materializer_receives_detached_promotion_payload(tmp_path: Path) -> None:
    store = GenerationStore(tmp_path / "review")
    store.initialize()

    def promote(current, current_registry):
        paper_ids, next_registry = current_registry.allocate(IdKind.PAPER)
        paper = Paper(
            paper_id=paper_ids[0],
            title="Stable title",
            authors=None,
            year=None,
            doi=None,
            journal=None,
            identity_keys=["sha256:" + "a" * 64],
            study_group_id=None,
            related_publications=[],
            independence_status=IndependenceStatus.UNKNOWN,
            raw_md_path="objects/raw.md",
            source_hash="sha256:" + "a" * 64,
            raw_md_hash="sha256:" + "a" * 64,
        )
        return PromotionPayload(
            current.model_copy(update={"papers": (paper,)}),
            next_registry,
            {"paper": paper.paper_id},
        )

    def mutate_copy(
        writer: AuxiliaryStagingWriter,
        generation: int,
        payload: PromotionPayload,
    ) -> None:
        payload.snapshot.papers[0].title = "Mutated callback title"
        payload.registry.counters[IdKind.PAPER] = 999
        payload.allocated_ids["paper"] = "P0999"

    result = store.commit(
        base_generation=0,
        dependencies={},
        promotion=promote,
        staging_materializer=mutate_copy,
    )
    _, snapshot, registry = store.load_current()
    assert snapshot.papers[0].title == "Stable title"
    assert registry.counters[IdKind.PAPER] == 1
    assert result.allocated_ids == {"paper": "P0001"}
    generation = store.generation_path(result.generation)
    assert generation.stat().st_mode & 0o222 == 0
    assert (generation / "auxiliary").stat().st_mode & 0o222 == 0


@pytest.mark.parametrize(
    "identifier",
    [
        "T0001",
        "P0001",
        "C0001",
        "R0001",
        "E0001",
        "CF0001",
        "PF0001",
        "PR0001",
        "SA0001",
        "RS0001",
        "RSA0001",
        "Q-C0001-SUP-01",
    ],
)
def test_registry_coverage_rejects_every_unrepresented_allocated_namespace(
    identifier: str,
) -> None:
    with pytest.raises(ValueError, match="does not cover"):
        CanonicalIdRegistry().validate_covers_identifiers([identifier])
    covering = CanonicalIdRegistry.from_identifiers([identifier])
    covering.validate_covers_identifiers([identifier])


def test_commit_rejects_snapshot_whose_registry_was_not_advanced(tmp_path: Path) -> None:
    store = GenerationStore(tmp_path / "review")
    store.initialize()

    def promote(snapshot, registry):
        theme = ThemeRecord(
            theme_id="T0001",
            title="Unregistered",
            description="fixture",
            origin="human",
            parent_theme_id=None,
        )
        return PromotionPayload(
            snapshot.model_copy(update={"themes": (theme,)}), registry, {}
        )

    with pytest.raises(Exception, match="does not cover theme"):
        store.commit(base_generation=0, dependencies={}, promotion=promote)
    assert store.current_generation() == 0


def test_load_rejects_coherently_rehashed_incomplete_registry(tmp_path: Path) -> None:
    paper = Paper(
        paper_id="P0001",
        title="Fixture",
        authors=None,
        year=None,
        doi=None,
        journal=None,
        identity_keys=["sha256:" + "b" * 64],
        study_group_id=None,
        related_publications=[],
        independence_status=IndependenceStatus.UNKNOWN,
        raw_md_path="objects/raw.md",
        source_hash="sha256:" + "b" * 64,
        raw_md_hash="sha256:" + "b" * 64,
    )
    store = GenerationStore(tmp_path / "review")
    store.initialize(RepositorySnapshot(papers=(paper,)))
    generation = store.generation_path(0)
    generation.chmod(0o755)
    registry_path = generation / "registry.json"
    manifest_path = generation / "generation_manifest.json"
    registry_path.chmod(0o644)
    manifest_path.chmod(0o644)
    empty = CanonicalIdRegistry()
    registry_path.write_text(empty.model_dump_json(indent=2) + "\n", encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["registry_hash"] = hash_json(empty.model_dump(mode="json"))
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="does not cover paper"):
        store.load_generation(0)


@pytest.mark.parametrize("budget", ["file", "total", "entries"])
def test_auxiliary_materialization_budgets_fail_before_publication(
    budget: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = GenerationStore(tmp_path / f"review-{budget}")
    store.initialize()

    def promote(snapshot, registry):
        return PromotionPayload(snapshot, registry, {})

    def exceed(
        writer: AuxiliaryStagingWriter,
        generation: int,
        payload: PromotionPayload,
    ) -> None:
        if budget == "file":
            writer.write_bytes("large.bin", b"12345")
        elif budget == "total":
            writer.write_bytes("one.bin", b"123")
            writer.write_bytes("two.bin", b"456")
        else:
            writer.write_bytes("a/b/value.bin", b"x")

    if budget == "file":
        monkeypatch.setattr(repository_module, "MAX_AUXILIARY_FILE_BYTES", 4)
    elif budget == "total":
        monkeypatch.setattr(repository_module, "MAX_AUXILIARY_TOTAL_BYTES", 5)
    else:
        monkeypatch.setattr(repository_module, "MAX_AUXILIARY_ENTRIES", 2)

    with pytest.raises(ValueError, match="materialization"):
        store.commit(
            base_generation=0,
            dependencies={},
            promotion=promote,
            staging_materializer=exceed,
        )
    assert store.current_generation() == 0


def test_writer_never_follows_a_malicious_parent_symlink(tmp_path: Path) -> None:
    store = GenerationStore(tmp_path / "review")
    store.initialize()
    outside = tmp_path / "outside"
    outside.mkdir()

    def promote(current, current_registry):
        return PromotionPayload(current, current_registry, {})

    def attack(
        writer: AuxiliaryStagingWriter,
        generation: int,
        payload: PromotionPayload,
    ) -> None:
        stage = next(store.generations_dir.glob(".*.staging-*"))
        (stage / "auxiliary" / "escape").symlink_to(outside, target_is_directory=True)
        writer.write_bytes("escape/pwned.txt", b"must not escape\n")

    with pytest.raises(OSError):
        store.commit(
            base_generation=0,
            dependencies={},
            promotion=promote,
            staging_materializer=attack,
        )
    store.recover()
    assert store.current_generation() == 0
    assert not (outside / "pwned.txt").exists()


def test_generation_directory_replay_is_rejected_by_manifest_owner(
    tmp_path: Path,
) -> None:
    store = GenerationStore(tmp_path / "review")
    store.initialize()

    def promote(snapshot, registry):
        return PromotionPayload(snapshot, registry, {})

    result = store.commit(base_generation=0, dependencies={}, promotion=promote)
    replay = store.generation_path(2)
    shutil.copytree(store.generation_path(result.generation), replay)
    store.current_path.write_text("000002\n", encoding="utf-8")

    with pytest.raises(ValueError, match="owner or lineage mismatch"):
        store.load_current()


@pytest.mark.parametrize(
    ("field", "value"),
    [("generation", 7), ("previous_generation", None)],
)
def test_manifest_owner_and_previous_generation_are_not_relabelable(
    field: str, value: object, tmp_path: Path
) -> None:
    store = GenerationStore(tmp_path / "review")
    store.initialize()

    def promote(snapshot, registry):
        return PromotionPayload(snapshot, registry, {})

    result = store.commit(base_generation=0, dependencies={}, promotion=promote)
    generation = store.generation_path(result.generation)
    generation.chmod(0o755)
    manifest_path = generation / "generation_manifest.json"
    manifest_path.chmod(0o644)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="owner or lineage mismatch"):
        store.load_generation(result.generation)
