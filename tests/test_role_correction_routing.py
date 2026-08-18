import asyncio

from backend.routers import stack_feedback
from backend.services import storage_service


def run(coro):
    return asyncio.run(coro)


def reset_memory():
    storage_service._db = None
    storage_service._memory_store.clear()


def analysis_doc():
    return {
        "repo": {"full_name": "StubbornJava/StubbornJava"},
        "commit_sha": "abc123",
        "pipeline_version": "test",
        "stack": {
            "library": [{
                "name": "Gradle",
                "technology_role": "library",
                "assignment_method": "deterministic",
            }],
        },
    }


def test_schema_known_inactive_role_routes_to_taxonomy(monkeypatch):
    reset_memory()

    async def get_repo(_repo_key):
        return analysis_doc()

    monkeypatch.setattr(storage_service, "get_repo", get_repo)
    result = run(stack_feedback.submit_technology_role_feedback(
        "analysis-1",
        "Gradle",
        stack_feedback.TechnologyRoleFeedback(
            current_role="library", correct=False, corrected_role="build_tool",
        ),
        repo_key="github:stubbornjava/stubbornjava",
    ))

    assert result["technology_role"] == "build"
    assert result["review_status"] == "taxonomy_pending"
    events = run(storage_service.get_correction_events("taxonomy_pending"))
    assert events[0]["analysis_id"] == "analysis-1"
    items, _ = run(storage_service.get_review_queue(kind="emergent_role"))
    assert items[0]["pipeline_value"] == "build"


def test_valid_role_correction_routes_to_classifier_diagnosis(monkeypatch):
    reset_memory()

    async def get_repo(_repo_key):
        return analysis_doc()

    monkeypatch.setattr(storage_service, "get_repo", get_repo)
    result = run(stack_feedback.submit_technology_role_feedback(
        "analysis-2",
        "Gradle",
        stack_feedback.TechnologyRoleFeedback(
            current_role="library", correct=False, corrected_role="infra",
        ),
        repo_key="github:stubbornjava/stubbornjava",
    ))

    assert result["review_status"] == "classifier_diagnosis"
    items, _ = run(storage_service.get_review_queue(kind="classifier_diagnosis"))
    assert items[0]["evidence"]["required_resolution"] == "classifier_fix"
    assert storage_service._memory("corrections") == {}


def test_promoted_emergent_role_is_accepted_without_v2_enum_membership(monkeypatch):
    reset_memory()

    async def get_repo(_repo_key):
        return analysis_doc()

    async def valid_roles():
        return {"languages", "library", "infra", "bundler"}

    monkeypatch.setattr(storage_service, "get_repo", get_repo)
    monkeypatch.setattr(
        "backend.services.technology_role_registry.valid_technology_roles",
        valid_roles,
    )
    result = run(stack_feedback.submit_technology_role_feedback(
        "analysis-3",
        "Gradle",
        stack_feedback.TechnologyRoleFeedback(
            current_role="library", correct=False, corrected_role="bundler",
        ),
        repo_key="github:stubbornjava/stubbornjava",
    ))

    assert result["technology_role"] == "bundler"
    assert result["review_status"] == "classifier_diagnosis"
