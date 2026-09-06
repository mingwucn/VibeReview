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
import tempfile
from pathlib import Path

from .conformance import SandboxConformanceReport
from .confinement import ConfinementQualification, QualificationFingerprint
from .hashing import hash_json
from .records import RuntimeModel

QUALIFICATION_STORE_ENV_VAR = "VIBEREVIEW_QUALIFICATION_DIR"

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
    temp_path.replace(path)
    return path


def load_qualification_for_fingerprint(
    fingerprint: QualificationFingerprint,
    *,
    root: Path | None = None,
) -> StoredQualification | None:
    """Load the stored qualification matching the current fingerprint, if any."""

    store_root = root if root is not None else default_qualification_root()
    path = _qualification_path(store_root, compute_storage_fingerprint(fingerprint))
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return StoredQualification.model_validate(payload)
    except (OSError, UnicodeDecodeError, ValueError):
        return None
