"""Bounded, redacted process-stream diagnostics contract (goal.md §6.8)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from vibereview.ids import Sha256

from .records import RuntimeModel


class DiagnosticCapture(RuntimeModel):
    """Retention record for one persisted, redacted diagnostic stream.

    ``bytes_observed`` counts raw bytes read from the process stream;
    ``bytes_retained`` counts redacted bytes persisted;
    ``retained_redacted_hash`` is the SHA-256 of the exact persisted
    diagnostic file and never describes an unredacted secret-bearing stream.
    """

    relative_path: Path
    bytes_observed: int = Field(ge=0)
    bytes_retained: int = Field(ge=0)
    truncated: bool
    retained_redacted_hash: Sha256
    redactions_applied: int = Field(ge=0)
