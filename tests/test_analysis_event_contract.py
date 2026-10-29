"""The analysis event stream's contract with the frontend.

backend/tests/fixtures/contracts/analysis-event-stream.json holds the events the stream sends for
each way an analysis can end. This test rebuilds them with the router's own event builder, so a
change to the event shape fails here; the frontend replays the same file (src/utils/analysisEvents.test.js
and e2e/analysis-flow.spec.js), so it cannot drift silently either.

Regenerate after a deliberate change:  UPDATE_CONTRACT_FIXTURES=1 python -m pytest tests/test_analysis_event_contract.py
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

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


def _state(status, last_completed, awaiting, failure_stage):
    return AnalysisStateResponse(
        analysis_id=ANALYSIS_ID, attempt_id=ATTEMPT_ID, status=status, created_at=CREATED,
        failure_stage=failure_stage, failure_code="EMBEDDING_FAILED" if failure_stage else None,
        awaiting_stage=awaiting, last_completed_stage=last_completed,
    )


def _streams():
    built = {}
    for name, states in STREAMS.items():
        events = [_state_event(_state(*state), sequence, REPOSITORY) for sequence, state in enumerate(states, 1)]
        built[name] = [{**event, "timestamp": TIMESTAMP} for event in events]
    return built


@pytest.fixture(autouse=True)
def readiness():
    analyses.configure_stage_readiness({"SUMMARIZING": UNCONFIGURED_SUMMARIES})
    yield
    analyses.configure_stage_readiness({})


def test_the_event_stream_matches_the_frontend_contract():
    built = _streams()
    if os.getenv("UPDATE_CONTRACT_FIXTURES"):
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps({"streams": built}, indent=2) + "\n", encoding="utf-8", newline="\n")
    assert json.loads(FIXTURE.read_text(encoding="utf-8")) == {"streams": built}


def test_a_parked_analysis_says_why_it_waits_and_how_to_resume():
    parked = _streams()["parked"][-1]
    assert parked["state"] == "AWAITING_STAGE" and parked["awaiting_stage"] == "SUMMARIZING"
    assert "GEMINI_API_KEY is not set" in parked["waiting_reason"]
    assert "resumes automatically" in parked["waiting_reason"]
    analyses.configure_stage_readiness({"SUMMARIZING": {"status": "enabled"}})
    assert "resumes this analysis shortly" in _state_event(_state("AWAITING_STAGE", "PROJECTING", "SUMMARIZING", None),
                                                           1)["waiting_reason"]


class OneStateConnection:
    """Answers the stream's two queries for one analysis resting in a finished state."""

    def fetch_one(self, sql, *params):
        if "canonical_key" in sql and "LATERAL" not in sql:
            return (REPOSITORY,)
        return (str(ANALYSIS_ID), "READY", CREATED, CREATED, str(ATTEMPT_ID), None, None, None, None)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(analyses.router)
    analyses.configure_connection_factory(OneStateConnection)
    with TestClient(app) as test_client:
        yield test_client


def _events(response):
    blocks = [block for block in response.text.replace("\r\n", "\n").split("\n\n") if "data:" in block]
    parsed = []
    for block in blocks:
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        parsed.append((fields.get("id"), json.loads(fields["data"])))
    return parsed


def test_stream_events_carry_ids_and_continue_numbering_after_a_reconnect(client):
    url = f"/api/v1/analyses/{ANALYSIS_ID}/events"
    [(event_id, event)] = _events(client.get(url))
    assert event_id == "1" and event["sequence"] == 1 and event["state"] == "READY"
    assert event["repository"] == REPOSITORY and event["stages"][0] == "INGESTING"

    [(event_id, event)] = _events(client.get(url, headers={"Last-Event-ID": "7"}))
    assert event_id == "8" and event["sequence"] == 8
    [(event_id, _)] = _events(client.get(url, headers={"Last-Event-ID": "not-a-number"}))
    assert event_id == "1"
