# Review-project placeholder template

This directory is the generic scaffold every VibeReview review project is
instantiated from (`vibereview project init` copies these files into the new
project root, substituting every `{{TOKEN}}`). It is tracked on the template
branch and shipped as package data; real review projects never edit these
placeholders — they instantiate from them, then version their own artifacts
append-only.

Tokens:

- `{{PROJECT_ID}}` — canonical project identifier (for example `AI_NCM_2026`).
- `{{WORKING_TOPIC}}` — the review's working topic, verbatim.
- `{{REVIEW_PROTOCOL}}` — pinned protocol bundle id.
- `{{CORE_PROCESSES}}` — YAML flow list of the primary process families.
- `{{SECONDARY_PROCESSES}}` — YAML flow list of secondary process families.
- `{{FUTURE_OUTLOOK}}` — YAML flow list of outlook-only directions.
- `{{PUBLICATION_CUTOFF}}` — ISO publication cutoff date or `null`.

Instantiation fails closed (`PLACEHOLDER_UNFILLED`) when a rendered file still
contains `{{`, so new tokens added here cannot leak into projects silently.
