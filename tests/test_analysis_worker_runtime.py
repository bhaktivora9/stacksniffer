import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import backend.main as app_module
from backend.services import analysis_worker
from backend.services.analysis_pipeline import PipelineNotImplemented


async def run_analysis_pipeline(analysis_id, attempt_id):
    raise PipelineNotImplemented("repository ingestion is not implemented yet")


class Result:
    rowcount = 1


class RecordingConnection:
    """Fake connection: empty queue, optional stale rows, records every statement."""

    def __init__(self, stale_rows=None):
        self.stale_rows = stale_rows or []
        self.calls = []

    def fetch_one(self, query, *args):
        self.calls.append((query, args))

    def fetch_all(self, query, *args):
        self.calls.append((query, args))
        return self.stale_rows if "aa.status = 'RUNNING'" in query else []

    def fetch_scalar(self, query, *args):
        self.calls.append((query, args))
        return 1

    def execute(self, query, *args):
        self.calls.append((query, args))
        return Result()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


# --- worker loop -----------------------------------------------------------------------------


def _run_one_claim(monkeypatch, process_attempt):
    analysis_id, attempt_id = uuid4(), uuid4()
    completed = []
    stop = asyncio.Event()
    claims = iter([(analysis_id, attempt_id)])

    def fake_claim(factory, resumable_stages=()):
        claimed = next(claims, None)
        if claimed is None:
            stop.set()
        return claimed

    async def fake_complete(factory, **kwargs):
        completed.append(kwargs)

    monkeypatch.setattr(analysis_worker, "claim_analysis_transactionally", fake_claim)
    monkeypatch.setattr(analysis_worker, "complete_attempt", fake_complete)
    asyncio.run(analysis_worker.run_worker_loop(lambda: None, process_attempt, stop_event=stop, poll_seconds=0))
    return analysis_id, attempt_id, completed


def test_loop_records_pipeline_failure_with_its_exception(monkeypatch):
    analysis_id, attempt_id, completed = _run_one_claim(monkeypatch, run_analysis_pipeline)

    assert len(completed) == 1
    call = completed[0]
    assert (call["analysis_id"], call["attempt_id"]) == (analysis_id, attempt_id)
    assert call["outcome"] == "FAILED"
    assert call["failure_code"] == "PipelineNotImplemented"
    assert "not implemented" in call["failure_detail"]["message"]


def test_loop_completes_with_the_pipeline_outcome(monkeypatch):
    async def succeed(analysis_id, attempt_id):
        return "SUCCEEDED"

    _, _, completed = _run_one_claim(monkeypatch, succeed)
    assert completed[0]["outcome"] == "SUCCEEDED"


def test_loop_stops_promptly_while_idle():
    async def run():
        stop = asyncio.Event()
        conn = RecordingConnection()
        task = asyncio.create_task(
            analysis_worker.run_worker_loop(lambda: conn, run_analysis_pipeline, stop_event=stop, poll_seconds=60)
        )
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, timeout=2)  # does not wait out the 60s poll

    asyncio.run(run())


# --- supervisor ------------------------------------------------------------------------------


def test_supervisor_restarts_the_loop_after_a_crash(monkeypatch):
    runs = []

    async def flaky_loop(factory, process_attempt, *, stop_event, **options):
        runs.append(options)
        if len(runs) == 1:
            raise ConnectionError("database went away")
        stop_event.set()

    monkeypatch.setattr(analysis_worker, "run_worker_loop", flaky_loop)
    stop = asyncio.Event()
    asyncio.run(analysis_worker.supervise_worker(
        lambda: None, run_analysis_pipeline, stop_event=stop, restart_delay_seconds=0, poll_seconds=0.5,
    ))

    assert len(runs) == 2
    assert runs[0] == {"poll_seconds": 0.5}


# --- stale-attempt recovery ------------------------------------------------------------------


def _recover(rows, max_attempts=3):
    conn = RecordingConnection(stale_rows=rows)
    requeued = asyncio.run(analysis_worker.recover_stale_attempts(
        lambda: conn, stale_after_seconds=1800, max_attempts=max_attempts,
    ))
    statements = [" ".join(query.split()) for query, _ in conn.calls]
    return requeued, statements, conn


def test_stale_attempt_is_failed_and_its_analysis_requeued():
    analysis_id = uuid4()
    requeued, statements, conn = _recover([(uuid4(), analysis_id, 1, "EXTRACTING")])

    assert requeued == [analysis_id]
    attempt_update = next(args for query, args in conn.calls if "UPDATE core.analysis_attempt" in query)
    assert attempt_update[:3] == ("FAILED", "EXTRACTING", "WORKER_LOST")
    assert any("SET status = %s" in s for s in statements)             # EXTRACTING -> FAILED
    assert any("ELSE 'QUEUED' END" in s for s in statements)           # FAILED -> QUEUED (or AWAITING_STAGE)
    assert any("SKIP LOCKED" in s for s in statements)


def test_stale_attempt_on_last_allowed_try_leaves_analysis_failed():
    requeued, statements, _ = _recover([(uuid4(), uuid4(), 3, "INGESTING")], max_attempts=3)
    assert requeued == []
    assert not any("ELSE 'QUEUED' END" in s for s in statements)


def test_stale_attempt_of_finished_analysis_only_closes_the_attempt():
    requeued, statements, _ = _recover([(uuid4(), uuid4(), 1, "READY")])
    assert requeued == []
    assert not any(s.startswith("UPDATE core.analysis SET") for s in statements)


# --- application startup ---------------------------------------------------------------------


READY_ANALYZERS = {"required": ["go", "java", "javascript", "python", "ruby", "scala"], "unavailable": {}}


def test_app_starts_the_worker_and_reports_health(monkeypatch):
    conn = RecordingConnection()
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setenv("ANALYSIS_WORKER_POLL_SECONDS", "0.05")
    monkeypatch.setattr(app_module, "make_connection_factory", lambda: (lambda: conn))

    with TestClient(app_module.app) as client:
        body = client.get("/api/health").json()
        ready = client.get("/api/health/ready")
        task = app_module.app.state.worker_task
        assert body == {"status": "ok", "database": "ok", "worker": "running", "analyzers": READY_ANALYZERS}
        assert ready.status_code == 200 and ready.json()["failing"] == []

    assert task.done()  # stopped on shutdown
    assert any("status = 'QUEUED'" in query for query, _ in conn.calls)  # it polled the queue


def test_an_api_only_process_with_the_worker_switched_off_is_ready(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setenv("ANALYSIS_WORKER_ENABLED", "false")
    monkeypatch.setattr(app_module, "make_connection_factory", lambda: (lambda: RecordingConnection()))

    with TestClient(app_module.app) as client:
        assert client.get("/api/health").json() == {
            "status": "ok", "database": "ok", "worker": "disabled", "analyzers": READY_ANALYZERS}
        assert client.get("/api/health/ready").status_code == 200


def test_health_without_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with TestClient(app_module.app) as client:
        body = client.get("/api/health").json()
        ready = client.get("/api/health/ready")
        live = client.get("/api/health/live")
    assert body == {"status": "degraded", "database": "unconfigured", "worker": "unconfigured",
                    "analyzers": READY_ANALYZERS}
    assert ready.status_code == 503 and set(ready.json()["failing"]) == {"database", "worker"}
    assert live.status_code == 200 and live.json() == {"status": "ok"}


def test_a_missing_required_grammar_fails_readiness_but_not_liveness(monkeypatch):
    from backend.services.extraction.analyzers import JavaAnalyzer
    from backend.services.extraction.tree_sitter_adapter import TreeSitterParserAdapter

    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setenv("ANALYSIS_WORKER_ENABLED", "false")
    monkeypatch.setattr(app_module, "make_connection_factory", lambda: (lambda: RecordingConnection()))
    # Simulate a deploy without tree-sitter-java.
    monkeypatch.setattr(JavaAnalyzer, "__init__", lambda self, adapter=None: setattr(
        self, "adapter", TreeSitterParserAdapter("java", "tree_sitter_java_missing", "tree-sitter-java")))

    with TestClient(app_module.app) as client:
        ready = client.get("/api/health/ready")
        live = client.get("/api/health/live")
        summary = client.get("/api/health").json()

    assert live.status_code == 200
    assert ready.status_code == 503
    body = ready.json()
    assert body["failing"] == ["analyzers"]
    assert list(body["analyzers"]["unavailable"]) == ["java"]
    assert body["analyzers"]["unavailable"]["java"].startswith("grammar unavailable: ModuleNotFoundError")
    assert summary["status"] == "degraded"


def test_the_offline_provider_enables_indexing(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setenv("ANALYSIS_EMBEDDING_PROVIDER", "offline")
    monkeypatch.setattr(app_module, "make_connection_factory", lambda: (lambda: RecordingConnection()))

    with TestClient(app_module.app) as client:
        ready = client.get("/api/health/ready")
    assert ready.status_code == 200
    assert ready.json()["indexing"] == {"status": "enabled", "provider": "offline",
                                        "embedding_profile": "offline-sha256-3072/1"}


def test_without_a_gemini_key_analyses_wait_for_indexing_and_the_service_stays_ready(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.delenv("ANALYSIS_EMBEDDING_PROVIDER", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(app_module, "make_connection_factory", lambda: (lambda: RecordingConnection()))

    with TestClient(app_module.app) as client:
        ready = client.get("/api/health/ready")
    assert ready.status_code == 200
    assert ready.json()["indexing"]["status"] == "unconfigured"
    assert "GEMINI_API_KEY is not set" in ready.json()["indexing"]["reason"]


def test_the_gemini_provider_is_closed_on_shutdown(monkeypatch):
    closed = []

    class FakeGemini:
        name = "google"

        def __init__(self, **options):
            pass

        def close(self):
            closed.append(True)

    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.delenv("ANALYSIS_EMBEDDING_PROVIDER", raising=False)
    monkeypatch.setattr(app_module, "GeminiEmbeddingProvider", FakeGemini)
    monkeypatch.setattr(app_module, "make_connection_factory", lambda: (lambda: RecordingConnection()))

    with TestClient(app_module.app) as client:
        indexing = client.get("/api/health/ready").json()["indexing"]
        assert indexing == {"status": "enabled", "provider": "google",
                            "embedding_profile": "gemini-embedding-001-3072/1"}
        assert "test-key-not-real" not in str(client.get("/api/health/ready").json())
    assert closed == [True]


def test_required_languages_are_configurable(monkeypatch):
    monkeypatch.setenv("ANALYSIS_REQUIRED_LANGUAGES", "python, java, kotlin")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with TestClient(app_module.app) as client:
        body = client.get("/api/health/ready").json()
    assert body["analyzers"]["required"] == ["java", "kotlin", "python"]
    assert body["analyzers"]["unavailable"] == {"kotlin": "no analyzer registered"}
