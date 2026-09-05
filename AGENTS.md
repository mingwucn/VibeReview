# VibeReviewPaper Agent Guide

## Current boundary

The V1.5.1b Phase-0 contract is frozen. Do not redesign the scientific chain or
add functionality from later phases. Phase 0 consists only of enums, Pydantic
models, serialization, local validation, repository validation, errors, and
regression tests.

Do not add a database, web UI, workflow engine, multi-agent architecture,
Graphify integration, PDF parsing, retrieval execution, LLM calls, caching,
manuscript assembly, or citation rendering.

The scientific contracts must remain independent of any LLM provider or agent
engine. Future orchestration may select Codex, Agy, Kimi, OpenCode, or another
engine behind adapters, but engine-specific payloads and state must not enter
these persistent models.

## Commands

```bash
python -m pytest
```

All Python and serialized text is UTF-8. Keep repository validators pure and
independent of the current working directory.
