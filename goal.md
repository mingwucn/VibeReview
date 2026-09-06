# VibeReview follow-up plan

## 1. Current baseline

I rechecked the repository. The current `master` remains at commit `a73c5343afbbf22e1b240f6a1a2b44dd90c323c7`. That commit contains the hardened scientific contracts and the Python runtime through the deterministic MockEngine milestone.

The implemented baseline now includes:

* immutable numbered repository generations;
* atomic `CURRENT` updates;
* project-level writer locking;
* crash recovery;
* Python-controlled canonical-ID allocation;
* task input and proposal DTOs;
* fixed `TaskType` and `TaskSpec` registration;
* immutable structured task snapshots;
* fallback and scientific-disposition separation;
* semantic cache signatures;
* attempt-level provenance;
* a deterministic MockEngine;
* tests for stale snapshots, fallback, transactions, concurrent ID allocation and negative scientific results.

The repository itself describes this boundary correctly: real engines, Graphify, PDF parsing and document-processing stages have not yet been implemented.

The current TaskSpec and task-workspace interfaces are not yet sufficient for an external engine. TaskSpec currently binds an input model and proposal model but does not yet distinguish private invocation data from sanitized engine input, while the workspace snapshots structured dependencies but not arbitrary Markdown resources.

The attached councils therefore point to the correct next move: **task-resource integrity must be completed before CodexEngine is connected**, and the corpus challenger must be retained before the scientific vertical slice.

---

# 2. Follow-up roadmap

```text
CURRENT
Phase-0 contracts + MockEngine runtime
↓
MILESTONE A
self-contained, sanitized task bundles
↓
MILESTONE B
fake subprocess worker and isolation boundary
↓
MILESTONE C
one real Codex ASSESS_CLAIM task
↓
MILESTONE D
review-project initialization + resource store
↓
MILESTONE E
raw paper Markdown and Deep Research ingestion
↓
MILESTONE F
paper concept sketches + corpus challenger
↓
MILESTONE G
Graphify adapter
↓
MILESTONE H
scientific task handlers
↓
MILESTONE I
five-paper review-section vertical slice
↓
MILESTONE J
PDF parser and additional engines
```

The next Codex session should implement **Milestone A only**.

---

# 3. Milestone A — self-contained task-resource boundary

## Objective

Every semantic task must become a complete, immutable and sanitized package that can be given to an external engine without exposing live canonical state or unresolved project paths.

The target authority chain is:

```text
private runtime invocation
↓
TaskSpec dependency/resource builders
↓
resource snapshotting
↓
sanitized engine input
↓
immutable engine bundle
↓
engine attempt
↓
bundle-integrity verification
↓
proposal validation
↓
freshness check
↓
locked canonical commit
```

---

## A1. Upgrade TaskSpec

The present `TaskSpec` should be replaced with a contract that distinguishes runtime invocation, engine input and engine proposal.

    ```python
@dataclass(frozen=True, slots=True)
    class TaskSpec(Generic[InvocationT, EngineInputT, ProposalT]):
        task_type: TaskType
        version: str

        invocation_model: type[InvocationT]
        engine_input_model: type[EngineInputT]
        proposal_model: type[ProposalT]

        dependency_builder: Callable[
            [InvocationT, RepositorySnapshot],
            tuple[str, ...],
        ]

        resource_builder: Callable[
            [InvocationT, RepositorySnapshot, ProjectContext],
            tuple["TaskResourceRequest", ...],
        ]

        engine_input_builder: Callable[
            [InvocationT, tuple["SnapshottedResource", ...]],
            EngineInputT,
        ]

        prompt_path: Path
        prompt_version: str

        promotion_handler: str
        disposition_handler: str
        ```

### Dependency rule

        Task dependencies must be derived by the registered TaskSpec. The normal runtime API should no longer accept arbitrary caller-controlled `dependency_ids`.

        Target:

    ```python
runtime.run(
        TaskType.ASSESS_CLAIM,
        invocation,
        engines=[engine],
        )
    ```

    Avoid unqualified dependency keys when several objects share a `claim_id`. The current repository index supports type-qualified identities; dependency builders should therefore use forms such as:

    ```text
    CandidateClaim:C0001
    ClaimAssessment:C0001
    FinalClaimValidation:C0001
    ```

    rather than an ambiguous bare `C0001`. The current snapshot implementation explicitly has several claim-keyed object classes sharing one identifier.

    ---

## A2. Separate resource requests from completed snapshots

    Add a runtime-private object:

    ```python
    class TaskResourceRequest(RuntimeModel):
        resource_id: str
        logical_name: str
        source_path: Path
        media_type: str
        ```

        Add an engine-visible object:

        ```python
        class SnapshottedResource(RuntimeModel):
            resource_id: str
            logical_name: str
            snapshot_relative_path: Path
            media_type: str
            size_bytes: int
            content_hash: Sha256
            ```

            The difference is mandatory:

            ```text
            TaskResourceRequest
            =
            where Python should obtain bytes

            SnapshottedResource
            =
            what bytes were copied into the task bundle
            ```

            `SnapshottedResource` must contain no original source path.

            ---

## A3. Separate private invocation DTOs from engine inputs

            Example private invocation:

            ```python
            class ParseDeepResearchInvocation(RuntimeModel):
                topic: str
                document_paths: list[Path]
                ```

                Example engine input:

                ```python
                class ParseDeepResearchInput(RuntimeModel):
                    topic: str
                    document_resource_ids: list[str]
                    ```

                    The private invocation may refer to user or canonical paths.

                    The engine input may refer only to:

                    ```text
                    task-local resource IDs
                    snapshot-relative paths
                    structured canonical identifiers
                    ```

                    No project-input path, generation path or home-directory path should be serializable into the engine-visible input.

                    ---

## A4. Refactor the task directory

                    Use:

                    ```text
                    work/tasks/TASK0042/
                    ├── private/
                    │   ├── invocation.json
                    │   ├── resource_sources.json
                    │   └── task_provenance.json
                    │
                    ├── bundle/
                    │   ├── instructions.md
                    │   ├── bundle_manifest.json
                    │   │
                    │   ├── contracts/
                    │   │   ├── input.schema.json
                    │   │   └── proposal.schema.json
                    │   │
                    │   └── input/
                    │       ├── input.json
                    │       ├── dependencies/
                    │       │   └── ...
                    │       └── resources/
                    │           ├── RES0001/
                    │           │   └── content.md
                    │           └── RES0002/
                    │               └── content.md
                    │
                    ├── attempts/
                    └── accepted/
                    ```

                    Only `bundle/` may be exposed to an external engine.

                    The engine must never receive `private/`.

                    ---

## A5. Use a private bundle-integrity trust anchor

                    An engine-readable manifest cannot safely authenticate itself.

                    Store authoritative expected values in:

                    ```text
                    private/task_provenance.json
                    ```

                    Example:

                    ```json
{
    "expected_bundle_manifest_hash": "sha256:...",

        "expected_immutable_files": {
            "instructions.md": "sha256:...",
            "contracts/input.schema.json": "sha256:...",
            "contracts/proposal.schema.json": "sha256:...",
            "input/input.json": "sha256:...",
            "input/dependencies/CandidateClaim__C0001.json": "sha256:...",
            "input/resources/RES0001/content.md": "sha256:..."
        }
}
```

`bundle/bundle_manifest.json` remains useful to the engine, but post-execution integrity must be checked against the expected hashes stored under `private/`.

This prevents an engine from modifying both a resource and the manifest that describes it.

---

## A6. Make resource naming Python-controlled

Use local resource IDs:

```text
RES0001
RES0002
...
```

Suggested pattern:

```text
^RES[0-9]{4,}$
```

The destination should be generated by Python:

```text
input/resources/RES0001/content.md
```

Rules:

* `resource_id` must be unique within a task;
* `logical_name` is metadata, not a filename;
* absolute destination paths are forbidden;
* `..` path components are forbidden;
* path separators from external metadata are forbidden;
* two source files with the same filename must remain distinct;
* supported extensions should come from a controlled media-type map.

Example:

```python
MEDIA_EXTENSIONS = {
    "text/markdown": ".md",
    "text/plain": ".txt",
    "application/json": ".json",
}
```

---

## A7. Implement copy-while-hashing

Resource snapshotting should follow:

```text
validate source and permitted root
↓
reject unsafe symlink/path escape
↓
open source
↓
fstat source before copy
↓
stream to temporary snapshot file
while calculating SHA-256 and byte size
↓
fstat source after copy
↓
verify stable device/inode/size/mtime where available
↓
flush and fsync
↓
atomically rename snapshot
↓
make snapshot read-only
```

The snapshot hash must be calculated from the bytes written into the task bundle.

If a mutable source changes while being copied:

```text
SNAPSHOT_SOURCE_CHANGED
```

should terminate task construction before any engine invocation.

This is an internal snapshot-construction failure, not a fallback condition.

---

## A8. Emit explicit JSON schemas

Every task bundle should contain:

```text
contracts/input.schema.json
contracts/proposal.schema.json
```

Generated from:

    ```python
    spec.engine_input_model.model_json_schema()
spec.proposal_model.model_json_schema()
    ```

    The instructions should state:

    ```text
    Read input/input.json.

    Read resources only through the resource entries in
    bundle_manifest.json.

    Return exactly one JSON object conforming to
    contracts/proposal.schema.json.

    Do not emit Markdown fences or explanatory text.

    Do not allocate canonical scientific IDs.
    ```

    The task signature should include:

    ```text
    TaskSpec version
    prompt hash
    input-schema hash
    proposal-schema hash
    structured-dependency hashes
    resource hashes
    engine-input hash
    bundle-manifest hash
    ```

    ---

## A9. Verify bundle integrity before and after every attempt

    Before execution:

    ```text
    verify bundle against private expected hashes
    ```

    After execution, before parsing the proposal:

    ```text
    verify bundle again against private expected hashes
    ```

    Post-execution mutation of any of the following must reject the attempt:

    ```text
    instructions.md
    bundle_manifest.json
    input/input.json
    input/dependencies/*
                         input/resources/*
                         contracts/input.schema.json
                         contracts/proposal.schema.json
                         ```

                         Add:

                         ```python
                         AttemptOutcome.ENGINE_WORKSPACE_INTEGRITY_FAILURE
                         ```

                         It is engine-attributable and may trigger configured fallback.

                         A mismatch detected before engine execution is an internal runtime failure and must not trigger fallback.

                         ---

## A10. Give every attempt a fresh execution copy

The task-level `bundle/` should remain an immutable template.

For each engine attempt, create:

```text
attempts/01-codex/workspace/
attempts/02-kimi/workspace/
```

from the task bundle.

The engine should operate on the attempt copy, not on the task's master bundle.

This ensures that:

```text
primary engine tampers with its workspace
```

does not corrupt the fallback engine's input.

Each fallback attempt starts from a newly verified copy of the original immutable bundle.

---

## A11. Add resource freshness checking

The writer-locked freshness check must cover:

```text
structured canonical dependencies
+
resource dependencies
```

Private task provenance should record, for mutable sources:

```json
{
"resource_id": "RES0001",
"source_dependency": {
"type": "external_file",
"source_hash_at_snapshot": "sha256:..."
},
"snapshot_hash": "sha256:..."
}
```

For later content-addressed resources:

```json
{
    "resource_id": "RES0001",
    "source_dependency": {
        "type": "content_addressed_resource",
        "content_hash": "sha256:..."
    }
}
```

A freshness mismatch must return:

```text
STALE_SNAPSHOT
```

It must not invoke a fallback engine.

For V1, the current conservative generation-level freshness rule may remain. Dependency-level relaxation can be considered only after the vertical slice if unnecessary stale rebuilds become measurable.

---

## A12. Add TaskSpec executability preflight

Before creating a task, verify:

```text
prompt exists
invocation model registered
engine-input model registered
proposal model registered
dependency builder registered
resource builder registered
engine-input builder registered
promotion handler implemented
disposition handler implemented
```

Add:

    ```python
validate_task_spec_executable(spec)
    ```

    and:

    ```python
executable_task_types()
    ```

    An unsupported TaskSpec should fail with:

    ```text
    TASK_TYPE_NOT_IMPLEMENTED
    ```

    before:

    ```text
    task ID allocation
    workspace creation
    engine execution
    fallback
    ```

    The current registry lists planned tasks whose promotion handlers are not all implemented, so this preflight is required before real engine use.

    ---

## A13. Complete the evidence-quality rule

    When:

    ```text
    EvidenceQuality.assessability = not_assessable
    ```

    require:

    ```text
    directness ∈ {
        unclear,
            not_assessable
    }
```

The existing restrictions on strength and methodological relevance should remain.

Required tests:

```text
not_assessable + direct          → fail
not_assessable + indirect        → fail
not_assessable + unclear         → pass
not_assessable + not_assessable  → pass
```

---

## A14. Add deterministic CI

Add:

```text
.github/workflows/tests.yml
```

```yaml
name: tests

on:
push:
pull_request:

jobs:
pytest:
runs-on: ubuntu-latest

strategy:
matrix:
python-version: ["3.11", "3.12", "3.13"]

steps:
- uses: actions/checkout@v4

- uses: actions/setup-python@v5
with:
python-version: ${{ matrix.python-version }}

- run: python -m pip install -e '.[test]'

- run: python -m pytest -m "not external_engine"
```

Register:

```toml
markers = [
    "external_engine: requires a live external LLM-agent engine",
]
```

Document the V1 execution environment as:

```text
Linux
WSL2
```

Native Windows support should remain deferred because the current lock implementation uses `fcntl`.

---

# 4. Milestone-A tests

At minimum, add these test groups.

## TaskSpec ownership

* dependencies are generated from the TaskSpec;
* normal callers cannot omit dependencies;
* claim-keyed dependencies use qualified identifiers;
* required resources are generated from the TaskSpec;
* unimplemented handlers fail before engine invocation.

## Resource copying

* copied bytes match source bytes;
* hash and size match copied bytes;
* mutation during copying is detected;
* duplicate source filenames do not collide;
* duplicate resource IDs fail;
* unsupported media types fail.

## Path safety

* absolute generated path rejected;
* traversal rejected;
* symlink escape rejected;
* resolved destination remains inside the bundle;
* logical names are never interpreted as paths.

## Privacy

Recursively inspect all engine-visible text and JSON. Verify absence of:

```text
project root
generation directory
original input path
user home path
private invocation path
```

## Trust-anchor integrity

Tamper with both:

```text
bundle_manifest.json
resource file
```

The attempt must still fail because authoritative expected hashes reside under `private/`.

## Attempt isolation

* primary attempt modifies its workspace;
* primary receives `ENGINE_WORKSPACE_INTEGRITY_FAILURE`;
* fallback gets a clean bundle;
* fallback may succeed;
* master task bundle remains unchanged.

## Resource freshness

* source association changes before commit;
* result becomes `STALE_SNAPSHOT`;
* fallback is not invoked;
* a new task snapshot is generated.

## Schema visibility

* input schema corresponds to `engine_input_model`;
* proposal schema corresponds to `proposal_model`;
* schemas are read-only;
* schema hashes enter the cache signature;
* schema changes invalidate cache reuse.

---

# 5. Milestone-A gate

Do not proceed until all are true:

```text
TaskSpec owns dependencies and resources

private invocation and engine input are distinct

resource requests and completed snapshots are distinct

engine-visible files contain no live paths

private task provenance is the integrity trust anchor

all resources are copied while hashing

source stability is checked

fallback attempts receive fresh bundles

resource freshness participates in commit checks

JSON schemas are visible to the engine

unimplemented tasks fail before engine invocation

EvidenceQuality directness constraint passes

local pytest passes

GitHub Actions passes
```

---

# 6. Milestone B — fake subprocess boundary

After Milestone A, implement a generic subprocess worker and a deterministic fake executable.

## Deliverables

```text
src/vibereview/runtime/engines/
├── base.py
├── subprocess.py
└── fake_worker.py
```

The fake worker should support modes:

```text
valid proposal
malformed JSON
schema-invalid JSON
non-zero exit
timeout
modify instructions
modify input schema
modify proposal schema
modify dependency
modify resource
modify bundle manifest
create unexpected output
attempt path traversal
```

## Execution rules

    ```python
subprocess.run(
        command_as_list,
        cwd=isolated_attempt_workspace,
        shell=False,
        )
    ```

    Use dedicated temporary values for:

    ```text
    HOME
    XDG_CONFIG_HOME
    XDG_CACHE_HOME
    TMPDIR
    ```

    Timeout handling must terminate the relevant process tree where practical.

## Gate

    ```text
    fake worker cannot mutate canonical state

    bundle tampering is detected

    unexpected files are detected or quarantined

    timeout works

    failed attempts remain recorded

    fallback classification remains correct
    ```

    ---

# 7. Milestone C — one Codex qualification task

    Only after Milestone B passes should CodexEngine be connected.

    Use one task:

    ```text
    ASSESS_CLAIM
    ```

    This task is already represented by a proposal DTO, disposition handler and promotion path, including tests showing that a valid `REJECT` is canonicalized without invoking fallback.

    Use four controlled fixtures:

    ```text
    RETAIN

    NARROW

    REJECT / insufficient_evidence

    REJECT / contradicted
    ```

    Assertions should concern runtime behavior, not exact wording:

    ```text
    proposal schema valid

    ClaimAssessment canonicalized

    REJECT retained in canonical state

    REJECT does not create ClaimPacket

    REJECT does not invoke fallback

    attempt provenance complete

    engine workspace unchanged

    canonical project inaccessible or read-only
    ```

    Mark live tests:

    ```python
    @pytest.mark.external_engine
    ```

    Do not run them in normal CI.

    ---

# 8. Milestone D — user-facing project and immutable resources

    After Codex qualification, implement:

    ```bash
    python vibe_review.py init reviews/<topic_slug>
    ```

    Generated layout:

    ```text
    reviews/<topic_slug>/
    ├── project.yaml
    ├── input/
    │   ├── deep_research/
    │   └── papers/
    ├── state/
    ├── work/
    └── output/
    ```

    Then add immutable content-addressed resources:

    ```text
    state/resources/sha256/<digest>
    ```

    Rules:

    ```text
    write temporary blob
    copy while hashing
    fsync
    atomic move to digest path
    verify existing digest collision
    never edit blob in place
    ```

    Mutable resource associations and metadata should remain in numbered repository generations.

    Start with raw paper Markdown. PDF parsing remains deferred.

    ---

# 9. Milestone E — Deep Research and paper ingestion

## Deep Research

    The proposal should preserve:

    ```text
    themes
    candidate claims
    terminology
    paper candidates
    controversies
    gaps
    ```

    Only themes and claims need immediate promotion into the frozen scientific graph. The remaining outputs should be retained as versioned discovery artifacts.

    Every item should retain:

    ```text
    source resource ID
    logical document name
    optional source section or offsets
    ```

## Papers

    Import:

    ```text
    input/papers/*.md
                  ```

                  into the immutable resource store, reconcile Paper identities, and create canonical Paper records.

                  ---

# 10. Milestone F — paper concept sketches and corpus challenger

This milestone is mandatory before retrieval queries are finalized.

```text
paper Markdown
↓
per-paper concept sketch
↓
batched corpus reduction
↓
CORPUS_CHALLENGER
↓
themes and claims absent from Deep Research
↓
merge/deduplicate
↓
retrieval-query generation
```

The five-paper fixture must contain one relevant concept absent from all Deep Research documents.

Expected:

```text
corpus challenger recovers omitted concept
```

This requirement was retained by both attached council reviews.

---

# 11. Milestone G — Graphify adapter

Implement:

```text
MockGraphify
↓
actual Graphify adapter
```

Graphify returns an ID-less runtime proposal.

```python
class RetrievedSpanProposal(BaseModel):
paper_ref: str
query_ref: str
start_offset: int
end_offset: int
source_text: str
source_span_hash: Sha256
retrieval_score: float | None
```

Before allocating `Rxxxx`:

```text
paper and query references resolve

offsets are valid

canonical_raw_md[start:end] == source_text

    SHA256(source_text) == source_span_hash
    ```

    Invalid backend proposals are retained in a runtime retrieval-attempt ledger with diagnostics. They do not receive canonical span IDs and do not enter the evidence chain.

    ---

# 12. Milestone H — scientific handlers

    Implement one handler at a time:

    1. retrieval-query generation;
    2. Graphify retrieval promotion;
    3. evidence assessment;
    4. publication-level evidence aggregation;
    5. claim assessment;
    6. claim revision;
    7. final claim validation;
    8. Python-created ClaimPacket;
    9. proposition generation;
    10. proposition audit;
    11. prose rendering;
    12. final sentence audit;
    13. deterministic manuscript assembly.

    Every handler requires:

    ```text
    TaskSpec
    invocation DTO
    engine-input DTO
    proposal DTO
    dependency builder
    resource builder
    promotion handler
    disposition handler
    MockEngine tests
    ```

    ---

# 13. Milestone I — five-paper vertical slice

    Inputs:

    ```text
    one topic
    two or three Deep Research Markdown files
    five paper Markdown files
    ```

    Required output:

    ```text
    theme map
    candidate claims
    corpus-challenger additions
    retrieval queries
    source spans
    EvidenceRecords
    ClaimPaperEvidence
    ClaimAssessments
    approved and rejected claims
    ClaimPackets
    PropositionRecords
    semantic audits
    RenderedSentences
    sentence audits
    one complete review section
    references
    audit report
    ```

    Acceptance requires:

    * every scientific sentence maps to approved claims;
    * every approved claim maps to publication-level evidence;
    * every evidence item maps to exact Markdown offsets;
    * contradictory and qualifying findings remain represented;
    * rejected claims remain canonical;
    * no negative scientific result triggers fallback;
    * no agent writes live canonical state;
    * the corpus challenger detects the deliberately omitted theme;
    * local and CI tests pass.

    ---

# 14. Additional engines and PDF parsing

    Only after the Codex five-paper slice passes:

    ```text
    KimiEngine
    ↓
    AgyEngine
    ↓
    OpenCodeEngine, optionally
    ```

    Each adapter must pass the same bounded-worker conformance suite.

    PDF parsing should then be added as:

    ```text
    PDFParserAdapter
    ↓
    canonical raw.md
    ↓
    unchanged scientific pipeline
    ```

    The raw-Markdown route must remain supported.

    ---

# 15. Immediate Codex assignment

    Send the following instruction now:

    > Implement **VibeReview Milestone A: self-contained task-resource integrity**.
    >
    > Do not modify the frozen scientific architecture and do not implement a real engine.
    >
    > Refactor TaskSpec so it owns:
    >
    > * an invocation model;
    > * an engine-input model;
    > * a proposal model;
    > * a dependency builder;
    > * a resource builder;
    > * an engine-input builder;
    > * prompt metadata;
    > * a promotion handler;
    > * a disposition handler.
    >
    > Remove caller-controlled dependency selection from the normal public runtime API. Use type-qualified dependency keys wherever persistent object classes share the same canonical identifier.
    >
    > Introduce:
    >
    > * `TaskResourceRequest`, retained only in private runtime provenance and allowed to contain a source path;
    > * `SnapshottedResource`, exposed to the engine and containing only a task-local resource ID, logical name, snapshot-relative path, media type, byte size and SHA-256.
    >
    > Refactor each task directory into:
    >
    > * `private/`;
    > * immutable template `bundle/`;
    > * per-attempt execution workspaces under `attempts/`;
    > * `accepted/`.
    >
    > Only a fresh per-attempt copy of `bundle/` may be given to an engine. A fallback attempt must never reuse a workspace modified by an earlier engine.
    >
    > Store source paths and authoritative expected bundle hashes in `private/task_provenance.json`. Store an engine-readable but non-authoritative manifest in `bundle/bundle_manifest.json`.
    >
    > Generate Python-controlled resource IDs such as `RES0001`. Never use logical names directly as paths. Reject absolute paths, traversal, unsafe symlink resolution, duplicate resource IDs and destination collisions.
    >
    > Snapshot resources by streaming source bytes into a temporary file while calculating SHA-256 and byte size. Compare source file-descriptor metadata before and after copying, then flush, fsync and atomically rename the task snapshot.
    >
    > Generate:
    >
    > * `bundle/contracts/input.schema.json`;
    > * `bundle/contracts/proposal.schema.json`;
    >
    > from the registered Pydantic models.
    >
    > Ensure the engine-facing input uses only resource IDs and task-relative resource paths. No engine-visible text or JSON may contain a project root, canonical-generation path, original input path or user-home path.
    >
    > Verify all immutable bundle files against private expected hashes before execution and again after execution. Add `ENGINE_WORKSPACE_INTEGRITY_FAILURE` for post-execution mutation. It may trigger fallback. Pre-execution integrity failure is an internal runtime failure and must not trigger fallback.
    >
    > Include resource dependencies in the writer-locked freshness check. Resource changes must produce `STALE_SNAPSHOT`, not fallback.
    >
    > Add TaskSpec executability preflight. Missing builders, models, prompts, promotion handlers or disposition handlers must fail before task allocation or engine invocation.
    >
    > Complete the EvidenceQuality rule: when `assessability=not_assessable`, directness may be only `unclear` or `not_assessable`.
    >
    > Add GitHub Actions deterministic CI for Python 3.11, 3.12 and 3.13 using `pytest -m "not external_engine"`. Register the marker and document Linux/WSL2 as the supported V1 environment.
    >
    > Add tests for:
    >
    > 1. TaskSpec-derived qualified dependencies;
    > 2. private invocation versus sanitized engine input;
    > 3. resource-request versus resource-snapshot separation;
    > 4. copied-byte hash and size;
    > 5. source mutation during copy;
    > 6. duplicate filenames and duplicate resource IDs;
    > 7. traversal, absolute paths and symlink escapes;
    > 8. absence of live paths from engine-visible files;
    > 9. private trust-anchor verification;
    > 10. tampering with instructions, schemas, structured dependencies, resources and bundle manifest;
    > 11. clean per-attempt bundles during fallback;
    > 12. resource freshness mismatch;
    > 13. generated JSON schemas and cache invalidation;
    > 14. unimplemented TaskSpecs failing before MockEngine invocation;
    > 15. all not-assessable/directness combinations.
    >
    > Run the full local pytest suite.
    >
    > Do not implement CodexEngine, KimiEngine, AgyEngine, OpenCodeEngine, fake subprocess execution, Deep Research semantic parsing, paper ingestion, corpus challenger, Graphify or manuscript generation.
    >
    > Return:
    >
    > * changed files;
    > * revised TaskSpec API;
    > * task-directory structure;
    > * runtime models;
    > * failure-taxonomy changes;
    > * test inventory;
    > * full pytest result;
    > * CI workflow;
    > * explicit confirmation that no real engine was implemented.

    The next implementation review should inspect the resulting Milestone-A code, representative `private/` and `bundle/` task artifacts, local pytest output and GitHub Actions results—not another architecture proposal.

