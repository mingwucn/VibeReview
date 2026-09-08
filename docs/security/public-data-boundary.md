# Public data boundary

This repository contains source code, contracts, documentation, and synthetic
test fixtures. It must not contain a live literature corpus, an external
library checkout or Git submodule, operator-specific library configuration,
review state, generated scientific prose, provider credentials, or session
state.

An earlier integration branch included a historical demonstration review whose
redistribution authority and scientific acceptance were not established. The
advertised branch was rebuilt from the clean runtime lineage. The material is
not a publication, benchmark result, or accepted scientific review, and no
automated structural check should be interpreted as semantic or human
validation.

## Repository rules

- Live review projects stay outside Git. `reviews/` retains only `.gitkeep`.
- External corpora and checkouts stay outside Git. Tests construct small,
  explicitly fictional repositories under `tmp_path`.
- Operator library configuration and credential leases stay outside Git.
- Historical fixture content is not mirrored here. A metadata-free removal
  notice may explain why a fixture is absent, but must not reproduce source
  identifiers, hashes, excerpts, claims, or generated prose.
- Runtime output, state, caches, and lock files are never tracked.
- A live external-corpus or engine smoke test needs both a dedicated pytest
  marker and an explicit opt-in. Ordinary CI must remain corpus-, credential-,
  and network-independent.

## History review

Before publishing or updating a public branch, inspect every reachable commit
from a full, non-shallow clone, not only the candidate tree. The committed CI
tripwire exhaustively enumerates each reachable tree and rejects reserved paths,
Git links, symlinks, and a small set of misleading acceptance phrases. It is a
containment guard, not proof that content is safe.

The release gate is a separate private scan. Compare both Git blob object IDs
and raw-content SHA-256 values with an administrator-held denylist outside this
repository. Also scan every reachable text blob for source excerpts, personal
paths, credentials, and project-specific identifiers.

The administrator ref scan is explicit and offline. First mirror or otherwise
materialize the exact provider-advertised refs in a trusted full clone, review
that ref list, and then pass every fully qualified local name separately:

```bash
python -m vibereview.library.public_guard . \
  --administrator-ref refs/heads/main \
  --administrator-ref refs/heads/reviewed-side \
  --private-denylist /trusted/operator/private-denylist.txt
```

This command never discovers or fetches refs. It rejects an empty, duplicate,
missing, shorthand, revision-expression, or otherwise unsafe ref list before
scanning. Annotated refs are frozen to their peeled commit IDs. Distinct names
at the same commit remain distinct coverage entries, and findings are qualified
with each ref that reaches them. The caller is responsible for proving that the
supplied names are the complete provider-advertised set; ordinary CI continues
to scan only `HEAD` and performs no network access.

Rewriting or deleting a branch removes an advertised Git reference; it cannot
recall existing clones and may not immediately remove hosting caches or action
artifacts. When material has already been exposed, retire associated workflow
runs and coordinate any cache purge through the hosting provider as needed.
