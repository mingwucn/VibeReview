"""Conformance-test failure diagnostics (goal.md §4.4).

A conformance run that is not ``VALID_SCIENTIFIC_RESULT`` fails with a JSON
dump of everything needed to diagnose the sandbox from CI output: the
outcome, the first attempt record, the persisted agent result (retained
stdout/stderr, Bubblewrap argv, exit code, detected failures, applied limits,
quiescence record) and the cached sandbox capability probe when one ran.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from vibereview.runtime import AttemptOutcome, ProjectRuntime, RuntimeResult

from helpers.sandbox_gate import cached_sandbox_probe


def assert_sandbox_result_valid(runtime: ProjectRuntime, result: RuntimeResult) -> None:
    """Fail with retained sandbox diagnostics unless the run is VALID (goal.md §4.4)."""

    if result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT:
        return
    record = result.attempt_records[0] if result.attempt_records else None
    agent_result: dict[str, Any] | None = None
    if record is not None:
        agent_result_path = runtime.project_root / record.agent_result_path
        if agent_result_path.is_file():
            try:
                agent_result = json.loads(
                    agent_result_path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                agent_result = {
                    "unreadable_agent_result": f"{agent_result_path}: {exc}"
                }
    probe = cached_sandbox_probe()
    details = {
        "outcome": result.outcome.value,
        "record": record.model_dump(mode="json") if record is not None else None,
        "agent_result": agent_result,
        "sandbox_probe": probe.model_dump(mode="json") if probe is not None else None,
    }
    pytest.fail(json.dumps(details, indent=2, ensure_ascii=False), pytrace=False)
