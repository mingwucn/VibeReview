from __future__ import annotations

import pytest
from pydantic import ValidationError

from vibereview import runtime as runtime_api
from vibereview.ids import candidate_claim_hash
from vibereview.models import CandidateClaim, RetrievalQuery, ThemeRecord
from vibereview.runtime import (
    AttemptOutcome,
    GenerateRetrievalQueriesInvocation,
    MockEngine,
    MockResponse,
    ProjectRuntime,
    RepositorySnapshot,
    RetrievalQueryProposal,
    RetrievalQueryProposalBundle,
    TaskType,
)
from vibereview.runtime.dto import (
    MAX_RETRIEVAL_QUERY_CHARS,
    MAX_RETRIEVAL_QUERY_PROPOSALS,
    MAX_RETRIEVAL_QUERY_SCOPE_CLAIMS,
    MAX_RETRIEVAL_QUERY_UTF8_BYTES,
)
from vibereview.runtime.promotion import ProposalValidationError, validate_proposal
from vibereview.runtime.registry import CanonicalIdRegistry
from vibereview.runtime.specs import TASK_SPECS


def _snapshot_with_claims(*claim_texts: str) -> RepositorySnapshot:
    theme = ThemeRecord(
        theme_id="T0001",
        title="Synthetic theme",
        description="Fictitious task fixture",
        origin="generated",
        parent_theme_id=None,
    )
    claims = tuple(
        CandidateClaim(
            claim_id=f"C{ordinal:04d}",
            theme_id=theme.theme_id,
            candidate_claim=text,
            origin="generated",
            origin_refs=[],
        )
        for ordinal, text in enumerate(claim_texts, start=1)
    )
    return RepositorySnapshot(themes=(theme,), candidate_claims=claims)


def _query_bundle(*queries: tuple[str, str, str, str]) -> RetrievalQueryProposalBundle:
    return RetrievalQueryProposalBundle(
        queries=[
            RetrievalQueryProposal(
                local_ref=local_ref,
                claim_ref=claim_ref,
                intent=intent,
                query_text=query_text,
            )
            for local_ref, claim_ref, intent, query_text in queries
        ]
    )


def _required_queries(claim_ref: str = "C0001") -> tuple[tuple[str, str, str, str], ...]:
    return (
        (f"{claim_ref}_support", claim_ref, "support", "synthetic support query"),
        (
            f"{claim_ref}_contradiction",
            claim_ref,
            "contradiction",
            "synthetic contradiction query",
        ),
        (f"{claim_ref}_boundary", claim_ref, "boundary", "synthetic boundary query"),
        (
            f"{claim_ref}_alternative",
            claim_ref,
            "alternative",
            "synthetic alternative query",
        ),
    )


def _runtime(tmp_path, snapshot: RepositorySnapshot) -> ProjectRuntime:
    return ProjectRuntime.create(
        tmp_path / "project",
        project_name="synthetic-retrieval-query-task",
        initial_snapshot=snapshot,
    )


def test_retrieval_query_proposals_are_public_runtime_exports():
    assert {
        "RetrievalQueryProposal",
        "RetrievalQueryProposalBundle",
    } <= set(runtime_api.__all__)


@pytest.mark.parametrize(
    ("claim_ids", "expected_error"),
    [
        ([], "at least 1"),
        (["C0001", "C0001"], "claim_ids must be unique"),
    ],
)
def test_query_invocation_requires_nonempty_unique_claims(
    tmp_path, claim_ids, expected_error
):
    runtime = _runtime(tmp_path, _snapshot_with_claims("Synthetic claim."))
    engine = MockEngine([], name="unused")

    with pytest.raises(ValidationError, match=expected_error):
        runtime.run(
            TaskType.GENERATE_RETRIEVAL_QUERIES,
            {"claim_ids": claim_ids},
            engines=[engine],
        )

    assert engine.calls == 0
    assert runtime.store.current_generation() == 0


def test_query_task_allocates_deterministic_per_intent_ordinals(tmp_path):
    claim_text = "Synthetic treatment changes a fictitious response."
    runtime = _runtime(tmp_path, _snapshot_with_claims(claim_text))
    proposal = _query_bundle(
        ("support_1", "C0001", "support", "synthetic supporting result"),
        (
            "contradiction_1",
            "C0001",
            "contradiction",
            "synthetic contradictory result",
        ),
        ("support_2", "C0001", "support", "synthetic replication result"),
        ("boundary_1", "C0001", "boundary", "synthetic boundary result"),
        ("alternative_1", "C0001", "alternative", "synthetic alternative result"),
    )

    result = runtime.run(
        TaskType.GENERATE_RETRIEVAL_QUERIES,
        GenerateRetrievalQueriesInvocation(claim_ids=["C0001"]),
        engines=[MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])],
    )

    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert result.transition is not None
    assert result.transition.canonicalized
    assert result.transition.downstream_eligible
    assert result.transition.scientific_disposition == "VALID"
    assert result.allocated_ids == {
        "support_1": "Q-C0001-SUP-01",
        "contradiction_1": "Q-C0001-CON-01",
        "support_2": "Q-C0001-SUP-02",
        "boundary_1": "Q-C0001-BND-01",
        "alternative_1": "Q-C0001-ALT-01",
    }

    generation, snapshot, registry = runtime.store.load_current()
    assert generation == 1
    assert [query.query_id for query in snapshot.retrieval_queries] == [
        "Q-C0001-SUP-01",
        "Q-C0001-CON-01",
        "Q-C0001-SUP-02",
        "Q-C0001-BND-01",
        "Q-C0001-ALT-01",
    ]
    assert all(
        query.candidate_claim_hash == candidate_claim_hash(claim_text)
        for query in snapshot.retrieval_queries
    )
    assert registry.query_ordinals == {
        "C0001:SUP": 2,
        "C0001:CON": 1,
        "C0001:BND": 1,
        "C0001:ALT": 1,
    }
    snapshot.validate_repository()


def test_query_task_appends_without_reallocating_existing_query(tmp_path):
    claim_text = "Synthetic treatment changes a fictitious response."
    snapshot = _snapshot_with_claims(claim_text).model_copy(
        update={
            "retrieval_queries": (
                RetrievalQuery(
                    query_id="Q-C0001-SUP-01",
                    claim_id="C0001",
                    candidate_claim_hash=candidate_claim_hash(claim_text),
                    intent="support",
                    query_text="existing synthetic query",
                ),
            )
        }
    )
    runtime = _runtime(tmp_path, snapshot)
    proposal = _query_bundle(
        ("new_support", "C0001", "support", "new synthetic supporting result"),
        *_required_queries()[1:],
    )

    result = runtime.run(
        TaskType.GENERATE_RETRIEVAL_QUERIES,
        GenerateRetrievalQueriesInvocation(claim_ids=["C0001"]),
        engines=[MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])],
    )

    assert result.allocated_ids == {
        "new_support": "Q-C0001-SUP-02",
        "C0001_contradiction": "Q-C0001-CON-01",
        "C0001_boundary": "Q-C0001-BND-01",
        "C0001_alternative": "Q-C0001-ALT-01",
    }
    _, current, registry = runtime.store.load_current()
    assert current.retrieval_queries[0] == snapshot.retrieval_queries[0]
    assert [query.query_id for query in current.retrieval_queries[1:]] == [
        "Q-C0001-SUP-02",
        "Q-C0001-CON-01",
        "Q-C0001-BND-01",
        "Q-C0001-ALT-01",
    ]
    assert registry.query_ordinals == {
        "C0001:SUP": 2,
        "C0001:CON": 1,
        "C0001:BND": 1,
        "C0001:ALT": 1,
    }
    current.validate_repository()


@pytest.mark.parametrize(
    ("claim_ref", "expected_error"),
    [
        ("C9999", "unknown claim_ref C9999"),
        ("C0002", "claim_ref C0002 is outside the task dependency scope"),
    ],
)
def test_query_task_rejects_unknown_or_out_of_scope_claim(
    tmp_path, claim_ref, expected_error
):
    runtime = _runtime(
        tmp_path,
        _snapshot_with_claims(
            "First synthetic claim.",
            "Second synthetic claim.",
        ),
    )
    proposal = _query_bundle(
        ("query_1", claim_ref, "support", "synthetic scoped query")
    )
    engine = MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])

    result = runtime.run(
        TaskType.GENERATE_RETRIEVAL_QUERIES,
        GenerateRetrievalQueriesInvocation(claim_ids=["C0001"]),
        engines=[engine],
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert result.generation is None
    assert not result.commit_performed
    assert engine.calls == 1
    assert expected_error in result.attempt_records[0].validation_errors[0]
    assert runtime.store.current_generation() == 0


def test_query_task_rejects_duplicate_local_references(tmp_path):
    runtime = _runtime(tmp_path, _snapshot_with_claims("Synthetic claim."))
    proposal = _query_bundle(
        ("duplicate", "C0001", "support", "synthetic query one"),
        ("duplicate", "C0001", "boundary", "synthetic query two"),
    )

    result = runtime.run(
        TaskType.GENERATE_RETRIEVAL_QUERIES,
        GenerateRetrievalQueriesInvocation(claim_ids=["C0001"]),
        engines=[MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])],
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert "local_ref must be unique" in result.attempt_records[0].validation_errors[0]
    assert runtime.store.current_generation() == 0


def test_missing_invocation_claim_fails_before_engine_use(tmp_path):
    runtime = _runtime(tmp_path, _snapshot_with_claims("Synthetic claim."))
    engine = MockEngine([], name="unused")

    with pytest.raises(KeyError, match="CandidateClaim:C9999"):
        runtime.run(
            TaskType.GENERATE_RETRIEVAL_QUERIES,
            GenerateRetrievalQueriesInvocation(claim_ids=["C9999"]),
            engines=[engine],
        )

    assert engine.calls == 0
    assert runtime.store.current_generation() == 0


def test_query_task_receipt_reuse_skips_engine_and_commit(tmp_path):
    runtime = _runtime(tmp_path, _snapshot_with_claims("Synthetic claim."))
    invocation = GenerateRetrievalQueriesInvocation(claim_ids=["C0001"])
    proposal = _query_bundle(*_required_queries())
    first_engine = MockEngine(
        [MockResponse(proposal=proposal.model_dump(mode="json"))], name="stable"
    )
    first = runtime.run(
        TaskType.GENERATE_RETRIEVAL_QUERIES,
        invocation,
        engines=[first_engine],
    )
    assert first.commit_performed
    assert first.generation == 1

    second_engine = MockEngine(
        [MockResponse(proposal=proposal.model_dump(mode="json"))], name="stable"
    )
    second = runtime.run(
        TaskType.GENERATE_RETRIEVAL_QUERIES,
        invocation,
        engines=[second_engine],
    )

    assert second.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert second.receipt_reused
    assert not second.commit_performed
    assert second.generation == 1
    assert second.reused_generation == 1
    assert second.allocated_ids == {
        "C0001_support": "Q-C0001-SUP-01",
        "C0001_contradiction": "Q-C0001-CON-01",
        "C0001_boundary": "Q-C0001-BND-01",
        "C0001_alternative": "Q-C0001-ALT-01",
    }
    assert second_engine.calls == 0
    assert runtime.store.current_generation() == 1


def test_nonempty_invocation_rejects_empty_query_bundle(tmp_path):
    runtime = _runtime(tmp_path, _snapshot_with_claims("Synthetic claim."))
    proposal = RetrievalQueryProposalBundle(queries=[])

    result = runtime.run(
        TaskType.GENERATE_RETRIEVAL_QUERIES,
        GenerateRetrievalQueriesInvocation(claim_ids=["C0001"]),
        engines=[MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])],
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert result.allocated_ids == {}
    assert "missing required intents" in result.attempt_records[0].validation_errors[0]
    assert runtime.store.current_generation() == 0


def test_query_bundle_must_cover_every_invoked_claim(tmp_path):
    runtime = _runtime(
        tmp_path,
        _snapshot_with_claims("First synthetic claim.", "Second synthetic claim."),
    )
    proposal = _query_bundle(*_required_queries("C0001"))

    result = runtime.run(
        TaskType.GENERATE_RETRIEVAL_QUERIES,
        GenerateRetrievalQueriesInvocation(claim_ids=["C0001", "C0002"]),
        engines=[MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])],
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert "C0002" in result.attempt_records[0].validation_errors[0]
    assert runtime.store.current_generation() == 0


def test_query_bundle_requires_all_four_baseline_intents(tmp_path):
    runtime = _runtime(tmp_path, _snapshot_with_claims("Synthetic claim."))
    proposal = _query_bundle(*_required_queries()[:-1])

    result = runtime.run(
        TaskType.GENERATE_RETRIEVAL_QUERIES,
        GenerateRetrievalQueriesInvocation(claim_ids=["C0001"]),
        engines=[MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])],
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    error = result.attempt_records[0].validation_errors[0]
    assert "C0001: alternative" in error
    assert runtime.store.current_generation() == 0


@pytest.mark.parametrize(
    "unusable_query_text",
    [
        "a an of _ 12",
        "___ --",
        "é à 22",
    ],
)
def test_query_text_requires_a_usable_deterministic_retrieval_term(
    tmp_path, unusable_query_text
):
    runtime = _runtime(tmp_path, _snapshot_with_claims("Synthetic claim."))
    proposal = _query_bundle(
        (
            "C0001_support",
            "C0001",
            "support",
            unusable_query_text,
        ),
        *_required_queries()[1:],
    )
    engine = MockEngine(
        [MockResponse(proposal=proposal.model_dump(mode="json"))]
    )

    result = runtime.run(
        TaskType.GENERATE_RETRIEVAL_QUERIES,
        GenerateRetrievalQueriesInvocation(claim_ids=["C0001"]),
        engines=[engine],
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert "no usable deterministic retrieval terms" in (
        result.attempt_records[0].validation_errors[0]
    )
    assert engine.calls == 1
    assert not result.commit_performed
    assert runtime.store.current_generation() == 0


def test_query_text_budget_rejects_4097_characters_before_commit(tmp_path):
    runtime = _runtime(tmp_path, _snapshot_with_claims("Synthetic claim."))
    raw_proposal = {
        "queries": [
            {
                "local_ref": "too_long",
                "claim_ref": "C0001",
                "intent": "support",
                "query_text": "x" * (MAX_RETRIEVAL_QUERY_CHARS + 1),
            }
        ]
    }

    result = runtime.run(
        TaskType.GENERATE_RETRIEVAL_QUERIES,
        GenerateRetrievalQueriesInvocation(claim_ids=["C0001"]),
        engines=[MockEngine([MockResponse(proposal=raw_proposal)])],
    )

    assert result.outcome is AttemptOutcome.ENGINE_SCHEMA_FAILURE
    assert not result.commit_performed
    assert runtime.store.current_generation() == 0


def test_query_text_budget_accepts_exact_char_and_utf8_boundaries():
    text = "\U0001f642" * MAX_RETRIEVAL_QUERY_CHARS
    proposal = RetrievalQueryProposal(
        local_ref="exact_boundary",
        claim_ref="C0001",
        intent="support",
        query_text=text,
    )

    assert len(proposal.query_text) == MAX_RETRIEVAL_QUERY_CHARS
    assert len(proposal.query_text.encode("utf-8")) == MAX_RETRIEVAL_QUERY_UTF8_BYTES


def test_query_invocation_and_proposal_lists_are_schema_bounded():
    with pytest.raises(ValidationError, match="at most 50"):
        GenerateRetrievalQueriesInvocation(
            claim_ids=[
                f"C{ordinal:04d}"
                for ordinal in range(1, MAX_RETRIEVAL_QUERY_SCOPE_CLAIMS + 2)
            ]
        )

    repeated = {
        "local_ref": "bounded_query",
        "claim_ref": "C0001",
        "intent": "support",
        "query_text": "synthetic supporting query",
    }
    with pytest.raises(ValidationError, match="at most 600"):
        RetrievalQueryProposalBundle.model_validate(
            {"queries": [repeated] * (MAX_RETRIEVAL_QUERY_PROPOSALS + 1)}
        )


def test_query_bundle_rejects_per_claim_intent_ordinal_exhaustion(tmp_path):
    runtime = _runtime(tmp_path, _snapshot_with_claims("Synthetic claim."))
    proposal = _query_bundle(
        *(
            (
                f"support_{ordinal}",
                "C0001",
                "support",
                f"synthetic supporting query {ordinal}",
            )
            for ordinal in range(100)
        )
    )

    result = runtime.run(
        TaskType.GENERATE_RETRIEVAL_QUERIES,
        GenerateRetrievalQueriesInvocation(claim_ids=["C0001"]),
        engines=[MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])],
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert "capacity for C0001/support" in (
        result.attempt_records[0].validation_errors[0]
    )
    assert not result.commit_performed
    assert runtime.store.current_generation() == 0


def test_query_bundle_has_finite_total_scope_capacity():
    snapshot = _snapshot_with_claims("Synthetic claim.")
    intents = (
        "support",
        "contradiction",
        "boundary",
        "alternative",
        "method_challenge",
        "null_result",
    )
    proposal = _query_bundle(
        *(
            (
                f"query_{ordinal}",
                "C0001",
                intents[ordinal % len(intents)],
                f"synthetic bounded query {ordinal}",
            )
            for ordinal in range(595)
        )
    )

    with pytest.raises(ProposalValidationError, match="total remaining"):
        validate_proposal(
            TASK_SPECS[TaskType.GENERATE_RETRIEVAL_QUERIES],
            proposal,
            snapshot,
            dependency_keys=("CandidateClaim:C0001",),
            registry=CanonicalIdRegistry.from_identifiers(
                snapshot.all_identifiers()
            ),
        )


def test_query_capacity_uses_existing_registry_ordinal(tmp_path):
    claim_text = "Synthetic claim."
    snapshot = _snapshot_with_claims(claim_text).model_copy(
        update={
            "retrieval_queries": (
                RetrievalQuery(
                    query_id="Q-C0001-SUP-99",
                    claim_id="C0001",
                    candidate_claim_hash=candidate_claim_hash(claim_text),
                    intent="support",
                    query_text="last available synthetic query",
                ),
            )
        }
    )
    runtime = _runtime(tmp_path, snapshot)
    proposal = _query_bundle(
        ("overflow", "C0001", "support", "one query beyond capacity")
    )

    result = runtime.run(
        TaskType.GENERATE_RETRIEVAL_QUERIES,
        GenerateRetrievalQueriesInvocation(claim_ids=["C0001"]),
        engines=[MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])],
    )

    assert result.outcome is AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    assert "capacity for C0001/support" in (
        result.attempt_records[0].validation_errors[0]
    )
    assert runtime.store.current_generation() == 0
