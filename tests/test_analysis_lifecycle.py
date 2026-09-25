import asyncio
from uuid import uuid4

import pytest

from backend.services.analysis_lifecycle import (
    AnalysisState,
    AnalysisTransitionError,
    RetryAttemptError,
    ReuseDecision,
    plan_analysis_work,
    requeue_failed_analysis,
    transition_allowed,
    transition_analysis,
)


class FakeConn:
    def __init__(self, rowcount=1, scalar_value=None, scalars=None):
        self.rowcount = rowcount
        self.scalar_value = scalar_value
        self.scalars = scalars or []
        self.calls = []

    def execute(self, query, *args):
        self.calls.append((query, args))
        return type("Result", (), {"rowcount": self.rowcount})()

    def fetch_scalar(self, query, *args):
        self.calls.append((query, args))
        if self.scalars:
            return self.scalars.pop(0)
        return self.scalar_value


def test_plan_analysis_work_no_changes_reuses_everything():
    repo_version = {
        "commit_sha": "abc123" * 4,
        "structural_pipeline_version": "structural-v1",
        "cached_checkout_available": True,
        "current_profile_state": {
            "commit_sha": "abc123" * 4,
            "structural_pipeline_version": "structural-v1",
            "embedding_profile": "emb-v1",
            "chunk_schema_version": "chunks-v1",
            "retriever_version": "retriever-v1",
            "prompt_profile": "prompt-v1",
        },
    }

    plan = plan_analysis_work(repo_version, {
        "embedding_profile": "emb-v1",
        "chunk_schema_version": "chunks-v1",
        "retriever_version": "retriever-v1",
        "prompt_profile": "prompt-v1",
    })

    assert plan.download == ReuseDecision.REUSE
    assert plan.extract == ReuseDecision.REUSE
    assert plan.embed == ReuseDecision.REUSE
    assert plan.retrieve == ReuseDecision.REUSE
    assert plan.summarize == ReuseDecision.REUSE
    assert plan.evaluate == ReuseDecision.REUSE


def test_plan_analysis_work_prompt_change_only_reuses_download_and_upstream():
    repo_version = {
        "commit_sha": "abc123" * 4,
        "structural_pipeline_version": "structural-v1",
        "cached_checkout_available": True,
        "current_profile_state": {
            "commit_sha": "abc123" * 4,
            "structural_pipeline_version": "structural-v1",
            "embedding_profile": "emb-v1",
            "chunk_schema_version": "chunks-v1",
            "retriever_version": "retriever-v1",
            "prompt_profile": "prompt-v0",
        },
    }

    plan = plan_analysis_work(repo_version, {"prompt_profile": "prompt-v1"})

    assert plan.download == ReuseDecision.REUSE
    assert plan.extract == ReuseDecision.REUSE
    assert plan.embed == ReuseDecision.REUSE
    assert plan.retrieve == ReuseDecision.REUSE
    assert plan.summarize == ReuseDecision.RUN
    assert plan.evaluate == ReuseDecision.RUN


def test_plan_analysis_work_embedding_change_invalidates_downstream():
    repo_version = {
        "commit_sha": "abc123" * 4,
        "structural_pipeline_version": "structural-v1",
        "cached_checkout_available": True,
        "current_profile_state": {
            "commit_sha": "abc123" * 4,
            "structural_pipeline_version": "structural-v1",
            "embedding_profile": "emb-v0",
            "chunk_schema_version": "chunks-v1",
            "retriever_version": "retriever-v1",
            "prompt_profile": "prompt-v1",
        },
    }

    plan = plan_analysis_work(repo_version, {"embedding_profile": "emb-v1"})

    assert plan.download == ReuseDecision.REUSE
    assert plan.extract == ReuseDecision.REUSE
    assert plan.embed == ReuseDecision.RUN
    assert plan.retrieve == ReuseDecision.RUN
    assert plan.evaluate == ReuseDecision.RUN


def test_transition_analysis_valid_and_invalid():
    async def _run():
        analysis_id = uuid4()
        valid_conn = FakeConn(rowcount=1)
        invalid_conn = FakeConn(rowcount=0)

        assert transition_allowed(AnalysisState.QUEUED, AnalysisState.INGESTING) is True
        assert transition_allowed(AnalysisState.QUEUED, AnalysisState.READY) is False
        assert transition_allowed(AnalysisState.READY, AnalysisState.INGESTING) is False

        result = await transition_analysis(valid_conn, analysis_id, AnalysisState.QUEUED, AnalysisState.INGESTING)
        assert result is True

        with pytest.raises(AnalysisTransitionError):
            await transition_analysis(invalid_conn, analysis_id, AnalysisState.QUEUED, AnalysisState.INGESTING)

    asyncio.run(_run())


def test_transition_to_finished_state_sets_completed_at():
    conn = FakeConn(rowcount=1)
    asyncio.run(transition_analysis(conn, uuid4(), AnalysisState.SUMMARIZING, AnalysisState.READY))

    query, args = conn.calls[0]
    assert "completed_at" in query
    assert args[0] == "READY"
    assert args[1] is True


def test_requeue_failed_analysis_returns_it_to_queued():
    conn = FakeConn(rowcount=1)
    asyncio.run(requeue_failed_analysis(conn, uuid4(), requested_by="tester"))

    query, _ = conn.calls[0]
    assert "SET status = 'QUEUED'" in query
    assert "status = 'FAILED'" in query


def test_requeue_rejects_analysis_that_is_not_failed():
    conn = FakeConn(rowcount=0)
    with pytest.raises(RetryAttemptError):
        asyncio.run(requeue_failed_analysis(conn, uuid4()))
