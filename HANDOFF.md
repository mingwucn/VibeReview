# Hand-off — Milestones B1 + B2 complete (2026-09-06)

## What was asked

Implement `goal.md` (scheduled to start at 5 a.m.). Per goal.md §25 ("Immediate
Codex assignment"), scope was **Milestones B1 + B2 only** — deterministic fake
subprocess execution — stopping after the conformance suite passes. No real
engine, no Graphify, no CLI/CAS/ingestion/manuscript work.

## Current state

- Branch head: `f5d490a` — four commits:
  - `49357d0` r4a — B1 contracts
  - `5f66d78` r4a — B1 contract tests
  - `a3be05e` r4b — B2 runner + fake worker
  - `f5d490a` r4c — B2 conformance tests
- Suite: **652 passed** via `python -m pytest -m "not external_engine" -q`
  (baseline was 326 at `r3` = `ca7e1a4`).
- `goal.md` has a pre-existing uncommitted modification (predates this work).
  This file (`HANDOFF.md`) is intentionally uncommitted.
- AGENTS.md "Current boundary" section documents the B1/B2 boundary — keep it
  in sync with any further milestone work.

## What was built

### B1 contracts (goal.md §6)

- `runtime/records.py`: `AttemptOutcome` +2 members
  (`ENGINE_OUTPUT_POLICY_FAILURE`, `ENGINE_RESOURCE_LIMIT_FAILURE`);
  `FALLBACK_OUTCOMES` = the 7 engine failures; `AttemptFailure` /
  `AttemptFailureStage` (8 stages) / `ResourceLimitCode` (8 codes);
  `TaskAttemptRecord.detected_failures`.
- `runtime/subprocess.py`: `SubprocessPolicy`, `deterministic_test_policy()`,
  `WRITABLE_QUOTA_ROOTS` / `QUOTA_EXEMPT_ROOTS` + `writable_quota_applies()`
  (output/scratch/home/tmp only), `primary_attempt_outcome()` — the frozen §6.6
  precedence as a pure keyword-flag function.
- `runtime/diagnostics.py`: `DiagnosticCapture` (hash covers only the persisted
  redacted file).
- `runtime/execution_inventory.py`: `ExecutionFileRecord` (unsafe types carry
  no size/hash).
- `runtime/confinement.py`: `ConfinementLevel`; `allows_real_engine()` is True
  **only** for OS_SANDBOX / ENGINE_NATIVE_SANDBOX (§8.3 — note this is stricter
  than §6.10's lone TEST_ONLY sentence; deliberate).
- `runtime/credentials.py`: `CredentialContext` (secrets masked,
  `exact_redaction_values` excluded from dumps), `EngineCredentialProvider`
  protocol, `NullCredentialProvider`, `SyntheticCredentialProvider`.
- `runtime/specs.py`: §6.11 object-root proposal-schema preflight (rejects
  `RootModel`, resolves top-level `$ref`, requires `type: object`).

### B2 runner (goal.md §7)

- `runtime/execution.py`: `ExecutionBackend` protocol,
  `TemporaryWorkspaceBackend` (TEST_ONLY), `LauncherConfiguration`,
  `SubprocessEngine` (AgentEngine), `SubprocessExecutionResult`,
  bounded redacting stream capture (carry-over = max secret length − 1,
  plus a straddle pull-back), SIGTERM→grace→SIGKILL process-group timeout.
- `runtime/output_policy.py`: trusted output-dir fd identity
  (`O_RDONLY|O_DIRECTORY|O_NOFOLLOW`, st_dev/st_ino recheck before
  missing-proposal classification), output-tree scan, race-resistant
  `dir_fd`/`O_NOFOLLOW` proposal import (regular file, `st_nlink == 1`, no
  bundle inode, size cap, stable fstat, strict UTF-8, non-empty).
- `runtime/resource_limits.py`: writable-tree monitor (scandir/lstat, no
  follow) with first-breach `ResourceLimitCode` + group kill; process count via
  `/proc` pgid membership. Also a post-exit final scan (breaches are
  deterministic even for fast workers).
- `runtime/kernel.py`: one inserted branch — non-VALID `execution_report`
  primary outcome recorded with `detected_failures`, then the normal fallback
  loop. MockEngine path untouched.
- `tests/helpers/fake_agent.py`: 32 §7.13 modes, composable via repeated
  `--mode`; config via `launcher/worker_config.json`; canned payloads staged as
  `launcher/{valid,schema_invalid,proposal_invalid}_proposal.json`.

### Tests added (237 → 652 total)

- B1: `test_attempt_precedence.py`, `test_subprocess_policy.py`,
  `test_diagnostic_contract.py`, `test_resource_limit_contract.py`,
  `test_output_policy_contract.py`, `test_proposal_schema_root.py`.
- B2 conformance (§7.14): `test_subprocess_execution.py`,
  `test_subprocess_failure_precedence.py`, `test_subprocess_diagnostics.py`,
  `test_subprocess_timeout.py`, `test_subprocess_output_directory.py`,
  `test_subprocess_proposal_import.py`, `test_subprocess_output_policy.py`,
  `test_subprocess_resource_limits.py`, `test_subprocess_fallback.py`,
  `test_subprocess_secrets.py`, `test_subprocess_cleanup.py`.

All §7.14 required assertions are covered, incl. fallback-gets-pristine-bundle
after tampering, REJECT canonicalized without fallback (ASSESS_CLAIM via
subprocess), 10 MB bounded streams, child-process group kill, both §8.2 secret
tests, and exec-root deletion.

## Known limitations / decisions

- Deferred by design to B3: kernel RLIMITs (trusted launcher path);
  `max_open_files` / `max_cpu_seconds` / `max_address_space_bytes` are policy
  fields the Python monitor does not yet enforce.
- GitHub Actions matrix (3.11–3.13) unchanged but not triggered locally —
  CI runs on push.
- `test_subprocess_cleanup.py::test_no_execution_root_leaks_across_multi_attempt_run`
  globs shared `/tmp` for `vibereview-exec-*`; it can false-fail if another
  pytest process runs concurrently on the same machine. Green in serialized
  runs; if it flakes in CI, scope the leak check to the project.
- Resource-limit combo tests pin determinism with
  `writable_tree_scan_interval_seconds=30.0` (post-exit scan path).
- Cache-hit behavior: a second identical run revalidates and recommits
  (generation 2, fresh IDs) — it does NOT keep generation 1. Tests assert
  non-execution via missing `execution_report`, not generation number.
- For malformed-JSON combos, the kernel never parses the proposal after a
  runner-stage failure, so "both conditions retained" is proven via the
  execution inventory's content hash, not `detected_failures`.

## How to verify

```bash
python -m pytest -m "not external_engine" -q   # expect: 652 passed
git log --oneline -5
```

## Next milestones (goal.md, not started)

- **B3** (§8): file-based credentials (0600/0700, cleanup), a qualified
  non-TEST_ONLY confinement backend (engine-native or Linux OS sandbox; fail
  closed otherwise). Suggested commit `r5a`.
- **C** (§9): CodexEngine adapter, one `ASSESS_CLAIM` task, four live fixtures
  marked `external_engine`. Suggested `r5b`.
- Then D (CLI), E (CAS), F (DR ingestion), G (concept sketches + corpus
  challenger), H (Graphify), I (evidence/claim loop), J (prose/manuscript),
  K (five-paper slice) per the §5 table and §22 commit discipline.

## Session machinery notes

- This work ran under goal mode + agent swarms. Implementing subagents keep
  context and can be resumed by id (`Agent(resume=...)`) if follow-up fixes are
  needed: runner = agent-16; fake worker = agent-17; B2 test modules =
  agent-18…agent-28.
- Provider 5-hour quota limits interrupted the swarm twice; scheduling via
  one-shot cron worked around it.
