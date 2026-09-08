"""Canonical hashing helpers used by generations, tasks, and semantic caches."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def hash_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def hash_text(value: str) -> str:
    return hash_bytes(value.encode("utf-8"))


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def hash_json(value: Any) -> str:
    return hash_bytes(canonical_json_bytes(value))


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def hash_tree(root: Path) -> str:
    """Hash a directory by relative path, file type, and file content."""

    entries: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append({"path": relative, "type": "symlink", "target": str(path.readlink())})
        elif path.is_file():
            entries.append({"path": relative, "type": "file", "hash": hash_file(path)})
        elif path.is_dir():
            entries.append({"path": relative, "type": "directory"})
    return hash_json(entries)


def code_fingerprint(paths: list[Path], contract_version: str) -> str:
    values = {
        "contract_version": contract_version,
        "files": [
            {"path": path.name, "hash": hash_file(path)}
            for path in sorted(paths)
            if path.is_file()
        ],
    }
    return hash_json(values)

