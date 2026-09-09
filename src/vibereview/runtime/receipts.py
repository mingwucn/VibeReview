"""Idempotent accepted-task receipt helpers and fingerprinting."""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Callable
from typing import Any

from vibereview.ids import Sha256

from .hashing import hash_json, hash_text
from .promotion import DISPOSITION_HANDLERS, PROMOTION_HANDLERS
from .records import (
    AppliedTaskReceipt,
    CanonicalObjectReceipt,
    TaskSemanticFingerprint,
    TaskType,
)
from .state import RepositorySnapshot


def compute_handler_fingerprint(handler: Any, name: str = "") -> str:
    """Compute machine-derived implementation fingerprint for a handler."""
    if handler is None:
        return hash_text(f"missing:{name}")
    hasher = hashlib.sha256()
    hasher.update(name.encode("utf-8"))
    try:
        source = inspect.getsource(handler)
        hasher.update(source.encode("utf-8"))
    except Exception:
        pass
    code = getattr(handler, "__code__", None)
    if code is not None:
        hasher.update(code.co_code)
        hasher.update(repr(code.co_consts).encode("utf-8"))
    else:
        hasher.update(str(handler).encode("utf-8"))
    return f"sha256:{hasher.hexdigest()}"


def compute_semantic_fingerprint(
    *,
    validator_fingerprint: str,
    promotion_handler_name: str,
    disposition_handler_name: str,
    scientific_contract_version: str = "V1.5.1b",
    runtime_contract_version: str = "1.6",
    promotion_handler: Callable | None = None,
    disposition_handler: Callable | None = None,
    promotion_handler_fingerprint: str | None = None,
) -> TaskSemanticFingerprint:
    promo_fn = promotion_handler or PROMOTION_HANDLERS.get(promotion_handler_name)
    disp_fn = disposition_handler or DISPOSITION_HANDLERS.get(disposition_handler_name)
    promo_fp = promotion_handler_fingerprint or compute_handler_fingerprint(
        promo_fn, promotion_handler_name
    )
    disp_fp = compute_handler_fingerprint(disp_fn, disposition_handler_name)

    combined_dict = {
        "validator_fingerprint": validator_fingerprint,
        "promotion_handler_fingerprint": promo_fp,
        "disposition_handler_fingerprint": disp_fp,
        "scientific_contract_version": scientific_contract_version,
        "runtime_contract_version": runtime_contract_version,
    }
    combined = hash_json(combined_dict)
    return TaskSemanticFingerprint(
        validator_fingerprint=validator_fingerprint,
        promotion_handler_fingerprint=promo_fp,
        disposition_handler_fingerprint=disp_fp,
        scientific_contract_version=scientific_contract_version,
        runtime_contract_version=runtime_contract_version,
        combined_fingerprint=combined,
    )


def compute_input_identity_key(
    *,
    task_type: TaskType | str,
    task_spec_version: str,
    prompt_hash: str,
    input_schema_hash: str,
    proposal_schema_hash: str,
    dependency_hashes: dict[str, str],
    resource_hashes: dict[str, str],
    engine_input_hash: str,
    engine: str,
    engine_version: str | None,
    safe_engine_configuration_hash: str,
    scientific_contract_version: str,
) -> str:
    """Compute the input-identity key for receipt lookup (goal.md §7.1).

    Covers everything that identifies *what* the task operates on: task type,
    TaskSpec version, prompt, schemas, dependency and resource hashes, the
    sanitized engine input, engine identity/version/configuration, and the
    scientific contract version. Semantic-evaluation fingerprints (validator,
    promotion handler, disposition handler) and generation numbers are
    excluded; those enter only through :func:`compute_semantic_task_key`.
    """
    task_type_str = task_type.value if hasattr(task_type, "value") else str(task_type)
    key_dict = {
        "task_type": task_type_str,
        "task_spec_version": task_spec_version,
        "prompt_hash": prompt_hash,
        "input_schema_hash": input_schema_hash,
        "proposal_schema_hash": proposal_schema_hash,
        "dependency_hashes": dict(sorted(dependency_hashes.items())),
        "resource_hashes": dict(sorted(resource_hashes.items())),
        "engine_input_hash": engine_input_hash,
        "engine": engine,
        "engine_version": engine_version,
        "safe_engine_configuration_hash": safe_engine_configuration_hash,
        "scientific_contract_version": scientific_contract_version,
    }
    return hash_json(key_dict)


def compute_semantic_task_key(
    *,
    input_identity_key: str,
    semantic_fingerprint: TaskSemanticFingerprint,
) -> str:
    """Compute semantic task key from the input identity plus semantics (goal.md §7.1).

    The semantic key derives from the input identity key and the
    semantic-evaluation fingerprints: validator fingerprint, promotion-handler
    fingerprint, disposition-handler fingerprint, and runtime contract version.
    Two runs share an input identity but differ here exactly when the
    evaluation semantics changed, which the kernel treats as
    reevaluation-required instead of reuse.
    """
    key_dict = {
        "input_identity_key": input_identity_key,
        "validator_fingerprint": semantic_fingerprint.validator_fingerprint,
        "promotion_handler_fingerprint": semantic_fingerprint.promotion_handler_fingerprint,
        "disposition_handler_fingerprint": semantic_fingerprint.disposition_handler_fingerprint,
        "runtime_contract_version": semantic_fingerprint.runtime_contract_version,
    }
    return hash_json(key_dict)


def extract_canonical_object_receipts(
    before_snapshot: RepositorySnapshot,
    after_snapshot: RepositorySnapshot,
    allocated_ids: dict[str, str],
) -> tuple[CanonicalObjectReceipt, ...]:
    """Extract canonical objects added or modified by a task promotion."""
    before_index = before_snapshot.object_index()
    after_index = after_snapshot.object_index()

    receipts: list[CanonicalObjectReceipt] = []
    seen_qids: set[str] = set()

    for key, obj in after_index.items():
        if ":" not in key:
            continue
        before_obj = before_index.get(key)
        if before_obj is None or before_obj != obj:
            if key not in seen_qids:
                seen_qids.add(key)
                obj_hash = hash_json(obj.model_dump(mode="json"))
                receipts.append(
                    CanonicalObjectReceipt(qualified_id=key, object_hash=obj_hash)
                )

    for raw_id in allocated_ids.values():
        for qid, obj in after_index.items():
            if ":" in qid and qid.endswith(f":{raw_id}"):
                if qid not in seen_qids:
                    seen_qids.add(qid)
                    obj_hash = hash_json(obj.model_dump(mode="json"))
                    receipts.append(
                        CanonicalObjectReceipt(qualified_id=qid, object_hash=obj_hash)
                    )

    return tuple(sorted(receipts, key=lambda r: r.qualified_id))


def verify_receipt_canonical_objects(
    receipt: AppliedTaskReceipt,
    snapshot: RepositorySnapshot,
) -> tuple[bool, str | None]:
    """Verify that all canonical objects in the receipt exist with identical hashes."""
    index = snapshot.object_index()
    for obj_receipt in receipt.canonical_objects:
        obj = index.get(obj_receipt.qualified_id)
        if obj is None:
            return False, f"RECEIPT_CANONICAL_OBJECT_MISSING:{obj_receipt.qualified_id}"
        current_hash = hash_json(obj.model_dump(mode="json"))
        if current_hash != obj_receipt.object_hash:
            return False, f"RECEIPT_CANONICAL_OBJECT_HASH_MISMATCH:{obj_receipt.qualified_id}"
    return True, None
