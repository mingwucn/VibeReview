"""Two-point qualification ordering and fail-closed live-runner tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vibereview.runtime import (
    AttemptOutcome,
    BubblewrapExecutionBackend,
    GenerateCandidateClaimsInvocation,
    EnvironmentFileCredentialProvider,
    LauncherConfiguration,
    MockEngine,
    NetworkPolicy,
    ProjectRuntime,
    RuntimeConfig,
    SubprocessEngine,
    StoredSandboxQualificationGate,
    SyntheticCredentialProvider,
    TaskType,
    TemporaryWorkspaceBackend,
)
from vibereview.runtime import (
    credential_exec_shim,
    execution as execution_module,
    trusted_launcher,
)
from vibereview.runtime.confinement import ConfinementLevel
from vibereview.runtime.hashing import hash_file
from vibereview.runtime.live_execution import (
    QualificationCheckpoint,
    QualificationEvidence,
    QualificationGateError,
)

WORKER = Path(__file__).resolve().parent.parent / "helpers" / "fake_agent.py"
PROPOSAL = {
    "themes": [{
        "local_ref": "theme_1", "title": "Gate", "description": "Gate test",
        "origin": "generated", "parent_ref": None,
    }],
    "claims": [{
        "local_ref": "claim_1", "theme_ref": "theme_1",
        "candidate_claim": "The qualification gate is checked twice.",
        "origin": "generated", "origin_refs": [],
    }],
}


class _OSBackend:
    confinement_level = ConfinementLevel.OS_SANDBOX
    network_policy = NetworkPolicy.HOST

    def __init__(self) -> None:
        self._delegate = TemporaryWorkspaceBackend(
            LauncherConfiguration(
                worker_script=WORKER,
                proposal_payloads={"valid_proposal.json": json.dumps(PROPOSAL).encode()},
            )
        )

    def prepare(self, task, policy, credentials=None):
        return self._delegate.prepare(task, policy, credentials)

    def quiesce(self, session, process, *, grace_seconds):
        return self._delegate.quiesce(session, process, grace_seconds=grace_seconds)

    def wrap_command(self, session, command):
        return self._delegate.wrap_command(session, command)


class _Gate:
    def __init__(self, events: list[str], fail_second: bool = False) -> None:
        self.events = events
        self.fail_second = fail_second

    def safe_configuration(self):
        return {"gate": "test-double"}

    def check(self, checkpoint):
        self.events.append(checkpoint.value)
        if self.fail_second and checkpoint is QualificationCheckpoint.PRE_LAUNCH:
            raise QualificationGateError(checkpoint, "qualification drift")
        return QualificationEvidence(
            checkpoint=checkpoint,
            storage_fingerprint="sha256:" + "1" * 64,
            report_hash="sha256:" + "2" * 64,
            network_policy=NetworkPolicy.HOST,
            backend_executable_identity="/stub/bin/bwrap",
            backend_executable_hash="sha256:" + "3" * 64,
        )


class _TrackedProvider(SyntheticCredentialProvider):
    def __init__(self, events: list[str]) -> None:
        super().__init__(provider_id="tracked", file_credentials={"credential": "secret-value"})
        self.events = events
        self.paths: list[Path] = []

    def prepare(self, engine_name, credentials_dir):
        self.events.append("credential_prepare")
        lease = super().prepare(engine_name, credentials_dir)
        self.paths.extend(lease._staged_files)
        original_enter = lease.__class__.__enter__
        original_exit = lease.__class__.__exit__
        events = self.events

        class WrappedLease(lease.__class__):
            def __enter__(self):
                events.append("credential_enter")
                return original_enter(self)

            def __exit__(self, exc_type, exc, traceback):
                events.append("credential_exit")
                return original_exit(self, exc_type, exc, traceback)

        lease.__class__ = WrappedLease
        return lease


def _engine(events: list[str], *, fail_second: bool = False) -> tuple[SubprocessEngine, _TrackedProvider]:
    provider = _TrackedProvider(events)
    return SubprocessEngine(
        worker_script=WORKER,
        modes=("valid",),
        proposal_payloads={"valid_proposal.json": json.dumps(PROPOSAL).encode()},
        credential_provider=provider,
        backend=_OSBackend(),
        compile_request=True,
        qualification_gate=_Gate(events, fail_second),
        _test_only_qualification_semantics=True,
        name="live-test",
    ), provider


def test_live_gate_runs_before_credentials_and_immediately_before_popen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    engine, _ = _engine(events)
    real_popen = execution_module.subprocess.Popen

    def tracked_popen(*args, **kwargs):
        events.append("popen")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(execution_module.subprocess, "Popen", tracked_popen)
    runtime = ProjectRuntime.create(tmp_path / "project", project_name="gate-order")
    result = runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        GenerateCandidateClaimsInvocation(topic="gate", existing_theme_ids=[]),
        engines=[engine],
    )
    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    assert events == [
        "pre_credentials", "credential_prepare", "credential_enter",
        "pre_launch", "popen", "credential_exit",
    ]


def test_prelaunch_qualification_drift_is_internal_no_fallback_and_cleans_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    engine, provider = _engine(events, fail_second=True)
    popen_called = False

    def forbidden_popen(*args, **kwargs):
        nonlocal popen_called
        popen_called = True
        raise AssertionError("Popen must not run after qualification drift")

    monkeypatch.setattr(execution_module.subprocess, "Popen", forbidden_popen)
    fallback = MockEngine([], name="must-not-run")
    runtime = ProjectRuntime.create(tmp_path / "project", project_name="gate-drift")
    result = runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        GenerateCandidateClaimsInvocation(topic="gate", existing_theme_ids=[]),
        engines=[engine, fallback],
    )
    assert result.outcome is AttemptOutcome.INTERNAL_RUNTIME_FAILURE
    assert len(result.attempt_records) == 1
    assert any(
        failure.code == "qualification_pre_launch_failed"
        for failure in result.attempt_records[0].detected_failures
    )
    assert fallback.calls == 0
    assert popen_called is False
    assert events == [
        "pre_credentials", "credential_prepare", "credential_enter",
        "pre_launch", "credential_exit",
    ]
    assert provider.paths and all(not path.exists() for path in provider.paths)


def test_nonfallback_qualification_failure_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    engine, _ = _engine(events, fail_second=True)
    monkeypatch.setattr(
        execution_module.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Popen must not run after qualification drift")
        ),
    )
    fallback = MockEngine([], name="must-not-run")
    runtime = ProjectRuntime.create(
        tmp_path / "project",
        project_name="no-internal-retry",
        config=RuntimeConfig(technical_attempts_per_engine=2),
    )
    result = runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        GenerateCandidateClaimsInvocation(topic="gate", existing_theme_ids=[]),
        engines=[engine, fallback],
    )
    assert result.outcome is AttemptOutcome.INTERNAL_RUNTIME_FAILURE
    assert len(result.attempt_records) == 1
    assert events.count("credential_enter") == 1
    assert fallback.calls == 0


def test_live_engine_constructor_rejects_missing_gate_compiler_or_os_backend() -> None:
    provider = SyntheticCredentialProvider(file_credentials={"credential": "secret"})
    with pytest.raises(ValueError, match="explicit live factory"):
        SubprocessEngine(worker_script=WORKER, credential_provider=provider, live=True)
    with pytest.raises(ValueError, match="explicit live factory"):
        SubprocessEngine(
            worker_script=WORKER, credential_provider=provider, backend=_OSBackend(),
            qualification_gate=_Gate([]), live=True,
        )


def test_private_factory_token_still_rejects_fake_os_backend_and_custom_gate(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    launcher = LauncherConfiguration(
        worker_script=WORKER,
        worker_script_sha256=hash_file(WORKER),
        trusted_launcher_sha256=hash_file(Path(trusted_launcher.__file__)),
        credential_exec_shim_sha256=hash_file(
            Path(credential_exec_shim.__file__)
        ),
    )
    provider = EnvironmentFileCredentialProvider(
        env_var="VIBEREVIEW_TEST_KEY", provider_id="test"
    )
    with pytest.raises(ValueError, match="qualified Bubblewrap backend"):
        SubprocessEngine(
            launcher_configuration=launcher,
            credential_provider=provider,
            backend=_OSBackend(),
            compile_request=True,
            qualification_gate=_Gate([]),
            live=True,
            _live_factory_token=execution_module._LIVE_FACTORY_TOKEN,
            authorized_project_root=project,
        )


def test_live_constructor_rejects_legacy_secret_env_credential_provider(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    launcher = LauncherConfiguration(
        worker_script=WORKER,
        worker_script_sha256=hash_file(WORKER),
        trusted_launcher_sha256=hash_file(Path(trusted_launcher.__file__)),
        credential_exec_shim_sha256=hash_file(
            Path(credential_exec_shim.__file__)
        ),
    )
    backend = BubblewrapExecutionBackend(
        launcher, bwrap_path=Path("/bin/true"), network_policy=NetworkPolicy.HOST
    )
    gate = StoredSandboxQualificationGate(
        backend, network_policy=NetworkPolicy.HOST,
        qualification_root=tmp_path / "qualification",
    )
    provider = SyntheticCredentialProvider(
        provider_id="legacy", secret_env={"API_KEY": "secret"}
    )
    with pytest.raises(ValueError, match="fixed-file credential provider"):
        SubprocessEngine(
            launcher_configuration=launcher,
            credential_provider=provider,
            backend=backend,
            compile_request=True,
            qualification_gate=gate,
            live=True,
            _live_factory_token=execution_module._LIVE_FACTORY_TOKEN,
            authorized_project_root=project,
        )
