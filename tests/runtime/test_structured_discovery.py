from __future__ import annotations

import json
from pathlib import Path

import pytest

from vibereview.models import Paper
from vibereview.runtime import (
    AttemptOutcome,
    CandidateClaimProposal,
    CorpusChallengerInvocation,
    DiscoveryCoverageFindingProposal,
    DiscoveryFindingProposal,
    DiscoveryInputDispositionProposal,
    DiscoveryPaperCandidateProposal,
    DiscoveryProposalBundle,
    DiscoverySourceBinding,
    DiscoveryTerminologyProposal,
    MockEngine,
    MockResponse,
    PaperConceptSketchProposal,
    ProjectRuntime,
    RepositorySnapshot,
    ResourceTextLocator,
    StructuredCorpusChallengeAdapter,
    TaskType,
    ThemeProposal,
    load_discovery_artifact,
    run_structured_candidate_claims,
    run_structured_corpus_challenge,
    run_structured_discovery_parse,
)
from vibereview.runtime.hashing import hash_bytes, hash_text


TOPIC = "residual stress"
PARSE_REFS = (
    "parsed_theme",
    "parsed_claim",
    "term_stress",
    "candidate_paper",
    "controversy_scale",
    "gap_boundary",
)


def _write_text(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _locator(resource_id: str, text: str, needle: str) -> ResourceTextLocator:
    start = text.index(needle)
    return ResourceTextLocator(
        resource_id=resource_id,
        resource_hash=hash_bytes(text.encode("utf-8")),
        start_offset=start,
        end_offset=start + len(needle),
        source_span_hash=hash_text(needle),
    )


def _runtime_with_five_papers(tmp_path: Path) -> tuple[ProjectRuntime, dict[str, str]]:
    root = tmp_path / "project"
    paper_texts: dict[str, str] = {}
    papers: list[Paper] = []
    for ordinal in range(1, 6):
        paper_id = f"P{ordinal:04d}"
        text = (
            f"{paper_id} reports concept-{ordinal}, a tested population, "
            "and a bounded thermal method."
        )
        relative = f"papers/{paper_id}/raw.md"
        _write_text(root, relative, text)
        paper_texts[paper_id] = text
        papers.append(
            Paper(
                paper_id=paper_id,
                title=f"Synthetic paper {ordinal}",
                authors=["Synthetic Author"],
                year=2025,
                doi=f"10.1000/synthetic.{ordinal}",
                journal="Synthetic Journal",
                identity_keys=[f"doi:10.1000/synthetic.{ordinal}"],
                study_group_id=None,
                related_publications=[],
                independence_status="independent",
                raw_md_path=relative,
                source_hash=hash_text(f"source:{paper_id}"),
                raw_md_hash=hash_bytes(text.encode("utf-8")),
            )
        )
    runtime = ProjectRuntime.create(
        root,
        project_name="structured-discovery",
        initial_snapshot=RepositorySnapshot(papers=tuple(papers)),
    )
    return runtime, paper_texts


def _discovery_documents(root: Path) -> tuple[list[Path], list[str]]:
    texts = [
        "Residual stress is a defined outcome. A candidate paper reports disagreement.",
        "Thermal processing is controversial and its boundary conditions remain absent.",
    ]
    paths = [
        _write_text(root, f"discovery/source-{ordinal}.md", text)
        for ordinal, text in enumerate(texts, start=1)
    ]
    return paths, texts


def _parse_proposal(document_texts: list[str]) -> DiscoveryProposalBundle:
    primary = _locator("RES0001", document_texts[0], "Residual stress")
    secondary = _locator("RES0002", document_texts[1], "controversial")
    return DiscoveryProposalBundle(
        themes=[
            ThemeProposal(
                local_ref="parsed_theme",
                title="Thermal processing",
                description="Processing effects on residual stress.",
                origin="deep_research",
            )
        ],
        claims=[
            CandidateClaimProposal(
                local_ref="parsed_claim",
                theme_ref="parsed_theme",
                candidate_claim="Thermal processing changes residual stress.",
                origin="deep_research",
                origin_refs=["RES0001"],
            )
        ],
        source_bindings=[
            DiscoverySourceBinding(target_ref="parsed_theme", locators=[primary]),
            DiscoverySourceBinding(target_ref="parsed_claim", locators=[primary]),
        ],
        terminology=[
            DiscoveryTerminologyProposal(
                local_ref="term_stress",
                term="Residual stress",
                meaning="A retained discovery definition.",
                locators=[primary],
            )
        ],
        paper_candidates=[
            DiscoveryPaperCandidateProposal(
                local_ref="candidate_paper",
                citation="A candidate paper",
                relevance="Reports disagreement.",
                locators=[primary],
            )
        ],
        controversies=[
            DiscoveryFindingProposal(
                local_ref="controversy_scale",
                summary="The effect is controversial.",
                locators=[secondary],
            )
        ],
        gaps=[
            DiscoveryFindingProposal(
                local_ref="gap_boundary",
                summary="Boundary conditions remain absent.",
                locators=[secondary],
            )
        ],
    )


def _challenge_proposal(
    paper_texts: dict[str, str], *, omit_ref: str | None = None
) -> DiscoveryProposalBundle:
    sketches = []
    for ordinal, (paper_id, text) in enumerate(sorted(paper_texts.items()), start=2):
        sketches.append(
            PaperConceptSketchProposal(
                local_ref=f"sketch_{paper_id.lower()}",
                paper_ref=paper_id,
                summary=f"Concept sketch for {paper_id}.",
                concepts=[f"concept-{ordinal - 1}"],
                populations=["tested population"],
                methods=["bounded thermal method"],
                locators=[_locator(f"RES{ordinal:04d}", text, paper_id)],
            )
        )
    findings = [
        DiscoveryCoverageFindingProposal(
            local_ref=f"coverage_{reference}",
            discovery_ref=reference,
            status="missing",
            missing_dimensions=["boundary_condition"],
            rationale="The locked corpus does not resolve this discovery item.",
        )
        for reference in PARSE_REFS
        if reference != omit_ref
    ]
    return DiscoveryProposalBundle(
        paper_concept_sketches=sketches,
        coverage_findings=findings,
    )


def _candidate_proposal() -> DiscoveryProposalBundle:
    included = {
        ("parse", "parsed_claim"),
        ("challenge", "coverage_parsed_claim"),
    }
    source_refs = [
        *(("parse", reference) for reference in PARSE_REFS),
        *(("challenge", f"coverage_{reference}") for reference in PARSE_REFS),
        *(("challenge", f"sketch_p{ordinal:04d}") for ordinal in range(1, 6)),
    ]
    return DiscoveryProposalBundle(
        themes=[
            ThemeProposal(
                local_ref="combined_theme",
                title="Combined thermal evidence",
                description="Discovery reconciled with the locked corpus.",
                origin="generated",
            )
        ],
        claims=[
            CandidateClaimProposal(
                local_ref="combined_claim",
                theme_ref="combined_theme",
                candidate_claim=(
                    "Thermal processing may change residual stress within tested "
                    "conditions."
                ),
                origin="generated",
                origin_refs=[
                    "parse:parsed_claim",
                    "challenge:coverage_parsed_claim",
                ],
            )
        ],
        input_dispositions=[
            DiscoveryInputDispositionProposal(
                source_kind=source_kind,
                source_ref=source_ref,
                disposition=(
                    "included" if (source_kind, source_ref) in included else "excluded"
                ),
                candidate_claim_refs=(
                    ["combined_claim"]
                    if (source_kind, source_ref) in included
                    else []
                ),
                reason=(
                    "Used in the bounded combined claim."
                    if (source_kind, source_ref) in included
                    else "Not selected for this bounded combined claim."
                ),
            )
            for source_kind, source_ref in source_refs
        ],
    )


def _run_parse(runtime: ProjectRuntime):
    paths, document_texts = _discovery_documents(runtime.project_root)
    proposal = _parse_proposal(document_texts)
    engine = MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])
    result = run_structured_discovery_parse(
        runtime,
        topic=TOPIC,
        document_paths=paths,
        engines=[engine],
    )
    assert result.artifact_reference is not None
    return result, engine


def _run_parse_and_challenge(
    runtime: ProjectRuntime, paper_texts: dict[str, str]
):
    parsed, _ = _run_parse(runtime)
    assert parsed.artifact_reference is not None
    challenged = run_structured_corpus_challenge(
        runtime,
        topic=TOPIC,
        discovery_artifact=parsed.artifact_reference,
        engines=[
            MockEngine(
                [
                    MockResponse(
                        proposal=_challenge_proposal(paper_texts).model_dump(
                            mode="json"
                        )
                    )
                ]
            )
        ],
    )
    assert challenged.artifact_reference is not None
    return parsed, challenged


def test_strict_discovery_chain_is_atomic_receipt_bound_and_idempotent(tmp_path):
    runtime, paper_texts = _runtime_with_five_papers(tmp_path)
    parse_input: dict[str, object] = {}
    parse_paths, document_texts = _discovery_documents(runtime.project_root)
    parse_engine = MockEngine(
        [MockResponse(proposal=_parse_proposal(document_texts).model_dump(mode="json"))],
        on_execute=lambda task, _call: parse_input.update(
            json.loads((task.input_dir / "input.json").read_text(encoding="utf-8"))
        ),
    )
    parsed = run_structured_discovery_parse(
        runtime,
        topic=TOPIC,
        document_paths=parse_paths,
        engines=[parse_engine],
    )
    assert parsed.result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert parsed.result.generation == 1
    assert parsed.result.transition is not None
    assert not parsed.result.transition.canonicalized
    assert parsed.artifact_reference is not None
    assert parse_input == {
        "topic": TOPIC,
        "document_resource_ids": ["RES0001", "RES0002"],
    }
    parsed_artifact = load_discovery_artifact(
        runtime.project_root, parsed.artifact_reference
    )
    assert set(parsed_artifact.resource_hashes) == {"RES0001", "RES0002"}

    challenge_input: dict[str, object] = {}
    challenge_engine = MockEngine(
        [
            MockResponse(
                proposal=_challenge_proposal(paper_texts).model_dump(mode="json")
            )
        ],
        on_execute=lambda task, _call: challenge_input.update(
            json.loads((task.input_dir / "input.json").read_text(encoding="utf-8"))
        ),
    )
    challenged = run_structured_corpus_challenge(
        runtime,
        topic=TOPIC,
        discovery_artifact=parsed.artifact_reference,
        engines=[challenge_engine],
    )
    assert challenged.result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert challenged.result.generation == 2
    assert challenged.artifact_reference is not None
    assert challenge_input["paper_resource_ids"] == [
        "RES0002",
        "RES0003",
        "RES0004",
        "RES0005",
        "RES0006",
    ]
    assert challenge_input["discovery_artifact"]["resource_id"] == "RES0001"
    assert str(runtime.project_root) not in json.dumps(challenge_input)
    challenged_artifact = load_discovery_artifact(
        runtime.project_root, challenged.artifact_reference
    )
    accepted_challenge = DiscoveryProposalBundle.model_validate(
        challenged_artifact.accepted_receipt.proposal_payload
    )
    assert {item.discovery_ref for item in accepted_challenge.coverage_findings} == set(
        PARSE_REFS
    )
    assert all(item.status == "missing" for item in accepted_challenge.coverage_findings)

    candidate_input: dict[str, object] = {}
    candidate_engine = MockEngine(
        [MockResponse(proposal=_candidate_proposal().model_dump(mode="json"))],
        on_execute=lambda task, _call: candidate_input.update(
            json.loads((task.input_dir / "input.json").read_text(encoding="utf-8"))
        ),
    )
    generated = run_structured_candidate_claims(
        runtime,
        topic=TOPIC,
        discovery_artifact=parsed.artifact_reference,
        challenge_artifact=challenged.artifact_reference,
        engines=[candidate_engine],
    )
    assert generated.result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert generated.result.generation == 3
    assert generated.result.allocated_ids == {
        "combined_theme": "T0001",
        "combined_claim": "C0001",
    }
    assert generated.artifact_reference is not None
    assert candidate_input["discovery_artifact"]["resource_id"] == "RES0001"
    assert candidate_input["challenge_artifact"]["resource_id"] == "RES0002"
    assert str(runtime.project_root) not in json.dumps(candidate_input)
    candidate_artifact = load_discovery_artifact(
        runtime.project_root, generated.artifact_reference
    )
    assert candidate_artifact.accepted_receipt.recorded_transition.canonicalized
    accepted_candidate = DiscoveryProposalBundle.model_validate(
        candidate_artifact.accepted_receipt.proposal_payload
    )
    assert len(accepted_candidate.input_dispositions) == len(PARSE_REFS) * 2 + 5
    assert {
        (item.source_kind, item.source_ref)
        for item in accepted_candidate.input_dispositions
        if item.disposition == "included"
    } == {
        ("parse", "parsed_claim"),
        ("challenge", "coverage_parsed_claim"),
    }

    generation, snapshot, _ = runtime.store.load_current()
    assert generation == 3
    assert len(snapshot.themes) == 1
    assert len(snapshot.candidate_claims) == 1
    assert snapshot.candidate_claims[0].origin_refs == [
        "parse:parsed_claim",
        "challenge:coverage_parsed_claim",
    ]

    calls_before_replay = candidate_engine.calls
    replay = run_structured_candidate_claims(
        runtime,
        topic=TOPIC,
        discovery_artifact=parsed.artifact_reference,
        challenge_artifact=challenged.artifact_reference,
        engines=[candidate_engine],
    )
    assert replay.result.receipt_reused
    assert not replay.result.commit_performed
    assert replay.result.generation == 3
    assert replay.artifact_reference == generated.artifact_reference
    assert candidate_engine.calls == calls_before_replay


def test_parse_rejects_malformed_locator_without_commit(tmp_path):
    runtime = ProjectRuntime.create(tmp_path / "project", project_name="bad-locator")
    paths, texts = _discovery_documents(runtime.project_root)
    proposal = _parse_proposal(texts)
    bad_locator = proposal.source_bindings[0].locators[0].model_copy(
        update={"resource_hash": "sha256:" + "f" * 64}
    )
    proposal = proposal.model_copy(
        update={
            "source_bindings": [
                proposal.source_bindings[0].model_copy(
                    update={"locators": [bad_locator]}
                ),
                proposal.source_bindings[1],
            ]
        }
    )
    run = run_structured_discovery_parse(
        runtime,
        topic=TOPIC,
        document_paths=paths,
        engines=[MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])],
    )
    assert run.result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert run.artifact_reference is None
    assert runtime.store.current_generation() == 0
    assert any(
        "resource hash differs" in error
        for record in run.result.attempt_records
        for error in record.validation_errors
    )


def test_challenger_rejects_silent_discovery_omission(tmp_path):
    runtime, paper_texts = _runtime_with_five_papers(tmp_path)
    parsed, _ = _run_parse(runtime)
    assert parsed.artifact_reference is not None
    challenged = run_structured_corpus_challenge(
        runtime,
        topic=TOPIC,
        discovery_artifact=parsed.artifact_reference,
        engines=[
            MockEngine(
                [
                    MockResponse(
                        proposal=_challenge_proposal(
                            paper_texts, omit_ref="gap_boundary"
                        ).model_dump(mode="json")
                    )
                ]
            )
        ],
    )
    assert challenged.result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert challenged.artifact_reference is None
    assert runtime.store.current_generation() == 1
    assert any(
        "cover every exact discovery concept" in error
        for record in challenged.result.attempt_records
        for error in record.validation_errors
    )


@pytest.mark.parametrize("defect", ["omitted", "excluded_used_input"])
def test_candidate_generation_requires_an_exact_input_disposition_partition(
    tmp_path, defect
):
    runtime, paper_texts = _runtime_with_five_papers(tmp_path)
    parsed, challenged = _run_parse_and_challenge(runtime, paper_texts)
    proposal = _candidate_proposal()
    dispositions = list(proposal.input_dispositions)
    if defect == "omitted":
        dispositions.pop()
    else:
        index = next(
            position
            for position, item in enumerate(dispositions)
            if (item.source_kind, item.source_ref) == ("parse", "parsed_claim")
        )
        dispositions[index] = dispositions[index].model_copy(
            update={"disposition": "excluded", "candidate_claim_refs": []}
        )
    proposal = proposal.model_copy(update={"input_dispositions": dispositions})
    result = run_structured_candidate_claims(
        runtime,
        topic=TOPIC,
        discovery_artifact=parsed.artifact_reference,
        challenge_artifact=challenged.artifact_reference,
        engines=[MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])],
    )
    assert result.result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert result.artifact_reference is None
    assert runtime.store.current_generation() == 2
    errors = [
        error
        for record in result.result.attempt_records
        for error in record.validation_errors
    ]
    assert any(
        expected in error
        for expected in (
            "explicitly include or exclude every",
            "excluded discovery input cannot remain",
        )
        for error in errors
    )


def test_challenger_rejects_wrong_locked_paper_source_hash_before_engine(tmp_path):
    runtime, paper_texts = _runtime_with_five_papers(tmp_path)
    parsed, _ = _run_parse(runtime)
    assert parsed.artifact_reference is not None
    _, snapshot, _ = runtime.store.load_current()
    papers = tuple(sorted(snapshot.papers, key=lambda item: item.paper_id))
    source_hashes = [paper.source_hash for paper in papers]
    source_hashes[0] = "sha256:" + "f" * 64
    invocation = CorpusChallengerInvocation(
        topic=TOPIC,
        paper_ids=[paper.paper_id for paper in papers],
        discovery_artifact=parsed.artifact_reference.invocation(runtime.project_root),
        paper_resource_paths=[runtime.project_root / paper.raw_md_path for paper in papers],
        paper_source_hashes=source_hashes,
    )
    adapter = StructuredCorpusChallengeAdapter(runtime.project_root, invocation)
    engine = MockEngine(
        [MockResponse(proposal=_challenge_proposal(paper_texts).model_dump(mode="json"))]
    )
    result = runtime.run(
        TaskType.CORPUS_CHALLENGER,
        invocation,
        engines=[engine],
        promotion_adapter=adapter,
    )
    assert result.outcome is AttemptOutcome.INTERNAL_RUNTIME_FAILURE
    assert result.generation is None
    assert engine.calls == 0
    assert runtime.store.current_generation() == 1
    assert result.receipt_rejection_reason is not None
    assert "raw resource differs for P0001" in result.receipt_rejection_reason


@pytest.mark.parametrize("field", ["start_offset", "end_offset"])
def test_locator_offsets_are_ordered(field):
    values = {
        "resource_id": "RES0001",
        "resource_hash": "sha256:" + "a" * 64,
        "start_offset": 2,
        "end_offset": 3,
        "source_span_hash": "sha256:" + "b" * 64,
    }
    values[field] = 3 if field == "start_offset" else 2
    with pytest.raises(ValueError):
        ResourceTextLocator(**values)
