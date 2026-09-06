"""Engine credential handoff contracts (goal.md §8.1).

Secret values are suppressed in repr, excluded from serialization and cache
signatures, and never persisted. Credential files live outside ``bundle/``
and credential material is deleted after execution; file creation is the
backend's job (B2/B3), not part of these contracts.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from pydantic import Field, SecretStr

from vibereview.ids import Sha256

from .hashing import hash_json
from .records import RuntimeModel


class CredentialContext(RuntimeModel):
    """Prepared credentials for one execution.

    ``secret_env`` values are SecretStr (masked in repr and dumps) and
    ``exact_redaction_values`` holds the raw secret bytes for the stream
    redactor; it is excluded from every dump and from ``repr`` so persisted
    artifacts never contain secret material.
    """

    public_env: dict[str, str]
    secret_env: dict[str, SecretStr]
    ephemeral_files: tuple[Path, ...]
    exact_redaction_values: tuple[bytes, ...] = Field(repr=False, exclude=True)
    provider_id: str
    nonsecret_configuration_fingerprint: Sha256


class EngineCredentialProvider(Protocol):
    @property
    def provider_id(self) -> str: ...

    def prepare(self, engine_name: str, execution_root: Path) -> CredentialContext: ...


def _nonsecret_fingerprint(provider_id: str, public_env: Mapping[str, str]) -> str:
    """Fingerprint over nonsecret configuration only; never over secrets."""

    return hash_json(
        {
            "provider_id": provider_id,
            "public_env": dict(sorted(public_env.items())),
        }
    )


class NullCredentialProvider:
    """Provider for engines that need no credentials (fake-worker tests)."""

    provider_id = "null"

    def prepare(self, engine_name: str, execution_root: Path) -> CredentialContext:
        return CredentialContext(
            public_env={},
            secret_env={},
            ephemeral_files=(),
            exact_redaction_values=(),
            provider_id=self.provider_id,
            nonsecret_configuration_fingerprint=_nonsecret_fingerprint(
                self.provider_id, {}
            ),
        )


class SyntheticCredentialProvider:
    """Test provider injecting configurable public and secret environment.

    Injected secret values are also exposed as ``exact_redaction_values`` so
    diagnostics redaction can be proven against a secret the fake child
    prints (goal.md §8.2).
    """

    def __init__(
        self,
        *,
        provider_id: str = "synthetic",
        public_env: Mapping[str, str] | None = None,
        secret_env: Mapping[str, str] | None = None,
    ) -> None:
        self._provider_id = provider_id
        self._public_env = dict(sorted((public_env or {}).items()))
        self._secret_env = dict(sorted((secret_env or {}).items()))

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def prepare(self, engine_name: str, execution_root: Path) -> CredentialContext:
        return CredentialContext(
            public_env=dict(self._public_env),
            secret_env={
                key: SecretStr(value) for key, value in self._secret_env.items()
            },
            ephemeral_files=(),
            exact_redaction_values=tuple(
                value.encode("utf-8") for value in self._secret_env.values()
            ),
            provider_id=self._provider_id,
            nonsecret_configuration_fingerprint=_nonsecret_fingerprint(
                self._provider_id, self._public_env
            ),
        )
