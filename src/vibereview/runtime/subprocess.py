"""Subprocess execution policy and frozen outcome precedence (goal.md §6.6-§6.7).

Contracts only: no external process is executed in Milestone B1. B2's runner
consumes these models and the deterministic precedence function.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ConfigDict, Field, model_validator

from .records import AttemptOutcome, RuntimeModel


WRITABLE_QUOTA_ROOTS: tuple[str, ...] = ("output", "scratch", "home", "tmp")
QUOTA_EXEMPT_ROOTS: tuple[str, ...] = ("bundle", "launcher", "credentials")


class SubprocessPolicy(RuntimeModel):
    """Deterministic bounds for one external engine process (goal.md §6.7)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timeout_seconds: float = Field(gt=0)
    terminate_grace_seconds: float = Field(ge=0)

    max_stdout_bytes: int = Field(gt=0)
    max_stderr_bytes: int = Field(gt=0)
    max_proposal_bytes: int = Field(gt=0)

    max_writable_tree_bytes: int = Field(gt=0)
    max_writable_entries: int = Field(gt=0)
    max_writable_single_file_bytes: int = Field(gt=0)
    max_writable_directory_depth: int = Field(gt=0)

    max_open_files: int | None = Field(default=None, gt=0)
    max_processes: int | None = Field(default=None, gt=0)
    max_cpu_seconds: int | None = Field(default=None, gt=0)
    max_address_space_bytes: int | None = Field(default=None, gt=0)

    writable_tree_scan_interval_seconds: float = Field(gt=0)

    inherited_environment_allowlist: tuple[str, ...]
    allowed_output_files: tuple[str, ...] = ("proposal.json",)

    @model_validator(mode="before")
    @classmethod
    def _normalize_entry_quota(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "max_writable_entries" not in data and "max_writable_files" in data:
                data = dict(data)
                data["max_writable_entries"] = data.pop("max_writable_files")
        return data

    @property
    def max_writable_files(self) -> int:
        return self.max_writable_entries


def writable_quota_applies(relative_path: Path) -> bool:
    """Writable-growth quotas cover only WRITABLE_QUOTA_ROOTS (goal.md §6.7).

    ``bundle/``, ``launcher/`` and ``credentials/`` are quota-exempt; the
    proposal itself remains subject to ``max_proposal_bytes``.
    """

    parts = relative_path.parts
    return bool(parts) and parts[0] in WRITABLE_QUOTA_ROOTS


def deterministic_test_policy() -> SubprocessPolicy:
    """Suggested deterministic test defaults from goal.md §6.7, §5.1."""

    return SubprocessPolicy(
        timeout_seconds=10.0,
        terminate_grace_seconds=1.0,
        max_stdout_bytes=65536,
        max_stderr_bytes=65536,
        max_proposal_bytes=1048576,
        max_writable_tree_bytes=16777216,
        max_writable_entries=256,
        max_writable_single_file_bytes=4194304,
        max_writable_directory_depth=8,
        writable_tree_scan_interval_seconds=0.05,
        inherited_environment_allowlist=("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE"),
    )


def primary_attempt_outcome(
    *,
    resource_limit_breached: bool = False,
    execution_failed: bool = False,
    bundle_modified: bool = False,
    output_policy_violated: bool = False,
    proposal_format_invalid: bool = False,
    proposal_schema_invalid: bool = False,
    proposal_task_invalid: bool = False,
) -> AttemptOutcome:
    """Frozen primary-outcome precedence (goal.md §6.6).

    Exactly one primary outcome is selected; all safely detected secondary
    defects are recorded separately on ``TaskAttemptRecord.detected_failures``.
    """

    if resource_limit_breached:
        return AttemptOutcome.ENGINE_RESOURCE_LIMIT_FAILURE
    if execution_failed:
        return AttemptOutcome.ENGINE_EXECUTION_FAILURE
    if bundle_modified:
        return AttemptOutcome.ENGINE_WORKSPACE_INTEGRITY_FAILURE
    if output_policy_violated:
        return AttemptOutcome.ENGINE_OUTPUT_POLICY_FAILURE
    if proposal_format_invalid:
        return AttemptOutcome.ENGINE_FORMAT_FAILURE
    if proposal_schema_invalid:
        return AttemptOutcome.ENGINE_SCHEMA_FAILURE
    if proposal_task_invalid:
        return AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE
    return AttemptOutcome.VALID_SCIENTIFIC_RESULT
