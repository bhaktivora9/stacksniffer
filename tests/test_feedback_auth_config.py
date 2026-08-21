import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from routers import admin_auth, feedback, stack_feedback


def make_client() -> TestClient:
    app = FastAPI()
    app.include_router(feedback.router)
    return TestClient(app)


def install_feedback_fakes(monkeypatch):
    calls = {"feedback": [], "corrections": []}

    async def get_repo(repo_key):
        return {
            "repo_key": repo_key,
            "commit_sha": "abc123",
            "pipeline_version": "v1",
            "stack": {
                "software_type": "web_app",
                "ai_classification_used": True,
                "pattern_matches": [],
            },
        }

    async def store_feedback(**kwargs):
        calls["feedback"].append(kwargs)

    async def is_valid_software_type(value):
        return value in {"web_app", "library"}

    async def upsert_software_type_correction(**kwargs):
        calls["corrections"].append(kwargs)

    async def no_pattern_updates(*args, **kwargs):
        return 0

    monkeypatch.setattr(feedback.storage_service, "get_repo", get_repo)
    monkeypatch.setattr(feedback.storage_service, "store_feedback", store_feedback)
    monkeypatch.setattr(feedback.storage_service, "is_valid_software_type", is_valid_software_type)
    monkeypatch.setattr(
        feedback.storage_service,
        "upsert_software_type_correction",
        upsert_software_type_correction,
    )
    monkeypatch.setattr(feedback, "reward_patterns", no_pattern_updates)
    monkeypatch.setattr(feedback, "penalize_patterns", no_pattern_updates)
    return calls


def test_plain_feedback_is_public(monkeypatch):
    install_feedback_fakes(monkeypatch)
    client = make_client()

    positive = client.post(
        "/api/feedback/github:owner/repo",
        json={"software_type_correct": True},
    )
    negative = client.post(
        "/api/feedback/github:owner/repo",
        json={"software_type_correct": False},
    )

    assert positive.status_code == 200
    assert negative.status_code == 200


def test_correction_requires_admin_session(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "correct-password")
    calls = install_feedback_fakes(monkeypatch)
    client = make_client()

    denied = client.post(
        "/api/feedback/github:owner/repo",
        json={
            "software_type_correct": False,
            "correct_software_type": "library",
        },
    )
    assert denied.status_code == 403

    token = admin_auth.create_admin_token("admin")
    approved = client.post(
        "/api/feedback/github:owner/repo",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "software_type_correct": False,
            "correct_software_type": "library",
        },
    )

    assert approved.status_code == 200
    assert calls["corrections"][0]["corrected_value"] == "library"


def test_role_confirmation_is_public_but_role_correction_requires_admin(monkeypatch):
    async def get_repo(repo_key):
        return {
            "repo_key": repo_key,
            "commit_sha": "abc123",
            "pipeline_version": "v1",
            "repo": {"full_name": "owner/repo"},
            "stack": {
                "frameworks": [
                    {
                        "name": "FastAPI",
                        "technology_role": "frameworks",
                        "detection_source": "ai",
                    }
                ],
            },
        }

    async def store_feedback(**kwargs):
        return None

    async def store_stack_feedback(*args, **kwargs):
        return None

    monkeypatch.setenv("ADMIN_PASSWORD", "correct-password")
    monkeypatch.setattr(stack_feedback.storage_service, "get_repo", get_repo)
    monkeypatch.setattr(stack_feedback.storage_service, "store_feedback", store_feedback)
    monkeypatch.setattr(stack_feedback.storage_service, "store_stack_feedback", store_stack_feedback)

    app = FastAPI()
    app.include_router(stack_feedback.router)
    client = TestClient(app)

    confirmed = client.post(
        "/api/stack-feedback/github:owner/repo/tech/FastAPI/role",
        json={"current_role": "frameworks", "correct": True},
    )
    assert confirmed.status_code == 200

    denied = client.post(
        "/api/stack-feedback/github:owner/repo/tech/FastAPI/role",
        json={
            "current_role": "frameworks",
            "correct": False,
            "corrected_role": "library",
        },
    )
    assert denied.status_code == 403
