from uuid import UUID, uuid4

from backend.services.analysis_persistence import (
    create_or_reuse_analysis,
    run_in_transaction,
)

CREATE_ARGS = {
    "canonical_repository_key": "github:owner/repository",
    "owner_name": "owner",
    "repository_name": "repository",
    "clone_url": "https://github.com/owner/repository.git",
    "requested_reference": "main",
    "resolved_commit_sha": "a" * 40,
    "structural_pipeline_version": "structural-v1",
}


class FakeConnection:
    def __init__(self, *, existing_analysis=None, latest_attempt=None):
        self.existing_analysis = existing_analysis
        self.latest_attempt = latest_attempt
        self.calls = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def fetch_scalar(self, query, *args):
        self.calls.append(query)
        if "FROM core.analysis_attempt" in query:
            return self.latest_attempt
        return uuid4()

    def fetch_one(self, query, *args):
        self.calls.append(query)
        if "INSERT INTO core.analysis" in query:
            return None if self.existing_analysis else (uuid4(), "QUEUED")
        if "FROM core.analysis" in query:
            return self.existing_analysis
        return None

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def test_creation_persists_in_dependency_order_without_an_attempt():
    conn = FakeConnection()
    result = create_or_reuse_analysis(conn, **CREATE_ARGS)

    assert isinstance(result.analysis_id, UUID)
    assert result.attempt_id is None
    assert result.status == "QUEUED"
    assert result.reused is False
    assert "INSERT INTO core.repository (" in conn.calls[0]
    assert "INSERT INTO core.repository_version" in conn.calls[1]
    assert "INSERT INTO core.analysis (" in conn.calls[2]
    assert not any("INSERT INTO core.analysis_attempt" in query for query in conn.calls)


def test_existing_analysis_is_reused_with_its_latest_attempt():
    analysis_id = uuid4()
    attempt_id = uuid4()
    conn = FakeConnection(existing_analysis=(analysis_id, "READY"), latest_attempt=attempt_id)

    result = create_or_reuse_analysis(conn, **CREATE_ARGS)

    assert result.analysis_id == analysis_id
    assert result.attempt_id == attempt_id
    assert result.status == "READY"
    assert result.reused is True


def test_transaction_commits_only_after_operation_returns():
    conn = FakeConnection()
    result = run_in_transaction(lambda: conn, lambda current: "created")

    assert result == "created"
    assert conn.committed is True
    assert conn.rolled_back is False
    assert conn.closed is True
