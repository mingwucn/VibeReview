from __future__ import annotations

import pytest

from vibereview.runtime.cache import SemanticCache
from vibereview.runtime.records import CacheSignature, TaskType


def _hashes(seed: str) -> dict[str, str]:
    return {
        "prompt_hash": "sha256:" + "a" * 64,
        "input_schema_hash": "sha256:" + "b" * 64,
        "proposal_schema_hash": "sha256:" + "c" * 64,
        "engine_input_hash": "sha256:" + "d" * 64,
        "bundle_manifest_hash": "sha256:" + "e" * 64,
    }


def _signature(validator_char: str, **overrides):
    values = {
        "task_type": TaskType.GENERATE_CANDIDATE_CLAIMS,
        "task_spec_version": "1",
        **_hashes("x"),
        "dependency_hashes": {"ThemeRecord:T0001": "sha256:" + "0" * 64},
        "resource_hashes": {"RES0001": "sha256:" + "1" * 64},
        "engine": "mock",
        "engine_version": "1",
        "safe_engine_configuration_hash": "sha256:" + "2" * 64,
        "scientific_contract_version": "V1.5.1b",
        "validator_fingerprint": "sha256:" + validator_char * 64,
    }
    values.update(overrides)
    return CacheSignature(**values)


def test_cache_is_engine_and_validation_contract_specific(tmp_path):
    cache = SemanticCache(tmp_path / "cache")
    original = _signature("d")
    changed_validator = _signature("e")
    proposal = {"themes": [], "claims": []}
    cache.put(original, proposal)
    assert cache.get(original) == proposal
    assert cache.get(changed_validator) is None


@pytest.mark.parametrize(
    "field",
    [
        "prompt_hash",
        "input_schema_hash",
        "proposal_schema_hash",
        "engine_input_hash",
        "bundle_manifest_hash",
    ],
)
def test_schema_and_bundle_hashes_invalidate_cache_reuse(tmp_path, field):
    cache = SemanticCache(tmp_path / "cache")
    original = _signature("d")
    changed = _signature("d", **{field: "sha256:" + "9" * 64})
    proposal = {"themes": [], "claims": []}
    cache.put(original, proposal)
    assert cache.get(changed) is None
    assert cache.get(original) == proposal


def test_dependency_and_resource_hashes_invalidate_cache_reuse(tmp_path):
    cache = SemanticCache(tmp_path / "cache")
    original = _signature("d")
    changed_dependency = _signature(
        "d", dependency_hashes={"ThemeRecord:T0001": "sha256:" + "3" * 64}
    )
    changed_resource = _signature(
        "d", resource_hashes={"RES0001": "sha256:" + "4" * 64}
    )
    proposal = {"themes": [], "claims": []}
    cache.put(original, proposal)
    assert cache.get(changed_dependency) is None
    assert cache.get(changed_resource) is None
    assert cache.get(original) == proposal
