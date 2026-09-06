"""Post-execution file-inventory contract (goal.md §6.9)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from vibereview.ids import Sha256

from .records import RuntimeModel


class ExecutionFileRecord(RuntimeModel):
    """One execution-tree entry.

    Safe file types may carry a content hash. FIFOs, sockets, devices,
    unsafe symlink targets and oversized files are never opened or hashed:
    their type and relative path are sufficient, so ``size_bytes`` and
    ``content_hash`` remain ``None``.
    """

    relative_path: Path
    file_type: str
    size_bytes: int | None = Field(ge=0)
    content_hash: Sha256 | None
