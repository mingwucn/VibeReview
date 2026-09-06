"""Execution confinement levels and qualified Linux sandbox contracts (goal.md §6.10, §8)."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
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

