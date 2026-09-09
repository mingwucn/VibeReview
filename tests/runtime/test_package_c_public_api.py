from __future__ import annotations

import importlib
from pathlib import Path
import subprocess
import sys

import pytest


RUNTIME_MODULES = (
    "artifacts",
    "discovery",
    "pilot_records",
    "pilot_usage",
    "pilot_journal",
    "pilot_manifest",
    "pilot_sequence",
    "pilot_validation",
    "pilot_packet",
    "pilot_reproduction",
)

LIBRARY_MODULES = ("pilot_setup", "pilot_controller")

TASK_CONTRACT_NAMES = (
    "AggregatePaperEvidenceInput",
    "ClaimPaperEvidenceProposal",
    "CorpusChallengerInput",
    "DiscoveryInputDispositionProposal",
    "FinalClaimValidationProposal",
    "FinalPaperRelationProposal",
    "GenerateRetrievalQueriesInput",
    "RevisedClaimProposal",
    "ReviseClaimInput",
    "ValidateFinalClaimInput",
)

ASSEMBLY_ONLY_PACKET_NAMES = frozenset(
    {
        "PacketFileRecord",
        "ReviewPacketManifest",
        "verify_synthetic_review_packet",
        "write_synthetic_review_packet",
    }
)


@pytest.mark.parametrize("module_name", RUNTIME_MODULES)
def test_runtime_facade_exports_declared_package_c_api(module_name: str) -> None:
    facade = importlib.import_module("vibereview.runtime")
    module = importlib.import_module(f"vibereview.runtime.{module_name}")

    expected = set(module.__all__)
    if module_name == "artifacts":
        expected -= ASSEMBLY_ONLY_PACKET_NAMES
    for name in expected:
        assert name in facade.__all__
        assert getattr(facade, name) is getattr(module, name)


def test_assembly_only_packet_family_is_direct_module_compatibility_only() -> None:
    facade = importlib.import_module("vibereview.runtime")
    artifacts = importlib.import_module("vibereview.runtime.artifacts")

    for name in ASSEMBLY_ONLY_PACKET_NAMES:
        assert hasattr(artifacts, name)
        assert name not in facade.__all__
        assert not hasattr(facade, name)
    assert hasattr(facade, "write_synthetic_pilot_packet")
    assert hasattr(facade, "verify_synthetic_pilot_packet")


@pytest.mark.parametrize("module_name", LIBRARY_MODULES)
def test_library_facade_exports_synthetic_pilot_api(module_name: str) -> None:
    facade = importlib.import_module("vibereview.library")
    module = importlib.import_module(f"vibereview.library.{module_name}")

    for name in module.__all__:
        assert name in facade.__all__
        assert getattr(facade, name) is getattr(module, name)


def test_task_contract_models_are_runtime_root_importable() -> None:
    facade = importlib.import_module("vibereview.runtime")
    dto = importlib.import_module("vibereview.runtime.dto")

    for name in TASK_CONTRACT_NAMES:
        assert name in facade.__all__
        assert getattr(facade, name) is getattr(dto, name)


@pytest.mark.parametrize(
    "imports",
    (
        ("vibereview.runtime", "vibereview.library"),
        ("vibereview.library", "vibereview.runtime"),
    ),
)
def test_runtime_and_library_facades_import_in_either_order(
    imports: tuple[str, str],
) -> None:
    source_root = Path(__file__).resolve().parents[2] / "src"
    code = (
        "import importlib, sys; "
        f"sys.path.insert(0, {str(source_root)!r}); "
        f"importlib.import_module({imports[0]!r}); "
        f"importlib.import_module({imports[1]!r})"
    )
    subprocess.run(
        [sys.executable, "-I", "-c", code],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
