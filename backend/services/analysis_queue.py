"""PostgreSQL-backed queue operations for analyses.

``core.analysis`` rows in ``QUEUED`` status are the queue. Claiming one moves
it to ``INGESTING`` and inserts the ``RUNNING`` attempt that owns the work.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

ATTEMPT_OUTCOMES = {"SUCCEEDED", "DEGRADED", "FAILED", "CANCELLED"}


class AnalysisQueueError(RuntimeError):
    """Raised when an analysis cannot be claimed safely."""


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
    return UUID(str(analysis_id)), UUID(str(attempt_id))


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
