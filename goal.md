# VibeReview — Full Concrete Implementation Plan

## Authoritative post-`r3` roadmap

This document supersedes the earlier fragmented handoffs. It begins from the currently inspected repository head, commit `ca7e1a4760efc0ceffc6b69813fc3bf44eb19d1e` (`r3`).

At this head:

* the frozen scientific contracts are implemented;
* immutable repository generations and atomic `CURRENT` updates are present;
* canonical IDs are allocated by Python inside locked transactions;
* task invocation, engine input and proposal DTOs are separated;
* task resources are copied into sanitized immutable bundles;
* each engine attempt receives a fresh bundle copy;
* resource freshness and bundle integrity are verified;
* the MockEngine path is implemented;
* deterministic CI passes on Python 3.11, 3.12 and 3.13;
* the recorded Python 3.11 job reports **326 passing tests**.

The latest council findings do not require a scientific redesign. They require a deterministic subprocess boundary, bounded diagnostics, secure proposal import, writable-resource quotas, tested confinement, explicit credential handling, preservation of the corpus challenger, complete Deep Research discovery output, and retention of the canonical `RetrievalDisposition` layer.

---

# 1. Final product objective

The normal user workflow should eventually be:

```bash
python -m vibereview init reviews/<topic_slug>

# Human adds:
# reviews/<topic_slug>/input/deep_research/*.md
# reviews/<topic_slug>/input/papers/*.md or *.pdf

python -m vibereview run reviews/<topic_slug>
```

The human supplies:

```text
1. Review topic
2. Several Deep Research documents organised by subtopic
3. Research papers as PDFs and/or canonical raw Markdown
```

The application produces:

```text
output/
├── status.md
├── scope.md
├── theme_map.md
├── corpus.md
├── discovery/
├── claims/
├── evidence/
├── sections/
├── manuscript_internal.md
├── manuscript.md
├── references.md
└── audit/
```

Codex, Kimi, Agy and OpenCode are replaceable semantic workers. They do not control the workflow or canonical scientific state.

---

# 2. Frozen responsibility model

```text
Human
=
scientific authority
topic selection
corpus supply
manual inspection
final editorial decision

Python
=
workflow authority
canonical-ID authority
state-transition authority
task routing
resource snapshotting
fallback policy
freshness checks
cache control
transaction control
audit control

Graphify
=
paper-text retrieval backend

LLM agent engine
=
bounded semantic worker
returns proposals only

Pydantic + repository validators
=
admission gate to canonical state
```

The governing rule is:

```text
Engine proposes
↓
Python validates
↓
Python determines scientific transition
↓
Python commits or rejects
```

An engine must never:

* allocate canonical scientific IDs;
* edit a repository generation;
* edit the paper corpus;
* approve a claim directly;
* select the next pipeline stage;
* reinterpret a technical failure as a scientific result;
* invoke a second model because the first model returned an undesirable conclusion.

---

# 3. Frozen scientific chain

```text
ThemeRecord
↓
CandidateClaim
↓
RetrievalQuery
↓
Graphify retrieval proposal
↓
validated RetrievedSpan
↓
RetrievalDisposition
↓
EvidenceRecord
↓
ClaimPaperEvidence
↓
ClaimAssessment
↓
claim revision
↓
FinalClaimValidation
↓
Python-created ClaimPacket
↓
PropositionRecord
↓
SemanticAuditResult
↓
RenderedSentence
↓
RenderedSentenceAudit
↓
deterministic citation rendering
↓
deterministic manuscript assembly
↓
final integrity audit
```

No generative transformation is permitted after the final rendered-sentence audit.

---

# 4. Scientific operating rules

## 4.1 Deep Research is discovery input

Deep Research may contribute:

```text
themes
terminology
candidate papers
candidate relationships
controversies
research gaps
possible manuscript structure
```

It does not constitute paper evidence.

The allowed path is:

```text
Deep Research statement
↓
candidate theme / claim / reference
↓
supplied paper corpus
↓
retrieved source evidence
↓
validated scientific claim
```

The disallowed path is:

```text
Deep Research statement
↓
direct manuscript assertion
```

## 4.2 Candidate claims are provisional

Every initial LLM statement is a candidate:

```text
candidate claim
↓
retain
weaken
narrow
reformulate
or reject
```

The evidence must be allowed to alter or eliminate the statement.

## 4.3 Retrieval must be adversarial

Every substantive claim should receive at least:

```text
support query
contradiction query
boundary-condition query
alternative-explanation query
```

Optional intents:

```text
methodological challenge
null result
```

## 4.4 Paper-level aggregation precedes claim synthesis

Multiple passages from one publication do not count as multiple independent studies.

```text
several spans from P0007
↓
several EvidenceRecords
↓
one ClaimPaperEvidence unit for Cxxxx × P0007
```

## 4.5 Negative results are canonical results

Examples:

```text
ClaimAssessment = REJECT
SemanticAuditResult = UNSUPPORTED
FinalClaimValidation = UNCLEAR
RenderedSentenceAudit = OVERSTATED
```

These results must be stored. They may block downstream transition, but they do not trigger engine fallback.

## 4.6 Epistemic scope is corpus-bounded

Default:

```yaml
epistemic_scope: supplied_corpus
```

Allowed:

> No contradictory evidence was identified in the supplied corpus.

Not automatically allowed:

> No contradictory studies exist.

---

# 5. Full milestone sequence

| Milestone | Deliverable                                  | Gate                               |
| --------- | -------------------------------------------- | ---------------------------------- |
| A         | Task-resource integrity                      | **Complete at `r3`**               |
| B1        | Subprocess contracts and failure semantics   | Unit tests pass                    |
| B2        | Deterministic fake subprocess boundary       | Conformance suite passes           |
| B3        | Credential and confinement qualification     | Real-engine gate passes            |
| C         | One bounded Codex `ASSESS_CLAIM` task        | Four live fixtures pass            |
| D         | User-facing CLI and project structure        | `init/status/run` work             |
| E         | Immutable content-addressed resources        | Markdown import is reproducible    |
| F         | Deep Research discovery ingestion            | Complete discovery bundle retained |
| G         | Paper concept sketches and corpus challenger | Omitted concept recovered          |
| H         | Graphify retrieval and dispositions          | Exact source provenance passes     |
| I         | Evidence and claim pipeline                  | Claim packets produced             |
| J         | Proposition, prose and manuscript pipeline   | Final semantic boundary passes     |
| K         | Five-paper vertical slice                    | One full review section passes     |
| L         | Scaling and additional engines               | 20–50-paper pilots pass            |
| M         | PDF parser and production review             | One-script review succeeds         |

---

# 6. Milestone B1 — subprocess contracts

## 6.1 Purpose

Milestone B1 defines all runtime objects and deterministic failure semantics required before an external process is executed.

No real LLM engine is implemented in this milestone.

## 6.2 New runtime modules

```text
src/vibereview/runtime/
├── subprocess.py
├── execution.py
├── diagnostics.py
├── output_policy.py
├── resource_limits.py
├── execution_inventory.py
├── confinement.py
└── credentials.py
```

The existing scientific models must not be modified except where a demonstrated contract defect exists.

---

## 6.3 Extend `AttemptOutcome`

Add:

```python
class AttemptOutcome(StrEnum):
    ENGINE_EXECUTION_FAILURE = "engine_execution_failure"
    ENGINE_FORMAT_FAILURE = "engine_format_failure"
    ENGINE_SCHEMA_FAILURE = "engine_schema_failure"
    ENGINE_PROPOSAL_VALIDATION_FAILURE = (
            "engine_proposal_validation_failure"
            )
    ENGINE_WORKSPACE_INTEGRITY_FAILURE = (
            "engine_workspace_integrity_failure"
            )
    ENGINE_OUTPUT_POLICY_FAILURE = (
            "engine_output_policy_failure"
            )
    ENGINE_RESOURCE_LIMIT_FAILURE = (
            "engine_resource_limit_failure"
            )

    VALID_SCIENTIFIC_RESULT = "valid_scientific_result"
    STALE_SNAPSHOT = "stale_snapshot"
    TASK_TYPE_NOT_IMPLEMENTED = "task_type_not_implemented"
    INTERNAL_RUNTIME_FAILURE = "internal_runtime_failure"
    TRANSACTION_FAILURE = "transaction_failure"
    CONTRACT_IMPLEMENTATION_FAILURE = (
            "contract_implementation_failure"
            )
    ```

    Fallback-eligible outcomes:

    ```python
    FALLBACK_OUTCOMES = {
        AttemptOutcome.ENGINE_EXECUTION_FAILURE,
        AttemptOutcome.ENGINE_FORMAT_FAILURE,
        AttemptOutcome.ENGINE_SCHEMA_FAILURE,
        AttemptOutcome.ENGINE_PROPOSAL_VALIDATION_FAILURE,
        AttemptOutcome.ENGINE_WORKSPACE_INTEGRITY_FAILURE,
        AttemptOutcome.ENGINE_OUTPUT_POLICY_FAILURE,
        AttemptOutcome.ENGINE_RESOURCE_LIMIT_FAILURE,
    }
```

Fallback remains forbidden for all other outcomes.

---

## 6.4 Record secondary technical failures

One primary outcome is required, but all safely detected technical defects should be retained.

```python
class AttemptFailureStage(StrEnum):
    PROCESS = "process"
    WORKSPACE = "workspace"
    OUTPUT_TREE = "output_tree"
    PROPOSAL_FILE = "proposal_file"
    FORMAT = "format"
    SCHEMA = "schema"
    PROPOSAL_VALIDATION = "proposal_validation"
    RESOURCE_LIMIT = "resource_limit"
    ```

    ```python
    class AttemptFailure(RuntimeModel):
        code: str
        stage: AttemptFailureStage
        message: str
        relative_path: Path | None = None
        ```

        Extend:

        ```python
        class TaskAttemptRecord(RuntimeModel):
# existing fields
            outcome: AttemptOutcome
            detected_failures: tuple[AttemptFailure, ...] = ()
            ```

            Unsafe files must not be opened merely to discover additional failures.

            ---

## 6.5 Resource-limit codes

            ```python
            class ResourceLimitCode(StrEnum):
                MAX_WRITABLE_TREE_BYTES = "max_writable_tree_bytes"
                MAX_WRITABLE_FILE_COUNT = "max_writable_file_count"
                MAX_WRITABLE_SINGLE_FILE_BYTES = (
                        "max_writable_single_file_bytes"
                        )
                MAX_WRITABLE_DIRECTORY_DEPTH = (
                        "max_writable_directory_depth"
                        )
                MAX_PROCESS_COUNT = "max_process_count"
                MAX_OPEN_FILES = "max_open_files"
                MAX_CPU_TIME = "max_cpu_time"
                MAX_ADDRESS_SPACE = "max_address_space"
                ```

                Example:

                ```json
{
    "code": "max_writable_tree_bytes",
    "stage": "resource_limit",
    "message": "Writable tree exceeded 16777216 bytes.",
    "relative_path": "scratch"
}
```

---

## 6.6 Freeze primary-outcome precedence

The following order is authoritative:

| Priority | Condition                                                          | Primary outcome                      |
| -------: | ------------------------------------------------------------------ | ------------------------------------ |
|        1 | Explicit resource-limit breach                                     | `ENGINE_RESOURCE_LIMIT_FAILURE`      |
|        2 | Launch failure, timeout or non-zero exit                           | `ENGINE_EXECUTION_FAILURE`           |
|        3 | Immutable bundle changed                                           | `ENGINE_WORKSPACE_INTEGRITY_FAILURE` |
|        4 | Output directory replaced, unauthorized output or unsafe file type | `ENGINE_OUTPUT_POLICY_FAILURE`       |
|        5 | Missing, empty, oversized, non-UTF-8 or malformed regular proposal | `ENGINE_FORMAT_FAILURE`              |
|        6 | Pydantic proposal mismatch                                         | `ENGINE_SCHEMA_FAILURE`              |
|        7 | Schema-valid but task-invalid proposal                             | `ENGINE_PROPOSAL_VALIDATION_FAILURE` |
|        8 | Contract-valid scientific proposal                                 | `VALID_SCIENTIFIC_RESULT`            |

Examples:

```text
non-zero exit + valid proposal
→ ENGINE_EXECUTION_FAILURE
```

```text
bundle mutation + malformed proposal
→ ENGINE_WORKSPACE_INTEGRITY_FAILURE
```

```text
unauthorized output + malformed proposal
→ ENGINE_OUTPUT_POLICY_FAILURE
```

```text
ClaimAssessment = REJECT
→ VALID_SCIENTIFIC_RESULT
```

---

## 6.7 Subprocess-policy model

```python
class SubprocessPolicy(RuntimeModel):
    timeout_seconds: float
    terminate_grace_seconds: float

    max_stdout_bytes: int
    max_stderr_bytes: int
    max_proposal_bytes: int

    max_writable_tree_bytes: int
    max_writable_files: int
    max_writable_single_file_bytes: int
    max_writable_directory_depth: int

    max_open_files: int | None = None
    max_processes: int | None = None
    max_cpu_seconds: int | None = None
    max_address_space_bytes: int | None = None

    writable_tree_scan_interval_seconds: float

    inherited_environment_allowlist: tuple[str, ...]
    allowed_output_files: tuple[str, ...] = ("proposal.json",)
    ```

    Suggested deterministic test defaults:

    ```yaml
    timeout_seconds: 10
    terminate_grace_seconds: 1

    max_stdout_bytes: 65536
    max_stderr_bytes: 65536
    max_proposal_bytes: 1048576

    max_writable_tree_bytes: 16777216
    max_writable_files: 256
    max_writable_single_file_bytes: 4194304
    max_writable_directory_depth: 8

    writable_tree_scan_interval_seconds: 0.05
    ```

    Writable-growth quotas apply only to:

    ```text
    output/
    scratch/
    home/
    tmp/
    ```

    They do not apply to:

    ```text
    bundle/
    launcher/
    credentials/
    ```

    The proposal remains subject to `max_proposal_bytes`.

    ---

## 6.8 Diagnostic-capture contract

    ```python
    class DiagnosticCapture(RuntimeModel):
        relative_path: Path

        bytes_observed: int
        bytes_retained: int
        truncated: bool

        retained_redacted_hash: Sha256
        redactions_applied: int
        ```

        Semantics:

        ```text
        bytes_observed
        =
        raw bytes read from process stream

        bytes_retained
        =
        redacted bytes persisted

        retained_redacted_hash
        =
        SHA-256 of the exact persisted diagnostic file
        ```

        No persistent hash should describe a complete unredacted secret-bearing stream.

        ---

## 6.9 Execution-file inventory

        ```python
        class ExecutionFileRecord(RuntimeModel):
            relative_path: Path
            file_type: str
            size_bytes: int | None
            content_hash: Sha256 | None
            ```

            Safe file types may be hashed.

            The following must not be opened or hashed:

            ```text
            FIFO
            socket
            device
            unsafe symlink target
            oversized file
            ```

            Their type and relative path are sufficient.

            ---

## 6.10 Confinement levels

            ```python
            class ConfinementLevel(StrEnum):
                TEST_ONLY = "test_only"
                PATH_HYGIENE = "path_hygiene"
                OS_SANDBOX = "os_sandbox"
                ENGINE_NATIVE_SANDBOX = "engine_native_sandbox"
                ```

                The initial temporary execution backend must be:

                ```text
                TEST_ONLY
                ```

                A real engine cannot be enabled through a `TEST_ONLY` backend.

                ---

## 6.11 Object-root proposal schemas

                Every engine proposal must be a JSON object.

                TaskSpec preflight must:

                1. reject Pydantic `RootModel`;
                2. generate `model_json_schema()`;
                3. resolve a top-level `$ref`, when necessary;
                4. require the effective schema root to be `type: object`.

                Valid:

                ```python
                class EvidenceRecordProposalBundle(BaseModel):
                    evidence: list[EvidenceRecordProposal]
                    ```

                    Invalid:

                    ```python
                    class EvidenceProposalList(
                            RootModel[list[EvidenceRecordProposal]]
                            ):
                        ...
                            ```

                                ---

## 6.12 Milestone-B1 tests

                                Add:

                                ```text
                                tests/runtime/
                                ├── test_attempt_precedence.py
                                ├── test_subprocess_policy.py
                                ├── test_diagnostic_contract.py
                                ├── test_resource_limit_contract.py
                                ├── test_output_policy_contract.py
                                └── test_proposal_schema_root.py
                                ```

                                Required cases:

                                ```text
                                new outcomes are fallback-eligible

                                scientific dispositions remain non-fallback

                                failure precedence is deterministic

                                writable quotas exclude bundle files

                                proposal root-list models fail preflight

                                named proposal bundles pass

                                diagnostic hash equals persisted redacted file hash
                                ```

## Milestone-B1 gate

                                ```text
                                all models and enums implemented

                                fallback set updated

                                failure precedence tested

                                object-root preflight tested

                                existing 326-test suite remains green

                                no external process executed yet
                                ```

                                ---

# 7. Milestone B2 — deterministic fake subprocess boundary

## 7.1 Purpose

                                A real operating-system process should be exercised before Codex is connected.

                                The fake worker must prove:

                                ```text
                                sanitized execution
                                bounded output
                                timeout handling
                                output-policy enforcement
                                bundle-integrity detection
                                resource-limit enforcement
                                safe proposal import
                                clean fallback
                                canonical-state preservation
                                ```

                                ---

## 7.2 Execution-root structure

                                Each attempt receives a new temporary directory outside the review project:

                                ```text
                                /tmp/vibereview-exec-<random>/
                                ├── bundle/          # copied immutable task input
                                ├── output/          # only proposal.json permitted
                                ├── scratch/         # bounded writable workspace
                                ├── home/            # isolated HOME
                                ├── tmp/             # isolated TMPDIR
                                ├── credentials/     # runtime-controlled
                                └── launcher/        # trusted fake worker/launcher
                                ```

                                The engine process must not receive:

                                ```text
                                review project root
                                state/generations path
                                task private path
                                original input file path
                                real user-home path
                                ```

                                ---

## 7.3 Generic execution backend

                                ```python
                                class ExecutionBackend(Protocol):
                                    @property
                                     def confinement_level(self) -> ConfinementLevel:
                                     ...

                                     def prepare(
                                             self,
                                             task: AgentTask,
                                             policy: SubprocessPolicy,
                                             credentials: "CredentialContext",
                                             ) -> "ExecutionSession":
                                     ...
                                     ```

                                     Initial implementation:

                                     ```python
                                     class TemporaryWorkspaceBackend:
                                         confinement_level = ConfinementLevel.TEST_ONLY
                                         ```

                                         This backend is accepted only for fake-worker tests.

                                         ---

## 7.4 Process invocation

                                         Use:

    ```python
subprocess.Popen(
        command_as_list,
        shell=False,
        cwd=execution_root,
        start_new_session=True,
        env=minimal_environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        )
    ```

    A shell-composed command is prohibited.

    A trusted launcher may apply POSIX resource limits before executing the fake worker. This is preferable to complex logic in an unsafe multithreaded `preexec_fn`.

    ---

## 7.5 Environment construction

    Inherited variables should be explicitly allowlisted.

    Reasonable public variables:

    ```text
    PATH
    LANG
    LC_ALL
    SSL_CERT_FILE, when needed
    ```

    Task-specific paths:

    ```text
    HOME=<execution_root>/home
    XDG_CONFIG_HOME=<execution_root>/home/.config
    XDG_CACHE_HOME=<execution_root>/home/.cache
    TMPDIR=<execution_root>/tmp
    ```

    The complete parent environment must not be copied.

    ---

## 7.6 Bounded stream capture

    Two concurrent byte readers should consume stdout and stderr.

    Each reader should:

    ```text
    read a fixed-size chunk
    ↓
    increase bytes_observed
    ↓
    apply exact-secret redaction
    ↓
    retain only configured bounded bytes
    ↓
    update persisted-byte hash
    ↓
    set truncated when additional data arrive
    ```

    To detect a secret split across chunks, the redactor should retain a carry-over window equal to:

    ```text
    maximum secret byte length − 1
    ```

    Persisted output:

    ```text
    attempt/stdout.txt
    attempt/stderr.txt
    ```

    Tests should emit at least 10 MB to each stream and prove:

    ```text
    no deadlock

    bytes_observed ≥ emitted bytes

    bytes_retained ≤ configured bound

    truncated = true

    persisted hash is correct
    ```

    An exact resident-memory threshold is not required in deterministic CI.

    ---

## 7.7 Trusted output-directory identity

    The parent creates `output/` and opens it before launch.

    ```python
output_fd = os.open(
        output_dir,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
trusted_output_stat = os.fstat(output_fd)
    ```

    After process termination:

    ```text
    lstat current output path

    require real directory

    require not symlink

    require st_dev and st_ino unchanged
    ```

    A replaced or symlinked output directory produces:

    ```text
    ENGINE_OUTPUT_POLICY_FAILURE
    ```

    This check precedes missing-proposal classification.

    ---

## 7.8 Race-resistant proposal import

    The proposal is opened relative to the trusted output directory descriptor:

    ```python
    proposal_fd = os.open(
            "proposal.json",
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=output_fd,
            )
    ```

    Then:

    ```text
    fstat descriptor

    require regular file

    require st_nlink == 1

    require inode not shared with protected bundle object

    require size <= max_proposal_bytes

    read at most max_proposal_bytes + 1 bytes

    fstat again

    require stable device/inode/size

    strict UTF-8 decode

    require non-empty content
    ```

    Never read as proposals:

    ```text
    symlink
    FIFO
    socket
    device
    hard link to bundle input
    unstable/replaced file
    ```

    Unsafe file type or hard-link conditions produce:

    ```text
    ENGINE_OUTPUT_POLICY_FAILURE
    ```

    Missing, empty, oversized, non-UTF-8 or malformed regular proposals produce:

    ```text
    ENGINE_FORMAT_FAILURE
    ```

    ---

## 7.9 Output-tree policy

    Allowed under `output/`:

    ```text
    proposal.json
    ```

    Anything else produces:

    ```text
    ENGINE_OUTPUT_POLICY_FAILURE
    ```

    Allowed under writable working areas:

    ```text
    scratch/**
             home/**
             tmp/**
             ```

             subject to quota and file-type rules.

             The input bundle must remain byte-for-byte identical to its private trust anchor.

             ---

## 7.10 Writable-tree monitor

Monitor only:

```text
output/
scratch/
home/
tmp/
```

Use `os.scandir()` and `lstat()` without following links.

Collect:

```text
total regular-file bytes
file count
largest individual file
maximum directory depth
special-file presence
symlink presence
```

On breach:

```text
record ResourceLimitCode
terminate process group
set ENGINE_RESOURCE_LIMIT_FAILURE
```

After a limit is detected, stop attempting a complete unbounded inventory.

Kernel-level limits may also be applied:

```text
RLIMIT_CPU
RLIMIT_FSIZE
RLIMIT_NOFILE
RLIMIT_NPROC
RLIMIT_AS
```

The Python monitor remains the deterministic fallback where kernel behaviour varies.

---

## 7.11 Timeout handling

On timeout:

```text
send SIGTERM to process group
↓
wait terminate_grace_seconds
↓
send SIGKILL to process group
↓
drain bounded diagnostics
```

    The primary result is:

    ```text
    ENGINE_EXECUTION_FAILURE
    ```

    The fake worker must include a mode that spawns a child and then blocks. Both parent and child must be terminated.

    ---

## 7.12 Artifact policy

    Always retain:

    ```text
    TaskAttemptRecord
    SubprocessExecutionResult
    detected failures
    bounded redacted stdout
    bounded redacted stderr
    safe execution inventory
    safe bounded proposal bytes
    ```

    Do not retain automatically:

    ```text
    scratch contents
    temporary home/cache contents
    credential contents
    unauthorized output contents
    symlink targets
    special files
    oversized file contents
    ```

    Default:

    ```text
    forensic quarantine = disabled
    ```

    After safe artifacts are imported, delete the external execution root.

    ---

## 7.13 Fake worker modes

    The deterministic helper must support:

    ```text
    valid
    nonzero
    malformed_json
    schema_invalid
    proposal_invalid

    missing_proposal
    empty_proposal
    non_utf8_proposal
    oversized_proposal

    large_stdout
    large_stderr
    parent_secret_probe
    injected_secret_probe

    timeout
    spawn_child_timeout

    tamper_instructions
    tamper_input
    tamper_input_schema
    tamper_proposal_schema
    tamper_dependency
    tamper_resource
    tamper_bundle_manifest

    extra_output
    proposal_symlink
    proposal_fifo
    proposal_socket
    proposal_hardlink
    output_directory_replacement

    large_scratch_file
    too_many_files
    too_deep_tree
    too_many_processes

    permitted_scratch
    ```

    Combination modes:

    ```text
    nonzero + valid proposal
    nonzero + bundle mutation
    bundle mutation + malformed proposal
    extra output + malformed proposal
    resource limit + nonzero exit
    ```

    ---

## 7.14 Milestone-B2 tests

    Add:

    ```text
    tests/runtime/
    ├── test_subprocess_execution.py
    ├── test_subprocess_failure_precedence.py
    ├── test_subprocess_diagnostics.py
    ├── test_subprocess_timeout.py
    ├── test_subprocess_output_directory.py
    ├── test_subprocess_proposal_import.py
    ├── test_subprocess_output_policy.py
    ├── test_subprocess_resource_limits.py
    ├── test_subprocess_fallback.py
    ├── test_subprocess_secrets.py
    └── test_subprocess_cleanup.py
    ```

    Required assertions include:

    ```text
    valid proposal commits a new generation

    all technical failures preserve canonical state

    primary tampering gives fallback a pristine bundle

    valid REJECT is canonicalized without fallback

    output-directory replacement is output-policy failure

    proposal hard link is rejected

    bundle file larger than writable single-file quota is permitted

    scratch file larger than quota is rejected

    10 MB stdout/stderr remain bounded

    child process is terminated on timeout

    external execution root is removed

    project/private paths do not appear in command or environment
    ```

## Milestone-B2 gate

    ```text
    fake worker conformance suite passes

    all current tests remain green

    Python 3.11–3.13 CI passes

    no real engine exists

    TemporaryWorkspaceBackend remains TEST_ONLY
    ```

    ---

# 8. Milestone B3 — credentials and real confinement

## 8.1 Credential provider

    ```python
    class EngineCredentialProvider(Protocol):
        @property
         def provider_id(self) -> str:
         ...

         def prepare(
                 self,
                 engine_name: str,
                 execution_root: Path,
                 ) -> "CredentialContext":
         ...
         ```

         ```python
         class CredentialContext:
             public_env: dict[str, str]
             secret_env: dict[str, SecretStr]

             ephemeral_files: tuple[Path, ...]
             exact_redaction_values: tuple[bytes, ...]

             provider_id: str
             nonsecret_configuration_fingerprint: Sha256
             ```

             Rules:

             ```text
             secret values never serialized

             secret values excluded from cache signatures

             secret values suppressed in repr/logs

             credential files outside bundle/

             credential file mode 0600

             credential directory mode 0700

             credential material deleted after execution
             ```

             Implement initially:

             ```text
             NullCredentialProvider
             SyntheticCredentialProvider for tests
             ```

             ---

## 8.2 Two mandatory secret tests

### Parent-only secret

             ```text
             secret exists in parent environment
             ↓
             not allowlisted
             ↓
             absent from child
             ↓
             absent from all persisted artifacts
             ```

### Intentionally injected secret

             ```text
             SyntheticCredentialProvider injects secret
             ↓
             fake child can access it
             ↓
             fake child prints it
             ↓
             persisted diagnostics contain redaction
             ↓
             credential files removed
             ↓
             secret absent from manifests and attempt JSON
             ```

             ---

## 8.3 Qualified confinement backend

             A real engine requires:

             ```text
             OS_SANDBOX
             or
             ENGINE_NATIVE_SANDBOX
             ```

             The recommended V1 order is:

             ```text
             1. probe an engine-native restricted-workspace mode;
             2. otherwise implement a Linux OS sandbox backend;
             3. fail closed when neither is available.
             ```

             A qualification record should contain:

             ```text
             backend name
             backend version
             confinement level
             filesystem policy
             network policy
             conformance-suite version
             tests passed
             ```

             Changing `cwd` alone is insufficient.

## Milestone-B3 gate

             ```text
             synthetic credential tests pass

             secret values are never serialized

             one non-TEST_ONLY confinement backend passes conformance

             real engine remains disabled until qualification succeeds
             ```

             ---

# 9. Milestone C — first Codex integration

## 9.1 Scope

             Implement one adapter:

             ```text
             CodexEngine
             ```

             Enable one semantic task:

             ```text
             ASSESS_CLAIM
             ```

             No document-scale task should be enabled yet.

             ---

## 9.2 Adapter design

             ```text
             src/vibereview/engines/
             ├── base.py
             └── codex.py
             ```

             The adapter performs only:

             ```text
             executable discovery
             version probing
             command construction
             credential preparation
             confinement selection
             subprocess execution
             proposal-file location
             AgentResult conversion
             ```

             It contains no scientific decision logic.

             Do not hard-code unverified command-line flags. The adapter should probe the installed executable and fail with a clear preflight error when the required mode is unavailable.

             ---

## 9.3 Codex configuration

             ```yaml
             engines:
codex:
executable: codex
timeout_seconds: 600
confinement_required: true
credential_provider: codex_local
```

Engine authentication remains external to the task bundle.

---

## 9.4 First production prompt

`ASSESS_CLAIM` should instruct Codex to:

    ```text
    read the candidate claim

    read every supplied ClaimPaperEvidence record

    reason at publication level

    distinguish support, contradiction and qualification

    choose RETAIN, WEAKEN, NARROW, REFORMULATE or REJECT

    distinguish insufficient evidence from contradiction

    preserve uncertainty

    return only proposal JSON

    allocate no canonical IDs

    treat REJECT as a valid result
    ```

    ---

## 9.5 Live qualification fixtures

    | Case | Evidence pattern                    | Expected decision                |
    | ---- | ----------------------------------- | -------------------------------- |
    | 1    | Consistent direct evidence          | `RETAIN`                         |
    | 2    | Support under restricted conditions | `NARROW`                         |
    | 3    | Inadequate evidence                 | `REJECT / insufficient_evidence` |
    | 4    | Strong contrary evidence            | `REJECT / contradicted`          |

    Tests should assert structure and runtime semantics, not exact prose.

    Required assertions:

    ```text
    proposal DTO valid

    ClaimAssessment canonicalized

    REJECT does not create ClaimPacket

    REJECT does not invoke fallback

    bundle unchanged

    credentials removed

    attempt provenance complete

    canonical project not writable
    ```

    Mark:

    ```python
    @pytest.mark.external_engine
    ```

    These tests remain outside ordinary CI.

## Milestone-C gate

    ```text
    four fixtures pass

    no fallback occurs for scientific rejection

    Codex sees only sanitized input

    no canonical-state access occurs

    all deterministic CI remains green
    ```

    ---

# 10. Milestone D — user-facing CLI and project layout

## 10.1 CLI

    Add:

    ```text
    src/vibereview/cli.py
    src/vibereview/__main__.py
    ```

    Use a console entry point:

    ```toml
    [project.scripts]
    vibereview = "vibereview.cli:main"
    ```

    Commands:

    ```bash
    vibereview init reviews/<slug>
    vibereview status reviews/<slug>
    vibereview validate reviews/<slug>
    vibereview run reviews/<slug>
    ```

    Later:

    ```bash
    vibereview export-redteam reviews/<slug>
    ```

    ---

## 10.2 Project structure

    ```text
    reviews/<slug>/
    ├── project.yaml
    ├── input/
    │   ├── deep_research/
    │   └── papers/
    ├── state/
    │   ├── generations/
    │   ├── resources/
    │   └── CURRENT
    ├── work/
    │   ├── tasks/
    │   ├── cache/
    │   └── logs/
    └── output/
    ```

    Minimal configuration:

    ```yaml
    topic: >
    Review topic

    review_type: critical_narrative
    epistemic_scope: supplied_corpus

    inputs:
deep_research: input/deep_research
papers: input/papers

engines:
default:
primary: codex

limits:
max_claim_revision_attempts: 2
max_proposition_repair_attempts: 2
max_render_repair_attempts: 2
```

The user should normally modify only:

```text
project.yaml
input/deep_research/
input/papers/
```

---

# 11. Milestone E — immutable content-addressed resources

## 11.1 Resource store

```text
state/resources/sha256/
├── ab/
│   └── abcdef...
└── cd/
└── cdef...
```

The file content determines the resource key.

## 11.2 Import algorithm

```text
source file
↓
temporary blob
↓
copy while hashing
↓
flush and fsync
↓
derive digest path
↓
verify existing digest path, when present
↓
atomic move
↓
make immutable/read-only
```

Never edit a blob in place.

Mutable metadata and project associations must reside in numbered repository generations, not mutable sidecars beside immutable blobs.

## 11.3 Runtime resource registry

Add runtime-generation records such as:

```python
class ProjectResourceRecord(RuntimeModel):
    resource_hash: Sha256
    media_type: str
    resource_kind: str

    logical_name: str
    imported_at: str

    original_basename: str
    ```

    Do not store the original absolute source path in the engine-visible or scientific state.

## 11.4 Initial supported inputs

    First support:

    ```text
    Deep Research Markdown
    paper Markdown
    ```

    PDF parsing remains deferred until the Markdown route passes end to end.

## Milestone-E tests

    ```text
    same bytes reuse same blob

    different bytes produce different blobs

    existing digest collision is verified

    input mutation after import does not change blob

    task snapshots use CAS blob, not mutable input file

    orphan blobs do not corrupt canonical generations
    ```

    ---

# 12. Milestone F — Deep Research ingestion

## 12.1 Input

    ```text
    input/deep_research/
    ├── 01_scope.md
    ├── 02_subtopic_a.md
    ├── 03_subtopic_b.md
    ├── 04_methods.md
    └── 05_conflicts_and_gaps.md
    ```

    Each file is imported into the CAS before semantic parsing.

    ---

## 12.2 Full discovery proposal

    Expand the runtime proposal:

    ```python
    class DiscoveryProposalBundle(BaseModel):
        themes: list[ThemeProposal]
        candidate_claims: list[CandidateClaimProposal]

        terminology: list[TerminologyProposal]
        paper_candidates: list[PaperCandidateProposal]
        controversies: list[ControversyProposal]
        gaps: list[GapProposal]
        ```

        Each discovery item should include:

        ```text
        source_resource_id
        source_document_name
        optional section
        optional start/end offsets
        ```

        Only themes and candidate claims require immediate promotion into the frozen scientific graph.

        The complete accepted discovery proposal must remain available as an immutable/versioned task artifact.

        ---

## 12.3 Promotion rules

        Python:

        ```text
        allocates T IDs
        allocates C IDs
        resolves proposal-local theme references
        validates hierarchy
        commits ThemeRecord and CandidateClaim
        retains remaining discovery artifacts
        ```

        Deep Research-derived `CandidateClaim.origin_refs` should refer to immutable resource identities, not mutable filenames.

## Milestone-F gate

        ```text
        all DR files are immutable resources

        complete discovery outputs retained

        themes/claims promoted with stable IDs

        no discovery statement becomes EvidenceRecord

        source provenance resolves
        ```

        ---

# 13. Milestone G — paper ingestion, concept sketches and corpus challenger

## 13.1 Paper Markdown ingestion

        Input:

        ```text
        input/papers/*.md
                      ```

                      Optional metadata sidecar:

                      ```text
                      paper_name.md
                      paper_name.metadata.json
                      ```

                      Paper identity order:

                      ```text
                      normalized DOI
                      ↓
                      source content hash
                      ↓
                      bibliographic fingerprint
                      ```

                      The existing stable Paper-ID and conflict rules remain authoritative.

                      ---

## 13.2 Canonical paper resource

Every Paper must resolve to an immutable Markdown blob.

```text
Paper.raw_md_path
→ content-addressed resource

Paper.raw_md_hash
→ exact canonical Markdown bytes
```

The source hash and normalized Markdown hash remain distinct when PDFs are added later.

---

## 13.3 Paper concept sketch

For every paper, run a bounded task producing:

```python
class PaperConceptSketchProposal(BaseModel):
paper_id: str

studied_systems: list[str]
methods: list[str]
variables: list[str]
reported_relationships: list[str]
mechanisms: list[str]
limitations: list[str]
terminology: list[str]
```

Concept sketches are discovery artifacts, not evidence.

They are cached by:

```text
paper raw_md_hash
TaskSpec version
engine identity
prompt hash
validator fingerprint
```

---

## 13.4 Corpus challenger

        Input:

        ```text
        DR-derived themes and candidate claims
        +
        paper concept sketches
        ```

        Task:

        ```text
        identify substantial concepts, findings, mechanisms,
        methods or controversies present in the supplied corpus
        but absent from the DR-derived map
        ```

        Output:

        ```text
        additional ThemeProposal
        additional CandidateClaimProposal
        missing terminology
        challenger notes
        ```

        Python merges:

        ```text
        DR-derived discovery
        +
        corpus-derived discovery
        ```

        Exact duplicates may be merged deterministically.

        Potential semantic duplicates should be flagged rather than silently merged unless a specific merge task is introduced.

        ---

## 13.5 Mandatory challenger fixture

        At least one five-paper fixture must contain:

        ```text
        a relevant concept present in the papers
        but absent from every Deep Research document
        ```

        The challenger must recover it before retrieval-query generation.

## Milestone-G gate

        ```text
        all papers imported

        all papers have concept sketches

        corpus challenger executed

        omitted fixture concept recovered

        candidate-claim set frozen only after challenger
        ```

        ---

# 14. Milestone H — Graphify and retrieval state

## 14.1 Separation from LLM engines

        Graphify is a retrieval backend:

        ```python
        class RetrievalBackend(Protocol):
            def index(
                    self,
                    paper: Paper,
                    raw_md_path: Path,
                    ) -> None:
            ...

            def search(
                    self,
                    query: RetrievalQuery,
                    ) -> list["RetrievedSpanProposal"]:
            ...
            ```

            Graphify must not classify stance.

            ---

## 14.2 Retrieval-query generation

            For each CandidateClaim, generate at least:

            ```text
            SUP
            CON
            BND
            ALT
            ```

            Python allocates canonical query IDs and ordinals.

            The engine proposes:

            ```text
            intent
            query text
            ```

            Python constructs:

            ```text
            query ID
            claim ID
            candidate-claim hash
            ```

            ---

## 14.3 ID-less span proposal

            ```python
            class RetrievedSpanProposal(BaseModel):
                paper_ref: str
                query_ref: str

                start_offset: int | None
                end_offset: int | None

                source_text: str
                source_span_hash: Sha256

                page: int | None
                section: str | None

                retrieval_score: float | None
                ```

                When offsets are missing, the adapter may attempt exact substring location in canonical Markdown.

                Rules:

                ```text
                one unique exact occurrence
                → offsets may be derived

                multiple occurrences
                → ambiguous proposal

                no occurrence
                → invalid proposal
                ```

                ---

## 14.4 Retrieval-attempt ledger

                Every raw backend result is preserved in a runtime ledger.

                ```python
                class RetrievalAttemptRecord(RuntimeModel):
                    backend: str
                    backend_version: str

                    query_id: str
                    paper_id: str

                    raw_proposal: dict
                    validation_status: str
                    errors: list[str]

                    canonical_span_id: str | None
                    ```

                    Invalid Graphify proposals:

                    ```text
                    remain in runtime ledger
                    receive no R ID
                    never enter scientific evidence
                    ```

                    ---

## 14.5 Canonical span validation

                    Before allocating `Rxxxx`:

                    ```text
                    paper exists
                    query exists
                    query belongs to claim
                    offsets ordered
                    raw_md[start:end] == source_text
                    SHA256(source_text) == source_span_hash
                    ```

                    Only then:

                    ```text
                    Python allocates R ID
                    ```

                    ---

## 14.6 RetrievalDisposition stage

                    Every canonical RetrievedSpan must receive exactly one:

                    ```text
                    assessed
                    duplicate
                    redundant
                    excluded_by_budget
                    invalid_locator
                    ```

                    Normal flow:

                    ```text
                    valid canonical spans
                    ↓
                    exact deduplication
                    ↓
                    near-duplicate analysis
                    ↓
                    intent/paper diversity preservation
                    ↓
                    budget selection
                    ↓
                    one RetrievalDisposition per span
                    ```

                    Only:

                    ```text
                    status = assessed
                    ```

                    may produce an EvidenceRecord.

                    Raw invalid backend proposals are not canonicalized merely to assign `invalid_locator`.

## Milestone-H tests

                    ```text
                    exact locator accepted

                    wrong source text rejected

                    wrong hash rejected

                    ambiguous source text rejected

                    invalid proposal retained in ledger

                    duplicate spans stay within one paper

                    cross-paper deduplication prohibited

                    every canonical span receives one disposition

                    only assessed spans advance
                    ```

                    ---

# 15. Milestone I — evidence and claim pipeline

                    Implement one task at a time.

## I1. Evidence assessment

                    Input:

                    ```text
                    CandidateClaim
                    RetrievedSpan
                    Paper metadata
                    bounded surrounding context
                    ```

                    Output:

                    ```text
                    EvidenceRecordProposal
                    ```

                    Required relation:

                    ```text
                    supports
                    contradicts
                    qualifies
                    contextual
                    unclear
                    ```

                    Quality may remain:

                    ```text
                    unknown
                    not_assessable
                    ```

                    when methods context is insufficient.

                    Python allocates `Exxxx`.

                    One assessed span must yield exactly one relevant EvidenceRecord.

                    ---

## I2. Publication-level aggregation

                    Input:

                    ```text
                    one claim
                    one paper
                    all EvidenceRecords for that pair
                    ```

                    Output:

                    ```text
                    ClaimPaperEvidenceProposal
                    ```

                    Python validates:

                    ```text
                    every evidence ID present exactly once
                    component relation matches EvidenceRecord
                    aggregate relation matches components
                    paper and claim IDs match
                    mixed evidence preserved
                    ```

                    ---

## I3. Candidate claim assessment

                    Input:

                    ```text
                    CandidateClaim
                    all ClaimPaperEvidence for the claim
                    ```

                    Output:

                    ```text
                    ClaimAssessmentProposal
                    ```

                    Decision:

                    ```text
                    RETAIN
                    WEAKEN
                    NARROW
                    REFORMULATE
                    REJECT
                    ```

                    Rejection basis:

                    ```text
                    insufficient_evidence
                    contradicted
                    out_of_scope
                    unresolvable
                    ```

                    A valid REJECT is stored and does not trigger fallback.

                    ---

## I4. Claim revision

                    Input:

                    ```text
                    candidate claim
                    assessment
                    paper-level evidence summaries
                    ```

                    Output:

                    ```python
                    class RevisedClaimProposal(BaseModel):
                        claim_ref: str
                        revised_claim: str
                        revision_summary: str
                        ```

                        The revised text remains a runtime proposal until final validation.

                        ---

## I5. Final claim validation

                        Input:

                        ```text
                        revised claim
                        same complete ClaimPaperEvidence set
                        ```

                        Output:

                        ```text
                        FinalClaimValidationProposal
                        ```

                        Checks:

                        ```text
                        scope
                        certainty
                        causal language
                        numerical claims
                        paper relations to final wording
                        ```

                        Outcomes:

                        ```text
                        VALID
                        REVISE_AGAIN
                        REJECT
                        UNCLEAR
                        ```

                        Maximum revision attempts:

                        ```text
                        2
                        ```

                        ---

## I6. ClaimPacket creation

                        Only Python creates ClaimPacket.

                        For `VALID`:

                        ```text
                        CandidateClaim
                        +
                        ClaimAssessment
                        +
                        FinalClaimValidation
                        +
                        exact CPE set
                        ↓
                        ClaimPacket
                        ```

                        For other statuses:

                        ```text
                        validation stored
                        no ClaimPacket
                        ```

## Milestone-I gate

                        ```text
                        supportive claim approved

                        overbroad claim narrowed

                        unsupported claim rejected

                        contradicted claim rejected

                        mixed paper evidence preserved

                        no span-count overweighting

                        all claim paths pass repository validation
                        ```

                        ---

# 16. Milestone J — propositions, prose and manuscript

## 16.1 Manuscript plan

                        Create a runtime-only deterministic structure:

                        ```python
                        class ManuscriptPlan(BaseModel):
                            title: str | None
                            components: list["ManuscriptComponent"]
                            ```

                            ```python
                            class ManuscriptComponent(BaseModel):
                                component_id: str
                                component_type: str
                                title: str
                                section_ids: list[str]
                                ```

                                ```python
                                class SectionPlan(BaseModel):
                                    section_id: str
                                    title: str
                                    claim_ids: list[str]
                                    paragraph_groups: list[list[str]]
                                    ```

                                    For V1, the initial plan may be derived from the theme hierarchy and optionally edited by the human.

                                    ---

## 16.2 Proposition generation

                                    For each paragraph group:

                                    ```text
                                    approved ClaimPackets
                                    +
                                    authorized paper evidence
                                    +
                                    optional CorpusFacts
                                    +
                                    optional ReviewProcessFacts
                                    ```

                                    produce typed:

                                    ```text
                                    PropositionRecord
                                    ```

                                    Every scientific proposition must include:

                                    ```text
                                    claim IDs
                                    citation bindings
                                    ```

                                    ---

## 16.3 Proposition audit

                                    Each proposition receives:

                                    ```text
                                    class correctness
                                    provenance entailment
                                    scope check
                                    certainty check
                                    citation appropriateness
                                    ```

                                    Only:

                                    ```text
                                    CORRECT + ENTAILED
                                    ```

                                    passes automatically.

                                    Repair attempts:

                                    ```text
                                    maximum 2
                                    ```

                                    Unresolved `UNCLEAR` requires human review and blocks integrity pass.

                                    ---

## 16.4 Prose rendering

                                    Passing propositions are grouped into paragraphs.

                                    The renderer may:

                                    ```text
                                    merge compatible propositions
                                    add grammatical transitions
                                    reduce repetition
                                    improve flow
                                    ```

                                    It may not:

                                    ```text
                                    add a mechanism
                                    broaden scope
                                    strengthen certainty
                                    invent a number
                                    introduce a new citation
                                    ```

                                    Output:

                                    ```text
                                    RenderedSentence[]
                                    ```

                                    ---

## 16.5 Final rendered-sentence audit

                                    Each sentence is checked against its source propositions.

                                    Allowed verdicts:

                                    ```text
                                    ENTAILED
                                    PARTIALLY_SUPPORTED
                                    OVERSTATED
                                    UNSUPPORTED
                                    UNCLEAR
                                    ```

                                    Only `ENTAILED` sentences qualify for final deterministic rendering.

                                    Maximum render repairs:

                                    ```text
                                    2
                                    ```

                                    ---

## 16.6 Deterministic citation rendering

                                    Internal citations:

                                    ```text
                                    [P0012]
                                    [P0038]
                                    ```

                                    Python maps them to the bibliography.

                                    For every scientific claim represented in a rendered sentence:

                                    ```text
                                    at least one authorized visible citation token
                                    ```

                                    must remain after proposition merging.

                                    ---

## 16.7 No generative operation after final audit

                                    After sentence audits pass, only these operations are permitted:

                                    ```text
                                    citation formatting
                                    reference ordering
                                    heading numbering
                                    sentence ordering
                                    paragraph assembly
                                    section assembly
                                    Markdown generation
                                    DOCX/LaTeX conversion
                                    whitespace normalization
                                    ```

                                    ---

## 16.8 Output files

                                    ```text
                                    output/
                                    ├── manuscript_internal.md
                                    ├── manuscript.md
                                    ├── references.md
                                    │
                                    ├── claims/
                                    │   ├── C0001.md
                                    │   └── ...
                                    │
                                    ├── evidence/
                                    │   ├── E0001.md
                                    │   └── ...
                                    │
                                    ├── sections/
                                    │   ├── S0001.md
                                    │   └── ...
                                    │
                                    └── audit/
                                    ├── source_integrity.json
                                    ├── scientific_provenance.json
                                    ├── citation_integrity.json
                                    └── summary.md
                                    ```

                                    ---

# 17. Milestone K — five-paper vertical slice

## 17.1 Inputs

                                    ```text
                                    one topic
                                    two or three Deep Research Markdown files
                                    five paper Markdown files
                                    ```

                                    Use two passes.

### Pass A — synthetic controlled fixture

                                    The papers are constructed to produce known conditions.

### Pass B — five real papers

                                    This tests realistic terminology and extraction variance.

                                    ---

## 17.2 Fixture design

                                    The five-paper fixture should include:

                                    ```text
                                    Claim A:
                                    supported by three papers

                                    Claim B:
                                    supported only under restricted conditions
                                    → NARROW

                                    Claim C:
                                    insufficient evidence
                                    → REJECT / insufficient_evidence

                                    Claim D:
                                    strong contradiction
                                    → REJECT / contradicted

                                    Claim E:
                                    one paper contains mixed findings

                                    Hidden theme:
                                    present in papers but absent from every DR report
                                    → corpus challenger must recover it
                                    ```

                                    ---

## 17.3 Required path

                                    ```text
                                    topic
                                    ↓
                                    project init
                                    ↓
                                    CAS import
                                    ↓
                                    DR parsing
                                    ↓
                                    paper registration
                                    ↓
                                    paper concept sketches
                                    ↓
                                    corpus challenger
                                    ↓
                                    candidate claim merge
                                    ↓
                                    retrieval queries
                                    ↓
                                    Graphify
                                    ↓
                                    retrieval ledger
                                    ↓
                                    RetrievedSpans
                                    ↓
                                    RetrievalDispositions
                                    ↓
                                    EvidenceRecords
                                    ↓
                                    ClaimPaperEvidence
                                    ↓
                                    ClaimAssessments
                                    ↓
                                    claim revision
                                    ↓
                                    FinalClaimValidation
                                    ↓
                                    ClaimPackets
                                    ↓
                                    ManuscriptPlan
                                    ↓
                                    PropositionRecords
                                    ↓
                                    proposition audits
                                    ↓
                                    RenderedSentences
                                    ↓
                                    sentence audits
                                    ↓
                                    deterministic section assembly
                                    ```

                                    ---

## 17.4 Integrity attacks

                                    The vertical slice must deliberately test:

                                    ```text
                                    support-only retrieval bias

                                    12 spans from one paper versus three contradictory papers

                                    invalid Graphify locator

                                    Graphify source-text mismatch

                                    cross-paper duplicate collapse

                                    EvidenceRecord from non-assessed span

                                    CPE relation mismatch

                                    REJECT promoted to ClaimPacket

                                    unlicensed CitationBinding

                                    unsupported proposition clause

                                    abstract certainty strengthening

                                    renderer changes association to causation

                                    citation lost during proposition merging

                                    resource changes while task is running

                                    engine tampers with input bundle

                                    external process exceeds writable quota

                                    stale cached proposal under a changed validator
                                    ```

                                    ---

## 17.5 Five-paper acceptance gate

                                    ```text
                                    every scientific sentence maps to approved ClaimPackets

                                    every ClaimPacket maps to complete paper-level evidence

                                    every EvidenceRecord maps to exact Markdown source text

                                    every RetrievedSpan has one RetrievalDisposition

                                    contradictory and qualifying evidence remains visible

                                    rejected claims remain canonical

                                    no negative scientific result triggers fallback

                                    corpus challenger recovers the hidden theme

                                    invalid Graphify proposals remain outside canonical evidence

                                    no engine writes canonical state

                                    all deterministic tests and CI pass

                                    one coherent critical-review section is produced
                                    ```

                                    ---

# 18. Milestone L — scaling and additional engines

## 18.1 Scale sequence

                                    ```text
                                    Pilot 1:
                                    5 papers
                                    5–10 claims

                                    Pilot 2:
                                    20–30 papers
                                    10–20 claims
                                    3–5 themes

                                    Pilot 3:
                                    approximately 50 core papers
                                    30–60 claims
                                    complete manuscript skeleton

                                    Pilot 4:
                                    larger corpus only after retrieval and context budgets are measured
                                    ```

                                    Do not begin with hundreds of papers.

                                    ---

## 18.2 Additional engine order

                                    ```text
                                    1. CodexEngine
                                    2. KimiEngine
                                    3. AgyEngine
                                    4. OpenCodeEngine, optionally
                                    ```

                                    Each adapter must pass the same:

                                    ```text
                                    subprocess conformance
                                    credential handling
                                    sandbox qualification
                                    proposal-schema validation
                                    fallback semantics
                                    canonical-state isolation
                                    ```

                                    ---

## 18.3 Engine routing

                                    ```yaml
                                    engines:
                                    default:
primary: codex
fallback:
- kimi

tasks:
parse_deep_research:
primary: kimi
fallback:
- codex

assess_evidence:
primary: codex
fallback:
- kimi

audit_proposition:
primary: kimi
fallback:
- codex
```

Fallback is invoked only for technical failures.

A valid scientific disagreement does not trigger model substitution.

---

## 18.4 No majority voting in V1

Do not implement:

```text
Codex says supports
Kimi says contradicts
Agy says supports
→ majority says supports
```

A later optional mode may use:

```text
primary assessment
+
independent challenger
+
human adjudication
```

for selected consequential claims.

---

## 18.5 Engine comparison experiment

After the Codex vertical slice:

```text
Run A:
all semantic tasks = Codex

Run B:
evidence assessment = Kimi

Run C:
semantic audit = Kimi
```

Compare:

```text
evidence-relation distributions
claim decisions
rejection frequency
scope narrowing
audit disagreement
runtime
cost
failure rate
```

Each run must remain independently reproducible.

---

# 19. PDF parsing milestone

PDF parsing should be added only after the raw-Markdown route passes.

```python
class PDFParserAdapter(Protocol):
    def parse(
            self,
            pdf_path: Path,
            output_path: Path,
            ) -> "ParsedPaper":
    ...
    ```

    The parser must produce canonical Markdown plus provenance metadata.

    Parser identity must enter:

    ```text
    run manifest
    raw_md provenance
    cache signature
    dependency invalidation
    ```

    The user may always bypass PDF parsing by supplying Markdown directly.

    ---

# 20. Red-team Deep Research loop

    Near manuscript completion:

    ```bash
    vibereview export-redteam reviews/<slug>
    ```

    Output:

    ```text
    output/redteam_context.md
    ```

    Contents:

    ```text
    scope
    major approved claims
    known contradictions
    uncertain claims
    current gaps
    representative papers
    ```

    The human runs external Deep Research and adds:

    ```text
    input/deep_research/DR04_redteam.md
    ```

    New paper candidates remain discovery objects until the relevant PDFs or Markdown files are supplied.

    ---

# 21. Target repository structure

    ```text
    VibeReview/
    ├── .github/
    │   └── workflows/
    │       └── tests.yml
    │
    ├── pyproject.toml
    ├── README.md
    ├── AGENTS.md
    ├── goal.md
    │
    ├── src/
    │   └── vibereview/
    │       ├── __init__.py
    │       ├── __main__.py
    │       ├── cli.py
    │       ├── models.py
    │       ├── enums.py
    │       ├── ids.py
    │       ├── validators.py
    │       ├── errors.py
    │       │
    │       ├── runtime/
    │       │   ├── kernel.py
    │       │   ├── records.py
    │       │   ├── dto.py
    │       │   ├── specs.py
    │       │   ├── tasks.py
    │       │   ├── repository.py
    │       │   ├── registry.py
    │       │   ├── cache.py
    │       │   ├── locking.py
    │       │   ├── hashing.py
    │       │   ├── subprocess.py
    │       │   ├── execution.py
    │       │   ├── diagnostics.py
    │       │   ├── output_policy.py
    │       │   ├── resource_limits.py
    │       │   ├── execution_inventory.py
    │       │   ├── confinement.py
    │       │   └── credentials.py
    │       │
    │       ├── engines/
    │       │   ├── base.py
    │       │   ├── mock.py
    │       │   ├── codex.py
    │       │   ├── kimi.py
    │       │   ├── agy.py
    │       │   └── opencode.py
    │       │
    │       ├── resources/
    │       │   ├── store.py
    │       │   ├── importers.py
    │       │   └── registry.py
    │       │
    │       ├── retrieval/
    │       │   ├── base.py
    │       │   ├── mock.py
    │       │   ├── graphify.py
    │       │   └── ledger.py
    │       │
    │       ├── rendering/
    │       │   ├── manuscript.py
    │       │   ├── citations.py
    │       │   └── bibliography.py
    │       │
    │       └── prompts/
    │           ├── parse_deep_research.md
    │           ├── corpus_challenger.md
    │           ├── generate_candidate_claims.md
    │           ├── generate_retrieval_queries.md
    │           ├── assess_evidence.md
    │           ├── aggregate_paper_evidence.md
    │           ├── assess_claim.md
    │           ├── revise_claim.md
    │           ├── validate_final_claim.md
    │           ├── generate_propositions.md
    │           ├── audit_proposition.md
    │           ├── render_prose.md
    │           └── audit_rendered_sentence.md
    │
    ├── tests/
    │   ├── helpers/
    │   │   └── fake_agent.py
    │   ├── runtime/
    │   ├── retrieval/
    │   ├── integration/
    │   └── fixtures/
    │
    └── reviews/
    ```

    ---

# 22. Implementation and commit discipline

    Each Codex assignment should implement one bounded milestone.

    Recommended branches or commits:

    ```text
    r4a
    Milestone B1 contracts

    r4b
    Milestone B2 subprocess runner

    r4c
    Milestone B2 conformance tests

    r5a
    Milestone B3 credentials/confinement

    r5b
    CodexEngine qualification

    r6
    CLI + CAS

    r7
    DR and paper ingestion

    r8
    corpus challenger

    r9
    Graphify

    r10
    evidence/claim loop

    r11
    writing/audit loop

    r12
    five-paper vertical slice
    ```

    Every milestone report should include:

    ```text
    files changed
    contracts added or modified
    test inventory
    full local pytest result
    GitHub Actions result
    remaining limitations
    confirmation that later milestones were not started
    ```

    ---

# 23. Deferred features

    The following should remain outside V1 until the five-paper and 20–30-paper gates pass:

    ```text
    database server
    web UI
    distributed workers
    formal PRISMA workflow
    systematic-review claims
    automatic literature-database searching
    automatic paper downloading
    journal-submission automation
    bibliometric dashboard
    majority-vote agent councils
    formal GRADE assessment
    fully automatic figure generation
    ```

    Elsevier API integration may later assist bibliographic resolution, but it should not precede the core evidence pipeline.

    Human-created figures may be registered as manuscript assets without being generated by the pipeline.

    ---

# 24. Definition of V1 complete

    V1 is complete when:

    1. A human can create a review project with one command.
    2. Topic, DR Markdown and paper Markdown/PDF are accepted.
    3. All source files are imported into immutable resources.
    4. Deep Research outputs are retained as discovery objects.
    5. A corpus challenger can add omitted themes and claims.
    6. Retrieval queries cover support, contradiction, boundaries and alternatives.
    7. Graphify spans are verified against canonical Markdown.
    8. Every canonical span has one RetrievalDisposition.
    9. Only assessed spans produce EvidenceRecords.
    10. Evidence is aggregated at publication level.
    11. Claims may be retained, narrowed, reformulated or rejected.
    12. Revised claims undergo final validation.
    13. Only Python creates approved ClaimPackets.
    14. Scientific propositions carry claim and citation provenance.
    15. Rendered prose receives a final semantic audit.
    16. No LLM runs after the final sentence audit.
    17. Final citation and manuscript assembly are deterministic.
    18. Negative and uncertain results remain canonical.
    19. At least one complete five-paper review section passes.
    20. A 20–30-paper pilot passes without contract changes.
    21. Codex is replaceable by another qualified engine without changing scientific models.
    22. Normal use requires only `vibereview run reviews/<project>`.

    ---

# 25. Immediate Codex assignment

    The full plan should be supplied as reference, but Codex should implement only the next bounded milestone.

    > **Implement VibeReview Milestone B1 and B2: deterministic fake subprocess execution.**
    >
    > Begin from current repository head `ca7e1a4760efc0ceffc6b69813fc3bf44eb19d1e`.
    >
    > Milestone A and the frozen scientific architecture are accepted. Do not redesign them.
    >
    > Do not implement CodexEngine, KimiEngine, AgyEngine, OpenCodeEngine, Graphify, PDF parsing, Deep Research semantic processing, project ingestion, content-addressed resources or manuscript generation.
    >
    > Add:
    >
    > * `ENGINE_OUTPUT_POLICY_FAILURE`;
    > * `ENGINE_RESOURCE_LIMIT_FAILURE`;
    > * `AttemptFailureStage.RESOURCE_LIMIT`;
    > * machine-readable `ResourceLimitCode`;
    > * structured secondary failure records;
    > * deterministic primary-outcome precedence.
    >
    > The primary-outcome order must be:
    >
    > 1. explicit resource-limit breach;
    > 2. process launch failure, timeout or non-zero exit;
    > 3. immutable bundle mutation;
    > 4. output-directory identity failure, unauthorized output or unsafe output type;
    > 5. missing, empty, oversized, non-UTF-8 or malformed regular proposal;
    > 6. Pydantic schema failure;
    > 7. task-specific proposal-validation failure;
    > 8. valid scientific result.
    >
    > Writable-growth quotas apply only to:
    >
    > * `output/`;
    > * `scratch/`;
    > * `home/`;
    > * `tmp/`.
    >
    > They do not apply to:
    >
    > * immutable `bundle/`;
    > * trusted `launcher/`;
    > * runtime-controlled `credentials/`.
    >
    > Implement a generic subprocess runner using:
    >
    > * argument-vector execution;
    > * `shell=False`;
    > * a new process session;
    > * an external temporary execution root;
    > * isolated `HOME`, XDG and TMP paths;
    > * a minimal environment allowlist;
    > * bounded timeout and termination grace period.
    >
    > Create and retain a trusted descriptor for the parent-created `output/` directory. Record its device and inode identity. After execution, verify that the path still resolves to the same real directory. Replacement or symlinking is `ENGINE_OUTPUT_POLICY_FAILURE`.
    >
    > Import `output/proposal.json` relative to the trusted output descriptor using no-follow semantics. Require:
    >
    > * regular file;
    > * stable identity;
    > * `st_nlink == 1`;
    > * no inode shared with protected bundle input;
    > * bounded size;
    > * strict UTF-8;
    > * non-empty content.
    >
    > Reject proposal symlinks, FIFOs, sockets, devices, hard links and replaced output directories as output-policy failures.
    >
    > Capture stdout and stderr concurrently and incrementally. Do not use unbounded buffering followed by truncation.
    >
    > Persist only bounded redacted bytes and record:
    >
    > * bytes observed;
    > * bytes retained;
    > * truncation flag;
    > * redaction count;
    > * SHA-256 of the exact persisted redacted bytes.
    >
    > Do not persist a hash over a complete unredacted stream.
    >
    > Add writable-tree monitoring for:
    >
    > * total bytes;
    > * file count;
    > * individual file size;
    > * directory depth;
    > * process count;
    > * open files;
    > * CPU time;
    > * optional address space.
    >
    > Terminate the process group on a limit breach and return `ENGINE_RESOURCE_LIMIT_FAILURE`.
    >
    > Persist only approved bounded artifacts and a safe file inventory. Do not persist arbitrary scratch, home, cache, credential, symlink, special-file or unauthorized-output content. Delete the temporary execution root after artifact import.
    >
    > Extend TaskSpec preflight so every proposal model has an effective JSON-object schema root. Reject root-list proposal models.
    >
    > Add:
    >
    > * `NullCredentialProvider`;
    > * `SyntheticCredentialProvider` for tests;
    > * secret-suppressing `CredentialContext`.
    >
    > Add a deterministic fake worker supporting:
    >
    > * valid proposal;
    > * non-zero exit;
    > * malformed, empty, missing, non-UTF-8 and oversized proposal;
    > * schema-invalid and task-invalid proposal;
    > * large stdout and stderr;
    > * parent-only and intentionally injected secret probes;
    > * timeout and child-process timeout;
    > * mutation of every immutable bundle-file class;
    > * unauthorized output;
    > * proposal symlink, FIFO, socket and hard link;
    > * output-directory replacement;
    > * writable-tree byte, file-count, file-size, depth and process-limit violations;
    > * permitted scratch output.
    >
    > Add combination tests proving deterministic failure precedence.
    >
    > Add fallback tests proving that a fallback attempt receives a pristine bundle after primary-engine tampering.
    >
    > Add a valid `ClaimAssessment=REJECT` subprocess test proving that the assessment is canonicalized and fallback is not invoked.
    >
    > Keep `TemporaryWorkspaceBackend` classified as `TEST_ONLY`. Do not enable a real engine.
    >
    > Run the complete local deterministic pytest suite and preserve the Python 3.11–3.13 GitHub Actions matrix.
    >
    > Stop after the fake subprocess conformance suite passes.
    >
    > Report:
    >
    > * files changed;
    > * runtime models and enums;
    > * failure-precedence implementation;
    > * execution-root layout;
    > * diagnostic-capture implementation;
    > * output-directory identity mechanism;
    > * descriptor-safe proposal import;
    > * writable-quota implementation;
    > * credential test implementation;
    > * fake-worker modes;
    > * tests added;
    > * complete pytest result;
    > * GitHub Actions result;
    > * confirmation that no real LLM engine was implemented.


