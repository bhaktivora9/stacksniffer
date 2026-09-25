"""Run the analysis flow against the real schema inside a rolled-back transaction.

Skipped unless DATABASE_URL points at a database with the core schema applied.
Nothing is committed: the connection's commit/close are no-ops and the
transaction is rolled back at the end.
"""

import asyncio
import os
from uuid import uuid4

import pytest

from backend.routers.analyses import _get_analysis
from backend.services.analysis_lifecycle import AnalysisState, transition_analysis
from backend.services.analysis_persistence import create_or_reuse_analysis
from backend.services.analysis_queue import claim_queued_analysis
from backend.services.analysis_worker import complete_attempt

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL is not set")


class UncommittedConnection:
    """Shares one transaction across helpers that expect to commit and close."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def conn():
    from backend.services.postgres import make_connection_factory

    real = make_connection_factory()()
    try:
        yield UncommittedConnection(real)
    finally:
        real.rollback()
        real.close()


def _create(conn, repository_name):
    return create_or_reuse_analysis(
        conn,
        canonical_repository_key=f"github:test-owner/{repository_name}",
        owner_name="test-owner",
        repository_name=repository_name,
        clone_url=f"https://github.com/test-owner/{repository_name}.git",
        requested_reference="main",
        resolved_commit_sha="c" * 40,
        structural_pipeline_version="structural-test",
        default_branch="main",
        client_request_id="pytest",
    )


def test_create_is_idempotent_against_the_real_schema(conn):
    repository_name = f"repo-{uuid4().hex[:8]}"
    first = _create(conn, repository_name)
    second = _create(conn, repository_name)

    assert first.status == "QUEUED"
    assert first.attempt_id is None
    assert first.reused is False
    assert second.analysis_id == first.analysis_id
    assert second.reused is True


def test_claim_advance_and_complete_against_the_real_schema(conn):
    created = _create(conn, f"repo-{uuid4().hex[:8]}")
    claimed = claim_queued_analysis(conn)
    if claimed is None or claimed[0] != created.analysis_id:
        pytest.skip("another QUEUED analysis is ahead of the test row")
    analysis_id, attempt_id = claimed

    state = _get_analysis(conn, analysis_id)
    assert state.status == "INGESTING"
    assert state.attempt_id == attempt_id

    async def run_pipeline():
        await transition_analysis(conn, analysis_id, AnalysisState.INGESTING, AnalysisState.EXTRACTING)
        await transition_analysis(conn, analysis_id, AnalysisState.EXTRACTING, AnalysisState.INDEXING)
        await complete_attempt(lambda: conn, analysis_id=analysis_id, attempt_id=attempt_id, outcome="SUCCEEDED")

    asyncio.run(run_pipeline())

    state = _get_analysis(conn, analysis_id)
    assert state.status == "READY"
    assert state.completed_at is not None
    assert conn.fetch_scalar(
        "SELECT status FROM core.analysis_attempt WHERE id = %s", str(attempt_id)
    ) == "SUCCEEDED"


def test_worker_loop_fails_a_claimed_analysis_with_the_placeholder_pipeline(conn):
    from backend.services.analysis_pipeline import run_analysis_pipeline
    from backend.services.analysis_worker import run_worker_loop

    _create(conn, f"repo-{uuid4().hex[:8]}")
    stop = asyncio.Event()
    processed = []

    async def pipeline(analysis_id, attempt_id):
        processed.append((analysis_id, attempt_id))
        stop.set()
        return await run_analysis_pipeline(analysis_id, attempt_id)

    asyncio.run(run_worker_loop(lambda: conn, pipeline, stop_event=stop, poll_seconds=0))

    analysis_id, attempt_id = processed[0]
    state = _get_analysis(conn, analysis_id)
    assert state.status == "FAILED"
    assert state.failure_stage == "INGESTING"
    assert state.failure_code == "PipelineNotImplemented"
    assert state.completed_at is not None


def test_stale_running_attempt_is_recovered_and_requeued(conn):
    from backend.services.analysis_worker import recover_stale_attempts

    created = _create(conn, f"repo-{uuid4().hex[:8]}")
    conn.execute("UPDATE core.analysis SET status = 'EXTRACTING' WHERE id = %s", str(created.analysis_id))
    attempt_id = conn.fetch_scalar(
        """
        INSERT INTO core.analysis_attempt (analysis_id, attempt_number, status, started_at)
        VALUES (%s, 1, 'RUNNING', NOW() - INTERVAL '2 hours')
        RETURNING id
        """,
        str(created.analysis_id),
    )

    requeued = asyncio.run(recover_stale_attempts(lambda: conn, stale_after_seconds=3600, max_attempts=3))

    assert created.analysis_id in requeued
    assert _get_analysis(conn, created.analysis_id).status == "QUEUED"
    row = conn.fetch_one(
        "SELECT status, failure_stage, failure_code FROM core.analysis_attempt WHERE id = %s", str(attempt_id)
    )
    assert tuple(row) == ("FAILED", "EXTRACTING", "WORKER_LOST")
