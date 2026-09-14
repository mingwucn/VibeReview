# VibeReviewPaper — Final Revised Implementation Plan

## 1. Objective

VibeReviewPaper should provide a reproducible workflow for constructing a scientific review paper through four clearly separated systems:

```text
Deep Research
↓
scientific discovery

MyLib
↓
source literature

VibeReview
↓
evidence and claim authorization

Human researcher
↓
final scientific acceptance
```

The intended scientific workflow is:

```text
Review protocol
↓
Research question and scope
↓
Broad Deep Research
↓
Provisional research outline
↓
Outline challenge
↓
Section-specific Deep Research
↓
Candidate claims
↓
Project-wide claim normalization
↓
Pinned MyLib corpus selection
↓
Corpus coverage assessment
↓
Adversarial claim-directed retrieval
↓
Retrieval coverage assessment
↓
Evidence assessment
↓
Paper-level evidence aggregation
↓
Study-dependence consideration
↓
Evidence-constrained claim synthesis
↓
Final claim validation
↓
ClaimPackets
↓
Evidence-informed manuscript outline
↓
Scientific propositions
↓
Citation authorization
↓
Proposition audit
↓
Controlled prose rendering
↓
Sentence audit
↓
Deterministic manuscript assembly
↓
Human scientific review
↓
Adversarial Deep Research
↓
Recent-literature check
↓
Final manuscript
```

The governing principle is:

> The review protocol constrains what may legitimately be claimed about the literature; the research outline determines what should be investigated; Deep Research identifies what may be worth testing; MyLib supplies the source corpus; VibeReview determines what the evidence permits; and the researcher determines whether the resulting review is scientifically acceptable.

---

# 2. Current repository baseline

The plan must begin from what already exists rather than from a hypothetical architecture.

The current VibeReview scientific models and transition semantics are frozen. Python already owns canonical identifier allocation, validation, orchestration, transactions, freshness, receipts and citation control. Negative or uncertain scientific outcomes are canonical rather than technical failures.

VibeReview already contains:

```text
immutable generations

bounded task/resource bundles

provider-neutral semantic-task infrastructure

deterministic subprocess execution

credential and sandbox boundaries

generic pinned-Git external-library import

raw text and graph candidate retrieval

canonical evidence promotion

claim assessment and validation

proposition generation

semantic proposition/sentence audits

deterministic assembly

synthetic Package C end-to-end test machinery
```

The current roadmap explicitly states that Package C should implement the existing scientific chain without redesigning the scientific models.

Therefore:

> **The primary objective is no longer to design VibeReview's scientific architecture. It is to operationalize the existing architecture for a real review project.**

---

# 3. MyLib is the real external evidence library

The evidence library is:

```text
mingwucn/MyLib
```

Its relevant structure already includes:

```text
MyLib/
├── Libs/
│   ├── paper1.md
│   ├── paper2.md
│   └── ...
├── ref.bib
├── Libs/graphify-out/
│   ├── graph.json
│   ├── GRAPH_REPORT.md
│   └── ...
└── pipeline/
├── convert_pdfs.py
├── link_graph_to_bib.py
├── inject_evidence.py
└── ...
```

The library already contains paper-level Markdown files and a substantial bibliography.

PDF conversion is already an upstream MyLib operation: `convert_pdfs.py` transforms PDF files into corresponding Markdown files under `Libs/`.

VibeReview therefore must **not** implement another PDF conversion or paper-library subsystem.

---

# 4. Existing MyLib ↔ VibeReview pinning must be reused

VibeReview already tracks MyLib through:

```text
external/MyLib
```

and `.gitmodules` identifies the submodule as `mingwucn/MyLib`.

At the time this plan was prepared, the VibeReview gitlink points to:

```text
8348a04d46d76d3eeb35839d2a9892cdd5c60cf8
```

which is also the inspected MyLib commit.

The existing VibeReview external-library boundary already verifies the configured commit, superproject gitlink and library `HEAD`, reads source material from pinned Git objects rather than mutable working-tree bytes, imports selected Markdown into generation-owned content-addressed paths, and preserves canonical paper identities.

Therefore:

```text
DO NOT build a second MyLibAdapter subsystem.
```

Instead:

```text
existing generic VibeReview library boundary
+
small MyLib-specific operator configuration
+
validation/reporting utilities
```

should be used.

---

# 5. Architectural boundary

## 5.1 MyLib owns

```text
paper acquisition

PDF storage

PDF → Markdown conversion

Libs/*.md

      ref.bib

      bibliographic maintenance

      Graphify execution

      Graphify output

      MyLib-local retrieval/writing utilities
      ```

## 5.2 Deep Research owns

```text
broad field discovery

section-specific discovery

candidate-paper identification

terminology discovery

candidate mechanisms

candidate contradictions

candidate research gaps
```

Deep Research output is **discovery state**, not publication evidence.

## 5.3 VibeReview owns

```text
pinned corpus import

discovery parsing

candidate-claim generation

retrieval planning

claim-directed source retrieval

retrieval ledgers

evidence assessment

evidence completeness

paper-level aggregation

claim assessment

claim revision

claim validation

ClaimPackets

propositions

citation authorization

semantic audits

manuscript assembly

scientific provenance

reproducibility
```

## 5.4 Human researcher owns

```text
review methodology

research question

scope

outline

corpus decisions

interpretation of major disputes

high-impact claim approval

manuscript organization

final scientific acceptance
```

---

# 6. Do not redesign the frozen scientific contract without evidence

No new canonical model, enum or state transition should be added merely because it appears convenient.

Before modifying:

```text
Paper

EvidenceRecord

ClaimPaperEvidence

FinalClaimValidation

ClaimPacket

Proposition

RenderedSentence

audit models
```

the following must exist:

```text
demonstrated contract defect
+
minimal failing regression
+
minimal contract correction
+
passing regression
```

Project-management information such as:

```text
research outline versions

review protocols

retrieval benchmark results

source-quality assessments

study-identity notes
```

should preferably remain external project artifacts unless the existing canonical contract genuinely cannot satisfy a scientific invariant.

---

# 7. Real review projects remain outside the public VibeReview repository

Recommended project structure:

```text
MyReview/
│
├── protocol/
│   ├── review_protocol.yaml
│   ├── research_question.md
│   ├── scope.md
│   └── inclusion_exclusion.md
│
├── outline/
│   ├── outline_v001.md
│   ├── outline_v002.md
│   ├── outline_v003.md
│   └── current.txt
│
├── discovery/
│   ├── DR001_scoping.md
│   ├── DR002_section_S01.md
│   ├── DR003_section_S02.md
│   ├── ...
│   ├── DR090_adversarial.md
│   └── DR099_recent.md
│
├── corpus/
│   ├── mylib.toml
│   ├── selection.yaml
│   ├── bibliography_reconciliation.json
│   ├── source_quality.json
│   └── corpus_coverage.json
│
├── benchmarks/
│   ├── retrieval/
│   └── semantic/
│
├── review_state/
│
├── exports/
│   ├── claims/
│   ├── evidence/
│   ├── retrieval/
│   ├── audits/
│   └── review_packets/
│
└── manuscript/
├── outline.md
├── sections/
└── review.md
```

Real scientific state, unpublished prose and corpus-derived artifacts should remain outside public VibeReview source control.

---

# 8. Phase A — Review Protocol

Before Deep Research begins, create a lightweight versioned review protocol.

For the intended initial use case, the default can be:

```yaml
review_mode: critical_narrative

research_question: ...

scope:
include: ...
exclude: ...

publication_cutoff: ...

corpus_strategy:
type: curated
comprehensiveness_claim: false

sources:
primary_research: preferred
review_articles: discovery_and_context
conference_papers: conditional
preprints: conditional

systematic_search_claim: false
quantitative_meta_analysis: false
gap_claims_allowed: false
```

The initial workflow does not need PRISMA-level infrastructure unless the eventual paper explicitly claims to be systematic.

The protocol exists mainly to prevent inappropriate manuscript language such as:

```text
"all studies demonstrate..."
```

when the project actually uses a curated MyLib corpus.

Protocol changes create versions rather than overwriting history.

---

# 9. Phase B — Define the scientific question

The researcher should define:

```text
scientific topic

principal review question

material/population/process/system

scope

explicit exclusions

publication period

review mode

intended audience

likely journal class
```

The question should be sufficiently narrow to guide research while remaining broad enough for discovery to modify the eventual organization.

---

# 10. Phase C — Broad Deep Research

Run:

```text
DR001_scoping.md
```

Its purpose is field discovery rather than manuscript drafting.

Require:

```text
major terminology

synonyms

major scientific themes

mechanisms

landmark studies

recent directions

methodological subdivisions

competing explanations

contradictions

boundary conditions

important primary studies

important review papers

suspected gaps
```

Include adversarial questions:

```text
Which important themes would an obvious textbook-style
outline omit?

Which studies or mechanisms conflict with the dominant
interpretation?

Which terminology differences could hide relevant literature?

Which subfields appear disconnected but address the same
scientific phenomenon?
```

---

# 11. Phase D — Construct the provisional research outline

Build the initial outline from:

```text
research question
+
researcher knowledge
+
DR001
```

This should be called the **research outline**.

It is not yet the manuscript outline.

A section specification should contain:

```text
section_id

title

scientific purpose

research questions

known terminology

anticipated mechanisms

anticipated boundary conditions

adversarial questions

status
```

Example:

```text
S03.1
Processing temperature

Purpose
Assess how processing temperature modifies phenomenon X.

Questions
- Which trends have been reported?
- Which mechanisms explain them?
- Under which conditions does the relationship reverse?
- Is it material-dependent?
- Are measurement methods responsible for apparent disagreement?

Adversarial questions
- Which studies contradict the expected trend?
- Which systems report no significant effect?
- Is temperature confounded with another process parameter?

Status
provisional
```

---

# 12. Phase E — Challenge the provisional outline

Before section-specific Deep Research is performed, run one outline challenge.

Inputs:

```text
review protocol
research question
DR001
outline_v001
```

Ask specifically for:

```text
missing mechanisms

missing material/process classes

missing methodological branches

topics incorrectly separated

topics that should be merged

important contradictions not represented

terminology blind spots

assumptions embedded in the current structure
```

The challenger makes recommendations only.

The researcher decides whether to revise the outline.

---

# 13. Phase F — Version the research outline

Never silently overwrite the outline.

Example:

```text
outline_v001
↓
new mechanism discovered
↓
outline_v002
↓
later evidence shows two sections overlap
↓
outline_v003
```

Each revision records:

```text
parent version

change reason

sections added

sections removed

sections merged

sections split

human approver
```

---

# 14. Phase G — Section-specific Deep Research

Run focused Deep Research investigations for major sections.

For example:

```text
DR002_mechanisms.md

DR003_temperature.md

DR004_process_speed.md

DR005_material_dependence.md

DR006_measurement.md

DR007_modelling.md
```

Each task receives only:

```text
overall question

scope

relevant section

research questions

neighboring sections

known terminology

adversarial questions
```

Require:

```text
current scientific understanding

proposed mechanisms

important primary studies

contradictory studies

null findings

boundary conditions

methodological limitations

alternative explanations

evidence that does not fit the supplied outline
```

---

# 15. Phase H — Preserve Deep Research unchanged

Raw Deep Research outputs should be immutable project resources.

For example:

```yaml
report_id: DR003
outline_version: O002
section_id: S03.1
purpose: section_research
content_sha256: ...
```

The body remains unchanged.

Parsed outputs are derived resources.

---

# 16. Phase I — Parse discovery through existing VibeReview tasks

Use the existing discovery/task infrastructure where possible.

Extract:

```text
themes

terminology

candidate claims

candidate papers

controversies

boundary conditions

candidate gaps

source references
```

The distinction remains:

```text
Deep Research
↓
discovery knowledge
```

not:

```text
Deep Research
↓
canonical scientific evidence
```

No Deep Research statement becomes publication evidence directly.

---

# 17. Phase J — Generate atomic candidate claims

Candidate claims should represent one scientifically testable proposition.

Bad:

```text
Temperature, composition and process speed interact to
modify thermal gradients, residual stress, microstructure
and mechanical performance.
```

Better:

```text
C001
Higher substrate temperature is associated with lower
thermal gradients.

C002
Lower thermal gradients are associated with reduced
residual stress.

C003
The relationship between substrate temperature and
residual stress depends on material class.
```

Claims should be:

```text
atomic

retrievable

scientifically meaningful

empirically assessable

appropriately scoped

free of unnecessary causal wording
```

---

# 18. Phase K — Normalize candidate claims globally

Candidate claims should be checked across the entire project.

Detect:

```text
duplicates

near-duplicates

compound claims

narrower/broader relationships

overlap

candidate contradictions
```

Conceptual relationships:

```text
DUPLICATE_OF

NARROWS

BROADENS

OVERLAPS

CONTRADICTS

RELATED_MECHANISM
```

These relationships need not become new canonical VibeReview models.

They may remain project/discovery metadata.

---

# 19. Phase L — Inspect pinned MyLib

Use the existing VibeReview external-library inspection boundary.

The MyLib configuration should identify:

```text
repository/submodule

full commit SHA

paper path:
Libs/*.md

      bibliography:
      ref.bib

      optional graph:
      Libs/graphify-out/graph.json
      ```

      Inspection should report:

      ```text
      number of Markdown papers

      number of bibliography records

      Markdown files without bibliography matches

      bibliography entries without Markdown

      duplicate candidates

      source blob identities

      total bytes

      graph availability
      ```

      Inspection itself creates no canonical scientific evidence.

      ---

# 20. Phase M — Bibliographic reconciliation

For each selected paper establish:

```text
canonical VibeReview Paper identity
↕
MyLib Markdown path
↕
MyLib Git blob
↕
ref.bib entry
↕
DOI / title / metadata
```

The existing VibeReview identity rules remain authoritative.

BibTeX citekeys should be treated as aliases unless the existing identity contract already says otherwise.

Matching should proceed:

```text
exact deterministic match
↓
high-confidence normalized match
↓
manual adjudication
```

Ambiguous or conflicting publication identity should fail closed.

---

# 21. Do not mutate MyLib during VibeReview execution

MyLib's own `link_graph_to_bib.py` may modify Markdown by inserting `\cite{...}` and can use an LLM fallback for unresolved mappings.

That may remain part of **upstream MyLib maintenance**.

It should not run as part of a VibeReview scientific execution.

During a VibeReview run:

```text
MyLib source = read only
```

because changing source Markdown may change:

```text
bytes

hashes

character offsets

evidence locators
```

and would require a new MyLib commit anyway.

---

# 22. Phase N — Source-quality assessment

Git provenance proves exactly what text was used.

It does not prove that PDF→Markdown conversion was accurate.

An inspected MyLib Markdown paper contains visible extraction artefacts, demonstrating why conversion quality should be assessed separately from provenance.

For each selected paper assign:

```text
READABLE

READABLE_WITH_ARTIFACTS

MATERIAL_EXTRACTION_PROBLEM

UNUSABLE
```

Check only what is scientifically relevant:

```text
title/authors identifiable

major headings recognizable

claim-relevant prose readable

numbers/units readable when used

equations readable when used

tables readable when used

no major missing section relevant to the claim
```

This should be claim-sensitive.

A corrupted equation is irrelevant if no claim uses the equation.

A `MATERIAL_EXTRACTION_PROBLEM` blocks evidence derived from the affected content until MyLib is corrected and repinned.

---

# 23. Phase O — Explicit corpus selection

Use the existing external-library selection mechanism.

Each candidate source should receive:

```text
INCLUDE
or
EXCLUDE
```

with:

```text
source hash

role

reviewer

review time

reason
```

For a large MyLib collection, software may prepopulate exclusions according to explicit project criteria, but the final selection must contain no undecided candidate.

The first pilot should deliberately select only a very small number of papers.

---

# 24. Phase P — Freeze/import the corpus

Use the existing VibeReview library transaction to import selected papers.

The corpus generation should bind:

```text
MyLib commit

selected Git objects

source hashes

canonical Paper identities

selection decisions

generation ownership

corpus lock

raw Markdown copies
```

Do not add another `CorpusSnapshot` scientific model if the existing corpus lock already satisfies the invariant.

---

# 25. Phase Q — Corpus coverage assessment

Corpus coverage asks:

> Does the selected MyLib subset adequately represent the literature needed to address the declared review scope?

Assess:

```text
outline themes

major mechanisms

important papers identified by Deep Research

major contradictory findings

materials/populations

methodologies

publication periods

known boundary conditions
```

Possible project-level results:

```text
ADEQUATE_FOR_DECLARED_SCOPE

PARTIAL

KNOWN_GAPS

INADEQUATE
```

If inadequate:

```text
expand corpus
```

or:

```text
narrow scope
```

Do not compensate with stronger prose.

---

# 26. Maintain three separate completeness concepts

This terminology should become permanent.

## 26.1 Corpus coverage

Are the necessary papers represented in the selected corpus?

## 26.2 Retrieval coverage

Has the retrieval process exposed enough relevant passages from that selected corpus?

## 26.3 Evidence closure

Once evidence has entered canonical scientific state, did any of it disappear during downstream synthesis?

Thus:

```text
evidence closure
≠
retrieval completeness
≠
literature completeness
```

---

# 27. Phase R — Generate adversarial retrieval plans

For every candidate claim, generate several retrieval intentions.

Minimum:

```text
SUPPORT

CONTRADICT

QUALIFY

CONTEXT
```

Recommended additional intents:

```text
BOUNDARY

NULL_RESULT

METHOD_DEPENDENCE

MECHANISM

TERMINOLOGY_VARIANT
```

Example:

```text
Claim:
Preheating reduces residual stress.

SUPPORT
preheating reduced residual stress

CONTRADICT
preheating increased residual stress

QUALIFY
preheating residual stress material dependence geometry

NULL_RESULT
preheating no significant residual stress effect

BOUNDARY
preheating residual stress temperature range
```

A single nearest-neighbor query is not sufficient.

---

# 28. Phase S — Raw Markdown is the authoritative retrieval source

The first real operational pilot should use imported raw MyLib Markdown as the authoritative source.

MyLib's existing evidence injector uses TF–IDF ranking across paper text, abstracts, titles and Graphify snippets. This is useful as an engineering baseline but is not equivalent to claim-directed scientific evidence validation.

The authoritative chain should remain:

```text
candidate claim
↓
retrieval query
↓
pinned raw MyLib Markdown
↓
candidate span
↓
scientific assessment
```

---

# 29. Graphify policy

Graphify may be useful for:

```text
query expansion

concept discovery

paper discovery

topic navigation

candidate mechanism discovery
```

It must not become canonical publication evidence.

The inspected MyLib graph report contains a significant inferred component and examples of semantically questionable inferred relationships.

Therefore:

```text
Graphify inference
↓
candidate retrieval lead
↓
original MyLib paper
↓
exact Markdown passage
↓
EvidenceRecord
```

The first real pilot should ideally succeed without Graphify dependency.

Graphify can then be added as an auxiliary retrieval channel and benchmarked separately.

---

# 30. Phase T — Validate exact source spans

Every promoted evidence span should resolve to:

```text
canonical Paper

imported raw source object

exact source locator

source digest

corpus generation

pinned MyLib source object
```

Use existing VibeReview source-locator semantics.

The current Package C roadmap already expects exact source slices, Unicode code-point offsets, raw content digests, paper ownership and corpus provenance before canonical span promotion.

Do not invent a parallel locator format.

---

# 31. Phase U — Assess retrieval coverage

Before strong claim synthesis, evaluate the retrieval run.

Diagnostics should include:

```text
retrieval intents executed

number of distinct queries

paper diversity

terminology diversity

support candidates

contradiction candidates

qualification candidates

null-result candidates

known-paper recovery

known-passage recovery

duplicate-result rate
```

Operational status may be:

```text
ADEQUATE_FOR_SYNTHESIS

PARTIAL

INADEQUATE
```

This is a diagnostic judgment, not proof of exhaustive recall.

---

# 32. Retrieval benchmark

Construct a small manually curated retrieval benchmark.

Include known examples of:

```text
obvious support

contradiction

qualification

null result

boundary condition

different terminology

indirect mechanistic evidence

method-specific result

relevant full-text statement absent from title/abstract
```

Measure:

```text
known-paper recall

known-passage recall

contradiction recall

qualification recall

terminology-variant recall
```

These metrics assess the retrieval system.

They do not prove corpus-wide recall.

---

# 33. Phase V — Evidence assessment

Retrieved spans remain candidates until scientifically classified.

Use the existing VibeReview task and scientific contracts.

Conceptually distinguish:

```text
SUPPORTS

CONTRADICTS

QUALIFIES

CONTEXTUAL

UNCLEAR

NOT_RELEVANT
```

where the current frozen models permit them.

Separately evaluate useful evidence characteristics such as:

```text
directness

scope match

methodological relevance

applicability

limitations
```

Do not combine these into a universal numerical evidence-quality score.

---

# 34. Semantic gold-standard benchmark

Before relying on a live model for operational scientific assessment, create approximately:

```text
30–100 manually adjudicated claim–passage pairs
```

Include:

```text
support

contradiction

qualification

context

unclear

irrelevant

negation

null result

partial support

boundary conditions

material/population mismatch

method-dependent result

causal versus correlational mismatch
```

Retain:

```text
adjudicated result

individual reviewer labels where available

human disagreement
```

Measure:

```text
per-class precision

per-class recall

confusion matrix

false-support rate

contradiction recall

qualification recall
```

False support deserves particular attention because it can directly produce unjustified manuscript claims.

---

# 35. Recalibrate after semantic-engine changes

Re-run the semantic benchmark after material changes to:

```text
provider

model

model version

prompt/task instructions

schema

preprocessing

classification policy
```

Where hosted-model versioning is opaque, benchmark periodically even if the public model name is unchanged.

---

# 36. Preserve negative and uncertain scientific results

The pipeline must preserve:

```text
contradictions

qualifications

null findings

uncertainty

unsupported claims

overstated claims

partial support
```

These are scientific outcomes.

They are not technical failures.

They must not trigger model shopping merely because the model produced an inconvenient verdict.

This is already consistent with the current VibeReview design.

---

# 37. Phase W — Aggregate evidence by paper

Several passages from one paper should not become several independent studies.

Canonical processing remains:

```text
EvidenceRecords
↓
ClaimPaperEvidence
```

For example:

```text
Paper P001

supporting spans:
E001
E002

qualifying span:
E003

overall relation:
supports with qualification

limitations:
...
```

All canonical evidence belonging to the paper/claim pair must remain visible to the aggregation task.

---

# 38. Verify evidence closure before changing the frozen contract

Earlier planning identified a possible closure defect.

The current repository has since undergone extensive synthetic Package C hardening.

Therefore the correct implementation step is now:

```text
TEST FIRST
```

Create regression fixtures that attempt to omit:

```text
supporting evidence

contradictory evidence

qualifying evidence

contextual evidence

unclear evidence
```

from:

```text
EvidenceRecord
↓
ClaimPaperEvidence
↓
FinalClaimValidation
↓
ClaimPacket
```

If the existing validators already reject all omissions:

```text
no contract modification
```

If a concrete omission succeeds:

```text
document defect
+
minimal validator fix
+
focused regression
```

Do not redesign the data model pre-emptively.

---

# 39. Phase X — Consider study/dataset dependence

Paper identity does not always equal independent scientific evidence.

Where relevant, note whether several papers use the same:

```text
dataset

cohort

experiment

test campaign
```

For the initial engineering-review use case, this may remain project-level synthesis metadata.

Do not redesign the frozen models solely to introduce a general study-identity framework before evidence shows that one is necessary.

The synthesis prompt must nevertheless avoid treating known duplicated data as independent replication.

---

# 40. Phase Y — Quality-aware claim synthesis

The system must not reduce evidence synthesis to vote counting.

Bad:

```text
4 support
1 contradict
→ supported
```

Instead consider:

```text
directness

scope match

methodological relevance

applicability

limitations

between-paper heterogeneity

strength of contradictory evidence

boundary conditions

study independence
```

Example:

```text
P001 — direct strong support
P002 — indirect support
P003 — strong qualification by material
P004 — direct contradiction at high temperature
P005 — support only below threshold T
```

may justify:

```text
REVISE / NARROW
```

rather than:

```text
RETAIN
```

---

# 41. Phase Z — Claim assessment and revision

The candidate:

```text
Preheating reduces residual stress.
```

might become:

```text
Within the lower processing-temperature regimes represented
in the available evidence, preheating is generally associated
with reduced residual stress, whereas results at higher
temperatures are heterogeneous.
```

The revised statement must then be reassessed against the complete canonical evidence set.

Do not assume revision automatically produces a valid claim.

---

# 42. Gap claims remain restricted

Statements such as:

```text
Few studies have investigated X.

No studies compare A and B.

Mechanism Y has not been examined.
```

require evidence about absence across a corpus/search process.

For the first operational review:

```text
publication-authorized gap claims = disabled
```

unless the review protocol establishes a sufficiently comprehensive search process.

Deep Research may still generate gap candidates.

They should remain discovery observations.

---

# 43. Phase AA — Final claim validation

Before ClaimPacket creation, validate:

```text
claim scope

certainty

causal language

quantitative precision

retrieval adequacy

evidence completeness

contradictory evidence

qualifications

known corpus limitations

source-quality concerns

study dependence where relevant
```

The scientific chain remains:

```text
candidate claim
↓
retrieval
↓
retrieval assessment
↓
EvidenceRecords
↓
ClaimPaperEvidence
↓
claim assessment
↓
claim revision
↓
FinalClaimValidation
↓
ClaimPacket
```

ClaimPacket remains the manuscript authorization boundary.

---

# 44. Phase AB — Build the manuscript outline from validated evidence

The manuscript outline should be different from the research outline.

Derive it from:

```text
research outline

validated claims

revised claims

rejected claims

contradictory findings

qualifications

evidence density

scientific uncertainty
```

Therefore sections may be:

```text
deleted

merged

split

reordered

converted into controversy sections

converted into limitations sections
```

The manuscript structure should reflect the evidence rather than force evidence into the original outline.

---

# 45. Manuscript section contract

Each manuscript section should specify:

```text
purpose

authorized ClaimPackets

required contradiction/qualification

prohibited unsupported assertions

required transitions

tables/figures where relevant
```

Example:

```text
Section 4
Thermal management and residual stress

Purpose
Describe evidence-supported effects of thermal-management
strategies.

Authorized:
CP001
CP004
CP009

Required qualification:
CP012

Required contradiction:
CP015

Prohibited:
unvalidated claims about complete stress elimination
```

---

# 46. Phase AC — Generate propositions before prose

Do not draft prose directly from raw evidence.

Generate propositions first.

Example:

```text
PR001

Preheating is generally associated with reduced thermal
gradients within the lower-temperature regimes represented
in the available evidence.

Sources:
CP001
CP004
```

Approximately one manuscript-level scientific assertion should exist per proposition.

---

# 47. Phase AD — Citation authorization

The internal evidence chain for a citation should be:

```text
Proposition
↓
ClaimPacket
↓
ClaimPaperEvidence
↓
EvidenceRecord
↓
exact imported Markdown span
↓
pinned MyLib source
```

A paper appearing somewhere inside a ClaimPacket is not sufficient reason to cite it for every proposition derived from that packet.

The relevant paper-level evidence must authorize the assertion.

---

# 48. Phase AE — Proposition audit

Before prose generation, assess whether the proposition:

```text
is authorized by the ClaimPackets

preserves scope

preserves certainty

uses valid citations

adds no unsupported scientific material

does not overstate evidence
```

Only passing propositions proceed to prose rendering.

---

# 49. Phase AF — Controlled prose rendering

The prose model may improve:

```text
syntax

readability

flow

terminology consistency

paragraph structure

transitions
```

It may not add new scientific propositions.

The proposition layer defines the maximum scientific content.

---

# 50. Phase AG — Sentence audit

Every substantive rendered sentence should be audited.

Example:

```text
Proposition:
Preheating was associated with lower residual stress.

Rendered:
Preheating eliminates residual stress.

Verdict:
OVERSTATED
```

The sentence is rejected.

The current VibeReview contract permits canonical rendered sentences only for passing `ENTAILED` audits; that gate should remain unchanged.

---

# 51. Phase AH — Deterministic manuscript assembly

After sentence audit:

```text
accepted sentences
+
section order
+
authorized citations
+
deterministic formatting
=
manuscript
```

No unrestricted scientific rewriting occurs after the final semantic audit.

Repeated assembly of the same canonical state should reproduce the same scientific output.

---

# 52. Phase AI — Human scientific review packet

For every manuscript section, expose:

```text
final prose

propositions

ClaimPackets

supporting papers

contradictory papers

qualifying evidence

uncertainty

retrieval diagnostics

source-quality notes

exact source spans

audit results

known corpus limitations
```

The researcher should be able to navigate:

```text
sentence
↓
proposition
↓
ClaimPacket
↓
ClaimPaperEvidence
↓
EvidenceRecord
↓
imported Markdown span
↓
MyLib Git blob
↓
pinned MyLib commit
```

---

# 53. Human modifications are provenance events

A human edit should not silently rewrite history.

Changes to:

```text
review scope

research outline

corpus selection

claim wording

evidence interpretation

manuscript organization
```

should produce new project/canonical state as appropriate.

Existing immutable generations and freshness mechanisms should be reused.

Affected downstream state should become stale.

---

# 54. Phase AJ — Adversarial Deep Research after drafting

After the first manuscript draft exists, run:

```text
DR090_adversarial.md
```

Provide:

```text
question

scope

manuscript outline

major conclusions

known mechanisms

known contradictions

important citations
```

Ask for:

```text
contradictory primary studies

missing mechanisms

alternative interpretations

methodological objections

unrepresented populations/materials

recent studies
```

DR090 returns to discovery state.

It never directly modifies canonical claims.

---

# 55. Phase AK — Recent-literature check

Before submission run:

```text
DR099_recent.md
```

Ask:

> Which newly published primary studies could materially change any of these conclusions?

If a paper is relevant:

```text
add paper to MyLib
↓
commit MyLib
↓
update VibeReview submodule pin
↓
create new corpus generation
↓
rerun affected retrieval
↓
rerun evidence synthesis
↓
invalidate affected ClaimPackets
↓
rerun manuscript stages
```

Do not manually patch a citation into accepted prose.

---

# 56. Replay versus semantic regeneration

Two reproducibility concepts must remain separate.

## Replay reproducibility

Given:

```text
same canonical proposals

same corpus generation

same implementation

same contracts
```

VibeReview should reproduce canonical scientific state.

## Regeneration reproducibility

A new LLM call may produce a different proposal.

Therefore preserve:

```text
provider

model

configuration

task/schema version

instruction resources

input bundle

raw response

normalized proposal

receipt
```

Byte-identical semantic regeneration is not required.

---

# 57. Security and authorization boundary

The current synthetic Package C harness is engineering evidence, not authorization for a real scientific run.

A real operational run requires explicit authorization for:

```text
external corpus

provider

model

network policy

host

output location

spending limit

task budget

human scientific review
```

Scientific success and repository-release success are separate concerns.

Do not infer:

```text
test suite passes
→ scientific acceptance
```

or:

```text
scientific pilot succeeds
→ public release authorized
```

---

# 58. Benchmark contamination policy

Once enough adjudicated examples exist, separate:

```text
development benchmark cases
```

from:

```text
evaluation benchmark cases
```

Examples repeatedly used for prompt tuning should not remain the sole final evaluation set.

Human disagreements should remain visible rather than being silently erased.

---

# 59. Adversarial pilot design

The first serious real-paper pilot should not contain only mutually agreeing studies.

Freeze pilot composition **before examining live-model results**.

Where possible include:

```text
one strong supporting paper

one strong contradicting paper

one qualifying paper

one null/weak-effect paper

one methodologically different paper

one boundary-condition result
```

For a larger pilot, additionally include:

```text
terminology variation

shared datasets/experiments

apparently conflicting quantitative results
```

The system should be tested on disagreement.

---

# 60. Implementation principle

Every new implementation step should follow:

```text
inspect existing capability
↓
test it
↓
identify demonstrated missing behavior
↓
implement smallest missing layer
```

not:

```text
redesign subsystem
↓
duplicate existing infrastructure
```

In particular, do not rebuild:

```text
external-library pinning

corpus locks

immutable generations

canonical Paper import

raw retrieval infrastructure

writer transactions

semantic audits

synthetic Package C controller
```

unless a concrete defect is first demonstrated.

---

# 61. Implementation Milestone 0 — Normative authorization

Before implementation:

1. Update `goal.md`.
2. Add or update the relevant normative document under `docs/`.
3. Define the exact operational slice.
4. Preserve the V1.5.1b frozen-model rule.
5. Declare non-goals.
6. Define acceptance evidence.
7. Record final verification in `HANDOFF.md`.

This follows the repository's documentation-first development policy.

The normative authorization for the software slice covering Milestones 1–6
is [docs/operations/mylib_operational_review.md](docs/operations/mylib_operational_review.md).
It defines the exact operational slice, preserves the V1.5.1b frozen-model
rule, declares non-goals, and defines acceptance evidence. Milestones 7–12
remain unauthorized pending separate operator approval.

### Exit criterion

The implementation slice is explicitly authorized by repository documentation.

---

# 62. Milestone 1 — Verify the actual MyLib boundary

Using the real submodule:

Verify:

```text
configured commit = gitlink = MyLib HEAD

all source reads come from pinned Git objects

dirty bytes are excluded

Libs/*.md inventory succeeds

      ref.bib inventory succeeds

      optional graph inventory succeeds

      import limits are respected

      selection decisions are complete
      ```

### Exit criterion

The real pinned MyLib can be inspected safely without mutation or scientific acceptance.

---

# 63. Milestone 2 — MyLib reconciliation and source QC

Implement only the missing operator/integration utilities for:

```text
Markdown ↔ bibliography reconciliation

duplicate-paper reporting

unmapped-source reporting

metadata conflict reporting

source-quality assessment
```

### Exit criterion

Every selected pilot paper has:

```text
one canonical publication identity

one pinned Markdown source

one bibliographic alias where available

one acceptable source-quality result
```

---

# 64. Milestone 3 — Evidence-closure regression

Challenge the frozen validators with synthetic adversarial omissions.

### Exit criterion

Either:

```text
current contract already enforces closure
```

or:

```text
one demonstrated defect is minimally fixed
```

No speculative scientific-model redesign.

---

# 65. Milestone 4 — Retrieval-coverage framework

Implement/extend:

```text
multi-intent query generation

raw text retrieval

bounded retrieval ledgers

known-passage fixtures

known-paper fixtures

retrieval-coverage report

negative-search preservation
```

### Exit criterion

Retrieval adequacy is measurable independently from evidence closure.

---

# 66. Milestone 5 — Semantic benchmark

Build:

```text
30–100 adjudicated claim–passage pairs
```

Measure the semantic engine's principal error modes.

### Exit criterion

There is empirical evidence about whether the evidence-assessment model can distinguish support, contradiction and qualification.

---

# 67. Milestone 6 — Five-real-paper deterministic vertical slice

Select five real MyLib papers.

Freeze selection before execution.

Use:

```text
real MyLib
+
pinned Git
+
manual/deterministic semantic proposals
+
no live provider requirement
```

Run:

```text
import
→ retrieval
→ evidence promotion
→ evidence assessment
→ aggregation
→ claim validation
```

### Exit criterion

Real scientific source spans survive the complete provenance chain independently of live-model behavior.

---

# 68. Milestone 7 — Five-paper live semantic pilot

After separate operator authorization, run the same frozen project with one qualified live provider.

Do not introduce multiple providers.

Evaluate against the semantic benchmark and manually inspected pilot results.

### Exit criterion

The live semantic engine operates without violating any Python-controlled scientific invariant.

---

# 69. Milestone 8 — Adversarial 5–20-paper mini-review

Use a deliberately conflicting corpus.

Run:

```text
discovery
→ claims
→ corpus challenge
→ retrieval
→ evidence
→ claim synthesis
```

### Exit criterion

Contradictions and boundary conditions cause scientifically appropriate narrowing or rejection rather than disappearing.

---

# 70. Milestone 9 — Manuscript vertical slice

Produce one complete manuscript section:

```text
validated claims
→ manuscript section contract
→ propositions
→ citation bindings
→ proposition audits
→ prose
→ sentence audits
→ deterministic assembly
```

### Exit criterion

Every substantive final sentence resolves to exact pinned MyLib evidence.

---

# 71. Milestone 10 — Adversarial discovery loop

Run DR090.

Import any genuinely missing literature through MyLib.

Rerun affected scientific state.

### Exit criterion

The system correctly invalidates and recomputes affected conclusions.

---

# 72. Milestone 11 — Complete review pilot

Run the system for one coherent real review-paper project.

Corpus size should be determined scientifically rather than by an arbitrary software target.

### Exit criterion

A complete evidence-constrained draft and human review packet can be produced.

---

# 73. Milestone 12 — Submission-readiness pass

Before submission:

```text
freeze protocol

freeze outline

freeze MyLib commit

freeze corpus selection

run DR099

resolve newly relevant papers

rerun affected claims

rerun semantic audits

run replay reproducibility

complete human scientific review

assemble deterministic manuscript
```

### Exit criterion

No unresolved stale scientific state remains.

---

# 74. Acceptance criteria

A complete operational project should satisfy the following.

## Protocol and discovery

1. Review methodology is explicit.
2. Scope is explicit.
3. Publication cutoff is explicit.
4. Corpus comprehensiveness claims are explicit.
5. Raw Deep Research reports are immutable.
6. Discovery reports remain non-evidentiary.
7. Candidate claims retain discovery provenance.
8. Outline versions are preserved.

## MyLib

9. MyLib is pinned to a full commit.
10. Gitlink, configured pin and MyLib `HEAD` agree.
11. Scientific reads use pinned Git objects.
12. Dirty worktree bytes are excluded.
13. VibeReview does not mutate MyLib.
14. Selected papers have explicit include/exclude decisions.
15. Selected papers have reconciled publication identities.
16. Ambiguous identity fails closed.
17. Selected sources receive source-quality assessment.
18. Material conversion defects block affected evidence.

## Corpus and retrieval

19. Corpus coverage is assessed.
20. Known corpus gaps are preserved.
21. Every substantive candidate claim receives adversarial retrieval.
22. Support-only retrieval is not sufficient.
23. Retrieval coverage is assessed independently of evidence closure.
24. Retrieval benchmarks include contradiction.
25. Retrieval benchmarks include qualification.
26. Retrieval benchmarks include terminology variation.
27. Retrieval benchmarks include null findings.
28. Graphify alone cannot authorize evidence.
29. Every promoted evidence span resolves to raw pinned Markdown.

## Evidence

30. Every promoted span has exactly one canonical scientific assessment where required.
31. Contradictory evidence remains canonical.
32. Qualifying evidence remains canonical.
33. Null/negative findings remain visible.
34. Uncertain findings remain visible.
35. Canonical evidence cannot silently disappear during aggregation.
36. Canonical paper-level evidence cannot silently disappear during final validation.
37. Paper passages are not treated as independent studies.
38. Evidence synthesis is not simple vote counting.
39. Known study dependence is considered where relevant.

## Claims

40. Inadequate retrieval can block strong validation.
41. Unsupported claims cannot become ClaimPackets.
42. Contradicted claims can be rejected.
43. Broad claims can be narrowed.
44. Revised claims are revalidated.
45. Gap claims remain prohibited unless independently justified.
46. Every ClaimPacket derives from the complete canonical evidence set.

## Semantic evaluation

47. An adjudicated semantic benchmark exists.
48. False-support rate is recorded.
49. Contradiction recall is recorded.
50. Qualification recall is recorded.
51. Human disagreement is retained.
52. Material model/prompt changes trigger recalibration.

## Manuscript

53. Manuscript organization is derived from validated evidence.
54. Every proposition uses authorized ClaimPackets.
55. Every citation resolves to authorized paper-level evidence.
56. Every paper-level citation resolves to EvidenceRecords.
57. Every substantive sentence has a passing audit.
58. Overstated rendering is blocked.
59. Unsupported rendering is blocked.
60. No unrestricted scientific rewriting occurs after final sentence audit.
61. Assembly is deterministic.

## Governance and reproducibility

62. Human changes retain provenance.
63. Upstream changes invalidate affected downstream state.
64. Live-provider use requires explicit authorization.
65. Real review state remains outside public source control.
66. Replay reproducibility succeeds.
67. Semantic regeneration is not falsely represented as deterministic.
68. Human scientific review is recorded separately from automated validation.

## Adversarial completion

69. The pilot contains conflicting literature.
70. The pilot contains boundary or qualification cases.
71. The pilot contains null/negative evidence where scientifically available.
72. Pilot composition is fixed before live evaluation.
73. DR090 is completed after first drafting.
74. New papers return through MyLib rather than manuscript patching.
75. DR099 is completed before submission.

## Final traceability

76. Every substantive sentence can be traced to a proposition.
77. Every proposition can be traced to ClaimPackets.
78. Every ClaimPacket can be traced to ClaimPaperEvidence.
79. Every ClaimPaperEvidence can be traced to EvidenceRecords.
80. Every EvidenceRecord can be traced to exact imported Markdown.
81. Imported Markdown can be traced to the MyLib Git object.
82. The Git object can be traced to the pinned MyLib commit.
83. A complete human review packet can be exported.

---

# 75. Explicit non-goals

Do not add before the real vertical slice demonstrates the need:

```text
PDF parsing inside VibeReview

OCR inside VibeReview

Graphify execution inside VibeReview

general workflow/DAG framework

multi-agent scientific architecture

web UI

database migration

multiple live providers

automatic Deep Research execution

automatic paper acquisition

automatic journal submission

automated meta-analysis

full systematic-review infrastructure unless explicitly required
```

---

# 76. Recommended implementation order

The implementation sequence should now be frozen as:

```text
0. Normative documentation update

1. Verify real MyLib pinned-library boundary

2. Bibliographic reconciliation + source-quality checks

3. Verify scientific evidence closure

4. Build retrieval-coverage benchmark

5. Build semantic benchmark

6. Run deterministic five-real-paper pilot

7. Run authorized live five-paper pilot

8. Run adversarial 5–20-paper mini-review

9. Generate one complete audited manuscript section

10. Run adversarial Deep Research loop

11. Run complete review-paper pilot

12. Run submission-readiness workflow
```

This order minimizes new architecture while maximizing empirical learning.

---

# 77. Practical researcher workflow

Once implemented, the normal workflow should be:

```text
1. Define review protocol.

2. Define question and scope.

3. Run DR001.

4. Build research outline.

5. Challenge research outline.

6. Revise/version outline.

7. Run section Deep Research.

8. Register discovery documents.

9. Generate atomic candidate claims.

10. Normalize claims project-wide.

11. Pin MyLib commit.

12. Inspect MyLib.

13. Reconcile selected papers with ref.bib.

14. Check source quality.

15. Select corpus.

16. Import/freeze corpus.

17. Assess corpus coverage.

18. Generate adversarial retrieval queries.

19. Retrieve raw MyLib passages.

20. Assess retrieval coverage.

21. Improve retrieval where necessary.

22. Assess scientific evidence.

23. Preserve contradictions, qualifications and null findings.

24. Aggregate evidence by paper.

25. Account for known shared studies/datasets.

26. Assess candidate claims.

27. Revise/narrow claims.

28. Validate final claims.

29. Generate ClaimPackets.

30. Build evidence-informed manuscript outline.

31. Generate propositions.

32. Bind proposition citations.

33. Audit propositions.

34. Render prose.

35. Audit sentences.

36. Assemble deterministic manuscript.

37. Inspect human scientific review packet.

38. Run DR090 adversarial discovery.

39. Update MyLib where missing papers are found.

40. Pin new corpus generation.

41. Rerun affected evidence and claims.

42. Run DR099 recent-literature discovery.

43. Resolve newly relevant papers.

44. Complete final human scientific review.

45. Run replay reproducibility.

46. Export manuscript.
```

---

# 78. Final design rule

The system should remain conceptually simple:

```text
Deep Research
asks:
"What should we investigate?"

MyLib
answers:
"What source literature do we actually possess?"

VibeReview
asks:
"What does this exact pinned evidence permit us to claim?"

The manuscript layer
asks:
"How may those authorized claims be expressed?"

The human researcher
decides:
"Is the resulting scientific review acceptable?"
```

The project should therefore resist adding machinery that does not strengthen one of those five responsibilities.

The next work should be implementation and empirical testing, not further redesign of the scientific architecture.
