# VibeReview Runtime Handoff

The current integration boundary is a public-safe foundation plus a
deterministic synthetic Package C engineering harness, not a completed or
operational scientific pilot.

The private `master` line is rooted in a neutral one-commit public-safe
snapshot. GitHub Actions are disabled. Provider-managed historical review
references remain retained by owner decision and must be considered in any
later repository-wide publication review.

Implemented foundations include the frozen V1.5.1b scientific contracts,
immutable generations, task-resource bundles, deterministic subprocess
execution, machine-local Bubblewrap qualification, bounded outbound-request
compilation, an explicit offline engine, and pinned-Git library import plus raw
candidate retrieval. A fixed `MockEngine` controller now exercises the existing
scientific chain on fictional five-paper repositories, including coupled
evidence promotion, immutable audit/draft/run artifacts, exact assembly,
offline packets, and reproduction comparison. The DeepSeek REST adapter can
only be constructed through the qualified live factory; no provider call was
made while implementing or testing this boundary. Kimi, Agy, and Codex live
factories are explicitly unavailable.

## Latest synthetic hardening

Pilot execution bypasses the runtime semantic proposal cache only for the
synthetic controller call. `MockEngine` scripts are stored as immutable
canonical snapshots, engine identities are sanitized and manifest-bound, and a
fresh engine cursor is restored only from authenticated journaled attempt
counts.

The controller persists a provenance-bound task marker before execution and
reconciles marked task workspaces at startup and before further work. Any
attempt lacking accepted or terminal-failure journal accounting fails closed
for explicit operator recovery; the controller does not invent accounting,
replay the attempt, or discard its workspace.

`PilotTaskUsageArtifact` now uses `package-c-pilot-task-usage-2`, and pilot
packets use `package-c-synthetic-pilot-packet-2`. Accepted and terminal-failure
task usage binds all actual attempts, request bytes, elapsed seconds, outputs,
proposals, diagnostics, and writable-tree accounting. Packet verification
reconciles accepted-task deltas against the immutable cumulative journal.
Version 1 usage and packet artifacts are superseded and are not accepted.

Aggregate usage-model ceilings now cover the structural maximum of 128
attempts. This corrects failure-witness representability; it does not enlarge
the registered `FivePaperPilotBudget`. `collect_pilot_task_usage()` produces
the retained-attempt witness before controller request/time binding. Persisted
canonical usage must pass through `bind_pilot_task_execution_accounting()`, or
use `build_pilot_task_usage()` with positive `task_request_bytes` and
`task_elapsed_seconds`.

The authoritative operator and implementer contract is the
[synthetic Package C harness guide](docs/operations/synthetic_package_c_harness.md).

## Current candidate evidence

The functional hardening is commit
`9b46c9aeec253137bbe29d713a87c9e1d6fc9623`. The normative guide and corrected
fingerprinted source docstrings are commit
`51373334ca62d225da7123e54e41daf627999564`. Because those Python docstrings are
inside the implementation-fingerprint closure, all behavioral evidence below
was rerun against exact commit `51373334ca62d225da7123e54e41daf627999564`
on 2026-09-13 with Python 3.13.12 on
`Linux-7.0.0-29-generic-x86_64-with-glibc2.43`.
The commands below preserve the exact options and selections; only the
operator-local absolute temporary and denylist paths are represented by
placeholders so personal filesystem paths do not enter Git.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m pytest -q \
  -m "not external_engine and not requires_bwrap and not external_corpus" \
  --basetemp=<outside-repository>
# 1608 passed, 21 deselected in 656.33s

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m pytest -q \
  -m "requires_bwrap or sandbox_conformance" \
  --basetemp=<outside-repository>
# 23 passed, 1606 deselected in 10.21s

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python -W error::ResourceWarning -m pytest -q \
  tests/library/test_pilot_controller_evidence_e2e.py \
  --basetemp=<outside-repository>
# 1 passed in 215.75s

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python -m vibereview.library.public_guard . \
  --revision HEAD \
  --private-denylist <administrator-held-denylist>

git diff-tree --check HEAD^ HEAD
```

The ordinary marker excludes live engines, tests requiring Bubblewrap, and
external corpora. The sandbox marker selects only Bubblewrap or
sandbox-conformance tests. The E2E uses fictional local Git repositories and
`MockEngine`; no live provider or operator corpus was used.

The public guard, candidate/master-ancestry private-denylist scan, and diff
check passed. This scan covers the candidate revision, index/worktree policy,
and its reachable master ancestry; it does not prove that every
provider-advertised reference is clear. Complete advertised-reference review,
hosted CI, runner qualification, and protection remain open administrative
gates. These are commit-bound engineering results, not live-provider,
external-corpus, scientific-acceptance, or release evidence.

The retired real-corpus pilot is held outside Git as a private historical
archive. It is excluded from review discovery and production imports. Its
structural artifacts do not constitute semantic evaluation or human review,
and it is publication ineligible.

## Quick verification

```bash
# Ordinary deterministic suite
python -m pytest -m "not external_engine and not requires_bwrap and not external_corpus"

# Linux Bubblewrap/conformance suite
python -m pytest -m "requires_bwrap or sandbox_conformance"

# Machine-local sandbox diagnostics
python -m vibereview.runtime.sandbox probe --json
python -m vibereview.runtime.sandbox qualify --network-policy deny
python -m vibereview.runtime.sandbox status
```

Do not copy qualification artifacts between machines. Do not run a live engine
merely because a credential happens to exist in the environment. A live run
requires explicit operator authorization, current qualification at both
checkpoints, a reviewed engine/model configuration, and a separately approved
spend ceiling. The example `deny` qualification cannot authorize the DeepSeek
REST adapter. Its `HOST` network profile must be qualified separately, permits
general host networking rather than a provider-only allowlist, and still does
not constitute authorization to send a request.

## MyLib operational review slice (Milestones 0–6)

The working tree additionally contains the authorized operational slice for
the real pinned MyLib library, normatively specified in
[`docs/operations/mylib_operational_review.md`](docs/operations/mylib_operational_review.md)
(Milestone 0 of `goal.md`). This slice authorizes Milestones 1–6 only; it
does not authorize a live provider, model spending, human scientific
acceptance, publication, or public export, and it does not change the frozen
V1.5.1b scientific models or enums.

Implemented pieces:

- Milestone 1: `tests/library/test_mylib_boundary.py` verifies the real
  in-tree submodule (`external/MyLib`, pin record
  `configs/libraries/mylib.toml`): configured commit = gitlink = HEAD,
  pinned-object-only reads, inventory of `Libs/*.md` / `ref.bib` / graph,
  selection completeness and budgets, and a before/after no-mutation oracle.
  Opt-in via `external_corpus` + `VIBEREVIEW_RUN_EXTERNAL_CORPUS=1`.
- Milestone 2: `library/aliases.py` (versioned operator adjudication file
  `vibereview-source-aliases-1`, wired into `inspect --aliases` and
  `import_selected_corpus(aliases=...)`, fail-closed on conflict),
  a unique-and-non-conflicting normalized DOI/title match tier in
  `resolver.py` (advisory only; it never authorizes import metadata), and
  `library/source_quality.py` (`vibereview-source-quality-1`:
  READABLE / READABLE_WITH_ARTIFACTS / MATERIAL_EXTRACTION_PROBLEM /
  UNUSABLE records, deterministic advisory diagnostics, and a
  selection-layer blocking check for defective included sources).
- Milestone 3: `tests/library/test_evidence_closure_regression.py`
  (70 tests, all five evidence categories × all four transitions through the
  real promotion path). TEST FIRST demonstrated one validator defect: with a
  ClaimPacket present, hand-built snapshots could omit a CPE from the final
  validation, or leave claim EvidenceRecords outside every CPE, and still
  pass `validate_repository`. Minimal fix in `validators.py`:
  `FINAL_CPE_COVERAGE_MISMATCH` and `UNAGGREGATED_EVIDENCE_AFTER_PACKET`,
  both gated on packet existence so legitimate intermediate states remain
  valid. Because the validator fingerprint covers `validators.py`, pre-fix
  semantic receipts are intentionally invalidated (designed behavior).
- Milestone 4: `library/retrieval_coverage.py` — deterministic multi-intent
  query-plan helper (`vibereview-retrieval-query-plan-1`), retrieval-coverage
  diagnostic report with operator-recorded status
  (`vibereview-retrieval-coverage-1`), and a retrieval benchmark harness
  (`vibereview-retrieval-benchmark-1` / `-report-1`) measuring known-paper,
  known-passage, and per-category recall over the deterministic retrievers.
- Milestone 5: `library/semantic_benchmark.py` — adjudicated claim–passage
  case format (`vibereview-semantic-benchmark-1`, development/evaluation
  split, retained reviewer disagreement), an engine-agnostic runner, and a
  metrics report (`vibereview-semantic-benchmark-report-1`) with per-class
  precision/recall, confusion matrix with an explicit `unpredicted` bucket,
  false-support rate, and first-class contradiction/qualification recall.
  Real adjudicated cases remain operator artifacts outside Git.
- Milestone 6: `library/operational_pilot.py` — a bounded operator harness
  (`vibereview-operational-pilot-1` proposal bundle keyed by stable
  content/span anchors) that drives the ordinary runtime with deterministic
  `build_offline_engine` proposals through import → retrieval → coupled
  evidence promotion → aggregation → claim assessment → final validation →
  ClaimPacket, with terminal `validate_repository`, exact span-locator, and
  packet-provenance verification. `tests/library/test_operational_pilot.py`
  covers it end-to-end on a synthetic five-paper fixture (ordinary suite);
  `tests/library/test_operational_pilot_mylib.py` runs it against the real
  pinned MyLib under explicit opt-in.

No synthetic Package C machinery (controller, journal, manifest, packet) was
reused for the real corpus; that harness stays synthetic-only.

### Verification of this slice

All commands run with `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src` on this
working tree (uncommitted; commit-bound evidence is recorded when the slice
lands in Git), Python 3.13 on Linux:

```bash
python -m pytest -q -m "not external_engine and not requires_bwrap and not external_corpus" \
  --basetemp=<outside-repository>
# 1734 passed, 27 deselected

python -m pytest -q -m "requires_bwrap or sandbox_conformance" \
  --basetemp=<outside-repository>
# 23 passed

VIBEREVIEW_RUN_EXTERNAL_CORPUS=1 \
  python -m pytest -q tests/library/test_mylib_boundary.py -m external_corpus \
  --basetemp=<outside-repository>
# 5 passed (real pinned MyLib; before/after library snapshot identical)

VIBEREVIEW_RUN_EXTERNAL_CORPUS=1 \
  VIBEREVIEW_OPERATIONAL_PILOT_DIR=<operator-project-outside-repository> \
  python -m pytest -q tests/library/test_operational_pilot_mylib.py \
  --basetemp=<outside-repository>
# 1 passed: five real MyLib papers imported; one claim assessed through the
# complete chain to a ClaimPacket; supports and qualifies spans resolve to
# exact pinned Markdown slices; final repository state byte-identical across
# three independent executions; library snapshot unchanged
```

The external-corpus runs used operator artifacts held outside Git; no corpus
content entered the repository. These are engineering results only: no live
provider was constructed, and nothing here constitutes scientific acceptance,
publication eligibility, or Milestone 7–12 authorization.

## Remaining gates

1. Before release or publication, enable and configure Actions, execute the
   exact candidate on a dedicated credential-free self-hosted runner, and
   verify enforceable required checks and review protection.
2. Scan the complete ancestry of public-safe master and every advertised
   reference with the path policy and administrator-held private denylist.
   Retained provider-managed references keep the repository-wide publication
   gate open.
3. Do not begin an operational Package C run until the administrative gates
   pass and the operator explicitly authorizes the provider, corpus, host
   policy, output location, and finite budgets. The synthetic harness is not
   that authorization. Runtime drafts must be promoted in
   validator-compatible coupled transactions; negative and uncertain
   scientific results remain canonical.
4. Keep real corpus/provider runs and human scientific review separately
   authorized. Automated success never makes an output publication eligible
   when human review is `NOT_PERFORMED`.

Historical technical handoffs remain under `docs/handoffs/`. The current
administrative gate is documented in
`docs/operations/runner_qualification_and_gate_runbook.md`, and the public data
boundary is documented in `docs/security/public-data-boundary.md`. Synthetic
Package C operation and recovery are documented in
`docs/operations/synthetic_package_c_harness.md`.
