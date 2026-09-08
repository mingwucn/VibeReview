"""Machine-local qualification storage (goal.md §6.4).

A qualification is host-specific: it attests that this machine, with this
exact Bubblewrap executable, confinement code, sandbox profile, platform
capabilities, network policy and conformance suite version, executed and
passed the conformance suite. A qualification produced by CI (or any other
host) is never authority for another machine — the stored fingerprint
invalidates automatically when any covered property changes.

The default root is ``~/.config/vibereview/qualifications/``; it can be
overridden with the ``VIBEREVIEW_QUALIFICATION_DIR`` environment variable or
an explicit ``root`` argument. Tests must always use the override and never
write to the real home directory.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

from .conformance import SandboxConformanceReport
from .confinement import ConfinementQualification, QualificationFingerprint
from .hashing import hash_json
from .records import RuntimeModel

QUALIFICATION_STORE_ENV_VAR = "VIBEREVIEW_QUALIFICATION_DIR"
_MAX_STORED_QUALIFICATION_BYTES = 2_097_152

_FINGERPRINT_FIELDS = (
    "backend_executable_hash",
    "confinement_code_fingerprint",
    "profile_hash",
    "platform_capability_fingerprint",
    "network_policy",
    "conformance_suite_version",
)


class StoredQualification(RuntimeModel):
    """A stored qualification together with its resolving conformance report."""

    qualification: ConfinementQualification
    report: SandboxConformanceReport


def default_qualification_root() -> Path:
    override = os.environ.get(QUALIFICATION_STORE_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "vibereview" / "qualifications"


def compute_storage_fingerprint(fingerprint: QualificationFingerprint) -> str:
    """Content fingerprint covering every property a qualification binds to."""

    payload = fingerprint.model_dump(mode="json")
    return hash_json({name: payload[name] for name in _FINGERPRINT_FIELDS})


def _qualification_path(root: Path, storage_fingerprint: str) -> Path:
    return root / f"{storage_fingerprint.removeprefix('sha256:')}.json"


def store_qualification(
    qualification: ConfinementQualification,
    report: SandboxConformanceReport,
    *,
    root: Path | None = None,
) -> Path:
    """Persist the qualification and its report under the machine-local root."""

    store_root = root if root is not None else default_qualification_root()
    store_root.mkdir(parents=True, exist_ok=True)
    if store_root.is_symlink() or not store_root.is_dir():
        raise RuntimeError("qualification root must be a non-symlink directory")
    os.chmod(store_root, 0o700)
    fingerprint_source = QualificationFingerprint(
        backend_executable_identity=qualification.backend_executable_identity,
        backend_executable_hash=qualification.backend_executable_hash,
        confinement_code_fingerprint=qualification.confinement_code_fingerprint,
        profile_hash=qualification.profile_hash,
        platform_capability_fingerprint=qualification.platform_capability_fingerprint,
        network_policy=qualification.network_policy,
        conformance_suite_version=qualification.conformance_suite_version,
    )
    path = _qualification_path(
        store_root, compute_storage_fingerprint(fingerprint_source)
    )
    payload = StoredQualification(qualification=qualification, report=report)
    text = json.dumps(payload.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=store_root,
        prefix=".qualification-",
        delete=False,
    ) as handle:
        handle.write(text)
        temp_path = Path(handle.name)
    os.chmod(temp_path, 0o600)
    temp_path.replace(path)
    os.chmod(path, 0o600)
    return path


def load_qualification_for_fingerprint(
    fingerprint: QualificationFingerprint,
    *,
    root: Path | None = None,
) -> StoredQualification | None:
    """Load the stored qualification matching the current fingerprint, if any."""

    store_root = root if root is not None else default_qualification_root()
    try:
        root_stat = os.lstat(store_root)
    except OSError:
        return None
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid != os.getuid()
        or stat.S_IMODE(root_stat.st_mode) & 0o077
    ):
        return None
    path = _qualification_path(store_root, compute_storage_fingerprint(fingerprint))
    try:
        item = os.lstat(path)
    except OSError:
        return None
    if (
        not stat.S_ISREG(item.st_mode)
        or item.st_uid != os.getuid()
        or stat.S_IMODE(item.st_mode) & 0o177
        or item.st_size > _MAX_STORED_QUALIFICATION_BYTES
    ):
        return None
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ) != (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns):
            return None
        chunks: list[bytes] = []
        observed = 0
        while True:
            chunk = os.read(
                descriptor,
                min(65_536, _MAX_STORED_QUALIFICATION_BYTES + 1 - observed),
            )
            if not chunk:
                break
            observed += len(chunk)
            if observed > _MAX_STORED_QUALIFICATION_BYTES:
                return None
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            observed != opened.st_size
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        ):
            return None
        payload = json.loads(b"".join(chunks).decode("utf-8"))
        return StoredQualification.model_validate(payload)
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
