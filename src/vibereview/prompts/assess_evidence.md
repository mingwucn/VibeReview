# Assess Evidence

Assess exactly the candidate refs listed in `input/input.json`. Read the
candidate ledger only through its `RES0001` resource entry and read the
CandidateClaim and RetrievalQuery only through the dependency entries in
`bundle_manifest.json`.

Return one decision for every listed candidate ref and no others. Use only
`assessed`, `duplicate`, or `redundant`. An `assessed` decision must carry one
evidence proposal and no canonical span ref. A `duplicate` decision must name
an existing canonical span from the input `canonical_span_refs` allowlist and
carry no evidence. A `redundant` decision may use only that same allowlist and
must provide a canonical span ref or a reason; it carries no evidence.

Return only JSON conforming to the registered proposal schema. Do not allocate
canonical span or evidence IDs.
