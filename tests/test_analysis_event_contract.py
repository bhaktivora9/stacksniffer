"""The analysis event stream's contract with the frontend.

backend/tests/fixtures/contracts/analysis-event-stream.json holds the events the stream sends for
each way an analysis can end. This test rebuilds them with the router's own event builder, so a
change to the event shape fails here; the frontend replays the same file (src/utils/analysisEvents.test.js
and e2e/analysis-flow.spec.js), so it cannot drift silently either.

Regenerate after a deliberate change:  UPDATE_CONTRACT_FIXTURES=1 python -m pytest tests/test_analysis_event_contract.py
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.routers import analyses
from backend.routers.analyses import AnalysisStateResponse, _state_event

FIXTURE = Path(__file__).resolve().parents[1] / "backend/tests/fixtures/contracts/analysis-event-stream.json"
ANALYSIS_ID = UUID("00000000-0000-4000-8000-0000000000a1")
ATTEMPT_ID = UUID("00000000-0000-4000-8000-0000000000b1")
REPOSITORY = "github:pallets/flask"
TIMESTAMP = "2026-10-29T18:00:00+00:00"
CREATED = datetime(2026, 10, 29, 18, 0, tzinfo=timezone.utc)
UNCONFIGURED_SUMMARIES = {"status": "unconfigured", "provider": "gemini", "reason": "GEMINI_API_KEY is not set"}

RUN = ["QUEUED", "INGESTING", "EXTRACTING", "INDEXING", "PROJECTING", "SUMMARIZING"]
# (status, last_completed_stage, awaiting_stage, failure_stage) for each stream, in order.
STREAMS = {
    "ready": [(s, None, None, None) for s in RUN] + [("READY", None, None, None)],
    "degraded": [(s, None, None, None) for s in RUN] + [("DEGRADED", None, None, None)],
    "parked": [(s, None, None, None) for s in RUN[:5]] + [("AWAITING_STAGE", "PROJECTING", "SUMMARIZING", None)],
    "failed": [(s, None, None, None) for s in RUN[:4]] + [("FAILED", None, None, "INDEXING")],
}


def _state(status, last_completed, awaiting, failure_stage, version=1):
    return AnalysisStateResponse(
        analysis_id=ANALYSIS_ID, attempt_id=ATTEMPT_ID, status=status, created_at=CREATED,
        failure_stage=failure_stage, failure_code="EMBEDDING_FAILED" if failure_stage else None,
        awaiting_stage=awaiting, last_completed_stage=last_completed, state_version=version,
    )


def _streams():
    built = {}
    for name, states in STREAMS.items():
        events = [_state_event(_state(*state, version=version), REPOSITORY) for version, state in enumerate(states, 1)]
        built[name] = [{**event, "timestamp": TIMESTAMP} for event in events]
    return built


@pytest.fixture(autouse=True)
def readiness():
    analyses.configure_stage_readiness({"SUMMARIZING": UNCONFIGURED_SUMMARIES})
    yield
    analyses.configure_stage_readiness({})


def _contract():
    stages = asyncio.run(analyses.pipeline_stages())["stages"]  # GET /api/v1/analyses/stages
    return {"stages": stages, "streams": _streams()}


def test_the_event_stream_matches_the_frontend_contract():
    built = _contract()
    if os.getenv("UPDATE_CONTRACT_FIXTURES"):
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(built, indent=2) + "\n", encoding="utf-8", newline="\n")
    assert json.loads(FIXTURE.read_text(encoding="utf-8")) == built


def test_a_parked_analysis_says_why_it_waits_and_how_to_resume():
    parked = _streams()["parked"][-1]
    assert parked["state"] == "AWAITING_STAGE" and parked["awaiting_stage"] == "SUMMARIZING"
    assert "GEMINI_API_KEY is not set" in parked["waiting_reason"]
    assert "resumes automatically" in parked["waiting_reason"]
    analyses.configure_stage_readiness({"SUMMARIZING": {"status": "enabled"}})
    assert "resumes this analysis shortly" in _state_event(
        _state("AWAITING_STAGE", "PROJECTING", "SUMMARIZING", None))["waiting_reason"]


def _row(status, version):
    return (str(ANALYSIS_ID), status, CREATED, CREATED, str(ATTEMPT_ID), None, None, None, None, version)


class ScriptedConnection:
    """Answers the stream's queries; each poll of the analysis returns the next scripted (status, version)."""

    def __init__(self, script):
        self.script = list(script)

    def __call__(self):
        return self

    def fetch_one(self, sql, *params):
        if "canonical_key" in sql and "LATERAL" not in sql:
            return (REPOSITORY,)
        status, version = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return _row(status, version)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _client(script, monkeypatch):
    """A fresh app, as after an API restart: nothing survives from an earlier one but the database."""
    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(analyses, "asyncio", SimpleNamespace(sleep=no_wait))
    app = FastAPI()
    app.include_router(analyses.router)
    analyses.configure_connection_factory(ScriptedConnection(script))
    return TestClient(app)


def _events(response):
    blocks = [block for block in response.text.replace("\r\n", "\n").split("\n\n") if "data:" in block]
    parsed = []
    for block in blocks:
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        parsed.append((fields.get("id"), json.loads(fields["data"])))
    return parsed


URL = f"/api/v1/analyses/{ANALYSIS_ID}/events"


def test_event_ids_are_the_persisted_state_version(monkeypatch):
    with _client([("READY", 7)], monkeypatch) as client:
        [(event_id, event)] = _events(client.get(URL))
    assert event_id == "7" and event["sequence"] == 7 and event["state"] == "READY"
    assert event["repository"] == REPOSITORY


def test_after_an_api_restart_a_reconnect_resumes_from_the_last_event_and_repeats_nothing(monkeypatch):
    # Before the restart the browser saw INDEXING at version 4.
    with _client([("INDEXING", 4), ("READY", 7)], monkeypatch) as client:
        assert [i for i, _ in _events(client.get(URL))][0] == "4"
    # A new process: an in-memory counter would restart at 1; the persisted version does not.
    with _client([("INDEXING", 4), ("PROJECTING", 5), ("READY", 7)], monkeypatch) as client:
        events = _events(client.get(URL, headers={"Last-Event-ID": "4"}))
    assert [(i, e["state"]) for i, e in events] == [("5", "PROJECTING"), ("7", "READY")]
    with _client([("READY", 7)], monkeypatch) as client:
        assert _events(client.get(URL, headers={"Last-Event-ID": "7"})) == []  # already seen
        assert [i for i, _ in _events(client.get(URL, headers={"Last-Event-ID": "junk"}))] == ["7"]


def test_a_state_that_repeats_is_sent_again(monkeypatch):
    # A retry re-enters INDEXING under a new version; deduplicating by state name would drop it.
    with _client([("INDEXING", 3), ("INDEXING", 6), ("READY", 9)], monkeypatch) as client:
        events = _events(client.get(URL))
    assert [(i, e["state"]) for i, e in events] == [("3", "INDEXING"), ("6", "INDEXING"), ("9", "READY")]


def test_the_stage_list_is_served_once(monkeypatch):
    with _client([("READY", 1)], monkeypatch) as client:
        stages = client.get("/api/v1/analyses/stages").json()["stages"]
    assert stages == ["INGESTING", "EXTRACTING", "INDEXING", "PROJECTING", "SUMMARIZING"]
    assert all("stages" not in event for stream in _streams().values() for event in stream)
