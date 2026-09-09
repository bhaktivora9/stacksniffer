from fastapi.testclient import TestClient

from agent_service.main import app


client = TestClient(app)


def test_health_and_readiness_are_public() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}