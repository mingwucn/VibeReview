#!/usr/bin/env python3
"""Trusted, bounded worker for the provider-specific runtime adapters.

The worker is copied into the read-only launcher mount.  It consumes the one
provider-neutral compiled request and writes only ``output/proposal.json``.
Provider diagnostics are never forwarded to stdout/stderr.
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import selectors
import select
import signal
import ssl
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


MAX_CONFIG_BYTES = 65_536
MAX_COMPILED_REQUEST_BYTES = 524_288
MAX_TRANSPORT_BYTES = 524_288
MAX_PROTOCOL_MESSAGES = 4_096
INTERNAL_FAILURE_EXIT = 78
_DEEPSEEK_CHAT_MODELS = frozenset({"deepseek-v4-flash", "deepseek-v4-pro"})
_DEEPSEEK_MAX_TIMEOUT_SECONDS = 240.0
_KIMI_CODE_HOME = Path("/work/home/kimi-code")
_FIXED_CREDENTIAL_PATH = Path("/work/credentials/credential")


class WorkerFailure(RuntimeError):
    pass


class ProviderFailure(RuntimeError):
    """A provider/CLI technical failure that may use the configured fallback."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise ProviderFailure("redirects are forbidden")


def _read_regular(path: Path, bound: int, label: str) -> bytes:
    try:
        item = os.lstat(path)
    except OSError as exc:
        raise WorkerFailure(f"{label} is unavailable") from exc
    if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1 or item.st_size > bound:
        raise WorkerFailure(f"{label} is unsafe or oversized")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_nlink,
        ) != (
            item.st_dev,
            item.st_ino,
            item.st_size,
            item.st_mtime_ns,
            item.st_nlink,
        ):
            raise WorkerFailure(f"{label} changed while opening")
        chunks: list[bytes] = []
        observed = 0
        while True:
            chunk = os.read(descriptor, min(65_536, bound + 1 - observed))
            if not chunk:
                break
            observed += len(chunk)
            if observed > bound:
                raise WorkerFailure(f"{label} exceeds its byte bound")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            observed != opened.st_size
            or (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_nlink,
            )
            != (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_nlink,
            )
        ):
            raise WorkerFailure(f"{label} changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _json_object(raw: bytes, label: str) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise WorkerFailure(f"{label} is not literal UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise WorkerFailure(f"{label} JSON root must be an object")
    return value


def _compiled_request(path: Path) -> tuple[dict[str, Any], str]:
    value = _json_object(
        _read_regular(path, MAX_COMPILED_REQUEST_BYTES, "compiled request"),
        "compiled request",
    )
    required = {
        "task_type",
        "system_prompt",
        "user_prompt",
        "proposal_schema",
        "sources",
        "source_bytes",
        "compiled_bytes",
        "request_digest",
    }
    if set(value) != required:
        raise WorkerFailure("compiled request has unexpected fields")
    for field in ("system_prompt", "user_prompt", "request_digest"):
        if not isinstance(value[field], str):
            raise WorkerFailure(f"compiled request {field} is invalid")
    if not isinstance(value["proposal_schema"], dict):
        raise WorkerFailure("compiled request proposal schema is invalid")
    prompt = f'{value["system_prompt"]}\n\n{value["user_prompt"]}'
    if len(prompt.encode("utf-8")) > MAX_COMPILED_REQUEST_BYTES:
        raise WorkerFailure("compiled CLI prompt exceeds its byte bound")
    return value, prompt


def _write_proposal(raw: bytes, bound: int) -> None:
    if len(raw) > min(bound, MAX_TRANSPORT_BYTES):
        raise WorkerFailure("proposal exceeds its byte bound")
    secret_values: list[bytes] = []
    credential_path = os.environ.get("VIBEREVIEW_FIXED_CREDENTIAL")
    if credential_path:
        try:
            secret_values.append(
                _read_regular(Path(credential_path), 1_048_576, "fixed credential")
            )
        except WorkerFailure:
            raise
    for name in ("CODEX_API_KEY", "KIMI_MODEL_API_KEY"):
        value_text = os.environ.get(name)
        if value_text:
            secret_values.append(value_text.encode("utf-8"))
    if any(secret and secret in raw for secret in secret_values):
        raise WorkerFailure("proposal contains credential material")
    target = Path("output/proposal.json")
    descriptor = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb", closefd=True) as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def _required_config(config: dict[str, Any], mode: str) -> dict[str, Any]:
    if config.get("mode") != mode:
        raise WorkerFailure("worker mode does not match trusted configuration")
    bound = config.get("max_transport_bytes")
    if (
        not isinstance(bound, int)
        or isinstance(bound, bool)
        or bound < 1
        or bound > MAX_TRANSPORT_BYTES
    ):
        raise WorkerFailure("invalid transport byte bound")
    return config


def _read_bounded_response(response: Any, bound: int) -> bytes:
    raw_length = response.headers.get("Content-Length")
    declared: int | None = None
    if raw_length is not None:
        try:
            declared = int(raw_length)
        except ValueError as exc:
            raise ProviderFailure("invalid response Content-Length") from exc
        if declared < 0 or declared > bound:
            raise ProviderFailure("response exceeds its declared byte bound")
    raw = response.read(bound + 1)
    if len(raw) > bound:
        raise ProviderFailure("response exceeds its byte bound")
    if declared is not None and len(raw) != declared:
        raise ProviderFailure("response body does not match Content-Length")
    return raw


def _deepseek(config: dict[str, Any], request: dict[str, Any]) -> bytes:
    allowed = {
        "mode", "endpoint", "model", "max_transport_bytes", "timeout_seconds",
        "max_tokens",
    }
    if set(config) != allowed:
        raise WorkerFailure("unexpected DeepSeek worker configuration")
    if config["endpoint"] != "https://api.deepseek.com/chat/completions":
        raise WorkerFailure("DeepSeek endpoint is not the approved literal endpoint")
    if config["model"] not in _DEEPSEEK_CHAT_MODELS:
        raise WorkerFailure("DeepSeek model is not in the trusted allowlist")
    timeout = config["timeout_seconds"]
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or timeout <= 0
        or timeout > _DEEPSEEK_MAX_TIMEOUT_SECONDS
        or not math.isfinite(timeout)
    ):
        raise WorkerFailure("invalid DeepSeek timeout")
    max_tokens = config["max_tokens"]
    if (
        not isinstance(max_tokens, int)
        or isinstance(max_tokens, bool)
        or max_tokens < 1
        or max_tokens > 8_192
    ):
        raise WorkerFailure("invalid DeepSeek output-token bound")
    credential_path = os.environ.get("VIBEREVIEW_FIXED_CREDENTIAL")
    if credential_path != str(_FIXED_CREDENTIAL_PATH):
        raise WorkerFailure("fixed DeepSeek credential path is unavailable")
    credential = _read_regular(_FIXED_CREDENTIAL_PATH, 16_384, "DeepSeek credential")
    try:
        token = credential.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkerFailure("DeepSeek credential is not UTF-8") from exc
    if not token or "\x00" in token or "\n" in token or "\r" in token:
        raise WorkerFailure("DeepSeek credential is not a safe scalar")
    body = json.dumps(
        {
            "model": config["model"],
            "messages": [
                {"role": "system", "content": request["system_prompt"]},
                {"role": "user", "content": request["user_prompt"]},
            ],
            "response_format": {"type": "json_object"},
            "stream": False,
            "max_tokens": max_tokens,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    bound = config["max_transport_bytes"]
    if len(body) > bound:
        raise WorkerFailure("DeepSeek request exceeds its byte bound")
    try:
        opener = urllib.request.build_opener(
            _NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )
        http_request = urllib.request.Request(
            config["endpoint"],
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "VibeReview/1",
            },
        )
        with opener.open(http_request, timeout=float(timeout)) as response:
            if response.status != http.client.OK:
                raise ProviderFailure("DeepSeek returned a non-success status")
            try:
                payload = _json_object(
                    _read_bounded_response(response, bound), "DeepSeek response"
                )
            except WorkerFailure as exc:
                raise ProviderFailure("DeepSeek response envelope is invalid") from exc
    except urllib.error.HTTPError as exc:
        # Deliberately do not read or expose a provider error body.
        raise ProviderFailure("DeepSeek returned a non-success status") from exc
    except urllib.error.URLError as exc:
        raise ProviderFailure("DeepSeek transport failed") from exc
    except (OSError, TimeoutError, UnicodeError, ValueError, http.client.HTTPException) as exc:
        raise ProviderFailure("DeepSeek transport failed") from exc
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ProviderFailure("DeepSeek response choices are invalid")
    if payload.get("model") != config["model"]:
        raise ProviderFailure("DeepSeek response model does not match the request")
    choice = choices[0]
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise ProviderFailure("DeepSeek response message is invalid")
    if choice["message"].get("role") != "assistant":
        raise ProviderFailure("DeepSeek response message role is invalid")
    if type(choice.get("index")) is not int or choice["index"] != 0:
        raise ProviderFailure("DeepSeek response choice index is invalid")
    if choice.get("finish_reason") != "stop":
        raise ProviderFailure("DeepSeek response did not terminate successfully")
    content = choice["message"].get("content")
    if not isinstance(content, str):
        raise ProviderFailure("DeepSeek response content is invalid or oversized")
    try:
        content_bytes = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProviderFailure("DeepSeek response content is invalid or oversized") from exc
    if len(content_bytes) > bound:
        raise ProviderFailure("DeepSeek response content is invalid or oversized")
    return content_bytes


def _terminate_child(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        process.kill()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _run_cli_bounded(
    argv: list[str], prompt: bytes, *, bound: int, timeout: float
) -> tuple[int, bytes, bytes]:
    if not argv or any("\x00" in item for item in argv):
        raise WorkerFailure("unsafe CLI argv")
    try:
        process = subprocess.Popen(
            argv,
            shell=False,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=dict(os.environ),
        )
    except OSError as exc:
        raise ProviderFailure("CLI executable could not be launched") from exc
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    streams = selectors.DefaultSelector()
    output = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    deadline = time.monotonic() + timeout
    try:
        for stream in (process.stdin, process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
        streams.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        streams.register(process.stdout, selectors.EVENT_READ, "stdout")
        streams.register(process.stderr, selectors.EVENT_READ, "stderr")
        sent = 0
        while streams.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderFailure("CLI transport timed out")
            events = streams.select(min(remaining, 0.1))
            if not events and process.poll() is not None:
                # Continue until both pipe EOFs are observed.
                continue
            for key, mask in events:
                stream = key.fileobj
                label = key.data
                if label == "stdin":
                    if sent == len(prompt):
                        streams.unregister(stream)
                        stream.close()
                        continue
                    try:
                        written = os.write(stream.fileno(), prompt[sent : sent + 65_536])
                    except BlockingIOError:
                        continue
                    sent += written
                    if sent == len(prompt):
                        streams.unregister(stream)
                        stream.close()
                    continue
                try:
                    chunk = os.read(stream.fileno(), 65_536)
                except BlockingIOError:
                    continue
                if not chunk:
                    streams.unregister(stream)
                    stream.close()
                    continue
                total += len(chunk)
                if total > bound:
                    raise ProviderFailure("CLI transport exceeded its byte bound")
                output[label].extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderFailure("CLI transport timed out")
        code = process.wait(timeout=remaining)
        return code, bytes(output["stdout"]), bytes(output["stderr"])
    except BaseException as exc:
        _terminate_child(process)
        if isinstance(exc, (WorkerFailure, ProviderFailure)):
            raise
        raise ProviderFailure("CLI transport failed") from exc
    finally:
        streams.close()


def _strict_cli_config(config: dict[str, Any], mode: str) -> tuple[int, float]:
    _required_config(config, mode)
    bound = config["max_transport_bytes"]
    timeout = config.get("child_timeout_seconds")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise WorkerFailure("invalid CLI child timeout")
    return bound, float(timeout)


def _agy(config: dict[str, Any], prompt: str) -> bytes:
    allowed = {
        "mode", "executable", "model", "max_transport_bytes",
        "child_timeout_seconds", "schema_path",
    }
    if set(config) != allowed or config["model"] != "gemini-3.8-flash-high":
        raise WorkerFailure("unexpected Agy worker configuration")
    bound, timeout = _strict_cli_config(config, "agy")
    argv = [
        config["executable"], "--print", "--mode", "plan", "--sandbox",
        "--disable-slash-commands", "--model", config["model"],
        "--output-format", "json", "--json-schema", config["schema_path"],
    ]
    code, stdout, _ = _run_cli_bounded(
        argv, prompt.encode("utf-8"), bound=bound, timeout=timeout
    )
    if code != 0:
        raise ProviderFailure("Agy CLI failed")
    try:
        envelope = _json_object(stdout, "Agy response")
    except WorkerFailure as exc:
        raise ProviderFailure("Agy response envelope is invalid") from exc
    content = envelope.get("result", envelope.get("response"))
    if not isinstance(content, str):
        raise ProviderFailure("Agy response lacks one final result")
    return content.encode("utf-8")


def _codex(config: dict[str, Any], prompt: str) -> bytes:
    allowed = {
        "mode", "executable", "model", "max_transport_bytes",
        "child_timeout_seconds", "schema_path", "result_path",
    }
    if set(config) != allowed:
        raise WorkerFailure("unexpected Codex worker configuration")
    bound, timeout = _strict_cli_config(config, "codex")
    result_path = Path(config["result_path"])
    if result_path != Path("scratch/codex-result.json"):
        raise WorkerFailure("unexpected Codex result path")
    argv = [
        config["executable"], "exec", "--ephemeral", "--ignore-user-config",
        "--ignore-rules", "--strict-config", "--skip-git-repo-check",
        "--sandbox", "read-only", "--model", config["model"],
        "--config", 'shell_environment_policy.inherit="none"',
        "--output-schema", config["schema_path"], "--color", "never",
        "--output-last-message", str(result_path), "-",
    ]
    code, _, _ = _run_cli_bounded(
        argv, prompt.encode("utf-8"), bound=bound, timeout=timeout
    )
    if code != 0:
        raise ProviderFailure("Codex CLI failed")
    try:
        return _read_regular(result_path, bound, "Codex final result")
    except WorkerFailure as exc:
        raise ProviderFailure("Codex final result is unavailable") from exc


class _AcpTransport:
    """Deadline-bound, incrementally drained newline-delimited ACP transport."""

    def __init__(self, process: subprocess.Popen[bytes], *, bound: int, timeout: float):
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        self.process = process
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.stderr = process.stderr
        self.bound = bound
        self.deadline = time.monotonic() + timeout
        self.observed = 0
        self.messages = 0
        self.pending = bytearray()
        self.queue: list[dict[str, Any]] = []
        self.chunks: list[str] = []
        self.selector = selectors.DefaultSelector()
        for stream in (self.stdout, self.stderr):
            os.set_blocking(stream.fileno(), False)
            self.selector.register(stream, selectors.EVENT_READ)

    def close(self) -> None:
        self.selector.close()

    def send(self, message: dict[str, Any]) -> None:
        raw = json.dumps(message, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(raw) > self.bound:
            raise WorkerFailure("ACP request exceeds its byte bound")
        descriptor = self.stdin.fileno()
        os.set_blocking(descriptor, False)
        sent = 0
        while sent < len(raw):
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderFailure("Kimi ACP timed out")
            _, writable, _ = select.select([], [descriptor], [], min(remaining, 0.1))
            if not writable:
                continue
            try:
                sent += os.write(descriptor, raw[sent : sent + 65_536])
            except BlockingIOError:
                continue

    def _message(self, raw: bytes) -> dict[str, Any]:
        self.messages += 1
        if self.messages > MAX_PROTOCOL_MESSAGES:
            raise ProviderFailure("Kimi ACP message-count bound exceeded")
        try:
            return _json_object(raw, "Kimi ACP message")
        except WorkerFailure as exc:
            raise ProviderFailure("Kimi ACP message is invalid") from exc

    def _drain(self) -> None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderFailure("Kimi ACP timed out")
        events = self.selector.select(min(remaining, 0.1))
        if not events:
            if self.process.poll() is not None:
                raise ProviderFailure("Kimi ACP exited before completing protocol")
            return
        for key, _ in events:
            stream = key.fileobj
            try:
                chunk = os.read(stream.fileno(), 65_536)
            except BlockingIOError:
                continue
            if not chunk:
                self.selector.unregister(stream)
                continue
            self.observed += len(chunk)
            if self.observed > self.bound:
                raise ProviderFailure("Kimi ACP output exceeds its byte bound")
            if stream is self.stderr:
                continue
            self.pending.extend(chunk)
            while b"\n" in self.pending:
                raw, _, rest = self.pending.partition(b"\n")
                self.pending = bytearray(rest)
                if raw:
                    self.queue.append(self._message(raw))

    def receive(self, expected_id: int) -> dict[str, Any]:
        while True:
            if not self.queue:
                self._drain()
                continue
            message = self.queue.pop(0)
            method = message.get("method")
            if method is not None and "id" in message:
                raise WorkerFailure("Kimi ACP reverse RPC is forbidden")
            if message.get("id") == expected_id:
                if "error" in message:
                    raise ProviderFailure("Kimi ACP returned an error")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise ProviderFailure("Kimi ACP response result is invalid")
                return result
            if method != "session/update":
                raise WorkerFailure("Kimi ACP emitted an unknown notification")
            params = message.get("params")
            update = params.get("update") if isinstance(params, dict) else None
            if not isinstance(update, dict) or update.get("sessionUpdate") != "agent_message_chunk":
                raise WorkerFailure("Kimi ACP emitted forbidden non-message activity")
            content = update.get("content")
            if isinstance(content, dict):
                text = content.get("text")
            else:
                text = content
            if not isinstance(text, str):
                raise ProviderFailure("Kimi ACP message chunk is invalid")
            if sum(len(item.encode("utf-8")) for item in self.chunks) + len(text.encode("utf-8")) > self.bound:
                raise ProviderFailure("Kimi ACP message content exceeds its byte bound")
            self.chunks.append(text)


def _kimi(config: dict[str, Any], prompt: str) -> bytes:
    allowed = {
        "mode", "executable", "model", "max_transport_bytes",
        "child_timeout_seconds",
    }
    if set(config) != allowed:
        raise WorkerFailure("unexpected Kimi ACP worker configuration")
    bound, timeout = _strict_cli_config(config, "kimi_acp")
    if os.environ.get("KIMI_CODE_HOME") != str(_KIMI_CODE_HOME):
        raise WorkerFailure("Kimi code home is not the isolated attempt path")
    try:
        home_item = os.lstat(_KIMI_CODE_HOME)
    except FileNotFoundError:
        _KIMI_CODE_HOME.mkdir(mode=0o700, parents=True)
    else:
        if stat.S_ISLNK(home_item.st_mode) or not stat.S_ISDIR(home_item.st_mode):
            raise WorkerFailure("Kimi code home is unsafe")
        if any(_KIMI_CODE_HOME.iterdir()):
            raise WorkerFailure("Kimi code home must start empty and tool-free")
    os.chmod(_KIMI_CODE_HOME, 0o700)
    try:
        process = subprocess.Popen(
            [config["executable"], "acp"],
            shell=False,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=dict(os.environ),
        )
    except OSError as exc:
        raise ProviderFailure("Kimi CLI executable could not be launched") from exc
    assert process.stdin is not None and process.stdout is not None
    transport = _AcpTransport(process, bound=bound, timeout=timeout)
    try:
        transport.send(
            {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientInfo": {"name": "vibereview", "version": "1"},
                    "clientCapabilities": {},
                },
            },
        )
        transport.receive(1)
        transport.send(
            {
                "jsonrpc": "2.0", "id": 2, "method": "session/new",
                "params": {"cwd": "/work", "mcpServers": []},
            },
        )
        session = transport.receive(2).get("sessionId")
        if not isinstance(session, str) or not session:
            raise ProviderFailure("Kimi ACP session id is invalid")
        transport.send(
            {
                "jsonrpc": "2.0", "id": 3, "method": "session/prompt",
                "params": {
                    "sessionId": session,
                    "prompt": [{"type": "text", "text": prompt}],
                    "model": config["model"],
                },
            },
        )
        result = transport.receive(3)
        content = result.get("content", result.get("text"))
        if content is None:
            content = "".join(transport.chunks)
        if not isinstance(content, str):
            raise ProviderFailure("Kimi ACP final response is invalid")
        proposal = content.encode("utf-8")
        transport.send(
            {
                "jsonrpc": "2.0", "id": 4, "method": "session/close",
                "params": {"sessionId": session},
            }
        )
        transport.receive(4)
        process.stdin.close()
        try:
            exit_code = process.wait(timeout=2)
        except subprocess.TimeoutExpired as exc:
            raise ProviderFailure("Kimi ACP did not exit after input close") from exc
        if exit_code != 0:
            raise ProviderFailure("Kimi ACP CLI failed")
        return proposal
    finally:
        transport.close()
        _terminate_child(process)


def _offline(config: dict[str, Any]) -> bytes:
    allowed = {"mode", "fixture_path", "max_transport_bytes"}
    if set(config) != allowed:
        raise WorkerFailure("unexpected offline worker configuration")
    return _read_regular(
        Path(config["fixture_path"]), config["max_transport_bytes"], "offline fixture"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VibeReview external-engine worker")
    parser.add_argument("--config", required=True)
    parser.add_argument("--mode", action="append", default=[])
    args = parser.parse_args(argv)
    if len(args.mode) != 1:
        return INTERNAL_FAILURE_EXIT
    try:
        config = _json_object(
            _read_regular(Path(args.config), MAX_CONFIG_BYTES, "worker configuration"),
            "worker configuration",
        )
        mode = args.mode[0]
        _required_config(config, mode)
        request, prompt = _compiled_request(Path("launcher/compiled_request.json"))
        if mode == "offline":
            proposal = _offline(config)
        elif mode == "deepseek":
            proposal = _deepseek(config, request)
        elif mode == "agy":
            proposal = _agy(config, prompt)
        elif mode == "codex":
            proposal = _codex(config, prompt)
        elif mode == "kimi_acp":
            proposal = _kimi(config, prompt)
        else:
            raise WorkerFailure("unknown worker mode")
        _write_proposal(proposal, config["max_transport_bytes"])
        return 0
    except ProviderFailure:
        return 1
    except Exception:
        # Provider responses and credentials are intentionally not reflected.
        return INTERNAL_FAILURE_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
