<!-- vibereview:compiled-prompt id="P010" compiler="vibereview-prompt-compiler-1" -->
<!-- vibereview:component role="core-prompt" ref="P010@1.0.0" sha256="sha256:2d1e44c3ed8c84403b9ebd9ea6b17eb6c6dccf90361459fe6714171172a9139e" -->
# P010 — Scientific Structure Generation (P010@1.0.0)

## Purpose

Generate candidate scientific structures for organizing the review and recommend
one of them. The scientific structure is the organizing hypothesis of the review:
it decides what the unit of organization is (process, AI function, or a hybrid of
both) and therefore what the research outline, the research runs, and ultimately
the manuscript will be shaped around.

## Procedure

1. Read the project brief and the protocol components appended after this body by
   the deterministic compiler. Treat the project brief as the source of the topic,
   core processes, secondary processes, future direction, review type, primary
   question, scope boundaries, and non-goals. Treat the protocol as binding
   constraints on what the review may claim.
2. Generate at least three candidate scientific structures. The set must include,
   at minimum:
   - one process-centered structure, organized around the process families named
     in the project brief;
   - one AI-function-centered structure, organized around the AI functions named
     in the project brief (for example prediction, optimization, monitoring,
     control, autonomy);
   - one hybrid structure that organizes the main body by process and
     cross-cuts it with AI-function and cross-cutting themes (for example data
     and sensing, robustness, physics-informed methods, autonomy).
   Each candidate must name its organizing unit, its main parts in order, and how
   it maps onto the core and secondary processes of the project brief.
3. Define explicit comparison criteria and apply them identically to every
   candidate. Use, at minimum: faithfulness to the primary review question;
   coverage of the scope boundaries; support for the critical-narrative review
   type; ability to expose contradictions, qualifications, and methodological
   differences; risk of duplication or forced content; and clarity for a reader.
4. Score or rank every candidate against the comparison criteria. Do not rank by
   familiarity or by what a typical survey does; rank by fit to this project
   brief and protocol.
5. Recommend exactly one candidate. State the recommendation with a justification
   that cites the comparison criteria, and state explicitly why the strongest
   alternative was not chosen. If the hybrid structure is recommended, justify the
   cross-cutting placement of themes rather than placing them inside process
   sections.
6. Check that the recommended structure respects every non-goal in the project
   brief and every constraint in the protocol. If it does not, adjust the
   structure, not the constraints.
7. Do not write the research outline here, do not name literature, and do not
   introduce any claim about the state of the field; this stage produces
   organization only.
<!-- vibereview:component role="project-brief" ref="project/project_brief_v001.md" sha256="sha256:44283b1c05ee5a495e88f435ca1dfe820fc2a1e94e5147d1f40625cc0e0b2b77" -->
project_brief v1

Source: run DR001 (ChatGPT Deep Research, compiled P001). Approved at human gate H1 on 2026-09-18.

PROJECT BRIEF

Working topic:
AI-assisted precision non-conventional manufacturing

Core processes:

* Electrical discharge machining (EDM): EDM is a core process because it represents a major precision non-conventional manufacturing family in which process behaviour is governed by coupled electrical, thermal, material-removal, and dynamic phenomena. It provides a principal setting for examining how AI can support prediction, parameter optimization, process monitoring, adaptive control, and progression toward autonomous operation.
* Electrochemical machining (ECM): ECM is a core process because it provides a distinct electrochemical material-removal mechanism with strong dependence on operating conditions, transport phenomena, inter-electrode conditions, and process stability. Its inclusion enables assessment of whether AI methods developed for prediction, optimization, monitoring, and control can generalize across fundamentally different physical mechanisms.
* Laser-based precision manufacturing: Laser-based precision manufacturing is a core process because it represents a major energy-beam-based family of non-conventional processes encompassing highly localized, transient, and strongly coupled thermal–material interactions. It provides a complementary basis for evaluating AI-assisted modelling, quality prediction, parameter optimization, sensing, process monitoring, feedback control, and autonomous decision-making.

Secondary processes:

* Ultrasonic machining: Ultrasonic machining is included as a secondary process to test whether methodological patterns identified in the core processes also appear in mechanically driven non-conventional manufacturing, particularly for modelling, parameter optimization, and monitoring.
* Abrasive jet machining: Abrasive jet machining is included as a secondary process because it extends the review to material-removal systems governed by particle–workpiece interactions and provides comparative evidence on the transferability of AI-based prediction and optimization approaches beyond the three core process families.
* Waterjet machining: Waterjet and abrasive-waterjet machining are included as secondary processes to provide a comparative fluid-jet process family in which AI may be applied to parameter selection, response prediction, quality control, and process monitoring.
* Electrochemical discharge machining (ECDM): ECDM is included as a secondary process because it combines electrochemical and discharge-related mechanisms and therefore provides a useful bridge between process families represented by ECM and EDM.
* Hybrid non-conventional manufacturing processes: Hybrid processes are included where two or more non-conventional mechanisms, or non-conventional and conventional mechanisms, are deliberately combined. They provide a comparative setting for examining whether AI methods can accommodate greater process coupling, multi-objective decision spaces, heterogeneous sensing, and control requirements.

Future direction:
Atomic manufacturing is treated exclusively as an outlook direction. It will be discussed as a possible long-term horizon for increasingly precise, data-rich, model-assisted, and autonomous manufacturing, without presenting atomic-scale manufacturing as an established extension of current AI-assisted non-conventional manufacturing capability. No claim of technological maturity, demonstrated continuity, or direct transferability from the reviewed process families to atomic manufacturing will be made without independently verified evidence in later stages.

Review type:
critical narrative review with structured evidence synthesis

Primary review question:
Across EDM, ECM, and laser-based precision non-conventional manufacturing, how are AI methods being used for prediction, optimization, monitoring, control, and autonomy, and what technical, methodological, data-related, and validation barriers limit their progression toward robust, transferable, closed-loop manufacturing intelligence?

Scope boundaries:

* Processes in scope: The primary scope comprises EDM, ECM, and laser-based precision manufacturing. Ultrasonic machining, abrasive jet machining, waterjet and abrasive-waterjet machining, ECDM, and hybrid non-conventional manufacturing processes are included as secondary comparative process families. Atomic manufacturing is excluded from the principal evidence synthesis and is considered only in the outlook.
* AI functions in scope: AI-assisted prediction of process responses and quality; surrogate and data-driven process modelling; parameter selection and optimization; condition and state monitoring; defect, anomaly, or quality classification; sensor-data interpretation and fusion; adaptive decision-making; feedback and closed-loop control; and approaches directed toward increasing levels of process autonomy. AI includes relevant machine-learning, deep-learning, data-driven, knowledge-based, and hybrid physics–data approaches where they perform one or more of these functions.
* Period: The primary evidence window is 2015 through 14 September 2026. Earlier studies may be included selectively as historical landmarks when required to explain the origin of methods, concepts, process-control approaches, or major technical developments. Historical-landmark inclusion does not expand the primary evidence window.
* Study population: Peer-reviewed studies and other scientifically traceable technical publications that investigate the development, application, validation, or critical assessment of AI, machine-learning, data-driven, knowledge-based, or hybrid computational methods in the in-scope manufacturing processes. For the core synthesis, priority is given to studies that connect computational methods to measurable process variables, process states, manufacturing outcomes, monitoring signals, optimization objectives, control actions, or autonomy-related functions. Secondary-process studies are used primarily for comparison, boundary testing, and identification of cross-process methodological patterns.

Non-goals:

* No systematic-search completeness claim.
* No meta-analysis.
* No claim that the assembled literature corpus exhaustively represents all publications on AI-assisted non-conventional manufacturing.
* No universal research-gap claims inferred solely from the absence of studies in a non-systematically assembled evidence corpus.
* No quantitative pooling of performance metrics taken from heterogeneous datasets, process conditions, machines, materials, sensing systems, validation procedures, or evaluation protocols.
* No assumption that predictive accuracy demonstrated within a single experimental dataset establishes robustness, physical validity, cross-machine transferability, industrial readiness, or closed-loop autonomy.
* No assumption that methods developed for one process family are directly transferable to another without process-specific evidence.
* No attempt to provide an exhaustive review of conventional machining, additive manufacturing, or general smart-manufacturing research unless such work is required to clarify an explicitly relevant concept or comparison.
* No treatment of atomic manufacturing as an established member of the reviewed process families; it remains an outlook topic only.
* No invention of missing evidence, citations, performance values, process capabilities, or literature-based conclusions during the planning stage.

Consistency check:
The primary review question is answerable within the stated boundaries because it is centred on the three designated core process families and on explicitly defined AI functions ranging from prediction and optimization to monitoring, control, and autonomy. The primary 2015–14 September 2026 evidence window provides the main temporal boundary, while selectively included historical landmarks serve only contextual purposes. Secondary processes are restricted to comparative evidence rather than being assigned equal coverage with the core processes, and atomic manufacturing is confined to the outlook. The question requires critical assessment of barriers to robustness, transferability, and closed-loop intelligence but does not require systematic-search completeness, meta-analysis, exhaustive field coverage, or unsupported research-gap claims. It is therefore consistent with a critical narrative review using structured evidence synthesis.
<!-- vibereview:component role="protocol" ref="project/protocol_v001.md" sha256="sha256:4a7735989a5276bb8be872ab41f328668e083bbd4d0733682d1fb43cd6356df0" -->
protocol v1

# Review protocol — AI_NCM_2026

Protocol bundle: vibereview-review-protocol-1.0 (prompt profile v002).

Binding constraints for every downstream prompt, run, and manuscript stage:

- Review type: critical narrative review with structured evidence synthesis.
- Corpus: curated corpus; no systematic-search completeness claim.
- No meta-analysis and no quantitative pooling of performance metrics from
  heterogeneous datasets, process conditions, machines, materials, sensing
  systems, validation procedures, or evaluation protocols.
- Primary period 2015 through 2026-09-14; earlier historical landmarks are
  permitted for context only and do not expand the evidence window.
- Gap claims are publication-disabled: absence of studies in a non-systematically
  assembled corpus must not be asserted as a universal research gap.
- MyLib (pinned, read-only external/MyLib submodule) is the evidence source.
- Deep Research is discovery-only; Deep Research outputs never become canonical
  evidence and no canonical evidence IDs are allocated to them.
- Atomic manufacturing is outlook-only; it is not a reviewed process family and
  no maturity, continuity, or transferability claim may be made for it without
  independently verified evidence in later stages.
- Prompt outputs never become evidence without the existing evidence pipeline
  (every registry entry is canonical_evidence: false).

Awaiting human gate H2.
<!-- vibereview:component role="output-contract" ref="P010@1.0.0" sha256="sha256:2d1e44c3ed8c84403b9ebd9ea6b17eb6c6dccf90361459fe6714171172a9139e" -->
## Output Contract

```text
SCIENTIFIC STRUCTURE CANDIDATES

Candidate 1 — process-centered:
- Organizing unit: process family
- Main parts (in order): ...
- Mapping to project brief: ...

Candidate 2 — AI-function-centered:
- Organizing unit: AI function
- Main parts (in order): ...
- Mapping to project brief: ...

Candidate 3 — hybrid:
- Organizing unit: process family with cross-cutting AI-function themes
- Main parts (in order): ...
- Cross-cutting themes: ...
- Mapping to project brief: ...

<additional candidates, if any>

COMPARISON

Criteria (applied identically to all candidates):
1. Faithfulness to the primary review question
2. Coverage of scope boundaries
3. Fit to critical narrative review with structured evidence synthesis
4. Ability to expose contradictions, qualifications, and methodological differences
5. Risk of duplication or forced content
6. Reader clarity

Comparison table:
<candidate × criterion assessment>

RECOMMENDATION

Recommended structure: <candidate name>
Justification: <cited against the comparison criteria>
Why the strongest alternative was not chosen: <reason>
```

- At least three candidates must appear, including process-centered,
  AI-function-centered, and hybrid.
- Comparison criteria must be stated before the comparison is made and applied
  identically to all candidates.
- Exactly one candidate is recommended, with justification.
- No literature, citations, or field claims may appear.
