"""Read access to the canonical IR for downstream stages (indexing, graph retrieval, classification).

Every downstream reader must go through these functions rather than query ``core.entity`` or
``core.repository_edge`` directly. By default they return **first-party code only**:

    source_file.is_generated = false AND source_file.is_vendored = false

* An entity is first-party when its file is first-party. Entities without a file
  (unresolved ``EXTERNAL_SYMBOL`` placeholders) belong to no file and are kept.
* An edge is first-party only when **both** endpoints are, so the default graph never
  contains an edge to a node it hides. A call from application code into vendored code
  is therefore hidden by default, like the vendored code itself.

Generated and vendored facts stay persisted, with their evidence, and are reachable
through the explicit ``IRScope.diagnostic()`` override for debugging and audits.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class IRScope:
    include_generated: bool = False
    include_vendored: bool = False

    @classmethod
    def first_party(cls) -> IRScope:
        return cls()

    @classmethod
    def diagnostic(cls) -> IRScope:
        """Everything extracted, including generated and vendored code. Not for indexing or retrieval."""
        return cls(include_generated=True, include_vendored=True)

    def file_clause(self, alias: str) -> str:
        """SQL condition on a (possibly NULL, LEFT JOINed) ``core.source_file`` alias."""
        conditions = []
        if not self.include_generated:
            conditions.append(f"{alias}.is_generated IS NOT TRUE")
        if not self.include_vendored:
            conditions.append(f"{alias}.is_vendored IS NOT TRUE")
        return " AND ".join(conditions) or "TRUE"


FIRST_PARTY = IRScope.first_party()


def fetch_entities(conn: Any, analysis_id: UUID, *, scope: IRScope = FIRST_PARTY,
                   entity_types: Iterable[str] | None = None) -> list[dict[str, Any]]:
    types = sorted(set(entity_types)) if entity_types else None
    rows = conn.fetch_all(
        f"""
        SELECT e.stable_key, e.entity_type, e.name, e.qualified_name, sf.path, e.start_line, e.end_line
        FROM core.entity AS e
        LEFT JOIN core.source_file AS sf ON sf.id = e.file_id AND sf.analysis_id = e.analysis_id
        WHERE e.analysis_id = %s
          AND {scope.file_clause("sf")}
          AND (%s::text[] IS NULL OR e.entity_type = ANY(%s::text[]))
        ORDER BY e.stable_key
        """,
        str(analysis_id), types, types,
    )
    columns = ("stable_key", "entity_type", "name", "qualified_name", "path", "start_line", "end_line")
    return [dict(zip(columns, row)) for row in rows]


def fetch_edges(conn: Any, analysis_id: UUID, *, scope: IRScope = FIRST_PARTY,
                relationship_types: Iterable[str] | None = None) -> list[dict[str, Any]]:
    types = sorted(set(relationship_types)) if relationship_types else None
    rows = conn.fetch_all(
        f"""
        SELECT s.stable_key, r.relationship_type, t.stable_key, r.certainty, r.metadata->>'resolution_basis'
        FROM core.repository_edge AS r
        JOIN core.entity AS s ON s.id = r.source_entity_id
        JOIN core.entity AS t ON t.id = r.target_entity_id
        LEFT JOIN core.source_file AS sf ON sf.id = s.file_id AND sf.analysis_id = r.analysis_id
        LEFT JOIN core.source_file AS tf ON tf.id = t.file_id AND tf.analysis_id = r.analysis_id
        WHERE r.analysis_id = %s
          AND {scope.file_clause("sf")}
          AND {scope.file_clause("tf")}
          AND (%s::text[] IS NULL OR r.relationship_type = ANY(%s::text[]))
        ORDER BY s.stable_key, r.relationship_type, t.stable_key
        """,
        str(analysis_id), types, types,
    )
    columns = ("source_key", "relationship_type", "target_key", "certainty", "resolution_basis")
    return [dict(zip(columns, row)) for row in rows]
