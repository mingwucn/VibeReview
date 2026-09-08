# Hand-off — r5e–r5h: Sandbox Qualification and Receipt Audit

## Summary

r5e–r5g repaired the sandbox conformance story after the hosted-CI sandbox job
failure and replaced declaration-based qualification with executed,
report-derived, machine-local qualification. r5h corrected receipt lookup
semantics and documentation before any live model may run.

- **Suite status (this host):** 747 passed via `python -m pytest`
  - Deterministic matrix: 728 passed, 19 deselected
    (`python -m pytest -m "not external_engine and not requires_bwrap"`)
  - Sandbox selection: 22 passed
    (`python -m pytest -m "requires_bwrap or sandbox_conformance"`)
- **Qualification on this host:** fingerprint
  `sha256:502e6522f6fd73878e0c558f34841ccb5167b32d3d66d92f34aaf505602ce59f`,
  report hash
  `sha256:c7ac966b313d8d725fbb69c401ee06f0a44448d834a872c0148236838ca9b81a`.

## The original CI failure class

The hosted `sandbox-conformance` job failed on `ubuntu-latest` runners. The
hypothesized mechanism was the Ubuntu 24.04 AppArmor unprivileged-userns
restriction (`kernel.apparmor_restrict_unprivileged_userns=1` without a
profile granting bubblewrap userns creation), which makes `bwrap --unshare-user`
fail for unprivileged processes.

That mechanism is **confirmed as plausible**: this development host carries the
same restriction (`/proc/sys/kernel/apparmor_restrict_unprivileged_userns=1`)
*and* a prepared profile (`/etc/apparmor.d/bwrap-userns-restrict`-style), and
the sandbox probes USABLE and qualifies here. The hosted runner's exact stderr
was never captured pre-r5e, so the failure class could not be proven from
evidence; the r5e probe models and the r5g `sandbox probe` CLI now capture the
stage, exit code, bounded stderr, and failure code (`SandboxFailureCode`) of
every probe command, so any recurrence is diagnosable from the uploaded
`sandbox-probe.json` artifact.

## r5e — Sandbox capability probe

- `runtime/confinement.py`: `SandboxProbeResult`, `SandboxProbeCommandResult`,
  `SandboxProbeStatus` (UNAVAILABLE / BLOCKED / USABLE), `SandboxFailureCode`.
- `probe_sandbox_capabilities()` runs staged probes — version, user+mount, PID,
  IPC, UTS, cgroup, network namespaces, and a full-profile execution probe —
  with a sanitized environment, bounded stream capture, and timeouts, outside
  any review project.
- Kernel sysctl reads (`unprivileged_userns_clone`,
  `apparmor_restrict_unprivileged_userns`, AppArmor profile detection) feed the
  BLOCKED vs UNAVAILABLE classification.

## r5f — Explicit profile and environment

- Single canonical bwrap profile argv builder (`build_bwrap_profile_argv`)
  shared by the probe and the backend, pinned by `SANDBOX_PROFILE_CANONICAL`
  and profile-hash tests.
- Sandbox-local HOME/XDG/TMPDIR, cleared environment, explicit PATH/LANG; CA
  bundles and DNS config bound read-only for the HOST network profile.

## r5g — Report-derived qualification, store, CLI, CI

- `runtime/conformance.py`: `SandboxConformanceCaseResult` /
  `SandboxConformanceReport` (goal.md §6.1) and `run_sandbox_conformance()`,
  which executes all 11 required cases through the real
  `SubprocessEngine` + `BubblewrapExecutionBackend` path against a throwaway
  project using the shipped worker `runtime/conformance_worker.py` (test
  helpers are never imported by shipped code). Case ids equal the pytest
  conformance test names; a drift test keeps the two suites in lockstep.
  `compute_conformance_report_hash()` uses the excluded-field pattern.
- `runtime/confinement.py`: `ConfinementQualification` reworked (§6.2) with
  `conformance_report_hash`, `required_cases_passed`, `qualified_at`;
  `issue_qualification()` is the only normal `qualified=True` constructor and
  binds every field from the executed report plus the current environment
  fingerprint — caller-provided test names are never evidence;
  `CONFORMANCE_SUITE_VERSION = "1.1"`; `REQUIRED_CONFORMANCE_TESTS` covers 11
  cases (added loopback-under-HOST, credential diagnostic redaction, and
  descendant quiescence).
- `require_real_engine_qualification(backend, qualification,
  current_fingerprint, *, current_probe, report)` (§6.3) fails closed on every
  mismatch class: backend level, qualified flag, probe USABLE, user/mount, PID
  and (for DENY) network namespace stage availability, level match, executable
  identity/hash, code fingerprint, profile hash, platform fingerprint, network
  policy, suite version, report hash recomputation, qualification↔report hash
  resolution, all-required-passed, and required-case coverage.
- `runtime/qualification_store.py`: machine-local store at
  `~/.config/vibereview/qualifications/<storage-fingerprint>.json`
  (`VIBEREVIEW_QUALIFICATION_DIR` override). The storage fingerprint covers the
  bubblewrap executable hash, confinement code fingerprint, profile hash,
  platform capability fingerprint, network policy, and suite version — any
  change invalidates the qualification automatically. A qualification from CI
  is never authority for another machine.
- `runtime/sandbox.py`: administrative CLI `python -m
  vibereview.runtime.sandbox probe|qualify|status` with `--json` and
  `--output-dir`; exit codes 0 (usable/qualified) / 2 (unavailable) / 3
  (blocked) / 4 (conformance failed) / 5 (internal error).
- CI: `sandbox-qualification` runs on `[self-hosted, linux,
  vibereview-sandbox]` and uploads `sandbox-probe.json`,
  `sandbox-conformance-report.json`, `confinement-qualification.json`.
  `sandbox-hosted-capability-check` proves fail-closed behavior on hosted
  runners (probe not usable ⇒ qualify exits 2/3/4 and writes no qualification
  artifact) and is never named or reported as a qualification.

## r5h — Receipt keys, integrity, generation semantics

- `runtime/receipts.py`: `compute_input_identity_key()` covers task type,
  TaskSpec version, prompt/schema hashes, dependency and resource hashes,
  engine-input hash, engine identity/version/configuration, and the scientific
  contract version — and excludes the validator, promotion, and disposition
  fingerprints and generation numbers. `compute_semantic_task_key()` now
  derives from the input identity key plus validator, promotion-handler, and
  disposition-handler fingerprints and the runtime contract version.
- `AppliedTaskReceipt` gains `input_identity_key` alongside
  `semantic_task_key`. Receipts are internal runtime state (not frozen
  scientific objects): pre-r5h stored receipts no longer validate and are
  simply absent for new projects.
- `runtime/kernel.py` lookup: exact semantic key → normal reuse evaluation;
  same input identity under a different semantic key → semantics changed → the
  receipt becomes the candidate and the reevaluation-required path engages;
  no input-identity match → unrelated task, its transition is never inspected.
  The old scan by task type + engine was removed.
- Receipt payload integrity before reuse (§7.2):
  `hash_json(receipt.proposal_payload) == receipt.proposal_hash` (checked
  first, before the payload is trusted), local_ref_map IDs ⊆ canonical object
  receipts, canonical object hashes (existing), and explicit task-type,
  engine-identity, input-identity, and semantic-fingerprint match rejections.
  Receipt `proposal_hash` now stores the canonical payload hash
  (`hash_json(raw_proposal)`); the raw engine-output hash remains on the
  attempt record only.
- Generation semantics (§7.3): on reuse, `RuntimeResult.generation` is the
  current canonical generation, `reused_generation` is the receipt's
  historical commit generation, `receipt_reused=True`,
  `commit_performed=False`.

## Current gate status

- Deterministic matrix and sandbox selection are green locally.
- This host probes USABLE and holds an issued qualification (fingerprint
  above).
- The `sandbox-qualification` CI job is defined; first green run on the
  self-hosted runner is pending, so the final pre-real-engine gate remains
  **blocked**.

## Branch protection (goal.md §6.7, verbatim)

> The current `master` branch is unprotected and has no required checks.
>
> After the repaired workflow is green, require:
>
> ```text
> pytest (3.11)
> pytest (3.12)
> pytest (3.13)
> sandbox-qualification
> ```
>
> for changes affecting:
>
> ```text
> runtime/confinement.py
> runtime/execution.py
> runtime/credentials.py
> runtime/trusted_launcher.py
> runtime/output_policy.py
> runtime/resource_limits.py
> engines/
> ```
>
> A ruleset may permit documentation-only changes to bypass the sandbox job,
> but code touching real-engine security boundaries must not merge without it.

Branch protection requires **repo-admin action after CI is green**; it is not
implemented by these commits.
