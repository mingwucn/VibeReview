"""B2 deterministic subprocess execution boundary (goal.md §7).

Each attempt runs in a fresh temporary execution root outside the review
project (§7.2) prepared by an :class:`ExecutionBackend`. The engine process
is invoked as an argv list with a minimal allowlisted environment (§7.4-7.5),
its stdout/stderr are drained by two bounded, redacting reader threads
(§7.6), the writable quota roots are monitored while it runs (§7.10), the
process group is SIGTERM/SIGKILL-escalated on timeout (§7.11), the output
directory identity is re-verified after exit (§7.7), the proposal is imported
through the trusted output descriptor (§7.8), and the external execution root
is deleted after the safe artifacts are imported (§7.12).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from pydantic import ConfigDict, Field, field_validator, model_validator

from vibereview.ids import Sha256

from .applied_limits import AppliedResourceLimit
from .confinement import (
    ConfinementLevel,
    NetworkPolicy,
    allows_real_engine,
    build_bwrap_profile_argv,
)
from .credentials import (
    CredentialContext,
    CredentialLease,
    CredentialStagingPaths,
    EngineCredentialProvider,
    NullCredentialProvider,
)
from .diagnostics import DiagnosticCapture
from .execution_inventory import ExecutionFileRecord
from .hashing import hash_bytes, hash_file, hash_json
from .launcher_policy import LauncherPolicy
from .live_execution import (
    ExecutionQualificationGate,
    QualificationCheckpoint,
    QualificationEvidence,
    QualificationGateError,
    StoredSandboxQualificationGate,
)
from .output_policy import (
    build_execution_inventory,
    import_proposal,
    open_tracked_output_dir,
    scan_output_tree,
    verify_output_directory_identity,
)
from .records import (
    AgentResult,
    AgentTask,
    AttemptFailure,
    AttemptFailureStage,
    AttemptOutcome,
    ResourceLimitCode,
    RuntimeModel,
)
from .request_compiler import (
    CompiledEngineRequest,
    RequestCompilationPolicy,
    RequestSourceRecord,
    compile_engine_request,
)
from .resource_limits import (
    check_process_count,
    count_process_group_members,
    scan_writable_roots,
)
from .subprocess import (
    SubprocessPolicy,
    deterministic_test_policy,
    primary_attempt_outcome,
)
from . import credential_exec_shim, trusted_launcher


REDACTED_PLACEHOLDER = b"[REDACTED]"
_STREAM_CHUNK_BYTES = 65536
_READER_JOIN_SECONDS = 5.0
_TRUSTED_WORKER_INTERNAL_EXIT = 78
_LIVE_FACTORY_TOKEN = object()


class _SecretRedactor:
    """Exact-byte redaction with a carry-over window (goal.md §7.6).

    A trailing window of ``max secret byte length - 1`` bytes is held back so
    a secret split across chunk boundaries is still redacted. Replacement
    counting covers the entire stream; on an untruncated capture it equals
    the persisted placeholder count.
    """

    def __init__(self, secrets: tuple[bytes, ...]):
        self._secrets = tuple(secret for secret in secrets if secret)
        self._carry = max((len(secret) for secret in self._secrets), default=1) - 1
        self._pending = b""
        self.redactions_applied = 0

    def _redact(self, data: bytes) -> bytes:
        for secret in self._secrets:
            occurrences = data.count(secret)
            if occurrences:
                self.redactions_applied += occurrences
                data = data.replace(secret, REDACTED_PLACEHOLDER)
        return data

    def feed(self, chunk: bytes) -> bytes:
        data = self._pending + chunk
        cut = len(data) - self._carry
        if cut <= 0:
            self._pending = data
            return b""
        cut = self._pull_back_straddlers(data, cut)
        self._pending = data[cut:]
        return self._redact(data[:cut])

    def _pull_back_straddlers(self, data: bytes, cut: int) -> int:
        """Move the emit cut before any secret occurrence straddling it.

        Every occurrence starting before ``cut`` is complete in ``data``
        (because ``cut <= len(data) - max_len + 1``), so a straddler can only
        start inside a window of ``carry`` bytes before the cut. Pulling the
        cut back to the earliest straddler guarantees the emitted prefix never
        contains a partial or split secret; the pulled-back tail is redacted
        once later chunks complete it.
        """

        while True:
            window_start = max(0, cut - self._carry)
            straddler: int | None = None
            for secret in self._secrets:
                search_end = min(len(data), cut + len(secret) - 1)
                position = data.find(secret, window_start, search_end)
                while position != -1 and position < cut:
                    if position + len(secret) > cut:
                        straddler = (
                            position
                            if straddler is None
                            else min(straddler, position)
                        )
                        break
                    position = data.find(secret, position + 1, search_end)
            if straddler is None:
                return cut
            cut = straddler

    def flush(self) -> bytes:
        emit, self._pending = self._pending, b""
        return self._redact(emit)


class _BoundedRetainer:
    """Retains at most ``bound`` redacted bytes; anything more is truncated."""

    def __init__(self, bound: int):
        self.bound = bound
        self._parts: list[bytes] = []
        self.bytes_retained = 0
        self.truncated = False

    def feed(self, data: bytes) -> None:
        if not data:
            return
        room = self.bound - self.bytes_retained
        if room <= 0:
            self.truncated = True
            return
        if len(data) > room:
            self._parts.append(data[:room])
            self.bytes_retained += room
            self.truncated = True
            return
        self._parts.append(data)
        self.bytes_retained += len(data)

    def bytes(self) -> bytes:
        return b"".join(self._parts)


def _trim_to_utf8_bound(text: str, bound: int) -> str:
    parts: list[str] = []
    total = 0
    for character in text:
        size = len(character.encode("utf-8"))
        if total + size > bound:
            break
        parts.append(character)
        total += size
    return "".join(parts)


class _StreamCapture:
    """One bounded, redacting reader thread for one process stream (§7.6)."""

    def __init__(
        self,
        filename: str,
        stream: Any,
        max_bytes: int,
        secrets: tuple[bytes, ...],
    ):
        self._filename = filename
        self._stream = stream
        self._redactor = _SecretRedactor(secrets)
        self._retainer = _BoundedRetainer(max_bytes)
        self.bytes_observed = 0
        self._thread = threading.Thread(
            target=self._drain, name=f"vibereview-{filename}", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def _drain(self) -> None:
        descriptor = self._stream.fileno()
        while True:
            try:
                chunk = os.read(descriptor, _STREAM_CHUNK_BYTES)
            except OSError:
                break
            if not chunk:
                break
            self.bytes_observed += len(chunk)
            self._retainer.feed(self._redactor.feed(chunk))
        self._retainer.feed(self._redactor.flush())

    def join(self, timeout: float) -> None:
        self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def finish(self) -> tuple[DiagnosticCapture, str]:
        """Produce the capture contract and the exact persisted text.

        The kernel persists ``attempt/stdout.txt`` / ``attempt/stderr.txt``
        from the returned text, so the hash covers exactly the encoded bytes
        of that text (§6.8). If the UTF-8 replacement of undecodable bytes
        would grow the persisted file past the configured bound, the text is
        trimmed at a character boundary and marked truncated.
        """

        retained = self._retainer.bytes()
        text = retained.decode("utf-8", errors="replace")
        encoded = text.encode("utf-8")
        truncated = self._retainer.truncated
        if len(encoded) > self._retainer.bound:
            text = _trim_to_utf8_bound(text, self._retainer.bound)
            encoded = text.encode("utf-8")
            truncated = True
        capture = DiagnosticCapture(
            relative_path=Path("attempt") / self._filename,
            bytes_observed=self.bytes_observed,
            bytes_retained=len(encoded),
            truncated=truncated,
            retained_redacted_hash=hash_bytes(encoded),
            redactions_applied=self._redactor.redactions_applied,
        )
        return capture, text


def _empty_capture(filename: str) -> tuple[DiagnosticCapture, str]:
    capture = DiagnosticCapture(
        relative_path=Path("attempt") / filename,
        bytes_observed=0,
        bytes_retained=0,
        truncated=False,
        retained_redacted_hash=hash_bytes(b""),
        redactions_applied=0,
    )
    return capture, ""


class LauncherConfiguration(RuntimeModel):
    """Trusted launcher payload staged into ``launcher/`` by the backend.

    ``worker_config`` is written verbatim as ``launcher/worker_config.json``
    (§7.13); ``proposal_payloads`` stages canned files such as
    ``valid_proposal.json`` that the fake worker copies into ``output/``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_script: Path
    worker_script_sha256: Sha256 | None = None
    trusted_launcher_sha256: Sha256 | None = None
    credential_exec_shim_sha256: Sha256 | None = None
    worker_config: dict[str, Any] = Field(default_factory=dict)
    proposal_payloads: dict[str, bytes] = Field(default_factory=dict)
    assets: tuple["LauncherAsset", ...] = ()
    max_asset_bytes: int = Field(default=536_870_912, gt=0)
    max_total_asset_bytes: int = Field(default=1_073_741_824, gt=0)

    @field_validator("proposal_payloads")
    @classmethod
    def _payload_names_are_safe(cls, value: dict[str, bytes]) -> dict[str, bytes]:
        for name in value:
            if (
                not name
                or name in {
                    ".", "..", "worker_config.json", "compiled_request.json",
                    "trusted_launcher.py", "credential_exec_shim.py",
                }
                or "/" in name
                or "\\" in name
            ):
                raise ValueError(f"unsafe launcher payload name {name!r}")
        return value


class LauncherAsset(RuntimeModel):
    """One pinned regular file copied into the read-only launcher tree."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_path: Path = Field(repr=False, exclude=True)
    relative_path: Path
    expected_sha256: Sha256
    executable: bool = False
    internal_trusted: bool = Field(default=False, exclude=True, repr=False)

    @field_validator("relative_path")
    @classmethod
    def _relative_path_is_safe(cls, value: Path) -> Path:
        if value.is_absolute() or ".." in value.parts or not value.parts:
            raise ValueError("launcher asset path must be safe and relative")
        return value

    @model_validator(mode="after")
    def _does_not_collide(self) -> "LauncherAsset":
        if not self.internal_trusted and self.relative_path.parts[0] in {
            "worker_config.json",
            "compiled_request.json",
            "trusted_launcher.py",
            "credential_exec_shim.py",
        }:
            raise ValueError("launcher asset path collides with a trusted file")
        return self


def _snapshot_launcher_configuration(
    launcher: LauncherConfiguration,
) -> LauncherConfiguration:
    """Revalidate a detached copy of every mutable launcher component."""

    return LauncherConfiguration(
        worker_script=launcher.worker_script,
        worker_script_sha256=launcher.worker_script_sha256,
        trusted_launcher_sha256=launcher.trusted_launcher_sha256,
        credential_exec_shim_sha256=launcher.credential_exec_shim_sha256,
        worker_config=copy.deepcopy(launcher.worker_config),
        proposal_payloads=copy.deepcopy(launcher.proposal_payloads),
        assets=tuple(
            LauncherAsset(
                source_path=asset.source_path,
                relative_path=asset.relative_path,
                expected_sha256=asset.expected_sha256,
                executable=asset.executable,
                internal_trusted=asset.internal_trusted,
            )
            for asset in launcher.assets
        ),
        max_asset_bytes=launcher.max_asset_bytes,
        max_total_asset_bytes=launcher.max_total_asset_bytes,
    )


class TrustedStagingError(ValueError):
    """A pinned trusted launcher input could not be copied exactly."""


def _copy_launcher_asset(
    asset: LauncherAsset,
    launcher_dir: Path,
    *,
    max_bytes: int,
) -> int:
    source = asset.source_path.resolve(strict=True)
    source_lstat = os.lstat(asset.source_path)
    if stat.S_ISLNK(source_lstat.st_mode) or not stat.S_ISREG(source_lstat.st_mode):
        raise TrustedStagingError(f"launcher asset source is not a regular non-symlink: {asset.relative_path}")
    if source_lstat.st_size > max_bytes:
        raise TrustedStagingError(f"launcher asset exceeds byte bound: {asset.relative_path}")
    target = launcher_dir / asset.relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = target.parent.resolve()
    launcher_root = launcher_dir.resolve()
    if resolved_parent != launcher_root and launcher_root not in resolved_parent.parents:
        raise TrustedStagingError(f"launcher asset escapes launcher root: {asset.relative_path}")
    source_fd = os.open(
        source,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        target_fd = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o500 if asset.executable else 0o400,
        )
    except BaseException:
        os.close(source_fd)
        raise
    digest = hashlib.sha256()
    observed = 0
    try:
        opened = os.fstat(source_fd)
        if (opened.st_dev, opened.st_ino) != (source_lstat.st_dev, source_lstat.st_ino):
            raise TrustedStagingError(f"launcher asset changed while opening: {asset.relative_path}")
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            observed += len(chunk)
            if observed > max_bytes:
                raise TrustedStagingError(f"launcher asset exceeds byte bound: {asset.relative_path}")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(target_fd, view)
                view = view[written:]
        after = os.fstat(source_fd)
        if (
            observed != opened.st_size
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        ):
            raise TrustedStagingError(f"launcher asset changed while copying: {asset.relative_path}")
        actual = f"sha256:{digest.hexdigest()}"
        if actual != asset.expected_sha256:
            raise TrustedStagingError(f"launcher asset hash mismatch: {asset.relative_path}")
        os.fsync(target_fd)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        os.close(source_fd)
        os.close(target_fd)
    os.chmod(target, 0o500 if asset.executable else 0o400)
    return observed


def _snapshot_bundle(
    bundle_dir: Path,
) -> tuple[dict[str, str], frozenset[tuple[int, int]]]:
    """Hash and inode-register every regular file in the exec-root bundle."""

    expected: dict[str, str] = {}
    inodes: set[tuple[int, int]] = set()
    stack: list[Path] = [bundle_dir]
    while stack:
        directory = stack.pop()
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            entry_path = Path(entry.path)
            entry_stat = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(entry_stat.st_mode):
                raise ValueError(
                    f"unexpected symlink in execution bundle copy: {entry_path}"
                )
            if stat.S_ISDIR(entry_stat.st_mode):
                stack.append(entry_path)
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                raise ValueError(
                    f"unexpected special file in execution bundle copy: {entry_path}"
                )
            relative = entry_path.relative_to(bundle_dir).as_posix()
            expected[relative] = hash_file(entry_path)
            inodes.add((entry_stat.st_dev, entry_stat.st_ino))
    return expected, frozenset(inodes)


def _build_environment(
    execution_root: Path, policy: SubprocessPolicy, credentials: CredentialContext
) -> dict[str, str]:
    """Minimal allowlisted environment (goal.md §7.5).

    The complete parent environment is never copied. Runtime-controlled task
    paths are assigned last so credential or inherited variables cannot
    shadow them.
    """

    environment: dict[str, str] = {}
    for name in policy.inherited_environment_allowlist:
        value = os.environ.get(name)
        if value is not None:
            environment[name] = value
    for key, value in credentials.public_env.items():
        environment[key] = value
    for key, secret in credentials.secret_env.items():
        environment[key] = secret.get_secret_value()
    for key, value in environment.items():
        if not key or "=" in key or "\x00" in key or "\x00" in value:
            raise ValueError(f"unsafe environment entry {key!r}")
    home_dir = execution_root / "home"
    environment["HOME"] = str(home_dir)
    environment["XDG_CONFIG_HOME"] = str(home_dir / ".config")
    environment["XDG_CACHE_HOME"] = str(home_dir / ".cache")
    environment["TMPDIR"] = str(execution_root / "tmp")
    return environment


def _build_live_supervisor_environment(
    execution_root: Path, policy: SubprocessPolicy
) -> dict[str, str]:
    """Fixed nonsecret environment for trusted-launcher/Bubblewrap only."""

    del policy
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    home_dir = execution_root / "home"
    environment.update(
        {
            "HOME": str(home_dir),
            "XDG_CONFIG_HOME": str(home_dir / ".config"),
            "XDG_CACHE_HOME": str(home_dir / ".cache"),
            "TMPDIR": str(execution_root / "tmp"),
        }
    )
    return environment


class ExecutionSession:
    """A prepared execution root and its trusted output descriptor (§7.2/§7.7)."""

    def __init__(
        self,
        *,
        root: Path,
        policy: SubprocessPolicy,
        credentials: CredentialContext,
        environment: dict[str, str],
        worker_name: str,
        output_fd: int | None,
        trusted_output_stat: os.stat_result,
        expected_bundle_files: dict[str, str],
        bundle_inodes: frozenset[tuple[int, int]],
    ):
        self.root = root
        self.policy = policy
        self.credentials = credentials
        self.environment = environment
        self.worker_name = worker_name
        self.output_fd = output_fd
        self.trusted_output_stat = trusted_output_stat
        self.expected_bundle_files = expected_bundle_files
        self.bundle_inodes = bundle_inodes
        self.compiled_request: CompiledEngineRequest | None = None
        self.qualification_checks: list[QualificationEvidence] = []
        self.live_execution = False
        self.pass_fds: tuple[int, ...] = ()
        self.pinned_backend_fd: int | None = None
        self.pinned_backend_stat: tuple[int, int, int, int] | None = None
        self.pinned_backend_hash: Sha256 | None = None
        self.pinned_backend_identity: str | None = None
        self.qualified_network_policy: NetworkPolicy | None = None

    @property
    def bundle_dir(self) -> Path:
        return self.root / "bundle"

    @property
    def output_dir(self) -> Path:
        return self.root / "output"

    @property
    def launcher_dir(self) -> Path:
        return self.root / "launcher"

    @property
    def credentials_dir(self) -> Path:
        return self.root / "credentials"

    @property
    def home_dir(self) -> Path:
        return self.root / "home"

    @property
    def scratch_dir(self) -> Path:
        return self.root / "scratch"

    @property
    def tmp_dir(self) -> Path:
        return self.root / "tmp"

    def attach_credentials(
        self, credentials: CredentialContext, *, live_supervisor: bool = False
    ) -> None:
        self.credentials = credentials
        self.environment = (
            _build_live_supervisor_environment(self.root, self.policy)
            if live_supervisor
            else _build_environment(self.root, self.policy, credentials)
        )

    def stage_compiled_request(self, request: CompiledEngineRequest) -> Path:
        """Stage one trusted, task-specific request without mutating bundle/."""

        target = self.launcher_dir / "compiled_request.json"
        if target.exists() or target.is_symlink():
            raise RuntimeError("compiled request launcher payload already exists")
        raw = request.worker_payload()
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o400,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        self.compiled_request = request
        return target

    def cleanup(self) -> None:
        """Close and remove the execution root, or fail if absence is unproved."""

        errors: list[str] = []
        if self.pinned_backend_fd is not None:
            try:
                os.close(self.pinned_backend_fd)
            except OSError as exc:
                errors.append(f"pinned backend descriptor close failed: {exc}")
            finally:
                self.pinned_backend_fd = None
                self.pass_fds = ()
        if self.output_fd is not None:
            try:
                os.close(self.output_fd)
            except OSError as exc:
                errors.append(f"output descriptor close failed: {exc}")
            finally:
                self.output_fd = None
        try:
            shutil.rmtree(self.root)
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append(f"execution root removal failed: {exc}")
        if os.path.lexists(self.root):
            errors.append("execution root still exists after cleanup")
        if errors:
            raise RuntimeError("; ".join(errors))


class ProcessQuiescenceResult(RuntimeModel):
    """Result of backend-aware process quiescence (goal.md §4.2)."""

    backend_method: str

    descendants_detected: int | None
    sigterm_sent: bool
    sigkill_sent: bool

    descendants_remaining: int | None
    quiescent: bool

    diagnostic: str | None = None


class ExecutionBackend(Protocol):
    """Generic execution backend (goal.md §7.3, §4.2)."""

    @property
    def confinement_level(self) -> ConfinementLevel: ...

    def prepare(
        self,
        task: AgentTask,
        policy: SubprocessPolicy,
        credentials: CredentialContext | None = None,
    ) -> ExecutionSession: ...

    def quiesce(
        self,
        session: ExecutionSession,
        process: subprocess.Popen[bytes],
        *,
        grace_seconds: float,
    ) -> ProcessQuiescenceResult: ...

    def wrap_command(
        self,
        session: ExecutionSession,
        cmd: list[str],
    ) -> list[str]: ...


class TemporaryWorkspaceBackend:
    """TEST_ONLY temporary-directory backend (goal.md §7.3).

    ``allows_real_engine(ConfinementLevel.TEST_ONLY)`` is false, so this
    backend can never enable a real engine (§6.10, §8.3); it is accepted only
    for fake-worker tests.
    """

    confinement_level = ConfinementLevel.TEST_ONLY

    def __init__(
        self,
        launcher: LauncherConfiguration,
        *,
        execution_root_parent: Path | None = None,
    ):
        self._launcher = _snapshot_launcher_configuration(launcher)
        self._execution_root_parent = execution_root_parent

    def prepare(
        self,
        task: AgentTask,
        policy: SubprocessPolicy,
        credentials: CredentialContext | None = None,
    ) -> ExecutionSession:
        parent = self._execution_root_parent
        if parent is not None:
            parent.mkdir(parents=True, exist_ok=True)
        root = Path(
            tempfile.mkdtemp(prefix="vibereview-exec-", dir=parent)
        ).resolve()
        try:
            bundle_dir = root / "bundle"
            output_dir = root / "output"
            scratch_dir = root / "scratch"
            home_dir = root / "home"
            tmp_dir = root / "tmp"
            credentials_dir = root / "credentials"
            launcher_dir = root / "launcher"
            for directory in (
                output_dir,
                scratch_dir,
                home_dir,
                tmp_dir,
                launcher_dir,
            ):
                directory.mkdir()
            credentials_dir.mkdir(mode=0o700)
            os.chmod(credentials_dir, 0o700)
            (home_dir / ".config").mkdir()
            (home_dir / ".cache").mkdir()

            shutil.copytree(task.workspace_dir, bundle_dir)
            expected, inodes = _snapshot_bundle(bundle_dir)

            worker_name = self._launcher.worker_script.name
            if (
                not worker_name
                or worker_name in {
                    ".", "..", "worker_config.json", "compiled_request.json",
                    "trusted_launcher.py", "credential_exec_shim.py",
                }
                or worker_name in self._launcher.proposal_payloads
            ):
                raise ValueError(f"unsafe launcher worker name {worker_name!r}")
            worker_expected = self._launcher.worker_script_sha256 or Sha256(
                hash_file(self._launcher.worker_script)
            )
            _copy_launcher_asset(
                LauncherAsset(
                    source_path=self._launcher.worker_script,
                    relative_path=Path(worker_name),
                    expected_sha256=worker_expected,
                    executable=True,
                    internal_trusted=True,
                ),
                launcher_dir,
                max_bytes=self._launcher.max_asset_bytes,
            )
            worker_config_text = (
                json.dumps(
                    self._launcher.worker_config,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            )
            (launcher_dir / "worker_config.json").write_text(
                worker_config_text, encoding="utf-8"
            )
            for payload_name, payload_bytes in sorted(
                self._launcher.proposal_payloads.items()
            ):
                (launcher_dir / payload_name).write_bytes(payload_bytes)

            asset_paths: set[Path] = set()
            total_asset_bytes = 0
            for asset in self._launcher.assets:
                if asset.relative_path in asset_paths:
                    raise ValueError(
                        f"duplicate launcher asset path {asset.relative_path.as_posix()}"
                    )
                asset_paths.add(asset.relative_path)
                total_asset_bytes += _copy_launcher_asset(
                    asset,
                    launcher_dir,
                    max_bytes=self._launcher.max_asset_bytes,
                )
                if total_asset_bytes > self._launcher.max_total_asset_bytes:
                    raise ValueError("total launcher-asset byte bound exceeded")

            trusted_launcher_src = Path(__file__).parent / "trusted_launcher.py"
            trusted_launcher_expected = (
                self._launcher.trusted_launcher_sha256
                or Sha256(hash_file(trusted_launcher_src))
            )
            _copy_launcher_asset(
                LauncherAsset(
                    source_path=trusted_launcher_src,
                    relative_path=Path("trusted_launcher.py"),
                    expected_sha256=trusted_launcher_expected,
                    executable=True,
                    internal_trusted=True,
                ),
                launcher_dir,
                max_bytes=self._launcher.max_asset_bytes,
            )
            credential_shim_src = Path(credential_exec_shim.__file__).resolve()
            credential_shim_expected = (
                self._launcher.credential_exec_shim_sha256
                or Sha256(hash_file(credential_shim_src))
            )
            _copy_launcher_asset(
                LauncherAsset(
                    source_path=credential_shim_src,
                    relative_path=Path("credential_exec_shim.py"),
                    expected_sha256=credential_shim_expected,
                    executable=True,
                    internal_trusted=True,
                ),
                launcher_dir,
                max_bytes=self._launcher.max_asset_bytes,
            )

            if credentials is None:
                credentials = CredentialContext(
                    public_env={},
                    secret_env={},
                    ephemeral_files=(),
                    exact_redaction_values=(),
                    provider_id="null",
                    nonsecret_configuration_fingerprint=Sha256("sha256:" + "0" * 64),
                )
            else:
                for source in credentials.ephemeral_files:
                    target = credentials_dir / source.name
                    if not target.exists():
                        shutil.copyfile(source, target)
                    os.chmod(target, 0o600)

            environment = _build_environment(root, policy, credentials)
            output_fd, trusted_stat = open_tracked_output_dir(output_dir)
            return ExecutionSession(
                root=root,
                policy=policy,
                credentials=credentials,
                environment=environment,
                worker_name=worker_name,
                output_fd=output_fd,
                trusted_output_stat=trusted_stat,
                expected_bundle_files=expected,
                bundle_inodes=inodes,
            )
        except BaseException as original:
            cleanup_error: BaseException | None = None
            try:
                shutil.rmtree(root)
            except FileNotFoundError:
                pass
            except BaseException as caught:
                cleanup_error = caught
            if os.path.lexists(root) and cleanup_error is None:
                cleanup_error = RuntimeError(
                    "execution root still exists after failed preparation cleanup"
                )
            if cleanup_error is not None:
                raise RuntimeError(
                    f"execution root preparation and cleanup failed: {cleanup_error}"
                ) from original
            raise

    def quiesce(
        self,
        session: ExecutionSession,
        process: subprocess.Popen[bytes],
        *,
        grace_seconds: float,
    ) -> ProcessQuiescenceResult:
        """Identify original process group, count members, terminate with grace, and verify (goal.md §4.3)."""
        pgid = process.pid
        try:
            initial_count = count_process_group_members(pgid)
        except Exception as exc:
            return ProcessQuiescenceResult(
                backend_method="process_group",
                descendants_detected=None,
                sigterm_sent=False,
                sigkill_sent=False,
                descendants_remaining=None,
                quiescent=False,
                diagnostic=f"quiescence inspection failed internally: {exc}",
            )

        if initial_count is None:
            return ProcessQuiescenceResult(
                backend_method="process_group",
                descendants_detected=None,
                sigterm_sent=False,
                sigkill_sent=False,
                descendants_remaining=None,
                quiescent=False,
                diagnostic="quiescence inspection failed internally: /proc unavailable",
            )

        if initial_count == 0:
            return ProcessQuiescenceResult(
                backend_method="process_group",
                descendants_detected=0,
                sigterm_sent=False,
                sigkill_sent=False,
                descendants_remaining=0,
                quiescent=True,
            )

        descendants_detected = initial_count
        sigterm_sent = True
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

        deadline = time.monotonic() + max(grace_seconds, 0.0)
        remaining: int | None = initial_count
        while time.monotonic() < deadline:
            try:
                remaining = count_process_group_members(pgid)
            except Exception:
                remaining = None
                break
            if remaining == 0:
                break
            time.sleep(0.05)

        sigkill_sent = False
        if remaining is None or remaining > 0:
            sigkill_sent = True
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            kill_deadline = time.monotonic() + 1.0
            while time.monotonic() < kill_deadline:
                try:
                    remaining = count_process_group_members(pgid)
                except Exception:
                    remaining = None
                    break
                if remaining == 0:
                    break
                time.sleep(0.05)

        quiescent = (remaining == 0)
        return ProcessQuiescenceResult(
            backend_method="process_group",
            descendants_detected=descendants_detected,
            sigterm_sent=sigterm_sent,
            sigkill_sent=sigkill_sent,
            descendants_remaining=remaining,
            quiescent=quiescent,
            diagnostic=None if quiescent else f"descendants remaining: {remaining}",
        )

    def wrap_command(
        self,
        session: ExecutionSession,
        cmd: list[str],
    ) -> list[str]:
        return list(cmd)


class BubblewrapExecutionBackend:
    """Linux OS sandbox backend using Bubblewrap (goal.md §8)."""

    confinement_level = ConfinementLevel.OS_SANDBOX

    def __init__(
        self,
        launcher: LauncherConfiguration,
        *,
        bwrap_path: Path | str | None = None,
        network_policy: NetworkPolicy = NetworkPolicy.DENY,
        execution_root_parent: Path | None = None,
    ):
        self._launcher = _snapshot_launcher_configuration(launcher)
        # Resolution is intentionally deferred until ``prepare``.  ProjectRuntime
        # checks an accepted receipt before calling ``engine.execute``, so replay
        # stays zero-cost even when this host no longer has Bubblewrap installed.
        self._bwrap_candidate = Path(bwrap_path) if bwrap_path is not None else None
        self._bwrap_path: Path | None = None
        self._network_policy = network_policy
        self._execution_root_parent = execution_root_parent
        self._temporary_backend = TemporaryWorkspaceBackend(
            launcher, execution_root_parent=execution_root_parent
        )

    @property
    def bwrap_path(self) -> Path:
        if self._bwrap_path is None:
            candidate = self._bwrap_candidate
            if candidate is None:
                found = shutil.which("bwrap")
                if found is None:
                    raise RuntimeError(
                        "bwrap executable not found; OS_SANDBOX requires bubblewrap"
                    )
                candidate = Path(found)
            resolved = candidate.resolve()
            if not resolved.is_file():
                raise RuntimeError(f"bwrap executable does not exist: {resolved}")
            self._bwrap_path = resolved
        return self._bwrap_path

    @property
    def network_policy(self) -> NetworkPolicy:
        return self._network_policy

    def prepare(
        self,
        task: AgentTask,
        policy: SubprocessPolicy,
        credentials: CredentialContext | None = None,
    ) -> ExecutionSession:
        # Resolve the confinement executable only after receipt lookup has missed.
        source = self.bwrap_path
        session = self._temporary_backend.prepare(task, policy, credentials)
        descriptor: int | None = None
        try:
            item = os.lstat(source)
            if (
                not stat.S_ISREG(item.st_mode)
                or not stat.S_IMODE(item.st_mode) & 0o111
            ):
                raise RuntimeError("bwrap executable is not a regular executable")
            descriptor = os.open(
                source,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            opened = os.fstat(descriptor)
            if (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
            ) != (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns):
                raise RuntimeError("bwrap executable changed while pinning")
            pinned_path = Path(f"/proc/self/fd/{descriptor}")
            pinned_hash = Sha256(hash_file(pinned_path))
            after = os.fstat(descriptor)
            if (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ) != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
                raise RuntimeError("bwrap executable changed while hashing")
            os.set_inheritable(descriptor, True)
            session.pinned_backend_fd = descriptor
            session.pass_fds = (descriptor,)
            session.pinned_backend_stat = (
                opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns
            )
            session.pinned_backend_hash = pinned_hash
            session.pinned_backend_identity = str(source)
            descriptor = None
            return session
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            session.cleanup()
            raise

    def quiesce(
        self,
        session: ExecutionSession,
        process: subprocess.Popen[bytes],
        *,
        grace_seconds: float,
    ) -> ProcessQuiescenceResult:
        return self._temporary_backend.quiesce(session, process, grace_seconds=grace_seconds)

    def wrap_command(
        self,
        session: ExecutionSession,
        cmd: list[str],
    ) -> list[str]:
        # Remap the inner worker script to /work/launcher/<worker_name>
        inner_worker = f"/work/launcher/{session.worker_name}"
        inner_argv = [sys.executable, inner_worker, *cmd[2:]]
        if session.credentials.fixed_file_name is not None:
            shim_argv = [
                sys.executable,
                "/work/launcher/credential_exec_shim.py",
                "--credential-name",
                session.credentials.fixed_file_name,
            ]
            if session.credentials.home_profile_relative_path is not None:
                shim_argv.extend(
                    [
                        "--home-target",
                        session.credentials.home_profile_relative_path.as_posix(),
                    ]
                )
            if session.credentials.credential_env_name is not None:
                shim_argv.extend(
                    ["--credential-env", session.credentials.credential_env_name]
                )
            inner_argv = [*shim_argv, "--", *inner_argv]
        bwrap_executable: Path | str = self.bwrap_path
        if session.pinned_backend_fd is not None:
            current = os.fstat(session.pinned_backend_fd)
            current_tuple = (
                current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns
            )
            if current_tuple != session.pinned_backend_stat:
                raise RuntimeError("pinned bwrap executable changed before launch")
            actual_hash = hash_file(Path(f"/proc/self/fd/{session.pinned_backend_fd}"))
            if actual_hash != session.pinned_backend_hash:
                raise RuntimeError("pinned bwrap executable hash changed before launch")
            if session.live_execution:
                if not session.qualification_checks:
                    raise RuntimeError("live bwrap launch lacks qualification evidence")
                evidence = session.qualification_checks[-1]
                if (
                    evidence.checkpoint is not QualificationCheckpoint.PRE_LAUNCH
                    or evidence.backend_executable_identity
                    != session.pinned_backend_identity
                    or evidence.backend_executable_hash != session.pinned_backend_hash
                ):
                    raise RuntimeError(
                        "qualified executable does not match the pinned bwrap launch"
                    )
            bwrap_executable = f"/proc/self/fd/{session.pinned_backend_fd}"
        network_policy = self._network_policy
        if session.live_execution:
            if session.qualified_network_policy is None:
                raise RuntimeError("live bwrap launch lacks a qualified network policy")
            if self._network_policy is not session.qualified_network_policy:
                raise RuntimeError("backend network policy changed after qualification")
            network_policy = session.qualified_network_policy
        return build_bwrap_profile_argv(
            bwrap_executable,
            network_policy,
            bundle_dir=session.bundle_dir,
            launcher_dir=session.launcher_dir,
            output_dir=session.output_dir,
            scratch_dir=session.scratch_dir,
            home_dir=session.home_dir,
            tmp_dir=session.tmp_dir,
            credentials_dir=session.credentials_dir,
            inner_argv=inner_argv,
        )


def _verify_execution_bundle(session: ExecutionSession) -> AttemptFailure | None:
    """A9-equivalent check of the exec-root bundle after process exit."""

    actual: dict[str, str] = {}
    stack: list[Path] = [session.bundle_dir]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            return AttemptFailure(
                code="bundle_integrity_mismatch",
                stage=AttemptFailureStage.WORKSPACE,
                message=f"execution bundle cannot be scanned: {exc}",
                relative_path=Path("bundle"),
            )
        for entry in entries:
            entry_path = Path(entry.path)
            relative = entry_path.relative_to(session.root)
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(entry_stat.st_mode):
                return AttemptFailure(
                    code="bundle_integrity_mismatch",
                    stage=AttemptFailureStage.WORKSPACE,
                    message=f"unexpected symlink in bundle: {relative.as_posix()}",
                    relative_path=relative,
                )
            if stat.S_ISDIR(entry_stat.st_mode):
                stack.append(entry_path)
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                return AttemptFailure(
                    code="bundle_integrity_mismatch",
                    stage=AttemptFailureStage.WORKSPACE,
                    message=f"unexpected special file in bundle: {relative.as_posix()}",
                    relative_path=relative,
                )
            actual[entry_path.relative_to(session.bundle_dir).as_posix()] = hash_file(
                entry_path
            )
    expected = session.expected_bundle_files
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(
        key for key in expected.keys() & actual.keys() if expected[key] != actual[key]
    )
    if missing or extra or changed:
        return AttemptFailure(
            code="bundle_integrity_mismatch",
            stage=AttemptFailureStage.WORKSPACE,
            message=(
                f"bundle integrity mismatch under {session.bundle_dir}: "
                f"missing={missing} extra={extra} changed={changed}"
            ),
            relative_path=Path("bundle"),
        )
    return None


def _signal_group(process: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _terminate_with_grace(process: subprocess.Popen, grace_seconds: float) -> int:
    """SIGTERM the process group, then SIGKILL after the grace window (§7.11)."""

    _signal_group(process, signal.SIGTERM)
    try:
        return process.wait(timeout=max(grace_seconds, 0.0) or 0.05)
    except subprocess.TimeoutExpired:
        _signal_group(process, signal.SIGKILL)
        return process.wait()


class SubprocessExecutionResult(RuntimeModel):
    """Retained record of one subprocess attempt (goal.md §7.12).

    The four condition flags feed the frozen §6.6 precedence; kernel-side
    parse/schema/proposal-validation stages supply the remaining conditions.
    ``execution_root`` is diagnostic only: the directory is deleted after the
    safe artifacts are imported.
    """

    argv: tuple[str, ...]
    execution_root: Path | None
    confinement_level: ConfinementLevel
    exit_code: int | None
    timed_out: bool
    launch_error: str | None
    duration_seconds: float = Field(ge=0)
    resource_limit_breached: bool
    bundle_modified: bool
    output_policy_violated: bool
    proposal_format_invalid: bool
    primary_outcome: AttemptOutcome
    detected_failures: tuple[AttemptFailure, ...]
    stdout_capture: DiagnosticCapture
    stderr_capture: DiagnosticCapture
    inventory: tuple[ExecutionFileRecord, ...]
    quiescence: ProcessQuiescenceResult | None = None
    applied_limits: tuple[AppliedResourceLimit, ...] = ()
    compiled_request_digest: Sha256 | None = None
    compiled_request_sources: tuple[RequestSourceRecord, ...] = ()
    qualification_checks: tuple[QualificationEvidence, ...] = ()


class SubprocessAgentResult(AgentResult):
    """AgentResult carrying the retained §7.12 subprocess execution record."""

    execution_report: SubprocessExecutionResult


class SubprocessEngine:
    """AgentEngine that runs an argv worker through the §7 machinery.

    The default backend is :class:`TemporaryWorkspaceBackend`
    (``ConfinementLevel.TEST_ONLY``), so this engine never runs a real engine
    (§6.10). ``proposal_payloads`` supplies the canned launcher files (for
    example ``valid_proposal.json``) that the deterministic fake worker
    copies into ``output/``.
    """

    def __init__(
        self,
        *,
        worker_script: Path | None = None,
        modes: tuple[str, ...] = ("valid",),
        worker_config: Mapping[str, Any] | None = None,
        proposal_payloads: Mapping[str, bytes] | None = None,
        launcher_configuration: LauncherConfiguration | None = None,
        policy: SubprocessPolicy | None = None,
        launcher_policy: LauncherPolicy | None = None,
        credential_provider: EngineCredentialProvider | None = None,
        backend: ExecutionBackend | None = None,
        compile_request: bool = False,
        request_policy: RequestCompilationPolicy | None = None,
        qualification_gate: ExecutionQualificationGate | None = None,
        live: bool = False,
        _live_factory_token: object | None = None,
        _test_only_qualification_semantics: bool = False,
        authorized_project_root: Path | None = None,
        execution_root_parent: Path | None = None,
        name: str = "subprocess-fake",
        version: str | None = "1",
    ):
        if not modes or any(not mode.strip() for mode in modes):
            raise ValueError("at least one non-empty worker mode is required")
        if backend is None and worker_script is None and launcher_configuration is None:
            raise TypeError("worker_script is required without an injected backend")
        if launcher_configuration is not None and any(
            value is not None
            for value in (worker_script, worker_config, proposal_payloads)
        ):
            raise ValueError(
                "launcher_configuration cannot be combined with launcher fields"
            )
        self.name = name
        self.version = version
        self._modes = tuple(modes)
        selected_policy = policy or deterministic_test_policy()
        self._policy = SubprocessPolicy.model_validate(
            selected_policy.model_dump(mode="python")
        )
        self._launcher_policy = (
            LauncherPolicy.model_validate(launcher_policy.model_dump(mode="python"))
            if launcher_policy is not None
            else None
        )
        self._credential_provider = credential_provider or NullCredentialProvider()
        selected_launcher = launcher_configuration or LauncherConfiguration(
            worker_script=(
                worker_script if worker_script is not None else Path("fake_agent.py")
            ),
            worker_config=dict(worker_config or {}),
            proposal_payloads=dict(proposal_payloads or {}),
        )
        self._launcher = _snapshot_launcher_configuration(selected_launcher)
        self._backend: ExecutionBackend = backend or TemporaryWorkspaceBackend(
            self._launcher, execution_root_parent=execution_root_parent
        )
        self._compile_request = compile_request
        selected_request_policy = request_policy or RequestCompilationPolicy()
        self._request_policy = RequestCompilationPolicy.model_validate(
            selected_request_policy.model_dump(mode="python")
        )
        self._qualification_gate = qualification_gate
        self._live = live
        self._authorized_project_root = (
            authorized_project_root.resolve(strict=True)
            if authorized_project_root is not None
            else None
        )
        if live and _test_only_qualification_semantics:
            raise ValueError("test-only qualification seam cannot enable live execution")
        if _test_only_qualification_semantics and qualification_gate is None:
            raise ValueError("test-only qualification seam requires a qualification gate")
        if live:
            if _live_factory_token is not _LIVE_FACTORY_TOKEN:
                raise ValueError("live engines must be built by an explicit live factory")
            if qualification_gate is None:
                raise ValueError("live subprocess engines require a qualification gate")
            if not compile_request:
                raise ValueError("live subprocess engines require bounded request compilation")
            if not allows_real_engine(self._backend.confinement_level):
                raise ValueError("live subprocess engines require a real-engine backend")
            if self._credential_provider.provider_id == "null":
                raise ValueError("live subprocess engines require a credential provider")
            if getattr(self._credential_provider, "delivery_mode", None) != "fixed_file":
                raise ValueError(
                    "live subprocess engines require a fixed-file credential provider"
                )
            if type(self._backend) is not BubblewrapExecutionBackend:
                raise ValueError("live engines require the qualified Bubblewrap backend")
            if not isinstance(qualification_gate, StoredSandboxQualificationGate):
                raise ValueError("live engines require the stored sandbox qualification gate")
            if qualification_gate.backend is not self._backend:
                raise ValueError("qualification gate is not bound to the launch backend")
            backend_launcher = getattr(self._backend, "_launcher", None)
            if backend_launcher != self._launcher:
                raise ValueError("live launch backend configuration is not bound to the engine")
            if self._launcher.worker_script_sha256 is None:
                raise ValueError("live engines require a pinned worker-script hash")
            if (
                self._launcher.trusted_launcher_sha256 is None
                or self._launcher.credential_exec_shim_sha256 is None
            ):
                raise ValueError("live engines require pinned trusted-script hashes")
            if self._authorized_project_root is None:
                raise ValueError("live engines require one authorized project root")

    def safe_configuration(self) -> Mapping[str, Any]:
        """Nonsecret, deterministic configuration for the cache signature."""

        worker_hash: str | None = self._launcher.worker_script_sha256
        if worker_hash is None:
            try:
                worker_hash = hash_file(self._launcher.worker_script)
            except OSError:
                worker_hash = None
        trusted_launcher_hash: str | None = self._launcher.trusted_launcher_sha256
        if trusted_launcher_hash is None:
            try:
                trusted_launcher_hash = hash_file(
                    Path(trusted_launcher.__file__).resolve()
                )
            except OSError:
                trusted_launcher_hash = None
        credential_shim_hash: str | None = (
            self._launcher.credential_exec_shim_sha256
        )
        if credential_shim_hash is None:
            try:
                credential_shim_hash = hash_file(
                    Path(credential_exec_shim.__file__).resolve()
                )
            except OSError:
                credential_shim_hash = None
        provider_safe = getattr(self._credential_provider, "safe_configuration", None)
        return {
            "engine": "subprocess",
            "backend": type(self._backend).__name__,
            "confinement_level": self._backend.confinement_level.value,
            "credential_provider": self._credential_provider.provider_id,
            "credential_configuration": (
                provider_safe() if callable(provider_safe) else {
                    "provider_id": self._credential_provider.provider_id
                }
            ),
            "compile_request": self._compile_request,
            "request_policy": json.loads(self._request_policy.model_dump_json()),
            "live": self._live,
            "authorized_project_root_id": (
                hash_json({"project_root": str(self._authorized_project_root)})
                if self._authorized_project_root is not None
                else None
            ),
            "qualification_gate": (
                self._qualification_gate.safe_configuration()
                if self._qualification_gate is not None
                else None
            ),
            "modes": list(self._modes),
            "policy": json.loads(self._policy.model_dump_json()),
            "worker_config": copy.deepcopy(dict(self._launcher.worker_config)),
            "worker_script_sha256": worker_hash,
            "trusted_launcher_sha256": trusted_launcher_hash,
            "credential_exec_shim_sha256": credential_shim_hash,
            "launcher_assets": [
                {
                    "relative_path": asset.relative_path.as_posix(),
                    "sha256": asset.expected_sha256,
                    "executable": asset.executable,
                }
                for asset in self._launcher.assets
            ],
            "proposal_payload_hashes": {
                payload_name: hash_bytes(payload)
                for payload_name, payload in sorted(
                    self._launcher.proposal_payloads.items()
                )
            },
        }

    def execute(self, task: AgentTask) -> SubprocessAgentResult:
        started = time.monotonic()
        if self._live:
            try:
                workspace = task.workspace_dir.resolve(strict=True)
                project = self._authorized_project_root
                if project is None or (
                    workspace != project and project not in workspace.parents
                ):
                    raise RuntimeError("task workspace is outside the authorized project")
            except Exception:
                report, stdout_text, stderr_text = self._internal_failure_report(
                    started,
                    "live_project_boundary_mismatch",
                    "live task workspace is outside the authorized project",
                )
                return self._agent_result(report, None, stdout_text, stderr_text)
        try:
            session = self._backend.prepare(task, self._policy)
        except TrustedStagingError as exc:
            report, stdout_text, stderr_text = self._internal_failure_report(
                started,
                "trusted_launcher_staging_failed",
                "trusted launcher staging failed",
            )
            return self._agent_result(report, None, stdout_text, stderr_text)
        except Exception as exc:
            if self._live:
                report, stdout_text, stderr_text = self._internal_failure_report(
                    started,
                    "live_backend_preparation_failed",
                    "live confinement backend preparation failed closed",
                )
                return self._agent_result(report, None, stdout_text, stderr_text)
            if not isinstance(exc, (OSError, ValueError)):
                raise
            report, stdout_text, stderr_text = self._launch_failure_report(
                started, f"execution root preparation failed: {exc}"
            )
            return self._agent_result(report, None, stdout_text, stderr_text)

        qualification_checks: list[QualificationEvidence] = []
        session.qualification_checks = qualification_checks
        session.live_execution = self._live
        try:
            if self._compile_request:
                if task.expected_bundle_manifest_hash is None:
                    raise RuntimeError("task lacks its private manifest trust anchor")
                compiled = compile_engine_request(
                    session.bundle_dir,
                    task.task_type,
                    task.expected_bundle_manifest_hash,
                    self._request_policy,
                )
                session.stage_compiled_request(compiled)
            if self._qualification_gate is not None:
                qualification_checks.append(
                    self._qualification_gate.check(
                        QualificationCheckpoint.PRE_CREDENTIALS
                    )
                )
        except Exception as exc:
            code = (
                "qualification_pre_credentials_failed"
                if isinstance(exc, QualificationGateError)
                else "request_compilation_failed"
            )
            report, stdout_text, stderr_text = self._internal_failure_report(
                started,
                code,
                str(exc),
                qualification_checks=tuple(qualification_checks),
            )
            report = self._cleanup_session_or_override(session, report)
            return self._agent_result(report, None, stdout_text, stderr_text)

        try:
            prepare_for_session = getattr(
                self._credential_provider, "prepare_for_session", None
            )
            if callable(prepare_for_session):
                lease = prepare_for_session(
                    self.name,
                    CredentialStagingPaths(
                        session.credentials_dir, session.home_dir
                    ),
                )
            else:
                lease = self._credential_provider.prepare(
                    self.name, session.credentials_dir
                )
        except Exception as exc:
            report, stdout_text, stderr_text = self._internal_failure_report(
                started,
                "credential_preparation_failed",
                f"credential preparation failed: {exc}",
                qualification_checks=tuple(qualification_checks),
            )
            report = self._cleanup_session_or_override(session, report)
            return self._agent_result(report, None, stdout_text, stderr_text)

        report: SubprocessExecutionResult | None = None
        proposal_text: str | None = None
        stdout_text: str = ""
        stderr_text: str = ""
        entered = False
        try:
            try:
                with lease:
                    entered = True
                    if self._live:
                        if lease.context.secret_env or lease.context.public_env:
                            raise RuntimeError(
                                "live credential contexts cannot populate supervisor environment"
                            )
                        if lease.context.fixed_file_name is None:
                            raise RuntimeError(
                                "live credential contexts require one fixed credential file"
                            )
                    session.attach_credentials(
                        lease.context, live_supervisor=self._live
                    )
                    try:
                        report, proposal_text, stdout_text, stderr_text = self._run(
                            session, lease.context, started
                        )
                    except QualificationGateError as exc:
                        report, stdout_text, stderr_text = self._internal_failure_report(
                            started,
                            "qualification_pre_launch_failed",
                            str(exc),
                            qualification_checks=tuple(qualification_checks),
                        )
                        proposal_text = None
                    except Exception as exc:
                        report, stdout_text, stderr_text = self._internal_failure_report(
                            started,
                            "execution_runtime_internal_error",
                            f"execution runtime failed internally: {exc}",
                            qualification_checks=tuple(qualification_checks),
                        )
                        proposal_text = None
            except Exception as exc:
                code = (
                    "credential_cleanup_failed"
                    if entered
                    else "credential_activation_failed"
                )
                cleanup_failure = AttemptFailure(
                    code=code,
                    stage=AttemptFailureStage.WORKSPACE,
                    message=f"credential lease lifecycle failed: {exc}",
                )
                if report is not None:
                    report = report.model_copy(
                        update={
                            "primary_outcome": AttemptOutcome.INTERNAL_RUNTIME_FAILURE,
                            "proposal_format_invalid": False,
                            "detected_failures": report.detected_failures + (cleanup_failure,),
                        }
                    )
                else:
                    report, stdout_text, stderr_text = self._internal_failure_report(
                        started,
                        code,
                        f"credential lease lifecycle failed: {exc}",
                        qualification_checks=tuple(qualification_checks),
                    )
                    report = report.model_copy(
                        update={"detected_failures": (cleanup_failure,)}
                    )
                proposal_text = None
        finally:
            pass
        assert report is not None
        report = self._cleanup_session_or_override(session, report)
        if report.primary_outcome is AttemptOutcome.INTERNAL_RUNTIME_FAILURE:
            proposal_text = None
        return self._agent_result(report, proposal_text, stdout_text, stderr_text)

    def _cleanup_session_or_override(
        self,
        session: ExecutionSession,
        report: SubprocessExecutionResult,
    ) -> SubprocessExecutionResult:
        try:
            session.cleanup()
        except Exception as exc:
            failure = AttemptFailure(
                code="execution_session_cleanup_failed",
                stage=AttemptFailureStage.WORKSPACE,
                message=f"execution session cleanup failed: {exc}",
            )
            return report.model_copy(
                update={
                    "primary_outcome": AttemptOutcome.INTERNAL_RUNTIME_FAILURE,
                    "proposal_format_invalid": False,
                    "detected_failures": report.detected_failures + (failure,),
                }
            )
        return report

    def _internal_failure_report(
        self,
        started: float,
        code: str,
        message: str,
        *,
        qualification_checks: tuple[QualificationEvidence, ...] = (),
    ) -> tuple[SubprocessExecutionResult, str, str]:
        stdout_capture, stdout_text = _empty_capture("stdout.txt")
        stderr_capture, stderr_text = _empty_capture("stderr.txt")
        report = SubprocessExecutionResult(
            argv=(),
            execution_root=None,
            confinement_level=self._backend.confinement_level,
            exit_code=None,
            timed_out=False,
            launch_error=message,
            duration_seconds=time.monotonic() - started,
            resource_limit_breached=False,
            bundle_modified=False,
            output_policy_violated=False,
            proposal_format_invalid=False,
            primary_outcome=AttemptOutcome.INTERNAL_RUNTIME_FAILURE,
            detected_failures=(
                AttemptFailure(
                    code=code,
                    stage=AttemptFailureStage.PROCESS,
                    message=message,
                ),
            ),
            stdout_capture=stdout_capture,
            stderr_capture=stderr_capture,
            inventory=(),
            qualification_checks=qualification_checks,
        )
        return report, stdout_text, stderr_text

    def _launch_failure_report(
        self, started: float, launch_error: str
    ) -> tuple[SubprocessExecutionResult, str, str]:
        stdout_capture, stdout_text = _empty_capture("stdout.txt")
        stderr_capture, stderr_text = _empty_capture("stderr.txt")
        failures = (
            AttemptFailure(
                code="process_launch_failed",
                stage=AttemptFailureStage.PROCESS,
                message=launch_error,
            ),
        )
        report = SubprocessExecutionResult(
            argv=(),
            execution_root=None,
            confinement_level=self._backend.confinement_level,
            exit_code=None,
            timed_out=False,
            launch_error=launch_error,
            duration_seconds=time.monotonic() - started,
            resource_limit_breached=False,
            bundle_modified=False,
            output_policy_violated=False,
            proposal_format_invalid=False,
            primary_outcome=primary_attempt_outcome(execution_failed=True),
            detected_failures=failures,
            stdout_capture=stdout_capture,
            stderr_capture=stderr_capture,
            inventory=(),
        )
        return report, stdout_text, stderr_text

    def _run(
        self,
        session: ExecutionSession,
        credentials: CredentialContext,
        started: float,
    ) -> tuple[SubprocessExecutionResult, str | None, str, str]:
        policy = session.policy
        launcher_policy = self._launcher_policy or LauncherPolicy.from_subprocess_policy(policy)
        (session.launcher_dir / "launcher_policy.json").write_text(
            launcher_policy.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        launcher_script = session.launcher_dir / "trusted_launcher.py"
        policy_file = session.launcher_dir / "launcher_policy.json"
        report_file = session.launcher_dir / "applied_limits.json"

        worker_cmd = [
            sys.executable,
            str(session.launcher_dir / session.worker_name),
            "--config",
            "launcher/worker_config.json",
        ]
        for mode in self._modes:
            worker_cmd.extend(["--mode", mode])

        process_failures: list[AttemptFailure] = []
        resource_failures: list[AttemptFailure] = []
        workspace_failures: list[AttemptFailure] = []
        output_failures: list[AttemptFailure] = []
        proposal_failures: list[AttemptFailure] = []

        launch_error: str | None = None
        timed_out = False
        killed_for_breach = False
        resource_limit_breached = False
        bundle_modified = False
        output_policy_violated = False
        proposal_format_invalid = False
        exit_code: int | None = None
        proposal_text: str | None = None

        secrets = credentials.exact_redaction_values
        if self._qualification_gate is not None:
            # This is deliberately the last trusted operation before Popen.
            evidence = self._qualification_gate.check(
                QualificationCheckpoint.PRE_LAUNCH
            )
            session.qualification_checks.append(evidence)
            session.qualified_network_policy = evidence.network_policy
        inner_cmd = self._backend.wrap_command(session, worker_cmd)
        argv = [
            sys.executable,
            str(launcher_script),
            "--policy",
            str(policy_file),
            "--report",
            str(report_file),
            "--",
            *inner_cmd,
        ]
        try:
            process: subprocess.Popen | None = subprocess.Popen(
                argv,
                shell=False,
                cwd=session.root,
                start_new_session=True,
                env=session.environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=session.pass_fds,
            )
        except OSError as exc:
            process = None
            launch_error = str(exc)
            process_failures.append(
                AttemptFailure(
                    code="process_launch_failed",
                    stage=AttemptFailureStage.PROCESS,
                    message=launch_error,
                )
            )

        if process is None:
            stdout_capture, stdout_text = _empty_capture("stdout.txt")
            stderr_capture, stderr_text = _empty_capture("stderr.txt")
            quiescence: ProcessQuiescenceResult | None = None
        else:
            stdout = _StreamCapture(
                "stdout.txt", process.stdout, policy.max_stdout_bytes, secrets
            )
            stderr = _StreamCapture(
                "stderr.txt", process.stderr, policy.max_stderr_bytes, secrets
            )
            stdout.start()
            stderr.start()
            deadline = time.monotonic() + policy.timeout_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if process.poll() is not None:
                        exit_code = process.returncode
                    else:
                        timed_out = True
                        process_failures.append(
                            AttemptFailure(
                                code="process_timeout",
                                stage=AttemptFailureStage.PROCESS,
                                message=(
                                    f"process exceeded {policy.timeout_seconds} "
                                    "seconds"
                                ),
                            )
                        )
                        exit_code = _terminate_with_grace(
                            process, policy.terminate_grace_seconds
                        )
                    break
                try:
                    exit_code = process.wait(
                        timeout=min(
                            policy.writable_tree_scan_interval_seconds, remaining
                        )
                    )
                    break
                except subprocess.TimeoutExpired:
                    pass
                breach = check_process_count(process.pid, policy)
                if breach is None:
                    breach = scan_writable_roots(session.root, policy)
                if breach is not None:
                    resource_limit_breached = True
                    killed_for_breach = True
                    resource_failures.append(breach.attempt_failure())
                    _signal_group(process, signal.SIGKILL)
                    exit_code = process.wait()
                    break

            quiescence = None
            try:
                quiescence = self._backend.quiesce(
                    session, process, grace_seconds=policy.terminate_grace_seconds
                )
            except Exception as exc:
                quiescence = ProcessQuiescenceResult(
                    backend_method="unknown",
                    descendants_detected=None,
                    sigterm_sent=False,
                    sigkill_sent=False,
                    descendants_remaining=None,
                    quiescent=False,
                    diagnostic=f"quiescence inspection failed internally: {exc}",
                )

            for capture in (stdout, stderr):
                capture.join(policy.terminate_grace_seconds + _READER_JOIN_SECONDS)
            if stdout.alive:
                process.stdout.close()
            if stderr.alive:
                process.stderr.close()
            for capture in (stdout, stderr):
                capture.join(_READER_JOIN_SECONDS)
            stdout_capture, stdout_text = stdout.finish()
            stderr_capture, stderr_text = stderr.finish()

        quiescence_failed_internally = False
        background_survived = False
        if quiescence is not None:
            if not quiescence.quiescent:
                if quiescence.descendants_detected is None or (
                    quiescence.diagnostic
                    and "failed internally" in quiescence.diagnostic
                ):
                    process_failures.append(
                        AttemptFailure(
                            code="quiescence_inspection_failed",
                            stage=AttemptFailureStage.PROCESS,
                            message=quiescence.diagnostic
                            or "quiescence inspection failed internally",
                        )
                    )
                    quiescence_failed_internally = True
                elif (
                    quiescence.descendants_remaining
                    and quiescence.descendants_remaining > 0
                ):
                    process_failures.append(
                        AttemptFailure(
                            code="descendants_cleanup_failed",
                            stage=AttemptFailureStage.PROCESS,
                            message=f"{quiescence.descendants_remaining} descendants survived cleanup",
                        )
                    )
                    quiescence_failed_internally = True
            if (
                exit_code == 0
                and not timed_out
                and not killed_for_breach
                and launch_error is None
                and quiescence.descendants_detected
                and quiescence.descendants_detected > 0
            ):
                background_survived = True
                process_failures.append(
                    AttemptFailure(
                        code="background_processes_survived_parent",
                        stage=AttemptFailureStage.PROCESS,
                        message=(
                            f"{quiescence.descendants_detected} background "
                            "processes survived normal parent exit"
                        ),
                    )
                )

        launcher_failed = False
        worker_failed_internally = False
        if exit_code == _TRUSTED_WORKER_INTERNAL_EXIT:
            worker_failed_internally = True
            process_failures.append(
                AttemptFailure(
                    code="external_adapter_failed_closed",
                    stage=AttemptFailureStage.PROCESS,
                    message="trusted external-engine adapter failed closed",
                )
            )
        elif exit_code == 125:
            launcher_failed = True
            process_failures.append(
                AttemptFailure(
                    code="launcher_setrlimit_failed",
                    stage=AttemptFailureStage.PROCESS,
                    message="trusted launcher reported failure before engine launch",
                )
            )
        elif (
            exit_code is not None
            and exit_code != 0
            and not timed_out
            and not killed_for_breach
            and launch_error is None
        ):
            if exit_code < 0:
                sig = -exit_code
                if hasattr(signal, "SIGXCPU") and sig == signal.SIGXCPU:
                    resource_limit_breached = True
                    resource_failures.append(
                        AttemptFailure(
                            code=ResourceLimitCode.MAX_CPU_TIME.value,
                            stage=AttemptFailureStage.RESOURCE_LIMIT,
                            message="process exceeded CPU time limit (SIGXCPU)",
                        )
                    )
                elif hasattr(signal, "SIGXFSZ") and sig == signal.SIGXFSZ:
                    resource_limit_breached = True
                    resource_failures.append(
                        AttemptFailure(
                            code=ResourceLimitCode.MAX_WRITABLE_SINGLE_FILE_BYTES.value,
                            stage=AttemptFailureStage.RESOURCE_LIMIT,
                            message="process exceeded file size limit (SIGXFSZ)",
                        )
                    )
                else:
                    process_failures.append(
                        AttemptFailure(
                            code="process_terminated_by_signal",
                            stage=AttemptFailureStage.PROCESS,
                            message=f"process terminated by signal {sig}",
                        )
                    )
            else:
                process_failures.append(
                    AttemptFailure(
                        code="process_nonzero_exit",
                        stage=AttemptFailureStage.PROCESS,
                        message=f"process exited with code {exit_code}",
                    )
                )

        applied_limits: list[AppliedResourceLimit] = []
        if report_file.is_file():
            try:
                report_data = json.loads(report_file.read_text(encoding="utf-8"))
                if isinstance(report_data, list):
                    for item in report_data:
                        applied_limits.append(AppliedResourceLimit.model_validate(item))
            except Exception:
                pass

        if launch_error is None and not resource_limit_breached:
            # Final scan: catches quota breaches that completed just before
            # the process exited, faster than one monitor interval.
            breach = scan_writable_roots(session.root, policy)
            if breach is not None:
                resource_limit_breached = True
                resource_failures.append(breach.attempt_failure())

        include_output = True
        identity_failure = verify_output_directory_identity(
            session.output_dir, session.trusted_output_stat
        )
        if identity_failure is not None:
            output_policy_violated = True
            include_output = False
            output_failures.append(identity_failure)

        bundle_failure = _verify_execution_bundle(session)
        if bundle_failure is not None:
            bundle_modified = True
            workspace_failures.append(bundle_failure)

        if (
            identity_failure is None
            and not quiescence_failed_internally
            and not background_survived
            and not launcher_failed
        ):
            tree_failures = scan_output_tree(session.output_dir, policy)
            if tree_failures:
                output_policy_violated = True
                output_failures.extend(tree_failures)
            imported = import_proposal(
                session.output_fd, policy, session.bundle_inodes
            )
            proposal_failures.extend(imported.failures)
            output_policy_violated = (
                output_policy_violated or imported.output_policy_violated
            )
            proposal_format_invalid = imported.format_invalid
            proposal_text = imported.text

        inventory = build_execution_inventory(
            session.root,
            policy,
            include_output=include_output,
            after_breach=resource_limit_breached,
            sensitive_roots=credentials.sensitive_roots,
        )

        execution_failed = (
            launch_error is not None
            or timed_out
            or (exit_code is not None and exit_code != 0)
            or background_survived
            or launcher_failed
        )
        if quiescence_failed_internally or launcher_failed or worker_failed_internally:
            primary = AttemptOutcome.INTERNAL_RUNTIME_FAILURE
        else:
            primary = primary_attempt_outcome(
                resource_limit_breached=resource_limit_breached,
                execution_failed=execution_failed,
                bundle_modified=bundle_modified,
                output_policy_violated=output_policy_violated,
                proposal_format_invalid=proposal_format_invalid,
            )
        if primary is not AttemptOutcome.VALID_SCIENTIFIC_RESULT:
            proposal_text = None

        if launch_error is not None:
            execution_error: str | None = launch_error
        elif timed_out:
            execution_error = (
                f"process timed out after {policy.timeout_seconds} seconds"
            )
        elif execution_failed:
            execution_error = f"process exited with code {exit_code}"
        else:
            execution_error = None

        report = SubprocessExecutionResult(
            argv=tuple(argv),
            execution_root=session.root,
            confinement_level=self._backend.confinement_level,
            exit_code=exit_code,
            timed_out=timed_out,
            launch_error=launch_error,
            duration_seconds=time.monotonic() - started,
            resource_limit_breached=resource_limit_breached,
            bundle_modified=bundle_modified,
            output_policy_violated=output_policy_violated,
            proposal_format_invalid=proposal_format_invalid,
            primary_outcome=primary,
            detected_failures=tuple(
                process_failures
                + resource_failures
                + workspace_failures
                + output_failures
                + proposal_failures
            ),
            stdout_capture=stdout_capture,
            stderr_capture=stderr_capture,
            inventory=inventory,
            quiescence=quiescence,
            applied_limits=tuple(applied_limits),
            compiled_request_digest=(
                session.compiled_request.request_digest
                if session.compiled_request is not None
                else None
            ),
            compiled_request_sources=(
                session.compiled_request.sources
                if session.compiled_request is not None
                else ()
            ),
            qualification_checks=tuple(session.qualification_checks),
        )
        return report, proposal_text, stdout_text, stderr_text

    def _agent_result(
        self,
        report: SubprocessExecutionResult,
        proposal_text: str | None,
        stdout_text: str,
        stderr_text: str,
    ) -> SubprocessAgentResult:
        if report.launch_error is not None:
            execution_error: str | None = report.launch_error
        elif report.timed_out:
            execution_error = (
                f"process timed out after {self._policy.timeout_seconds} seconds"
            )
        elif report.exit_code is not None and report.exit_code != 0:
            execution_error = f"process exited with code {report.exit_code}"
        else:
            execution_error = None
        return SubprocessAgentResult(
            engine=self.name,
            engine_version=self.version,
            execution_succeeded=(
                report.launch_error is None
                and not report.timed_out
                and report.exit_code == 0
                and report.primary_outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT
            ),
            output_text=proposal_text or "",
            stdout=stdout_text,
            stderr=stderr_text,
            execution_error=execution_error,
            execution_report=report,
        )
