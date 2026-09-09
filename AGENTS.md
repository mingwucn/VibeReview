# VibeReviewPaper Agent Guide

## Current boundary

The V1.5.1b scientific contract is frozen. Do not redesign its models, enums,
or validators without a demonstrated contract defect and a focused regression.

The implemented runtime boundary includes immutable generations, task/proposal
contracts, fallback and freshness policy, self-contained task-resource bundles,
the deterministic subprocess runner, process/filesystem/output bounds,
credential leases, machine-local Bubblewrap conformance qualification, and
accepted-task receipts. The public integration foundation additionally
provides:

- a bounded provider-neutral outbound-request compiler that includes every
  manifest-listed instruction, schema, engine input, dependency, and resource;
- an explicit deterministic offline engine;
- a fail-closed qualified DeepSeek REST construction boundary with fixed-file
  credential delivery and no redirects or unbounded response reads;
- unavailable Kimi, Agy, and Codex live factories pending machine-local
  capability, sterile-authentication, and tool-secret isolation evidence; and
- a generic pinned-Git external-library inventory/import layer plus raw text and
  graph candidate retrieval that allocates no canonical evidence IDs.

A deterministic, synthetic-only Package C engineering harness exercises the
existing task contracts with `MockEngine`: structured discovery and challenge,
bounded retrieval ledgers, writer-locked coupled span/evidence promotion,
claim and draft transitions, semantic audits, exact assembly, immutable pilot
journals, offline packets, and reproduction comparison. It is test machinery,
not an operational scientific pilot. It does not authorize an external corpus,
a live provider, model spending, human scientific acceptance, publication, or
public export. Do not add a database, web UI, generic workflow/DAG engine,
multi-agent scientific architecture, Graphify integration, PDF parsing, or an
unqualified provider path.

Because the frozen repository validator permits canonical rendered sentences
only for `ENTAILED` sentence audits, other contract-valid sentence verdicts are
retained as immutable generation-owned audit artifacts and accepted receipts
with `canonicalized=true`. They are authoritative blocking run state, not
fallback triggers, and allocate no canonical `RenderedSentence`/audit pair.

Historical real-corpus artifacts are private, invalidated, and absent from this
repository. Never restore them, their source identifiers, or derivative prose
to Git. Synthetic fixtures must be clearly fictitious. Public-history checks
and an administrator-held denylist are release gates, not optional lint.

Python owns canonical IDs, state, transactions, task routing, validation,
receipts, and citations. Provider-specific state must not enter scientific
models. Every contract-valid scientific result is canonicalized, including
`REJECT`, `UNCLEAR`, `UNSUPPORTED`, `OVERSTATED`, and
`PARTIALLY_SUPPORTED`; these outcomes may block downstream transition but must
never trigger fallback or model shopping.

## Environment

Linux and WSL2 are supported. Native Windows remains deferred because the
writer lock uses `fcntl`. All Python and serialized text is UTF-8. Keep
repository validators pure and independent of the current working directory.

Tests requiring a live external provider must be marked `external_engine` and
require an explicit opt-in in addition to credentials. Tests requiring an
operator-owned corpus must be marked `external_corpus`. Bubblewrap tests use
`requires_bwrap` or `sandbox_conformance`.

## Commands

```bash
python -m pytest -m "not external_engine and not requires_bwrap and not external_corpus"
python -m pytest -m "requires_bwrap or sandbox_conformance"
```
