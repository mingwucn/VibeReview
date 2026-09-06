# VibeReview Runtime Handoff

The active boundary is the post-r5h pre-real-engine state: Milestone B3 plus
the sandbox probe/conformance/qualification machinery (r5e–r5g) and the
receipt-correctness and documentation audit (r5h). No real external engine
exists yet; Milestone C (`CodexEngine`) remains deferred.

For detailed historical and milestone handoffs, see:

- **Milestone A (Task-Resource Boundary, r3):** [`docs/handoffs/r3-task-resources.md`](docs/handoffs/r3-task-resources.md)
- **Milestones B1 + B2 (Deterministic Subprocess Runner, r4):** [`docs/handoffs/r4-b1-b2.md`](docs/handoffs/r4-b1-b2.md)
- **Milestone B3 (Pre-Real-Engine Hardening Release, r5):** [`docs/handoffs/r5-pre-real-engine.md`](docs/handoffs/r5-pre-real-engine.md)
- **Sandbox qualification and receipt audit (r5e–r5h):** [`docs/handoffs/r5e-r5h-sandbox-qualification.md`](docs/handoffs/r5e-r5h-sandbox-qualification.md)

## Quick Verification

```bash
# Standard deterministic test matrix (excludes live engine and bwrap tests)
python -m pytest -m "not external_engine and not requires_bwrap"

# Sandbox conformance suite (requires Linux bubblewrap)
python -m pytest -m "requires_bwrap or sandbox_conformance"

# Full local suite
python -m pytest

# Sandbox diagnostics (probe / qualify / status)
python -m vibereview.runtime.sandbox probe --json
python -m vibereview.runtime.sandbox qualify --network-policy deny
python -m vibereview.runtime.sandbox status
```

## Current State

- **Scientific Contract Version:** V1.5.1b (frozen)
- **Runtime Contract Version:** 1.6
- **Suite status (qualification host):** 747 passed via `python -m pytest`
  - Deterministic matrix: 728 passed, 19 deselected
    (`python -m pytest -m "not external_engine and not requires_bwrap"`)
  - Sandbox selection: 22 passed
    (`python -m pytest -m "requires_bwrap or sandbox_conformance"`)
- **Sandbox qualification:** the conformance report derives the qualification
  via `issue_qualification()`; machine-local store under
  `~/.config/vibereview/qualifications/` (`VIBEREVIEW_QUALIFICATION_DIR`
  override). This development host is qualified (fingerprint
  `sha256:502e6522f6fd73878e0c558f34841ccb5167b32d3d66d92f34aaf505602ce59f`).
  CI qualification evidence from the self-hosted `vibereview-sandbox` runner
  is pending; see the r5e–r5h handoff for the branch-protection requirement.
- **Receipts:** lookup uses `input_identity_key` (what the task operates on)
  plus `semantic_task_key` (input identity + evaluation semantics). Unrelated
  invocations of the same task type are never confused; a semantics change on
  identical input fails closed with `ACCEPTED_RECEIPT_REEVALUATION_REQUIRED`.
  Receipt reuse reports the current canonical generation in
  `RuntimeResult.generation` and the historical commit generation in
  `reused_generation`.
- **Live Engine Status:** Blocked behind `require_real_engine_qualification()`.
  The final pre-real-engine gate additionally requires CI qualification
  evidence and branch protection (goal.md §6.7).
