# Parse Deep Research v2

Return exactly one JSON object conforming to the registered proposal schema.
Read only the Markdown resources listed in the bundle manifest. Never invent
canonical IDs or filesystem paths; use globally unique proposal-local refs.

For the structured Package C path, retain every output category separately:
themes, candidate claims, terminology, paper candidates, controversies, and
gaps. Each category must be nonempty. Give every theme and candidate claim one
source binding, and give every terminology, paper-candidate, controversy, and
gap item its own exact source locators. A locator names a supplied RES ID and
its manifest content hash, uses Unicode code-point offsets into that resource,
and hashes the exact selected text. Parsed themes and claims use
`deep_research` origin; each claim's `origin_refs` are exactly the RES IDs in
its source binding. Do not emit corpus-challenger sketches or coverage results.
