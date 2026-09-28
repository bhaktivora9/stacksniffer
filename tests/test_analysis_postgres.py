"""Run the analysis flow against the real schema inside a rolled-back transaction.

Skipped unless TEST_DATABASE_URL points at a disposable database with the core schema
applied. DATABASE_URL is deliberately ignored: importing backend.main loads
backend/.env, which would otherwise point these tests at a real database.
Nothing is committed: the connection's commit/close are no-ops and the
transaction is rolled back at the end.
"""

import asyncio
import os
import shutil
import subprocess
from uuid import uuid4

import pytest

from backend.routers.analyses import _get_analysis
from backend.services.analysis_lifecycle import AnalysisState, transition_analysis
from backend.services.analysis_persistence import create_or_reuse_analysis
from backend.services.analysis_queue import claim_queued_analysis
from backend.services.analysis_worker import complete_attempt

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not set")


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

    real = make_connection_factory(os.environ["TEST_DATABASE_URL"])()
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


def _local_repository(tmp_path):
    identity = ["-c", "user.name=test", "-c", "user.email=test@example.com"]
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print('hello')\n")
    for args in (["init", "--quiet"], ["add", "main.py"], ["commit", "--quiet", "-m", "one"]):
        subprocess.run(["git", *identity, *args], cwd=source, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True)
    return source, sha.stdout.strip()


async def _process_claimed(conn, pipeline, created):
    """Claim the test row and run it through the pipeline the way the worker loop does."""
    from backend.services.analysis_worker import _failure_of

    claimed = claim_queued_analysis(conn)
    if claimed is None or claimed[0] != created.analysis_id:
        pytest.skip("another QUEUED analysis is ahead of the test row")
    analysis_id, attempt_id = claimed
    try:
        outcome = await pipeline(analysis_id, attempt_id)
        await complete_attempt(lambda: conn, analysis_id=analysis_id, attempt_id=attempt_id, outcome=outcome)
    except Exception as exc:
        code, detail = _failure_of(exc)
        await complete_attempt(
            lambda: conn, analysis_id=analysis_id, attempt_id=attempt_id,
            outcome="FAILED", failure_code=code, failure_detail=detail,
        )
    return attempt_id


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_pipeline_acquires_the_commit_and_retries_keep_earlier_evidence(conn, tmp_path):
    from backend.services.analysis_lifecycle import requeue_failed_analysis
    from backend.services.analysis_pipeline import AnalysisPipeline
    from backend.services.repository_acquisition import GitRepositoryAcquirer

    source, sha = _local_repository(tmp_path)
    repository_name = f"repo-{uuid4().hex[:8]}"
    created = create_or_reuse_analysis(
        conn,
        canonical_repository_key=f"github:test-owner/{repository_name}",
        owner_name="test-owner",
        repository_name=repository_name,
        clone_url=f"https://github.com/test-owner/{repository_name}.git",
        requested_reference="main",
        resolved_commit_sha=sha,
        structural_pipeline_version="structural-test",
    )
    acquirer = GitRepositoryAcquirer(
        workdir_root=tmp_path / "work", remote_url_for=lambda key: source.as_uri(), allowed_protocols=("file",)
    )
    pipeline = AnalysisPipeline(lambda: conn, acquirer, ownership_check_seconds=60)

    first = asyncio.run(_process_claimed(conn, pipeline, created))
    state = _get_analysis(conn, created.analysis_id)
    assert state.status == "FAILED"
    assert state.failure_stage == "EXTRACTING"  # acquisition succeeded; extraction does not exist yet
    assert state.failure_code == "PipelineNotImplemented"

    asyncio.run(requeue_failed_analysis(conn, created.analysis_id, requested_by="pytest"))
    second = asyncio.run(_process_claimed(conn, pipeline, created))

    acquisitions = conn.fetch_scalar(
        "SELECT metadata->'acquisitions' FROM core.analysis WHERE id = %s", str(created.analysis_id)
    )
    assert set(acquisitions) == {str(first), str(second)}
    assert all(entry["verified_commit_sha"] == sha for entry in acquisitions.values())
    assert list((tmp_path / "work").iterdir()) == []


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
