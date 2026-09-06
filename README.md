# VibeReviewPaper

VibeReviewPaper is a Python application for producing evidence-constrained
scientific reviews. The repository contains the frozen V1.5.1b scientific
contracts, runtime 1.6 execution kernel, deterministic subprocess runner,
and pre-real-engine hardening release:

- strict Pydantic scientific models and repository validation;
- immutable, numbered repository generations and atomic `CURRENT` updates;
- writer locking, transactional canonical-ID allocation, and crash recovery;
- immutable task snapshots, proposal DTOs, freshness checks, and attempt logs;
- validator-aware semantic cache primitives, accepted-task receipts, and bounded fallback policy;
- replaceable `AgentEngine` architecture (`MockEngine` and deterministic `SubprocessEngine`);
- qualified Linux OS sandbox backend (`BubblewrapExecutionBackend`) with fail-closed qualification gate.

## Implementation Status

| Component | Status |
|---|---|
| Scientific contracts (V1.5.1b) | complete |
| MockEngine runtime | complete |
| Task-resource boundary (Milestone A) | complete |
| Subprocess contracts & precedence (Milestone B1) | complete |
| Deterministic subprocess runner (Milestone B2) | complete |
| Pre-real-engine hardening (Milestone B3) | complete |
| Qualified Linux confinement (`BubblewrapExecutionBackend`) | complete |
| CodexEngine adapter (Milestone C) | pending |
| Scientific vertical slice | pending |

## Environment and Support

### Core Linux and WSL2 support
The supported V1 runtime environment is Linux and WSL2. Native Windows support
remains deferred because the repository writer lock uses POSIX `fcntl`.
The standard deterministic matrix runs under Python 3.11–3.13 without external
sandboxing dependencies:

```bash
python -m pip install -e '.[test]'
python -m pytest -m "not external_engine and not requires_bwrap"
```

### Qualified live-engine support
Live semantic engines (such as a future `CodexEngine`) require an execution
backend operating at `ConfinementLevel.OS_SANDBOX` or `ENGINE_NATIVE_SANDBOX`.
The default qualified Linux backend is `BubblewrapExecutionBackend` (`bwrap`).

On WSL2 and Linux hosts running live engines:
- Unprivileged user namespaces must be enabled (`sysctl kernel.unprivileged_userns_clone=1` or modern Linux default).
- Bubblewrap must be installed (`bwrap`).
- Conformance qualification is verified through `require_real_engine_qualification()`.
  If sandbox isolation, host canary protection, project path inaccessibility, or credential isolation fails, real engines fail closed immediately.

Run the sandbox conformance suite:

```bash
python -m pytest -m "requires_bwrap or sandbox_conformance"
```

The future user-facing entry point will be:

```bash
python vibe_review.py run reviews/<project>
```

That CLI, live-engine adapters, and all document-processing phases remain out of
scope. The contracts are engine-neutral so later Python orchestration can choose
among different bounded LLM workers without changing scientific provenance.

Contract-valid negative or uncertain results are canonical state. Fallback is
reserved for engine execution, format, schema, or proposal-validation failures;
it is never used to seek a preferred scientific conclusion.
