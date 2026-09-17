# VibeReview Reproducible Review-Pipeline Plan

**File:** `plan_reproducible_prompt_pipeline.md`
**Project:** VibeReview / VibeReviewPaper
**Baseline:** `mylib-operational-slice-m6`
**Demonstration review:** AI-assisted precision non-conventional manufacturing
**Core processes:** EDM, ECM, precision laser machining
**Secondary processes:** ultrasonic, abrasive jet/waterjet, ECDM, hybrid non-conventional processes
**Forward-looking topic:** atomic / near-atomic manufacturing
**Status:** implementation plan
**Date:** 2026-09-14

---

# 1. Objective

Build a reusable and reproducible review-paper workflow:

```text
research topic
↓
project brief
↓
review protocol
↓
scientific structure
↓
provisional research outline
↓
broad + section-specific Deep Research
↓
candidate scientific claims
↓
claim normalization
↓
pinned MyLib corpus
↓
adversarial retrieval
↓
exact Markdown evidence spans
↓
paper-level evidence aggregation
↓
evidence-constrained claims
↓
manuscript outline
↓
propositions
↓
citation bindings
↓
controlled prose
↓
semantic audits
↓
deterministic manuscript
↓
human scientific review
```

The central reproducibility rule is:

> Every reusable prompt is immutable by released version; every project artifact is append-only/versioned; every compiled prompt and raw model output is immutable; every scientific conclusion can be traced both to the prompt lineage that generated it and to the pinned MyLib evidence that authorizes it.

---

# 2. Architectural Principles

## 2.1 Python owns scientific state

Python controls:

- canonical identifiers;
- prompt registry resolution;
- prompt compilation;
- hashes;
- file/version validation;
- MyLib pinning;
- evidence retrieval;
- state transitions;
- receipts;
- citation authorization;
- evidence-closure checks;
- deterministic assembly.

LLMs return proposals only.

## 2.2 Deep Research is external/manual

Pattern:

```text
VibeReview compiles immutable prompt
↓
human copies exact prompt into ChatGPT Deep Research
↓
human saves raw report unchanged
↓
VibeReview registers report + hashes
↓
existing discovery pipeline parses report
```

VibeReview does not automate the Deep Research UI.

## 2.3 MyLib is the evidence boundary

```text
Deep Research
= discovery

MyLib
= evidence source
```

Deep Research output never directly becomes canonical evidence.

## 2.4 Research outline and manuscript outline are different

```text
research outline
= what should be investigated

manuscript outline
= what the validated evidence justifies presenting
```

## 2.5 Evidence closure is mandatory

For every claim:

```text
all canonical EvidenceRecords
↓
all canonical ClaimPaperEvidence
↓
FinalClaimValidation
↓
ClaimPacket
```

No canonical support, contradiction, qualification, contextual evidence, or unclear evidence may silently disappear.

---

# 3. Reproducibility Model

Three forms of immutability are required.

## 3.1 Protocol immutability

Released reusable prompts and focus modules never change in place.

Example:

```text
DR110@1.0.0
```

always resolves to identical bytes.

An improved prompt becomes:

```text
DR110@1.1.0
```

## 3.2 Project-history immutability

Approved project artifacts are never overwritten.

Use:

```text
project_brief_v001.md
project_brief_v002.md

protocol_v001.md
protocol_v002.md

structure_v001.md
structure_v002.md

outline_v001.md
outline_v002.md
```

Convenience files such as `outline.md` may point to the currently approved version, but the versioned artifact remains authoritative.

## 3.3 Run immutability

Every AI or Deep Research run stores:

```text
compiled_prompt.md
raw_output.md
run.json
```

These are immutable. A repeat execution creates a new run.

---

# 4. Reusable Prompt Classes

## 4.1 Core prompt

Reusable scientific procedure.

Example:

```text
DR110 — section-specific Deep Research
```

## 4.2 Focus module

Reusable domain-specific instruction.

Example:

```text
focus/edm@1.0.0
```

## 4.3 Project input

Project-specific scientific artifact.

Example:

```text
outline_v002.md#S03.2
```

## 4.4 Project supplement

Project-specific additional instruction.

Example:

```text
supplements/S03.2_v001.md
```

## 4.5 Compiled prompt

Exact text shown to the model.

Example:

```text
compiled/DR007_S03.2.md
```

Only compiled prompts are executed.

---

# 5. Prompt Lifecycle

Every reusable prompt has one of three states:

```text
DRAFT
RELEASED
DEPRECATED
```

### DRAFT
Editable; not allowed in production/reproducible runs.

### RELEASED
Immutable; hash registered; valid for project pinning.

### DEPRECATED
Still immutable and replayable; not recommended for new projects.

Released prompt versions are never deleted.

---

# 6. Prompt Semantic Versioning

Use:

```text
MAJOR.MINOR.PATCH
```

### PATCH
Editorial clarification with no intended scientific-behaviour change.

### MINOR
Scientifically meaningful compatible instruction change.

### MAJOR
Task contract or expected output changes materially.

Every release requires a scientific change note.

---

# 7. Immutable Core Prompt Library

Initial release:

```text
VibeReview Review Protocol 1.0
```

## Project planning

```text
P001@1.0.0 — Project Brief
P010@1.0.0 — Scientific Structure Generation
P020@1.0.0 — Provisional Research Outline
P030@1.0.0 — Outline Challenge
P040@1.0.0 — Outline Revision Proposal
```

## Deep Research

```text
DR100@1.0.0 — Broad Scientific Scoping
DR110@1.0.0 — Section-Specific Deep Research
DR120@1.0.0 — Adversarial Review Deep Research
DR130@1.0.0 — Recent-Literature Materiality Check
```

## Candidate claims

```text
C200@1.0.0 — Atomic Candidate-Claim Generation
C210@1.0.0 — Candidate-Claim Normalization
C220@1.0.0 — Candidate-Claim Challenge
```

## Evidence synthesis

```text
S300@1.0.0 — Evidence-Constrained Claim Synthesis
S310@1.0.0 — Cross-Section Claim Synthesis
```

## Manuscript

```text
M400@1.0.0 — Manuscript-Outline Derivation
M410@1.0.0 — Manuscript Section Contract
M420@1.0.0 — Proposition Generation
M430@1.0.0 — Controlled Prose Rendering
M440@1.0.0 — Proposition Audit
M450@1.0.0 — Sentence Audit
```

---

# 8. Immutable Focus Modules

Initial manufacturing module pack:

```text
manufacturing_ai@1.0.0
edm@1.0.0
ecm@1.0.0
laser_precision@1.0.0
other_nonconventional@1.0.0
sensing@1.0.0
ai_validation@1.0.0
autonomy@1.0.0
physics_informed_ai@1.0.0
atomic_manufacturing@1.0.0
```

Focus modules are reusable and immutable by version. They contain domain emphasis, not project-specific conclusions.

---

# 9. Prompt Registry

Add:

```text
src/vibereview/prompts/registry.yaml
```

Every released prompt entry contains:

```yaml
prompt_id:
version:
path:
sha256:
status:
scientific_role:
canonical_evidence:
release_date:
change_note:
```

Registry validation fails if prompt bytes do not match the recorded hash.

A protocol bundle may pin all prompt versions:

```yaml
protocols:
vibereview-review-protocol-1.0:
status: released
prompts:
project_brief: P001@1.0.0
structure: P010@1.0.0
outline: P020@1.0.0
outline_challenge: P030@1.0.0
outline_revision: P040@1.0.0
scoping_research: DR100@1.0.0
section_research: DR110@1.0.0
adversarial_research: DR120@1.0.0
recent_check: DR130@1.0.0
claim_generation: C200@1.0.0
claim_normalization: C210@1.0.0
claim_challenge: C220@1.0.0
claim_synthesis: S300@1.0.0
cross_section_synthesis: S310@1.0.0
manuscript_outline: M400@1.0.0
section_contract: M410@1.0.0
propositions: M420@1.0.0
rendering: M430@1.0.0
proposition_audit: M440@1.0.0
sentence_audit: M450@1.0.0
```

---

# 10. Source-Repository Layout

```text
src/vibereview/prompts/
├── registry.yaml
├── project/
│   ├── P001/1.0.0.md
│   ├── P010/1.0.0.md
│   ├── P020/1.0.0.md
│   ├── P030/1.0.0.md
│   └── P040/1.0.0.md
├── deep_research/
│   ├── DR100/1.0.0.md
│   ├── DR110/1.0.0.md
│   ├── DR120/1.0.0.md
│   └── DR130/1.0.0.md
├── claims/
│   ├── C200/1.0.0.md
│   ├── C210/1.0.0.md
│   └── C220/1.0.0.md
├── synthesis/
│   ├── S300/1.0.0.md
│   └── S310/1.0.0.md
├── manuscript/
│   ├── M400/1.0.0.md
│   ├── M410/1.0.0.md
│   ├── M420/1.0.0.md
│   ├── M430/1.0.0.md
│   ├── M440/1.0.0.md
│   └── M450/1.0.0.md
└── focus/
├── manufacturing_ai/1.0.0.md
├── edm/1.0.0.md
├── ecm/1.0.0.md
├── laser_precision/1.0.0.md
├── other_nonconventional/1.0.0.md
├── sensing/1.0.0.md
├── ai_validation/1.0.0.md
├── autonomy/1.0.0.md
├── physics_informed_ai/1.0.0.md
└── atomic_manufacturing/1.0.0.md
```

No real review data are committed here.

---

# 11. External Review-Project Layout

```text
reviews/AI_NCM_Review/
├── project.yaml
├── project/
│   ├── project_brief_v001.md
│   ├── protocol_v001.md
│   ├── structure_v001.md
│   └── prompt_profile_v001.yaml
├── outline/
│   ├── outline_v001.md
│   ├── outline_v002.md
│   ├── outline_changes.md
│   └── current
├── supplements/
├── deep_research/
│   ├── compiled/
│   ├── runs/
│   ├── raw/
│   └── parsed/
├── claims/
│   ├── candidate/
│   ├── normalized/
│   └── synthesis/
├── corpus/
│   ├── selection/
│   ├── source_quality/
│   ├── aliases/
│   └── coverage/
├── benchmarks/
│   ├── retrieval/
│   └── semantic/
├── analysis/
│   ├── evidence/
│   ├── claim_packets/
│   ├── matrices/
│   └── audits/
├── manuscript/
│   ├── outline/
│   ├── section_contracts/
│   ├── propositions/
│   ├── sections/
│   └── review.md
└── run_manifests/
```

This stays outside the public VibeReview source repository.

---

# 12. Project Configuration

Example `project.yaml`:

```yaml
project_id: AI_NCM_2026

review_protocol:
id: vibereview-review-protocol-1.0

review_mode:
critical_narrative

working_topic:
AI-assisted precision non-conventional manufacturing

core_processes:
- edm
- ecm
- laser_precision

secondary_processes:
- ultrasonic
- abrasive_jet
- waterjet
- ecdm
- hybrid

future_outlook:
- atomic_manufacturing

publication_window:
primary_start: 2015
cutoff: 2026-09-14
historical_landmarks: true

systematic_search_claim:
false

gap_claims_allowed:
false

mylib:
source: external/MyLib
require_pinned_commit: true
require_read_only: true
```

---

# 13. Prompt Profile

Example `prompt_profile_v001.yaml`:

```yaml
prompt_profile_version: 1

protocol:
id: vibereview-review-protocol-1.0

focus_modules:
general:
- manufacturing_ai@1.0.0

S03:
- edm@1.0.0

S04:
- ecm@1.0.0

S05:
- laser_precision@1.0.0

S07:
- sensing@1.0.0

S09:
- ai_validation@1.0.0

S10:
- physics_informed_ai@1.0.0

S11:
- autonomy@1.0.0

S12:
- atomic_manufacturing@1.0.0
```

Projects are never silently migrated to later prompt releases.

---

# 14. Deterministic Prompt Compiler

Implement:

```python
compile_prompt(
        prompt_ref="DR110@1.0.0",
        project_inputs=[...],
        focus_modules=[...],
        supplement=None,
        )
```

Required behavior:

1. resolve exact prompt version through registry;
2. verify prompt SHA-256;
3. verify focus-module hashes;
4. load project artifacts;
5. extract exact outline section;
6. canonicalize component order;
7. compile exact model-facing text;
8. hash compiled bytes;
9. emit manifest;
10. reject unregistered/manual modifications.

Frozen compilation order:

```text
core prompt
↓
project brief
↓
protocol
↓
exact outline section
↓
focus modules in declared order
↓
project supplement
↓
output contract
```

Same components must produce byte-identical compiled prompts.

---

# 15. Compiled Prompt Manifest

Each compiled prompt has:

```text
DR007_S03.2.md
DR007_S03.2.manifest.json
```

Manifest fields:

```json
{
    "compiled_prompt_id": "DR007",
        "prompt": {
            "id": "DR110",
            "version": "1.0.0",
            "sha256": "..."
        },
        "modules": [
        {
            "id": "edm",
            "version": "1.0.0",
            "sha256": "..."
        },
        {
            "id": "sensing",
            "version": "1.0.0",
            "sha256": "..."
        }
        ],
            "inputs": [
            {
                "path": "project/project_brief_v001.md",
                "sha256": "..."
            },
            {
                "path": "project/protocol_v001.md",
                "sha256": "..."
            },
            {
                "path": "outline/outline_v002.md",
                "sha256": "..."
            }
            ],
                "section_id": "S03.2",
                "supplement": null,
                "compiled_sha256": "...",
                "compiler_version": "..."
}
```

---

# 16. Run Manifest

Every model/Deep Research execution stores:

```text
runs/DR007/
├── compiled_prompt.md
├── raw_output.md
└── run.json
```

`run.json` records:

```json
{
    "run_id": "DR007",
        "compiled_prompt_sha256": "...",
        "output_sha256": "...",
        "provider": "ChatGPT",
        "mode": "Deep Research",
        "model": "platform-reported-if-available",
        "model_version": "unknown-if-not-reported",
        "started_at": "...",
        "completed_at": "...",
        "operator": "...",
        "manual_ui_boundary": true
}
```

Unknown metadata are stored as `unknown`, never inferred.

---

# 17. Manual Deep Research Procedure

For each Deep Research run:

1. compile prompt;
2. validate manifest;
3. open exact compiled prompt;
4. copy without editing;
5. run Deep Research;
6. save raw report unchanged;
7. register report;
8. compute output SHA-256;
9. save run manifest;
10. parse through VibeReview discovery task.

If extra instructions are required:

```text
create supplement version
↓
recompile
↓
new run
```

Manual editing of compiled prompts is forbidden.

---

# 18. Stage A — Project Brief

Run:

```text
P001@1.0.0
```

Output:

```text
project_brief_v001.md
```

Expected project definition:

```text
Working topic:
AI-assisted precision non-conventional manufacturing

Core:
EDM
ECM
precision laser machining

Secondary:
other/hybrid non-conventional processes

Future direction:
atomic / near-atomic manufacturing

Review type:
critical narrative with structured evidence synthesis

Primary question:
How is AI used for prediction, optimization, monitoring,
    control and autonomy in precision non-conventional
    manufacturing, and what prevents robust transferable
    closed-loop intelligence?
    ```

    Human Gate H1: approve project brief.

    ---

# 19. Stage B — Review Protocol

    Create:

    ```text
    protocol_v001.md
    ```

    Record:

    - critical narrative review;
    - curated corpus;
    - no systematic-search completeness claim;
    - no meta-analysis;
    - primary period 2015–2026;
    - historical landmarks permitted;
    - gap claims publication-disabled;
    - MyLib is evidence source;
    - Deep Research is discovery-only;
    - atomic manufacturing is outlook-only.

    Human Gate H2.

    ---

# 20. Stage C — Scientific Structure

    Compile:

    ```text
    P010@1.0.0
    +
    project_brief_v001
    +
    protocol_v001
    ```

    Require alternatives:

    ```text
    process-centered
    AI-function-centered
    hybrid
    ```

    Expected selection: hybrid.

    Save:

    ```text
    structure_v001.md
    ```

    Human Gate H3.

    ---

# 21. Stage D — Broad Deep Research

    Compile:

    ```text
    DR100@1.0.0
    +
    project brief
    +
    protocol
    ```

    Run:

    ```text
    DR001_scoping
    ```

    Extract:

    - terminology;
    - themes;
    - AI tasks;
    - process families;
    - primary studies;
    - reviews;
    - contradictions;
    - methodological differences;
    - missing branches;
    - candidate claims.

    No canonical evidence is created.

    ---

# 22. Stage E — Provisional Research Outline

    Compile:

    ```text
    P020@1.0.0
    +
    project brief
    +
    protocol
    +
    structure
    +
    DR001 parsed discovery summary
    ```

    Output:

    ```text
    outline_v001.md
    ```

    Initial expected structure:

    ```text
    S01 Introduction

    S02 Scope, definitions and evaluation framework

    S03 AI in EDM
    S03.1 prediction
    S03.2 optimization
    S03.3 monitoring
    S03.4 adaptive control
    S03.5 limitations/generalization

    S04 AI in ECM
    S04.1 prediction
    S04.2 geometry/process modelling
    S04.3 optimization
    S04.4 monitoring/control
    S04.5 limitations

    S05 AI in precision laser machining
    S05.1 outcome prediction
    S05.2 image/spectral monitoring
    S05.3 optimization
    S05.4 adaptive control
    S05.5 generalization

    S06 Other and hybrid processes

    S07 Cross-process AI functions

    S08 Data and sensing

    S09 Scientific robustness and reproducibility

    S10 Physics-informed and transferable AI

    S11 Toward autonomous precision manufacturing

    S12 Atomic / near-atomic manufacturing outlook

    S13 Conclusions
    ```

    Human Gate H4.

    ---

# 23. Stage F — Outline Challenge

    Compile:

    ```text
    P030@1.0.0
    +
    project brief
    +
    protocol
    +
    DR001
    +
    outline_v001
    ```

    Challenge classifications:

    ```text
    KEEP
    RENAME
    MERGE
    SPLIT
    MOVE
    ADD
    REMOVE
    UNRESOLVED
    ```

    Human decides each item.

    Create:

    ```text
    outline_v002.md
    ```

    Never overwrite `outline_v001.md`.

    Human Gate H5.

    ---

# 24. Stage G — Section-Specific Deep Research

    For each major section:

    ```text
    DR110@1.0.0
    +
    project brief
    +
    protocol
    +
    outline_v002#section
    +
    focus modules
    +
    optional supplement
    ```

    Suggested runs:

    ```text
    DR002_S03_EDM
    DR003_S04_ECM
    DR004_S05_LASER
    DR005_S06_OTHER
    DR006_S07_CROSS_PROCESS
    DR007_S08_SENSING
    DR008_S09_VALIDATION
    DR009_S10_PHYSICS_AI
    DR010_S11_AUTONOMY
    DR011_S12_ATOMIC
    ```

    Each report must seek:

    - current understanding;
    - primary studies;
    - contradictions;
    - null findings;
    - qualifications;
    - boundaries;
    - alternatives;
    - methodological weaknesses;
    - generalization problems;
    - candidate claims;
    - outline-challenging evidence.

    ---

# 25. Stage H — Candidate Claims

    Compile:

    ```text
    C200@1.0.0
    +
    outline section
    +
    relevant DR reports
    ```

    Target:

    ```text
    60–90 raw candidate claims
    ```

    Normalize to approximately:

    ```text
    35–55 substantive claims
    ```

    Each claim stores:

    - statement;
    - section ID;
    - Deep Research run origin;
    - source references;
    - claim type;
    - scope;
    - retrieval hints.

    ---

# 26. Stage I — Claim Normalization

    Compile:

    ```text
    C210@1.0.0
    +
    all candidate claims
    +
    outline_v002
    ```

    Allowed relationships:

    ```text
    DUPLICATE_OF
    NARROWS
    BROADENS
    OVERLAPS
    CONTRADICTS
    RELATED_MECHANISM
    ```

    Outputs:

    ```text
    claims_normalized_v001
    claim_mapping_v001
    ```

    Human Gate H6 for consequential merges/splits.

    ---

# 27. Stage J — MyLib Corpus

    Reuse current MyLib operational substrate.

    Process:

    ```text
    inspect pinned MyLib
    ↓
    bibliographic reconciliation
    ↓
    source-quality assessment
    ↓
    explicit corpus selection
    ↓
    freeze/import corpus
    ↓
    corpus coverage assessment
    ```

    Source-quality states:

    ```text
    READABLE
    READABLE_WITH_ARTIFACTS
    MATERIAL_EXTRACTION_PROBLEM
    UNUSABLE
    ```

    Defective sources cannot authorize evidence.

    Human Gate H7 for exceptions.

    ---

# 28. Stage K — Corpus Coverage

    Evaluate:

    - outline themes;
    - process families;
    - AI functions;
    - DR-identified important papers;
    - contradictions;
    - materials;
    - methodologies;
    - periods;
    - boundary conditions.

    Statuses:

    ```text
    ADEQUATE_FOR_DECLARED_SCOPE
    PARTIAL
    KNOWN_GAPS
    INADEQUATE
    ```

    If inadequate:

    ```text
    expand MyLib
    or
    narrow review scope
    ```

    Never compensate with stronger wording.

    ---

# 29. Stage L — Adversarial Retrieval

    Every substantive claim receives multiple intents:

    ```text
    support
    contradiction
    boundary
    alternative
    method_challenge
    null_result
    ```

    Terminology variants are included in query text.

    Support-only retrieval is prohibited.

    ---

# 30. Stage M — Retrieval Coverage

    Measure:

    - intents executed;
    - query count;
    - paper diversity;
    - terminology diversity;
    - candidate counts;
    - duplicate rate;
    - known-paper recovery;
    - known-passage recovery.

    Status:

    ```text
    ADEQUATE_FOR_SYNTHESIS
    PARTIAL
    INADEQUATE
    ```

    Keep separate:

    ```text
    corpus coverage
    ≠
    retrieval coverage
    ≠
    evidence closure
    ```

    ---

# 31. Stage N — Human Semantic Benchmark

    Before live LLM evidence assessment, build:

    ```text
    50–100 adjudicated claim–passage pairs
    ```

    Recommended composition:

    ```text
    support: 15
    contradiction: 15
    qualification: 15
    contextual: 10
    unclear: 10
    irrelevant/null/boundary: 15–35
    ```

    Include EDM, ECM, laser, and at least one secondary process.

    Record:

    - exact claim;
    - exact pinned passage;
    - human label;
    - rationale;
    - reviewers;
    - disagreement;
    - development/evaluation split.

    Metrics:

    - per-class precision;
    - per-class recall;
    - confusion matrix;
    - false-support rate;
    - contradiction recall;
    - qualification recall.

    Primary safety metric:

    ```text
    FALSE SUPPORT RATE
    ```

    Human Gate H8.

    ---

# 32. Stage O — Live Semantic Pilot

    Only after separate authorization.

    Use one provider/model.

    Pilot:

    ```text
    5 real papers
    3–5 candidate claims
    support + contradiction + qualification
    ```

    Compare model output with human benchmark.

    Do not model-shop for preferred scientific conclusions.

    ---

# 33. Stage P — Evidence Assessment

    Canonical relations:

    ```text
    SUPPORTS
    CONTRADICTS
    QUALIFIES
    CONTEXTUAL
    UNCLEAR
    ```

    Evaluate:

    - directness;
    - methodological relevance;
    - strength;
    - assessability;
    - limitations.

    Retrieved spans remain noncanonical candidates until assessment.

    ---

# 34. Stage Q — Paper-Level Aggregation

    For every:

    ```text
(claim, paper)
    ```

    aggregate complete canonical evidence into:

    ```text
    ClaimPaperEvidence
    ```

    Multiple passages from one paper do not count as multiple independent studies.

    ---

# 35. Stage R — Claim Synthesis

    Compile:

    ```text
    S300@1.0.0
    +
    complete ClaimPaperEvidence
    +
    retrieval coverage
    +
    source quality
    +
    known study dependence
    ```

    Do not vote count.

    Consider:

    - directness;
    - scope;
    - methodological relevance;
    - generalization;
    - heterogeneity;
    - contradictions;
    - boundaries;
    - study independence;
    - source quality.

    Possible outcomes:

    ```text
    RETAIN
    NARROW
    REVISE
    REJECT
    UNCLEAR
    ```

    Revised claims require revalidation.

    Human Gate H9 for major interpretations.

    ---

# 36. Stage S — Final Claim Validation

    Validate:

    - scope;
    - certainty;
    - causal wording;
    - numerical precision;
    - retrieval adequacy;
    - evidence completeness;
    - contradictions;
    - qualifications;
    - source concerns;
    - corpus limitations.

    Only valid claims generate:

    ```text
    ClaimPacket
    ```

    ClaimPacket remains the manuscript authorization boundary.

    ---

# 37. Stage T — Manuscript Outline

    Compile:

    ```text
    M400@1.0.0
    +
    research outline
    +
    ClaimPackets
    +
    rejected claims
    +
    contradictions
    +
    qualifications
    +
    evidence density
    +
    known limitations
    ```

    Output:

    ```text
    manuscript_outline_v001.md
    ```

    The manuscript outline may merge, split, delete, reorder, or convert research sections into controversy/limitations sections.

    Human Gate H10.

    ---

# 38. Stage U — Section Contracts

    Compile:

    ```text
    M410@1.0.0
    ```

    Each manuscript section defines:

    - purpose;
    - authorized ClaimPackets;
    - required contradiction;
    - required qualification;
    - known uncertainty;
    - prohibited assertions;
    - figures/tables.

    No prose generation without a section contract.

    ---

# 39. Stage V — Proposition Generation

    Compile:

    ```text
    M420@1.0.0
    +
    section contract
    +
    authorized ClaimPackets
    ```

    Each proposition contains approximately one substantive scientific assertion.

    No raw evidence-to-prose shortcut.

    ---

# 40. Stage W — Proposition Audit

    Compile:

    ```text
    M440@1.0.0
    ```

    Check:

    - proposition classification;
    - ClaimPacket authorization;
    - scope;
    - certainty;
    - citation binding;
    - unsupported additions;
    - overstatement.

    Only passing propositions proceed.

    ---

# 41. Stage X — Controlled Prose Rendering

    Compile:

    ```text
    M430@1.0.0
    ```

    The LLM may improve:

    - syntax;
    - flow;
    - readability;
    - terminology;
    - paragraph transitions.

    It may not introduce new scientific propositions.

    ---

# 42. Stage Y — Sentence Audit

    Compile:

    ```text
    M450@1.0.0
    ```

    Every substantive sentence must pass:

    ```text
    ENTAILED
    ```

    before canonical manuscript inclusion.

    ---

# 43. Stage Z — Deterministic Assembly

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

    No unrestricted scientific rewriting occurs after final sentence audit.

    Repeated assembly of unchanged canonical state must be byte-stable.

    ---

# 44. Adversarial Deep Research

    After first draft compile:

    ```text
    DR120@1.0.0
    ```

    Run:

    ```text
    DR090_adversarial
    ```

    Seek:

    - contradictory studies;
    - null results;
    - alternative mechanisms;
    - methodological objections;
    - missing materials/populations;
    - missed landmark papers;
    - recent challenges.

    Relevant literature returns through:

    ```text
    MyLib
    ↓
    new pin
    ↓
    new corpus generation
    ↓
    affected claims rerun
    ```

    Never patch the manuscript directly.

    ---

# 45. Recent-Literature Check

    Before submission:

    ```text
    DR130@1.0.0
    ```

    Run:

    ```text
    DR099_recent
    ```

    Only studies capable of materially changing conclusions should be returned.

    Relevant papers re-enter through MyLib and invalidate affected downstream state.

    ---

# 46. Atomic-Manufacturing Policy

    Atomic manufacturing remains:

    ```text
    future_outlook_only
    ```

    Compile:

    ```text
    DR110@1.0.0
    +
    atomic_manufacturing@1.0.0
    +
    S12
    ```

    Require distinction among:

    ```text
    demonstrated capability
    plausible methodological transfer
    speculative extrapolation
    ```

    Frame the connection as transfer of methods:

    - physics-informed learning;
    - active learning;
    - uncertainty-aware control;
    - inverse design;
    - autonomous metrology;
    - Bayesian experimental design.

    Do not claim direct process continuity from EDM/ECM/laser to atomic manufacturing.

    ---

# 47. Invalidation Rules

## Discovery prompt update

    A material DR prompt change may invalidate:

    - rerun choice for DR reports;
    - parsed discovery;
    - candidate claims;
    - discovery-based coverage conclusions.

    It does not automatically invalidate independently verified MyLib evidence unless claim definitions change.

## Claim-synthesis prompt update

    Potentially invalidates:

    - claim synthesis;
    - final validation;
    - ClaimPackets;
    - downstream propositions/manuscript.

## Manuscript prompt update

    Does not invalidate evidence or ClaimPackets.

    It invalidates:

    - affected propositions;
    - prose;
    - audits;
    - manuscript assembly.

    Implement explicit project-artifact dependency tracking.

    ---

# 48. Human Gates

    Mandatory human gates:

    ```text
    H1 project brief
    H2 review protocol
    H3 scientific structure
    H4 provisional outline
    H5 outline challenge decisions
    H6 consequential claim normalization
    H7 corpus/source-quality exceptions
    H8 semantic benchmark labels
    H9 major claim interpretations
    H10 manuscript outline
    H11 final scientific manuscript review
    ```

    Human changes create new versions; they never silently rewrite prior artifacts.

    ---

# 49. CLI Roadmap

## Prompt registry

    ```bash
    vibereview prompts list
    vibereview prompts show DR110@1.0.0
    vibereview prompts verify
    vibereview prompts status
    ```

## Project

    ```bash
    vibereview project init reviews/AI_NCM_Review
    vibereview project status
    ```

## Compile

    ```bash
    vibereview prompts compile P010 --project reviews/AI_NCM_Review

    vibereview prompts compile P020 --project reviews/AI_NCM_Review

    vibereview prompts compile-dr   --project reviews/AI_NCM_Review   --section S03
    ```

## Register Deep Research

    ```bash
    vibereview research register-output   --run DR002   --prompt compiled/DR002_S03.md   --output deep_research/raw/DR002_S03.md
    ```

## Discovery and claims

    ```bash
    vibereview discovery parse DR002
    vibereview claims generate --section S03
    vibereview claims normalize
    ```

## Manuscript

    ```bash
    vibereview manuscript outline
    vibereview manuscript section-contract S03
    vibereview manuscript propositions S03
    vibereview manuscript audit-propositions S03
    vibereview manuscript render S03
    vibereview manuscript audit-sentences S03
    vibereview manuscript assemble
    ```

    ---

# 50. Prompt-Infrastructure Tests

## Registry tests

    - released prompt hash matches;
    - changed released prompt fails;
    - missing prompt fails;
    - duplicate prompt/version fails;
    - deprecated prompt remains resolvable.

## Compilation tests

    - same inputs produce byte-identical prompt;
    - input ordering is deterministic;
    - exact section extraction is deterministic;
    - focus-module order is deterministic;
    - supplement is represented in manifest;
    - unknown/unreleased prompt fails.

## Run-registration tests

    - raw output hash recorded;
    - compiled prompt hash must match;
    - existing run cannot be overwritten;
    - unknown provider metadata are explicit;
    - manual prompt modification fails.

## Versioning tests

    - released prompt cannot be modified;
    - project migration creates a new prompt profile;
    - old project remains replayable.

    ---

# 51. Scientific Prompt Regression Tests

    `DR110` must always require:

    - primary studies;
    - contradictions;
    - null findings;
    - boundary conditions;
    - alternative explanations;
    - methodological weaknesses;
    - generalization issues;
    - evidence that challenges the outline.

    `C200` must require:

    - atomic claims;
    - explicit scope;
    - restrained causal language;
    - retrieval hints.

    `S300` must prohibit:

    - simple vote counting;
    - omission of contradictory evidence.

    `M430` must prohibit:

    - new scientific propositions.

    `M450` must require:

    - entailment assessment.

    ---

# 52. Implementation Milestones

## P0 — Prompt architecture authorization

    Deliver:

    - this plan;
    - normative prompt protocol;
    - immutability/version rules;
    - manifest formats.

    Exit:

    ```text
    prompt architecture approved
    ```

## P1 — Prompt registry

    Implement registry, hashes, resolver, immutable releases.

    Exit:

    ```text
    released prompt bytes cannot change undetected
    ```

## P2 — Prompt compiler

    Implement deterministic compilation.

    Exit:

    ```text
    same inputs → same compiled bytes
    ```

## P3 — Project artifact versioning

    Implement versioning for:

    - project brief;
    - protocol;
    - structure;
    - outline;
    - supplements;
    - prompt profile.

    Exit:

    ```text
    approved project artifacts are append-only
    ```

## P4 — Deep Research run registration

    Implement compile → save prompt → register raw report → hash → manifest.

    Exit:

    ```text
    every DR report traces to exact prompt + inputs
    ```

## P5 — Real AI-NCM planning run

    Generate:

    ```text
    project_brief_v001
    protocol_v001
    structure_v001
    DR001_scoping
    outline_v001
    outline challenge
    outline_v002
    ```

    Exit:

    ```text
    research plan reproducible
    ```

## P6 — Section Deep Research

    Run all major section DR prompts.

    Exit:

    ```text
    all section reports immutable and provenance-bound
    ```

## P7 — Candidate claims

    Generate and normalize claims.

    Exit:

    ```text
    claims trace to exact DR + outline provenance
    ```

## P8 — MyLib evidence integration

    Reuse `mylib-operational-slice-m6`.

    Exit:

    ```text
    real claims reach complete ClaimPackets
    ```

## P9 — Live semantic benchmark/pilot

    Create real benchmark and authorize one live provider.

    Exit:

    ```text
    semantic performance characterized
    ```

## P10 — Adversarial mini-review

    Use 15–20 deliberately conflicting real papers.

    Exit:

    ```text
    contradictions survive synthesis
    ```

## P11 — Manuscript vertical slice

    Produce one complete real manuscript section.

    Exit:

    ```text
    sentence
    → proposition
    → ClaimPacket
    → evidence
    → pinned Markdown
    ```

## P12 — Complete review

    Produce full AI-assisted precision non-conventional manufacturing review.

    Exit:

    ```text
    complete evidence-constrained manuscript + human review packet
    ```

    ---

# 53. Acceptance Criteria

    The complete system must satisfy all of the following.

    1. Every reusable released prompt has stable ID/version/hash.
    2. Released prompt bytes cannot be edited in place.
    3. Old prompt versions remain available.
    4. Every project pins one prompt protocol/profile.
    5. Projects are never silently migrated.
    6. Every focus module is versioned and immutable.
    7. Project artifacts are versioned rather than overwritten.
    8. Every compiled prompt has a manifest.
    9. Every compiled prompt is byte-reproducible from declared inputs.
    10. Manual edits to compiled prompts are detected/prohibited.
    11. Raw Deep Research reports are preserved unchanged.
    12. Every DR output records exact compiled-prompt hash.
    13. Provider/model metadata are recorded where available.
    14. Unknown metadata are explicitly marked unknown.
    15. Deep Research remains discovery-only.
    16. Candidate claims preserve outline + DR provenance.
    17. MyLib remains the evidence boundary.
    18. All evidence resolves to exact pinned Markdown.
    19. Evidence closure remains enforced.
    20. Manuscript propositions derive only from authorized ClaimPackets.
    21. Prose cannot add unauthorized scientific claims.
    22. Every substantive sentence has a passing audit.
    23. Repeated deterministic assembly is byte-stable.
    24. Human decisions are versioned provenance events.
    25. Final manuscript supports both prompt-lineage and evidence-lineage reconstruction.

    ---

# 54. Final Traceability Target

    For every substantive final sentence:

## Evidence lineage

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
    exact Markdown span
    ↓
    MyLib Git blob
    ↓
    pinned MyLib commit
    ```

## Research-generation lineage

    ```text
    sentence
    ↓
    proposition
    ↓
    candidate/final claim
    ↓
    Deep Research report
    ↓
    compiled immutable prompt
    ↓
    core prompt version
    ↓
    focus module version
    ↓
    outline section version
    ↓
    protocol version
    ↓
    project brief version
    ```

    Both lineages must be reconstructible.

    ---

# 55. Immediate Next Actions

    Execute in this order.

    1. Freeze this plan as the implementation roadmap.
    2. Add a normative prompt-protocol document under `docs/operations/`.
    3. Implement `prompts/registry.yaml`.
    4. Implement released-prompt immutability checks.
    5. Create `VibeReview Review Protocol 1.0`.
    6. Implement deterministic prompt compiler + manifest.
    7. Implement external project-artifact versioning.
    8. Implement Deep Research run registration.
    9. Create local `reviews/AI_NCM_Review/`.
    10. Run `P001 → protocol → P010 → DR100 → P020 → P030`.
    11. Freeze `outline_v002`.
    12. Compile and execute section-specific DR runs.
    13. Generate and normalize candidate claims.
    14. Enter the existing MyLib evidence workflow.
    15. Build the human semantic benchmark.
    16. Authorize one live semantic model.
    17. Run the adversarial mini-review.
    18. Produce one audited manuscript section.
    19. Scale to the complete review only after the manuscript vertical slice passes.

    ---

# 56. Final Design Rule

    The reusable VibeReview review pipeline follows:

    ```text
    IMMUTABLE GENERAL PROCEDURE
    +
    VERSIONED PROJECT SCIENCE
    +
    IMMUTABLE MODEL RUNS
    +
    PINNED MYLIB EVIDENCE
    +
    PYTHON-CONTROLLED SCIENTIFIC STATE
    +
    HUMAN SCIENTIFIC AUTHORITY
    ```

    The system must never depend on:

    - an unversioned prompt;
    - an overwritten project artifact;
    - undocumented chat history;
    - an unpinned corpus;
    - manually altered compiled prompts;
    - an unaudited scientific sentence.

    This is the intended reproducibility contract for the review pipeline.
