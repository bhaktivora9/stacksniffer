import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from services import github_service


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def get(self, url, headers=None, **kwargs):
        self.calls.append({"url": url, "headers": headers or {}, "kwargs": kwargs})
        return self.responses.pop(0)


def test_github_get_retries_without_bad_token(monkeypatch):
    client = FakeClient([FakeResponse(401), FakeResponse(200)])
    monkeypatch.setattr(github_service, "_GITHUB_AUTH_DISABLED", False)
    monkeypatch.setattr(github_service, "GITHUB_TOKEN", "bad-token")

    response = asyncio.run(
        github_service._github_get(
            client,
            "https://api.github.com/repos/jgraph/drawio/commits",
            headers=github_service._build_headers(),
            params={"per_page": "1"},
        )
    )

    assert response.status_code == 200
    assert "Authorization" in client.calls[0]["headers"]
    assert "Authorization" not in client.calls[1]["headers"]
    assert github_service._GITHUB_AUTH_DISABLED is True
