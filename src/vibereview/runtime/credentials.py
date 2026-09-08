"""Engine credential handoff contracts (goal.md §8.1).

Secret values are suppressed in repr, excluded from serialization and cache
signatures, and never persisted. Credential files live outside ``bundle/``
and credential material is deleted after execution; file creation is the
backend's job (B2/B3), not part of these contracts.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Protocol

from pydantic import Field, SecretStr, field_validator

from vibereview.ids import Sha256

from .hashing import hash_json
from .locking import AdvisoryFileLock
from .records import RuntimeModel


class CredentialContext(RuntimeModel):
    """Prepared credentials for one execution (goal.md §7.3).

    ``secret_env`` values are SecretStr (masked in repr and dumps) and
    ``exact_redaction_values`` holds the raw secret bytes for the stream
    redactor; all secret-bearing fields are excluded from every dump and
    from ``repr`` so persisted artifacts never contain secret material.
    """

    public_env: dict[str, str]
    secret_env: dict[str, SecretStr] = Field(repr=False, exclude=True)
    ephemeral_files: tuple[Path, ...] = Field(repr=False, exclude=True)
    exact_redaction_values: tuple[bytes, ...] = Field(repr=False, exclude=True)
    fixed_file_name: str | None = None
    credential_env_name: str | None = None
    home_profile_relative_path: Path | None = None
    sensitive_roots: tuple[Path, ...] = Field(default=(), repr=False, exclude=True)
    provider_id: str
    nonsecret_configuration_fingerprint: Sha256

    @field_validator("fixed_file_name")
    @classmethod
    def _fixed_name_is_safe(cls, value: str | None) -> str | None:
        return _validate_fixed_name(value) if value is not None else None

    @field_validator("credential_env_name")
    @classmethod
    def _credential_env_is_allowlisted(cls, value: str | None) -> str | None:
        return _validate_credential_env_name(value)

    @field_validator("home_profile_relative_path")
    @classmethod
    def _home_profile_is_safe(cls, value: Path | None) -> Path | None:
        return _validate_home_relative_path(value)


class CredentialLease(Protocol):
    """Mandatory context manager lifecycle for staged credentials (goal.md §7.2)."""

    @property
    def context(self) -> CredentialContext: ...

    def __enter__(self) -> "CredentialLease": ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> bool | None: ...


class EngineCredentialProvider(Protocol):
    @property
    def provider_id(self) -> str: ...

    def prepare(
        self, engine_name: str, credentials_dir: Path
    ) -> CredentialLease: ...


class CredentialStagingPaths:
    """Trusted attempt-local locations supplied to session-aware providers."""

    def __init__(self, credentials_dir: Path, home_dir: Path) -> None:
        self.credentials_dir = credentials_dir.resolve()
        self.home_dir = home_dir.resolve()


def _nonsecret_fingerprint(provider_id: str, public_env: Mapping[str, str]) -> str:
    """Fingerprint over nonsecret configuration only; never over secrets."""

    return hash_json(
        {
            "provider_id": provider_id,
            "public_env": dict(sorted(public_env.items())),
        }
    )


class NullCredentialLease:
    """No-op lease for NullCredentialProvider."""

    def __init__(self, context: CredentialContext) -> None:
        self._context = context

    @property
    def context(self) -> CredentialContext:
        return self._context

    def __enter__(self) -> "NullCredentialLease":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._context, name)

    def __repr__(self) -> str:
        return repr(self._context)

    def __str__(self) -> str:
        return str(self._context)


class NullCredentialProvider:
    """Provider for engines that need no credentials (fake-worker tests)."""

    provider_id = "null"
    delivery_mode = "none"

    def prepare(self, engine_name: str, credentials_dir: Path) -> NullCredentialLease:
        context = CredentialContext(
            public_env={},
            secret_env={},
            ephemeral_files=(),
            exact_redaction_values=(),
            provider_id=self.provider_id,
            nonsecret_configuration_fingerprint=_nonsecret_fingerprint(
                self.provider_id, {}
            ),
        )
        return NullCredentialLease(context)


class SyntheticCredentialLease:
    """Lease managing ephemeral files and cleanup for SyntheticCredentialProvider."""

    def __init__(
        self,
        *,
        context: CredentialContext,
        credentials_dir: Path,
        staged_files: tuple[Path, ...] = (),
        fail_cleanup: bool = False,
    ) -> None:
        self._context = context
        self._credentials_dir = credentials_dir
        self._staged_files = staged_files
        self._fail_cleanup = fail_cleanup

    @property
    def context(self) -> CredentialContext:
        return self._context

    def __enter__(self) -> "SyntheticCredentialLease":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        if self._fail_cleanup:
            raise RuntimeError("credential lease cleanup deliberately failed")
        for path in self._staged_files:
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._context, name)

    def __repr__(self) -> str:
        return repr(self._context)

    def __str__(self) -> str:
        return str(self._context)


class SyntheticCredentialProvider:
    """Test provider injecting configurable public and secret environment.

    Injected secret values are also exposed as ``exact_redaction_values`` so
    diagnostics redaction can be proven against a secret the fake child
    prints (goal.md §8.2, §7.6). Supports file credentials and simulated
    cleanup failures.
    """

    def __init__(
        self,
        *,
        provider_id: str = "synthetic",
        public_env: Mapping[str, str] | None = None,
        secret_env: Mapping[str, str] | None = None,
        file_credentials: Mapping[str, str | bytes] | None = None,
        fail_cleanup: bool = False,
    ) -> None:
        self._provider_id = provider_id
        self._public_env = dict(sorted((public_env or {}).items()))
        self._secret_env = dict(sorted((secret_env or {}).items()))
        self._file_credentials = dict(file_credentials or {})
        self._fail_cleanup = fail_cleanup

    delivery_mode = "legacy"

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def prepare(
        self, engine_name: str, credentials_dir: Path
    ) -> SyntheticCredentialLease:
        credentials_dir = credentials_dir.resolve()
        credentials_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(credentials_dir, 0o700)
        except OSError:
            pass

        staged_files: list[Path] = []
        exact_redactions: list[bytes] = [
            value.encode("utf-8") for value in self._secret_env.values() if value
        ]

        for filename, content in self._file_credentials.items():
            filename = _validate_fixed_name(filename)
            target = credentials_dir / filename
            if isinstance(content, str):
                target.write_text(content, encoding="utf-8")
                raw_val = content.encode("utf-8")
            else:
                target.write_bytes(content)
                raw_val = content
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
            staged_files.append(target)
            if raw_val and len(raw_val) >= 4:
                exact_redactions.append(raw_val)

        context = CredentialContext(
            public_env=dict(self._public_env),
            secret_env={
                key: SecretStr(value) for key, value in self._secret_env.items()
            },
            ephemeral_files=tuple(staged_files),
            exact_redaction_values=tuple(exact_redactions),
            provider_id=self._provider_id,
            nonsecret_configuration_fingerprint=_nonsecret_fingerprint(
                self._provider_id, self._public_env
            ),
        )
        return SyntheticCredentialLease(
            context=context,
            credentials_dir=credentials_dir,
            staged_files=tuple(staged_files),
            fail_cleanup=self._fail_cleanup,
        )


def _validate_fixed_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name):
        raise ValueError(f"unsafe fixed credential filename {name!r}")
    return name


def _validate_home_relative_path(path: Path | None) -> Path | None:
    if path is None:
        return None
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("home credential target must be a safe relative path")
    return path


_ALLOWED_CREDENTIAL_ENV_NAMES = frozenset(
    {"CODEX_API_KEY", "KIMI_MODEL_API_KEY"}
)


def _validate_credential_env_name(name: str | None) -> str | None:
    if name is None:
        return None
    if name not in _ALLOWED_CREDENTIAL_ENV_NAMES:
        raise ValueError("credential environment-variable name is not allowlisted")
    return name


class FixedFileCredentialLease:
    """Stage one bounded secret file only while a lease is active."""

    def __init__(
        self,
        *,
        provider_id: str,
        paths: CredentialStagingPaths,
        fixed_file_name: str,
        secret_loader: Callable[[], bytes],
        max_bytes: int,
        home_profile_relative_path: Path | None = None,
        credential_env_name: str | None = None,
        validator: Callable[[bytes], tuple[bytes, ...]] | None = None,
        lock: AdvisoryFileLock | None = None,
        rotation_promoter: Callable[[Path, bytes], None] | None = None,
    ) -> None:
        self._provider_id = provider_id
        self._paths = paths
        self._fixed_file_name = _validate_fixed_name(fixed_file_name)
        self._secret_loader = secret_loader
        self._max_bytes = max_bytes
        self._home_profile_relative_path = _validate_home_relative_path(
            home_profile_relative_path
        )
        self._credential_env_name = _validate_credential_env_name(
            credential_env_name
        )
        if (
            self._home_profile_relative_path is not None
            and self._credential_env_name is not None
        ):
            raise ValueError(
                "credential cannot be staged into HOME and the process environment"
            )
        self._validator = validator
        self._lock = lock
        self._rotation_promoter = rotation_promoter
        self._staged_path = paths.credentials_dir / self._fixed_file_name
        self._entered = False
        self._context: CredentialContext | None = None

    @property
    def context(self) -> CredentialContext:
        if self._context is None:
            raise RuntimeError("credential context requested before lease entry")
        return self._context

    def __enter__(self) -> "FixedFileCredentialLease":
        if self._entered:
            raise RuntimeError("credential lease cannot be entered twice")
        if self._lock is not None:
            self._lock.__enter__()
        try:
            raw = self._secret_loader()
            if not isinstance(raw, bytes) or not raw or len(raw) > self._max_bytes:
                raise RuntimeError("credential payload is empty or exceeds its bound")
            scalar_redactions = self._validator(raw) if self._validator else ()
            self._paths.credentials_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self._paths.credentials_dir, 0o700)
            descriptor = os.open(
                self._staged_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                self._staged_path.unlink(missing_ok=True)
                raise
            # Even very short credentials remain secret material.  Retaining
            # every non-empty exact value may redact more diagnostics for an
            # implausibly short token, but it cannot permit that token to leak.
            exact = tuple(value for value in (raw, *scalar_redactions) if value)
            sensitive = (
                (Path("home") / self._home_profile_relative_path.parent,)
                if self._home_profile_relative_path is not None
                else ()
            )
            self._context = CredentialContext(
                public_env={},
                secret_env={},
                ephemeral_files=(self._staged_path,),
                exact_redaction_values=exact,
                fixed_file_name=self._fixed_file_name,
                credential_env_name=self._credential_env_name,
                home_profile_relative_path=self._home_profile_relative_path,
                sensitive_roots=sensitive,
                provider_id=self._provider_id,
                nonsecret_configuration_fingerprint=_nonsecret_fingerprint(
                    self._provider_id,
                    {
                        "fixed_file": self._fixed_file_name,
                        "credential_env": self._credential_env_name or "",
                        "home_profile": (
                            self._home_profile_relative_path.as_posix()
                            if self._home_profile_relative_path is not None
                            else ""
                        ),
                    },
                ),
            )
            self._entered = True
            return self
        except BaseException as original:
            cleanup_errors: list[BaseException] = []
            try:
                self._staged_path.unlink(missing_ok=True)
                if self._staged_path.exists() or self._staged_path.is_symlink():
                    cleanup_errors.append(
                        RuntimeError("credential activation cleanup could not be verified")
                    )
            except BaseException as caught:
                cleanup_errors.append(caught)
            try:
                if self._lock is not None:
                    self._lock.__exit__(None, None, None)
            except BaseException as caught:
                cleanup_errors.append(caught)
            finally:
                self._entered = False
            if cleanup_errors:
                raise RuntimeError(
                    "credential activation and cleanup failed closed"
                ) from original
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        cleanup_error: BaseException | None = None
        home_target = (
            self._paths.home_dir / self._home_profile_relative_path
            if self._home_profile_relative_path is not None
            else None
        )
        try:
            if (
                self._rotation_promoter is not None
                and self._home_profile_relative_path is not None
            ):
                rotated = home_target
                assert rotated is not None
                if rotated.is_symlink() or not rotated.is_file():
                    raise RuntimeError("rotated credential file is missing or unsafe")
                raw = rotated.read_bytes()
                if not raw or len(raw) > self._max_bytes:
                    raise RuntimeError("rotated credential payload is invalid or oversized")
                if self._validator is not None:
                    self._validator(raw)
                self._rotation_promoter(rotated, raw)
        except BaseException as caught:
            cleanup_error = caught
        finally:
            try:
                for target in (home_target, self._staged_path):
                    if target is None:
                        continue
                    try:
                        target.unlink(missing_ok=True)
                        if target.exists() or target.is_symlink():
                            raise RuntimeError("credential cleanup could not be verified")
                    except BaseException as caught:
                        if cleanup_error is None:
                            cleanup_error = caught
            finally:
                try:
                    if self._lock is not None:
                        self._lock.__exit__(exc_type, exc, traceback)
                except BaseException as caught:
                    if cleanup_error is None:
                        cleanup_error = caught
                finally:
                    self._entered = False
        if cleanup_error is not None:
            raise cleanup_error
        return None


class EnvironmentFileCredentialProvider:
    """Stage one exact environment secret as a file, never env or argv."""

    delivery_mode = "fixed_file"

    def __init__(
        self,
        *,
        env_var: str,
        provider_id: str,
        fixed_file_name: str = "credential",
        max_bytes: int = 16_384,
        credential_env_name: str | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", env_var):
            raise ValueError("unsafe credential environment-variable name")
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._env_var = env_var
        self._provider_id = provider_id
        self._fixed_file_name = _validate_fixed_name(fixed_file_name)
        self._max_bytes = max_bytes
        self._credential_env_name = _validate_credential_env_name(
            credential_env_name
        )

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def safe_configuration(self) -> dict[str, Any]:
        return {
            "provider_id": self._provider_id,
            "source": "single_environment_variable",
            "env_var": self._env_var,
            "fixed_file_name": self._fixed_file_name,
            "max_bytes": self._max_bytes,
            "credential_env_name": self._credential_env_name,
        }

    def prepare_for_session(
        self, engine_name: str, paths: CredentialStagingPaths
    ) -> FixedFileCredentialLease:
        def load() -> bytes:
            value = os.environ.get(self._env_var)
            if value is None or not value:
                raise RuntimeError(f"required credential {self._env_var} is unavailable")
            if "\x00" in value or "\r" in value or "\n" in value:
                raise RuntimeError(
                    f"required credential {self._env_var} is not one exact scalar"
                )
            return value.encode("utf-8")

        return FixedFileCredentialLease(
            provider_id=self._provider_id,
            paths=paths,
            fixed_file_name=self._fixed_file_name,
            secret_loader=load,
            max_bytes=self._max_bytes,
            credential_env_name=self._credential_env_name,
        )

    def prepare(self, engine_name: str, credentials_dir: Path) -> CredentialLease:
        raise RuntimeError("fixed-file credentials require an execution session")


class LockedJsonTokenCredentialProvider:
    """Lease one scalar token from a dedicated machine-local JSON vault.

    The source profile is never copied.  The provider holds a sibling advisory
    lock while the attempt is active and stages only the selected scalar into
    the fixed credential file consumed by the in-sandbox exec shim.  Refresh is
    deliberately unsupported: a client requiring mutable profile refresh must
    be declared unavailable until a separately qualified promotion protocol is
    implemented.
    """

    delivery_mode = "fixed_file"

    def __init__(
        self,
        *,
        provider_id: str,
        vault_path: Path,
        token_field_path: tuple[str, ...],
        credential_env_name: str | None = None,
        home_profile_relative_path: Path | None = None,
        fixed_file_name: str = "credential",
        vault_id: str,
        max_vault_bytes: int = 1_048_576,
        max_token_bytes: int = 16_384,
        lock_timeout_seconds: float = 30.0,
    ) -> None:
        if not token_field_path or any(not item for item in token_field_path):
            raise ValueError("token_field_path must contain non-empty object keys")
        if not vault_id or any(character in vault_id for character in "\x00\r\n"):
            raise ValueError("vault_id must be a non-empty single-line identifier")
        if max_vault_bytes < 1 or max_token_bytes < 1:
            raise ValueError("credential bounds must be positive")
        self._provider_id = provider_id
        self._vault_path = vault_path.expanduser().absolute()
        self._token_field_path = token_field_path
        self._credential_env_name = _validate_credential_env_name(
            credential_env_name
        )
        self._home_profile_relative_path = _validate_home_relative_path(
            home_profile_relative_path
        )
        if (self._credential_env_name is None) == (
            self._home_profile_relative_path is None
        ):
            raise ValueError(
                "exactly one credential environment or HOME delivery mode is required"
            )
        self._fixed_file_name = _validate_fixed_name(fixed_file_name)
        self._vault_id = vault_id
        self._max_vault_bytes = max_vault_bytes
        self._max_token_bytes = max_token_bytes
        self._lock_timeout_seconds = lock_timeout_seconds

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def credential_env_name(self) -> str | None:
        return self._credential_env_name

    @property
    def home_profile_relative_path(self) -> Path | None:
        return self._home_profile_relative_path

    @property
    def vault_path(self) -> Path:
        return self._vault_path

    def safe_configuration(self) -> dict[str, Any]:
        return {
            "provider_id": self._provider_id,
            "source": "dedicated_locked_json_token_vault",
            "vault_id": self._vault_id,
            "token_field_path": list(self._token_field_path),
            "credential_env_name": self._credential_env_name,
            "home_profile_relative_path": (
                self._home_profile_relative_path.as_posix()
                if self._home_profile_relative_path is not None
                else None
            ),
            "fixed_file_name": self._fixed_file_name,
            "refresh_supported": False,
            "max_vault_bytes": self._max_vault_bytes,
            "max_token_bytes": self._max_token_bytes,
        }

    def _load_token(self) -> bytes:
        item = os.lstat(self._vault_path)
        if not stat.S_ISREG(item.st_mode) or item.st_size > self._max_vault_bytes:
            raise RuntimeError("dedicated credential vault is unsafe or oversized")
        descriptor = os.open(
            self._vault_path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (item.st_dev, item.st_ino):
                raise RuntimeError("dedicated credential vault changed while opening")
            chunks: list[bytes] = []
            observed = 0
            while True:
                chunk = os.read(
                    descriptor,
                    min(65_536, self._max_vault_bytes + 1 - observed),
                )
                if not chunk:
                    break
                observed += len(chunk)
                if observed > self._max_vault_bytes:
                    raise RuntimeError("dedicated credential vault is oversized")
                chunks.append(chunk)
            after = os.fstat(descriptor)
            if (
                observed != opened.st_size
                or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            ):
                raise RuntimeError("dedicated credential vault changed or is oversized")
            raw = b"".join(chunks)
        finally:
            os.close(descriptor)
        try:
            value: Any = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("dedicated credential vault is not UTF-8 JSON") from exc
        for key in self._token_field_path:
            if not isinstance(value, dict) or key not in value:
                raise RuntimeError("dedicated credential token field is unavailable")
            value = value[key]
        if not isinstance(value, str):
            raise RuntimeError("dedicated credential token is not a string")
        token = value.encode("utf-8")
        if (
            not token
            or len(token) > self._max_token_bytes
            or b"\x00" in token
            or b"\n" in token
            or b"\r" in token
        ):
            raise RuntimeError("dedicated credential token is invalid or oversized")
        return token

    def prepare_for_session(
        self, engine_name: str, paths: CredentialStagingPaths
    ) -> FixedFileCredentialLease:
        lock_path = self._vault_path.with_name(self._vault_path.name + ".vibereview.lock")
        return FixedFileCredentialLease(
            provider_id=self._provider_id,
            paths=paths,
            fixed_file_name=self._fixed_file_name,
            secret_loader=self._load_token,
            max_bytes=self._max_token_bytes,
            credential_env_name=self._credential_env_name,
            home_profile_relative_path=self._home_profile_relative_path,
            lock=AdvisoryFileLock(lock_path, timeout_seconds=self._lock_timeout_seconds),
        )

    def prepare(self, engine_name: str, credentials_dir: Path) -> CredentialLease:
        raise RuntimeError("fixed-file credentials require an execution session")
