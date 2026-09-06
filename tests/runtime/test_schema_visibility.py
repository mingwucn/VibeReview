"""Milestone-A schema visibility: engine-facing JSON schemas and cache signature.

Covers goal.md section 4 "Schema visibility": bundle contracts equal the
registered models' JSON schemas, schema files are read-only in the master
bundle and in per-attempt workspace copies, schema hashes enter the cache
signature, instructions state the A8 bundle protocol, and the engine-readable
bundle manifest stays consistent with the private provenance trust anchor.

Cache-invalidation by schema-hash drift is already covered parametrized in
tests/runtime/test_cache.py, so it is not repeated here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vibereview.runtime import (
    AssessClaimInvocation,
    AttemptOutcome,
    CandidateClaimProposal,
    DiscoveryProposalBundle,
    MockEngine,
    MockResponse,
    ParseDeepResearchInvocation,
    ProjectContext,
    ProjectRuntime,
    TaskType,
    ThemeProposal,
)
from vibereview.runtime.hashing import hash_file
from vibereview.runtime.records import CacheSignature
from vibereview.runtime.specs import TASK_SPECS
from vibereview.runtime.state import RepositorySnapshot
from vibereview.runtime.tasks import TaskWorkspace

SCHEMA_TASK_TYPES = [TaskType.PARSE_DEEP_RESEARCH, TaskType.ASSESS_CLAIM]

SCHEMA_FILES = ("input.schema.json", "proposal.schema.json")

# The five bundle-protocol directives required by goal.md A8.
PROTOCOL_DIRECTIVES = (
    "- Read `input/input.json`.",
    "- Read resources only through the resource entries in `bundle_manifest.json`.",
    "- Return exactly one JSON object conforming to `contracts/proposal.schema.json`.",
    "- Do not emit Markdown fences or explanatory text.",
    "- Do not allocate canonical scientific IDs.",
)


def _create_task(tmp_path: Path, bundle_factory, task_type: TaskType):
    """Build one immutable task bundle through the real TaskWorkspace path."""

    project = tmp_path / "project"
    spec = TASK_SPECS[task_type]
    if task_type is TaskType.PARSE_DEEP_RESEARCH:
        snapshot = RepositorySnapshot()
        documents = project / "input" / "deep_research"
        documents.mkdir(parents=True)
        alpha = documents / "alpha.md"
        beta = documents / "beta.md"
        alpha.write_text("# Alpha\n\nResidual stress survey.\n", encoding="utf-8")
        beta.write_text("# Beta\n\nSecond document.\n", encoding="utf-8")
        invocation = ParseDeepResearchInvocation(
            topic="residual stress", document_paths=[alpha, beta]
        )
    elif task_type is TaskType.ASSESS_CLAIM:
        snapshot = RepositorySnapshot.model_validate(bundle_factory())
        invocation = AssessClaimInvocation(
            claim_id="C0001", claim_paper_evidence_ids=["CPE-C0001-P0001"]
        )
    else:  # pragma: no cover - parametrization guard
        raise AssertionError(f"unexpected task type {task_type}")
    context = ProjectContext(project_root=project)
    dependencies = {
        key: snapshot.dependency_hash(key)
        for key in spec.dependency_builder(invocation, snapshot)
    }
    resource_requests = spec.resource_builder(invocation, snapshot, context)
    workspace = TaskWorkspace(project)
    task_dir, _manifest, provenance = workspace.create(
        spec=spec,
        invocation=invocation,
        base_generation=0,
        dependencies=dependencies,
        resource_requests=resource_requests,
        snapshot=snapshot,
        context=context,
    )
    return workspace, task_dir, provenance, spec


@pytest.mark.parametrize("task_type", SCHEMA_TASK_TYPES)
def test_input_schema_file_equals_engine_input_model_json_schema(
    tmp_path, bundle_factory, task_type
):
    _, task_dir, _, spec = _create_task(tmp_path, bundle_factory, task_type)
    schema_path = task_dir / "bundle" / "contracts" / "input.schema.json"
    expected = spec.engine_input_model.model_json_schema()
    # Semantic correspondence, robust to serialization choices...
    assert json.loads(schema_path.read_text(encoding="utf-8")) == expected
    # ...and the exact canonical bytes the engine receives.
    assert schema_path.read_bytes() == (
        json.dumps(expected, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


@pytest.mark.parametrize("task_type", SCHEMA_TASK_TYPES)
def test_proposal_schema_file_equals_proposal_model_json_schema(
    tmp_path, bundle_factory, task_type
):
    _, task_dir, _, spec = _create_task(tmp_path, bundle_factory, task_type)
    schema_path = task_dir / "bundle" / "contracts" / "proposal.schema.json"
    expected = spec.proposal_model.model_json_schema()
    assert json.loads(schema_path.read_text(encoding="utf-8")) == expected
    assert schema_path.read_bytes() == (
        json.dumps(expected, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


@pytest.mark.parametrize("task_type", SCHEMA_TASK_TYPES)
def test_schema_files_are_read_only_in_master_bundle(
    tmp_path, bundle_factory, task_type
):
    _, task_dir, _, _ = _create_task(tmp_path, bundle_factory, task_type)
    contracts = task_dir / "bundle" / "contracts"
    for name in SCHEMA_FILES:
        assert not ((contracts / name).stat().st_mode & 0o222), name


@pytest.mark.parametrize("task_type", SCHEMA_TASK_TYPES)
def test_attempt_workspace_schema_copies_are_read_only_and_byte_identical(
    tmp_path, bundle_factory, task_type
):
    workspace, task_dir, _, spec = _create_task(tmp_path, bundle_factory, task_type)
    master = task_dir / "bundle" / "contracts"
    # Every attempt (primary or fallback) starts from a fresh read-only copy.
    for engine_name in ("primary", "fallback"):
        _, agent_task, _ = workspace.next_attempt(
            task_dir, engine_name, spec.task_type
        )
        copies = agent_task.workspace_dir / "contracts"
        for name in SCHEMA_FILES:
            copied = copies / name
            assert copied.read_bytes() == (master / name).read_bytes()
            assert not (copied.stat().st_mode & 0o222), (engine_name, name)


def test_cache_signature_carries_schema_hashes_matching_bundle_files(tmp_path):
    project = tmp_path / "project"
    documents = project / "input" / "deep_research"
    documents.mkdir(parents=True)
    document = documents / "survey.md"
    document.write_text(
        "# Deep research\n\nResidual stress findings.\n", encoding="utf-8"
    )
    runtime = ProjectRuntime.create(project, project_name="schemas")
    proposal = DiscoveryProposalBundle(
        themes=[
            ThemeProposal(
                local_ref="theme_1",
                title="Thermal management",
                description="Generated by deterministic fixture",
                origin="generated",
            )
        ],
        claims=[
            CandidateClaimProposal(
                local_ref="claim_1",
                theme_ref="theme_1",
                candidate_claim="Preheating changes residual stress.",
                origin="generated",
                origin_refs=[],
            )
        ],
    )
    engine = MockEngine([MockResponse(proposal=proposal.model_dump(mode="json"))])
    result = runtime.run(
        TaskType.PARSE_DEEP_RESEARCH,
        ParseDeepResearchInvocation(
            topic="residual stress", document_paths=[document]
        ),
        engines=[engine],
    )
    assert result.outcome is AttemptOutcome.VALID_SCIENTIFIC_RESULT

    task_dir = runtime.project_root / "work" / "tasks" / result.task_id
    bundle = task_dir / "bundle"
    provenance = TaskWorkspace.load_provenance(task_dir)

    # The successful run stored exactly one semantic-cache entry whose
    # signature is the one the runtime actually consulted.
    entries = [
        path
        for path in (runtime.project_root / "work" / "cache").iterdir()
        if path.is_dir()
    ]
    assert len(entries) == 1
    signature = CacheSignature.model_validate_json(
        (entries[0] / "signature.json").read_text(encoding="utf-8")
    )

    input_schema = bundle / "contracts" / "input.schema.json"
    proposal_schema = bundle / "contracts" / "proposal.schema.json"
    assert signature.input_schema_hash == hash_file(input_schema)
    assert signature.proposal_schema_hash == hash_file(proposal_schema)
    # The signature hashes are anchored in the private provenance.
    assert signature.input_schema_hash == provenance.input_schema_hash
    assert signature.proposal_schema_hash == provenance.proposal_schema_hash
    spec = TASK_SPECS[TaskType.PARSE_DEEP_RESEARCH]
    assert signature.task_type is TaskType.PARSE_DEEP_RESEARCH
    assert signature.task_spec_version == spec.version


@pytest.mark.parametrize("task_type", SCHEMA_TASK_TYPES)
def test_instructions_state_the_bundle_protocol_directives(
    tmp_path, bundle_factory, task_type
):
    _, task_dir, _, spec = _create_task(tmp_path, bundle_factory, task_type)
    instructions = (task_dir / "bundle" / "instructions.md").read_text(
        encoding="utf-8"
    )
    prompt = spec.prompt_path.read_text(encoding="utf-8")
    assert instructions.startswith(prompt)
    protocol_section = instructions[len(prompt) :]
    assert "# Bundle protocol" in protocol_section
    for directive in PROTOCOL_DIRECTIVES:
        assert directive in protocol_section


@pytest.mark.parametrize("task_type", SCHEMA_TASK_TYPES)
def test_bundle_manifest_hashes_are_consistent_with_private_provenance(
    tmp_path, bundle_factory, task_type
):
    _, task_dir, created_provenance, spec = _create_task(
        tmp_path, bundle_factory, task_type
    )
    bundle = task_dir / "bundle"
    # Cross-check against the on-disk private trust anchor, not the object
    # returned at construction time.
    provenance = TaskWorkspace.load_provenance(task_dir)
    assert provenance == created_provenance
    manifest = json.loads(
        (bundle / "bundle_manifest.json").read_text(encoding="utf-8")
    )
    expected = TaskWorkspace.expected_immutable_map(provenance)

    assert manifest["task_type"] == spec.task_type.value
    assert manifest["task_spec_version"] == spec.version
    assert manifest["prompt_version"] == spec.prompt_version
    # The manifest cannot authenticate itself; its hash lives under private/.
    assert provenance.expected_bundle_manifest_hash == hash_file(
        bundle / "bundle_manifest.json"
    )

    schemas = manifest["schemas"]
    assert schemas["input"] == {
        "path": "contracts/input.schema.json",
        "sha256": provenance.input_schema_hash,
    }
    assert schemas["proposal"] == {
        "path": "contracts/proposal.schema.json",
        "sha256": provenance.proposal_schema_hash,
    }

    listed = {
        manifest["instructions"]["path"]: manifest["instructions"]["sha256"],
        manifest["engine_input"]["path"]: manifest["engine_input"]["sha256"],
        schemas["input"]["path"]: schemas["input"]["sha256"],
        schemas["proposal"]["path"]: schemas["proposal"]["sha256"],
        **{entry["path"]: entry["sha256"] for entry in manifest["dependencies"]},
        **{entry["path"]: entry["sha256"] for entry in manifest["resources"]},
    }
    # Every manifest-listed hash agrees with the private expected hashes and
    # with the real bytes currently in the bundle (consistency, not authority).
    assert set(listed) == set(provenance.expected_immutable_files)
    for relative, sha256 in listed.items():
        assert expected[relative] == sha256, relative
        assert hash_file(bundle / relative) == sha256, relative

    listed_resources = {
        entry["resource_id"]: entry for entry in manifest["resources"]
    }
    assert set(listed_resources) == {
        entry.resource_id for entry in provenance.resources
    }
    for entry in provenance.resources:
        resource = listed_resources[entry.resource_id]
        assert resource["sha256"] == entry.snapshot_hash
        assert resource["size_bytes"] == entry.size_bytes
        assert resource["logical_name"] == entry.logical_name
        assert resource["media_type"] == entry.media_type
        assert resource["path"] == entry.bundle_relative_path.as_posix()

    listed_dependencies = {
        entry["key"]: entry for entry in manifest["dependencies"]
    }
    assert set(listed_dependencies) == set(provenance.dependencies)
    for key, entry in listed_dependencies.items():
        safe_name = key.replace(":", "__")
        assert entry["path"] == f"input/dependencies/{safe_name}.json"
