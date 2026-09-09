from __future__ import annotations

import json
from pathlib import Path

from vibereview.models import CandidateClaim, ClaimAssessment, ThemeRecord
from vibereview.runtime.coupled import CoupledCommitPlan
from vibereview.runtime.dto import RevisedClaimProposal, ReviseClaimInvocation
from vibereview.runtime.engine import MockEngine, MockResponse
from vibereview.runtime.kernel import ProjectRuntime
from vibereview.runtime.records import AttemptOutcome, RuntimeConfig, TaskType
from vibereview.runtime.repository import (
    GenerationStore,
    PromotionPayload,
    StaleSnapshotError,
)
from vibereview.runtime.state import RepositorySnapshot


class _NoOpCoupledAdapter:
    task_type = TaskType.REVISE_CLAIM
    promotion_handler = "promote_revised_claim"
    promotion_fingerprint = "sha256:" + "1" * 64
    rebuild_on_stale = False

    def __init__(self) -> None:
        self.validations = 0
        self.reuse_checks = 0
        self.pre_execution_checks = 0
        self.reject_reuse = False
        self.reject_pre_execution_as_stale = False

    def validate_pre_execution(self, **_kwargs) -> None:
        self.pre_execution_checks += 1
        if self.reject_pre_execution_as_stale:
            raise StaleSnapshotError("synthetic coupled task is stale")

    def validate_proposal(self, *, proposal, invocation, **_kwargs) -> None:
        self.validations += 1
        assert isinstance(proposal, RevisedClaimProposal)
        assert isinstance(invocation, ReviseClaimInvocation)
        if proposal.claim_ref != invocation.claim_id:
            raise ValueError("proposal claim differs from the invocation")

    def prepare_commit(self, **_kwargs) -> CoupledCommitPlan:
        def promote(snapshot, registry):
            return PromotionPayload(snapshot, registry, {})

        def materialize(writer, _generation, _payload, receipt) -> None:
            writer.write_bytes(
                "coupled/receipt.json",
                json.dumps(
                    {"semantic_task_key": receipt.semantic_task_key},
                    sort_keys=True,
                ).encode("utf-8"),
            )

        return CoupledCommitPlan(promote, materialize)

    def verify_receipt_reuse(
        self, *, project_root: Path, receipt, **_kwargs
    ) -> tuple[bool, str | None]:
        self.reuse_checks += 1
        _, _, auxiliary = GenerationStore(project_root).load_generation_auxiliary(
            receipt.committed_generation, demonstration_paths()
        )
        observed = json.loads(auxiliary["coupled/receipt.json"])
        return (
            not self.reject_reuse
            and observed == {"semantic_task_key": receipt.semantic_task_key},
            "coupled receipt artifact changed",
        )


def demonstration_paths() -> set[str]:
    return {"coupled/receipt.json"}


def _runtime(tmp_path: Path, *, receipts: bool = True) -> ProjectRuntime:
    snapshot = RepositorySnapshot(
        themes=(
            ThemeRecord(
                theme_id="T0001",
                title="Synthetic theme",
                description="Synthetic adapter fixture.",
                origin="human",
                parent_theme_id=None,
            ),
        ),
        candidate_claims=(
            CandidateClaim(
                claim_id="C0001",
                theme_id="T0001",
                candidate_claim="A synthetic claim.",
                origin="human",
                origin_refs=[],
            ),
        ),
        claim_assessments=(
            ClaimAssessment(
                claim_id="C0001",
                aggregate_strength="unknown",
                evidence_sufficiency="unclear",
                decision="REFORMULATE",
                rejection_basis=None,
                support_summary="",
                contradiction_summary="",
                qualification_summary="Synthetic fixture requires revision.",
                reason="Exercise the coupled runtime seam.",
            ),
        ),
    )
    return ProjectRuntime.create(
        tmp_path / "project",
        project_name="coupled-adapter-test",
        initial_snapshot=snapshot,
        config=RuntimeConfig(enable_receipts=receipts),
    )


def _invocation() -> ReviseClaimInvocation:
    return ReviseClaimInvocation(
        claim_id="C0001", current_candidate_claim="A synthetic claim."
    )


def _engine() -> MockEngine:
    proposal = RevisedClaimProposal(
        claim_ref="C0001", final_claim="A bounded synthetic claim."
    )
    return MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])


def test_coupled_adapter_commits_receipt_and_auxiliary_together(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    adapter = _NoOpCoupledAdapter()
    engine = _engine()

    result = runtime.run(
        TaskType.REVISE_CLAIM,
        _invocation(),
        engines=[engine],
        promotion_adapter=adapter,
    )

    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert result.generation == 1
    assert result.commit_performed is True
    assert result.transition is not None
    assert result.transition.canonicalized is False
    assert engine.calls == 1
    assert adapter.pre_execution_checks == 1
    receipts = runtime.store.load_receipts(1)
    assert len(receipts) == 1
    _, _, auxiliary = runtime.store.load_generation_auxiliary(
        1, demonstration_paths()
    )
    assert json.loads(auxiliary["coupled/receipt.json"]) == {
        "semantic_task_key": receipts[0].semantic_task_key
    }


def test_coupled_adapter_receipt_reuse_calls_domain_verifier(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    adapter = _NoOpCoupledAdapter()
    engine = _engine()
    first = runtime.run(
        TaskType.REVISE_CLAIM,
        _invocation(),
        engines=[engine],
        promotion_adapter=adapter,
    )
    second = runtime.run(
        TaskType.REVISE_CLAIM,
        _invocation(),
        engines=[engine],
        promotion_adapter=adapter,
    )

    assert first.generation == second.generation == 1
    assert second.receipt_reused is True
    assert second.commit_performed is False
    assert second.reused_generation == 1
    assert second.transition is not None
    assert second.transition.canonicalized is False
    assert second.transition == first.transition
    assert adapter.reuse_checks == 1
    assert adapter.pre_execution_checks == 1
    assert engine.calls == 1


def test_missing_or_mismatched_adapter_fails_before_engine(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    engine = _engine()
    without = runtime.run(TaskType.REVISE_CLAIM, _invocation(), engines=[engine])
    assert without.outcome is AttemptOutcome.TASK_TYPE_NOT_IMPLEMENTED
    assert engine.calls == 0

    adapter = _NoOpCoupledAdapter()
    adapter.task_type = TaskType.ASSESS_EVIDENCE
    wrong = runtime.run(
        TaskType.REVISE_CLAIM,
        _invocation(),
        engines=[engine],
        promotion_adapter=adapter,
    )
    assert wrong.outcome is AttemptOutcome.TASK_TYPE_NOT_IMPLEMENTED
    assert engine.calls == 0


def test_coupled_adapter_requires_receipts_before_engine(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, receipts=False)
    engine = _engine()
    result = runtime.run(
        TaskType.REVISE_CLAIM,
        _invocation(),
        engines=[engine],
        promotion_adapter=_NoOpCoupledAdapter(),
    )
    assert result.outcome is AttemptOutcome.TASK_TYPE_NOT_IMPLEMENTED
    assert engine.calls == 0


def test_exact_coupled_receipt_integrity_failure_never_reexecutes_engine(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    adapter = _NoOpCoupledAdapter()
    engine = _engine()
    first = runtime.run(
        TaskType.REVISE_CLAIM,
        _invocation(),
        engines=[engine],
        promotion_adapter=adapter,
    )
    adapter.reject_reuse = True

    second = runtime.run(
        TaskType.REVISE_CLAIM,
        _invocation(),
        engines=[engine],
        promotion_adapter=adapter,
    )

    assert first.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert second.outcome is AttemptOutcome.INTERNAL_RUNTIME_FAILURE
    assert second.receipt_reused is False
    assert second.commit_performed is False
    assert second.receipt_rejection_reason == "coupled receipt artifact changed"
    assert runtime.store.current_generation() == 1
    assert engine.calls == 1


def test_noncanonical_adapter_fingerprint_fails_before_engine(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    adapter = _NoOpCoupledAdapter()
    adapter.promotion_fingerprint = "sha256:" + "z" * 64
    engine = _engine()

    result = runtime.run(
        TaskType.REVISE_CLAIM,
        _invocation(),
        engines=[engine],
        promotion_adapter=adapter,
    )

    assert result.outcome is AttemptOutcome.TASK_TYPE_NOT_IMPLEMENTED
    assert engine.calls == 0


def test_coupled_stale_pre_execution_check_never_calls_engine(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    adapter = _NoOpCoupledAdapter()
    adapter.reject_pre_execution_as_stale = True
    engine = _engine()

    result = runtime.run(
        TaskType.REVISE_CLAIM,
        _invocation(),
        engines=[engine],
        promotion_adapter=adapter,
    )

    assert result.outcome is AttemptOutcome.STALE_SNAPSHOT
    assert result.generation is None
    assert result.attempt_records == []
    assert result.commit_performed is False
    assert adapter.pre_execution_checks == 1
    assert engine.calls == 0
