"""Tests for qualified Linux confinement backend and qualification gate (goal.md §8)."""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest

from vibereview.runtime import (
    REQUIRED_CONFORMANCE_TESTS,
    AttemptOutcome,
    BubblewrapExecutionBackend,
    ConfinementLevel,
    GenerateCandidateClaimsInvocation,
    LauncherConfiguration,
    NetworkPolicy,
    ProjectRuntime,
    SandboxProbeStatus,
    SandboxProfile,
    SubprocessAgentResult,
    SubprocessEngine,
    SubprocessPolicy,
    SyntheticCredentialProvider,
    TaskType,
    allows_real_engine,
    compute_confinement_code_fingerprint,
    compute_profile_hash,
    deterministic_test_policy,
    issue_qualification,
    probe_platform_capabilities,
    require_real_engine_qualification,
    sandbox_profile,
)
from vibereview.runtime.confinement import SANDBOX_PROFILE_CANONICAL
from vibereview.runtime.hashing import canonical_json_bytes, hash_bytes

from helpers.conformance_fixtures import (
    current_fingerprint_for,
    fabricated_passing_report,
    fabricated_probe,
)
from helpers.sandbox_assert import assert_sandbox_result_valid
from helpers.sandbox_gate import require_usable_sandbox

WORKER = Path(__file__).resolve().parent.parent / "helpers" / "fake_agent.py"

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


def _get_agent_result(runtime: ProjectRuntime, result) -> SubprocessAgentResult:
    record = result.attempt_records[0]
    agent_result_path = runtime.project_root / record.agent_result_path
    return SubprocessAgentResult.model_validate_json(
        agent_result_path.read_text(encoding="utf-8")
    )


def _get_stdout(runtime: ProjectRuntime, result) -> str:
    return _get_agent_result(runtime, result).stdout


LOOPBACK_RESPONSE = "vibereview-loopback-ok"


class _LoopbackServer:
    """Temporary loopback TCP server with a known response (goal.md §5.4)."""

    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(4)
        self._socket.settimeout(0.2)
        self.connections_accepted = 0
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._serve, name="vibereview-loopback-test", daemon=True
        )
        self._thread.start()

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        return self._socket.getsockname()[1]

    def _serve(self) -> None:
        while not self._stopped.is_set():
            try:
                connection, _ = self._socket.accept()
            except (TimeoutError, OSError):
                continue
            with connection:
                self.connections_accepted += 1
                connection.sendall((LOOPBACK_RESPONSE + "\n").encode("utf-8"))

    def close(self) -> None:
        self._stopped.set()
        self._socket.close()
        self._thread.join(timeout=2.0)


@pytest.fixture
def loopback_server():
    server = _LoopbackServer()
    try:
        yield server
    finally:
        server.close()


@pytest.mark.requires_bwrap
@pytest.mark.sandbox_conformance
def test_canary_read_denied(tmp_path: Path):
    """Reading a host canary outside confinement is denied (goal.md §8.7)."""
    require_usable_sandbox()
    canary = tmp_path / "host_canary.txt"
    canary.write_text("SUPER_SECRET_HOST_CANARY", encoding="utf-8")

    engine = _engine(
        "fake-canary-read",
        modes=("canary_probe", "valid"),
        worker_config={"canary_read_path": str(canary)},
    )
    runtime = _init_runtime(tmp_path)
    result = _run_task(runtime, engine)

    assert_sandbox_result_valid(runtime, result)
    stdout = _get_stdout(runtime, result)
    assert "CANARY_READ_ERROR=" in stdout
    assert "SUPER_SECRET_HOST_CANARY" not in stdout


@pytest.mark.requires_bwrap
@pytest.mark.sandbox_conformance
def test_canary_write_denied(tmp_path: Path):
    """Writing to a host canary path outside confinement is denied (goal.md §8.7)."""
    require_usable_sandbox()
    host_target = tmp_path / "host_pwn.txt"

    engine = _engine(
        "fake-canary-write",
        modes=("canary_probe", "valid"),
        worker_config={"canary_write_path": str(host_target)},
    )
    runtime = _init_runtime(tmp_path)
    result = _run_task(runtime, engine)

    assert_sandbox_result_valid(runtime, result)
    stdout = _get_stdout(runtime, result)
    assert "CANARY_WRITE_ERROR=" in stdout
    assert not host_target.exists()


@pytest.mark.requires_bwrap
@pytest.mark.sandbox_conformance
def test_project_root_inaccessible(tmp_path: Path):
    """The review project root is not accessible inside the sandbox (goal.md §8.2, §8.7)."""
    require_usable_sandbox()
    runtime = _init_runtime(tmp_path)
    project_marker = runtime.project_root / "project.json"

    engine = _engine(
        "fake-project-inaccessible",
        modes=("canary_probe", "valid"),
        worker_config={"canary_read_path": str(project_marker)},
    )
    result = _run_task(runtime, engine)

    assert_sandbox_result_valid(runtime, result)
    stdout = _get_stdout(runtime, result)
    assert "CANARY_READ_ERROR=" in stdout


@pytest.mark.requires_bwrap
@pytest.mark.sandbox_conformance
def test_task_private_inaccessible(tmp_path: Path):
    """Task private/ provenance directory is inaccessible from the sandbox (goal.md §8.2, §8.7)."""
    require_usable_sandbox()
    runtime = _init_runtime(tmp_path)
    private_target = runtime.project_root / "work" / "tasks" / "TASK0001" / "private" / "task_provenance.json"

    engine = _engine(
        "fake-private-inaccessible",
        modes=("canary_probe", "valid"),
        worker_config={"canary_read_path": str(private_target)},
    )
    result = _run_task(runtime, engine)

    assert_sandbox_result_valid(runtime, result)
    stdout = _get_stdout(runtime, result)
    assert "CANARY_READ_ERROR=" in stdout


@pytest.mark.requires_bwrap
@pytest.mark.sandbox_conformance
def test_bundle_immutable(tmp_path: Path):
    """Bundle directory is mounted read-only inside the sandbox (goal.md §8.2, §8.7)."""
    require_usable_sandbox()
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


@pytest.mark.requires_bwrap
@pytest.mark.sandbox_conformance
def test_authorized_output_and_scratch_writable(tmp_path: Path):
    """Authorized output and scratch directories are writable in sandbox (goal.md §8.2, §8.7)."""
    require_usable_sandbox()
    engine = _engine(
        "fake-output-writable",
        modes=("permitted_scratch", "valid"),
    )
    runtime = _init_runtime(tmp_path)
    result = _run_task(runtime, engine)

    assert_sandbox_result_valid(runtime, result)
    record = result.attempt_records[0]
    assert record.accepted_attempt is True


@pytest.mark.requires_bwrap
@pytest.mark.sandbox_conformance
def test_network_denied_under_deny(tmp_path: Path, loopback_server):
    """Network egress is blocked when network policy is DENY (goal.md §8.4, §8.7).

    Loopback-based: the worker cannot even reach a server on the host's own
    loopback interface, so the test never depends on the public internet.
    """
    require_usable_sandbox()
    backend = _bwrap_backend(
        network_policy=NetworkPolicy.DENY,
        worker_config={
            "network_host": loopback_server.host,
            "network_port": loopback_server.port,
        },
    )
    engine = _engine(
        "fake-net-deny",
        modes=("loopback_connect", "valid"),
        backend=backend,
    )
    runtime = _init_runtime(tmp_path)
    result = _run_task(runtime, engine)

    assert_sandbox_result_valid(runtime, result)
    stdout = _get_stdout(runtime, result)
    assert "NETWORK_DENIED=" in stdout
    assert "NETWORK_RESPONSE=" not in stdout


@pytest.mark.requires_bwrap
@pytest.mark.sandbox_conformance
def test_host_network_profile_connects_to_loopback_server(tmp_path: Path, loopback_server):
    """HOST policy keeps the host network: the worker reaches the loopback server (goal.md §5.4)."""
    require_usable_sandbox()
    backend = _bwrap_backend(
        network_policy=NetworkPolicy.HOST,
        worker_config={
            "network_host": loopback_server.host,
            "network_port": loopback_server.port,
        },
    )
    engine = _engine(
        "fake-net-host",
        modes=("loopback_connect", "valid"),
        backend=backend,
    )
    assert backend.network_policy == NetworkPolicy.HOST
    runtime = _init_runtime(tmp_path)
    result = _run_task(runtime, engine)

    assert_sandbox_result_valid(runtime, result)
    stdout = _get_stdout(runtime, result)
    assert f"NETWORK_RESPONSE={LOOPBACK_RESPONSE}" in stdout
    assert loopback_server.connections_accepted >= 1


@pytest.mark.requires_bwrap
@pytest.mark.sandbox_conformance
def test_deny_network_profile_cannot_connect(tmp_path: Path, loopback_server):
    """DENY policy: the same loopback endpoint is unreachable, the proposal still succeeds (goal.md §5.4)."""
    require_usable_sandbox()
    backend = _bwrap_backend(
        network_policy=NetworkPolicy.DENY,
        worker_config={
            "network_host": loopback_server.host,
            "network_port": loopback_server.port,
        },
    )
    engine = _engine(
        "fake-net-deny-unreachable",
        modes=("loopback_connect", "valid"),
        backend=backend,
    )
    runtime = _init_runtime(tmp_path)
    result = _run_task(runtime, engine)

    assert_sandbox_result_valid(runtime, result)
    stdout = _get_stdout(runtime, result)
    assert "NETWORK_DENIED=" in stdout
    assert loopback_server.connections_accepted == 0


@pytest.mark.requires_bwrap
@pytest.mark.sandbox_conformance
def test_intended_credential_readable(tmp_path: Path):
    """Intended credential in credentials/ is readable by sandboxed child (goal.md §8.7)."""
    require_usable_sandbox()
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

    assert_sandbox_result_valid(runtime, result)
    record = result.attempt_records[0]
    agent_result_path = runtime.project_root / record.agent_result_path
    agent_result = SubprocessAgentResult.model_validate_json(
        agent_result_path.read_text(encoding="utf-8")
    )
    # The secret token was printed, but redacted in stdout to [REDACTED]
    assert "FILE_CREDENTIAL_NAME=token.txt" in agent_result.stdout
    assert "[REDACTED]" in agent_result.stdout
    assert "super-secret-token-for-bwrap" not in agent_result.stdout


@pytest.mark.requires_bwrap
@pytest.mark.sandbox_conformance
def test_credential_diagnostic_redaction(tmp_path: Path):
    """Leased credential material never appears in retained diagnostics (goal.md §6.1)."""
    require_usable_sandbox()
    secret = "r5g-redaction-token"
    cred_provider = SyntheticCredentialProvider(
        file_credentials={"token.txt": secret},
    )
    engine = _engine(
        "fake-cred-redaction",
        modes=("read_file_credential", "valid"),
        credential_provider=cred_provider,
    )
    runtime = _init_runtime(tmp_path)
    result = _run_task(runtime, engine)

    assert_sandbox_result_valid(runtime, result)
    agent_result = _get_agent_result(runtime, result)
    assert "[REDACTED]" in agent_result.stdout
    assert secret not in agent_result.stdout
    assert secret not in agent_result.stderr


def test_explicit_namespace_profile_matches_profile_hash():
    """The profile hash is generated from the explicit SandboxProfile structure (goal.md §5.1)."""
    deny_profile = sandbox_profile(NetworkPolicy.DENY)
    assert deny_profile == SandboxProfile(
        user_namespace=True,
        mount_namespace=True,
        pid_namespace=True,
        ipc_namespace=True,
        uts_namespace=True,
        cgroup_namespace=True,
        network_policy=NetworkPolicy.DENY,
    )
    expected_deny = hash_bytes(
        canonical_json_bytes(
            {
                **SANDBOX_PROFILE_CANONICAL,
                "profile": deny_profile.model_dump(mode="json"),
            }
        )
    )
    assert compute_profile_hash(NetworkPolicy.DENY) == expected_deny

    host_profile = sandbox_profile(NetworkPolicy.HOST)
    expected_host = hash_bytes(
        canonical_json_bytes(
            {
                **SANDBOX_PROFILE_CANONICAL,
                "profile": host_profile.model_dump(mode="json"),
            }
        )
    )
    assert compute_profile_hash(NetworkPolicy.HOST) == expected_host
    assert compute_profile_hash(NetworkPolicy.DENY) != compute_profile_hash(NetworkPolicy.HOST)


@pytest.mark.requires_bwrap
@pytest.mark.sandbox_conformance
def test_xdg_paths_are_sandbox_local(tmp_path: Path):
    """HOME, XDG and TMPDIR variables are reconstructed sandbox-locally (goal.md §5.2)."""
    require_usable_sandbox()
    engine = _engine("fake-env-probe", modes=("env_probe", "valid"))
    runtime = _init_runtime(tmp_path)
    result = _run_task(runtime, engine)

    assert_sandbox_result_valid(runtime, result)
    stdout = _get_stdout(runtime, result)
    values = {}
    for line in stdout.splitlines():
        if line.startswith(("HOME=", "XDG_CONFIG_HOME=", "XDG_CACHE_HOME=", "TMPDIR=")):
            name, _, value = line.partition("=")
            values[name] = value
    assert values == {
        "HOME": "/work/home",
        "XDG_CONFIG_HOME": "/work/home/.config",
        "XDG_CACHE_HOME": "/work/home/.cache",
        "TMPDIR": "/work/tmp",
    }
    for value in values.values():
        assert value.startswith("/work/")


@pytest.mark.requires_bwrap
@pytest.mark.sandbox_conformance
def test_bundle_and_launcher_are_read_only(tmp_path: Path):
    """Neither bundle/ nor launcher/ accepts writes inside the sandbox (goal.md §5.3)."""
    require_usable_sandbox()
    engine = _engine(
        "fake-read-only-roots",
        modes=("bundle_write_probe", "launcher_write_probe", "valid"),
    )
    runtime = _init_runtime(tmp_path)
    result = _run_task(runtime, engine)

    assert_sandbox_result_valid(runtime, result)
    stdout = _get_stdout(runtime, result)
    assert "BUNDLE_WRITE_ERROR=" in stdout
    assert "BUNDLE_WRITE_OK=" not in stdout
    assert "LAUNCHER_WRITE_ERROR=" in stdout
    assert "LAUNCHER_WRITE_OK=" not in stdout


@pytest.mark.requires_bwrap
@pytest.mark.sandbox_conformance
def test_output_and_scratch_are_writable(tmp_path: Path):
    """output/ and scratch/ accept writes and are inventoried (goal.md §5.3)."""
    require_usable_sandbox()
    engine = _engine("fake-writable-roots", modes=("permitted_scratch",))
    runtime = _init_runtime(tmp_path)
    result = _run_task(runtime, engine)

    assert_sandbox_result_valid(runtime, result)
    record = result.attempt_records[0]
    assert record.accepted_attempt is True
    agent_result = _get_agent_result(runtime, result)
    inventory_paths = {
        entry.relative_path.as_posix()
        for entry in agent_result.execution_report.inventory
    }
    assert "scratch/ok.txt" in inventory_paths
    assert "output/proposal.json" in inventory_paths


@pytest.mark.requires_bwrap
@pytest.mark.sandbox_conformance
def test_credentials_are_minimally_visible(tmp_path: Path):
    """Only the intended credential is visible, and credentials/ is read-only (goal.md §5.3)."""
    require_usable_sandbox()
    cred_provider = SyntheticCredentialProvider(
        file_credentials={"token.txt": "r5f-minimal-credential-token"},
    )
    engine = _engine(
        "fake-cred-minimal",
        modes=("read_file_credential", "credential_write_probe", "valid"),
        credential_provider=cred_provider,
    )
    runtime = _init_runtime(tmp_path)
    result = _run_task(runtime, engine)

    assert_sandbox_result_valid(runtime, result)
    agent_result = _get_agent_result(runtime, result)
    visible = [
        line
        for line in agent_result.stdout.splitlines()
        if line.startswith("FILE_CREDENTIAL_NAME=")
    ]
    assert visible == ["FILE_CREDENTIAL_NAME=token.txt"]
    assert "CREDENTIAL_WRITE_ERROR=" in agent_result.stdout
    assert "CREDENTIAL_WRITE_OK=" not in agent_result.stdout
    assert "[REDACTED]" in agent_result.stdout
    assert "r5f-minimal-credential-token" not in agent_result.stdout


@pytest.mark.requires_bwrap
@pytest.mark.sandbox_conformance
def test_project_root_is_absent(tmp_path: Path):
    """The review project root does not exist inside the sandbox at all (goal.md §5.3)."""
    require_usable_sandbox()
    runtime = _init_runtime(tmp_path)

    engine = _engine(
        "fake-project-absent",
        modes=("canary_probe", "valid"),
        worker_config={"canary_read_path": str(runtime.project_root)},
    )
    result = _run_task(runtime, engine)

    assert_sandbox_result_valid(runtime, result)
    stdout = _get_stdout(runtime, result)
    assert "CANARY_READ_ERROR=FileNotFoundError" in stdout


@pytest.mark.requires_bwrap
@pytest.mark.sandbox_conformance
def test_task_private_is_absent(tmp_path: Path):
    """Task private/ provenance paths do not exist inside the sandbox at all (goal.md §5.3)."""
    require_usable_sandbox()
    runtime = _init_runtime(tmp_path)
    private_target = runtime.project_root / "work" / "tasks" / "TASK0001" / "private" / "task_provenance.json"

    engine = _engine(
        "fake-private-absent",
        modes=("canary_probe", "valid"),
        worker_config={"canary_read_path": str(private_target)},
    )
    result = _run_task(runtime, engine)

    assert_sandbox_result_valid(runtime, result)
    stdout = _get_stdout(runtime, result)
    assert "CANARY_READ_ERROR=FileNotFoundError" in stdout


@pytest.mark.requires_bwrap
@pytest.mark.sandbox_conformance
def test_descendants_die_with_sandbox(tmp_path: Path):
    """Worker-spawned descendants do not survive the confined attempt (goal.md §5.1).

    Under the TEST_ONLY backend this worker behavior fails with
    ``background_processes_survived_parent`` (see test_subprocess_quiescence.py);
    inside the PID-namespace sandbox the descendants die with the namespace
    init and the attempt stays valid and quiescent.
    """
    require_usable_sandbox()
    engine = _engine(
        "fake-descendants",
        modes=("valid", "spawn_multiple_children_then_exit_zero"),
        worker_config={"timeout_sleep_seconds": 60},
    )
    runtime = _init_runtime(tmp_path)
    result = _run_task(runtime, engine)

    assert_sandbox_result_valid(runtime, result)
    record = result.attempt_records[0]
    assert record.accepted_attempt is True
    agent_result = _get_agent_result(runtime, result)
    quiescence = agent_result.execution_report.quiescence
    assert quiescence is not None
    assert quiescence.quiescent is True
    assert quiescence.descendants_remaining == 0


@pytest.mark.sandbox_conformance
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

    probe = fabricated_probe()
    report = fabricated_passing_report(probe)
    current_fp = current_fingerprint_for(probe)

    valid_qualification = issue_qualification(report, current_fp)
    assert valid_qualification.qualified is True
    assert valid_qualification.conformance_report_hash == report.report_hash
    assert set(valid_qualification.required_cases_passed) == set(REQUIRED_CONFORMANCE_TESTS)

    class DummyOSBackend:
        confinement_level = ConfinementLevel.OS_SANDBOX

    # 1. Matching qualification succeeds
    require_real_engine_qualification(
        DummyOSBackend(), valid_qualification, current_fp,
        current_probe=probe, report=report,
    )

    # 2. TEST_ONLY backend fails closed
    class DummyTestBackend:
        confinement_level = ConfinementLevel.TEST_ONLY

    with pytest.raises(RuntimeError, match="does not allow real engine"):
        require_real_engine_qualification(
            DummyTestBackend(), valid_qualification, current_fp,
            current_probe=probe, report=report,
        )

    # 3. Not qualified fails closed
    unqualified = valid_qualification.model_copy(update={"qualified": False})
    with pytest.raises(RuntimeError, match="marked not qualified"):
        require_real_engine_qualification(
            DummyOSBackend(), unqualified, current_fp,
            current_probe=probe, report=report,
        )

    # 4. Unusable current probe fails closed
    unusable_probe = fabricated_probe(status=SandboxProbeStatus.UNAVAILABLE)
    with pytest.raises(RuntimeError, match="current sandbox probe is not usable"):
        require_real_engine_qualification(
            DummyOSBackend(), valid_qualification, current_fp,
            current_probe=unusable_probe, report=report,
        )

    # 5. Fingerprint mismatch fails closed
    tampered_fp = current_fp.model_copy(
        update={"confinement_code_fingerprint": "sha256:" + "0" * 64}
    )
    with pytest.raises(RuntimeError, match="confinement code fingerprint mismatch"):
        require_real_engine_qualification(
            DummyOSBackend(), valid_qualification, tampered_fp,
            current_probe=probe, report=report,
        )

    # 6. Missing required test fails closed
    incomplete = valid_qualification.model_copy(
        update={"required_cases_passed": ("test_canary_read_denied",)}
    )
    with pytest.raises(RuntimeError, match="missing required conformance tests"):
        require_real_engine_qualification(
            DummyOSBackend(), incomplete, current_fp,
            current_probe=probe, report=report,
        )


@pytest.mark.sandbox_conformance
def test_temporary_workspace_backend_remains_test_only():
    """TemporaryWorkspaceBackend is frozen as TEST_ONLY and cannot run real engines (goal.md §8.1)."""
    assert not allows_real_engine(ConfinementLevel.TEST_ONLY)
    from vibereview.runtime.execution import TemporaryWorkspaceBackend
    assert TemporaryWorkspaceBackend.confinement_level == ConfinementLevel.TEST_ONLY
