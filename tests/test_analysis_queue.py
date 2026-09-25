import asyncio
from uuid import UUID, uuid4

import pytest

from backend.services.analysis_lifecycle import AnalysisTransitionError
from backend.services.analysis_queue import claim_queued_analysis
from backend.services.analysis_worker import claim_analysis_transactionally, complete_attempt


class Result:
    rowcount = 1


class FakeConnection:
    def __init__(self, status="QUEUED"):
        self.attempt_id = uuid4()
        self.analysis_id = uuid4()
        self.status = status
        self.calls = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def fetch_one(self, query, *args):
        self.calls.append((query, args))
        if "SKIP LOCKED" in query:
            return (self.analysis_id,)
        return None

    def fetch_scalar(self, query, *args):
        self.calls.append((query, args))
        if "SELECT status FROM core.analysis" in query:
            return self.status
        return self.attempt_id

    def execute(self, query, *args):
        self.calls.append((query, args))
        return Result()

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True

    def queries(self):
        return [query for query, _ in self.calls]


def test_claim_moves_analysis_to_ingesting_and_creates_running_attempt():
    conn = FakeConnection()
    claimed = claim_analysis_transactionally(lambda: conn)

    assert claimed == (conn.analysis_id, conn.attempt_id)
    queries = conn.queries()
    assert "FOR UPDATE SKIP LOCKED" in queries[0]
    assert "SET status = 'INGESTING'" in queries[1]
    assert "INSERT INTO core.analysis_attempt" in queries[2] and "'RUNNING'" in queries[2]
    assert conn.committed is True
    assert conn.closed is True


def test_empty_queue_returns_without_claiming():
    class EmptyConnection(FakeConnection):
        def fetch_one(self, query, *args):
            self.calls.append((query, args))
            return None

    conn = EmptyConnection()
    assert claim_queued_analysis(conn) is None
    assert len(conn.calls) == 1


def test_complete_attempt_finishes_from_the_current_state():
    conn = FakeConnection(status="SUMMARIZING")
    asyncio.run(complete_attempt(
        lambda: conn, analysis_id=conn.analysis_id, attempt_id=conn.attempt_id, outcome="SUCCEEDED",
    ))

    attempt_update = next(args for query, args in conn.calls if "UPDATE core.analysis_attempt" in query)
    analysis_update = next(args for query, args in conn.calls if "UPDATE core.analysis\n" in query)
    assert attempt_update[0] == "SUCCEEDED"
    assert analysis_update[0] == "READY"
    assert analysis_update[-1] == "SUMMARIZING"
    assert conn.committed is True


def test_failed_attempt_records_the_stage_it_failed_in():
    conn = FakeConnection(status="EXTRACTING")
    asyncio.run(complete_attempt(
        lambda: conn, analysis_id=conn.analysis_id, attempt_id=conn.attempt_id,
        outcome="FAILED", failure_code="ValueError", failure_detail={"message": "boom"},
    ))

    attempt_update = next(args for query, args in conn.calls if "UPDATE core.analysis_attempt" in query)
    assert attempt_update[:3] == ("FAILED", "EXTRACTING", "ValueError")


def test_success_is_rejected_before_the_pipeline_reaches_a_final_stage():
    conn = FakeConnection(status="INGESTING")
    with pytest.raises(AnalysisTransitionError):
        asyncio.run(complete_attempt(
            lambda: conn, analysis_id=conn.analysis_id, attempt_id=conn.attempt_id, outcome="SUCCEEDED",
        ))
    assert conn.rolled_back is True
    assert conn.committed is False
