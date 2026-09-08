"""Read-only inspection and search over a graph stored in a pinned Git blob."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from itertools import islice
from typing import Any, Protocol

from .git_source import PinnedGitSource, compute_content_sha256
from .models import GraphSchemaReport, GraphSchemaStatus, UnsupportedGraphSchemaError


class LibraryGraphAdapter(Protocol):
    def inspect(self) -> GraphSchemaReport: ...
    def find_candidates(self, query: str) -> list[dict[str, Any]]: ...
    def iter_candidates(self, query: str) -> Iterator[dict[str, Any]]: ...
    def resolve_sources(self, hits: list[dict[str, Any]]) -> list[dict[str, Any]]: ...


def _unsupported(path: str, content_hash: str, *, root_type: str, keys: list[str] | None = None) -> GraphSchemaReport:
    return GraphSchemaReport(
        graph_path=path,
        content_sha256=content_hash,
        root_type=root_type,
        container_keys=keys or [],
        node_count=0,
        edge_count=0,
        node_id_field="",
        edge_source_field="",
        edge_target_field="",
        node_fields_observed=[],
        edge_fields_observed=[],
        relation_labels=[],
        extraction_classes=[],
        schema_status=GraphSchemaStatus.UNSUPPORTED_GRAPH_SCHEMA,
    )


class ReadOnlyGraphAdapter:
    """Adapter constructed only from already-pinned bytes, never a working-tree path."""

    def __init__(
        self,
        content: bytes,
        source_relative_path: str,
        *,
        library_id: str | None = None,
        source_commit: str | None = None,
    ) -> None:
        self._source_relative_path = source_relative_path
        self._library_id = library_id
        self._source_commit = source_commit
        self._content = bytes(content)
        self._hash = compute_content_sha256(self._content)
        self._data: dict[str, Any] | None = None
        self._report: GraphSchemaReport | None = None

    @property
    def source_relative_path(self) -> str:
        return self._source_relative_path

    @property
    def library_id(self) -> str | None:
        return self._library_id

    @property
    def source_commit(self) -> str | None:
        return self._source_commit

    @property
    def content_sha256(self) -> str:
        return self._hash

    @classmethod
    def from_source(
        cls, source: PinnedGitSource, source_relative_path: str
    ) -> "ReadOnlyGraphAdapter":
        return cls(
            source.read_path(source_relative_path),
            source_relative_path,
            library_id=source.config.library_id,
            source_commit=source.expected_commit,
        )

    def _decode(self) -> Any:
        try:
            return json.loads(self._content.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def inspect(self) -> GraphSchemaReport:
        if self._report is not None:
            return self._report.model_copy(deep=True)
        data = self._decode()
        if not isinstance(data, dict):
            self._report = _unsupported(
                self.source_relative_path,
                self._hash,
                root_type="invalid_json" if data is None else type(data).__name__,
            )
            return self._report.model_copy(deep=True)
        keys = sorted(str(key) for key in data)
        nodes = data.get("nodes")
        edges = data.get("links", data.get("edges"))
        if not isinstance(nodes, list) or not isinstance(edges, list):
            self._report = _unsupported(
                self.source_relative_path, self._hash, root_type="dict", keys=keys
            )
            return self._report.model_copy(deep=True)
        if any(not isinstance(item, dict) for item in (*nodes, *edges)):
            self._report = _unsupported(
                self.source_relative_path, self._hash, root_type="dict", keys=keys
            )
            return self._report.model_copy(deep=True)

        node_fields: set[str] = set()
        node_ids: set[str] = set()
        duplicates: list[str] = []
        has_nested_evidence = False
        for node in nodes:
            node_fields.update(str(key) for key in node)
            if "id" not in node:
                continue
            node_id = str(node["id"])
            if node_id in node_ids:
                duplicates.append(node_id)
            node_ids.add(node_id)
            attributes = node.get("attributes")
            if isinstance(attributes, dict):
                for value in attributes.values():
                    values = value if isinstance(value, list) else [value]
                    if any(isinstance(item, dict) and "evidence" in item for item in values):
                        has_nested_evidence = True

        edge_fields: set[str] = set()
        relation_labels: set[str] = set()
        extraction_classes: set[str] = set()
        dangling: list[dict[str, Any]] = []
        source_field = "source"
        target_field = "target"
        for index, edge in enumerate(edges):
            edge_fields.update(str(key) for key in edge)
            if "source" not in edge and "_src" in edge:
                source_field = "_src"
            if "target" not in edge and "_tgt" in edge:
                target_field = "_tgt"
            source_value = edge.get("source", edge.get("_src"))
            target_value = edge.get("target", edge.get("_tgt"))
            if source_value is not None and str(source_value) not in node_ids:
                dangling.append({"edge_index": index, "endpoint": "source", "value": str(source_value)})
            if target_value is not None and str(target_value) not in node_ids:
                dangling.append({"edge_index": index, "endpoint": "target", "value": str(target_value)})
            if edge.get("relation") is not None:
                relation_labels.add(str(edge["relation"]))
            extraction = edge.get("extraction_class", edge.get("confidence"))
            if extraction is not None:
                extraction_classes.add(str(extraction))

        status = (
            GraphSchemaStatus.UNSUPPORTED_GRAPH_SCHEMA
            if duplicates or dangling or any("id" not in node for node in nodes)
            else GraphSchemaStatus.SUPPORTED
        )
        self._data = data
        self._report = GraphSchemaReport(
            graph_path=self.source_relative_path,
            content_sha256=self._hash,
            root_type="dict",
            container_keys=keys,
            node_count=len(nodes),
            edge_count=len(edges),
            node_id_field="id",
            edge_source_field=source_field,
            edge_target_field=target_field,
            node_fields_observed=sorted(node_fields),
            edge_fields_observed=sorted(edge_fields),
            relation_labels=sorted(relation_labels),
            extraction_classes=sorted(extraction_classes),
            duplicate_node_ids=sorted(set(duplicates)),
            dangling_endpoints=dangling,
            has_nested_evidence=has_nested_evidence,
            schema_status=status,
        )
        return self._report.model_copy(deep=True)

    def graph_nodes(self) -> list[dict[str, Any]]:
        report = self.inspect()
        if report.schema_status is not GraphSchemaStatus.SUPPORTED or self._data is None:
            raise UnsupportedGraphSchemaError("cannot use an unsupported graph export")
        return copy.deepcopy(self._data["nodes"])

    def iter_candidates(self, query: str) -> Iterator[dict[str, Any]]:
        terms = [term.casefold() for term in query.split() if term]
        if not terms:
            return
        report = self.inspect()
        if report.schema_status is not GraphSchemaStatus.SUPPORTED or self._data is None:
            raise UnsupportedGraphSchemaError("cannot use an unsupported graph export")
        for node in self._data["nodes"]:
            searchable = " ".join(
                str(node.get(field, "")) for field in ("id", "label", "norm_label")
            ).casefold()
            if all(term in searchable for term in terms):
                yield copy.deepcopy(node)

    def find_candidates(
        self, query: str, *, max_results: int = 1_000
    ) -> list[dict[str, Any]]:
        if not 1 <= max_results <= 10_000:
            raise ValueError("graph result limit must be between 1 and 10000")
        return list(islice(self.iter_candidates(query), max_results))

    def resolve_sources(self, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "node_id": hit.get("id"),
                "citekey": hit.get("citekey") or hit.get("id"),
                "label": hit.get("label"),
                "file_type": hit.get("file_type"),
                "paper_type": hit.get("paper_type"),
            }
            for hit in hits
        ]
