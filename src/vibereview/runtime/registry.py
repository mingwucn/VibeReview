"""Canonical scientific-ID allocation performed only inside writer transactions."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import Field, model_validator

from vibereview.enums import RetrievalIntent

from .records import RuntimeModel


class IdKind(StrEnum):
    THEME = "theme"
    PAPER = "paper"
    CLAIM = "claim"
    SPAN = "span"
    EVIDENCE = "evidence"
    CORPUS_FACT = "corpus_fact"
    PROCESS_FACT = "process_fact"
    PROPOSITION = "proposition"
    SEMANTIC_AUDIT = "semantic_audit"
    RENDERED_SENTENCE = "rendered_sentence"
    RENDERED_SENTENCE_AUDIT = "rendered_sentence_audit"


_PREFIXES = {
    IdKind.THEME: "T",
    IdKind.PAPER: "P",
    IdKind.CLAIM: "C",
    IdKind.SPAN: "R",
    IdKind.EVIDENCE: "E",
    IdKind.CORPUS_FACT: "CF",
    IdKind.PROCESS_FACT: "PF",
    IdKind.PROPOSITION: "PR",
    IdKind.SEMANTIC_AUDIT: "SA",
    IdKind.RENDERED_SENTENCE: "RS",
    IdKind.RENDERED_SENTENCE_AUDIT: "RSA",
}

_FIXED_FOUR_DIGIT = {IdKind.THEME, IdKind.PAPER, IdKind.CLAIM}


class CanonicalIdRegistry(RuntimeModel):
    counters: dict[IdKind, int] = Field(default_factory=dict)
    query_ordinals: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _valid_counter_ranges(self) -> "CanonicalIdRegistry":
        if any(value < 0 for value in self.counters.values()):
            raise ValueError("canonical ID counters must be nonnegative")
        if any(
            kind in _FIXED_FOUR_DIGIT and value > 9999
            for kind, value in self.counters.items()
        ):
            raise ValueError("fixed-width canonical ID counter exceeds its namespace")
        for key, value in self.query_ordinals.items():
            if not re.fullmatch(r"C[0-9]{4}:(?:SUP|CON|BND|ALT|MTH|NUL)", key):
                raise ValueError("query ordinal registry key is malformed")
            if not 0 <= value <= 99:
                raise ValueError("query ordinal registry value is outside 00..99")
        return self

    def validate_covers_identifiers(self, identifiers: list[str]) -> None:
        """Require counters at or above every identifier already in canonical state."""

        required = type(self).from_identifiers(identifiers)
        for kind, minimum in required.counters.items():
            if self.counters.get(kind, 0) < minimum:
                raise ValueError(
                    f"canonical ID registry does not cover {kind.value} identifier history"
                )
        for key, minimum in required.query_ordinals.items():
            if self.query_ordinals.get(key, 0) < minimum:
                raise ValueError(
                    "canonical ID registry does not cover retrieval-query ordinal history"
                )

    def allocate(self, kind: IdKind, count: int = 1) -> tuple[list[str], "CanonicalIdRegistry"]:
        if count < 1:
            raise ValueError("allocation count must be positive")
        current = self.counters.get(kind, 0)
        end = current + count
        if kind in _FIXED_FOUR_DIGIT and end > 9999:
            raise OverflowError(f"{kind.value} ID space is exhausted")
        prefix = _PREFIXES[kind]
        identifiers = [f"{prefix}{number:04d}" for number in range(current + 1, end + 1)]
        counters = dict(self.counters)
        counters[kind] = end
        return identifiers, self.model_copy(update={"counters": counters})

    def allocate_query(
        self, claim_id: str, intent: RetrievalIntent
    ) -> tuple[str, "CanonicalIdRegistry"]:
        codes = {
            RetrievalIntent.SUPPORT: "SUP",
            RetrievalIntent.CONTRADICTION: "CON",
            RetrievalIntent.BOUNDARY: "BND",
            RetrievalIntent.ALTERNATIVE: "ALT",
            RetrievalIntent.METHOD_CHALLENGE: "MTH",
            RetrievalIntent.NULL_RESULT: "NUL",
        }
        key = f"{claim_id}:{codes[intent]}"
        ordinal = self.query_ordinals.get(key, 0) + 1
        if ordinal > 99:
            raise OverflowError(f"query ordinal space is exhausted for {key}")
        values = dict(self.query_ordinals)
        values[key] = ordinal
        return (
            f"Q-{claim_id}-{codes[intent]}-{ordinal:02d}",
            self.model_copy(update={"query_ordinals": values}),
        )

    @staticmethod
    def claim_paper_evidence_id(claim_id: str, paper_id: str) -> str:
        if not re.fullmatch(r"C[0-9]{4}", claim_id):
            raise ValueError("invalid claim ID for CPE allocation")
        if not re.fullmatch(r"P[0-9]{4}", paper_id):
            raise ValueError("invalid paper ID for CPE allocation")
        return f"CPE-{claim_id}-{paper_id}"

    @classmethod
    def from_identifiers(cls, identifiers: list[str]) -> "CanonicalIdRegistry":
        counters: dict[IdKind, int] = {}
        query_ordinals: dict[str, int] = {}
        ordered = sorted(_PREFIXES.items(), key=lambda item: len(item[1]), reverse=True)
        for identifier in identifiers:
            query = re.fullmatch(
                r"Q-(C[0-9]{4})-(SUP|CON|BND|ALT|MTH|NUL)-([0-9]{2})",
                identifier,
            )
            if query:
                key = f"{query.group(1)}:{query.group(2)}"
                query_ordinals[key] = max(
                    query_ordinals.get(key, 0), int(query.group(3))
                )
                continue
            for kind, prefix in ordered:
                match = re.fullmatch(rf"{prefix}([0-9]+)", identifier)
                if match:
                    counters[kind] = max(counters.get(kind, 0), int(match.group(1)))
                    break
        return cls(counters=counters, query_ordinals=query_ordinals)
