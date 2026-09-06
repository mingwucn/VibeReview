# VibeReviewPaper Agent Guide

## Current boundary

The V1.5.1b scientific contract and runtime handoff are frozen. The implemented
boundary ends after immutable generations, task/proposal contracts, fallback
and freshness policy, and MockEngine end-to-end tests. Milestone A added the
self-contained task-resource boundary: TaskSpec-owned dependencies and
resources, private invocation DTOs versus sanitized engine inputs,
copy-while-hashing resource snapshots, private bundle-integrity trust anchors
under `private/task_provenance.json`, per-attempt workspace copies of the
immutable `bundle/` template, executability preflight, and resource freshness
inside the writer-locked commit. Milestone B1 added the subprocess contracts
only: extended `AttemptOutcome`/fallback set, secondary `AttemptFailure`
records, `SubprocessPolicy` and writable-quota roots, `DiagnosticCapture`,
`ExecutionFileRecord`, `ConfinementLevel`, credential providers, frozen
primary-outcome precedence, and object-root proposal-schema preflight.
Milestone B2 added the deterministic runner machinery in
`runtime/execution.py`, `runtime/output_policy.py` and
`runtime/resource_limits.py`: `ExecutionBackend`, `TemporaryWorkspaceBackend`
(TEST_ONLY; a real engine can never be enabled through it), per-attempt
temporary execution roots outside the review project, bounded redacting
stream capture, writable-tree and process-group monitoring, race-resistant
proposal import, and `SubprocessEngine` (an `AgentEngine` that runs an argv
worker and reports through the frozen §6.6 precedence). The deterministic
fake worker (`tests/helpers/fake_agent.py`, 32 modes) and the B2
conformance suite (eleven `tests/runtime/test_subprocess_*.py` modules)
exercise the full boundary; no real external engine exists yet. Do not redesign the scientific chain or
implement a real semantic engine without a new milestone.

Do not add a database, web UI, workflow engine, multi-agent architecture,
Graphify integration, PDF parsing, retrieval execution, real LLM calls,
manuscript assembly, or citation rendering.

The scientific contracts must remain independent of any LLM provider or agent
engine. Future orchestration may select Codex, Agy, Kimi, OpenCode, or another
engine behind adapters, but engine-specific payloads and state must not enter
these persistent models.

Every contract-valid scientific result is canonicalized, including REJECT,
UNCLEAR, UNSUPPORTED, OVERSTATED, and PARTIALLY_SUPPORTED. These dispositions
may block downstream transition but must never trigger engine fallback.

## Environment

The supported V1 execution environment is Linux and WSL2. Native Windows
support remains deferred because the writer lock uses `fcntl`.

Tests that require a live external LLM-agent engine must be marked
`external_engine`. They are excluded from normal CI, which runs
`python -m pytest -m "not external_engine"`.

## Commands

```bash
python -m pytest
```

All Python and serialized text is UTF-8. Keep repository validators pure and
independent of the current working directory.
