from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from vibereview.library.models import (
    CorpusImportSource,
    CorpusLockManifest,
    CorpusLockPaper,
    CorpusSelectionDocument,
    CorpusSelectionManifest,
    GenerationLibraryManifest,
    LibrarySourceObject,
)
from vibereview.enums import (
    CorpusFactDerivationType,
    PropositionContentClass,
    ReviewProcessSourceType,
)
from vibereview.models import (
    CorpusFact,
    CorpusFactDerivation,
    ReviewProcessFact,
)
from vibereview.runtime import pilot_packet as packet_module
from vibereview.runtime.artifacts import AssemblyBudget, assemble_exact_section
from vibereview.runtime.hashing import (
    canonical_json_bytes,
    hash_bytes,
    hash_json,
    hash_text,
)
from vibereview.runtime.pilot_journal import (
    PilotExactAssemblyControlPayload,
    PilotRetrievalControlPayload,
    PilotStageMaterializer,
    PilotValidationControlPayload,
    record_pilot_control_event,
)
from vibereview.runtime.pilot_manifest import (
    compute_package_c_implementation_fingerprint,
)
from vibereview.runtime.pilot_packet import (
    PilotPacketError,
    SyntheticPilotPacketManifest,
    verify_synthetic_pilot_packet,
    write_synthetic_pilot_packet,
)
from vibereview.runtime.pilot_records import (
    PILOT_CORPUS_FACT_TEXT,
    PILOT_HUMAN_REVIEW_FACT_TEXT,
    PILOT_SCOPE_FACT_TEXT,
    FivePaperPilotBudget,
    PilotEngineRoleBinding,
    PilotRunManifest,
    PilotRunRegistrationArtifact,
    PilotRunRegistrationReference,
    PilotStage,
    PilotStageStatus,
    PilotValidationReport,
    ValidationStatus,
    compute_pilot_engine_role_plan_hash,
    compute_pilot_schema_fingerprint,
)
from vibereview.runtime.pilot_usage import (
    PilotAttemptUsage,
    PilotTaskUsageArtifact,
    PilotTaskUsageTotals,
    canonical_pilot_task_usage_bytes,
)
from vibereview.runtime.pilot_validation import validate_synthetic_pilot_run
from vibereview.runtime.receipts import (
    compute_input_identity_key,
    compute_semantic_task_key,
)
from vibereview.runtime.records import (
    AppliedTaskReceipt,
    AttemptOutcome,
    CanonicalObjectReceipt,
    ResourceProvenance,
    ResourceSourceDependency,
    TaskProvenance,
    TaskSemanticFingerprint,
    TaskType,
    TransitionDecision,
)
from vibereview.runtime.registry import IdKind
from vibereview.runtime.repository import GenerationStore, PromotionPayload
from vibereview.runtime.specs import TASK_SPECS
from vibereview.runtime.state import RepositorySnapshot


def _sha(character: str) -> str:
    return "sha256:" + character * 64


def _paper_markdown(position: int, title: str) -> bytes:
    if position == 1:
        return f"# Results\nStress declined\n\n# Paper\n{title}\n".encode("utf-8")
    return f"# Synthetic source {position}\n\n{title}\n".encode("utf-8")


def _raw_path(generation: int, content_hash: str) -> str:
    return (
        f"state/generations/{generation:06d}/auxiliary/library/objects/"
        f"sha256/{content_hash.removeprefix('sha256:')}/raw.md"
    )


def _five_paper_snapshot(bundle_factory) -> RepositorySnapshot:
    snapshot = RepositorySnapshot.model_validate(bundle_factory())
    prototype = snapshot.papers[0]
    papers = [prototype]
    base_ordinal = int(prototype.paper_id.removeprefix("P"))
    for ordinal in range(2, 6):
        paper_ordinal = base_ordinal + ordinal - 1
        papers.append(
            prototype.model_copy(
                update={
                    "paper_id": f"P{paper_ordinal:04d}",
                    "title": f"Synthetic packet paper {ordinal}",
                    "authors": [f"Fixture Author {ordinal}"],
                    "year": 2025 + ordinal,
                    "doi": None,
                    "identity_keys": [f"fixture:packet-paper-{ordinal}"],
                }
            )
        )
    exact_papers = []
    for position, paper in enumerate(papers, start=1):
        content_hash = hash_bytes(_paper_markdown(position, paper.title))
        exact_papers.append(
            paper.model_copy(
                update={
                    "raw_md_path": _raw_path(1, content_hash),
                    "source_hash": content_hash,
                    "raw_md_hash": content_hash,
                }
            )
        )
    proposition = snapshot.proposition_records[0].model_copy(
        update={
            "content_class": PropositionContentClass.RHETORICAL,
            "claim_ids": [],
            "citation_bindings": [],
            "corpus_fact_ids": [],
            "process_fact_ids": [],
        }
    )
    semantic_audit = snapshot.semantic_audits[0].model_copy(
        update={
            "referenced_claim_ids": [],
            "referenced_corpus_fact_ids": [],
            "referenced_process_fact_ids": [],
        }
    )
    result = RepositorySnapshot(
        papers=tuple(exact_papers),
        proposition_records=(proposition,),
        semantic_audits=(semantic_audit,),
        rendered_sentences=snapshot.rendered_sentences,
        rendered_sentence_audits=snapshot.rendered_sentence_audits,
    )
    result.validate_repository()
    return result


def _corpus_witnesses(snapshot: RepositorySnapshot):
    papers = tuple(sorted(snapshot.papers, key=lambda item: item.paper_id))
    source_commit = "1" * 40
    documents = tuple(
        CorpusSelectionDocument(
            source_relative_path=f"papers/source-{position}.md",
            content_sha256=paper.source_hash,
            decision="include",
            role="pilot-paper",
            accepted_by="synthetic-fixture",
            accepted_at="2030-01-01T00:00:00+00:00",
            reason="bounded synthetic Package C corpus",
        )
        for position, paper in enumerate(papers, start=1)
    )
    selection = CorpusSelectionManifest(
        library_id="synthetic-packet-library",
        source_commit=source_commit,
        documents=list(documents),
    )
    selection_bytes = canonical_json_bytes(selection.model_dump(mode="json"))
    selection_hash = hash_bytes(selection_bytes)
    source_objects = [
        LibrarySourceObject(
            role="paper",
            source_relative_path=document.source_relative_path,
            git_blob_id=(f"{position:x}" * 40)[:40],
            content_sha256=document.content_sha256,
        )
        for position, document in enumerate(documents, start=1)
    ]
    import_source = CorpusImportSource(
        library_id=selection.library_id,
        source_commit=source_commit,
        superproject_commit="2" * 40,
        gitlink_path="vendor/synthetic-library",
        markdown_root="papers",
        bibliography_path=None,
        graph_path=None,
        selection_manifest_hash=selection_hash,
        objects=source_objects,
    )
    import_source_hash = hash_json(import_source.model_dump(mode="json"))
    lock = CorpusLockManifest(
        library_id=selection.library_id,
        source_commit=source_commit,
        selection_manifest_hash=selection_hash,
        import_source=import_source,
        import_source_hash=import_source_hash,
        committed_generation=1,
        papers=[
            CorpusLockPaper(
                paper_id=paper.paper_id,
                source_relative_path=document.source_relative_path,
                git_blob_id=source_object.git_blob_id,
                source_hash=paper.source_hash,
                raw_md_path=paper.raw_md_path,
                raw_md_hash=paper.raw_md_hash,
                title=paper.title,
                doi=paper.doi,
            )
            for paper, document, source_object in zip(
                papers, documents, source_objects, strict=True
            )
        ],
    )
    lock_bytes = canonical_json_bytes(lock.model_dump(mode="json")) + b"\n"
    import_manifest = GenerationLibraryManifest(
        generation=1,
        corpus_lock_hash=hash_bytes(lock_bytes),
        selection_manifest_hash=selection_hash,
        import_source_hash=import_source_hash,
        resource_hashes={paper.raw_md_path: paper.raw_md_hash for paper in papers},
    )
    raw = {
        paper.paper_id: _paper_markdown(position, paper.title)
        for position, paper in enumerate(papers, start=1)
    }
    return lock, import_manifest, selection, raw


def _run_manifest(snapshot: RepositorySnapshot) -> PilotRunManifest:
    lock, _, selection, _ = _corpus_witnesses(snapshot)
    lock_content = canonical_json_bytes(lock.model_dump(mode="json")) + b"\n"
    selection_content = canonical_json_bytes(selection.model_dump(mode="json"))
    binding = PilotEngineRoleBinding(
        engine="synthetic-mock",
        engine_version="1",
        safe_configuration_hash=hash_text("packet engine plan"),
    )
    plan = {task_type: binding for task_type in TaskType}
    return PilotRunManifest(
        run_id="RUN-synthetic-packet",
        created_at="2030-01-01T00:00:00+00:00",
        topic="Synthetic packet topic.",
        source_generation=1,
        corpus_lock_hash=hash_bytes(lock_content),
        selection_manifest_hash=hash_bytes(selection_content),
        paper_source_hashes=tuple(
            item.source_hash
            for item in sorted(snapshot.papers, key=lambda paper: paper.paper_id)
        ),
        discovery_resource_hashes=(_sha("6"), _sha("7")),
        engine_role_plan=plan,
        engine_role_plan_hash=compute_pilot_engine_role_plan_hash(plan),
        prompt_fingerprints={task_type: _sha("c") for task_type in TaskType},
        schema_fingerprints={
            task_type: compute_pilot_schema_fingerprint(
                input_schema_hash=_sha("8"),
                proposal_schema_hash=_sha("9"),
            )
            for task_type in TaskType
        },
        validator_fingerprint=_sha("e"),
        package_c_implementation_fingerprint=(
            compute_package_c_implementation_fingerprint()
        ),
        budget=FivePaperPilotBudget(),
    )


def _task_provenance(
    manifest: PilotRunManifest,
    *,
    task_type: TaskType,
    source_generation: int,
    ordinal: int,
) -> TaskProvenance:
    spec = TASK_SPECS[task_type]
    instructions_hash = manifest.prompt_fingerprints[task_type]
    resources = (
        tuple(
            ResourceProvenance(
                resource_id=f"RES{resource_ordinal:04d}",
                logical_name=f"discovery-{resource_ordinal}.md",
                media_type="text/markdown",
                bundle_relative_path=Path(
                    "input/resources/"
                    f"RES{resource_ordinal:04d}/content.md"
                ),
                source_path=Path(
                    f"/synthetic/discovery-{resource_ordinal}.md"
                ),
                source_dependency=ResourceSourceDependency(
                    source_hash_at_snapshot=digest
                ),
                snapshot_hash=digest,
                size_bytes=1,
            )
            for resource_ordinal, digest in enumerate(
                manifest.discovery_resource_hashes,
                start=1,
            )
        )
        if task_type is TaskType.PARSE_DEEP_RESEARCH
        else ()
    )
    return TaskProvenance(
        task_id=f"TASK{ordinal:04d}",
        task_type=task_type,
        task_spec_version=spec.version,
        prompt_version=spec.prompt_version,
        base_generation=source_generation,
        dependencies={},
        instructions_hash=instructions_hash,
        input_snapshot_hash=hash_text(f"packet input:{ordinal}"),
        engine_input_hash=hash_text(f"packet engine input:{ordinal}"),
        input_schema_hash=_sha("8"),
        proposal_schema_hash=_sha("9"),
        expected_bundle_manifest_hash=hash_text(f"packet bundle:{ordinal}"),
        expected_immutable_files={"instructions.md": instructions_hash},
        resources=resources,
    )


def _task_proposal(
    snapshot: RepositorySnapshot,
    task_type: TaskType,
    *,
    item_index: int = 0,
) -> dict:
    if task_type in {
        TaskType.PARSE_DEEP_RESEARCH,
        TaskType.CORPUS_CHALLENGER,
        TaskType.GENERATE_CANDIDATE_CLAIMS,
    }:
        value = {"themes": [], "claims": []}
    elif task_type is TaskType.GENERATE_RETRIEVAL_QUERIES:
        value = {"queries": []}
    elif task_type is TaskType.GENERATE_PROPOSITIONS:
        proposition = snapshot.proposition_records[0]
        value = {
            "propositions": [
                {
                    "local_ref": "proposition_one",
                    "text": proposition.text,
                    "content_class": proposition.content_class.value,
                    "claim_refs": list(proposition.claim_ids),
                    "citation_bindings": [
                        {
                            "paper_ref": item.paper_id,
                            "claim_ref": item.claim_id,
                            "claim_paper_evidence_ref": (
                                item.claim_paper_evidence_id
                            ),
                        }
                        for item in proposition.citation_bindings
                    ],
                    "corpus_fact_refs": list(proposition.corpus_fact_ids),
                    "process_fact_refs": list(proposition.process_fact_ids),
                }
            ]
        }
    elif task_type is TaskType.AUDIT_PROPOSITION:
        audit = snapshot.semantic_audits[0]
        value = {
            "target_ref": "proposition_one",
            "class_verdict": audit.class_verdict.value,
            "provenance_verdict": audit.provenance_verdict.value,
            "reason": audit.reason,
            "referenced_claim_refs": list(audit.referenced_claim_ids),
            "referenced_corpus_fact_refs": list(
                audit.referenced_corpus_fact_ids
            ),
            "referenced_process_fact_refs": list(
                audit.referenced_process_fact_ids
            ),
        }
    elif task_type is TaskType.RENDER_PROSE:
        value = {
            "sentences": [
                {
                    "local_ref": f"sentence_{index + 1:04d}",
                    "text": sentence.text,
                    "source_proposition_refs": list(
                        sentence.source_proposition_ids
                    ),
                }
                for index, sentence in enumerate(snapshot.rendered_sentences)
            ]
        }
    elif task_type is TaskType.AUDIT_RENDERED_SENTENCE:
        audit = snapshot.rendered_sentence_audits[item_index]
        value = {
            "sentence_ref": f"sentence_{item_index + 1:04d}",
            "verdict": audit.verdict.value,
            "reason": audit.reason,
        }
    else:  # pragma: no cover - the fixture names its exact task set
        raise AssertionError(task_type)
    model = TASK_SPECS[task_type].proposal_model.model_validate(value)
    return model.model_dump(mode="json")


def _semantic_fingerprint(manifest: PilotRunManifest) -> TaskSemanticFingerprint:
    components = {
        "validator_fingerprint": manifest.validator_fingerprint,
        "promotion_handler_fingerprint": hash_text("packet promotion"),
        "disposition_handler_fingerprint": hash_text("packet disposition"),
        "scientific_contract_version": "V1.5.1b",
        "runtime_contract_version": "1.6",
    }
    return TaskSemanticFingerprint(
        **components,
        combined_fingerprint=hash_json(components),
    )


def _canonical_witnesses(
    snapshot: RepositorySnapshot,
    task_type: TaskType,
    *,
    item_index: int = 0,
) -> tuple[CanonicalObjectReceipt, ...]:
    if task_type is TaskType.AUDIT_PROPOSITION:
        qualified_ids = (
            f"PropositionRecord:{snapshot.proposition_records[0].proposition_id}",
            f"SemanticAuditResult:{snapshot.semantic_audits[0].audit_id}",
        )
    elif task_type is TaskType.AUDIT_RENDERED_SENTENCE:
        sentence = snapshot.rendered_sentences[item_index]
        audit = snapshot.rendered_sentence_audits[item_index]
        qualified_ids = (
            f"RenderedSentence:{sentence.sentence_id}",
            f"RenderedSentenceAudit:{audit.audit_id}",
        )
    else:
        return ()
    index = snapshot.object_index()
    return tuple(
        CanonicalObjectReceipt(
            qualified_id=qualified_id,
            object_hash=hash_json(index[qualified_id].model_dump(mode="json")),
        )
        for qualified_id in qualified_ids
    )


def _synthetic_task_usage(
    manifest: PilotRunManifest,
    provenance: TaskProvenance,
    receipt: AppliedTaskReceipt,
    proposal: dict,
    *,
    ordinal: int,
) -> tuple[str, bytes, PilotTaskUsageTotals]:
    proposal_content = canonical_json_bytes(proposal)
    proposal_content_hash = hash_bytes(proposal_content)
    stdout = f"synthetic stdout for task {ordinal}\n".encode("utf-8")
    stderr = f"synthetic stderr for task {ordinal}\n".encode("utf-8")
    writable_inventory = [
        {
            "relative_path": "output",
            "file_type": "directory",
            "size_bytes": None,
            "content_hash": None,
        },
        {
            "relative_path": "output/proposal.json",
            "file_type": "regular",
            "size_bytes": len(proposal_content),
            "content_hash": proposal_content_hash,
        },
    ]
    attempt_id = receipt.accepted_attempt_id.removeprefix(
        f"{provenance.task_id}/"
    )
    attempt = PilotAttemptUsage(
        task_id=provenance.task_id,
        attempt_id=attempt_id,
        engine=receipt.engine,
        engine_version=receipt.engine_version,
        outcome=AttemptOutcome.VALID_SCIENTIFIC_RESULT,
        accepted_attempt=True,
        attempt_record_inferred=True,
        agent_result_content_hash=hash_text(
            f"packet agent result:{ordinal}"
        ),
        attempt_record_content_hash=None,
        attempt_record_semantic_hash=None,
        output_content_hash=proposal_content_hash,
        proposal_content_hash=proposal_content_hash,
        proposal_semantic_hash=receipt.proposal_hash,
        stdout_content_hash=hash_bytes(stdout),
        stderr_content_hash=hash_bytes(stderr),
        writable_inventory_hash=hash_json(writable_inventory),
        output_bytes=len(proposal_content),
        proposal_bytes=len(proposal_content),
        stdout_bytes=len(stdout),
        stderr_bytes=len(stderr),
        retained_diagnostic_bytes=len(stdout) + len(stderr),
        writable_entry_count=len(writable_inventory),
        writable_tree_bytes=len(proposal_content),
    )
    totals = PilotTaskUsageTotals(
        attempt_count=1,
        output_bytes=attempt.output_bytes,
        proposal_bytes=attempt.proposal_bytes,
        stdout_bytes=attempt.stdout_bytes,
        stderr_bytes=attempt.stderr_bytes,
        retained_diagnostic_bytes=attempt.retained_diagnostic_bytes,
        writable_entry_count=attempt.writable_entry_count,
        writable_tree_bytes=attempt.writable_tree_bytes,
    )
    usage = PilotTaskUsageArtifact(
        task_id=provenance.task_id,
        accepted_attempt_id=receipt.accepted_attempt_id,
        budget_content_hash=hash_json(manifest.budget.model_dump(mode="json")),
        attempts=(attempt,),
        totals=totals,
    )
    semantic_digest = receipt.semantic_task_key.removeprefix("sha256:")
    usage_path = (
        f"pilot/runs/{manifest.run_id}/attempt-usage/"
        f"{ordinal:04d}-{receipt.task_type.value}-{semantic_digest}.json"
    )
    return usage_path, canonical_pilot_task_usage_bytes(usage), totals


def _commit_task_event(
    store: GenerationStore,
    manifest: PilotRunManifest,
    registration: PilotRunRegistrationReference,
    *,
    task_type: TaskType,
    stage: PilotStage,
    ordinal: int,
    previous,
    budget: dict[str, int],
    item_index: int = 0,
):
    source_generation, snapshot, _ = store.load_current()
    provenance = _task_provenance(
        manifest,
        task_type=task_type,
        source_generation=source_generation,
        ordinal=ordinal,
    )
    proposal = _task_proposal(snapshot, task_type, item_index=item_index)
    binding = manifest.engine_role_plan[task_type]
    fingerprint = _semantic_fingerprint(manifest)
    input_key = compute_input_identity_key(
        task_type=task_type,
        task_spec_version=provenance.task_spec_version,
        prompt_hash=provenance.instructions_hash,
        input_schema_hash=provenance.input_schema_hash,
        proposal_schema_hash=provenance.proposal_schema_hash,
        dependency_hashes=provenance.dependencies,
        resource_hashes={
            resource.resource_id: resource.snapshot_hash
            for resource in provenance.resources
        },
        engine_input_hash=provenance.engine_input_hash,
        engine="ordered-engine-plan",
        engine_version=fingerprint.runtime_contract_version,
        safe_engine_configuration_hash=binding.safe_configuration_hash,
        scientific_contract_version=fingerprint.scientific_contract_version,
    )
    witnesses = _canonical_witnesses(
        snapshot,
        task_type,
        item_index=item_index,
    )
    if task_type is TaskType.AUDIT_RENDERED_SENTENCE:
        disposition = str(proposal["verdict"])
        downstream_eligible = disposition == "ENTAILED"
    else:
        disposition = "VALID"
        downstream_eligible = True
    local_ref_map = {}
    if task_type is TaskType.AUDIT_PROPOSITION:
        local_ref_map = {
            "proposition_one": snapshot.proposition_records[0].proposition_id
        }
    elif task_type is TaskType.AUDIT_RENDERED_SENTENCE:
        local_ref_map = {
            f"sentence_{item_index + 1:04d}": (
                snapshot.rendered_sentences[item_index].sentence_id
            )
        }
    receipt = AppliedTaskReceipt(
        input_identity_key=input_key,
        semantic_task_key=compute_semantic_task_key(
            input_identity_key=input_key,
            semantic_fingerprint=fingerprint,
        ),
        task_type=task_type,
        task_spec_version=provenance.task_spec_version,
        proposal_hash=hash_json(proposal),
        proposal_payload=proposal,
        semantic_fingerprint=fingerprint,
        source_generation=source_generation,
        committed_generation=source_generation + 1,
        canonical_objects=witnesses,
        local_ref_map=local_ref_map,
        recorded_transition=TransitionDecision(
            scientific_disposition=disposition,
            canonicalized=bool(witnesses) and downstream_eligible,
            downstream_eligible=downstream_eligible,
        ),
        engine=binding.engine,
        engine_version=binding.engine_version,
        accepted_attempt_id=f"{provenance.task_id}/01-synthetic",
    )
    usage_path, usage_content, usage_totals = _synthetic_task_usage(
        manifest,
        provenance,
        receipt,
        proposal,
        ordinal=ordinal,
    )
    usage_deltas = {
        "semantic_engine_invocations": usage_totals.attempt_count,
        "proposal_bytes": usage_totals.output_bytes,
        "stdout_bytes": usage_totals.stdout_bytes,
        "stderr_bytes": usage_totals.stderr_bytes,
        "retained_diagnostic_bytes": usage_totals.retained_diagnostic_bytes,
        "writable_entries": usage_totals.writable_entry_count,
        "writable_tree_bytes": usage_totals.writable_tree_bytes,
    }
    for name, delta in usage_deltas.items():
        budget[name] = budget.get(name, 0) + delta
    domain_content = canonical_json_bytes(proposal) + b"\n"
    domain_path = f"pilot-fixture/{task_type.value}.json"

    def inner(writer, _generation, _payload, _receipt) -> None:
        writer.write_bytes(domain_path, domain_content)
        writer.write_bytes(usage_path, usage_content)

    materializer = PilotStageMaterializer(
        project_root=store.project_root,
        registration=registration,
        run_manifest=manifest,
        task_provenance=provenance,
        engine_plan_hash=binding.safe_configuration_hash,
        ordinal=ordinal,
        stage=stage,
        input_hashes={"fixture": hash_text(f"packet task:{ordinal}")},
        artifact_hashes={
            domain_path: hash_bytes(domain_content),
            usage_path: hash_bytes(usage_content),
        },
        budget_consumed=dict(budget),
        previous_stage=previous,
        inner=inner,
    )
    store.commit(
        base_generation=source_generation,
        dependencies={},
        promotion=lambda current, registry: PromotionPayload(
            current, registry, {}
        ),
        receipt_factory=lambda _generation, _before, _payload: receipt,
        staging_materializer=lambda writer, generation, payload: materializer(
            writer, generation, payload, receipt
        ),
    )
    return materializer.prepared_for(receipt).reference


def _register(
    root: Path, snapshot: RepositorySnapshot
) -> tuple[GenerationStore, PilotRunManifest, PilotRunRegistrationReference]:
    store = GenerationStore(root)
    store.initialize(snapshot)
    lock, import_manifest, selection, raw = _corpus_witnesses(snapshot)

    def materialize_corpus(writer, next_generation, _payload):
        assert next_generation == 1
        writer.write_bytes(
            "library/corpus.lock.json",
            canonical_json_bytes(lock.model_dump(mode="json")) + b"\n",
        )
        writer.write_bytes(
            "library/import_manifest.json",
            canonical_json_bytes(import_manifest.model_dump(mode="json")) + b"\n",
        )
        writer.write_bytes(
            "library/selection_manifest.json",
            canonical_json_bytes(selection.model_dump(mode="json")),
        )
        prefix = "state/generations/000001/auxiliary/"
        by_id = {paper.paper_id: paper for paper in lock.papers}
        for paper_id, content in raw.items():
            writer.write_bytes(
                by_id[paper_id].raw_md_path.removeprefix(prefix), content
            )

    store.commit(
        base_generation=0,
        dependencies={},
        promotion=lambda current, registry: PromotionPayload(
            current, registry, {}
        ),
        staging_materializer=materialize_corpus,
    )
    manifest = _run_manifest(snapshot)
    manifest_path = f"pilot/runs/{manifest.run_id}/run_manifest.json"
    manifest_content = canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n"
    reference_box: dict[str, PilotRunRegistrationReference] = {}

    def promote(current, registry):
        corpus_ids, registry = registry.allocate(IdKind.CORPUS_FACT)
        process_ids, registry = registry.allocate(IdKind.PROCESS_FACT, 2)
        corpus = CorpusFact(
            corpus_fact_id=corpus_ids[0],
            text=PILOT_CORPUS_FACT_TEXT,
            derivation_type=CorpusFactDerivationType.REGISTRY_ARITHMETIC,
            source_paper_ids=[item.paper_id for item in current.papers],
            derivation=CorpusFactDerivation(
                task_version=None,
                model_signature=None,
                input_hash=None,
                output_hash=None,
            ),
        )
        prefix = f"pilot-run:{manifest.manifest_hash.removeprefix('sha256:')}"
        scope = ReviewProcessFact(
            process_fact_id=process_ids[0],
            text=PILOT_SCOPE_FACT_TEXT,
            source_type=ReviewProcessSourceType.RUN_MANIFEST,
            source_key=f"{prefix}:scope",
        )
        human = ReviewProcessFact(
            process_fact_id=process_ids[1],
            text=PILOT_HUMAN_REVIEW_FACT_TEXT,
            source_type=ReviewProcessSourceType.RUN_MANIFEST,
            source_key=f"{prefix}:human-review",
        )
        proposition = current.proposition_records[0].model_copy(
            update={
                "content_class": PropositionContentClass.REVIEW_PROCESS_STATEMENT,
                "process_fact_ids": [scope.process_fact_id],
            }
        )
        semantic_audit = current.semantic_audits[0].model_copy(
            update={
                "referenced_process_fact_ids": [scope.process_fact_id],
            }
        )
        promoted = current.model_copy(
            update={
                "corpus_facts": current.corpus_facts + (corpus,),
                "process_facts": current.process_facts + (scope, human),
                "proposition_records": (proposition,),
                "semantic_audits": (semantic_audit,),
            }
        )
        return PromotionPayload(
            promoted,
            registry,
            {
                "corpus_fact": corpus.corpus_fact_id,
                "scope": scope.process_fact_id,
                "human": human.process_fact_id,
            },
        )

    def materialize(writer, next_generation, payload):
        artifact = PilotRunRegistrationArtifact(
            run_id=manifest.run_id,
            source_generation=manifest.source_generation,
            owner_generation=next_generation,
            run_manifest_hash=manifest.manifest_hash,
            run_manifest_content_hash=hash_bytes(manifest_content),
            run_manifest_relative_path=manifest_path,
            corpus_fact_id=payload.allocated_ids["corpus_fact"],
            scope_process_fact_id=payload.allocated_ids["scope"],
            human_review_process_fact_id=payload.allocated_ids["human"],
        )
        content = canonical_json_bytes(artifact.model_dump(mode="json")) + b"\n"
        artifact_hash = hash_bytes(content)
        reference = PilotRunRegistrationReference(
            run_id=manifest.run_id,
            owner_generation=next_generation,
            artifact_hash=artifact_hash,
            relative_path=(
                f"pilot/runs/{manifest.run_id}/registration-"
                f"{artifact_hash.removeprefix('sha256:')}.json"
            ),
        )
        writer.write_bytes(manifest_path, manifest_content)
        writer.write_bytes(reference.relative_path, content)
        reference_box["value"] = reference

    store.commit(
        base_generation=manifest.source_generation,
        dependencies={},
        promotion=promote,
        staging_materializer=materialize,
    )
    return store, manifest, reference_box["value"]


def _pilot_fixture(tmp_path: Path, bundle_factory):
    snapshot = _five_paper_snapshot(bundle_factory)
    review = tmp_path / "review"
    store, run_manifest, registration = _register(review, snapshot)
    previous = record_pilot_control_event(
        review,
        registration,
        run_manifest,
        ordinal=1,
        stage=PilotStage.PREREQUISITES,
        status=PilotStageStatus.COMPLETED,
        artifact_payload={"prerequisites": "verified"},
    ).reference
    ordinal = 2
    cumulative_budget: dict[str, int] = {}
    task_stages = (
        (TaskType.PARSE_DEEP_RESEARCH, PilotStage.DISCOVERY),
        (TaskType.CORPUS_CHALLENGER, PilotStage.CORPUS_CHALLENGE),
        (TaskType.GENERATE_CANDIDATE_CLAIMS, PilotStage.DISCOVERY),
        (
            TaskType.GENERATE_RETRIEVAL_QUERIES,
            PilotStage.QUERY_GENERATION,
        ),
    )
    for task_type, stage in task_stages:
        previous = _commit_task_event(
            store,
            run_manifest,
            registration,
            task_type=task_type,
            stage=stage,
            ordinal=ordinal,
            previous=previous,
            budget=cumulative_budget,
        )
        ordinal += 1

    retrieval_source = store.current_generation()
    retrieval = PilotRetrievalControlPayload.from_ledgers(
        source_generation=retrieval_source,
        corpus_lock_hash=run_manifest.corpus_lock_hash,
        ledgers=(),
    )
    cumulative_budget.update(
        {
            "queries": 0,
            "raw_candidates": 0,
            "assessed_candidates": 0,
        }
    )
    previous = record_pilot_control_event(
        review,
        registration,
        run_manifest,
        ordinal=ordinal,
        stage=PilotStage.RETRIEVAL,
        status=PilotStageStatus.COMPLETED,
        artifact_payload=retrieval.model_dump(mode="json"),
        budget_consumed=cumulative_budget,
        previous_stage=previous,
    ).reference
    ordinal += 1

    for task_type, stage in (
        (TaskType.GENERATE_PROPOSITIONS, PilotStage.PROPOSITION_AUDIT),
        (TaskType.AUDIT_PROPOSITION, PilotStage.PROPOSITION_AUDIT),
        (TaskType.RENDER_PROSE, PilotStage.SENTENCE_AUDIT),
    ):
        previous = _commit_task_event(
            store,
            run_manifest,
            registration,
            task_type=task_type,
            stage=stage,
            ordinal=ordinal,
            previous=previous,
            budget=cumulative_budget,
        )
        ordinal += 1

    sentence_count = len(store.load_current()[1].rendered_sentences)
    for item_index in range(sentence_count):
        previous = _commit_task_event(
            store,
            run_manifest,
            registration,
            task_type=TaskType.AUDIT_RENDERED_SENTENCE,
            stage=PilotStage.SENTENCE_AUDIT,
            ordinal=ordinal,
            previous=previous,
            budget=cumulative_budget,
            item_index=item_index,
        )
        ordinal += 1

    assembly_source = store.current_generation() + 1
    body, assembly = assemble_exact_section(
        store.load_current()[1],
        source_generation=assembly_source,
        budget=AssemblyBudget(
            max_sentences=18,
            max_citations=30,
            max_body_utf8_bytes=64 * 1024,
        ),
    )
    assembly_control = PilotExactAssemblyControlPayload.from_assembly(
        body, assembly
    )
    exact_event = record_pilot_control_event(
        review,
        registration,
        run_manifest,
        ordinal=ordinal,
        stage=PilotStage.EXACT_ASSEMBLY,
        status=PilotStageStatus.COMPLETED,
        artifact_payload=assembly_control.model_dump(mode="json"),
        budget_consumed=cumulative_budget,
        previous_stage=previous,
    )
    ordinal += 1
    validation = validate_synthetic_pilot_run(
        review,
        registration=registration,
        journal_head=exact_event.reference,
        body=body,
        assembly=assembly,
    )
    validation_control = PilotValidationControlPayload.from_report(
        report=validation.report,
        diagnostics=validation.diagnostics,
    )
    final_event = record_pilot_control_event(
        review,
        registration,
        run_manifest,
        ordinal=ordinal,
        stage=PilotStage.VALIDATION_REPORT,
        status=PilotStageStatus.COMPLETED,
        artifact_payload=validation_control.model_dump(mode="json"),
        budget_consumed=cumulative_budget,
        previous_stage=exact_event.reference,
    )
    assert final_event.generation == assembly.source_generation + 1
    public = tmp_path / "public"
    public.mkdir()
    return (
        store,
        review,
        public,
        registration,
        final_event.reference,
        body,
        assembly,
    )


def _write_fixture(tmp_path: Path, bundle_factory):
    fixture = _pilot_fixture(tmp_path, bundle_factory)
    _, review, public, registration, head, body, assembly = fixture
    packet = tmp_path / "packet"
    manifest = write_synthetic_pilot_packet(
        review,
        packet,
        public_repository_root=public,
        registration=registration,
        journal_head=head,
        body=body,
        assembly=assembly,
    )
    return fixture, packet, manifest


def test_packet_is_complete_nonpublication_offline_and_idempotent(
    tmp_path: Path, bundle_factory
) -> None:
    fixture, packet, manifest = _write_fixture(tmp_path, bundle_factory)
    _, review, public, registration, head, body, assembly = fixture

    assert verify_synthetic_pilot_packet(packet) == manifest
    assert manifest.publication_eligible is False
    assert manifest.human_review == "NOT_PERFORMED"
    assert manifest.journal_head == head
    assert manifest.source_generation == head.owner_generation
    assert manifest.source_generation == assembly.source_generation + 1
    assert len(manifest.corpus_sources) == 5
    assert manifest.corpus_lock_hash == json.loads(
        (packet / "run_manifest.json").read_bytes()
    )["corpus_lock_hash"]
    assert manifest.selection_manifest_hash == json.loads(
        (packet / "run_manifest.json").read_bytes()
    )["selection_manifest_hash"]
    assert {
        "corpus-lock.json",
        "corpus-import-manifest.json",
        "corpus-selection-manifest.json",
    } <= {item.relative_path for item in manifest.files}
    assert all(
        (packet / item.packet_relative_path).read_bytes()
        and hash_bytes((packet / item.packet_relative_path).read_bytes())
        == item.raw_md_hash
        for item in manifest.corpus_sources
    )
    assert {item.relative_path for item in manifest.files} == {
        path.name for path in packet.iterdir() if path.name != "pilot_packet.json"
    }
    human = json.loads((packet / "human_review.json").read_bytes())
    assert human["status"] == "NOT_PERFORMED"
    assert human["publication_eligible"] is False
    serialized_packet = b"".join(path.read_bytes() for path in packet.iterdir())
    assert b'"source_path"' not in serialized_packet
    assert str(review).encode("utf-8") not in serialized_packet

    before = {
        path.name: (path.stat().st_dev, path.stat().st_ino, path.read_bytes())
        for path in packet.iterdir()
    }
    reused = write_synthetic_pilot_packet(
        review,
        packet,
        public_repository_root=public,
        registration=registration,
        journal_head=head,
        body=body,
        assembly=assembly,
    )
    after = {
        path.name: (path.stat().st_dev, path.stat().st_ino, path.read_bytes())
        for path in packet.iterdir()
    }
    assert reused == manifest
    assert after == before

    review.rename(tmp_path / "detached-review")
    assert verify_synthetic_pilot_packet(packet) == manifest


def test_packet_rejects_attempt_usage_above_registered_per_attempt_limit(
    tmp_path: Path, bundle_factory, monkeypatch
) -> None:
    original = _synthetic_task_usage

    def oversized_usage(*args, **kwargs):
        path, content, _ = original(*args, **kwargs)
        usage = PilotTaskUsageArtifact.model_validate_json(content)
        attempt = usage.attempts[0]
        manifest = args[0] if args else kwargs["manifest"]
        stdout_bytes = manifest.budget.max_stdout_bytes_per_attempt + 1
        changed_attempt = PilotAttemptUsage.model_validate(
            {
                **attempt.model_dump(mode="json"),
                "stdout_content_hash": hash_bytes(b"X" * stdout_bytes),
                "stdout_bytes": stdout_bytes,
                "retained_diagnostic_bytes": stdout_bytes + attempt.stderr_bytes,
            }
        )
        totals = PilotTaskUsageTotals(
            attempt_count=1,
            output_bytes=changed_attempt.output_bytes,
            proposal_bytes=changed_attempt.proposal_bytes,
            stdout_bytes=changed_attempt.stdout_bytes,
            stderr_bytes=changed_attempt.stderr_bytes,
            retained_diagnostic_bytes=changed_attempt.retained_diagnostic_bytes,
            writable_entry_count=changed_attempt.writable_entry_count,
            writable_tree_bytes=changed_attempt.writable_tree_bytes,
        )
        changed = PilotTaskUsageArtifact(
            task_id=usage.task_id,
            accepted_attempt_id=usage.accepted_attempt_id,
            budget_content_hash=usage.budget_content_hash,
            attempts=(changed_attempt,),
            totals=totals,
        )
        return path, canonical_pilot_task_usage_bytes(changed), totals

    monkeypatch.setattr(
        sys.modules[__name__], "_synthetic_task_usage", oversized_usage
    )

    with pytest.raises(PilotPacketError, match="exceeds its run budget"):
        _write_fixture(tmp_path, bundle_factory)


def _located_packet_values(packet: Path, bundle_factory):
    prepared = packet_module.load_verified_synthetic_pilot_packet(packet)
    values = dict(prepared.files)
    run_manifest = PilotRunManifest.model_validate_json(values["run_manifest.json"])
    snapshot = RepositorySnapshot.model_validate_json(values["repository.json"])
    original = RepositorySnapshot.model_validate(bundle_factory())
    source = prepared.manifest.corpus_sources[0]
    prototype = original.retrieved_spans[0]
    span = prototype.model_copy(
        update={
            "paper_id": source.paper_id,
            "locator": prototype.locator.model_copy(
                update={
                    "raw_md_path": source.raw_md_path,
                    "source_span_hash": hash_bytes(
                        prototype.source_text.encode("utf-8")
                    ),
                }
            ),
        }
    )
    located = snapshot.model_copy(update={"retrieved_spans": (span,)})
    return prepared.manifest, values, run_manifest, located


def test_offline_corpus_witness_recomputes_unicode_locator_slices(
    tmp_path: Path, bundle_factory
) -> None:
    _, packet, _ = _write_fixture(tmp_path, bundle_factory)
    manifest, values, run_manifest, located = _located_packet_values(
        packet, bundle_factory
    )

    packet_module._verify_embedded_corpus(
        values,
        packet_manifest=manifest,
        run_manifest=run_manifest,
        snapshot=located,
    )


@pytest.mark.parametrize(
    "mutation",
    ("raw", "locator_path", "paper_hash", "slice", "source_text", "span_hash"),
)
def test_offline_corpus_witness_rejects_locator_and_source_tampering(
    tmp_path: Path, bundle_factory, mutation: str
) -> None:
    _, packet, _ = _write_fixture(tmp_path, bundle_factory)
    manifest, values, run_manifest, located = _located_packet_values(
        packet, bundle_factory
    )
    source = manifest.corpus_sources[0]
    span = located.retrieved_spans[0]
    if mutation == "raw":
        values[source.packet_relative_path] += b"tampered"
    elif mutation == "locator_path":
        span = span.model_copy(
            update={
                "locator": span.locator.model_copy(
                    update={"raw_md_path": "wrong/raw.md"}
                )
            }
        )
        located = located.model_copy(update={"retrieved_spans": (span,)})
    elif mutation == "paper_hash":
        papers = tuple(
            item.model_copy(update={"raw_md_hash": _sha("f")})
            if item.paper_id == source.paper_id
            else item
            for item in located.papers
        )
        located = located.model_copy(update={"papers": papers})
    elif mutation == "slice":
        span = span.model_copy(
            update={
                "locator": span.locator.model_copy(
                    update={
                        "start_offset": span.locator.start_offset + 1,
                        "end_offset": span.locator.end_offset + 1,
                    }
                )
            }
        )
        located = located.model_copy(update={"retrieved_spans": (span,)})
    elif mutation == "source_text":
        span = span.model_copy(update={"source_text": "Stress increase"})
        located = located.model_copy(update={"retrieved_spans": (span,)})
    else:
        span = span.model_copy(
            update={
                "locator": span.locator.model_copy(
                    update={"source_span_hash": _sha("f")}
                )
            }
        )
        located = located.model_copy(update={"retrieved_spans": (span,)})

    with pytest.raises(PilotPacketError, match="corpus|raw|span"):
        packet_module._verify_embedded_corpus(
            values,
            packet_manifest=manifest,
            run_manifest=run_manifest,
            snapshot=located,
        )


def test_packet_validation_gate_rejects_failures_and_requires_execution(
    tmp_path: Path, bundle_factory
) -> None:
    _, packet, _ = _write_fixture(tmp_path, bundle_factory)
    prepared = packet_module.load_verified_synthetic_pilot_packet(packet)
    report = PilotValidationReport.model_validate_json(
        prepared.files["validation_report.json"]
    )
    snapshot = RepositorySnapshot.model_validate_json(
        prepared.files["repository.json"]
    )
    packet_module._verify_validation_acceptance_gate(report, snapshot)

    for field in ("locator_verification", "citation_authorization"):
        with pytest.raises(PilotPacketError, match="rejects failed"):
            packet_module._verify_validation_acceptance_gate(
                report.model_copy(update={field: ValidationStatus.FAILED}),
                snapshot,
            )

    _, _, _, located = _located_packet_values(packet, bundle_factory)
    with pytest.raises(PilotPacketError, match="requires passed locator"):
        packet_module._verify_validation_acceptance_gate(report, located)

    original = RepositorySnapshot.model_validate(bundle_factory())
    proposition = snapshot.proposition_records[0].model_copy(
        update={
            "citation_bindings": original.proposition_records[0].citation_bindings
        }
    )
    cited = snapshot.model_copy(update={"proposition_records": (proposition,)})
    with pytest.raises(PilotPacketError, match="requires passed citation"):
        packet_module._verify_validation_acceptance_gate(report, cited)

    passed = report.model_copy(
        update={
            "locator_verification": ValidationStatus.PASSED,
            "citation_authorization": ValidationStatus.PASSED,
        }
    )
    packet_module._verify_validation_acceptance_gate(passed, located)
    packet_module._verify_validation_acceptance_gate(passed, cited)


def test_packet_rejects_private_or_absolute_task_provenance() -> None:
    with pytest.raises(PilotPacketError, match="private provenance"):
        packet_module._reject_secret_fields(
            {"task_provenance": {"resources": [{"source_path": "/private/a"}]}}
        )
    with pytest.raises(PilotPacketError, match="absolute private provenance"):
        packet_module._reject_secret_fields(
            {"task_provenance": {"workspace_hint": "/private/work"}}
        )


def test_offline_verifier_passes_embedded_snapshot_to_fixed_sequence(
    tmp_path: Path, bundle_factory, monkeypatch
) -> None:
    _, packet, manifest = _write_fixture(tmp_path, bundle_factory)
    original = packet_module.validate_fixed_pilot_artifact_sequence
    captured: list[RepositorySnapshot] = []

    def capture(events, *, control_artifacts, snapshot):
        captured.append(snapshot)
        return original(
            events,
            control_artifacts=control_artifacts,
            snapshot=snapshot,
        )

    monkeypatch.setattr(
        packet_module,
        "validate_fixed_pilot_artifact_sequence",
        capture,
    )

    assert verify_synthetic_pilot_packet(packet) == manifest
    assert len(captured) == 1
    assert captured[0].canonical_hash() == manifest.repository_hash


@pytest.mark.parametrize("mutation", ["incomplete", "reversed"])
def test_packet_rejects_incomplete_or_reversed_journal_inventory(
    tmp_path: Path, bundle_factory, mutation: str
) -> None:
    _, packet, _ = _write_fixture(tmp_path, bundle_factory)
    manifest_path = packet / "pilot_packet.json"
    raw = json.loads(manifest_path.read_bytes())
    if mutation == "incomplete":
        raw["journal"] = raw["journal"][:-1]
    else:
        raw["journal"] = list(reversed(raw["journal"]))
    manifest_path.chmod(0o644)
    manifest_path.write_bytes(canonical_json_bytes(raw) + b"\n")

    with pytest.raises(PilotPacketError):
        verify_synthetic_pilot_packet(packet)


@pytest.mark.parametrize("mutation", ["tamper", "missing", "extra"])
def test_packet_rejects_tampered_missing_or_extra_files(
    tmp_path: Path, bundle_factory, mutation: str
) -> None:
    _, packet, _ = _write_fixture(tmp_path, bundle_factory)
    damaged = tmp_path / f"damaged-{mutation}"
    shutil.copytree(packet, damaged)
    if mutation == "tamper":
        target = damaged / "section.md"
        target.chmod(0o644)
        target.write_bytes(target.read_bytes() + b"changed\n")
    elif mutation == "missing":
        (damaged / "section.md").unlink()
    else:
        (damaged / "extra.txt").write_text("unexpected\n", encoding="utf-8")

    with pytest.raises(PilotPacketError):
        verify_synthetic_pilot_packet(damaged)


def _rewrite_manifest_file_record(
    packet: Path, filename: str, content: bytes, **manifest_updates
) -> None:
    manifest_path = packet / "pilot_packet.json"
    manifest = SyntheticPilotPacketManifest.model_validate_json(
        manifest_path.read_bytes()
    )
    records = tuple(
        item.model_copy(
            update={"size_bytes": len(content), "content_hash": hash_bytes(content)}
        )
        if item.relative_path == filename
        else item
        for item in manifest.files
    )
    manifest = manifest.model_copy(update={"files": records, **manifest_updates})
    target = packet / filename
    target.chmod(0o644)
    target.write_bytes(content)
    manifest_path.chmod(0o644)
    manifest_path.write_bytes(
        canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n"
    )


def test_offline_verifier_rejects_rehashed_corpus_witness_tampering(
    tmp_path: Path, bundle_factory
) -> None:
    _, packet, manifest = _write_fixture(tmp_path, bundle_factory)
    raw_name = manifest.corpus_sources[0].packet_relative_path
    for mutation, filename in (
        ("lock", "corpus-lock.json"),
        ("import", "corpus-import-manifest.json"),
        ("selection", "corpus-selection-manifest.json"),
        ("raw", raw_name),
    ):
        damaged = tmp_path / f"corpus-{mutation}"
        shutil.copytree(packet, damaged)
        target = damaged / filename
        if mutation == "raw":
            content = target.read_bytes() + b"tampered"
            updates = {}
        else:
            value = json.loads(target.read_bytes())
            if mutation == "lock":
                value["papers"][0]["title"] = "Tampered title"
                content = canonical_json_bytes(value) + b"\n"
                updates = {"corpus_lock_hash": hash_bytes(content)}
            elif mutation == "import":
                path = next(iter(value["resource_hashes"]))
                value["resource_hashes"][path] = _sha("f")
                content = canonical_json_bytes(value) + b"\n"
                updates = {"corpus_import_manifest_hash": hash_bytes(content)}
            else:
                value["documents"][0]["accepted_by"] = "tampered"
                content = canonical_json_bytes(value)
                updates = {"selection_manifest_hash": hash_bytes(content)}
        _rewrite_manifest_file_record(damaged, filename, content, **updates)

        with pytest.raises(PilotPacketError, match="corpus|raw"):
            verify_synthetic_pilot_packet(damaged)


def test_packet_rejects_a_rewritten_publication_claim(
    tmp_path: Path, bundle_factory
) -> None:
    _, packet, _ = _write_fixture(tmp_path, bundle_factory)
    raw = json.loads((packet / "human_review.json").read_bytes())
    raw["status"] = "APPROVED"
    raw["publication_eligible"] = True
    content = canonical_json_bytes(raw) + b"\n"
    _rewrite_manifest_file_record(packet, "human_review.json", content)

    with pytest.raises(PilotPacketError):
        verify_synthetic_pilot_packet(packet)


def test_packet_rejects_receipt_payload_hash_tampering(
    tmp_path: Path, bundle_factory
) -> None:
    _, packet, _ = _write_fixture(tmp_path, bundle_factory)
    components = {
        "validator_fingerprint": _sha("1"),
        "promotion_handler_fingerprint": _sha("2"),
        "disposition_handler_fingerprint": _sha("3"),
        "scientific_contract_version": "V1.5.1b",
        "runtime_contract_version": "1.6",
    }
    fingerprint = TaskSemanticFingerprint(
        **components,
        combined_fingerprint=hash_json(components),
    )
    input_key = hash_text("packet receipt input")
    receipt = AppliedTaskReceipt(
        input_identity_key=input_key,
        semantic_task_key=compute_semantic_task_key(
            input_identity_key=input_key, semantic_fingerprint=fingerprint
        ),
        task_type=TaskType.PARSE_DEEP_RESEARCH,
        task_spec_version="2",
        proposal_hash=_sha("f"),
        proposal_payload={},
        semantic_fingerprint=fingerprint,
        source_generation=1,
        committed_generation=2,
        canonical_objects=(),
        local_ref_map={},
        recorded_transition=TransitionDecision(
            scientific_disposition="VALID",
            canonicalized=False,
            downstream_eligible=True,
        ),
        engine="synthetic-mock",
        engine_version="1",
        accepted_attempt_id="TASK0001/01-synthetic",
    )
    content = canonical_json_bytes([receipt.model_dump(mode="json")]) + b"\n"
    _rewrite_manifest_file_record(
        packet,
        "applied_tasks.json",
        content,
        accepted_receipts_hash=hash_bytes(content),
        accepted_receipt_count=1,
    )

    with pytest.raises(PilotPacketError, match="proposal hash"):
        verify_synthetic_pilot_packet(packet)


def test_writer_rejects_a_noncurrent_assembly_and_head(
    tmp_path: Path, bundle_factory
) -> None:
    fixture = _pilot_fixture(tmp_path, bundle_factory)
    store, review, public, registration, head, body, assembly = fixture
    store.commit(
        base_generation=store.current_generation(),
        dependencies={},
        promotion=lambda snapshot, registry: PromotionPayload(snapshot, registry, {}),
    )

    destination = tmp_path / "packet"
    with pytest.raises(PilotPacketError, match="predecessor|CURRENT"):
        write_synthetic_pilot_packet(
            review,
            destination,
            public_repository_root=public,
            registration=registration,
            journal_head=head,
            body=body,
            assembly=assembly,
        )
    assert not destination.exists()


def test_writer_rejects_protected_destinations_and_does_not_follow_symlinks(
    tmp_path: Path, bundle_factory
) -> None:
    fixture = _pilot_fixture(tmp_path, bundle_factory)
    _, review, public, registration, head, body, assembly = fixture
    arguments = {
        "public_repository_root": public,
        "registration": registration,
        "journal_head": head,
        "body": body,
        "assembly": assembly,
    }

    with pytest.raises(PilotPacketError, match="protected root"):
        write_synthetic_pilot_packet(review, public / "packet", **arguments)

    symlink_target = tmp_path / "symlink-target"
    symlink_target.mkdir()
    destination = tmp_path / "packet-link"
    destination.symlink_to(symlink_target, target_is_directory=True)
    with pytest.raises(FileExistsError):
        write_synthetic_pilot_packet(review, destination, **arguments)
    assert not tuple(symlink_target.iterdir())
