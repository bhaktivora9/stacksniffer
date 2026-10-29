"""Analysis API: create and follow analyses, search them, and ask grounded questions about them."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from backend.services.repository_resolver import (
    canonicalize_repository_url,
    parse_github_repository_url,
)

try:
    from services.analysis_persistence import (
        AnalysisCreation,
        create_or_reuse_analysis,
        run_in_transaction,
    )
    from services.repository_resolver import (
        RepositoryResolutionError,
        resolve_repository_reference,
    )
    from services.graphrag.ask import AskError, ask
    from services.graphrag.llm import GenerationError
    from services.semantic.embeddings import EmbeddingError
    from services.semantic.search import MAX_QUERY_CHARS, MAX_RESULTS, SearchError, search_analysis
except ModuleNotFoundError:  # Package imports from the repository root.
    from backend.services.analysis_persistence import (
        AnalysisCreation,
        create_or_reuse_analysis,
        run_in_transaction,
    )
    from backend.services.repository_resolver import (
        RepositoryResolutionError,
        resolve_repository_reference,
    )
    from backend.services.graphrag.ask import AskError, ask
    from backend.services.graphrag.llm import GenerationError
    from backend.services.semantic.embeddings import EmbeddingError
    from backend.services.semantic.search import MAX_QUERY_CHARS, MAX_RESULTS, SearchError, search_analysis

router = APIRouter(prefix="/api/v1/analyses", tags=["analyses"])

_connection_factory: Callable[[], Any] | None = None
# (provider, chunk profile, embedding profile): the profiles the service indexes with.
_search_configuration: tuple[Any, Any, Any] | None = None
# The text provider for answers (the one that summarizes communities).
_text_provider: Any = None


class CreateAnalysisRequest(BaseModel):
    repository_url: str = Field(min_length=1)
    reference: str | None = Field(default=None, min_length=1)
    client_request_id: str | None = Field(default=None, min_length=1, max_length=200)


class CreateAnalysisResponse(BaseModel):
    analysis_id: UUID
    attempt_id: UUID | None
    canonical_repository_key: str
    requested_reference: str
    resolved_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    structural_pipeline_version: str
    status: str
    reused: bool
    events_url: str


class AnalysisStateResponse(BaseModel):
    analysis_id: UUID
    attempt_id: UUID | None
    status: str
    created_at: datetime
    completed_at: datetime | None = None
    failure_stage: str | None = None
    failure_code: str | None = None
    # The stage to resume at (set while AWAITING_STAGE, and kept if a resumed attempt fails).
    awaiting_stage: str | None = None
    last_completed_stage: str | None = None


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    k: int = Field(default=10, ge=1, le=MAX_RESULTS)
    language: str | None = Field(default=None, min_length=1)
    # Generated and vendored code is excluded unless asked for.
    include_third_party: bool = False


class SearchEvidence(BaseModel):
    file_path: str
    start_line: int
    end_line: int


class SearchHitResponse(BaseModel):
    rank: int
    score: float
    chunk_id: UUID
    stable_chunk_key: str
    chunk_kind: str
    content_hash: str
    content: str
    entity_stable_key: str | None
    entity_type: str | None
    entity_name: str | None
    evidence: SearchEvidence


class SearchResponse(BaseModel):
    analysis_id: UUID
    repository_version_id: UUID
    canonical_repository_key: str
    commit_sha: str
    retrieval_mode: str = "VECTOR_ONLY"
    chunk_profile: str
    embedding_profile: str
    distance_metric: str
    k: int
    first_party_only: bool
    language: str | None
    indexed_chunks: int
    embedded_chunks: int
    index_complete: bool
    results: list[SearchHitResponse]
    latency_ms: dict[str, float]


# Terminal from the client's point of view: the worker has finished with it.
# AWAITING_STAGE will not move until a new stage is deployed, so the stream ends there too.
_STREAM_END_STATES = {"READY", "DEGRADED", "FAILED", "AWAITING_STAGE"}

_STATE_MESSAGES = {
    "QUEUED": "Analysis queued",
    "INGESTING": "Downloading repository",
    "EXTRACTING": "Extracting structure",
    "INDEXING": "Indexing content",
    "PROJECTING": "Building graph projection",
    "SUMMARIZING": "Summarizing",
    "AWAITING_STAGE": "Waiting for the next analysis stage",
    "READY": "Analysis complete",
    "DEGRADED": "Analysis completed with degraded results",
    "FAILED": "Analysis failed",
}


def _row_value(row: Any, key: str, index: int) -> Any:
    return row[index] if isinstance(row, (tuple, list)) else row[key]


def _get_analysis(conn: Any, analysis_id: UUID) -> AnalysisStateResponse | None:
    row = conn.fetch_one(
        """
        SELECT a.id, a.status, a.created_at, a.completed_at,
               aa.id AS attempt_id, aa.failure_stage, aa.failure_code,
               a.awaiting_stage, a.last_completed_stage
        FROM core.analysis AS a
        LEFT JOIN LATERAL (
            SELECT id, failure_stage, failure_code
            FROM core.analysis_attempt
            WHERE analysis_id = a.id
            ORDER BY attempt_number DESC
            LIMIT 1
        ) AS aa ON TRUE
        WHERE a.id = %s
        """,
        str(analysis_id),
    )
    if row is None:
        return None
    attempt_id = _row_value(row, "attempt_id", 4)
    return AnalysisStateResponse(
        analysis_id=UUID(str(_row_value(row, "id", 0))),
        attempt_id=UUID(str(attempt_id)) if attempt_id else None,
        status=str(_row_value(row, "status", 1)),
        created_at=_row_value(row, "created_at", 2),
        completed_at=_row_value(row, "completed_at", 3),
        failure_stage=_row_value(row, "failure_stage", 5),
        failure_code=_row_value(row, "failure_code", 6),
        awaiting_stage=_row_value(row, "awaiting_stage", 7),
        last_completed_stage=_row_value(row, "last_completed_stage", 8),
    )


def _state_event(state: AnalysisStateResponse, sequence: int) -> dict[str, Any]:
    message = _STATE_MESSAGES.get(state.status, state.status)
    if state.status == "FAILED" and state.failure_stage:
        message = f"Analysis failed during {state.failure_stage.lower()}"
    if state.status == "AWAITING_STAGE" and state.awaiting_stage:
        done = (state.last_completed_stage or "").lower()
        message = f"Completed through {done}; waiting for the {state.awaiting_stage.lower()} stage"
    return {
        "analysis_id": str(state.analysis_id),
        "attempt_id": str(state.attempt_id) if state.attempt_id else None,
        "sequence": sequence,
        "state": state.status,
        "message": message,
        "awaiting_stage": state.awaiting_stage,
        "last_completed_stage": state.last_completed_stage,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def configure_connection_factory(factory: Callable[[], Any]) -> None:
    """Configure the PostgreSQL connection factory during application startup."""
    global _connection_factory
    _connection_factory = factory


def configure_search(provider: Any, chunk_profile: Any = None, embedding_profile: Any = None) -> None:
    """Configure query embedding with the provider and profiles the indexing stage uses; None disables search."""
    global _search_configuration
    _search_configuration = None if provider is None else (provider, chunk_profile, embedding_profile)


def configure_answers(provider: Any) -> None:
    """Configure the text provider that answers questions; None disables answers."""
    global _text_provider
    _text_provider = provider


def _connection_factory_or_raise() -> Callable[[], Any]:
    if _connection_factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="analysis persistence is not configured",
        )
    return _connection_factory


def _response(result: AnalysisCreation) -> CreateAnalysisResponse:
    return CreateAnalysisResponse(
        analysis_id=result.analysis_id,
        attempt_id=result.attempt_id,
        canonical_repository_key=result.canonical_repository_key,
        requested_reference=result.requested_reference,
        resolved_commit_sha=result.resolved_commit_sha,
        structural_pipeline_version=result.structural_pipeline_version,
        status=result.status,
        reused=result.reused,
        events_url=f"/api/v1/analyses/{result.analysis_id}/events",
    )


@router.post("", response_model=CreateAnalysisResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_analysis(request: CreateAnalysisRequest) -> CreateAnalysisResponse:
    try:
        canonical_key = canonicalize_repository_url(request.repository_url)
        owner_name, repository_name = parse_github_repository_url(request.repository_url)
        resolved = await resolve_repository_reference(
            request.repository_url,
            request.reference,
        )
    except RepositoryResolutionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    requested_reference = resolved.requested_reference
    structural_version = os.getenv("STRUCTURAL_PIPELINE_VERSION", "structural-v1")
    factory = _connection_factory_or_raise()

    try:
        result = await run_in_threadpool(
            run_in_transaction,
            factory,
            lambda conn: create_or_reuse_analysis(
                conn,
                canonical_repository_key=canonical_key,
                owner_name=owner_name,
                repository_name=repository_name,
                clone_url=resolved.clone_url,
                requested_reference=requested_reference,
                resolved_commit_sha=resolved.commit_sha,
                structural_pipeline_version=structural_version,
                default_branch=resolved.default_branch,
                client_request_id=request.client_request_id,
            ),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="unable to persist analysis request",
        ) from exc

    return _response(result)


@router.get("/{analysis_id}", response_model=AnalysisStateResponse)
async def get_analysis(analysis_id: UUID) -> AnalysisStateResponse:
    factory = _connection_factory_or_raise()

    def read(conn: Any) -> AnalysisStateResponse | None:
        return _get_analysis(conn, analysis_id)

    result = await run_in_threadpool(run_in_transaction, factory, read)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="analysis not found")
    return result


@router.post("/{analysis_id}/search", response_model=SearchResponse)
async def search(analysis_id: UUID, request: SearchRequest) -> SearchResponse:
    factory = _connection_factory_or_raise()
    if _search_configuration is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="search is not configured: no embedding provider",
        )
    provider, chunk_profile, embedding_profile = _search_configuration
    started = time.perf_counter()
    try:
        result = await run_in_threadpool(
            run_in_transaction,
            factory,
            lambda conn: search_analysis(
                conn, analysis_id, request.query, provider, chunk_profile, embedding_profile,
                k=request.k, first_party_only=not request.include_third_party, language=request.language,
            ),
        )
    except SearchError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except EmbeddingError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"query embedding failed: {exc.failure_code}",
        ) from exc
    total_ms = (time.perf_counter() - started) * 1000

    return SearchResponse(
        analysis_id=result.analysis_id,
        repository_version_id=result.repository_version_id,
        canonical_repository_key=result.canonical_repository_key,
        commit_sha=result.commit_sha,
        chunk_profile=result.chunk_profile,
        embedding_profile=result.embedding_profile,
        distance_metric=result.distance_metric,
        k=result.k,
        first_party_only=result.first_party_only,
        language=result.language,
        indexed_chunks=result.indexed_chunks,
        embedded_chunks=result.embedded_chunks,
        index_complete=result.index_complete,
        results=[
            SearchHitResponse(
                rank=hit.rank,
                score=hit.chunk.score,
                chunk_id=hit.chunk.chunk_id,
                stable_chunk_key=hit.chunk.stable_chunk_key,
                chunk_kind=hit.chunk_kind,
                content_hash=hit.chunk.content_hash,
                content=hit.content,
                entity_stable_key=hit.chunk.entity_stable_key,
                entity_type=hit.entity_type,
                entity_name=hit.entity_name,
                evidence=SearchEvidence(
                    file_path=hit.chunk.file_path,
                    start_line=hit.chunk.start_line,
                    end_line=hit.chunk.end_line,
                ),
            )
            for hit in result.hits
        ],
        latency_ms={
            "embed": round(result.embed_ms, 2),
            "search": round(result.search_ms, 2),
            "total": round(total_ms, 2),
        },
    )


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    # Omitted: routed from the question (repository-wide wording goes GLOBAL).
    mode: str | None = Field(default=None, pattern="^(?i:local|global)$")


@router.post("/{analysis_id}/ask")
async def ask_question(analysis_id: UUID, request: AskRequest) -> dict[str, Any]:
    factory = _connection_factory_or_raise()
    if _search_configuration is None or _text_provider is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="answers are not configured: an embedding and a text provider are required",
        )
    embedder, chunk_profile, embedding_profile = _search_configuration

    def run(conn: Any):
        return ask(conn, analysis_id, request.question, embedder=embedder, chunk_profile=chunk_profile,
                   embedding_profile=embedding_profile, provider=_text_provider, mode=request.mode)

    try:
        answer = await run_in_threadpool(run_in_transaction, factory, run)
    except (AskError, SearchError) as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except (EmbeddingError, GenerationError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"answer generation failed: {exc.failure_code}") from exc
    return {
        "query_run_id": str(answer.query_run_id),
        "mode": answer.mode,
        "answer": answer.answer,
        "grounding_status": answer.grounding_status,
        "citations": answer.citations,
        "unsupported_labels": answer.unsupported_labels,
        "context": [{"label": item.label, "kind": item.kind, "reason": item.reason, "score": round(item.score, 4),
                     **item.display} for item in answer.context],
        "latency_ms": answer.latency_ms,
        "tokens": {"input": answer.input_tokens, "output": answer.output_tokens},
    }


def _overview(conn: Any, analysis_id: UUID) -> dict[str, Any] | None:
    state = _get_analysis(conn, analysis_id)
    if state is None:
        return None
    a = str(analysis_id)
    repository = conn.fetch_one(
        "SELECT r.canonical_key, r.owner_name, r.repository_name, v.commit_sha, v.requested_ref "
        "FROM core.analysis an JOIN core.repository_version v ON v.id = an.repository_version_id "
        "JOIN core.repository r ON r.id = v.repository_id WHERE an.id = %s", a)
    counts = conn.fetch_one(
        "SELECT (SELECT count(*) FROM core.source_file WHERE analysis_id = %s), "
        "(SELECT count(*) FROM core.entity WHERE analysis_id = %s AND entity_type <> 'EXTERNAL_SYMBOL'), "
        "(SELECT count(*) FROM core.repository_edge WHERE analysis_id = %s), "
        "(SELECT count(*) FROM semantic.chunk WHERE analysis_id = %s)", a, a, a, a)
    languages = conn.fetch_all(
        "SELECT language, count(*) FROM core.source_file WHERE analysis_id = %s AND language IS NOT NULL "
        "AND NOT (is_generated OR is_vendored) GROUP BY language ORDER BY count(*) DESC LIMIT 8", a)
    snapshot = conn.fetch_one(
        "SELECT id, node_count, edge_count, projection_version, activated_at FROM graphrag.graph_snapshot "
        "WHERE analysis_id = %s AND status = 'ACTIVE'", a)
    communities: list[dict[str, Any]] = []
    run_metrics = None
    if snapshot is not None:
        run = conn.fetch_one("SELECT id, parameters->'metrics' FROM graphrag.community_run WHERE graph_snapshot_id = %s "
                             "AND status = 'SUCCEEDED' ORDER BY completed_at DESC LIMIT 1", str(snapshot[0]))
        if run is not None:
            run_metrics = run[1]
            rows = conn.fetch_all(
                "SELECT c.id, c.community_key, c.member_count, c.metrics, s.status, s.structured_summary, "
                "(SELECT count(*) FROM graphrag.community k WHERE k.parent_community_id = c.id) "
                "FROM graphrag.community c LEFT JOIN LATERAL (SELECT status, structured_summary "
                "FROM graphrag.community_summary WHERE community_id = c.id ORDER BY completed_at DESC NULLS LAST "
                "LIMIT 1) s ON TRUE WHERE c.community_run_id = %s AND c.hierarchy_level = 0 "
                "ORDER BY c.member_count DESC, c.community_key", str(run[0]))
            for community_id, key, members, metrics, summary_status, summary, children in rows:
                summary = summary or {}
                references = summary.get("references", {})
                communities.append({
                    "id": str(community_id), "key": key, "member_count": members, "children": children,
                    "files": (metrics or {}).get("top_files", []), "summary_status": summary_status,
                    "title": summary.get("title"), "summary": summary.get("summary"),
                    "key_points": [{"text": point["text"], "references": [
                        {"label": label, **{k: references[label].get(k) for k in
                                            ("file", "start_line", "end_line", "stable_key", "title")}}
                        for label in point.get("references", []) if label in references]}
                        for point in summary.get("key_points", []) if point.get("supported")],
                })
    return {
        "analysis": json.loads(state.model_dump_json()),
        "repository": {"canonical_key": repository[0], "owner": repository[1], "name": repository[2],
                       "commit_sha": repository[3], "requested_reference": repository[4]},
        "counts": {"files": counts[0], "entities": counts[1], "relationships": counts[2], "chunks": counts[3]},
        "languages": [{"language": language, "files": files} for language, files in languages],
        "graph": None if snapshot is None else {
            "nodes": snapshot[1], "edges": snapshot[2], "projection_version": snapshot[3],
            "activated_at": snapshot[4].isoformat() if snapshot[4] else None, "communities": run_metrics},
        "communities": communities,
        "answers_available": _text_provider is not None and _search_configuration is not None,
    }


@router.get("/{analysis_id}/overview")
async def get_overview(analysis_id: UUID) -> dict[str, Any]:
    factory = _connection_factory_or_raise()
    result = await run_in_threadpool(run_in_transaction, factory, lambda conn: _overview(conn, analysis_id))
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="analysis not found")
    return result


@router.get("/{analysis_id}/events")
async def stream_analysis_events(analysis_id: UUID) -> EventSourceResponse:
    factory = _connection_factory_or_raise()

    # Unnamed events with JSON data, so the browser's EventSource.onmessage receives them.
    async def events():
        sequence = 0
        last_seen: tuple[str, UUID | None] | None = None
        while True:
            state = await run_in_threadpool(
                run_in_transaction,
                factory,
                lambda conn: _get_analysis(conn, analysis_id),
            )
            if state is None:
                yield {"data": json.dumps({"state": "FAILED", "sequence": sequence + 1, "message": "analysis not found"})}
                return
            if (state.status, state.attempt_id) != last_seen:
                last_seen = (state.status, state.attempt_id)
                sequence += 1
                yield {"data": json.dumps(_state_event(state, sequence))}
            if state.status in _STREAM_END_STATES:
                return
            await asyncio.sleep(1.0)

    return EventSourceResponse(events())
