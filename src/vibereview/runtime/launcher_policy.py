"""Trusted launcher policy (goal.md §6.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import Field

from .records import RuntimeModel

if TYPE_CHECKING:
    from .subprocess import SubprocessPolicy


class LauncherPolicy(RuntimeModel):
    """Resource limits policy passed to the trusted launcher."""

    max_open_files: int | None = None
    max_cpu_seconds: int | None = None
    max_address_space_bytes: int | None = None
    max_writable_single_file_bytes: int | None = None
    max_processes: int | None = None

    @classmethod
    def from_subprocess_policy(cls, policy: SubprocessPolicy) -> LauncherPolicy:
        return cls(
            max_open_files=policy.max_open_files,
            max_cpu_seconds=policy.max_cpu_seconds,
            max_address_space_bytes=policy.max_address_space_bytes,
            max_writable_single_file_bytes=None,
            max_processes=policy.max_processes,
        )
