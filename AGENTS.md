# VibeReviewPaper Agent Guide

## Current boundary

The V1.5.1b scientific contract and runtime handoff are frozen. The implemented
boundary ends after immutable generations, task/proposal contracts, fallback
and freshness policy, and MockEngine end-to-end tests. Do not redesign the
scientific chain or implement a real semantic engine without a new milestone.

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

## Commands

```bash
python -m pytest
```

All Python and serialized text is UTF-8. Keep repository validators pure and
independent of the current working directory.
