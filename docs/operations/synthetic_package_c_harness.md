# Synthetic Package C engineering harness

## Status and authority

This document is the normative technical and operational guide for the
deterministic synthetic Package C harness. The harness proves that the existing
scientific contracts can be orchestrated, journaled, validated, packaged, and
compared on explicitly fictional local fixtures. It is test machinery, not an
operational scientific pilot.

The harness does not authorize a live provider, an operator corpus, model
spending, human scientific acceptance, publication, or public export. It must
not be extended into any of those activities without a separately approved
milestone. The frozen V1.5.1b scientific models, enums, and validators remain
provider-neutral and must not be redesigned to accommodate this harness.

The implementation is intentionally a fixed controller, not a general workflow
or DAG engine. Python owns task routing, validation, state transitions,
transactions, canonical identifiers, receipts, citation authorization, and
fallback decisions. Engines return proposals only.

## Supported boundary and non-goals

The supported harness has all of the following properties:

- exactly five selected, fictional papers in an immutable imported corpus;
- two or three distinct, bounded discovery documents;
- one concrete `MockEngine` instance for every `TaskType`;
- no engine fallback and no callback-backed pilot engine;
- deterministic text and graph candidate retrieval from the imported local
  corpus;
- a fixed scientific stage order enforced independently from the controller;
- immutable, hash-chained journal events and accepted-task receipts;
- bounded per-attempt and cumulative execution accounting;
- exact, deterministic assembly and independent validation statuses;
- a closed, offline-verifiable, explicitly nonpublication packet; and
- bounded, ID-independent comparison of two to four such packets.

The following are outside the boundary:

- live DeepSeek, Kimi, Agy, Codex, OpenCode, or other provider execution;
- real or operator-owned corpus use;
- provider selection, model shopping, or fallback after a valid scientific
  result;
- PDF parsing, retrieval expansion, Graphify integration, a database, web UI,
  generic workflow engine, or multi-agent scientific orchestration;
- human review, publication eligibility, citation rendering for publication,
  or public export; and
- treating a synthetic validation or reproduction result as a scientific
  benchmark or reproducibility claim.

## Public API and source of truth

The harness is a Python API. There is no supported Package C CLI. The public
facades export the following lifecycle entry points:

From `vibereview.runtime`:

- `build_synthetic_pilot_run_manifest`
- `compute_package_c_implementation_fingerprint`
- `validate_synthetic_pilot_run`
- `build_synthetic_pilot_packet`
- `write_synthetic_pilot_packet`
- `load_verified_synthetic_pilot_packet`
- `verify_synthetic_pilot_packet`
- `compare_synthetic_pilot_reproductions`
- `collect_pilot_task_usage`
- `bind_pilot_task_execution_accounting`
- `build_pilot_task_usage`

From `vibereview.library`:

- `register_synthetic_pilot_setup`
- `SyntheticPilotController`
- `SyntheticPilotControllerResult`

`build_synthetic_pilot_review_packet`,
`write_synthetic_pilot_review_packet`, and
`verify_synthetic_pilot_review_packet` are discoverability aliases for their
corresponding pilot-packet functions. `compare_synthetic_pilot_packets` is an
alias for `compare_synthetic_pilot_reproductions`; there is no separate
review-packet alias for the retained-value loader.

The complete executable example is
[`tests/library/test_pilot_controller_evidence_e2e.py`](../../tests/library/test_pilot_controller_evidence_e2e.py).
It is the reference for constructing fictional inputs and scripted responses.
Examples in this document describe control flow; they are not authorization to
replace the mock engines or fixture corpus.

## Serialized contract versions

Serialized artifacts are closed models with extra fields forbidden. Their
versions must be treated as exact protocol identifiers, not approximate release
labels.

| Artifact family | Exported or serialized version |
| --- | --- |
| Controller | `PILOT_CONTROLLER_VERSION = "1"` |
| Setup result | `package-c-pilot-setup-1` |
| Run manifest | `package-c-pilot-run-1` |
| Registration | `package-c-pilot-registration-1` |
| Journal family | `PILOT_JOURNAL_VERSION = "2"` |
| Stage record | `package-c-stage-1` |
| Stage artifact | `package-c-stage-artifact-3` |
| Current-head index | `package-c-pilot-current-head-1` |
| Task usage | `package-c-pilot-task-usage-2` |
| Validation report | `package-c-validation-1` |
| Packet | `package-c-synthetic-pilot-packet-2` |
| Reproduction report | `package-c-pilot-reproduction-1` |

The version identifiers are intentionally independent. For example, the
journal-family version and stage-artifact schema version do not have to share a
number.

There is no compatibility pilot and no implicit serialized-artifact migration.
The packet verifier rejects the superseded packet-v1 manifest. Usage-v1 or
otherwise incompatible artifacts must not be edited and rehashed to look
current. Regenerate a packet from a currently authenticated compatible review
project, or recreate the disposable synthetic run from its known fictional
inputs under the exact reviewed checkout.

## Filesystem and privacy boundary

The public source repository, review project, corpus/library checkout, discovery
resources, and packet destination are distinct concepts.

- The review project must be outside this Git repository. Its `state/`,
  `work/`, journal, task bundles, attempts, cache, and locks are runtime state.
- Discovery resources and imported corpus data must come from explicitly
  allowed roots outside the public repository.
- `register_synthetic_pilot_setup` requires `review_root` and
  `public_repository_root` to be disjoint.
- `write_synthetic_pilot_packet` requires its destination to be outside both
  the review root and public repository root.
- Packet and review writers retain directory descriptors, reject unsafe path
  rebinding, and do not follow symlink substitutions.
- Reproduction reports and any derived diagnostics are runtime outputs and
  remain outside Git.

A pilot packet is self-contained because it embeds the five exact raw Markdown
objects needed to replay locators. It also contains generated section text,
canonical repository state, receipts, journal artifacts, and validation
diagnostics. `synthetic_fixture=true`, sanitized task provenance, and the
built-in obvious-secret scan do not make those bytes suitable for Git,
publication, or public release. The secret scan is a narrow defense-in-depth
check, not a data-classification or redistribution review.

## Lifecycle overview

The supported lifecycle is:

1. Create a fictional five-paper external library and import exactly one pinned
   selection into a fresh external review project.
2. Construct `ProjectRuntime` with receipts enabled, zero fallback engines, a
   finite technical-attempt count, and explicit allowed source roots.
3. Construct one immutable scripted `MockEngine` for every `TaskType`.
4. Call `build_synthetic_pilot_run_manifest` without mutating the project.
5. Call `register_synthetic_pilot_setup` to atomically register the manifest
   and deterministic run facts.
6. Construct `SyntheticPilotController` with that exact runtime, manifest,
   registration, and engine map.
7. Execute the fixed stage sequence below. Every accepted semantic event
   commits its domain changes, receipt, usage, and journal event together.
8. Run exact assembly and the validation-report control stage.
9. Write a packet to a protected external destination and verify it offline.
10. Repeat the independently created fictional run only when reproduction
    comparison is required, then compare two to four verified packets.

No step may replace a valid negative or uncertain scientific outcome with a
second engine merely to seek a preferred result.

## Manifest construction

`build_synthetic_pilot_run_manifest` is read-only. It performs no engine call
and writes no state. It freezes all inputs needed to decide whether a later
controller is still executing the registered run:

- run identifier, topic, timestamp, and CURRENT source generation;
- corpus-lock and selection-manifest hashes;
- five canonical paper source hashes in paper-ID order;
- two or three distinct discovery-resource content hashes in supplied order;
- a complete role map for every `TaskType`;
- each task's compiled prompt and input/proposal-schema fingerprints;
- the repository validator fingerprint;
- the Package C implementation fingerprint; and
- the exact `FivePaperPilotBudget`.

The implementation fingerprint uses checkout-independent relative paths. It
hashes Python sources under the `vibereview` package, runtime and library
modules, prompt files, and the generated task schemas. A code, prompt, or
schema change therefore invalidates an old manifest even if the checkout path
is unchanged.

Covered Python files are hashed byte-for-byte, so a source comment or docstring
change also changes the implementation fingerprint and intentionally prevents
an older manifest from resuming under that checkout. Markdown-only edits
outside the hashed package and prompt paths do not alter this fingerprint.

The engine role binding includes the sanitized engine name/version and the hash
of its safe configuration. For a `MockEngine`, that configuration includes the
technical-attempt limit, the exact ordered response-script fingerprint, and
whether a callback exists. Script payloads themselves are not exposed through
the safe configuration.

Manifest construction fails before mutation when fallback is enabled, the role
map is incomplete, a role is not a `MockEngine`, the corpus is not the exact
locked five-paper corpus, a source exceeds its byte budget, a discovery path is
outside the allowed roots, or discovery contents are duplicated.

## Setup registration

`register_synthetic_pilot_setup` accepts only a source generation containing
the five locked papers and no other scientific collection. The canonical ID
registry must exactly cover that paper-only snapshot.

Setup owns one writer-locked generation transaction. It creates or verifies:

- one corpus fact stating that the synthetic corpus contains exactly five
  selected papers;
- one process fact stating that the run is bounded and non-exhaustive;
- one process fact stating that no authorized human review or publication
  sign-off occurred;
- canonical run-manifest bytes; and
- a content-addressed registration artifact binding those facts and bytes.

An exact repeated setup returns the original setup identity even if CURRENT
later advanced. A different or stale setup cannot reuse it. A pre-CURRENT crash
is recovered through `GenerationStore` and must leave no partially visible
setup.

## Fixed controller phases

The controller exposes semantic methods, but the independent sequence validator
defines the accepted ordering and closure rules. The phase number is monotonic;
later work cannot begin while required work in an earlier phase is incomplete.

| Phase | Stage | Controller operation and closure rule |
| --- | --- | --- |
| 0 | `prerequisites` | `record_prerequisites()` must be the first completed control event. |
| 1 | `discovery` | `parse_deep_research()` uses the exact ordered discovery resources registered in the manifest. |
| 2 | `corpus_challenge` | `corpus_challenger()` consumes the authenticated discovery artifact. |
| 3 | `discovery` | `generate_candidate_claims()` consumes both discovery and challenge artifacts. |
| 4 | `query_generation` | `generate_retrieval_queries()` produces the fixed required query intents for each claim. |
| 5 | `retrieval` | Python runs the bounded retrievers; `record_retrieval()` records the exact complete ledger set as a control event. |
| 6 | `evidence_assessment` | `assess_evidence()` assesses selected candidates. Every selected candidate must be accounted for before claim aggregation. |
| 7 | `claim_aggregation` | `aggregate_paper_evidence()` is repeated for applicable claim/paper groups, then `assess_claim()` closes each claim's complete evidence partition. |
| 8 | `claim_validation` | Decision-dependent `revise_claim()` and `validate_final_claim()` transitions must be correctly paired; a rejected claim is terminal for that claim. |
| 9 | `proposition_audit` | `generate_propositions()` creates a draft batch; every item in a batch must be handled by `audit_proposition()` before a repair batch or later phase. |
| 10 | `sentence_audit` | `render_prose()` creates a sentence draft batch; every item must be handled by `audit_rendered_sentence()` before a repair batch or assembly. |
| 11 | `exact_assembly` | `exact_assembly()` performs no generative transformation and records exact body and assembly bytes in a control event. |
| 12 | `validation_report` | `validation_report()` must immediately follow exact assembly and records the independent validation statuses and diagnostics. |

Repeated per-item phases are bounded by the registered candidate, evidence,
claim-paper, proposition, repair, sentence, and invocation ceilings. Singleton
prefix tasks and control stages may not be repeated. An event with `BLOCKED` or
`FAILED` status is terminal and must be the journal head.

`REJECT`, `UNCLEAR`, `UNSUPPORTED`, `OVERSTATED`, and
`PARTIALLY_SUPPORTED` are contract-valid scientific state. They may block a
later transition, but they are not engine failures and must never cause
fallback or model shopping. A non-`ENTAILED` sentence audit remains an
immutable generation-owned audit artifact and accepted receipt with
`canonicalized=true`; it does not allocate a canonical rendered-sentence/audit
pair.

## MockEngine contract

The pilot accepts exactly the concrete `MockEngine` type for every role. Engine
identities must match these patterns:

- name: `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`
- version: `^[A-Za-z0-9][A-Za-z0-9._+:-]{0,127}$`, or `None`

`MockEngine` deep-validates each `MockResponse`, canonicalizes the complete
ordered script to immutable bytes, and computes `script_fingerprint` from the
initial script. Later mutation of a caller-owned proposal or a value returned
by `scripted_responses` cannot change execution or the fingerprint.

Each call deserializes a fresh response from the immutable backing bytes. An
empty script returns a deterministic execution failure. After a nonempty script
is exhausted, subsequent calls reuse its final response. `MockResponse` bounds
raw output and proposal data at 4 MiB and each retained stdout/stderr value at
1 MiB before the narrower run budget is applied.

The general `MockEngine` test seam supports `on_execute`, but the synthetic
pilot rejects any callback-backed engine. `restore_calls()` executes no
response and is valid only on a pristine, callback-free engine. The controller
may use it only after authenticating the exact engine plan and reconstructing
the expected cursor from journaled attempt usage.

## Cache, receipts, and idempotency

The ordinary runtime semantic cache cannot be an execution witness for the
synthetic pilot. During one controller call, `_SyntheticPilotCacheGate` bypasses
cache reads and writes. It does not delete a pre-existing cache entry and does
not disable cache behavior for unrelated runtime calls.

Accepted-task receipts remain the idempotency authority. Repeating an exact
accepted input with unchanged semantics:

- revalidates the immutable receipt and its canonical-object witnesses;
- does not execute the engine;
- does not create a generation or duplicate journal event;
- returns the current canonical generation; and
- retains the original commit generation as reuse provenance.

A changed input, prompt, schema, validator, promotion/disposition handler,
engine plan, script, or other semantic fingerprint is not an exact replay and
must not silently reuse the receipt.

## Usage-v2 and budget accounting

`PilotTaskUsageArtifact` is a sanitized generation-owned witness. It records
only bounded counts, outcomes, provider-neutral engine identity, and content or
semantic hashes. It does not serialize engine output, diagnostic text,
execution arguments, private source paths, or workspace paths.

`collect_pilot_task_usage()` authenticates every retained attempt artifact and
returns an unbound intermediate with `task_request_bytes=0` and
`task_elapsed_seconds=0`. It allows exactly one missing `attempt_record.json`:
the accepted attempt currently being materialized inside its successful
generation transaction. Every earlier failed-attempt record must exist.

`bind_pilot_task_execution_accounting()` attaches the controller-owned request
and elapsed measurements. Both values must be positive integers; booleans and
zero are rejected. `build_pilot_task_usage()` performs collect, bind, and
canonical serialization, and therefore requires both values.

The controller computes:

- `task_request_bytes` as the exact byte size of all immutable files declared
  by task provenance, multiplied by the actual retained attempt count; and
- `task_elapsed_seconds` as one monotonic task duration, rounded up to a whole
  second with a minimum value of one.

Every failed and accepted attempt contributes its output bytes, imported
proposal bytes, stdout, stderr, retained diagnostics, writable-entry count, and
writable-tree bytes. The usage model structurally supports at most 128 attempts,
with aggregate structural ceilings of 512 MiB for output, 512 MiB for imported
proposals, 524,288 writable entries, and 32 GiB of writable-tree bytes. The
registered `FivePaperPilotBudget` is deliberately much narrower and is always
the operative acceptance limit.

The journal counter named `proposal_bytes` charges total `AgentResult` output
bytes. The usage artifact additionally retains exact imported-proposal byte
counts and hashes, and both output and imported proposal sizes are checked
against the registered proposal budget.

`collect_pilot_task_usage(..., enforce_budget=False)` is reserved for a
controller that has already rejected semantic publication and needs a bounded
terminal witness explaining the overrun. It is not a general budget bypass.
Accepted semantic events must have no usage overruns. An observed technical,
attempt-resource, or task-time failure produces one terminal `FAILED` control
event with exact attempt usage. Positive overrun deltas are recorded only when
a configured limit was exceeded; published cumulative counters are capped at
their registered ceilings. A preflight budget refusal runs no engine and
publishes no terminal event.

`EngineUsageRecord` separately represents provider token, cache, price, and
cost metrics. `MockEngine` does not provide them, so all such fields remain
unset and `MOCK_USAGE_UNAVAILABLE_REASON` is required. This does not weaken the
exact byte, attempt, filesystem, diagnostic, request, or elapsed accounting.

The current default `FivePaperPilotBudget` is summarized below. The model in
`vibereview.runtime.pilot_records` remains the source of truth for validation
ranges and cross-field rules.

| Area | Default limits |
| --- | --- |
| Corpus and discovery | 5 papers; 2-3 discovery documents; 192 KiB per discovery document; 512 KiB discovery total; 2 MiB per paper; 8 MiB corpus total |
| Discovery and queries | 10 themes; 20 discovery leads; 5 claims; 4 query intents per claim; 20 queries; retrieval top-k 4 per backend |
| Retrieval and evidence | 160 raw candidates; 60 assessed candidates; 60 evidence records; 25 claim-paper evidence records |
| Drafts and audits | 12 initial propositions plus 6 repairs; 18 initial sentences plus 6 repairs |
| Engine and time | 128 semantic invocations; 180 seconds per task; 4 hours cumulative run time |
| Request and proposal | 512 KiB request budget; 1 MiB proposal/output budget |
| Per attempt | 64 KiB stdout; 64 KiB stderr; 128 KiB retained diagnostics; 256 writable entries; 16 MiB writable tree |
| Cumulative execution | 8 MiB stdout; 8 MiB stderr; 16 MiB retained diagnostics; 8,192 writable entries; 256 MiB writable tree |

## Journal integrity

The pilot journal is generation-owned and hash-chained. It is not the source of
a generic scheduler. Each event:

- has a consecutive ordinal and owns exactly the next generation;
- binds the registered manifest and registration artifact;
- names its exact predecessor by run, ordinal, stage, owner generation, record
  hash, artifact hash, and content-addressed path;
- carries monotonic cumulative budget counters; and
- updates the generation-owned `current_head.json` index atomically.

A completed semantic event additionally binds the sanitized task provenance,
accepted receipt, declared engine plan, domain-artifact hashes, and exactly one
task. These values cross the same generation commit as the scientific state
and registry changes.

Python-owned control stages write typed control artifacts. Completed
prerequisites, retrieval, exact-assembly, and validation control events do not
change scientific state or the ID registry, but they intentionally own a new
no-op generation so their evidence and head advance atomically. Exact repeated
control input may reuse an already committed identical event.

The sequence is bounded at 128 events. A completed event cannot carry a failure
reason; a blocked or failed event must carry one and terminates the sequence.
Loading a head reauthenticates its canonical bytes, registration, manifest,
predecessor chain, CURRENT owner generation, control coverage, receipts, and
domain artifacts.

## Restart and recovery matrix

The controller writes
`work/tasks/<task-id>/private/synthetic_pilot_controller.json` after validating
task provenance and before any engine attempt can start. The bounded,
read-only marker binds the run ID, manifest hash, registration hash, task ID,
task type, and task-provenance hash.

On construction and before later work, the controller discovers the
authenticated CURRENT head, validates the fixed sequence, loads attempt usage
from accepted and failed events, reconciles every marked pilot task directory,
and compares each engine cursor with the journal-derived count.

| Condition | Supported behavior |
| --- | --- |
| Clean restart after journaled events | Recreate the exact immutable MockEngine scripts. A pristine matching engine cursor is restored from authenticated journal counts, and the run may continue. |
| Lost caller response after a committed semantic event | Head discovery plus accepted-receipt reuse returns the committed result without engine execution or a new generation. |
| Lost caller response after an exact control event | The content-addressed control event is discovered and exactly reused. |
| Crash within a generation transaction before CURRENT | `GenerationStore.recover()` reconciles or removes staging and incomplete generations according to the immutable generation protocol; partial canonical state is not exposed. |
| CURRENT advanced without the registered exact head | Fail with stale-state error before engine execution. |
| Engine cursor differs from authenticated journal accounting | Fail closed before engine execution. Do not skip or rewind responses manually. |
| Marked task has durable attempts absent from journal accounting | Fail closed with `unjournaled pilot task attempts require recovery` before any engine call. There is no public in-place repair API. Preserve the workspace for diagnosis and recreate the disposable synthetic review project from its known inputs when a clean rerun is required. |
| Marker, provenance, or journaled attempt-ID set differs on restart | Fail closed; do not delete or rewrite the witness to force continuation. |

Initial usage collection authenticates and rechecks each retained stream,
proposal, attempt record, and content hash before the immutable usage artifact
is committed. Later restart reconciliation treats that generation-owned usage
artifact as authoritative and compares the retained workspace's attempt-ID set;
it does not claim to rehash every already-accounted workspace byte.

Recovery never authorizes fallback, suppresses a valid scientific result, or
reconstructs missing scientific semantics from unauthenticated files.

## Independent validation

`validate_synthetic_pilot_run` holds the repository writer lock while
authenticating registration and CURRENT, running checks, and reauthenticating
the same state. It issues six independent statuses, each `PASSED`, `FAILED`, or
`NOT_EXECUTED`:

- `structural_validation`
- `locator_verification`
- `semantic_audits_executed`
- `citation_authorization`
- `exact_assembly`
- `artifact_integrity`

A missing applicable object can correctly produce `NOT_EXECUTED`; validation
does not turn absence into success. The report always records
`human_review_status=NOT_PERFORMED`, `synthetic_only=true`, and
`publication_eligible=false`.

## Packet creation and offline verification

`write_synthetic_pilot_packet` authenticates the exact live CURRENT review,
then atomically creates a new packet directory or exactly reuses an identical
existing directory. It never overwrites a different packet. It revalidates
retained review, public-repository, destination, CURRENT, snapshot, registry,
receipts, registration, manifest, and head anchors before accepting the write.

The packet's closed inventory contains at least:

- exact section and assembly bytes;
- canonical repository and registry state;
- accepted receipts;
- run manifest and registration;
- complete journal and referenced domain artifacts;
- corpus lock, import manifest, and selection manifest;
- exactly five content-addressed raw Markdown objects;
- validation report and bounded diagnostics; and
- an explicit human-review record fixed to `NOT_PERFORMED` and
  `publication_eligible=false`.

Packet-v2 verification requires exactly one task-usage-v2 domain artifact for
each accepted semantic event. It verifies canonical bytes, content and semantic
hashes, accepted attempt, proposal receipt, engine identity, budget hash,
positive request/time values, and the exact delta from the preceding journal
budget. Rehashing altered usage cannot make inconsistent accounting valid.

The packet has a maximum of 4,096 payload files, 4,096 accepted receipts, and
256 MiB total payload bytes. Its flat file inventory is sorted, unique, and
exact; journal ordinals are closed and domain/corpus files are content
addressed.

`verify_synthetic_pilot_packet` uses only the packet directory and embedded
witnesses. Verification remains possible after the source review project is
moved or removed. It verifies the complete sequence and requires passed
structural validation, exact assembly, and artifact integrity. Locator and
citation checks may be `NOT_EXECUTED` only when no corresponding canonical spans
or citation bindings exist; a present span or binding requires its check to
pass. Verification never upgrades human review or publication eligibility.

## Reproduction comparison

`compare_synthetic_pilot_reproductions` accepts exactly two to four independently
verified packet directories. Before comparison it requires an identical
normalized run identity. Normalization excludes only run labels,
time/generation coordinates, and canonical IDs. It retains the exact topic,
corpus and discovery identities, engine plan, prompt and schema fingerprints,
validator and implementation fingerprints, budget, synthetic status, and
nonpublication status.

Canonical IDs are replaced only in known identity/reference positions. Literal
text that happens to resemble an ID is not rewritten. Set-like references are
normalized deterministically; exact text hashes, locator hashes, source graph,
and assembled sentence order remain observable. The report separates subjects
into ordered `agreements` and `differences` and applies no acceptance threshold,
score, or scientific verdict. It always records `human_review=NOT_PERFORMED`
and `publication_eligible=false`.

## Verification and required evidence

Run the focused contract and controller suite without a provider or external
corpus:

```bash
python -m pytest -q \
  tests/runtime/test_mock_runtime.py \
  tests/runtime/test_package_c_*.py \
  tests/library/test_pilot_setup.py \
  tests/library/test_pilot_controller.py
```

Run the slower full evidence-to-packet reproduction separately:

```bash
python -m pytest -q \
  tests/library/test_pilot_controller_evidence_e2e.py
```

Run the ordinary deterministic repository suite:

```bash
python -m pytest \
  -m "not external_engine and not requires_bwrap and not external_corpus"
```

Run the sandbox/conformance selection only on a prepared Linux host:

```bash
python -m pytest -m "requires_bwrap or sandbox_conformance"
```

Run the committed public-boundary regressions and candidate-tree guard:

```bash
python -m pytest -q \
  tests/library/test_public_boundary.py \
  tests/test_public_repository_policy.py

PYTHONPATH=src python -m vibereview.library.public_guard .
git diff --check
```

Before publication or release, an administrator must additionally scan every
provider-advertised, already-local fully qualified ref with the private denylist
procedure in
[`docs/security/public-data-boundary.md`](../security/public-data-boundary.md).
The private scan, hosted CI configuration, runner qualification, and branch
protection are administrative gates that local tests cannot satisfy.

Acceptance evidence must name the exact commit, commands, environment, marker
selection, and result. Do not hard-code transient pass counts, machine
qualification fingerprints, or a claim that a local run constitutes hosted CI.

## Change checklist for future implementation work

Any change to this harness must preserve all of the following:

1. Update `goal.md` and this guide with the authorized scope, invariants,
   compatibility effect, and acceptance evidence before implementation; keep
   code and tests traceable to that documented contract.
2. Keep the scientific models, enums, and validators engine-neutral and frozen
   unless a demonstrated contract defect has a focused regression.
3. Do not add a provider, external corpus, spending path, human acceptance, or
   publication/export feature under the synthetic milestone.
4. Keep valid negative and uncertain dispositions canonical; never make them a
   fallback condition.
5. Keep canonical ID allocation, scientific state, registry, receipts, domain
   artifacts, usage, and journal evidence inside the appropriate writer-locked
   transaction.
6. Keep cache bypass scoped to pilot calls and receipt reuse independently
   authenticated.
7. Preserve fail-closed restart reconciliation and never infer journal
   accounting from an unauthenticated orphan attempt.
8. When a serialized shape changes, update its explicit schema version, packet
   verification, compatibility statement, public exports, and focused tests
   together.
9. Run the focused tests before the full ordinary and sandbox selections. Do
   not advance the milestone boundary while a required test fails.
10. Keep all runtime and packet outputs outside Git and rerun the public-history
   gates before any push intended for release or publication.
