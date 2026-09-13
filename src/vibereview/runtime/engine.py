"""Bounded semantic-engine interface and deterministic MockEngine."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from pydantic import ConfigDict, model_validator

from .hashing import canonical_json_bytes, hash_json
from .records import AgentResult, AgentTask, RuntimeModel


SAFE_ENGINE_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
SAFE_ENGINE_VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._+:-]{0,127}$"

_SAFE_ENGINE_NAME_RE = re.compile(SAFE_ENGINE_NAME_PATTERN)
_SAFE_ENGINE_VERSION_RE = re.compile(SAFE_ENGINE_VERSION_PATTERN)


class AgentEngine(Protocol):
    name: str
    version: str | None

    def safe_configuration(self) -> Mapping[str, Any]: ...

    def execute(self, task: AgentTask) -> AgentResult: ...


class MockResponse(RuntimeModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal: dict[str, Any] | None = None
    raw_output: str | None = None
    execution_error: str | None = None
    stdout: str = ""
    stderr: str = ""

    @model_validator(mode="after")
    def _bounded_script_payload(self) -> "MockResponse":
        limits = {
            "raw output": (self.raw_output, 4 * 1024 * 1024),
            "execution error": (self.execution_error, 1024 * 1024),
            "stdout": (self.stdout, 1024 * 1024),
            "stderr": (self.stderr, 1024 * 1024),
        }
        for label, (value, limit) in limits.items():
            if value is not None and len(value.encode("utf-8")) > limit:
                raise ValueError(f"mock {label} exceeds its byte limit")
        if self.proposal is not None:
            try:
                compact = json.dumps(
                    self.proposal,
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                ).encode("utf-8")
                imported = (
                    json.dumps(
                        self.proposal,
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=2,
                        allow_nan=False,
                    )
                    + "\n"
                ).encode("utf-8")
            except (TypeError, ValueError, UnicodeError):
                raise ValueError("mock proposal is not finite UTF-8 JSON") from None
            if max(len(compact), len(imported)) > 4 * 1024 * 1024:
                raise ValueError("mock proposal exceeds its byte limit")
        return self


class MockEngine:
    """A deterministic scripted engine used to prove the complete runtime path."""

    def __init__(
        self,
        responses: list[MockResponse],
        *,
        name: str = "mock",
        version: str | None = "1",
        on_execute: Callable[[AgentTask, int], None] | None = None,
    ):
        if not isinstance(name, str) or _SAFE_ENGINE_NAME_RE.fullmatch(name) is None:
            raise ValueError("mock engine name is not a sanitized identity")
        if version is not None and (
            not isinstance(version, str)
            or _SAFE_ENGINE_VERSION_RE.fullmatch(version) is None
        ):
            raise ValueError("mock engine version is not a sanitized identity")
        self.name = name
        self.version = version
        detached_responses = tuple(
            MockResponse.model_validate(response.model_dump(mode="python"))
            for response in responses
        )
        self._response_payloads = tuple(
            canonical_json_bytes(response.model_dump(mode="json"))
            for response in detached_responses
        )
        self._script_fingerprint = hash_json(
            {
                "script_version": "mock-response-script-1",
                "responses": [
                    response.model_dump(mode="json")
                    for response in detached_responses
                ],
            }
        )
        self._lock = threading.Lock()
        self._calls = 0
        self._on_execute = on_execute

    @property
    def calls(self) -> int:
        with self._lock:
            return self._calls

    @property
    def script_fingerprint(self) -> str:
        """Hash the exact ordered initial script without exposing its payloads."""

        return self._script_fingerprint

    @property
    def has_execution_callback(self) -> bool:
        """Report callback presence without exposing executable callback state."""

        return self._on_execute is not None

    @property
    def scripted_responses(self) -> tuple[MockResponse, ...]:
        """Return detached copies of the immutable initial response script."""

        return tuple(
            MockResponse.model_validate_json(payload)
            for payload in self._response_payloads
        )

    def restore_calls(self, calls: int) -> None:
        """Restore a pristine deterministic cursor from authenticated history.

        No response is executed. The caller must authenticate the immutable
        initial response script before invoking this one-time operation.
        """

        if not isinstance(calls, int) or isinstance(calls, bool) or calls < 0:
            raise ValueError("mock engine call cursor is invalid")
        with self._lock:
            if self._on_execute is not None:
                raise ValueError("callback-backed mock cursors cannot be restored")
            if self._calls != 0:
                raise ValueError("only a pristine mock cursor can be restored")
            self._calls = calls

    def safe_configuration(self) -> Mapping[str, Any]:
        return {
            "engine": "deterministic-mock",
            "version": self.version,
            "script_fingerprint": self.script_fingerprint,
            "has_execution_callback": self.has_execution_callback,
        }

    def execute(self, task: AgentTask) -> AgentResult:
        with self._lock:
            call_number = self._calls
            self._calls += 1
            if not self._response_payloads:
                response = MockResponse(execution_error="no scripted response remains")
            else:
                payload = self._response_payloads[
                    min(call_number, len(self._response_payloads) - 1)
                ]
                response = MockResponse.model_validate_json(payload)
        if self._on_execute is not None:
            self._on_execute(task, call_number)
        if response.execution_error is not None:
            return AgentResult(
                engine=self.name,
                engine_version=self.version,
                execution_succeeded=False,
                output_text="",
                stdout=response.stdout,
                stderr=response.stderr,
                execution_error=response.execution_error,
            )
        output = response.raw_output
        if output is None:
            output = json.dumps(
                response.proposal or {}, ensure_ascii=False, sort_keys=True
            )
        return AgentResult(
            engine=self.name,
            engine_version=self.version,
            execution_succeeded=True,
            output_text=output,
            stdout=response.stdout,
            stderr=response.stderr,
        )
