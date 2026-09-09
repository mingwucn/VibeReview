# Audit Rendered Sentence

Audit exactly the final rendered sentence draft named by `draft_local_ref`,
including its citation markers. Read its exact generation-owned accepted bytes
only through resource `RES0001` and use only the manifested source proposition
and PASS-audit dependencies. Set `sentence_ref` to the draft local reference,
never to a canonical `RS` identifier.

Return only JSON conforming to the registered proposal schema. Negative and
uncertain verdicts are canonical scientific outcomes, not engine failures.
