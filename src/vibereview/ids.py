"""Identifier, DOI, and hash primitives for the Phase-0 contract."""

from __future__ import annotations

import hashlib
import re
from typing import Annotated

from pydantic import Field


THEME_ID_PATTERN = r"^T[0-9]{4}$"
PAPER_ID_PATTERN = r"^P[0-9]{4}$"
CLAIM_ID_PATTERN = r"^C[0-9]{4}$"
QUERY_ID_PATTERN = (
    r"^Q-C[0-9]{4}-(SUP|CON|BND|ALT|MTH|NUL)-(0[1-9]|[1-9][0-9])$"
)
SPAN_ID_PATTERN = r"^R[0-9]{4,}$"
EVIDENCE_ID_PATTERN = r"^E[0-9]{4,}$"
CPE_ID_PATTERN = r"^CPE-C[0-9]{4}-P[0-9]{4}$"
CORPUS_FACT_ID_PATTERN = r"^CF[0-9]{4,}$"
PROCESS_FACT_ID_PATTERN = r"^PF[0-9]{4,}$"
PROPOSITION_ID_PATTERN = r"^PR[0-9]{4,}$"
SEMANTIC_AUDIT_ID_PATTERN = r"^SA[0-9]{4,}$"
SENTENCE_ID_PATTERN = r"^RS[0-9]{4,}$"
SENTENCE_AUDIT_ID_PATTERN = r"^RSA[0-9]{4,}$"
SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"

ThemeId = Annotated[str, Field(pattern=THEME_ID_PATTERN)]
PaperId = Annotated[str, Field(pattern=PAPER_ID_PATTERN)]
ClaimId = Annotated[str, Field(pattern=CLAIM_ID_PATTERN)]
QueryId = Annotated[str, Field(pattern=QUERY_ID_PATTERN)]
SpanId = Annotated[str, Field(pattern=SPAN_ID_PATTERN)]
EvidenceId = Annotated[str, Field(pattern=EVIDENCE_ID_PATTERN)]
ClaimPaperEvidenceId = Annotated[str, Field(pattern=CPE_ID_PATTERN)]
CorpusFactId = Annotated[str, Field(pattern=CORPUS_FACT_ID_PATTERN)]
ProcessFactId = Annotated[str, Field(pattern=PROCESS_FACT_ID_PATTERN)]
PropositionId = Annotated[str, Field(pattern=PROPOSITION_ID_PATTERN)]
SemanticAuditId = Annotated[str, Field(pattern=SEMANTIC_AUDIT_ID_PATTERN)]
SentenceId = Annotated[str, Field(pattern=SENTENCE_ID_PATTERN)]
SentenceAuditId = Annotated[str, Field(pattern=SENTENCE_AUDIT_ID_PATTERN)]
Sha256 = Annotated[str, Field(pattern=SHA256_PATTERN)]


QUERY_INTENT_CODES = {
    "SUP": "support",
    "CON": "contradiction",
    "BND": "boundary",
    "ALT": "alternative",
    "MTH": "method_challenge",
    "NUL": "null_result",
}

_DOI_URL_RE = re.compile(r"^https?://(?:dx\.)?doi\.org/", re.IGNORECASE)
_DOI_PREFIX_RE = re.compile(r"^doi:\s*", re.IGNORECASE)
_RAW_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)
_SHA256_RE = re.compile(SHA256_PATTERN)
_QUERY_RE = re.compile(QUERY_ID_PATTERN)


def candidate_claim_hash(candidate_claim: str) -> str:
    """Hash the exact CandidateClaim text without normalization."""

    digest = hashlib.sha256(candidate_claim.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def normalize_doi(value: str) -> str:
    """Return a DOI using the frozen ``doi:<lowercase>`` representation."""

    normalized = value.strip()
    normalized = _DOI_URL_RE.sub("", normalized)
    normalized = _DOI_PREFIX_RE.sub("", normalized)
    normalized = normalized.strip().lower()
    if not normalized:
        raise ValueError("DOI must not be empty")
    return f"doi:{normalized}"


def normalize_identity_key(value: str) -> str:
    """Normalize known identity-key families while preserving unknown aliases."""

    key = value.strip()
    if not key:
        raise ValueError("identity key must not be empty")
    without_url = _DOI_URL_RE.sub("", key)
    without_prefix = _DOI_PREFIX_RE.sub("", key)
    if without_url != key or without_prefix != key or _RAW_DOI_RE.fullmatch(key):
        return normalize_doi(key)
    if key.lower().startswith("sha256:"):
        if not _SHA256_RE.fullmatch(key):
            raise ValueError("SHA-256 identity key must use canonical lowercase form")
        return key
    if key.lower().startswith("bib:"):
        payload = " ".join(key[4:].strip().split()).casefold()
        if not payload:
            raise ValueError("bibliographic identity key must not be empty")
        return f"bib:{payload}"
    return key


def parse_query_id(query_id: str) -> tuple[str, str, int]:
    """Return encoded claim ID, intent value, and ordinal from a valid query ID."""

    match = _QUERY_RE.fullmatch(query_id)
    if match is None:
        raise ValueError("invalid retrieval query ID")
    claim_id = query_id.split("-", 3)[1]
    code = match.group(1)
    ordinal = int(match.group(2))
    return claim_id, QUERY_INTENT_CODES[code], ordinal

