# MyLib Integration Operator Guide

## Overview

`mingwucn/MyLib` is integrated into VibeReview as a pinned, read-only Git submodule located at `external/MyLib`.
It serves as an independently maintained repository of literature Markdown, bibliographic entries (`ref.bib`),
and graph exports (`Libs/graphify-out/graph.json`).

VibeReview does not absorb MyLib's codebase, does not execute its writing/drafting pipeline,
and does not expose the entire submodule checkout to semantic agents.

## Architectural Boundaries

1. **Read-Only Access**: All operations on `external/MyLib` are strictly read-only. VibeReview never writes, modifies, or deletes files within the submodule.
2. **Offline Operation**: Normal review execution and inspection commands are completely offline with respect to Git. No `git fetch`, `git pull`, `git checkout`, or upstream update occurs during runtime. Source updates are explicit operator actions.
3. **Network Isolation**: Pinned submodule access operates with `allow_network = false`.
4. **No Code/Pipeline Execution**: MyLib's scripts (e.g. under `pipeline/`), agent configurations (`.agent/`, `.opencode/`, etc.), and LaTeX compilation tools are strictly excluded from execution.
5. **Graph Assertions as Retrieval Leads**: Upstream graph nodes, edges, and attributes (including fields named `evidence`) are treated solely as retrieval leads and metadata annotations. They are never ingested directly as scientific evidence (`EvidenceRecord`) or approved claims without exact-text verification against accepted source Markdown.
6. **Isolated Outputs**: All inspection reports, caches, and review resources are written to VibeReview directories (`work/`, `state/`, or `output/`), never inside `external/MyLib`.

## Submodule Pin and Configuration

The pinned revision is configured in `configs/libraries/mylib.toml`:

```toml
[library]
id = "mylib"
path = "external/MyLib"
expected_commit = "8348a04d46d76d3eeb35839d2a9892cdd5c60cf8"
read_only = true
allow_network = false
require_clean_tracked_files = true

[sources]
markdown_root = "Libs"
bibliography = "ref.bib"

[graph]
mode = "inspect_only"
path = "Libs/graphify-out/graph.json"

[selection]
require_explicit_acceptance = true
allow_missing_pdf = true
```

The Git index entry for `external/MyLib` must have mode `160000` pointing to the exact pinned commit.

## Submodule Initialization for Operators

For a fresh checkout of VibeReview:

```bash
git submodule sync -- external/MyLib
git submodule update --init --checkout -- external/MyLib
```

Credentials for private repositories belong in the operator's credential helper or CI secret store, never in repository configuration files or task bundles.

## Offline Inspection Command

The inspection CLI requires an operator-held review configuration (see
`vibereview.library.project_config`) that lives **outside** the public
repository and points at the pinned library, plus the public repository root
used to enforce the output-location policy. The committed
`configs/libraries/mylib.toml` is the pin record of the expected commit; it
is not itself a loadable review configuration.

```bash
python -m vibereview.library.inspect \
    --config <operator-review-config.toml> \
    --public-repository-root <path-to-vibereview-repo> \
    --output <output-dir-outside-repository>
```

The optional `--aliases <path>` argument supplies an operator adjudication
file (`vibereview-source-aliases-1`; held outside the repository, library, and
superproject) that feeds the manual-adjudication tier of source mapping.

### Generated Audit Reports

The inspection command generates the following files in the specified output directory:

| Report File | Description |
|---|---|
| `inventory.json` | Complete machine-readable inventory of all tracked blobs with Git object IDs, SHA-256 digests, sizes, and document classifications. |
| `inventory.md` | Human-readable markdown summary table of the inventory categorized by document kind. |
| `graph_schema.json` | Structural analysis of the exported graph (`graph.json`), recording root type, container keys, node/edge fields, relations, extraction classes, duplicate node ID checks, and dangling link checks. |
| `source_mapping.json` | Crosswalk between candidate paper Markdown, BibTeX bibliography keys, and graph nodes, categorizing mappings as observed, missing, or ambiguous, and recording the match tier (alias, exact, normalized, or none) that resolved each source. |
| `metadata_conflicts.json` | Detected conflicts including duplicate BibTeX keys, competing DOIs, conflicting titles, and alias/adjudication conflicts. |
| `excluded_entries.json` | Tracked entries excluded from paper candidacy (tooling scripts, prompts, caches, generated reports, admin configs) with specific rationale. |
| `upstream_integrity.json` | Verification of submodule commit match, gitlink mode (`160000`), working tree cleanliness, and blob integrity. |
| `source_aliases_applied.json` | The operator alias document applied during this inspection (only when `--aliases` was supplied), so reports remain self-describing. |
| `summary.json` | Machine-readable run summary: library id, pinned commit, integrity status, blob/paper/bibliography/graph counts, and conflict counts. |

All reports strictly distinguish `observed`, `missing`, `ambiguous`, and `not_checked` states without fabricating metadata or approval.

## Upstream Update Protocol

MyLib is updated exclusively in its own upstream repository. To update the pinned submodule in VibeReview:

1. Create a dedicated update branch in VibeReview:
   ```bash
   git switch -c update/mylib-pin
   ```
2. Update the submodule gitlink to the reviewed commit:
   ```bash
   git -C external/MyLib checkout --detach <new_commit_sha>
   git add external/MyLib
   ```
3. Update `expected_commit` in `configs/libraries/mylib.toml`.
4. Run the offline inspection command and generate an old/new inventory diff.
5. Revalidate source crosswalks and explicit acceptance manifests.
6. Commit and review the update. Existing frozen review corpus locks remain immutable and reproducible.

## CI and Access Policy

Ordinary CI runs tests against synthetic fixtures in `tests/library/fixtures/` and does not fetch or clone `external/MyLib`.
A separate, optional integration check in private or self-hosted CI environments can verify the live submodule using the read-only inspection command.
