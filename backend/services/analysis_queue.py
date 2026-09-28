"""PostgreSQL-backed queue operations for analyses.

``core.analysis`` rows in ``QUEUED`` status are the queue. Claiming one moves
it to ``INGESTING`` and inserts the ``RUNNING`` attempt that owns the work.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any
from uuid import UUID

ATTEMPT_OUTCOMES = {"SUCCEEDED", "DEGRADED", "FAILED", "CANCELLED"}


class AnalysisQueueError(RuntimeError):
    """Raised when an analysis cannot be claimed safely."""


class AttemptOwnershipLost(RuntimeError):
    """The attempt was closed elsewhere (e.g. stale recovery); its worker must stop without finalizing."""


def claim_queued_analysis(conn: Any) -> tuple[UUID, UUID] | None:
    """Claim the oldest QUEUED analysis and return ``(analysis_id, attempt_id)``.

    The caller owns the transaction and must commit the claim before doing work.
    ``SKIP LOCKED`` lets multiple workers consume the queue concurrently.
    """
    row = conn.fetch_one(
        """
        SELECT id
        FROM core.analysis
        WHERE status = 'QUEUED'
        ORDER BY created_at
        LIMIT 1
        FOR UPDATE SKIP LOCKED
        """
    )
    if row is None:
        return None

    analysis_id = row[0] if isinstance(row, (tuple, list)) else row["id"]
    result = conn.execute(
        """
        UPDATE core.analysis
        SET status = 'INGESTING'
        WHERE id = %s AND status = 'QUEUED'
        """,
        analysis_id,
    )
    if getattr(result, "rowcount", result) != 1:
        raise AnalysisQueueError("queued analysis was claimed by another worker")

    return UUID(str(analysis_id)), _insert_attempt(conn, analysis_id)


def _insert_attempt(conn: Any, analysis_id: Any) -> UUID:
    attempt_id = conn.fetch_scalar(
        """
        INSERT INTO core.analysis_attempt (analysis_id, attempt_number, status)
        SELECT %s, COALESCE(MAX(attempt_number), 0) + 1, 'RUNNING'
        FROM core.analysis_attempt
        WHERE analysis_id = %s
        RETURNING id
        """,
        analysis_id,
        analysis_id,
    )
    if attempt_id is None:
        raise AnalysisQueueError("database did not return attempt identity")
    return UUID(str(attempt_id))


def claim_resumable_analysis(conn: Any, stages: Iterable[str]) -> tuple[UUID, UUID] | None:
    """Claim the oldest AWAITING_STAGE analysis whose awaited stage is in ``stages``.

    It moves straight to that stage under a new RUNNING attempt; the stages it already
    completed are not repeated. With no runnable stages nothing is claimed, so parked
    analyses stay parked until a pipeline that can continue them is deployed.
    """
    stages = sorted(set(stages))
    if not stages:
        return None
    row = conn.fetch_one(
        """
        SELECT id, awaiting_stage
        FROM core.analysis
        WHERE status = 'AWAITING_STAGE' AND awaiting_stage = ANY(%s)
        ORDER BY created_at
        LIMIT 1
        FOR UPDATE SKIP LOCKED
        """,
        stages,
    )
    if row is None:
        return None
    analysis_id, awaiting = (row[0], row[1]) if isinstance(row, (tuple, list)) else (row["id"], row["awaiting_stage"])
    result = conn.execute(
        "UPDATE core.analysis SET status = %s WHERE id = %s AND status = 'AWAITING_STAGE'",
        awaiting,
        analysis_id,
    )
    if getattr(result, "rowcount", result) != 1:
        raise AnalysisQueueError("awaiting analysis was claimed by another worker")
    return UUID(str(analysis_id)), _insert_attempt(conn, analysis_id)


def attempt_is_owned(conn: Any, analysis_id: UUID, attempt_id: UUID) -> bool:
    """Return whether this attempt is still RUNNING and still drives an in-progress analysis."""
    owned = conn.fetch_scalar(
        """
        SELECT 1
        FROM core.analysis_attempt AS aa
        JOIN core.analysis AS a ON a.id = aa.analysis_id
        WHERE aa.id = %s AND a.id = %s
          AND aa.status = 'RUNNING'
          AND a.status IN ('INGESTING', 'EXTRACTING', 'INDEXING', 'PROJECTING', 'SUMMARIZING')
        """,
        str(attempt_id),
        str(analysis_id),
    )
    return owned is not None


def finish_attempt(
    conn: Any,
    attempt_id: UUID,
    status: str,
    *,
    failure_stage: str | None = None,
    failure_code: str | None = None,
    failure_detail: dict[str, Any] | None = None,
) -> None:
    """Persist a terminal attempt status without changing previous attempts."""
    if status not in ATTEMPT_OUTCOMES:
        raise ValueError(f"invalid terminal attempt status: {status}")
    conn.execute(
        """
        UPDATE core.analysis_attempt
        SET status = %s, completed_at = NOW(),
            failure_stage = %s, failure_code = %s, failure_detail = %s::jsonb
        WHERE id = %s AND status = 'RUNNING'
        """,
        status,
        failure_stage,
        failure_code,
        json.dumps(failure_detail) if failure_detail is not None else None,
        str(attempt_id),
    )
