from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from vibereview.runtime.confinement import ConfinementLevel
from vibereview.runtime.diagnostics import DiagnosticCapture
from vibereview.runtime.execution import (
    SubprocessAgentResult,
    SubprocessExecutionResult,
)
from vibereview.runtime.execution_inventory import ExecutionFileRecord
from vibereview.runtime.hashing import (
    canonical_json_bytes,
    hash_bytes,
    hash_json,
    hash_text,
)
from vibereview.runtime.pilot_records import FivePaperPilotBudget
from vibereview.runtime.pilot_usage import (
    MAX_PILOT_ATTEMPTS_PER_TASK,
    PilotTaskUsageArtifact,
    PilotUsageError,
    build_pilot_task_usage,
    canonical_pilot_task_usage_bytes,
    collect_pilot_task_usage,
)
from vibereview.runtime.records import AgentResult, AttemptOutcome, TaskAttemptRecord


TASK_ID = "TASK0001"


def _attempt_root(project: Path, attempt_id: str) -> Path:
    root = project / "work" / "tasks" / TASK_ID / "attempts" / attempt_id
    root.mkdir(parents=True)
    (root / "workspace").mkdir()
    return root


def _capture(filename: str, content: bytes) -> DiagnosticCapture:
    return DiagnosticCapture(
        relative_path=Path("attempt") / filename,
        bytes_observed=len(content),
        bytes_retained=len(content),
        truncated=False,
        retained_redacted_hash=hash_bytes(content),
        redactions_applied=0,
    )


def _report(
    *,
    stdout: bytes,
    stderr: bytes,
    inventory: tuple[ExecutionFileRecord, ...],
) -> SubprocessExecutionResult:
    return SubprocessExecutionResult(
        argv=("synthetic-worker",),
        execution_root=Path("/private/synthetic/execution-root"),
        confinement_level=ConfinementLevel.TEST_ONLY,
        exit_code=0,
        timed_out=False,
        launch_error=None,
        duration_seconds=0.01,
        resource_limit_breached=False,
        bundle_modified=False,
        output_policy_violated=False,
        proposal_format_invalid=False,
        primary_outcome=AttemptOutcome.VALID_SCIENTIFIC_RESULT,
        detected_failures=(),
        stdout_capture=_capture("stdout.txt", stdout),
        stderr_capture=_capture("stderr.txt", stderr),
        inventory=inventory,
    )


def _write_attempt(
    project: Path,
    attempt_id: str,
    *,
    output: str,
    stdout: str = "",
    stderr: str = "",
    outcome: AttemptOutcome,
    accepted: bool = False,
    write_record: bool = True,
    write_proposal: bool = False,
    inventory: tuple[ExecutionFileRecord, ...] | None = None,
    execution_error: str | None = None,
    record_output_hash: str | None | object = ...,
) -> Path:
    root = _attempt_root(project, attempt_id)
    stdout_bytes = stdout.encode("utf-8")
    stderr_bytes = stderr.encode("utf-8")
    if inventory is None:
        result: AgentResult = AgentResult(
            engine=f"synthetic-{attempt_id[-1]}",
            engine_version="1",
            execution_succeeded=(
                outcome is not AttemptOutcome.ENGINE_EXECUTION_FAILURE
            ),
            output_text=output,
            stdout=stdout,
            stderr=stderr,
            execution_error=execution_error,
        )
    else:
        result = SubprocessAgentResult(
            engine=f"synthetic-{attempt_id[-1]}",
            engine_version="1",
            execution_succeeded=True,
            output_text=output,
            stdout=stdout,
            stderr=stderr,
            execution_error=execution_error,
            execution_report=_report(
                stdout=stdout_bytes,
                stderr=stderr_bytes,
                inventory=inventory,
            ),
        )
    (root / "agent_result.json").write_text(
        result.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (root / "stdout.txt").write_bytes(stdout_bytes)
    (root / "stderr.txt").write_bytes(stderr_bytes)
    if write_proposal:
        payload = json.loads(output)
        (root / "proposal.json").write_bytes(canonical_json_bytes(payload))
    if write_record:
        if record_output_hash is ...:
            selected_hash = hash_text(output) if output else None
        else:
            selected_hash = record_output_hash
        record = TaskAttemptRecord(
            task_id=TASK_ID,
            attempt_id=attempt_id,
            engine=result.engine,
            engine_version=result.engine_version,
            outcome=outcome,
            format_valid=outcome
            not in {
                AttemptOutcome.ENGINE_EXECUTION_FAILURE,
                AttemptOutcome.ENGINE_FORMAT_FAILURE,
            },
            schema_valid=outcome
            not in {
                AttemptOutcome.ENGINE_EXECUTION_FAILURE,
                AttemptOutcome.ENGINE_FORMAT_FAILURE,
                AttemptOutcome.ENGINE_SCHEMA_FAILURE,
            },
            proposal_validation_valid=(True if accepted else None),
            accepted_attempt=accepted,
            validation_errors=[] if accepted else ["synthetic failure"],
            output_hash=selected_hash,
            agent_result_path=Path(
                f"work/tasks/{TASK_ID}/attempts/{attempt_id}/agent_result.json"
            ),
        )
        (root / "attempt_record.json").write_text(
            record.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    return root


def _small_budget(**updates: int) -> FivePaperPilotBudget:
    values = dict(
        max_proposal_bytes=256,
        max_stdout_bytes_per_attempt=16,
        max_stderr_bytes_per_attempt=16,
        max_retained_diagnostic_bytes_per_attempt=32,
        max_writable_entries_per_attempt=8,
        max_writable_tree_bytes_per_attempt=256,
        max_cumulative_stdout_bytes=32,
        max_cumulative_stderr_bytes=32,
        max_cumulative_retained_diagnostic_bytes=64,
        max_cumulative_writable_entries=16,
        max_cumulative_writable_tree_bytes=512,
    )
    values.update(updates)
    return FivePaperPilotBudget(**values)


def test_failed_then_success_usage_includes_every_attempt_and_infers_current(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    accepted_output = '{"accepted":true}'
    # Create in reverse order to prove filesystem creation order is irrelevant.
    _write_attempt(
        project,
        "02-success",
        output=accepted_output,
        stdout="ok\n",
        outcome=AttemptOutcome.VALID_SCIENTIFIC_RESULT,
        write_record=False,
        write_proposal=True,
    )
    _write_attempt(
        project,
        "01-failed",
        output="not-json",
        stderr="retry\n",
        outcome=AttemptOutcome.ENGINE_FORMAT_FAILURE,
    )

    usage = collect_pilot_task_usage(
        project,
        task_id=TASK_ID,
        budget=_small_budget(),
        accepted_attempt_id=f"{TASK_ID}/02-success",
    )

    assert tuple(item.attempt_id for item in usage.attempts) == (
        "01-failed",
        "02-success",
    )
    assert [item.accepted_attempt for item in usage.attempts] == [False, True]
    assert usage.attempts[0].attempt_record_inferred is False
    assert usage.attempts[1].attempt_record_inferred is True
    assert usage.attempts[1].attempt_record_content_hash is None
    assert usage.accepted_attempt_id == f"{TASK_ID}/02-success"
    assert usage.totals.attempt_count == 2
    assert usage.totals.output_bytes == len("not-json") + len(accepted_output)
    assert usage.totals.proposal_bytes == len(accepted_output)
    assert usage.totals.stdout_bytes == 3
    assert usage.totals.stderr_bytes == 6
    assert usage.totals.retained_diagnostic_bytes == 9


def test_canonical_bytes_are_stable_and_model_totals_are_immutable(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _write_attempt(
        project,
        "01-failed",
        output="bad",
        outcome=AttemptOutcome.ENGINE_FORMAT_FAILURE,
    )
    usage = collect_pilot_task_usage(
        project, task_id=TASK_ID, budget=_small_budget()
    )

    first = canonical_pilot_task_usage_bytes(usage)
    second = build_pilot_task_usage(
        project, task_id=TASK_ID, budget=_small_budget()
    )
    assert first == second
    assert first.endswith(b"\n")
    assert canonical_json_bytes(json.loads(first)) + b"\n" == first
    assert PilotTaskUsageArtifact.model_validate_json(first) == usage
    with pytest.raises(ValidationError):
        usage.totals.stdout_bytes = 99
    changed = usage.model_dump(mode="json")
    changed["totals"]["stdout_bytes"] += 1
    with pytest.raises(ValidationError, match="totals"):
        PilotTaskUsageArtifact.model_validate(changed)


def test_subprocess_streams_diagnostics_and_writable_inventory_are_exact(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    scratch = b"12345"
    inventory = (
        ExecutionFileRecord(
            relative_path=Path("scratch/nested"),
            file_type="directory",
            size_bytes=None,
            content_hash=None,
        ),
        ExecutionFileRecord(
            relative_path=Path("scratch/nested/value.bin"),
            file_type="regular",
            size_bytes=len(scratch),
            content_hash=hash_bytes(scratch),
        ),
    )
    _write_attempt(
        project,
        "01-failed",
        output="bad",
        stdout="out",
        stderr="err!",
        outcome=AttemptOutcome.ENGINE_FORMAT_FAILURE,
        inventory=inventory,
    )

    usage = collect_pilot_task_usage(
        project, task_id=TASK_ID, budget=_small_budget()
    )
    attempt = usage.attempts[0]
    assert attempt.stdout_bytes == 3
    assert attempt.stderr_bytes == 4
    assert attempt.retained_diagnostic_bytes == 7
    assert attempt.writable_entry_count == 2
    assert attempt.writable_tree_bytes == 5
    assert attempt.writable_inventory_hash is not None
    assert usage.totals.writable_entry_count == 2
    assert usage.totals.writable_tree_bytes == 5


@pytest.mark.parametrize(
    ("stdout", "stderr", "inventory", "budget_updates", "message"),
    [
        ("12345", "", (), {"max_stdout_bytes_per_attempt": 4}, "stdout"),
        ("", "12345", (), {"max_stderr_bytes_per_attempt": 4}, "stderr"),
        (
            "123",
            "123",
            (),
            {"max_retained_diagnostic_bytes_per_attempt": 5},
            "diagnostic",
        ),
        (
            "",
            "",
            tuple(
                ExecutionFileRecord(
                    relative_path=Path(f"scratch/{index}"),
                    file_type="regular",
                    size_bytes=1,
                    content_hash=hash_bytes(bytes([index])),
                )
                for index in range(3)
            ),
            {"max_writable_entries_per_attempt": 2},
            "writable entry",
        ),
        (
            "",
            "",
            (
                ExecutionFileRecord(
                    relative_path=Path("scratch/value"),
                    file_type="regular",
                    size_bytes=5,
                    content_hash=hash_bytes(b"12345"),
                ),
            ),
            {"max_writable_tree_bytes_per_attempt": 4},
            "writable tree",
        ),
    ],
)
def test_every_per_attempt_resource_cap_fails_closed(
    tmp_path: Path,
    stdout: str,
    stderr: str,
    inventory: tuple[ExecutionFileRecord, ...],
    budget_updates: dict[str, int],
    message: str,
) -> None:
    project = tmp_path / "project"
    _write_attempt(
        project,
        "01-failed",
        output="bad",
        stdout=stdout,
        stderr=stderr,
        outcome=AttemptOutcome.ENGINE_FORMAT_FAILURE,
        inventory=inventory if inventory else None,
    )
    # model_copy intentionally makes a single tighter test seam; production
    # FivePaperPilotBudget validation separately checks cross-field coherence.
    budget = _small_budget().model_copy(update=budget_updates)

    with pytest.raises(PilotUsageError, match=message):
        collect_pilot_task_usage(project, task_id=TASK_ID, budget=budget)


def test_cumulative_caps_include_failed_attempts_before_success(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    for ordinal in (1, 2):
        _write_attempt(
            project,
            f"0{ordinal}-failed",
            output="bad",
            stdout="123",
            outcome=AttemptOutcome.ENGINE_FORMAT_FAILURE,
        )
    budget = _small_budget(
        max_stdout_bytes_per_attempt=3,
        max_cumulative_stdout_bytes=5,
    )

    with pytest.raises(PilotUsageError, match="cumulative stdout"):
        collect_pilot_task_usage(project, task_id=TASK_ID, budget=budget)


def test_recorded_valid_attempt_without_proposal_is_rejected(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _write_attempt(
        project,
        "01-invalid",
        output='{"accepted":true}',
        outcome=AttemptOutcome.VALID_SCIENTIFIC_RESULT,
        accepted=True,
    )

    with pytest.raises(PilotUsageError, match="proposal presence"):
        collect_pilot_task_usage(project, task_id=TASK_ID, budget=_small_budget())


def test_execution_failure_record_rejects_successful_agent_result(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    attempt = _write_attempt(
        project,
        "01-invalid",
        output="",
        outcome=AttemptOutcome.ENGINE_EXECUTION_FAILURE,
        execution_error="synthetic launch failure",
    )
    raw_result = json.loads(
        (attempt / "agent_result.json").read_text(encoding="utf-8")
    )
    raw_result["execution_succeeded"] = True
    (attempt / "agent_result.json").write_text(
        json.dumps(raw_result), encoding="utf-8"
    )

    with pytest.raises(PilotUsageError, match="execution-failure attempt"):
        collect_pilot_task_usage(project, task_id=TASK_ID, budget=_small_budget())


def test_schema_valid_attempt_without_proposal_is_rejected(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    attempt = _write_attempt(
        project,
        "01-invalid",
        output='{"schema":"valid"}',
        outcome=AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE,
    )
    raw_record = json.loads(
        (attempt / "attempt_record.json").read_text(encoding="utf-8")
    )
    raw_record["proposal_validation_valid"] = False
    (attempt / "attempt_record.json").write_text(
        json.dumps(raw_record), encoding="utf-8"
    )

    with pytest.raises(PilotUsageError, match="proposal presence"):
        collect_pilot_task_usage(project, task_id=TASK_ID, budget=_small_budget())


def test_proposal_content_and_semantic_hashes_distinguish_whitespace(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    payload = {"accepted": True, "nested": {"value": 3}}
    attempt = _write_attempt(
        project,
        "01-success",
        output=json.dumps(payload, separators=(",", ":")),
        outcome=AttemptOutcome.VALID_SCIENTIFIC_RESULT,
        write_record=False,
        write_proposal=True,
    )
    pretty_proposal = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    (attempt / "proposal.json").write_bytes(pretty_proposal)

    usage = collect_pilot_task_usage(
        project,
        task_id=TASK_ID,
        budget=_small_budget(),
        accepted_attempt_id=f"{TASK_ID}/01-success",
    )

    recorded = usage.attempts[0]
    assert recorded.proposal_content_hash == hash_bytes(pretty_proposal)
    assert recorded.proposal_semantic_hash == hash_json(payload)
    assert recorded.proposal_content_hash != recorded.proposal_semantic_hash


def test_budget_bypass_retains_small_overrun_for_terminal_witness(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _write_attempt(
        project,
        "01-over-budget",
        output="bad",
        stdout="12345",
        outcome=AttemptOutcome.ENGINE_FORMAT_FAILURE,
    )
    budget = _small_budget().model_copy(
        update={"max_stdout_bytes_per_attempt": 4}
    )

    with pytest.raises(PilotUsageError, match="per-attempt stdout"):
        collect_pilot_task_usage(project, task_id=TASK_ID, budget=budget)

    usage = collect_pilot_task_usage(
        project,
        task_id=TASK_ID,
        budget=budget,
        enforce_budget=False,
    )
    assert usage.attempts[0].stdout_bytes == 5
    assert usage.totals.stdout_bytes == 5


def test_mutated_stream_and_record_hash_are_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    attempt = _write_attempt(
        project,
        "01-failed",
        output="bad",
        stdout="original",
        outcome=AttemptOutcome.ENGINE_FORMAT_FAILURE,
    )
    (attempt / "stdout.txt").write_text("mutated", encoding="utf-8")
    with pytest.raises(PilotUsageError, match="streams differ"):
        collect_pilot_task_usage(project, task_id=TASK_ID, budget=_small_budget())

    project_two = tmp_path / "project-two"
    _write_attempt(
        project_two,
        "01-failed",
        output="bad",
        outcome=AttemptOutcome.ENGINE_FORMAT_FAILURE,
        record_output_hash=hash_text("different"),
    )
    with pytest.raises(PilotUsageError, match="output hash"):
        collect_pilot_task_usage(
            project_two, task_id=TASK_ID, budget=_small_budget()
        )


def test_symlink_nonregular_escape_and_unbounded_attempts_are_rejected(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    project = tmp_path / "symlink-project"
    attempt = _write_attempt(
        project,
        "01-failed",
        output="bad",
        outcome=AttemptOutcome.ENGINE_FORMAT_FAILURE,
    )
    (attempt / "stdout.txt").unlink()
    (attempt / "stdout.txt").symlink_to(outside)
    with pytest.raises(PilotUsageError, match="symlink"):
        collect_pilot_task_usage(project, task_id=TASK_ID, budget=_small_budget())

    fifo_project = tmp_path / "fifo-project"
    fifo_attempt = _write_attempt(
        fifo_project,
        "01-failed",
        output="bad",
        outcome=AttemptOutcome.ENGINE_FORMAT_FAILURE,
    )
    os.mkfifo(fifo_attempt / "unexpected.pipe")
    with pytest.raises(PilotUsageError, match="special"):
        collect_pilot_task_usage(
            fifo_project, task_id=TASK_ID, budget=_small_budget()
        )

    escape_project = tmp_path / "escape-project"
    escaped_inventory = (
        ExecutionFileRecord(
            relative_path=Path("output/../private/secret"),
            file_type="regular",
            size_bytes=1,
            content_hash=hash_bytes(b"x"),
        ),
    )
    _write_attempt(
        escape_project,
        "01-failed",
        output="bad",
        outcome=AttemptOutcome.ENGINE_FORMAT_FAILURE,
        inventory=escaped_inventory,
    )
    with pytest.raises(PilotUsageError, match="path escape"):
        collect_pilot_task_usage(
            escape_project, task_id=TASK_ID, budget=_small_budget()
        )

    many_project = tmp_path / "many-project"
    attempts = many_project / "work" / "tasks" / TASK_ID / "attempts"
    for ordinal in range(MAX_PILOT_ATTEMPTS_PER_TASK + 1):
        (attempts / f"{ordinal:03d}-synthetic").mkdir(parents=True)
    with pytest.raises(PilotUsageError, match="attempt limit"):
        collect_pilot_task_usage(
            many_project, task_id=TASK_ID, budget=FivePaperPilotBudget()
        )


def test_serialized_usage_contains_no_raw_secret_or_private_absolute_path(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    secret = "synthetic-secret-value"
    private_path = f"{project}/work/tasks/{TASK_ID}/private/task_provenance.json"
    output = json.dumps({"secret_fixture": secret})
    root = _write_attempt(
        project,
        "01-failed",
        output=output,
        stderr=f"failure at {private_path}",
        outcome=AttemptOutcome.ENGINE_SCHEMA_FAILURE,
        write_proposal=True,
        execution_error=f"cannot read {private_path}",
    )
    raw_record = json.loads((root / "attempt_record.json").read_text(encoding="utf-8"))
    raw_record["validation_errors"] = [f"{secret} at {private_path}"]
    (root / "attempt_record.json").write_text(
        json.dumps(raw_record), encoding="utf-8"
    )

    content = build_pilot_task_usage(
        project, task_id=TASK_ID, budget=FivePaperPilotBudget()
    )

    assert secret.encode("utf-8") not in content
    assert private_path.encode("utf-8") not in content
    assert str(project).encode("utf-8") not in content
    assert b"output_text" not in content
    assert b"validation_errors" not in content
    assert b"execution_error" not in content
    assert b"execution_root" not in content
    parsed = PilotTaskUsageArtifact.model_validate_json(content)
    assert parsed.attempts[0].output_content_hash == hash_text(output)
    assert parsed.attempts[0].proposal_content_hash is not None
