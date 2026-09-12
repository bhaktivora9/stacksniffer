import asyncio
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import services.ai_pipeline as ai_pipeline
import services.storage_service as storage_service


class _Response:
    def __init__(self, payload):
        self.text = json.dumps(payload)


class _Model:
    def __init__(self, payload):
        self.payload = payload
        self.prompt = ""

    def generate_content(self, prompt, **_kwargs):
        self.prompt = prompt
        return _Response(self.payload)


def _run(coro):
    return asyncio.run(coro)


def _configure_taxonomy(monkeypatch):
    async def options():
        return "unknown | library | ml_platform | application_platform"

    async def valid():
        return {"unknown", "library", "ml_platform", "application_platform"}

    async def feedback():
        return ""

    monkeypatch.setattr(ai_pipeline, "_get_software_type_options", options)
    monkeypatch.setattr(ai_pipeline, "_get_valid_software_types", valid)
    monkeypatch.setattr(ai_pipeline, "_build_software_type_feedback_context", feedback)


def test_non_listed_existing_type_is_rejected_not_recorded(monkeypatch):
    _configure_taxonomy(monkeypatch)
    model = _Model({
        "software_type": "ai_framework",
        "software_type_is_new": False,
        "emergent_software_type": None,
        "software_type_confidence": 0.95,
        "software_type_reasoning": "Recognized LangChain",
        "rejected": False,
    })
    recorded = []

    async def record(name, example_repo):
        recorded.append((name, example_repo))

    monkeypatch.setattr(ai_pipeline, "_json_model", model)
    monkeypatch.setattr(storage_service, "record_emergent_software_type", record)

    result = _run(ai_pipeline.classify_software_type({}, [], repo_name="langchain"))

    assert result["rejected"] is True
    assert result["software_type"] == "unknown"
    assert result["software_type_is_new"] is False
    assert result["coerced_from"] == "ai_framework"
    assert result["coercion_reason"] == "non_listed_type_claimed_as_established"
    assert recorded == []
    assert "recognition, not analysis" in model.prompt


def test_explicit_new_type_enters_emergence_queue(monkeypatch):
    _configure_taxonomy(monkeypatch)
    model = _Model({
        "software_type": "unknown",
        "software_type_is_new": True,
        "emergent_software_type": "spatial_computing_runtime",
        "software_type_confidence": 0.8,
        "software_type_reasoning": "Detected spatial runtime and scene graph",
        "specific_identity": "language_runtime",
        "rejected": False,
    })
    recorded = []

    async def record(name, example_repo):
        recorded.append((name, example_repo))

    monkeypatch.setattr(ai_pipeline, "_json_model", model)
    monkeypatch.setattr(storage_service, "record_emergent_software_type", record)

    result = _run(ai_pipeline.classify_software_type({}, [], repo_name="novel/repo"))

    assert result["software_type"] == "unknown"
    assert result["software_type_is_new"] is True
    assert result["emergent_software_type"] == "spatial_computing_runtime"
    assert result["specific_identity"] is None
    assert result["coerced_from"] == "spatial_computing_runtime"
    assert recorded == [("spatial_computing_runtime", "novel/repo")]


def test_prompt_routes_runtime_and_os_identity_to_is_new(monkeypatch):
    _configure_taxonomy(monkeypatch)
    model = _Model({
        "software_type": "unknown",
        "software_type_is_new": True,
        "emergent_software_type": "language_runtime",
        "software_type_confidence": 0.9,
        "software_type_reasoning": "Runtime executes user programs",
        "specific_identity": None,
        "rejected": False,
    })

    async def record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ai_pipeline, "_json_model", model)
    monkeypatch.setattr(storage_service, "record_emergent_software_type", record)
    result = _run(ai_pipeline.classify_software_type({}, ["Python/ceval.c"], repo_name="python/cpython"))

    assert result["software_type_is_new"] is True
    assert result["emergent_software_type"] == "language_runtime"
    assert result["specific_identity"] is None
    assert "LANGUAGE RUNTIMES AND OPERATING SYSTEMS ARE is_new CASES" in model.prompt
    assert "terminal binary" in model.prompt.lower()
    assert "invoke the runtime" in model.prompt.lower()
    assert "software_type_is_new and specific_identity are MUTUALLY EXCLUSIVE" in model.prompt


def test_specific_identity_is_normalized_without_triggering_novelty(monkeypatch):
    _configure_taxonomy(monkeypatch)
    model = _Model({
        "software_type": "library",
        "software_type_is_new": False,
        "emergent_software_type": None,
        "software_type_confidence": 0.9,
        "software_type_reasoning": "Importable model client",
        "specific_identity": "Model Serving",
        "rejected": False,
    })
    monkeypatch.setattr(ai_pipeline, "_json_model", model)

    result = _run(ai_pipeline.classify_software_type({}, [], repo_name="example/repo"))

    assert result["software_type"] == "library"
    assert result["software_type_is_new"] is False
    assert result["specific_identity"] == "model_serving"


def test_canonical_specific_identity_is_cleared(monkeypatch):
    _configure_taxonomy(monkeypatch)
    model = _Model({
        "software_type": "library",
        "software_type_is_new": False,
        "emergent_software_type": None,
        "software_type_confidence": 0.9,
        "software_type_reasoning": "Plain importable package",
        "specific_identity": "library",
        "rejected": False,
    })
    monkeypatch.setattr(ai_pipeline, "_json_model", model)

    result = _run(ai_pipeline.classify_software_type({}, [], repo_name="requests"))

    assert result["specific_identity"] is None


def test_specific_identity_fallback_for_drawio(monkeypatch):
    _configure_taxonomy(monkeypatch)
    model = _Model({
        "software_type": "application_platform",
        "software_type_is_new": False,
        "emergent_software_type": None,
        "software_type_confidence": 0.88,
        "software_type_reasoning": "Diagram editor with web and desktop surfaces",
        "specific_identity": None,
        "rejected": False,
    })
    monkeypatch.setattr(ai_pipeline, "_json_model", model)

    result = _run(
        ai_pipeline.classify_software_type(
            {},
            ["src/main/webapp/js/diagramly/App.js", "src/main/webapp/mxgraph/Editor.js"],
            repo_name="jgraph/drawio",
        )
    )

    assert result["software_type"] == "application_platform"
    assert result["specific_identity"] == "diagram_editor"
