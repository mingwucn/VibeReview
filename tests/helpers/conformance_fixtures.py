"""Fabricated conformance fixtures for qualification/gate unit tests (goal.md §6).

These fixtures never ground a real qualification: they exist so deterministic
tests can exercise the qualification model, issuance checks, the real-engine
gate, and the qualification store without executing the sandboxed conformance
suite.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from vibereview.runtime import (
    CONFORMANCE_SUITE_VERSION,
    REQUIRED_CONFORMANCE_TESTS,
    AttemptOutcome,
    ConfinementLevel,
    NetworkPolicy,
    QualificationFingerprint,
    SandboxConformanceCaseResult,
    SandboxConformanceReport,
    SandboxProbeCommandResult,
    SandboxProbeResult,
    SandboxProbeStatus,
    compute_confinement_code_fingerprint,
    compute_conformance_report_hash,
    compute_profile_hash,
    probe_platform_capabilities,
)
from vibereview.runtime.confinement import (
    CGROUP_NAMESPACE_PROBE,
    FULL_PROFILE_PROBE,
    IPC_NAMESPACE_PROBE,
    NETWORK_NAMESPACE_PROBE,
    PID_NAMESPACE_PROBE,
    USER_MOUNT_NAMESPACE_PROBE,
    UTS_NAMESPACE_PROBE,
    VERSION_PROBE,
)

PROBE_STAGE_NAMES: tuple[str, ...] = (
    VERSION_PROBE,
    USER_MOUNT_NAMESPACE_PROBE,
    PID_NAMESPACE_PROBE,
    IPC_NAMESPACE_PROBE,
    UTS_NAMESPACE_PROBE,
    CGROUP_NAMESPACE_PROBE,
    NETWORK_NAMESPACE_PROBE,
    FULL_PROFILE_PROBE,
)

DEFAULT_EXECUTABLE_HASH = "sha256:" + "a" * 64


def fabricated_probe(
    *,
    executable_hash: str = DEFAULT_EXECUTABLE_HASH,
    status: SandboxProbeStatus = SandboxProbeStatus.USABLE,
    failing_stages: tuple[str, ...] = (),
) -> SandboxProbeResult:
    """A fully usable staged probe result with every namespace stage passing."""
    commands = tuple(
        SandboxProbeCommandResult(
            name=name,
            argv=("bwrap", name),
            exit_code=1 if name in failing_stages else 0,
            stdout="",
            stderr="",
            duration_seconds=0.01,
        )
        for name in PROBE_STAGE_NAMES
    )
    return SandboxProbeResult(
        backend_name="bubblewrap",
        backend_version="0.11.1",
        executable_path=Path("/usr/bin/bwrap"),
        executable_hash=executable_hash,
        status=status,
        failure_code=None,
        diagnostic=None,
        operating_system="linux",
        architecture="x86_64",
        kernel_release="6.1.0-test",
        wsl_detected=False,
        unprivileged_userns_clone="1",
        apparmor_restrict_unprivileged_userns="0",
        apparmor_profile_detected=False,
        commands=commands,
    )


def fabricated_passing_report(
    probe: SandboxProbeResult,
    *,
    network_policy: NetworkPolicy = NetworkPolicy.DENY,
    suite_version: str = CONFORMANCE_SUITE_VERSION,
) -> SandboxConformanceReport:
    """A passing conformance report over every required case, hash computed."""
    now = datetime.now(UTC).isoformat()
    cases = tuple(
        SandboxConformanceCaseResult(
            case_id=case_id,
            passed=True,
            started_at=now,
            finished_at=now,
            diagnostic=None,
            attempt_outcome=AttemptOutcome.VALID_SCIENTIFIC_RESULT,
            stdout_hash="sha256:" + "b" * 64,
            stderr_hash="sha256:" + "c" * 64,
        )
        for case_id in REQUIRED_CONFORMANCE_TESTS
    )
    placeholder = SandboxConformanceReport(
        suite_version=suite_version,
        backend_name="bubblewrap",
        confinement_level=ConfinementLevel.OS_SANDBOX,
        network_policy=network_policy,
        probe=probe,
        cases=cases,
        required_case_ids=REQUIRED_CONFORMANCE_TESTS,
        all_required_passed=True,
        report_hash="sha256:" + "0" * 64,
    )
    return placeholder.model_copy(
        update={"report_hash": compute_conformance_report_hash(placeholder)}
    )


def current_fingerprint_for(
    probe: SandboxProbeResult,
    *,
    network_policy: NetworkPolicy = NetworkPolicy.DENY,
    suite_version: str = CONFORMANCE_SUITE_VERSION,
) -> QualificationFingerprint:
    """The current-environment fingerprint matching a fabricated probe/report."""
    platform_fp = probe_platform_capabilities()
    executable_identity = (
        str(probe.executable_path) if probe.executable_path is not None else "none"
    )
    executable_hash = probe.executable_hash or ("sha256:" + "d" * 64)
    return QualificationFingerprint(
        backend_executable_identity=executable_identity,
        backend_executable_hash=executable_hash,
        confinement_code_fingerprint=compute_confinement_code_fingerprint(),
        profile_hash=compute_profile_hash(network_policy),
        platform_capability_fingerprint=platform_fp.fingerprint,
        network_policy=network_policy,
        conformance_suite_version=suite_version,
    )
