# VibeReviewPaper Agent Guide

## Documentation-first workflow

Before implementing a new milestone or materially changing an existing
boundary, update `goal.md` and the applicable normative document under `docs/`
with the authorized scope, invariants, non-goals, serialized compatibility,
and acceptance evidence required. Implementation and tests must trace to that
documented contract. Record exact verification in `HANDOFF.md` before pushing.

Documentation is not authorization for a live provider, external corpus,
spending, publication, or a scientific-contract redesign. Those actions still
require the explicit gates below.

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

The review-prompt foundation additionally provides an immutable versioned
prompt registry with released-prompt hash verification (`prompts/registry.yaml`
plus 20 released prompts and 10 released focus modules under
`src/vibereview/prompts/`), a deterministic prompt compiler with per-compile
manifests, external review-project artifact versioning, and manual Deep
Research run registration (`vibereview.prompting`). Its normative contract is
[docs/operations/review_prompt_protocol.md](docs/operations/review_prompt_protocol.md).
It is provenance machinery only: it never creates canonical evidence and
authorizes no live provider, external corpus, spending, publication, or
prompted run. The P5–P12 stages of `goal.md` require separate operator
approval.

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

Synthetic pilot calls must bypass semantic proposal-cache reuse while
preserving ordinary runtime cache behavior. `MockEngine` scripts must remain
canonical immutable snapshots; engine names and versions must remain sanitized
and manifest-bound. A pristine cursor may be restored only from authenticated
journaled attempt counts.

The controller must persist a provenance-bound marker before an attempt starts.
Marked attempts without accepted or terminal-failure journal accounting are
fail-closed recovery state. Do not silently replay, delete, or fabricate
accounting for them.

Treat `package-c-pilot-task-usage-2` and
`package-c-synthetic-pilot-packet-2` as the current synthetic formats.
Persisted task usage must bind positive request-byte and elapsed-time
accounting, and packet verification must reconcile those deltas against the
cumulative journal. Do not silently accept or upgrade version 1 artifacts. The
authoritative contract is the
[synthetic Package C harness guide](docs/operations/synthetic_package_c_harness.md).

The MyLib operational review program is authorized for Milestones 1–6 only by
[docs/operations/mylib_operational_review.md](docs/operations/mylib_operational_review.md):
real-submodule boundary verification, operator reconciliation aliases and
source-quality assessment, evidence-closure regression, retrieval-coverage and
semantic-benchmark machinery, and a deterministic offline-engine operational
pilot harness. It does not authorize a live provider, spending, human
scientific acceptance, publication, or public export; Milestones 7–12 of
`goal.md` require separate operator approval. Operator artifacts (selection
manifests, adjudication aliases, source-quality records, benchmark cases,
proposal bundles) live outside the public repository and carry explicit
format-version identifiers.

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
python -m vibereview.prompting.cli prompts verify
```
