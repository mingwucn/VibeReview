"""Engine credential handoff contracts (goal.md §8.1).

Secret values are suppressed in repr, excluded from serialization and cache
signatures, and never persisted. Credential files live outside ``bundle/``
and credential material is deleted after execution; file creation is the
backend's job (B2/B3), not part of these contracts.
"""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
from typing import Any, Protocol

from pydantic import Field, SecretStr

from vibereview.ids import Sha256

from .hashing import hash_json
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
    provider_id: str
    nonsecret_configuration_fingerprint: Sha256


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
