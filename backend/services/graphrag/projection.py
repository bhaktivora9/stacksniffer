"""The PostgreSQL graph projection of one analysis (PROJECTING).

Nodes are the analysis's internal entities (declarations and files) outside generated and
vendored files. Edges are its canonical ``core.repository_edge`` rows between such nodes, plus
edges whose target the extractor left as an ``EXTERNAL_SYMBOL`` but that resolve to exactly one
internal entity (an import of a repository class, ``extends Person``, ``crud.authenticate()``).
Resolved edges are marked ``derived``: they live only in the projection and are never written
back as canonical facts.

The graph is rebuilt from PostgreSQL on demand. A ``graphrag.graph_snapshot`` row records its
versions and content fingerprint, so a rebuilt graph is checked against the snapshot it claims
to be; this keeps the projection disposable, as the R1 architecture requires.
"""

from __future__ import annotations

import hashlib
import posixpath
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

GRAPH_SCHEMA_VERSION = "pg-graph/1"
PROJECTION_VERSION = "stacksniffer-projection/1"
_CALLABLE_TYPES = ("CLASS", "INTERFACE", "METHOD", "FUNCTION")
_RECEIVERS = {"self", "this", "super", "cls"}
_MODULE_SUFFIXES = (".py", "/__init__.py", ".js", ".ts", ".jsx", ".tsx", "/index.js", "/index.ts", ".rb", ".go")


class StaleProjection(RuntimeError):
    """The graph rebuilt from PostgreSQL no longer matches the active snapshot."""

    failure_code = "PROJECTION_STALE"


class ProjectionMissing(RuntimeError):
    failure_code = "PROJECTION_MISSING"


@dataclass(frozen=True)
class Node:
    id: UUID
    stable_key: str
    entity_type: str
    name: str
    qualified_name: str | None
    file_id: UUID | None
    path: str | None
    start_line: int | None
    end_line: int | None
    parent_id: UUID | None


@dataclass(frozen=True)
class Edge:
    source: UUID
    target: UUID
    relation: str
    derived: bool


@dataclass
class ProjectedGraph:
    analysis_id: UUID
    nodes: dict[UUID, Node]
    edges: list[Edge]
    unresolved: int = 0
    fingerprint: str = ""
    _adjacency: dict[UUID, list[tuple[UUID, str, bool]]] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        adjacency: dict[UUID, list[tuple[UUID, str, bool]]] = defaultdict(list)
        for edge in self.edges:
            adjacency[edge.source].append((edge.target, edge.relation, True))
            adjacency[edge.target].append((edge.source, edge.relation, False))
        self._adjacency = dict(adjacency)
        if not self.fingerprint:
            self.fingerprint = _fingerprint(self)

    def neighbors(self, node_id: UUID) -> list[tuple[UUID, str, bool]]:
        """(neighbour, relation, outgoing) for every edge touching the node."""
        return self._adjacency.get(node_id, [])

    @property
    def derived_edges(self) -> int:
        return sum(edge.derived for edge in self.edges)


def _fingerprint(graph: ProjectedGraph) -> str:
    keys = {node.id: node.stable_key for node in graph.nodes.values()}
    digest = hashlib.sha256(PROJECTION_VERSION.encode())
    for key in sorted(keys.values()):
        digest.update(b"N" + key.encode() + b"\n")
    for line in sorted(f"{keys[e.source]}|{e.relation}|{keys[e.target]}|{int(e.derived)}" for e in graph.edges):
        digest.update(b"E" + line.encode() + b"\n")
    return digest.hexdigest()


def build_graph(conn: Any, analysis_id: UUID) -> ProjectedGraph:
    analysis = str(analysis_id)
    nodes = {row[0]: Node(*row) for row in conn.fetch_all(
        "SELECT e.id, e.stable_key, e.entity_type, e.name, e.qualified_name, e.file_id, f.path, e.start_line, "
        "e.end_line, e.parent_entity_id FROM core.entity e LEFT JOIN core.source_file f ON f.id = e.file_id "
        "WHERE e.analysis_id = %s AND e.entity_type <> 'EXTERNAL_SYMBOL' "
        "AND (f.id IS NULL OR NOT (f.is_generated OR f.is_vendored))", analysis)}
    externals = {row[0]: (row[1], row[2]) for row in conn.fetch_all(
        "SELECT id, stable_key, name FROM core.entity WHERE analysis_id = %s AND entity_type = 'EXTERNAL_SYMBOL'",
        analysis)}
    rows = conn.fetch_all("SELECT source_entity_id, target_entity_id, relationship_type FROM core.repository_edge "
                          "WHERE analysis_id = %s", analysis)
    resolver = _Resolver(nodes)
    edges: set[Edge] = set()
    pending: list[tuple[UUID, str, str, str]] = []
    for source, target, relation in rows:
        if source not in nodes:
            continue
        if target in nodes:
            edges.add(Edge(source, target, relation, False))
        elif target in externals:
            key, name = externals[target]
            pending.append((source, relation, key, name))
    # Imports first: they narrow which internal entities a file's other references can mean.
    pending.sort(key=lambda item: item[1] != "IMPORTS")
    unresolved = 0
    for source, relation, key, name in pending:
        resolved = resolver.resolve(source, relation, key, name)
        if resolved is None or resolved == source:
            unresolved += 1
            continue
        if relation == "IMPORTS":
            resolver.record_import(source, resolved)
        edges.add(Edge(source, resolved, relation, True))
    canonical = {(e.source, e.target, e.relation) for e in edges if not e.derived}
    unique = [e for e in edges if not e.derived or (e.source, e.target, e.relation) not in canonical]
    unique.sort(key=lambda e: (nodes[e.source].stable_key, e.relation, nodes[e.target].stable_key, e.derived))
    return ProjectedGraph(analysis_id, nodes, unique, unresolved)


class _Resolver:
    """Maps an external reference to one internal entity, or to nothing when it is ambiguous."""

    def __init__(self, nodes: dict[UUID, Node]):
        self.nodes = nodes
        self.by_name: dict[str, list[UUID]] = defaultdict(list)
        self.by_qualified: dict[str, list[UUID]] = defaultdict(list)
        self.files_by_path: dict[str, UUID] = {}
        self.imports: dict[UUID, set[UUID]] = defaultdict(set)  # file id -> imported internal entity ids
        for node in nodes.values():
            if node.entity_type == "FILE" and node.path:
                self.files_by_path[node.path] = node.id
            elif node.entity_type in _CALLABLE_TYPES:
                self.by_name[node.name].append(node.id)
                if node.qualified_name:
                    self.by_qualified[node.qualified_name].append(node.id)

    def record_import(self, source: UUID, target: UUID) -> None:
        file_id = self.nodes[source].file_id
        if file_id is not None:
            self.imports[file_id].add(target)

    def resolve(self, source: UUID, relation: str, key: str, name: str) -> UUID | None:
        kind = key.split(":")[2] if key.count(":") >= 3 else ""
        if relation == "IMPORTS" or kind in ("import", "module"):
            return self._resolve_import(name)
        return self._resolve_reference(source, name)

    def _resolve_import(self, name: str) -> UUID | None:
        exact = self.by_qualified.get(name, [])
        if len(exact) == 1:
            return exact[0]
        module = name.replace(".", "/")
        files = {fid for path, fid in self.files_by_path.items()
                 for suffix in _MODULE_SUFFIXES if path == module + suffix or path.endswith("/" + module + suffix)}
        return next(iter(files)) if len(files) == 1 else None

    def _resolve_reference(self, source: UUID, name: str) -> UUID | None:
        segments = [s for s in name.replace("::", ".").split(".") if s]
        if not segments:
            return None
        last, qualifier = segments[-1], (segments[-2] if len(segments) > 1 else None)
        candidates = self.by_name.get(last, [])
        if not candidates:
            return None
        if qualifier and qualifier not in _RECEIVERS:
            narrowed = [c for c in candidates if self._named_by(c, qualifier)]
            if len(narrowed) == 1:
                return narrowed[0]
            if narrowed:
                candidates = narrowed
        preferred = self._near(source, candidates)
        if len(preferred) == 1:
            return preferred[0]
        unqualified = qualifier is None or qualifier in _RECEIVERS
        return candidates[0] if unqualified and len(candidates) == 1 else None

    def _named_by(self, candidate: UUID, qualifier: str) -> bool:
        node = self.nodes[candidate]
        stem = posixpath.splitext(posixpath.basename(node.path or ""))[0]
        parent = self.nodes.get(node.parent_id) if node.parent_id else None
        return (stem == qualifier or (parent is not None and parent.name == qualifier)
                or f".{qualifier}." in f".{node.qualified_name or ''}")

    def _near(self, source: UUID, candidates: list[UUID]) -> list[UUID]:
        """Candidates in the source's own file, a file it imports, or its directory (package)."""
        origin = self.nodes[source]
        imported = self.imports.get(origin.file_id, set())
        imported_files = {self.nodes[i].file_id if self.nodes[i].entity_type != "FILE" else i for i in imported}
        directory = posixpath.dirname(origin.path or "")
        near = []
        for candidate in candidates:
            node = self.nodes[candidate]
            container = node.parent_id if node.entity_type in ("METHOD",) else candidate
            if (node.file_id == origin.file_id or candidate in imported or container in imported
                    or node.file_id in imported_files or posixpath.dirname(node.path or "") == directory):
                near.append(candidate)
        return near


# --- snapshots -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    id: UUID
    analysis_id: UUID
    fingerprint: str
    node_count: int
    edge_count: int


def project_analysis(conn: Any, analysis_id: UUID) -> tuple[Snapshot, ProjectedGraph]:
    """Build, validate and activate the analysis's snapshot; an identical active snapshot is reused."""
    graph = build_graph(conn, analysis_id)
    for edge in graph.edges:  # validation: every edge stays inside the analysis's node set
        if edge.source not in graph.nodes or edge.target not in graph.nodes:
            raise StaleProjection(f"edge {edge} leaves the projected node set")
    row = conn.fetch_one(
        "SELECT id, status FROM graphrag.graph_snapshot WHERE analysis_id = %s AND graph_schema_version = %s "
        "AND projection_version = %s AND content_fingerprint = %s",
        str(analysis_id), GRAPH_SCHEMA_VERSION, PROJECTION_VERSION, graph.fingerprint)
    if row is not None and row[1] == "ACTIVE":
        return Snapshot(row[0], analysis_id, graph.fingerprint, len(graph.nodes), len(graph.edges)), graph
    conn.execute("UPDATE graphrag.graph_snapshot SET status = 'RETIRED' WHERE analysis_id = %s AND status = 'ACTIVE'",
                 str(analysis_id))
    if row is None:
        snapshot_id = conn.fetch_scalar(
            "INSERT INTO graphrag.graph_snapshot (analysis_id, graph_schema_version, projection_version, "
            "content_fingerprint, status, node_count, edge_count, completed_at) "
            "VALUES (%s, %s, %s, %s, 'VALIDATING', %s, %s, now()) RETURNING id",
            str(analysis_id), GRAPH_SCHEMA_VERSION, PROJECTION_VERSION, graph.fingerprint, len(graph.nodes),
            len(graph.edges))
    else:
        snapshot_id = row[0]
    conn.execute("UPDATE graphrag.graph_snapshot SET status = 'ACTIVE', activated_at = now() WHERE id = %s",
                 str(snapshot_id))
    return Snapshot(snapshot_id, analysis_id, graph.fingerprint, len(graph.nodes), len(graph.edges)), graph


_CACHE: dict[UUID, ProjectedGraph] = {}
_CACHE_SIZE = 8


def active_graph(conn: Any, analysis_id: UUID) -> tuple[Snapshot, ProjectedGraph]:
    """The analysis's active snapshot and its graph, rebuilt (or cached) and checked against it."""
    row = conn.fetch_one("SELECT id, content_fingerprint, node_count, edge_count FROM graphrag.graph_snapshot "
                         "WHERE analysis_id = %s AND status = 'ACTIVE'", str(analysis_id))
    if row is None:
        raise ProjectionMissing(f"analysis {analysis_id} has no active graph snapshot")
    snapshot = Snapshot(row[0], analysis_id, row[1], row[2], row[3])
    graph = _CACHE.get(snapshot.id)
    if graph is None:
        graph = build_graph(conn, analysis_id)
        if graph.fingerprint != snapshot.fingerprint:
            raise StaleProjection(f"analysis {analysis_id}: the canonical graph changed since snapshot {snapshot.id}")
        if len(_CACHE) >= _CACHE_SIZE:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[snapshot.id] = graph
    return snapshot, graph
