"""Deterministic transport stubs for REST and inert CLI protocol adapters."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from vibereview.runtime import external_worker


def _executable(path: Path, source: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
    path.chmod(0o700)
    return path


def test_deepseek_rest_is_literal_bounded_and_no_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = tmp_path / "credential"
    credential.write_text("deepseek-secret", encoding="utf-8")
    monkeypatch.setattr(external_worker, "_FIXED_CREDENTIAL_PATH", credential)
    monkeypatch.setenv("VIBEREVIEW_FIXED_CREDENTIAL", str(credential))
    captured: dict[str, object] = {}

    class Response:
        status = 200
        payload = json.dumps(
            {
                "model": "deepseek-v4-flash",
                "choices": [{
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": ' ```json\n{"ok":true}\n```\n',
                    },
                }],
            }
        ).encode()
        headers = {"Content-Length": str(len(payload))}

        def read(self, amount):
            return self.payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    class Opener:
        def open(self, request, timeout):
            captured.update(request=request, timeout=timeout)
            return Response()

    monkeypatch.setattr(external_worker.urllib.request, "build_opener", lambda *handlers: Opener())
    result = external_worker._deepseek(
        {
            "mode": "deepseek",
            "endpoint": "https://api.deepseek.com/chat/completions",
            "model": "deepseek-v4-flash",
            "max_transport_bytes": 1024,
            "timeout_seconds": 5,
            "max_tokens": 100,
        },
        {"system_prompt": "SYSTEM", "user_prompt": "USER"},
    )
    assert result == b' ```json\n{"ok":true}\n```\n'
    request = captured["request"]
    body = json.loads(request.data)
    assert body["messages"] == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "USER"},
    ]
    assert body["max_tokens"] == 100
    assert body["response_format"] == {"type": "json_object"}
    assert body["stream"] is False
    assert request.full_url == "https://api.deepseek.com/chat/completions"
    assert request.get_header("Authorization") == "Bearer deepseek-secret"


def test_deepseek_redirect_non_success_oversize_and_malformed_are_provider_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = tmp_path / "credential"
    credential.write_text("deepseek-secret", encoding="utf-8")
    monkeypatch.setattr(external_worker, "_FIXED_CREDENTIAL_PATH", credential)
    monkeypatch.setenv("VIBEREVIEW_FIXED_CREDENTIAL", str(credential))
    config = {
        "mode": "deepseek",
        "endpoint": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-v4-flash",
        "max_transport_bytes": 1_024,
        "timeout_seconds": 5,
        "max_tokens": 100,
    }
    request = {"system_prompt": "JSON", "user_prompt": "USER"}

    with pytest.raises(external_worker.ProviderFailure, match="redirects"):
        external_worker._NoRedirect().redirect_request(
            None, None, 302, "redirect", {}, "https://evil.invalid"
        )

    class Response:
        def __init__(self, status: int, payload: bytes):
            self.status = status
            self.payload = payload
            self.headers = {"Content-Length": str(len(payload))}

        def read(self, amount):
            return self.payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    for response, message in (
        (Response(503, b"{}"), "non-success"),
        (Response(200, b"x" * 1_025), "byte bound"),
        (Response(200, b"not-json"), "envelope"),
    ):
        class Opener:
            def open(self, http_request, timeout, *, response=response):
                return response

        monkeypatch.setattr(
            external_worker.urllib.request,
            "build_opener",
            lambda *handlers, opener=Opener(): opener,
        )
        with pytest.raises(external_worker.ProviderFailure, match=message):
            external_worker._deepseek(config, request)


def _deepseek_test_config(*, bound: int = 4_096) -> dict[str, object]:
    return {
        "mode": "deepseek",
        "endpoint": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-v4-flash",
        "max_transport_bytes": bound,
        "timeout_seconds": 5,
        "max_tokens": 100,
    }


def _install_deepseek_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = tmp_path / "credential"
    credential.write_text("deepseek-secret", encoding="utf-8")
    monkeypatch.setattr(external_worker, "_FIXED_CREDENTIAL_PATH", credential)
    monkeypatch.setenv("VIBEREVIEW_FIXED_CREDENTIAL", str(credential))


@pytest.mark.parametrize(
    "timeout", [0, -1, True, 240.0001, float("inf"), float("nan"), "5"]
)
def test_deepseek_worker_rejects_invalid_or_unbounded_timeout(timeout: object) -> None:
    config = _deepseek_test_config()
    config["timeout_seconds"] = timeout

    with pytest.raises(external_worker.WorkerFailure, match="timeout"):
        external_worker._deepseek(
            config, {"system_prompt": "JSON", "user_prompt": ""}
        )


def _install_deepseek_response(
    monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    class Response:
        status = 200
        headers = {"Content-Length": str(len(payload))}

        def read(self, amount):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    class Opener:
        def open(self, request, timeout):
            return Response()

    monkeypatch.setattr(
        external_worker.urllib.request, "build_opener", lambda *handlers: Opener()
    )


def _deepseek_envelope(
    content: str,
    *,
    finish_reason: object = "stop",
    model: object = "deepseek-v4-flash",
    index: object = 0,
    include_finish_reason: bool = True,
    role: object = "assistant",
    include_role: bool = True,
) -> bytes:
    message = {"content": content}
    if include_role:
        message["role"] = role
    choice = {"index": index, "message": message}
    if include_finish_reason:
        choice["finish_reason"] = finish_reason
    return json.dumps(
        {"model": model, "choices": [choice]},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")


def test_deepseek_non_success_body_is_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_deepseek_credential(tmp_path, monkeypatch)

    class Response:
        status = 401
        headers = {"Content-Length": "999"}

        def read(self, amount):
            raise AssertionError("provider error body must never be read")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    class Opener:
        def open(self, request, timeout):
            return Response()

    monkeypatch.setattr(
        external_worker.urllib.request, "build_opener", lambda *handlers: Opener()
    )
    with pytest.raises(external_worker.ProviderFailure, match="non-success"):
        external_worker._deepseek(
            _deepseek_test_config(), {"system_prompt": "JSON", "user_prompt": ""}
        )


@pytest.mark.parametrize(
    "failure",
    [external_worker.urllib.error.URLError("offline"), TimeoutError("timeout")],
)
def test_deepseek_transport_errors_are_provider_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    _install_deepseek_credential(tmp_path, monkeypatch)

    class Opener:
        def open(self, request, timeout):
            raise failure

    monkeypatch.setattr(
        external_worker.urllib.request, "build_opener", lambda *handlers: Opener()
    )
    with pytest.raises(external_worker.ProviderFailure, match="transport failed"):
        external_worker._deepseek(
            _deepseek_test_config(), {"system_prompt": "JSON", "user_prompt": ""}
        )


@pytest.mark.parametrize("declared", [True, False])
def test_deepseek_rejects_declared_and_chunked_oversized_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, declared: bool
) -> None:
    _install_deepseek_credential(tmp_path, monkeypatch)
    bound = 1_024

    class Response:
        status = 200
        headers = {"Content-Length": str(bound + 1)} if declared else {}

        def read(self, amount):
            return b"x" * (bound + 1)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    class Opener:
        def open(self, request, timeout):
            return Response()

    monkeypatch.setattr(
        external_worker.urllib.request, "build_opener", lambda *handlers: Opener()
    )
    with pytest.raises(external_worker.ProviderFailure, match="byte bound"):
        external_worker._deepseek(
            _deepseek_test_config(bound=bound),
            {"system_prompt": "JSON", "user_prompt": ""},
        )


@pytest.mark.parametrize("declared_delta", [-1, 1])
def test_deepseek_rejects_content_length_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declared_delta: int,
) -> None:
    _install_deepseek_credential(tmp_path, monkeypatch)
    payload = _deepseek_envelope('{"ok":true}')

    class Response:
        status = 200
        headers = {"Content-Length": str(len(payload) + declared_delta)}

        def read(self, amount):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    class Opener:
        def open(self, request, timeout):
            return Response()

    monkeypatch.setattr(
        external_worker.urllib.request, "build_opener", lambda *handlers: Opener()
    )
    with pytest.raises(external_worker.ProviderFailure, match="Content-Length"):
        external_worker._deepseek(
            _deepseek_test_config(), {"system_prompt": "JSON", "user_prompt": ""}
        )


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"[]",
        b"{}",
        b'{"choices":[],"choices":[]}',
        b'{"choices":NaN}',
        b'{"choices":[]}',
        b'{"choices":[{},{}]}',
        b'{"choices":[{}]}',
        b'{"choices":[{"message":{}}]}',
        b'{"choices":[{"message":{"content":7}}]}',
    ],
)
def test_deepseek_rejects_invalid_response_envelopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    _install_deepseek_credential(tmp_path, monkeypatch)

    class Response:
        status = 200
        headers = {"Content-Length": str(len(payload))}

        def read(self, amount):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    class Opener:
        def open(self, request, timeout):
            return Response()

    monkeypatch.setattr(
        external_worker.urllib.request, "build_opener", lambda *handlers: Opener()
    )
    with pytest.raises(external_worker.ProviderFailure):
        external_worker._deepseek(
            _deepseek_test_config(), {"system_prompt": "JSON", "user_prompt": ""}
        )


def test_deepseek_rejects_content_over_bound_after_envelope_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_deepseek_credential(tmp_path, monkeypatch)
    payload = _deepseek_envelope("x" * 1_025)

    class Response:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    class Opener:
        def open(self, request, timeout):
            return Response()

    monkeypatch.setattr(
        external_worker.urllib.request, "build_opener", lambda *handlers: Opener()
    )
    monkeypatch.setattr(external_worker, "_read_bounded_response", lambda *args: payload)
    with pytest.raises(external_worker.ProviderFailure, match="content"):
        external_worker._deepseek(
            _deepseek_test_config(bound=1_024),
            {"system_prompt": "JSON", "user_prompt": ""},
        )


@pytest.mark.parametrize(
    ("finish_reason", "include_finish_reason"),
    [
        (None, False),
        (None, True),
        ("length", True),
        ("content_filter", True),
        ("tool_calls", True),
        ("insufficient_system_resource", True),
        ("unknown", True),
    ],
)
def test_deepseek_rejects_nonstop_or_missing_termination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    finish_reason: object,
    include_finish_reason: bool,
) -> None:
    _install_deepseek_credential(tmp_path, monkeypatch)
    _install_deepseek_response(
        monkeypatch,
        _deepseek_envelope(
            '{"themes":[],"claims":[]}',
            finish_reason=finish_reason,
            include_finish_reason=include_finish_reason,
        ),
    )
    with pytest.raises(external_worker.ProviderFailure, match="terminate"):
        external_worker._deepseek(
            _deepseek_test_config(), {"system_prompt": "JSON", "user_prompt": ""}
        )


@pytest.mark.parametrize(
    ("model", "index"),
    [
        ("deepseek-v4-pro", 0),
        (None, 0),
        ("deepseek-v4-flash", 1),
        ("deepseek-v4-flash", True),
        ("deepseek-v4-flash", "0"),
    ],
)
def test_deepseek_binds_response_model_and_exact_choice_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: object,
    index: object,
) -> None:
    _install_deepseek_credential(tmp_path, monkeypatch)
    _install_deepseek_response(
        monkeypatch, _deepseek_envelope('{"ok":true}', model=model, index=index)
    )
    with pytest.raises(external_worker.ProviderFailure, match="model|index"):
        external_worker._deepseek(
            _deepseek_test_config(), {"system_prompt": "JSON", "user_prompt": ""}
        )


@pytest.mark.parametrize(
    ("role", "include_role"),
    [(None, False), (None, True), ("user", True), ("system", True)],
)
def test_deepseek_requires_exact_assistant_message_role(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: object,
    include_role: bool,
) -> None:
    _install_deepseek_credential(tmp_path, monkeypatch)
    _install_deepseek_response(
        monkeypatch,
        _deepseek_envelope('{"ok":true}', role=role, include_role=include_role),
    )
    with pytest.raises(external_worker.ProviderFailure, match="role"):
        external_worker._deepseek(
            _deepseek_test_config(), {"system_prompt": "JSON", "user_prompt": ""}
        )


def test_deepseek_invalid_unicode_content_is_a_provider_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_deepseek_credential(tmp_path, monkeypatch)
    _install_deepseek_response(monkeypatch, _deepseek_envelope("\ud800"))
    with pytest.raises(external_worker.ProviderFailure, match="content"):
        external_worker._deepseek(
            _deepseek_test_config(), {"system_prompt": "JSON", "user_prompt": ""}
        )


def test_proposal_write_preserves_literal_model_bytes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "output").mkdir()
    monkeypatch.delenv("VIBEREVIEW_FIXED_CREDENTIAL", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_MODEL_API_KEY", raising=False)
    raw = b' \n{ "answer" : [1, 2] }\n\n'
    external_worker._write_proposal(raw, 1_024)
    assert (tmp_path / "output/proposal.json").read_bytes() == raw


def test_proposal_write_rejects_even_a_short_exact_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "output").mkdir()
    monkeypatch.delenv("VIBEREVIEW_FIXED_CREDENTIAL", raising=False)
    monkeypatch.setenv("CODEX_API_KEY", "x")
    monkeypatch.delenv("KIMI_MODEL_API_KEY", raising=False)

    with pytest.raises(external_worker.WorkerFailure, match="credential material"):
        external_worker._write_proposal(b'{"answer":"x"}', 1_024)
    assert not (tmp_path / "output/proposal.json").exists()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (external_worker.ProviderFailure("provider"), 1),
        (external_worker.WorkerFailure("invariant"), external_worker.INTERNAL_FAILURE_EXIT),
    ],
)
def test_worker_main_distinguishes_provider_and_trusted_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected: int,
) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "worker.json"
    config.write_text(
        json.dumps(
            {
                "mode": "deepseek",
                "endpoint": "https://api.deepseek.com/chat/completions",
                "model": "deepseek-v4-flash",
                "max_transport_bytes": 128,
                "timeout_seconds": 5,
                "max_tokens": 100,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        external_worker,
        "_compiled_request",
        lambda path: ({"system_prompt": "JSON", "user_prompt": "USER"}, "prompt"),
    )
    monkeypatch.setattr(
        external_worker,
        "_deepseek",
        lambda *args, **kwargs: (_ for _ in ()).throw(failure),
    )
    assert external_worker.main(["--config", str(config), "--mode", "deepseek"]) == expected


def test_bounded_cli_drains_both_streams_and_rejects_flood(tmp_path: Path) -> None:
    stub = _executable(
        tmp_path / "flood",
        "import sys\nsys.stdout.write('x'*100)\nsys.stderr.write('y'*100)\n",
    )
    with pytest.raises(external_worker.ProviderFailure, match="byte bound"):
        external_worker._run_cli_bounded(
            [str(stub)], b"prompt", bound=128, timeout=2
        )


def test_bounded_cli_timeout_kills_child(tmp_path: Path) -> None:
    stub = _executable(tmp_path / "slow", "import time\ntime.sleep(30)\n")
    with pytest.raises(external_worker.ProviderFailure, match="timed out"):
        external_worker._run_cli_bounded(
            [str(stub)], b"prompt", bound=128, timeout=0.1
        )


def test_agy_uses_locked_model_plan_mode_and_literal_json(tmp_path: Path) -> None:
    record = tmp_path / "agy-argv.json"
    stub = _executable(
        tmp_path / "agy",
        "import json,os,sys\n"
        "open(os.environ['ARGV_RECORD'],'w').write(json.dumps(sys.argv[1:]))\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'result': json.dumps({'ok': 'agy'})}))\n",
    )
    os.environ["ARGV_RECORD"] = str(record)
    try:
        result = external_worker._agy(
            {
                "mode": "agy", "executable": str(stub),
                "model": "gemini-3.8-flash-high", "max_transport_bytes": 4096,
                "child_timeout_seconds": 2, "schema_path": "/schema.json",
            },
            "complete prompt",
        )
    finally:
        os.environ.pop("ARGV_RECORD", None)
    assert result == b'{"ok": "agy"}'
    argv = json.loads(record.read_text(encoding="utf-8"))
    assert ["--mode", "plan"] == argv[argv.index("--mode") : argv.index("--mode") + 2]
    assert argv[argv.index("--model") + 1] == "gemini-3.8-flash-high"
    assert "--dangerously-skip-permissions" not in argv


def test_codex_forces_no_shell_environment_inheritance(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    record = tmp_path / "codex-argv.json"
    stub = _executable(
        tmp_path / "codex",
        "import json,os,sys\n"
        "open(os.environ['ARGV_RECORD'],'w').write(json.dumps(sys.argv[1:]))\n"
        "args=sys.argv[1:]\n"
        "out=args[args.index('--output-last-message')+1]\n"
        "open(out,'w').write(json.dumps({'ok':'codex'}))\n"
        "sys.stdin.read()\n",
    )
    (tmp_path / "scratch").mkdir()
    monkeypatch.setenv("ARGV_RECORD", str(record))
    result = external_worker._codex(
        {
            "mode": "codex", "executable": str(stub), "model": "test-model",
            "max_transport_bytes": 4096, "child_timeout_seconds": 2,
            "schema_path": "/schema.json", "result_path": "scratch/codex-result.json",
        },
        "complete prompt",
    )
    assert result == b'{"ok": "codex"}'
    argv = json.loads(record.read_text(encoding="utf-8"))
    assert 'shell_environment_policy.inherit="none"' in argv
    assert "--ephemeral" in argv
    assert "--ignore-user-config" in argv


def _kimi_stub(path: Path, *, reverse: bool = False) -> Path:
    prompt_reply = (
        "print(json.dumps({'jsonrpc':'2.0','id':99,'method':'terminal/create','params':{}}),flush=True)"
        if reverse
        else "print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'{\\\"ok\\\":\\\"kimi\\\"}'}}}}),flush=True); print(json.dumps({'jsonrpc':'2.0','id':3,'result':{'stopReason':'end_turn'}}),flush=True)"
    )
    return _executable(
        path,
        "import json,sys\n"
        "for line in sys.stdin:\n"
        " m=json.loads(line); i=m['id']\n"
        " if i==1: print(json.dumps({'jsonrpc':'2.0','id':1,'result':{}}),flush=True)\n"
        " elif i==2: print(json.dumps({'jsonrpc':'2.0','id':2,'result':{'sessionId':'S'}}),flush=True)\n"
        f" elif i==3: {prompt_reply}\n"
        " elif i==4: print(json.dumps({'jsonrpc':'2.0','id':4,'result':{}}),flush=True); break\n",
    )


def test_kimi_acp_collects_chunks_and_closes_session(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "kimi-home"
    monkeypatch.setattr(external_worker, "_KIMI_CODE_HOME", home)
    monkeypatch.setenv("KIMI_CODE_HOME", str(home))
    result = external_worker._kimi(
        {
            "mode": "kimi_acp", "executable": str(_kimi_stub(tmp_path / "kimi")),
            "model": "explicit", "max_transport_bytes": 16_384,
            "child_timeout_seconds": 2,
        },
        "complete prompt",
    )
    assert result == b'{"ok":"kimi"}'
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


def test_kimi_acp_rejects_reverse_rpc_without_servicing_it(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "kimi-home"
    monkeypatch.setattr(external_worker, "_KIMI_CODE_HOME", home)
    monkeypatch.setenv("KIMI_CODE_HOME", str(home))
    with pytest.raises(external_worker.WorkerFailure, match="reverse RPC"):
        external_worker._kimi(
            {
                "mode": "kimi_acp",
                "executable": str(_kimi_stub(tmp_path / "kimi-reverse", reverse=True)),
                "model": "explicit", "max_transport_bytes": 16_384,
                "child_timeout_seconds": 2,
            },
            "complete prompt",
        )


@pytest.mark.parametrize("kind", ["stderr_flood", "timeout", "message_count"])
def test_kimi_acp_transport_fails_closed_under_protocol_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    home = tmp_path / "kimi-home"
    monkeypatch.setattr(external_worker, "_KIMI_CODE_HOME", home)
    monkeypatch.setenv("KIMI_CODE_HOME", str(home))
    if kind == "stderr_flood":
        stub = _executable(
            tmp_path / "kimi-stderr",
            "import sys,time\nsys.stderr.write('x'*20000);sys.stderr.flush();time.sleep(30)\n",
        )
        bound = 1_024
        timeout = 2
        message = "byte bound"
    elif kind == "timeout":
        stub = _executable(tmp_path / "kimi-timeout", "import time\ntime.sleep(30)\n")
        bound = 16_384
        timeout = 0.1
        message = "timed out"
    else:
        stub = _kimi_stub(tmp_path / "kimi-count")
        monkeypatch.setattr(external_worker, "MAX_PROTOCOL_MESSAGES", 2)
        bound = 16_384
        timeout = 2
        message = "message-count"
    with pytest.raises(external_worker.ProviderFailure, match=message):
        external_worker._kimi(
            {
                "mode": "kimi_acp", "executable": str(stub), "model": "explicit",
                "max_transport_bytes": bound, "child_timeout_seconds": timeout,
            },
            "complete prompt",
        )
