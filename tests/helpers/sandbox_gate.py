"""Probe-based gate for positive sandbox conformance tests (goal.md §4.5).

Binary presence alone no longer qualifies a host: positive conformance tests
require ``SandboxProbeStatus.USABLE``. An unavailable backend skips (the host
was never prepared for Bubblewrap qualification); a blocked backend fails the
qualification job loudly with the full probe report — it must never silently
skip and never produce a qualified artifact.
"""

from __future__ import annotations

import json

import pytest

from vibereview.runtime import (
    SandboxProbeResult,
    SandboxProbeStatus,
    probe_sandbox_capabilities,
)

_PROBE_CACHE: SandboxProbeResult | None = None


def cached_sandbox_probe() -> SandboxProbeResult | None:
    """The cached probe result, or None when no probe has run this session."""

    return _PROBE_CACHE


def require_usable_sandbox() -> SandboxProbeResult:
    """Probe once per session; skip when unavailable, fail loudly when blocked."""

    global _PROBE_CACHE
    if _PROBE_CACHE is None:
        _PROBE_CACHE = probe_sandbox_capabilities()
    probe = _PROBE_CACHE
    if probe.status is SandboxProbeStatus.USABLE:
        return probe
    report = json.dumps(probe.model_dump(mode="json"), indent=2, ensure_ascii=False)
    if probe.status is SandboxProbeStatus.UNAVAILABLE:
        pytest.skip(f"sandbox backend unavailable on this host:\n{report}")
    pytest.fail(
        "sandbox capability probe is BLOCKED; host is not qualified:\n" + report,
        pytrace=False,
    )
