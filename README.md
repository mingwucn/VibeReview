# VibeReviewPaper

VibeReviewPaper is a Python foundation for evidence-constrained scientific
reviews. The repository currently contains:

- the frozen V1.5.1b scientific contracts and pure repository validators;
- immutable numbered generations, writer-locked canonical-ID allocation,
  freshness checks, crash recovery, and accepted-task receipts;
- provider-neutral task/proposal contracts and bounded immutable task bundles;
- a deterministic subprocess runner with output, process, filesystem, and
  credential-lifecycle controls;
- a Linux Bubblewrap backend with machine-local probe, conformance, and
  fail-closed qualification checks;
- a bounded provider-neutral outbound-request compiler and explicit offline
  engine adapter;
- a qualified DeepSeek REST construction boundary that remains unusable unless
  the caller supplies explicit authorization, a currently allowlisted model,
  a fixed-file credential lease, and matching machine-local qualification at
  both live checkpoints; and
- a generic external-Git library foundation that reads one exact pinned commit,
  imports approved Markdown into immutable content-addressed generation data,
  and returns noncanonical retrieval candidates with integrity ledgers.

This is not an operational scientific-review generator. Kimi, Agy, and Codex
live factories fail closed because their sterile authentication and tool
isolation have not been qualified. The task-driven scientific orchestration,
canonical evidence promotion, manuscript assembly, and publication workflow
remain future work. No live provider call is made by the normal or sandbox test
suites.

The hosted repository is currently private and GitHub Actions are disabled.
Workflow files in this tree therefore do not constitute hosted CI,
qualification, branch-protection, or release evidence. No Package C,
live-provider, or external-corpus operation is authorized by the repository's
current state.

Historical real-corpus pilot material is deliberately absent from this
public-safe source tree. Its redistribution authority was not established, its
canned semantic audits were not genuine evaluations, human scientific review
was not performed, and it is not publication eligible. Only synthetic fixtures
belong in Git.

## Safety boundary

Python owns scientific IDs, state transitions, transactions, task routing,
validation, receipts, and citation authorization. Engines return proposals;
they do not mutate canonical state. Contract-valid negative or uncertain
results such as `REJECT`, `UNCLEAR`, `UNSUPPORTED`, `OVERSTATED`, and
`PARTIALLY_SUPPORTED` are canonical outcomes and never trigger engine fallback.

Exact offsets and hashes prove where text came from. They do not prove that the
text semantically supports a claim. Raw text and graph retrieval therefore
produce runtime candidates only; promotion into canonical spans and evidence is
not part of the current library foundation.

External corpora, Git checkouts, operator selection/configuration, credentials,
review state, and generated output must remain outside this source tree. The
committed public-boundary checks inspect the selected revision's complete
reachable history and reject forbidden paths and Git modes. They do not by
themselves clear other advertised references. An administrator-held private
denylist scan across every advertised reference remains required before making
the repository public or publishing a release.

## Verification

The supported V1 environment is Linux and WSL2 with Python 3.11 through 3.13.
Native Windows support remains deferred because the writer lock uses POSIX
`fcntl`.

Run deterministic tests without private corpora, live providers, or Bubblewrap:

```bash
python -m pip install -e '.[test]'
python -m pytest -m "not external_engine and not requires_bwrap and not external_corpus"
```

Run the sandbox selection only on a prepared Linux host:

```bash
python -m pytest -m "requires_bwrap or sandbox_conformance"
```

The administrative sandbox commands are:

```bash
python -m vibereview.runtime.sandbox probe --json
python -m vibereview.runtime.sandbox qualify --network-policy deny
python -m vibereview.runtime.sandbox status
```

Qualification is machine-local and binds the exact executable, runtime code,
profile, platform capabilities, network policy, and executed conformance
report. A report or fingerprint from another host is not authority to run a
live engine. In particular, the `deny` example above cannot authorize the
DeepSeek REST adapter: that adapter requires a separately reviewed and
qualified `HOST` profile. `HOST` permits general host networking and is not an
endpoint allowlist, so qualifying it is not by itself operator authorization
to make a provider call. Current acceptance evidence belongs to the exact
reviewed commit and CI artifacts; this README intentionally records neither
fixed test counts nor a reusable machine fingerprint.

The separately marked `external_corpus` smoke test requires an explicit opt-in
and an operator-owned approved selection. It is read-only and makes no network
or engine call. Tests marked `external_engine` require separate explicit
authorization and are excluded from ordinary CI.
