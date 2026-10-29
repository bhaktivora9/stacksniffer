"""Community detection over a graph snapshot, persisted as a versioned community run.

Edges are weighted by relation (inheritance binds hardest, imports least). Louvain's levels
become the hierarchy: level 0 is the coarsest partition, and each finer community whose members
differ from its parent's is kept as that parent's child. Communities of one node are noise and
are not persisted; the run records how many there were. Test code stays in the graph (local
search can reach it) but joins no community, so summaries describe the product, not its tests.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .louvain import ALGORITHM, ALGORITHM_VERSION, louvain, modularity
from .projection import ProjectedGraph, Snapshot

RELATION_WEIGHTS = {"EXTENDS": 2.0, "IMPLEMENTS": 2.0, "CONTAINS": 1.0, "CALLS": 1.0, "IMPORTS": 0.5,
                    "DECLARES": 1.0, "DEPENDS_ON": 0.5}
PARAMETERS = {"resolution": 1.0, "seed": 42, "min_members": 2, "relation_weights": RELATION_WEIGHTS,
              "exclude": "tests"}
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec|testing)/|(^|/)test_[^/]*$|_test\.[a-z]+$|"
                        r"(Tests?|IT|Spec)\.(java|scala|kt|groovy)$|\.(test|spec)\.[jt]sx?$|_spec\.rb$|(^|/)conftest\.py$")


def is_test_path(path: str | None) -> bool:
    return bool(path) and bool(_TEST_PATH.search(path))


@dataclass(frozen=True)
class Community:
    id: UUID
    key: str
    level: int
    parent_id: UUID | None
    members: tuple[UUID, ...]


@dataclass(frozen=True)
class CommunityRun:
    id: UUID
    communities: tuple[Community, ...]

    def children(self, community_id: UUID) -> list[Community]:
        return [c for c in self.communities if c.parent_id == community_id]


def weighted_edges(graph: ProjectedGraph) -> list[tuple[UUID, UUID, float]]:
    return [(e.source, e.target, RELATION_WEIGHTS.get(e.relation, 0.5)) for e in graph.edges]


def detect_communities(conn: Any, snapshot: Snapshot, graph: ProjectedGraph) -> CommunityRun:
    """Run (or reuse) Louvain over the snapshot and persist the hierarchy."""
    dataset = hashlib.sha256(f"{snapshot.fingerprint}|{json.dumps(PARAMETERS, sort_keys=True)}".encode()).hexdigest()
    existing = conn.fetch_one(
        "SELECT id, status FROM graphrag.community_run WHERE graph_snapshot_id = %s AND algorithm = %s "
        "AND algorithm_version = %s AND dataset_fingerprint = %s",
        str(snapshot.id), ALGORITHM, ALGORITHM_VERSION, dataset)
    if existing is not None and existing[1] == "SUCCEEDED":
        return load_run(conn, existing[0])
    if existing is not None:  # an interrupted run: start it over
        conn.execute("DELETE FROM graphrag.community_run WHERE id = %s", str(existing[0]))

    nodes = [n for n, node in graph.nodes.items() if not is_test_path(node.path)]
    edges = weighted_edges(graph)
    levels = louvain(nodes, edges, seed=PARAMETERS["seed"], resolution=PARAMETERS["resolution"])
    coarse, fine = levels[-1], levels[0]
    run_id = conn.fetch_scalar(
        "INSERT INTO graphrag.community_run (graph_snapshot_id, algorithm, algorithm_version, parameters, random_seed, "
        "dataset_fingerprint, status) VALUES (%s, %s, %s, %s::jsonb, %s, %s, 'RUNNING') RETURNING id",
        str(snapshot.id), ALGORITHM, ALGORITHM_VERSION, json.dumps(PARAMETERS), PARAMETERS["seed"], dataset)

    def groups(partition: dict) -> list[tuple[UUID, ...]]:
        by_label: dict[int, list[UUID]] = defaultdict(list)
        for node, label in partition.items():
            by_label[label].append(node)
        members = [tuple(sorted(m, key=lambda n: graph.nodes[n].stable_key)) for m in by_label.values()]
        return sorted(members, key=lambda m: (-len(m), graph.nodes[m[0]].stable_key))

    minimum = PARAMETERS["min_members"]
    created: list[Community] = []
    parents: dict[UUID, UUID] = {}  # node -> level-0 community id
    singletons = 0
    for index, members in enumerate(groups(coarse)):
        if len(members) < minimum:
            singletons += 1
            continue
        community = _insert(conn, run_id, f"L0-{index}", 0, None, members, graph)
        created.append(community)
        for node in members:
            parents[node] = community.id
    if len(levels) > 1:
        for index, members in enumerate(groups(fine)):
            parent = parents.get(members[0])
            if parent is None or len(members) < minimum:
                continue
            if set(members) == set(next(c.members for c in created if c.id == parent)):
                continue  # the same set as its parent adds no level
            created.append(_insert(conn, run_id, f"L1-{index}", 1, parent, members, graph))

    metrics = {"levels": len(levels), "modularity": round(modularity(edges, coarse), 4),
               "communities": {str(level): sum(c.level == level for c in created) for level in (0, 1)},
               "singletons": singletons, "nodes": len(nodes), "test_nodes": len(graph.nodes) - len(nodes),
               "largest_share": round(max((len(c.members) for c in created if c.level == 0), default=0)
                                      / max(len(nodes), 1), 4)}
    conn.execute("UPDATE graphrag.community_run SET status = 'SUCCEEDED', completed_at = now(), "
                 "parameters = parameters || jsonb_build_object('metrics', %s::jsonb) WHERE id = %s",
                 json.dumps(metrics), str(run_id))
    return CommunityRun(run_id, tuple(created))


def _insert(conn: Any, run_id: UUID, key: str, level: int, parent: UUID | None, members: tuple[UUID, ...],
            graph: ProjectedGraph) -> Community:
    files = sorted({graph.nodes[m].path for m in members if graph.nodes[m].path})
    member_set = set(members)
    internal = sum(1 for e in graph.edges if e.source in member_set and e.target in member_set)
    community_id = conn.fetch_scalar(
        "INSERT INTO graphrag.community (community_run_id, parent_community_id, community_key, hierarchy_level, "
        "member_count, metrics) VALUES (%s, %s, %s, %s, %s, %s::jsonb) RETURNING id",
        str(run_id), str(parent) if parent else None, key, level, len(members),
        json.dumps({"files": len(files), "internal_edges": internal, "top_files": files[:5]}))
    conn.execute("INSERT INTO graphrag.community_member (community_id, entity_id) "
                 "SELECT %s, unnest(%s::uuid[])", str(community_id), [str(m) for m in members])
    return Community(community_id, key, level, parent, members)


def load_run(conn: Any, run_id: UUID) -> CommunityRun:
    members: dict[UUID, list[UUID]] = defaultdict(list)
    for community_id, entity_id in conn.fetch_all(
            "SELECT m.community_id, m.entity_id FROM graphrag.community_member m "
            "JOIN graphrag.community c ON c.id = m.community_id WHERE c.community_run_id = %s", str(run_id)):
        members[community_id].append(entity_id)
    communities = tuple(
        Community(cid, key, level, parent, tuple(members[cid]))
        for cid, key, level, parent in conn.fetch_all(
            "SELECT id, community_key, hierarchy_level, parent_community_id FROM graphrag.community "
            "WHERE community_run_id = %s ORDER BY hierarchy_level, community_key", str(run_id)))
    return CommunityRun(run_id, communities)


def active_run(conn: Any, snapshot_id: UUID) -> CommunityRun | None:
    run_id = conn.fetch_scalar("SELECT id FROM graphrag.community_run WHERE graph_snapshot_id = %s "
                               "AND status = 'SUCCEEDED' ORDER BY completed_at DESC LIMIT 1", str(snapshot_id))
    return load_run(conn, run_id) if run_id else None
