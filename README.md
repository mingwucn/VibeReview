# VibeReviewPaper

VibeReviewPaper is a Python application for producing evidence-constrained
scientific reviews. The repository contains the frozen V1.5.1b scientific
contracts, runtime 1.6 execution kernel, deterministic subprocess runner,
and pre-real-engine hardening release:

- strict Pydantic scientific models and repository validation;
- immutable, numbered repository generations and atomic `CURRENT` updates;
- writer locking, transactional canonical-ID allocation, and crash recovery;
- immutable task snapshots, proposal DTOs, freshness checks, and attempt logs;
- validator-aware semantic cache primitives, accepted-task receipts with
  input-identity/semantic key separation, and bounded fallback policy;
- replaceable `AgentEngine` architecture (`MockEngine` and deterministic `SubprocessEngine`);
- Bubblewrap OS sandbox backend (`BubblewrapExecutionBackend`) with staged
  capability probe, programmatic conformance suite, report-derived
  qualification, and fail-closed real-engine gate.

## Implementation Status

| Component | Status |
|---|---|
| Scientific contracts (V1.5.1b) | complete |
| MockEngine runtime | complete |
| Task-resource boundary (Milestone A) | complete |
| Subprocess contracts & precedence (Milestone B1) | complete |
| Deterministic subprocess runner (Milestone B2) | complete |
| Pre-real-engine hardening (Milestone B3) | complete |
| Bubblewrap backend implementation | complete |
| Sandbox CLI unit-test isolation (r5i) | complete |
| CI diagnostics & evidence indexing (r5j) | complete |
| Sandbox conformance qualification | qualified on the development host; CI runner registration pending |
| Receipt correctness & documentation audit (r5h) | complete |
| Final pre-real-engine gate | blocked until CI qualification evidence and branch protection exist |
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

The administrative sandbox CLI probes, qualifies, and inspects the local host:

```bash
python -m vibereview.runtime.sandbox probe --json
python -m vibereview.runtime.sandbox qualify --network-policy deny
python -m vibereview.runtime.sandbox status
```

`qualify` executes the 11-case conformance suite through the real sandboxed
runner and, only when every required case passes, issues a machine-local
qualification under `~/.config/vibereview/qualifications/` (override with
`VIBEREVIEW_QUALIFICATION_DIR`). The qualification binds the exact bubblewrap
executable, confinement code, sandbox profile, platform capabilities, network
policy, and conformance suite version; any change invalidates it automatically.
A qualification produced by one machine is never authority for another.

Run the sandbox conformance suite directly:

```bash
python -m pytest -m "requires_bwrap or sandbox_conformance"
```

Qualification environment and results to date: the development host (Linux
7.0.0-29-generic x86_64, bubblewrap 0.11.1, AppArmor userns restriction with a
`bwrap-userns-restrict`-style profile present) probes USABLE and holds issued
qualification fingerprint
`sha256:502e6522f6fd73878e0c558f34841ccb5167b32d3d66d92f34aaf505602ce59f`.
The full suite passes 776 tests locally on this host (757 deterministic and 19
requires_bwrap/conformance tests). In CI, unit test hermeticity is resolved (r5i)
and diagnostics are hardened (r5j); the `sandbox-qualification` CI job (self-hosted
`vibereview-sandbox` runner) is defined and pending dedicated runner infrastructure
(see `docs/operations/runner_qualification_and_gate_runbook.md`). The final pre-real-engine
gate remains blocked until CI qualification evidence exists and branch protection is configured.

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
