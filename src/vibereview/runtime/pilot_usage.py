"""Bounded, sanitized attempt accounting for the synthetic Package C pilot.

The runtime writes an accepted attempt's ``attempt_record.json`` only after the
generation commit returns.  Package C auxiliary materializers run *inside*
that commit, so this module deliberately permits exactly one absent record:
the caller-named accepted attempt.  Every earlier failed attempt must already
have its record.

Only counts, provider-neutral engine identity, outcomes, and content hashes
leave this boundary.  Engine output, diagnostic text, execution arguments,
and filesystem paths are inspected but are never serialized.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from vibereview.ids import Sha256

from .execution import SubprocessExecutionResult
from .hashing import canonical_json_bytes, hash_bytes, hash_json
from .pilot_records import FivePaperPilotBudget
from .records import AgentResult, AttemptOutcome, RuntimeModel, TaskAttemptRecord
from .repository import UnsafeRepositoryEntryError, read_contained_regular_file
from .subprocess import WRITABLE_QUOTA_ROOTS


PILOT_USAGE_SCHEMA_VERSION = "package-c-pilot-task-usage-1"
MAX_PILOT_ATTEMPTS_PER_TASK = 128
MAX_PILOT_ATTEMPT_TREE_ENTRIES = 100_000
MAX_PILOT_AGENT_RESULT_BYTES = 64 * 1024 * 1024
MAX_PILOT_ATTEMPT_RECORD_BYTES = 8 * 1024 * 1024
MAX_PILOT_JSON_NODES = 100_000
MAX_PILOT_JSON_DEPTH = 64
MAX_PILOT_RELATIVE_PATH_BYTES = 4_096
MAX_PILOT_IMPORTED_PROPOSAL_BYTES = 4 * 1024 * 1024

_TASK_ID_RE = re.compile(r"^TASK[0-9]{4,12}$")
_ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")
_ENGINE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ENGINE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:-]{0,127}$")


class PilotUsageError(ValueError):
    """Attempt artifacts cannot produce exact, bounded synthetic accounting."""


class PilotUsageModel(RuntimeModel):
    """Closed immutable base for generation-owned usage DTOs."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class PilotAttemptUsage(PilotUsageModel):
    """Sanitized accounting for one engine invocation.

    ``output_bytes`` is the UTF-8 size of ``AgentResult.output_text`` and is
    charged for failed as well as successful attempts. ``proposal_bytes`` is
    the exact size of the runtime-imported ``proposal.json`` when one exists.
    """

    task_id: Annotated[str, Field(pattern=r"^TASK[0-9]{4,12}$", max_length=16)]
    attempt_id: Annotated[
        str,
        Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$", max_length=192),
    ]
    engine: Annotated[
        str,
        Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", max_length=128),
    ]
    engine_version: Annotated[str | None, Field(default=None, max_length=128)]
    outcome: AttemptOutcome
    accepted_attempt: bool
    attempt_record_inferred: bool
    agent_result_content_hash: Sha256
    attempt_record_content_hash: Sha256 | None
    attempt_record_semantic_hash: Sha256 | None
    output_content_hash: Sha256
    proposal_content_hash: Sha256 | None
    proposal_semantic_hash: Sha256 | None
    stdout_content_hash: Sha256
    stderr_content_hash: Sha256
    writable_inventory_hash: Sha256 | None
    output_bytes: int = Field(ge=0, le=4 * 1024 * 1024)
    proposal_bytes: int = Field(ge=0, le=MAX_PILOT_IMPORTED_PROPOSAL_BYTES)
    stdout_bytes: int = Field(ge=0, le=1024 * 1024)
    stderr_bytes: int = Field(ge=0, le=1024 * 1024)
    retained_diagnostic_bytes: int = Field(ge=0, le=4 * 1024 * 1024)
    writable_entry_count: int = Field(ge=0, le=4_096)
    writable_tree_bytes: int = Field(ge=0, le=256 * 1024 * 1024)

    @field_validator("engine_version")
    @classmethod
    def _safe_engine_version(cls, value: str | None) -> str | None:
        if value is not None and _ENGINE_VERSION_RE.fullmatch(value) is None:
            raise ValueError("engine version is not a sanitized identity")
        return value

    @model_validator(mode="after")
    def _closed_attempt(self) -> "PilotAttemptUsage":
        if self.accepted_attempt is not (
            self.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
        ):
            raise ValueError("accepted flag and attempt outcome disagree")
        if self.attempt_record_inferred is not (
            self.attempt_record_content_hash is None
            and self.attempt_record_semantic_hash is None
        ):
            raise ValueError("inferred-record flag and record hash disagree")
        if (self.attempt_record_content_hash is None) is not (
            self.attempt_record_semantic_hash is None
        ):
            raise ValueError("attempt record byte and semantic hashes disagree")
        if self.attempt_record_inferred and not self.accepted_attempt:
            raise ValueError("only the accepted attempt record may be inferred")
        if (self.proposal_bytes == 0) is not (
            self.proposal_content_hash is None
        ):
            raise ValueError("proposal byte count and content hash disagree")
        if (self.proposal_bytes == 0) is not (
            self.proposal_semantic_hash is None
        ):
            raise ValueError("proposal byte count and semantic hash disagree")
        if self.retained_diagnostic_bytes != (
            self.stdout_bytes + self.stderr_bytes
        ):
            raise ValueError("retained diagnostics must equal both retained streams")
        if (self.writable_inventory_hash is None) and (
            self.writable_entry_count != 0 or self.writable_tree_bytes != 0
        ):
            raise ValueError("writable accounting requires an inventory hash")
        return self


class PilotTaskUsageTotals(PilotUsageModel):
    """Exact sum of every attempt belonging to one task."""

    attempt_count: int = Field(ge=1, le=MAX_PILOT_ATTEMPTS_PER_TASK)
    output_bytes: int = Field(ge=0, le=4 * 1024 * 1024)
    proposal_bytes: int = Field(ge=0, le=4 * 1024 * 1024)
    stdout_bytes: int = Field(ge=0, le=128 * 1024 * 1024)
    stderr_bytes: int = Field(ge=0, le=128 * 1024 * 1024)
    retained_diagnostic_bytes: int = Field(ge=0, le=512 * 1024 * 1024)
    writable_entry_count: int = Field(ge=0, le=262_144)
    writable_tree_bytes: int = Field(ge=0, le=8 * 1024 * 1024 * 1024)

    @model_validator(mode="after")
    def _diagnostic_sum(self) -> "PilotTaskUsageTotals":
        if self.retained_diagnostic_bytes != (
            self.stdout_bytes + self.stderr_bytes
        ):
            raise ValueError("total retained diagnostics must equal both streams")
        return self


class PilotTaskUsageArtifact(PilotUsageModel):
    """Generation-owned, synthetic-only witness for all attempts of one task."""

    schema_version: Literal["package-c-pilot-task-usage-1"] = (
        PILOT_USAGE_SCHEMA_VERSION
    )
    synthetic_only: Literal[True] = True
    task_id: Annotated[str, Field(pattern=r"^TASK[0-9]{4,12}$", max_length=16)]
    accepted_attempt_id: Annotated[str | None, Field(default=None, max_length=209)]
    budget_content_hash: Sha256
    attempts: Annotated[
        tuple[PilotAttemptUsage, ...],
        Field(min_length=1, max_length=MAX_PILOT_ATTEMPTS_PER_TASK),
    ]
    totals: PilotTaskUsageTotals

    @model_validator(mode="after")
    def _exact_bindings(self) -> "PilotTaskUsageArtifact":
        attempt_ids = tuple(item.attempt_id for item in self.attempts)
        if attempt_ids != tuple(sorted(attempt_ids)):
            raise ValueError("pilot attempts must be deterministically ordered")
        if len(attempt_ids) != len(set(attempt_ids)):
            raise ValueError("pilot attempt identifiers must be unique")
        if any(item.task_id != self.task_id for item in self.attempts):
            raise ValueError("pilot attempt belongs to another task")

        accepted = tuple(
            item.attempt_id for item in self.attempts if item.accepted_attempt
        )
        expected_accepted = (
            None if not accepted else f"{self.task_id}/{accepted[0]}"
        )
        if len(accepted) > 1 or self.accepted_attempt_id != expected_accepted:
            raise ValueError("accepted attempt binding disagrees with attempts")

        expected_totals = _sum_attempts(self.attempts)
        if self.totals != expected_totals:
            raise ValueError("pilot task usage totals are not exact")
        return self

    @property
    def artifact_hash(self) -> Sha256:
        return hash_bytes(canonical_pilot_task_usage_bytes(self))


@dataclass(frozen=True, slots=True)
class _TreeEntry:
    relative_path: str
    entry_type: Literal["directory", "regular"]
    device: int
    inode: int
    size: int
    modified_ns: int


@dataclass(frozen=True, slots=True)
class _AttemptSource:
    attempt_id: str
    agent_result: AgentResult
    agent_result_hash: Sha256
    attempt_record: TaskAttemptRecord | None
    attempt_record_hash: Sha256 | None
    attempt_record_semantic_hash: Sha256 | None
    output_bytes: int
    output_hash: Sha256
    proposal_bytes: int
    proposal_hash: Sha256 | None
    proposal_semantic_hash: Sha256 | None
    stdout_bytes: int
    stdout_hash: Sha256
    stderr_bytes: int
    stderr_hash: Sha256
    retained_diagnostic_bytes: int
    writable_entry_count: int
    writable_tree_bytes: int
    writable_inventory_hash: Sha256 | None


def _safe_task_id(task_id: str) -> None:
    if not isinstance(task_id, str) or _TASK_ID_RE.fullmatch(task_id) is None:
        raise PilotUsageError("pilot usage task identifier is invalid")


def _safe_attempt_id(attempt_id: str) -> None:
    if _ATTEMPT_ID_RE.fullmatch(attempt_id) is None:
        raise PilotUsageError("pilot attempt identifier is invalid")


def _qualified_accepted_id(task_id: str, value: str | None) -> str | None:
    if value is None:
        return None
    prefix = f"{task_id}/"
    if not isinstance(value, str) or not value.startswith(prefix):
        raise PilotUsageError("accepted attempt does not belong to the task")
    suffix = value.removeprefix(prefix)
    _safe_attempt_id(suffix)
    if "/" in suffix:
        raise PilotUsageError("accepted attempt identifier is not flat")
    return value


def _safe_engine_identity(engine: str, version: str | None) -> None:
    if _ENGINE_RE.fullmatch(engine) is None:
        raise PilotUsageError("engine identity is not sanitized")
    if version is not None and _ENGINE_VERSION_RE.fullmatch(version) is None:
        raise PilotUsageError("engine version is not sanitized")


def _bounded_json(value: Any, *, label: str) -> None:
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_PILOT_JSON_NODES:
            raise PilotUsageError(f"{label} exceeds its JSON node limit")
        if depth > MAX_PILOT_JSON_DEPTH:
            raise PilotUsageError(f"{label} exceeds its JSON depth limit")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise PilotUsageError(f"{label} contains a non-finite number")
        elif not isinstance(item, (str, int, float, bool, type(None))):
            raise PilotUsageError(f"{label} contains a non-JSON value")


def _strict_json(content: bytes, *, label: str) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PilotUsageError(f"{label} contains duplicate JSON keys")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise PilotUsageError(f"{label} contains a non-finite number")

    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except PilotUsageError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise PilotUsageError(f"{label} is not bounded strict UTF-8 JSON") from None
    _bounded_json(value, label=label)
    return value


def _path_is_canonical(relative_path: Path) -> bool:
    raw = relative_path.as_posix()
    parsed = PurePosixPath(raw)
    return bool(
        raw
        and not parsed.is_absolute()
        and parsed.as_posix() == raw
        and all(part not in {"", ".", ".."} for part in parsed.parts)
        and len(raw.encode("utf-8")) <= MAX_PILOT_RELATIVE_PATH_BYTES
    )


def _directory_identity(path: Path) -> tuple[int, int, int, int]:
    try:
        info = path.lstat()
    except OSError:
        raise PilotUsageError(
            "pilot attempt hierarchy is absent or unreadable"
        ) from None
    if not stat.S_ISDIR(info.st_mode):
        raise PilotUsageError("pilot attempt hierarchy contains an unsafe directory")
    return (info.st_dev, info.st_ino, info.st_mode, info.st_mtime_ns)


def _attempt_tree_snapshot(
    project_root: Path, task_id: str
) -> tuple[tuple[tuple[int, int, int, int], ...], tuple[_TreeEntry, ...]]:
    hierarchy = (
        project_root,
        project_root / "work",
        project_root / "work" / "tasks",
        project_root / "work" / "tasks" / task_id,
        project_root / "work" / "tasks" / task_id / "attempts",
    )
    identities = tuple(_directory_identity(path) for path in hierarchy)
    attempts_root = hierarchy[-1]
    entries: list[_TreeEntry] = []
    stack: list[tuple[Path, PurePosixPath]] = [(attempts_root, PurePosixPath())]
    try:
        while stack:
            directory, relative_directory = stack.pop()
            with os.scandir(directory) as iterator:
                children = []
                remaining = MAX_PILOT_ATTEMPT_TREE_ENTRIES - len(entries)
                for child in iterator:
                    children.append(child)
                    if len(children) > remaining:
                        raise PilotUsageError(
                            "pilot attempt tree exceeds its entry limit"
                        )
                children.sort(key=lambda item: item.name)
            child_directories: list[tuple[Path, PurePosixPath]] = []
            for child in children:
                relative = relative_directory / child.name
                if (
                    not relative.parts
                    or any(part in {"", ".", ".."} for part in relative.parts)
                    or len(relative.as_posix().encode("utf-8"))
                    > MAX_PILOT_RELATIVE_PATH_BYTES
                ):
                    raise PilotUsageError("pilot attempt tree contains an unsafe path")
                info = child.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    kind: Literal["directory", "regular"] = "directory"
                    child_directories.append((Path(child.path), relative))
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    kind = "regular"
                else:
                    raise PilotUsageError(
                        "pilot attempt tree contains a symlink, hardlink, "
                        "or special entry"
                    )
                entries.append(
                    _TreeEntry(
                        relative_path=relative.as_posix(),
                        entry_type=kind,
                        device=info.st_dev,
                        inode=info.st_ino,
                        size=info.st_size,
                        modified_ns=info.st_mtime_ns,
                    )
                )
                if len(entries) > MAX_PILOT_ATTEMPT_TREE_ENTRIES:
                    raise PilotUsageError(
                        "pilot attempt tree exceeds its entry limit"
                    )
            stack.extend(reversed(child_directories))
    except PilotUsageError:
        raise
    except OSError:
        raise PilotUsageError("pilot attempt tree changed or is unreadable") from None
    return identities, tuple(sorted(entries, key=lambda item: item.relative_path))


def _attempt_ids(entries: tuple[_TreeEntry, ...]) -> tuple[str, ...]:
    direct = tuple(item for item in entries if "/" not in item.relative_path)
    if not direct:
        raise PilotUsageError("pilot task has no attempts to account")
    if len(direct) > MAX_PILOT_ATTEMPTS_PER_TASK:
        raise PilotUsageError("pilot task exceeds its attempt limit")
    values: list[str] = []
    for item in direct:
        if item.entry_type != "directory":
            raise PilotUsageError("pilot attempts root contains a non-directory entry")
        _safe_attempt_id(item.relative_path)
        values.append(item.relative_path)
    return tuple(sorted(values))


def _source_relative(task_id: str, attempt_id: str, filename: str) -> str:
    return PurePosixPath(
        "work", "tasks", task_id, "attempts", attempt_id, filename
    ).as_posix()


def _read_source(
    project_root: Path,
    relative_path: str,
    *,
    max_bytes: int,
    label: str,
) -> tuple[bytes, Sha256]:
    try:
        content, snapshot = read_contained_regular_file(
            project_root, relative_path, max_bytes=max_bytes
        )
    except (OSError, UnsafeRepositoryEntryError, ValueError):
        raise PilotUsageError(f"{label} is unsafe, missing, or over budget") from None
    return content, snapshot.content_hash


def _entry_exists(entries: tuple[_TreeEntry, ...], relative: str) -> bool:
    return any(item.relative_path == relative for item in entries)


def _load_agent_result(content: bytes) -> tuple[AgentResult, dict[str, Any]]:
    raw = _strict_json(content, label="agent result")
    if not isinstance(raw, dict):
        raise PilotUsageError("agent result root must be an object")
    base_fields = frozenset(AgentResult.model_fields)
    if not base_fields.issuperset(set(raw) - {"execution_report"}):
        raise PilotUsageError("agent result contains an unknown field")
    try:
        result = AgentResult.model_validate(
            {name: value for name, value in raw.items() if name in base_fields}
        )
    except Exception:
        raise PilotUsageError(
            "agent result does not match its runtime contract"
        ) from None
    _safe_engine_identity(result.engine, result.engine_version)
    return result, raw


def _load_attempt_record(content: bytes) -> TaskAttemptRecord:
    raw = _strict_json(content, label="attempt record")
    try:
        return TaskAttemptRecord.model_validate(raw)
    except Exception:
        raise PilotUsageError(
            "attempt record does not match its runtime contract"
        ) from None


def _validate_stream_capture(
    report: SubprocessExecutionResult,
    *,
    stdout: bytes,
    stderr: bytes,
) -> None:
    for capture, expected_path, content in (
        (report.stdout_capture, "attempt/stdout.txt", stdout),
        (report.stderr_capture, "attempt/stderr.txt", stderr),
    ):
        if (
            capture.relative_path.as_posix() != expected_path
            or capture.bytes_retained != len(content)
            or capture.retained_redacted_hash != hash_bytes(content)
        ):
            raise PilotUsageError(
                "subprocess diagnostic capture differs from its retained stream"
            )


def _writable_usage(
    raw_agent_result: dict[str, Any],
    *,
    stdout: bytes,
    stderr: bytes,
    output_bytes: int,
    output_hash: Sha256,
) -> tuple[int, int, Sha256 | None, SubprocessExecutionResult | None]:
    raw_report = raw_agent_result.get("execution_report")
    if raw_report is None:
        return 0, 0, None, None
    try:
        report = SubprocessExecutionResult.model_validate(raw_report)
    except Exception:
        raise PilotUsageError(
            "subprocess execution report does not match its runtime contract"
        ) from None
    _validate_stream_capture(report, stdout=stdout, stderr=stderr)

    if len(report.inventory) > 4_096:
        raise PilotUsageError("subprocess writable inventory is unbounded")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    total_bytes = 0
    output_inventory: tuple[int, Sha256] | None = None
    for item in report.inventory:
        if not _path_is_canonical(item.relative_path):
            raise PilotUsageError("writable inventory contains a path escape")
        relative = item.relative_path.as_posix()
        if item.relative_path.parts[0] not in WRITABLE_QUOTA_ROOTS:
            raise PilotUsageError("writable inventory contains a non-writable root")
        if relative in seen:
            raise PilotUsageError("writable inventory repeats a path")
        seen.add(relative)
        if item.file_type == "directory":
            if item.size_bytes is not None or item.content_hash is not None:
                raise PilotUsageError("writable directory carries file content")
        elif item.file_type == "regular":
            if item.size_bytes is None or item.content_hash is None:
                raise PilotUsageError("writable regular file lacks exact accounting")
            total_bytes += item.size_bytes
            if relative == "output/proposal.json":
                output_inventory = (item.size_bytes, item.content_hash)
        else:
            raise PilotUsageError(
                "writable inventory contains a symlink or nonregular entry"
            )
        normalized.append(
            {
                "relative_path": relative,
                "file_type": item.file_type,
                "size_bytes": item.size_bytes,
                "content_hash": item.content_hash,
            }
        )
    if output_inventory is not None and output_bytes:
        if output_inventory != (output_bytes, output_hash):
            raise PilotUsageError(
                "writable proposal inventory differs from AgentResult output"
            )
    normalized.sort(key=lambda item: item["relative_path"])
    return len(normalized), total_bytes, hash_json(normalized), report


def _validate_attempt_source(
    *,
    result: AgentResult,
    record: TaskAttemptRecord,
    proposal_present: bool,
    report: SubprocessExecutionResult | None,
) -> None:
    if report is not None:
        report_valid = (
            report.primary_outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
        )
        if result.execution_succeeded is not report_valid:
            raise PilotUsageError(
                "subprocess execution status contradicts its primary outcome"
            )
        if not report_valid:
            if (
                proposal_present
                or record.outcome is not report.primary_outcome
                or record.detected_failures != report.detected_failures
                or record.format_valid
                or record.schema_valid
                or record.proposal_validation_valid is not None
                or record.accepted_attempt
            ):
                raise PilotUsageError(
                    "attempt record contradicts its subprocess execution report"
                )
            return
        if record.detected_failures != report.detected_failures:
            raise PilotUsageError(
                "attempt failures contradict its subprocess execution report"
            )
    elif record.detected_failures:
        raise PilotUsageError("non-subprocess attempt carries execution failures")

    expected_flags: dict[
        AttemptOutcome, tuple[bool, bool, bool | None, bool]
    ] = {
        AttemptOutcome.ENGINE_EXECUTION_FAILURE: (False, False, None, False),
        AttemptOutcome.ENGINE_WORKSPACE_INTEGRITY_FAILURE: (
            False,
            False,
            None,
            False,
        ),
        AttemptOutcome.ENGINE_FORMAT_FAILURE: (False, False, None, False),
        AttemptOutcome.ENGINE_SCHEMA_FAILURE: (True, False, None, False),
        AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE: (
            True,
            True,
            False,
            False,
        ),
        AttemptOutcome.VALID_SCIENTIFIC_RESULT: (True, True, True, True),
        AttemptOutcome.STALE_SNAPSHOT: (True, True, True, False),
        AttemptOutcome.INTERNAL_RUNTIME_FAILURE: (True, True, True, False),
        AttemptOutcome.TRANSACTION_FAILURE: (True, True, True, False),
        AttemptOutcome.CONTRACT_IMPLEMENTATION_FAILURE: (
            True,
            True,
            True,
            False,
        ),
    }
    if record.outcome in {
        AttemptOutcome.ENGINE_OUTPUT_POLICY_FAILURE,
        AttemptOutcome.ENGINE_RESOURCE_LIMIT_FAILURE,
    }:
        raise PilotUsageError("subprocess-only outcome lacks an execution report")
    expected = expected_flags.get(record.outcome)
    observed = (
        record.format_valid,
        record.schema_valid,
        record.proposal_validation_valid,
        record.accepted_attempt,
    )
    if expected is None or observed != expected:
        raise PilotUsageError("attempt outcome and validation state disagree")
    if proposal_present is not record.format_valid:
        raise PilotUsageError("attempt proposal presence contradicts format validity")
    if record.outcome is AttemptOutcome.ENGINE_EXECUTION_FAILURE:
        if result.execution_succeeded:
            raise PilotUsageError("execution-failure attempt is inconsistent")
    elif not result.execution_succeeded:
        raise PilotUsageError("attempt outcome contradicts execution status")


def _proposal_usage(
    content: bytes, *, output_text: str
) -> tuple[int, Sha256, Sha256]:
    proposal = _strict_json(content, label="imported proposal")
    output = _strict_json(output_text.encode("utf-8"), label="agent output")
    if not isinstance(proposal, dict) or not isinstance(output, dict):
        raise PilotUsageError("imported proposal and agent output must be objects")
    if canonical_json_bytes(proposal) != canonical_json_bytes(output):
        raise PilotUsageError("imported proposal differs from AgentResult output")
    return len(content), hash_bytes(content), hash_json(proposal)


def _load_attempt_sources(
    project_root: Path,
    *,
    task_id: str,
    attempt_ids: tuple[str, ...],
    entries: tuple[_TreeEntry, ...],
    budget: FivePaperPilotBudget,
) -> tuple[tuple[_AttemptSource, ...], dict[str, tuple[int, Sha256]]]:
    sources: list[_AttemptSource] = []
    retained: dict[str, tuple[int, Sha256]] = {}
    for attempt_id in attempt_ids:
        prefix = f"{attempt_id}/"
        for required in ("agent_result.json", "stdout.txt", "stderr.txt"):
            if not _entry_exists(entries, prefix + required):
                raise PilotUsageError("pilot attempt lacks a required artifact")

        agent_relative = _source_relative(task_id, attempt_id, "agent_result.json")
        agent_content, agent_hash = _read_source(
            project_root,
            agent_relative,
            max_bytes=MAX_PILOT_AGENT_RESULT_BYTES,
            label="agent result",
        )
        retained[agent_relative] = (len(agent_content), agent_hash)
        result, raw_result = _load_agent_result(agent_content)

        stdout_relative = _source_relative(task_id, attempt_id, "stdout.txt")
        stdout, stdout_hash = _read_source(
            project_root,
            stdout_relative,
            max_bytes=1024 * 1024,
            label="retained stdout",
        )
        retained[stdout_relative] = (len(stdout), stdout_hash)
        stderr_relative = _source_relative(task_id, attempt_id, "stderr.txt")
        stderr, stderr_hash = _read_source(
            project_root,
            stderr_relative,
            max_bytes=1024 * 1024,
            label="retained stderr",
        )
        retained[stderr_relative] = (len(stderr), stderr_hash)
        if (
            result.stdout.encode("utf-8") != stdout
            or result.stderr.encode("utf-8") != stderr
        ):
            raise PilotUsageError("retained streams differ from AgentResult")

        output = result.output_text.encode("utf-8")
        output_hash = hash_bytes(output)
        proposal_relative = _source_relative(task_id, attempt_id, "proposal.json")
        proposal_bytes = 0
        proposal_hash: Sha256 | None = None
        proposal_semantic_hash: Sha256 | None = None
        if _entry_exists(entries, prefix + "proposal.json"):
            proposal_content, proposal_hash = _read_source(
                project_root,
                proposal_relative,
                max_bytes=MAX_PILOT_IMPORTED_PROPOSAL_BYTES,
                label="imported proposal",
            )
            retained[proposal_relative] = (len(proposal_content), proposal_hash)
            (
                proposal_bytes,
                proposal_hash,
                proposal_semantic_hash,
            ) = _proposal_usage(proposal_content, output_text=result.output_text)

        record_relative = _source_relative(
            task_id, attempt_id, "attempt_record.json"
        )
        record: TaskAttemptRecord | None = None
        record_hash: Sha256 | None = None
        record_semantic_hash: Sha256 | None = None
        if _entry_exists(entries, prefix + "attempt_record.json"):
            record_content, record_hash = _read_source(
                project_root,
                record_relative,
                max_bytes=MAX_PILOT_ATTEMPT_RECORD_BYTES,
                label="attempt record",
            )
            retained[record_relative] = (len(record_content), record_hash)
            record = _load_attempt_record(record_content)
            record_semantic_hash = hash_json(record.model_dump(mode="json"))
            expected_agent_path = agent_relative
            if (
                record.task_id != task_id
                or record.attempt_id != attempt_id
                or record.engine != result.engine
                or record.engine_version != result.engine_version
                or record.agent_result_path.as_posix() != expected_agent_path
            ):
                raise PilotUsageError("attempt record identity binding differs")
            expected_record_output_hash = output_hash if output else None
            if record.output_hash != expected_record_output_hash:
                raise PilotUsageError("attempt record output hash differs")
            if record.accepted_attempt is not (
                record.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
            ):
                raise PilotUsageError("attempt record acceptance is inconsistent")

        (
            writable_count,
            writable_bytes,
            inventory_hash,
            execution_report,
        ) = _writable_usage(
            raw_result,
            stdout=stdout,
            stderr=stderr,
            output_bytes=len(output),
            output_hash=output_hash,
        )
        if record is not None:
            _validate_attempt_source(
                result=result,
                record=record,
                proposal_present=proposal_bytes > 0,
                report=execution_report,
            )
        sources.append(
            _AttemptSource(
                attempt_id=attempt_id,
                agent_result=result,
                agent_result_hash=agent_hash,
                attempt_record=record,
                attempt_record_hash=record_hash,
                attempt_record_semantic_hash=record_semantic_hash,
                output_bytes=len(output),
                output_hash=output_hash,
                proposal_bytes=proposal_bytes,
                proposal_hash=proposal_hash,
                proposal_semantic_hash=proposal_semantic_hash,
                stdout_bytes=len(stdout),
                stdout_hash=stdout_hash,
                stderr_bytes=len(stderr),
                stderr_hash=stderr_hash,
                retained_diagnostic_bytes=len(stdout) + len(stderr),
                writable_entry_count=writable_count,
                writable_tree_bytes=writable_bytes,
                writable_inventory_hash=inventory_hash,
            )
        )
    return tuple(sources), retained


def _accepted_binding(
    task_id: str,
    attempts: tuple[_AttemptSource, ...],
    requested: str | None,
) -> str | None:
    missing = tuple(
        item.attempt_id for item in attempts if item.attempt_record is None
    )
    recorded = tuple(
        item.attempt_id
        for item in attempts
        if item.attempt_record is not None and item.attempt_record.accepted_attempt
    )
    if len(recorded) > 1:
        raise PilotUsageError("task contains more than one accepted attempt")
    if requested is None:
        if missing:
            raise PilotUsageError(
                "an absent attempt record requires the named accepted attempt"
            )
        return None if not recorded else f"{task_id}/{recorded[0]}"

    suffix = requested.removeprefix(f"{task_id}/")
    if suffix not in {item.attempt_id for item in attempts}:
        raise PilotUsageError("named accepted attempt is absent")
    if missing not in ((), (suffix,)):
        raise PilotUsageError("only the named accepted record may be absent")
    if recorded and recorded != (suffix,):
        raise PilotUsageError("recorded and named accepted attempts differ")
    named = next(item for item in attempts if item.attempt_id == suffix)
    if named.attempt_record is not None and not named.attempt_record.accepted_attempt:
        raise PilotUsageError("named accepted attempt is recorded as rejected")
    if not named.agent_result.execution_succeeded:
        raise PilotUsageError("named accepted attempt did not execute successfully")
    if not named.output_bytes or not named.proposal_bytes:
        raise PilotUsageError("named accepted attempt lacks an imported proposal")
    return requested


def _sum_attempts(
    attempts: tuple[PilotAttemptUsage, ...],
) -> PilotTaskUsageTotals:
    return PilotTaskUsageTotals(
        attempt_count=len(attempts),
        output_bytes=sum(item.output_bytes for item in attempts),
        proposal_bytes=sum(item.proposal_bytes for item in attempts),
        stdout_bytes=sum(item.stdout_bytes for item in attempts),
        stderr_bytes=sum(item.stderr_bytes for item in attempts),
        retained_diagnostic_bytes=sum(
            item.retained_diagnostic_bytes for item in attempts
        ),
        writable_entry_count=sum(item.writable_entry_count for item in attempts),
        writable_tree_bytes=sum(item.writable_tree_bytes for item in attempts),
    )


def _enforce_attempt_budget(
    item: PilotAttemptUsage, budget: FivePaperPilotBudget
) -> None:
    limits = (
        (item.output_bytes, budget.max_proposal_bytes, "output bytes"),
        (item.proposal_bytes, budget.max_proposal_bytes, "proposal bytes"),
        (
            item.stdout_bytes,
            budget.max_stdout_bytes_per_attempt,
            "stdout bytes",
        ),
        (
            item.stderr_bytes,
            budget.max_stderr_bytes_per_attempt,
            "stderr bytes",
        ),
        (
            item.retained_diagnostic_bytes,
            budget.max_retained_diagnostic_bytes_per_attempt,
            "retained diagnostic bytes",
        ),
        (
            item.writable_entry_count,
            budget.max_writable_entries_per_attempt,
            "writable entry count",
        ),
        (
            item.writable_tree_bytes,
            budget.max_writable_tree_bytes_per_attempt,
            "writable tree bytes",
        ),
    )
    for value, limit, label in limits:
        if value > limit:
            raise PilotUsageError(f"per-attempt {label} budget exceeded")


def _enforce_total_budget(
    totals: PilotTaskUsageTotals, budget: FivePaperPilotBudget
) -> None:
    limits = (
        (totals.output_bytes, budget.max_proposal_bytes, "output bytes"),
        (totals.proposal_bytes, budget.max_proposal_bytes, "proposal bytes"),
        (totals.stdout_bytes, budget.max_cumulative_stdout_bytes, "stdout bytes"),
        (totals.stderr_bytes, budget.max_cumulative_stderr_bytes, "stderr bytes"),
        (
            totals.retained_diagnostic_bytes,
            budget.max_cumulative_retained_diagnostic_bytes,
            "retained diagnostic bytes",
        ),
        (
            totals.writable_entry_count,
            budget.max_cumulative_writable_entries,
            "writable entry count",
        ),
        (
            totals.writable_tree_bytes,
            budget.max_cumulative_writable_tree_bytes,
            "writable tree bytes",
        ),
    )
    for value, limit, label in limits:
        if value > limit:
            raise PilotUsageError(f"cumulative {label} budget exceeded")


def pilot_task_usage_budget_overruns(
    usage: PilotTaskUsageArtifact,
    budget: FivePaperPilotBudget,
) -> dict[str, int]:
    """Return exact positive overages against one task's registered limits."""

    if not isinstance(usage, PilotTaskUsageArtifact) or not isinstance(
        budget, FivePaperPilotBudget
    ):
        raise TypeError("pilot usage budget comparison requires typed inputs")
    observed = {
        "attempt_count": usage.totals.attempt_count,
        "per_attempt_output_bytes": max(
            item.output_bytes for item in usage.attempts
        ),
        "per_attempt_proposal_bytes": max(
            item.proposal_bytes for item in usage.attempts
        ),
        "per_attempt_stdout_bytes": max(
            item.stdout_bytes for item in usage.attempts
        ),
        "per_attempt_stderr_bytes": max(
            item.stderr_bytes for item in usage.attempts
        ),
        "per_attempt_retained_diagnostic_bytes": max(
            item.retained_diagnostic_bytes for item in usage.attempts
        ),
        "per_attempt_writable_entries": max(
            item.writable_entry_count for item in usage.attempts
        ),
        "per_attempt_writable_tree_bytes": max(
            item.writable_tree_bytes for item in usage.attempts
        ),
        "task_output_bytes": usage.totals.output_bytes,
        "task_proposal_bytes": usage.totals.proposal_bytes,
        "task_stdout_bytes": usage.totals.stdout_bytes,
        "task_stderr_bytes": usage.totals.stderr_bytes,
        "task_retained_diagnostic_bytes": (
            usage.totals.retained_diagnostic_bytes
        ),
        "task_writable_entries": usage.totals.writable_entry_count,
        "task_writable_tree_bytes": usage.totals.writable_tree_bytes,
    }
    limits = {
        "attempt_count": budget.max_semantic_engine_invocations,
        "per_attempt_output_bytes": budget.max_proposal_bytes,
        "per_attempt_proposal_bytes": budget.max_proposal_bytes,
        "per_attempt_stdout_bytes": budget.max_stdout_bytes_per_attempt,
        "per_attempt_stderr_bytes": budget.max_stderr_bytes_per_attempt,
        "per_attempt_retained_diagnostic_bytes": (
            budget.max_retained_diagnostic_bytes_per_attempt
        ),
        "per_attempt_writable_entries": budget.max_writable_entries_per_attempt,
        "per_attempt_writable_tree_bytes": (
            budget.max_writable_tree_bytes_per_attempt
        ),
        "task_output_bytes": budget.max_proposal_bytes,
        "task_proposal_bytes": budget.max_proposal_bytes,
        "task_stdout_bytes": budget.max_cumulative_stdout_bytes,
        "task_stderr_bytes": budget.max_cumulative_stderr_bytes,
        "task_retained_diagnostic_bytes": (
            budget.max_cumulative_retained_diagnostic_bytes
        ),
        "task_writable_entries": budget.max_cumulative_writable_entries,
        "task_writable_tree_bytes": budget.max_cumulative_writable_tree_bytes,
    }
    return {
        name: value - limits[name]
        for name, value in sorted(observed.items())
        if value > limits[name]
    }


def _revalidate_sources(
    project_root: Path, retained: dict[str, tuple[int, Sha256]]
) -> None:
    for relative, expected in sorted(retained.items()):
        content, observed = _read_source(
            project_root,
            relative,
            max_bytes=expected[0],
            label="retained attempt artifact",
        )
        if (len(content), observed) != expected:
            raise PilotUsageError("pilot attempt artifact changed during accounting")


def collect_pilot_task_usage(
    project_root: Path,
    *,
    task_id: str,
    budget: FivePaperPilotBudget,
    accepted_attempt_id: str | None = None,
    enforce_budget: bool = True,
) -> PilotTaskUsageArtifact:
    """Inspect and bind every attempt for ``task_id`` without retaining text.

    ``accepted_attempt_id`` uses the receipt form ``TASK####/attempt-id``.  It
    may be omitted for a failed-only task or when an already-written record
    unambiguously marks the accepted attempt.  During commit materialization,
    pass the receipt's accepted attempt ID; that one record may not exist yet.

    A controller may set ``enforce_budget=False`` only after a budget violation
    has already rejected publication. The returned failure witness stays
    bounded by this module's structural limits while retaining the observed
    counters that explain the terminal event.
    """

    _safe_task_id(task_id)
    if not isinstance(budget, FivePaperPilotBudget):
        raise PilotUsageError("pilot usage requires a FivePaperPilotBudget")
    accepted = _qualified_accepted_id(task_id, accepted_attempt_id)
    root = Path(project_root).absolute()

    before = _attempt_tree_snapshot(root, task_id)
    attempt_ids = _attempt_ids(before[1])
    if enforce_budget and len(attempt_ids) > budget.max_semantic_engine_invocations:
        raise PilotUsageError("pilot task exceeds its semantic invocation budget")
    sources, retained = _load_attempt_sources(
        root,
        task_id=task_id,
        attempt_ids=attempt_ids,
        entries=before[1],
        budget=budget,
    )
    accepted = _accepted_binding(task_id, sources, accepted)
    accepted_suffix = (
        None if accepted is None else accepted.removeprefix(f"{task_id}/")
    )

    attempts: list[PilotAttemptUsage] = []
    for source in sources:
        inferred = source.attempt_record is None
        is_accepted = source.attempt_id == accepted_suffix
        outcome = (
            AttemptOutcome.VALID_SCIENTIFIC_RESULT
            if inferred
            else source.attempt_record.outcome
        )
        item = PilotAttemptUsage(
            task_id=task_id,
            attempt_id=source.attempt_id,
            engine=source.agent_result.engine,
            engine_version=source.agent_result.engine_version,
            outcome=outcome,
            accepted_attempt=is_accepted,
            attempt_record_inferred=inferred,
            agent_result_content_hash=source.agent_result_hash,
            attempt_record_content_hash=source.attempt_record_hash,
            attempt_record_semantic_hash=source.attempt_record_semantic_hash,
            output_content_hash=source.output_hash,
            proposal_content_hash=source.proposal_hash,
            proposal_semantic_hash=source.proposal_semantic_hash,
            stdout_content_hash=source.stdout_hash,
            stderr_content_hash=source.stderr_hash,
            writable_inventory_hash=source.writable_inventory_hash,
            output_bytes=source.output_bytes,
            proposal_bytes=source.proposal_bytes,
            stdout_bytes=source.stdout_bytes,
            stderr_bytes=source.stderr_bytes,
            retained_diagnostic_bytes=source.retained_diagnostic_bytes,
            writable_entry_count=source.writable_entry_count,
            writable_tree_bytes=source.writable_tree_bytes,
        )
        if enforce_budget:
            _enforce_attempt_budget(item, budget)
        attempts.append(item)

    exact_attempts = tuple(attempts)
    totals = _sum_attempts(exact_attempts)
    if enforce_budget:
        _enforce_total_budget(totals, budget)
    _revalidate_sources(root, retained)
    after = _attempt_tree_snapshot(root, task_id)
    if after != before:
        raise PilotUsageError("pilot attempt tree changed during accounting")

    return PilotTaskUsageArtifact(
        task_id=task_id,
        accepted_attempt_id=accepted,
        budget_content_hash=hash_json(budget.model_dump(mode="json")),
        attempts=exact_attempts,
        totals=totals,
    )


def canonical_pilot_task_usage_bytes(usage: PilotTaskUsageArtifact) -> bytes:
    """Return byte-stable JSON ready for ``AuxiliaryStagingWriter``."""

    if not isinstance(usage, PilotTaskUsageArtifact):
        raise TypeError("usage must be a PilotTaskUsageArtifact")
    return canonical_json_bytes(usage.model_dump(mode="json")) + b"\n"


def build_pilot_task_usage(
    project_root: Path,
    *,
    task_id: str,
    budget: FivePaperPilotBudget,
    accepted_attempt_id: str | None = None,
) -> bytes:
    """Collect one task's exact usage and return canonical auxiliary bytes."""

    return canonical_pilot_task_usage_bytes(
        collect_pilot_task_usage(
            project_root,
            task_id=task_id,
            budget=budget,
            accepted_attempt_id=accepted_attempt_id,
        )
    )


__all__ = [
    "MAX_PILOT_ATTEMPTS_PER_TASK",
    "PILOT_USAGE_SCHEMA_VERSION",
    "PilotAttemptUsage",
    "PilotTaskUsageArtifact",
    "PilotTaskUsageTotals",
    "PilotUsageError",
    "build_pilot_task_usage",
    "canonical_pilot_task_usage_bytes",
    "collect_pilot_task_usage",
    "pilot_task_usage_budget_overruns",
]
