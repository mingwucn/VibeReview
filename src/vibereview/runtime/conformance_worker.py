#!/usr/bin/env python3
"""Dedicated conformance worker for the programmatic sandbox suite (goal.md §6.1).

Executed by the runtime as a real OS process through the qualified
Bubblewrap backend:

```text
[sys.executable, /work/launcher/conformance_worker.py,
 "--config", "launcher/worker_config.json", "--mode", MODE, ...]
```

with ``cwd=<execution_root>`` and the cleared, reconstructed sandbox
environment. Multiple ``--mode`` flags compose in the order given. The
``valid`` mode copies ``launcher/valid_proposal.json`` into
``output/proposal.json``; every other mode only probes confinement behavior
and prints deterministic markers on stdout.

``worker_config.json`` keys (all optional):

```text
canary_read_path    canary_read target (host path that must be inaccessible)
canary_write_path   canary_write target (host path that must stay absent)
network_host        loopback_connect host
network_port        loopback_connect port
```

Everything emitted is deterministic: no randomness and no timestamps.
Pure stdlib; no review-project or test-helper imports.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

BUNDLE = Path("bundle")
LAUNCHER = Path("launcher")
OUTPUT = Path("output")
SCRATCH = Path("scratch")
CREDENTIALS = Path("credentials")
PROPOSAL = OUTPUT / "proposal.json"


class _FixtureError(RuntimeError):
    """The worker was invoked against an inconsistent execution root."""


def _load_config(path: str) -> dict:
    config_path = Path(path)
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _FixtureError(f"cannot read worker config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise _FixtureError("worker config must be a JSON object")
    return data


def _mode_valid(config: dict) -> None:
    source = LAUNCHER / "valid_proposal.json"
    if not source.is_file():
        raise _FixtureError("valid: launcher/valid_proposal.json does not exist")
    shutil.copyfile(source, PROPOSAL)


def _mode_canary_read(config: dict) -> None:
    target = config.get("canary_read_path")
    if not isinstance(target, str) or not target:
        raise _FixtureError("canary_read: canary_read_path not configured")
    try:
        content = Path(target).read_text(encoding="utf-8")
        print(f"CANARY_READ_OK={content}")
    except Exception as exc:
        print(f"CANARY_READ_ERROR={type(exc).__name__}")


def _mode_canary_write(config: dict) -> None:
    target = config.get("canary_write_path")
    if not isinstance(target, str) or not target:
        raise _FixtureError("canary_write: canary_write_path not configured")
    try:
        Path(target).write_text("pwned", encoding="utf-8")
        print(f"CANARY_WRITE_OK={target}")
    except Exception as exc:
        print(f"CANARY_WRITE_ERROR={type(exc).__name__}")


def _mode_write_scratch(config: dict) -> None:
    (SCRATCH / "conformance_scratch.txt").write_text(
        "conformance-scratch\n", encoding="utf-8"
    )
    print("SCRATCH_WRITTEN=conformance_scratch.txt")


def _mode_bundle_write(config: dict) -> None:
    target = BUNDLE / "write_probe.tmp"
    try:
        target.write_text("write-probe\n", encoding="utf-8")
        print("BUNDLE_WRITE_OK")
    except Exception as exc:
        print(f"BUNDLE_WRITE_ERROR={type(exc).__name__}")
    finally:
        if target.is_file():
            target.unlink()


def _mode_read_credentials(config: dict) -> None:
    files = sorted(CREDENTIALS.glob("*")) if CREDENTIALS.is_dir() else []
    if not files:
        print("FILE_CREDENTIAL=<none>")
        return
    for path in files:
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace")
            print(f"FILE_CREDENTIAL_NAME={path.name}")
            print(f"FILE_CREDENTIAL_CONTENT={content}")


def _mode_loopback_connect(config: dict) -> None:
    host = config.get("network_host")
    port = config.get("network_port")
    if not isinstance(host, str) or not host or not isinstance(port, int) or port <= 0:
        raise _FixtureError("loopback_connect: network_host/network_port not configured")
    try:
        with socket.create_connection((host, port), timeout=2.0) as connection:
            data = connection.recv(256)
        print(f"NETWORK_RESPONSE={data.decode('utf-8', errors='replace').strip()}")
    except Exception as exc:
        print(f"NETWORK_DENIED={type(exc).__name__}")


def _mode_spawn_descendants(config: dict) -> None:
    for _ in range(3):
        subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    print("DESCENDANTS_SPAWNED=3")


_MODE_HANDLERS = {
    "valid": _mode_valid,
    "canary_read": _mode_canary_read,
    "canary_write": _mode_canary_write,
    "write_scratch": _mode_write_scratch,
    "bundle_write": _mode_bundle_write,
    "read_credentials": _mode_read_credentials,
    "loopback_connect": _mode_loopback_connect,
    "spawn_descendants": _mode_spawn_descendants,
}

ALL_MODES = tuple(sorted(_MODE_HANDLERS))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="VibeReview sandbox conformance worker (goal.md §6.1)."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--mode", dest="modes", action="append", required=True, choices=ALL_MODES)
    args = parser.parse_args(argv)
    try:
        config = _load_config(args.config)
        for mode in args.modes:
            _MODE_HANDLERS[mode](config)
    except _FixtureError as exc:
        print(f"conformance_worker: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
