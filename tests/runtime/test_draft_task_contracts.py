"""Bounded TaskSpecs for accepted proposition and sentence draft transitions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import vibereview.runtime as public_runtime
from vibereview.runtime import (
    AuditPropositionInput,
    AuditPropositionInvocation,
    AuditRenderedSentenceInput,
    AuditRenderedSentenceInvocation,
    AttemptOutcome,
    CitationBindingProposal,
    GeneratePropositionsInput,
    GeneratePropositionsInvocation,
    MockEngine,
    ProjectContext,
    ProjectRuntime,
    PropositionProposal,
    PropositionProposalBundle,
    RenderProseInput,
    RenderProseInvocation,
    RenderedSentenceAuditProposal,
    RenderedSentenceProposal,
    RenderedSentenceProposalBundle,
    RepositorySnapshot,
    SemanticAuditProposal,
    SnapshottedResource,
    TaskSpecNotExecutableError,
    TaskType,
    TaskWorkspace,
)
from vibereview.runtime.dto import (
    MAX_DRAFT_TASK_ITEMS,
    MAX_DRAFT_TEXT_CHARS,
)
from vibereview.runtime.hashing import hash_bytes
from vibereview.runtime.promotion import interpret_disposition
from vibereview.runtime.specs import (
    TASK_SPECS,
    executable_task_types,
    validate_task_spec_executable,
)


DRAFT_HASH = "sha256:" + "d" * 64


def _source_selection() -> dict[str, list[str]]:
    return {
        "claim_packet_ids": ["C0001"],
        "corpus_fact_ids": ["CF0001"],
        "process_fact_ids": ["PF0001"],
    }


def _audit_proposition_invocation(path: Path, **updates) -> AuditPropositionInvocation:
    values = {
        "draft_kind": "proposition_draft",
        "draft_owner_generation": 1,
        "draft_source_generation": 0,
        "draft_task_id": "TASK0042",
        "draft_artifact_hash": DRAFT_HASH,
        "draft_local_ref": "proposition_one",
        **_source_selection(),
        "draft_artifact_path": path,
    }
    values.update(updates)
    return AuditPropositionInvocation.model_validate(values)


def _audit_sentence_invocation(
    path: Path, **updates
) -> AuditRenderedSentenceInvocation:
    values = {
        "draft_kind": "rendered_sentence_draft",
        "draft_owner_generation": 1,
        "draft_source_generation": 0,
        "draft_task_id": "TASK0043",
        "draft_artifact_hash": DRAFT_HASH,
        "draft_local_ref": "sentence_one",
        "source_proposition_ids": ["PR0001"],
        "draft_artifact_path": path,
    }
    values.update(updates)
    return AuditRenderedSentenceInvocation.model_validate(values)


def _scientific_proposition(**updates) -> PropositionProposal:
    values = {
        "local_ref": "proposition_one",
        "text": "Preheating reduced residual stress in the tested process window.",
        "content_class": "ScientificClaim",
        "claim_refs": ["C0001"],
        "citation_bindings": [
            {
                "paper_ref": "P0001",
                "claim_ref": "C0001",
                "claim_paper_evidence_ref": "CPE-C0001-P0001",
            }
        ],
        "corpus_fact_refs": [],
        "process_fact_refs": [],
    }
    values.update(updates)
    return PropositionProposal.model_validate(values)


def test_process_only_source_selection_supports_an_honest_no_claim_report() -> None:
    invocation = GeneratePropositionsInvocation(
        claim_packet_ids=[],
        corpus_fact_ids=[],
        process_fact_ids=["PF0001"],
    )

    assert invocation.claim_packet_ids == []
    assert invocation.process_fact_ids == ["PF0001"]


@pytest.mark.parametrize(
    ("model", "values", "message"),
    [
        (
            GeneratePropositionsInvocation,
            {"claim_packet_ids": [], "corpus_fact_ids": [], "process_fact_ids": []},
            "requires at least one",
        ),
        (
            GeneratePropositionsInvocation,
            {
                "claim_packet_ids": ["C0001", "C0001"],
                "corpus_fact_ids": [],
                "process_fact_ids": [],
            },
            "claim_packet_ids must be unique",
        ),
        (
            GeneratePropositionsInvocation,
            {
                "claim_packet_ids": ["C0001"],
                "corpus_fact_ids": ["CF0001", "CF0001"],
                "process_fact_ids": [],
            },
            "corpus_fact_ids must be unique",
        ),
        (
            RenderProseInvocation,
            {"proposition_ids": []},
            "at least 1",
        ),
        (
            RenderProseInvocation,
            {"proposition_ids": ["PR0001", "PR0001"]},
            "proposition_ids must be unique",
        ),
    ],
)
def test_task_driving_lists_are_nonempty_bounded_and_unique(model, values, message):
    with pytest.raises(ValidationError, match=message):
        model.model_validate(values)


def test_task_driving_lists_have_finite_capacity():
    packet_ids = [f"C{ordinal:04d}" for ordinal in range(MAX_DRAFT_TASK_ITEMS + 1)]
    with pytest.raises(ValidationError, match="at most 100"):
        GeneratePropositionsInvocation(
            claim_packet_ids=packet_ids,
            corpus_fact_ids=[],
            process_fact_ids=[],
        )
    proposition_ids = [
        f"PR{ordinal:04d}" for ordinal in range(MAX_DRAFT_TASK_ITEMS + 1)
    ]
    with pytest.raises(ValidationError, match="at most 100"):
        RenderProseInvocation(proposition_ids=proposition_ids)


@pytest.mark.parametrize(
    ("factory", "updates", "message"),
    [
        (
            _audit_proposition_invocation,
            {"draft_kind": "rendered_sentence_draft"},
            "proposition_draft",
        ),
        (
            _audit_proposition_invocation,
            {"draft_owner_generation": 2},
            "immediately follow",
        ),
        (
            _audit_proposition_invocation,
            {"draft_local_ref": "PR0001"},
            "must not be a canonical proposition ID",
        ),
        (
            _audit_sentence_invocation,
            {"draft_kind": "proposition_draft"},
            "rendered_sentence_draft",
        ),
        (
            _audit_sentence_invocation,
            {"draft_local_ref": "RS0001"},
            "must not be a canonical sentence ID",
        ),
        (
            _audit_sentence_invocation,
            {"source_proposition_ids": []},
            "at least 1",
        ),
        (
            _audit_sentence_invocation,
            {"source_proposition_ids": ["PR0001", "PR0001"]},
            "source_proposition_ids must be unique",
        ),
    ],
)
def test_accepted_draft_anchors_are_kind_generation_and_target_bound(
    tmp_path, factory, updates, message
):
    with pytest.raises(ValidationError, match=message):
        factory(tmp_path / "draft.json", **updates)


def test_accepted_draft_resource_paths_must_be_absolute():
    with pytest.raises(ValidationError, match="must be absolute"):
        _audit_proposition_invocation(Path("relative/draft.json"))
    with pytest.raises(ValidationError, match="must be absolute"):
        _audit_sentence_invocation(Path("relative/draft.json"))


def test_proposition_drafts_are_nonempty_bounded_and_locally_addressed():
    proposition = _scientific_proposition()
    bundle = PropositionProposalBundle(propositions=[proposition])
    assert bundle.propositions[0].local_ref == "proposition_one"
    with pytest.raises(ValidationError, match="at least 1"):
        PropositionProposalBundle(propositions=[])
    with pytest.raises(ValidationError, match="local_refs must be unique"):
        PropositionProposalBundle(propositions=[proposition, proposition])
    with pytest.raises(ValidationError, match="canonical PR ID"):
        _scientific_proposition(local_ref="PR0001")
    with pytest.raises(ValidationError):
        _scientific_proposition(text="x" * (MAX_DRAFT_TEXT_CHARS + 1))
    with pytest.raises(ValidationError, match="claim_refs must be unique"):
        _scientific_proposition(claim_refs=["C0001", "C0001"])
    repeated_binding = CitationBindingProposal(
        paper_ref="P0001",
        claim_ref="C0001",
        claim_paper_evidence_ref="CPE-C0001-P0001",
    )
    with pytest.raises(ValidationError, match="citation_bindings must be unique"):
        _scientific_proposition(
            citation_bindings=[repeated_binding, repeated_binding]
        )


def test_sentence_drafts_and_both_audit_targets_are_local_and_bounded():
    sentence = RenderedSentenceProposal(
        local_ref="sentence_one",
        text="Preheating reduced stress [P0001].",
        source_proposition_refs=["PR0001"],
    )
    assert RenderedSentenceProposalBundle(sentences=[sentence]).sentences == [sentence]
    with pytest.raises(ValidationError, match="at least 1"):
        RenderedSentenceProposalBundle(sentences=[])
    with pytest.raises(ValidationError, match="source_proposition_refs must be unique"):
        RenderedSentenceProposal.model_validate(
            {
                **sentence.model_dump(mode="json"),
                "source_proposition_refs": ["PR0001", "PR0001"],
            }
        )
    with pytest.raises(ValidationError, match="canonical RS ID"):
        RenderedSentenceProposal(
            local_ref="RS0001",
            text="Exact sentence.",
            source_proposition_refs=["PR0001"],
        )
    with pytest.raises(ValidationError, match="forbidden line marker"):
        RenderedSentenceProposal(
            local_ref="sentence_two",
            text="This is not one sentence.\nThis is another line.",
            source_proposition_refs=["PR0001"],
        )
    with pytest.raises(ValidationError, match="draft local ref"):
        SemanticAuditProposal(
            target_ref="PR0001",
            class_verdict="CORRECT",
            provenance_verdict="ENTAILED",
            reason="Canonical targets are forbidden here.",
            referenced_claim_refs=["C0001"],
            referenced_corpus_fact_refs=[],
            referenced_process_fact_refs=[],
        )
    with pytest.raises(ValidationError, match="sentence draft local ref"):
        RenderedSentenceAuditProposal(
            sentence_ref="RS0001",
            verdict="ENTAILED",
            reason="Canonical targets are forbidden here.",
        )


@pytest.mark.parametrize(
    (
        "class_verdict",
        "provenance_verdict",
        "scientific_disposition",
        "downstream_eligible",
        "human_review_required",
    ),
    [
        ("CORRECT", "ENTAILED", "ENTAILED", True, False),
        (
            "CORRECT",
            "PARTIALLY_SUPPORTED",
            "PARTIALLY_SUPPORTED",
            False,
            False,
        ),
        ("MISCLASSIFIED", "ENTAILED", "MISCLASSIFIED", False, False),
        ("CORRECT", "UNCLEAR", "UNCLEAR", False, True),
    ],
    ids=["pass", "partially-supported", "misclassified", "unclear"],
)
def test_draft_local_semantic_audit_preserves_frozen_transitions(
    class_verdict,
    provenance_verdict,
    scientific_disposition,
    downstream_eligible,
    human_review_required,
):
    proposal = SemanticAuditProposal(
        target_ref="proposition_one",
        class_verdict=class_verdict,
        provenance_verdict=provenance_verdict,
        reason="Synthetic draft-local audit.",
        referenced_claim_refs=["C0001"],
        referenced_corpus_fact_refs=[],
        referenced_process_fact_refs=[],
    )

    transition = interpret_disposition(
        TASK_SPECS[TaskType.AUDIT_PROPOSITION], proposal
    )

    assert transition.scientific_disposition == scientific_disposition
    assert transition.downstream_eligible is downstream_eligible
    assert transition.human_review_required is human_review_required


def test_generate_and_audit_proposition_manifest_complete_source_context(
    bundle_factory,
):
    snapshot = RepositorySnapshot.model_validate(bundle_factory())
    generate = GeneratePropositionsInvocation(**_source_selection())
    audit = _audit_proposition_invocation(Path("/private/draft.json"))
    expected = (
        "ClaimPacket:C0001",
        "CandidateClaim:C0001",
        "ThemeRecord:T0001",
        "ClaimAssessment:C0001",
        "FinalClaimValidation:C0001",
        "ClaimPaperEvidence:CPE-C0001-P0001",
        "Paper:P0001",
        "EvidenceRecord:E0001",
        "RetrievedSpan:R0001",
        "RetrievalDisposition:R0001",
        "RetrievalQuery:Q-C0001-SUP-01",
        "CorpusFact:CF0001",
        "ReviewProcessFact:PF0001",
    )
    for task_type, invocation in (
        (TaskType.GENERATE_PROPOSITIONS, generate),
        (TaskType.AUDIT_PROPOSITION, audit),
    ):
        keys = TASK_SPECS[task_type].dependency_builder(invocation, snapshot)
        assert keys == expected
        assert len(keys) == len(set(keys))
        assert all(snapshot.dependency_hash(key) for key in keys)


def test_render_and_sentence_audit_require_exactly_one_pass_audit(bundle_factory):
    snapshot = RepositorySnapshot.model_validate(bundle_factory())
    render = RenderProseInvocation(proposition_ids=["PR0001"])
    sentence_audit = _audit_sentence_invocation(Path("/private/draft.json"))
    expected = (
        "PropositionRecord:PR0001",
        "SemanticAuditResult:SA0001",
    )
    for task_type, invocation in (
        (TaskType.RENDER_PROSE, render),
        (TaskType.AUDIT_RENDERED_SENTENCE, sentence_audit),
    ):
        assert (
            TASK_SPECS[task_type].dependency_builder(invocation, snapshot)
            == expected
        )

    nonpassing = snapshot.semantic_audits[0].model_copy(
        update={"provenance_verdict": "UNSUPPORTED"}
    )
    nonpassing_snapshot = snapshot.model_copy(update={"semantic_audits": (nonpassing,)})
    with pytest.raises(ValueError, match="does not have a PASS"):
        TASK_SPECS[TaskType.RENDER_PROSE].dependency_builder(
            render, nonpassing_snapshot
        )

    repeated = snapshot.semantic_audits[0].model_copy(update={"audit_id": "SA0002"})
    repeated_snapshot = snapshot.model_copy(
        update={"semantic_audits": (*snapshot.semantic_audits, repeated)}
    )
    with pytest.raises(ValueError, match="exactly one semantic audit"):
        TASK_SPECS[TaskType.AUDIT_RENDERED_SENTENCE].dependency_builder(
            sentence_audit, repeated_snapshot
        )


@pytest.mark.parametrize(
    ("task_type", "logical_name", "invocation_factory"),
    [
        (
            TaskType.AUDIT_PROPOSITION,
            "accepted_proposition_draft.json",
            _audit_proposition_invocation,
        ),
        (
            TaskType.AUDIT_RENDERED_SENTENCE,
            "accepted_rendered_sentence_draft.json",
            _audit_sentence_invocation,
        ),
    ],
)
def test_audit_draft_resource_is_fixed_hash_bound_and_path_private(
    tmp_path, bundle_factory, task_type, logical_name, invocation_factory
):
    project = tmp_path / "project"
    source_path = (
        project
        / "state/generations/000001/auxiliary/drafts/v1"
        / task_type.value
        / "secret-live-draft.json"
    )
    source_path.parent.mkdir(parents=True)
    content = b'{"synthetic":"accepted generation-owned draft"}\n'
    source_path.write_bytes(content)
    invocation = invocation_factory(
        source_path,
        draft_artifact_hash=hash_bytes(content),
    )
    snapshot = RepositorySnapshot.model_validate(bundle_factory())
    spec = TASK_SPECS[task_type]
    dependencies = {
        key: snapshot.dependency_hash(key)
        for key in spec.dependency_builder(invocation, snapshot)
    }
    context = ProjectContext(project_root=project)
    requests = spec.resource_builder(invocation, snapshot, context)
    assert len(requests) == 1
    assert requests[0].resource_id == "RES0001"
    assert requests[0].logical_name == logical_name
    assert requests[0].media_type == "application/json"
    assert requests[0].source_path == source_path

    task_dir, manifest, provenance = TaskWorkspace(project).create(
        spec=spec,
        invocation=invocation,
        base_generation=1,
        dependencies=dependencies,
        resource_requests=requests,
        snapshot=snapshot,
        context=context,
    )
    assert tuple(manifest.dependencies) == tuple(dependencies)
    assert provenance.resources[0].snapshot_hash == hash_bytes(content)
    copied = task_dir / "bundle/input/resources/RES0001/content.json"
    assert copied.read_bytes() == content
    engine_input = json.loads(
        (task_dir / "bundle/input/input.json").read_text(encoding="utf-8")
    )
    assert engine_input["draft_resource_id"] == "RES0001"
    assert engine_input["draft_artifact_hash"] == hash_bytes(content)
    assert "draft_artifact_path" not in engine_input
    source_bytes = str(source_path).encode("utf-8")
    for path in (task_dir / "bundle").rglob("*"):
        if path.is_file():
            assert source_bytes not in path.read_bytes(), path
    assert str(source_path) in (
        task_dir / "private/invocation.json"
    ).read_text(encoding="utf-8")

    canonical_index = snapshot.object_index()
    dependency_root = task_dir / "bundle/input/dependencies"
    for key in dependencies:
        payload = json.loads(
            (dependency_root / f"{key.replace(':', '__')}.json").read_text(
                encoding="utf-8"
            )
        )
        assert payload == json.loads(canonical_index[key].model_dump_json())


def test_draft_input_builder_rejects_missing_or_hash_mismatched_resource(tmp_path):
    invocation = _audit_proposition_invocation(tmp_path / "draft.json")
    spec = TASK_SPECS[TaskType.AUDIT_PROPOSITION]
    with pytest.raises(ValueError, match="exactly RES0001"):
        spec.engine_input_builder(invocation, ())
    wrong_hash = SnapshottedResource(
        resource_id="RES0001",
        logical_name="accepted_proposition_draft.json",
        snapshot_relative_path=Path("input/resources/RES0001/content.json"),
        media_type="application/json",
        size_bytes=2,
        content_hash="sha256:" + "a" * 64,
    )
    with pytest.raises(ValueError, match="hash differs"):
        spec.engine_input_builder(invocation, (wrong_hash,))


def test_all_four_specs_are_v2_object_rooted_and_adapter_gated():
    expected = {
        TaskType.GENERATE_PROPOSITIONS: (
            GeneratePropositionsInvocation,
            GeneratePropositionsInput,
            PropositionProposalBundle,
            "accept_proposition_drafts",
        ),
        TaskType.AUDIT_PROPOSITION: (
            AuditPropositionInvocation,
            AuditPropositionInput,
            SemanticAuditProposal,
            "promote_proposition_with_audit",
        ),
        TaskType.RENDER_PROSE: (
            RenderProseInvocation,
            RenderProseInput,
            RenderedSentenceProposalBundle,
            "accept_rendered_sentence_drafts",
        ),
        TaskType.AUDIT_RENDERED_SENTENCE: (
            AuditRenderedSentenceInvocation,
            AuditRenderedSentenceInput,
            RenderedSentenceAuditProposal,
            "promote_rendered_sentence_with_audit",
        ),
    }
    assert not set(expected) & set(executable_task_types())
    for task_type, (
        invocation_model,
        input_model,
        proposal_model,
        handler,
    ) in expected.items():
        spec = TASK_SPECS[task_type]
        assert spec.version == spec.prompt_version == "2"
        assert spec.invocation_model is invocation_model
        assert spec.engine_input_model is input_model
        assert spec.proposal_model is proposal_model
        assert spec.proposal_model.model_json_schema()["type"] == "object"
        assert spec.promotion_handler == handler
        with pytest.raises(TaskSpecNotExecutableError, match=handler):
            validate_task_spec_executable(spec)
        validate_task_spec_executable(
            spec, additional_promotion_handlers=frozenset({handler})
        )


def test_all_four_tasks_fail_closed_without_adapter_before_engine_or_task_creation(
    tmp_path, bundle_factory
):
    snapshot = RepositorySnapshot.model_validate(bundle_factory())
    invocations = {
        TaskType.GENERATE_PROPOSITIONS: GeneratePropositionsInvocation(
            **_source_selection()
        ),
        TaskType.AUDIT_PROPOSITION: _audit_proposition_invocation(
            tmp_path / "missing-proposition-draft.json"
        ),
        TaskType.RENDER_PROSE: RenderProseInvocation(proposition_ids=["PR0001"]),
        TaskType.AUDIT_RENDERED_SENTENCE: _audit_sentence_invocation(
            tmp_path / "missing-sentence-draft.json"
        ),
    }
    for task_type, invocation in invocations.items():
        project = tmp_path / task_type.value
        runtime = ProjectRuntime.create(
            project,
            project_name=task_type.value,
            initial_snapshot=snapshot,
        )
        engine = MockEngine([])
        result = runtime.run(task_type, invocation, engines=[engine])
        assert result.outcome is AttemptOutcome.TASK_TYPE_NOT_IMPLEMENTED
        assert engine.calls == 0
        assert not (project / "work/tasks").exists()


def test_public_runtime_exports_complete_draft_task_contract():
    expected = {
        "AuditPropositionInput": AuditPropositionInput,
        "AuditPropositionInvocation": AuditPropositionInvocation,
        "AuditRenderedSentenceInput": AuditRenderedSentenceInput,
        "AuditRenderedSentenceInvocation": AuditRenderedSentenceInvocation,
        "CitationBindingProposal": CitationBindingProposal,
        "GeneratePropositionsInput": GeneratePropositionsInput,
        "GeneratePropositionsInvocation": GeneratePropositionsInvocation,
        "PropositionProposal": PropositionProposal,
        "PropositionProposalBundle": PropositionProposalBundle,
        "RenderProseInput": RenderProseInput,
        "RenderProseInvocation": RenderProseInvocation,
        "RenderedSentenceAuditProposal": RenderedSentenceAuditProposal,
        "RenderedSentenceProposal": RenderedSentenceProposal,
        "RenderedSentenceProposalBundle": RenderedSentenceProposalBundle,
        "SemanticAuditProposal": SemanticAuditProposal,
    }
    for name, value in expected.items():
        assert name in public_runtime.__all__
        assert getattr(public_runtime, name) is value
