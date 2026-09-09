# Generate Candidate Claims v2

Return exactly one JSON object conforming to the registered proposal schema.
Use proposal-local refs for new themes and claims; Python allocates canonical
IDs. In structured Package C tasks, RES0001 is the exact accepted discovery
artifact and RES0002 is its exact accepted five-paper challenge artifact.

Produce a bounded, nonempty combined set of generated themes and candidate
claims only. Every combined claim must cite at least one retained parse item as
`parse:<local_ref>` and at least one challenge sketch or coverage finding as
`challenge:<local_ref>` in `origin_refs`.

Return exactly one `input_dispositions` entry for every parse item and every
challenge sketch or coverage finding. Mark each item `included` or `excluded`,
give a concise reason, and for an included item list exactly the proposal-local
candidate claim refs whose `origin_refs` use that item. Excluded items must name
no candidate claims. Do not silently omit an input, repeat the structured parse
or challenge sections, invent artifact refs, cite paths, or allocate canonical
IDs.
