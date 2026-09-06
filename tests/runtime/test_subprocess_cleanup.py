"""Artifact retention and execution-root cleanup (goal.md §7.12, §7.14).

After every attempt the external execution root is deleted; only the bounded,
redacted attempt artifacts (attempt record, agent result with the execution
report, stdout/stderr captures, and an accepted proposal on success) are
retained inside the review project. Scratch, home/tmp and unauthorized output
contents are never copied into the project tree.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from vibereview.runtime import (
    AttemptOutcome,
    GenerateCandidateClaimsInvocation,
    ProjectRuntime,
    SubprocessEngine,
    TaskAttemptRecord,
    TaskType,
)
from vibereview.runtime.hashing import hash_bytes

WORKER = Path(__file__).resolve().parent.parent / "helpers" / "fake_agent.py"

VALID_PROPOSAL = json.dumps(
    {
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
                "candidate_claim": "Preheating changes residual stress.",
                "origin": "generated",
                "origin_refs": [],
            }
        ],
    }
).encode("utf-8")

# Content markers written by the fake worker (tests/helpers/fake_agent.py);
# they must survive only as inventory metadata, never as retained bytes.
SCRATCH_MARKER = b"permitted-scratch"
EXTRA_OUTPUT_PAYLOAD = b"unauthorized\n"

MAX_CAPTURE_BYTES = 65536

SUCCESS_ARTIFACTS = {
    "agent_result.json",
    "attempt_record.json",
    "proposal.json",
    "stderr.txt",
    "stdout.txt",
    "workspace",
}
FAILURE_ARTIFACTS = SUCCESS_ARTIFACTS - {"proposal.json"}


def _engine(name: str, modes: tuple[str, ...], **overrides) -> SubprocessEngine:
    payloads = {"valid_proposal.json": VALID_PROPOSAL}
    payloads.update(overrides.pop("proposal_payloads", {}))
    return SubprocessEngine(
        worker_script=WORKER,
        modes=modes,
        proposal_payloads=payloads,
        name=name,
        **overrides,
    )


def _run(tmp_path: Path, engines: list[SubprocessEngine]):
    runtime = ProjectRuntime.create(tmp_path / "project", project_name="cleanup")
    result = runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        GenerateCandidateClaimsInvocation(topic="cleanup", existing_theme_ids=[]),
        engines=engines,
    )
    return runtime, result


def _attempt_dir(runtime: ProjectRuntime, record: TaskAttemptRecord) -> Path:
    return (
        runtime.project_root
        / "work"
        / "tasks"
        / record.task_id
        / "attempts"
        / record.attempt_id
    )


def _execution_report(runtime: ProjectRuntime, record: TaskAttemptRecord) -> dict:
    agent_result = json.loads(
        (_attempt_dir(runtime, record) / "agent_result.json").read_text(
            encoding="utf-8"
        )
    )
    return agent_result["execution_report"]


def _project_files(runtime: ProjectRuntime) -> list[Path]:
    return [
        path for path in sorted(runtime.project_root.rglob("*")) if path.is_file()
    ]


def test_valid_attempt_removes_execution_root(tmp_path):
    runtime, result = _run(tmp_path, [_engine("cleanup-valid", ("valid",))])
    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert result.generation == 1
    report = _execution_report(runtime, result.attempt_records[0])
    root = Path(report["execution_root"])
    assert root.name.startswith("vibereview-exec-")
    assert runtime.project_root not in root.parents
    assert not root.exists()


@pytest.mark.parametrize(
    ("modes", "expected"),
    [
        (("tamper_input",), AttemptOutcome.ENGINE_WORKSPACE_INTEGRITY_FAILURE),
        (("large_scratch_file",), AttemptOutcome.ENGINE_RESOURCE_LIMIT_FAILURE),
    ],
)
def test_failed_attempt_removes_execution_root(tmp_path, modes, expected):
    runtime = ProjectRuntime.create(tmp_path / "project", project_name="cleanup")
    generation_before = runtime.store.current_generation()
    result = runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        GenerateCandidateClaimsInvocation(topic="cleanup", existing_theme_ids=[]),
        engines=[_engine("cleanup-failure", modes)],
    )
    assert result.outcome is expected
    assert result.generation is None
    assert runtime.store.current_generation() == generation_before
    record = result.attempt_records[0]
    assert record.outcome is expected
    report = _execution_report(runtime, record)
    root = Path(report["execution_root"])
    assert root.name.startswith("vibereview-exec-")
    assert not root.exists()


def test_retained_artifacts_exist_and_are_bounded(tmp_path):
    runtime, result = _run(tmp_path, [_engine("cleanup-retain", ("valid",))])
    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    record = result.attempt_records[0]
    attempt_dir = _attempt_dir(runtime, record)

    stdout_path = attempt_dir / "stdout.txt"
    stderr_path = attempt_dir / "stderr.txt"
    assert stdout_path.is_file()
    assert stderr_path.is_file()
    assert stdout_path.stat().st_size <= MAX_CAPTURE_BYTES
    assert stderr_path.stat().st_size <= MAX_CAPTURE_BYTES

    report = _execution_report(runtime, record)
    assert isinstance(report["inventory"], list)
    for stream, path in (("stdout", stdout_path), ("stderr", stderr_path)):
        capture = report[f"{stream}_capture"]
        assert capture["relative_path"] == f"attempt/{stream}.txt"
        assert capture["bytes_retained"] == path.stat().st_size
        assert capture["bytes_retained"] <= MAX_CAPTURE_BYTES
        assert capture["truncated"] is False
        assert capture["retained_redacted_hash"] == hash_bytes(path.read_bytes())

    persisted_record = TaskAttemptRecord.model_validate_json(
        (attempt_dir / "attempt_record.json").read_text(encoding="utf-8")
    )
    assert persisted_record == record
    assert {path.name for path in attempt_dir.iterdir()} == SUCCESS_ARTIFACTS


def test_permitted_scratch_contents_are_not_retained(tmp_path):
    runtime, result = _run(tmp_path, [_engine("cleanup-scratch", ("permitted_scratch",))])
    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    record = result.attempt_records[0]
    report = _execution_report(runtime, record)
    inventory = {entry["relative_path"]: entry for entry in report["inventory"]}
    scratch_entry = inventory["scratch/ok.txt"]
    assert scratch_entry["file_type"] == "regular"
    assert scratch_entry["size_bytes"] == 1024
    assert scratch_entry["content_hash"] is not None
    for relative, entry in inventory.items():
        if relative.startswith(("home/", "tmp/")):
            assert entry["file_type"] == "directory"
            assert entry["size_bytes"] is None
            assert entry["content_hash"] is None
    for path in _project_files(runtime):
        assert SCRATCH_MARKER not in path.read_bytes(), path


def test_unauthorized_output_content_is_not_retained(tmp_path):
    runtime, result = _run(tmp_path, [_engine("cleanup-extra", ("extra_output",))])
    assert result.outcome is AttemptOutcome.ENGINE_OUTPUT_POLICY_FAILURE
    assert result.generation is None
    record = result.attempt_records[0]
    codes = {
        failure["code"]
        for failure in _execution_report(runtime, record)["detected_failures"]
    }
    assert "unexpected_output_file" in codes
    for path in _project_files(runtime):
        assert EXTRA_OUTPUT_PAYLOAD not in path.read_bytes(), path
    attempt_dir = _attempt_dir(runtime, record)
    assert not (attempt_dir / "proposal.json").exists()
    assert {path.name for path in attempt_dir.iterdir()} == FAILURE_ARTIFACTS


def test_no_execution_root_leaks_across_multi_attempt_run(tmp_path):
    execution_root_parent = tmp_path / "execution-roots"
    execution_root_parent.mkdir(parents=True, exist_ok=True)
    runtime, result = _run(
        tmp_path,
        [
            _engine(
                "cleanup-tamper",
                ("tamper_input",),
                execution_root_parent=execution_root_parent,
            ),
            _engine(
                "cleanup-valid",
                ("valid",),
                execution_root_parent=execution_root_parent,
            ),
        ],
    )
    assert [record.outcome for record in result.attempt_records] == [
        AttemptOutcome.ENGINE_WORKSPACE_INTEGRITY_FAILURE,
        AttemptOutcome.VALID_SCIENTIFIC_RESULT,
    ]
    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    for record in result.attempt_records:
        report = _execution_report(runtime, record)
        assert not Path(report["execution_root"]).exists()
    assert list(execution_root_parent.glob("vibereview-exec-*")) == []


@pytest.mark.parametrize("modes", [("valid",), ("tamper_input",)])
def test_project_and_private_paths_appear_in_no_argv_or_persisted_artifact(
    tmp_path, modes
):
    runtime, result = _run(tmp_path, [_engine("cleanup-paths", modes)])
    record = result.attempt_records[0]
    report = _execution_report(runtime, record)
    project_text = str(runtime.project_root)
    private_text = str(
        runtime.project_root / "work" / "tasks" / record.task_id / "private"
    )
    assert report["argv"]
    for element in report["argv"]:
        assert project_text not in element
        assert private_text not in element
    for artifact in _attempt_dir(runtime, record).iterdir():
        if artifact.is_file():
            content = artifact.read_bytes()
            assert project_text.encode() not in content, artifact
            assert private_text.encode() not in content, artifact


def test_agent_result_path_is_project_relative(tmp_path):
    runtime, result = _run(
        tmp_path,
        [
            _engine("cleanup-rel-fail", ("tamper_input",)),
            _engine("cleanup-rel-ok", ("valid",)),
        ],
    )
    assert len(result.attempt_records) == 2
    for record in result.attempt_records:
        assert not record.agent_result_path.is_absolute()
        resolved = runtime.project_root / record.agent_result_path
        assert resolved.is_file()
        persisted_record = TaskAttemptRecord.model_validate_json(
            (_attempt_dir(runtime, record) / "attempt_record.json").read_text(
                encoding="utf-8"
            )
        )
        assert not persisted_record.agent_result_path.is_absolute()
        assert persisted_record.agent_result_path == record.agent_result_path
