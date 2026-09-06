"""Timeout handling at the deterministic subprocess boundary (goal.md §7.11, §7.14).

The fake worker's ``timeout`` and ``spawn_child_timeout`` modes block for
``timeout_sleep_seconds`` — far longer than the policy timeout — so each run
exercises the §7.11 escalation: SIGTERM to the process group, a
``terminate_grace_seconds`` grace window, SIGKILL to the group, then a
bounded drain of both diagnostic streams. The primary outcome is
``ENGINE_EXECUTION_FAILURE`` (§6.6 priority 2), which outranks the missing
proposal left behind by the killed worker (priority 5).
"""

from __future__ import annotations

import hashlib
import os
import signal
import time
from pathlib import Path

from vibereview.runtime import (
    AttemptFailureStage,
    AttemptOutcome,
    GenerateCandidateClaimsInvocation,
    ProjectRuntime,
    SubprocessAgentResult,
    SubprocessEngine,
    SyntheticCredentialProvider,
    TaskAttemptRecord,
    TaskType,
    deterministic_test_policy,
)
from vibereview.runtime.subprocess import SubprocessPolicy

WORKER = Path(__file__).resolve().parent.parent / "helpers" / "fake_agent.py"

TIMEOUT_SECONDS = 0.8
TERMINATE_GRACE_SECONDS = 0.3
# Deliberately distinctive: this exact text appears in the cmdline of the
# child spawned by the spawn_child_timeout mode and nowhere else on the host.
TIMEOUT_SLEEP_SECONDS = 97.5
CHILD_SLEEP_SIGNATURE = f"time.sleep({TIMEOUT_SLEEP_SECONDS!r})"
# Loose, CI-safe bound: far above the ~1 s a timeout attempt needs and far
# below the worker's 97.5 s sleep, proving the attempt never waits it out.
WALL_CLOCK_BOUND_SECONDS = 10.0
STREAM_BYTES = 4096
MAX_DIAGNOSTIC_BYTES = 65536  # deterministic_test_policy max_stdout/stderr_bytes


def _policy() -> SubprocessPolicy:
    return deterministic_test_policy().model_copy(
        update={
            "timeout_seconds": TIMEOUT_SECONDS,
            "terminate_grace_seconds": TERMINATE_GRACE_SECONDS,
        }
    )


def _engine(
    name: str,
    modes: tuple[str, ...],
    worker_config: dict[str, object] | None = None,
) -> SubprocessEngine:
    config: dict[str, object] = {"timeout_sleep_seconds": TIMEOUT_SLEEP_SECONDS}
    config.update(worker_config or {})
    return SubprocessEngine(
        worker_script=WORKER,
        modes=modes,
        worker_config=config,
        policy=_policy(),
        credential_provider=SyntheticCredentialProvider(),
        execution_root_parent=None,
        name=name,
    )


def _run_single_attempt(tmp_path: Path, engine: SubprocessEngine):
    """Run one failing attempt and load its retained artifacts."""

    runtime = ProjectRuntime.create(tmp_path / "project", project_name="timeout-test")
    started = time.monotonic()
    result = runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        GenerateCandidateClaimsInvocation(topic="timeout", existing_theme_ids=[]),
        engines=[engine],
    )
    elapsed = time.monotonic() - started
    (record,) = result.attempt_records
    attempt_dir = (
        runtime.project_root
        / "work"
        / "tasks"
        / record.task_id
        / "attempts"
        / record.attempt_id
    )
    agent_result = SubprocessAgentResult.model_validate_json(
        (attempt_dir / "agent_result.json").read_text(encoding="utf-8")
    )
    return runtime, result, record, attempt_dir, agent_result.execution_report, elapsed


def _assert_timeout_outcome(result, record, report) -> None:
    assert result.outcome is AttemptOutcome.ENGINE_EXECUTION_FAILURE
    assert result.generation is None
    assert record.outcome is AttemptOutcome.ENGINE_EXECUTION_FAILURE
    assert record.accepted_attempt is False
    assert report.timed_out is True
    assert report.launch_error is None
    timeout_failures = [
        failure
        for failure in record.detected_failures
        if failure.code == "process_timeout"
    ]
    assert len(timeout_failures) == 1
    assert timeout_failures[0].stage is AttemptFailureStage.PROCESS


def _survivors_with_signature(signature: str) -> list[str]:
    """Live processes whose cmdline contains the signature (Linux /proc).

    Zombies have an empty cmdline and can never match, so a match is always
    a genuinely surviving process.
    """

    survivors = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            cmdline = Path(entry.path, "cmdline").read_bytes()
        except OSError:
            continue
        text = cmdline.replace(b"\x00", b" ").decode("utf-8", errors="replace")
        if signature in text:
            survivors.append(f"pid {entry.name}: {text.strip()}")
    return survivors


def _assert_no_surviving_child(signature: str) -> None:
    # A survivor of the group kill would keep sleeping for most of
    # TIMEOUT_SLEEP_SECONDS, so it would still match after this grace window;
    # the window only absorbs kernel reaping lag.
    deadline = time.monotonic() + 2.0
    survivors = _survivors_with_signature(signature)
    while survivors and time.monotonic() < deadline:
        time.sleep(0.05)
        survivors = _survivors_with_signature(signature)
    assert not survivors, f"child survived the process-group kill: {survivors}"


def test_timeout_reports_process_timeout_and_bounds_wall_clock(tmp_path):
    runtime, result, record, _, report, elapsed = _run_single_attempt(
        tmp_path, _engine("timeout-basic", ("timeout",))
    )

    _assert_timeout_outcome(result, record, report)
    # Canonical state is preserved: the fresh repository stays at generation 0.
    assert runtime.store.current_generation() == 0
    # The §7.11 escalation killed the worker by group signal (SIGTERM, with
    # SIGKILL if the grace window elapsed), never by a normal exit code.
    assert report.exit_code in (-signal.SIGTERM, -signal.SIGKILL)
    assert report.duration_seconds >= TIMEOUT_SECONDS
    # The attempt finished in a couple of seconds — nowhere near the worker's
    # 97.5 s sleep — so the policy timeout is what ended it.
    assert elapsed < WALL_CLOCK_BOUND_SECONDS


def test_timeout_primary_outcome_outranks_missing_proposal(tmp_path):
    # The killed worker never wrote output/proposal.json. §6.6 freezes
    # execution failure (priority 2) above proposal-format failure
    # (priority 5); the missing proposal stays on the record as a safely
    # detected secondary failure.
    _, result, record, _, report, _ = _run_single_attempt(
        tmp_path, _engine("timeout-precedence", ("timeout",))
    )

    assert result.outcome is AttemptOutcome.ENGINE_EXECUTION_FAILURE
    assert report.primary_outcome is AttemptOutcome.ENGINE_EXECUTION_FAILURE
    assert report.proposal_format_invalid is True

    codes = {failure.code for failure in record.detected_failures}
    assert "process_timeout" in codes
    assert "proposal_missing" in codes
    stages = {failure.code: failure.stage for failure in record.detected_failures}
    assert stages["process_timeout"] is AttemptFailureStage.PROCESS
    assert stages["proposal_missing"] is AttemptFailureStage.PROPOSAL_FILE

    # Kernel parse/schema/proposal-validation stages never ran.
    assert record.format_valid is False
    assert record.schema_valid is False
    assert record.proposal_validation_valid is None
    assert record.output_hash is None


def test_spawn_child_timeout_kills_the_whole_process_group(tmp_path):
    # §7.11 requires both the blocking worker and the child it spawned to be
    # terminated (§7.14: "child process is terminated on timeout"). The
    # child's cmdline carries CHILD_SLEEP_SIGNATURE, so any survivor of the
    # group SIGTERM/SIGKILL would still be visible in /proc long after this
    # test finishes.
    _, result, record, _, report, elapsed = _run_single_attempt(
        tmp_path, _engine("timeout-spawn", ("spawn_child_timeout",))
    )

    _assert_timeout_outcome(result, record, report)
    assert elapsed < WALL_CLOCK_BOUND_SECONDS
    _assert_no_surviving_child(CHILD_SLEEP_SIGNATURE)


def test_bounded_captures_are_drained_and_persisted_after_kill(tmp_path):
    # The worker emits STREAM_BYTES to stderr and then blocks until killed.
    # §7.11 drains the bounded diagnostics after the group kill, §7.12 retains
    # them as attempt/stdout.txt and attempt/stderr.txt, and §6.8 fixes
    # retained_redacted_hash as the SHA-256 of the exact persisted file.
    _, result, record, attempt_dir, report, _ = _run_single_attempt(
        tmp_path,
        _engine(
            "timeout-drain",
            ("large_stderr", "timeout"),
            {"stream_bytes": STREAM_BYTES},
        ),
    )

    _assert_timeout_outcome(result, record, report)

    stdout_path = attempt_dir / "stdout.txt"
    stderr_path = attempt_dir / "stderr.txt"
    assert stdout_path.is_file()
    assert stderr_path.is_file()

    stderr_bytes = stderr_path.read_bytes()
    assert report.stderr_capture.bytes_observed >= STREAM_BYTES
    assert report.stderr_capture.bytes_retained == len(stderr_bytes)
    assert report.stderr_capture.bytes_retained <= MAX_DIAGNOSTIC_BYTES
    assert report.stderr_capture.truncated is False
    assert report.stderr_capture.redactions_applied == 0
    assert report.stderr_capture.retained_redacted_hash == (
        f"sha256:{hashlib.sha256(stderr_bytes).hexdigest()}"
    )
    assert b"vibereview-fake-stderr" in stderr_bytes

    # Nothing was written to stdout: the drained capture is a clean empty file.
    stdout_bytes = stdout_path.read_bytes()
    assert stdout_bytes == b""
    assert report.stdout_capture.bytes_observed == 0
    assert report.stdout_capture.bytes_retained == 0
    assert report.stdout_capture.retained_redacted_hash == (
        f"sha256:{hashlib.sha256(b'').hexdigest()}"
    )


def test_timeout_artifacts_retained_and_execution_root_removed(tmp_path):
    # §7.12: the attempt record, agent result and bounded captures are
    # retained; the external execution root is deleted once the safe
    # artifacts are imported.
    runtime, result, record, attempt_dir, report, _ = _run_single_attempt(
        tmp_path, _engine("timeout-artifacts", ("timeout",))
    )

    _assert_timeout_outcome(result, record, report)

    assert report.execution_root is not None
    assert not report.execution_root.exists()
    assert str(runtime.project_root) not in str(report.execution_root)

    assert (attempt_dir / "attempt_record.json").is_file()
    persisted_record = TaskAttemptRecord.model_validate_json(
        (attempt_dir / "attempt_record.json").read_text(encoding="utf-8")
    )
    assert persisted_record == record
    assert (attempt_dir / "agent_result.json").is_file()
    assert (attempt_dir / "stdout.txt").is_file()
    assert (attempt_dir / "stderr.txt").is_file()
    # The killed worker never produced a proposal and nothing was imported.
    assert not (attempt_dir / "proposal.json").exists()
