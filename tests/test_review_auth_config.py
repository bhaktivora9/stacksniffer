import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from routers import review
from routers import admin_auth


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


def test_review_approve_requires_valid_admin_session(monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "maintainer")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct-password")

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

    approved = client.post(
        approve_path,
        json={"target_value": "library"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"


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
