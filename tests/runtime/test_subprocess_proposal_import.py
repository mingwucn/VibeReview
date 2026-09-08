"""Race-resistant proposal-import conformance (goal.md §7.8, §7.14).

The proposal is opened relative to the trusted output descriptor with
``O_NOFOLLOW``, fstat-verified before and after a bounded read, required to be
a singly linked regular file outside the bundle inode set, size-capped, strict
UTF-8 and non-empty. Unsafe types and hard links are
``ENGINE_OUTPUT_POLICY_FAILURE``; missing, empty, oversized and non-UTF-8
regular proposals are ``ENGINE_FORMAT_FAILURE``; malformed JSON passes the
import and fails at the kernel format stage. A valid proposal round-trips
byte-exact.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from vibereview.runtime import (
    AttemptFailureStage,
    AttemptOutcome,
    GenerateCandidateClaimsInvocation,
    ProjectRuntime,
    SubprocessEngine,
    TaskAttemptRecord,
    TaskType,
    deterministic_test_policy,
)

WORKER = Path(__file__).resolve().parent.parent / "helpers" / "fake_agent.py"

# deterministic_test_policy() max_proposal_bytes (goal.md §6.7).
MAX_PROPOSAL_BYTES = 1024 * 1024

# Worker fixture payload (tests/helpers/fake_agent.py MALFORMED_JSON_PAYLOAD).
MALFORMED_JSON_PAYLOAD = b'{"this is not json'

VALID_PROPOSAL: dict[str, Any] = {
    "themes": [
        {
            "local_ref": "theme_1",
            "title": "Thermal management",
            "description": "Effects of process heating",
            "origin": "generated",
            "parent_ref": None,
        }
    ],
    "claims": [
        {
            "local_ref": "claim_1",
            "theme_ref": "theme_1",
            # Multi-byte characters exercise the strict UTF-8 round-trip.
            "candidate_claim": "Preheating reduces residual stress in the β phase.",
            "origin": "generated",
            "origin_refs": [],
        }
    ],
}
# Deliberately not the kernel's canonical serialization: insertion-ordered
# keys, spaces, a trailing newline and non-ASCII bytes must all survive.
VALID_PROPOSAL_BYTES = (
    json.dumps(VALID_PROPOSAL, ensure_ascii=False, indent=2) + "\n"
).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _engine(
    name: str,
    modes: tuple[str, ...],
    *,
    worker_config: dict[str, Any] | None = None,
    payloads: dict[str, bytes] | None = None,
) -> SubprocessEngine:
    return SubprocessEngine(
        worker_script=WORKER,
        modes=modes,
        worker_config=worker_config or {},
        proposal_payloads=payloads or {},
        name=name,
        policy=deterministic_test_policy(),
    )


def _run(runtime: ProjectRuntime, engine: SubprocessEngine):
    return runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        GenerateCandidateClaimsInvocation(
            topic="proposal import", existing_theme_ids=[]
        ),
        engines=[engine],
    )


def _single_record(result) -> TaskAttemptRecord:
    assert len(result.attempt_records) == 1
    return result.attempt_records[0]


def _task_dir(runtime: ProjectRuntime, record: TaskAttemptRecord) -> Path:
    return runtime.project_root / "work" / "tasks" / record.task_id


def _attempt_dir(runtime: ProjectRuntime, record: TaskAttemptRecord) -> Path:
    return _task_dir(runtime, record) / "attempts" / record.attempt_id


def _read_agent_result(
    runtime: ProjectRuntime, record: TaskAttemptRecord
) -> tuple[str, dict[str, Any]]:
    raw = (runtime.project_root / record.agent_result_path).read_text(
        encoding="utf-8"
    )
    return raw, json.loads(raw)


def _inventory_entry(
    report: dict[str, Any], relative_path: str = "output/proposal.json"
) -> dict[str, Any] | None:
    for entry in report["inventory"]:
        if entry["relative_path"] == relative_path:
            return entry
    return None


def _assert_state_preserved(runtime: ProjectRuntime, result) -> None:
    assert result.generation is None
    assert runtime.store.current_generation() == 0


def _assert_clean_process(report: dict[str, Any]) -> None:
    assert report["exit_code"] == 0
    assert report["timed_out"] is False
    assert report["launch_error"] is None
    assert report["resource_limit_breached"] is False
    assert report["bundle_modified"] is False


def _assert_import_failure_record(record: TaskAttemptRecord, code: str) -> None:
    assert record.accepted_attempt is False
    assert record.format_valid is False
    assert record.schema_valid is False
    assert record.proposal_validation_valid is None
    assert record.output_hash is None
    matching = [
        failure for failure in record.detected_failures if failure.code == code
    ]
    assert len(matching) == 1
    failure = matching[0]
    assert failure.stage is AttemptFailureStage.PROPOSAL_FILE
    assert failure.relative_path == Path("output") / "proposal.json"
    assert record.validation_errors == [
        failure.message for failure in record.detected_failures
    ]


def _assert_no_proposal_content_leaked(
    runtime: ProjectRuntime,
    record: TaskAttemptRecord,
    raw_agent_result: str,
    agent_result: dict[str, Any],
    report: dict[str, Any],
    hidden_name: str | None,
) -> None:
    assert agent_result["output_text"] == ""
    assert record.output_hash is None
    assert not (_attempt_dir(runtime, record) / "proposal.json").exists()
    accepted = _task_dir(runtime, record) / "accepted"
    assert not (accepted / "proposal.json").exists()
    assert not (accepted / "promotion.json").exists()
    assert report["stdout_capture"]["bytes_observed"] == 0
    assert report["stderr_capture"]["bytes_observed"] == 0
    if hidden_name is not None:
        record_text = (
            _attempt_dir(runtime, record) / "attempt_record.json"
        ).read_text(encoding="utf-8")
        assert hidden_name not in raw_agent_result
        assert hidden_name not in record_text


@pytest.mark.parametrize(
    ("mode", "expected_code", "worker_config"),
    [
        ("missing_proposal", "proposal_missing", None),
        ("empty_proposal", "proposal_empty", None),
        ("non_utf8_proposal", "proposal_not_utf8", None),
        (
            "oversized_proposal",
            "proposal_oversized",
            {"proposal_size": MAX_PROPOSAL_BYTES + 1},
        ),
    ],
    ids=["missing", "empty", "non_utf8", "oversized"],
)
def test_unimportable_regular_proposals_are_format_failures(
    tmp_path, mode, expected_code, worker_config
):
    runtime = ProjectRuntime.create(
        tmp_path / "project", project_name="import-format"
    )
    engine = _engine(f"fake-import-{mode}", (mode,), worker_config=worker_config)
    result = _run(runtime, engine)
    record = _single_record(result)
    assert result.outcome is AttemptOutcome.ENGINE_FORMAT_FAILURE
    assert record.outcome is AttemptOutcome.ENGINE_FORMAT_FAILURE
    _assert_state_preserved(runtime, result)
    _assert_import_failure_record(record, expected_code)
    _, agent_result = _read_agent_result(runtime, record)
    assert agent_result["output_text"] == ""
    report = agent_result["execution_report"]
    _assert_clean_process(report)
    assert (
        report["primary_outcome"]
        == AttemptOutcome.ENGINE_FORMAT_FAILURE.value
    )
    assert report["proposal_format_invalid"] is True
    assert report["output_policy_violated"] is False
    if mode == "missing_proposal":
        assert _inventory_entry(report) is None


def test_oversized_proposal_content_is_never_read(tmp_path):
    runtime = ProjectRuntime.create(
        tmp_path / "project", project_name="import-oversized"
    )
    engine = _engine(
        "fake-import-oversized",
        ("oversized_proposal",),
        worker_config={"proposal_size": MAX_PROPOSAL_BYTES + 1},
    )
    result = _run(runtime, engine)
    record = _single_record(result)
    assert result.outcome is AttemptOutcome.ENGINE_FORMAT_FAILURE
    _assert_state_preserved(runtime, result)
    _assert_import_failure_record(record, "proposal_oversized")
    raw, agent_result = _read_agent_result(runtime, record)
    report = agent_result["execution_report"]
    _assert_clean_process(report)
    # Oversized files must not be opened or hashed (§6.9, §7.12): the import
    # rejected on the fstat size and the inventory recorded the type only.
    entry = _inventory_entry(report)
    assert entry is not None
    assert entry["file_type"] == "oversized"
    assert entry["size_bytes"] is None
    assert entry["content_hash"] is None
    assert agent_result["output_text"] == ""
    assert record.output_hash is None
    assert not (_attempt_dir(runtime, record) / "proposal.json").exists()
    assert "padding" not in raw


def test_malformed_json_fails_at_the_kernel_format_stage(tmp_path):
    runtime = ProjectRuntime.create(
        tmp_path / "project", project_name="import-malformed"
    )
    engine = _engine("fake-import-malformed", ("malformed_json",))
    result = _run(runtime, engine)
    record = _single_record(result)
    assert result.outcome is AttemptOutcome.ENGINE_FORMAT_FAILURE
    assert record.outcome is AttemptOutcome.ENGINE_FORMAT_FAILURE
    _assert_state_preserved(runtime, result)
    # The runner imported a well-formed regular file, so it detected no
    # failure; the kernel's JSON parse stage rejected the text instead.
    assert record.detected_failures == ()
    assert record.format_valid is False
    assert record.schema_valid is False
    assert record.proposal_validation_valid is None
    assert record.accepted_attempt is False
    assert record.validation_errors
    assert record.output_hash == _sha256(MALFORMED_JSON_PAYLOAD)
    _, agent_result = _read_agent_result(runtime, record)
    assert agent_result["output_text"] == MALFORMED_JSON_PAYLOAD.decode("utf-8")
    report = agent_result["execution_report"]
    _assert_clean_process(report)
    assert (
        report["primary_outcome"]
        == AttemptOutcome.VALID_SCIENTIFIC_RESULT.value
    )
    assert report["proposal_format_invalid"] is False
    assert report["output_policy_violated"] is False
    assert not (_attempt_dir(runtime, record) / "proposal.json").exists()
    accepted = _task_dir(runtime, record) / "accepted"
    assert not (accepted / "proposal.json").exists()
    assert not (accepted / "promotion.json").exists()


@pytest.mark.parametrize(
    ("mode", "expected_code", "inventory_type", "hidden_name"),
    [
        ("proposal_symlink", "proposal_symlink", "symlink", "bundle_manifest"),
        ("proposal_fifo", "proposal_unsafe_type", "fifo", None),
        ("proposal_socket", "proposal_unsafe_type", "socket", None),
        ("proposal_hardlink", "proposal_hardlink", "regular", "bundle_manifest"),
    ],
    ids=["symlink", "fifo", "socket", "hardlink"],
)
def test_unsafe_proposal_types_are_output_policy_failures(
    tmp_path, mode, expected_code, inventory_type, hidden_name
):
    runtime = ProjectRuntime.create(
        tmp_path / "project", project_name="import-unsafe"
    )
    engine = _engine(f"fake-import-{mode}", (mode,))
    result = _run(runtime, engine)
    record = _single_record(result)
    assert result.outcome is AttemptOutcome.ENGINE_OUTPUT_POLICY_FAILURE
    assert record.outcome is AttemptOutcome.ENGINE_OUTPUT_POLICY_FAILURE
    _assert_state_preserved(runtime, result)
    _assert_import_failure_record(record, expected_code)
    raw, agent_result = _read_agent_result(runtime, record)
    report = agent_result["execution_report"]
    _assert_clean_process(report)
    assert (
        report["primary_outcome"]
        == AttemptOutcome.ENGINE_OUTPUT_POLICY_FAILURE.value
    )
    assert report["output_policy_violated"] is True
    assert report["proposal_format_invalid"] is False
    _assert_no_proposal_content_leaked(
        runtime, record, raw, agent_result, report, hidden_name
    )
    entry = _inventory_entry(report)
    assert entry is not None
    assert entry["file_type"] == inventory_type
    if inventory_type != "regular":
        # FIFOs, sockets and symlinks are never opened, sized or hashed (§6.9).
        assert entry["size_bytes"] is None
        assert entry["content_hash"] is None


def test_valid_proposal_imports_byte_exact(tmp_path):
    runtime = ProjectRuntime.create(
        tmp_path / "project", project_name="import-valid"
    )
    engine = _engine(
        "fake-import-valid",
        ("valid",),
        payloads={"valid_proposal.json": VALID_PROPOSAL_BYTES},
    )
    result = _run(runtime, engine)
    record = _single_record(result)
    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert result.generation == 1
    assert runtime.store.current_generation() == 1
    assert result.allocated_ids == {"theme_1": "T0001", "claim_1": "C0001"}
    assert record.accepted_attempt is True
    assert record.format_valid is True
    assert record.schema_valid is True
    assert record.proposal_validation_valid is True
    assert record.detected_failures == ()
    assert record.validation_errors == []
    assert record.output_hash == _sha256(VALID_PROPOSAL_BYTES)
    _, agent_result = _read_agent_result(runtime, record)
    assert agent_result["output_text"].encode("utf-8") == VALID_PROPOSAL_BYTES
    report = agent_result["execution_report"]
    _assert_clean_process(report)
    assert (
        report["primary_outcome"]
        == AttemptOutcome.VALID_SCIENTIFIC_RESULT.value
    )
    # The inventory independently re-read and hashed the same bytes.
    entry = _inventory_entry(report)
    assert entry is not None
    assert entry["file_type"] == "regular"
    assert entry["size_bytes"] == len(VALID_PROPOSAL_BYTES)
    assert entry["content_hash"] == _sha256(VALID_PROPOSAL_BYTES)
    accepted = json.loads(
        (_task_dir(runtime, record) / "accepted" / "proposal.json").read_text(
            encoding="utf-8"
        )
    )
    assert accepted == VALID_PROPOSAL
