"""Validated, generation-coupled import of pinned Markdown objects."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any

from vibereview.enums import IndependenceStatus
from vibereview.models import Paper
from vibereview.runtime.hashing import canonical_json_bytes, hash_bytes, hash_json
from vibereview.runtime.locking import AdvisoryFileLock
from vibereview.runtime.registry import CanonicalIdRegistry, IdKind
from vibereview.runtime.repository import (
    CrashPoint,
    AuxiliaryStagingWriter,
    GenerationStore,
    PromotionPayload,
    atomic_write_text,
    read_contained_regular_file,
)
from vibereview.runtime.state import RepositorySnapshot

from .bibliography import load_bibliography
from .git_source import PinnedGitSource, compute_content_sha256
from .graph import ReadOnlyGraphAdapter
from .inventory import build_library_inventory, extract_title_candidate
from .models import (
    CorpusImportResult,
    CorpusImportSource,
    CorpusIntegrityError,
    CorpusNotImportedError,
    CorpusLockManifest,
    CorpusLockPaper,
    CorpusSelectionError,
    CorpusSelectionManifest,
    DocumentKind,
    GenerationLibraryManifest,
    LibraryConfig,
    LibraryDocumentRecord,
    LibrarySourceObject,
)
from .resolver import resolve_source_mappings


IMPORT_LOCK = ".library-import.lock"
LOCK_FILE = "library/corpus.lock.json"
IMPORT_MANIFEST_FILE = "library/import_manifest.json"
SELECTION_MANIFEST_FILE = "library/selection_manifest.json"
GENERATION_LOCK_FILE = f"auxiliary/{LOCK_FILE}"
GENERATION_IMPORT_MANIFEST_FILE = f"auxiliary/{IMPORT_MANIFEST_FILE}"
_RAW_PATH_RE = re.compile(
    r"^state/generations/([0-9]{6})/auxiliary/library/objects/sha256/([0-9a-f]{64})/raw\.md$"
)


def load_selection_manifest(path: Path) -> CorpusSelectionManifest:
    if not path.exists():
        raise FileNotFoundError(f"selection manifest not found: {path}")
    content, _ = read_contained_regular_file(
        path.parent, path.name, max_bytes=16 * 1024 * 1024
    )
    return CorpusSelectionManifest.model_validate_json(content)


def save_selection_manifest(manifest: CorpusSelectionManifest, path: Path) -> None:
    atomic_write_text(path, manifest.model_dump_json(indent=2) + "\n")


def validate_selection_manifest(
    manifest: CorpusSelectionManifest,
    inventory: list[LibraryDocumentRecord],
    *,
    config: LibraryConfig | None = None,
) -> list[str]:
    errors: list[str] = []
    if not inventory:
        return ["inventory is empty"]
    if {item.source_commit for item in inventory} != {manifest.source_commit}:
        errors.append("selection source_commit does not equal the inventory commit")
    if {item.library_id for item in inventory} != {manifest.library_id}:
        errors.append("selection library_id does not equal the inventory library")
    by_path = {item.source_relative_path: item for item in inventory}
    candidate_paths = {
        item.source_relative_path
        for item in inventory
        if item.document_kind is DocumentKind.CANDIDATE_PAPER_MARKDOWN
    }
    decision_paths = {
        item.source_relative_path
        for item in manifest.documents
        if item.source_relative_path in candidate_paths
    }
    for missing in sorted(candidate_paths - decision_paths):
        errors.append(f"selection omits a candidate Markdown paper: {missing}")
    included_hashes: set[str] = set()
    included_count = 0
    included_bytes = 0
    for selected in manifest.documents:
        record = by_path.get(selected.source_relative_path)
        if record is None:
            errors.append(
                f"selected path is not present at the pinned commit: {selected.source_relative_path}"
            )
            continue
        if record.content_sha256 != selected.content_sha256:
            errors.append(
                f"content hash mismatch for selected path: {selected.source_relative_path}"
            )
        if selected.decision == "include":
            included_count += 1
            included_bytes += record.size_bytes
            if record.document_kind is not DocumentKind.CANDIDATE_PAPER_MARKDOWN:
                errors.append(
                    f"included path is not a candidate Markdown paper: {selected.source_relative_path}"
                )
            if selected.content_sha256 in included_hashes:
                errors.append("included documents must have distinct content hashes")
            included_hashes.add(selected.content_sha256)
    if included_count == 0:
        errors.append("selection must include at least one paper")
    max_documents = config.max_selected_documents if config is not None else 1_000
    max_bytes = config.max_selected_bytes if config is not None else 1024 * 1024 * 1024
    if included_count > max_documents:
        errors.append(
            "selection exceeds max_selected_documents "
            f"({included_count} > {max_documents})"
        )
    if included_bytes > max_bytes:
        errors.append(
            f"selection exceeds max_selected_bytes ({included_bytes} > {max_bytes})"
        )
    return errors


def _require_disjoint_roots(
    review_root: Path, protected_root: Path, *, protected_label: str
) -> None:
    review = review_root.resolve(strict=False)
    protected = protected_root.resolve(strict=False)
    if review == protected or review in protected.parents or protected in review.parents:
        raise CorpusSelectionError(
            f"review state and {protected_label} must use disjoint directory trees"
        )


def _raw_stage_path(content_hash: str) -> str:
    digest = content_hash.removeprefix("sha256:")
    return f"library/objects/sha256/{digest}/raw.md"


def _raw_final_path(generation: int, content_hash: str) -> str:
    return (
        f"state/generations/{generation:06d}/auxiliary/"
        f"{_raw_stage_path(content_hash)}"
    )


def _lock_bytes(lock: CorpusLockManifest) -> bytes:
    return canonical_json_bytes(lock.model_dump(mode="json")) + b"\n"


def _manifest_bytes(manifest: GenerationLibraryManifest) -> bytes:
    return canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n"


def _selection_bytes(manifest: CorpusSelectionManifest) -> bytes:
    return canonical_json_bytes(manifest.model_dump(mode="json"))


def _find_latest_library_generation(store: GenerationStore, current: int) -> int:
    for generation in range(current, -1, -1):
        candidate = store.generation_path(generation) / GENERATION_IMPORT_MANIFEST_FILE
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CorpusIntegrityError(
                "generation-owned library provenance is not a regular file"
            )
        return generation
    raise CorpusNotImportedError("no generation-owned corpus import exists")


def load_corpus_lock(
    review_root: Path, *, generation: int | None = None
) -> tuple[CorpusLockManifest, str]:
    """Load and rehash the newest corpus lock at or before ``generation``."""

    store = GenerationStore(review_root)
    current = store.current_generation() if generation is None else generation
    owner_generation = _find_latest_library_generation(store, current)
    try:
        _, _, auxiliary = store.load_generation_auxiliary(
            owner_generation,
            {LOCK_FILE, IMPORT_MANIFEST_FILE, SELECTION_MANIFEST_FILE},
        )
    except (OSError, ValueError) as exc:
        raise CorpusIntegrityError(
            "generation-owned library provenance is not anchored"
        ) from exc
    manifest_bytes = auxiliary[IMPORT_MANIFEST_FILE]
    try:
        import_manifest = GenerationLibraryManifest.model_validate_json(
            manifest_bytes
        )
    except Exception as exc:
        raise CorpusIntegrityError("generation library manifest is invalid") from exc
    if import_manifest.generation != owner_generation:
        raise CorpusIntegrityError("generation library manifest owner mismatch")
    lock_bytes = auxiliary[import_manifest.corpus_lock_path]
    if hash_bytes(lock_bytes) != import_manifest.corpus_lock_hash:
        raise CorpusIntegrityError("generation-owned corpus lock hash mismatch")
    try:
        lock = CorpusLockManifest.model_validate_json(lock_bytes)
    except Exception as exc:
        raise CorpusIntegrityError("generation-owned corpus lock schema is invalid") from exc
    if lock.committed_generation != owner_generation:
        raise CorpusIntegrityError("corpus lock generation mismatch")
    if hash_json(lock.import_source.model_dump(mode="json")) != lock.import_source_hash:
        raise CorpusIntegrityError("corpus import-source digest mismatch")
    if import_manifest.import_source_hash != lock.import_source_hash:
        raise CorpusIntegrityError("library manifest import-source digest mismatch")
    selection_bytes = auxiliary[import_manifest.selection_manifest_path]
    if (
        hash_bytes(selection_bytes) != import_manifest.selection_manifest_hash
        or import_manifest.selection_manifest_hash != lock.selection_manifest_hash
    ):
        raise CorpusIntegrityError("generation-owned selection manifest hash mismatch")
    try:
        selection = CorpusSelectionManifest.model_validate_json(selection_bytes)
    except Exception as exc:
        raise CorpusIntegrityError("generation-owned selection manifest is invalid") from exc
    if _selection_bytes(selection) != selection_bytes:
        raise CorpusIntegrityError("generation-owned selection manifest is not canonical")
    if (
        selection.library_id != lock.library_id
        or selection.source_commit != lock.source_commit
    ):
        raise CorpusIntegrityError("selection and corpus lock identity fields disagree")
    selected = {
        (item.source_relative_path, item.content_sha256)
        for item in selection.documents
        if item.decision == "include"
    }
    locked_selected = {
        (paper.source_relative_path, paper.source_hash) for paper in lock.papers
    }
    if selected != locked_selected:
        raise CorpusIntegrityError("selection inclusions disagree with the corpus lock")
    expected_resources = {paper.raw_md_path: paper.raw_md_hash for paper in lock.papers}
    if import_manifest.resource_hashes != expected_resources:
        raise CorpusIntegrityError("library manifest resource index disagrees with corpus lock")
    return lock, import_manifest.corpus_lock_hash


def verify_corpus_lock(
    review_root: Path,
    lock: CorpusLockManifest,
    snapshot: RepositorySnapshot,
) -> dict[str, bytes]:
    """Rehash every exact raw object and cross-check it against canonical Papers."""

    root = review_root.resolve(strict=False)
    store = GenerationStore(root)
    raw_locations: dict[str, tuple[int, str]] = {}
    for locked in lock.papers:
        match = _RAW_PATH_RE.fullmatch(locked.raw_md_path)
        if match is None:
            raise CorpusIntegrityError("raw Markdown path is not content addressed")
        raw_generation = int(match.group(1))
        if raw_generation > lock.committed_generation:
            raise CorpusIntegrityError("raw Markdown cannot be owned by a future generation")
        prefix = f"state/generations/{match.group(1)}/auxiliary/"
        raw_locations[locked.paper_id] = (
            raw_generation,
            locked.raw_md_path.removeprefix(prefix),
        )
    requested_by_generation: dict[int, set[str]] = {}
    for generation, relative in raw_locations.values():
        requested_by_generation.setdefault(generation, set()).add(relative)
    requested_by_generation.setdefault(lock.committed_generation, set()).update(
        {LOCK_FILE, IMPORT_MANIFEST_FILE, SELECTION_MANIFEST_FILE}
    )
    auxiliary_by_generation: dict[int, dict[str, bytes]] = {}
    for generation, requested in sorted(requested_by_generation.items()):
        try:
            _, _, auxiliary_by_generation[generation] = (
                store.load_generation_auxiliary(generation, requested)
            )
        except (OSError, ValueError) as exc:
            raise CorpusIntegrityError(
                "generation-owned library files are not one anchored snapshot"
            ) from exc
    auxiliary = auxiliary_by_generation[lock.committed_generation]
    try:
        observed_lock = CorpusLockManifest.model_validate_json(auxiliary[LOCK_FILE])
    except Exception as exc:
        raise CorpusIntegrityError("anchored corpus lock became invalid") from exc
    if observed_lock != lock:
        raise CorpusIntegrityError("corpus lock changed before raw verification")
    papers = {paper.paper_id: paper for paper in snapshot.papers}
    seen_ids: set[str] = set()
    verified_bytes: dict[str, bytes] = {}
    for locked in lock.papers:
        if locked.paper_id in seen_ids:
            raise CorpusIntegrityError("corpus lock contains duplicate paper IDs")
        seen_ids.add(locked.paper_id)
        paper = papers.get(locked.paper_id)
        if paper is None:
            raise CorpusIntegrityError(f"locked paper is absent: {locked.paper_id}")
        if (
            paper.source_hash != locked.source_hash
            or paper.raw_md_hash != locked.raw_md_hash
            or paper.raw_md_path != locked.raw_md_path
        ):
            raise CorpusIntegrityError(
                f"locked paper disagrees with canonical state: {locked.paper_id}"
            )
        match = _RAW_PATH_RE.fullmatch(locked.raw_md_path)
        if match is None or f"sha256:{match.group(2)}" != locked.raw_md_hash:
            raise CorpusIntegrityError("raw Markdown path is not content addressed")
        raw_generation, raw_relative = raw_locations[locked.paper_id]
        raw_content = auxiliary_by_generation[raw_generation][raw_relative]
        if hash_bytes(raw_content) != locked.raw_md_hash:
            raise CorpusIntegrityError(f"raw Markdown hash mismatch: {locked.paper_id}")
        verified_bytes[locked.paper_id] = raw_content
    return verified_bytes


def _identity_candidates(
    content_hash: str, doi: str | None, bibliography_key: str | None
) -> list[str]:
    values = [content_hash]
    if doi:
        values.append(doi)
    if bibliography_key:
        values.append(f"bib:{bibliography_key.casefold()}")
    return values


def _paper_identity_values(paper: Paper) -> set[str]:
    """All authoritative and indexed identities owned by one canonical Paper."""

    identities = {*paper.identity_keys, paper.source_hash}
    if paper.doi is not None:
        identities.add(paper.doi)
    return identities


def _merge_paper_metadata(
    paper: Paper,
    incoming: dict[str, Any],
    *,
    inferred_title: str,
) -> dict[str, Any]:
    """Fill absent canonical metadata and reject incompatible replacements."""

    updates: dict[str, Any] = {}
    for field in ("title", "authors", "year", "doi", "journal"):
        value = incoming[field]
        if value is None:
            continue
        current = getattr(paper, field)
        if current is None or (
            field == "title"
            and current == inferred_title
            and incoming["title"] != inferred_title
        ):
            updates[field] = value
        elif current != value:
            raise CorpusSelectionError(
                f"selected metadata conflicts with canonical Paper {field}"
            )
    return updates


def import_selected_corpus(
    review_root: Path,
    manifest: CorpusSelectionManifest,
    config: LibraryConfig,
    *,
    public_repository_root: Path,
    crash_at: CrashPoint | None = None,
) -> CorpusImportResult:
    """Commit canonical Papers, raw bytes, and provenance through one CURRENT boundary."""

    review_root = review_root.resolve(strict=False)
    _require_disjoint_roots(
        review_root, config.library_path, protected_label="the external library"
    )
    _require_disjoint_roots(
        review_root,
        config.superproject_path,
        protected_label="the library superproject",
    )
    _require_disjoint_roots(
        review_root,
        public_repository_root,
        protected_label="the public repository",
    )

    # No destination mutation is allowed until the exact pin, clean checkout,
    # inventory, selection, and bytes all agree.
    source = PinnedGitSource.open(config)
    inventory, _ = build_library_inventory(source, config)
    errors = validate_selection_manifest(manifest, inventory, config=config)
    if errors:
        raise CorpusSelectionError("invalid corpus selection: " + "; ".join(errors))
    by_path = {item.source_relative_path: item for item in inventory}
    canonical_manifest = manifest.model_copy(
        update={
            "documents": sorted(
                manifest.documents,
                key=lambda item: item.source_relative_path,
            )
        }
    )
    included = [
        item for item in canonical_manifest.documents if item.decision == "include"
    ]
    raw_payloads: dict[str, bytes] = {}
    observed_selected_bytes = 0
    for selected in included:
        content = source.read_blob(by_path[selected.source_relative_path].git_blob_id)
        observed_selected_bytes += len(content)
        if observed_selected_bytes > config.max_selected_bytes:
            raise CorpusSelectionError(
                "selected pinned objects exceed max_selected_bytes during materialization"
            )
        if compute_content_sha256(content) != selected.content_sha256:
            raise CorpusSelectionError("pinned object changed during import preparation")
        raw_payloads[selected.source_relative_path] = content

    bibliography, duplicate_keys = load_bibliography(source, config)
    graph_nodes: list[dict[str, Any]] = []
    if config.graph_path is not None:
        graph_nodes = ReadOnlyGraphAdapter.from_source(source, config.graph_path).graph_nodes()
    candidates = [
        item
        for item in inventory
        if item.document_kind is DocumentKind.CANDIDATE_PAPER_MARKDOWN
    ]
    mapping, conflicts = resolve_source_mappings(
        candidates, bibliography, duplicate_keys, graph_nodes
    )
    if conflicts.duplicate_bib_keys or conflicts.conflicting_dois:
        raise CorpusSelectionError("bibliographic identity conflicts make import ambiguous")
    selected_paths = {item.source_relative_path for item in included}
    selected_title_conflicts = [
        item
        for item in conflicts.inconsistent_titles
        if item.get("source_path") in selected_paths
    ]
    selected_ambiguous = [
        item.paper_path
        for item in mapping.mapped_papers
        if item.paper_path in selected_paths and item.status.value == "ambiguous"
    ]
    if selected_title_conflicts or selected_ambiguous:
        raise CorpusSelectionError(
            "selected document metadata is conflicting or ambiguous"
        )
    mapping_by_path = {item.paper_path: item for item in mapping.mapped_papers}
    bib_by_key = {item.key: item for item in bibliography}
    stable_identity_paths: dict[str, str] = {}
    for selected in included:
        mapped = mapping_by_path.get(selected.source_relative_path)
        bib = (
            bib_by_key.get(mapped.bib_key)
            if mapped is not None and mapped.bib_key is not None
            else None
        )
        doi = bib.doi if bib is not None else (mapped.doi if mapped else None)
        bib_key = mapped.bib_key if mapped else None
        stable_identities = ([doi] if doi else []) + (
            [f"bib:{bib_key.casefold()}"] if bib_key else []
        )
        for identity in stable_identities:
            prior = stable_identity_paths.setdefault(identity, selected.source_relative_path)
            if prior != selected.source_relative_path:
                raise CorpusSelectionError(
                    "multiple selected documents share one stable publication identity"
                )
    selection_hash = hash_json(canonical_manifest.model_dump(mode="json"))
    source_objects = [
        LibrarySourceObject(
            role="paper",
            source_relative_path=selected.source_relative_path,
            git_blob_id=by_path[selected.source_relative_path].git_blob_id,
            content_sha256=selected.content_sha256,
        )
        for selected in included
    ]
    for role, path in (
        ("bibliography", config.bibliography),
        ("graph", config.graph_path),
    ):
        if path is not None:
            record = by_path[path]
            source_objects.append(
                LibrarySourceObject(
                    role=role,
                    source_relative_path=path,
                    git_blob_id=record.git_blob_id,
                    content_sha256=record.content_sha256,
                )
            )
    import_source = CorpusImportSource(
        library_id=config.library_id,
        source_commit=config.expected_commit,
        superproject_commit=source.integrity.superproject_commit,
        gitlink_path=config.gitlink_path,
        markdown_root=config.markdown_root,
        bibliography_path=config.bibliography,
        graph_path=config.graph_path,
        selection_manifest_hash=selection_hash,
        objects=sorted(
            source_objects,
            key=lambda item: (item.role, item.source_relative_path),
        ),
    )
    import_source_hash = hash_json(import_source.model_dump(mode="json"))

    review_root.mkdir(parents=True, exist_ok=True)
    store = GenerationStore(review_root)
    store.initialize()
    with AdvisoryFileLock(review_root / "state" / IMPORT_LOCK):
        # Decide reuse while holding the same writer lock that protects CURRENT.
        # A concurrent scientific commit therefore cannot make a no-op result stale
        # between the snapshot read and the complete provenance verification.
        with store.writer_lock():
            base_generation, snapshot, _ = store.load_current()
            try:
                active_lock, active_hash = load_corpus_lock(
                    review_root, generation=base_generation
                )
            except CorpusNotImportedError:
                active_lock = None
                active_hash = None
            if active_lock is not None and (
                active_lock.library_id == manifest.library_id
                and active_lock.source_commit == manifest.source_commit
                and active_lock.selection_manifest_hash == selection_hash
                and active_lock.import_source_hash == import_source_hash
            ):
                verify_corpus_lock(review_root, active_lock, snapshot)
                return CorpusImportResult(
                    generation=base_generation,
                    corpus_lock_hash=active_hash,
                    reused=True,
                )

        locked_papers: list[CorpusLockPaper] = []
        stage_resources: dict[str, bytes] = {}

        def promote(
            current_snapshot: RepositorySnapshot,
            registry: CanonicalIdRegistry,
        ) -> PromotionPayload:
            nonlocal locked_papers
            current_integrity = source.verify()
            if (
                current_integrity.superproject_commit
                != source.integrity.superproject_commit
            ):
                raise CorpusSelectionError(
                    "library superproject changed during import preparation"
                )
            papers_by_id = {paper.paper_id: paper for paper in current_snapshot.papers}
            identity_index: dict[str, set[str]] = {}
            for paper in current_snapshot.papers:
                for identity in _paper_identity_values(paper):
                    identity_index.setdefault(identity, set()).add(paper.paper_id)
            allocated_ids: dict[str, str] = {}
            claimed_ids: set[str] = set()
            next_locked: list[CorpusLockPaper] = []
            next_generation = base_generation + 1

            for selected in included:
                record = by_path[selected.source_relative_path]
                mapped = mapping_by_path.get(selected.source_relative_path)
                bib = (
                    bib_by_key.get(mapped.bib_key)
                    if mapped is not None and mapped.bib_key is not None
                    else None
                )
                doi = bib.doi if bib is not None else (mapped.doi if mapped else None)
                bib_key = mapped.bib_key if mapped else None
                inferred_title = extract_title_candidate(
                    selected.source_relative_path
                )
                incoming_metadata = {
                    "title": (bib.title if bib else None)
                    or (mapped.title if mapped else None)
                    or inferred_title,
                    "authors": (bib.authors or None) if bib else None,
                    "year": bib.year if bib else None,
                    "doi": doi,
                    "journal": bib.journal if bib else None,
                }
                identities = _identity_candidates(selected.content_sha256, doi, bib_key)
                matches = {
                    paper_id
                    for identity in identities
                    for paper_id in identity_index.get(identity, set())
                }
                if len(matches) > 1:
                    raise CorpusSelectionError("stable publication identities resolve to multiple Papers")
                paper = papers_by_id[next(iter(matches))] if matches else None
                if paper is not None and paper.paper_id in claimed_ids:
                    raise CorpusSelectionError("multiple selected documents resolve to one stable Paper")
                metadata_updates = (
                    _merge_paper_metadata(
                        paper, incoming_metadata, inferred_title=inferred_title
                    )
                    if paper is not None
                    else incoming_metadata
                )

                if paper is None:
                    paper_ids, registry = registry.allocate(IdKind.PAPER)
                    paper_id = paper_ids[0]
                    allocated_ids[selected.source_relative_path] = paper_id
                    raw_path = _raw_final_path(next_generation, selected.content_sha256)
                    paper = Paper(
                        paper_id=paper_id,
                        title=metadata_updates["title"],
                        authors=metadata_updates["authors"],
                        year=metadata_updates["year"],
                        doi=metadata_updates["doi"],
                        journal=metadata_updates["journal"],
                        identity_keys=identities,
                        study_group_id=None,
                        related_publications=[],
                        independence_status=IndependenceStatus.UNKNOWN,
                        raw_md_path=raw_path,
                        source_hash=selected.content_sha256,
                        raw_md_hash=selected.content_sha256,
                    )
                    stage_resources[_raw_stage_path(selected.content_sha256)] = raw_payloads[
                        selected.source_relative_path
                    ]
                elif paper.raw_md_hash != selected.content_sha256:
                    # A stable DOI/bibliographic identity changed bytes. Reuse the
                    # canonical Paper ID while retaining the old generation/object.
                    raw_path = _raw_final_path(next_generation, selected.content_sha256)
                    combined_identities = list(dict.fromkeys([*paper.identity_keys, *identities]))
                    paper = paper.model_copy(
                        update={
                            **metadata_updates,
                            "identity_keys": combined_identities,
                            "raw_md_path": raw_path,
                            "source_hash": selected.content_sha256,
                            "raw_md_hash": selected.content_sha256,
                        }
                    )
                    stage_resources[_raw_stage_path(selected.content_sha256)] = raw_payloads[
                        selected.source_relative_path
                    ]
                else:
                    combined_identities = list(dict.fromkeys([*paper.identity_keys, *identities]))
                    if combined_identities != paper.identity_keys or metadata_updates:
                        paper = paper.model_copy(
                            update={
                                **metadata_updates,
                                "identity_keys": combined_identities,
                            }
                        )

                papers_by_id[paper.paper_id] = paper
                claimed_ids.add(paper.paper_id)
                for identity in _paper_identity_values(paper):
                    identity_index.setdefault(identity, set()).add(paper.paper_id)
                next_locked.append(
                    CorpusLockPaper(
                        paper_id=paper.paper_id,
                        source_relative_path=selected.source_relative_path,
                        git_blob_id=record.git_blob_id,
                        source_hash=selected.content_sha256,
                        raw_md_path=paper.raw_md_path,
                        raw_md_hash=paper.raw_md_hash,
                        title=paper.title,
                        doi=paper.doi,
                        bibliography_key=bib_key,
                    )
                )
            locked_papers = sorted(next_locked, key=lambda item: item.paper_id)
            promoted = current_snapshot.model_copy(
                update={
                    "papers": tuple(
                        sorted(papers_by_id.values(), key=lambda item: item.paper_id)
                    )
                }
            )
            return PromotionPayload(promoted, registry, allocated_ids)

        def materialize(
            writer: AuxiliaryStagingWriter,
            next_generation: int,
            _: PromotionPayload,
        ) -> None:
            if next_generation != base_generation + 1:
                raise CorpusIntegrityError("unexpected generation during import staging")
            for relative_path, content in sorted(stage_resources.items()):
                expected = f"sha256:{PurePosixPath(relative_path).parts[-2]}"
                if hash_bytes(content) != expected:
                    raise CorpusIntegrityError("raw staging payload hash mismatch")
                writer.write_bytes(relative_path, content)
            lock = CorpusLockManifest(
                library_id=config.library_id,
                source_commit=config.expected_commit,
                selection_manifest_hash=selection_hash,
                import_source=import_source,
                import_source_hash=import_source_hash,
                committed_generation=next_generation,
                papers=locked_papers,
            )
            lock_content = _lock_bytes(lock)
            lock_hash = hash_bytes(lock_content)
            selection_content = _selection_bytes(canonical_manifest)
            if hash_bytes(selection_content) != selection_hash:
                raise CorpusIntegrityError("canonical selection digest changed")
            writer.write_bytes(SELECTION_MANIFEST_FILE, selection_content)
            writer.write_bytes(LOCK_FILE, lock_content)
            import_manifest = GenerationLibraryManifest(
                generation=next_generation,
                corpus_lock_hash=lock_hash,
                selection_manifest_hash=selection_hash,
                import_source_hash=import_source_hash,
                resource_hashes={paper.raw_md_path: paper.raw_md_hash for paper in locked_papers},
            )
            writer.write_bytes(IMPORT_MANIFEST_FILE, _manifest_bytes(import_manifest))

        try:
            result = store.commit(
                base_generation=base_generation,
                dependencies={},
                promotion=promote,
                staging_materializer=materialize,
                crash_at=crash_at,
            )
        except BaseException:
            # Before CURRENT, recovery discards all coupled staged bytes. After the
            # AFTER_CURRENT test point, the complete generation is the committed state.
            store.recover()
            raise
        committed_snapshot, _ = store.load_generation(result.generation)
        lock, lock_hash = load_corpus_lock(
            review_root, generation=result.generation
        )
        if lock.committed_generation != result.generation:
            raise CorpusIntegrityError(
                "committed import generation lacks its generation-owned corpus lock"
            )
        verify_corpus_lock(review_root, lock, committed_snapshot)
        return CorpusImportResult(
            generation=result.generation,
            allocated_ids=result.allocated_ids,
            corpus_lock_hash=lock_hash,
        )
