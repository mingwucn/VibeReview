from __future__ import annotations

from vibereview.runtime.cache import SemanticCache
from vibereview.runtime.records import CacheSignature, TaskType


def _signature(validator_char: str):
    return CacheSignature(
        task_type=TaskType.GENERATE_CANDIDATE_CLAIMS,
        task_spec_version="1",
        prompt_hash="sha256:" + "a" * 64,
        input_snapshot_hash="sha256:" + "b" * 64,
        engine="mock",
        engine_version="1",
        safe_engine_configuration_hash="sha256:" + "c" * 64,
        scientific_contract_version="V1.5.1b",
        validator_fingerprint="sha256:" + validator_char * 64,
    )


def test_cache_is_engine_and_validation_contract_specific(tmp_path):
    cache = SemanticCache(tmp_path / "cache")
    original = _signature("d")
    changed_validator = _signature("e")
    proposal = {"themes": [], "claims": []}
    cache.put(original, proposal)
    assert cache.get(original) == proposal
    assert cache.get(changed_validator) is None

