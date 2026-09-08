"""Fail-closed authorization and qualification gates for live engines."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pydantic import ConfigDict

from .confinement import (
    NetworkPolicy,
    compute_current_qualification_fingerprint,
    probe_sandbox_capabilities,
    require_real_engine_qualification,
)
from .records import RuntimeModel, Sha256
from .hashing import hash_json


class QualificationCheckpoint(StrEnum):
    PRE_CREDENTIALS = "pre_credentials"
    PRE_LAUNCH = "pre_launch"


class QualificationEvidence(RuntimeModel):
    """Nonsecret evidence retained for one live-execution checkpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint: QualificationCheckpoint
    storage_fingerprint: Sha256
    report_hash: Sha256
    network_policy: NetworkPolicy
    backend_executable_identity: str
    backend_executable_hash: Sha256


class ExecutionQualificationGate(Protocol):
    def check(self, checkpoint: QualificationCheckpoint) -> QualificationEvidence: ...

    def safe_configuration(self) -> dict[str, Any]: ...


class QualificationGateError(RuntimeError):
    def __init__(self, checkpoint: QualificationCheckpoint, message: str) -> None:
        self.checkpoint = checkpoint
        super().__init__(message)


class StoredSandboxQualificationGate:
    """Recompute and validate machine-local Bubblewrap qualification each time.

    Nothing is checked at factory construction: accepted receipt lookup therefore
    remains zero-cost and occurs before either live checkpoint.  ``check`` never
    caches a probe, fingerprint, store lookup, or report validation.
    """

    def __init__(
        self,
        backend: Any,
        *,
        network_policy: NetworkPolicy,
        qualification_root: Path | None = None,
    ) -> None:
        if getattr(backend, "network_policy", None) != network_policy:
            raise ValueError("backend and qualification-gate network policies differ")
        self._backend = backend
        self._network_policy = network_policy
        self._qualification_root = qualification_root

    def safe_configuration(self) -> dict[str, Any]:
        root = self._qualification_root
        return {
            "gate": "stored_sandbox_qualification",
            "network_policy": self._network_policy.value,
            "revalidate_each_checkpoint": True,
            "qualification_store_id": hash_json(
                {
                    "root": str(root.expanduser().absolute())
                    if root is not None
                    else "default"
                }
            ),
        }

    @property
    def backend(self) -> Any:
        """Exact backend instance whose profile is requalified."""

        return self._backend

    def check(self, checkpoint: QualificationCheckpoint) -> QualificationEvidence:
        # Deferred to avoid conformance -> execution -> live gate import cycles.
        from .qualification_store import (
            compute_storage_fingerprint,
            load_qualification_for_fingerprint,
        )

        try:
            if getattr(self._backend, "network_policy", None) != self._network_policy:
                raise RuntimeError("backend network policy drifted after gate construction")
            fingerprint = compute_current_qualification_fingerprint(
                self._backend, network_policy=self._network_policy
            )
            stored = load_qualification_for_fingerprint(
                fingerprint, root=self._qualification_root
            )
            if stored is None:
                raise RuntimeError("no current matching machine-local qualification")
            probe = probe_sandbox_capabilities(
                getattr(self._backend, "bwrap_path", None),
                network_policy=self._network_policy,
            )
            if getattr(self._backend, "network_policy", None) != self._network_policy:
                raise RuntimeError("backend network policy drifted during qualification")
            probe_identity = (
                str(probe.executable_path.resolve())
                if probe.executable_path is not None
                else "none"
            )
            if (
                probe_identity != fingerprint.backend_executable_identity
                or probe.executable_hash != fingerprint.backend_executable_hash
            ):
                raise RuntimeError("sandbox executable changed between fingerprint and probe")
            after = compute_current_qualification_fingerprint(
                self._backend, network_policy=self._network_policy
            )
            if after != fingerprint:
                raise RuntimeError("qualification fingerprint drifted during checkpoint")
            require_real_engine_qualification(
                self._backend,
                stored.qualification,
                fingerprint,
                current_probe=probe,
                report=stored.report,
            )
        except Exception as exc:
            raise QualificationGateError(checkpoint, str(exc)) from exc
        return QualificationEvidence(
            checkpoint=checkpoint,
            storage_fingerprint=compute_storage_fingerprint(fingerprint),
            report_hash=stored.report.report_hash,
            network_policy=self._network_policy,
            backend_executable_identity=fingerprint.backend_executable_identity,
            backend_executable_hash=fingerprint.backend_executable_hash,
        )
