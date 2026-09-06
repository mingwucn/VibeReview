"""Output-tree policy, trusted output identity, and proposal import (goal.md §7.7-§7.9).

All checks are ``lstat``-based and never follow links. Unsafe entries are
never opened: their type and relative path are sufficient (§6.9). The child
process has exited before these checks run, so the only remaining races are
the planted-file conditions the checks defend against.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

from pydantic import ConfigDict

from .execution_inventory import ExecutionFileRecord
from .hashing import hash_file
from .records import AttemptFailure, AttemptFailureStage, RuntimeModel
from .subprocess import WRITABLE_QUOTA_ROOTS, SubprocessPolicy


PROPOSAL_FILENAME = "proposal.json"

_INVENTORY_ENTRY_LIMIT = 4096
_INVENTORY_ENTRY_LIMIT_AFTER_BREACH = 256


def _failure(
    code: str,
    stage: AttemptFailureStage,
    message: str,
    relative_path: Path | None = None,
) -> AttemptFailure:
    return AttemptFailure(
        code=code, stage=stage, message=message, relative_path=relative_path
    )


def classify_mode(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "regular"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISBLK(mode) or stat.S_ISCHR(mode):
        return "device"
    return "other"


def open_tracked_output_dir(output_dir: Path) -> tuple[int, os.stat_result]:
    """Open ``output/`` before launch and anchor its identity (goal.md §7.7)."""

    descriptor = os.open(
        output_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    return descriptor, os.fstat(descriptor)


def verify_output_directory_identity(
    output_dir: Path, trusted_stat: os.stat_result
) -> AttemptFailure | None:
    """Re-verify ``output/`` after exit; a replaced or symlinked directory is
    an output-policy violation and precedes missing-proposal classification
    (goal.md §7.7)."""

    try:
        current = os.lstat(output_dir)
    except OSError:
        return _failure(
            "output_directory_replaced",
            AttemptFailureStage.OUTPUT_TREE,
            "output directory was removed during execution",
            Path("output"),
        )
    if not stat.S_ISDIR(current.st_mode) or stat.S_ISLNK(current.st_mode):
        return _failure(
            "output_directory_replaced",
            AttemptFailureStage.OUTPUT_TREE,
            "output directory is no longer a real directory",
            Path("output"),
        )
    if (current.st_dev, current.st_ino) != (
        trusted_stat.st_dev,
        trusted_stat.st_ino,
    ):
        return _failure(
            "output_directory_replaced",
            AttemptFailureStage.OUTPUT_TREE,
            "output directory was replaced during execution",
            Path("output"),
        )
    return None


def scan_output_tree(
    output_dir: Path, policy: SubprocessPolicy
) -> tuple[AttemptFailure, ...]:
    """Enforce the allowed-output-file set under ``output/`` (goal.md §7.9).

    The proposal file itself is classified by the race-resistant import; the
    scan only flags unexpected names and unsafe types of other allowed names.
    """

    failures: list[AttemptFailure] = []
    try:
        entries = sorted(os.scandir(output_dir), key=lambda entry: entry.name)
    except OSError as exc:
        return (
            _failure(
                "output_directory_replaced",
                AttemptFailureStage.OUTPUT_TREE,
                f"output directory cannot be scanned: {exc}",
                Path("output"),
            ),
        )
    for entry in entries:
        relative_path = Path("output") / entry.name
        if entry.name not in policy.allowed_output_files:
            failures.append(
                _failure(
                    "unexpected_output_file",
                    AttemptFailureStage.OUTPUT_TREE,
                    f"output/{entry.name} is not an allowed output file.",
                    relative_path,
                )
            )
            continue
        if entry.name == PROPOSAL_FILENAME:
            continue
        try:
            entry_stat = entry.stat(follow_symlinks=False)
        except OSError:
            entry_stat = None
        if entry_stat is None or not stat.S_ISREG(entry_stat.st_mode):
            failures.append(
                _failure(
                    "unsafe_output_entry",
                    AttemptFailureStage.OUTPUT_TREE,
                    f"output/{entry.name} is not a regular file.",
                    relative_path,
                )
            )
    return tuple(failures)


class ProposalImport(RuntimeModel):
    """Result of the race-resistant proposal import (goal.md §7.8)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str | None
    failures: tuple[AttemptFailure, ...] = ()
    output_policy_violated: bool = False
    format_invalid: bool = False


def _import_failure(
    code: str, message: str, *, policy_violation: bool
) -> ProposalImport:
    failure = _failure(
        code,
        AttemptFailureStage.PROPOSAL_FILE,
        message,
        Path("output") / PROPOSAL_FILENAME,
    )
    return ProposalImport(
        text=None,
        failures=(failure,),
        output_policy_violated=policy_violation,
        format_invalid=not policy_violation,
    )


def import_proposal(
    output_fd: int,
    policy: SubprocessPolicy,
    bundle_inodes: frozenset[tuple[int, int]],
) -> ProposalImport:
    """Import ``proposal.json`` relative to the trusted output descriptor.

    Unsafe file types and hard-link conditions are output-policy violations;
    missing, empty, oversized or non-UTF-8 regular files are format failures
    (goal.md §7.8). Malformed JSON remains the kernel's existing stage.
    """

    cap = policy.max_proposal_bytes
    try:
        proposal_fd = os.open(
            PROPOSAL_FILENAME,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=output_fd,
        )
    except FileNotFoundError:
        return _import_failure(
            "proposal_missing", "proposal.json is missing", policy_violation=False
        )
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return _import_failure(
                "proposal_symlink",
                "proposal.json is a symlink",
                policy_violation=True,
            )
        if exc.errno in {errno.ENXIO, errno.ENODEV}:
            return _import_failure(
                "proposal_unsafe_type",
                "proposal.json is not a regular file",
                policy_violation=True,
            )
        raise
    try:
        before = os.fstat(proposal_fd)
        if not stat.S_ISREG(before.st_mode):
            return _import_failure(
                "proposal_unsafe_type",
                f"proposal.json has unsafe type "
                f"{classify_mode(before.st_mode)}",
                policy_violation=True,
            )
        if before.st_nlink != 1:
            return _import_failure(
                "proposal_hardlink",
                "proposal.json has more than one link",
                policy_violation=True,
            )
        if (before.st_dev, before.st_ino) in bundle_inodes:
            return _import_failure(
                "proposal_hardlink",
                "proposal.json shares an inode with a bundle file",
                policy_violation=True,
            )
        if before.st_size > cap:
            return _import_failure(
                "proposal_oversized",
                f"proposal.json exceeded {cap} bytes",
                policy_violation=False,
            )
        chunks: list[bytes] = []
        remaining = cap + 1
        while remaining > 0:
            chunk = os.read(proposal_fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > cap:
            return _import_failure(
                "proposal_oversized",
                f"proposal.json exceeded {cap} bytes",
                policy_violation=False,
            )
        after = os.fstat(proposal_fd)
        if (after.st_dev, after.st_ino, after.st_size) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
        ):
            return _import_failure(
                "proposal_unstable",
                "proposal.json changed while it was being read",
                policy_violation=True,
            )
    finally:
        os.close(proposal_fd)
    if not data:
        return _import_failure(
            "proposal_empty", "proposal.json is empty", policy_violation=False
        )
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return _import_failure(
            "proposal_not_utf8",
            "proposal.json is not strict UTF-8",
            policy_violation=False,
        )
    return ProposalImport(text=text)


def build_execution_inventory(
    execution_root: Path,
    policy: SubprocessPolicy,
    *,
    include_output: bool = True,
    after_breach: bool = False,
) -> tuple[ExecutionFileRecord, ...]:
    """Record the writable roots' entries without ever opening unsafe files.

    Safe regular files within the applicable size cap may be hashed. FIFOs,
    sockets, devices, symlinks and oversized files contribute type and
    relative path only (goal.md §6.9). The inventory is bounded; after a
    resource-limit breach the bound tightens because a complete inventory is
    no longer attempted (§7.10).
    """

    limit = (
        _INVENTORY_ENTRY_LIMIT_AFTER_BREACH
        if after_breach
        else _INVENTORY_ENTRY_LIMIT
    )
    records: list[ExecutionFileRecord] = []
    roots = WRITABLE_QUOTA_ROOTS if include_output else WRITABLE_QUOTA_ROOTS[1:]
    stack: list[tuple[Path, Path]] = []
    for root_name in roots:
        root_path = execution_root / root_name
        try:
            root_stat = os.lstat(root_path)
        except OSError:
            continue
        if stat.S_ISDIR(root_stat.st_mode):
            stack.append((root_path, Path(root_name)))
    while stack and len(records) < limit:
        directory, relative = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            if len(records) >= limit:
                break
            entry_relative = relative / entry.name
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            file_type = classify_mode(entry_stat.st_mode)
            if file_type == "directory":
                stack.append((Path(entry.path), entry_relative))
                records.append(
                    ExecutionFileRecord(
                        relative_path=entry_relative,
                        file_type=file_type,
                        size_bytes=None,
                        content_hash=None,
                    )
                )
                continue
            if file_type != "regular":
                records.append(
                    ExecutionFileRecord(
                        relative_path=entry_relative,
                        file_type=file_type,
                        size_bytes=None,
                        content_hash=None,
                    )
                )
                continue
            size_cap = policy.max_writable_single_file_bytes
            if entry_relative == Path("output") / PROPOSAL_FILENAME:
                size_cap = min(size_cap, policy.max_proposal_bytes)
            if entry_stat.st_size > size_cap:
                records.append(
                    ExecutionFileRecord(
                        relative_path=entry_relative,
                        file_type="oversized",
                        size_bytes=None,
                        content_hash=None,
                    )
                )
                continue
            records.append(
                ExecutionFileRecord(
                    relative_path=entry_relative,
                    file_type=file_type,
                    size_bytes=entry_stat.st_size,
                    content_hash=hash_file(Path(entry.path)),
                )
            )
    return tuple(records)
