"""Pipeline wiring: one acquisition per attempt, typed failures, ownership loss."""

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from backend.services import analysis_worker
from backend.services.analysis_lifecycle import ReuseDecision, plan_analysis_work
from backend.services.analysis_pipeline import (
    AnalysisPipeline,
    ExtractionCancelled,
    PipelineNotImplemented,
)
from backend.services.analysis_queue import AttemptOwnershipLost
from backend.services.repository_acquisition import (
    AcquisitionCancelled,
    RepositorySnapshot,
    RepositoryTooLarge,
)

SHA = "a" * 40


class Result:
    rowcount = 1


class FakeConnection:
    """Answers the pipeline's queries and records every statement."""

    def __init__(self, owned=True):
        self.owned = owned
        self.statements = []

    def fetch_one(self, query, *args):
        self.statements.append((" ".join(query.split()), args))
        return ("github:test-owner/test-repo", SHA)

    def fetch_scalar(self, query, *args):
        self.statements.append((" ".join(query.split()), args))
        return 1 if self.owned else None

    def execute(self, query, *args):
        self.statements.append((" ".join(query.split()), args))
        return Result()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class FakeAcquirer:
    def __init__(self, failure=None, wait_for_cancel=False):
        self.failure = failure
        self.wait_for_cancel = wait_for_cancel
        self.requests = []
        self.released = 0

    @asynccontextmanager
    async def acquire(self, request, *, cancel_event: threading.Event):
        self.requests.append(request)
        try:
            if self.wait_for_cancel:
                await asyncio.to_thread(cancel_event.wait, 5)
                raise AcquisitionCancelled("acquisition was cancelled")
            if self.failure:
                raise self.failure
            yield RepositorySnapshot(
                analysis_id=request.analysis_id,
                attempt_id=request.attempt_id,
                canonical_repository_key=request.canonical_repository_key,
                commit_sha=request.commit_sha,
                root=Path("/tmp/never-persisted"),
                files=("main.py",),
                checkout_bytes=6,
                duration_seconds=0.25,
                excluded={"binary": 1},
            )
        finally:
            self.released += 1


def run_pipeline(acquirer, conn=None, **options):
    conn = conn or FakeConnection()
    pipeline = AnalysisPipeline(lambda: conn, acquirer, **options)
    analysis_id, attempt_id = uuid4(), uuid4()
    return conn, analysis_id, attempt_id, lambda: asyncio.run(pipeline(analysis_id, attempt_id))


def test_acquires_once_records_evidence_and_hands_the_snapshot_to_extraction():
    handed = []

    async def structural_stage(context):
        handed.append(context)
        return "SUCCEEDED"

    acquirer = FakeAcquirer()
    conn, analysis_id, attempt_id, run = run_pipeline(acquirer, structural_stage=structural_stage)

    assert run() == "SUCCEEDED"
    assert len(acquirer.requests) == 1
    request = acquirer.requests[0]
    assert (request.analysis_id, request.attempt_id, request.commit_sha) == (analysis_id, attempt_id, SHA)
    assert request.canonical_repository_key == "github:test-owner/test-repo"
    context = handed[0]
    assert (context.analysis_id, context.attempt_id, context.commit_sha) == (analysis_id, attempt_id, SHA)
    assert context.checkout_path == Path("/tmp/never-persisted")
    assert context.files == ("main.py",)
    assert not context.cancellation_token.cancelled
    assert acquirer.released == 1

    statements = [query for query, _ in conn.statements]
    assert not any(query.startswith("INSERT") for query in statements)  # no new identities
    evidence = next(args for query, args in conn.statements if "'acquisitions'" in query)
    assert evidence[0] == str(attempt_id)  # keyed per attempt, so retries keep earlier evidence
    provenance = json.loads(evidence[1])
    assert provenance["verified_commit_sha"] == SHA
    assert "never-persisted" not in evidence[1]
    transition = next(args for query, args in conn.statements if query.startswith("UPDATE core.analysis SET status"))
    assert (transition[0], transition[3]) == ("EXTRACTING", "INGESTING")


def test_missing_extraction_stage_fails_after_releasing_the_snapshot():
    acquirer = FakeAcquirer()
    _, _, _, run = run_pipeline(acquirer)
    with pytest.raises(PipelineNotImplemented):
        run()
    assert acquirer.released == 1


def test_acquisition_failure_is_persisted_with_its_typed_code(monkeypatch):
    failure = RepositoryTooLarge("too big", checkout_bytes=10, max_checkout_bytes=5)
    conn = FakeConnection()
    completed = run_worker_once(monkeypatch, AnalysisPipeline(lambda: conn, FakeAcquirer(failure=failure)))

    assert completed[0]["outcome"] == "FAILED"
    assert completed[0]["failure_code"] == "REPOSITORY_TOO_LARGE"
    assert completed[0]["failure_detail"] == {
        "message": "too big", "checkout_bytes": 10, "max_checkout_bytes": 5, "error_type": "RepositoryTooLarge",
    }
    assert not any(query.startswith("UPDATE core.analysis SET status") for query, _ in conn.statements)


def test_lost_ownership_cancels_acquisition_and_is_not_finalized(monkeypatch):
    conn = FakeConnection(owned=False)
    acquirer = FakeAcquirer(wait_for_cancel=True)
    _, _, _, run = run_pipeline(acquirer, conn=conn, ownership_check_seconds=0.05)
    with pytest.raises(AttemptOwnershipLost):
        run()
    assert acquirer.released == 1

    pipeline = AnalysisPipeline(lambda: conn, FakeAcquirer(wait_for_cancel=True), ownership_check_seconds=0.05)
    assert run_worker_once(monkeypatch, pipeline) == []  # the recovering worker already closed it


def test_lost_ownership_during_extraction_signals_the_structural_stage(monkeypatch):
    observed = []

    async def cooperative_stage(context):
        # What SS-BE-201 must do: check the token between files and stop with ExtractionCancelled.
        for _ in range(100):
            await asyncio.sleep(0.02)
            if context.cancellation_token.cancelled:
                observed.append(context.cancellation_token.reason)
                context.cancellation_token.raise_if_cancelled()
        return "SUCCEEDED"

    conn = FakeConnection(owned=False)
    acquirer = FakeAcquirer()
    _, _, _, run = run_pipeline(acquirer, conn=conn, structural_stage=cooperative_stage, ownership_check_seconds=0.05)
    with pytest.raises(AttemptOwnershipLost):
        run()
    assert observed == ["ownership_lost"]
    assert acquirer.released == 1


def test_extraction_cancelled_for_other_reasons_is_its_own_failure_code(monkeypatch):
    async def stage(context):
        context.cancellation_token.cancel("shutdown")
        context.cancellation_token.raise_if_cancelled()

    completed = run_worker_once(monkeypatch, AnalysisPipeline(lambda: FakeConnection(), FakeAcquirer(), structural_stage=stage))
    assert completed[0]["failure_code"] == "EXTRACTION_CANCELLED"
    assert completed[0]["failure_detail"]["error_type"] == ExtractionCancelled.__name__


def test_profile_changes_reuse_structural_output_without_reacquiring():
    version = {
        "commit_sha": SHA,
        "structural_pipeline_version": "structural-v1",
        "current_profile_state": {"commit_sha": SHA, "structural_pipeline_version": "structural-v1"},
    }
    for profile in ("embedding_profile", "chunk_schema_version", "retriever_version", "prompt_profile"):
        plan = plan_analysis_work(version, {profile: "changed"})
        assert plan.download == ReuseDecision.REUSE, profile
        assert plan.extract == ReuseDecision.REUSE, profile


def run_worker_once(monkeypatch, pipeline):
    completed = []
    stop = asyncio.Event()
    claims = iter([(uuid4(), uuid4())])

    def fake_claim(factory, resumable_stages=()):
        claimed = next(claims, None)
        if claimed is None:
            stop.set()
        return claimed

    async def fake_complete(factory, **kwargs):
        completed.append(kwargs)

    monkeypatch.setattr(analysis_worker, "claim_analysis_transactionally", fake_claim)
    monkeypatch.setattr(analysis_worker, "complete_attempt", fake_complete)
    asyncio.run(analysis_worker.run_worker_loop(lambda: None, pipeline, stop_event=stop, poll_seconds=0))
    return completed
