# Generate Propositions

Draft propositions only from the explicitly selected ClaimPacket, CorpusFact,
and ReviewProcessFact dependencies in the task bundle. Every scientific claim
reference and citation binding must be licensed by those dependencies; every
corpus-fact or process-fact reference must come from its corresponding input
allowlist. Do not introduce an unmanifested source.

Return a nonempty bounded JSON object conforming to the registered proposal
schema. Use non-canonical local references for every draft. Python accepts the
exact draft artifact first; it allocates no PropositionRecord ID at this stage.
