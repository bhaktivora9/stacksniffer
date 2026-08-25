import asyncio
import base64
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from routers import review
from routers import admin_auth
from cors_config import allowed_origins
from services import learning_service
from services import storage_service


def make_client() -> TestClient:
    app = FastAPI()
    app.include_router(review.router)
    return TestClient(app)


def test_admin_password_missing_fails_closed(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)

    with pytest.raises(HTTPException) as exc:
        admin_auth.admin_password()

    assert exc.value.status_code == 500
    assert exc.value.detail == "ADMIN_PASSWORD is not configured"


def test_admin_token_contains_role_and_prefers_session_secret(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "admin-password")
    monkeypatch.setenv("REVIEW_SESSION_SECRET", "session-secret")

    token = admin_auth.create_admin_token("admin")
    payload = admin_auth.verify_admin_token(token)

    assert payload["role"] == "admin"
    monkeypatch.delenv("REVIEW_SESSION_SECRET")
    assert admin_auth.verify_admin_token(token) is None


def test_cors_origins_ignore_wildcard_with_credentials(monkeypatch):
    monkeypatch.setenv(
        "ALLOWED_ORIGINS",
        "*, https://stacksniffer.vercel.app/",
    )

    assert allowed_origins() == ["https://stacksniffer.vercel.app"]


@pytest.mark.parametrize("configured", ["*", " , "])
def test_cors_origins_reject_config_without_explicit_origin(configured):
    with pytest.raises(ValueError, match="ALLOWED_ORIGINS must include"):
        allowed_origins(configured)


def test_cors_origins_default_only_when_env_absent(monkeypatch):
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)

    assert allowed_origins() == ["http://localhost:5173", "http://localhost:3000"]


def test_cors_origin_defaults_are_returned_as_defensive_copy(monkeypatch):
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)

    origins = allowed_origins()
    origins.append("https://unexpected.example")

    assert allowed_origins() == ["http://localhost:5173", "http://localhost:3000"]


def test_learning_service_exposes_classifier_availability_probe(monkeypatch):
    class FakeClassifierPath:
        present = False

        def exists(self):
            return self.present

    model_path = FakeClassifierPath()
    monkeypatch.setattr(learning_service, "SOFTWARE_TYPE_CLASSIFIER_PATH", model_path)

    assert learning_service.software_type_classifier_available() is False
    model_path.present = True
    assert learning_service.software_type_classifier_available() is True


def test_memory_stats_include_stack_feedback_count(monkeypatch):
    monkeypatch.setattr(storage_service, "_db", None)
    monkeypatch.setitem(storage_service._memory_store, "analyses_result", {})
    monkeypatch.setitem(storage_service._memory_store, "feedback", {})
    monkeypatch.setitem(
        storage_service._memory_store,
        "stack_feedback",
        {"analysis-1": [{"type": "correct"}, {"type": "wrong"}]},
    )

    stats = asyncio.run(storage_service.get_stats())

    assert stats["with_stack_feedback"] == 2


def test_review_approve_requires_valid_admin_session(monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "maintainer")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct-password")
    monkeypatch.delenv("ADMIN_SESSION_COOKIE_ENABLED", raising=False)

    async def get_review_item(item_id):
        return {
            "_id": item_id,
            "kind": "correction",
            "status": "pending",
            "pipeline_value": "web_app",
            "proposed_value": "library",
            "assignment_method": "ai_inferred",
            "evidence": {
                "repo_key": "github:owner/repo",
                "correction_field": "software_type",
            },
            "trigger_event_ids": ["event-1"],
        }

    async def get_valid_software_types():
        return ["web_app", "library"]

    async def approve_software_type_correction(*args, **kwargs):
        return None

    async def set_review_item_status(*args, **kwargs):
        return None

    async def resolve_correction_events(*args, **kwargs):
        return None

    monkeypatch.setattr(review.storage_service, "get_review_item", get_review_item)
    monkeypatch.setattr(review.storage_service, "get_valid_software_types", get_valid_software_types)
    monkeypatch.setattr(
        review.storage_service,
        "approve_software_type_correction",
        approve_software_type_correction,
    )
    monkeypatch.setattr(review.storage_service, "set_review_item_status", set_review_item_status)
    monkeypatch.setattr(review.storage_service, "resolve_correction_events", resolve_correction_events)

    client = make_client()
    approve_path = "/api/review/review-item-1/approve"

    assert client.post(approve_path, json={"target_value": "library"}).status_code == 403
    assert client.post(
        approve_path,
        json={"target_value": "library"},
        headers={"Authorization": "Bearer wrong-token"},
    ).status_code == 403

    wrong_login = client.post(
        "/api/review/login",
        json={"username": "maintainer", "password": "wrong-password"},
    )
    assert wrong_login.status_code == 403

    wrong_user_login = client.post(
        "/api/review/login",
        json={"username": "guest", "password": "correct-password"},
    )
    assert wrong_user_login.status_code == 403

    login = client.post(
        "/api/review/login",
        json={"username": "maintainer", "password": "correct-password"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    assert login.json()["role"] == "admin"
    assert "set-cookie" not in login.headers

    approved = client.post(
        approve_path,
        json={"target_value": "library"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"


def test_review_session_post_supports_deprecated_basic_login(monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "maintainer")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct-password")
    credentials = base64.b64encode(b"maintainer:correct-password").decode()

    client = make_client()
    response = client.post(
        "/api/review/session",
        headers={"Authorization": f"Basic {credentials}"},
    )

    assert response.status_code == 200
    assert response.json()["access_token"]
    assert response.json()["role"] == "admin"
    assert response.headers["Deprecation"] == "true"


def test_review_queue_is_readable_without_admin_session(monkeypatch):
    async def get_review_queue(**kwargs):
        return [
            {
                "_id": "review-item-1",
                "kind": "correction",
                "status": "pending",
                "pipeline_value": "web_app",
                "proposed_value": "library",
            }
        ], 1

    monkeypatch.setattr(review.storage_service, "get_review_queue", get_review_queue)

    client = make_client()
    response = client.get("/api/review/queue?kind=correction&status=pending")

    assert response.status_code == 200
    assert response.json()["total"] == 1
