"""Applied resource limit records (goal.md §6.3)."""

from __future__ import annotations

from pydantic import Field

from .records import ResourceLimitCode, RuntimeModel


class AppliedResourceLimit(RuntimeModel):
    """Record of one applied kernel or runtime resource limit."""

    code: ResourceLimitCode
    requested_value: int | None = None
    applied_soft_value: int | None = None
    applied_hard_value: int | None = None
    supported: bool
    authoritative: bool
    note: str | None = None
