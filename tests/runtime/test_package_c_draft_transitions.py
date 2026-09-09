from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from vibereview.runtime.artifacts import AssemblyBudget, assemble_exact_section
from vibereview.runtime.draft_transitions import (
    PropositionAuditAdapter,
    PropositionDraftAcceptanceAdapter,
    RenderedSentenceAuditAdapter,
    RenderedSentenceDraftAcceptanceAdapter,
    RevisedClaimDraftAdapter,
    load_proposition_audit_artifact,
    load_sentence_audit_artifact,
)
from vibereview.runtime.drafts import (
    DraftArtifactError,
    DraftArtifactKind,
    load_draft_artifact_for_receipt,
)
from vibereview.runtime.dto import (
    AuditPropositionInvocation,
    AuditRenderedSentenceInvocation,
    GeneratePropositionsInvocation,
    PropositionProposalBundle,
    RenderProseInvocation,
    RenderedSentenceAuditProposal,
    RenderedSentenceProposalBundle,
    RevisedClaimProposal,
    ReviseClaimInvocation,
    SemanticAuditProposal,
)
from vibereview.runtime.engine import MockEngine, MockResponse
from vibereview.runtime.hashing import hash_json, hash_text
from vibereview.runtime.kernel import ProjectRuntime
from vibereview.runtime.records import AttemptOutcome, TaskType
from vibereview.runtime.repository import (
    CrashPoint,
    InjectedCrash,
    PromotionPayload,
    StaleSnapshotError,
)
from vibereview.runtime.specs import TASK_SPECS
from vibereview.runtime.state import RepositorySnapshot


def _without_prose(bundle_factory) -> RepositorySnapshot:
    values = bundle_factory()
    values["proposition_records"] = []
    values["semantic_audits"] = []
    values["rendered_sentences"] = []
    values["rendered_sentence_audits"] = []
    return RepositorySnapshot.model_validate(values)


def _ready_to_render(bundle_factory) -> RepositorySnapshot:
    values = bundle_factory()
    values["rendered_sentences"] = []
    values["rendered_sentence_audits"] = []
    return RepositorySnapshot.model_validate(values)


def _ready_to_render_two(bundle_factory) -> RepositorySnapshot:
    values = bundle_factory()
    values["rendered_sentences"] = []
    values["rendered_sentence_audits"] = []
    values["proposition_records"].append(
        values["proposition_records"][0].model_copy(
            update={"proposition_id": "PR0002"}
        )
    )
    values["semantic_audits"].append(
        values["semantic_audits"][0].model_copy(
            update={"audit_id": "SA0002", "target_id": "PR0002"}
        )
    )
    return RepositorySnapshot.model_validate(values)


def _runtime(tmp_path: Path, snapshot: RepositorySnapshot) -> ProjectRuntime:
    return ProjectRuntime.create(
        tmp_path / "review",
        project_name="package-c-draft-transitions",
        initial_snapshot=snapshot,
    )


def _scientific_proposition_bundle(
    *, paper_ref: str = "P0001"
) -> PropositionProposalBundle:
    return PropositionProposalBundle(
        propositions=[
            {
                "local_ref": "proposition_one",
                "text": "Preheating reduced stress in the tested window.",
                "content_class": "ScientificClaim",
                "claim_refs": ["C0001"],
                "citation_bindings": [
                    {
                        "paper_ref": paper_ref,
                        "claim_ref": "C0001",
                        "claim_paper_evidence_ref": "CPE-C0001-P0001",
                    }
                ],
                "corpus_fact_refs": [],
                "process_fact_refs": [],
            }
        ]
    )


def _run_proposition_draft(runtime: ProjectRuntime):
    proposal = _scientific_proposition_bundle()
    engine = MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])
    invocation = GeneratePropositionsInvocation(
        claim_packet_ids=["C0001"],
        corpus_fact_ids=[],
        process_fact_ids=[],
    )
    result = runtime.run(
        TaskType.GENERATE_PROPOSITIONS,
        invocation,
        engines=[engine],
        promotion_adapter=PropositionDraftAcceptanceAdapter(),
    )
    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert result.generation == 1
    assert result.allocated_ids == {}
    receipt = runtime.store.load_receipts(1)[-1]
    artifact, reference = load_draft_artifact_for_receipt(
        runtime.project_root, receipt
    )
    return proposal, invocation, engine, artifact, reference


def _proposition_audit_invocation(
    runtime,
    reference,
    *,
    corpus_fact_ids: list[str] | None = None,
    process_fact_ids: list[str] | None = None,
):
    anchor = reference.anchor(runtime.project_root, "proposition_one")
    return AuditPropositionInvocation(
        **anchor.model_dump(mode="json"),
        claim_packet_ids=["C0001"],
        corpus_fact_ids=corpus_fact_ids or [],
        process_fact_ids=process_fact_ids or [],
    )


def _run_sentence_draft(runtime: ProjectRuntime):
    exact_text = "Preheating reduced stress [@P0001, p. 3]."
    proposal = RenderedSentenceProposalBundle(
        sentences=[
            {
                "local_ref": "sentence_one",
                "text": exact_text,
                "source_proposition_refs": ["PR0001"],
            }
        ]
    )
    engine = MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])
    result = runtime.run(
        TaskType.RENDER_PROSE,
        RenderProseInvocation(proposition_ids=["PR0001"]),
        engines=[engine],
        promotion_adapter=RenderedSentenceDraftAcceptanceAdapter(),
    )
    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert result.generation == 1
    assert result.allocated_ids == {}
    receipt = runtime.store.load_receipts(1)[-1]
    artifact, reference = load_draft_artifact_for_receipt(
        runtime.project_root, receipt
    )
    return exact_text, proposal, engine, artifact, reference


def _sentence_audit_invocation(runtime, reference):
    anchor = reference.anchor(runtime.project_root, "sentence_one")
    return AuditRenderedSentenceInvocation(
        **anchor.model_dump(mode="json"),
        source_proposition_ids=["PR0001"],
    )


def _assert_audit_artifact_has_only_path_free_provenance(
    runtime: ProjectRuntime, artifact, reference
) -> None:
    artifact_path = (
        runtime.project_root
        / "state"
        / "generations"
        / f"{reference.owner_generation:06d}"
        / "auxiliary"
        / reference.relative_path
    )
    content = artifact_path.read_bytes()
    payload = json.loads(content)

    def strings(value):
        if isinstance(value, dict):
            for key, nested in value.items():
                yield key
                yield from strings(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from strings(nested)
        elif isinstance(value, str):
            yield value

    assert b'"source_path"' not in content
    assert str(runtime.project_root).encode("utf-8") not in content
    assert not [value for value in strings(payload) if Path(value).is_absolute()]
    assert artifact.task_provenance_hash == hash_json(
        artifact.task_provenance.model_dump(mode="json")
    )
    resource = artifact.task_provenance.resources[0]
    assert not hasattr(resource, "source_path")
    assert resource.bundle_relative_path == Path(
        "input/resources/RES0001/content.json"
    )
    assert resource.snapshot_hash == artifact.source_draft.artifact_hash
    assert (
        resource.source_dependency.source_hash_at_snapshot
        == artifact.source_draft.artifact_hash
    )


def test_revised_claim_acceptance_is_receipt_aware_and_noncanonical(
    tmp_path: Path, bundle_factory
) -> None:
    snapshot = RepositorySnapshot.model_validate(bundle_factory())
    runtime = _runtime(tmp_path, snapshot)
    invocation = ReviseClaimInvocation(
        claim_id="C0001",
        current_candidate_claim="Preheating reduces residual stress.",
    )
    proposal = RevisedClaimProposal(
        claim_ref="C0001",
        final_claim="Preheating reduced stress in the tested process window.",
    )
    engine = MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])
    adapter = RevisedClaimDraftAdapter()

    first = runtime.run(
        TaskType.REVISE_CLAIM,
        invocation,
        engines=[engine],
        promotion_adapter=adapter,
    )
    second = runtime.run(
        TaskType.REVISE_CLAIM,
        invocation,
        engines=[engine],
        promotion_adapter=adapter,
    )

    assert first.generation == second.generation == 1
    assert first.allocated_ids == {}
    assert second.receipt_reused is True
    assert engine.calls == 1
    current = runtime.store.load_generation(1)[0]
    assert current == snapshot
    receipt = runtime.store.load_receipts(1)[-1]
    artifact, reference = load_draft_artifact_for_receipt(
        runtime.project_root, receipt
    )
    assert artifact.artifact_kind is DraftArtifactKind.REVISED_CLAIM
    assert reference.draft_local_refs == ("C0001",)


@pytest.mark.parametrize(
    ("class_verdict", "provenance_verdict", "eligible", "human_review"),
    [
        ("CORRECT", "ENTAILED", True, False),
        ("CORRECT", "UNSUPPORTED", False, False),
        ("UNCLEAR", "UNCLEAR", False, True),
    ],
)
def test_proposition_audit_atomically_promotes_pair_for_every_verdict(
    tmp_path: Path,
    bundle_factory,
    class_verdict: str,
    provenance_verdict: str,
    eligible: bool,
    human_review: bool,
) -> None:
    runtime = _runtime(tmp_path, _without_prose(bundle_factory))
    _, _, draft_engine, _, reference = _run_proposition_draft(runtime)
    invocation = _proposition_audit_invocation(runtime, reference)
    proposal = SemanticAuditProposal(
        target_ref="proposition_one",
        class_verdict=class_verdict,
        provenance_verdict=provenance_verdict,
        reason="Synthetic exact semantic audit.",
        referenced_claim_refs=["C0001"],
        referenced_corpus_fact_refs=[],
        referenced_process_fact_refs=[],
    )
    engine = MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])
    adapter = PropositionAuditAdapter()

    first = runtime.run(
        TaskType.AUDIT_PROPOSITION,
        invocation,
        engines=[engine],
        promotion_adapter=adapter,
    )
    second = runtime.run(
        TaskType.AUDIT_PROPOSITION,
        invocation,
        engines=[engine],
        promotion_adapter=adapter,
    )

    assert first.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert first.generation == second.generation == 2
    assert first.allocated_ids == {
        "proposition_one": "PR0001",
        "audit:proposition_one": "SA0001",
    }
    assert first.transition is not None
    assert first.transition.downstream_eligible is eligible
    assert first.transition.human_review_required is human_review
    assert second.receipt_reused is True
    assert engine.calls == 1
    assert draft_engine.calls == 1
    snapshot = runtime.store.load_generation(2)[0]
    assert len(snapshot.proposition_records) == 1
    assert len(snapshot.semantic_audits) == 1
    assert snapshot.proposition_records[0].proposition_id == "PR0001"
    assert snapshot.semantic_audits[0].audit_id == "SA0001"
    assert snapshot.semantic_audits[0].target_id == "PR0001"
    assert snapshot.semantic_audits[0].provenance_verdict.value == provenance_verdict
    receipt = runtime.store.load_receipts(2)[-1]
    audit_artifact, audit_reference = load_proposition_audit_artifact(
        runtime.project_root, receipt
    )
    assert audit_artifact.source_draft == reference
    assert audit_artifact.proposition == _scientific_proposition_bundle().propositions[0]
    assert audit_artifact.audit_proposal == proposal
    assert audit_artifact.canonical_id_mapping == first.allocated_ids
    assert audit_reference.owner_generation == 2
    _assert_audit_artifact_has_only_path_free_provenance(
        runtime, audit_artifact, audit_reference
    )


@pytest.mark.parametrize(
    ("verdict", "allocates_pair", "eligible", "human_review"),
    [
        ("ENTAILED", True, True, False),
        ("UNSUPPORTED", False, False, False),
        ("UNCLEAR", False, False, True),
    ],
)
def test_sentence_audit_canonicalizes_every_verdict_and_only_entailment_allocates(
    tmp_path: Path,
    bundle_factory,
    verdict: str,
    allocates_pair: bool,
    eligible: bool,
    human_review: bool,
) -> None:
    runtime = _runtime(tmp_path, _ready_to_render(bundle_factory))
    exact_text, _, draft_engine, _, reference = _run_sentence_draft(runtime)
    invocation = _sentence_audit_invocation(runtime, reference)
    proposal = RenderedSentenceAuditProposal(
        sentence_ref="sentence_one",
        verdict=verdict,
        reason="Synthetic exact rendered-sentence audit.",
    )
    engine = MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])
    adapter = RenderedSentenceAuditAdapter()

    first = runtime.run(
        TaskType.AUDIT_RENDERED_SENTENCE,
        invocation,
        engines=[engine],
        promotion_adapter=adapter,
    )
    second = runtime.run(
        TaskType.AUDIT_RENDERED_SENTENCE,
        invocation,
        engines=[engine],
        promotion_adapter=adapter,
    )

    assert first.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert first.generation == second.generation == 2
    assert first.transition is not None
    assert first.transition.canonicalized is True
    assert first.transition.downstream_eligible is eligible
    assert first.transition.human_review_required is human_review
    assert second.receipt_reused is True
    assert engine.calls == 1
    assert draft_engine.calls == 1
    snapshot = runtime.store.load_generation(2)[0]
    receipt = runtime.store.load_receipts(2)[-1]
    audit_artifact, audit_reference = load_sentence_audit_artifact(
        runtime.project_root, receipt
    )
    assert audit_artifact.source_draft == reference
    assert audit_artifact.rendered_sentence.text == exact_text
    assert audit_artifact.audit_proposal == proposal
    assert audit_artifact.canonical_id_mapping == first.allocated_ids
    assert audit_reference.owner_generation == 2
    _assert_audit_artifact_has_only_path_free_provenance(
        runtime, audit_artifact, audit_reference
    )
    if allocates_pair:
        assert first.allocated_ids == {
            "sentence_one": "RS0001",
            "audit:sentence_one": "RSA0001",
        }
        assert snapshot.rendered_sentences[0].text == exact_text
        assert snapshot.rendered_sentence_audits[0].verdict.value == "ENTAILED"
    else:
        assert first.allocated_ids == {}
        assert snapshot.rendered_sentences == ()
        assert snapshot.rendered_sentence_audits == ()
        assert audit_artifact.audit_proposal.verdict.value == verdict


def test_proposition_draft_rejects_unauthorized_citation_before_commit(
    tmp_path: Path, bundle_factory
) -> None:
    runtime = _runtime(tmp_path, _without_prose(bundle_factory))
    proposal = _scientific_proposition_bundle(paper_ref="P9999")
    engine = MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])

    result = runtime.run(
        TaskType.GENERATE_PROPOSITIONS,
        GeneratePropositionsInvocation(
            claim_packet_ids=["C0001"],
            corpus_fact_ids=[],
            process_fact_ids=[],
        ),
        engines=[engine],
        promotion_adapter=PropositionDraftAcceptanceAdapter(),
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert runtime.store.current_generation() == 0
    assert runtime.store.load_receipts(0) == ()


def test_process_only_report_can_close_an_honest_no_claim_packet_run(
    tmp_path: Path, bundle_factory
) -> None:
    snapshot = _without_prose(bundle_factory)
    process_fact = snapshot.process_facts[0]
    runtime = _runtime(tmp_path, snapshot)
    proposition_bundle = PropositionProposalBundle(
        propositions=[
            {
                "local_ref": "process_report",
                "text": process_fact.text,
                "content_class": "ReviewProcessStatement",
                "claim_refs": [],
                "citation_bindings": [],
                "corpus_fact_refs": [],
                "process_fact_refs": [process_fact.process_fact_id],
            }
        ]
    )
    proposition_invocation = GeneratePropositionsInvocation(
        claim_packet_ids=[],
        corpus_fact_ids=[],
        process_fact_ids=[process_fact.process_fact_id],
    )
    proposition_result = runtime.run(
        TaskType.GENERATE_PROPOSITIONS,
        proposition_invocation,
        engines=[
            MockEngine(
                [MockResponse(proposal=proposition_bundle.model_dump(mode="json"))]
            )
        ],
        promotion_adapter=PropositionDraftAcceptanceAdapter(),
    )
    assert proposition_result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    proposition_receipt = runtime.store.load_receipts(1)[-1]
    _, proposition_reference = load_draft_artifact_for_receipt(
        runtime.project_root, proposition_receipt
    )
    proposition_anchor = proposition_reference.anchor(
        runtime.project_root, "process_report"
    )
    proposition_audit = runtime.run(
        TaskType.AUDIT_PROPOSITION,
        AuditPropositionInvocation(
            **proposition_anchor.model_dump(mode="json"),
            claim_packet_ids=[],
            corpus_fact_ids=[],
            process_fact_ids=[process_fact.process_fact_id],
        ),
        engines=[
            MockEngine(
                [
                    MockResponse(
                        proposal=SemanticAuditProposal(
                            target_ref="process_report",
                            class_verdict="CORRECT",
                            provenance_verdict="ENTAILED",
                            reason="Exact synthetic process-fact restatement.",
                            referenced_claim_refs=[],
                            referenced_corpus_fact_refs=[],
                            referenced_process_fact_refs=[
                                process_fact.process_fact_id
                            ],
                        ).model_dump(mode="json")
                    )
                ]
            )
        ],
        promotion_adapter=PropositionAuditAdapter(),
    )
    assert proposition_audit.allocated_ids["process_report"] == "PR0001"

    sentence_bundle = RenderedSentenceProposalBundle(
        sentences=[
            {
                "local_ref": "process_sentence",
                "text": process_fact.text,
                "source_proposition_refs": ["PR0001"],
            }
        ]
    )
    sentence_draft = runtime.run(
        TaskType.RENDER_PROSE,
        RenderProseInvocation(proposition_ids=["PR0001"]),
        engines=[
            MockEngine([MockResponse(proposal=sentence_bundle.model_dump(mode="json"))])
        ],
        promotion_adapter=RenderedSentenceDraftAcceptanceAdapter(),
    )
    assert sentence_draft.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    sentence_receipt = runtime.store.load_receipts(3)[-1]
    _, sentence_reference = load_draft_artifact_for_receipt(
        runtime.project_root, sentence_receipt
    )
    sentence_anchor = sentence_reference.anchor(runtime.project_root, "process_sentence")
    sentence_audit = runtime.run(
        TaskType.AUDIT_RENDERED_SENTENCE,
        AuditRenderedSentenceInvocation(
            **sentence_anchor.model_dump(mode="json"),
            source_proposition_ids=["PR0001"],
        ),
        engines=[
            MockEngine(
                [
                    MockResponse(
                        proposal=RenderedSentenceAuditProposal(
                            sentence_ref="process_sentence",
                            verdict="ENTAILED",
                            reason="Exact synthetic process-fact restatement.",
                        ).model_dump(mode="json")
                    )
                ]
            )
        ],
        promotion_adapter=RenderedSentenceAuditAdapter(),
    )
    assert sentence_audit.allocated_ids["process_sentence"] == "RS0001"
    final_generation, final_snapshot, _ = runtime.store.load_current()
    body, assembly = assemble_exact_section(
        final_snapshot,
        source_generation=final_generation,
        budget=AssemblyBudget(
            max_sentences=1,
            max_citations=0,
            max_body_utf8_bytes=16_384,
        ),
    )
    assert body == (process_fact.text + "\n").encode("utf-8")
    assert assembly.citations == ()


@pytest.mark.parametrize("omission", ["claim", "facts"])
def test_proposition_draft_rejects_omitted_invoked_sources(
    tmp_path: Path, bundle_factory, omission: str
) -> None:
    runtime = _runtime(tmp_path, _without_prose(bundle_factory))
    if omission == "claim":
        proposal = PropositionProposalBundle(
            propositions=[
                {
                    "local_ref": "corpus_only",
                    "text": "The supplied corpus contains one paper.",
                    "content_class": "CorpusFact",
                    "claim_refs": [],
                    "citation_bindings": [],
                    "corpus_fact_refs": ["CF0001"],
                    "process_fact_refs": [],
                }
            ]
        )
        invocation = GeneratePropositionsInvocation(
            claim_packet_ids=["C0001"],
            corpus_fact_ids=["CF0001"],
            process_fact_ids=[],
        )
    else:
        proposal = _scientific_proposition_bundle()
        invocation = GeneratePropositionsInvocation(
            claim_packet_ids=["C0001"],
            corpus_fact_ids=["CF0001"],
            process_fact_ids=["PF0001"],
        )
    engine = MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])

    result = runtime.run(
        TaskType.GENERATE_PROPOSITIONS,
        invocation,
        engines=[engine],
        promotion_adapter=PropositionDraftAcceptanceAdapter(),
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert runtime.store.current_generation() == 0


def test_rendered_sentence_draft_rejects_omitted_invoked_proposition(
    tmp_path: Path, bundle_factory
) -> None:
    runtime = _runtime(tmp_path, _ready_to_render_two(bundle_factory))
    proposal = RenderedSentenceProposalBundle(
        sentences=[
            {
                "local_ref": "sentence_one",
                "text": "Preheating reduced stress [@P0001].",
                "source_proposition_refs": ["PR0001"],
            }
        ]
    )
    engine = MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])

    result = runtime.run(
        TaskType.RENDER_PROSE,
        RenderProseInvocation(proposition_ids=["PR0001", "PR0002"]),
        engines=[engine],
        promotion_adapter=RenderedSentenceDraftAcceptanceAdapter(),
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert runtime.store.current_generation() == 0


def test_rendered_sentence_draft_rejects_unlicensed_citation_marker(
    tmp_path: Path, bundle_factory
) -> None:
    runtime = _runtime(tmp_path, _ready_to_render(bundle_factory))
    proposal = RenderedSentenceProposalBundle(
        sentences=[
            {
                "local_ref": "sentence_one",
                "text": "Preheating reduced stress [@P9999].",
                "source_proposition_refs": ["PR0001"],
            }
        ]
    )
    engine = MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])

    result = runtime.run(
        TaskType.RENDER_PROSE,
        RenderProseInvocation(proposition_ids=["PR0001"]),
        engines=[engine],
        promotion_adapter=RenderedSentenceDraftAcceptanceAdapter(),
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert runtime.store.current_generation() == 0


def test_audit_rejects_changed_source_allowlist_and_tampered_draft(
    tmp_path: Path, bundle_factory
) -> None:
    runtime = _runtime(tmp_path, _without_prose(bundle_factory))
    _, _, _, _, reference = _run_proposition_draft(runtime)
    invocation = _proposition_audit_invocation(runtime, reference)
    proposal = SemanticAuditProposal(
        target_ref="proposition_one",
        class_verdict="CORRECT",
        provenance_verdict="ENTAILED",
        reason="Exact.",
        referenced_claim_refs=["C0001"],
        referenced_corpus_fact_refs=[],
        referenced_process_fact_refs=[],
    )
    wrong_allowlist = invocation.model_copy(
        update={"claim_packet_ids": ["C9999"]}
    )
    adapter = PropositionAuditAdapter()
    snapshot, registry = runtime.store.load_generation(1)
    with pytest.raises(Exception, match="allowlist"):
        adapter.validate_proposal(
            spec=TASK_SPECS[TaskType.AUDIT_PROPOSITION],
            proposal=proposal,
            snapshot=snapshot,
            dependency_keys={},
            invocation=wrong_allowlist,
            registry=registry,
        )

    task_dir, manifest, provenance = runtime._create_task(
        TASK_SPECS[TaskType.AUDIT_PROPOSITION], invocation
    )
    del task_dir
    artifact_path = invocation.draft_artifact_path
    outside = tmp_path / "outside.json"
    outside.write_bytes(artifact_path.read_bytes())
    os.chmod(artifact_path.parent, 0o700)
    artifact_path.unlink()
    artifact_path.symlink_to(outside)
    with pytest.raises(DraftArtifactError):
        adapter.validate_pre_execution(
            project_root=runtime.project_root,
            spec=TASK_SPECS[TaskType.AUDIT_PROPOSITION],
            invocation=invocation,
            manifest=manifest,
            provenance=provenance,
        )


def test_audit_rejects_dependency_hashes_that_differ_from_source_draft(
    tmp_path: Path, bundle_factory
) -> None:
    runtime = _runtime(tmp_path, _without_prose(bundle_factory))
    _, _, _, _, reference = _run_proposition_draft(runtime)
    invocation = _proposition_audit_invocation(runtime, reference)
    spec = TASK_SPECS[TaskType.AUDIT_PROPOSITION]
    _, manifest, provenance = runtime._create_task(spec, invocation)
    changed = dict(provenance.dependencies)
    changed[next(iter(changed))] = "sha256:" + "0" * 64
    mismatched = provenance.model_copy(update={"dependencies": changed})

    with pytest.raises(DraftArtifactError, match="dependency hashes differ"):
        PropositionAuditAdapter().validate_pre_execution(
            project_root=runtime.project_root,
            spec=spec,
            invocation=invocation,
            manifest=manifest,
            provenance=mismatched,
        )


def test_audit_pre_execution_rejects_stale_generation_before_engine(
    tmp_path: Path, bundle_factory
) -> None:
    runtime = _runtime(tmp_path, _without_prose(bundle_factory))
    _, _, _, _, reference = _run_proposition_draft(runtime)
    invocation = _proposition_audit_invocation(runtime, reference)
    spec = TASK_SPECS[TaskType.AUDIT_PROPOSITION]
    _, manifest, provenance = runtime._create_task(spec, invocation)
    runtime.store.commit(
        base_generation=1,
        dependencies={},
        promotion=lambda snapshot, registry: PromotionPayload(
            snapshot, registry, {}
        ),
    )

    with pytest.raises(StaleSnapshotError):
        PropositionAuditAdapter().validate_pre_execution(
            project_root=runtime.project_root,
            spec=spec,
            invocation=invocation,
            manifest=manifest,
            provenance=provenance,
        )


def test_changed_audit_semantics_cannot_reaudit_same_draft_item(
    tmp_path: Path, bundle_factory
) -> None:
    runtime = _runtime(tmp_path, _without_prose(bundle_factory))
    _, _, _, _, reference = _run_proposition_draft(runtime)
    invocation = _proposition_audit_invocation(runtime, reference)
    proposal = SemanticAuditProposal(
        target_ref="proposition_one",
        class_verdict="CORRECT",
        provenance_verdict="UNSUPPORTED",
        reason="Final negative audit.",
        referenced_claim_refs=["C0001"],
        referenced_corpus_fact_refs=[],
        referenced_process_fact_refs=[],
    )
    first_engine = MockEngine(
        [MockResponse(proposal=proposal.model_dump(mode="json"))]
    )
    first = runtime.run(
        TaskType.AUDIT_PROPOSITION,
        invocation,
        engines=[first_engine],
        promotion_adapter=PropositionAuditAdapter(),
    )
    changed_adapter = PropositionAuditAdapter()
    changed_adapter.promotion_fingerprint = hash_text("changed-audit-semantics")
    blocked_engine = MockEngine(
        [MockResponse(proposal=proposal.model_dump(mode="json"))]
    )

    blocked = runtime.run(
        TaskType.AUDIT_PROPOSITION,
        invocation,
        engines=[blocked_engine],
        promotion_adapter=changed_adapter,
    )

    assert first.generation == 2
    assert blocked.outcome is AttemptOutcome.INTERNAL_RUNTIME_FAILURE
    assert blocked_engine.calls == 0
    assert runtime.store.current_generation() == 2
    snapshot = runtime.store.load_generation(2)[0]
    assert len(snapshot.proposition_records) == 1
    assert len(snapshot.semantic_audits) == 1


def test_changed_semantics_cannot_reaudit_canonical_negative_sentence_outcome(
    tmp_path: Path, bundle_factory
) -> None:
    runtime = _runtime(tmp_path, _ready_to_render(bundle_factory))
    _, _, _, _, reference = _run_sentence_draft(runtime)
    invocation = _sentence_audit_invocation(runtime, reference)
    proposal = RenderedSentenceAuditProposal(
        sentence_ref="sentence_one",
        verdict="UNCLEAR",
        reason="Final uncertain audit.",
    )
    first = runtime.run(
        TaskType.AUDIT_RENDERED_SENTENCE,
        invocation,
        engines=[MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])],
        promotion_adapter=RenderedSentenceAuditAdapter(),
    )
    changed_adapter = RenderedSentenceAuditAdapter()
    changed_adapter.promotion_fingerprint = hash_text(
        "changed-sentence-audit-semantics"
    )
    blocked_engine = MockEngine(
        [MockResponse(proposal=proposal.model_dump(mode="json"))]
    )

    blocked = runtime.run(
        TaskType.AUDIT_RENDERED_SENTENCE,
        invocation,
        engines=[blocked_engine],
        promotion_adapter=changed_adapter,
    )

    assert first.generation == 2
    assert first.allocated_ids == {}
    assert blocked.outcome is AttemptOutcome.INTERNAL_RUNTIME_FAILURE
    assert blocked_engine.calls == 0
    assert runtime.store.current_generation() == 2
    snapshot = runtime.store.load_generation(2)[0]
    assert snapshot.rendered_sentences == ()
    assert snapshot.rendered_sentence_audits == ()


def test_identical_cloned_draft_cannot_reset_audit_but_repair_can(
    tmp_path: Path, bundle_factory
) -> None:
    runtime = _runtime(tmp_path, _without_prose(bundle_factory))
    original, invocation, _, _, reference = _run_proposition_draft(runtime)
    audit_invocation = _proposition_audit_invocation(runtime, reference)
    audit = SemanticAuditProposal(
        target_ref="proposition_one",
        class_verdict="CORRECT",
        provenance_verdict="UNSUPPORTED",
        reason="Final negative audit.",
        referenced_claim_refs=["C0001"],
        referenced_corpus_fact_refs=[],
        referenced_process_fact_refs=[],
    )
    runtime.run(
        TaskType.AUDIT_PROPOSITION,
        audit_invocation,
        engines=[MockEngine([MockResponse(proposal=audit.model_dump(mode="json"))])],
        promotion_adapter=PropositionAuditAdapter(),
    )

    clone_invocation = GeneratePropositionsInvocation(
        claim_packet_ids=["C0001"],
        corpus_fact_ids=["CF0001"],
        process_fact_ids=["PF0001"],
    )
    clone_bundle = PropositionProposalBundle(
        propositions=[
            original.propositions[0],
            {
                "local_ref": "corpus_context",
                "text": "The supplied corpus contains one paper.",
                "content_class": "CorpusFact",
                "claim_refs": [],
                "citation_bindings": [],
                "corpus_fact_refs": ["CF0001"],
                "process_fact_refs": [],
            },
            {
                "local_ref": "process_context",
                "text": "The default validation policy was used.",
                "content_class": "ReviewProcessStatement",
                "claim_refs": [],
                "citation_bindings": [],
                "corpus_fact_refs": [],
                "process_fact_refs": ["PF0001"],
            },
        ]
    )
    clone_adapter = PropositionDraftAcceptanceAdapter()
    clone_adapter.promotion_fingerprint = hash_text("clone-draft-semantics")
    clone_result = runtime.run(
        TaskType.GENERATE_PROPOSITIONS,
        clone_invocation,
        engines=[
            MockEngine(
                [MockResponse(proposal=clone_bundle.model_dump(mode="json"))],
                name="clone-mock",
            )
        ],
        promotion_adapter=clone_adapter,
    )
    assert clone_result.generation == 3
    clone_receipt = runtime.store.load_receipts(3)[-1]
    _, clone_reference = load_draft_artifact_for_receipt(
        runtime.project_root, clone_receipt
    )
    clone_audit_invocation = _proposition_audit_invocation(
        runtime,
        clone_reference,
        corpus_fact_ids=["CF0001"],
        process_fact_ids=["PF0001"],
    )
    blocked_engine = MockEngine(
        [MockResponse(proposal=audit.model_dump(mode="json"))]
    )
    blocked = runtime.run(
        TaskType.AUDIT_PROPOSITION,
        clone_audit_invocation,
        engines=[blocked_engine],
        promotion_adapter=PropositionAuditAdapter(),
    )
    assert blocked.outcome is AttemptOutcome.INTERNAL_RUNTIME_FAILURE
    assert blocked_engine.calls == 0
    assert runtime.store.current_generation() == 3

    repaired = clone_bundle.model_copy(
        update={
            "propositions": [
                clone_bundle.propositions[0].model_copy(
                    update={
                        "text": (
                            "Preheating reduced stress only in the directly "
                            "tested process window."
                        )
                    }
                ),
                *clone_bundle.propositions[1:],
            ]
        }
    )
    repair_adapter = PropositionDraftAcceptanceAdapter()
    repair_adapter.promotion_fingerprint = hash_text("repair-draft-semantics")
    repair_result = runtime.run(
        TaskType.GENERATE_PROPOSITIONS,
        clone_invocation,
        engines=[
            MockEngine(
                [MockResponse(proposal=repaired.model_dump(mode="json"))],
                name="repair-mock",
            )
        ],
        promotion_adapter=repair_adapter,
    )
    assert repair_result.generation == 4
    repair_receipt = runtime.store.load_receipts(4)[-1]
    _, repair_reference = load_draft_artifact_for_receipt(
        runtime.project_root, repair_receipt
    )
    repaired_audit = audit.model_copy(
        update={"reason": "Audit of the materially repaired draft."}
    )
    accepted = runtime.run(
        TaskType.AUDIT_PROPOSITION,
        _proposition_audit_invocation(
            runtime,
            repair_reference,
            corpus_fact_ids=["CF0001"],
            process_fact_ids=["PF0001"],
        ),
        engines=[
            MockEngine(
                [MockResponse(proposal=repaired_audit.model_dump(mode="json"))]
            )
        ],
        promotion_adapter=PropositionAuditAdapter(),
    )
    assert accepted.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert accepted.generation == 5
    assert accepted.allocated_ids["proposition_one"] == "PR0002"


def test_audit_artifact_tampering_fails_exact_verification(
    tmp_path: Path, bundle_factory
) -> None:
    runtime = _runtime(tmp_path, _without_prose(bundle_factory))
    _, _, _, _, reference = _run_proposition_draft(runtime)
    invocation = _proposition_audit_invocation(runtime, reference)
    proposal = SemanticAuditProposal(
        target_ref="proposition_one",
        class_verdict="CORRECT",
        provenance_verdict="ENTAILED",
        reason="Exact.",
        referenced_claim_refs=["C0001"],
        referenced_corpus_fact_refs=[],
        referenced_process_fact_refs=[],
    )
    runtime.run(
        TaskType.AUDIT_PROPOSITION,
        invocation,
        engines=[MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])],
        promotion_adapter=PropositionAuditAdapter(),
    )
    receipt = runtime.store.load_receipts(2)[-1]
    _, audit_reference = load_proposition_audit_artifact(
        runtime.project_root, receipt
    )
    artifact_path = (
        runtime.project_root
        / "state/generations/000002/auxiliary"
        / audit_reference.relative_path
    )
    os.chmod(artifact_path, 0o600)
    artifact_path.write_bytes(artifact_path.read_bytes() + b" ")

    with pytest.raises(DraftArtifactError):
        load_proposition_audit_artifact(runtime.project_root, receipt)


def test_proposition_pair_crash_never_publishes_half_transition(
    tmp_path: Path, bundle_factory
) -> None:
    runtime = _runtime(tmp_path, _without_prose(bundle_factory))
    _, _, _, _, reference = _run_proposition_draft(runtime)
    invocation = _proposition_audit_invocation(runtime, reference)
    proposal = SemanticAuditProposal(
        target_ref="proposition_one",
        class_verdict="CORRECT",
        provenance_verdict="ENTAILED",
        reason="Exact.",
        referenced_claim_refs=["C0001"],
        referenced_corpus_fact_refs=[],
        referenced_process_fact_refs=[],
    )
    spec = TASK_SPECS[TaskType.AUDIT_PROPOSITION]
    _, manifest, provenance = runtime._create_task(spec, invocation)
    plan = PropositionAuditAdapter().prepare_commit(
        project_root=runtime.project_root,
        spec=spec,
        proposal=proposal,
        invocation=invocation,
        manifest=manifest,
        provenance=provenance,
    )

    with pytest.raises(InjectedCrash):
        runtime.store.commit(
            base_generation=manifest.base_generation,
            dependencies=dict(manifest.dependencies),
            promotion=plan.promotion,
            crash_at=CrashPoint.AFTER_SCIENTIFIC_STAGING_BEFORE_RECEIPT_STAGING,
        )

    assert runtime.store.current_generation() == 1
    snapshot = runtime.store.load_generation(1)[0]
    assert snapshot.proposition_records == ()
    assert snapshot.semantic_audits == ()
