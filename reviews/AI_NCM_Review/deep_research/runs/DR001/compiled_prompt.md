<!-- vibereview:compiled-prompt id="P001" compiler="vibereview-prompt-compiler-1" -->
<!-- vibereview:component role="core-prompt" ref="P001@1.0.0" sha256="sha256:2dcf5097a1b23016656af12bd54c306cf996237402672123908da254a2c00bcb" -->
# P001 — Project Brief (P001@1.0.0)

## Purpose

Derive a complete, approvable project brief from the working topic supplied by the
operator. The project brief is the root project artifact: every later stage —
protocol, scientific structure, outline, research, and manuscript — traces back to
it. It fixes what the review is about, what it covers, and what it explicitly
refuses to claim.

## Procedure

1. Read the working topic given in the project-config component (and, when one is
   present, the project-brief component) appended after this body by the
   deterministic compiler, and restate the topic verbatim as the working topic
   of the brief.
2. Identify the core processes of the topic: the primary process families that the
   review is fundamentally about. List them individually and describe in one or two
   sentences each why each is core.
3. Identify the secondary processes: process families that matter to the review but
   are not its center of gravity. List them and state how they relate to the core.
4. Identify the future direction of the topic: the horizon the review looks toward
   without claiming it as established capability. State clearly that the future
   direction is treated as outlook only.
5. Fix the review type. The default and expected type is a critical narrative
   review with structured evidence synthesis. State this explicitly and do not
   upgrade it to a systematic-review design.
6. Derive the primary review question as a single interrogative sentence that spans
   the core processes, the AI functions under study (for example prediction,
   optimization, monitoring, control, and autonomy), and the central obstacle the
   review investigates (for example, what prevents robust transferable closed-loop
   intelligence).
7. Define scope boundaries: process families in scope, AI functions in scope,
   period covered, and population of studies considered. Boundaries must be
   concrete enough that a later stage can check coverage against them.
8. State explicit non-goals. The brief must record, at minimum, that the review
   makes no systematic-search completeness claim and performs no meta-analysis.
   Add any further non-goals the topic requires.
9. Cross-check the brief for internal consistency: the primary question must be
   answerable within the stated scope boundaries and must not rely on any excluded
   claim type.
10. Write the brief in the output structure below. Do not invent citations,
    evidence, or literature claims anywhere in the brief; it is a planning artifact
    only.
<!-- vibereview:component role="project-config" ref="project.yaml" sha256="sha256:8184bc979631b70568b19802696a25d19eabda0bcd9d6bd902fea196bf536a46" -->
# VibeReview review-project configuration.
# Instantiated from the packaged placeholder template by `vibereview project
# init`. Every compiled prompt binds this file's hash at compile time.
project_id: "AI_NCM_2026"

review_protocol:
  id: "vibereview-review-protocol-1.0"

review_mode: critical_narrative

working_topic: "AI-assisted precision non-conventional manufacturing"

core_processes: ["edm", "ecm", "laser_precision"]

secondary_processes: ["ultrasonic", "abrasive_jet", "waterjet", "ecdm", "hybrid"]

future_outlook: ["atomic_manufacturing"]

publication_window:
  primary_start: 2015
  cutoff: "2026-09-14"
  historical_landmarks: true

systematic_search_claim: false

gap_claims_allowed: false

mylib:
  source: external/MyLib
  require_pinned_commit: true
  require_read_only: true
<!-- vibereview:component role="output-contract" ref="P001@1.0.0" sha256="sha256:2dcf5097a1b23016656af12bd54c306cf996237402672123908da254a2c00bcb" -->
## Output Contract

```text
PROJECT BRIEF

Working topic:
<verbatim working topic from the compiled project input>

Core processes:
- <process name>: <why this process is core>
- ...

Secondary processes:
- <process name>: <relationship to the core>
- ...

Future direction:
<horizon description, explicitly outlook-only>

Review type:
critical narrative review with structured evidence synthesis

Primary review question:
<one interrogative sentence>

Scope boundaries:
- Processes in scope: ...
- AI functions in scope: ...
- Period: ...
- Study population: ...

Non-goals:
- No systematic-search completeness claim.
- No meta-analysis.
- <any additional non-goals>

Consistency check:
<statement that the primary question is answerable within the stated boundaries>
```

- Every field above must be present; none may be left blank.
- The working topic must appear verbatim.
- Non-goals must include the no-systematic-completeness and no-meta-analysis items.
- The brief must contain no literature claims, no citations, and no invented data.
