"""Transactional persistence for immutable analysis identities."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class AnalysisCreation:
    analysis_id: UUID
    attempt_id: UUID | None
    canonical_repository_key: str
    requested_reference: str
    resolved_commit_sha: str
    structural_pipeline_version: str
    status: str
    reused: bool


class AnalysisPersistenceError(RuntimeError):
    """Raised when an analysis cannot be created transactionally."""


def _scalar(conn: Any, query: str, *args: Any) -> Any:
    value = conn.fetch_scalar(query, *args)
    if value is None:
        raise AnalysisPersistenceError("database did not return the requested identity")
    return value


def _column(row: Any, key: str, index: int) -> Any:
    return row[index] if isinstance(row, (tuple, list)) else row[key]


def create_or_reuse_analysis(
    conn: Any,
    *,
    canonical_repository_key: str,
    owner_name: str,
    repository_name: str,
    clone_url: str,
    requested_reference: str,
    resolved_commit_sha: str,
    structural_pipeline_version: str,
    default_branch: str | None = None,
    client_request_id: str | None = None,
) -> AnalysisCreation:
    """Create repository identity, version, and a QUEUED analysis in order.

    The caller owns the transaction. Attempts are not created here: the worker
    inserts a RUNNING attempt when it claims a QUEUED analysis.
    """
    repository_id = _scalar(
        conn,
        """
        INSERT INTO core.repository (
            canonical_key, provider, owner_name, repository_name, clone_url, default_branch
        )
        VALUES (%s, 'github', %s, %s, %s, %s)
        ON CONFLICT (canonical_key) DO UPDATE
        SET default_branch = COALESCE(EXCLUDED.default_branch, core.repository.default_branch),
            updated_at = NOW()
        RETURNING id
        """,
        canonical_repository_key,
        owner_name,
        repository_name,
        clone_url,
        default_branch,
    )

    # The no-op update makes RETURNING yield the existing row on conflict.
    repository_version_id = _scalar(
        conn,
        """
        INSERT INTO core.repository_version (repository_id, commit_sha, requested_ref)
        VALUES (%s, %s, %s)
        ON CONFLICT (repository_id, commit_sha) DO UPDATE
        SET requested_ref = COALESCE(core.repository_version.requested_ref, EXCLUDED.requested_ref)
        RETURNING id
        """,
        repository_id,
        resolved_commit_sha,
        requested_reference,
    )

    metadata = json.dumps({"client_request_id": client_request_id} if client_request_id else {})
    analysis = conn.fetch_one(
        """
        INSERT INTO core.analysis (repository_version_id, structural_pipeline_version, status, metadata)
        VALUES (%s, %s, 'QUEUED', %s::jsonb)
        ON CONFLICT (repository_version_id, structural_pipeline_version) DO NOTHING
        RETURNING id, status
        """,
        repository_version_id,
        structural_pipeline_version,
        metadata,
    )
    reused = analysis is None
    if reused:
        analysis = conn.fetch_one(
            """
            SELECT id, status
            FROM core.analysis
            WHERE repository_version_id = %s AND structural_pipeline_version = %s
            """,
            repository_version_id,
            structural_pipeline_version,
        )
    if analysis is None:
        raise AnalysisPersistenceError("database did not return analysis identity")
    analysis_id = _column(analysis, "id", 0)

    attempt_id = conn.fetch_scalar(
        """
        SELECT id
        FROM core.analysis_attempt
        WHERE analysis_id = %s
        ORDER BY attempt_number DESC
        LIMIT 1
        """,
        analysis_id,
    )

    return AnalysisCreation(
        analysis_id=UUID(str(analysis_id)),
        attempt_id=UUID(str(attempt_id)) if attempt_id else None,
        canonical_repository_key=canonical_repository_key,
        requested_reference=requested_reference,
        resolved_commit_sha=resolved_commit_sha,
        structural_pipeline_version=structural_pipeline_version,
        status=str(_column(analysis, "status", 1)),
        reused=reused,
    )


def run_in_transaction(connection_factory: Callable[[], Any], operation: Callable[[Any], Any]) -> Any:
    """Run an operation and commit before returning; rollback on failure."""
    conn = connection_factory()
    try:
        result = operation(conn)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
