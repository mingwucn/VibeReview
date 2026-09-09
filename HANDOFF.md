# VibeReview Runtime Handoff

The current integration boundary is a public-safe foundation plus a
deterministic synthetic Package C engineering harness, not a completed or
operational scientific pilot.

The neutral public-safe root snapshot is now `origin/master` in a private
hosting repository. GitHub Actions are disabled. Provider-managed historical
review references remain retained by owner decision and must be considered in
any later repository-wide publication review.

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
boundary is documented in `docs/security/public-data-boundary.md`.
