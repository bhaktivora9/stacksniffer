"""Vector-only retrieval over one analysis: the SS-ML-302 baseline behind the search API.

A search is scoped to exactly one analysis, so to one repository version, under the chunk and
embedding profiles the service indexes with. Nothing is registered or written: profiles are
looked up by fingerprint, and an analysis that has no vectors under them fails with a typed
error that says why (not indexed yet, or indexed under other profiles) instead of returning an
empty ranking that looks like "no matches".
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .embeddings import EmbeddingProvider, check_compatibility, validate_vector
from .profiles import ChunkProfile, EmbeddingProfile
from .retrieval import RetrievedChunk, vector_search

MAX_RESULTS = 50
MAX_QUERY_CHARS = 2000
# States in which the analysis cannot have a complete index yet.
_NOT_YET_INDEXED = ("QUEUED", "INGESTING", "EXTRACTING", "INDEXING")


class SearchError(RuntimeError):
    failure_code = "SEARCH_FAILED"
    status_code = 400


class InvalidSearch(SearchError):
    failure_code = "INVALID_SEARCH"


class AnalysisNotFound(SearchError):
    failure_code = "ANALYSIS_NOT_FOUND"
    status_code = 404


class IndexNotReady(SearchError):
    """The analysis has no searchable vectors under the service's profiles."""

    failure_code = "INDEX_NOT_READY"
    status_code = 409


@dataclass(frozen=True)
class SearchHit:
    rank: int
    chunk: RetrievedChunk
    chunk_kind: str
    content: str
    entity_type: str | None
    entity_name: str | None


@dataclass(frozen=True)
class SearchResult:
    analysis_id: UUID
    repository_version_id: UUID
    canonical_repository_key: str
    commit_sha: str
    chunk_profile: str
    embedding_profile: str
    distance_metric: str
    first_party_only: bool
    language: str | None
    k: int
    indexed_chunks: int
    embedded_chunks: int
    hits: list[SearchHit]
    embed_ms: float
    search_ms: float

    @property
    def index_complete(self) -> bool:
        return self.embedded_chunks == self.indexed_chunks


def search_analysis(conn: Any, analysis_id: UUID, query: str, provider: EmbeddingProvider,
                    chunk_profile: ChunkProfile, embedding_profile: EmbeddingProfile, *, k: int = 10,
                    first_party_only: bool = True, language: str | None = None) -> SearchResult:
    query = query.strip()
    if not query or len(query) > MAX_QUERY_CHARS:
        raise InvalidSearch(f"the query must be 1 to {MAX_QUERY_CHARS} characters")
    if not 1 <= k <= MAX_RESULTS:
        raise InvalidSearch(f"k must be between 1 and {MAX_RESULTS}")
    check_compatibility(provider, embedding_profile)

    analysis = conn.fetch_one(
        "SELECT a.status, a.awaiting_stage, a.repository_version_id, r.canonical_key, rv.commit_sha "
        "FROM core.analysis a JOIN core.repository_version rv ON rv.id = a.repository_version_id "
        "JOIN core.repository r ON r.id = rv.repository_id WHERE a.id = %s", str(analysis_id))
    if analysis is None:
        raise AnalysisNotFound(f"analysis {analysis_id} does not exist")
    status, awaiting, version_id, repository_key, commit_sha = analysis
    if status in _NOT_YET_INDEXED or (status == "AWAITING_STAGE" and awaiting == "INDEXING"):
        state = f"AWAITING_STAGE ({awaiting})" if status == "AWAITING_STAGE" else status
        raise IndexNotReady(f"analysis {analysis_id} is {state}; it has not been indexed yet")

    chunk_name = f"{chunk_profile.profile_key}/{chunk_profile.profile_version}"
    embedding_name = f"{embedding_profile.profile_key}/{embedding_profile.profile_version}"
    profile_ids = conn.fetch_one(
        "SELECT (SELECT id FROM semantic.chunk_profile WHERE configuration_fingerprint = %s), "
        "(SELECT id FROM semantic.embedding_profile WHERE configuration_fingerprint = %s)",
        chunk_profile.fingerprint, embedding_profile.fingerprint)
    chunk_profile_id, embedding_profile_id = profile_ids if profile_ids else (None, None)
    indexed, embedded = conn.fetch_one(
        "SELECT count(*), count(e.chunk_id) FROM semantic.chunk c "
        "LEFT JOIN semantic.embedding e ON e.chunk_id = c.id AND e.chunk_content_hash = c.content_hash "
        "AND e.embedding_profile_id = %s WHERE c.analysis_id = %s AND c.chunk_profile_id = %s",
        str(embedding_profile_id) if embedding_profile_id else None, str(analysis_id),
        str(chunk_profile_id) if chunk_profile_id else None)
    if not indexed:
        raise IndexNotReady(f"analysis {analysis_id} has no chunks under chunk profile {chunk_name}")
    if not embedded:
        raise IndexNotReady(f"analysis {analysis_id} has no vectors under embedding profile {embedding_name}")

    started = time.perf_counter()
    # The template frames documents; a query is embedded as written, under the query task.
    result = provider.embed([query], profile=embedding_profile, task=embedding_profile.query_task)[0]
    vector = validate_vector(result.values, embedding_profile)
    embedded_at = time.perf_counter()
    ranked = vector_search(conn, analysis_id, chunk_profile_id, embedding_profile_id,
                           embedding_profile.distance_metric, vector, k, first_party_only=first_party_only,
                           language=language)
    searched_at = time.perf_counter()

    return SearchResult(
        analysis_id=analysis_id, repository_version_id=UUID(str(version_id)),
        canonical_repository_key=repository_key, commit_sha=commit_sha, chunk_profile=chunk_name,
        embedding_profile=embedding_name, distance_metric=embedding_profile.distance_metric,
        first_party_only=first_party_only, language=language, k=k, indexed_chunks=indexed,
        embedded_chunks=embedded, hits=_hits(conn, ranked),
        embed_ms=(embedded_at - started) * 1000, search_ms=(searched_at - embedded_at) * 1000)


def _hits(conn: Any, ranked: list[RetrievedChunk]) -> list[SearchHit]:
    if not ranked:
        return []
    details = {row[0]: row[1:] for row in conn.fetch_all(
        "SELECT c.id, c.chunk_kind, c.content, en.entity_type, COALESCE(en.qualified_name, en.name) "
        "FROM semantic.chunk c LEFT JOIN core.entity en ON en.id = c.entity_id WHERE c.id = ANY(%s::uuid[])",
        [str(chunk.chunk_id) for chunk in ranked])}
    return [SearchHit(rank, chunk, *details[chunk.chunk_id]) for rank, chunk in enumerate(ranked, start=1)]
