from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from vibereview.runtime.drafts import (
    MAX_DRAFT_DEPENDENCIES,
    DraftArtifactError,
    DraftArtifactKind,
    DraftArtifactStore,
    build_draft_artifact,
    draft_resource_path,
    load_draft_artifact,
    load_draft_artifact_for_receipt,
    make_draft_artifact_materializer,
)
from vibereview.runtime.dto import (
    MAX_DRAFT_TEXT_UTF8_BYTES,
    PropositionProposalBundle,
    RenderedSentenceProposalBundle,
    RevisedClaimProposal,
)
from vibereview.runtime.hashing import hash_json, hash_text
from vibereview.runtime.receipts import compute_semantic_task_key
from vibereview.runtime.records import (
    AppliedTaskReceipt,
    ResourceProvenance,
    ResourceSourceDependency,
    TaskProvenance,
    TaskSemanticFingerprint,
    TaskType,
    TransitionDecision,
)
from vibereview.runtime.repository import GenerationStore, PromotionPayload


_TASK_BY_KIND = {
    DraftArtifactKind.REVISED_CLAIM: TaskType.REVISE_CLAIM,
    DraftArtifactKind.PROPOSITION_DRAFT: TaskType.GENERATE_PROPOSITIONS,
    DraftArtifactKind.RENDERED_SENTENCE_DRAFT: TaskType.RENDER_PROSE,
}

_TASK_ID_BY_KIND = {
    DraftArtifactKind.REVISED_CLAIM: "TASK0101",
    DraftArtifactKind.PROPOSITION_DRAFT: "TASK0102",
    DraftArtifactKind.RENDERED_SENTENCE_DRAFT: "TASK0103",
}


def _proposal(artifact_kind: DraftArtifactKind):
    if artifact_kind is DraftArtifactKind.REVISED_CLAIM:
        return RevisedClaimProposal(
            claim_ref="C0001",
            final_claim="A bounded, accepted revised claim.",
        )
    if artifact_kind is DraftArtifactKind.PROPOSITION_DRAFT:
        return PropositionProposalBundle(
            propositions=[
                {
                    "local_ref": "proposition_one",
                    "text": "This sentence provides review context.",
                    "content_class": "Rhetorical",
                    "claim_refs": [],
                    "citation_bindings": [],
                    "corpus_fact_refs": [],
                    "process_fact_refs": [],
                }
            ]
        )
    return RenderedSentenceProposalBundle(
        sentences=[
            {
                "local_ref": "sentence_one",
                "text": "This is exact rendered prose.",
                "source_proposition_refs": ["PR0001"],
            }
        ]
    )


def _fingerprint() -> TaskSemanticFingerprint:
    components = {
        "validator_fingerprint": hash_text("validator"),
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
    artifact_kind: DraftArtifactKind,
    *,
    dependencies: dict[str, str] | None = None,
    resources: tuple[ResourceProvenance, ...] = (),
) -> TaskProvenance:
    return TaskProvenance(
        task_id=_TASK_ID_BY_KIND[artifact_kind],
        task_type=_TASK_BY_KIND[artifact_kind],
        task_spec_version="1.0",
        prompt_version="1.0",
        base_generation=0,
        dependencies=dependencies or {},
        instructions_hash=hash_text("instructions"),
        input_snapshot_hash=hash_text("input snapshot"),
        engine_input_hash=hash_text("engine input"),
        input_schema_hash=hash_text("input schema"),
        proposal_schema_hash=hash_text("proposal schema"),
        expected_bundle_manifest_hash=hash_text("bundle manifest"),
        expected_immutable_files={
            "instructions.md": hash_text("instructions file"),
        },
        resources=resources,
    )


def _receipt(
    artifact_kind: DraftArtifactKind,
    proposal,
    *,
    committed_generation: int = 1,
) -> AppliedTaskReceipt:
    payload = proposal.model_dump(mode="json")
    fingerprint = _fingerprint()
    input_identity_key = hash_text(f"input:{artifact_kind.value}")
    return AppliedTaskReceipt(
        input_identity_key=input_identity_key,
        semantic_task_key=compute_semantic_task_key(
            input_identity_key=input_identity_key,
            semantic_fingerprint=fingerprint,
        ),
        task_type=_TASK_BY_KIND[artifact_kind],
        task_spec_version="1.0",
        proposal_hash=hash_json(payload),
        proposal_payload=payload,
        semantic_fingerprint=fingerprint,
        source_generation=0,
        committed_generation=committed_generation,
        canonical_objects=(),
        local_ref_map={},
        recorded_transition=TransitionDecision(
            scientific_disposition="VALID",
            canonicalized=False,
            downstream_eligible=True,
        ),
        engine="synthetic",
        engine_version="1.0",
        accepted_attempt_id=f"{_TASK_ID_BY_KIND[artifact_kind]}/01-synthetic",
    )


def _commit_draft(
    project_root: Path,
    artifact_kind: DraftArtifactKind,
):
    store = GenerationStore(project_root)
    store.initialize()
    proposal = _proposal(artifact_kind)
    provenance = _provenance(artifact_kind)
    receipt_box: dict[str, AppliedTaskReceipt] = {}
    coupled_materializer = make_draft_artifact_materializer(
        artifact_kind=artifact_kind,
        task_provenance=provenance,
        proposal=proposal,
    )

    def receipt_factory(next_generation, _before_snapshot, _payload):
        receipt = _receipt(
            artifact_kind,
            proposal,
            committed_generation=next_generation,
        )
        receipt_box["receipt"] = receipt
        return receipt

    def staging_materializer(writer, next_generation, payload):
        coupled_materializer(
            writer,
            next_generation,
            payload,
            receipt_box["receipt"],
        )

    result = store.commit(
        base_generation=0,
        dependencies={},
        promotion=lambda snapshot, registry: PromotionPayload(
            snapshot, registry, {}
        ),
        receipt_factory=receipt_factory,
        staging_materializer=staging_materializer,
    )
    assert result.receipt is not None
    prepared = build_draft_artifact(
        artifact_kind=artifact_kind,
        task_provenance=provenance,
        acceptance_receipt=result.receipt,
        proposal=proposal,
    )
    return store, proposal, provenance, result.receipt, prepared


@pytest.mark.parametrize("artifact_kind", tuple(DraftArtifactKind))
def test_all_draft_kinds_cross_the_generation_transaction_and_load_exactly(
    tmp_path: Path,
    artifact_kind: DraftArtifactKind,
) -> None:
    project_root = tmp_path / "review"
    store, proposal, provenance, receipt, prepared = _commit_draft(
        project_root, artifact_kind
    )

    assert store.current_generation() == 1
    snapshot, registry = store.load_generation(1)
    assert snapshot == store.load_generation(0)[0]
    assert registry == store.load_generation(0)[1]
    assert store.load_receipts(1)[-1] == receipt

    loaded = DraftArtifactStore(project_root).load(
        prepared.reference,
        expected_task_provenance=provenance,
        expected_receipt=receipt,
    )
    assert loaded == prepared.artifact
    reused, reused_reference = load_draft_artifact_for_receipt(
        project_root, receipt
    )
    assert reused == loaded
    assert reused_reference == prepared.reference
    assert loaded.proposal_payload == proposal.model_dump(mode="json")
    assert loaded.proposal_hash == hash_json(loaded.proposal_payload)
    assert prepared.reference.relative_path.startswith(
        f"drafts/v1/{artifact_kind.value}/"
    )
    assert prepared.reference.relative_path.endswith(
        prepared.reference.artifact_hash.removeprefix("sha256:") + ".json"
    )

    artifact_path = (
        project_root
        / "state/generations/000001/auxiliary"
        / prepared.reference.relative_path
    )
    for local_ref in prepared.reference.draft_local_refs:
        anchor = prepared.reference.anchor(project_root, local_ref)
        assert anchor.draft_kind is artifact_kind
        assert anchor.draft_owner_generation == 1
        assert anchor.draft_source_generation == 0
        assert anchor.draft_task_id == prepared.reference.task_id
        assert anchor.draft_artifact_hash == prepared.reference.artifact_hash
        assert anchor.draft_artifact_path == artifact_path
        assert anchor.draft_artifact_path.is_absolute()
        assert anchor.draft_local_ref == local_ref
    assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o444
    assert artifact_path.read_bytes() == prepared.content
    assert prepared.content.endswith(b"\n")


def test_content_address_is_deterministic_and_binds_every_runtime_anchor() -> None:
    kind = DraftArtifactKind.REVISED_CLAIM
    proposal = _proposal(kind)
    provenance = _provenance(kind)
    receipt = _receipt(kind, proposal)

    first = build_draft_artifact(
        artifact_kind=kind,
        task_provenance=provenance,
        acceptance_receipt=receipt,
        proposal=proposal,
    )
    second = build_draft_artifact(
        artifact_kind=kind.value,
        task_provenance=provenance,
        acceptance_receipt=receipt,
        proposal=proposal.model_dump(mode="json"),
    )
    assert first == second

    changed_proposal = RevisedClaimProposal(
        claim_ref="C0001", final_claim="A different accepted revision."
    )
    changed = build_draft_artifact(
        artifact_kind=kind,
        task_provenance=provenance,
        acceptance_receipt=_receipt(kind, changed_proposal),
        proposal=changed_proposal,
    )
    assert changed.reference.artifact_hash != first.reference.artifact_hash
    assert changed.reference.relative_path != first.reference.relative_path

    with pytest.raises(DraftArtifactError, match="local reference is not present"):
        first.reference.anchor(Path("/review"), "not_in_the_draft")
    with pytest.raises(DraftArtifactError, match="project root must be absolute"):
        draft_resource_path(Path("relative-review"), first.reference)


def test_dependency_and_resource_hashes_are_exact_provenance_bindings() -> None:
    kind = DraftArtifactKind.REVISED_CLAIM
    resource_hash = hash_text("immutable resource bytes")
    resource = ResourceProvenance(
        resource_id="RES0001",
        logical_name="source.txt",
        media_type="text/plain",
        bundle_relative_path=Path("bundle/input/resources/RES0001.txt"),
        source_path=Path("/authorized/source.txt"),
        source_dependency=ResourceSourceDependency(
            source_hash_at_snapshot=resource_hash
        ),
        snapshot_hash=resource_hash,
        size_bytes=24,
    )
    dependencies = {
        "CandidateClaim:C0001": hash_text("claim dependency"),
        "ClaimAssessment:C0001": hash_text("assessment dependency"),
    }
    proposal = _proposal(kind)
    prepared = build_draft_artifact(
        artifact_kind=kind,
        task_provenance=_provenance(
            kind, dependencies=dependencies, resources=(resource,)
        ),
        acceptance_receipt=_receipt(kind, proposal),
        proposal=proposal,
    )

    assert prepared.artifact.dependency_hashes == dependencies
    assert prepared.artifact.resource_hashes == {"RES0001": resource_hash}
    assert prepared.artifact.task_provenance_hash == hash_json(
        prepared.artifact.task_provenance.model_dump(mode="json")
    )


@pytest.mark.parametrize(
    "mutate_receipt",
    [
        lambda receipt: receipt.model_copy(
            update={"proposal_hash": hash_text("wrong proposal")}
        ),
        lambda receipt: receipt.model_copy(
            update={"semantic_task_key": hash_text("wrong semantics")}
        ),
        lambda receipt: receipt.model_copy(update={"committed_generation": 2}),
        lambda receipt: receipt.model_copy(
            update={"local_ref_map": {"draft": "PR0001"}}
        ),
        lambda receipt: receipt.model_copy(
            update={
                "recorded_transition": receipt.recorded_transition.model_copy(
                    update={"canonicalized": True}
                )
            }
        ),
    ],
)
def test_inconsistent_or_already_canonical_receipts_are_rejected(
    mutate_receipt,
) -> None:
    kind = DraftArtifactKind.REVISED_CLAIM
    proposal = _proposal(kind)
    receipt = mutate_receipt(_receipt(kind, proposal))

    with pytest.raises(DraftArtifactError, match="bindings are invalid"):
        build_draft_artifact(
            artifact_kind=kind,
            task_provenance=_provenance(kind),
            acceptance_receipt=receipt,
            proposal=proposal,
        )


def test_draft_store_enforces_schema_and_dependency_budgets() -> None:
    kind = DraftArtifactKind.REVISED_CLAIM
    with pytest.raises(ValueError, match="at most 16384 characters"):
        RevisedClaimProposal(
            claim_ref="C0001",
            final_claim="x" * (MAX_DRAFT_TEXT_UTF8_BYTES + 1),
        )

    dependencies = {
        f"Synthetic:{index:04d}": hash_text(str(index))
        for index in range(MAX_DRAFT_DEPENDENCIES + 1)
    }
    proposal = _proposal(kind)
    with pytest.raises(DraftArtifactError, match="bindings are invalid"):
        build_draft_artifact(
            artifact_kind=kind,
            task_provenance=_provenance(kind, dependencies=dependencies),
            acceptance_receipt=_receipt(kind, proposal),
            proposal=proposal,
        )


def test_materializer_is_no_overwrite_and_failure_never_publishes_generation(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "review"
    store = GenerationStore(project_root)
    store.initialize()
    kind = DraftArtifactKind.REVISED_CLAIM
    proposal = _proposal(kind)
    provenance = _provenance(kind)
    receipt_box: dict[str, AppliedTaskReceipt] = {}
    coupled_materializer = make_draft_artifact_materializer(
        artifact_kind=kind,
        task_provenance=provenance,
        proposal=proposal,
    )

    def receipt_factory(next_generation, _before_snapshot, _payload):
        receipt = _receipt(kind, proposal, committed_generation=next_generation)
        receipt_box["receipt"] = receipt
        return receipt

    def duplicate_materializer(writer, next_generation, payload):
        coupled_materializer(
            writer, next_generation, payload, receipt_box["receipt"]
        )
        coupled_materializer(
            writer, next_generation, payload, receipt_box["receipt"]
        )

    with pytest.raises(FileExistsError):
        store.commit(
            base_generation=0,
            dependencies={},
            promotion=lambda snapshot, registry: PromotionPayload(
                snapshot, registry, {}
            ),
            receipt_factory=receipt_factory,
            staging_materializer=duplicate_materializer,
        )

    assert store.current_generation() == 0
    assert not store.generation_path(1).exists()
    assert store.load_receipts(0) == ()


def test_exact_loader_rejects_path_injection_and_no_follow_tampering(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "review"
    _, _, _, _, prepared = _commit_draft(
        project_root, DraftArtifactKind.REVISED_CLAIM
    )

    forged_reference = prepared.reference.model_copy(
        update={"relative_path": "../../outside.json"}
    )
    with pytest.raises(DraftArtifactError, match="could not be verified"):
        load_draft_artifact(project_root, forged_reference)

    artifact_path = (
        project_root
        / "state/generations/000001/auxiliary"
        / prepared.reference.relative_path
    )
    outside = tmp_path / "outside.json"
    outside.write_bytes(prepared.content)
    os.chmod(artifact_path.parent, 0o700)
    artifact_path.unlink()
    artifact_path.symlink_to(outside)

    with pytest.raises(DraftArtifactError, match="could not be verified"):
        load_draft_artifact(project_root, prepared.reference)


def test_expected_provenance_is_checked_on_an_exact_read(tmp_path: Path) -> None:
    project_root = tmp_path / "review"
    _, _, _, _, prepared = _commit_draft(
        project_root, DraftArtifactKind.REVISED_CLAIM
    )
    wrong_provenance = _provenance(
        DraftArtifactKind.REVISED_CLAIM,
        dependencies={"CandidateClaim:C0001": hash_text("different")},
    )

    with pytest.raises(DraftArtifactError, match="expected task provenance"):
        load_draft_artifact(
            project_root,
            prepared.reference,
            expected_task_provenance=wrong_provenance,
        )
