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
from .selection import load_corpus_lock, verify_corpus_lock


MAX_TOP_K = 100
MAX_QUERY_TERMS = 64
MAX_CANDIDATE_CHARS = 16_384
MAX_INVALID_DIAGNOSTICS = 100
MAX_QUERY_CHARS = 4_096
MAX_QUERY_UTF8_BYTES = 16_384


class CandidateHitOrigin(StrEnum):
    GRAPH = "graph"
    TEXT_BASELINE = "text_baseline"
    BOTH = "both"


class RawCandidateHit(RuntimeModel):
    """Runtime retrieval proposal with no canonical R identifier or disposition."""

    candidate_key: Sha256
    paper_id: PaperId
    raw_md_path: str
    query_id: QueryId
    intent: RetrievalIntent
    origin: CandidateHitOrigin
    source_text: str = Field(min_length=1, max_length=MAX_CANDIDATE_CHARS)
    offset_unit: Literal["unicode_code_points"] = "unicode_code_points"
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, gt=0)
    source_span_hash: Sha256 | None = None
    section: str | None = None
    upstream_node_id: str | None = None
    upstream_relation: str | None = None
    score: float = Field(ge=0.0)
    is_valid: bool
    validation_error: str | None = None

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
        if self.is_valid:
            if self.start_offset is None or self.source_span_hash is None:
                raise ValueError("valid candidates require offsets and a source-span hash")
            if self.validation_error is not None:
                raise ValueError("valid candidates cannot carry a validation error")
            assert self.end_offset is not None
            if self.end_offset - self.start_offset != len(self.source_text):
                raise ValueError(
                    "valid candidate offsets must span exactly the source text"
                )
            if self.source_span_hash != hash_bytes(self.source_text.encode("utf-8")):
                raise ValueError(
                    "valid candidate source-span hash must match the source text"
                )
        elif not self.validation_error:
            raise ValueError("invalid candidates require a validation error")
        return self


class RetrievalLedger(RuntimeModel):
    query_id: QueryId
    corpus_lock_hash: Sha256
    total_candidates: int = Field(ge=0)
    valid_candidates: int = Field(ge=0)
    invalid_candidates: int = Field(ge=0)
    raw_hits: list[RawCandidateHit]

    @model_validator(mode="after")
    def _counts_and_query_agree(self) -> "RetrievalLedger":
        valid = sum(hit.is_valid for hit in self.raw_hits)
        invalid = len(self.raw_hits) - valid
        if self.total_candidates != len(self.raw_hits):
            raise ValueError("retrieval ledger total does not equal raw_hits")
        if self.valid_candidates != valid or self.invalid_candidates != invalid:
            raise ValueError("retrieval ledger validity counts do not match raw_hits")
        if any(hit.query_id != self.query_id for hit in self.raw_hits):
            raise ValueError("retrieval ledger contains a hit for another query")
        return self


@dataclass(frozen=True, slots=True, init=False, eq=False)
class VerifiedCorpus:
    """In-memory texts loaded only after lock, Paper, path, and raw hash checks."""

    review_root: Path
    generation: int
    lock_hash: Sha256
    _lock_bytes: bytes
    _texts: Mapping[str, tuple[str, str, str]]

    def __init__(self, review_root: Path) -> None:
        root = review_root.resolve(strict=False)
        store = GenerationStore(root)
        try:
            generation, snapshot, _ = store.load_current()
        except (OSError, ValueError) as exc:
            raise CorpusIntegrityError(
                "active generation or auxiliary hash mismatch"
            ) from exc
        lock, lock_hash = load_corpus_lock(root, generation=generation)
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
        object.__setattr__(self, "generation", generation)
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


def _query_terms(query: str) -> list[str]:
    if len(query) > MAX_QUERY_CHARS or len(query.encode("utf-8")) > MAX_QUERY_UTF8_BYTES:
        raise ValueError("retrieval query exceeds its deterministic text budget")
    terms = [term.casefold() for term in re.findall(r"[^\W_]+", query, flags=re.UNICODE)]
    return list(dict.fromkeys(term for term in terms if len(term) > 2))[:MAX_QUERY_TERMS]


def _candidate_key(
    *,
    paper_id: str,
    raw_md_hash: str,
    query_id: str,
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


@dataclass(frozen=True, slots=True, init=False, eq=False)
class DeterministicTextRetriever:
    _corpus: VerifiedCorpus

    def __init__(self, review_root: Path) -> None:
        object.__setattr__(self, "_corpus", VerifiedCorpus(review_root))

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
    ) -> list[RawCandidateHit]:
        if not 1 <= top_k <= MAX_TOP_K:
            raise ValueError(f"top_k must be between 1 and {MAX_TOP_K}")
        _, encoded_intent, _ = parse_query_id(query_id)
        if encoded_intent != intent.value:
            raise ValueError("query ID intent does not match requested retrieval intent")
        terms = _query_terms(query)
        if not terms:
            return []
        valid_hits: list[RawCandidateHit] = []
        invalid_hits: list[RawCandidateHit] = []
        for paper_id, (raw_path, raw_hash, text) in sorted(self.corpus.texts.items()):
            headings = _heading_index(text)
            for start, end in _paragraph_slices(text):
                clean = text[start:end]
                lowered = clean.casefold()
                matched = sum(term in lowered for term in terms)
                if matched == 0:
                    continue
                if len(clean) > MAX_CANDIDATE_CHARS:
                    bounded = clean[:MAX_CANDIDATE_CHARS]
                    _retain_best(
                        invalid_hits,
                        RawCandidateHit(
                            candidate_key=_candidate_key(
                                paper_id=paper_id,
                                raw_md_hash=raw_hash,
                                query_id=query_id,
                                source_text=bounded,
                                start_offset=None,
                                end_offset=None,
                                origin=CandidateHitOrigin.TEXT_BASELINE,
                            ),
                            paper_id=paper_id,
                            raw_md_path=raw_path,
                            query_id=query_id,
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
                span_hash = hash_bytes(clean.encode("utf-8"))
                score = matched / len(terms)
                _retain_best(
                    valid_hits,
                    RawCandidateHit(
                        candidate_key=_candidate_key(
                            paper_id=paper_id,
                            raw_md_hash=raw_hash,
                            query_id=query_id,
                            source_text=clean,
                            start_offset=start,
                            end_offset=end,
                            origin=CandidateHitOrigin.TEXT_BASELINE,
                        ),
                        paper_id=paper_id,
                        raw_md_path=raw_path,
                        query_id=query_id,
                        intent=intent,
                        origin=CandidateHitOrigin.TEXT_BASELINE,
                        source_text=clean,
                        start_offset=start,
                        end_offset=end,
                        source_span_hash=span_hash,
                        section=_section_at(headings, start),
                        score=score,
                        is_valid=True,
                    ),
                    limit=top_k,
                    key=lambda hit: (
                        -hit.score,
                        hit.paper_id,
                        hit.start_offset or 0,
                        hit.candidate_key,
                    ),
                )
        return [*valid_hits, *invalid_hits]


@dataclass(frozen=True, slots=True, init=False, eq=False)
class GraphAssistedRetriever:
    _corpus: VerifiedCorpus
    _graph_content: bytes
    _graph_path: str
    _library_id: str | None
    _source_commit: str | None
    _citekey_to_paper: Mapping[str, str]

    def __init__(self, adapter: ReadOnlyGraphAdapter, review_root: Path) -> None:
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
        corpus = VerifiedCorpus(review_root)
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
    ) -> list[RawCandidateHit]:
        if not 1 <= top_k <= MAX_TOP_K:
            raise ValueError(f"top_k must be between 1 and {MAX_TOP_K}")
        _, encoded_intent, _ = parse_query_id(query_id)
        if encoded_intent != intent.value:
            raise ValueError("query ID intent does not match requested retrieval intent")
        if not _query_terms(query):
            return []
        valid_results: list[RawCandidateHit] = []
        invalid_results: list[RawCandidateHit] = []
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
        for node in ReadOnlyGraphAdapter.iter_candidates(private_adapter, query):
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
                    candidate = RawCandidateHit(
                        candidate_key=_candidate_key(
                                paper_id=paper_id,
                                raw_md_hash=raw_hash,
                                query_id=query_id,
                                source_text=evidence,
                                start_offset=start,
                                end_offset=end,
                                origin=CandidateHitOrigin.GRAPH,
                        ),
                        paper_id=paper_id,
                        raw_md_path=raw_path,
                        query_id=query_id,
                        intent=intent,
                        origin=CandidateHitOrigin.GRAPH,
                        source_text=evidence,
                        start_offset=start,
                        end_offset=end,
                        source_span_hash=span_hash,
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
                        _retain_best(
                            valid_results,
                            candidate,
                            limit=top_k,
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
        return [*valid_results, *invalid_results]


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
        ):
            raise CorpusIntegrityError("retrieval ensemble members use different corpus locks")

    def retrieve(
        self,
        query_text: str,
        query_id: QueryId,
        intent: RetrievalIntent,
        *,
        top_k: int = 10,
    ) -> tuple[list[RawCandidateHit], RetrievalLedger]:
        text_hits = self.text_retriever.search(
            query_text, query_id, intent, top_k=top_k
        )
        graph_hits = (
            self.graph_retriever.search(query_text, query_id, intent, top_k=top_k)
            if self.graph_retriever is not None
            else []
        )
        invalid = [hit for hit in [*text_hits, *graph_hits] if not hit.is_valid]
        merged: dict[tuple[str, int, int, str], RawCandidateHit] = {}
        for hit in [item for item in [*text_hits, *graph_hits] if item.is_valid]:
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
                            source_text=prior.source_text,
                            start_offset=prior.start_offset,
                            end_offset=prior.end_offset,
                            origin=CandidateHitOrigin.BOTH,
                        ),
                    }
                )
        valid = sorted(
            merged.values(),
            key=lambda hit: (-hit.score, hit.paper_id, hit.start_offset or 0, hit.candidate_key),
        )[:top_k]
        ledger_hits = [*valid, *invalid]
        ledger = RetrievalLedger(
            query_id=query_id,
            corpus_lock_hash=self.text_retriever.corpus.lock_hash,
            total_candidates=len(ledger_hits),
            valid_candidates=len(valid),
            invalid_candidates=len(invalid),
            raw_hits=ledger_hits,
        )
        return valid, ledger
