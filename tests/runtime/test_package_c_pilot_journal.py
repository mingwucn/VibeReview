from __future__ import annotations

from pathlib import Path

import pytest

from vibereview.enums import CorpusFactDerivationType, ReviewProcessSourceType
from vibereview.models import (
    CorpusFact,
    CorpusFactDerivation,
    Paper,
    ReviewProcessFact,
)
from vibereview.runtime.artifacts import AssemblyBudget, assemble_exact_section
from vibereview.runtime.hashing import (
    canonical_json_bytes,
    hash_bytes,
    hash_json,
    hash_text,
)
from vibereview.runtime.pilot_journal import (
    PILOT_CURRENT_HEAD_FILENAME,
    PilotControlArtifact,
    PilotExactAssemblyControlPayload,
    PilotJournalError,
    PilotRetrievalControlPayload,
    PilotStageMaterializer,
    PilotTaskProvenanceWitness,
    PilotValidationControlPayload,
    discover_current_pilot_head,
    load_pilot_run_registration,
    load_pilot_stage_artifact,
    record_pilot_control_event,
)
from vibereview.runtime.pilot_manifest import (
    compute_package_c_implementation_fingerprint,
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
from vibereview.runtime.receipts import (
    compute_input_identity_key,
    compute_semantic_task_key,
)
from vibereview.runtime.records import (
    AppliedTaskReceipt,
    ResourceProvenance,
    ResourceSourceDependency,
    TaskProvenance,
    TaskSemanticFingerprint,
    TaskType,
    TransitionDecision,
)
from vibereview.runtime.registry import IdKind
from vibereview.runtime.repository import (
    CrashPoint,
    GenerationStore,
    InjectedCrash,
    PromotionPayload,
    StaleSnapshotError,
)
from vibereview.runtime.state import RepositorySnapshot


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _source_snapshot() -> RepositorySnapshot:
    papers = tuple(
        Paper(
            paper_id=f"P{ordinal:04d}",
            title=f"Synthetic paper {ordinal}",
            authors=["Fixture Author"],
            year=2030,
            doi=None,
            journal="Synthetic Journal",
            identity_keys=[f"fixture:paper-{ordinal}"],
            study_group_id=None,
            related_publications=[],
            independence_status="unknown",
            raw_md_path=f"papers/P{ordinal:04d}/raw.md",
            source_hash=_hash(str(ordinal)),
            raw_md_hash=_hash(chr(ord("a") + ordinal)),
        )
        for ordinal in range(1, 6)
    )
    snapshot = RepositorySnapshot(papers=papers)
    snapshot.validate_repository()
    return snapshot


def _five_paper_complete_snapshot(bundle_factory) -> RepositorySnapshot:
    bundle = bundle_factory()
    prototype = bundle["papers"][0]
    papers = []
    for ordinal in range(1, 6):
        papers.append(
            prototype.model_copy(
                update={
                    "paper_id": f"P{ordinal:04d}",
                    "title": f"Synthetic paper {ordinal}",
                    "doi": None,
                    "identity_keys": [f"fixture:paper-{ordinal}"],
                    "raw_md_path": f"papers/P{ordinal:04d}/raw.md",
                    "source_hash": _hash(str(ordinal)),
                    "raw_md_hash": _hash(chr(ord("a") + ordinal)),
                }
            )
        )
    bundle["papers"] = papers
    snapshot = RepositorySnapshot.model_validate(bundle)
    snapshot.validate_repository()
    return snapshot


def _fingerprint(validator_hash: str) -> TaskSemanticFingerprint:
    components = {
        "validator_fingerprint": validator_hash,
        "promotion_handler_fingerprint": hash_text("promotion"),
        "disposition_handler_fingerprint": hash_text("disposition"),
        "scientific_contract_version": "V1.5.1b",
        "runtime_contract_version": "1.6",
    }
    return TaskSemanticFingerprint(
        **components,
        combined_fingerprint=hash_json(components),
    )


def _provenance(
    *, source: int, task_id: str, task_type: TaskType
) -> TaskProvenance:
    resources = ()
    if task_type is TaskType.PARSE_DEEP_RESEARCH:
        resources = tuple(
            ResourceProvenance(
                resource_id=f"RES{ordinal:04d}",
                logical_name=f"discovery-{ordinal}.md",
                media_type="text/markdown",
                bundle_relative_path=Path(
                    f"input/resources/RES{ordinal:04d}/content.md"
                ),
                source_path=Path(f"/synthetic/discovery-{ordinal}.md"),
                source_dependency=ResourceSourceDependency(
                    source_hash_at_snapshot=_hash(character)
                ),
                snapshot_hash=_hash(character),
                size_bytes=1,
            )
            for ordinal, character in enumerate(("6", "7"), start=1)
        )
    return TaskProvenance(
        task_id=task_id,
        task_type=task_type,
        task_spec_version="1",
        prompt_version="1",
        base_generation=source,
        dependencies={},
        instructions_hash=hash_text("instructions"),
        input_snapshot_hash=hash_text("input snapshot"),
        engine_input_hash=hash_text("engine input"),
        input_schema_hash=hash_text("input schema"),
        proposal_schema_hash=hash_text("proposal schema"),
        expected_bundle_manifest_hash=hash_text("bundle manifest"),
        expected_immutable_files={"instructions.md": hash_text("instructions")},
        resources=resources,
    )


def _manifest(
    *, source: int = 0, snapshot: RepositorySnapshot | None = None
) -> PilotRunManifest:
    binding = PilotEngineRoleBinding(
        engine="synthetic-mock",
        engine_version="1",
        safe_configuration_hash=hash_text("single synthetic engine plan"),
    )
    plan = {task_type: binding for task_type in TaskType}
    prototype = _provenance(
        source=source + 1,
        task_id="TASK0001",
        task_type=TaskType.PARSE_DEEP_RESEARCH,
    )
    prompt_hashes = {
        task_type: prototype.instructions_hash for task_type in TaskType
    }
    schema_hash = compute_pilot_schema_fingerprint(
        input_schema_hash=prototype.input_schema_hash,
        proposal_schema_hash=prototype.proposal_schema_hash,
    )
    return PilotRunManifest(
        run_id="RUN-synthetic-journal",
        created_at="2030-01-01T00:00:00+00:00",
        topic="Synthetic journal topic.",
        source_generation=source,
        corpus_lock_hash=_hash("a"),
        selection_manifest_hash=_hash("b"),
        paper_source_hashes=(
            tuple(
                item.source_hash
                for item in sorted(snapshot.papers, key=lambda item: item.paper_id)
            )
            if snapshot is not None
            else tuple(_hash(str(value)) for value in range(1, 6))
        ),
        discovery_resource_hashes=(_hash("6"), _hash("7")),
        engine_role_plan=plan,
        engine_role_plan_hash=compute_pilot_engine_role_plan_hash(plan),
        prompt_fingerprints=prompt_hashes,
        schema_fingerprints={task_type: schema_hash for task_type in TaskType},
        validator_fingerprint=hash_text("validator"),
        package_c_implementation_fingerprint=(
            compute_package_c_implementation_fingerprint()
        ),
        budget=FivePaperPilotBudget(),
    )


def _register_run(
    project_root: Path,
    *,
    initial_snapshot: RepositorySnapshot | None = None,
) -> tuple[GenerationStore, PilotRunManifest, PilotRunRegistrationReference]:
    store = GenerationStore(project_root)
    source_snapshot = initial_snapshot or _source_snapshot()
    store.initialize(source_snapshot)
    manifest = _manifest(snapshot=source_snapshot)
    manifest_path = f"pilot/runs/{manifest.run_id}/run_manifest.json"
    manifest_content = canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n"
    registration_box: dict[str, object] = {}

    def promote(snapshot, registry):
        corpus_ids, registry = registry.allocate(IdKind.CORPUS_FACT)
        process_ids, registry = registry.allocate(IdKind.PROCESS_FACT, 2)
        corpus = CorpusFact(
            corpus_fact_id=corpus_ids[0],
            text=PILOT_CORPUS_FACT_TEXT,
            derivation_type=CorpusFactDerivationType.REGISTRY_ARITHMETIC,
            source_paper_ids=[item.paper_id for item in snapshot.papers],
            derivation=CorpusFactDerivation(
                task_version=None,
                model_signature=None,
                input_hash=None,
                output_hash=None,
            ),
        )
        source_prefix = f"pilot-run:{manifest.manifest_hash.removeprefix('sha256:')}"
        scope = ReviewProcessFact(
            process_fact_id=process_ids[0],
            text=PILOT_SCOPE_FACT_TEXT,
            source_type=ReviewProcessSourceType.RUN_MANIFEST,
            source_key=f"{source_prefix}:scope",
        )
        human = ReviewProcessFact(
            process_fact_id=process_ids[1],
            text=PILOT_HUMAN_REVIEW_FACT_TEXT,
            source_type=ReviewProcessSourceType.RUN_MANIFEST,
            source_key=f"{source_prefix}:human-review",
        )
        promoted = snapshot.model_copy(
            update={
                "corpus_facts": snapshot.corpus_facts + (corpus,),
                "process_facts": snapshot.process_facts + (scope, human),
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
        registration = PilotRunRegistrationArtifact(
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
        registration_content = (
            canonical_json_bytes(registration.model_dump(mode="json")) + b"\n"
        )
        registration_hash = hash_bytes(registration_content)
        reference = PilotRunRegistrationReference(
            run_id=manifest.run_id,
            owner_generation=next_generation,
            artifact_hash=registration_hash,
            relative_path=(
                f"pilot/runs/{manifest.run_id}/"
                f"registration-{registration_hash.removeprefix('sha256:')}.json"
            ),
        )
        writer.write_bytes(manifest_path, manifest_content)
        writer.write_bytes(reference.relative_path, registration_content)
        registration_box["reference"] = reference

    store.commit(
        base_generation=0,
        dependencies={},
        promotion=promote,
        staging_materializer=materialize,
    )
    reference = registration_box["reference"]
    assert isinstance(reference, PilotRunRegistrationReference)
    assert load_pilot_run_registration(project_root, reference)[1] == manifest
    return store, manifest, reference


def _receipt(
    *, provenance: TaskProvenance, committed: int
) -> AppliedTaskReceipt:
    fingerprint = _fingerprint(hash_text("validator"))
    input_key = compute_input_identity_key(
        task_type=provenance.task_type,
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
        safe_engine_configuration_hash=hash_text(
            "single synthetic engine plan"
        ),
        scientific_contract_version=fingerprint.scientific_contract_version,
    )
    proposal = {"themes": [], "claims": []}
    return AppliedTaskReceipt(
        input_identity_key=input_key,
        semantic_task_key=compute_semantic_task_key(
            input_identity_key=input_key,
            semantic_fingerprint=fingerprint,
        ),
        task_type=provenance.task_type,
        task_spec_version=provenance.task_spec_version,
        proposal_hash=hash_json(proposal),
        proposal_payload=proposal,
        semantic_fingerprint=fingerprint,
        source_generation=provenance.base_generation,
        committed_generation=committed,
        canonical_objects=(),
        local_ref_map={},
        recorded_transition=TransitionDecision(
            scientific_disposition="VALID",
            canonicalized=False,
            downstream_eligible=True,
        ),
        engine="synthetic-mock",
        engine_version="1",
        accepted_attempt_id=f"{provenance.task_id}/01-synthetic",
    )


def _materializer(
    *,
    store: GenerationStore,
    manifest: PilotRunManifest,
    registration: PilotRunRegistrationReference,
    provenance: TaskProvenance,
    ordinal: int,
    stage: PilotStage,
    previous=None,
) -> PilotStageMaterializer:
    domain_content = b"{}\n"

    def inner(writer, _generation, _payload, _receipt_value):
        writer.write_bytes("domain/result.json", domain_content)

    return PilotStageMaterializer(
        project_root=store.project_root,
        registration=registration,
        run_manifest=manifest,
        task_provenance=provenance,
        engine_plan_hash=manifest.engine_role_plan[
            provenance.task_type
        ].safe_configuration_hash,
        ordinal=ordinal,
        stage=stage,
        input_hashes={"source": hash_text("source")},
        artifact_hashes={"domain/result.json": hash_bytes(domain_content)},
        budget_consumed={"semantic_engine_invocations": ordinal},
        previous_stage=previous,
        inner=inner,
    )


def _commit_with_stage(
    store: GenerationStore,
    materializer: PilotStageMaterializer,
    receipt: AppliedTaskReceipt,
):
    receipt_box: dict[str, AppliedTaskReceipt] = {}

    def receipt_factory(next_generation, _before, _payload):
        assert next_generation == receipt.committed_generation
        receipt_box["value"] = receipt
        return receipt

    def staging(writer, next_generation, payload):
        materializer(writer, next_generation, payload, receipt_box["value"])

    return store.commit(
        base_generation=receipt.source_generation,
        dependencies={},
        promotion=lambda snapshot, registry: PromotionPayload(snapshot, registry, {}),
        receipt_factory=receipt_factory,
        staging_materializer=staging,
    )


def _record_control(
    store: GenerationStore,
    manifest: PilotRunManifest,
    registration: PilotRunRegistrationReference,
    *,
    ordinal: int,
    stage: PilotStage,
    status: PilotStageStatus,
    previous=None,
    reason: str | None = None,
    budget: dict[str, int] | None = None,
    payload: dict | None = None,
    crash_at: CrashPoint | None = None,
):
    source_generation = (
        previous.owner_generation if previous is not None else registration.owner_generation
    )
    exact_budget = dict(budget or {})
    if payload is None and status is PilotStageStatus.COMPLETED:
        if stage is PilotStage.RETRIEVAL:
            typed = PilotRetrievalControlPayload.from_ledgers(
                source_generation=source_generation,
                corpus_lock_hash=manifest.corpus_lock_hash,
                ledgers=(),
            )
            payload = typed.model_dump(mode="json")
            exact_budget.setdefault("queries", 0)
            exact_budget.setdefault("raw_candidates", 0)
            exact_budget.setdefault("assessed_candidates", 0)
        elif stage is PilotStage.EXACT_ASSEMBLY:
            snapshot, _ = store.load_generation(source_generation)
            body, assembly = assemble_exact_section(
                snapshot,
                source_generation=source_generation + 1,
                budget=AssemblyBudget(
                    max_sentences=18,
                    max_citations=30,
                    max_body_utf8_bytes=64 * 1024,
                ),
            )
            payload = PilotExactAssemblyControlPayload.from_assembly(
                body, assembly
            ).model_dump(mode="json")
        elif stage is PilotStage.VALIDATION_REPORT:
            snapshot, _ = store.load_generation(source_generation)
            report = PilotValidationReport(
                run_id=manifest.run_id,
                source_generation=source_generation,
                repository_hash=snapshot.canonical_hash(),
                structural_validation=ValidationStatus.PASSED,
                locator_verification=ValidationStatus.NOT_EXECUTED,
                semantic_audits_executed=ValidationStatus.NOT_EXECUTED,
                citation_authorization=ValidationStatus.NOT_EXECUTED,
                exact_assembly=ValidationStatus.NOT_EXECUTED,
                artifact_integrity=ValidationStatus.PASSED,
                human_review_status="NOT_PERFORMED",
            )
            diagnostics = {
                "structural_validation": (),
                "locator_verification": ("No canonical locators were present.",),
                "semantic_audits_executed": ("No semantic audits were present.",),
                "citation_authorization": ("No citation bindings were present.",),
                "exact_assembly": ("No exact assembly was supplied.",),
                "artifact_integrity": (),
            }
            payload = PilotValidationControlPayload.from_report(
                report=report, diagnostics=diagnostics
            ).model_dump(mode="json")
    if payload is None:
        payload = {"fixture": stage.value}
    return record_pilot_control_event(
        store.project_root,
        registration,
        manifest,
        ordinal=ordinal,
        stage=stage,
        status=status,
        artifact_payload=payload,
        input_hashes={"fixture_input": hash_text(f"input:{ordinal}")},
        budget_consumed=exact_budget,
        previous_stage=previous,
        reason=reason,
        crash_at=crash_at,
    )


def test_stage_requires_registered_setup_and_exact_domain_artifact(
    tmp_path: Path,
) -> None:
    store, manifest, registration = _register_run(tmp_path / "review")
    provenance = _provenance(
        source=1,
        task_id="TASK0001",
        task_type=TaskType.PARSE_DEEP_RESEARCH,
    )
    receipt = _receipt(provenance=provenance, committed=2)
    materializer = _materializer(
        store=store,
        manifest=manifest,
        registration=registration,
        provenance=provenance,
        ordinal=1,
        stage=PilotStage.DISCOVERY,
    )
    commit = _commit_with_stage(store, materializer, receipt)
    prepared = materializer.prepared_for(receipt)

    assert commit.generation == 2
    artifact = load_pilot_stage_artifact(
        store.project_root,
        prepared.reference,
        expected_registration=registration,
        expected_run_manifest_hash=manifest.manifest_hash,
    )
    assert artifact.stage_record.task_ids == ("TASK0001",)
    assert artifact.task_provenance == PilotTaskProvenanceWitness.from_private(
        provenance
    )
    serialized = artifact.model_dump(mode="json")
    assert "source_path" not in str(serialized)
    assert "/synthetic/" not in str(serialized)
    assert artifact.accepted_receipt == receipt


def test_two_events_form_an_existing_consecutive_chain(tmp_path: Path) -> None:
    store, manifest, registration = _register_run(tmp_path / "review")
    first_provenance = _provenance(
        source=1,
        task_id="TASK0001",
        task_type=TaskType.PARSE_DEEP_RESEARCH,
    )
    first_receipt = _receipt(provenance=first_provenance, committed=2)
    first_materializer = _materializer(
        store=store,
        manifest=manifest,
        registration=registration,
        provenance=first_provenance,
        ordinal=1,
        stage=PilotStage.DISCOVERY,
    )
    _commit_with_stage(store, first_materializer, first_receipt)
    first = first_materializer.prepared_for(first_receipt)

    second_provenance = _provenance(
        source=2,
        task_id="TASK0002",
        task_type=TaskType.CORPUS_CHALLENGER,
    )
    second_receipt = _receipt(provenance=second_provenance, committed=3)
    second_materializer = _materializer(
        store=store,
        manifest=manifest,
        registration=registration,
        provenance=second_provenance,
        ordinal=2,
        stage=PilotStage.CORPUS_CHALLENGE,
        previous=first.reference,
    )
    _commit_with_stage(store, second_materializer, second_receipt)
    second = second_materializer.prepared_for(second_receipt)

    loaded = load_pilot_stage_artifact(
        store.project_root,
        second.reference,
        expected_registration=registration,
        expected_previous_stage=first.reference,
    )
    assert loaded.stage_record.ordinal == 2
    assert loaded.stage_record.previous_stage == first.reference.predecessor()


def test_empty_repository_cannot_attest_an_unregistered_run(tmp_path: Path) -> None:
    store = GenerationStore(tmp_path / "empty")
    store.initialize()
    fake = PilotRunRegistrationReference(
        run_id="RUN-synthetic-journal",
        owner_generation=1,
        artifact_hash=_hash("f"),
        relative_path=(
            "pilot/runs/RUN-synthetic-journal/registration-"
            + "f" * 64
            + ".json"
        ),
    )
    with pytest.raises(PilotJournalError, match="could not be verified"):
        PilotStageMaterializer(
            project_root=store.project_root,
            registration=fake,
            run_manifest=_manifest(),
            task_provenance=_provenance(
                source=1,
                task_id="TASK0001",
                task_type=TaskType.PARSE_DEEP_RESEARCH,
            ),
            engine_plan_hash=hash_text("single synthetic engine plan"),
            ordinal=1,
            stage=PilotStage.DISCOVERY,
            input_hashes={},
        )


def test_stage_task_mismatch_and_forged_receipt_fail_closed(tmp_path: Path) -> None:
    store, manifest, registration = _register_run(tmp_path / "review")
    provenance = _provenance(
        source=1,
        task_id="TASK0001",
        task_type=TaskType.PARSE_DEEP_RESEARCH,
    )
    receipt = _receipt(provenance=provenance, committed=2)
    wrong_stage = _materializer(
        store=store,
        manifest=manifest,
        registration=registration,
        provenance=provenance,
        ordinal=1,
        stage=PilotStage.EXACT_ASSEMBLY,
    )
    with pytest.raises(PilotJournalError, match="bindings are invalid"):
        wrong_stage.prepared_for(receipt)

    forged = receipt.model_copy(update={"proposal_hash": _hash("f")})
    materializer = _materializer(
        store=store,
        manifest=manifest,
        registration=registration,
        provenance=provenance,
        ordinal=1,
        stage=PilotStage.DISCOVERY,
    )
    with pytest.raises(PilotJournalError, match="pre-commit validation"):
        _commit_with_stage(store, materializer, forged)
    assert store.current_generation() == 1

    forged_input_key = _hash("e")
    forged_identity = receipt.model_copy(
        update={
            "input_identity_key": forged_input_key,
            "semantic_task_key": compute_semantic_task_key(
                input_identity_key=forged_input_key,
                semantic_fingerprint=receipt.semantic_fingerprint,
            ),
        }
    )
    with pytest.raises(PilotJournalError, match="pre-commit validation"):
        _commit_with_stage(store, materializer, forged_identity)
    assert store.current_generation() == 1

    reversed_resources = provenance.model_copy(
        update={"resources": tuple(reversed(provenance.resources))}
    )
    reversed_materializer = _materializer(
        store=store,
        manifest=manifest,
        registration=registration,
        provenance=reversed_resources,
        ordinal=1,
        stage=PilotStage.DISCOVERY,
    )
    reversed_receipt = _receipt(provenance=reversed_resources, committed=2)
    with pytest.raises(PilotJournalError, match="bindings are invalid"):
        reversed_materializer.prepared_for(reversed_receipt)


@pytest.mark.parametrize("case", ["missing", "wrong_hash", "unlisted"])
def test_stage_domain_declarations_are_verified_before_publication(
    tmp_path: Path,
    case: str,
) -> None:
    store, manifest, registration = _register_run(tmp_path / case)
    provenance = _provenance(
        source=1,
        task_id="TASK0001",
        task_type=TaskType.PARSE_DEEP_RESEARCH,
    )
    receipt = _receipt(provenance=provenance, committed=2)
    domain_path = "domain/result.json"
    domain_content = b"{}\n"

    def inner(writer, _generation, _payload, _receipt_value):
        if case != "missing":
            writer.write_bytes(domain_path, domain_content)

    declared = {
        domain_path: (
            hash_text("wrong") if case == "wrong_hash" else hash_bytes(domain_content)
        )
    }
    if case == "unlisted":
        declared = {}
    materializer = PilotStageMaterializer(
        project_root=store.project_root,
        registration=registration,
        run_manifest=manifest,
        task_provenance=provenance,
        engine_plan_hash=manifest.engine_role_plan[
            provenance.task_type
        ].safe_configuration_hash,
        ordinal=1,
        stage=PilotStage.DISCOVERY,
        input_hashes={},
        artifact_hashes=declared,
        inner=inner,
    )

    with pytest.raises(PilotJournalError, match="declarations differ"):
        _commit_with_stage(store, materializer, receipt)
    assert store.current_generation() == 1


def test_fabricated_predecessor_and_budget_overrun_are_rejected(tmp_path: Path) -> None:
    store, manifest, registration = _register_run(tmp_path / "review")
    fake_previous = {
        "run_id": manifest.run_id,
        "ordinal": 1,
        "stage": PilotStage.DISCOVERY,
        "owner_generation": 2,
        "stage_record_hash": _hash("c"),
        "artifact_hash": _hash("d"),
        "relative_path": (
            f"pilot/runs/{manifest.run_id}/stages/"
            f"0001-discovery-{'d' * 64}.json"
        ),
    }
    from vibereview.runtime.pilot_journal import PilotStageArtifactReference

    with pytest.raises(PilotJournalError):
        PilotStageMaterializer(
            project_root=store.project_root,
            registration=registration,
            run_manifest=manifest,
            task_provenance=_provenance(
                source=2,
                task_id="TASK0002",
                task_type=TaskType.CORPUS_CHALLENGER,
            ),
            engine_plan_hash=hash_text("single synthetic engine plan"),
            ordinal=2,
            stage=PilotStage.CORPUS_CHALLENGE,
            input_hashes={},
            previous_stage=PilotStageArtifactReference.model_validate(fake_previous),
        )

    provenance = _provenance(
        source=1,
        task_id="TASK0001",
        task_type=TaskType.PARSE_DEEP_RESEARCH,
    )
    receipt = _receipt(provenance=provenance, committed=2)
    over_budget = _materializer(
        store=store,
        manifest=manifest,
        registration=registration,
        provenance=provenance,
        ordinal=1,
        stage=PilotStage.DISCOVERY,
    )
    over_budget.budget_consumed["semantic_engine_invocations"] = 129
    with pytest.raises(PilotJournalError, match="bindings are invalid"):
        over_budget.prepared_for(receipt)


@pytest.mark.parametrize(
    "stage",
    [
        PilotStage.PREREQUISITES,
        PilotStage.RETRIEVAL,
        PilotStage.EXACT_ASSEMBLY,
        PilotStage.VALIDATION_REPORT,
    ],
)
def test_completed_control_stage_is_an_atomic_noop_generation(
    tmp_path: Path,
    stage: PilotStage,
    bundle_factory,
) -> None:
    store, manifest, registration = _register_run(
        tmp_path / stage.value,
        initial_snapshot=(
            _five_paper_complete_snapshot(bundle_factory)
            if stage is PilotStage.EXACT_ASSEMBLY
            else None
        ),
    )
    before_snapshot, before_registry = store.load_generation(1)

    result = _record_control(
        store,
        manifest,
        registration,
        ordinal=1,
        stage=stage,
        status=PilotStageStatus.COMPLETED,
        budget={"elapsed_seconds": 3},
    )

    assert result.generation == 2
    assert result.commit_performed is True
    assert result.reused_generation is None
    after_snapshot, after_registry = store.load_generation(2)
    assert after_snapshot == before_snapshot
    assert after_registry == before_registry
    loaded = load_pilot_stage_artifact(store.project_root, result.reference)
    assert loaded.accepted_receipt is None
    assert loaded.task_provenance is None
    assert loaded.stage_record.status is PilotStageStatus.COMPLETED
    assert loaded.stage_record.task_ids == ()
    _, _, selected = store.load_generation_auxiliary(
        2, {result.control_artifact_relative_path}
    )
    content = selected[result.control_artifact_relative_path]
    control = PilotControlArtifact.model_validate_json(content)
    assert content == canonical_json_bytes(control.model_dump(mode="json")) + b"\n"
    if stage is PilotStage.RETRIEVAL:
        assert PilotRetrievalControlPayload.model_validate(control.payload).query_count == 0
    elif stage is PilotStage.EXACT_ASSEMBLY:
        exact = PilotExactAssemblyControlPayload.model_validate(control.payload)
        assert exact.assembly.source_generation == 2
        assert exact.section_body_utf8
    elif stage is PilotStage.VALIDATION_REPORT:
        validation = PilotValidationControlPayload.model_validate(control.payload)
        assert validation.report.source_generation == 1
    else:
        assert control.payload == {"fixture": stage.value}


@pytest.mark.parametrize("status", [PilotStageStatus.BLOCKED, PilotStageStatus.FAILED])
def test_blocked_and_failed_control_events_are_valid_for_semantic_stages(
    tmp_path: Path,
    status: PilotStageStatus,
) -> None:
    store, manifest, registration = _register_run(tmp_path / status.value.lower())
    result = _record_control(
        store,
        manifest,
        registration,
        ordinal=1,
        stage=PilotStage.SENTENCE_AUDIT,
        status=status,
        reason=f"synthetic {status.value.lower()} reason",
        payload={"diagnostic": status.value},
    )

    loaded = load_pilot_stage_artifact(store.project_root, result.reference)
    assert loaded.stage_record.status is status
    assert loaded.stage_record.reason == f"synthetic {status.value.lower()} reason"
    assert loaded.stage_record.artifact_hashes == {
        result.control_artifact_relative_path: result.control_artifact_hash
    }


def test_task_stage_resolves_dynamic_artifact_hashes_after_inner_write(
    tmp_path: Path,
) -> None:
    store, manifest, registration = _register_run(tmp_path / "review")
    provenance = _provenance(
        source=1,
        task_id="TASK0001",
        task_type=TaskType.PARSE_DEEP_RESEARCH,
    )
    receipt = _receipt(provenance=provenance, committed=2)
    domain_path = "domain/dynamic-result.json"
    domain_content = b'{"dynamic":true}\n'
    call_order: list[str] = []

    def inner(writer, _generation, _payload, _receipt_value):
        writer.write_bytes(domain_path, domain_content)
        call_order.append("inner")

    def artifact_hash_provider(receipt_value):
        assert receipt_value == receipt
        assert call_order and call_order[0] == "inner"
        call_order.append("provider")
        return {domain_path: hash_bytes(domain_content)}

    materializer = PilotStageMaterializer(
        project_root=store.project_root,
        registration=registration,
        run_manifest=manifest,
        task_provenance=provenance,
        engine_plan_hash=manifest.engine_role_plan[
            provenance.task_type
        ].safe_configuration_hash,
        ordinal=1,
        stage=PilotStage.DISCOVERY,
        input_hashes={"source": hash_text("source")},
        artifact_hash_provider=artifact_hash_provider,
        inner=inner,
    )
    _commit_with_stage(store, materializer, receipt)
    prepared = materializer.prepared_for(receipt)
    loaded = load_pilot_stage_artifact(store.project_root, prepared.reference)

    assert call_order[:2] == ["inner", "provider"]
    assert loaded.stage_record.artifact_hashes == {
        domain_path: hash_bytes(domain_content)
    }
    with pytest.raises(PilotJournalError, match="mutually exclusive"):
        PilotStageMaterializer(
            project_root=store.project_root,
            registration=registration,
            run_manifest=manifest,
            task_provenance=provenance,
            engine_plan_hash=manifest.engine_role_plan[
                provenance.task_type
            ].safe_configuration_hash,
            ordinal=1,
            stage=PilotStage.DISCOVERY,
            input_hashes={},
            artifact_hashes={domain_path: hash_bytes(domain_content)},
            artifact_hash_provider=artifact_hash_provider,
        )


def test_control_chain_is_cumulative_and_old_exact_event_reuses(
    tmp_path: Path,
) -> None:
    store, manifest, registration = _register_run(tmp_path / "review")
    first = _record_control(
        store,
        manifest,
        registration,
        ordinal=1,
        stage=PilotStage.PREREQUISITES,
        status=PilotStageStatus.COMPLETED,
        budget={"elapsed_seconds": 1},
        payload={"checks": ["registered", "bounded"]},
    )
    second = _record_control(
        store,
        manifest,
        registration,
        ordinal=2,
        stage=PilotStage.DISCOVERY,
        status=PilotStageStatus.BLOCKED,
        previous=first.reference,
        reason="no synthetic discovery input",
        budget={"elapsed_seconds": 2},
        payload={"blocker": "missing_input"},
    )

    loaded_second = load_pilot_stage_artifact(
        store.project_root,
        second.reference,
        expected_previous_stage=first.reference,
    )
    assert loaded_second.stage_record.previous_stage == first.reference.predecessor()
    reused = _record_control(
        store,
        manifest,
        registration,
        ordinal=1,
        stage=PilotStage.PREREQUISITES,
        status=PilotStageStatus.COMPLETED,
        budget={"elapsed_seconds": 1},
        payload={"checks": ["registered", "bounded"]},
    )
    assert reused.generation == 3
    assert reused.commit_performed is False
    assert reused.reused_generation == 2
    assert reused.reference == first.reference
    assert store.current_generation() == 3

    with pytest.raises(StaleSnapshotError, match="stale"):
        _record_control(
            store,
            manifest,
            registration,
            ordinal=1,
            stage=PilotStage.PREREQUISITES,
            status=PilotStageStatus.COMPLETED,
            budget={"elapsed_seconds": 1},
            payload={"checks": ["changed"]},
        )


def test_control_chain_rejects_regressed_cumulative_budget_before_commit(
    tmp_path: Path,
) -> None:
    store, manifest, registration = _register_run(tmp_path / "review")
    first = _record_control(
        store,
        manifest,
        registration,
        ordinal=1,
        stage=PilotStage.PREREQUISITES,
        status=PilotStageStatus.COMPLETED,
        budget={"raw_candidates": 4, "elapsed_seconds": 2},
    )
    before = store.current_generation()
    with pytest.raises(PilotJournalError, match="regressed: raw_candidates"):
        _record_control(
            store,
            manifest,
            registration,
            ordinal=2,
            stage=PilotStage.RETRIEVAL,
            status=PilotStageStatus.COMPLETED,
            previous=first.reference,
            budget={"raw_candidates": 3, "elapsed_seconds": 2},
        )
    assert store.current_generation() == before


def test_new_control_event_rejects_stale_current_without_exact_reuse(
    tmp_path: Path,
) -> None:
    store, manifest, registration = _register_run(tmp_path / "review")
    store.commit(
        base_generation=1,
        dependencies={},
        promotion=lambda snapshot, registry: PromotionPayload(snapshot, registry, {}),
    )

    with pytest.raises(StaleSnapshotError, match="stale"):
        _record_control(
            store,
            manifest,
            registration,
            ordinal=1,
            stage=PilotStage.PREREQUISITES,
            status=PilotStageStatus.COMPLETED,
        )
    assert store.current_generation() == 2
    with pytest.raises(PilotJournalError, match="could not be authenticated"):
        discover_current_pilot_head(store.project_root, registration, manifest)


@pytest.mark.parametrize(
    "crash_at",
    list(CrashPoint),
)
def test_control_event_crash_recovery_commits_or_reuses_exactly(
    tmp_path: Path,
    crash_at: CrashPoint,
) -> None:
    store, manifest, registration = _register_run(tmp_path / crash_at.value)
    with pytest.raises(InjectedCrash):
        _record_control(
            store,
            manifest,
            registration,
            ordinal=1,
            stage=PilotStage.PREREQUISITES,
            status=PilotStageStatus.COMPLETED,
            payload={"crash_case": crash_at.value},
            crash_at=crash_at,
        )

    expected_current = 2 if crash_at is CrashPoint.AFTER_CURRENT else 1
    assert store.current_generation() == expected_current
    recovered = _record_control(
        store,
        manifest,
        registration,
        ordinal=1,
        stage=PilotStage.PREREQUISITES,
        status=PilotStageStatus.COMPLETED,
        payload={"crash_case": crash_at.value},
    )
    assert recovered.generation == 2
    assert recovered.commit_performed is (crash_at is not CrashPoint.AFTER_CURRENT)
    assert recovered.reused_generation == (
        2 if crash_at is CrashPoint.AFTER_CURRENT else None
    )
    load_pilot_stage_artifact(store.project_root, recovered.reference)


@pytest.mark.parametrize("target", ["control", "journal"])
def test_control_or_journal_byte_tamper_fails_closed(
    tmp_path: Path,
    target: str,
) -> None:
    store, manifest, registration = _register_run(tmp_path / target)
    result = _record_control(
        store,
        manifest,
        registration,
        ordinal=1,
        stage=PilotStage.PREREQUISITES,
        status=PilotStageStatus.COMPLETED,
    )
    relative_path = (
        result.control_artifact_relative_path
        if target == "control"
        else result.reference.relative_path
    )
    path = store.generation_path(2) / "auxiliary" / relative_path
    path.chmod(0o600)
    path.write_bytes(b"{}\n")

    with pytest.raises(PilotJournalError, match="auxiliary hash mismatch"):
        load_pilot_stage_artifact(store.project_root, result.reference)


def test_control_status_and_canonical_byte_bounds_fail_before_commit(
    tmp_path: Path,
) -> None:
    store, manifest, registration = _register_run(tmp_path / "review")
    with pytest.raises(PilotJournalError, match="bindings are invalid"):
        _record_control(
            store,
            manifest,
            registration,
            ordinal=1,
            stage=PilotStage.DISCOVERY,
            status=PilotStageStatus.COMPLETED,
        )
    with pytest.raises(PilotJournalError, match="requires a reason"):
        _record_control(
            store,
            manifest,
            registration,
            ordinal=1,
            stage=PilotStage.DISCOVERY,
            status=PilotStageStatus.FAILED,
        )
    with pytest.raises(PilotJournalError, match="non-finite"):
        _record_control(
            store,
            manifest,
            registration,
            ordinal=1,
            stage=PilotStage.PREREQUISITES,
            status=PilotStageStatus.COMPLETED,
            payload={"invalid": float("nan")},
        )
    with pytest.raises(PilotJournalError, match="exceeds its byte limit"):
        _record_control(
            store,
            manifest,
            registration,
            ordinal=1,
            stage=PilotStage.PREREQUISITES,
            status=PilotStageStatus.COMPLETED,
            payload={"too_large": "x" * (8 * 1024 * 1024)},
        )
    assert store.current_generation() == 1


def test_current_head_index_recovers_after_lost_control_response(
    tmp_path: Path,
) -> None:
    store, manifest, registration = _register_run(tmp_path / "restart")
    assert discover_current_pilot_head(
        store.project_root, registration, manifest
    ) is None

    with pytest.raises(InjectedCrash):
        _record_control(
            store,
            manifest,
            registration,
            ordinal=1,
            stage=PilotStage.PREREQUISITES,
            status=PilotStageStatus.COMPLETED,
            payload={"crash_case": CrashPoint.AFTER_CURRENT.value},
            crash_at=CrashPoint.AFTER_CURRENT,
        )

    discovered = discover_current_pilot_head(
        store.project_root, registration, manifest
    )
    assert discovered is not None
    assert discovered.owner_generation == 2
    assert discovered.ordinal == 1
    restarted = _record_control(
        store,
        manifest,
        registration,
        ordinal=1,
        stage=PilotStage.PREREQUISITES,
        status=PilotStageStatus.COMPLETED,
        payload={"crash_case": CrashPoint.AFTER_CURRENT.value},
    )
    assert restarted.commit_performed is False
    assert restarted.reference == discovered


def test_current_head_index_advances_with_semantic_event_and_hides_source_paths(
    tmp_path: Path,
) -> None:
    store, manifest, registration = _register_run(tmp_path / "semantic-head")
    prerequisites = _record_control(
        store,
        manifest,
        registration,
        ordinal=1,
        stage=PilotStage.PREREQUISITES,
        status=PilotStageStatus.COMPLETED,
    )
    provenance = _provenance(
        source=2,
        task_id="TASK0001",
        task_type=TaskType.PARSE_DEEP_RESEARCH,
    )
    receipt = _receipt(provenance=provenance, committed=3)
    materializer = _materializer(
        store=store,
        manifest=manifest,
        registration=registration,
        provenance=provenance,
        ordinal=2,
        stage=PilotStage.DISCOVERY,
        previous=prerequisites.reference,
    )
    _commit_with_stage(store, materializer, receipt)

    discovered = discover_current_pilot_head(
        store.project_root, registration, manifest
    )
    assert discovered == materializer.prepared_for(receipt).reference
    index_path = (
        store.generation_path(3)
        / "auxiliary"
        / f"pilot/runs/{manifest.run_id}/{PILOT_CURRENT_HEAD_FILENAME}"
    )
    index_text = index_path.read_text(encoding="utf-8")
    assert "source_path" not in index_text
    stage_text = (
        store.generation_path(3)
        / "auxiliary"
        / discovered.relative_path
    ).read_text(encoding="utf-8")
    assert "source_path" not in stage_text
    assert "/synthetic/" not in stage_text


def test_current_head_index_tamper_fails_closed(tmp_path: Path) -> None:
    store, manifest, registration = _register_run(tmp_path / "head-tamper")
    _record_control(
        store,
        manifest,
        registration,
        ordinal=1,
        stage=PilotStage.PREREQUISITES,
        status=PilotStageStatus.COMPLETED,
    )
    path = (
        store.generation_path(2)
        / "auxiliary"
        / f"pilot/runs/{manifest.run_id}/{PILOT_CURRENT_HEAD_FILENAME}"
    )
    path.chmod(0o600)
    path.write_bytes(b"{}\n")
    with pytest.raises(PilotJournalError, match="could not be verified"):
        discover_current_pilot_head(store.project_root, registration, manifest)


def test_precommit_validators_abort_control_and_semantic_publication(
    tmp_path: Path,
) -> None:
    control_store, control_manifest, control_registration = _register_run(
        tmp_path / "control"
    )
    seen_controls: list[tuple[object, object, RepositorySnapshot]] = []

    def reject_control(event, control, snapshot):
        seen_controls.append((event, control, snapshot))
        raise ValueError("synthetic prospective rejection")

    with pytest.raises(PilotJournalError, match="prospective control"):
        record_pilot_control_event(
            control_store.project_root,
            control_registration,
            control_manifest,
            ordinal=1,
            stage=PilotStage.PREREQUISITES,
            status=PilotStageStatus.COMPLETED,
            artifact_payload={"checks": ["registered"]},
            precommit_validator=reject_control,
        )
    assert len(seen_controls) == 1
    assert seen_controls[0][1] is not None
    assert control_store.current_generation() == 1
    assert discover_current_pilot_head(
        control_store.project_root, control_registration, control_manifest
    ) is None

    semantic_store, semantic_manifest, semantic_registration = _register_run(
        tmp_path / "semantic"
    )
    prerequisites = _record_control(
        semantic_store,
        semantic_manifest,
        semantic_registration,
        ordinal=1,
        stage=PilotStage.PREREQUISITES,
        status=PilotStageStatus.COMPLETED,
    )
    provenance = _provenance(
        source=2,
        task_id="TASK0001",
        task_type=TaskType.PARSE_DEEP_RESEARCH,
    )
    receipt = _receipt(provenance=provenance, committed=3)
    seen_semantic: list[tuple[object, object, RepositorySnapshot]] = []

    def reject_semantic(event, control, snapshot):
        seen_semantic.append((event, control, snapshot))
        raise ValueError("synthetic prospective rejection")

    materializer = PilotStageMaterializer(
        project_root=semantic_store.project_root,
        registration=semantic_registration,
        run_manifest=semantic_manifest,
        task_provenance=provenance,
        engine_plan_hash=semantic_manifest.engine_role_plan[
            provenance.task_type
        ].safe_configuration_hash,
        ordinal=2,
        stage=PilotStage.DISCOVERY,
        input_hashes={"source": hash_text("source")},
        artifact_hashes={"domain/result.json": hash_bytes(b"{}\n")},
        budget_consumed={"semantic_engine_invocations": 1},
        previous_stage=prerequisites.reference,
        inner=lambda writer, _generation, _payload, _receipt_value: (
            writer.write_bytes("domain/result.json", b"{}\n")
        ),
        precommit_validator=reject_semantic,
    )
    with pytest.raises(PilotJournalError, match="prospective semantic event"):
        _commit_with_stage(semantic_store, materializer, receipt)
    assert len(seen_semantic) == 1
    assert seen_semantic[0][1] is None
    assert semantic_store.current_generation() == 2
    assert discover_current_pilot_head(
        semantic_store.project_root, semantic_registration, semantic_manifest
    ) == prerequisites.reference
