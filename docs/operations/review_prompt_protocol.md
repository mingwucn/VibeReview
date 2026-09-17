# VibeReview review-prompt protocol

## Status and authority

This document is the normative authorization for the prompt-infrastructure
slice of the reproducible review pipeline planned in `goal.md` (sections 3–17,
49–51 and milestones P0–P4). It authorizes exactly the following and nothing
more:

- P0 — this normative prompt-protocol document.
- P1 — immutable versioned prompt registry (`registry.yaml`), released-prompt
  hash verification, and a fail-closed resolver.
- P2 — deterministic prompt compiler with per-compile manifests.
- P3 — external review-project artifact versioning and prompt profiles.
- P4 — manual Deep Research run registration (compiled prompt → raw report →
  run manifest), with overwrite and tamper detection.

The frozen V1.5.1b scientific models, enums, and validators remain unchanged.
Nothing in this slice creates canonical scientific state: the prompt library,
compiler, and run registry are engineering provenance machinery. Deep Research
remains discovery-only and manually executed; the compiled prompt is copied
verbatim into the provider UI by a human operator.

This document does not authorize a live provider, model spending, automatic
Deep Research execution, an external corpus, human scientific acceptance,
publication, or public export. Milestones P5–P12 of `goal.md` (real planning
runs, section Deep Research, candidate claims against real reports, live
semantic benchmark/pilot, adversarial mini-review, manuscript slice, complete
review) remain unauthorized until separately approved. The existing runtime,
library, and synthetic harness contracts are unchanged; in particular the
legacy flat task prompts under `src/vibereview/prompts/*.md` stay in place and
are consumed by the runtime exactly as before.

## Invariants

1. **Protocol immutability.** A released prompt or focus module
   (`<ID>@<version>`) resolves to identical bytes forever. Byte/hash mismatch
   is a hard failure. Improved prompts are new versions; released versions are
   never deleted or edited in place.
2. **Project-history immutability.** Approved project artifacts are never
   overwritten. New approvals create new `_vNNN` versions; convenience
   pointers (`outline/current`) reference, never replace, versioned artifacts.
3. **Run immutability.** Every registered run stores the exact compiled
   prompt bytes, the raw output bytes, and a run manifest. Runs are
   append-only; re-registration under an existing run ID fails closed.
4. **Determinism.** The same components in the same declared versions produce
   byte-identical compiled prompts and manifests. No timestamps or random
   values enter compiled bytes or manifests.
5. **Fail-closed resolution.** DRAFT prompts are rejected for compilation;
   unknown IDs, unknown versions, hash mismatches, changed inputs, and manual
   edits of compiled prompts all fail closed with explicit codes.
6. **Discovery/evidence separation.** This machinery never creates canonical
   evidence. MyLib remains the sole evidence boundary; Deep Research output
   reaches canonical state only through the separately authorized existing
   evidence pipeline.
7. **Unknown metadata stay unknown.** Provider/model metadata that the
   operator does not record are stored as the literal string `unknown`, never
   inferred.

## Non-goals

- No live or automatic provider execution, no spend, no `external_engine`
  path is added or widened.
- No parsing of registered reports into canonical claims (that is the
  existing runtime discovery task, authorized separately under P5+).
- No database, web UI, generic workflow engine, multi-agent orchestration,
  PDF parsing, or Graphify execution.
- No real review project, corpus content, or operator artifact enters the
  public repository; `reviews/*` stays git-ignored and local-only.
- No change to the frozen scientific contract, task contracts, or the
  synthetic Package C harness.

## Serialized formats

All serialized text is UTF-8. Hashes use the repository-wide
`sha256:<hex>` form (`vibereview.runtime.hashing`). All YAML/JSON below is
normative: validators fail closed on unknown fields
(`extra="forbid"` semantics).

### Prompt registry — `src/vibereview/prompts/registry.yaml`

```yaml
registry_version: 1
protocols:
  vibereview-review-protocol-1.0:
    status: released
    prompts:
      project_brief: P001@1.0.0
      # ... one pinned <ID>@<version> per protocol slot (goal.md §9)
prompts:
  P001@1.0.0:
    class: project
    path: project/P001/1.0.0.md
    sha256: sha256:...
    status: RELEASED
    scientific_role: project brief derivation
    canonical_evidence: false
    release_date: "2026-09-17"
    change_note: Initial release.
focus_modules:
  edm@1.0.0:
    path: focus/edm/1.0.0.md
    sha256: sha256:...
    status: RELEASED
    release_date: "2026-09-17"
    change_note: Initial release.
```

- Key format is `<ID>@<semver>`; duplicate keys fail closed.
- `class` is one of `project`, `deep_research`, `claims`, `synthesis`,
  `manuscript`; focus modules live under `focus_modules`.
- `status` is `DRAFT`, `RELEASED`, or `DEPRECATED`. Only `RELEASED` entries
  may be compiled. `DEPRECATED` entries remain resolvable and replayable.
- `canonical_evidence` is always `false` for every initial release; prompt
  outputs never become evidence without the existing evidence pipeline.
- Every entry’s recorded `sha256` must equal the SHA-256 of the referenced
  file bytes at verification time, regardless of status.
- Prompt files are laid out as `src/vibereview/prompts/<class>/<ID>/<version>.md`
  and `src/vibereview/prompts/focus/<name>/<version>.md` (goal.md §10).

### Core prompt files

Each core prompt markdown file contains exactly one `## Output Contract`
heading. The compiler treats everything before that heading as the procedure
body and everything from that heading to EOF as the output contract; the two
are compiled as separate ordered components (goal.md §14).

### Compiled prompt and manifest

Compilation emits `<compiled_prompt_id>.md` and
`<compiled_prompt_id>.manifest.json`. Compiled bytes are the ordered
concatenation of labeled components in the frozen order:

```text
core prompt body
→ project brief
→ protocol
→ exact outline section
→ focus modules in declared order
→ project supplement
→ output contract
```

- Project inputs are passed as an explicit role→path mapping; roles are
  emitted in the frozen canonical role order
  (`project_config`, `project_brief`, `protocol`, `structure`, `outline`,
  `discovery`, `reports`, `claims`, `packets`, `other`), never caller order.
  `project_config` carries the review project's `project.yaml` so planning
  compiles (for example P001) can bind the working topic before a project
  brief exists.
- The outline section component is the exact byte slice of the pinned outline
  file from the heading that starts with the requested section ID
  (`^(#{1,6})\s+SECTION_ID\b`) to the next heading of equal or higher level.
  A missing or ambiguous section fails closed.
- Component blocks are separated by deterministic XML-style comment banners
  recording component kind, identity, and hash; the emitted file ends with
  exactly one trailing newline.

Manifest schema (`vibereview-compiled-prompt-manifest-1`):

```json
{
  "format_version": "vibereview-compiled-prompt-manifest-1",
  "compiled_prompt_id": "DR007",
  "prompt": {"id": "DR110", "version": "1.0.0", "sha256": "sha256:..."},
  "modules": [{"id": "edm", "version": "1.0.0", "sha256": "sha256:..."}],
  "inputs": [{"role": "project_brief",
              "path": "project/project_brief_v001.md",
              "sha256": "sha256:..."}],
  "section_id": "S03.2",
  "supplement": null,
  "compiled_sha256": "sha256:...",
  "compiler_version": "vibereview-prompt-compiler-1"
}
```

`compiled_sha256` is the SHA-256 of the exact compiled bytes. The manifest
never contains timestamps.

### Review project layout and versioning

External review projects follow goal.md §11. `project init` creates the
directory skeleton plus `project.yaml` (goal.md §12) and
`project/prompt_profile_v001.yaml` (goal.md §13). Artifact families
(`project_brief`, `protocol`, `structure`, `outline`, `prompt_profile`,
`supplement`) version as `<name>_vNNN.md|yaml` with three-digit, gap-free
ordinals; writing an already-used ordinal or changing an existing versioned
file fails closed. The prompt profile pins one protocol and exact
`<ID>@<version>` focus-module refs per outline section; migration to a new
protocol or prompt release writes a new profile version and never edits an
older one, so old projects remain replayable.

### Deep Research run registration

A run registers the immutable triple under `deep_research/runs/<run_id>/`
(`compiled_prompt.md`, `raw_output.md`, `run.json`). Run IDs match
`^[A-Z]{2}[0-9]{3}(_[A-Za-z0-9.]+)?$` (for example `DR001_scoping`,
`DR002_S03`). `run.json` uses format `vibereview-deep-research-run-1`:

```json
{
  "format_version": "vibereview-deep-research-run-1",
  "run_id": "DR002",
  "compiled_prompt_path": "deep_research/compiled/DR002_S03.md",
  "compiled_prompt_sha256": "sha256:...",
  "raw_output_path": "deep_research/raw/DR002_S03.md",
  "raw_output_sha256": "sha256:...",
  "manifest_sha256": "sha256:...",
  "provider": "ChatGPT",
  "mode": "Deep Research",
  "model": "unknown",
  "model_version": "unknown",
  "started_at": "unknown",
  "completed_at": "unknown",
  "operator": "operator-name",
  "manual_ui_boundary": true
}
```

Registration fails closed when: the run already exists; the compiled prompt
bytes differ from its manifest `compiled_sha256` (manual edit detection); the
manifest itself fails revalidation (prompt/module/input hash drift since
compilation); the raw output is missing or empty; or the operator is not
recorded. Timestamps use ISO-8601 UTC or the literal `unknown`.

## Acceptance evidence

- Registry verification passes on the committed library; editing any released
  prompt byte, deleting a prompt file, duplicating a key, or requesting a
  DRAFT/unknown ref fails closed.
- Compilation is byte-reproducible: same components → identical compiled
  bytes and manifest hash; focus-module and input order follow the frozen
  order regardless of caller order; supplements appear in the manifest.
- Run registration is append-only and tamper-evident; unknown provider
  metadata are stored as `unknown`.
- Project artifacts are append-only/versioned; migration writes a new prompt
  profile and leaves the old one intact.
- The scientific regression requirements of goal.md §51 hold as committed
  prompt bytes: `DR110` requires primary studies, contradictions, null
  findings, boundary conditions, alternative explanations, methodological
  weaknesses, generalization issues, and outline-challenging evidence;
  `C200` requires atomic claims, explicit scope, restrained causal language,
  and retrieval hints; `S300` prohibits vote counting and omission of
  contradictory evidence; `M430` prohibits new scientific propositions;
  `M450` requires entailment assessment.
- Verification is recorded in `HANDOFF.md` before push, per `AGENTS.md`.
