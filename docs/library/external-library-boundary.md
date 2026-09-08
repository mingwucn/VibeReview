# External library boundary

VibeReview can inspect and import an operator-supplied Markdown library without
embedding its location or identity in this repository. The operator keeps the
TOML configuration, selection manifest, source checkout, review state, and
inspection output outside both the public repository and the configured Git
superproject.

The configuration must name a full commit object and a superproject gitlink.
Both the committed superproject tree entry and its stage-zero index entry must
be mode `160000` and must equal that commit. Library `HEAD` must equal the same
commit. Reads are made with replacement objects and inherited Git object/config
overrides disabled, and papers, bibliography, and graph data are read only from
blobs reachable from the configured commit.

Clean-worktree enforcement defaults to true and includes tracked, untracked,
ignored, assume-unchanged, and skip-worktree state. An operator may explicitly
set `require_clean_worktree = false` when dirty local files must coexist with
pinned-object inspection. This does not relax `HEAD` or gitlink verification,
and dirty worktree bytes are never used as library input.

Every candidate Markdown paper in the pinned inventory requires exactly one
include or exclude decision. Each decision records its source hash, role,
reviewer label, review time, and reason. `max_selected_documents` and
`max_selected_bytes` bound import preparation; the defaults are 1,000 documents
and 1 GiB. A narrower operator policy, such as a five-paper pilot budget, can
set lower values without changing the scientific contracts.

Import copies selected Markdown into generation-owned SHA-256 paths and commits
the canonical Papers, selection, source-object inventory, corpus lock, and raw
objects through one `CURRENT` transaction. Existing publication identities are
reused. Missing metadata may be enriched, but conflicting canonical metadata
fails closed. Retrieval revalidates the generation anchor and raw hashes and
returns runtime candidates only; it does not allocate canonical evidence IDs or
fabricate an assessed disposition.

The import API requires the public-repository root on every call. It rejects a
review-state root that overlaps that public root, the external library, or the
library superproject; this containment check cannot be disabled by omission.

The optional external-corpus smoke test is marked `external_corpus` and is
excluded from ordinary CI. It runs only with explicit operator opt-in and
environment-supplied paths, performs inspection only, and compares source and
superproject snapshots before and after. It performs no import, output write,
network access, or scientific acceptance.
