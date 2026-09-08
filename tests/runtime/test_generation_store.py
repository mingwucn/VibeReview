from __future__ import annotations

import threading
import time
from multiprocessing import Process

import pytest

from vibereview.models import ThemeRecord
from vibereview.runtime.locking import AdvisoryFileLock
from vibereview.runtime.registry import IdKind
from vibereview.runtime.registry import CanonicalIdRegistry
from vibereview.runtime.repository import (
    CrashPoint,
    GenerationStore,
    InjectedCrash,
    PromotionPayload,
    StagedRepositoryValidationError,
    StaleSnapshotError,
)
from vibereview.runtime.state import RepositorySnapshot


def _add_theme(title: str = "Theme"):
    def promote(snapshot, registry):
        identifiers, registry = registry.allocate(IdKind.THEME)
        theme = ThemeRecord(
            theme_id=identifiers[0],
            title=title,
            description="test",
            origin="human",
            parent_theme_id=None,
        )
        return PromotionPayload(
            snapshot.model_copy(update={"themes": snapshot.themes + (theme,)}),
            registry,
            {"theme": theme.theme_id},
        )

    return promote


def test_initial_generation_is_complete_and_loadable(tmp_path):
    store = GenerationStore(tmp_path / "project")
    assert store.initialize() == 0
    generation, snapshot, registry = store.load_current()
    assert generation == 0
    assert snapshot == RepositorySnapshot()
    assert registry.counters == {}
    assert (store.generation_path(0) / "COMPLETE").is_file()


def test_commit_creates_new_immutable_generation(tmp_path):
    store = GenerationStore(tmp_path / "project")
    store.initialize()
    old_bytes = (store.generation_path(0) / "repository.json").read_bytes()
    result = store.commit(
        base_generation=0, dependencies={}, promotion=_add_theme("New")
    )
    assert result.generation == 1
    assert result.allocated_ids == {"theme": "T0001"}
    assert (store.generation_path(0) / "repository.json").read_bytes() == old_bytes
    generation, snapshot, _ = store.load_current()
    assert generation == 1
    assert [theme.title for theme in snapshot.themes] == ["New"]
    assert not ((store.generation_path(0) / "repository.json").stat().st_mode & 0o222)
    assert not ((store.generation_path(1) / "repository.json").stat().st_mode & 0o222)


def test_registry_allocates_query_ordinals_and_cpe_ids_in_python():
    registry = CanonicalIdRegistry()
    first, registry = registry.allocate_query("C0003", "contradiction")
    second, registry = registry.allocate_query("C0003", "contradiction")
    assert first == "Q-C0003-CON-01"
    assert second == "Q-C0003-CON-02"
    assert registry.claim_paper_evidence_id("C0003", "P0012") == "CPE-C0003-P0012"


def test_freshness_is_checked_before_promotion_and_id_allocation(tmp_path):
    store = GenerationStore(tmp_path / "project")
    store.initialize()
    store.commit(base_generation=0, dependencies={}, promotion=_add_theme("First"))
    called = False

    def stale_promotion(snapshot, registry):
        nonlocal called
        called = True
        return _add_theme("Stale")(snapshot, registry)

    with pytest.raises(StaleSnapshotError):
        store.commit(base_generation=0, dependencies={}, promotion=stale_promotion)
    assert not called


@pytest.mark.parametrize("crash_at", list(CrashPoint))
def test_crash_recovery_never_exposes_hybrid_generation(tmp_path, crash_at):
    store = GenerationStore(tmp_path / crash_at.value)
    store.initialize()
    with pytest.raises(InjectedCrash):
        store.commit(
            base_generation=0,
            dependencies={},
            promotion=_add_theme("Promoted"),
            crash_at=crash_at,
        )
    store.recover()
    generation, snapshot, _ = store.load_current()
    if crash_at is CrashPoint.AFTER_CURRENT:
        assert generation == 1
        assert [theme.title for theme in snapshot.themes] == ["Promoted"]
    else:
        assert generation == 0
        assert snapshot.themes == ()


def test_staged_repository_validation_failure_keeps_current(tmp_path):
    store = GenerationStore(tmp_path / "project")
    store.initialize()

    def invalid(snapshot, registry):
        from vibereview.models import CandidateClaim

        claim = CandidateClaim(
            claim_id="C0001",
            theme_id="T9999",
            candidate_claim="Orphan claim",
            origin="human",
            origin_refs=[],
        )
        return PromotionPayload(
            snapshot.model_copy(update={"candidate_claims": (claim,)}), registry, {}
        )

    with pytest.raises(StagedRepositoryValidationError):
        store.commit(base_generation=0, dependencies={}, promotion=invalid)
    assert store.current_generation() == 0


def test_writer_lock_serializes_threads(tmp_path):
    lock_path = tmp_path / "writer.lock"
    entered: list[str] = []
    first_entered = threading.Event()

    def first():
        with AdvisoryFileLock(lock_path, 2):
            entered.append("first")
            first_entered.set()
            time.sleep(0.1)

    def second():
        first_entered.wait(1)
        with AdvisoryFileLock(lock_path, 2):
            entered.append("second")

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert entered == ["first", "second"]


def test_writer_lock_is_released_after_process_death(tmp_path):
    lock_path = tmp_path / "writer.lock"

    def acquire_and_exit(path):
        import os

        with AdvisoryFileLock(path, 2):
            os._exit(0)

    process = Process(target=acquire_and_exit, args=(lock_path,))
    process.start()
    process.join(timeout=2)
    assert process.exitcode == 0
    with AdvisoryFileLock(lock_path, 2):
        pass
