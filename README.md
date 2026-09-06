# VibeReviewPaper

VibeReviewPaper is a Python application for producing evidence-constrained
scientific reviews. The repository contains the frozen scientific contracts and
the Python runtime through the deterministic MockEngine milestone:

- strict Pydantic scientific models and repository validation;
- immutable, numbered repository generations and atomic `CURRENT` updates;
- writer locking, transactional canonical-ID allocation, and crash recovery;
- immutable task snapshots, proposal DTOs, freshness checks, and attempt logs;
- validator-aware semantic cache primitives and bounded fallback policy;
- a replaceable `AgentEngine` protocol with `MockEngine` only.

## Development

The supported V1 execution environment is Linux and WSL2. Native Windows
support is deferred because the project writer lock uses `fcntl`.

Use Python 3.11 or newer. Install the package with its test dependencies and
run the Phase-0 gate:

```bash
python -m pip install -e '.[test]'
python -m pytest
```

The future user-facing entry point will be:

```bash
python vibe_review.py run reviews/<project>
```

That CLI, real-engine adapters, and all document-processing phases remain out of
scope. The contracts are engine-neutral so later Python orchestration can choose
among different bounded LLM workers without changing scientific provenance.

Contract-valid negative or uncertain results are canonical state. Fallback is
reserved for engine execution, format, schema, or proposal-validation failures;
it is never used to seek a preferred scientific conclusion.
