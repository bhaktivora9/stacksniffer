import asyncio

from backend.routers import learning
from backend.services import storage_service


def run(coro):
    return asyncio.run(coro)


def test_learning_events_exclude_pending_and_include_approved_and_coercions():
    storage_service._db = None
    storage_service._memory_store.clear()

    pending_id = run(storage_service.append_correction_event(
        correction_field="software_type",
        pipeline_value="library",
        proposed_value="framework",
        evidence={"repo_key": "github:example/pending", "analysis_id": "pending-1"},
        assignment_method="ai_inferred",
        actor=None,
        actor_kind="user",
    ))
    approved_id = run(storage_service.append_correction_event(
        correction_field="software_type",
        pipeline_value="web_app",
        proposed_value="library",
        evidence={"repo_key": "github:example/approved", "analysis_id": "approved-1"},
        assignment_method="ai_inferred",
        actor=None,
        actor_kind="user",
    ))
    run(storage_service.resolve_correction_events(
        [approved_id], resolution="approved", actor="alice",
    ))
    run(storage_service.append_correction_event(
        correction_field="software_type",
        pipeline_value="ml_platform",
        proposed_value="unknown",
        evidence={"repo_key": "github:example/coerced", "analysis_id": "coerced-1"},
        assignment_method="system_guard",
        actor="system",
        actor_kind="system",
        resolution="system_coercion",
        event_kind="system_coercion",
    ))
    run(storage_service.append_correction_event(
        correction_field="technology_role",
        pipeline_value="infra",
        proposed_value="bundler",
        evidence={"repo_key": "github:example/role", "analysis_id": "role-1"},
        assignment_method="deterministic",
        actor=None,
        actor_kind="user",
        resolution="classifier_diagnosis",
    ))
    run(storage_service.append_correction_event(
        correction_field="architectural_layer",
        pipeline_value="backend",
        proposed_value="frontend",
        evidence={"repo_key": "github:example/layer", "analysis_id": "layer-1"},
        assignment_method="ai_inferred",
        actor=None,
        actor_kind="user",
    ))

    response = run(learning.learning_events())
    ids = {event["event_id"] for event in response["events"]}
    assert pending_id not in ids
    assert approved_id in ids
    assert response["count"] == 2
    assert response["feedback_counts"] == {
        "software_type": 2,
        "technology_role": 1,
        "architectural_layer": 1,
    }
    assert response["feedback_count_total"] == 4
    assert response["scope"] == "approved_human_corrections_and_system_coercions"
