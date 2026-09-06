"""Execution confinement levels (goal.md §6.10)."""

from __future__ import annotations

from enum import StrEnum


class ConfinementLevel(StrEnum):
    TEST_ONLY = "test_only"
    PATH_HYGIENE = "path_hygiene"
    OS_SANDBOX = "os_sandbox"
    ENGINE_NATIVE_SANDBOX = "engine_native_sandbox"


def allows_real_engine(level: ConfinementLevel) -> bool:
    """A real engine requires OS_SANDBOX or ENGINE_NATIVE_SANDBOX (§8.3)."""

    return level in {
        ConfinementLevel.OS_SANDBOX,
        ConfinementLevel.ENGINE_NATIVE_SANDBOX,
    }
