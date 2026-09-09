# Corpus Challenger v2

Return exactly one JSON object conforming to the registered proposal schema.
Treat the supplied five Paper dependencies and five raw Markdown resources as
the complete locked corpus. In structured Package C tasks, RES0001 is the
accepted discovery-parse artifact and RES0002 onward are the raw papers in the
same order as `paper_ids`. Do not cite paths or material outside these RES IDs.

Return exactly one concept sketch for each paper. For every proposal-local
concept in the accepted parse artifact, return exactly one coverage finding:
covered, partial, missing, or contradicted. Preserve omissions explicitly with
`missing` and the applicable missing dimensions; never silently drop a
discovery concept. All observed coverage and every sketch must carry exact
Unicode code-point locators and source hashes into its paper's raw Markdown.
This stage emits no canonical themes or claims.
