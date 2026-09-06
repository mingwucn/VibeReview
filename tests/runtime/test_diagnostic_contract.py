"""DiagnosticCapture retention contract (goal.md §6.8, §6.12)."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from vibereview.ids import SHA256_PATTERN
from vibereview.runtime.diagnostics import DiagnosticCapture
from vibereview.runtime.hashing import hash_bytes, hash_file

SECRET = b"sk-live-secret-token"
REDACTED_PLACEHOLDER = b"[REDACTED]"

EMPTY_SHA256 = (
    "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)

EXPECTED_FIELDS = (
    "relative_path",
    "bytes_observed",
    "bytes_retained",
    "truncated",
    "retained_redacted_hash",
    "redactions_applied",
)


def _persist(tmp_path: Path, relative_path: Path, payload: bytes) -> Path:
    target = tmp_path / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


def _capture(**overrides) -> DiagnosticCapture:
    values = {
        "relative_path": Path("attempt/stdout.txt"),
        "bytes_observed": 128,
        "bytes_retained": 128,
        "truncated": False,
        "retained_redacted_hash": "sha256:" + "a" * 64,
        "redactions_applied": 0,
    }
    values.update(overrides)
    return DiagnosticCapture(**values)


def test_capture_records_bounded_retention_fields():
    capture = _capture(
        bytes_observed=70000,
        bytes_retained=65536,
        truncated=True,
        redactions_applied=2,
    )
    assert capture.relative_path == Path("attempt/stdout.txt")
    assert capture.bytes_observed == 70000
    assert capture.bytes_retained == 65536
    assert capture.truncated is True
    assert capture.redactions_applied == 2


def test_retained_hash_equals_persisted_redacted_file_hash(tmp_path):
    """§6.12: retained_redacted_hash covers the exact persisted redacted file."""
    raw = b"prefix " + SECRET + b" middle " + SECRET + b" suffix\n"
    redacted = raw.replace(SECRET, REDACTED_PLACEHOLDER)
    persisted = _persist(tmp_path, Path("attempt/stdout.txt"), redacted)
    expected_hash = "sha256:" + hashlib.sha256(redacted).hexdigest()
    assert hash_file(persisted) == expected_hash

    capture = DiagnosticCapture(
        relative_path=Path("attempt/stdout.txt"),
        bytes_observed=len(raw),
        bytes_retained=len(redacted),
        truncated=False,
        retained_redacted_hash=hash_file(persisted),
        redactions_applied=2,
    )
    assert capture.retained_redacted_hash == expected_hash
    assert capture.bytes_retained == persisted.stat().st_size
    assert capture.bytes_observed == len(raw)
    assert capture.redactions_applied == redacted.count(REDACTED_PLACEHOLDER)


def test_persisted_hash_never_describes_unredacted_stream(tmp_path):
    """§6.8: the recorded hash must not match the secret-bearing raw stream."""
    raw = b"token=" + SECRET + b"\n"
    redacted = raw.replace(SECRET, REDACTED_PLACEHOLDER)
    persisted = _persist(tmp_path, Path("attempt/stderr.txt"), redacted)
    capture = DiagnosticCapture(
        relative_path=Path("attempt/stderr.txt"),
        bytes_observed=len(raw),
        bytes_retained=len(redacted),
        truncated=False,
        retained_redacted_hash=hash_file(persisted),
        redactions_applied=1,
    )
    assert capture.retained_redacted_hash != hash_bytes(raw)
    assert SECRET not in persisted.read_bytes()


def test_retained_hash_is_canonical_sha256(tmp_path):
    payload = b"engine diagnostic line\n"
    persisted = _persist(tmp_path, Path("attempt/stdout.txt"), payload)
    capture = DiagnosticCapture(
        relative_path=Path("attempt/stdout.txt"),
        bytes_observed=len(payload),
        bytes_retained=len(payload),
        truncated=False,
        retained_redacted_hash=hash_file(persisted),
        redactions_applied=0,
    )
    assert re.fullmatch(SHA256_PATTERN, capture.retained_redacted_hash)
    assert capture.retained_redacted_hash.startswith("sha256:")
    digest = capture.retained_redacted_hash.removeprefix("sha256:")
    assert len(digest) == 64
    assert digest == digest.lower()
    int(digest, 16)


@pytest.mark.parametrize(
    "bad_hash",
    [
        "",
        "a" * 64,
        "sha256:" + "a" * 63,
        "sha256:" + "a" * 65,
        "sha256:" + "A" * 64,
        "sha256:" + "g" * 64,
        "SHA256:" + "a" * 64,
        "sha256 " + "a" * 64,
    ],
)
def test_retained_hash_rejects_non_canonical_values(bad_hash):
    with pytest.raises(ValidationError):
        _capture(retained_redacted_hash=bad_hash)


@pytest.mark.parametrize(
    "field", ["bytes_observed", "bytes_retained", "redactions_applied"]
)
def test_counts_reject_negative_values(field):
    with pytest.raises(ValidationError):
        _capture(**{field: -1})


def test_counts_accept_zero():
    capture = _capture(bytes_observed=0, bytes_retained=0, redactions_applied=0)
    assert capture.bytes_observed == 0
    assert capture.bytes_retained == 0
    assert capture.redactions_applied == 0


def test_zero_byte_capture_of_empty_stream(tmp_path):
    persisted = _persist(tmp_path, Path("attempt/stdout.txt"), b"")
    capture = DiagnosticCapture(
        relative_path=Path("attempt/stdout.txt"),
        bytes_observed=0,
        bytes_retained=0,
        truncated=False,
        retained_redacted_hash=hash_file(persisted),
        redactions_applied=0,
    )
    assert capture.retained_redacted_hash == EMPTY_SHA256


def test_truncated_capture_retains_bounded_bytes():
    capture = _capture(
        bytes_observed=10 * 1024 * 1024,
        bytes_retained=65536,
        truncated=True,
    )
    assert capture.bytes_retained < capture.bytes_observed
    assert capture.truncated


def test_untruncated_capture_retains_all_observed_bytes(tmp_path):
    payload = b"complete stderr\n"
    persisted = _persist(tmp_path, Path("attempt/stderr.txt"), payload)
    capture = DiagnosticCapture(
        relative_path=Path("attempt/stderr.txt"),
        bytes_observed=len(payload),
        bytes_retained=persisted.stat().st_size,
        truncated=False,
        retained_redacted_hash=hash_file(persisted),
        redactions_applied=0,
    )
    assert capture.bytes_observed == capture.bytes_retained
    assert not capture.truncated


def test_model_surface_exposes_only_the_persisted_redacted_hash():
    assert tuple(DiagnosticCapture.model_fields) == EXPECTED_FIELDS
    for name in DiagnosticCapture.model_fields:
        assert "unredacted" not in name
        assert "raw" not in name


def test_contract_documents_hash_covers_only_persisted_redacted_file():
    doc = DiagnosticCapture.__doc__ or ""
    assert "retained_redacted_hash" in doc
    assert "unredacted" in doc


@pytest.mark.parametrize(
    "extra", ["unredacted_hash", "raw_hash", "observed_hash", "raw_bytes"]
)
def test_extra_unredacted_hash_fields_are_rejected(extra):
    with pytest.raises(ValidationError):
        _capture(**{extra: "sha256:" + "b" * 64})


@pytest.mark.parametrize("missing", EXPECTED_FIELDS)
def test_all_fields_are_required(missing):
    values = {
        "relative_path": Path("attempt/stdout.txt"),
        "bytes_observed": 1,
        "bytes_retained": 1,
        "truncated": False,
        "retained_redacted_hash": "sha256:" + "a" * 64,
        "redactions_applied": 0,
    }
    del values[missing]
    with pytest.raises(ValidationError):
        DiagnosticCapture(**values)


def test_assignment_is_validated():
    capture = _capture()
    with pytest.raises(ValidationError):
        capture.bytes_retained = -5
    with pytest.raises(ValidationError):
        capture.retained_redacted_hash = "not-a-hash"
    capture.truncated = True
    assert capture.truncated is True


def test_capture_round_trips_through_dump_and_json(tmp_path):
    payload = b"stdout with secret removed\n"
    persisted = _persist(tmp_path, Path("attempt/stdout.txt"), payload)
    capture = DiagnosticCapture(
        relative_path=Path("attempt/stdout.txt"),
        bytes_observed=len(payload),
        bytes_retained=len(payload),
        truncated=False,
        retained_redacted_hash=hash_file(persisted),
        redactions_applied=0,
    )
    assert DiagnosticCapture.model_validate(capture.model_dump()) == capture
    assert (
        DiagnosticCapture.model_validate_json(capture.model_dump_json()) == capture
    )
    dumped = json.loads(capture.model_dump_json())
    assert dumped["relative_path"] == "attempt/stdout.txt"
    assert dumped["retained_redacted_hash"] == hash_file(persisted)
