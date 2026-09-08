"""Explicit offline/live factory separation and fail-closed CLI status."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from vibereview.runtime import (
    AttemptOutcome,
    BubblewrapExecutionBackend,
    GenerateCandidateClaimsInvocation,
    LauncherConfiguration,
    MockEngine,
    NetworkPolicy,
    ProjectRuntime,
    RequestCompilationPolicy,
    RuntimeConfig,
    SubprocessEngine,
    TaskType,
)
from vibereview.runtime import credential_exec_shim, execution as execution_module
from vibereview.runtime.credentials import LockedJsonTokenCredentialProvider
from vibereview.runtime.external_engines import (
    AdapterAvailability,
    CliCapabilityProof,
    CliCredentialDelivery,
    CliProtocol,
    CliProvider,
    EngineUnavailableError,
    FivePaperComparisonPlan,
    LiveExecutionAuthorization,
    PilotEngineMatrix,
    bounded_live_policy,
    build_live_agy_engine,
    build_live_codex_engine,
    build_live_deepseek_engine,
    build_live_kimi_engine,
    build_offline_engine,
)
from vibereview.runtime.hashing import hash_file
from vibereview.runtime.live_execution import (
    QualificationCheckpoint,
    QualificationEvidence,
)
from vibereview.runtime import external_worker, trusted_launcher
from helpers.sandbox_gate import require_usable_sandbox


PROPOSAL = {
    "themes": [{
        "local_ref": "theme_1", "title": "Offline", "description": "fixture",
        "origin": "generated", "parent_ref": None,
    }],
    "claims": [{
        "local_ref": "claim_1", "theme_ref": "theme_1",
        "candidate_claim": "Offline compilation is complete.",
        "origin": "generated", "origin_refs": [],
    }],
}


def _raw_offline_launcher(raw: bytes) -> LauncherConfiguration:
    worker = Path(external_worker.__file__).resolve()
    return LauncherConfiguration(
        worker_script=worker,
        worker_script_sha256=hash_file(worker),
        trusted_launcher_sha256=hash_file(Path(trusted_launcher.__file__)),
        credential_exec_shim_sha256=hash_file(
            Path(credential_exec_shim.__file__)
        ),
        worker_config={
            "mode": "offline",
            "fixture_path": "launcher/offline_proposal.json",
            "max_transport_bytes": 524_288,
        },
        proposal_payloads={"offline_proposal.json": raw},
    )


def _authorization() -> LiveExecutionAuthorization:
    return LiveExecutionAuthorization(
        acknowledgement="LIVE_EXTERNAL_EXECUTION_AUTHORIZED"
    )


def _proof(tmp_path: Path, provider: CliProvider, protocol: CliProtocol) -> CliCapabilityProof:
    executable = tmp_path / provider.value
    executable.write_text("stub", encoding="utf-8")
    return CliCapabilityProof(
        provider=provider,
        protocol=protocol,
        executable_path=executable.resolve(),
        executable_sha256="sha256:" + "1" * 64,
        version="test",
        help_sha256="sha256:" + "2" * 64,
        noninteractive_verified=True,
        structured_output_verified=True,
        credential_delivery=CliCredentialDelivery.FIXED_ENV,
        tool_secret_canary_passed=True,
    )


def _credentials(tmp_path: Path, env: str) -> LockedJsonTokenCredentialProvider:
    vault = tmp_path / "vault.json"
    vault.write_text(json.dumps({"token": "secret-token"}), encoding="utf-8")
    return LockedJsonTokenCredentialProvider(
        provider_id="test-vault",
        vault_path=vault,
        token_field_path=("token",),
        credential_env_name=env,
        vault_id="dedicated-test-profile",
    )


def test_offline_factory_runs_complete_compiler_without_live_gate(tmp_path: Path) -> None:
    runtime = ProjectRuntime.create(tmp_path / "project", project_name="offline")
    result = runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        GenerateCandidateClaimsInvocation(topic="offline", existing_theme_ids=[]),
        engines=[build_offline_engine(PROPOSAL)],
    )
    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    attempt = runtime.project_root / result.attempt_records[0].agent_result_path
    payload = json.loads(attempt.read_text(encoding="utf-8"))
    report = payload["execution_report"]
    assert report["compiled_request_digest"].startswith("sha256:")
    assert report["qualification_checks"] == []


@pytest.mark.parametrize("raw", [b"not literal json\n", b""])
def test_external_worker_preserves_invalid_or_empty_output_for_parent_format_classification(
    tmp_path: Path, raw: bytes
) -> None:
    launcher = _raw_offline_launcher(raw)
    engine = SubprocessEngine(
        launcher_configuration=launcher,
        modes=("offline",),
        policy=bounded_live_policy(timeout_seconds=30),
        compile_request=True,
        name="literal-offline",
    )
    runtime = ProjectRuntime.create(tmp_path / "project", project_name="literal-output")
    result = runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        GenerateCandidateClaimsInvocation(topic="offline", existing_theme_ids=[]),
        engines=[engine],
    )
    assert result.outcome is AttemptOutcome.ENGINE_FORMAT_FAILURE
    attempt = runtime.project_root / result.attempt_records[0].agent_result_path
    worker_report = json.loads(attempt.read_text(encoding="utf-8"))["execution_report"]
    assert worker_report["exit_code"] == 0
    assert worker_report["primary_outcome"] == (
        "valid_scientific_result" if raw else "engine_format_failure"
    )


@pytest.mark.requires_bwrap
@pytest.mark.sandbox_conformance
def test_offline_external_worker_runs_through_real_usable_bubblewrap(tmp_path: Path) -> None:
    require_usable_sandbox()
    launcher = _raw_offline_launcher(json.dumps(PROPOSAL).encode("utf-8"))
    backend = BubblewrapExecutionBackend(launcher, network_policy=NetworkPolicy.DENY)
    engine = SubprocessEngine(
        launcher_configuration=launcher,
        modes=("offline",),
        policy=bounded_live_policy(timeout_seconds=30),
        backend=backend,
        compile_request=True,
        name="offline-bubblewrap",
    )
    runtime = ProjectRuntime.create(tmp_path / "project", project_name="offline-bwrap")
    result = runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        GenerateCandidateClaimsInvocation(topic="offline", existing_theme_ids=[]),
        engines=[engine],
    )
    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
    attempt = runtime.project_root / result.attempt_records[0].agent_result_path
    worker_report = json.loads(attempt.read_text(encoding="utf-8"))["execution_report"]
    assert worker_report["confinement_level"] == "os_sandbox"


def test_deepseek_factory_constructs_without_preflight_or_provider_call(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    engine = build_live_deepseek_engine(
        authorization=_authorization(),
        project_root=project,
        model="deepseek-v4-flash",
        qualification_root=tmp_path / "qualification",
        bwrap_path=tmp_path / "not-present-yet",
    )
    safe = engine.safe_configuration()
    assert engine.name == "deepseek-rest"
    assert safe["live"] is True
    assert safe["compile_request"] is True
    assert safe["worker_config"]["endpoint"] == "https://api.deepseek.com/chat/completions"
    assert safe["worker_config"]["model"] == "deepseek-v4-flash"
    assert safe["worker_config"]["max_tokens"] == 8_192
    assert safe["worker_script_sha256"] == hash_file(Path(external_worker.__file__))
    assert safe["trusted_launcher_sha256"] == hash_file(Path(trusted_launcher.__file__))
    assert safe["credential_exec_shim_sha256"] == hash_file(
        Path(credential_exec_shim.__file__)
    )
    assert safe["authorized_project_root_id"].startswith("sha256:")
    assert str(project) not in json.dumps(safe)


def test_deepseek_factory_rejects_project_owned_qualification_store(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(EngineUnavailableError, match="outside the review project"):
        build_live_deepseek_engine(
            authorization=_authorization(),
            project_root=project,
            model="deepseek-v4-flash",
            qualification_root=project / "qualifications",
        )


def test_deepseek_factory_requires_real_authorization_and_existing_project(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="authorization"):
        build_live_deepseek_engine(
            authorization=None,  # type: ignore[arg-type]
            project_root=tmp_path / "project",
            model="deepseek-v4-flash",
            qualification_root=tmp_path / "qualification",
        )
    with pytest.raises(ValueError, match="existing directory"):
        build_live_deepseek_engine(
            authorization=_authorization(),
            project_root=tmp_path / "project",
            model="deepseek-v4-flash",
            qualification_root=tmp_path / "qualification",
        )


def test_deepseek_factory_rejects_credential_source_substitution(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(ValueError, match="requires DEEPSEEK_API_KEY"):
        build_live_deepseek_engine(
            authorization=_authorization(), project_root=project,
            model="deepseek-v4-flash", api_key_env="AWS_SECRET_ACCESS_KEY",
            qualification_root=tmp_path / "qualification",
        )


@pytest.mark.parametrize("model", ["deepseek-chat", "deepseek-reasoner", "unknown"])
def test_deepseek_factory_rejects_retired_or_unverified_models(
    tmp_path: Path, model: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(ValueError, match="verified allowlist"):
        build_live_deepseek_engine(
            authorization=_authorization(), project_root=project, model=model,
            qualification_root=tmp_path / "qualification",
        )


@pytest.mark.parametrize(
    ("builder", "provider", "protocol", "env_name", "message"),
    [
        (build_live_kimi_engine, CliProvider.KIMI, CliProtocol.KIMI_ACP,
         "KIMI_MODEL_API_KEY", "Kimi CLI ACP is unavailable"),
        (build_live_agy_engine, CliProvider.AGY, CliProtocol.AGY_PRINT_JSON,
         "CODEX_API_KEY", "Agy CLI is unavailable"),
        (build_live_codex_engine, CliProvider.CODEX, CliProtocol.CODEX_EXEC_JSON,
         "CODEX_API_KEY", "Codex CLI is unavailable"),
    ],
)
def test_cli_live_factories_are_explicitly_unavailable(
    tmp_path: Path, builder, provider, protocol, env_name: str, message: str
) -> None:
    provider_root = tmp_path / provider.value
    provider_root.mkdir(exist_ok=True)
    (tmp_path / "project").mkdir(exist_ok=True)
    kwargs = dict(
        authorization=_authorization(),
        project_root=tmp_path / "project",
        proof=_proof(provider_root, provider, protocol),
        credential_provider=_credentials(provider_root, env_name),
    )
    if provider in {CliProvider.KIMI, CliProvider.CODEX}:
        kwargs["model"] = "explicit-test-model"
    with pytest.raises(EngineUnavailableError, match=message):
        builder(**kwargs)


def test_matrix_labels_every_unqualified_cli_arm_unavailable(tmp_path: Path) -> None:
    (tmp_path / "project").mkdir()
    deepseek = build_live_deepseek_engine(
        authorization=_authorization(),
        project_root=tmp_path / "project",
        model="deepseek-v4-flash",
        qualification_root=tmp_path / "qualification",
    )
    matrix = PilotEngineMatrix(deepseek_primary=deepseek)
    assert matrix.operational == (deepseek,)
    assert matrix.kimi_fallback is AdapterAvailability.UNAVAILABLE
    plan = FivePaperComparisonPlan(
        paper_keys=("P1", "P2", "P3", "P4", "P5"), engines=matrix
    )
    with pytest.raises(EngineUnavailableError, match="comparison is unavailable"):
        plan.independent_runs()


_LIVE_POLICY_FIELDS = (
    "timeout_seconds", "terminate_grace_seconds", "max_stdout_bytes",
    "max_stderr_bytes", "max_proposal_bytes", "max_writable_tree_bytes",
    "max_writable_entries", "max_writable_single_file_bytes",
    "max_writable_directory_depth", "max_open_files", "max_processes",
    "max_cpu_seconds", "max_address_space_bytes",
)


@pytest.mark.parametrize("field", _LIVE_POLICY_FIELDS)
def test_deepseek_factory_rejects_each_relaxed_live_policy_bound(
    tmp_path: Path, field: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    ceiling = bounded_live_policy()
    current = getattr(ceiling, field)
    assert current is not None
    relaxed = ceiling.model_copy(update={field: current + 1})
    with pytest.raises(ValueError, match=f"relaxes {field}"):
        build_live_deepseek_engine(
            authorization=_authorization(), project_root=project,
            model="deepseek-v4-flash", qualification_root=tmp_path / "qualification",
            policy=relaxed,
        )


def test_deepseek_factory_rejects_relaxed_policy_shape_and_accepts_stricter(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    ceiling = bounded_live_policy()
    for update, message in (
        ({"max_processes": None}, "max_processes"),
        ({"inherited_environment_allowlist": (*ceiling.inherited_environment_allowlist, "AWS_SECRET_ACCESS_KEY")}, "environment allowlist"),
        ({"allowed_output_files": ("proposal.json", "extra.json")}, "only proposal.json"),
        ({"writable_tree_scan_interval_seconds": 0.06}, "scan interval"),
    ):
        with pytest.raises(ValueError, match=message):
            build_live_deepseek_engine(
                authorization=_authorization(), project_root=project,
                model="deepseek-v4-flash", qualification_root=tmp_path / "qualification",
                policy=ceiling.model_copy(update=update),
            )
    engine = build_live_deepseek_engine(
        authorization=_authorization(), project_root=project,
        model="deepseek-v4-flash", qualification_root=tmp_path / "qualification",
        policy=ceiling.model_copy(update={"max_stdout_bytes": 32_768}),
        request_policy=RequestCompilationPolicy(
            max_total_source_bytes=262_144,
            max_compiled_request_bytes=262_144,
        ),
    )
    assert engine.safe_configuration()["policy"]["max_stdout_bytes"] == 32_768


@pytest.mark.parametrize("timeout_seconds", [1.0, 3.0, 5.0])
def test_deepseek_factory_rejects_timeout_without_transport_cleanup_margin(
    tmp_path: Path, timeout_seconds: float
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    policy = bounded_live_policy().model_copy(
        update={"timeout_seconds": timeout_seconds}
    )
    with pytest.raises(ValueError, match="exceed 5 seconds"):
        build_live_deepseek_engine(
            authorization=_authorization(),
            project_root=project,
            model="deepseek-v4-flash",
            qualification_root=tmp_path / "qualification",
            policy=policy,
        )


def test_live_policy_is_immutable_and_detached_from_caller(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    policy = bounded_live_policy()
    engine = build_live_deepseek_engine(
        authorization=_authorization(),
        project_root=project,
        model="deepseek-v4-flash",
        qualification_root=tmp_path / "qualification",
        policy=policy,
    )
    assert engine._policy is not policy
    with pytest.raises(ValidationError):
        policy.max_processes = None

    # Even deliberate low-level mutation of the caller's object cannot alter
    # the detached policy retained by the live engine.
    object.__setattr__(policy, "max_processes", None)
    object.__setattr__(policy, "timeout_seconds", 10_000.0)
    retained = engine.safe_configuration()["policy"]
    assert retained["max_processes"] == 32
    assert retained["timeout_seconds"] == 300.0


def test_launcher_nested_configuration_is_detached_and_safe_dump_is_a_copy() -> None:
    nested = {"mode": "test", "options": {"value": "original"}}
    launcher = LauncherConfiguration(worker_script=Path("fake.py"), worker_config=nested)
    engine = SubprocessEngine(launcher_configuration=launcher)

    launcher.worker_config["options"]["value"] = "caller mutation"
    retained = engine.safe_configuration()
    assert retained["worker_config"]["options"]["value"] == "original"
    assert engine._backend._launcher.worker_config["options"]["value"] == "original"

    retained["worker_config"]["options"]["value"] = "dump mutation"
    assert engine.safe_configuration()["worker_config"]["options"]["value"] == "original"


def test_mutated_launcher_payload_names_are_revalidated_before_snapshot() -> None:
    launcher = LauncherConfiguration(
        worker_script=Path("fake.py"),
        proposal_payloads={"valid.json": b"{}"},
    )
    launcher.proposal_payloads["../escape.json"] = b"{}"

    with pytest.raises(ValidationError, match="unsafe launcher payload"):
        SubprocessEngine(launcher_configuration=launcher)


@pytest.mark.parametrize("field", tuple(RequestCompilationPolicy.model_fields))
def test_deepseek_factory_rejects_each_relaxed_request_bound(
    tmp_path: Path, field: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    ceiling = RequestCompilationPolicy()
    relaxed = ceiling.model_copy(update={field: getattr(ceiling, field) + 1})
    with pytest.raises(ValueError, match=f"relaxes {field}"):
        build_live_deepseek_engine(
            authorization=_authorization(), project_root=project,
            model="deepseek-v4-flash", qualification_root=tmp_path / "qualification",
            request_policy=relaxed,
        )


def test_default_qualification_root_is_resolved_once_and_env_drift_cannot_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    root_a = tmp_path / "qualification-a"
    root_b = tmp_path / "qualification-b"
    monkeypatch.setenv("VIBEREVIEW_QUALIFICATION_DIR", str(root_a))
    engine = build_live_deepseek_engine(
        authorization=_authorization(), project_root=project,
        model="deepseek-v4-flash", bwrap_path=tmp_path / "missing-bwrap",
    )
    before = engine.safe_configuration()
    assert engine._qualification_gate._qualification_root == root_a.resolve()
    monkeypatch.setenv("VIBEREVIEW_QUALIFICATION_DIR", str(root_b))
    assert engine.safe_configuration() == before
    assert engine._qualification_gate._qualification_root == root_a.resolve()


def test_live_factory_is_bound_to_one_exact_project_before_backend_preflight(
    tmp_path: Path,
) -> None:
    authorized = ProjectRuntime.create(
        tmp_path / "authorized", project_name="authorized"
    )
    other = ProjectRuntime.create(tmp_path / "other", project_name="other")
    engine = build_live_deepseek_engine(
        authorization=_authorization(), project_root=authorized.project_root,
        model="deepseek-v4-flash", qualification_root=tmp_path / "qualification",
        bwrap_path=tmp_path / "missing-bwrap",
    )
    result = other.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        GenerateCandidateClaimsInvocation(topic="other", existing_theme_ids=[]),
        engines=[engine],
    )
    assert result.outcome is AttemptOutcome.INTERNAL_RUNTIME_FAILURE
    assert any(
        failure.code == "live_project_boundary_mismatch"
        for failure in result.attempt_records[0].detected_failures
    )


def test_trusted_core_script_hash_mismatch_is_internal_before_popen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = Path(external_worker.__file__).resolve()
    launcher = LauncherConfiguration(
        worker_script=worker,
        worker_script_sha256=hash_file(worker),
        trusted_launcher_sha256="sha256:" + "0" * 64,
        credential_exec_shim_sha256=hash_file(Path(credential_exec_shim.__file__)),
        worker_config={
            "mode": "offline", "fixture_path": "launcher/offline_proposal.json",
            "max_transport_bytes": 524_288,
        },
        proposal_payloads={"offline_proposal.json": json.dumps(PROPOSAL).encode()},
    )
    engine = SubprocessEngine(
        launcher_configuration=launcher, modes=("offline",), name="bad-core-hash"
    )
    monkeypatch.setattr(
        execution_module.subprocess, "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Popen must not run after trusted staging mismatch")
        ),
    )
    runtime = ProjectRuntime.create(tmp_path / "project", project_name="bad-core")
    result = runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        GenerateCandidateClaimsInvocation(topic="offline", existing_theme_ids=[]),
        engines=[engine],
    )
    assert result.outcome is AttemptOutcome.INTERNAL_RUNTIME_FAILURE
    assert any(
        failure.code == "trusted_launcher_staging_failed"
        for failure in result.attempt_records[0].detected_failures
    )


def test_live_prepare_failure_is_nonfallback_and_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = ProjectRuntime.create(
        tmp_path / "project", project_name="prepare-failure",
        config=RuntimeConfig(technical_attempts_per_engine=2),
    )
    engine = build_live_deepseek_engine(
        authorization=_authorization(), project_root=runtime.project_root,
        model="deepseek-v4-flash", qualification_root=tmp_path / "qualification",
        bwrap_path=tmp_path / "missing-bwrap",
    )
    fallback = MockEngine([], name="must-not-run")
    monkeypatch.setattr(
        execution_module.subprocess, "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Popen must not run after live prepare failure")
        ),
    )
    result = runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        GenerateCandidateClaimsInvocation(topic="live", existing_theme_ids=[]),
        engines=[engine, fallback],
    )
    assert result.outcome is AttemptOutcome.INTERNAL_RUNTIME_FAILURE
    assert len(result.attempt_records) == 1
    assert fallback.calls == 0
    assert any(
        failure.code == "live_backend_preparation_failed"
        for failure in result.attempt_records[0].detected_failures
    )


def test_network_policy_mutation_between_gate_and_wrap_fails_before_popen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = ProjectRuntime.create(tmp_path / "project", project_name="policy-drift")
    bwrap = Path("/bin/true").resolve()
    engine = build_live_deepseek_engine(
        authorization=_authorization(), project_root=runtime.project_root,
        model="deepseek-v4-flash", qualification_root=tmp_path / "qualification",
        bwrap_path=bwrap,
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret-token")

    def check(checkpoint: QualificationCheckpoint) -> QualificationEvidence:
        if checkpoint is QualificationCheckpoint.PRE_LAUNCH:
            engine._backend._network_policy = NetworkPolicy.DENY
        return QualificationEvidence(
            checkpoint=checkpoint,
            storage_fingerprint="sha256:" + "1" * 64,
            report_hash="sha256:" + "2" * 64,
            network_policy=NetworkPolicy.HOST,
            backend_executable_identity=str(bwrap),
            backend_executable_hash=hash_file(bwrap),
        )

    monkeypatch.setattr(engine._qualification_gate, "check", check)
    monkeypatch.setattr(
        execution_module.subprocess, "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Popen must not run after policy drift")
        ),
    )
    result = runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        GenerateCandidateClaimsInvocation(topic="policy", existing_theme_ids=[]),
        engines=[engine],
    )
    assert result.outcome is AttemptOutcome.INTERNAL_RUNTIME_FAILURE
    assert any(
        failure.code == "execution_runtime_internal_error"
        for failure in result.attempt_records[0].detected_failures
    )


def test_live_supervisor_popen_receives_only_fixed_nonsecret_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = ProjectRuntime.create(tmp_path / "project", project_name="clean-env")
    bwrap = Path("/bin/true").resolve()
    engine = build_live_deepseek_engine(
        authorization=_authorization(), project_root=runtime.project_root,
        model="deepseek-v4-flash", qualification_root=tmp_path / "qualification",
        bwrap_path=bwrap,
    )
    secrets = {
        "DEEPSEEK_API_KEY": "deepseek-secret",
        "CODEX_API_KEY": "codex-secret",
        "KIMI_MODEL_API_KEY": "kimi-secret",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", "/secret-bearing/path")
    monkeypatch.setenv("SSL_CERT_FILE", "/secret/certificate-path")

    def check(checkpoint: QualificationCheckpoint) -> QualificationEvidence:
        return QualificationEvidence(
            checkpoint=checkpoint,
            storage_fingerprint="sha256:" + "1" * 64,
            report_hash="sha256:" + "2" * 64,
            network_policy=NetworkPolicy.HOST,
            backend_executable_identity=str(bwrap),
            backend_executable_hash=hash_file(bwrap),
        )

    captured: dict[str, object] = {}

    def capture_popen(*args, **kwargs):
        captured.update(kwargs)
        captured["argv"] = args[0]
        raise OSError("deterministic no-launch stub")

    monkeypatch.setattr(engine._qualification_gate, "check", check)
    monkeypatch.setattr(execution_module.subprocess, "Popen", capture_popen)
    result = runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        GenerateCandidateClaimsInvocation(topic="clean", existing_theme_ids=[]),
        engines=[engine],
    )
    assert result.outcome is AttemptOutcome.ENGINE_EXECUTION_FAILURE
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert environment["LANG"] == environment["LC_ALL"] == "C.UTF-8"
    assert not set(secrets) & set(environment)
    assert "SSL_CERT_FILE" not in environment
    assert all(value not in repr(captured) for value in secrets.values())
