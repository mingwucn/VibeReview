"""Engine-specific semantic proposal cache primitives."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .hashing import hash_json
from .records import CacheSignature
from .repository import atomic_write_text


class SemanticCache:
    def __init__(self, root: Path):
        self.root = root

    @staticmethod
    def key(signature: CacheSignature) -> str:
        return hash_json(signature.model_dump(mode="json")).removeprefix("sha256:")

    def get(self, signature: CacheSignature) -> dict[str, Any] | None:
        entry = self.root / self.key(signature)
        metadata_path = entry / "signature.json"
        proposal_path = entry / "proposal.json"
        if not metadata_path.is_file() or not proposal_path.is_file():
            return None
        try:
            stored = CacheSignature.model_validate_json(
                metadata_path.read_text(encoding="utf-8")
            )
            if stored != signature:
                return None
            value = json.loads(proposal_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (ValueError, OSError, json.JSONDecodeError):
            return None

    def put(self, signature: CacheSignature, proposal: dict[str, Any]) -> Path:
        entry = self.root / self.key(signature)
        entry.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            entry / "signature.json", signature.model_dump_json(indent=2) + "\n"
        )
        atomic_write_text(
            entry / "proposal.json",
            json.dumps(proposal, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        return entry

