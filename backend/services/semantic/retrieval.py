"""Vector retrieval over persisted embeddings, and relevance of retrieved chunks to judged targets.

The query repeats the HNSW index's expression and predicate (006): the halfvec(3072) cast, the
profile, and `is_first_party` for default retrieval, so generated and vendored chunks are
excluded by indexed filtering rather than after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence
from uuid import UUID

from .embeddings import vector_literal

# distance operator and the score it maps to (higher is better)
_OPERATORS = {
    "COSINE": ("<=>", lambda distance: 1.0 - distance),
    "L2": ("<->", lambda distance: -distance),
    "INNER_PRODUCT": ("<#>", lambda distance: -distance),  # pgvector returns the negative inner product
}


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: UUID
    stable_chunk_key: str
    content_hash: str
    file_path: str
    start_line: int
    end_line: int
    entity_stable_key: str | None
    score: float
    vector_score: float


@dataclass(frozen=True)
class Judgment:
    id: UUID | None
    relevance_grade: int
    entity_stable_key: str | None
    file_path: str | None
    start_line: int | None
    end_line: int | None


def search_query(distance_metric: str, *, first_party_only: bool = True, with_language: bool = False) -> str:
    """The retrieval SQL. Parameters: query vector, embedding profile, analysis, chunk profile,
    [language], k. ORDER BY is the bare index expression against a parameter so the HNSW index
    applies; ties are broken by the caller."""
    operator, _ = _OPERATORS[distance_metric]
    filters = (" AND e.is_first_party" if first_party_only else "") + (" AND c.language = %s" if with_language else "")
    return (
        "SELECT c.id, c.stable_chunk_key, c.content_hash, f.path, c.start_line, c.end_line, en.stable_key, "
        f"(e.embedding::halfvec(3072) {operator} %s::halfvec(3072)) AS distance "
        "FROM semantic.embedding e "
        "JOIN semantic.chunk c ON c.id = e.chunk_id AND c.content_hash = e.chunk_content_hash "
        "JOIN core.source_file f ON f.id = c.file_id "
        "LEFT JOIN core.entity en ON en.id = c.entity_id "
        f"WHERE e.embedding_profile_id = %s AND c.analysis_id = %s AND c.chunk_profile_id = %s{filters} "
        "ORDER BY distance LIMIT %s")


def vector_search(conn: Any, analysis_id: UUID, chunk_profile_id: UUID, embedding_profile_id: UUID,
                  distance_metric: str, query_vector: Sequence[float], k: int, *, first_party_only: bool = True,
                  language: str | None = None) -> list[RetrievedChunk]:
    _, to_score = _OPERATORS[distance_metric]
    arguments: list[Any] = [vector_literal(query_vector), str(embedding_profile_id), str(analysis_id),
                            str(chunk_profile_id)]
    if language is not None:
        arguments.append(language)
    rows = conn.fetch_all(search_query(distance_metric, first_party_only=first_party_only,
                                       with_language=language is not None), *arguments, k)
    found = [RetrievedChunk(row[0], row[1], row[2], row[3], row[4], row[5], row[6], to_score(row[7]),
                            to_score(row[7])) for row in rows]
    return sorted(found, key=lambda chunk: (-chunk.score, chunk.stable_chunk_key))


def matching_judgments(chunk: RetrievedChunk, judgments: Sequence[Judgment]) -> list[Judgment]:
    """Judgments a chunk satisfies: it belongs to the judged entity, or it overlaps the judged file range."""
    matched = []
    for judgment in judgments:
        by_entity = judgment.entity_stable_key is not None and chunk.entity_stable_key == judgment.entity_stable_key
        by_range = (judgment.file_path == chunk.file_path and judgment.start_line is not None
                    and chunk.start_line <= judgment.end_line and judgment.start_line <= chunk.end_line)
        if by_entity or by_range:
            matched.append(judgment)
    return matched
