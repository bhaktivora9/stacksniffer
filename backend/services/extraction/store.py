"""PostgreSQL persistence for canonical extraction output.

Each batch is one transaction, written in dependency order: source files,
evidence, entities, parents, entity evidence, edges, edge evidence. All writes
are upserts keyed on the schema's natural keys, so repeating an extraction
creates no duplicates.

Every upsert returns the ids of the rows it wrote together with their natural
keys; later statements in the batch refer to those ids directly. Row estimates
are stale for an analysis whose rows were inserted moments earlier in the same
transaction, and any join the planner may reorder went quadratic at repository
scale (natural-key joins stalled for 10+ minutes on pandas). So no statement
joins on natural keys, and the link-table re-checks are primary-key probes
fenced with OFFSET 0: each is planned on its own, so it can only use the primary
key (not the analysis_id index the stale estimates favour), and the analysis is
checked on the one row it returns.

Ids are only ever taken from this analysis's own upserts (or, for a key written
by an earlier batch, looked up with ``analysis_id``), so references cannot cross
analyses. The entity and edge tables also enforce this with composite foreign
keys; the link-table inserts re-check ``analysis_id`` through primary-key joins,
because ``entity_evidence`` and ``edge_evidence`` have none. A reference that does
not resolve is reported with its file and stable key before anything is written.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any
from uuid import UUID

from .contracts import ExtractionResult, FileExtraction


class ExtractionPersistenceError(RuntimeError):
    """A batch could not be written. ``context`` identifies the input, never its source contents.

    Keys: analysis_id, attempt_id, batch, table, files (the batch's paths), and either
    ``unresolved`` (rows whose references did not resolve, with what is missing) or
    ``sqlstate`` / ``constraint`` for other database errors. It is persisted as the
    attempt's failure detail.
    """

    failure_code = "EXTRACTION_PERSISTENCE_FAILED"
    MAX_REPORTED_ROWS = 10

    def __init__(self, message: str, context: dict[str, Any] | None = None):
        self.summary = message
        self.context = dict(context or {})
        super().__init__(self._render())

    def _render(self) -> str:
        where = ", ".join(f"{k}={self.context[k]}" for k in ("analysis_id", "attempt_id", "batch", "table")
                          if self.context.get(k) is not None)
        rows = self.context.get("unresolved") or []
        shown = "; ".join(
            " ".join(f"{k}={v}" for k, v in row.items() if v not in (None, [], "")) for row in rows[:3]
        )
        more = f" (+{len(rows) - 3} more)" if len(rows) > 3 else ""
        return f"{self.summary} [{where}]" + (f": {shown}{more}" if shown else "")

    def with_context(self, **context: Any) -> "ExtractionPersistenceError":
        merged = {**self.context, **{k: v for k, v in context.items() if v is not None}}
        return ExtractionPersistenceError(self.summary, merged)

    @property
    def failure_detail(self) -> dict[str, Any]:
        return {"message": str(self), **self.context}


def build_batch_payload(files: Sequence[FileExtraction]) -> dict[str, list[dict[str, Any]]]:
    """Flatten a batch into deduplicated rows keyed by natural keys (no database ids)."""
    source_files, evidence, entities, parents, entity_links, edges, edge_links = {}, {}, {}, {}, {}, {}, {}
    for file in files:
        source_files[file.path] = {
            "path": file.path,
            "language": file.language,
            "content_hash": file.content_hash,
            "size_bytes": file.size_bytes,
            "parse_status": file.parse_status.value,
            "is_generated": file.is_generated,
            "is_vendored": file.is_vendored,
            "metadata": {
                **file.metadata,
                "analyzer": file.analyzer,
                "extractor_version": file.extractor_version,
                "line_count": file.line_count,
                "capabilities": file.capabilities.as_dict(),
                "errors": [error.as_dict() for error in file.errors],
            },
        }
        for item in file.evidence:
            evidence.setdefault(item.key, {
                "path": item.path,
                "start_line": item.start_line,
                "end_line": item.end_line,
                "start_byte": item.start_byte,
                "end_byte": item.end_byte,
                "content_hash": item.content_hash,
                "extractor": item.extractor,
                "extractor_version": item.extractor_version,
                "certainty": item.certainty.value,
                "metadata": {**item.metadata, "commit_sha": item.commit_sha},
            })
        for entity in file.entities:
            entities.setdefault(entity.stable_key, {
                "stable_key": entity.stable_key,
                "entity_type": entity.entity_type.value,
                "name": entity.name,
                "qualified_name": entity.qualified_name,
                "path": entity.path,
                "start_line": entity.start_line,
                "end_line": entity.end_line,
                "extractor_version": entity.extractor_version,
                "metadata": {**entity.metadata, "extractor": entity.extractor},
            })
            if entity.parent_key:
                parents[entity.stable_key] = {"stable_key": entity.stable_key, "parent_key": entity.parent_key}
            for key, role in entity.evidence:
                link = _evidence_ref(key) | {"stable_key": entity.stable_key, "role": role.value}
                entity_links[(entity.stable_key, key, role.value)] = link
        for relationship in file.relationships:
            edges.setdefault(relationship.identity, {
                "source_key": relationship.source_key,
                "relationship_type": relationship.relationship_type.value,
                "target_key": relationship.target_key,
                "certainty": relationship.certainty.value,
                "confidence": relationship.confidence,
                "extractor": relationship.extractor,
                "extractor_version": relationship.extractor_version,
                "metadata": dict(relationship.metadata),
            })
            for key in relationship.evidence:
                edge_links[(relationship.identity, key)] = _evidence_ref(key) | {
                    "source_key": relationship.source_key,
                    "relationship_type": relationship.relationship_type.value,
                    "target_key": relationship.target_key,
                    "edge_version": relationship.extractor_version,
                }
    return {
        "source_files": list(source_files.values()),
        "evidence": list(evidence.values()),
        "entities": list(entities.values()),
        "parents": list(parents.values()),
        "entity_links": list(entity_links.values()),
        "edges": list(edges.values()),
        "edge_links": list(edge_links.values()),
    }


def _evidence_ref(key: tuple) -> dict[str, Any]:
    path, start_line, end_line, content_hash, extractor_version = key
    return {"path": path, "start_line": start_line, "end_line": end_line,
            "content_hash": content_hash, "evidence_version": extractor_version}


STATEMENTS = {
    "source_files": """
        INSERT INTO core.source_file (
            analysis_id, path, language, content_hash, size_bytes, parse_status, is_generated, is_vendored, metadata)
        SELECT %(analysis_id)s, x.path, x.language, x.content_hash, x.size_bytes, x.parse_status,
               x.is_generated, x.is_vendored, x.metadata
        FROM jsonb_to_recordset(%(rows)s::jsonb) AS x(
            path text, language text, content_hash text, size_bytes bigint, parse_status text,
            is_generated boolean, is_vendored boolean, metadata jsonb)
        ON CONFLICT (analysis_id, path) DO UPDATE
        SET language = EXCLUDED.language, content_hash = EXCLUDED.content_hash, size_bytes = EXCLUDED.size_bytes,
            parse_status = EXCLUDED.parse_status, is_generated = EXCLUDED.is_generated,
            is_vendored = EXCLUDED.is_vendored, metadata = EXCLUDED.metadata
        RETURNING id, path
    """,
    "evidence": """
        INSERT INTO core.evidence (
            analysis_id, file_id, start_line, end_line, start_byte, end_byte,
            content_hash, extractor, extractor_version, certainty, metadata)
        SELECT %(analysis_id)s, x.file_id, x.start_line, x.end_line, x.start_byte, x.end_byte,
               x.content_hash, x.extractor, x.extractor_version, x.certainty, x.metadata
        FROM jsonb_to_recordset(%(rows)s::jsonb) AS x(
            file_id uuid, start_line int, end_line int, start_byte int, end_byte int, content_hash text,
            extractor text, extractor_version text, certainty text, metadata jsonb)
        ON CONFLICT ON CONSTRAINT evidence_location_uq DO UPDATE SET metadata = EXCLUDED.metadata
        RETURNING id, file_id, start_line, end_line, content_hash, extractor_version
    """,
    "entities": """
        INSERT INTO core.entity (
            analysis_id, file_id, entity_type, name, qualified_name, stable_key,
            start_line, end_line, extractor_version, metadata)
        SELECT %(analysis_id)s, x.file_id, x.entity_type, x.name, x.qualified_name, x.stable_key,
               x.start_line, x.end_line, x.extractor_version, x.metadata
        FROM jsonb_to_recordset(%(rows)s::jsonb) AS x(
            stable_key text, entity_type text, name text, qualified_name text, file_id uuid,
            start_line int, end_line int, extractor_version text, metadata jsonb)
        ON CONFLICT (analysis_id, stable_key) DO UPDATE
        SET entity_type = EXCLUDED.entity_type, name = EXCLUDED.name, qualified_name = EXCLUDED.qualified_name,
            file_id = EXCLUDED.file_id, start_line = EXCLUDED.start_line, end_line = EXCLUDED.end_line,
            extractor_version = EXCLUDED.extractor_version, metadata = EXCLUDED.metadata
        RETURNING id, stable_key
    """,
    "parents": """
        UPDATE core.entity AS e
        SET parent_entity_id = x.parent_id
        FROM jsonb_to_recordset(%(rows)s::jsonb) AS x(id uuid, parent_id uuid)
        WHERE e.id = x.id AND e.analysis_id = %(analysis_id)s
        RETURNING e.id
    """,
    "entity_links": """
        INSERT INTO core.entity_evidence (entity_id, evidence_id, evidence_role)
        SELECT e.id, ev.id, x.role
        FROM jsonb_to_recordset(%(rows)s::jsonb) AS x(entity_id uuid, evidence_id uuid, role text)
        CROSS JOIN LATERAL (SELECT id, analysis_id FROM core.entity WHERE id = x.entity_id OFFSET 0) AS e
        CROSS JOIN LATERAL (SELECT id, analysis_id FROM core.evidence WHERE id = x.evidence_id OFFSET 0) AS ev
        WHERE e.analysis_id = %(analysis_id)s AND ev.analysis_id = %(analysis_id)s
        ON CONFLICT (entity_id, evidence_id, evidence_role) DO UPDATE SET evidence_role = EXCLUDED.evidence_role
        RETURNING entity_id
    """,
    "edges": """
        INSERT INTO core.repository_edge (
            analysis_id, source_entity_id, target_entity_id, relationship_type,
            certainty, confidence, extractor, extractor_version, metadata)
        SELECT %(analysis_id)s, x.source_id, x.target_id, x.relationship_type,
               x.certainty, x.confidence, x.extractor, x.extractor_version, x.metadata
        FROM jsonb_to_recordset(%(rows)s::jsonb) AS x(
            source_id uuid, target_id uuid, relationship_type text, certainty text, confidence numeric,
            extractor text, extractor_version text, metadata jsonb)
        ON CONFLICT ON CONSTRAINT repository_edge_identity_uq DO UPDATE
        SET certainty = EXCLUDED.certainty, confidence = EXCLUDED.confidence, metadata = EXCLUDED.metadata
        RETURNING id, source_entity_id, relationship_type, target_entity_id, extractor_version
    """,
    "edge_links": """
        INSERT INTO core.edge_evidence (edge_id, evidence_id)
        SELECT ed.id, ev.id
        FROM jsonb_to_recordset(%(rows)s::jsonb) AS x(edge_id uuid, evidence_id uuid)
        CROSS JOIN LATERAL (SELECT id, analysis_id FROM core.repository_edge WHERE id = x.edge_id OFFSET 0) AS ed
        CROSS JOIN LATERAL (SELECT id, analysis_id FROM core.evidence WHERE id = x.evidence_id OFFSET 0) AS ev
        WHERE ed.analysis_id = %(analysis_id)s AND ev.analysis_id = %(analysis_id)s
        ON CONFLICT (edge_id, evidence_id) DO UPDATE SET evidence_id = EXCLUDED.evidence_id
        RETURNING edge_id
    """,
}
# Evidence must exist before anything links to it; entities before parents and edges.
WRITE_ORDER = ("source_files", "evidence", "entities", "parents", "entity_links", "edges", "edge_links")


def _key_file(key: str | None) -> str | None:
    """The file a stable key belongs to (``<type>:<path>::<name>`` or ``file:<path>``); None for externals."""
    if not key or key.startswith("external:"):
        return None
    return key.split(":", 1)[1].split("::", 1)[0]


def _evidence_label(row: dict[str, Any]) -> str:
    return f"{row['path']}:{row['start_line']}-{row['end_line']}"


def _evidence_key(row: dict[str, Any]) -> tuple:
    version = row.get("evidence_version") or row.get("extractor_version")
    return (row["path"], row["start_line"], row["end_line"], row["content_hash"], version)


class _BatchIds:
    """Natural key -> id for everything this batch wrote, with a lookup for keys written earlier."""

    def __init__(self, conn: Any, analysis_id: UUID):
        self.conn, self.analysis_id = conn, str(analysis_id)
        self.files: dict[str, str] = {}
        self.evidence: dict[tuple, str] = {}
        self.entities: dict[str, str] = {}
        self.edges: dict[tuple, str] = {}

    def load_entities(self, keys: set[str]) -> None:
        """Entities referenced by this batch but written by an earlier one, scoped to the analysis."""
        missing = sorted(k for k in keys if k and k not in self.entities)
        if missing:
            for entity_id, key in self.conn.fetch_all(
                    "SELECT id, stable_key FROM core.entity WHERE analysis_id = %s AND stable_key = ANY(%s)",
                    self.analysis_id, missing):
                self.entities[key] = str(entity_id)


def _unresolved(row: dict[str, Any], missing: list[str]) -> dict[str, Any]:
    key = row.get("stable_key") or row.get("source_key")
    return {
        "file": row.get("path") or _key_file(key),
        "stable_key": key,
        "relationship_type": row.get("relationship_type"),
        "target_key": row.get("target_key"),
        "evidence": _evidence_label(row) if "start_line" in row and "path" in row else None,
        "missing": missing,
    }


def _raise_unresolved(analysis_id: UUID, table: str, rows: list[dict[str, Any]], unresolved: list[dict[str, Any]]):
    raise ExtractionPersistenceError(
        f"{table}: {len(unresolved)} of {len(rows)} rows reference something that did not resolve within the analysis",
        {"analysis_id": str(analysis_id), "table": table, "expected_rows": len(rows),
         "unresolved": unresolved[:ExtractionPersistenceError.MAX_REPORTED_ROWS],
         "unresolved_count": len(unresolved)},
    )


def _id_rows(table: str, rows: list[dict[str, Any]], ids: _BatchIds, analysis_id: UUID) -> list[dict[str, Any]]:
    """Translate natural-key rows into id rows; any reference that does not resolve is reported, not dropped."""
    out, unresolved = [], []
    for row in rows:
        missing: list[str] = []
        if table == "evidence":
            file_id = ids.files.get(row["path"])
            if file_id is None:
                missing.append(f"source_file:{row['path']}")
            out.append({**{k: v for k, v in row.items() if k != "path"}, "file_id": file_id})
        elif table == "entities":
            file_id = ids.files.get(row["path"]) if row.get("path") else None
            if row.get("path") and file_id is None:
                missing.append(f"source_file:{row['path']}")
            out.append({**{k: v for k, v in row.items() if k != "path"}, "file_id": file_id})
        elif table == "parents":
            entity_id, parent_id = ids.entities.get(row["stable_key"]), ids.entities.get(row["parent_key"])
            missing += [f"entity:{k}" for k, v in ((row["stable_key"], entity_id), (row["parent_key"], parent_id))
                        if v is None]
            out.append({"id": entity_id, "parent_id": parent_id})
        elif table == "entity_links":
            entity_id, evidence_id = ids.entities.get(row["stable_key"]), ids.evidence.get(_evidence_key(row))
            if entity_id is None:
                missing.append(f"entity:{row['stable_key']}")
            if evidence_id is None:
                missing.append(f"evidence:{_evidence_label(row)}")
            out.append({"entity_id": entity_id, "evidence_id": evidence_id, "role": row["role"]})
        elif table == "edges":
            source_id, target_id = ids.entities.get(row["source_key"]), ids.entities.get(row["target_key"])
            missing += [f"entity:{k}" for k, v in ((row["source_key"], source_id), (row["target_key"], target_id))
                        if v is None]
            out.append({**{k: v for k, v in row.items() if k not in ("source_key", "target_key")},
                        "source_id": source_id, "target_id": target_id})
        elif table == "edge_links":
            edge = ids.edges.get((row["source_key"], row["relationship_type"], row["target_key"], row["edge_version"]))
            evidence_id = ids.evidence.get(_evidence_key(row))
            if edge is None:
                missing.append(f"edge:{row['relationship_type']}")
            if evidence_id is None:
                missing.append(f"evidence:{_evidence_label(row)}")
            out.append({"edge_id": edge, "evidence_id": evidence_id})
        else:
            out.append(row)
        if missing:
            unresolved.append(_unresolved(row, missing))
    if unresolved:
        _raise_unresolved(analysis_id, table, rows, unresolved)
    return out


def write_batch(conn: Any, analysis_id: UUID, payload: dict[str, list[dict[str, Any]]],
                progress: dict[str, Any] | None = None) -> dict[str, int]:
    """Write a payload inside the caller's transaction; returns rows written per table.

    ``progress["table"]`` names the statement being executed, for error reporting.
    """
    progress = progress if progress is not None else {}
    ids = _BatchIds(conn, analysis_id)
    written = {}
    for name in WRITE_ORDER:
        rows = payload[name]
        progress["table"] = name
        if not rows:
            written[name] = 0
            continue
        if name in ("parents", "edges"):
            ids.load_entities({row.get(k) for row in rows for k in ("stable_key", "parent_key", "source_key", "target_key")})
        id_rows = _id_rows(name, rows, ids, analysis_id)
        returned = conn.fetch_all_named(STATEMENTS[name], {"analysis_id": str(analysis_id), "rows": json.dumps(id_rows)})
        if len(returned) != len(rows):
            raise ExtractionPersistenceError(
                f"{name}: wrote {len(returned)} of {len(rows)} rows; a reference did not resolve within the analysis",
                {"analysis_id": str(analysis_id), "table": name, "expected_rows": len(rows),
                 "written_rows": len(returned)},
            )
        if name == "source_files":
            ids.files = {path: str(file_id) for file_id, path in returned}
        elif name == "evidence":
            paths = {file_id: path for path, file_id in ids.files.items()}
            ids.evidence = {(paths[str(f)], s_, e_, h, v): str(i) for i, f, s_, e_, h, v in returned}
        elif name == "entities":
            ids.entities.update({key: str(entity_id) for entity_id, key in returned})
        elif name == "edges":
            keys = {v: k for k, v in ids.entities.items()}
            ids.edges = {(keys[str(src)], rtype, keys[str(tgt)], version): str(edge_id)
                         for edge_id, src, rtype, tgt, version in returned}
        written[name] = len(returned)
    return written


def record_extraction_metrics(conn: Any, result: ExtractionResult) -> None:
    """Keep metrics per attempt, so a retry never overwrites an earlier attempt's evidence."""
    conn.execute(
        """
        UPDATE core.analysis
        SET metadata = metadata || jsonb_build_object(
            'extractions',
            COALESCE(metadata->'extractions', '{}'::jsonb) || jsonb_build_object(%s::text, %s::jsonb)
        )
        WHERE id = %s
        """,
        str(result.attempt_id),
        json.dumps(result.as_metrics()),
        str(result.analysis_id),
    )


class PostgresExtractionStore:
    """``ExtractionStore`` that commits each batch in its own transaction."""

    MAX_REPORTED_FILES = 20

    def __init__(self, connection_factory: Callable[[], Any], analysis_id: UUID, attempt_id: UUID | None = None):
        self._connection_factory = connection_factory
        self.analysis_id = analysis_id
        self.attempt_id = attempt_id
        self.batches = 0

    def persist(self, files: Sequence[FileExtraction]) -> None:
        self.batches += 1
        payload = build_batch_payload(files)
        progress: dict[str, Any] = {}
        context = {
            "analysis_id": str(self.analysis_id),
            "attempt_id": str(self.attempt_id) if self.attempt_id else None,
            "batch": self.batches,
            "files": [f.path for f in files][:self.MAX_REPORTED_FILES],
            "file_count": len(files),
        }
        conn = self._connection_factory()
        try:
            write_batch(conn, self.analysis_id, payload, progress)
            conn.commit()
        except ExtractionPersistenceError as exc:
            conn.rollback()
            raise exc.with_context(**context) from exc
        except Exception as exc:
            conn.rollback()
            # The driver's message and detail can echo row values; report only what identifies the failure.
            diag = getattr(exc, "diag", None)
            raise ExtractionPersistenceError(
                f"{progress.get('table', 'batch')}: {type(exc).__name__}",
                {**context, "table": progress.get("table"), "error_type": type(exc).__name__,
                 "sqlstate": getattr(exc, "sqlstate", None) or getattr(diag, "sqlstate", None),
                 "constraint": getattr(diag, "constraint_name", None)},
            ) from exc
        finally:
            conn.close()
