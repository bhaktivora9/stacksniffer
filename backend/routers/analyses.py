"""Analysis creation API."""

from __future__ import annotations

import asyncio
import json
import os
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

router = APIRouter(prefix="/api/v1/analyses", tags=["analyses"])

_connection_factory: Callable[[], Any] | None = None


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


# Terminal from the client's point of view: the worker has finished with it.
_STREAM_END_STATES = {"READY", "DEGRADED", "FAILED"}

_STATE_MESSAGES = {
    "QUEUED": "Analysis queued",
    "INGESTING": "Downloading repository",
    "EXTRACTING": "Extracting structure",
    "INDEXING": "Indexing content",
    "PROJECTING": "Building graph projection",
    "SUMMARIZING": "Summarizing",
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
               aa.id AS attempt_id, aa.failure_stage, aa.failure_code
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
    )


def _state_event(state: AnalysisStateResponse, sequence: int) -> dict[str, Any]:
    message = _STATE_MESSAGES.get(state.status, state.status)
    if state.status == "FAILED" and state.failure_stage:
        message = f"Analysis failed during {state.failure_stage.lower()}"
    return {
        "analysis_id": str(state.analysis_id),
        "attempt_id": str(state.attempt_id) if state.attempt_id else None,
        "sequence": sequence,
        "state": state.status,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def configure_connection_factory(factory: Callable[[], Any]) -> None:
    """Configure the PostgreSQL connection factory during application startup."""
    global _connection_factory
    _connection_factory = factory


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

    requested_reference = resolved["reference"]
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
                clone_url=f"{resolved['url']}.git",
                requested_reference=requested_reference,
                resolved_commit_sha=resolved["commit"],
                structural_pipeline_version=structural_version,
                # The resolver falls back to the default branch when no reference is given.
                default_branch=requested_reference if request.reference is None else None,
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
