"""Provider-neutral seam for task promotions that must commit coupled state.

The generic runtime owns task construction, engine execution, receipts, and the
generation transaction.  A domain package may supply an adapter when a frozen
repository invariant requires several canonical collections and a runtime
artifact to cross that transaction together.  Runtime code never imports the
domain package that implements the adapter.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Protocol

from pydantic import BaseModel

from .records import (
    AppliedTaskReceipt,
    TaskManifest,
    TaskProvenance,
    TaskSpec,
    TaskType,
)
from .registry import CanonicalIdRegistry
from .repository import AuxiliaryStagingWriter, Promotion, PromotionPayload
from .state import RepositorySnapshot


CoupledStagingMaterializer = Callable[
    [AuxiliaryStagingWriter, int, PromotionPayload, AppliedTaskReceipt], None
]

_CANONICAL_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class CoupledProposalValidationError(ValueError):
    """A coupled proposal is scientifically invalid for its locked inputs.

    Adapters must use this exception for deterministic caller/proposal defects.
    Other adapter exceptions are treated as runtime implementation failures so
    they can never become fallback-eligible scientific rejections.
    """


@dataclass(frozen=True, slots=True)
class CoupledCommitPlan:
    """Exact promotion and optional receipt-aware auxiliary materializer."""

    promotion: Promotion
    staging_materializer: CoupledStagingMaterializer | None = None

    def __post_init__(self) -> None:
        if not callable(self.promotion):
            raise TypeError("coupled commit promotion must be callable")
        if self.staging_materializer is not None and not callable(
            self.staging_materializer
        ):
            raise TypeError("coupled staging materializer must be callable")


class CoupledPromotionAdapter(Protocol):
    """Structural interface implemented outside ``vibereview.runtime``."""

    task_type: TaskType
    promotion_handler: str
    promotion_fingerprint: str
    rebuild_on_stale: bool
    canonicalizes_accepted_artifact: bool

    def validate_pre_execution(
        self,
        *,
        project_root: Path,
        spec: TaskSpec,
        invocation: BaseModel,
        manifest: TaskManifest,
        provenance: TaskProvenance,
    ) -> None:
        """Reject a task that is already known to be stale before engine use.

        The runtime calls this only after attempting exact accepted-receipt
        reuse.  Domain adapters may therefore permit historical construction
        for zero-cost replay while still preventing a stale, unmatched task
        from reaching an engine.  The writer-locked promotion remains the
        authoritative freshness check for races after this hook returns.
        """
        ...

    def validate_proposal(
        self,
        *,
        spec: TaskSpec,
        proposal: BaseModel,
        snapshot: RepositorySnapshot,
        dependency_keys: Mapping[str, str],
        invocation: BaseModel,
        registry: CanonicalIdRegistry,
        accepted_allocations: Mapping[str, str] | None = None,
    ) -> None: ...

    def prepare_commit(
        self,
        *,
        project_root: Path,
        spec: TaskSpec,
        proposal: BaseModel,
        invocation: BaseModel,
        manifest: TaskManifest,
        provenance: TaskProvenance,
    ) -> CoupledCommitPlan: ...

    def verify_receipt_reuse(
        self,
        *,
        project_root: Path,
        spec: TaskSpec,
        proposal: BaseModel,
        invocation: BaseModel,
        manifest: TaskManifest,
        provenance: TaskProvenance,
        receipt: AppliedTaskReceipt,
        current_generation: int,
        current_snapshot: RepositorySnapshot,
        current_registry: CanonicalIdRegistry,
    ) -> tuple[bool, str | None]: ...


def validate_coupled_adapter(
    adapter: CoupledPromotionAdapter,
    *,
    task_type: TaskType,
    spec: TaskSpec,
) -> None:
    """Reject a mismatched adapter before task creation or engine execution."""

    if adapter.task_type is not task_type:
        raise ValueError("coupled promotion adapter task type does not match the task")
    if adapter.promotion_handler != spec.promotion_handler:
        raise ValueError("coupled promotion adapter handler does not match the TaskSpec")
    if type(adapter.rebuild_on_stale) is not bool:
        raise ValueError("coupled promotion adapter rebuild_on_stale must be boolean")
    if type(getattr(adapter, "canonicalizes_accepted_artifact", False)) is not bool:
        raise ValueError(
            "coupled promotion adapter canonical-artifact flag must be boolean"
        )
    if not isinstance(adapter.promotion_fingerprint, str) or not _CANONICAL_SHA256.fullmatch(
        adapter.promotion_fingerprint
    ):
        raise ValueError("coupled promotion adapter fingerprint is not canonical SHA-256")
    for method_name in (
        "validate_pre_execution",
        "validate_proposal",
        "prepare_commit",
        "verify_receipt_reuse",
    ):
        if not callable(getattr(adapter, method_name, None)):
            raise ValueError(
                f"coupled promotion adapter is missing callable {method_name}"
            )
