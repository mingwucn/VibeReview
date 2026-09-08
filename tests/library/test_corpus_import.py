from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import threading

import pytest

from vibereview.enums import IndependenceStatus
from vibereview.library.git_source import PinnedGitSource
from vibereview.library.inventory import build_library_inventory
from vibereview.library.models import (
    CorpusIntegrityError,
    CorpusSelectionDocument,
    CorpusSelectionError,
    CorpusSelectionManifest,
    DirtyWorkingTreeError,
    LibraryConfig,
)
from vibereview.library.selection import (
    import_selected_corpus as _import_selected_corpus,
    load_corpus_lock,
    validate_selection_manifest,
    verify_corpus_lock,
)
from vibereview.runtime.repository import CrashPoint, GenerationStore
from vibereview.runtime.hashing import hash_bytes
from vibereview.runtime.state import RepositorySnapshot
from vibereview.models import Paper


def import_selected_corpus(
    review_root: Path,
    manifest: CorpusSelectionManifest,
    config: LibraryConfig,
    **kwargs: object,
):
    kwargs.setdefault(
        "public_repository_root", review_root.parent / "public-repository"
    )
    return _import_selected_corpus(review_root, manifest, config, **kwargs)


def make_selection(config: LibraryConfig) -> CorpusSelectionManifest:
    source = PinnedGitSource.open(config)
    inventory, _ = build_library_inventory(source, config)
    candidates = [record for record in inventory if record.document_kind.value == "candidate_paper_markdown"]
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
            for record in candidates
        ],
    )


def resources(review_root: Path) -> dict[str, str]:
    state = review_root / "state" / "generations"
    if not state.exists():
        return {}
    return {
        path.relative_to(state).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(state.rglob("library/*"))
        if path.is_file() and ".staging-" not in path.as_posix()
    }


def advance_source_pin(
    repository: Path, config: LibraryConfig, message: str
) -> LibraryConfig:
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", message],
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
        ["git", "-C", str(config.superproject_path), "commit", "-m", message],
        check=True,
        capture_output=True,
    )
    return config.model_copy(update={"expected_commit": commit})


def test_import_is_content_addressed_and_idempotent(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    _, config, expected = synthetic_library
    manifest = make_selection(config)
    review_root = tmp_path / "review"

    first = import_selected_corpus(review_root, manifest, config)
    store = GenerationStore(review_root)
    generation, snapshot, _ = store.load_current()
    lock, lock_hash = load_corpus_lock(review_root)
    verify_corpus_lock(review_root, lock, snapshot)

    assert generation == first.generation == 1
    assert first.reused is False
    assert first.corpus_lock_hash == lock_hash
    assert len(snapshot.papers) == 2
    for paper, locked in zip(snapshot.papers, lock.papers, strict=True):
        digest = paper.raw_md_hash.removeprefix("sha256:")
        assert paper.raw_md_path == (
            f"state/generations/000001/auxiliary/library/objects/sha256/{digest}/raw.md"
        )
        assert locked.git_blob_id != paper.raw_md_hash
        assert (review_root / paper.raw_md_path).read_bytes() in expected.values()

    before_current = (review_root / "state" / "CURRENT").read_bytes()
    before_resources = resources(review_root)
    second = import_selected_corpus(review_root, manifest, config)
    assert second.reused is True
    assert second.generation == first.generation
    assert (review_root / "state" / "CURRENT").read_bytes() == before_current
    assert resources(review_root) == before_resources


def test_pin_and_cleanliness_are_checked_before_destination_mutation(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    repository, config, _ = synthetic_library
    manifest = make_selection(config)
    (repository / "papers" / "local.md").write_text("untracked\n", encoding="utf-8")
    review_root = tmp_path / "must-not-exist"
    with pytest.raises(DirtyWorkingTreeError):
        import_selected_corpus(review_root, manifest, config)
    assert not review_root.exists()


def test_public_repository_root_cannot_be_omitted_from_import_api(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    _, config, _ = synthetic_library
    with pytest.raises(TypeError, match="public_repository_root"):
        _import_selected_corpus(tmp_path / "review", make_selection(config), config)


@pytest.mark.parametrize(
    "crash_at", [point for point in CrashPoint if point is not CrashPoint.AFTER_CURRENT]
)
def test_failed_import_preserves_current_and_corpus_resources(
    crash_at: CrashPoint,
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
) -> None:
    _, config, _ = synthetic_library
    manifest = make_selection(config)
    review_root = tmp_path / f"review-{crash_at.value}"
    store = GenerationStore(review_root)
    store.initialize()
    before_current = (review_root / "state" / "CURRENT").read_bytes()
    before_resources = resources(review_root)

    with pytest.raises(Exception):
        import_selected_corpus(review_root, manifest, config, crash_at=crash_at)

    assert (review_root / "state" / "CURRENT").read_bytes() == before_current
    assert resources(review_root) == before_resources
    assert store.current_generation() == 0


def test_after_current_crash_leaves_a_complete_coupled_import(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    _, config, _ = synthetic_library
    manifest = make_selection(config)
    review_root = tmp_path / "review-after-current"
    GenerationStore(review_root).initialize()
    with pytest.raises(Exception):
        import_selected_corpus(
            review_root, manifest, config, crash_at=CrashPoint.AFTER_CURRENT
        )
    store = GenerationStore(review_root)
    generation, snapshot, _ = store.load_current()
    lock, _ = load_corpus_lock(review_root)
    assert generation == 1
    verify_corpus_lock(review_root, lock, snapshot)


def test_changed_bytes_reuse_stable_paper_identity_and_preserve_old_object(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    repository, config, _ = synthetic_library
    review_root = tmp_path / "review-update"
    first = import_selected_corpus(review_root, make_selection(config), config)
    store = GenerationStore(review_root)
    _, first_snapshot, _ = store.load_current()
    alpha_before = next(p for p in first_snapshot.papers if p.doi is not None)
    old_path = review_root / alpha_before.raw_md_path
    old_bytes = old_path.read_bytes()

    source_path = repository / "papers" / "Writer - 2024 - Alpha.md"
    source_path.write_text(
        "\\cite{Alpha2024}\n# Alpha revision\n\nA revised synthetic observation.\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "revised synthetic bytes"],
        check=True,
        capture_output=True,
    )
    new_commit = subprocess.run(
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
            f"160000,{new_commit},{config.gitlink_path}",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(config.superproject_path),
            "commit",
            "-m",
            "advance synthetic pin",
        ],
        check=True,
        capture_output=True,
    )
    revised_config = config.model_copy(update={"expected_commit": new_commit})
    second = import_selected_corpus(
        review_root, make_selection(revised_config), revised_config
    )
    _, revised_snapshot, _ = store.load_current()
    alpha_after = next(p for p in revised_snapshot.papers if p.doi is not None)

    assert first.generation == 1 and second.generation == 2
    assert alpha_after.paper_id == alpha_before.paper_id
    assert alpha_after.raw_md_hash != alpha_before.raw_md_hash
    assert alpha_after.raw_md_path.startswith(
        "state/generations/000002/auxiliary/library/"
    )
    assert old_path.read_bytes() == old_bytes


def test_changed_import_source_configuration_is_not_a_false_noop(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    _, config, _ = synthetic_library
    review_root = tmp_path / "review-provenance"
    manifest = make_selection(config)
    first = import_selected_corpus(review_root, manifest, config)
    alternate = config.model_copy(update={"graph_path": "alternate-graph.json"})
    second = import_selected_corpus(review_root, manifest, alternate)
    lock, _ = load_corpus_lock(review_root)
    assert first.generation == 1
    assert second.generation == 2
    assert second.reused is False
    assert lock.import_source.graph_path == "alternate-graph.json"


def test_conflicting_doi_fails_before_review_state_exists(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    repository, config, _ = synthetic_library
    beta = repository / "papers" / "Writer - 2023 - Beta.md"
    beta.write_text("\\cite{Beta2023}\n# Beta\n\nSynthetic text.\n", encoding="utf-8")
    bibliography = repository / "references.bib"
    bibliography.write_text(
        bibliography.read_text(encoding="utf-8")
        + (
            "@article{Beta2023,\n"
            " title={Different Synthetic Study},\n"
            " year={2023},\n"
            " doi={10.5555/synthetic.alpha},\n"
            " file={Writer - 2023 - Beta.md}\n"
            "}\n"
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "conflicting fixture metadata"],
        check=True,
        capture_output=True,
    )
    new_commit = subprocess.run(
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
            f"160000,{new_commit},{config.gitlink_path}",
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
            "advance conflicting fixture pin",
        ],
        check=True,
        capture_output=True,
    )
    revised = config.model_copy(update={"expected_commit": new_commit})
    review_root = tmp_path / "must-remain-absent"
    with pytest.raises(Exception, match="identity conflicts"):
        import_selected_corpus(review_root, make_selection(revised), revised)
    assert not review_root.exists()


def test_authoritative_doi_and_source_hash_ambiguity_fails_before_promotion(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    _, config, _ = synthetic_library
    manifest = make_selection(config)
    alpha_hash = next(
        item.content_sha256
        for item in manifest.documents
        if item.source_relative_path.endswith("Alpha.md")
    )
    papers = (
        Paper(
            paper_id="P0001",
            title="Existing DOI owner",
            authors=None,
            year=None,
            doi="10.5555/synthetic.alpha",
            journal=None,
            identity_keys=["bib:existing-doi-owner"],
            study_group_id=None,
            related_publications=[],
            independence_status=IndependenceStatus.UNKNOWN,
            raw_md_path="existing/P0001/raw.md",
            source_hash="sha256:" + "1" * 64,
            raw_md_hash="sha256:" + "1" * 64,
        ),
        Paper(
            paper_id="P0002",
            title="Existing source owner",
            authors=None,
            year=None,
            doi="10.5555/other",
            journal=None,
            identity_keys=["bib:existing-source-owner"],
            study_group_id=None,
            related_publications=[],
            independence_status=IndependenceStatus.UNKNOWN,
            raw_md_path="existing/P0002/raw.md",
            source_hash=alpha_hash,
            raw_md_hash=alpha_hash,
        ),
    )
    review_root = tmp_path / "review-authoritative-conflict"
    store = GenerationStore(review_root)
    store.initialize(RepositorySnapshot(papers=papers))
    before_current = store.current_path.read_bytes()
    before_resources = resources(review_root)

    with pytest.raises(CorpusSelectionError, match="multiple Papers"):
        import_selected_corpus(review_root, manifest, config)

    assert store.current_path.read_bytes() == before_current
    assert resources(review_root) == before_resources
    assert not store.generation_path(1).exists()


def test_auxiliary_self_consistency_cannot_bypass_generation_anchor(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    _, config, _ = synthetic_library
    review_root = tmp_path / "review-anchor"
    import_selected_corpus(review_root, make_selection(config), config)
    auxiliary = review_root / "state" / "generations" / "000001" / "auxiliary"
    lock_path = auxiliary / "library" / "corpus.lock.json"
    import_path = auxiliary / "library" / "import_manifest.json"
    lock_path.chmod(0o644)
    import_path.chmod(0o644)
    lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
    lock_data["library_id"] = "tampered"
    lock_bytes = json.dumps(
        lock_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode() + b"\n"
    lock_path.write_bytes(lock_bytes)
    import_data = json.loads(import_path.read_text(encoding="utf-8"))
    import_data["corpus_lock_hash"] = hash_bytes(lock_bytes)
    import_path.write_text(
        json.dumps(
            import_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(CorpusIntegrityError, match="not anchored"):
        load_corpus_lock(review_root)


def test_lock_parser_consumes_bytes_from_same_anchored_auxiliary_scan(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, config, _ = synthetic_library
    review_root = tmp_path / "review-stable-aux-read"
    import_selected_corpus(review_root, make_selection(config), config)
    original = GenerationStore.load_generation_auxiliary
    replaced = False

    def replace_after_scan(self, generation, relative_paths):
        nonlocal replaced
        result = original(self, generation, relative_paths)
        if "library/corpus.lock.json" in relative_paths and not replaced:
            replaced = True
            path = (
                self.generation_path(generation)
                / "auxiliary/library/corpus.lock.json"
            )
            path.chmod(0o644)
            path.write_bytes(b"{}")
        return result

    monkeypatch.setattr(
        GenerationStore, "load_generation_auxiliary", replace_after_scan
    )
    lock, _ = load_corpus_lock(review_root)
    assert lock.library_id == config.library_id
    assert replaced is True


def test_concurrent_identical_import_allocates_once(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    _, config, _ = synthetic_library
    review_root = tmp_path / "review-concurrent"
    manifest = make_selection(config)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: import_selected_corpus(review_root, manifest, config),
                range(2),
            )
        )
    store = GenerationStore(review_root)
    generation, snapshot, _ = store.load_current()
    assert generation == 1
    assert len(snapshot.papers) == 2
    assert sorted(result.reused for result in results) == [False, True]


def test_later_scientific_generation_resolves_ancestral_corpus_lock(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    _, config, _ = synthetic_library
    review_root = tmp_path / "review-ancestor"
    import_selected_corpus(review_root, make_selection(config), config)
    store = GenerationStore(review_root)
    generation, _, _ = store.load_current()

    def no_op(snapshot, registry):
        from vibereview.runtime.repository import PromotionPayload

        return PromotionPayload(snapshot, registry, {})

    store.commit(base_generation=generation, dependencies={}, promotion=no_op)
    current, snapshot, _ = store.load_current()
    lock, _ = load_corpus_lock(review_root)
    assert current == 2
    assert lock.committed_generation == 1
    verify_corpus_lock(review_root, lock, snapshot)


def test_selection_order_is_canonical_for_hashes_and_id_allocation(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    _, config, _ = synthetic_library
    selection = make_selection(config)
    reversed_selection = selection.model_copy(
        update={"documents": list(reversed(selection.documents))}
    )
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first = import_selected_corpus(first_root, selection, config)
    second = import_selected_corpus(second_root, reversed_selection, config)
    first_lock, _ = load_corpus_lock(first_root)
    second_lock, _ = load_corpus_lock(second_root)

    assert first.corpus_lock_hash == second.corpus_lock_hash
    assert first_lock.selection_manifest_hash == second_lock.selection_manifest_hash
    assert [
        (paper.source_relative_path, paper.paper_id) for paper in first_lock.papers
    ] == [
        (paper.source_relative_path, paper.paper_id) for paper in second_lock.papers
    ]


def test_complete_canonical_selection_is_generation_owned(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    _, config, _ = synthetic_library
    manifest = make_selection(config)
    source = PinnedGitSource.open(config)
    inventory, _ = build_library_inventory(source, config)
    readme = next(item for item in inventory if item.source_relative_path == "README.md")
    exclusion = CorpusSelectionDocument(
        source_relative_path=readme.source_relative_path,
        content_sha256=readme.content_sha256,
        decision="exclude",
        role="administrative",
        accepted_by="synthetic-test",
        accepted_at="2030-01-02T00:00:00Z",
        reason="not a paper",
    )
    manifest = manifest.model_copy(
        update={"documents": [*reversed(manifest.documents), exclusion]}
    )
    review_root = tmp_path / "review-selection-provenance"
    import_selected_corpus(review_root, manifest, config)
    lock, _ = load_corpus_lock(review_root)
    selection_path = (
        review_root
        / "state/generations/000001/auxiliary/library/selection_manifest.json"
    )
    stored = CorpusSelectionManifest.model_validate_json(selection_path.read_bytes())

    assert hash_bytes(selection_path.read_bytes()) == lock.selection_manifest_hash
    assert [item.source_relative_path for item in stored.documents] == sorted(
        item.source_relative_path for item in manifest.documents
    )
    stored_exclusion = next(item for item in stored.documents if item.decision == "exclude")
    assert stored_exclusion.accepted_by == "synthetic-test"
    assert stored_exclusion.accepted_at == "2030-01-02T00:00:00Z"
    assert stored_exclusion.reason == "not a paper"


def test_selection_requires_one_decision_for_every_candidate_paper(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
) -> None:
    _, config, _ = synthetic_library
    manifest = make_selection(config)
    inventory, _ = build_library_inventory(PinnedGitSource.open(config), config)
    omitted = manifest.model_copy(update={"documents": manifest.documents[:-1]})
    errors = validate_selection_manifest(omitted, inventory, config=config)
    assert any("omits a candidate Markdown paper" in error for error in errors)

    extra = CorpusSelectionDocument(
        source_relative_path="papers/not-at-pin.md",
        content_sha256="sha256:" + "0" * 64,
        decision="exclude",
        role="candidate",
        accepted_by="synthetic-test",
        accepted_at="2030-01-01T00:00:00Z",
        reason="fixture extra decision",
    )
    errors = validate_selection_manifest(
        manifest.model_copy(update={"documents": [*manifest.documents, extra]}),
        inventory,
        config=config,
    )
    assert any("not present at the pinned commit" in error for error in errors)


def test_selection_count_and_total_byte_budgets_fail_before_state_creation(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    _, config, _ = synthetic_library
    manifest = make_selection(config)
    inventory, _ = build_library_inventory(PinnedGitSource.open(config), config)
    selected_paths = {item.source_relative_path for item in manifest.documents}
    total = sum(
        item.size_bytes
        for item in inventory
        if item.source_relative_path in selected_paths
    )

    count_root = tmp_path / "count-must-not-exist"
    with pytest.raises(CorpusSelectionError, match="max_selected_documents"):
        import_selected_corpus(
            count_root,
            manifest,
            config.model_copy(update={"max_selected_documents": 1}),
        )
    assert not count_root.exists()

    byte_root = tmp_path / "bytes-must-not-exist"
    with pytest.raises(CorpusSelectionError, match="max_selected_bytes"):
        import_selected_corpus(
            byte_root,
            manifest,
            config.model_copy(update={"max_selected_bytes": total - 1}),
        )
    assert not byte_root.exists()

    exact_root = tmp_path / "exact-budget"
    result = import_selected_corpus(
        exact_root,
        manifest,
        config.model_copy(
            update={
                "max_selected_documents": len(manifest.documents),
                "max_selected_bytes": total,
            }
        ),
    )
    assert result.generation == 1


def test_same_content_reimport_enriches_only_missing_canonical_metadata(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    _, config, _ = synthetic_library
    review_root = tmp_path / "review-metadata-enrichment"
    minimal = config.model_copy(update={"bibliography": None, "graph_path": None})
    first = import_selected_corpus(review_root, make_selection(minimal), minimal)
    _, first_snapshot, _ = GenerationStore(review_root).load_current()
    alpha_before = next(
        paper for paper in first_snapshot.papers if paper.title == "Alpha"
    )
    assert alpha_before.doi is None
    assert alpha_before.authors is None

    second = import_selected_corpus(review_root, make_selection(config), config)
    _, second_snapshot, _ = GenerationStore(review_root).load_current()
    alpha_after = next(
        paper for paper in second_snapshot.papers if paper.paper_id == alpha_before.paper_id
    )
    assert first.generation == 1 and second.generation == 2
    assert alpha_after.raw_md_path == alpha_before.raw_md_path
    assert alpha_after.title == "Synthetic Alpha Study"
    assert alpha_after.authors == ["A. Example", "B. Example"]
    assert alpha_after.year == 2024
    assert alpha_after.doi == "doi:10.5555/synthetic.alpha"


def test_same_content_reimport_rejects_conflicting_canonical_metadata(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    repository, config, _ = synthetic_library
    review_root = tmp_path / "review-metadata-conflict"
    import_selected_corpus(review_root, make_selection(config), config)
    store = GenerationStore(review_root)
    before_current = store.current_path.read_bytes()
    before_resources = resources(review_root)
    bibliography = repository / "references.bib"
    bibliography.write_text(
        bibliography.read_text(encoding="utf-8").replace(
            "A. Example and B. Example", "C. Different"
        ),
        encoding="utf-8",
    )
    revised = advance_source_pin(repository, config, "conflicting authors fixture")

    with pytest.raises(CorpusSelectionError, match="canonical Paper authors"):
        import_selected_corpus(review_root, make_selection(revised), revised)
    assert store.current_path.read_bytes() == before_current
    assert resources(review_root) == before_resources


def test_import_root_must_be_disjoint_from_source_and_known_public_repository(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    _, config, _ = synthetic_library
    manifest = make_selection(config)
    inside_superproject = config.superproject_path / "review-state"
    with pytest.raises(CorpusSelectionError, match="superproject"):
        import_selected_corpus(inside_superproject, manifest, config)
    assert not inside_superproject.exists()

    public_root = tmp_path / "public"
    public_root.mkdir()
    inside_public = public_root / "review-state"
    with pytest.raises(CorpusSelectionError, match="public repository"):
        import_selected_corpus(
            inside_public,
            manifest,
            config,
            public_repository_root=public_root,
        )
    assert not inside_public.exists()


def test_selected_inconsistent_bibliographic_title_fails_before_state_creation(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]], tmp_path: Path
) -> None:
    repository, config, _ = synthetic_library
    bibliography = repository / "references.bib"
    bibliography.write_text(
        bibliography.read_text(encoding="utf-8").replace(
            "Synthetic Alpha Study", "Wholly Unrelated Metadata"
        ),
        encoding="utf-8",
    )
    revised = advance_source_pin(repository, config, "conflicting title fixture")
    review_root = tmp_path / "must-not-exist-title-conflict"

    with pytest.raises(CorpusSelectionError, match="conflicting or ambiguous"):
        import_selected_corpus(review_root, make_selection(revised), revised)
    assert not review_root.exists()


def test_noop_provenance_check_holds_writer_lock(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, config, _ = synthetic_library
    review_root = tmp_path / "review-noop-race"
    manifest = make_selection(config)
    import_selected_corpus(review_root, manifest, config)
    store = GenerationStore(review_root)
    entered = threading.Event()
    release = threading.Event()
    writer_finished = threading.Event()
    import vibereview.library.selection as selection_module

    original_verify = selection_module.verify_corpus_lock

    def blocking_verify(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(selection_module, "verify_corpus_lock", blocking_verify)
    result: list[object] = []
    importer = threading.Thread(
        target=lambda: result.append(
            import_selected_corpus(review_root, manifest, config)
        )
    )
    importer.start()
    assert entered.wait(5)

    def promote(snapshot, registry):
        from vibereview.runtime.repository import PromotionPayload

        return PromotionPayload(snapshot, registry, {})

    def competing_writer() -> None:
        store.commit(base_generation=1, dependencies={}, promotion=promote)
        writer_finished.set()

    writer = threading.Thread(target=competing_writer)
    writer.start()
    assert not writer_finished.wait(0.1)
    release.set()
    importer.join(5)
    writer.join(5)
    assert result and result[0].reused is True
    assert result[0].generation == 1
    assert writer_finished.is_set()


def test_postcommit_verification_uses_exact_committed_generation(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, config, _ = synthetic_library
    review_root = tmp_path / "review-postcommit-race"
    manifest = make_selection(config)
    original_commit = GenerationStore.commit
    injected = False

    def racing_commit(self, **kwargs):
        nonlocal injected
        result = original_commit(self, **kwargs)
        if kwargs.get("staging_materializer") is not None and not injected:
            injected = True

            def promote(snapshot, registry):
                from vibereview.runtime.repository import PromotionPayload

                return PromotionPayload(snapshot, registry, {})

            original_commit(
                self,
                base_generation=result.generation,
                dependencies={},
                promotion=promote,
            )
        return result

    monkeypatch.setattr(GenerationStore, "commit", racing_commit)
    result = import_selected_corpus(review_root, manifest, config)
    assert result.generation == 1
    assert GenerationStore(review_root).current_generation() == 2
    lock, _ = load_corpus_lock(review_root, generation=result.generation)
    assert lock.committed_generation == result.generation


def test_superproject_commit_cannot_change_between_provenance_and_promotion(
    synthetic_library: tuple[Path, LibraryConfig, dict[str, bytes]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, config, _ = synthetic_library
    review_root = tmp_path / "review-superproject-race"
    original_commit = GenerationStore.commit
    advanced = False

    def advance_then_commit(self, **kwargs):
        nonlocal advanced
        if not advanced:
            advanced = True
            note = config.superproject_path / "operator-note.txt"
            note.write_text("new superproject metadata\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(config.superproject_path), "add", "operator-note.txt"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(config.superproject_path),
                    "commit",
                    "-m",
                    "advance superproject only",
                ],
                check=True,
                capture_output=True,
            )
        return original_commit(self, **kwargs)

    monkeypatch.setattr(GenerationStore, "commit", advance_then_commit)
    with pytest.raises(CorpusSelectionError, match="superproject changed"):
        import_selected_corpus(review_root, make_selection(config), config)
    store = GenerationStore(review_root)
    assert store.current_generation() == 0
    assert not store.generation_path(1).exists()
