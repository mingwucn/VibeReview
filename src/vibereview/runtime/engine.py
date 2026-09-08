"""Bounded semantic-engine interface and deterministic MockEngine."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from pydantic import ConfigDict

from .records import AgentResult, AgentTask, RuntimeModel


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
        self.name = name
        self.version = version
        self._responses = list(responses)
        self._lock = threading.Lock()
        self._calls = 0
        self._on_execute = on_execute

    @property
    def calls(self) -> int:
        with self._lock:
            return self._calls

    def safe_configuration(self) -> Mapping[str, Any]:
        return {"engine": "deterministic-mock", "version": self.version}

    def execute(self, task: AgentTask) -> AgentResult:
        with self._lock:
            call_number = self._calls
            self._calls += 1
            if not self._responses:
                response = MockResponse(execution_error="no scripted response remains")
            elif len(self._responses) == 1:
                response = self._responses[0]
            else:
                response = self._responses.pop(0)
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

