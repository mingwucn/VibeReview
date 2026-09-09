"""Deterministic five-paper pilot facts and immutable run-policy registration.

The setup step is Python-owned: it derives only corpus arithmetic and explicit
process limitations.  It performs no semantic inference and invokes no engine.
All canonical identifiers and the run manifest cross one GenerationStore
transaction, and an exact rerun reuses the original setup generation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import ConfigDict

from vibereview.enums import CorpusFactDerivationType, ReviewProcessSourceType
from vibereview.models import (
    CorpusFact,
    CorpusFactDerivation,
    ReviewProcessFact,
)
from vibereview.runtime.hashing import canonical_json_bytes, hash_bytes
from vibereview.runtime.pilot_records import (
    PILOT_CORPUS_FACT_TEXT,
    PILOT_HUMAN_REVIEW_FACT_TEXT,
    PILOT_SCOPE_FACT_TEXT,
    PilotRunManifest,
    PilotRunRegistrationArtifact,
    PilotRunRegistrationReference,
)
from vibereview.runtime.records import RuntimeModel
from vibereview.runtime.registry import CanonicalIdRegistry, IdKind
from vibereview.runtime.repository import (
    CrashPoint,
    GenerationStore,
    PromotionPayload,
    StaleSnapshotError,
)
from vibereview.runtime.state import RepositorySnapshot

from .models import CorpusIntegrityError, PathSecurityError
from .selection import load_corpus_lock, verify_corpus_lock


PILOT_SETUP_VERSION = "1"
class _FrozenRuntimeModel(RuntimeModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SyntheticPilotSetupResult(_FrozenRuntimeModel):
    schema_version: Literal["package-c-pilot-setup-1"] = (
        "package-c-pilot-setup-1"
    )
    generation: int
    commit_performed: bool
    reused_generation: int | None
    run_manifest_path: str
    run_manifest_content_hash: str
    corpus_fact_id: str
    scope_process_fact_id: str
    human_review_process_fact_id: str
    registration: PilotRunRegistrationReference


def _run_manifest_relative_path(run_id: str) -> str:
    return f"pilot/runs/{run_id}/run_manifest.json"


def _run_manifest_bytes(manifest: PilotRunManifest) -> bytes:
    return canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n"


def _registration_bytes(
    registration: PilotRunRegistrationArtifact,
) -> bytes:
    return canonical_json_bytes(registration.model_dump(mode="json")) + b"\n"


def _registration_relative_path(
    run_id: str, artifact_hash: str
) -> str:
    digest = artifact_hash.removeprefix("sha256:")
    return f"pilot/runs/{run_id}/registration-{digest}.json"


def _build_registration(
    manifest: PilotRunManifest,
    *,
    corpus_fact_id: str,
    scope_process_fact_id: str,
    human_review_process_fact_id: str,
) -> tuple[
    PilotRunRegistrationArtifact,
    PilotRunRegistrationReference,
    bytes,
]:
    artifact = PilotRunRegistrationArtifact(
        run_id=manifest.run_id,
        source_generation=manifest.source_generation,
        owner_generation=manifest.source_generation + 1,
        run_manifest_hash=manifest.manifest_hash,
        run_manifest_content_hash=hash_bytes(_run_manifest_bytes(manifest)),
        run_manifest_relative_path=_run_manifest_relative_path(manifest.run_id),
        corpus_fact_id=corpus_fact_id,
        scope_process_fact_id=scope_process_fact_id,
        human_review_process_fact_id=human_review_process_fact_id,
    )
    content = _registration_bytes(artifact)
    artifact_hash = hash_bytes(content)
    reference = PilotRunRegistrationReference(
        run_id=manifest.run_id,
        owner_generation=artifact.owner_generation,
        artifact_hash=artifact_hash,
        relative_path=_registration_relative_path(manifest.run_id, artifact_hash),
    )
    return artifact, reference, content


def _process_source_key(manifest: PilotRunManifest, suffix: str) -> str:
    return f"pilot-run:{manifest.manifest_hash.removeprefix('sha256:')}:{suffix}"


def _validate_source_corpus(
    review_root: Path,
    manifest: PilotRunManifest,
) -> tuple[RepositorySnapshot, tuple[str, ...]]:
    store = GenerationStore(review_root)
    snapshot, registry = store.load_generation(manifest.source_generation)
    lock, lock_hash = load_corpus_lock(
        review_root, generation=manifest.source_generation
    )
    verify_corpus_lock(review_root, lock, snapshot)
    if lock_hash != manifest.corpus_lock_hash:
        raise CorpusIntegrityError("pilot run manifest names another corpus lock")
    if lock.selection_manifest_hash != manifest.selection_manifest_hash:
        raise CorpusIntegrityError(
            "pilot run manifest names another selection manifest"
        )
    if lock.committed_generation != manifest.source_generation:
        raise CorpusIntegrityError("pilot corpus lock is owned by another generation")
    ordered_papers = tuple(sorted(lock.papers, key=lambda item: item.paper_id))
    if len(ordered_papers) != 5:
        raise CorpusIntegrityError("synthetic pilot setup requires exactly five papers")
    expected_hashes = tuple(item.source_hash for item in ordered_papers)
    if manifest.paper_source_hashes != expected_hashes:
        raise CorpusIntegrityError(
            "pilot run manifest paper hashes are not the canonical five-paper order"
        )
    paper_ids = tuple(item.paper_id for item in ordered_papers)
    if {item.paper_id for item in snapshot.papers} != set(paper_ids):
        raise CorpusIntegrityError(
            "pilot source generation contains papers outside the locked five-paper corpus"
        )
    nonpaper_collections = tuple(
        name
        for name in type(snapshot).model_fields
        if name != "papers" and getattr(snapshot, name)
    )
    if nonpaper_collections:
        raise CorpusIntegrityError(
            "synthetic pilot setup requires a fresh paper-only source generation"
        )
    expected_registry = CanonicalIdRegistry.from_identifiers(
        snapshot.all_identifiers()
    )
    if registry != expected_registry:
        raise CorpusIntegrityError(
            "synthetic pilot setup requires an exact fresh-source ID registry"
        )
    return snapshot, paper_ids


def _expected_process_facts(
    manifest: PilotRunManifest,
    *,
    scope_id: str,
    human_id: str,
) -> tuple[ReviewProcessFact, ReviewProcessFact]:
    return (
        ReviewProcessFact(
            process_fact_id=scope_id,
            text=PILOT_SCOPE_FACT_TEXT,
            source_type=ReviewProcessSourceType.RUN_MANIFEST,
            source_key=_process_source_key(manifest, "scope"),
        ),
        ReviewProcessFact(
            process_fact_id=human_id,
            text=PILOT_HUMAN_REVIEW_FACT_TEXT,
            source_type=ReviewProcessSourceType.RUN_MANIFEST,
            source_key=_process_source_key(manifest, "human-review"),
        ),
    )


def _find_exact_setup(
    review_root: Path,
    manifest: PilotRunManifest,
    paper_ids: tuple[str, ...],
) -> SyntheticPilotSetupResult | None:
    """Return the immutable first setup generation if it is exactly intact."""

    owner_generation = manifest.source_generation + 1
    store = GenerationStore(review_root)
    with store.writer_lock():
        current_generation = store.current_generation()
        if current_generation < owner_generation:
            return None
        try:
            owner_snapshot, _, auxiliary = store.load_generation_auxiliary(
                owner_generation, {_run_manifest_relative_path(manifest.run_id)}
            )
        except (FileNotFoundError, ValueError):
            # The consecutive generation may belong to an unrelated writer.
            # Either way it is not an exact reusable setup and the caller
            # remains fail-closed with StaleSnapshotError.
            return None
        content = auxiliary[_run_manifest_relative_path(manifest.run_id)]
        try:
            observed_manifest = PilotRunManifest.model_validate_json(content)
        except Exception as exc:
            raise CorpusIntegrityError("stored pilot run manifest is invalid") from exc
        if observed_manifest != manifest or content != _run_manifest_bytes(manifest):
            raise CorpusIntegrityError(
                "stored pilot run manifest differs from the request"
            )

        corpus_matches = tuple(
            item
            for item in owner_snapshot.corpus_facts
            if item.text == PILOT_CORPUS_FACT_TEXT
            and item.derivation_type
            is CorpusFactDerivationType.REGISTRY_ARITHMETIC
            and tuple(item.source_paper_ids) == paper_ids
            and item.derivation
            == CorpusFactDerivation(
                task_version=None,
                model_signature=None,
                input_hash=None,
                output_hash=None,
            )
        )
        scope_key = _process_source_key(manifest, "scope")
        human_key = _process_source_key(manifest, "human-review")
        scope_matches = tuple(
            item
            for item in owner_snapshot.process_facts
            if item.source_key == scope_key
        )
        human_matches = tuple(
            item
            for item in owner_snapshot.process_facts
            if item.source_key == human_key
        )
        if not (
            len(corpus_matches) == len(scope_matches) == len(human_matches) == 1
        ):
            raise CorpusIntegrityError(
                "pilot setup generation lacks one exact corpus/process fact set"
            )
        expected_scope, expected_human = _expected_process_facts(
            manifest,
            scope_id=scope_matches[0].process_fact_id,
            human_id=human_matches[0].process_fact_id,
        )
        if scope_matches[0] != expected_scope or human_matches[0] != expected_human:
            raise CorpusIntegrityError("pilot process facts differ from the run policy")

        _, registration_reference, registration_content = _build_registration(
            manifest,
            corpus_fact_id=corpus_matches[0].corpus_fact_id,
            scope_process_fact_id=scope_matches[0].process_fact_id,
            human_review_process_fact_id=human_matches[0].process_fact_id,
        )
        _, _, registration_auxiliary = store.load_generation_auxiliary(
            owner_generation, {registration_reference.relative_path}
        )
        if (
            registration_auxiliary[registration_reference.relative_path]
            != registration_content
        ):
            raise CorpusIntegrityError("stored pilot registration differs from setup")

        _, current_snapshot, current_registry = store.load_current()
        current_corpus_matches = tuple(
            item
            for item in current_snapshot.corpus_facts
            if item.corpus_fact_id == corpus_matches[0].corpus_fact_id
        )
        current_scope_matches = tuple(
            item
            for item in current_snapshot.process_facts
            if item.source_key == scope_key
        )
        current_human_matches = tuple(
            item
            for item in current_snapshot.process_facts
            if item.source_key == human_key
        )
        if (
            current_corpus_matches != (corpus_matches[0],)
            or current_scope_matches != (scope_matches[0],)
            or current_human_matches != (human_matches[0],)
        ):
            raise CorpusIntegrityError(
                "CURRENT no longer contains one unique exact pilot fact set"
            )
        current_registry.validate_covers_identifiers(
            current_snapshot.all_identifiers()
        )
    return SyntheticPilotSetupResult(
        generation=current_generation,
        commit_performed=False,
        reused_generation=owner_generation,
        run_manifest_path=(
            f"state/generations/{owner_generation:06d}/auxiliary/"
            f"{_run_manifest_relative_path(manifest.run_id)}"
        ),
        run_manifest_content_hash=hash_bytes(content),
        corpus_fact_id=corpus_matches[0].corpus_fact_id,
        scope_process_fact_id=scope_matches[0].process_fact_id,
        human_review_process_fact_id=human_matches[0].process_fact_id,
        registration=registration_reference,
    )


def register_synthetic_pilot_setup(
    review_root: Path,
    manifest: PilotRunManifest,
    *,
    public_repository_root: Path,
    crash_at: CrashPoint | None = None,
) -> SyntheticPilotSetupResult:
    """Commit or exactly reuse the deterministic five-paper pilot setup."""

    root = review_root.resolve(strict=False)
    public_root = public_repository_root.resolve(strict=False)
    if (
        root == public_root
        or public_root in root.parents
        or root in public_root.parents
    ):
        raise PathSecurityError(
            "pilot review root and public repository must be disjoint"
        )
    source_snapshot, paper_ids = _validate_source_corpus(root, manifest)
    store = GenerationStore(root)
    current_generation = store.current_generation()
    if current_generation != manifest.source_generation:
        reused = _find_exact_setup(root, manifest, paper_ids)
        if reused is not None:
            return reused
        raise StaleSnapshotError(
            "pilot run source generation is stale and has no exact setup artifact"
        )

    manifest_content = _run_manifest_bytes(manifest)
    manifest_relative = _run_manifest_relative_path(manifest.run_id)

    def promote(snapshot, registry):
        if snapshot != source_snapshot:
            raise CorpusIntegrityError("pilot source snapshot changed before promotion")
        corpus_matches = tuple(
            item
            for item in snapshot.corpus_facts
            if item.text == PILOT_CORPUS_FACT_TEXT
            and tuple(item.source_paper_ids) == paper_ids
            and item.derivation_type
            is CorpusFactDerivationType.REGISTRY_ARITHMETIC
            and item.derivation
            == CorpusFactDerivation(
                task_version=None,
                model_signature=None,
                input_hash=None,
                output_hash=None,
            )
        )
        if len(corpus_matches) > 1:
            raise CorpusIntegrityError("pilot corpus fact is ambiguous")
        allocated: dict[str, str] = {}
        if corpus_matches:
            corpus_fact = corpus_matches[0]
        else:
            identifiers, registry = registry.allocate(IdKind.CORPUS_FACT)
            corpus_fact = CorpusFact(
                corpus_fact_id=identifiers[0],
                text=PILOT_CORPUS_FACT_TEXT,
                derivation_type=CorpusFactDerivationType.REGISTRY_ARITHMETIC,
                source_paper_ids=list(paper_ids),
                derivation=CorpusFactDerivation(
                    task_version=None,
                    model_signature=None,
                    input_hash=None,
                    output_hash=None,
                ),
            )
            allocated["corpus_fact"] = corpus_fact.corpus_fact_id

        source_keys = {
            _process_source_key(manifest, "scope"),
            _process_source_key(manifest, "human-review"),
        }
        if any(item.source_key in source_keys for item in snapshot.process_facts):
            raise CorpusIntegrityError(
                "pilot process source key already exists without its setup artifact"
            )
        process_ids, registry = registry.allocate(IdKind.PROCESS_FACT, 2)
        scope_fact, human_fact = _expected_process_facts(
            manifest,
            scope_id=process_ids[0],
            human_id=process_ids[1],
        )
        allocated.update(
            {
                "process_scope": scope_fact.process_fact_id,
                "process_human_review": human_fact.process_fact_id,
            }
        )
        promoted = snapshot.model_copy(
            update={
                "corpus_facts": (
                    snapshot.corpus_facts
                    if corpus_matches
                    else snapshot.corpus_facts + (corpus_fact,)
                ),
                "process_facts": snapshot.process_facts
                + (scope_fact, human_fact),
            }
        )
        return PromotionPayload(promoted, registry, allocated)

    registration_box: dict[
        str, PilotRunRegistrationReference | bytes
    ] = {}

    def materialize(writer, next_generation, payload):
        if next_generation != manifest.source_generation + 1:
            raise CorpusIntegrityError("pilot setup generation is not consecutive")
        corpus_facts = tuple(
            item
            for item in payload.snapshot.corpus_facts
            if item.text == PILOT_CORPUS_FACT_TEXT
            and tuple(item.source_paper_ids) == paper_ids
        )
        scope_key = _process_source_key(manifest, "scope")
        human_key = _process_source_key(manifest, "human-review")
        scope_facts = tuple(
            item
            for item in payload.snapshot.process_facts
            if item.source_key == scope_key
        )
        human_facts = tuple(
            item
            for item in payload.snapshot.process_facts
            if item.source_key == human_key
        )
        if not (
            len(corpus_facts) == len(scope_facts) == len(human_facts) == 1
        ):
            raise CorpusIntegrityError(
                "pilot setup materialization lacks one exact fact set"
            )
        _, registration_reference, registration_content = _build_registration(
            manifest,
            corpus_fact_id=corpus_facts[0].corpus_fact_id,
            scope_process_fact_id=scope_facts[0].process_fact_id,
            human_review_process_fact_id=human_facts[0].process_fact_id,
        )
        writer.write_bytes(manifest_relative, manifest_content)
        writer.write_bytes(
            registration_reference.relative_path, registration_content
        )
        registration_box["reference"] = registration_reference
        registration_box["content"] = registration_content

    try:
        commit = store.commit(
            base_generation=manifest.source_generation,
            dependencies={},
            promotion=promote,
            staging_materializer=materialize,
            crash_at=crash_at,
        )
    except StaleSnapshotError:
        reused = _find_exact_setup(root, manifest, paper_ids)
        if reused is not None:
            return reused
        raise
    except BaseException:
        store.recover()
        raise

    registration_reference = registration_box.get("reference")
    registration_content = registration_box.get("content")
    if not isinstance(registration_reference, PilotRunRegistrationReference) or not isinstance(
        registration_content, bytes
    ):
        raise CorpusIntegrityError("pilot setup did not materialize its registration")
    snapshot, _, auxiliary = store.load_generation_auxiliary(
        commit.generation,
        {manifest_relative, registration_reference.relative_path},
    )
    content = auxiliary[manifest_relative]
    if content != manifest_content:
        raise CorpusIntegrityError("committed pilot run manifest changed")
    if auxiliary[registration_reference.relative_path] != registration_content:
        raise CorpusIntegrityError("committed pilot registration changed")
    corpus_id = commit.allocated_ids.get("corpus_fact")
    if corpus_id is None:
        matches = [
            item.corpus_fact_id
            for item in snapshot.corpus_facts
            if item.text == PILOT_CORPUS_FACT_TEXT
            and tuple(item.source_paper_ids) == paper_ids
        ]
        if len(matches) != 1:
            raise CorpusIntegrityError("committed pilot corpus fact is ambiguous")
        corpus_id = matches[0]
    return SyntheticPilotSetupResult(
        generation=commit.generation,
        commit_performed=True,
        reused_generation=None,
        run_manifest_path=(
            f"state/generations/{commit.generation:06d}/auxiliary/"
            f"{manifest_relative}"
        ),
        run_manifest_content_hash=hash_bytes(content),
        corpus_fact_id=corpus_id,
        scope_process_fact_id=commit.allocated_ids["process_scope"],
        human_review_process_fact_id=commit.allocated_ids[
            "process_human_review"
        ],
        registration=registration_reference,
    )


__all__ = [
    "PILOT_CORPUS_FACT_TEXT",
    "PILOT_HUMAN_REVIEW_FACT_TEXT",
    "PILOT_SCOPE_FACT_TEXT",
    "PILOT_SETUP_VERSION",
    "SyntheticPilotSetupResult",
    "register_synthetic_pilot_setup",
]
