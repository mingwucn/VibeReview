"""Descriptive, offline comparison of synthetic pilot review packets.

Comparability is a strict prerequisite, not a score.  Once the immutable run
inputs match, canonical identifiers are replaced with semantic identities and
the comparator reports agreements and differences without selecting a winner
or defining an acceptance threshold.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import ConfigDict, Field, model_validator

from .artifacts import AssemblyRecord
from .hashing import hash_bytes, hash_json
from .pilot_packet import (
    SyntheticPilotPacketManifest,
    load_verified_synthetic_pilot_packet,
)
from .pilot_records import PilotRunManifest, PilotValidationReport
from .records import AppliedTaskReceipt, RuntimeModel
from .state import RepositorySnapshot


PILOT_REPRODUCTION_VERSION = "1"
MIN_REPRODUCTION_PACKETS = 2
MAX_REPRODUCTION_PACKETS = 4
MAX_REPRODUCTION_ITEMS_PER_COLLECTION = 5_000
MAX_REPRODUCTION_TOTAL_ITEMS = 25_000

_VALIDATION_FIELDS = (
    "structural_validation",
    "locator_verification",
    "semantic_audits_executed",
    "citation_authorization",
    "exact_assembly",
    "artifact_integrity",
)
_OWNER_ID_FIELDS = (
    ("themes", "theme_id"),
    ("papers", "paper_id"),
    ("candidate_claims", "claim_id"),
    ("retrieval_queries", "query_id"),
    ("retrieved_spans", "span_id"),
    ("evidence_records", "evidence_id"),
    ("claim_paper_evidence", "claim_paper_evidence_id"),
    ("corpus_facts", "corpus_fact_id"),
    ("process_facts", "process_fact_id"),
    ("proposition_records", "proposition_id"),
    ("semantic_audits", "audit_id"),
    ("rendered_sentences", "sentence_id"),
    ("rendered_sentence_audits", "audit_id"),
)
_TEXT_KEYS = frozenset(
    {
        "assessment_note",
        "candidate_claim",
        "citation",
        "contradiction_summary",
        "description",
        "evidence_summary",
        "final_claim",
        "meaning",
        "notes",
        "qualification_summary",
        "rationale",
        "reason",
        "source_text",
        "summary",
        "support_summary",
        "term",
        "text",
        "title",
    }
)
_PILOT_SOURCE_KEY = re.compile(r"pilot-run:[0-9a-f]{64}:")
_SET_LIKE_LIST_FIELDS = frozenset(
    {
        "boundary_conditions",
        "citation_bindings",
        "claim_ids",
        "claim_paper_evidence_ids",
        "contradicts",
        "contextual",
        "corpus_fact_ids",
        "evidence_ids",
        "identity_keys",
        "limitations",
        "origin_refs",
        "paper_relations",
        "parent_span_ids",
        "process_fact_ids",
        "qualifies",
        "referenced_claim_ids",
        "referenced_corpus_fact_ids",
        "referenced_process_fact_ids",
        "related_publications",
        "source_claim_packet_ids",
        "source_paper_ids",
        "source_proposition_ids",
        "supports",
        "unclear",
    }
)
_REFERENCE_FIELDS = frozenset(
    {
        "audit_id",
        "canonical_span_id",
        "citation_bindings",
        "claim_id",
        "claim_ids",
        "claim_paper_evidence_id",
        "claim_paper_evidence_ids",
        "contradicts",
        "contextual",
        "corpus_fact_id",
        "corpus_fact_ids",
        "evidence_id",
        "evidence_ids",
        "origin_refs",
        "paper_id",
        "parent_span_ids",
        "parent_theme_id",
        "process_fact_id",
        "process_fact_ids",
        "proposition_id",
        "qualifies",
        "query_id",
        "referenced_claim_ids",
        "referenced_corpus_fact_ids",
        "referenced_process_fact_ids",
        "related_publications",
        "retrieved_span_id",
        "sentence_id",
        "source_claim_packet_ids",
        "source_paper_ids",
        "source_proposition_ids",
        "span_id",
        "supports",
        "target_id",
        "theme_id",
        "unclear",
    }
)
_FORWARD_IDENTITY_FIELDS: dict[str, frozenset[str]] = {
    "themes": frozenset({"parent_theme_id"}),
    "papers": frozenset({"related_publications"}),
    # Reserved for snapshots that add explicit retrieved-span parentage.  It is
    # harmless for the frozen model, where that field is currently absent.
    "retrieved_spans": frozenset({"parent_span_ids"}),
}


class PilotReproductionError(ValueError):
    """Packets cannot be compared without inventing missing equivalence."""


class PilotReproductionModel(RuntimeModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PilotSemanticItem(PilotReproductionModel):
    semantic_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    occurrences: int = Field(ge=1, le=MAX_REPRODUCTION_ITEMS_PER_COLLECTION)
    text_hashes: tuple[str, ...] = Field(max_length=256)
    locator_hashes: tuple[str, ...] = Field(max_length=256)


class PilotSemanticSetSummary(PilotReproductionModel):
    item_count: int = Field(ge=0, le=MAX_REPRODUCTION_ITEMS_PER_COLLECTION)
    distinct_item_count: int = Field(
        ge=0, le=MAX_REPRODUCTION_ITEMS_PER_COLLECTION
    )
    semantic_set_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    items: tuple[PilotSemanticItem, ...] = Field(
        max_length=MAX_REPRODUCTION_ITEMS_PER_COLLECTION
    )

    @model_validator(mode="after")
    def _counts_match_items(self) -> "PilotSemanticSetSummary":
        if self.distinct_item_count != len(self.items):
            raise ValueError("semantic distinct-item count differs")
        if self.item_count != sum(item.occurrences for item in self.items):
            raise ValueError("semantic item count differs")
        if tuple(item.semantic_hash for item in self.items) != tuple(
            sorted(item.semantic_hash for item in self.items)
        ):
            raise ValueError("semantic items must be hash ordered")
        return self


class PilotReproductionObservation(PilotReproductionModel):
    subject: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=512)
    values: dict[str, PilotSemanticSetSummary] = Field(
        min_length=MIN_REPRODUCTION_PACKETS,
        max_length=MAX_REPRODUCTION_PACKETS,
    )


class PilotReproductionPacketWitness(PilotReproductionModel):
    packet_label: str = Field(pattern=r"^packet-[0-9]{2}$")
    packet_manifest_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    run_id: str
    source_generation: int = Field(ge=1)
    repository_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    repository_semantic_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    section_text_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    validation_statuses: dict[str, str]


class PilotReproductionReport(PilotReproductionModel):
    """A bounded description, deliberately without a verdict or score."""

    schema_version: Literal["package-c-pilot-reproduction-1"] = (
        "package-c-pilot-reproduction-1"
    )
    packet_count: int = Field(
        ge=MIN_REPRODUCTION_PACKETS, le=MAX_REPRODUCTION_PACKETS
    )
    shared_run_identity_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    packets: tuple[PilotReproductionPacketWitness, ...] = Field(
        min_length=MIN_REPRODUCTION_PACKETS,
        max_length=MAX_REPRODUCTION_PACKETS,
    )
    agreements: tuple[PilotReproductionObservation, ...]
    differences: tuple[PilotReproductionObservation, ...]
    comparison_policy: Literal[
        "ID_INDEPENDENT_SEMANTIC_HASHES_LOCATORS_AND_TEXT"
    ] = "ID_INDEPENDENT_SEMANTIC_HASHES_LOCATORS_AND_TEXT"
    human_review: Literal["NOT_PERFORMED"] = "NOT_PERFORMED"
    publication_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _closed_subjects(self) -> "PilotReproductionReport":
        if self.packet_count != len(self.packets):
            raise ValueError("reproduction packet count differs")
        labels = tuple(item.packet_label for item in self.packets)
        expected_labels = tuple(
            f"packet-{number:02d}" for number in range(1, len(labels) + 1)
        )
        if labels != expected_labels:
            raise ValueError("reproduction packet labels are not canonical")
        agreement_subjects = tuple(item.subject for item in self.agreements)
        difference_subjects = tuple(item.subject for item in self.differences)
        if set(agreement_subjects) & set(difference_subjects):
            raise ValueError("reproduction subject is both agreement and difference")
        if agreement_subjects != tuple(sorted(agreement_subjects)):
            raise ValueError("reproduction agreements are not ordered")
        if difference_subjects != tuple(sorted(difference_subjects)):
            raise ValueError("reproduction differences are not ordered")
        return self


@dataclass(frozen=True, slots=True)
class _LoadedPacket:
    manifest: SyntheticPilotPacketManifest
    run_manifest: PilotRunManifest
    snapshot: RepositorySnapshot
    assembly: AssemblyRecord
    report: PilotValidationReport
    section: bytes
    receipts: tuple[AppliedTaskReceipt, ...]


@dataclass(frozen=True, slots=True)
class _SemanticPacket:
    loaded: _LoadedPacket
    collections: dict[str, PilotSemanticSetSummary]
    repository_semantic_hash: str
    section: PilotSemanticSetSummary


def _load_packet(packet_dir: Path) -> _LoadedPacket:
    try:
        prepared = load_verified_synthetic_pilot_packet(packet_dir)
        manifest = prepared.manifest
        files = prepared.files
        run_manifest = PilotRunManifest.model_validate_json(
            files["run_manifest.json"]
        )
        snapshot = RepositorySnapshot.model_validate_json(
            files["repository.json"]
        )
        assembly = AssemblyRecord.model_validate_json(
            files["assembly.json"]
        )
        report = PilotValidationReport.model_validate_json(
            files["validation_report.json"]
        )
        section = files["section.md"]
        receipt_values = json.loads(files["applied_tasks.json"])
        receipts = tuple(
            AppliedTaskReceipt.model_validate(item) for item in receipt_values
        )
    except Exception as exc:
        raise PilotReproductionError("pilot reproduction packet is invalid") from exc
    return _LoadedPacket(
        manifest=manifest,
        run_manifest=run_manifest,
        snapshot=snapshot,
        assembly=assembly,
        report=report,
        section=section,
        receipts=receipts,
    )


def _normalized_run_identity(manifest: PilotRunManifest) -> dict[str, Any]:
    """Exclude only run labels, time/generation coordinates, and canonical IDs."""

    return {
        "topic": manifest.topic,
        "corpus_lock_hash": manifest.corpus_lock_hash,
        "selection_manifest_hash": manifest.selection_manifest_hash,
        "paper_source_hashes": list(manifest.paper_source_hashes),
        "discovery_resource_hashes": list(manifest.discovery_resource_hashes),
        "engine_role_plan": {
            task_type.value: binding.model_dump(mode="json")
            for task_type, binding in sorted(
                manifest.engine_role_plan.items(), key=lambda item: item[0].value
            )
        },
        "prompt_fingerprints": {
            task_type.value: value
            for task_type, value in sorted(
                manifest.prompt_fingerprints.items(), key=lambda item: item[0].value
            )
        },
        "schema_fingerprints": {
            task_type.value: value
            for task_type, value in sorted(
                manifest.schema_fingerprints.items(), key=lambda item: item[0].value
            )
        },
        "validator_fingerprint": manifest.validator_fingerprint,
        "package_c_implementation_fingerprint": (
            manifest.package_c_implementation_fingerprint
        ),
        "budget": manifest.budget.model_dump(mode="json"),
        "synthetic_only": manifest.synthetic_only,
        "human_review_status": manifest.human_review_status.value,
        "publication_eligible": manifest.publication_eligible,
    }


def _replace_semantic_ids(
    value: Any,
    identities: dict[str, str],
    *,
    self_id: str | None = None,
    self_collection: str | None = None,
    field_name: str | None = None,
) -> Any:
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, child in sorted(value.items()):
            normalized_child = _replace_semantic_ids(
                child,
                identities,
                self_id=self_id,
                self_collection=self_collection,
                field_name=key,
            )
            if key in _SET_LIKE_LIST_FIELDS and isinstance(normalized_child, list):
                normalized_child = sorted(
                    normalized_child,
                    key=lambda item: json.dumps(
                        item,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            normalized[key] = normalized_child
        return normalized
    if isinstance(value, list):
        return [
            _replace_semantic_ids(
                item,
                identities,
                self_id=self_id,
                self_collection=self_collection,
                field_name=field_name,
            )
            for item in value
        ]
    if not isinstance(value, str):
        return value
    if field_name == "source_key":
        return _PILOT_SOURCE_KEY.sub("pilot-run:<RUN>:", value)
    if field_name == "raw_md_path":
        parts = value.split("/")
        for index, part in enumerate(parts):
            if self_id is not None and part == self_id:
                parts[index] = f"<SELF:{self_collection}>"
            elif part in identities:
                parts[index] = identities[part]
        return "/".join(parts)
    if field_name not in _REFERENCE_FIELDS:
        return value
    if self_id is not None and value == self_id:
        return f"<SELF:{self_collection}>"
    return identities.get(value, value)


def _canonical_semantic_identities(snapshot: RepositorySnapshot) -> dict[str, str]:
    identities: dict[str, str] = {}
    for collection_name, id_field in _OWNER_ID_FIELDS:
        values = tuple(getattr(snapshot, collection_name))
        for value in values:
            payload = value.model_dump(mode="json")
            identifier = payload.pop(id_field)
            # Forward references are retained in the final normalized record,
            # but omitted while computing the referent token so allocation
            # order cannot leak raw canonical IDs into that token.
            for field in _FORWARD_IDENTITY_FIELDS.get(
                collection_name, frozenset()
            ):
                payload.pop(field, None)
            normalized_payload = _replace_semantic_ids(
                payload,
                identities,
                self_id=identifier,
                self_collection=collection_name,
            )
            digest = hash_json(
                {"collection": collection_name, "content": normalized_payload}
            )
            identities[identifier] = (
                f"<SEM:{collection_name}:{digest.removeprefix('sha256:')}>"
            )
    return identities


def _collect_text_and_locator_hashes(
    value: Any,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    text_hashes: list[str] = []
    locator_hashes: list[str] = []

    def visit(current: Any, key: str | None = None) -> None:
        if isinstance(current, dict):
            for child_key, child in current.items():
                if child_key == "locator" and isinstance(child, dict):
                    locator_hashes.append(hash_json(child))
                visit(child, child_key)
        elif isinstance(current, list):
            for child in current:
                visit(child, key)
        elif isinstance(current, str) and key in _TEXT_KEYS:
            text_hashes.append(hash_bytes(current.encode("utf-8")))

    visit(value)
    return tuple(sorted(text_hashes)), tuple(sorted(locator_hashes))


def _semantic_set(
    values: list[tuple[str, tuple[str, ...], tuple[str, ...]]],
) -> PilotSemanticSetSummary:
    if len(values) > MAX_REPRODUCTION_ITEMS_PER_COLLECTION:
        raise PilotReproductionError("semantic collection exceeds comparison bound")
    counts = Counter(item[0] for item in values)
    metadata: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    for semantic_hash, text_hashes, locator_hashes in values:
        previous = metadata.get(semantic_hash)
        observed = (text_hashes, locator_hashes)
        if previous is not None and previous != observed:
            raise PilotReproductionError("semantic hash metadata collision")
        metadata[semantic_hash] = observed
    items = tuple(
        PilotSemanticItem(
            semantic_hash=semantic_hash,
            occurrences=counts[semantic_hash],
            text_hashes=metadata[semantic_hash][0],
            locator_hashes=metadata[semantic_hash][1],
        )
        for semantic_hash in sorted(counts)
    )
    return PilotSemanticSetSummary(
        item_count=len(values),
        distinct_item_count=len(items),
        semantic_set_hash=hash_json(
            [
                {
                    "semantic_hash": item.semantic_hash,
                    "occurrences": item.occurrences,
                    "text_hashes": item.text_hashes,
                    "locator_hashes": item.locator_hashes,
                }
                for item in items
            ]
        ),
        items=items,
    )


def _ordered_text_summary(body: bytes) -> PilotSemanticSetSummary:
    lines = body.decode("utf-8").splitlines(keepends=True)
    values = [
        (
            hash_bytes(line.encode("utf-8")),
            (hash_bytes(line.encode("utf-8")),),
            (),
        )
        for line in lines
    ]
    unordered = _semantic_set(values)
    return unordered.model_copy(
        update={
            "semantic_set_hash": hash_json(
                {
                    "exact_body_hash": hash_bytes(body),
                    "ordered_text_hashes": [item[0] for item in values],
                }
            )
        }
    )


def _semantic_packet(loaded: _LoadedPacket) -> _SemanticPacket:
    identities = _canonical_semantic_identities(loaded.snapshot)
    collections: dict[str, PilotSemanticSetSummary] = {}
    total = 0
    for collection_name in type(loaded.snapshot).model_fields:
        semantic_values: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
        for item in getattr(loaded.snapshot, collection_name):
            normalized = _replace_semantic_ids(
                item.model_dump(mode="json"), identities
            )
            text_hashes, locator_hashes = _collect_text_and_locator_hashes(normalized)
            semantic_values.append(
                (hash_json(normalized), text_hashes, locator_hashes)
            )
        total += len(semantic_values)
        if total > MAX_REPRODUCTION_TOTAL_ITEMS:
            raise PilotReproductionError("pilot repository exceeds comparison bound")
        collections[collection_name] = _semantic_set(semantic_values)
    section = _ordered_text_summary(loaded.section)
    repository_semantic_hash = hash_json(
        {
            name: summary.semantic_set_hash
            for name, summary in sorted(collections.items())
        }
    )
    return _SemanticPacket(
        loaded=loaded,
        collections=collections,
        repository_semantic_hash=repository_semantic_hash,
        section=section,
    )


def _status_summary(value: str) -> PilotSemanticSetSummary:
    semantic_hash = hash_json({"validation_status": value})
    return _semantic_set([(semantic_hash, (hash_bytes(value.encode("utf-8")),), ())])


def compare_synthetic_pilot_reproductions(
    packet_dirs: Sequence[Path],
) -> PilotReproductionReport:
    """Compare two to four offline packets after a strict run-identity gate."""

    if not MIN_REPRODUCTION_PACKETS <= len(packet_dirs) <= MAX_REPRODUCTION_PACKETS:
        raise PilotReproductionError("pilot reproduction requires two to four packets")
    loaded = tuple(_load_packet(Path(path)) for path in packet_dirs)
    identities = tuple(
        hash_json(_normalized_run_identity(item.run_manifest)) for item in loaded
    )
    if len(set(identities)) != 1:
        raise PilotReproductionError(
            "pilot packets do not share the exact normalized run identity"
        )
    semantic_packets = tuple(_semantic_packet(item) for item in loaded)
    labels = tuple(f"packet-{number:02d}" for number in range(1, len(loaded) + 1))

    agreements: list[PilotReproductionObservation] = []
    differences: list[PilotReproductionObservation] = []

    def observe(
        subject: str,
        description: str,
        summaries: tuple[PilotSemanticSetSummary, ...],
    ) -> None:
        observation = PilotReproductionObservation(
            subject=subject,
            description=description,
            values=dict(zip(labels, summaries, strict=True)),
        )
        hashes = {item.semantic_set_hash for item in summaries}
        (agreements if len(hashes) == 1 else differences).append(observation)

    for collection_name in type(loaded[0].snapshot).model_fields:
        observe(
            f"repository.{collection_name}",
            "ID-independent canonical objects; text and locator hashes are retained.",
            tuple(item.collections[collection_name] for item in semantic_packets),
        )
    observe(
        "assembled_section",
        "Exact rendered sentence text compared independently of sentence IDs.",
        tuple(item.section for item in semantic_packets),
    )
    for field in _VALIDATION_FIELDS:
        observe(
            f"validation.{field}",
            "Recorded validation status; no acceptance threshold is applied.",
            tuple(
                _status_summary(getattr(item.report, field).value) for item in loaded
            ),
        )

    witnesses = tuple(
        PilotReproductionPacketWitness(
            packet_label=label,
            packet_manifest_hash=item.loaded.manifest.artifact_hash,
            run_id=item.loaded.run_manifest.run_id,
            source_generation=item.loaded.manifest.source_generation,
            repository_hash=item.loaded.manifest.repository_hash,
            repository_semantic_hash=item.repository_semantic_hash,
            section_text_hash=hash_bytes(item.loaded.section),
            validation_statuses={
                field: getattr(item.loaded.report, field).value
                for field in _VALIDATION_FIELDS
            },
        )
        for label, item in zip(labels, semantic_packets, strict=True)
    )
    return PilotReproductionReport(
        packet_count=len(loaded),
        shared_run_identity_hash=identities[0],
        packets=witnesses,
        agreements=tuple(sorted(agreements, key=lambda item: item.subject)),
        differences=tuple(sorted(differences, key=lambda item: item.subject)),
    )


compare_synthetic_pilot_packets = compare_synthetic_pilot_reproductions


__all__ = [
    "MAX_REPRODUCTION_PACKETS",
    "MIN_REPRODUCTION_PACKETS",
    "PILOT_REPRODUCTION_VERSION",
    "PilotReproductionError",
    "PilotReproductionObservation",
    "PilotReproductionPacketWitness",
    "PilotReproductionReport",
    "PilotSemanticItem",
    "PilotSemanticSetSummary",
    "compare_synthetic_pilot_packets",
    "compare_synthetic_pilot_reproductions",
]
