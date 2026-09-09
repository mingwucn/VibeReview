from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from vibereview.models import ThemeRecord
from vibereview.library.git_source import PinnedGitSource
from vibereview.library.inventory import build_library_inventory
from vibereview.library.models import (
    CorpusIntegrityError,
    CorpusSelectionDocument,
    CorpusSelectionManifest,
    DocumentKind,
    LibraryConfig,
    PathSecurityError,
)
from vibereview.library.pilot_setup import (
    PILOT_CORPUS_FACT_TEXT,
    PILOT_HUMAN_REVIEW_FACT_TEXT,
    PILOT_SCOPE_FACT_TEXT,
    register_synthetic_pilot_setup,
)
from vibereview.library.selection import import_selected_corpus, load_corpus_lock
from vibereview.runtime.hashing import hash_text
from vibereview.runtime.pilot_journal import load_pilot_run_registration
from vibereview.runtime.pilot_manifest import compute_package_c_implementation_fingerprint
from vibereview.runtime.pilot_records import (
    FivePaperPilotBudget,
    PilotEngineRoleBinding,
    PilotRunManifest,
    compute_pilot_engine_role_plan_hash,
)
from vibereview.runtime.records import TaskType
from vibereview.runtime.registry import IdKind
from vibereview.runtime.repository import (
    CrashPoint,
    GenerationStore,
    InjectedCrash,
    PromotionPayload,
    StaleSnapshotError,
)
from vibereview.runtime.state import RepositorySnapshot



def _run_git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _five_paper_library(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> LibraryConfig:
    repository, config, _ = synthetic_library
    for ordinal in range(3, 6):
        (repository / "papers" / f"Fixture - 2030 - Paper {ordinal}.md").write_text(
            f"# Synthetic Paper {ordinal}\n\n"
            f"A fictitious observation numbered {ordinal}.\n",
            encoding="utf-8",
        )
    _run_git(repository, "add", "papers")
    _run_git(repository, "commit", "-m", "add synthetic five-paper pilot fixture")
    commit = _run_git(repository, "rev-parse", "HEAD")
    _run_git(config.superproject_path, "add", config.gitlink_path)
    _run_git(
        config.superproject_path,
        "commit",
        "-m",
        "advance synthetic library fixture",
    )
    return config.model_copy(update={"expected_commit": commit})


def _selection(config: LibraryConfig) -> CorpusSelectionManifest:
    inventory, _ = build_library_inventory(PinnedGitSource.open(config), config)
    documents = [
        CorpusSelectionDocument(
            source_relative_path=item.source_relative_path,
            content_sha256=item.content_sha256,
            decision="include",
            role="synthetic_five_paper_pilot",
            accepted_by="synthetic-fixture",
            accepted_at="2030-01-01T00:00:00Z",
            reason="Exercise deterministic pilot setup only.",
        )
        for item in inventory
        if item.document_kind is DocumentKind.CANDIDATE_PAPER_MARKDOWN
    ]
    assert len(documents) == 5
    return CorpusSelectionManifest(
        library_id=config.library_id,
        source_commit=config.expected_commit,
        documents=documents,
    )


def _prepared_case(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
) -> tuple[Path, PilotRunManifest]:
    config = _five_paper_library(synthetic_library)
    review_root = tmp_path / "private-review"
    imported = import_selected_corpus(
        review_root,
        _selection(config),
        config,
        public_repository_root=tmp_path / "public-repository",
    )
    lock, lock_hash = load_corpus_lock(review_root, generation=imported.generation)
    paper_hashes = tuple(
        paper.source_hash for paper in sorted(lock.papers, key=lambda item: item.paper_id)
    )
    binding = PilotEngineRoleBinding(
        engine="synthetic-mock",
        engine_version="1",
        safe_configuration_hash=hash_text("mock engine plan"),
    )
    plan = {task_type: binding for task_type in TaskType}
    manifest = PilotRunManifest(
        run_id="RUN-synthetic-five-paper",
        created_at="2030-01-01T00:00:00+00:00",
        topic="Synthetic five-paper setup topic.",
        source_generation=imported.generation,
        corpus_lock_hash=lock_hash,
        selection_manifest_hash=lock.selection_manifest_hash,
        paper_source_hashes=paper_hashes,
        discovery_resource_hashes=(hash_text("discovery one"), hash_text("discovery two")),
        engine_role_plan=plan,
        engine_role_plan_hash=compute_pilot_engine_role_plan_hash(plan),
        prompt_fingerprints={task_type: hash_text("prompt") for task_type in TaskType},
        schema_fingerprints={task_type: hash_text("schema") for task_type in TaskType},
        validator_fingerprint=hash_text("validator"),
        package_c_implementation_fingerprint=compute_package_c_implementation_fingerprint(),
        budget=FivePaperPilotBudget(),
    )
    return review_root, manifest


def test_setup_atomically_registers_only_deterministic_facts_and_run_policy(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
) -> None:
    review_root, manifest = _prepared_case(synthetic_library, tmp_path)
    result = register_synthetic_pilot_setup(
        review_root,
        manifest,
        public_repository_root=tmp_path / "public-repository",
    )

    assert result.commit_performed is True
    assert result.reused_generation is None
    assert result.generation == manifest.source_generation + 1
    generation, snapshot, registry = GenerationStore(review_root).load_current()
    assert generation == result.generation
    assert [item.text for item in snapshot.corpus_facts] == [
        PILOT_CORPUS_FACT_TEXT
    ]
    assert [item.text for item in snapshot.process_facts] == [
        PILOT_SCOPE_FACT_TEXT,
        PILOT_HUMAN_REVIEW_FACT_TEXT,
    ]
    assert len(snapshot.corpus_facts[0].source_paper_ids) == 5
    assert result.corpus_fact_id == snapshot.corpus_facts[0].corpus_fact_id
    assert result.scope_process_fact_id == snapshot.process_facts[0].process_fact_id
    assert (
        result.human_review_process_fact_id
        == snapshot.process_facts[1].process_fact_id
    )
    registry.validate_covers_identifiers(snapshot.all_identifiers())
    manifest_path = review_root / result.run_manifest_path
    assert not (manifest_path.stat().st_mode & 0o222)
    assert PilotRunManifest.model_validate_json(manifest_path.read_bytes()) == manifest
    registration, registered_manifest = load_pilot_run_registration(
        review_root, result.registration, expected_manifest=manifest
    )
    assert registered_manifest == manifest
    assert registration.corpus_fact_id == result.corpus_fact_id


def test_exact_setup_reuses_original_generation_after_unrelated_advancement(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
) -> None:
    review_root, manifest = _prepared_case(synthetic_library, tmp_path)
    first = register_synthetic_pilot_setup(
        review_root,
        manifest,
        public_repository_root=tmp_path / "public-repository",
    )
    store = GenerationStore(review_root)
    later = store.commit(
        base_generation=first.generation,
        dependencies={},
        promotion=lambda snapshot, registry: PromotionPayload(
            snapshot, registry, {}
        ),
    )

    second = register_synthetic_pilot_setup(
        review_root,
        manifest,
        public_repository_root=tmp_path / "public-repository",
    )

    assert second.commit_performed is False
    assert second.generation == later.generation
    assert second.reused_generation == first.generation
    assert second.corpus_fact_id == first.corpus_fact_id
    assert second.scope_process_fact_id == first.scope_process_fact_id
    assert second.human_review_process_fact_id == first.human_review_process_fact_id
    assert store.current_generation() == later.generation


def test_stale_different_run_cannot_silently_reuse_setup(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
) -> None:
    review_root, manifest = _prepared_case(synthetic_library, tmp_path)
    store = GenerationStore(review_root)
    store.commit(
        base_generation=manifest.source_generation,
        dependencies={},
        promotion=lambda snapshot, registry: PromotionPayload(
            snapshot, registry, {}
        ),
    )

    with pytest.raises((StaleSnapshotError, ValueError)):
        register_synthetic_pilot_setup(
            review_root,
            manifest,
            public_repository_root=tmp_path / "public-repository",
        )

    generation, snapshot, _ = store.load_current()
    assert generation == manifest.source_generation + 1
    assert snapshot.corpus_facts == ()
    assert snapshot.process_facts == ()


def test_pre_current_crash_leaves_no_partial_setup(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
) -> None:
    review_root, manifest = _prepared_case(synthetic_library, tmp_path)
    with pytest.raises(InjectedCrash):
        register_synthetic_pilot_setup(
            review_root,
            manifest,
            public_repository_root=tmp_path / "public-repository",
            crash_at=CrashPoint.AFTER_COMPLETION_MARKER_BEFORE_CURRENT,
        )

    store = GenerationStore(review_root)
    generation, snapshot, registry = store.load_current()
    assert generation == manifest.source_generation
    assert snapshot.corpus_facts == ()
    assert snapshot.process_facts == ()
    assert registry.counters.get("corpus_fact", 0) == 0
    assert registry.counters.get("process_fact", 0) == 0


def test_manifest_paper_hash_order_is_fail_closed_before_mutation(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
) -> None:
    review_root, manifest = _prepared_case(synthetic_library, tmp_path)
    changed = manifest.model_copy(
        update={"paper_source_hashes": tuple(reversed(manifest.paper_source_hashes))}
    )
    with pytest.raises(CorpusIntegrityError, match="canonical five-paper order"):
        register_synthetic_pilot_setup(
            review_root,
            changed,
            public_repository_root=tmp_path / "public-repository",
        )
    assert GenerationStore(review_root).current_generation() == manifest.source_generation


def test_setup_rejects_a_canonically_contaminated_source_generation(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
) -> None:
    config = _five_paper_library(synthetic_library)
    review_root = tmp_path / "private-review"
    GenerationStore(review_root).initialize(
        RepositorySnapshot(
            themes=(
                ThemeRecord(
                    theme_id="T0001",
                    title="Pre-existing unrelated theme",
                    description="Must not enter a fresh synthetic pilot source.",
                    origin="generated",
                    parent_theme_id=None,
                ),
            )
        )
    )
    imported = import_selected_corpus(
        review_root,
        _selection(config),
        config,
        public_repository_root=tmp_path / "public-repository",
    )
    lock, lock_hash = load_corpus_lock(review_root, generation=imported.generation)
    paper_hashes = tuple(
        paper.source_hash
        for paper in sorted(lock.papers, key=lambda item: item.paper_id)
    )
    binding = PilotEngineRoleBinding(
        engine="synthetic-mock",
        engine_version="1",
        safe_configuration_hash=hash_text("mock engine plan"),
    )
    plan = {task_type: binding for task_type in TaskType}
    manifest = PilotRunManifest(
        run_id="RUN-contaminated-source",
        created_at="2030-01-01T00:00:00+00:00",
        topic="Synthetic contaminated-source topic.",
        source_generation=imported.generation,
        corpus_lock_hash=lock_hash,
        selection_manifest_hash=lock.selection_manifest_hash,
        paper_source_hashes=paper_hashes,
        discovery_resource_hashes=(hash_text("discovery one"), hash_text("discovery two")),
        engine_role_plan=plan,
        engine_role_plan_hash=compute_pilot_engine_role_plan_hash(plan),
        prompt_fingerprints={task_type: hash_text("prompt") for task_type in TaskType},
        schema_fingerprints={task_type: hash_text("schema") for task_type in TaskType},
        validator_fingerprint=hash_text("validator"),
        package_c_implementation_fingerprint=compute_package_c_implementation_fingerprint(),
        budget=FivePaperPilotBudget(),
    )

    with pytest.raises(CorpusIntegrityError, match="fresh paper-only"):
        register_synthetic_pilot_setup(
            review_root,
            manifest,
            public_repository_root=tmp_path / "public-repository",
        )

    assert GenerationStore(review_root).current_generation() == imported.generation


def test_setup_refuses_a_review_root_inside_the_public_repository(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
) -> None:
    review_root, manifest = _prepared_case(synthetic_library, tmp_path)
    with pytest.raises(PathSecurityError, match="must be disjoint"):
        register_synthetic_pilot_setup(
            review_root,
            manifest,
            public_repository_root=review_root.parent,
        )
    assert GenerationStore(review_root).current_generation() == manifest.source_generation


def test_setup_reuse_rejects_a_contradictory_duplicate_process_source_key(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
) -> None:
    review_root, manifest = _prepared_case(synthetic_library, tmp_path)
    first = register_synthetic_pilot_setup(
        review_root,
        manifest,
        public_repository_root=tmp_path / "public-repository",
    )
    store = GenerationStore(review_root)

    def add_contradiction(snapshot, registry):
        from vibereview.models import ReviewProcessFact

        process_ids, registry = registry.allocate(IdKind.PROCESS_FACT)
        original = next(
            item
            for item in snapshot.process_facts
            if item.process_fact_id == first.human_review_process_fact_id
        )
        contradictory = ReviewProcessFact(
            process_fact_id=process_ids[0],
            text="An authorized publication sign-off was performed.",
            source_type=original.source_type,
            source_key=original.source_key,
        )
        return PromotionPayload(
            snapshot.model_copy(
                update={
                    "process_facts": snapshot.process_facts + (contradictory,)
                }
            ),
            registry,
            {"contradiction": contradictory.process_fact_id},
        )

    store.commit(
        base_generation=first.generation,
        dependencies={},
        promotion=add_contradiction,
    )

    with pytest.raises(CorpusIntegrityError, match="unique exact pilot fact"):
        register_synthetic_pilot_setup(
            review_root,
            manifest,
            public_repository_root=tmp_path / "public-repository",
        )
