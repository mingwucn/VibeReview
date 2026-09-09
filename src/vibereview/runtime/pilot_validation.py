"""Independent validation statuses for the bounded synthetic Package C pilot.

The validator deliberately does not collapse repository shape, source-location
checks, semantic-audit execution, citation authorization, exact assembly, and
runtime-artifact integrity into one boolean.  It never upgrades the synthetic
run to publication-eligible and it does not perform a human review.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from vibereview.ids import Sha256

from .artifacts import AssemblyRecord, verify_exact_assembly
from .hashing import hash_bytes
from .pilot_journal import (
    PilotJournalError,
    PilotStageArtifactReference,
    load_pilot_run_registration,
    load_pilot_stage_artifact,
)
from .pilot_records import (
    HumanReviewStatus,
    PilotRunManifest,
    PilotRunRegistrationReference,
    PilotValidationReport,
    ValidationStatus,
)
from .repository import GenerationStore
from .state import RepositorySnapshot


_CHECK_NAMES = (
    "structural_validation",
    "locator_verification",
    "semantic_audits_executed",
    "citation_authorization",
    "exact_assembly",
    "artifact_integrity",
)


class PilotValidationError(ValueError):
    """The run identity is not trustworthy enough to issue a report."""


@dataclass(frozen=True, slots=True)
class SyntheticPilotValidationResult:
    """A frozen report plus bounded, check-specific diagnostic messages."""

    report: PilotValidationReport
    diagnostics: Mapping[str, tuple[str, ...]]


def _verified_corpus(project_root: Path, generation: int):
    # Lazy import keeps the provider-neutral runtime import graph independent of
    # the optional external-library adapter.
    from vibereview.library.retrieval import VerifiedCorpus

    return VerifiedCorpus(project_root, generation=generation)


def _check_locators(
    snapshot: RepositorySnapshot,
    *,
    project_root: Path,
    generation: int,
    expected_lock_hash: Sha256,
) -> tuple[ValidationStatus, tuple[str, ...]]:
    if not snapshot.retrieved_spans:
        return ValidationStatus.NOT_EXECUTED, (
            "No canonical retrieved spans were present.",
        )
    try:
        corpus = _verified_corpus(project_root, generation)
        if corpus.lock_hash != expected_lock_hash:
            raise ValueError("verified corpus lock differs from the run manifest")
        for span in snapshot.retrieved_spans:
            try:
                raw_path, raw_hash, text = corpus.texts[span.paper_id]
            except KeyError as exc:
                raise ValueError(
                    f"retrieved span {span.span_id} names an unlocked paper"
                ) from exc
            locator = span.locator
            if locator.raw_md_path != raw_path:
                raise ValueError(
                    f"retrieved span {span.span_id} raw path differs from the corpus lock"
                )
            paper = next(
                (item for item in snapshot.papers if item.paper_id == span.paper_id),
                None,
            )
            if paper is None or paper.raw_md_hash != raw_hash:
                raise ValueError(
                    f"retrieved span {span.span_id} paper hash differs from the corpus lock"
                )
            observed = text[locator.start_offset : locator.end_offset]
            if observed != span.source_text:
                raise ValueError(
                    f"retrieved span {span.span_id} source slice differs"
                )
            if hash_bytes(observed.encode("utf-8")) != locator.source_span_hash:
                raise ValueError(
                    f"retrieved span {span.span_id} source hash differs"
                )
        return ValidationStatus.PASSED, ()
    except Exception as exc:
        del exc
        return ValidationStatus.FAILED, ("LOCATOR_VERIFICATION_FAILED",)


def _check_semantic_audits(
    snapshot: RepositorySnapshot,
) -> tuple[ValidationStatus, tuple[str, ...]]:
    if not snapshot.proposition_records and not snapshot.rendered_sentences:
        return ValidationStatus.NOT_EXECUTED, (
            "No canonical propositions or rendered sentences were present.",
        )
    proposition_ids = {item.proposition_id for item in snapshot.proposition_records}
    audited_propositions = {item.target_id for item in snapshot.semantic_audits}
    sentence_ids = {item.sentence_id for item in snapshot.rendered_sentences}
    audited_sentences = {item.sentence_id for item in snapshot.rendered_sentence_audits}
    if proposition_ids != audited_propositions or sentence_ids != audited_sentences:
        return ValidationStatus.FAILED, (
            "Canonical proposition or sentence audit coverage is incomplete.",
        )
    return ValidationStatus.PASSED, ()


def _check_citations(
    snapshot: RepositorySnapshot,
) -> tuple[ValidationStatus, tuple[str, ...]]:
    bindings = tuple(
        binding
        for proposition in snapshot.proposition_records
        for binding in proposition.citation_bindings
    )
    if not bindings:
        return ValidationStatus.NOT_EXECUTED, (
            "No canonical citation bindings were present.",
        )
    # Full repository validation proves each binding resolves to the exact CPE
    # and that the corresponding ClaimPacket licenses it.
    try:
        snapshot.validate_repository()
    except Exception as exc:
        del exc
        return ValidationStatus.FAILED, ("CITATION_AUTHORIZATION_FAILED",)
    return ValidationStatus.PASSED, ()


def _validate_authenticated_snapshot(
    root: Path,
    *,
    manifest: PilotRunManifest,
    generation: int,
    snapshot: RepositorySnapshot,
    registration: PilotRunRegistrationReference,
    journal_head: PilotStageArtifactReference | None,
    body: bytes | None = None,
    assembly: AssemblyRecord | None = None,
) -> SyntheticPilotValidationResult:
    diagnostics: dict[str, tuple[str, ...]] = {name: () for name in _CHECK_NAMES}

    try:
        snapshot.validate_repository()
        structural = ValidationStatus.PASSED
    except Exception as exc:  # pragma: no cover - GenerationStore also validates
        del exc
        structural = ValidationStatus.FAILED
        diagnostics["structural_validation"] = ("STRUCTURAL_VALIDATION_FAILED",)

    locator, locator_messages = _check_locators(
        snapshot,
        project_root=root,
        generation=generation,
        expected_lock_hash=manifest.corpus_lock_hash,
    )
    diagnostics["locator_verification"] = locator_messages

    semantic, semantic_messages = _check_semantic_audits(snapshot)
    diagnostics["semantic_audits_executed"] = semantic_messages

    citations, citation_messages = _check_citations(snapshot)
    diagnostics["citation_authorization"] = citation_messages

    if (body is None) != (assembly is None):
        exact_assembly = ValidationStatus.FAILED
        diagnostics["exact_assembly"] = (
            "Exact assembly requires both body bytes and an assembly record.",
        )
    elif body is None or assembly is None:
        exact_assembly = ValidationStatus.NOT_EXECUTED
        diagnostics["exact_assembly"] = (
            "No exact assembly was supplied for validation.",
        )
    else:
        try:
            if assembly.source_generation != generation:
                raise ValueError("assembly source generation is not CURRENT")
            verify_exact_assembly(snapshot, body, assembly)
            exact_assembly = ValidationStatus.PASSED
        except Exception as exc:
            del exc
            exact_assembly = ValidationStatus.FAILED
            diagnostics["exact_assembly"] = ("EXACT_ASSEMBLY_FAILED",)

    if journal_head is None:
        artifact_integrity = ValidationStatus.NOT_EXECUTED
        diagnostics["artifact_integrity"] = (
            "No pilot journal head was supplied.",
        )
    else:
        try:
            artifact = load_pilot_stage_artifact(
                root,
                journal_head,
                expected_registration=registration,
                expected_run_manifest_hash=manifest.manifest_hash,
            )
            if journal_head.owner_generation != generation:
                raise PilotJournalError("pilot journal head is not CURRENT")
            if artifact.stage_record.committed_generation != generation:
                raise PilotJournalError("pilot journal record is not CURRENT")
            artifact_integrity = ValidationStatus.PASSED
        except Exception as exc:
            del exc
            artifact_integrity = ValidationStatus.FAILED
            diagnostics["artifact_integrity"] = ("ARTIFACT_INTEGRITY_FAILED",)

    report = PilotValidationReport(
        run_id=manifest.run_id,
        source_generation=generation,
        repository_hash=snapshot.canonical_hash(),
        structural_validation=structural,
        locator_verification=locator,
        semantic_audits_executed=semantic,
        citation_authorization=citations,
        exact_assembly=exact_assembly,
        artifact_integrity=artifact_integrity,
        human_review_status=HumanReviewStatus.NOT_PERFORMED,
    )
    return SyntheticPilotValidationResult(
        report=report,
        diagnostics=MappingProxyType(dict(diagnostics)),
    )


def validate_synthetic_pilot_run(
    project_root: Path,
    *,
    registration: PilotRunRegistrationReference,
    journal_head: PilotStageArtifactReference | None,
    body: bytes | None = None,
    assembly: AssemblyRecord | None = None,
) -> SyntheticPilotValidationResult:
    """Validate one exact CURRENT synthetic run without making a pass claim.

    Registration and CURRENT are trust anchors: if either cannot be
    authenticated, no report is issued. The repository writer lock remains held
    through every check and a final registration/CURRENT/head authentication,
    so a report cannot span two canonical generations. Individual checks still
    produce independent PASSED, FAILED, or NOT_EXECUTED statuses.
    """

    root = project_root.resolve(strict=True)
    store = GenerationStore(root)
    try:
        with store.writer_lock():
            registered_artifact, manifest = load_pilot_run_registration(
                root, registration
            )
            generation, snapshot, _ = store.load_current()
            result = _validate_authenticated_snapshot(
                root,
                manifest=manifest,
                generation=generation,
                snapshot=snapshot,
                registration=registration,
                journal_head=journal_head,
                body=body,
                assembly=assembly,
            )

            final_registration, final_manifest = load_pilot_run_registration(
                root, registration, expected_manifest=manifest
            )
            final_generation, final_snapshot, _ = store.load_current()
            if (
                final_registration != registered_artifact
                or final_manifest != manifest
                or final_generation != generation
                or final_snapshot != snapshot
            ):
                raise PilotValidationError(
                    "pilot registration or CURRENT changed during validation"
                )
            if (
                journal_head is not None
                and result.report.artifact_integrity is ValidationStatus.PASSED
            ):
                final_head = load_pilot_stage_artifact(
                    root,
                    journal_head,
                    expected_registration=registration,
                    expected_run_manifest_hash=manifest.manifest_hash,
                )
                if (
                    journal_head.owner_generation != generation
                    or final_head.stage_record.committed_generation != generation
                ):
                    raise PilotValidationError(
                        "pilot journal head changed during validation"
                    )
            return result
    except PilotValidationError:
        raise
    except Exception as exc:
        raise PilotValidationError(
            "pilot registration or CURRENT generation could not be authenticated"
        ) from exc


__all__ = [
    "PilotValidationError",
    "SyntheticPilotValidationResult",
    "validate_synthetic_pilot_run",
]
