#!/usr/bin/env python3
"""Deterministic fake engine worker for the Milestone B2 subprocess boundary.

The runtime executes this file as a real OS process:

```text
[sys.executable, <abs path to this file>,
 "--config", "<execution_root>/launcher/worker_config.json",
 "--mode", MODE, ...]
```

with ``cwd=<execution_root>`` and a minimal environment. The execution root
contains ``bundle/`` (immutable input, normally left untouched), ``output/``,
``scratch/``, ``home/``, ``tmp/``, ``credentials/`` and ``launcher/``.

Multiple ``--mode`` flags compose in the order given. The process exit code
is 0 unless the ``nonzero`` mode was requested (exit 3). Fixture-usage
errors (malformed config, missing tamper targets, missing launcher payloads)
exit 2 with a diagnostic on stderr.

``worker_config.json`` keys (all optional):

```text
stream_bytes            (default 10 MiB)   large_stdout / large_stderr volume
proposal_size           (default 2 MiB)    oversized_proposal byte count
timeout_sleep_seconds   (default 60)       timeout / spawn_child_timeout sleep
parent_probe_var        (default VIBEREVIEW_PARENT_SECRET)
injected_probe_var      (default VIBEREVIEW_INJECTED_SECRET)
scratch_file_bytes      (default 8 MiB)    large_scratch_file byte count
file_count              (default 300)      too_many_files count
tree_depth              (default 12)       too_deep_tree depth
process_count           (default 64)       too_many_processes count
extra_output_name       (default extra.txt)
```

Everything emitted by this worker is deterministic: no randomness and no
timestamps appear in any file or stream content. Pure stdlib.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

BUNDLE = Path("bundle")
LAUNCHER = Path("launcher")
OUTPUT = Path("output")
SCRATCH = Path("scratch")
PROPOSAL = OUTPUT / "proposal.json"

TAMPER_SUFFIX = b"TAMPERED\n"

DEFAULT_STREAM_BYTES = 10 * 1024 * 1024
DEFAULT_PROPOSAL_SIZE = 2 * 1024 * 1024
DEFAULT_TIMEOUT_SLEEP_SECONDS = 60
DEFAULT_PARENT_PROBE_VAR = "VIBEREVIEW_PARENT_SECRET"
DEFAULT_INJECTED_PROBE_VAR = "VIBEREVIEW_INJECTED_SECRET"
DEFAULT_SCRATCH_FILE_BYTES = 8 * 1024 * 1024
DEFAULT_FILE_COUNT = 300
DEFAULT_TREE_DEPTH = 12
DEFAULT_PROCESS_COUNT = 64
DEFAULT_EXTRA_OUTPUT_NAME = "extra.txt"

MALFORMED_JSON_PAYLOAD = b'{"this is not json'
NON_UTF8_PAYLOAD = b"\xff\xfe\x00bad"
SCHEMA_INVALID_FALLBACK = b'{"unexpected": true}\n'
PROPOSAL_INVALID_FALLBACK = b'{"proposals": []}\n'
EXTRA_OUTPUT_PAYLOAD = b"unauthorized\n"

# Bundle layout mirrors TaskWorkspace.create in src/vibereview/runtime/tasks.py:
# bundle/instructions.md, bundle/bundle_manifest.json,
# bundle/contracts/{input,proposal}.schema.json,
# bundle/input/input.json, bundle/input/dependencies/<id>.json and
# bundle/input/resources/<RESxxxx>/content<ext>.
TAMPER_FIXED_TARGETS = {
    "tamper_instructions": BUNDLE / "instructions.md",
    "tamper_input": BUNDLE / "input" / "input.json",
    "tamper_input_schema": BUNDLE / "contracts" / "input.schema.json",
    "tamper_proposal_schema": BUNDLE / "contracts" / "proposal.schema.json",
    "tamper_bundle_manifest": BUNDLE / "bundle_manifest.json",
}
TAMPER_GLOB_ROOTS = {
    "tamper_dependency": BUNDLE / "input" / "dependencies",
    "tamper_resource": BUNDLE / "input" / "resources",
}


class _FixtureError(RuntimeError):
    """The worker was invoked against an inconsistent execution root."""


def _int_value(config: dict, key: str, default: int) -> int:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _FixtureError(f"config key {key!r} must be an integer")
    if value < 0:
        raise _FixtureError(f"config key {key!r} must be >= 0")
    return value


def _seconds_value(config: dict, key: str, default: float) -> float:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _FixtureError(f"config key {key!r} must be a number")
    if value < 0:
        raise _FixtureError(f"config key {key!r} must be >= 0")
    return float(value)


def _str_value(config: dict, key: str, default: str) -> str:
    value = config.get(key, default)
    if not isinstance(value, str):
        raise _FixtureError(f"config key {key!r} must be a string")
    return value


class _Context:
    def __init__(self, config: dict) -> None:
        self.stream_bytes = _int_value(config, "stream_bytes", DEFAULT_STREAM_BYTES)
        self.proposal_size = _int_value(
            config, "proposal_size", DEFAULT_PROPOSAL_SIZE
        )
        self.timeout_sleep_seconds = _seconds_value(
            config, "timeout_sleep_seconds", DEFAULT_TIMEOUT_SLEEP_SECONDS
        )
        self.parent_probe_var = _str_value(
            config, "parent_probe_var", DEFAULT_PARENT_PROBE_VAR
        )
        self.injected_probe_var = _str_value(
            config, "injected_probe_var", DEFAULT_INJECTED_PROBE_VAR
        )
        self.scratch_file_bytes = _int_value(
            config, "scratch_file_bytes", DEFAULT_SCRATCH_FILE_BYTES
        )
        self.file_count = _int_value(config, "file_count", DEFAULT_FILE_COUNT)
        self.tree_depth = _int_value(config, "tree_depth", DEFAULT_TREE_DEPTH)
        self.process_count = _int_value(
            config, "process_count", DEFAULT_PROCESS_COUNT
        )
        self.extra_output_name = _str_value(
            config, "extra_output_name", DEFAULT_EXTRA_OUTPUT_NAME
        )


def _load_config(path: str) -> dict:
    config_path = Path(path)
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _FixtureError(f"cannot read worker config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise _FixtureError(f"worker config {path} must be a JSON object")
    return data


def _first_bundle_file() -> Path:
    if not BUNDLE.is_dir():
        raise _FixtureError("bundle/ directory does not exist")
    candidates = sorted(
        (path for path in BUNDLE.rglob("*") if path.is_file()),
        key=lambda path: path.as_posix(),
    )
    if not candidates:
        raise _FixtureError("bundle/ contains no files")
    return candidates[0]


def _clear_proposal_path() -> Path:
    if PROPOSAL.is_symlink() or PROPOSAL.exists():
        PROPOSAL.unlink()
    return PROPOSAL


def _silence_stream(buffer) -> None:
    """Redirect a broken stream to devnull so interpreter exit stays clean."""

    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, buffer.fileno())
        finally:
            os.close(devnull)
    except OSError:
        pass


def _emit_stream(buffer, total: int, seed: bytes) -> None:
    chunk_size = 64 * 1024
    line = seed + b"\n"
    chunk = (line * (chunk_size // len(line) + 1))[:chunk_size]
    remaining = total
    try:
        while remaining > 0:
            piece = chunk if remaining >= chunk_size else chunk[:remaining]
            buffer.write(piece)
            remaining -= len(piece)
        buffer.flush()
    except BrokenPipeError:
        _silence_stream(buffer)


def _write_sized_file(path: Path, total: int, seed: bytes) -> None:
    chunk_size = 1024 * 1024
    line = seed + b"\n"
    chunk = (line * (chunk_size // len(line) + 1))[:chunk_size]
    remaining = total
    with path.open("wb") as handle:
        while remaining > 0:
            piece = chunk if remaining >= chunk_size else chunk[:remaining]
            handle.write(piece)
            remaining -= len(piece)


def _copy_or_write(source: Path, fallback: bytes) -> None:
    if source.is_file():
        shutil.copyfile(source, PROPOSAL)
    else:
        PROPOSAL.write_bytes(fallback)


def _reap(child: subprocess.Popen) -> None:
    if child.poll() is None:
        try:
            child.terminate()
        except OSError:
            pass
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def _mode_noop(ctx: _Context) -> None:
    # nonzero and missing_proposal need no filesystem action.
    return None


def _mode_valid(ctx: _Context) -> None:
    source = LAUNCHER / "valid_proposal.json"
    if not source.is_file():
        raise _FixtureError("valid: launcher/valid_proposal.json does not exist")
    shutil.copyfile(source, PROPOSAL)


def _mode_malformed_json(ctx: _Context) -> None:
    PROPOSAL.write_bytes(MALFORMED_JSON_PAYLOAD)


def _mode_schema_invalid(ctx: _Context) -> None:
    _copy_or_write(
        LAUNCHER / "schema_invalid_proposal.json", SCHEMA_INVALID_FALLBACK
    )


def _mode_proposal_invalid(ctx: _Context) -> None:
    _copy_or_write(
        LAUNCHER / "proposal_invalid_proposal.json", PROPOSAL_INVALID_FALLBACK
    )


def _mode_empty_proposal(ctx: _Context) -> None:
    PROPOSAL.write_bytes(b"")


def _mode_non_utf8_proposal(ctx: _Context) -> None:
    PROPOSAL.write_bytes(NON_UTF8_PAYLOAD)


def _mode_oversized_proposal(ctx: _Context) -> None:
    prefix = b'{"padding":"'
    suffix = b'"}'
    size = ctx.proposal_size
    if size <= len(prefix) + len(suffix):
        payload = (prefix + suffix)[:size]
    else:
        payload = prefix + b"A" * (size - len(prefix) - len(suffix)) + suffix
    PROPOSAL.write_bytes(payload)


def _mode_large_stdout(ctx: _Context) -> None:
    _emit_stream(sys.stdout.buffer, ctx.stream_bytes, b"vibereview-fake-stdout")


def _mode_large_stderr(ctx: _Context) -> None:
    _emit_stream(sys.stderr.buffer, ctx.stream_bytes, b"vibereview-fake-stderr")


def _mode_parent_secret_probe(ctx: _Context) -> None:
    print("PARENT_SECRET=" + (os.environ.get(ctx.parent_probe_var) or "<absent>"))


def _mode_injected_secret_probe(ctx: _Context) -> None:
    print(
        "INJECTED_SECRET=" + (os.environ.get(ctx.injected_probe_var) or "<absent>")
    )


def _mode_timeout(ctx: _Context) -> None:
    time.sleep(ctx.timeout_sleep_seconds)


def _mode_spawn_child_timeout(ctx: _Context) -> None:
    # The child stays in the worker's process group so a group-wide
    # SIGTERM/SIGKILL from the runtime terminates both processes.
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import time; time.sleep({ctx.timeout_sleep_seconds!r})",
        ]
    )
    try:
        time.sleep(ctx.timeout_sleep_seconds)
    finally:
        _reap(child)


def _tamper(mode_name: str) -> None:
    fixed = TAMPER_FIXED_TARGETS.get(mode_name)
    if fixed is not None:
        target = fixed
    else:
        root = TAMPER_GLOB_ROOTS[mode_name]
        candidates = (
            sorted(
                (path for path in root.rglob("*") if path.is_file()),
                key=lambda path: path.as_posix(),
            )
            if root.is_dir()
            else []
        )
        if not candidates:
            raise _FixtureError(f"{mode_name}: no files under {root.as_posix()}")
        target = candidates[0]
    if not target.is_file():
        raise _FixtureError(f"{mode_name}: target {target.as_posix()} is missing")
    try:
        os.chmod(target, 0o644)
    except OSError:
        pass
    with target.open("ab") as handle:
        handle.write(TAMPER_SUFFIX)


def _tamper_handler(mode_name: str) -> Callable[[_Context], None]:
    def handler(ctx: _Context) -> None:
        _tamper(mode_name)

    return handler


def _mode_extra_output(ctx: _Context) -> None:
    name = ctx.extra_output_name
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise _FixtureError(f"extra_output: unsafe extra_output_name {name!r}")
    (OUTPUT / name).write_bytes(EXTRA_OUTPUT_PAYLOAD)


def _mode_proposal_symlink(ctx: _Context) -> None:
    proposal = _clear_proposal_path()
    target = _first_bundle_file()
    os.symlink(os.path.relpath(target, proposal.parent), proposal)


def _mode_proposal_fifo(ctx: _Context) -> None:
    os.mkfifo(_clear_proposal_path())


def _mode_proposal_socket(ctx: _Context) -> None:
    proposal = _clear_proposal_path()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(proposal))
    finally:
        listener.close()


def _mode_proposal_hardlink(ctx: _Context) -> None:
    proposal = _clear_proposal_path()
    os.link(_first_bundle_file(), proposal)


def _mode_output_directory_replacement(ctx: _Context) -> None:
    aside = Path("output.original")
    if aside.exists():
        raise _FixtureError(
            "output_directory_replacement: output.original already exists"
        )
    os.rename(OUTPUT, aside)
    OUTPUT.mkdir()
    _mode_valid(ctx)


def _mode_large_scratch_file(ctx: _Context) -> None:
    _write_sized_file(
        SCRATCH / "big.bin", ctx.scratch_file_bytes, b"vibereview-fake-scratch"
    )


def _mode_too_many_files(ctx: _Context) -> None:
    for index in range(ctx.file_count):
        (SCRATCH / f"file_{index:04d}.txt").write_bytes(b"x\n")


def _mode_too_deep_tree(ctx: _Context) -> None:
    current = SCRATCH
    for _ in range(ctx.tree_depth):
        current = current / "d"
        current.mkdir(exist_ok=True)


def _mode_too_many_processes(ctx: _Context) -> None:
    children = [
        subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(ctx.process_count)
    ]
    try:
        time.sleep(1.0)
    finally:
        for child in children:
            _reap(child)


def _mode_permitted_scratch(ctx: _Context) -> None:
    payload = (b"permitted-scratch\n" * 57)[:1023] + b"\n"
    (SCRATCH / "ok.txt").write_bytes(payload)
    _mode_valid(ctx)


_MODE_HANDLERS: dict[str, Callable[[_Context], None]] = {
    "valid": _mode_valid,
    "nonzero": _mode_noop,
    "malformed_json": _mode_malformed_json,
    "schema_invalid": _mode_schema_invalid,
    "proposal_invalid": _mode_proposal_invalid,
    "missing_proposal": _mode_noop,
    "empty_proposal": _mode_empty_proposal,
    "non_utf8_proposal": _mode_non_utf8_proposal,
    "oversized_proposal": _mode_oversized_proposal,
    "large_stdout": _mode_large_stdout,
    "large_stderr": _mode_large_stderr,
    "parent_secret_probe": _mode_parent_secret_probe,
    "injected_secret_probe": _mode_injected_secret_probe,
    "timeout": _mode_timeout,
    "spawn_child_timeout": _mode_spawn_child_timeout,
    "extra_output": _mode_extra_output,
    "proposal_symlink": _mode_proposal_symlink,
    "proposal_fifo": _mode_proposal_fifo,
    "proposal_socket": _mode_proposal_socket,
    "proposal_hardlink": _mode_proposal_hardlink,
    "output_directory_replacement": _mode_output_directory_replacement,
    "large_scratch_file": _mode_large_scratch_file,
    "too_many_files": _mode_too_many_files,
    "too_deep_tree": _mode_too_deep_tree,
    "too_many_processes": _mode_too_many_processes,
    "permitted_scratch": _mode_permitted_scratch,
}
for _tamper_mode in (*TAMPER_FIXED_TARGETS, *TAMPER_GLOB_ROOTS):
    _MODE_HANDLERS[_tamper_mode] = _tamper_handler(_tamper_mode)

ALL_MODES = tuple(sorted(_MODE_HANDLERS))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic fake engine worker (VibeReview Milestone B2)."
    )
    parser.add_argument(
        "--config",
        required=True,
        help="path to launcher/worker_config.json inside the execution root",
    )
    parser.add_argument(
        "--mode",
        dest="modes",
        action="append",
        required=True,
        choices=ALL_MODES,
        help="worker mode; repeat to compose modes in order",
    )
    args = parser.parse_args(argv)
    try:
        context = _Context(_load_config(args.config))
        for mode in args.modes:
            _MODE_HANDLERS[mode](context)
    except _FixtureError as exc:
        print(f"fake_agent: {exc}", file=sys.stderr)
        return 2
    return 3 if "nonzero" in args.modes else 0


if __name__ == "__main__":
    raise SystemExit(main())
