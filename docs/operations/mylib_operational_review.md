# MyLib operational review program

## Status and authority

This document is the normative authorization for the implementation slice that
operationalizes the existing VibeReview architecture against the real pinned
`mingwucn/MyLib` library, as planned in `goal.md`. It authorizes exactly the
following milestones and nothing more:

- Milestone 1 — verify the real pinned MyLib boundary.
- Milestone 2 — bibliographic reconciliation utilities and source-quality
  assessment.
- Milestone 3 — evidence-closure adversarial regression.
- Milestone 4 — retrieval-coverage framework and retrieval benchmark harness.
- Milestone 5 — semantic-benchmark machinery.
- Milestone 6 — deterministic five-real-paper vertical slice.

The frozen V1.5.1b scientific models, enums, and validators remain unchanged.
No new canonical model, enum value, or state transition is authorized by this
document. In particular, the frozen `RetrievalIntent` vocabulary
(`support`, `contradiction`, `boundary`, `alternative`, `method_challenge`,
`null_result`) already covers the adversarial retrieval intents required by
`goal.md` Phase R: `alternative` carries context/qualification-seeking leads,
`method_challenge` carries method-dependence and mechanism-challenge leads,
and `null_result` carries null-finding leads. Terminology-variant expansion is
a property of query text, not of the intent enum. Do not widen the enum.

This document does not authorize a live provider, model spending, human
scientific acceptance, publication, public export, or any change to MyLib
content. Milestones 7 through 12 of `goal.md` (live semantic pilot, adversarial
mini-review, manuscript slice, adversarial discovery loop, complete review
pilot, submission readiness) remain unauthorized until separately approved.
The synthetic Package C harness stays synthetic; its contract is
[`synthetic_package_c_harness.md`](synthetic_package_c_harness.md).

## Supported boundary and non-goals

The supported operational slice has all of the following properties:

- MyLib is consumed exclusively through the existing pinned-Git external
  library boundary (`vibereview.library.git_source`, `inventory`,
  `bibliography`, `graph`, `resolver`, `selection`, `retrieval`,
  `retrieval_promotion`). MyLib is read-only during every VibeReview
  operation; MyLib's own mutation tools (`pipeline/`, Graphify, LLM-backed
  `link_graph_to_bib.py`) are upstream maintenance and never run inside a
  VibeReview scientific execution.
- Every operational run uses an operator-held review configuration that lives
  outside the public repository (`vibereview.library.project_config`).
  `configs/libraries/mylib.toml` is the committed operator pin record; it is
  not a loadable review configuration.
- Project-level operational artifacts — outline versions, review protocol,
  source-quality assessments, corpus-coverage judgments, retrieval-coverage
  diagnostics, retrieval and semantic benchmark data, claim-normalization
  relationships, study-dependence notes — remain external project artifacts
  under the operator's review root. They do not enter canonical scientific
  models and they do not enter the public repository.
- Real-corpus tests are marked `external_corpus`, require the explicit
  `VIBEREVIEW_RUN_EXTERNAL_CORPUS=1` opt-in plus operator-supplied
  configuration, and assert that the pinned library is byte-identical before
  and after the run. Ordinary CI never touches `external/MyLib`.
- Semantic proposals in the Milestone 6 slice are manual or deterministic
  operator artifacts supplied from outside the repository. No live provider
  is constructed or called.

The following are outside this slice:

- live DeepSeek, Kimi, Agy, Codex, or any other provider execution;
- a second MyLib adapter, PDF parsing, OCR, Graphify execution, or any
  mutation of `external/MyLib`;
- canonical scientific-model changes, new `RetrievalIntent` values, a
  `CorpusSnapshot` model, or a universal numerical evidence-quality score;
- automatic Deep Research execution, automatic paper acquisition, a web UI,
  a database, a generic workflow/DAG engine, or multi-agent orchestration;
- publication eligibility, public export of real corpus-derived artifacts, or
  human scientific review recording; and
- treating retrieval or semantic benchmark numbers as proof of corpus-wide
  recall or scientific acceptance.

## Milestone contracts

### Milestone 1 — real MyLib boundary verification

Deliver an `external_corpus`-marked verification that, against the real
pinned MyLib:

1. proves configured commit = superproject gitlink = library `HEAD`;
2. proves every source read is served from verified pinned Git objects and
   that dirty worktree bytes are excluded;
3. inventories `Libs/*.md`, `ref.bib`, and the optional graph export;
4. enforces import and selection budgets; and
5. asserts the library state snapshot is identical before and after.

The documented inspection command in
[`docs/integrations/mylib.md`](../integrations/mylib.md) must match the
actual `vibereview.library.inspect` CLI, including its required
`--public-repository-root` argument.

Exit criterion: the real pinned MyLib is inspected and verified without
mutation and without scientific acceptance.

### Milestone 2 — reconciliation and source quality

Implement only the missing operator utilities:

1. Wire the existing manual-adjudication alias input
   (`resolver.resolve_source_mappings(aliases=...)`) into inspection and
   selection through an operator-supplied alias file held outside the
   repository, keyed by source content hash. Ambiguous or conflicting
   publication identity continues to fail closed.
2. Add a high-confidence normalized-match tier (normalized title/DOI
   equivalence) between the exact deterministic match and manual
   adjudication. Normalized matches are reported as advisory candidates;
   conflicting outcomes remain fail-closed.
3. Add a source-quality assessment utility producing, per selected paper, a
   project-level record with classification `READABLE`,
   `READABLE_WITH_ARTIFACTS`, `MATERIAL_EXTRACTION_PROBLEM`, or `UNUSABLE`,
   an assessor, an assessment time, and a rationale. The utility performs
   deterministic structural checks (title/heading presence, encoding and
   extraction-artifact heuristics, section coverage) as decision support;
   the recorded classification is the operator's. A
   `MATERIAL_EXTRACTION_PROBLEM` or `UNUSABLE` classification blocks
   selection of the affected source until MyLib is corrected and repinned;
   enforcement happens at the selection-manifest layer, not through a new
   canonical model.

Exit criterion: every selected pilot paper has one canonical publication
identity, one pinned Markdown source, one bibliographic alias where
available, and one recorded acceptable source-quality result.

### Milestone 3 — evidence-closure regression

Test first. Add adversarial regression fixtures parameterized over all five
evidence categories (supporting, contradictory, qualifying, contextual,
unclear) that attempt omission at each transition:
`EvidenceRecord` → `ClaimPaperEvidence` → `FinalClaimValidation` →
`ClaimPacket`. Exercise both the writer-locked promotion path and the
frozen repository validator.

If the existing contract rejects every omission, record that result and add
no contract change. If a concrete omission succeeds, document the defect,
apply the minimal validator correction, and land the focused regression in
the same change. Do not redesign the data model pre-emptively.

Exit criterion: closure behavior at all four transitions is pinned by
passing adversarial regressions, with any demonstrated defect minimally
fixed.

#### Recorded result — one demonstrated validator defect, minimally fixed

The adversarial regression lives in
`tests/library/test_evidence_closure_regression.py` (library-scoped because
the span → `EvidenceRecord` cells require the pinned-Git `synthetic_library`
fixture). The closure matrix resolved as follows:

- Retrieved span → `EvidenceRecord`: already enforced. Proposal decisions
  must equal the ordered invocation candidate refs
  (`library/evidence_task.py` adapter identity check), an assessed decision
  requires exactly one evidence payload (`runtime/dto.py`
  `EvidenceCandidateDecisionProposal`), and the frozen validator enforces
  assessed-span/evidence cardinality
  (`validators.py` `ASSESSED_SPAN_EVIDENCE_CARDINALITY`).
- `EvidenceRecord` → `ClaimPaperEvidence`: already enforced. The invocation
  must name exactly all current `EvidenceRecord` IDs for the claim and paper
  before any engine call (`runtime/specs.py`), the writer-locked proposal
  re-checks the exact set and the component-relation partition
  (`runtime/promotion.py` `_validate_claim_paper_evidence`), and relabeling
  a non-support category as `supports` is rejected against the canonical
  `EvidenceRecord` relation.
- `ClaimPaperEvidence` → `FinalClaimValidation`: already enforced. The
  `ASSESS_CLAIM` invocation must cover all current CPE IDs
  (`runtime/specs.py`), and the final-validation proposal's
  `paper_relations` must exactly cover every current CPE for the claim
  (`runtime/promotion.py` `_validate_final_claim`).
- `FinalClaimValidation` → `ClaimPacket`: already enforced on the promotion
  path. The packet is derived deterministically from the snapshot's complete
  CPE set (`runtime/promotion.py` `_claim_packet_for_validation`), so a
  proposal cannot omit a CPE from the packet, and the frozen validator
  already required packet CPE set == final paper-relation CPE set
  (`CPE_SET_CONTINUITY_MISMATCH`).

Demonstrated defect (validator only): the frozen repository validator
accepted semantically closed snapshots in which canonical evidence was
invisible to the validated chain — (a) a `ClaimPacket` and a `VALID`
`FinalClaimValidation` that agreed with each other while both omitted an
existing CPE of the claim (e.g. a contradicts CPE), and (b) a `ClaimPacket`
existing while an assessed `EvidenceRecord` of the claim belonged to no CPE.
Neither state is reachable through the writer-locked promotion path (CPE
changes are blocked once a `ClaimAssessment` exists, and new evidence
assessment is blocked once a CPE exists), but the frozen validator is the
commit-time gate and must reject them.

Minimal correction (no model, enum, or public-signature change): while a
`ClaimPacket` exists for a claim, `validators.py` now additionally requires
(1) the claim's `FinalClaimValidation` `paper_relations` to exactly cover
every current CPE for the claim (`FINAL_CPE_COVERAGE_MISMATCH`, inside
`_collect_claim_bundle`), and (2) every `EvidenceRecord` of the claim to
belong to a `ClaimPaperEvidence` (`UNAGGREGATED_EVIDENCE_AFTER_PACKET`, new
`_collect_packet_evidence_closure` collector invoked from
`validate_repository`). Both checks are gated on packet existence, so
legitimate intermediate states — assessed-but-unaggregated evidence and
partial aggregation with no packet — remain valid; the regression module
pins both directions.

### Milestone 4 — retrieval-coverage framework

Implement, as project-level utilities over the existing deterministic
retrievers and bounded ledgers:

1. a deterministic multi-intent query-plan helper that expands a candidate
   claim into the required per-claim intents
   (`support`, `contradiction`, `boundary`, `alternative`) plus optional
   `method_challenge` and `null_result` intents, using the existing query-ID
   and ledger contracts;
2. a retrieval-coverage diagnostic report computing, per claim and in
   aggregate: intents executed, distinct query count, paper diversity,
   terminology diversity, per-intent candidate counts, duplicate-result
   rate, and known-paper/known-passage recovery against operator fixtures;
3. a retrieval-benchmark harness consuming a small curated case file
   (synthetic cases in tests; real adjudicated cases outside the
   repository) with known support, contradiction, qualification, null,
   boundary, terminology-variant, and full-text-only examples, reporting
   known-paper recall, known-passage recall, and per-intent recall; and
4. an operational status judgment of `ADEQUATE_FOR_SYNTHESIS`, `PARTIAL`,
   or `INADEQUATE`, recorded as an external project artifact. It is a
   diagnostic, never proof of exhaustive recall, and never a canonical
   scientific state.

Retrieval-coverage assessment remains independent from evidence closure and
from corpus coverage.

Exit criterion: retrieval adequacy is measurable independently from
evidence closure, demonstrated on synthetic fixtures in the ordinary test
suite.

### Milestone 5 — semantic-benchmark machinery

Implement the harness only:

1. a serialized adjudicated case format for claim–passage pairs
   (adjudicated label, optional per-reviewer labels, retained disagreement,
   development/evaluation split marker);
2. a runner that evaluates an evidence-assessment engine (the offline or
   mock engine in tests; a live engine only under Milestone 7's separate
   authorization) against a case file; and
3. a metrics report with per-class precision and recall, a confusion
   matrix, false-support rate, contradiction recall, and qualification
   recall.

The 30–100 real adjudicated pairs are human work product and live outside
the repository; they are not fabricated by this milestone. Tests use small
synthetic case files. Benchmark cases used for prompt tuning must not be
the sole evaluation set; the split marker makes that auditable.

Exit criterion: the machinery computes correct metrics on synthetic
fixtures, so the semantic engine's principal error modes become measurable
once adjudicated cases exist.

### Milestone 6 — deterministic five-real-paper vertical slice

Implement an `external_corpus`-marked operator harness that composes the
existing boundaries — pinned import, deterministic retrieval, coupled
evidence promotion, claim aggregation, final claim validation — against
five real MyLib papers selected and frozen before execution. Semantic
proposals (claims, query plans, evidence assessments, claim assessments,
final validations) are deterministic operator artifacts held outside the
repository; the offline engine and existing promotion contracts remain the
only execution path. No live provider is constructed. The harness asserts
the exact provenance chain: every promoted span resolves to an exact pinned
MyLib source slice, and the library state snapshot is unchanged by the run.

Exit criterion: real scientific source spans survive the complete
provenance chain independently of live-model behavior.

## Invariants preserved across the slice

1. Python owns canonical IDs, state, transactions, task routing, validation,
   receipts, and citations. Engines return proposals only.
2. Negative and uncertain scientific outcomes (`REJECT`, `UNCLEAR`,
   `UNSUPPORTED`, `OVERSTATED`, `PARTIALLY_SUPPORTED`) are canonical state,
   never fallback triggers.
3. Corpus coverage, retrieval coverage, and evidence closure are three
   separate completeness concepts; no report may conflate them.
4. Graphify output is a retrieval lead only; it becomes evidence solely
   through exact pinned-Markdown span verification.
5. Gap claims remain publication-disabled for this program unless a future
   protocol establishes a sufficiently comprehensive search process.
6. Real corpus content, discovery reports, and project artifacts stay
   outside the public repository; public-history gates rerun before any
   release push.

## Serialized compatibility

This slice adds no canonical serialized contract. New project-level formats
(source-quality records, alias files, coverage diagnostics, benchmark case
files, benchmark reports) are versioned operator artifacts outside the
repository and carry explicit format-version identifiers. The existing
`external_corpus` marker, environment opt-in, and corpus immutability
oracle pattern established by `tests/library/test_external_corpus_smoke.py`
are reused unchanged.

## Verification and required evidence

Per milestone, run the focused new tests, then:

```bash
python -m pytest -m "not external_engine and not requires_bwrap and not external_corpus"
python -m pytest -m "requires_bwrap or sandbox_conformance"
python -m pytest -m "external_corpus"   # operator host only, explicit opt-in
python -m vibereview.library.public_guard . --revision HEAD --private-denylist <administrator-held-denylist>
```

Record exact commit, commands, environment, marker selection, and results
in `HANDOFF.md` before pushing. External-corpus evidence must state that
the library state snapshot was unchanged. No evidence from this slice may
claim scientific acceptance, publication eligibility, or live-provider
qualification.
