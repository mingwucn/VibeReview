"""Subprocess credential hygiene (goal.md §8.2, §7.5-§7.6, §8.1).

Two mandatory secret tests plus the chunk-boundary redaction case:

1. A secret that exists only in the parent environment is not allowlisted,
   never reaches the child process, and appears in no persisted artifact.
2. A secret injected through SyntheticCredentialProvider is visible to the
   child, is redacted from persisted stdout diagnostics, and appears in no
   manifest or attempt JSON; the external execution root (including
   ``credentials/``) is removed, and CredentialContext repr/dumps mask it.
3. A secret printed repeatedly across many 64 KiB stream-read chunks is
   still redacted everywhere (the redactor's carry-over window).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from vibereview.runtime import (
    AttemptOutcome,
    GenerateCandidateClaimsInvocation,
    ProjectRuntime,
    SubprocessEngine,
    SyntheticCredentialProvider,
    TaskType,
    deterministic_test_policy,
)
from vibereview.runtime.hashing import hash_bytes, hash_file

WORKER = Path(__file__).resolve().parent.parent / "helpers" / "fake_agent.py"

PARENT_SECRET_VAR = "VIBEREVIEW_PARENT_SECRET"
PARENT_SECRET_VALUE = "VIBEREVIEW_TEST_SENTINEL_PARENT_ONLY"
INJECTED_SECRET_VAR = "VIBEREVIEW_INJECTED_SECRET"
INJECTED_SECRET_VALUE = "VIBEREVIEW_TEST_SENTINEL_INJECTED"
CHUNK_SECRET_VALUE = "VIBEREVIEW_TEST_SENTINEL_CHUNK_BOUNDARY"
CHUNK_PROBE_REPEATS = 2048

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


def _run_discovery(tmp_path: Path, engine: SubprocessEngine, project_name: str):
    runtime = ProjectRuntime.create(tmp_path / "project", project_name=project_name)
    result = runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        GenerateCandidateClaimsInvocation(topic="secrets", existing_theme_ids=[]),
        engines=[engine],
    )
    return runtime, result


def _attempt_dir(runtime: ProjectRuntime, result) -> Path:
    record = result.attempt_records[0]
    return (
        runtime.project_root
        / "work"
        / "tasks"
        / record.task_id
        / "attempts"
        / record.attempt_id
    )


def _execution_report(attempt_dir: Path) -> dict:
    return json.loads((attempt_dir / "agent_result.json").read_bytes())[
        "execution_report"
    ]


def _assert_bytes_absent(root: Path, needle: bytes) -> None:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            assert needle not in path.read_bytes(), f"secret leaked into {path}"


def test_parent_environment_secret_never_reaches_child_or_artifacts(
    tmp_path, monkeypatch
):
    """§8.2 parent-only secret: present in the parent, absent everywhere else."""

    monkeypatch.setenv(PARENT_SECRET_VAR, PARENT_SECRET_VALUE)
    policy = deterministic_test_policy()
    assert PARENT_SECRET_VAR not in policy.inherited_environment_allowlist
    engine = SubprocessEngine(
        worker_script=WORKER,
        modes=("parent_secret_probe", "valid"),
        proposal_payloads={"valid_proposal.json": VALID_PROPOSAL},
        policy=policy,
        name="parent-secret-probe",
    )
    runtime, result = _run_discovery(tmp_path, engine, "parent-secret")

    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert result.generation == 1
    attempt_dir = _attempt_dir(runtime, result)
    stdout_bytes = (attempt_dir / "stdout.txt").read_bytes()
    # The child itself reports the variable unset: it never entered the
    # allowlisted child environment.
    assert stdout_bytes == b"PARENT_SECRET=<absent>\n"
    report = _execution_report(attempt_dir)
    assert report["stdout_capture"]["bytes_retained"] == len(stdout_bytes)
    # No redaction values were registered: the parent secret was invisible
    # to the credential boundary as well.
    assert report["stdout_capture"]["redactions_applied"] == 0
    # The secret value appears in no persisted artifact (attempt dir, task
    # dir, manifests, cache).
    _assert_bytes_absent(runtime.project_root, PARENT_SECRET_VALUE.encode("utf-8"))
    # The in-test parent environment is intact; monkeypatch restores it.
    assert os.environ[PARENT_SECRET_VAR] == PARENT_SECRET_VALUE


def test_injected_secret_is_redacted_and_never_persisted(tmp_path):
    """§8.2 injected secret: usable by the child, redacted in diagnostics."""

    provider = SyntheticCredentialProvider(
        secret_env={INJECTED_SECRET_VAR: INJECTED_SECRET_VALUE}
    )
    engine = SubprocessEngine(
        worker_script=WORKER,
        modes=("injected_secret_probe", "valid"),
        proposal_payloads={"valid_proposal.json": VALID_PROPOSAL},
        credential_provider=provider,
        name="injected-secret-probe",
    )
    runtime, result = _run_discovery(tmp_path, engine, "injected-secret")

    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert result.generation == 1
    record = result.attempt_records[0]
    assert record.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert record.accepted_attempt
    attempt_dir = _attempt_dir(runtime, result)

    # The child saw the injected value and printed it; the persisted capture
    # holds only the redacted form.
    stdout_path = attempt_dir / "stdout.txt"
    stdout_bytes = stdout_path.read_bytes()
    assert stdout_bytes == b"INJECTED_SECRET=[REDACTED]\n"
    assert INJECTED_SECRET_VALUE.encode("utf-8") not in stdout_bytes

    capture = _execution_report(attempt_dir)["stdout_capture"]
    assert capture["redactions_applied"] >= 1
    # The retained hash covers exactly the persisted redacted file and never
    # the unredacted secret-bearing stream.
    assert capture["retained_redacted_hash"] == hash_file(stdout_path)
    unredacted_stream = (
        b"INJECTED_SECRET=" + INJECTED_SECRET_VALUE.encode("utf-8") + b"\n"
    )
    assert capture["retained_redacted_hash"] != hash_bytes(unredacted_stream)

    # The secret is absent from the attempt JSON and every manifest byte.
    secret_bytes = INJECTED_SECRET_VALUE.encode("utf-8")
    assert secret_bytes not in (attempt_dir / "attempt_record.json").read_bytes()
    assert secret_bytes not in (attempt_dir / "agent_result.json").read_bytes()
    assert secret_bytes not in (attempt_dir / "stderr.txt").read_bytes()
    assert secret_bytes not in (attempt_dir / "proposal.json").read_bytes()
    _assert_bytes_absent(runtime.project_root, secret_bytes)

    # The external execution root — including its credentials/ directory —
    # is removed once the safe artifacts are imported (§7.12).
    report = _execution_report(attempt_dir)
    execution_root = report["execution_root"]
    assert execution_root is not None
    assert not Path(execution_root).exists()
    assert not (Path(execution_root) / "credentials").exists()

    # CredentialContext never exposes the secret through repr or dumps, and
    # the nonsecret fingerprint does not depend on the secret value (§8.1).
    context = provider.prepare(engine.name, tmp_path)
    assert INJECTED_SECRET_VALUE not in repr(context)
    assert INJECTED_SECRET_VALUE not in str(context)
    assert INJECTED_SECRET_VALUE not in repr(context.model_dump())
    assert INJECTED_SECRET_VALUE not in context.model_dump_json()
    assert "exact_redaction_values" not in context.model_dump()
    assert "exact_redaction_values" not in context.model_dump_json()
    other = SyntheticCredentialProvider(
        secret_env={INJECTED_SECRET_VAR: "VIBEREVIEW_TEST_SENTINEL_DIFFERENT"}
    )
    assert (
        context.nonsecret_configuration_fingerprint
        == other.prepare(engine.name, tmp_path).nonsecret_configuration_fingerprint
    )
    # The cache-signature input carries no secret material.
    safe_configuration = json.dumps(
        dict(engine.safe_configuration()), ensure_ascii=False, sort_keys=True
    )
    assert INJECTED_SECRET_VALUE not in safe_configuration


def test_secret_spanning_stream_chunks_is_redacted(tmp_path):
    """§7.6: redaction holds for secrets split across stream-read chunks."""

    provider = SyntheticCredentialProvider(
        secret_env={INJECTED_SECRET_VAR: CHUNK_SECRET_VALUE}
    )
    policy = deterministic_test_policy().model_copy(
        update={"max_stdout_bytes": 8 * 1024 * 1024}
    )
    engine = SubprocessEngine(
        worker_script=WORKER,
        modes=("injected_secret_probe",) * CHUNK_PROBE_REPEATS + ("valid",),
        proposal_payloads={"valid_proposal.json": VALID_PROPOSAL},
        policy=policy,
        credential_provider=provider,
        name="chunk-redaction-probe",
    )
    runtime, result = _run_discovery(tmp_path, engine, "chunk-redaction")

    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert result.generation == 1
    attempt_dir = _attempt_dir(runtime, result)

    # The raw stream spans many 64 KiB read chunks, so secret occurrences
    # straddle chunk boundaries; the carry-over window must catch them all.
    raw_line = b"INJECTED_SECRET=" + CHUNK_SECRET_VALUE.encode("utf-8") + b"\n"
    raw_stream = raw_line * CHUNK_PROBE_REPEATS
    assert len(raw_stream) > 65536

    stdout_path = attempt_dir / "stdout.txt"
    stdout_bytes = stdout_path.read_bytes()
    redacted_line = b"INJECTED_SECRET=[REDACTED]\n"
    assert stdout_bytes == redacted_line * CHUNK_PROBE_REPEATS
    assert CHUNK_SECRET_VALUE.encode("utf-8") not in stdout_bytes

    capture = _execution_report(attempt_dir)["stdout_capture"]
    assert capture["bytes_observed"] == len(raw_stream)
    assert capture["truncated"] is False
    assert capture["bytes_retained"] == len(stdout_bytes)
    assert capture["redactions_applied"] == CHUNK_PROBE_REPEATS
    assert stdout_bytes.count(b"[REDACTED]") == CHUNK_PROBE_REPEATS
    assert capture["retained_redacted_hash"] == hash_file(stdout_path)
    assert capture["retained_redacted_hash"] != hash_bytes(raw_stream)
    _assert_bytes_absent(
        runtime.project_root, CHUNK_SECRET_VALUE.encode("utf-8")
    )
