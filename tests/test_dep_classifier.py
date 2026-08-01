import asyncio
import json

import backend.services.dep_classifier as dep_classifier
from backend.services.dep_classifier import apply_file_signals


def _by_name(file_tree: list[str]) -> dict[str, dict]:
    return {item["name"]: item for item in apply_file_signals(file_tree)}


def test_common_language_extension_signals():
    detected = _by_name([
        "backend/app.py",
        "backend/routes.py",
        "web/index.js",
        "web/config.js",
        "web/App.tsx",
        "web/Button.tsx",
    ])

    assert detected["Python"]["file_count"] == 2
    assert detected["JavaScript"]["file_count"] == 2
    assert detected["TypeScript"]["file_count"] == 2


def test_related_extensions_are_aggregated_for_language_count():
    detected = _by_name([
        "src/index.ts",
        "src/api.ts",
        "src/App.tsx",
        "src/Button.tsx",
    ])

    assert detected["TypeScript"]["file_count"] == 4


def test_manifest_signal_preserves_and_receives_source_count():
    detected = _by_name(["go.mod", "cmd/main.go", "pkg/service.go"])

    assert detected["Go"]["confidence"] == 1.0
    assert detected["Go"]["file_count"] == 2


def test_dependency_tail_technology_role_and_layer_share_one_gemini_call(monkeypatch):
    class Response:
        text = json.dumps(
            [
                {
                    "name": "Mystery Queue",
                    "technology_role": "messaging",
                    "architectural_layer": "data",
                    "layer_confidence": 0.99,
                    "confidence": 0.81,
                    "scope": "required",
                    "packages": ["mystery-queue"],
                },
                {
                    "name": "Opaque Utility",
                    "technology_role": "library",
                    "architectural_layer": None,
                    "confidence": 0.62,
                    "scope": "required",
                    "packages": ["opaque-util"],
                },
                {
                    "name": "Network Thing",
                    "technology_role": "library",
                    "architectural_layer": "networking",
                    "confidence": 0.7,
                    "scope": "required",
                    "packages": ["network-thing"],
                },
                {
                    "name": "Missing Layer",
                    "technology_role": "library",
                    "confidence": 0.7,
                    "scope": "required",
                    "packages": ["missing-layer"],
                },
            ]
        )

    class Model:
        def __init__(self):
            self.calls = []

        def generate_content(self, prompt, request_options=None):
            self.calls.append((prompt, request_options))
            return Response()

    async def no_feedback():
        return ""

    async def no_store(*_args, **_kwargs):
        return None

    async def technology_roles():
        return {"messaging", "library"}

    monkeypatch.setattr(dep_classifier, "_build_technology_role_feedback_context", no_feedback)
    monkeypatch.setattr(dep_classifier, "_store_emergent_technology_roles", no_store)
    monkeypatch.setattr(dep_classifier, "valid_technology_roles", technology_roles)
    model = Model()

    result = asyncio.run(
        dep_classifier.classify_dependencies(
            raw_deps=[
                {"name": "mystery-queue", "scope": "required"},
                {"name": "opaque-util", "scope": "required"},
                {"name": "network-thing", "scope": "required"},
                {"name": "missing-layer", "scope": "required"},
            ],
            file_tree=["requirements.txt"],
            repo_full_name="test/example",
            _json_model=model,
        )
    )

    assert len(model.calls) == 1
    assert model.calls[0][1] == {
        "timeout": dep_classifier.GEMINI_REQUEST_TIMEOUT_SECONDS
    }
    assert "architectural_layer" in model.calls[0][0]
    assert result[0]["technology_role"] == "messaging"
    assert result[0]["architectural_layer"] == "data"
    assert result[0]["layer_confidence"] == 0.80
    assert result[0]["layer_resolution"] == "resolved"
    assert result[1]["architectural_layer"] is None
    assert result[1]["layer_resolution"] == "llm_null"
    assert result[2]["architectural_layer"] is None
    assert result[2]["layer_resolution"] == "off_enum"
    assert result[3]["architectural_layer"] is None
    assert result[3]["layer_resolution"] == "missing"
