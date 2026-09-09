"""Deterministic Package C runtime artifacts for synthetic engineering runs.

These records are not scientific models.  They bind an exact, already audited
canonical snapshot to byte-stable assembly output.  No text generation or
post-audit paraphrasing occurs here.
"""

from __future__ import annotations

import ctypes
import errno
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from pydantic import ConfigDict, Field, model_validator

from vibereview.enums import AuditProvenanceVerdict
from vibereview.ids import (
    ClaimId,
    ClaimPaperEvidenceId,
    PaperId,
    PropositionId,
    SentenceId,
    Sha256,
)
from vibereview.validators import derive_semantic_audit_disposition

from .hashing import canonical_json_bytes, hash_bytes
from .records import RuntimeModel
from .repository import GenerationStore
from .state import RepositorySnapshot


class PackageCArtifactError(ValueError):
    """An exact-assembly or artifact-integrity invariant was violated."""


class ArtifactModel(RuntimeModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AssemblyBudget(ArtifactModel):
    """Operator-selected engineering bounds; never evidence-quality thresholds."""

    max_sentences: int = Field(ge=1, le=1_000)
    max_citations: int = Field(ge=0, le=10_000)
    max_body_utf8_bytes: int = Field(ge=1, le=16 * 1024 * 1024)


class AssemblySentenceRecord(ArtifactModel):
    sentence_id: SentenceId
    text_hash: Sha256
    source_proposition_ids: tuple[PropositionId, ...]
    start_codepoint: int = Field(ge=0)
    end_codepoint: int = Field(gt=0)
    start_utf8_byte: int = Field(ge=0)
    end_utf8_byte: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered_ranges(self) -> "AssemblySentenceRecord":
        if self.end_codepoint <= self.start_codepoint:
            raise ValueError("sentence code-point range is empty")
        if self.end_utf8_byte <= self.start_utf8_byte:
            raise ValueError("sentence UTF-8 range is empty")
        return self


class AssemblyCitationRecord(ArtifactModel):
    sentence_id: SentenceId
    proposition_id: PropositionId
    paper_id: PaperId
    claim_id: ClaimId
    claim_paper_evidence_id: ClaimPaperEvidenceId


class AssemblyRecord(ArtifactModel):
    schema_version: Literal["package-c-assembly-1"] = "package-c-assembly-1"
    source_generation: int = Field(ge=0)
    repository_hash: Sha256
    budget: AssemblyBudget
    sentences: tuple[AssemblySentenceRecord, ...]
    citations: tuple[AssemblyCitationRecord, ...]
    body_hash: Sha256
    body_size_bytes: int = Field(ge=0)
    selection_policy: Literal["ALL_CURRENT_RENDERED_SENTENCES_NUMERIC_ID"] = (
        "ALL_CURRENT_RENDERED_SENTENCES_NUMERIC_ID"
    )

    @property
    def artifact_hash(self) -> Sha256:
        return hash_bytes(_model_file_bytes(self))


class PacketFileRecord(ArtifactModel):
    relative_path: Literal["section.md", "assembly.json", "repository.json"]
    size_bytes: int = Field(ge=0)
    content_hash: Sha256


class ReviewPacketManifest(ArtifactModel):
    schema_version: Literal["package-c-synthetic-review-packet-1"] = (
        "package-c-synthetic-review-packet-1"
    )
    synthetic_fixture: Literal[True] = True
    source_generation: int = Field(ge=0)
    repository_hash: Sha256
    assembly_artifact_hash: Sha256
    section_body_hash: Sha256
    files: tuple[PacketFileRecord, ...]
    human_review: Literal["NOT_PERFORMED"] = "NOT_PERFORMED"
    publication_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _closed_file_manifest(self) -> "ReviewPacketManifest":
        expected = ("assembly.json", "repository.json", "section.md")
        observed = tuple(sorted(item.relative_path for item in self.files))
        if observed != expected:
            raise ValueError(
                "review packet manifest must cover the exact payload file set"
            )
        return self

    @property
    def artifact_hash(self) -> Sha256:
        return hash_bytes(_model_file_bytes(self))


def _model_file_bytes(model: RuntimeModel) -> bytes:
    return canonical_json_bytes(model.model_dump(mode="json")) + b"\n"


def _sentence_number(sentence_id: str) -> int:
    match = re.fullmatch(r"RS([0-9]+)", sentence_id)
    if match is None:
        raise PackageCArtifactError(f"invalid canonical sentence ID {sentence_id!r}")
    return int(match.group(1))


def assemble_exact_section(
    snapshot: RepositorySnapshot,
    *,
    source_generation: int,
    budget: AssemblyBudget,
) -> tuple[bytes, AssemblyRecord]:
    """Assemble every current audited sentence with one trailing LF each."""

    snapshot.validate_repository()
    sentence_by_id = {item.sentence_id: item for item in snapshot.rendered_sentences}
    ordered_ids = tuple(sorted(sentence_by_id, key=_sentence_number))
    if not ordered_ids:
        raise PackageCArtifactError("exact assembly requires at least one sentence")
    if len(ordered_ids) > budget.max_sentences:
        raise PackageCArtifactError("assembly sentence count exceeds its explicit budget")

    sentence_audits: dict[str, list[object]] = {}
    for audit in snapshot.rendered_sentence_audits:
        sentence_audits.setdefault(audit.sentence_id, []).append(audit)
    proposition_by_id = {
        item.proposition_id: item for item in snapshot.proposition_records
    }
    semantic_audits: dict[str, list[object]] = {}
    for audit in snapshot.semantic_audits:
        semantic_audits.setdefault(audit.target_id, []).append(audit)

    body = bytearray()
    codepoint_cursor = 0
    sentence_records: list[AssemblySentenceRecord] = []
    citation_records: list[AssemblyCitationRecord] = []
    seen_citations: set[tuple[str, str, str, str, str]] = set()

    for sentence_id in ordered_ids:
        sentence = sentence_by_id[sentence_id]
        if any(marker in sentence.text for marker in ("\r", "\n", "\x00")):
            raise PackageCArtifactError(
                f"rendered sentence {sentence_id} contains a forbidden line marker"
            )
        audits = sentence_audits.get(sentence_id, [])
        if len(audits) != 1 or audits[0].verdict is not AuditProvenanceVerdict.ENTAILED:
            raise PackageCArtifactError(
                f"rendered sentence {sentence_id} lacks one ENTAILED audit"
            )

        encoded = sentence.text.encode("utf-8")
        start_byte = len(body)
        start_codepoint = codepoint_cursor
        body.extend(encoded)
        end_byte = len(body)
        end_codepoint = start_codepoint + len(sentence.text)
        body.extend(b"\n")
        codepoint_cursor = end_codepoint + 1

        proposition_ids = tuple(sentence.source_proposition_ids)
        if len(proposition_ids) != len(set(proposition_ids)):
            raise PackageCArtifactError(
                f"rendered sentence {sentence_id} repeats a source proposition"
            )
        for proposition_id in proposition_ids:
            proposition = proposition_by_id.get(proposition_id)
            if proposition is None:
                raise PackageCArtifactError(
                    f"rendered sentence {sentence_id} references unknown {proposition_id}"
                )
            proposition_audits = semantic_audits.get(proposition_id, [])
            if (
                len(proposition_audits) != 1
                or derive_semantic_audit_disposition(proposition_audits[0]).value
                != "PASS"
            ):
                raise PackageCArtifactError(
                    f"source proposition {proposition_id} lacks one passing audit"
                )
            for binding in proposition.citation_bindings:
                key = (
                    sentence_id,
                    proposition_id,
                    binding.paper_id,
                    binding.claim_id,
                    binding.claim_paper_evidence_id,
                )
                if key in seen_citations:
                    raise PackageCArtifactError(
                        f"duplicate citation binding in proposition {proposition_id}"
                    )
                seen_citations.add(key)
                citation_records.append(
                    AssemblyCitationRecord(
                        sentence_id=sentence_id,
                        proposition_id=proposition_id,
                        paper_id=binding.paper_id,
                        claim_id=binding.claim_id,
                        claim_paper_evidence_id=binding.claim_paper_evidence_id,
                    )
                )
                if len(citation_records) > budget.max_citations:
                    raise PackageCArtifactError(
                        "assembly citation count exceeds its explicit budget"
                    )

        sentence_records.append(
            AssemblySentenceRecord(
                sentence_id=sentence_id,
                text_hash=hash_bytes(encoded),
                source_proposition_ids=proposition_ids,
                start_codepoint=start_codepoint,
                end_codepoint=end_codepoint,
                start_utf8_byte=start_byte,
                end_utf8_byte=end_byte,
            )
        )

    body_bytes = bytes(body)
    if len(body_bytes) > budget.max_body_utf8_bytes:
        raise PackageCArtifactError("assembly body exceeds its explicit UTF-8 budget")
    citations = tuple(
        sorted(
            citation_records,
            key=lambda item: (
                _sentence_number(item.sentence_id),
                item.proposition_id,
                item.paper_id,
                item.claim_id,
                item.claim_paper_evidence_id,
            ),
        )
    )
    record = AssemblyRecord(
        source_generation=source_generation,
        repository_hash=snapshot.canonical_hash(),
        budget=budget,
        sentences=tuple(sentence_records),
        citations=citations,
        body_hash=hash_bytes(body_bytes),
        body_size_bytes=len(body_bytes),
    )
    return body_bytes, record


def verify_exact_assembly(
    snapshot: RepositorySnapshot,
    body: bytes,
    record: AssemblyRecord,
) -> None:
    """Rehydrate an assembly and require byte- and record-exact equality."""

    rebuilt_body, rebuilt_record = assemble_exact_section(
        snapshot,
        source_generation=record.source_generation,
        budget=record.budget,
    )
    if body != rebuilt_body:
        raise PackageCArtifactError("section body does not match canonical sentences")
    if record != rebuilt_record:
        raise PackageCArtifactError("assembly record does not match canonical sentences")


_RENAME_NOREPLACE = 1
_PACKET_FILES = frozenset(
    {"section.md", "assembly.json", "repository.json", "review_packet.json"}
)
_MAX_PACKET_FILE_BYTES = 32 * 1024 * 1024
_MAX_PACKET_TOTAL_BYTES = 64 * 1024 * 1024


def _rename_directory_noreplace_at(
    parent_descriptor: int,
    source_name: str,
    destination_name: str,
) -> None:
    """Publish one sibling directory atomically through a retained parent FD."""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOTSUP, "renameat2(RENAME_NOREPLACE) is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        parent_descriptor,
        os.fsencode(source_name),
        parent_descriptor,
        os.fsencode(destination_name),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), destination_name)
    raise OSError(error, os.strerror(error), destination_name)


def _same_directory(
    left: os.stat_result,
    right: os.stat_result,
) -> bool:
    return (
        stat.S_ISDIR(left.st_mode)
        and stat.S_ISDIR(right.st_mode)
        and (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)
    )


@dataclass(frozen=True, slots=True)
class _RetainedDirectoryPath:
    """A fully opened absolute directory path whose every name stays pinned."""

    path: Path
    description: str
    descriptors: tuple[int, ...]
    identities: tuple[os.stat_result, ...]
    bindings: tuple[tuple[int, str, os.stat_result], ...]

    @property
    def descriptor(self) -> int:
        return self.descriptors[-1]

    @property
    def identity(self) -> os.stat_result:
        return self.identities[-1]

    @property
    def identity_keys(self) -> frozenset[tuple[int, int]]:
        return frozenset((item.st_dev, item.st_ino) for item in self.identities)

    def verify(self) -> None:
        _verify_open_directory_path(
            Path(self.path.anchor),
            self.identities[0],
            description=f"{self.description} ancestry",
        )
        for parent_descriptor, name, identity in self.bindings:
            _verify_directory_entry_at(
                parent_descriptor,
                name,
                identity,
                description=self.description,
            )
        _verify_open_directory_path(
            self.path,
            self.identity,
            description=self.description,
        )

    def close(self) -> None:
        for descriptor in reversed(self.descriptors):
            os.close(descriptor)


def _retain_directory_path(path: Path, *, description: str) -> _RetainedDirectoryPath:
    """Resolve once, then retain every directory inode from ``/`` to ``path``."""

    resolved = path.resolve(strict=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptors: list[int] = []
    identities: list[os.stat_result] = []
    bindings: list[tuple[int, str, os.stat_result]] = []
    try:
        root_descriptor = os.open(resolved.anchor, flags)
        descriptors.append(root_descriptor)
        identities.append(os.fstat(root_descriptor))
        for component in resolved.parts[1:]:
            parent_descriptor = descriptors[-1]
            descriptor = os.open(component, flags, dir_fd=parent_descriptor)
            identity = os.fstat(descriptor)
            _verify_directory_entry_at(
                parent_descriptor,
                component,
                identity,
                description=description,
            )
            bindings.append((parent_descriptor, component, identity))
            descriptors.append(descriptor)
            identities.append(identity)
        retained = _RetainedDirectoryPath(
            path=resolved,
            description=description,
            descriptors=tuple(descriptors),
            identities=tuple(identities),
            bindings=tuple(bindings),
        )
        retained.verify()
        return retained
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _retained_paths_overlap(
    left: _RetainedDirectoryPath,
    right: _RetainedDirectoryPath,
) -> bool:
    left_key = (left.identity.st_dev, left.identity.st_ino)
    right_key = (right.identity.st_dev, right.identity.st_ino)
    return left_key in right.identity_keys or right_key in left.identity_keys


def _verify_open_directory_path(
    path: Path,
    opened: os.stat_result,
    *,
    description: str = "review packet parent directory",
) -> None:
    try:
        current = path.lstat()
    except OSError as exc:
        raise PackageCArtifactError(f"{description} changed") from exc
    if not _same_directory(opened, current):
        raise PackageCArtifactError(f"{description} changed")


def _verify_directory_entry_at(
    parent_descriptor: int,
    name: str,
    opened: os.stat_result,
    *,
    description: str,
) -> None:
    try:
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError as exc:
        raise PackageCArtifactError(f"{description} changed") from exc
    if not _same_directory(opened, current):
        raise PackageCArtifactError(f"{description} changed")


def _descriptor_directory_path(descriptor: int) -> Path:
    """Return the Linux path that resolves through one already-open directory FD."""

    return Path("/proc/self/fd") / str(descriptor)


def _open_directory_at(
    parent_descriptor: int,
    name: str,
    *,
    description: str,
) -> tuple[int, os.stat_result]:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_descriptor,
        )
    except OSError as exc:
        raise PackageCArtifactError(f"{description} contains an unsafe path") from exc
    try:
        identity = os.fstat(descriptor)
        _verify_directory_entry_at(
            parent_descriptor,
            name,
            identity,
            description=description,
        )
        return descriptor, identity
    except BaseException:
        os.close(descriptor)
        raise


def _write_packet_file_at(
    directory_descriptor: int,
    name: str,
    content: bytes,
    *,
    before_write: Callable[[], None] | None = None,
) -> None:
    if len(content) > _MAX_PACKET_FILE_BYTES:
        raise PackageCArtifactError(f"review packet file is too large: {name}")
    if before_write is not None:
        before_write()
    descriptor = os.open(
        name,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_CLOEXEC
        | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_descriptor,
    )
    try:
        if before_write is not None:
            before_write()
        view = memoryview(content)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:  # pragma: no cover - defensive POSIX write invariant
                raise OSError("short review packet write")
            written += count
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size != len(content)
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise PackageCArtifactError(f"review packet file changed: {name}")
    finally:
        os.close(descriptor)


def _bounded_packet_inventory(
    directory_descriptor: int,
) -> tuple[frozenset[str], bool]:
    """Read at most one entry beyond the only permitted packet inventory."""

    names: list[str] = []
    with os.scandir(directory_descriptor) as entries:
        for entry in entries:
            names.append(entry.name)
            if len(names) > len(_PACKET_FILES):
                return frozenset(names), True
    return frozenset(names), False


def _remove_closed_packet_at(
    parent_descriptor: int,
    name: str,
    expected_identity: os.stat_result,
) -> None:
    """Best-effort cleanup of the exact directory inode owned by this writer."""

    directory_descriptor: int | None = None
    try:
        directory_descriptor, observed_identity = _open_directory_at(
            parent_descriptor,
            name,
            description="owned review packet directory",
        )
        if not _same_directory(expected_identity, observed_identity):
            return
        observed, overflow = _bounded_packet_inventory(directory_descriptor)
        if overflow or not observed <= _PACKET_FILES:
            return
        for child in observed:
            info = os.stat(child, dir_fd=directory_descriptor, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return
        for child in observed:
            os.unlink(child, dir_fd=directory_descriptor)
        _verify_directory_entry_at(
            parent_descriptor,
            name,
            expected_identity,
            description="owned review packet directory",
        )
        os.rmdir(name, dir_fd=parent_descriptor)
    except (OSError, PackageCArtifactError):
        return
    finally:
        if directory_descriptor is not None:
            os.close(directory_descriptor)


@dataclass(frozen=True, slots=True)
class _RetainedPacketFile:
    """One bounded packet read whose inode stays pinned until verification ends."""

    name: str
    descriptor: int
    identity: os.stat_result
    content: bytes


def _read_packet_file(
    directory_descriptor: int,
    name: str,
) -> _RetainedPacketFile:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    retained = False
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > _MAX_PACKET_FILE_BYTES
        ):
            raise PackageCArtifactError(f"unsafe review packet file: {name}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, _MAX_PACKET_FILE_BYTES + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_PACKET_FILE_BYTES:
                raise PackageCArtifactError(f"review packet file is too large: {name}")
        current = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
            or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
            or after.st_nlink != 1
            or before.st_size != total
            or after.st_size != total
        ):
            raise PackageCArtifactError(f"review packet file changed while read: {name}")
        result = _RetainedPacketFile(
            name=name,
            descriptor=descriptor,
            identity=after,
            content=b"".join(chunks),
        )
        retained = True
        return result
    finally:
        if not retained:
            os.close(descriptor)


def _close_retained_packet_files(files: tuple[_RetainedPacketFile, ...]) -> None:
    for item in files:
        os.close(item.descriptor)


def _revalidate_retained_packet_file(
    directory_descriptor: int,
    item: _RetainedPacketFile,
) -> None:
    """Re-bind a packet name and its bytes after semantic verification."""

    try:
        before = os.fstat(item.descriptor)
        current = os.stat(
            item.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size != len(item.content)
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or current.st_size != len(item.content)
            or (before.st_dev, before.st_ino)
            != (item.identity.st_dev, item.identity.st_ino)
            or (current.st_dev, current.st_ino)
            != (item.identity.st_dev, item.identity.st_ino)
        ):
            raise PackageCArtifactError(
                f"review packet file changed after read: {item.name}"
            )

        os.lseek(item.descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                item.descriptor,
                min(1024 * 1024, _MAX_PACKET_FILE_BYTES + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_PACKET_FILE_BYTES:
                raise PackageCArtifactError(
                    f"review packet file is too large: {item.name}"
                )

        after = os.fstat(item.descriptor)
        rebound = os.stat(
            item.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            b"".join(chunks) != item.content
            or total != len(item.content)
            or not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or after.st_size != total
            or not stat.S_ISREG(rebound.st_mode)
            or rebound.st_nlink != 1
            or rebound.st_size != total
            or (after.st_dev, after.st_ino)
            != (item.identity.st_dev, item.identity.st_ino)
            or (rebound.st_dev, rebound.st_ino)
            != (item.identity.st_dev, item.identity.st_ino)
        ):
            raise PackageCArtifactError(
                f"review packet file changed after read: {item.name}"
            )
    except OSError as exc:
        raise PackageCArtifactError(
            f"review packet file changed after read: {item.name}"
        ) from exc


def _require_closed_packet_inventory(
    directory_descriptor: int,
    *,
    changed: bool,
) -> None:
    names, overflow = _bounded_packet_inventory(directory_descriptor)
    if overflow or names != _PACKET_FILES:
        qualifier = "changed" if changed else "differs"
        bounded_names = sorted(names)[: len(_PACKET_FILES) + 1]
        suffix = " (additional entries omitted)" if overflow else ""
        raise PackageCArtifactError(
            f"review packet file set {qualifier}: {bounded_names}{suffix}"
        )


def _read_closed_packet_from_open_directory(
    parent_descriptor: int,
    packet_name: str,
    directory_descriptor: int,
    directory_identity: os.stat_result,
) -> tuple[dict[str, bytes], tuple[_RetainedPacketFile, ...]]:
    _verify_directory_entry_at(
        parent_descriptor,
        packet_name,
        directory_identity,
        description="review packet directory",
    )
    _require_closed_packet_inventory(directory_descriptor, changed=False)
    retained: list[_RetainedPacketFile] = []
    try:
        for name in sorted(_PACKET_FILES):
            retained.append(_read_packet_file(directory_descriptor, name))
        values = {item.name: item.content for item in retained}
        _require_closed_packet_inventory(directory_descriptor, changed=True)
        if sum(len(value) for value in values.values()) > _MAX_PACKET_TOTAL_BYTES:
            raise PackageCArtifactError("review packet exceeds its total byte limit")
        _verify_directory_entry_at(
            parent_descriptor,
            packet_name,
            directory_identity,
            description="review packet directory",
        )
        return values, tuple(retained)
    except BaseException:
        _close_retained_packet_files(tuple(retained))
        raise


def _read_closed_packet_at(
    parent_descriptor: int,
    packet_name: str,
) -> dict[str, bytes]:
    directory_descriptor: int | None = None
    try:
        directory_descriptor, directory_identity = _open_directory_at(
            parent_descriptor,
            packet_name,
            description="review packet",
        )
        values, retained = _read_closed_packet_from_open_directory(
            parent_descriptor,
            packet_name,
            directory_descriptor,
            directory_identity,
        )
        try:
            return values
        finally:
            _close_retained_packet_files(retained)
    except OSError as exc:
        raise PackageCArtifactError("review packet contains an unsafe path") from exc
    finally:
        if directory_descriptor is not None:
            os.close(directory_descriptor)


def _read_closed_packet(packet_dir: Path) -> dict[str, bytes]:
    parent = packet_dir.parent.resolve(strict=True)
    if packet_dir.name in {"", ".", ".."}:
        raise PackageCArtifactError("review packet directory name is invalid")
    parent_descriptor = os.open(
        parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    )
    try:
        parent_identity = os.fstat(parent_descriptor)
        values = _read_closed_packet_at(parent_descriptor, packet_dir.name)
        _verify_open_directory_path(parent, parent_identity)
        return values
    except OSError as exc:
        raise PackageCArtifactError("review packet contains an unsafe path") from exc
    finally:
        os.close(parent_descriptor)


def _verify_packet_values(
    review_root: Path,
    values: dict[str, bytes],
) -> ReviewPacketManifest:
    try:
        manifest = ReviewPacketManifest.model_validate_json(
            values["review_packet.json"]
        )
        assembly = AssemblyRecord.model_validate_json(values["assembly.json"])
        exported_snapshot = RepositorySnapshot.model_validate_json(
            values["repository.json"]
        )
    except Exception as exc:
        raise PackageCArtifactError("review packet contains invalid JSON records") from exc
    if values["review_packet.json"] != _model_file_bytes(manifest):
        raise PackageCArtifactError("review packet manifest is not canonical JSON")
    if values["assembly.json"] != _model_file_bytes(assembly):
        raise PackageCArtifactError("assembly record is not canonical JSON")
    if values["repository.json"] != _model_file_bytes(exported_snapshot):
        raise PackageCArtifactError("repository dossier is not canonical JSON")

    file_records = {item.relative_path: item for item in manifest.files}
    for name in ("section.md", "assembly.json", "repository.json"):
        content = values[name]
        record = file_records[name]
        if record.size_bytes != len(content) or record.content_hash != hash_bytes(content):
            raise PackageCArtifactError(f"review packet digest mismatch: {name}")
    if (
        manifest.assembly_artifact_hash != hash_bytes(values["assembly.json"])
        or manifest.section_body_hash != hash_bytes(values["section.md"])
        or manifest.source_generation != assembly.source_generation
        or manifest.repository_hash != assembly.repository_hash
        or manifest.repository_hash != exported_snapshot.canonical_hash()
    ):
        raise PackageCArtifactError("review packet records disagree")

    try:
        source_snapshot, _ = GenerationStore(review_root).load_generation(
            manifest.source_generation
        )
    except Exception as exc:
        raise PackageCArtifactError("review packet source generation is unavailable") from exc
    if source_snapshot != exported_snapshot:
        raise PackageCArtifactError("review packet dossier differs from its source generation")
    verify_exact_assembly(source_snapshot, values["section.md"], assembly)
    return manifest


def _verify_open_packet_at(
    review_root: Path,
    parent_descriptor: int,
    packet_name: str,
    directory_descriptor: int,
    directory_identity: os.stat_result,
) -> ReviewPacketManifest:
    values, retained = _read_closed_packet_from_open_directory(
        parent_descriptor,
        packet_name,
        directory_descriptor,
        directory_identity,
    )
    try:
        manifest = _verify_packet_values(review_root, values)
        # Semantic verification reloads the source generation and can take longer
        # than the byte reads. Re-inventory, re-read every retained inode, and
        # re-bind every public name afterward so no stale byte snapshot succeeds.
        _require_closed_packet_inventory(directory_descriptor, changed=True)
        for item in retained:
            _revalidate_retained_packet_file(directory_descriptor, item)
        _require_closed_packet_inventory(directory_descriptor, changed=True)
        _verify_directory_entry_at(
            parent_descriptor,
            packet_name,
            directory_identity,
            description="review packet directory",
        )
        return manifest
    finally:
        _close_retained_packet_files(retained)


def verify_synthetic_review_packet(
    review_root: Path, packet_dir: Path
) -> ReviewPacketManifest:
    """Verify the closed packet and its exact immutable source generation."""

    review = review_root.resolve(strict=True)
    parent = packet_dir.parent.resolve(strict=True)
    if packet_dir.name in {"", ".", ".."}:
        raise PackageCArtifactError("review packet directory name is invalid")
    review_path: _RetainedDirectoryPath | None = None
    parent_path: _RetainedDirectoryPath | None = None
    packet_descriptor: int | None = None
    try:
        review_path = _retain_directory_path(
            review, description="review project root"
        )
        parent_path = _retain_directory_path(
            parent, description="review packet parent directory"
        )
        review_path.verify()
        parent_path.verify()
        packet_descriptor, packet_identity = _open_directory_at(
            parent_path.descriptor,
            packet_dir.name,
            description="review packet",
        )
        manifest = _verify_open_packet_at(
            _descriptor_directory_path(review_path.descriptor),
            parent_path.descriptor,
            packet_dir.name,
            packet_descriptor,
            packet_identity,
        )
        parent_path.verify()
        review_path.verify()
        _verify_directory_entry_at(
            parent_path.descriptor,
            packet_dir.name,
            packet_identity,
            description="review packet directory",
        )
        return manifest
    except OSError as exc:
        raise PackageCArtifactError("review packet contains an unsafe path") from exc
    finally:
        if packet_descriptor is not None:
            os.close(packet_descriptor)
        if parent_path is not None:
            parent_path.close()
        if review_path is not None:
            review_path.close()


def write_synthetic_review_packet(
    review_root: Path,
    output_dir: Path,
    *,
    public_repository_root: Path,
    body: bytes,
    assembly: AssemblyRecord,
) -> ReviewPacketManifest:
    """Write a generation-bound synthetic draft packet with atomic no-replace."""

    parent = output_dir.parent.resolve(strict=True)
    if output_dir.name in {"", ".", ".."}:
        raise PackageCArtifactError("review packet directory name is invalid")
    destination = parent / output_dir.name
    review = review_root.resolve(strict=True)
    public_repository = public_repository_root.resolve(strict=True)

    def overlaps(left: Path, right: Path) -> bool:
        return left == right or left in right.parents or right in left.parents

    if overlaps(review, public_repository):
        raise PackageCArtifactError(
            "synthetic review project must be outside the public repository"
        )
    if overlaps(destination, review) or overlaps(destination, public_repository):
        raise PackageCArtifactError(
            "synthetic review packet destination must be outside source repositories"
        )

    parent_path: _RetainedDirectoryPath | None = None
    review_path: _RetainedDirectoryPath | None = None
    public_path: _RetainedDirectoryPath | None = None
    existing_descriptor: int | None = None
    staging_name = f".package-c-staging-{uuid.uuid4().hex}"
    staging_descriptor: int | None = None
    staging_identity: os.stat_result | None = None
    staging_live = False
    published_owned = False
    accepted = False
    source_store: GenerationStore | None = None
    payloads: dict[str, bytes] = {}
    manifest: ReviewPacketManifest | None = None

    def require_retained_paths() -> tuple[
        _RetainedDirectoryPath,
        _RetainedDirectoryPath,
        _RetainedDirectoryPath,
    ]:
        if parent_path is None or review_path is None or public_path is None:
            raise AssertionError("review packet roots were not retained")
        return parent_path, review_path, public_path

    def verify_roots(destination_identity: os.stat_result | None = None) -> None:
        retained_parent, retained_review, retained_public = require_retained_paths()
        retained_parent.verify()
        retained_review.verify()
        retained_public.verify()
        if _retained_paths_overlap(retained_review, retained_public):
            raise PackageCArtifactError(
                "synthetic review project must be outside the public repository"
            )
        protected = (retained_review, retained_public)
        if any(
            (item.identity.st_dev, item.identity.st_ino)
            in retained_parent.identity_keys
            for item in protected
        ):
            raise PackageCArtifactError(
                "synthetic review packet destination must be outside source repositories"
            )
        if destination_identity is not None:
            destination_key = (
                destination_identity.st_dev,
                destination_identity.st_ino,
            )
            if any(
                destination_key == (item.identity.st_dev, item.identity.st_ino)
                or destination_key in item.identity_keys
                for item in protected
            ):
                raise PackageCArtifactError(
                    "synthetic review packet destination must be outside source repositories"
                )

    def verify_source_current() -> None:
        if source_store is None:
            raise AssertionError("review packet source store was not opened")
        verify_roots()
        try:
            current_generation = source_store.current_generation()
        except Exception as exc:
            raise PackageCArtifactError(
                "assembly source generation is unavailable"
            ) from exc
        if current_generation != assembly.source_generation:
            raise PackageCArtifactError(
                "assembly source generation is not CURRENT"
            )
        verify_roots()

    try:
        parent_path = _retain_directory_path(
            parent, description="review packet parent directory"
        )
        review_path = _retain_directory_path(
            review, description="review project root"
        )
        public_path = _retain_directory_path(
            public_repository, description="public repository root"
        )
        verify_roots()
        source_store = GenerationStore(
            _descriptor_directory_path(review_path.descriptor)
        )
        verify_source_current()
        try:
            snapshot, _ = source_store.load_generation(assembly.source_generation)
        except Exception as exc:
            raise PackageCArtifactError(
                "assembly source generation is unavailable"
            ) from exc
        verify_source_current()
        verify_exact_assembly(snapshot, body, assembly)

        assembly_bytes = _model_file_bytes(assembly)
        repository_bytes = _model_file_bytes(snapshot)
        payloads = {
            "section.md": body,
            "assembly.json": assembly_bytes,
            "repository.json": repository_bytes,
        }
        for name, content in payloads.items():
            if len(content) > _MAX_PACKET_FILE_BYTES:
                raise PackageCArtifactError(f"review packet file is too large: {name}")
        files = tuple(
            PacketFileRecord(
                relative_path=name,
                size_bytes=len(content),
                content_hash=hash_bytes(content),
            )
            for name, content in sorted(payloads.items())
        )
        manifest = ReviewPacketManifest(
            source_generation=assembly.source_generation,
            repository_hash=assembly.repository_hash,
            assembly_artifact_hash=hash_bytes(assembly_bytes),
            section_body_hash=assembly.body_hash,
            files=files,
        )
        payloads["review_packet.json"] = _model_file_bytes(manifest)
        if len(payloads["review_packet.json"]) > _MAX_PACKET_FILE_BYTES:
            raise PackageCArtifactError(
                "review packet file is too large: review_packet.json"
            )
        if sum(len(value) for value in payloads.values()) > _MAX_PACKET_TOTAL_BYTES:
            raise PackageCArtifactError("review packet exceeds its total byte limit")

        parent_descriptor = parent_path.descriptor
        review_descriptor = review_path.descriptor
        try:
            destination_info = os.stat(
                output_dir.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            destination_info = None
        if destination_info is not None:
            if not stat.S_ISDIR(destination_info.st_mode):
                raise FileExistsError(output_dir.name)
            existing_descriptor, existing_identity = _open_directory_at(
                parent_descriptor,
                output_dir.name,
                description="review packet",
            )
            if not _same_directory(destination_info, existing_identity):
                raise PackageCArtifactError(
                    "review packet directory changed before read"
                )
            verify_roots(existing_identity)
            observed = _verify_open_packet_at(
                _descriptor_directory_path(review_descriptor),
                parent_descriptor,
                output_dir.name,
                existing_descriptor,
                existing_identity,
            )
            if observed == manifest:
                os.fsync(parent_descriptor)
                verify_source_current()
                verify_roots(existing_identity)
                _verify_directory_entry_at(
                    parent_descriptor,
                    output_dir.name,
                    existing_identity,
                    description="review packet directory",
                )
                return manifest
            raise FileExistsError(output_dir.name)

        os.mkdir(staging_name, mode=0o700, dir_fd=parent_descriptor)
        staging_identity = os.stat(
            staging_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISDIR(staging_identity.st_mode):
            raise PackageCArtifactError(
                "review packet staging directory contains an unsafe path"
            )
        staging_live = True
        os.chmod(
            staging_name,
            0o700,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        _verify_directory_entry_at(
            parent_descriptor,
            staging_name,
            staging_identity,
            description="review packet staging directory",
        )
        staging_descriptor, opened_staging_identity = _open_directory_at(
            parent_descriptor,
            staging_name,
            description="review packet staging directory",
        )
        if not _same_directory(staging_identity, opened_staging_identity):
            raise PackageCArtifactError("review packet staging directory changed")

        def verify_staging() -> None:
            if staging_descriptor is None or staging_identity is None:
                raise AssertionError("review packet staging directory is unavailable")
            verify_source_current()
            current = os.fstat(staging_descriptor)
            if not _same_directory(staging_identity, current):
                raise PackageCArtifactError("review packet staging directory changed")
            _verify_directory_entry_at(
                parent_descriptor,
                staging_name,
                staging_identity,
                description="review packet staging directory",
            )

        for name, content in payloads.items():
            _write_packet_file_at(
                staging_descriptor,
                name,
                content,
                before_write=verify_staging,
            )
            verify_staging()
        os.fsync(staging_descriptor)
        _require_closed_packet_inventory(staging_descriptor, changed=False)
        verify_staging()
        try:
            _rename_directory_noreplace_at(
                parent_descriptor,
                staging_name,
                output_dir.name,
            )
        except FileExistsError as collision:
            try:
                existing_descriptor, existing_identity = _open_directory_at(
                    parent_descriptor,
                    output_dir.name,
                    description="review packet",
                )
                observed = _verify_open_packet_at(
                    _descriptor_directory_path(review_descriptor),
                    parent_descriptor,
                    output_dir.name,
                    existing_descriptor,
                    existing_identity,
                )
            except (OSError, ValueError) as exc:
                raise collision from exc
            if observed != manifest:
                raise collision
            verify_roots(existing_identity)
            _verify_directory_entry_at(
                parent_descriptor,
                output_dir.name,
                existing_identity,
                description="review packet directory",
            )
            os.fsync(parent_descriptor)
            verify_source_current()
            verify_roots(existing_identity)
            _verify_directory_entry_at(
                parent_descriptor,
                output_dir.name,
                existing_identity,
                description="review packet directory",
            )
            return manifest
        staging_live = False
        published_owned = True
        _verify_directory_entry_at(
            parent_descriptor,
            output_dir.name,
            staging_identity,
            description="published review packet directory",
        )
        verify_roots(staging_identity)
        verify_source_current()
        os.fsync(parent_descriptor)
        observed = _verify_open_packet_at(
            _descriptor_directory_path(review_descriptor),
            parent_descriptor,
            output_dir.name,
            staging_descriptor,
            staging_identity,
        )
        if observed != manifest:
            raise PackageCArtifactError("published review packet changed")
        _require_closed_packet_inventory(staging_descriptor, changed=True)
        verify_source_current()
        verify_roots(staging_identity)
        _verify_directory_entry_at(
            parent_descriptor,
            output_dir.name,
            staging_identity,
            description="published review packet directory",
        )
        accepted = True
        return manifest
    finally:
        if staging_live and staging_identity is not None:
            _remove_closed_packet_at(
                parent_path.descriptor,
                staging_name,
                staging_identity,
            )
        if published_owned and not accepted and staging_identity is not None:
            _remove_closed_packet_at(
                parent_path.descriptor,
                output_dir.name,
                staging_identity,
            )
            try:
                os.fsync(parent_path.descriptor)
            except OSError:
                pass
        if existing_descriptor is not None:
            os.close(existing_descriptor)
        if staging_descriptor is not None:
            os.close(staging_descriptor)
        if public_path is not None:
            public_path.close()
        if review_path is not None:
            review_path.close()
        if parent_path is not None:
            parent_path.close()


__all__ = [
    "AssemblyBudget",
    "AssemblyCitationRecord",
    "AssemblyRecord",
    "AssemblySentenceRecord",
    "PacketFileRecord",
    "PackageCArtifactError",
    "ReviewPacketManifest",
    "assemble_exact_section",
    "verify_exact_assembly",
    "verify_synthetic_review_packet",
    "write_synthetic_review_packet",
]
