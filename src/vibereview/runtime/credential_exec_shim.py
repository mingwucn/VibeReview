#!/usr/bin/env python3
"""Trusted in-sandbox credential/profile staging followed by ``execve``.

Only fixed paths and nonsecret path variables cross argv/environment.  Secret
bytes remain in the read-only ``/work/credentials`` mount and, when a vendor
requires a HOME profile, in the attempt-local ``/work/home`` tree.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from pathlib import Path


MAX_CREDENTIAL_BYTES = 1_048_576
_CREDENTIAL_ROOT = Path("/work/credentials")
_HOME_ROOT = Path("/work/home")
_ALLOWED_CREDENTIAL_ENV_NAMES = frozenset(
    {"CODEX_API_KEY", "KIMI_MODEL_API_KEY"}
)


def _safe_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise ValueError("unsafe credential filename")
    return value


def _safe_home_target(value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("unsafe HOME credential target")
    return relative


def _read_fixed(path: Path) -> bytes:
    item_stat = os.lstat(path)
    if not stat.S_ISREG(item_stat.st_mode) or item_stat.st_size > MAX_CREDENTIAL_BYTES:
        raise RuntimeError("fixed credential is unsafe or oversized")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ) != (
            item_stat.st_dev,
            item_stat.st_ino,
            item_stat.st_size,
            item_stat.st_mtime_ns,
        ):
            raise RuntimeError("fixed credential changed while opening")
        chunks: list[bytes] = []
        observed = 0
        while True:
            chunk = os.read(descriptor, min(65_536, MAX_CREDENTIAL_BYTES + 1 - observed))
            if not chunk:
                break
            observed += len(chunk)
            if observed > MAX_CREDENTIAL_BYTES:
                raise RuntimeError("fixed credential exceeds byte bound")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            observed != opened.st_size
            or (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            )
            != (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
            )
        ):
            raise RuntimeError("fixed credential changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _credential_scalar(raw: bytes) -> str:
    """Decode one opaque credential without silently normalizing its value."""

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("fixed credential is not UTF-8") from exc
    if not text or "\x00" in text or "\n" in text or "\r" in text:
        raise RuntimeError("fixed credential is not a single safe scalar")
    return text


def _stage_home_profile(relative: Path, raw: bytes) -> None:
    target = _HOME_ROOT / relative
    home = _HOME_ROOT.resolve()
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    resolved_parent = target.parent.resolve()
    if resolved_parent != home and home not in resolved_parent.parents:
        raise RuntimeError("HOME credential target escapes the attempt home")
    descriptor = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb", closefd=True) as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VibeReview fixed-credential exec shim")
    parser.add_argument("--credential-name", required=True)
    parser.add_argument("--home-target")
    parser.add_argument("--credential-env", choices=sorted(_ALLOWED_CREDENTIAL_ENV_NAMES))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("credential shim: no command", file=sys.stderr)
        return 125
    try:
        name = _safe_name(args.credential_name)
        source = _CREDENTIAL_ROOT / name
        raw = _read_fixed(source)
        if not raw:
            raise RuntimeError("fixed credential is empty")
        if args.home_target is not None and args.credential_env is not None:
            raise RuntimeError("credential HOME and environment modes are exclusive")
        if args.home_target is not None:
            _stage_home_profile(_safe_home_target(args.home_target), raw)
        environment = {
            key: os.environ[key]
            for key in ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE")
            if key in os.environ
        }
        environment["HOME"] = str(_HOME_ROOT)
        environment["XDG_CONFIG_HOME"] = str(_HOME_ROOT / ".config")
        environment["XDG_CACHE_HOME"] = str(_HOME_ROOT / ".cache")
        environment["TMPDIR"] = "/work/tmp"
        environment["KIMI_CODE_HOME"] = "/work/home/kimi-code"
        environment["VIBEREVIEW_FIXED_CREDENTIAL"] = f"/work/credentials/{name}"
        if args.credential_env is not None:
            environment[args.credential_env] = _credential_scalar(raw)
        os.execvpe(command[0], command, environment)
    except Exception as exc:
        # Never include credential bytes in diagnostics.
        print(f"credential shim: {type(exc).__name__}", file=sys.stderr)
        return 125
    return 125


if __name__ == "__main__":
    raise SystemExit(main())
