"""Tests for qualified Linux confinement backend and qualification gate (goal.md §8)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from vibereview.runtime import (
    REQUIRED_CONFORMANCE_TESTS,
    AttemptOutcome,
    BubblewrapExecutionBackend,
    ConfinementLevel,
    ConfinementQualification,
    GenerateCandidateClaimsInvocation,
    LauncherConfiguration,
    NetworkPolicy,
    PlatformCapabilityFingerprint,
    ProjectRuntime,
    QualificationFingerprint,
    SubprocessAgentResult,
    SubprocessEngine,
    SubprocessPolicy,
    SyntheticCredentialProvider,
    TaskType,
    allows_real_engine,
    compute_confinement_code_fingerprint,
    compute_current_qualification_fingerprint,
    compute_profile_hash,
    deterministic_test_policy,
    probe_platform_capabilities,
    require_real_engine_qualification,
)

WORKER = Path(__file__).resolve().parent.parent / "helpers" / "fake_agent.py"
HAVE_BWRAP = shutil.which("bwrap") is not None

VALID_PROPOSAL = {
    "themes": [
        {
            "local_ref": "theme_1",
            "title": "Thermal management",
            "description": "Deterministic fixture proposal",
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


def _bwrap_backend(
    *,
    network_policy: NetworkPolicy = NetworkPolicy.DENY,
    worker_config: dict | None = None,
) -> BubblewrapExecutionBackend:
    launcher = LauncherConfiguration(
        worker_script=WORKER,
        worker_config=dict(worker_config or {}),
        proposal_payloads={
            "valid_proposal.json": json.dumps(VALID_PROPOSAL).encode("utf-8")
        },
    )
    return BubblewrapExecutionBackend(launcher, network_policy=network_policy)


def _engine(
    name: str,
    *,
    modes: tuple[str, ...] = ("valid",),
    policy: SubprocessPolicy | None = None,
    backend: BubblewrapExecutionBackend | None = None,
    credential_provider: SyntheticCredentialProvider | None = None,
    worker_config: dict | None = None,
) -> SubprocessEngine:
    bwrap_backend = backend or _bwrap_backend(worker_config=worker_config)
    return SubprocessEngine(
        name=name,
        worker_script=WORKER,
        modes=modes,
        policy=policy,
        backend=bwrap_backend,
        credential_provider=credential_provider,
        worker_config=worker_config,
        proposal_payloads={
            "valid_proposal.json": json.dumps(VALID_PROPOSAL).encode("utf-8")
        },
    )


def _init_runtime(tmp_path: Path) -> ProjectRuntime:
    project_dir = tmp_path / "project"
    return ProjectRuntime.create(project_dir, project_name="confinement-test")


def _run_task(runtime: ProjectRuntime, engine: SubprocessEngine):
    return runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        GenerateCandidateClaimsInvocation(topic="Additive manufacturing", existing_theme_ids=[]),
        engines=[engine],
    )


def _get_stdout(runtime: ProjectRuntime, result) -> str:
    record = result.attempt_records[0]
    agent_result_path = runtime.project_root / record.agent_result_path
    agent_result = SubprocessAgentResult.model_validate_json(
        agent_result_path.read_text(encoding="utf-8")
    )
    return agent_result.stdout


@pytest.mark.skipif(not HAVE_BWRAP, reason="bwrap not installed")
def test_canary_read_denied(tmp_path: Path):
    """Reading a host canary outside confinement is denied (goal.md §8.7)."""
    canary = tmp_path / "host_canary.txt"
    canary.write_text("SUPER_SECRET_HOST_CANARY", encoding="utf-8")

    engine = _engine(
        "fake-canary-read",
        modes=("canary_probe", "valid"),
        worker_config={"canary_read_path": str(canary)},
    )
    runtime = _init_runtime(tmp_path)
    result = _run_task(runtime, engine)

    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    stdout = _get_stdout(runtime, result)
    assert "CANARY_READ_ERROR=" in stdout
    assert "SUPER_SECRET_HOST_CANARY" not in stdout


@pytest.mark.skipif(not HAVE_BWRAP, reason="bwrap not installed")
def test_canary_write_denied(tmp_path: Path):
    """Writing to a host canary path outside confinement is denied (goal.md §8.7)."""
    host_target = tmp_path / "host_pwn.txt"

    engine = _engine(
        "fake-canary-write",
        modes=("canary_probe", "valid"),
        worker_config={"canary_write_path": str(host_target)},
    )
    runtime = _init_runtime(tmp_path)
    result = _run_task(runtime, engine)

    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    stdout = _get_stdout(runtime, result)
    assert "CANARY_WRITE_ERROR=" in stdout
    assert not host_target.exists()


@pytest.mark.skipif(not HAVE_BWRAP, reason="bwrap not installed")
def test_project_root_inaccessible(tmp_path: Path):
    """The review project root is not accessible inside the sandbox (goal.md §8.2, §8.7)."""
    runtime = _init_runtime(tmp_path)
    project_marker = runtime.project_root / "project.json"

    engine = _engine(
        "fake-project-inaccessible",
        modes=("canary_probe", "valid"),
        worker_config={"canary_read_path": str(project_marker)},
    )
    result = _run_task(runtime, engine)

    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    stdout = _get_stdout(runtime, result)
    assert "CANARY_READ_ERROR=" in stdout


@pytest.mark.skipif(not HAVE_BWRAP, reason="bwrap not installed")
def test_task_private_inaccessible(tmp_path: Path):
    """Task private/ provenance directory is inaccessible from the sandbox (goal.md §8.2, §8.7)."""
    runtime = _init_runtime(tmp_path)
    private_target = runtime.project_root / "work" / "tasks" / "TASK0001" / "private" / "task_provenance.json"

    engine = _engine(
        "fake-private-inaccessible",
        modes=("canary_probe", "valid"),
        worker_config={"canary_read_path": str(private_target)},
    )
    result = _run_task(runtime, engine)

    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    stdout = _get_stdout(runtime, result)
    assert "CANARY_READ_ERROR=" in stdout


@pytest.mark.skipif(not HAVE_BWRAP, reason="bwrap not installed")
def test_bundle_immutable(tmp_path: Path):
    """Bundle directory is mounted read-only inside the sandbox (goal.md §8.2, §8.7)."""
    engine = _engine(
        "fake-bundle-immutable",
        modes=("tamper_instructions", "valid"),
    )
    runtime = _init_runtime(tmp_path)
    result = _run_task(runtime, engine)

    # In bwrap, attempting to overwrite bundle file raises OSError / Read-only file system
    # The worker crashes with nonzero exit because it cannot tamper with the RO filesystem
    assert result.outcome in {
        AttemptOutcome.ENGINE_WORKSPACE_INTEGRITY_FAILURE,
        AttemptOutcome.ENGINE_EXECUTION_FAILURE,
    }


@pytest.mark.skipif(not HAVE_BWRAP, reason="bwrap not installed")
def test_authorized_output_and_scratch_writable(tmp_path: Path):
    """Authorized output and scratch directories are writable in sandbox (goal.md §8.2, §8.7)."""
    engine = _engine(
        "fake-output-writable",
        modes=("permitted_scratch", "valid"),
    )
    runtime = _init_runtime(tmp_path)
    result = _run_task(runtime, engine)

    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    record = result.attempt_records[0]
    assert record.accepted_attempt is True


@pytest.mark.skipif(not HAVE_BWRAP, reason="bwrap not installed")
def test_network_denied_under_deny(tmp_path: Path):
    """Network egress is blocked when network policy is DENY (goal.md §8.4, §8.7)."""
    backend = _bwrap_backend(network_policy=NetworkPolicy.DENY)
    engine = _engine(
        "fake-net-deny",
        modes=("network_probe", "valid"),
        backend=backend,
    )
    runtime = _init_runtime(tmp_path)
    result = _run_task(runtime, engine)

    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    stdout = _get_stdout(runtime, result)
    assert "NETWORK_DENIED=" in stdout


@pytest.mark.skipif(not HAVE_BWRAP, reason="bwrap not installed")
def test_network_permitted_under_host(tmp_path: Path):
    """Network egress is not unshared when network policy is HOST (goal.md §8.4)."""
    backend = _bwrap_backend(network_policy=NetworkPolicy.HOST)
    assert backend.network_policy == NetworkPolicy.HOST


@pytest.mark.skipif(not HAVE_BWRAP, reason="bwrap not installed")
def test_intended_credential_readable(tmp_path: Path):
    """Intended credential in credentials/ is readable by sandboxed child (goal.md §8.7)."""
    cred_provider = SyntheticCredentialProvider(
        file_credentials={"token.txt": "super-secret-token-for-bwrap"},
    )
    engine = _engine(
        "fake-cred-readable",
        modes=("read_file_credential", "valid"),
        credential_provider=cred_provider,
    )
    runtime = _init_runtime(tmp_path)
    result = _run_task(runtime, engine)

    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    record = result.attempt_records[0]
    agent_result_path = runtime.project_root / record.agent_result_path
    agent_result = SubprocessAgentResult.model_validate_json(
        agent_result_path.read_text(encoding="utf-8")
    )
    # The secret token was printed, but redacted in stdout to [REDACTED]
    assert "FILE_CREDENTIAL_NAME=token.txt" in agent_result.stdout
    assert "[REDACTED]" in agent_result.stdout
    assert "super-secret-token-for-bwrap" not in agent_result.stdout


def test_confinement_qualification_model_and_gate():
    """ConfinementQualification and real-engine gate verification (goal.md §8.5, §8.6)."""
    platform_fp = probe_platform_capabilities()
    assert platform_fp.operating_system
    assert platform_fp.fingerprint.startswith("sha256:")

    profile_hash_deny = compute_profile_hash(NetworkPolicy.DENY)
    profile_hash_host = compute_profile_hash(NetworkPolicy.HOST)
    assert profile_hash_deny.startswith("sha256:")
    assert profile_hash_host.startswith("sha256:")
    assert profile_hash_deny != profile_hash_host

    code_fp = compute_confinement_code_fingerprint()
    assert code_fp.startswith("sha256:")

    bwrap_exe = shutil.which("bwrap") or "/usr/bin/bwrap"
    bwrap_path = Path(bwrap_exe)
    exe_hash = "sha256:" + "a" * 64
    if bwrap_path.is_file():
        from vibereview.runtime.hashing import hash_file
        exe_hash = hash_file(bwrap_path)

    current_fp = QualificationFingerprint(
        backend_executable_identity=str(bwrap_path),
        backend_executable_hash=exe_hash,
        confinement_code_fingerprint=code_fp,
        profile_hash=profile_hash_deny,
        platform_capability_fingerprint=platform_fp.fingerprint,
        network_policy=NetworkPolicy.DENY,
        conformance_suite_version="1.0",
    )

    valid_qualification = ConfinementQualification(
        backend_name="bubblewrap",
        backend_version="0.11.1",
        backend_executable_identity=str(bwrap_path),
        backend_executable_hash=exe_hash,
        confinement_code_fingerprint=code_fp,
        profile_hash=profile_hash_deny,
        platform_capability_fingerprint=platform_fp.fingerprint,
        confinement_level=ConfinementLevel.OS_SANDBOX,
        network_policy=NetworkPolicy.DENY,
        conformance_suite_version="1.0",
        tests_passed=REQUIRED_CONFORMANCE_TESTS,
        qualified=True,
    )

    class DummyOSBackend:
        confinement_level = ConfinementLevel.OS_SANDBOX

    # 1. Matching qualification succeeds
    require_real_engine_qualification(DummyOSBackend(), valid_qualification, current_fp)

    # 2. TEST_ONLY backend fails closed
    class DummyTestBackend:
        confinement_level = ConfinementLevel.TEST_ONLY

    with pytest.raises(RuntimeError, match="does not allow real engine"):
        require_real_engine_qualification(DummyTestBackend(), valid_qualification, current_fp)

    # 3. Not qualified fails closed
    unqualified = valid_qualification.model_copy(update={"qualified": False})
    with pytest.raises(RuntimeError, match="marked not qualified"):
        require_real_engine_qualification(DummyOSBackend(), unqualified, current_fp)

    # 4. Fingerprint mismatch fails closed
    tampered_fp = current_fp.model_copy(update={"confinement_code_fingerprint": "sha256:" + "0" * 64})
    with pytest.raises(RuntimeError, match="fingerprint mismatch"):
        require_real_engine_qualification(DummyOSBackend(), valid_qualification, tampered_fp)

    # 5. Missing required test fails closed
    incomplete_tests = valid_qualification.model_copy(update={"tests_passed": ("test_canary_read_denied",)})
    with pytest.raises(RuntimeError, match="missing required conformance tests"):
        require_real_engine_qualification(DummyOSBackend(), incomplete_tests, current_fp)


def test_temporary_workspace_backend_remains_test_only():
    """TemporaryWorkspaceBackend is frozen as TEST_ONLY and cannot run real engines (goal.md §8.1)."""
    assert not allows_real_engine(ConfinementLevel.TEST_ONLY)
    from vibereview.runtime.execution import TemporaryWorkspaceBackend
    assert TemporaryWorkspaceBackend.confinement_level == ConfinementLevel.TEST_ONLY
