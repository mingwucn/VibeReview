"""r5e sandbox capability probe contract tests (goal.md §4.6).

These tests are deterministic and host-independent: missing or denied
Bubblewrap hosts are simulated with fake ``bwrap`` executables on a temporary
path that emit the known failure messages and exit nonzero; real security
settings are never touched. None of them require the real bwrap, so they run
in ordinary deterministic CI without the ``requires_bwrap`` marker.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

import helpers.sandbox_gate as sandbox_gate
from helpers.sandbox_assert import assert_sandbox_result_valid
from vibereview.runtime import (
    AttemptOutcome,
    GenerateCandidateClaimsInvocation,
    ProjectRuntime,
    SandboxFailureCode,
    SandboxProbeStatus,
    SubprocessEngine,
    TaskType,
    compute_confinement_code_fingerprint,
    probe_platform_capabilities,
    probe_sandbox_capabilities,
)
from vibereview.runtime import confinement as confinement_module

WORKER = Path(__file__).resolve().parent.parent / "helpers" / "fake_agent.py"

_FAKE_USERNS_DENIED = """#!/bin/sh
if [ "$1" = "--version" ]; then
    echo "bubblewrap 0.11.1"
    exit 0
fi
echo "bwrap: setting up uid map: Permission denied" >&2
exit 1
"""

_FAKE_NETWORK_DENIED = """#!/bin/sh
if [ "$1" = "--version" ]; then
    echo "bubblewrap 0.11.1"
    exit 0
fi
case " $* " in
    *" --unshare-net "*)
        echo "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted" >&2
        exit 1
        ;;
esac
exit 0
"""

_FAKE_PROFILE_FAILED = """#!/bin/sh
if [ "$1" = "--version" ]; then
    echo "bubblewrap 0.11.1"
    exit 0
fi
case " $* " in
    *" /work/bundle "*)
        echo "bwrap: bind mount unexpectedly failed" >&2
        exit 1
        ;;
esac
exit 0
"""

_FAKE_ENV_DUMP = """#!/bin/sh
if [ "$1" = "--version" ]; then
    echo "bubblewrap 0.11.1"
    exit 0
fi
env >&2
echo "bwrap: setting up uid map: Permission denied" >&2
exit 1
"""


def _write_fake_bwrap(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "bwrap"
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)
    return script


def test_platform_fingerprint_probe_uses_fixed_nonsecret_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "must-not-reach-platform-probe"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(confinement_module.subprocess, "run", fake_run)
    probe_platform_capabilities(Path("/bin/true"))
    environment = captured["env"]
    assert environment == {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
    }
    assert secret not in repr(captured)


def test_confinement_fingerprint_covers_all_acceptance_enforcement_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_fingerprint(paths, *, contract_version):
        captured["names"] = {Path(path).name for path in paths}
        captured["contract"] = contract_version
        return "sha256:" + "1" * 64

    monkeypatch.setattr(confinement_module, "code_fingerprint", fake_fingerprint)
    assert compute_confinement_code_fingerprint() == "sha256:" + "1" * 64
    assert {
        "confinement.py", "execution.py", "credentials.py", "locking.py",
        "trusted_launcher.py", "credential_exec_shim.py", "output_policy.py",
        "resource_limits.py", "qualification_store.py", "request_compiler.py",
        "external_worker.py", "live_execution.py", "subprocess.py", "kernel.py",
        "records.py", "conformance.py", "conformance_worker.py", "sandbox.py",
    }.issubset(captured["names"])
    assert captured["contract"] == "1.8"


@pytest.fixture
def undetermined_kernel_settings(monkeypatch: pytest.MonkeyPatch):
    """Pin sysctl reads to 'undeterminable' so classification is host-independent."""

    monkeypatch.setattr(
        confinement_module, "_read_kernel_setting", lambda path: None
    )


def test_bwrap_binary_absent_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A missing bwrap executable is UNAVAILABLE, never BLOCKED or USABLE (goal.md §4.2)."""
    probe = probe_sandbox_capabilities(tmp_path / "no-such-bwrap")
    assert probe.status is SandboxProbeStatus.UNAVAILABLE
    assert probe.failure_code is SandboxFailureCode.EXECUTABLE_NOT_FOUND
    assert probe.executable_hash is None
    assert probe.backend_version is None
    assert probe.commands == ()

    # Default PATH lookup with no bwrap on PATH is equally UNAVAILABLE.
    monkeypatch.setenv("PATH", str(tmp_path))
    path_probe = probe_sandbox_capabilities()
    assert path_probe.status is SandboxProbeStatus.UNAVAILABLE
    assert path_probe.failure_code is SandboxFailureCode.EXECUTABLE_NOT_FOUND
    assert path_probe.executable_path is None


def test_bwrap_binary_present_but_userns_denied_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    undetermined_kernel_settings,
):
    """A present binary failing the user-namespace probe is BLOCKED (goal.md §4.2-4.3)."""
    fake = _write_fake_bwrap(tmp_path, _FAKE_USERNS_DENIED)
    probe = probe_sandbox_capabilities(fake)
    assert probe.status is SandboxProbeStatus.BLOCKED
    assert probe.failure_code is SandboxFailureCode.USER_NAMESPACE_DENIED
    assert probe.backend_version == "0.11.1"
    assert probe.executable_hash is not None
    assert probe.executable_hash.startswith("sha256:")
    assert [command.name for command in probe.commands] == [
        "version",
        "user_mount_namespace",
    ]
    denied = probe.commands[-1]
    assert denied.exit_code == 1
    assert "setting up uid map" in denied.stderr
    assert probe.diagnostic is not None
    assert "user_mount_namespace" in probe.diagnostic

    # The same denial plus the AppArmor restricted-userns sysctl classifies
    # as the AppArmor restriction (goal.md §4.3).
    monkeypatch.setattr(
        confinement_module, "_read_kernel_setting", lambda path: "1"
    )
    apparmor_probe = probe_sandbox_capabilities(fake)
    assert apparmor_probe.status is SandboxProbeStatus.BLOCKED
    assert apparmor_probe.failure_code is SandboxFailureCode.APPARMOR_USERNS_RESTRICTION
    assert apparmor_probe.apparmor_restrict_unprivileged_userns == "1"


def test_network_namespace_denial_is_classified(
    tmp_path: Path, undetermined_kernel_settings
):
    """The DENY profile requires the network namespace; denial is classified (goal.md §4.2-4.3)."""
    fake = _write_fake_bwrap(tmp_path, _FAKE_NETWORK_DENIED)
    probe = probe_sandbox_capabilities(fake)
    assert probe.status is SandboxProbeStatus.BLOCKED
    assert probe.failure_code is SandboxFailureCode.NETWORK_NAMESPACE_DENIED
    assert [command.name for command in probe.commands] == [
        "version",
        "user_mount_namespace",
        "pid_namespace",
        "ipc_namespace",
        "uts_namespace",
        "cgroup_namespace",
        "network_namespace",
    ]
    network_command = probe.commands[-1]
    assert network_command.exit_code == 1
    assert "loopback: Failed RTM_NEWADDR" in network_command.stderr
    assert "--unshare-net" in network_command.argv


def test_full_profile_probe_required_for_usable(
    tmp_path: Path, undetermined_kernel_settings
):
    """Only the complete VibeReview profile probe can yield USABLE (goal.md §4.2)."""
    fake = _write_fake_bwrap(tmp_path, _FAKE_PROFILE_FAILED)
    probe = probe_sandbox_capabilities(fake)
    assert probe.status is not SandboxProbeStatus.USABLE
    assert probe.status is SandboxProbeStatus.BLOCKED
    assert probe.failure_code is SandboxFailureCode.PROFILE_EXECUTION_FAILED
    assert [command.name for command in probe.commands] == [
        "version",
        "user_mount_namespace",
        "pid_namespace",
        "ipc_namespace",
        "uts_namespace",
        "cgroup_namespace",
        "network_namespace",
        "full_profile",
    ]
    full_profile = probe.commands[-1]
    assert full_profile.exit_code == 1
    # The probe shares the production profile argv construction: the explicit
    # namespace flags (goal.md §5.1) and the /work tree, never --unshare-all.
    assert "--unshare-all" not in full_profile.argv
    for flag in (
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--unshare-net",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
    ):
        assert flag in full_profile.argv
    assert "/work/bundle" in full_profile.argv


def test_blocked_probe_cannot_create_qualification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    undetermined_kernel_settings,
):
    """The positive-test gate refuses to treat BLOCKED/UNAVAILABLE as usable (goal.md §4.5)."""
    fake = _write_fake_bwrap(tmp_path, _FAKE_USERNS_DENIED)
    probe = probe_sandbox_capabilities(fake)
    assert probe.status is not SandboxProbeStatus.USABLE

    monkeypatch.setattr(sandbox_gate, "_PROBE_CACHE", probe)
    with pytest.raises(pytest.fail.Exception):
        sandbox_gate.require_usable_sandbox()

    unavailable = probe_sandbox_capabilities(tmp_path / "no-such-bwrap")
    monkeypatch.setattr(sandbox_gate, "_PROBE_CACHE", unavailable)
    with pytest.raises(pytest.skip.Exception):
        sandbox_gate.require_usable_sandbox()


def test_conformance_failure_prints_stderr_and_detected_failures(tmp_path: Path):
    """A failed conformance run reports argv, exit code, stderr and failures (goal.md §4.4)."""
    runtime = ProjectRuntime.create(tmp_path / "project", project_name="probe-diag")
    engine = SubprocessEngine(
        name="probe-diag-failing",
        worker_script=WORKER,
        modes=("large_stderr", "nonzero"),
        worker_config={"stream_bytes": 512},
    )
    result = runtime.run(
        TaskType.GENERATE_CANDIDATE_CLAIMS,
        GenerateCandidateClaimsInvocation(topic="diagnostics", existing_theme_ids=[]),
        engines=[engine],
    )
    assert result.outcome is AttemptOutcome.ENGINE_EXECUTION_FAILURE

    with pytest.raises(pytest.fail.Exception) as excinfo:
        assert_sandbox_result_valid(runtime, result)
    message = str(excinfo.value)
    assert "engine_execution_failure" in message
    assert "process_nonzero_exit" in message
    assert "vibereview-fake-stderr" in message
    assert '"argv"' in message
    assert '"exit_code"' in message
    assert '"applied_limits"' in message
    assert '"quiescence"' in message


def test_probe_report_contains_no_project_private_paths(
    tmp_path: Path, undetermined_kernel_settings
):
    """The persistable probe report never mentions the review project (goal.md §4.1)."""
    runtime = ProjectRuntime.create(tmp_path / "project", project_name="probe-privacy")
    fake = _write_fake_bwrap(tmp_path, _FAKE_PROFILE_FAILED)
    probe = probe_sandbox_capabilities(fake)
    report_text = json.dumps(probe.model_dump(mode="json"), ensure_ascii=False)
    assert str(runtime.project_root) not in report_text
    # Probe commands ran in their own temporary tree under the system temp
    # dir, outside any review project.
    assert str(Path(tempfile.gettempdir()).resolve()) in report_text


def test_probe_report_contains_no_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    undetermined_kernel_settings,
):
    """Probe commands run with a sanitized environment; no secrets leak (goal.md §4.1)."""
    secret = "r5e-probe-secret-token"
    monkeypatch.setenv("VIBEREVIEW_R5E_TEST_CREDENTIAL", secret)
    fake = _write_fake_bwrap(tmp_path, _FAKE_ENV_DUMP)
    probe = probe_sandbox_capabilities(fake)

    env_dump = probe.commands[-1].stderr
    assert "PATH=" in env_dump
    report_text = json.dumps(probe.model_dump(mode="json"), ensure_ascii=False)
    assert secret not in report_text
    assert "VIBEREVIEW_R5E_TEST_CREDENTIAL" not in report_text
