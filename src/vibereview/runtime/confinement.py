"""Execution confinement levels and qualified Linux sandbox contracts (goal.md §6.10, §8)."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .hashing import canonical_json_bytes, code_fingerprint, hash_bytes, hash_file
from .records import RuntimeModel, Sha256

if TYPE_CHECKING:
    from .execution import ExecutionBackend


class ConfinementLevel(StrEnum):
    TEST_ONLY = "test_only"
    PATH_HYGIENE = "path_hygiene"
    OS_SANDBOX = "os_sandbox"
    ENGINE_NATIVE_SANDBOX = "engine_native_sandbox"


def allows_real_engine(level: ConfinementLevel) -> bool:
    """A real engine requires OS_SANDBOX or ENGINE_NATIVE_SANDBOX (§8.3)."""

    return level in {
        ConfinementLevel.OS_SANDBOX,
        ConfinementLevel.ENGINE_NATIVE_SANDBOX,
    }


class NetworkPolicy(StrEnum):
    """Network egress policy for confined execution (goal.md §8.4)."""

    DENY = "deny"
    HOST = "host"


class PlatformCapabilityFingerprint(RuntimeModel):
    """Probed host platform capability record (goal.md §8.5)."""

    operating_system: str
    architecture: str
    kernel_release: str

    user_namespace_available: bool
    mount_namespace_available: bool
    pid_namespace_available: bool

    wsl_detected: bool

    fingerprint: Sha256


class QualificationFingerprint(RuntimeModel):
    """Current runtime fingerprint against which a qualification is evaluated (goal.md §8.5)."""

    backend_executable_identity: str
    backend_executable_hash: Sha256
    confinement_code_fingerprint: Sha256
    profile_hash: Sha256
    platform_capability_fingerprint: Sha256
    network_policy: NetworkPolicy
    conformance_suite_version: str


class ConfinementQualification(RuntimeModel):
    """Attested qualification record for real engine execution (goal.md §8.5)."""

    backend_name: str
    backend_version: str | None

    backend_executable_identity: str
    backend_executable_hash: Sha256

    confinement_code_fingerprint: Sha256
    profile_hash: Sha256
    platform_capability_fingerprint: Sha256

    confinement_level: ConfinementLevel
    network_policy: NetworkPolicy

    conformance_suite_version: str
    tests_passed: tuple[str, ...]

    qualified: bool


REQUIRED_CONFORMANCE_TESTS: tuple[str, ...] = (
    "test_canary_read_denied",
    "test_canary_write_denied",
    "test_project_root_inaccessible",
    "test_task_private_inaccessible",
    "test_bundle_immutable",
    "test_authorized_output_and_scratch_writable",
    "test_network_denied_under_deny",
    "test_intended_credential_readable",
)

SANDBOX_PROFILE_CANONICAL: dict[str, Any] = {
    "mounts": [
        {"path": "/work/bundle", "mode": "ro"},
        {"path": "/work/launcher", "mode": "ro"},
        {"path": "/work/output", "mode": "rw"},
        {"path": "/work/scratch", "mode": "rw"},
        {"path": "/work/home", "mode": "rw"},
        {"path": "/work/tmp", "mode": "rw"},
        {"path": "/work/credentials", "mode": "ro_minimal"},
        {"path": "/proc", "mode": "proc"},
        {"path": "/dev", "mode": "dev"},
        {"path": "/usr", "mode": "ro"},
    ],
    "hidden": [
        "review_project_root",
        "state/generations",
        "work/tasks/*/private",
        "real_home",
        "unrelated_host_directories",
    ],
    "lifecycle": ["die_with_parent", "new_session"],
}


def compute_profile_hash(network_policy: NetworkPolicy) -> str:
    """Compute sha256 of canonical sandbox profile."""
    payload = {
        **SANDBOX_PROFILE_CANONICAL,
        "network_policy": network_policy.value,
    }
    return hash_bytes(canonical_json_bytes(payload))


def compute_confinement_code_fingerprint() -> str:
    """Compute code fingerprint for confinement and execution runtime modules."""
    here = Path(__file__).resolve()
    execution_py = here.parent / "execution.py"
    files = [here]
    if execution_py.is_file():
        files.append(execution_py)
    return code_fingerprint(files, contract_version="1.6")


def probe_platform_capabilities(bwrap_path: Path | str | None = None) -> PlatformCapabilityFingerprint:
    """Probe host platform and namespace capabilities (goal.md §8.5)."""
    system = platform.system()
    arch = platform.machine()
    release = platform.release()
    wsl = (
        Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists()
        or "microsoft" in release.lower()
        or "wsl" in release.lower()
    )

    user_ns = False
    mount_ns = False
    pid_ns = False
    resolved_bwrap = bwrap_path or shutil.which("bwrap")
    if resolved_bwrap is not None and system == "Linux":
        try:
            res = subprocess.run(
                [
                    str(resolved_bwrap),
                    "--ro-bind",
                    "/usr",
                    "/usr",
                    "--proc",
                    "/proc",
                    "--dev",
                    "/dev",
                    "--unshare-all",
                    "true",
                ],
                capture_output=True,
                timeout=5.0,
            )
            if res.returncode == 0:
                user_ns = True
                mount_ns = True
                pid_ns = True
        except Exception:
            pass

    data = {
        "operating_system": system,
        "architecture": arch,
        "kernel_release": release,
        "user_namespace_available": user_ns,
        "mount_namespace_available": mount_ns,
        "pid_namespace_available": pid_ns,
        "wsl_detected": wsl,
    }
    fp = hash_bytes(canonical_json_bytes(data))

    return PlatformCapabilityFingerprint(
        operating_system=system,
        architecture=arch,
        kernel_release=release,
        user_namespace_available=user_ns,
        mount_namespace_available=mount_ns,
        pid_namespace_available=pid_ns,
        wsl_detected=wsl,
        fingerprint=fp,
    )


def compute_current_qualification_fingerprint(
    backend: ExecutionBackend,
    network_policy: NetworkPolicy = NetworkPolicy.DENY,
    conformance_suite_version: str = "1.0",
) -> QualificationFingerprint:
    """Compute current environment fingerprint against which qualification is compared."""
    bwrap_path = getattr(backend, "bwrap_path", None) or shutil.which("bwrap")
    if bwrap_path is not None and Path(bwrap_path).is_file():
        exe_path = Path(bwrap_path).resolve()
        exe_identity = str(exe_path)
        exe_hash = hash_file(exe_path)
    else:
        exe_identity = "none"
        exe_hash = hash_bytes(b"none")

    code_fp = compute_confinement_code_fingerprint()
    profile_h = compute_profile_hash(network_policy)
    platform_fp = probe_platform_capabilities(bwrap_path).fingerprint

    return QualificationFingerprint(
        backend_executable_identity=exe_identity,
        backend_executable_hash=exe_hash,
        confinement_code_fingerprint=code_fp,
        profile_hash=profile_h,
        platform_capability_fingerprint=platform_fp,
        network_policy=network_policy,
        conformance_suite_version=conformance_suite_version,
    )


def require_real_engine_qualification(
    backend: ExecutionBackend,
    qualification: ConfinementQualification,
    current_fingerprint: QualificationFingerprint,
) -> None:
    """Fail closed unless the backend and qualification satisfy all requirements (goal.md §8.6)."""
    if not allows_real_engine(backend.confinement_level):
        raise RuntimeError(
            f"backend confinement level {backend.confinement_level} does not allow real engine execution"
        )
    if not qualification.qualified:
        raise RuntimeError("confinement qualification is marked not qualified")

    if qualification.backend_executable_identity != current_fingerprint.backend_executable_identity:
        raise RuntimeError("backend executable identity mismatch")
    if qualification.backend_executable_hash != current_fingerprint.backend_executable_hash:
        raise RuntimeError("backend executable hash mismatch")
    if qualification.confinement_code_fingerprint != current_fingerprint.confinement_code_fingerprint:
        raise RuntimeError("confinement code fingerprint mismatch")
    if qualification.profile_hash != current_fingerprint.profile_hash:
        raise RuntimeError("sandbox profile hash mismatch")
    if qualification.platform_capability_fingerprint != current_fingerprint.platform_capability_fingerprint:
        raise RuntimeError("platform capability fingerprint mismatch")
    if qualification.network_policy != current_fingerprint.network_policy:
        raise RuntimeError("network policy mismatch")
    if qualification.conformance_suite_version != current_fingerprint.conformance_suite_version:
        raise RuntimeError("conformance suite version mismatch")

    missing = set(REQUIRED_CONFORMANCE_TESTS) - set(qualification.tests_passed)
    if missing:
        raise RuntimeError(f"qualification is missing required conformance tests: {sorted(missing)}")



# --- r5e: sandbox capability probing and shared profile argv (goal.md §4) ---


class SandboxProbeStatus(StrEnum):
    """Staged capability verdict for the sandbox backend (goal.md §4.2)."""

    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"
    USABLE = "usable"


class SandboxFailureCode(StrEnum):
    """Diagnostic category for a failed sandbox capability probe (goal.md §4.3)."""

    EXECUTABLE_NOT_FOUND = "executable_not_found"
    VERSION_PROBE_FAILED = "version_probe_failed"

    USER_NAMESPACE_DENIED = "user_namespace_denied"
    MOUNT_NAMESPACE_DENIED = "mount_namespace_denied"
    PID_NAMESPACE_DENIED = "pid_namespace_denied"
    NETWORK_NAMESPACE_DENIED = "network_namespace_denied"

    APPARMOR_USERNS_RESTRICTION = "apparmor_userns_restriction"
    PROFILE_EXECUTION_FAILED = "profile_execution_failed"
    UNKNOWN = "unknown"


class SandboxProbeCommandResult(RuntimeModel):
    """Bounded retained record of one probe command (goal.md §4.3)."""

    name: str
    argv: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float


class SandboxProbeResult(RuntimeModel):
    """Host capability report for the Bubblewrap sandbox backend (goal.md §4.1).

    The report is persistable as a diagnostic artifact: probe commands run
    with a sanitized environment inside their own temporary tree outside any
    review project, and captured streams are bounded, so the record contains
    no secrets and no project-private paths.
    """

    backend_name: str
    backend_version: str | None
    executable_path: Path | None
    executable_hash: Sha256 | None

    status: SandboxProbeStatus
    failure_code: SandboxFailureCode | None
    diagnostic: str | None

    operating_system: str
    architecture: str
    kernel_release: str
    wsl_detected: bool

    unprivileged_userns_clone: str | None
    apparmor_restrict_unprivileged_userns: str | None
    apparmor_profile_detected: bool | None

    commands: tuple[SandboxProbeCommandResult, ...]


VERSION_PROBE = "version"
USER_MOUNT_NAMESPACE_PROBE = "user_mount_namespace"
PID_NAMESPACE_PROBE = "pid_namespace"
NETWORK_NAMESPACE_PROBE = "network_namespace"
FULL_PROFILE_PROBE = "full_profile"

_PROBE_STREAM_BOUND_BYTES = 8192
_PROBE_TIMEOUT_SECONDS = 10.0

_USERNS_CLONE_SYSCTL = Path("/proc/sys/kernel/unprivileged_userns_clone")
_APPARMOR_USERNS_SYSCTL = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
_APPARMOR_PROFILES_PATH = Path("/sys/kernel/security/apparmor/profiles")

_FULL_PROFILE_PROBE_SNIPPET = (
    "from pathlib import Path; "
    "text = Path('/work/bundle/probe.json').read_text(encoding='utf-8'); "
    "assert 'vibereview-sandbox-probe' in text, 'bundle read failed'; "
    "Path('/work/scratch/probe.txt').write_text('scratch-ok\\n', encoding='utf-8'); "
    "Path('/work/output/probe.txt').write_text('output-ok\\n', encoding='utf-8')"
)


def build_bwrap_profile_argv(
    bwrap_path: Path | str,
    network_policy: NetworkPolicy,
    *,
    bundle_dir: Path,
    launcher_dir: Path,
    output_dir: Path,
    scratch_dir: Path,
    home_dir: Path,
    tmp_dir: Path,
    credentials_dir: Path | None,
    inner_argv: Sequence[str],
) -> list[str]:
    """Build the Bubblewrap argv for the VibeReview sandbox profile (goal.md §8.2).

    Shared by ``BubblewrapExecutionBackend.wrap_command`` and the staged
    capability probe so the probe can never drift from the production
    profile (goal.md §4.2).
    """

    bwrap_cmd: list[str] = [str(bwrap_path)]

    # Read-only system and runtime mounts
    bwrap_cmd.extend(["--ro-bind", "/usr", "/usr"])
    for sys_path in (
        "/lib",
        "/lib64",
        "/bin",
        "/sbin",
        "/etc/ssl",
        "/etc/pki",
        "/etc/ca-certificates",
        "/etc/resolv.conf",
    ):
        if Path(sys_path).exists():
            bwrap_cmd.extend(["--ro-bind-try", sys_path, sys_path])

    # Python interpreter and library prefixes
    py_prefix = Path(sys.prefix).resolve()
    bwrap_cmd.extend(["--ro-bind-try", str(py_prefix), str(py_prefix)])
    py_base_prefix = Path(sys.base_prefix).resolve()
    if py_base_prefix != py_prefix:
        bwrap_cmd.extend(["--ro-bind-try", str(py_base_prefix), str(py_base_prefix)])
    py_exec = Path(sys.executable).resolve()
    if not str(py_exec).startswith(str(py_prefix)) and not str(py_exec).startswith(str(py_base_prefix)):
        bwrap_cmd.extend(["--ro-bind-try", str(py_exec), str(py_exec)])

    # Namespaces and process lifecycle
    if network_policy == NetworkPolicy.DENY:
        bwrap_cmd.append("--unshare-all")
    else:
        bwrap_cmd.extend(["--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts"])

    bwrap_cmd.extend([
        "--proc", "/proc",
        "--dev", "/dev",
        "--die-with-parent",
        "--dir", "/work",
        "--ro-bind", str(bundle_dir), "/work/bundle",
        "--ro-bind", str(launcher_dir), "/work/launcher",
        "--bind", str(output_dir), "/work/output",
        "--bind", str(scratch_dir), "/work/scratch",
        "--bind", str(home_dir), "/work/home",
        "--bind", str(tmp_dir), "/work/tmp",
    ])

    if credentials_dir is not None and credentials_dir.is_dir():
        bwrap_cmd.extend(["--ro-bind", str(credentials_dir), "/work/credentials"])

    bwrap_cmd.extend([
        "--chdir", "/work",
        "--setenv", "HOME", "/work/home",
        "--setenv", "TMPDIR", "/work/tmp",
    ])

    bwrap_cmd.extend([str(argument) for argument in inner_argv])
    return bwrap_cmd


def classify_sandbox_failure(
    probe_name: str,
    stderr: str,
    *,
    apparmor_restrict_unprivileged_userns: str | None,
) -> SandboxFailureCode:
    """Map a failed probe's retained stderr to a diagnostic category (goal.md §4.3).

    Classification only labels known failure messages; success is never
    inferred from error text. A namespace denial together with the AppArmor
    restricted-userns sysctl identifies the AppArmor restriction.
    """

    text = stderr.lower()
    namespace_denied = (
        "setting up uid map" in text or "creating new namespace failed" in text
    )
    if namespace_denied and (apparmor_restrict_unprivileged_userns or "").strip() == "1":
        return SandboxFailureCode.APPARMOR_USERNS_RESTRICTION
    if "loopback: failed rtm_newaddr" in text:
        return SandboxFailureCode.NETWORK_NAMESPACE_DENIED
    if "setting up uid map" in text:
        return SandboxFailureCode.USER_NAMESPACE_DENIED
    if "creating new namespace failed" in text:
        if probe_name == NETWORK_NAMESPACE_PROBE:
            return SandboxFailureCode.NETWORK_NAMESPACE_DENIED
        if probe_name == PID_NAMESPACE_PROBE:
            return SandboxFailureCode.PID_NAMESPACE_DENIED
        return SandboxFailureCode.USER_NAMESPACE_DENIED
    if probe_name == FULL_PROFILE_PROBE:
        return SandboxFailureCode.PROFILE_EXECUTION_FAILED
    return SandboxFailureCode.UNKNOWN


def _read_kernel_setting(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _detect_bwrap_apparmor_profile() -> bool | None:
    try:
        text = _APPARMOR_PROFILES_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        name = line.strip().split(" ", 1)[0] if line.strip() else ""
        if "bwrap" in name or "bubblewrap" in name:
            return True
    return False


def _probe_environment() -> dict[str, str]:
    """Sanitized probe environment: no inherited variables, hence no secrets."""

    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
    }


def _trim_text_to_utf8_bound(text: str, bound: int) -> str:
    parts: list[str] = []
    total = 0
    for character in text:
        size = len(character.encode("utf-8"))
        if total + size > bound:
            break
        parts.append(character)
        total += size
    return "".join(parts)


def _bounded_probe_text(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        text = data.decode("utf-8", errors="replace")
    else:
        text = data
    return _trim_text_to_utf8_bound(text, _PROBE_STREAM_BOUND_BYTES)


def _run_probe_command(
    name: str,
    argv: Sequence[str],
    *,
    cwd: Path,
) -> SandboxProbeCommandResult:
    started = time.monotonic()
    exit_code: int | None
    try:
        completed = subprocess.run(
            [str(argument) for argument in argv],
            capture_output=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            cwd=cwd,
            env=_probe_environment(),
        )
        exit_code = completed.returncode
        stdout_text = _bounded_probe_text(completed.stdout)
        stderr_text = _bounded_probe_text(completed.stderr)
    except subprocess.TimeoutExpired as exc:
        exit_code = None
        stdout_text = _bounded_probe_text(exc.stdout)
        stderr_text = _bounded_probe_text(exc.stderr)
        note = f"[probe timed out after {_PROBE_TIMEOUT_SECONDS} seconds]"
        stderr_text = f"{stderr_text}\n{note}" if stderr_text else note
    except OSError as exc:
        exit_code = None
        stdout_text = ""
        stderr_text = f"probe launch failed: {exc}"
    return SandboxProbeCommandResult(
        name=name,
        argv=tuple(str(argument) for argument in argv),
        exit_code=exit_code,
        stdout=stdout_text,
        stderr=stderr_text,
        duration_seconds=time.monotonic() - started,
    )


def _parse_bwrap_version(stdout: str) -> str | None:
    for token in stdout.split():
        if re.fullmatch(r"\d+(\.\d+)+", token):
            return token
    return None


def _true_command() -> str:
    resolved = shutil.which("true")
    if resolved is not None:
        return resolved
    for candidate in ("/usr/bin/true", "/bin/true"):
        if Path(candidate).is_file():
            return candidate
    return "true"


def _minimal_probe_argv(bwrap_path: Path, *unshare_flags: str) -> list[str]:
    """Smallest user/mount configuration plus the namespace under test."""

    argv: list[str] = [str(bwrap_path)]
    for sys_path in ("/usr", "/bin", "/lib", "/lib64"):
        if Path(sys_path).exists():
            argv.extend(["--ro-bind-try", sys_path, sys_path])
    argv.extend(unshare_flags)
    if "--unshare-pid" in unshare_flags:
        argv.extend(["--proc", "/proc"])
    argv.extend(["--die-with-parent", _true_command()])
    return argv


def _probe_diagnostic(name: str, command: SandboxProbeCommandResult) -> str:
    exit_label = "timeout" if command.exit_code is None else f"exit {command.exit_code}"
    first_line = next(
        (line.strip() for line in command.stderr.splitlines() if line.strip()), ""
    )
    detail = f": {first_line[:200]}" if first_line else ""
    return f"{name} probe failed ({exit_label}){detail}"


def probe_sandbox_capabilities(
    bwrap_path: Path | str | None = None,
    *,
    network_policy: NetworkPolicy = NetworkPolicy.DENY,
) -> SandboxProbeResult:
    """Probe actual host sandbox capabilities in five stages (goal.md §4.2).

    Binary presence alone never yields ``USABLE``: only the full VibeReview
    profile probe (stage 5, built through the same argv construction as the
    production backend) can produce ``USABLE``. A present binary that fails
    any earlier required stage is ``BLOCKED``; a missing binary is
    ``UNAVAILABLE``.
    """

    operating_system = platform.system()
    architecture = platform.machine()
    kernel_release = platform.release()
    wsl_detected = (
        Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists()
        or "microsoft" in kernel_release.lower()
        or "wsl" in kernel_release.lower()
    )
    unprivileged_userns_clone = _read_kernel_setting(_USERNS_CLONE_SYSCTL)
    apparmor_restrict = _read_kernel_setting(_APPARMOR_USERNS_SYSCTL)
    apparmor_profile_detected = _detect_bwrap_apparmor_profile()

    commands: list[SandboxProbeCommandResult] = []
    backend_version: str | None = None
    executable_path: Path | None = None
    executable_hash: str | None = None

    def _result(
        status: SandboxProbeStatus,
        failure_code: SandboxFailureCode | None,
        diagnostic: str | None,
    ) -> SandboxProbeResult:
        return SandboxProbeResult(
            backend_name="bubblewrap",
            backend_version=backend_version,
            executable_path=executable_path,
            executable_hash=executable_hash,
            status=status,
            failure_code=failure_code,
            diagnostic=diagnostic,
            operating_system=operating_system,
            architecture=architecture,
            kernel_release=kernel_release,
            wsl_detected=wsl_detected,
            unprivileged_userns_clone=unprivileged_userns_clone,
            apparmor_restrict_unprivileged_userns=apparmor_restrict,
            apparmor_profile_detected=apparmor_profile_detected,
            commands=tuple(commands),
        )

    resolved = bwrap_path if bwrap_path is not None else shutil.which("bwrap")
    if resolved is None:
        return _result(
            SandboxProbeStatus.UNAVAILABLE,
            SandboxFailureCode.EXECUTABLE_NOT_FOUND,
            "bubblewrap executable not found on PATH",
        )
    executable_path = Path(resolved).resolve()
    if not executable_path.is_file():
        return _result(
            SandboxProbeStatus.UNAVAILABLE,
            SandboxFailureCode.EXECUTABLE_NOT_FOUND,
            f"bubblewrap executable does not exist: {executable_path}",
        )
    executable_hash = hash_file(executable_path)

    probe_root = Path(tempfile.mkdtemp(prefix="vibereview-probe-")).resolve()
    try:
        version_command = _run_probe_command(
            VERSION_PROBE, [str(executable_path), "--version"], cwd=probe_root
        )
        commands.append(version_command)
        if version_command.exit_code != 0:
            return _result(
                SandboxProbeStatus.BLOCKED,
                SandboxFailureCode.VERSION_PROBE_FAILED,
                _probe_diagnostic(VERSION_PROBE, version_command),
            )
        backend_version = _parse_bwrap_version(version_command.stdout)

        stages: list[tuple[str, list[str]]] = [
            (
                USER_MOUNT_NAMESPACE_PROBE,
                _minimal_probe_argv(executable_path, "--unshare-user"),
            ),
            (
                PID_NAMESPACE_PROBE,
                _minimal_probe_argv(
                    executable_path, "--unshare-user", "--unshare-pid"
                ),
            ),
            (
                NETWORK_NAMESPACE_PROBE,
                _minimal_probe_argv(
                    executable_path, "--unshare-user", "--unshare-net"
                ),
            ),
        ]
        for stage_name, stage_argv in stages:
            command = _run_probe_command(stage_name, stage_argv, cwd=probe_root)
            commands.append(command)
            if command.exit_code == 0:
                continue
            code = classify_sandbox_failure(
                stage_name,
                command.stderr,
                apparmor_restrict_unprivileged_userns=apparmor_restrict,
            )
            if (
                stage_name == NETWORK_NAMESPACE_PROBE
                and network_policy is not NetworkPolicy.DENY
            ):
                # Network isolation is required only for the DENY profile
                # (goal.md §4.2); the failed probe stays on the record.
                continue
            return _result(
                SandboxProbeStatus.BLOCKED, code, _probe_diagnostic(stage_name, command)
            )

        tree_root = probe_root / "full-profile"
        bundle_dir = tree_root / "bundle"
        launcher_dir = tree_root / "launcher"
        output_dir = tree_root / "output"
        scratch_dir = tree_root / "scratch"
        home_dir = tree_root / "home"
        tmp_dir = tree_root / "tmp"
        credentials_dir = tree_root / "credentials"
        for directory in (
            bundle_dir,
            launcher_dir,
            output_dir,
            scratch_dir,
            home_dir,
            tmp_dir,
        ):
            directory.mkdir(parents=True)
        credentials_dir.mkdir(mode=0o700)
        (bundle_dir / "probe.json").write_text(
            '{"probe": "vibereview-sandbox-probe"}\n', encoding="utf-8"
        )
        profile_argv = build_bwrap_profile_argv(
            executable_path,
            network_policy,
            bundle_dir=bundle_dir,
            launcher_dir=launcher_dir,
            output_dir=output_dir,
            scratch_dir=scratch_dir,
            home_dir=home_dir,
            tmp_dir=tmp_dir,
            credentials_dir=credentials_dir,
            inner_argv=[sys.executable, "-c", _FULL_PROFILE_PROBE_SNIPPET],
        )
        profile_command = _run_probe_command(
            FULL_PROFILE_PROBE, profile_argv, cwd=probe_root
        )
        commands.append(profile_command)
        if profile_command.exit_code != 0:
            code = classify_sandbox_failure(
                FULL_PROFILE_PROBE,
                profile_command.stderr,
                apparmor_restrict_unprivileged_userns=apparmor_restrict,
            )
            return _result(
                SandboxProbeStatus.BLOCKED,
                code,
                _probe_diagnostic(FULL_PROFILE_PROBE, profile_command),
            )
        if not (
            (scratch_dir / "probe.txt").is_file()
            and (output_dir / "probe.txt").is_file()
        ):
            return _result(
                SandboxProbeStatus.BLOCKED,
                SandboxFailureCode.PROFILE_EXECUTION_FAILED,
                "full profile probe exited 0 but produced no scratch/output files",
            )
        return _result(SandboxProbeStatus.USABLE, None, None)
    finally:
        shutil.rmtree(probe_root, ignore_errors=True)
