# Historical research-data removal

An earlier private integration branch combined generic library code with a
prepublication research corpus and generated review artifacts. The public
line was reconstructed from a clean base. Research text, derived outputs,
repository-specific configuration, and corpus fingerprints are not present in
this history.

Only the generic, read-only Git-object boundary was reimplemented. External
library locations, exact commits, selections, and audit output are supplied by
an operator outside this repository. The automated public-boundary test checks
every path reachable from the candidate branch. A separate operator-private
denylist can additionally scan known object IDs, blob hashes, and text markers
without publishing those fingerprints here.

Imported selections, corpus locks, and content-addressed Markdown copies share
one generation transaction. Files are fsynced, auxiliary directories are
fsynced from leaves upward, and completed generation directories are sealed
read-only before `CURRENT` is changed. Loading still rehashes every object and
remains the authoritative tamper detector; filesystem permissions are only an
additional accidental-mutation barrier.
