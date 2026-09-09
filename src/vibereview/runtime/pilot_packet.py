"""Offline, nonpublication evidence packets for the synthetic Package C pilot.

The live review repository is consulted only while a packet is captured.  The
resulting directory is a closed, content-addressed witness that can be checked
without trusting ``CURRENT`` (or even retaining the source repository).

The writer intentionally reuses the descriptor-retention primitives from
``runtime.artifacts``.  They are an internal runtime dependency, rather than a
second and subtly weaker implementation of the same no-follow boundary.
"""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ConfigDict, Field, model_validator

from vibereview.enums import CorpusFactDerivationType, ReviewProcessSourceType
from vibereview.ids import Sha256
from vibereview.models import CorpusFactDerivation

from .artifacts import (
    AssemblyRecord,
    PackageCArtifactError,
    _close_retained_packet_files,
    _descriptor_directory_path,
    _open_directory_at,
    _read_packet_file,
    _rename_directory_noreplace_at,
    _retain_directory_path,
    _retained_paths_overlap,
    _revalidate_retained_packet_file,
    _same_directory,
    _verify_directory_entry_at,
    _write_packet_file_at,
    verify_exact_assembly,
)
from .hashing import canonical_json_bytes, hash_bytes, hash_json
from .pilot_journal import (
    MAX_STAGE_CHAIN_EVENTS,
    PilotControlArtifact,
    PilotExactAssemblyControlPayload,
    PilotStageArtifact,
    PilotStageArtifactReference,
    PilotValidationControlPayload,
    load_pilot_control_artifact,
    load_pilot_run_registration,
    load_pilot_stage_artifact,
    validate_pilot_control_artifact_binding,
)
from .pilot_records import (
    PILOT_CORPUS_FACT_TEXT,
    PILOT_HUMAN_REVIEW_FACT_TEXT,
    PILOT_SCOPE_FACT_TEXT,
    HumanReviewStatus,
    PilotRunManifest,
    PilotRunRegistrationArtifact,
    PilotRunRegistrationReference,
    PilotStagePredecessor,
    PilotValidationReport,
    ValidationStatus,
)
from .pilot_usage import (
    PilotTaskUsageArtifact,
    canonical_pilot_task_usage_bytes,
    pilot_task_usage_budget_overruns,
)
from .pilot_validation import SyntheticPilotValidationResult
from .pilot_sequence import validate_fixed_pilot_artifact_sequence
from .receipts import (
    compute_input_identity_key,
    compute_semantic_task_key,
    verify_receipt_canonical_objects,
)
from .records import AppliedTaskReceipt, RuntimeModel, TaskProvenance
from .registry import CanonicalIdRegistry
from .repository import GenerationStore
from .state import RepositorySnapshot

if TYPE_CHECKING:
    from vibereview.library.models import (
        CorpusLockManifest,
        CorpusSelectionManifest,
        GenerationLibraryManifest,
    )


PILOT_PACKET_VERSION = "1"
PILOT_PACKET_MANIFEST_FILE = "pilot_packet.json"
MAX_PILOT_PACKET_FILES = 4_096
MAX_PILOT_PACKET_TOTAL_BYTES = 256 * 1024 * 1024
MAX_PILOT_PACKET_RECEIPTS = 4_096
MAX_PILOT_PACKET_DIAGNOSTICS_PER_CHECK = 32
MAX_PILOT_PACKET_DIAGNOSTIC_LENGTH = 4_096
MAX_PILOT_RAW_MARKDOWN_BYTES = 32 * 1024 * 1024
PILOT_CORPUS_PAPER_COUNT = 5

_CORPUS_LOCK_PACKET_FILE = "corpus-lock.json"
_CORPUS_IMPORT_PACKET_FILE = "corpus-import-manifest.json"
_CORPUS_SELECTION_PACKET_FILE = "corpus-selection-manifest.json"
_GENERATION_CORPUS_LOCK_FILE = "library/corpus.lock.json"
_GENERATION_CORPUS_IMPORT_FILE = "library/import_manifest.json"
_GENERATION_CORPUS_SELECTION_FILE = "library/selection_manifest.json"
_RAW_MARKDOWN_PATH = re.compile(
    r"^state/generations/([0-9]{6})/auxiliary/library/objects/sha256/"
    r"([0-9a-f]{64})/raw\.md$"
)

_BASE_PAYLOAD_FILES = frozenset(
    {
        "section.md",
        "assembly.json",
        "repository.json",
        "registry.json",
        "applied_tasks.json",
        "run_manifest.json",
        "run_registration.json",
        "validation_report.json",
        "validation_diagnostics.json",
        "human_review.json",
        _CORPUS_LOCK_PACKET_FILE,
        _CORPUS_IMPORT_PACKET_FILE,
        _CORPUS_SELECTION_PACKET_FILE,
    }
)
_VALIDATION_CHECKS = (
    "structural_validation",
    "locator_verification",
    "semantic_audits_executed",
    "citation_authorization",
    "exact_assembly",
    "artifact_integrity",
)
_SAFE_PACKET_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,254}$")
_SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "credentials",
        "access_token",
        "refresh_token",
        "password",
        "passwd",
        "secret",
    }
)
_PRIVATE_PROVENANCE_FIELD_NAMES = frozenset({"source_path"})


class PilotPacketError(ValueError):
    """The packet is incomplete, unsafe, or cryptographically inconsistent."""


class PilotPacketModel(RuntimeModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _safe_packet_name(name: str) -> bool:
    return bool(_SAFE_PACKET_NAME.fullmatch(name)) and name not in {".", ".."}


def _safe_auxiliary_path(path: str) -> bool:
    parsed = PurePosixPath(path)
    return (
        bool(path)
        and len(path) <= 1_024
        and not parsed.is_absolute()
        and parsed.as_posix() == path
        and all(part not in {"", ".", ".."} for part in parsed.parts)
    )


def _model_bytes(model: RuntimeModel) -> bytes:
    return canonical_json_bytes(model.model_dump(mode="json")) + b"\n"


def _json_bytes(value: Any) -> bytes:
    return canonical_json_bytes(value) + b"\n"


class PilotPacketFileRecord(PilotPacketModel):
    relative_path: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0, le=32 * 1024 * 1024)
    content_hash: Sha256

    @model_validator(mode="after")
    def _flat_safe_path(self) -> "PilotPacketFileRecord":
        if not _safe_packet_name(self.relative_path):
            raise ValueError("pilot packet filename is unsafe")
        if self.relative_path == PILOT_PACKET_MANIFEST_FILE:
            raise ValueError("pilot packet manifest cannot list itself")
        return self


class PilotPacketJournalEntry(PilotPacketModel):
    reference: PilotStageArtifactReference
    packet_relative_path: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def _content_addressed_name(self) -> "PilotPacketJournalEntry":
        digest = self.reference.artifact_hash.removeprefix("sha256:")
        expected = f"journal-stage-{self.reference.ordinal:04d}-{digest}.json"
        if self.packet_relative_path != expected:
            raise ValueError("pilot journal packet path is not content addressed")
        return self


class PilotPacketDomainArtifact(PilotPacketModel):
    owner_generation: int = Field(ge=1)
    source_relative_path: str = Field(min_length=1, max_length=1_024)
    packet_relative_path: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0, le=32 * 1024 * 1024)
    content_hash: Sha256

    @model_validator(mode="after")
    def _safe_content_addressed_paths(self) -> "PilotPacketDomainArtifact":
        if not _safe_auxiliary_path(self.source_relative_path):
            raise ValueError("pilot domain source path is unsafe")
        digest = self.content_hash.removeprefix("sha256:")
        if self.packet_relative_path != f"domain-{digest}.bin":
            raise ValueError("pilot domain packet path is not content addressed")
        return self


class PilotPacketCorpusSource(PilotPacketModel):
    """One exact raw Markdown object needed for offline locator replay."""

    paper_id: str = Field(pattern=r"^P[0-9]{4,}$")
    source_relative_path: str = Field(min_length=1, max_length=1_024)
    raw_md_path: str = Field(min_length=1, max_length=1_024)
    packet_relative_path: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=1, le=MAX_PILOT_RAW_MARKDOWN_BYTES)
    source_hash: Sha256
    raw_md_hash: Sha256

    @model_validator(mode="after")
    def _closed_source_identity(self) -> "PilotPacketCorpusSource":
        if not _safe_auxiliary_path(self.source_relative_path):
            raise ValueError("pilot corpus source path is unsafe")
        match = _RAW_MARKDOWN_PATH.fullmatch(self.raw_md_path)
        if match is None or f"sha256:{match.group(2)}" != self.raw_md_hash:
            raise ValueError("pilot raw Markdown path is not content addressed")
        digest = self.raw_md_hash.removeprefix("sha256:")
        if self.packet_relative_path != f"corpus-raw-{digest}.md":
            raise ValueError("pilot raw Markdown packet path is not content addressed")
        return self


class PilotPacketDiagnostics(PilotPacketModel):
    schema_version: Literal["package-c-pilot-diagnostics-1"] = (
        "package-c-pilot-diagnostics-1"
    )
    checks: dict[str, tuple[str, ...]]

    @model_validator(mode="after")
    def _closed_bounded_checks(self) -> "PilotPacketDiagnostics":
        if tuple(sorted(self.checks)) != tuple(sorted(_VALIDATION_CHECKS)):
            raise ValueError("pilot diagnostics must cover the exact validation checks")
        for messages in self.checks.values():
            if len(messages) > MAX_PILOT_PACKET_DIAGNOSTICS_PER_CHECK:
                raise ValueError("pilot diagnostics exceed their message budget")
            if any(
                not message or len(message) > MAX_PILOT_PACKET_DIAGNOSTIC_LENGTH
                for message in messages
            ):
                raise ValueError("pilot diagnostic message is empty or too large")
        return self


class PilotPacketHumanReview(PilotPacketModel):
    schema_version: Literal["package-c-pilot-human-review-1"] = (
        "package-c-pilot-human-review-1"
    )
    run_id: str
    source_generation: int = Field(ge=0)
    validation_report_hash: Sha256
    status: Literal[HumanReviewStatus.NOT_PERFORMED] = (
        HumanReviewStatus.NOT_PERFORMED
    )
    statement: Literal[PILOT_HUMAN_REVIEW_FACT_TEXT] = (
        PILOT_HUMAN_REVIEW_FACT_TEXT
    )
    publication_eligible: Literal[False] = False


class SyntheticPilotPacketManifest(PilotPacketModel):
    """The complete, exact inventory and trust anchors of one pilot packet."""

    schema_version: Literal["package-c-synthetic-pilot-packet-1"] = (
        "package-c-synthetic-pilot-packet-1"
    )
    synthetic_fixture: Literal[True] = True
    source_generation: int = Field(ge=1)
    repository_hash: Sha256
    registry_hash: Sha256
    section_body_hash: Sha256
    assembly_artifact_hash: Sha256
    accepted_receipts_hash: Sha256
    accepted_receipt_count: int = Field(ge=0, le=MAX_PILOT_PACKET_RECEIPTS)
    registration: PilotRunRegistrationReference
    run_manifest_hash: Sha256
    run_manifest_content_hash: Sha256
    corpus_lock_hash: Sha256
    selection_manifest_hash: Sha256
    corpus_import_manifest_hash: Sha256
    corpus_sources: tuple[PilotPacketCorpusSource, ...] = Field(
        min_length=PILOT_CORPUS_PAPER_COUNT,
        max_length=PILOT_CORPUS_PAPER_COUNT,
    )
    journal_head: PilotStageArtifactReference
    journal: tuple[PilotPacketJournalEntry, ...] = Field(
        min_length=1, max_length=MAX_STAGE_CHAIN_EVENTS
    )
    domain_artifacts: tuple[PilotPacketDomainArtifact, ...] = Field(
        max_length=MAX_PILOT_PACKET_FILES
    )
    validation_report_hash: Sha256
    validation_diagnostics_hash: Sha256
    files: tuple[PilotPacketFileRecord, ...] = Field(
        min_length=len(_BASE_PAYLOAD_FILES), max_length=MAX_PILOT_PACKET_FILES
    )
    human_review: Literal["NOT_PERFORMED"] = "NOT_PERFORMED"
    publication_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _closed_inventory_and_chain(self) -> "SyntheticPilotPacketManifest":
        file_names = tuple(item.relative_path for item in self.files)
        if file_names != tuple(sorted(file_names)) or len(set(file_names)) != len(
            file_names
        ):
            raise ValueError("pilot packet file inventory must be sorted and unique")
        journal_keys = tuple(
            (item.reference.ordinal, item.packet_relative_path) for item in self.journal
        )
        if journal_keys != tuple(sorted(journal_keys)):
            raise ValueError("pilot packet journal must be ordered by ordinal")
        if tuple(item.reference.ordinal for item in self.journal) != tuple(
            range(1, len(self.journal) + 1)
        ):
            raise ValueError("pilot packet journal ordinals are not closed")
        if self.journal[-1].reference != self.journal_head:
            raise ValueError("pilot packet journal does not end at its declared head")
        if (
            self.journal_head.owner_generation != self.source_generation
            or self.journal_head.run_id != self.registration.run_id
        ):
            raise ValueError("pilot packet source, run, and journal head differ")
        domain_keys = tuple(
            (item.owner_generation, item.source_relative_path)
            for item in self.domain_artifacts
        )
        if domain_keys != tuple(sorted(domain_keys)) or len(set(domain_keys)) != len(
            domain_keys
        ):
            raise ValueError("pilot domain inventory must be sorted and unique")
        corpus_keys = tuple(
            (item.paper_id, item.source_relative_path) for item in self.corpus_sources
        )
        if corpus_keys != tuple(sorted(corpus_keys)) or len(set(corpus_keys)) != len(
            corpus_keys
        ):
            raise ValueError("pilot corpus inventory must be sorted and unique")
        if len({item.raw_md_path for item in self.corpus_sources}) != len(
            self.corpus_sources
        ):
            raise ValueError("pilot corpus raw paths must be unique")
        expected_names = set(_BASE_PAYLOAD_FILES)
        expected_names.update(item.packet_relative_path for item in self.journal)
        expected_names.update(
            item.packet_relative_path for item in self.domain_artifacts
        )
        expected_names.update(
            item.packet_relative_path for item in self.corpus_sources
        )
        if set(file_names) != expected_names:
            raise ValueError(
                "pilot packet manifest does not cover its exact payload set"
            )
        return self

    @property
    def artifact_hash(self) -> Sha256:
        return hash_bytes(_model_bytes(self))


@dataclass(frozen=True, slots=True)
class PilotJournalCapture:
    reference: PilotStageArtifactReference
    artifact: PilotStageArtifact


@dataclass(frozen=True, slots=True)
class PilotDomainCapture:
    owner_generation: int
    source_relative_path: str
    content: bytes


@dataclass(frozen=True, slots=True)
class PreparedSyntheticPilotPacket:
    manifest: SyntheticPilotPacketManifest
    files: Mapping[str, bytes]


def _semantic_fingerprint_hash(receipt: AppliedTaskReceipt) -> Sha256:
    fingerprint = receipt.semantic_fingerprint
    return hash_json(
        {
            "validator_fingerprint": fingerprint.validator_fingerprint,
            "promotion_handler_fingerprint": (
                fingerprint.promotion_handler_fingerprint
            ),
            "disposition_handler_fingerprint": (
                fingerprint.disposition_handler_fingerprint
            ),
            "scientific_contract_version": fingerprint.scientific_contract_version,
            "runtime_contract_version": fingerprint.runtime_contract_version,
        }
    )


def _verify_receipt_payload(
    receipt: AppliedTaskReceipt, snapshot: RepositorySnapshot
) -> None:
    from .specs import TASK_SPECS

    try:
        TASK_SPECS[receipt.task_type].proposal_model.model_validate(
            receipt.proposal_payload
        )
    except Exception as exc:
        raise PilotPacketError(
            "pilot packet receipt proposal does not match its TaskSpec"
        ) from exc
    if receipt.proposal_hash != hash_json(receipt.proposal_payload):
        raise PilotPacketError("pilot packet receipt proposal hash mismatch")
    if (
        receipt.semantic_fingerprint.combined_fingerprint
        != _semantic_fingerprint_hash(receipt)
    ):
        raise PilotPacketError("pilot packet receipt semantic fingerprint mismatch")
    if receipt.semantic_task_key != compute_semantic_task_key(
        input_identity_key=receipt.input_identity_key,
        semantic_fingerprint=receipt.semantic_fingerprint,
    ):
        raise PilotPacketError("pilot packet receipt semantic key mismatch")
    if receipt.committed_generation != receipt.source_generation + 1:
        raise PilotPacketError("pilot packet receipt generation lineage is invalid")
    qualified_ids = tuple(item.qualified_id for item in receipt.canonical_objects)
    if len(qualified_ids) != len(set(qualified_ids)):
        raise PilotPacketError("pilot packet receipt repeats a canonical witness")
    raw_ids = {item.rsplit(":", 1)[-1] for item in qualified_ids}
    if any(value not in raw_ids for value in receipt.local_ref_map.values()):
        raise PilotPacketError("pilot packet receipt local map lacks a witness")
    valid, reason = verify_receipt_canonical_objects(receipt, snapshot)
    if not valid:
        raise PilotPacketError(reason or "pilot packet receipt witness changed")


def _verify_receipt_provenance(
    receipt: AppliedTaskReceipt,
    provenance: TaskProvenance,
    *,
    safe_engine_configuration_hash: Sha256,
) -> None:
    if (
        receipt.task_type is not provenance.task_type
        or receipt.task_spec_version != provenance.task_spec_version
        or receipt.source_generation != provenance.base_generation
    ):
        raise PilotPacketError("pilot journal receipt and provenance differ")
    prefix = f"{provenance.task_id}/"
    suffix = receipt.accepted_attempt_id.removeprefix(prefix)
    if (
        not receipt.accepted_attempt_id.startswith(prefix)
        or not suffix
        or "/" in suffix
    ):
        raise PilotPacketError("pilot journal receipt attempt is not task-owned")
    resource_hashes = {
        item.resource_id: item.snapshot_hash for item in provenance.resources
    }
    expected_input_key = compute_input_identity_key(
        task_type=receipt.task_type,
        task_spec_version=receipt.task_spec_version,
        prompt_hash=provenance.instructions_hash,
        input_schema_hash=provenance.input_schema_hash,
        proposal_schema_hash=provenance.proposal_schema_hash,
        dependency_hashes=provenance.dependencies,
        resource_hashes=resource_hashes,
        engine_input_hash=provenance.engine_input_hash,
        engine="ordered-engine-plan",
        engine_version=receipt.semantic_fingerprint.runtime_contract_version,
        safe_engine_configuration_hash=safe_engine_configuration_hash,
        scientific_contract_version=(
            receipt.semantic_fingerprint.scientific_contract_version
        ),
    )
    if receipt.input_identity_key != expected_input_key:
        raise PilotPacketError("pilot journal receipt input-identity key mismatch")


def _process_source_key(manifest: PilotRunManifest, suffix: str) -> str:
    return f"pilot-run:{manifest.manifest_hash.removeprefix('sha256:')}:{suffix}"


def _verify_registration_snapshot(
    snapshot: RepositorySnapshot,
    registration: PilotRunRegistrationArtifact,
    manifest: PilotRunManifest,
) -> None:
    papers = tuple(sorted(snapshot.papers, key=lambda item: item.paper_id))
    if len(papers) != 5 or tuple(item.source_hash for item in papers) != (
        manifest.paper_source_hashes
    ):
        raise PilotPacketError("pilot packet does not contain the registered papers")
    paper_ids = tuple(item.paper_id for item in papers)
    corpus = tuple(
        item
        for item in snapshot.corpus_facts
        if item.corpus_fact_id == registration.corpus_fact_id
        and item.text == PILOT_CORPUS_FACT_TEXT
        and item.derivation_type is CorpusFactDerivationType.REGISTRY_ARITHMETIC
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
    scope = tuple(
        item
        for item in snapshot.process_facts
        if item.process_fact_id == registration.scope_process_fact_id
        and item.text == PILOT_SCOPE_FACT_TEXT
        and item.source_type is ReviewProcessSourceType.RUN_MANIFEST
        and item.source_key == scope_key
    )
    human = tuple(
        item
        for item in snapshot.process_facts
        if item.process_fact_id == registration.human_review_process_fact_id
        and item.text == PILOT_HUMAN_REVIEW_FACT_TEXT
        and item.source_type is ReviewProcessSourceType.RUN_MANIFEST
        and item.source_key == human_key
    )
    if not (len(corpus) == len(scope) == len(human) == 1):
        raise PilotPacketError("pilot packet registered fact set is absent or changed")


def _reject_secret_fields(value: Any) -> None:
    pending = [(value, False)]
    nodes = 0
    while pending:
        current, in_task_provenance = pending.pop()
        nodes += 1
        if nodes > 1_000_000:
            raise PilotPacketError("pilot packet JSON exceeds its inspection budget")
        if isinstance(current, dict):
            for key, child in current.items():
                normalized = str(key).strip().lower().replace("-", "_")
                if normalized in _SECRET_FIELD_NAMES:
                    raise PilotPacketError(
                        f"pilot packet contains forbidden secret field {key!r}"
                    )
                if normalized in _PRIVATE_PROVENANCE_FIELD_NAMES:
                    raise PilotPacketError(
                        f"pilot packet contains private provenance field {key!r}"
                    )
                pending.append(
                    (child, in_task_provenance or normalized == "task_provenance")
                )
        elif isinstance(current, list):
            pending.extend((child, in_task_provenance) for child in current)
        elif in_task_provenance and isinstance(current, str):
            if PurePosixPath(current).is_absolute() or re.match(
                r"^[A-Za-z]:[\\/]", current
            ):
                raise PilotPacketError(
                    "pilot packet contains an absolute private provenance path"
                )


def _verify_no_obvious_secrets(values: Mapping[str, bytes]) -> None:
    forbidden_markers = (
        b"-----BEGIN PRIVATE KEY-----",
        b"-----BEGIN RSA PRIVATE KEY-----",
    )
    for name, content in values.items():
        if any(marker in content for marker in forbidden_markers):
            raise PilotPacketError(
                f"pilot packet contains private-key material: {name}"
            )
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        _reject_secret_fields(value)


def _stage_packet_name(reference: PilotStageArtifactReference) -> str:
    return (
        f"journal-stage-{reference.ordinal:04d}-"
        f"{reference.artifact_hash.removeprefix('sha256:')}.json"
    )


def _domain_packet_name(content_hash: Sha256) -> str:
    return f"domain-{content_hash.removeprefix('sha256:')}.bin"


def _corpus_raw_packet_name(content_hash: Sha256) -> str:
    return f"corpus-raw-{content_hash.removeprefix('sha256:')}.md"


def build_synthetic_pilot_packet(
    *,
    snapshot: RepositorySnapshot,
    registry: CanonicalIdRegistry,
    receipts: Sequence[AppliedTaskReceipt],
    body: bytes,
    assembly: AssemblyRecord,
    run_manifest: PilotRunManifest,
    registration_artifact: PilotRunRegistrationArtifact,
    registration: PilotRunRegistrationReference,
    journal: Sequence[PilotJournalCapture],
    domain_artifacts: Sequence[PilotDomainCapture],
    corpus_lock: CorpusLockManifest,
    corpus_import_manifest: GenerationLibraryManifest,
    corpus_selection_manifest: CorpusSelectionManifest,
    corpus_raw_markdown: Mapping[str, bytes],
    validation: SyntheticPilotValidationResult,
) -> PreparedSyntheticPilotPacket:
    """Build deterministic packet bytes from already authenticated live values."""

    from vibereview.library.models import (
        CorpusLockManifest,
        CorpusSelectionManifest,
        GenerationLibraryManifest,
    )

    try:
        exact_snapshot = RepositorySnapshot.model_validate(
            snapshot.model_dump(mode="json")
        )
        exact_registry = CanonicalIdRegistry.model_validate(
            registry.model_dump(mode="json")
        )
        exact_receipts = tuple(
            AppliedTaskReceipt.model_validate(item.model_dump(mode="json"))
            for item in receipts
        )
        exact_manifest = PilotRunManifest.model_validate(
            run_manifest.model_dump(mode="json")
        )
        exact_registration_artifact = PilotRunRegistrationArtifact.model_validate(
            registration_artifact.model_dump(mode="json")
        )
        exact_registration = PilotRunRegistrationReference.model_validate(
            registration.model_dump(mode="json")
        )
        exact_corpus_lock = CorpusLockManifest.model_validate(
            corpus_lock.model_dump(mode="json")
        )
        exact_corpus_import = GenerationLibraryManifest.model_validate(
            corpus_import_manifest.model_dump(mode="json")
        )
        exact_corpus_selection = CorpusSelectionManifest.model_validate(
            corpus_selection_manifest.model_dump(mode="json")
        )
        exact_report = PilotValidationReport.model_validate(
            validation.report.model_dump(mode="json")
        )
        if set(validation.diagnostics) != set(_VALIDATION_CHECKS):
            raise ValueError(
                "validation diagnostics do not cover the exact check set"
            )
        diagnostics = PilotPacketDiagnostics(
            checks={
                name: tuple(validation.diagnostics.get(name, ()))
                for name in _VALIDATION_CHECKS
            }
        )
    except Exception as exc:
        raise PilotPacketError("pilot packet inputs are invalid") from exc
    if len(exact_receipts) > MAX_PILOT_PACKET_RECEIPTS:
        raise PilotPacketError("pilot packet receipt count exceeds its bound")

    payloads: dict[str, bytes] = {
        "section.md": bytes(body),
        "assembly.json": _model_bytes(assembly),
        "repository.json": _model_bytes(exact_snapshot),
        "registry.json": _model_bytes(exact_registry),
        "applied_tasks.json": _json_bytes(
            [item.model_dump(mode="json") for item in exact_receipts]
        ),
        "run_manifest.json": _model_bytes(exact_manifest),
        "run_registration.json": _model_bytes(exact_registration_artifact),
        "validation_report.json": _model_bytes(exact_report),
        "validation_diagnostics.json": _model_bytes(diagnostics),
        _CORPUS_LOCK_PACKET_FILE: _model_bytes(exact_corpus_lock),
        _CORPUS_IMPORT_PACKET_FILE: _model_bytes(exact_corpus_import),
        _CORPUS_SELECTION_PACKET_FILE: canonical_json_bytes(
            exact_corpus_selection.model_dump(mode="json")
        ),
    }
    human_review = PilotPacketHumanReview(
        run_id=exact_manifest.run_id,
        source_generation=exact_report.source_generation,
        validation_report_hash=hash_bytes(payloads["validation_report.json"]),
    )
    payloads["human_review.json"] = _model_bytes(human_review)

    raw_keys = set(corpus_raw_markdown)
    expected_raw_keys = {item.paper_id for item in exact_corpus_lock.papers}
    if raw_keys != expected_raw_keys:
        raise PilotPacketError(
            "pilot packet raw Markdown does not exactly cover its corpus lock"
        )
    corpus_sources: list[PilotPacketCorpusSource] = []
    for locked in sorted(exact_corpus_lock.papers, key=lambda item: item.paper_id):
        content = bytes(corpus_raw_markdown[locked.paper_id])
        if not content or len(content) > MAX_PILOT_RAW_MARKDOWN_BYTES:
            raise PilotPacketError("pilot packet raw Markdown exceeds its byte bound")
        name = _corpus_raw_packet_name(locked.raw_md_hash)
        previous = payloads.get(name)
        if previous is not None and previous != content:
            raise PilotPacketError("pilot raw Markdown content-address collision")
        payloads[name] = content
        corpus_sources.append(
            PilotPacketCorpusSource(
                paper_id=locked.paper_id,
                source_relative_path=locked.source_relative_path,
                raw_md_path=locked.raw_md_path,
                packet_relative_path=name,
                size_bytes=len(content),
                source_hash=locked.source_hash,
                raw_md_hash=locked.raw_md_hash,
            )
        )

    journal_entries: list[PilotPacketJournalEntry] = []
    for capture in journal:
        reference = PilotStageArtifactReference.model_validate(
            capture.reference.model_dump(mode="json")
        )
        artifact = PilotStageArtifact.model_validate(
            capture.artifact.model_dump(mode="json")
        )
        name = _stage_packet_name(reference)
        content = _model_bytes(artifact)
        previous = payloads.get(name)
        if previous is not None and previous != content:
            raise PilotPacketError("pilot journal content-address collision")
        payloads[name] = content
        journal_entries.append(
            PilotPacketJournalEntry(
                reference=reference,
                packet_relative_path=name,
            )
        )

    domain_entries: list[PilotPacketDomainArtifact] = []
    for capture in sorted(
        domain_artifacts,
        key=lambda item: (item.owner_generation, item.source_relative_path),
    ):
        content = bytes(capture.content)
        content_hash = hash_bytes(content)
        name = _domain_packet_name(content_hash)
        previous = payloads.get(name)
        if previous is not None and previous != content:
            raise PilotPacketError("pilot domain content-address collision")
        payloads[name] = content
        domain_entries.append(
            PilotPacketDomainArtifact(
                owner_generation=capture.owner_generation,
                source_relative_path=capture.source_relative_path,
                packet_relative_path=name,
                size_bytes=len(content),
                content_hash=content_hash,
            )
        )

    if len(payloads) > MAX_PILOT_PACKET_FILES:
        raise PilotPacketError("pilot packet file count exceeds its bound")
    if any(len(content) > 32 * 1024 * 1024 for content in payloads.values()):
        raise PilotPacketError("pilot packet contains an oversized file")
    file_records = tuple(
        PilotPacketFileRecord(
            relative_path=name,
            size_bytes=len(content),
            content_hash=hash_bytes(content),
        )
        for name, content in sorted(payloads.items())
    )
    if not journal_entries:
        raise PilotPacketError("pilot packet requires a complete journal chain")
    packet_manifest = SyntheticPilotPacketManifest(
        source_generation=journal_entries[-1].reference.owner_generation,
        repository_hash=exact_snapshot.canonical_hash(),
        registry_hash=hash_json(exact_registry.model_dump(mode="json")),
        section_body_hash=hash_bytes(bytes(body)),
        assembly_artifact_hash=hash_bytes(payloads["assembly.json"]),
        accepted_receipts_hash=hash_bytes(payloads["applied_tasks.json"]),
        accepted_receipt_count=len(exact_receipts),
        registration=exact_registration,
        run_manifest_hash=exact_manifest.manifest_hash,
        run_manifest_content_hash=hash_bytes(payloads["run_manifest.json"]),
        corpus_lock_hash=hash_bytes(payloads[_CORPUS_LOCK_PACKET_FILE]),
        selection_manifest_hash=hash_bytes(
            payloads[_CORPUS_SELECTION_PACKET_FILE]
        ),
        corpus_import_manifest_hash=hash_bytes(
            payloads[_CORPUS_IMPORT_PACKET_FILE]
        ),
        corpus_sources=tuple(corpus_sources),
        journal_head=journal_entries[-1].reference,
        journal=tuple(journal_entries),
        domain_artifacts=tuple(domain_entries),
        validation_report_hash=hash_bytes(payloads["validation_report.json"]),
        validation_diagnostics_hash=hash_bytes(
            payloads["validation_diagnostics.json"]
        ),
        files=file_records,
    )
    payloads[PILOT_PACKET_MANIFEST_FILE] = _model_bytes(packet_manifest)
    if (
        len(payloads[PILOT_PACKET_MANIFEST_FILE]) > 32 * 1024 * 1024
        or sum(len(content) for content in payloads.values())
        > MAX_PILOT_PACKET_TOTAL_BYTES
    ):
        raise PilotPacketError("pilot packet exceeds its byte budget")
    _verify_packet_values(payloads)
    return PreparedSyntheticPilotPacket(
        manifest=packet_manifest,
        files=MappingProxyType(dict(sorted(payloads.items()))),
    )


def _parse_canonical_model(
    values: Mapping[str, bytes],
    name: str,
    model: type[RuntimeModel],
    *,
    trailing_newline: bool = True,
) -> RuntimeModel:
    try:
        content = values[name]
        parsed = model.model_validate_json(content)
    except Exception as exc:
        raise PilotPacketError(f"pilot packet contains invalid {name}") from exc
    expected = canonical_json_bytes(parsed.model_dump(mode="json"))
    if trailing_newline:
        expected += b"\n"
    if content != expected:
        raise PilotPacketError(f"pilot packet JSON is not canonical: {name}")
    return parsed


def _expected_corpus_resource_hashes(
    lock: CorpusLockManifest,
) -> dict[str, Sha256]:
    resources = {item.raw_md_path: item.raw_md_hash for item in lock.papers}
    for item in lock.import_source.objects:
        if item.role not in {"bibliography", "graph"}:
            continue
        digest = item.content_sha256.removeprefix("sha256:")
        path = (
            f"state/generations/{lock.committed_generation:06d}/auxiliary/"
            f"library/source_objects/sha256/{digest}/{item.role}.blob"
        )
        resources[path] = item.content_sha256
    return resources


def _verify_embedded_corpus(
    values: Mapping[str, bytes],
    *,
    packet_manifest: SyntheticPilotPacketManifest,
    run_manifest: PilotRunManifest,
    snapshot: RepositorySnapshot,
) -> None:
    from vibereview.library.models import (
        CorpusLockManifest,
        CorpusSelectionManifest,
        GenerationLibraryManifest,
    )

    lock = _parse_canonical_model(
        values, _CORPUS_LOCK_PACKET_FILE, CorpusLockManifest
    )
    import_manifest = _parse_canonical_model(
        values, _CORPUS_IMPORT_PACKET_FILE, GenerationLibraryManifest
    )
    selection = _parse_canonical_model(
        values,
        _CORPUS_SELECTION_PACKET_FILE,
        CorpusSelectionManifest,
        trailing_newline=False,
    )
    assert isinstance(lock, CorpusLockManifest)
    assert isinstance(import_manifest, GenerationLibraryManifest)
    assert isinstance(selection, CorpusSelectionManifest)

    lock_hash = hash_bytes(values[_CORPUS_LOCK_PACKET_FILE])
    selection_hash = hash_bytes(values[_CORPUS_SELECTION_PACKET_FILE])
    import_hash = hash_bytes(values[_CORPUS_IMPORT_PACKET_FILE])
    if (
        packet_manifest.corpus_lock_hash != lock_hash
        or packet_manifest.corpus_lock_hash != run_manifest.corpus_lock_hash
        or packet_manifest.selection_manifest_hash != selection_hash
        or packet_manifest.selection_manifest_hash
        != run_manifest.selection_manifest_hash
        or packet_manifest.corpus_import_manifest_hash != import_hash
        or import_manifest.corpus_lock_hash != lock_hash
        or import_manifest.selection_manifest_hash != selection_hash
        or import_manifest.import_source_hash != lock.import_source_hash
        or lock.selection_manifest_hash != selection_hash
        or lock.import_source.selection_manifest_hash != selection_hash
        or lock.import_source_hash
        != hash_json(lock.import_source.model_dump(mode="json"))
    ):
        raise PilotPacketError("pilot packet corpus witness hashes disagree")
    if (
        import_manifest.generation != lock.committed_generation
        or lock.committed_generation > run_manifest.source_generation
        or selection.library_id != lock.library_id
        or selection.source_commit != lock.source_commit
        or import_manifest.resource_hashes
        != _expected_corpus_resource_hashes(lock)
    ):
        raise PilotPacketError("pilot packet corpus witness identities disagree")

    selected = {
        (item.source_relative_path, item.content_sha256)
        for item in selection.documents
        if item.decision == "include"
    }
    locked_selected = {
        (item.source_relative_path, item.source_hash) for item in lock.papers
    }
    if selected != locked_selected:
        raise PilotPacketError("pilot packet selection differs from its corpus lock")
    if len(lock.papers) != PILOT_CORPUS_PAPER_COUNT:
        raise PilotPacketError("pilot packet corpus lock must contain five papers")

    packet_sources = {item.paper_id: item for item in packet_manifest.corpus_sources}
    locked_sources = {item.paper_id: item for item in lock.papers}
    snapshot_papers = {item.paper_id: item for item in snapshot.papers}
    if (
        set(packet_sources) != set(locked_sources)
        or set(locked_sources) != set(snapshot_papers)
        or tuple(
            item.source_hash for item in sorted(lock.papers, key=lambda item: item.paper_id)
        )
        != run_manifest.paper_source_hashes
    ):
        raise PilotPacketError("pilot packet corpus paper coverage differs")

    texts: dict[str, str] = {}
    for paper_id, locked in locked_sources.items():
        source = packet_sources[paper_id]
        paper = snapshot_papers[paper_id]
        if (
            source.source_relative_path != locked.source_relative_path
            or source.raw_md_path != locked.raw_md_path
            or source.source_hash != locked.source_hash
            or source.raw_md_hash != locked.raw_md_hash
            or paper.source_hash != locked.source_hash
            or paper.raw_md_hash != locked.raw_md_hash
            or paper.raw_md_path != locked.raw_md_path
        ):
            raise PilotPacketError("pilot packet corpus source identity differs")
        match = _RAW_MARKDOWN_PATH.fullmatch(locked.raw_md_path)
        if (
            match is None
            or int(match.group(1)) > lock.committed_generation
            or f"sha256:{match.group(2)}" != locked.raw_md_hash
        ):
            raise PilotPacketError("pilot packet raw Markdown path is invalid")
        content = values[source.packet_relative_path]
        observed_hash = hash_bytes(content)
        if (
            not content
            or len(content) != source.size_bytes
            or len(content) > MAX_PILOT_RAW_MARKDOWN_BYTES
            or observed_hash != source.raw_md_hash
            or observed_hash != source.source_hash
        ):
            raise PilotPacketError("pilot packet raw Markdown hash differs")
        try:
            texts[paper_id] = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise PilotPacketError("pilot packet raw Markdown is not UTF-8") from exc

    for span in snapshot.retrieved_spans:
        text = texts.get(span.paper_id)
        source = packet_sources.get(span.paper_id)
        if text is None or source is None:
            raise PilotPacketError("pilot packet span names an unlocked paper")
        locator = span.locator
        if (
            locator.raw_md_path != source.raw_md_path
            or locator.end_offset > len(text)
        ):
            raise PilotPacketError("pilot packet span locator differs from raw Markdown")
        observed = text[locator.start_offset : locator.end_offset]
        if (
            observed != span.source_text
            or hash_bytes(observed.encode("utf-8")) != locator.source_span_hash
        ):
            raise PilotPacketError("pilot packet span source slice or hash differs")


def _verify_validation_acceptance_gate(
    report: PilotValidationReport,
    snapshot: RepositorySnapshot,
) -> None:
    has_spans = bool(snapshot.retrieved_spans)
    has_citation_bindings = any(
        proposition.citation_bindings
        for proposition in snapshot.proposition_records
    )
    if report.locator_verification is ValidationStatus.FAILED:
        raise PilotPacketError("pilot packet rejects failed locator verification")
    if report.citation_authorization is ValidationStatus.FAILED:
        raise PilotPacketError("pilot packet rejects failed citation authorization")
    if has_spans and report.locator_verification is not ValidationStatus.PASSED:
        raise PilotPacketError(
            "pilot packet requires passed locator verification for canonical spans"
        )
    if (
        has_citation_bindings
        and report.citation_authorization is not ValidationStatus.PASSED
    ):
        raise PilotPacketError(
            "pilot packet requires passed citation authorization for bindings"
        )
    if (
        report.locator_verification is ValidationStatus.NOT_EXECUTED
        and has_spans
    ):
        raise PilotPacketError(
            "pilot packet cannot skip locator verification with canonical spans"
        )
    if (
        report.citation_authorization is ValidationStatus.NOT_EXECUTED
        and has_citation_bindings
    ):
        raise PilotPacketError(
            "pilot packet cannot skip citation authorization with bindings"
        )


def _verify_packet_values(values: Mapping[str, bytes]) -> SyntheticPilotPacketManifest:
    manifest = _parse_canonical_model(
        values, PILOT_PACKET_MANIFEST_FILE, SyntheticPilotPacketManifest
    )
    assert isinstance(manifest, SyntheticPilotPacketManifest)
    expected_names = {PILOT_PACKET_MANIFEST_FILE}
    expected_names.update(item.relative_path for item in manifest.files)
    if set(values) != expected_names:
        raise PilotPacketError("pilot packet file set differs from its manifest")
    if len(values) > MAX_PILOT_PACKET_FILES + 1:
        raise PilotPacketError("pilot packet file count exceeds its bound")
    if sum(len(content) for content in values.values()) > MAX_PILOT_PACKET_TOTAL_BYTES:
        raise PilotPacketError("pilot packet exceeds its byte budget")
    for record in manifest.files:
        content = values[record.relative_path]
        if (
            len(content) != record.size_bytes
            or hash_bytes(content) != record.content_hash
        ):
            raise PilotPacketError(
                f"pilot packet digest mismatch: {record.relative_path}"
            )
    _verify_no_obvious_secrets(values)

    assembly = _parse_canonical_model(values, "assembly.json", AssemblyRecord)
    snapshot = _parse_canonical_model(values, "repository.json", RepositorySnapshot)
    registry = _parse_canonical_model(values, "registry.json", CanonicalIdRegistry)
    run_manifest = _parse_canonical_model(
        values, "run_manifest.json", PilotRunManifest
    )
    registration_artifact = _parse_canonical_model(
        values, "run_registration.json", PilotRunRegistrationArtifact
    )
    report = _parse_canonical_model(
        values, "validation_report.json", PilotValidationReport
    )
    diagnostics = _parse_canonical_model(
        values, "validation_diagnostics.json", PilotPacketDiagnostics
    )
    human_review = _parse_canonical_model(
        values, "human_review.json", PilotPacketHumanReview
    )
    assert isinstance(assembly, AssemblyRecord)
    assert isinstance(snapshot, RepositorySnapshot)
    assert isinstance(registry, CanonicalIdRegistry)
    assert isinstance(run_manifest, PilotRunManifest)
    assert isinstance(registration_artifact, PilotRunRegistrationArtifact)
    assert isinstance(report, PilotValidationReport)
    assert isinstance(diagnostics, PilotPacketDiagnostics)
    assert isinstance(human_review, PilotPacketHumanReview)

    try:
        snapshot.validate_repository()
        registry.validate_covers_identifiers(snapshot.all_identifiers())
        verify_exact_assembly(snapshot, values["section.md"], assembly)
    except Exception as exc:
        raise PilotPacketError(
            "pilot packet canonical state or assembly is invalid"
        ) from exc
    if (
        manifest.repository_hash != snapshot.canonical_hash()
        or manifest.repository_hash != assembly.repository_hash
        or manifest.registry_hash != hash_json(registry.model_dump(mode="json"))
        or manifest.section_body_hash != hash_bytes(values["section.md"])
        or manifest.assembly_artifact_hash != hash_bytes(values["assembly.json"])
        or manifest.accepted_receipts_hash
        != hash_bytes(values["applied_tasks.json"])
        or manifest.run_manifest_hash != run_manifest.manifest_hash
        or manifest.run_manifest_content_hash
        != hash_bytes(values["run_manifest.json"])
        or manifest.validation_report_hash
        != hash_bytes(values["validation_report.json"])
        or manifest.validation_diagnostics_hash
        != hash_bytes(values["validation_diagnostics.json"])
    ):
        raise PilotPacketError("pilot packet top-level bindings disagree")
    _verify_embedded_corpus(
        values,
        packet_manifest=manifest,
        run_manifest=run_manifest,
        snapshot=snapshot,
    )

    try:
        raw_receipts = json.loads(values["applied_tasks.json"])
        if not isinstance(raw_receipts, list):
            raise TypeError("accepted receipts must be a JSON array")
        receipts = tuple(
            AppliedTaskReceipt.model_validate(item) for item in raw_receipts
        )
    except Exception as exc:
        raise PilotPacketError("pilot packet accepted receipts are invalid") from exc
    if values["applied_tasks.json"] != _json_bytes(
        [item.model_dump(mode="json") for item in receipts]
    ):
        raise PilotPacketError("pilot packet accepted receipts are not canonical JSON")
    if len(receipts) != manifest.accepted_receipt_count:
        raise PilotPacketError("pilot packet accepted receipt count differs")
    semantic_keys = tuple(item.semantic_task_key for item in receipts)
    if len(set(semantic_keys)) != len(semantic_keys):
        raise PilotPacketError("pilot packet repeats an accepted semantic receipt")
    for receipt in receipts:
        if receipt.committed_generation > manifest.source_generation:
            raise PilotPacketError("pilot packet receipt is newer than its source")
        _verify_receipt_payload(receipt, snapshot)

    if (
        hash_bytes(values["run_registration.json"])
        != manifest.registration.artifact_hash
    ):
        raise PilotPacketError("pilot packet registration content hash mismatch")
    if (
        registration_artifact.run_id != manifest.registration.run_id
        or registration_artifact.owner_generation
        != manifest.registration.owner_generation
        or registration_artifact.run_manifest_hash != run_manifest.manifest_hash
        or registration_artifact.run_manifest_content_hash
        != hash_bytes(values["run_manifest.json"])
        or registration_artifact.source_generation != run_manifest.source_generation
        or run_manifest.run_id != manifest.registration.run_id
    ):
        raise PilotPacketError("pilot packet registration and run manifest differ")
    _verify_registration_snapshot(snapshot, registration_artifact, run_manifest)

    journal_artifacts: list[PilotStageArtifact] = []
    required_domains: dict[tuple[int, str], Sha256] = {}
    previous_reference: PilotStageArtifactReference | None = None
    previous_artifact: PilotStageArtifact | None = None
    receipt_by_key = {item.semantic_task_key: item for item in receipts}
    journal_receipt_keys: set[Sha256] = set()
    for entry in manifest.journal:
        artifact = _parse_canonical_model(
            values, entry.packet_relative_path, PilotStageArtifact
        )
        assert isinstance(artifact, PilotStageArtifact)
        reference = entry.reference
        if (
            hash_bytes(values[entry.packet_relative_path]) != reference.artifact_hash
            or artifact.stage_record_hash != reference.stage_record_hash
            or artifact.stage_record.ordinal != reference.ordinal
            or artifact.stage_record.stage is not reference.stage
            or artifact.stage_record.committed_generation
            != reference.owner_generation
            or artifact.registration != manifest.registration
            or artifact.run_manifest != run_manifest
            or artifact.run_manifest_hash != manifest.run_manifest_hash
        ):
            raise PilotPacketError("pilot packet journal reference is inconsistent")
        if previous_reference is None:
            if (
                reference.ordinal != 1
                or artifact.stage_record.previous_stage is not None
                or artifact.stage_record.source_generation
                != manifest.registration.owner_generation
            ):
                raise PilotPacketError(
                    "pilot packet journal does not reach registration"
                )
        else:
            if artifact.stage_record.previous_stage != previous_reference.predecessor():
                raise PilotPacketError("pilot packet journal predecessor changed")
            assert previous_artifact is not None
            for name, prior_value in (
                previous_artifact.stage_record.budget_consumed.items()
            ):
                if artifact.stage_record.budget_consumed.get(name, 0) < prior_value:
                    raise PilotPacketError("pilot packet journal budget regressed")
        for path, expected_hash in artifact.stage_record.artifact_hashes.items():
            key = (reference.owner_generation, path)
            if key in required_domains and required_domains[key] != expected_hash:
                raise PilotPacketError("pilot journal has conflicting domain hashes")
            required_domains[key] = expected_hash
        if artifact.accepted_receipt is not None:
            if artifact.task_provenance is None:
                raise PilotPacketError("pilot journal receipt lacks provenance")
            _verify_receipt_payload(artifact.accepted_receipt, snapshot)
            _verify_receipt_provenance(
                artifact.accepted_receipt,
                artifact.task_provenance,
                safe_engine_configuration_hash=artifact.engine_plan_hash,
            )
            if artifact.accepted_receipt.semantic_task_key in journal_receipt_keys:
                raise PilotPacketError("pilot journal repeats an accepted receipt")
            journal_receipt_keys.add(artifact.accepted_receipt.semantic_task_key)
            if (
                receipt_by_key.get(artifact.accepted_receipt.semantic_task_key)
                != artifact.accepted_receipt
            ):
                raise PilotPacketError(
                    "pilot journal accepted receipt is absent from packet receipts"
                )
        journal_artifacts.append(artifact)
        previous_reference = reference
        previous_artifact = artifact
    if previous_reference != manifest.journal_head:
        raise PilotPacketError("pilot packet journal head differs")
    if journal_receipt_keys != set(receipt_by_key):
        raise PilotPacketError(
            "pilot packet receipts are not exactly covered by its journal"
        )

    domain_index = {
        (item.owner_generation, item.source_relative_path): item
        for item in manifest.domain_artifacts
    }
    if set(domain_index) != set(required_domains):
        raise PilotPacketError("pilot packet domain artifact coverage is incomplete")
    for key, expected_hash in required_domains.items():
        item = domain_index[key]
        content = values[item.packet_relative_path]
        if (
            item.content_hash != expected_hash
            or item.content_hash != hash_bytes(content)
            or item.size_bytes != len(content)
        ):
            raise PilotPacketError("pilot packet domain artifact differs from journal")

    prior_budget: dict[str, int] = {}
    for artifact in journal_artifacts:
        record = artifact.stage_record
        receipt = artifact.accepted_receipt
        if receipt is not None:
            usage_paths = tuple(
                path
                for path in record.artifact_hashes
                if "/attempt-usage/" in path
            )
            if len(usage_paths) != 1:
                raise PilotPacketError(
                    "pilot semantic event lacks exact attempt accounting"
                )
            usage_path = usage_paths[0]
            domain = domain_index[(record.committed_generation, usage_path)]
            content = values[domain.packet_relative_path]
            try:
                usage = PilotTaskUsageArtifact.model_validate_json(content)
            except Exception as exc:
                raise PilotPacketError(
                    "pilot attempt accounting is invalid"
                ) from exc
            if (
                canonical_pilot_task_usage_bytes(usage) != content
                or usage.artifact_hash != domain.content_hash
                or usage.task_id
                != artifact.task_provenance.task_id  # type: ignore[union-attr]
                or usage.accepted_attempt_id != receipt.accepted_attempt_id
                or usage.budget_content_hash
                != hash_json(run_manifest.budget.model_dump(mode="json"))
                or any(
                    item.engine != receipt.engine
                    or item.engine_version != receipt.engine_version
                    for item in usage.attempts
                )
            ):
                raise PilotPacketError(
                    "pilot attempt accounting differs from its receipt or run"
                )
            if pilot_task_usage_budget_overruns(usage, run_manifest.budget):
                raise PilotPacketError(
                    "pilot accepted attempt accounting exceeds its run budget"
                )
            accepted = tuple(
                item for item in usage.attempts if item.accepted_attempt
            )
            if (
                len(accepted) != 1
                or accepted[0].proposal_semantic_hash != receipt.proposal_hash
            ):
                raise PilotPacketError(
                    "pilot accepted attempt differs from its proposal receipt"
                )
            deltas = {
                "semantic_engine_invocations": usage.totals.attempt_count,
                "proposal_bytes": usage.totals.output_bytes,
                "stdout_bytes": usage.totals.stdout_bytes,
                "stderr_bytes": usage.totals.stderr_bytes,
                "retained_diagnostic_bytes": (
                    usage.totals.retained_diagnostic_bytes
                ),
                "writable_entries": usage.totals.writable_entry_count,
                "writable_tree_bytes": usage.totals.writable_tree_bytes,
            }
            for name, delta in deltas.items():
                if record.budget_consumed.get(name, 0) != (
                    prior_budget.get(name, 0) + delta
                ):
                    raise PilotPacketError(
                        "pilot attempt accounting differs from journal budget"
                    )
        prior_budget = dict(record.budget_consumed)

    control_artifacts: list[PilotControlArtifact] = []
    exact_control: PilotExactAssemblyControlPayload | None = None
    validation_control: PilotValidationControlPayload | None = None
    try:
        for artifact in journal_artifacts:
            if artifact.task_provenance is not None:
                continue
            paths = tuple(artifact.stage_record.artifact_hashes)
            if len(paths) != 1:
                raise ValueError("taskless stage does not name one control")
            key = (artifact.stage_record.committed_generation, paths[0])
            domain = domain_index[key]
            content = values[domain.packet_relative_path]
            control = PilotControlArtifact.model_validate_json(content)
            if content != _model_bytes(control):
                raise ValueError("control artifact is not canonical JSON")
            parsed = validate_pilot_control_artifact_binding(
                artifact,
                control,
                snapshot=snapshot,
            )
            control_artifacts.append(control)
            if isinstance(parsed, PilotExactAssemblyControlPayload):
                exact_control = parsed
            elif isinstance(parsed, PilotValidationControlPayload):
                validation_control = parsed
        sequence = validate_fixed_pilot_artifact_sequence(
            tuple(journal_artifacts),
            control_artifacts=tuple(control_artifacts),
            snapshot=snapshot,
        )
    except Exception as exc:
        raise PilotPacketError(
            "pilot packet fixed journal sequence is invalid"
        ) from exc
    if not sequence.closure_complete or sequence.head != manifest.journal_head:
        raise PilotPacketError("pilot packet journal is not a complete fixed run")
    if exact_control is None or validation_control is None:
        raise PilotPacketError("pilot packet lacks its typed closure controls")
    if (
        exact_control.assembly != assembly
        or exact_control.section_body_utf8.encode("utf-8") != values["section.md"]
        or validation_control.report != report
        or validation_control.diagnostics.model_dump(mode="json")
        != diagnostics.model_dump(mode="json")
    ):
        raise PilotPacketError(
            "pilot packet closure controls differ from its exported artifacts"
        )

    report_statuses = {
        name: getattr(report, name) for name in _VALIDATION_CHECKS
    }
    for name, status_value in report_statuses.items():
        messages = diagnostics.checks[name]
        if (status_value is ValidationStatus.PASSED) != (not messages):
            raise PilotPacketError("pilot validation status and diagnostics disagree")
    _verify_validation_acceptance_gate(report, snapshot)
    if (
        report.run_id != run_manifest.run_id
        or report.source_generation != assembly.source_generation
        or report.repository_hash != manifest.repository_hash
        or report.structural_validation is not ValidationStatus.PASSED
        or report.exact_assembly is not ValidationStatus.PASSED
        or report.artifact_integrity is not ValidationStatus.PASSED
        or report.human_review_status is not HumanReviewStatus.NOT_PERFORMED
        or report.publication_eligible is not False
        or human_review.run_id != report.run_id
        or human_review.source_generation != report.source_generation
        or human_review.validation_report_hash != manifest.validation_report_hash
        or human_review.status is not HumanReviewStatus.NOT_PERFORMED
        or human_review.publication_eligible is not False
        or manifest.publication_eligible is not False
    ):
        raise PilotPacketError("pilot validation/nonpublication bindings disagree")
    return manifest


def _bounded_inventory(directory_descriptor: int) -> tuple[frozenset[str], bool]:
    names: list[str] = []
    with os.scandir(directory_descriptor) as entries:
        for entry in entries:
            names.append(entry.name)
            if len(names) > MAX_PILOT_PACKET_FILES + 1:
                return frozenset(names), True
    return frozenset(names), False


def _require_inventory(
    directory_descriptor: int, expected: set[str] | None = None
) -> frozenset[str]:
    names, overflow = _bounded_inventory(directory_descriptor)
    if overflow:
        raise PilotPacketError("pilot packet contains too many directory entries")
    if any(not _safe_packet_name(name) for name in names):
        raise PilotPacketError("pilot packet contains an unsafe filename")
    if expected is not None and names != frozenset(expected):
        raise PilotPacketError("pilot packet file set changed")
    return names


def _load_open_packet_at(
    parent_descriptor: int,
    packet_name: str,
    packet_descriptor: int,
    packet_identity: os.stat_result,
) -> PreparedSyntheticPilotPacket:
    _verify_directory_entry_at(
        parent_descriptor,
        packet_name,
        packet_identity,
        description="pilot packet directory",
    )
    names = _require_inventory(packet_descriptor)
    if PILOT_PACKET_MANIFEST_FILE not in names:
        raise PilotPacketError("pilot packet manifest is missing")
    retained = []
    try:
        retained_bytes = 0
        for name in sorted(names):
            item = _read_packet_file(packet_descriptor, name)
            retained.append(item)
            retained_bytes += len(item.content)
            if retained_bytes > MAX_PILOT_PACKET_TOTAL_BYTES:
                raise PilotPacketError("pilot packet exceeds its byte budget")
        values = {item.name: item.content for item in retained}
        _require_inventory(packet_descriptor, set(names))
        manifest = _verify_packet_values(values)
        expected = {PILOT_PACKET_MANIFEST_FILE}
        expected.update(item.relative_path for item in manifest.files)
        _require_inventory(packet_descriptor, expected)
        for item in retained:
            _revalidate_retained_packet_file(packet_descriptor, item)
        _require_inventory(packet_descriptor, expected)
        _verify_directory_entry_at(
            parent_descriptor,
            packet_name,
            packet_identity,
            description="pilot packet directory",
        )
        return PreparedSyntheticPilotPacket(
            manifest=manifest,
            files=MappingProxyType(dict(sorted(values.items()))),
        )
    finally:
        _close_retained_packet_files(tuple(retained))


def _verify_open_packet_at(
    parent_descriptor: int,
    packet_name: str,
    packet_descriptor: int,
    packet_identity: os.stat_result,
) -> SyntheticPilotPacketManifest:
    return _load_open_packet_at(
        parent_descriptor,
        packet_name,
        packet_descriptor,
        packet_identity,
    ).manifest


def load_verified_synthetic_pilot_packet(
    packet_dir: Path,
) -> PreparedSyntheticPilotPacket:
    """Return one descriptor-held, fully verified snapshot of packet bytes."""

    if packet_dir.name in {"", ".", ".."}:
        raise PilotPacketError("pilot packet directory name is invalid")
    parent_path = None
    packet_descriptor: int | None = None
    try:
        parent_path = _retain_directory_path(
            packet_dir.parent.resolve(strict=True),
            description="pilot packet parent directory",
        )
        packet_descriptor, packet_identity = _open_directory_at(
            parent_path.descriptor,
            packet_dir.name,
            description="pilot packet",
        )
        prepared = _load_open_packet_at(
            parent_path.descriptor,
            packet_dir.name,
            packet_descriptor,
            packet_identity,
        )
        parent_path.verify()
        _verify_directory_entry_at(
            parent_path.descriptor,
            packet_dir.name,
            packet_identity,
            description="pilot packet directory",
        )
        return prepared
    except PilotPacketError:
        raise
    except (OSError, PackageCArtifactError) as exc:
        raise PilotPacketError("pilot packet contains an unsafe path") from exc
    finally:
        if packet_descriptor is not None:
            os.close(packet_descriptor)
        if parent_path is not None:
            parent_path.close()


def verify_synthetic_pilot_packet(
    packet_dir: Path,
) -> SyntheticPilotPacketManifest:
    """Verify a packet using only its closed directory and embedded witnesses."""

    return load_verified_synthetic_pilot_packet(packet_dir).manifest


def _reference_from_predecessor(
    predecessor: PilotStagePredecessor,
) -> PilotStageArtifactReference:
    return PilotStageArtifactReference(
        run_id=predecessor.run_id,
        ordinal=predecessor.ordinal,
        stage=predecessor.stage,
        owner_generation=predecessor.owner_generation,
        stage_record_hash=predecessor.stage_record_hash,
        artifact_hash=predecessor.artifact_hash,
        relative_path=predecessor.relative_path,
    )


def _capture_live_packet(
    project_root: Path,
    *,
    registration: PilotRunRegistrationReference,
    journal_head: PilotStageArtifactReference,
    body: bytes,
    assembly: AssemblyRecord,
    validation: SyntheticPilotValidationResult | None,
) -> PreparedSyntheticPilotPacket:
    from vibereview.library.models import (
        CorpusSelectionManifest,
        GenerationLibraryManifest,
    )
    from vibereview.library.selection import load_corpus_lock, verify_corpus_lock

    store = GenerationStore(project_root)
    current_generation, snapshot, registry = store.load_current()
    if assembly.source_generation + 1 != current_generation:
        raise PilotPacketError(
            "pilot packet assembly is not the final validation predecessor"
        )
    try:
        verify_exact_assembly(snapshot, body, assembly)
        registration_artifact, run_manifest = load_pilot_run_registration(
            project_root, registration
        )
        head = load_pilot_stage_artifact(
            project_root,
            journal_head,
            expected_registration=registration,
            expected_run_manifest_hash=run_manifest.manifest_hash,
        )
    except Exception as exc:
        raise PilotPacketError(
            "pilot packet registration or journal head is not authentic"
        ) from exc
    if (
        journal_head.owner_generation != current_generation
        or head.stage_record.committed_generation != current_generation
    ):
        raise PilotPacketError("pilot packet journal head is not CURRENT")

    try:
        corpus_lock, live_lock_hash = load_corpus_lock(
            project_root, generation=current_generation
        )
        corpus_raw_markdown = verify_corpus_lock(
            project_root, corpus_lock, snapshot
        )
        _, _, corpus_witness_bytes = store.load_generation_auxiliary(
            corpus_lock.committed_generation,
            {
                _GENERATION_CORPUS_LOCK_FILE,
                _GENERATION_CORPUS_IMPORT_FILE,
                _GENERATION_CORPUS_SELECTION_FILE,
            },
        )
        if (
            hash_bytes(corpus_witness_bytes[_GENERATION_CORPUS_LOCK_FILE])
            != live_lock_hash
        ):
            raise ValueError("corpus lock changed while being captured")
        corpus_import_manifest = GenerationLibraryManifest.model_validate_json(
            corpus_witness_bytes[_GENERATION_CORPUS_IMPORT_FILE]
        )
        corpus_selection_manifest = CorpusSelectionManifest.model_validate_json(
            corpus_witness_bytes[_GENERATION_CORPUS_SELECTION_FILE]
        )
        if (
            corpus_witness_bytes[_GENERATION_CORPUS_LOCK_FILE]
            != _model_bytes(corpus_lock)
            or corpus_witness_bytes[_GENERATION_CORPUS_IMPORT_FILE]
            != _model_bytes(corpus_import_manifest)
            or corpus_witness_bytes[_GENERATION_CORPUS_SELECTION_FILE]
            != canonical_json_bytes(
                corpus_selection_manifest.model_dump(mode="json")
            )
        ):
            raise ValueError("corpus witnesses are not canonical")
    except Exception as exc:
        raise PilotPacketError(
            "pilot packet corpus witnesses could not be authenticated"
        ) from exc

    captures_reversed: list[PilotJournalCapture] = []
    current_reference = journal_head
    while True:
        try:
            artifact = load_pilot_stage_artifact(
                project_root,
                current_reference,
                expected_registration=registration,
                expected_run_manifest_hash=run_manifest.manifest_hash,
            )
        except Exception as exc:
            raise PilotPacketError(
                "pilot packet journal chain could not be loaded"
            ) from exc
        captures_reversed.append(PilotJournalCapture(current_reference, artifact))
        predecessor = artifact.stage_record.previous_stage
        if predecessor is None:
            break
        if len(captures_reversed) >= MAX_STAGE_CHAIN_EVENTS:
            raise PilotPacketError("pilot packet journal exceeds its event bound")
        current_reference = _reference_from_predecessor(predecessor)
    journal = tuple(reversed(captures_reversed))

    requested_by_generation: dict[int, set[str]] = {}
    for capture in journal:
        requested_by_generation.setdefault(
            capture.reference.owner_generation, set()
        ).update(capture.artifact.stage_record.artifact_hashes)
    domains: list[PilotDomainCapture] = []
    for generation, paths in sorted(requested_by_generation.items()):
        try:
            _, _, selected = store.load_generation_auxiliary(generation, paths)
        except Exception as exc:
            raise PilotPacketError(
                "pilot packet domain artifacts could not be loaded"
            ) from exc
        for path in sorted(paths):
            content = selected[path]
            expected_hash = next(
                capture.artifact.stage_record.artifact_hashes[path]
                for capture in journal
                if capture.reference.owner_generation == generation
                and path in capture.artifact.stage_record.artifact_hashes
            )
            if hash_bytes(content) != expected_hash:
                raise PilotPacketError("pilot packet domain artifact hash mismatch")
            domains.append(PilotDomainCapture(generation, path, content))

    receipts = store.load_receipts(current_generation)
    try:
        final_control = load_pilot_control_artifact(
            project_root,
            head,
        )
        parsed_final = validate_pilot_control_artifact_binding(
            head,
            final_control,
            snapshot=snapshot,
        )
        if not isinstance(parsed_final, PilotValidationControlPayload):
            raise TypeError("journal head is not a validation control")
        exact_validation = SyntheticPilotValidationResult(
            report=parsed_final.report,
            diagnostics=MappingProxyType(dict(parsed_final.diagnostics.checks)),
        )
    except Exception as exc:
        raise PilotPacketError(
            "pilot packet final validation control is not authentic"
        ) from exc
    if validation is not None and (
        validation.report != exact_validation.report
        or dict(validation.diagnostics) != dict(exact_validation.diagnostics)
    ):
        raise PilotPacketError("supplied pilot validation differs from live validation")
    prepared = build_synthetic_pilot_packet(
        snapshot=snapshot,
        registry=registry,
        receipts=receipts,
        body=body,
        assembly=assembly,
        run_manifest=run_manifest,
        registration_artifact=registration_artifact,
        registration=registration,
        journal=journal,
        domain_artifacts=tuple(domains),
        corpus_lock=corpus_lock,
        corpus_import_manifest=corpus_import_manifest,
        corpus_selection_manifest=corpus_selection_manifest,
        corpus_raw_markdown=corpus_raw_markdown,
        validation=exact_validation,
    )
    # Authenticate the live anchors again after every byte has been captured.
    if store.current_generation() != current_generation:
        raise PilotPacketError("CURRENT changed while building the pilot packet")
    if load_pilot_run_registration(
        project_root, registration, expected_manifest=run_manifest
    )[0] != registration_artifact:
        raise PilotPacketError("pilot registration changed while building packet")
    if load_pilot_stage_artifact(
        project_root,
        journal_head,
        expected_registration=registration,
        expected_run_manifest_hash=run_manifest.manifest_hash,
    ) != head:
        raise PilotPacketError("pilot journal head changed while building packet")
    return prepared


def _remove_owned_packet_at(
    parent_descriptor: int,
    name: str,
    identity: os.stat_result,
    expected_names: set[str],
) -> None:
    descriptor: int | None = None
    try:
        descriptor, observed = _open_directory_at(
            parent_descriptor, name, description="owned pilot packet"
        )
        if not _same_directory(identity, observed):
            return
        names = _require_inventory(descriptor)
        if not names <= expected_names:
            return
        for child in names:
            info = os.stat(child, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return
        for child in names:
            os.unlink(child, dir_fd=descriptor)
        _verify_directory_entry_at(
            parent_descriptor, name, identity, description="owned pilot packet"
        )
        os.rmdir(name, dir_fd=parent_descriptor)
    except (OSError, ValueError, PackageCArtifactError):
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)


def write_synthetic_pilot_packet(
    review_root: Path,
    output_dir: Path,
    *,
    public_repository_root: Path,
    registration: PilotRunRegistrationReference,
    journal_head: PilotStageArtifactReference,
    body: bytes,
    assembly: AssemblyRecord,
    validation: SyntheticPilotValidationResult | None = None,
) -> SyntheticPilotPacketManifest:
    """Authenticate CURRENT, then atomically create or exactly reuse a packet."""

    if output_dir.name in {"", ".", ".."}:
        raise PilotPacketError("pilot packet directory name is invalid")
    parent = output_dir.parent.resolve(strict=True)
    review = review_root.resolve(strict=True)
    public = public_repository_root.resolve(strict=True)
    destination = parent / output_dir.name

    def lexical_overlap(left: Path, right: Path) -> bool:
        return left == right or left in right.parents or right in left.parents

    if lexical_overlap(review, public):
        raise PilotPacketError("pilot review root overlaps the public repository")
    if lexical_overlap(destination, review) or lexical_overlap(destination, public):
        raise PilotPacketError("pilot packet destination overlaps a protected root")

    parent_path = review_path = public_path = None
    staging_descriptor: int | None = None
    existing_descriptor: int | None = None
    staging_identity: os.stat_result | None = None
    staging_name = f".package-c-pilot-staging-{uuid.uuid4().hex}"
    staging_live = False
    published_owned = False
    accepted = False
    expected_names: set[str] = set()
    prepared: PreparedSyntheticPilotPacket | None = None

    def roots():
        if parent_path is None or review_path is None or public_path is None:
            raise AssertionError("pilot packet roots are not retained")
        return parent_path, review_path, public_path

    def verify_roots(candidate: os.stat_result | None = None) -> None:
        retained_parent, retained_review, retained_public = roots()
        retained_parent.verify()
        retained_review.verify()
        retained_public.verify()
        if _retained_paths_overlap(retained_review, retained_public):
            raise PilotPacketError("pilot review root overlaps public repository")
        protected = (retained_review, retained_public)
        if any(
            (item.identity.st_dev, item.identity.st_ino)
            in retained_parent.identity_keys
            for item in protected
        ):
            raise PilotPacketError("pilot packet destination overlaps a protected root")
        if candidate is not None:
            key = (candidate.st_dev, candidate.st_ino)
            if any(
                key == (item.identity.st_dev, item.identity.st_ino)
                or key in item.identity_keys
                for item in protected
            ):
                raise PilotPacketError(
                    "pilot packet directory is inside a protected root"
                )

    def verify_current() -> None:
        if prepared is None:
            raise AssertionError("pilot packet has not been prepared")
        verify_roots()
        store = GenerationStore(_descriptor_directory_path(review_path.descriptor))
        if store.current_generation() != prepared.manifest.source_generation:
            raise PilotPacketError("CURRENT changed while writing pilot packet")
        verify_roots()

    def verify_live_anchors() -> None:
        """Rebind every live trust anchor before accepting the packet."""

        if prepared is None or review_path is None:
            raise AssertionError("pilot packet has not been prepared")
        verify_current()
        retained_review = _descriptor_directory_path(review_path.descriptor)
        store = GenerationStore(retained_review)
        try:
            generation, live_snapshot, live_registry = store.load_current()
            live_receipts = store.load_receipts(generation)
            packet_snapshot = RepositorySnapshot.model_validate_json(
                prepared.files["repository.json"]
            )
            packet_registry = CanonicalIdRegistry.model_validate_json(
                prepared.files["registry.json"]
            )
            packet_receipts = tuple(
                AppliedTaskReceipt.model_validate(item)
                for item in json.loads(prepared.files["applied_tasks.json"])
            )
            packet_manifest = PilotRunManifest.model_validate_json(
                prepared.files["run_manifest.json"]
            )
            packet_registration = PilotRunRegistrationArtifact.model_validate_json(
                prepared.files["run_registration.json"]
            )
            packet_head = PilotStageArtifact.model_validate_json(
                prepared.files[_stage_packet_name(journal_head)]
            )
            live_registration, live_manifest = load_pilot_run_registration(
                retained_review,
                registration,
                expected_manifest=packet_manifest,
            )
            live_head = load_pilot_stage_artifact(
                retained_review,
                journal_head,
                expected_registration=registration,
                expected_run_manifest_hash=packet_manifest.manifest_hash,
            )
        except Exception as exc:
            raise PilotPacketError("live pilot packet anchors changed") from exc
        if (
            generation != prepared.manifest.source_generation
            or live_snapshot != packet_snapshot
            or live_registry != packet_registry
            or live_receipts != packet_receipts
            or live_registration != packet_registration
            or live_manifest != packet_manifest
            or live_head != packet_head
        ):
            raise PilotPacketError("live pilot packet anchors changed")
        verify_current()

    try:
        parent_path = _retain_directory_path(
            parent, description="pilot packet parent directory"
        )
        review_path = _retain_directory_path(review, description="pilot review root")
        public_path = _retain_directory_path(
            public, description="public repository root"
        )
        verify_roots()
        retained_review = _descriptor_directory_path(review_path.descriptor)
        prepared = _capture_live_packet(
            retained_review,
            registration=registration,
            journal_head=journal_head,
            body=body,
            assembly=assembly,
            validation=validation,
        )
        expected_names = set(prepared.files)
        verify_current()
        parent_descriptor = parent_path.descriptor

        try:
            destination_info = os.stat(
                output_dir.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            destination_info = None
        if destination_info is not None:
            if not stat.S_ISDIR(destination_info.st_mode):
                raise FileExistsError(output_dir.name)
            existing_descriptor, existing_identity = _open_directory_at(
                parent_descriptor, output_dir.name, description="pilot packet"
            )
            if not _same_directory(destination_info, existing_identity):
                raise PilotPacketError("pilot packet directory changed before read")
            verify_roots(existing_identity)
            observed = _verify_open_packet_at(
                parent_descriptor,
                output_dir.name,
                existing_descriptor,
                existing_identity,
            )
            if observed != prepared.manifest:
                raise FileExistsError(output_dir.name)
            os.fsync(parent_descriptor)
            verify_live_anchors()
            verify_roots(existing_identity)
            _verify_directory_entry_at(
                parent_descriptor,
                output_dir.name,
                existing_identity,
                description="pilot packet directory",
            )
            return prepared.manifest

        os.mkdir(staging_name, mode=0o700, dir_fd=parent_descriptor)
        staging_live = True
        staging_identity = os.stat(
            staging_name, dir_fd=parent_descriptor, follow_symlinks=False
        )
        if not stat.S_ISDIR(staging_identity.st_mode):
            raise PilotPacketError("pilot packet staging path is unsafe")
        os.chmod(
            staging_name, 0o700, dir_fd=parent_descriptor, follow_symlinks=False
        )
        staging_descriptor, opened_identity = _open_directory_at(
            parent_descriptor,
            staging_name,
            description="pilot packet staging directory",
        )
        if not _same_directory(staging_identity, opened_identity):
            raise PilotPacketError("pilot packet staging directory changed")

        def verify_staging() -> None:
            if staging_descriptor is None or staging_identity is None:
                raise AssertionError("pilot staging directory is unavailable")
            verify_current()
            if not _same_directory(staging_identity, os.fstat(staging_descriptor)):
                raise PilotPacketError("pilot packet staging directory changed")
            _verify_directory_entry_at(
                parent_descriptor,
                staging_name,
                staging_identity,
                description="pilot packet staging directory",
            )

        for name, content in prepared.files.items():
            _write_packet_file_at(
                staging_descriptor, name, content, before_write=verify_staging
            )
            verify_staging()
        os.fsync(staging_descriptor)
        _require_inventory(staging_descriptor, expected_names)
        verify_staging()
        verify_live_anchors()
        try:
            _rename_directory_noreplace_at(
                parent_descriptor, staging_name, output_dir.name
            )
        except FileExistsError as collision:
            try:
                existing_descriptor, existing_identity = _open_directory_at(
                    parent_descriptor, output_dir.name, description="pilot packet"
                )
                observed = _verify_open_packet_at(
                    parent_descriptor,
                    output_dir.name,
                    existing_descriptor,
                    existing_identity,
                )
            except (OSError, ValueError, PackageCArtifactError) as exc:
                raise collision from exc
            if observed != prepared.manifest:
                raise collision
            os.fsync(parent_descriptor)
            verify_live_anchors()
            verify_roots(existing_identity)
            _verify_directory_entry_at(
                parent_descriptor,
                output_dir.name,
                existing_identity,
                description="pilot packet directory",
            )
            return prepared.manifest
        staging_live = False
        published_owned = True
        _verify_directory_entry_at(
            parent_descriptor,
            output_dir.name,
            staging_identity,
            description="published pilot packet directory",
        )
        verify_roots(staging_identity)
        verify_live_anchors()
        os.fsync(parent_descriptor)
        observed = _verify_open_packet_at(
            parent_descriptor,
            output_dir.name,
            staging_descriptor,
            staging_identity,
        )
        if observed != prepared.manifest:
            raise PilotPacketError("published pilot packet changed")
        verify_live_anchors()
        verify_roots(staging_identity)
        _verify_directory_entry_at(
            parent_descriptor,
            output_dir.name,
            staging_identity,
            description="published pilot packet directory",
        )
        accepted = True
        return prepared.manifest
    except PilotPacketError:
        raise
    except PackageCArtifactError as exc:
        raise PilotPacketError(str(exc)) from exc
    finally:
        if (
            staging_live
            and staging_identity is not None
            and parent_path is not None
        ):
            _remove_owned_packet_at(
                parent_path.descriptor,
                staging_name,
                staging_identity,
                expected_names,
            )
        if (
            published_owned
            and not accepted
            and staging_identity is not None
            and parent_path is not None
        ):
            _remove_owned_packet_at(
                parent_path.descriptor,
                output_dir.name,
                staging_identity,
                expected_names,
            )
            try:
                os.fsync(parent_path.descriptor)
            except OSError:
                pass
        if existing_descriptor is not None:
            os.close(existing_descriptor)
        if staging_descriptor is not None:
            os.close(staging_descriptor)
        if public_path is not None:
            public_path.close()
        if review_path is not None:
            review_path.close()
        if parent_path is not None:
            parent_path.close()


# Explicitly named aliases make the review-packet purpose discoverable while
# retaining the shorter API used by the rest of the runtime artifact family.
build_synthetic_pilot_review_packet = build_synthetic_pilot_packet
verify_synthetic_pilot_review_packet = verify_synthetic_pilot_packet
write_synthetic_pilot_review_packet = write_synthetic_pilot_packet


__all__ = [
    "MAX_PILOT_PACKET_FILES",
    "MAX_PILOT_PACKET_RECEIPTS",
    "MAX_PILOT_PACKET_TOTAL_BYTES",
    "MAX_PILOT_RAW_MARKDOWN_BYTES",
    "PILOT_PACKET_MANIFEST_FILE",
    "PILOT_PACKET_VERSION",
    "PilotDomainCapture",
    "PilotJournalCapture",
    "PilotPacketDiagnostics",
    "PilotPacketCorpusSource",
    "PilotPacketDomainArtifact",
    "PilotPacketError",
    "PilotPacketFileRecord",
    "PilotPacketHumanReview",
    "PilotPacketJournalEntry",
    "PreparedSyntheticPilotPacket",
    "SyntheticPilotPacketManifest",
    "build_synthetic_pilot_packet",
    "build_synthetic_pilot_review_packet",
    "load_verified_synthetic_pilot_packet",
    "verify_synthetic_pilot_packet",
    "verify_synthetic_pilot_review_packet",
    "write_synthetic_pilot_packet",
    "write_synthetic_pilot_review_packet",
]
