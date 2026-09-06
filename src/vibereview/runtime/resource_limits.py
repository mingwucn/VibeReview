"""Writable-tree quota monitor machinery (goal.md §7.10).

The scanner walks only the writable quota roots (``output/``, ``scratch/``,
``home/``, ``tmp/``) with ``os.scandir``/``lstat`` semantics and never follows
links; ``bundle/``, ``launcher/`` and ``credentials/`` are quota-exempt
(§6.7). Traversal is name-sorted and stops at the first breach so detection
is deterministic. Directory depth counts path components below the execution
root, so the top-level quota roots themselves have depth 1.

Kernel-level limits (RLIMIT_*) may additionally be applied by a trusted
launcher; this Python monitor remains the deterministic fallback where kernel
behaviour varies (§7.10).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from pydantic import ConfigDict

from .records import (
    AttemptFailure,
    AttemptFailureStage,
    ResourceLimitCode,
    RuntimeModel,
)
from .subprocess import WRITABLE_QUOTA_ROOTS, SubprocessPolicy


# Hard bound on entries visited in one scan pass; a tree this large has
# already made a complete inventory meaningless (§7.10).
_SCAN_ENTRY_LIMIT = 100_000


class WritableTreeBreach(RuntimeModel):
    """One detected writable-quota breach (goal.md §6.5, §7.10)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ResourceLimitCode
    message: str
    relative_path: Path | None = None

    def attempt_failure(self) -> AttemptFailure:
        return AttemptFailure(
            code=self.code,
            stage=AttemptFailureStage.RESOURCE_LIMIT,
            message=self.message,
            relative_path=self.relative_path,
        )


def scan_writable_roots(
    execution_root: Path, policy: SubprocessPolicy
) -> WritableTreeBreach | None:
    """Scan the writable quota roots and return the first breach, if any.

    Only regular-file bytes and counts feed the quotas; symlinks and special
    files are never followed or opened here (they surface in the post-exit
    execution inventory instead).
    """

    total_bytes = 0
    file_count = 0
    visited = 0
    stack: list[tuple[Path, Path]] = []
    for root_name in WRITABLE_QUOTA_ROOTS:
        root_path = execution_root / root_name
        try:
            root_stat = os.lstat(root_path)
        except OSError:
            continue
        if stat.S_ISDIR(root_stat.st_mode):
            stack.append((root_path, Path(root_name)))
    while stack:
        directory, relative = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            continue
        child_directories: list[tuple[Path, Path]] = []
        for entry in entries:
            entry_relative = relative / entry.name
            visited += 1
            if visited > _SCAN_ENTRY_LIMIT:
                return WritableTreeBreach(
                    code=ResourceLimitCode.MAX_WRITABLE_FILE_COUNT,
                    message=(
                        "Writable tree scan stopped after "
                        f"{_SCAN_ENTRY_LIMIT} entries."
                    ),
                    relative_path=relative,
                )
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(entry_stat.st_mode):
                depth = len(entry_relative.parts)
                if depth > policy.max_writable_directory_depth:
                    return WritableTreeBreach(
                        code=ResourceLimitCode.MAX_WRITABLE_DIRECTORY_DEPTH,
                        message=(
                            f"Writable directory depth exceeded "
                            f"{policy.max_writable_directory_depth}."
                        ),
                        relative_path=entry_relative,
                    )
                child_directories.append((Path(entry.path), entry_relative))
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                continue
            file_count += 1
            total_bytes += entry_stat.st_size
            quota_root = Path(entry_relative.parts[0])
            if file_count > policy.max_writable_files:
                return WritableTreeBreach(
                    code=ResourceLimitCode.MAX_WRITABLE_FILE_COUNT,
                    message=(
                        f"Writable file count exceeded "
                        f"{policy.max_writable_files}."
                    ),
                    relative_path=quota_root,
                )
            if entry_stat.st_size > policy.max_writable_single_file_bytes:
                return WritableTreeBreach(
                    code=ResourceLimitCode.MAX_WRITABLE_SINGLE_FILE_BYTES,
                    message=(
                        f"Writable file {entry_relative.as_posix()} exceeded "
                        f"{policy.max_writable_single_file_bytes} bytes."
                    ),
                    relative_path=entry_relative,
                )
            if total_bytes > policy.max_writable_tree_bytes:
                return WritableTreeBreach(
                    code=ResourceLimitCode.MAX_WRITABLE_TREE_BYTES,
                    message=(
                        f"Writable tree exceeded "
                        f"{policy.max_writable_tree_bytes} bytes."
                    ),
                    relative_path=quota_root,
                )
        # Depth-first with name-sorted siblings: push reversed so the pop
        # order is deterministic.
        stack.extend(reversed(child_directories))
    return None


def count_process_group_members(process_group_id: int) -> int | None:
    """Count live ``/proc`` entries in one process group.

    Returns ``None`` where ``/proc`` is unavailable so the caller can skip
    the process-count quota instead of failing closed on a platform the
    monitor cannot inspect.
    """

    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return None
    count = 0
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat_text = (entry / "stat").read_text(
                encoding="utf-8", errors="replace"
            )
            closing = stat_text.rfind(")")
            if closing == -1:
                continue
            fields = stat_text[closing + 1 :].split()
            # After comm: state, ppid, pgrp (proc_pid_stat(5)).
            if len(fields) < 3:
                continue
            if int(fields[2]) == process_group_id:
                count += 1
        except (OSError, ValueError):
            continue
    return count


def check_process_count(
    process_group_id: int, policy: SubprocessPolicy
) -> WritableTreeBreach | None:
    """Enforce ``max_processes`` against the child's process group (§7.10)."""

    if policy.max_processes is None:
        return None
    count = count_process_group_members(process_group_id)
    if count is None or count <= policy.max_processes:
        return None
    return WritableTreeBreach(
        code=ResourceLimitCode.MAX_PROCESS_COUNT,
        message=f"Process count exceeded {policy.max_processes}.",
    )
