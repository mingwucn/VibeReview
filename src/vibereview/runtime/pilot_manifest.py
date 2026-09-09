"""Build an exact, bounded run policy for the synthetic five-paper pilot."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from .engine import AgentEngine, MockEngine
from .hashing import hash_bytes, hash_json
from .kernel import ProjectRuntime
from .pilot_records import (
    FivePaperPilotBudget,
    PilotEngineRoleBinding,
    PilotRunManifest,
    compute_pilot_engine_role_plan_hash,
    compute_pilot_schema_fingerprint,
)
from .records import TaskType
from .repository import read_contained_regular_file
from .specs import TASK_SPECS
from .tasks import BUNDLE_PROTOCOL_SECTION


class PilotManifestError(ValueError):
    """A proposed synthetic run policy is incomplete, stale, or over budget."""


def _verified_corpus(project_root: Path, generation: int):
    from vibereview.library.retrieval import VerifiedCorpus

    return VerifiedCorpus(project_root, generation=generation)


def _read_allowed_resource(
    runtime: ProjectRuntime,
    path: Path,
    *,
    max_bytes: int,
) -> bytes:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise PilotManifestError("discovery resource is unavailable") from exc
    roots = tuple(
        root.resolve(strict=True)
        for root in (runtime.allowed_source_roots or (runtime.project_root,))
    )
    matches = tuple(
        root for root in roots if resolved == root or root in resolved.parents
    )
    if not matches:
        raise PilotManifestError(
            "discovery resource is outside the runtime source allowlist"
        )
    # Prefer the closest retained root, minimizing the traversed relative path.
    root = max(matches, key=lambda item: len(item.parts))
    relative = resolved.relative_to(root).as_posix()
    try:
        content, _ = read_contained_regular_file(
            root, relative, max_bytes=max_bytes
        )
    except Exception as exc:
        raise PilotManifestError(
            "discovery resource is not a bounded regular file"
        ) from exc
    return content


def _task_prompt_hash(task_type: TaskType) -> str:
    spec = TASK_SPECS[task_type]
    try:
        prompt = spec.prompt_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PilotManifestError(
            f"prompt is unavailable for {task_type.value}"
        ) from exc
    return hash_bytes((prompt + BUNDLE_PROTOCOL_SECTION).encode("utf-8"))


def _schema_file_hash(model: type) -> str:
    content = (
        json.dumps(
            model.model_json_schema(),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    return hash_bytes(content)


_PACKAGE_C_IMPLEMENTATION_PATTERNS = (
    "*.py",
    "runtime/*.py",
    "library/*.py",
    "prompts/*.md",
)


def compute_package_c_implementation_fingerprint() -> str:
    """Hash the complete local Package C implementation and task contracts.

    Relative paths, never checkout-specific absolute paths, identify every
    runtime/library Python source and prompt.  Generated input and proposal
    schemas are included explicitly so schema behavior is bound even when it
    comes from an imported model outside those package directories.
    """

    package_root = Path(__file__).resolve().parents[1]
    paths = sorted(
        {
            path
            for pattern in _PACKAGE_C_IMPLEMENTATION_PATTERNS
            for path in package_root.glob(pattern)
            if path.is_file()
        },
        key=lambda path: path.relative_to(package_root).as_posix(),
    )
    if not paths:
        raise PilotManifestError("Package C implementation sources are unavailable")
    files: list[dict[str, str]] = []
    for path in paths:
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise PilotManifestError(
                "Package C implementation source is unavailable"
            ) from exc
        files.append(
            {
                "path": path.relative_to(package_root).as_posix(),
                "content_hash": hash_bytes(content),
            }
        )
    task_contracts = {
        task_type.value: {
            "compiled_prompt_hash": _task_prompt_hash(task_type),
            "schema_fingerprint": compute_pilot_schema_fingerprint(
                input_schema_hash=_schema_file_hash(
                    TASK_SPECS[task_type].engine_input_model
                ),
                proposal_schema_hash=_schema_file_hash(
                    TASK_SPECS[task_type].proposal_model
                ),
            ),
        }
        for task_type in sorted(TaskType, key=lambda item: item.value)
    }
    return hash_json(
        {
            "fingerprint_version": "package-c-implementation-2",
            "files": files,
            "task_contracts": task_contracts,
        }
    )


def _engine_plan_hash(runtime: ProjectRuntime, engine: AgentEngine) -> str:
    return hash_json(
        {
            "technical_attempts_per_engine": (
                runtime.config.technical_attempts_per_engine
            ),
            "engines": [
                {
                    "name": engine.name,
                    "version": engine.version,
                    "safe_configuration": dict(engine.safe_configuration()),
                }
            ],
        }
    )


def build_synthetic_pilot_run_manifest(
    runtime: ProjectRuntime,
    *,
    run_id: str,
    topic: str,
    discovery_document_paths: Sequence[Path],
    engines: Mapping[TaskType, AgentEngine],
    budget: FivePaperPilotBudget | None = None,
    created_at: str | None = None,
) -> PilotRunManifest:
    """Freeze exact inputs, contracts, and one synthetic engine per task role.

    The function performs no engine call and writes no state.  The returned
    manifest can be registered atomically by ``register_synthetic_pilot_setup``.
    """

    selected_budget = budget or FivePaperPilotBudget()
    if runtime.config.max_fallback_engines != 0:
        raise PilotManifestError("synthetic pilot forbids engine fallback")
    if set(engines) != set(TaskType):
        raise PilotManifestError("engine role map must cover every TaskType exactly")
    if not (
        selected_budget.min_discovery_documents
        <= len(discovery_document_paths)
        <= selected_budget.max_discovery_documents
    ):
        raise PilotManifestError("discovery document count is outside its budget")

    try:
        generation, snapshot, _ = runtime.store.load_current()
    except Exception as exc:
        raise PilotManifestError("CURRENT generation is unavailable") from exc
    papers = tuple(sorted(snapshot.papers, key=lambda item: item.paper_id))
    if len(papers) != selected_budget.paper_count:
        raise PilotManifestError("synthetic pilot requires exactly five papers")
    corpus = _verified_corpus(runtime.project_root, generation)
    lock = corpus.lock
    locked_ids = tuple(item.paper_id for item in lock.papers)
    if locked_ids != tuple(item.paper_id for item in papers):
        raise PilotManifestError("corpus lock paper order differs from canonical state")

    corpus_bytes = 0
    for paper in papers:
        try:
            _, _, text = corpus.texts[paper.paper_id]
        except KeyError as exc:
            raise PilotManifestError("canonical paper is absent from the corpus lock") from exc
        size = len(text.encode("utf-8"))
        if size > selected_budget.max_paper_bytes:
            raise PilotManifestError("paper exceeds the run byte budget")
        corpus_bytes += size
    if corpus_bytes > selected_budget.max_corpus_bytes:
        raise PilotManifestError("corpus exceeds the run byte budget")

    discovery_hashes: list[str] = []
    discovery_total = 0
    for path in discovery_document_paths:
        content = _read_allowed_resource(
            runtime,
            Path(path),
            max_bytes=selected_budget.max_discovery_document_bytes,
        )
        discovery_total += len(content)
        if discovery_total > selected_budget.max_discovery_total_bytes:
            raise PilotManifestError("discovery resources exceed their total byte budget")
        discovery_hashes.append(hash_bytes(content))
    if len(discovery_hashes) != len(set(discovery_hashes)):
        raise PilotManifestError("discovery resources must have distinct content")

    engine_role_plan: dict[TaskType, PilotEngineRoleBinding] = {}
    for task_type in TaskType:
        engine = engines[task_type]
        if not isinstance(engine, MockEngine):
            raise PilotManifestError(
                "synthetic pilot role map accepts only deterministic MockEngine instances"
            )
        engine_role_plan[task_type] = PilotEngineRoleBinding(
            engine=engine.name,
            engine_version=engine.version,
            safe_configuration_hash=_engine_plan_hash(runtime, engine),
        )

    prompt_hashes = {
        task_type: _task_prompt_hash(task_type) for task_type in TaskType
    }
    schema_hashes = {
        task_type: compute_pilot_schema_fingerprint(
            input_schema_hash=_schema_file_hash(
                TASK_SPECS[task_type].engine_input_model
            ),
            proposal_schema_hash=_schema_file_hash(
                TASK_SPECS[task_type].proposal_model
            ),
        )
        for task_type in TaskType
    }
    timestamp = created_at or datetime.now(UTC).isoformat()
    return PilotRunManifest(
        run_id=run_id,
        created_at=timestamp,
        topic=topic,
        source_generation=generation,
        corpus_lock_hash=corpus.lock_hash,
        selection_manifest_hash=lock.selection_manifest_hash,
        paper_source_hashes=tuple(item.source_hash for item in papers),
        discovery_resource_hashes=tuple(discovery_hashes),
        engine_role_plan=engine_role_plan,
        engine_role_plan_hash=compute_pilot_engine_role_plan_hash(
            engine_role_plan
        ),
        prompt_fingerprints=prompt_hashes,
        schema_fingerprints=schema_hashes,
        validator_fingerprint=runtime.validator_fingerprint,
        package_c_implementation_fingerprint=(
            compute_package_c_implementation_fingerprint()
        ),
        budget=selected_budget,
    )


__all__ = [
    "PilotManifestError",
    "build_synthetic_pilot_run_manifest",
    "compute_package_c_implementation_fingerprint",
]
