# VibeReview Runtime Handoff

The active milestone is **Milestone B3 (Pre-Real-Engine Hardening Release)**.
All seven work packages (WP1–WP7) are implemented, verified, and passing 706/706 tests.

For detailed historical and milestone handoffs, see:

- **Milestone A (Task-Resource Boundary, r3):** [`docs/handoffs/r3-task-resources.md`](docs/handoffs/r3-task-resources.md)
- **Milestones B1 + B2 (Deterministic Subprocess Runner, r4):** [`docs/handoffs/r4-b1-b2.md`](docs/handoffs/r4-b1-b2.md)
- **Milestone B3 (Pre-Real-Engine Hardening Release, r5):** [`docs/handoffs/r5-pre-real-engine.md`](docs/handoffs/r5-pre-real-engine.md)

## Quick Verification

```bash
# Standard deterministic test matrix (excludes live engine and bwrap tests)
python -m pytest -m "not external_engine and not requires_bwrap"

# Sandbox conformance suite (requires Linux bubblewrap)
python -m pytest -m "requires_bwrap or sandbox_conformance"

# Full local suite
python -m pytest
```

## Current State

- **Scientific Contract Version:** V1.5.1b (frozen)
- **Runtime Contract Version:** 1.6
- **Live Engine Status:** Blocked behind `require_real_engine_qualification()`. Milestone C (`CodexEngine`) is deferred.
