"""Deterministic retrieval candidates; canonical spans are deliberately out of scope."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import re
from types import MappingProxyType
from typing import Literal

from pydantic import Field, model_validator

from vibereview.enums import RetrievalIntent
from vibereview.ids import PaperId, QueryId, Sha256, parse_query_id
from vibereview.runtime.hashing import hash_bytes, hash_json
from vibereview.runtime.records import RuntimeModel
from vibereview.runtime.repository import GenerationStore

from .git_source import compute_content_sha256
from .graph import ReadOnlyGraphAdapter
from .models import CorpusIntegrityError, CorpusLockManifest
from .selection import (
    load_corpus_lock,
    load_corpus_source_object,
    verify_corpus_lock,
)


MAX_TOP_K = 100
MAX_QUERY_TERMS = 64
MAX_CANDIDATE_CHARS = 16_384
MAX_CONTEXT_CHARS = 16_384
MAX_INVALID_DIAGNOSTICS = 100
MAX_QUERY_CHARS = 4_096
MAX_QUERY_UTF8_BYTES = 16_384
MAX_LEDGER_HITS = 2 * MAX_TOP_K + 2 * MAX_INVALID_DIAGNOSTICS
MAX_LEDGER_METADATA_CHARS = 4_096
MAX_TRUNCATED_CANDIDATES = 2**63 - 1
MAX_EXTENSION_BACKEND_RESULTS = MAX_TOP_K + MAX_INVALID_DIAGNOSTICS


class CandidateHitOrigin(StrEnum):
    GRAPH = "graph"
    TEXT_BASELINE = "text_baseline"
    BOTH = "both"


class RawCandidateHit(RuntimeModel):
    """Runtime retrieval proposal with no canonical R identifier or disposition."""

    candidate_key: Sha256
    paper_id: PaperId
    raw_md_path: str = Field(min_length=1, max_length=MAX_LEDGER_METADATA_CHARS)
    query_id: QueryId
    query_text_hash: Sha256
    intent: RetrievalIntent
    origin: CandidateHitOrigin
    source_text: str = Field(min_length=1, max_length=MAX_CANDIDATE_CHARS)
    offset_unit: Literal["unicode_code_points"] = "unicode_code_points"
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, gt=0)
    source_span_hash: Sha256 | None = None
    context_text: str | None = Field(
        default=None, min_length=1, max_length=MAX_CONTEXT_CHARS
    )
    context_start_offset: int | None = Field(default=None, ge=0)
    context_end_offset: int | None = Field(default=None, gt=0)
    context_utf8_hash: Sha256 | None = None
    section: str | None = Field(default=None, max_length=MAX_LEDGER_METADATA_CHARS)
    upstream_node_id: str | None = Field(
        default=None, max_length=MAX_LEDGER_METADATA_CHARS
    )
    upstream_relation: str | None = Field(
        default=None, max_length=MAX_LEDGER_METADATA_CHARS
    )
    score: float = Field(ge=0.0, le=1.0)
    is_valid: bool
    validation_error: str | None = Field(
        default=None, max_length=MAX_LEDGER_METADATA_CHARS
    )

    @model_validator(mode="after")
    def _validate_runtime_candidate(self) -> "RawCandidateHit":
        claim_id, encoded_intent, _ = parse_query_id(self.query_id)
        del claim_id
        if encoded_intent != self.intent.value:
            raise ValueError("query ID intent and candidate intent disagree")
        located = self.start_offset is not None or self.end_offset is not None
        if located and (self.start_offset is None or self.end_offset is None):
            raise ValueError("candidate offsets must both be present or absent")
        if self.start_offset is not None and self.end_offset is not None:
            if self.end_offset <= self.start_offset:
                raise ValueError("candidate end_offset must be greater than start_offset")
        context_values = (
            self.context_text,
            self.context_start_offset,
            self.context_end_offset,
            self.context_utf8_hash,
        )
        if any(item is not None for item in context_values) and any(
            item is None for item in context_values
        ):
            raise ValueError("candidate context fields must be present together")
        if self.is_valid:
            if self.start_offset is None or self.source_span_hash is None:
                raise ValueError("valid candidates require offsets and a source-span hash")
            if any(item is None for item in context_values):
                raise ValueError("valid candidates require bounded source context")
            if self.validation_error is not None:
                raise ValueError("valid candidates cannot carry a validation error")
            assert self.end_offset is not None
            assert self.context_text is not None
            assert self.context_start_offset is not None
            assert self.context_end_offset is not None
            assert self.context_utf8_hash is not None
            if self.end_offset - self.start_offset != len(self.source_text):
                raise ValueError(
                    "valid candidate offsets must span exactly the source text"
                )
            if self.source_span_hash != hash_bytes(self.source_text.encode("utf-8")):
                raise ValueError(
                    "valid candidate source-span hash must match the source text"
                )
            if not (
                self.context_start_offset
                <= self.start_offset
                < self.end_offset
                <= self.context_end_offset
            ):
                raise ValueError("candidate context must contain the source span")
            if (
                self.context_end_offset - self.context_start_offset
                != len(self.context_text)
            ):
                raise ValueError(
                    "candidate context offsets must span exactly the context text"
                )
            relative_start = self.start_offset - self.context_start_offset
            relative_end = self.end_offset - self.context_start_offset
            if self.context_text[relative_start:relative_end] != self.source_text:
                raise ValueError("candidate context does not contain its source text")
            if self.context_utf8_hash != hash_bytes(
                self.context_text.encode("utf-8")
            ):
                raise ValueError("candidate context UTF-8 hash does not match its text")
        elif not self.validation_error:
            raise ValueError("invalid candidates require a validation error")
        elif any(item is not None for item in context_values):
            raise ValueError("invalid candidates cannot carry source context")
        if self.origin is CandidateHitOrigin.TEXT_BASELINE:
            if self.upstream_node_id is not None or self.upstream_relation is not None:
                raise ValueError("text candidates cannot carry graph provenance")
        elif self.upstream_node_id is None or self.upstream_relation is None:
            raise ValueError("graph-derived candidates require graph provenance")
        return self


def _valid_hit_order_key(hit: RawCandidateHit) -> tuple[float, str, int, str]:
    if hit.start_offset is None:  # pragma: no cover - guarded by the model
        raise ValueError("valid candidate lacks a start offset")
    return (-hit.score, hit.paper_id, hit.start_offset, hit.candidate_key)


def _invalid_hit_order_key(
    hit: RawCandidateHit,
) -> tuple[float, str, str, str, str, str]:
    return (
        -hit.score,
        hit.paper_id,
        hit.candidate_key,
        hit.upstream_node_id or "",
        hit.upstream_relation or "",
        hit.validation_error or "",
    )


class RetrievalLedger(RuntimeModel):
    source_generation: int = Field(ge=0)
    query_id: QueryId
    query_text_hash: Sha256
    corpus_lock_hash: Sha256
    requested_top_k: int = Field(ge=1, le=MAX_TOP_K)
    max_query_terms: int = Field(ge=1, le=MAX_QUERY_TERMS)
    total_candidates: int = Field(ge=0)
    valid_candidates: int = Field(ge=0)
    invalid_candidates: int = Field(ge=0)
    text_truncated_valid_candidates: int = Field(
        default=0, ge=0, le=MAX_TRUNCATED_CANDIDATES
    )
    text_truncated_invalid_candidates: int = Field(
        default=0, ge=0, le=MAX_TRUNCATED_CANDIDATES
    )
    graph_truncated_valid_candidates: int = Field(
        default=0, ge=0, le=MAX_TRUNCATED_CANDIDATES
    )
    graph_truncated_invalid_candidates: int = Field(
        default=0, ge=0, le=MAX_TRUNCATED_CANDIDATES
    )
    selected_candidate_keys: list[Sha256]
    excluded_by_budget_candidate_keys: list[Sha256]
    raw_hits: list[RawCandidateHit]

    @model_validator(mode="after")
    def _counts_and_query_agree(self) -> "RetrievalLedger":
        valid = sum(hit.is_valid for hit in self.raw_hits)
        invalid = len(self.raw_hits) - valid
        if len(self.raw_hits) > MAX_LEDGER_HITS:
            raise ValueError("retrieval ledger exceeds its bounded hit limit")
        if valid > 2 * MAX_TOP_K or invalid > 2 * MAX_INVALID_DIAGNOSTICS:
            raise ValueError("retrieval ledger exceeds its per-class hit limits")
        if self.total_candidates != len(self.raw_hits):
            raise ValueError("retrieval ledger total does not equal raw_hits")
        if self.valid_candidates != valid or self.invalid_candidates != invalid:
            raise ValueError("retrieval ledger validity counts do not match raw_hits")
        if any(hit.query_id != self.query_id for hit in self.raw_hits):
            raise ValueError("retrieval ledger contains a hit for another query")
        if any(hit.query_text_hash != self.query_text_hash for hit in self.raw_hits):
            raise ValueError("retrieval ledger contains a hit for another query text")
        keys = [hit.candidate_key for hit in self.raw_hits]
        if len(keys) != len(set(keys)):
            raise ValueError("retrieval candidate keys must be unique across all raw hits")
        selected = self.selected_candidate_keys
        excluded = self.excluded_by_budget_candidate_keys
        if len(selected) > self.requested_top_k:
            raise ValueError("retrieval ledger selects more than the promotion budget")
        if len(selected) != len(set(selected)) or len(excluded) != len(set(excluded)):
            raise ValueError("retrieval ledger candidate classifications must be unique")
        if set(selected) & set(excluded):
            raise ValueError("selected and over-budget candidates must be disjoint")
        valid_keys = {hit.candidate_key for hit in self.raw_hits if hit.is_valid}
        if set(selected) | set(excluded) != valid_keys:
            raise ValueError(
                "selected and over-budget candidates must partition valid raw hits"
            )
        ordered_valid = sorted(
            (hit for hit in self.raw_hits if hit.is_valid),
            key=_valid_hit_order_key,
        )
        expected_selected = [
            hit.candidate_key for hit in ordered_valid[: self.requested_top_k]
        ]
        expected_excluded = [
            hit.candidate_key for hit in ordered_valid[self.requested_top_k :]
        ]
        if selected != expected_selected or excluded != expected_excluded:
            raise ValueError(
                "retrieval ledger selection does not match deterministic ranking"
            )
        ordered_invalid = sorted(
            (hit for hit in self.raw_hits if not hit.is_valid),
            key=_invalid_hit_order_key,
        )
        if self.raw_hits != [*ordered_valid, *ordered_invalid]:
            raise ValueError("retrieval ledger hits are not in deterministic order")
        return self


@dataclass(frozen=True, slots=True, init=False, eq=False)
class VerifiedCorpus:
    """In-memory texts loaded only after lock, Paper, path, and raw hash checks."""

    review_root: Path
    generation: int
    lock_hash: Sha256
    _lock_bytes: bytes
    _texts: Mapping[str, tuple[str, str, str]]

    def __init__(self, review_root: Path, *, generation: int | None = None) -> None:
        root = review_root.resolve(strict=False)
        store = GenerationStore(root)
        try:
            if generation is None:
                resolved_generation, snapshot, _ = store.load_current()
            else:
                snapshot, _ = store.load_generation(generation)
                resolved_generation = generation
        except (OSError, ValueError) as exc:
            raise CorpusIntegrityError(
                "active generation or auxiliary hash mismatch"
            ) from exc
        lock, lock_hash = load_corpus_lock(root, generation=resolved_generation)
        verified = verify_corpus_lock(root, lock, snapshot)
        texts: dict[str, tuple[str, str, str]] = {}
        for paper in lock.papers:
            content = verified[paper.paper_id]
            try:
                text = content.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise CorpusIntegrityError(
                    f"raw Markdown is not UTF-8: {paper.paper_id}"
                ) from exc
            texts[paper.paper_id] = (
                paper.raw_md_path,
                paper.raw_md_hash,
                text,
            )
        object.__setattr__(self, "review_root", root)
        object.__setattr__(self, "generation", resolved_generation)
        object.__setattr__(self, "lock_hash", lock_hash)
        object.__setattr__(self, "_lock_bytes", lock.model_dump_json().encode("utf-8"))
        object.__setattr__(self, "_texts", MappingProxyType(texts))

    @property
    def lock(self) -> CorpusLockManifest:
        """Return a detached lock model; caller mutation cannot alter retrieval."""

        return CorpusLockManifest.model_validate_json(self._lock_bytes)

    @property
    def texts(self) -> Mapping[str, tuple[str, str, str]]:
        return self._texts


def _query_text_hash(query: str) -> Sha256:
    return hash_bytes(query.encode("utf-8"))


def _query_terms(query: str, *, max_terms: int = MAX_QUERY_TERMS) -> list[str]:
    if len(query) > MAX_QUERY_CHARS or len(query.encode("utf-8")) > MAX_QUERY_UTF8_BYTES:
        raise ValueError("retrieval query exceeds its deterministic text budget")
    if not 1 <= max_terms <= MAX_QUERY_TERMS:
        raise ValueError(f"max_terms must be between 1 and {MAX_QUERY_TERMS}")
    terms = [term.casefold() for term in re.findall(r"[^\W_]+", query, flags=re.UNICODE)]
    return list(dict.fromkeys(term for term in terms if len(term) > 2))[:max_terms]


def _candidate_key(
    *,
    paper_id: str,
    raw_md_hash: str,
    query_id: str,
    query_text_hash: str,
    source_text: str,
    start_offset: int | None,
    end_offset: int | None,
    origin: CandidateHitOrigin,
) -> str:
    return hash_json(
        {
            "paper_id": paper_id,
            "raw_md_hash": raw_md_hash,
            "query_id": query_id,
            "query_text_hash": query_text_hash,
            "source_text": source_text,
            "start_offset": start_offset,
            "end_offset": end_offset,
            "origin": origin.value,
        }
    )


def _heading_index(text: str) -> tuple[list[int], list[str]]:
    offsets: list[int] = []
    headings: list[str] = []
    for match in re.finditer(r"(?m)^#+[^\n]*", text):
        heading = match.group(0).lstrip("#").strip()
        if heading:
            offsets.append(match.start())
            headings.append(heading)
    return offsets, headings


def _section_at(index: tuple[list[int], list[str]], offset: int) -> str | None:
    offsets, headings = index
    position = bisect_right(offsets, offset) - 1
    return headings[position] if position >= 0 else None


def _paragraph_slices(text: str):
    start = 0
    for separator in re.finditer(r"\r?\n[ \t]*\r?\n", text):
        end = separator.start()
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start < end:
            yield start, end
        start = separator.end()
    end = len(text)
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start < end:
        yield start, end


def _bounded_context_slice(
    text: str,
    start_offset: int,
    end_offset: int,
) -> tuple[int, int, str]:
    """Return a deterministic code-point window containing the source span."""

    span_length = end_offset - start_offset
    if not 0 <= start_offset < end_offset <= len(text):
        raise ValueError("source offsets are outside the raw Markdown")
    if span_length > MAX_CONTEXT_CHARS:
        raise ValueError("source span exceeds the context size limit")
    remaining = MAX_CONTEXT_CHARS - span_length
    before = min(start_offset, remaining // 2)
    after = min(len(text) - end_offset, remaining - before)
    remaining -= before + after
    if remaining:
        extra_before = min(start_offset - before, remaining)
        before += extra_before
        remaining -= extra_before
    if remaining:
        after += min(len(text) - end_offset - after, remaining)
    context_start = start_offset - before
    context_end = end_offset + after
    return context_start, context_end, text[context_start:context_end]


def _retain_best(
    values: list[RawCandidateHit],
    value: RawCandidateHit,
    *,
    limit: int,
    key,
) -> None:
    values.append(value)
    values.sort(key=key)
    if len(values) > limit:
        values.pop()


@dataclass(frozen=True, slots=True)
class _BackendSearchBatch:
    """Bounded backend output plus every candidate omitted by fixed limits."""

    hits: tuple[RawCandidateHit, ...]
    truncated_valid_candidates: int = 0
    truncated_invalid_candidates: int = 0


@dataclass(frozen=True, slots=True, init=False, eq=False)
class DeterministicTextRetriever:
    _corpus: VerifiedCorpus

    def __init__(self, review_root: Path) -> None:
        object.__setattr__(self, "_corpus", VerifiedCorpus(review_root))

    @classmethod
    def _from_verified_corpus(
        cls, corpus: VerifiedCorpus
    ) -> "DeterministicTextRetriever":
        retriever = object.__new__(cls)
        object.__setattr__(retriever, "_corpus", corpus)
        return retriever

    @property
    def corpus(self) -> VerifiedCorpus:
        return self._corpus

    def search(
        self,
        query: str,
        query_id: QueryId,
        intent: RetrievalIntent,
        *,
        top_k: int = 10,
        max_query_terms: int = MAX_QUERY_TERMS,
    ) -> list[RawCandidateHit]:
        return list(
            self._search_with_accounting(
                query,
                query_id,
                intent,
                valid_limit=top_k,
                max_query_terms=max_query_terms,
            ).hits
        )

    def _search_with_accounting(
        self,
        query: str,
        query_id: QueryId,
        intent: RetrievalIntent,
        *,
        valid_limit: int,
        max_query_terms: int,
    ) -> _BackendSearchBatch:
        if not 1 <= valid_limit <= MAX_TOP_K:
            raise ValueError(f"valid_limit must be between 1 and {MAX_TOP_K}")
        if not 1 <= max_query_terms <= MAX_QUERY_TERMS:
            raise ValueError(
                f"max_query_terms must be between 1 and {MAX_QUERY_TERMS}"
            )
        _, encoded_intent, _ = parse_query_id(query_id)
        if encoded_intent != intent.value:
            raise ValueError("query ID intent does not match requested retrieval intent")
        terms = _query_terms(query, max_terms=max_query_terms)
        if not terms:
            return _BackendSearchBatch(())
        query_hash = _query_text_hash(query)
        valid_hits: list[RawCandidateHit] = []
        invalid_hits: list[RawCandidateHit] = []
        valid_candidates_seen = 0
        invalid_candidates_seen = 0
        for paper_id, (raw_path, raw_hash, text) in sorted(self.corpus.texts.items()):
            headings = _heading_index(text)
            for start, end in _paragraph_slices(text):
                clean = text[start:end]
                lowered = clean.casefold()
                matched = sum(term in lowered for term in terms)
                if matched == 0:
                    continue
                if len(clean) > MAX_CANDIDATE_CHARS:
                    invalid_candidates_seen += 1
                    bounded = clean[:MAX_CANDIDATE_CHARS]
                    _retain_best(
                        invalid_hits,
                        RawCandidateHit(
                            candidate_key=_candidate_key(
                                paper_id=paper_id,
                                raw_md_hash=raw_hash,
                                query_id=query_id,
                                query_text_hash=query_hash,
                                source_text=bounded,
                                start_offset=None,
                                end_offset=None,
                                origin=CandidateHitOrigin.TEXT_BASELINE,
                            ),
                            paper_id=paper_id,
                            raw_md_path=raw_path,
                            query_id=query_id,
                            query_text_hash=query_hash,
                            intent=intent,
                            origin=CandidateHitOrigin.TEXT_BASELINE,
                            source_text=bounded,
                            score=matched / len(terms),
                            is_valid=False,
                            validation_error=(
                                "matching paragraph exceeds the runtime candidate size limit"
                            ),
                        ),
                        limit=MAX_INVALID_DIAGNOSTICS,
                        key=lambda hit: (-hit.score, hit.paper_id, hit.candidate_key),
                    )
                    continue
                valid_candidates_seen += 1
                span_hash = hash_bytes(clean.encode("utf-8"))
                context_start, context_end, context_text = _bounded_context_slice(
                    text, start, end
                )
                score = matched / len(terms)
                _retain_best(
                    valid_hits,
                    RawCandidateHit(
                        candidate_key=_candidate_key(
                            paper_id=paper_id,
                            raw_md_hash=raw_hash,
                            query_id=query_id,
                            query_text_hash=query_hash,
                            source_text=clean,
                            start_offset=start,
                            end_offset=end,
                            origin=CandidateHitOrigin.TEXT_BASELINE,
                        ),
                        paper_id=paper_id,
                        raw_md_path=raw_path,
                        query_id=query_id,
                        query_text_hash=query_hash,
                        intent=intent,
                        origin=CandidateHitOrigin.TEXT_BASELINE,
                        source_text=clean,
                        start_offset=start,
                        end_offset=end,
                        source_span_hash=span_hash,
                        context_text=context_text,
                        context_start_offset=context_start,
                        context_end_offset=context_end,
                        context_utf8_hash=hash_bytes(context_text.encode("utf-8")),
                        section=_section_at(headings, start),
                        score=score,
                        is_valid=True,
                    ),
                    limit=valid_limit,
                    key=lambda hit: (
                        -hit.score,
                        hit.paper_id,
                        hit.start_offset or 0,
                        hit.candidate_key,
                    ),
                )
        return _BackendSearchBatch(
            hits=tuple([*valid_hits, *invalid_hits]),
            truncated_valid_candidates=valid_candidates_seen - len(valid_hits),
            truncated_invalid_candidates=(
                invalid_candidates_seen - len(invalid_hits)
            ),
        )


@dataclass(frozen=True, slots=True, init=False, eq=False)
class GraphAssistedRetriever:
    _corpus: VerifiedCorpus
    _graph_content: bytes
    _graph_path: str
    _library_id: str | None
    _source_commit: str | None
    _citekey_to_paper: Mapping[str, str]

    def __init__(self, adapter: ReadOnlyGraphAdapter, review_root: Path) -> None:
        self._initialize(adapter, VerifiedCorpus(review_root))

    @classmethod
    def _from_verified_corpus(
        cls, corpus: VerifiedCorpus
    ) -> "GraphAssistedRetriever | None":
        lock = corpus.lock
        graph_objects = [
            item for item in lock.import_source.objects if item.role == "graph"
        ]
        if not graph_objects:
            return None
        if len(graph_objects) != 1:
            raise CorpusIntegrityError(
                "corpus lock must identify exactly one graph source object"
            )
        graph_object = graph_objects[0]
        graph_content = load_corpus_source_object(
            corpus.review_root, lock, role="graph"
        )
        if graph_content is None:  # pragma: no cover - lock/object invariant
            raise CorpusIntegrityError("locked graph source object is absent")
        retriever = object.__new__(cls)
        retriever._initialize(
            ReadOnlyGraphAdapter(
                graph_content,
                graph_object.source_relative_path,
                library_id=lock.library_id,
                source_commit=lock.source_commit,
            ),
            corpus,
        )
        return retriever

    def _initialize(
        self, adapter: ReadOnlyGraphAdapter, corpus: VerifiedCorpus
    ) -> None:
        if type(adapter) is not ReadOnlyGraphAdapter:
            raise CorpusIntegrityError(
                "graph retrieval requires the exact concrete ReadOnlyGraphAdapter"
            )
        if not isinstance(adapter._content, bytes):
            raise CorpusIntegrityError("graph adapter has no immutable byte snapshot")
        graph_content = bytes(adapter._content)
        graph_path = adapter._source_relative_path
        library_id = adapter._library_id
        source_commit = adapter._source_commit
        observed_hash = compute_content_sha256(graph_content)
        lock = corpus.lock
        if (
            library_id != lock.library_id
            or source_commit != lock.source_commit
        ):
            raise CorpusIntegrityError(
                "graph adapter does not come from the corpus lock's library and commit"
            )
        graph_objects = [
            item
            for item in lock.import_source.objects
            if item.role == "graph"
        ]
        if len(graph_objects) != 1:
            raise CorpusIntegrityError(
                "corpus lock must identify exactly one graph source object"
            )
        graph_object = graph_objects[0]
        if (
            graph_path != graph_object.source_relative_path
            or observed_hash != graph_object.content_sha256
        ):
            raise CorpusIntegrityError(
                "graph adapter path or content does not match the locked source object"
            )
        object.__setattr__(self, "_corpus", corpus)
        object.__setattr__(self, "_graph_content", graph_content)
        object.__setattr__(self, "_graph_path", graph_path)
        object.__setattr__(self, "_library_id", library_id)
        object.__setattr__(self, "_source_commit", source_commit)
        object.__setattr__(
            self,
            "_citekey_to_paper",
            MappingProxyType(
                {
                    paper.bibliography_key.casefold(): paper.paper_id
                    for paper in lock.papers
                    if paper.bibliography_key
                }
            ),
        )

    @property
    def corpus(self) -> VerifiedCorpus:
        return self._corpus

    def search(
        self,
        query: str,
        query_id: QueryId,
        intent: RetrievalIntent,
        *,
        top_k: int = 10,
        max_query_terms: int = MAX_QUERY_TERMS,
    ) -> list[RawCandidateHit]:
        return list(
            self._search_with_accounting(
                query,
                query_id,
                intent,
                valid_limit=top_k,
                max_query_terms=max_query_terms,
            ).hits
        )

    def _search_with_accounting(
        self,
        query: str,
        query_id: QueryId,
        intent: RetrievalIntent,
        *,
        valid_limit: int,
        max_query_terms: int,
    ) -> _BackendSearchBatch:
        if not 1 <= valid_limit <= MAX_TOP_K:
            raise ValueError(f"valid_limit must be between 1 and {MAX_TOP_K}")
        if not 1 <= max_query_terms <= MAX_QUERY_TERMS:
            raise ValueError(
                f"max_query_terms must be between 1 and {MAX_QUERY_TERMS}"
            )
        _, encoded_intent, _ = parse_query_id(query_id)
        if encoded_intent != intent.value:
            raise ValueError("query ID intent does not match requested retrieval intent")
        terms = _query_terms(query, max_terms=max_query_terms)
        if not terms:
            return _BackendSearchBatch(())
        query_hash = _query_text_hash(query)
        valid_results: list[RawCandidateHit] = []
        invalid_results: list[RawCandidateHit] = []
        valid_candidates_seen = 0
        invalid_candidates_seen = 0
        heading_indexes: dict[str, tuple[list[int], list[str]]] = {}
        # Reparse a fresh private adapter from the constructor-time verified
        # byte snapshot.  Neither a caller-mutated adapter instance nor an
        # overridden candidate method participates in retrieval.
        private_adapter = ReadOnlyGraphAdapter(
            self._graph_content,
            self._graph_path,
            library_id=self._library_id,
            source_commit=self._source_commit,
        )
        for node in ReadOnlyGraphAdapter.iter_candidates(
            private_adapter, " ".join(terms)
        ):
            citekey = str(node.get("citekey", node.get("id", ""))).casefold()
            paper_id = self._citekey_to_paper.get(citekey)
            if paper_id is None:
                continue
            raw_path, raw_hash, text = self.corpus.texts[paper_id]
            attributes = node.get("attributes")
            if not isinstance(attributes, dict):
                continue
            for relation, raw_items in sorted(attributes.items()):
                items = raw_items if isinstance(raw_items, list) else [raw_items]
                for item in items:
                    if not isinstance(item, dict) or not isinstance(item.get("evidence"), str):
                        continue
                    evidence = item["evidence"].strip()
                    if not evidence:
                        continue
                    if len(evidence) > MAX_CANDIDATE_CHARS:
                        evidence = evidence[:MAX_CANDIDATE_CHARS]
                        error = "graph evidence exceeds the runtime candidate size limit"
                        start = end = None
                    else:
                        start = text.find(evidence)
                        end = start + len(evidence) if start >= 0 else None
                        if start < 0:
                            start = None
                            error = "graph evidence text is not an exact raw Markdown slice"
                        elif text.find(evidence, start + 1) >= 0:
                            start = end = None
                            error = "graph evidence text occurs multiple times in raw Markdown"
                        else:
                            error = None
                    valid = start is not None and end is not None
                    span_hash = hash_bytes(evidence.encode("utf-8")) if valid else None
                    if valid:
                        assert start is not None and end is not None
                        context_start, context_end, context_text = (
                            _bounded_context_slice(text, start, end)
                        )
                        context_hash = hash_bytes(context_text.encode("utf-8"))
                    else:
                        context_start = context_end = None
                        context_text = context_hash = None
                    candidate = RawCandidateHit(
                        candidate_key=_candidate_key(
                                paper_id=paper_id,
                                raw_md_hash=raw_hash,
                                query_id=query_id,
                                query_text_hash=query_hash,
                                source_text=evidence,
                                start_offset=start,
                                end_offset=end,
                                origin=CandidateHitOrigin.GRAPH,
                        ),
                        paper_id=paper_id,
                        raw_md_path=raw_path,
                        query_id=query_id,
                        query_text_hash=query_hash,
                        intent=intent,
                        origin=CandidateHitOrigin.GRAPH,
                        source_text=evidence,
                        start_offset=start,
                        end_offset=end,
                        source_span_hash=span_hash,
                        context_text=context_text,
                        context_start_offset=context_start,
                        context_end_offset=context_end,
                        context_utf8_hash=context_hash,
                        section=(
                            _section_at(
                                heading_indexes.setdefault(
                                    paper_id, _heading_index(text)
                                ),
                                start,
                            )
                            if start is not None
                            else None
                        ),
                        upstream_node_id=str(node.get("id", "")) or None,
                        upstream_relation=str(relation),
                        score=0.9,
                        is_valid=valid,
                        validation_error=error,
                    )
                    if candidate.is_valid:
                        valid_candidates_seen += 1
                        _retain_best(
                            valid_results,
                            candidate,
                            limit=valid_limit,
                            key=lambda hit: (
                                -hit.score,
                                hit.paper_id,
                                hit.start_offset
                                if hit.start_offset is not None
                                else 2**63,
                                hit.candidate_key,
                            ),
                        )
                    else:
                        invalid_candidates_seen += 1
                        _retain_best(
                            invalid_results,
                            candidate,
                            limit=MAX_INVALID_DIAGNOSTICS,
                            key=lambda hit: (
                                -hit.score,
                                hit.paper_id,
                                hit.candidate_key,
                            ),
                        )
        return _BackendSearchBatch(
            hits=tuple([*valid_results, *invalid_results]),
            truncated_valid_candidates=valid_candidates_seen - len(valid_results),
            truncated_invalid_candidates=(
                invalid_candidates_seen - len(invalid_results)
            ),
        )


def _bounded_backend_batch(
    retriever,
    query_text: str,
    query_id: QueryId,
    intent: RetrievalIntent,
    *,
    max_query_terms: int,
) -> _BackendSearchBatch:
    accounted_search = getattr(retriever, "_search_with_accounting", None)
    if callable(accounted_search):
        return accounted_search(
            query_text,
            query_id,
            intent,
            valid_limit=MAX_TOP_K,
            max_query_terms=max_query_terms,
        )

    # Test and extension doubles that implement only the public protocol still
    # receive the largest bounded request.  Bound their returned values here
    # and make any coordinator-side truncation explicit in the ledger.
    returned_iter = iter(
        retriever.search(
            query_text,
            query_id,
            intent,
            top_k=MAX_TOP_K,
            max_query_terms=max_query_terms,
        )
    )
    returned = []
    for _ in range(MAX_EXTENSION_BACKEND_RESULTS + 1):
        try:
            returned.append(next(returned_iter))
        except StopIteration:
            break
    if len(returned) > MAX_EXTENSION_BACKEND_RESULTS:
        raise ValueError(
            "retrieval extension backend exceeded its bounded result limit"
        )
    valid = sorted(
        (item for item in returned if item.is_valid), key=_valid_hit_order_key
    )
    invalid = sorted(
        (item for item in returned if not item.is_valid), key=_invalid_hit_order_key
    )
    retained_valid = valid[:MAX_TOP_K]
    retained_invalid = invalid[:MAX_INVALID_DIAGNOSTICS]
    return _BackendSearchBatch(
        hits=tuple([*retained_valid, *retained_invalid]),
        truncated_valid_candidates=len(valid) - len(retained_valid),
        truncated_invalid_candidates=len(invalid) - len(retained_invalid),
    )


class UnifiedRetrievalCoordinator:
    """Merge runtime candidates without assigning IDs or scientific dispositions."""

    def __init__(
        self,
        text_retriever: DeterministicTextRetriever,
        graph_retriever: GraphAssistedRetriever | None = None,
    ) -> None:
        self.text_retriever = text_retriever
        self.graph_retriever = graph_retriever
        if graph_retriever is not None and (
            graph_retriever.corpus.lock_hash != text_retriever.corpus.lock_hash
            or graph_retriever.corpus.generation
            != text_retriever.corpus.generation
        ):
            raise CorpusIntegrityError(
                "retrieval ensemble members use different corpus generations"
            )

    @classmethod
    def from_generation(
        cls, review_root: Path, *, generation: int | None = None
    ) -> "UnifiedRetrievalCoordinator":
        """Construct the deterministic ensemble from one verified generation."""

        corpus = VerifiedCorpus(review_root, generation=generation)
        text = DeterministicTextRetriever._from_verified_corpus(corpus)
        graph = GraphAssistedRetriever._from_verified_corpus(corpus)
        return cls(text, graph)

    def retrieve(
        self,
        query_text: str,
        query_id: QueryId,
        intent: RetrievalIntent,
        *,
        top_k: int = 10,
        max_query_terms: int = MAX_QUERY_TERMS,
    ) -> tuple[list[RawCandidateHit], RetrievalLedger]:
        if not 1 <= top_k <= MAX_TOP_K:
            raise ValueError(f"top_k must be between 1 and {MAX_TOP_K}")
        if not 1 <= max_query_terms <= MAX_QUERY_TERMS:
            raise ValueError(
                f"max_query_terms must be between 1 and {MAX_QUERY_TERMS}"
            )
        text_batch = _bounded_backend_batch(
            self.text_retriever,
            query_text,
            query_id,
            intent,
            max_query_terms=max_query_terms,
        )
        graph_batch = (
            _bounded_backend_batch(
                self.graph_retriever,
                query_text,
                query_id,
                intent,
                max_query_terms=max_query_terms,
            )
            if self.graph_retriever is not None
            else _BackendSearchBatch(())
        )
        text_hits = list(text_batch.hits)
        graph_hits = list(graph_batch.hits)
        backend_hits = [*text_hits, *graph_hits]
        backend_keys = [hit.candidate_key for hit in backend_hits]
        if len(backend_keys) != len(set(backend_keys)):
            raise ValueError("retrieval backends emitted duplicate candidate keys")
        query_hash = _query_text_hash(query_text)
        invalid = [hit for hit in backend_hits if not hit.is_valid]
        merged: dict[tuple[str, int, int, str], RawCandidateHit] = {}
        for hit in [item for item in backend_hits if item.is_valid]:
            assert hit.start_offset is not None and hit.end_offset is not None
            assert hit.source_span_hash is not None
            key = (hit.paper_id, hit.start_offset, hit.end_offset, hit.source_span_hash)
            prior = merged.get(key)
            if prior is None:
                merged[key] = hit
            elif prior.origin is not hit.origin:
                graph_hit = (
                    hit if hit.origin is CandidateHitOrigin.GRAPH else prior
                )
                merged[key] = prior.model_copy(
                    update={
                        "origin": CandidateHitOrigin.BOTH,
                        "score": max(prior.score, hit.score),
                        "upstream_node_id": graph_hit.upstream_node_id,
                        "upstream_relation": graph_hit.upstream_relation,
                        "candidate_key": _candidate_key(
                            paper_id=prior.paper_id,
                            raw_md_hash=self.text_retriever.corpus.texts[prior.paper_id][1],
                            query_id=query_id,
                            query_text_hash=query_hash,
                            source_text=prior.source_text,
                            start_offset=prior.start_offset,
                            end_offset=prior.end_offset,
                            origin=CandidateHitOrigin.BOTH,
                        ),
                    }
                )
        ordered_valid = sorted(
            merged.values(),
            key=_valid_hit_order_key,
        )
        valid = ordered_valid[:top_k]
        excluded_by_budget = ordered_valid[top_k:]
        invalid = sorted(
            invalid,
            key=_invalid_hit_order_key,
        )
        ledger_hits = [*valid, *excluded_by_budget, *invalid]
        ledger = RetrievalLedger(
            source_generation=self.text_retriever.corpus.generation,
            query_id=query_id,
            query_text_hash=query_hash,
            corpus_lock_hash=self.text_retriever.corpus.lock_hash,
            requested_top_k=top_k,
            max_query_terms=max_query_terms,
            total_candidates=len(ledger_hits),
            valid_candidates=len(ordered_valid),
            invalid_candidates=len(invalid),
            text_truncated_valid_candidates=(
                text_batch.truncated_valid_candidates
            ),
            text_truncated_invalid_candidates=(
                text_batch.truncated_invalid_candidates
            ),
            graph_truncated_valid_candidates=(
                graph_batch.truncated_valid_candidates
            ),
            graph_truncated_invalid_candidates=(
                graph_batch.truncated_invalid_candidates
            ),
            selected_candidate_keys=[hit.candidate_key for hit in valid],
            excluded_by_budget_candidate_keys=[
                hit.candidate_key for hit in excluded_by_budget
            ],
            raw_hits=ledger_hits,
        )
        return valid, ledger
