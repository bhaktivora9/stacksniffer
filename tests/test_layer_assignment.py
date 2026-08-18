from models.schemas import (
    DetectedTech,
    RepositoryClassification,
)
from services.layer_assignment import assign_missing_architectural_layers


def _classification():
    return RepositoryClassification(
        artifact_count="multi",
        artifacts=[
            {
                "name": "api",
                "type": "deployable_service",
                "path": "/",
                "primary": True,
            },
            {
                "name": "ui",
                "type": "web_application",
                "path": "/ui",
                "subordinate_to": "api",
            },
        ],
    )


def test_repository_language_is_not_an_architectural_runtime():
    tech = DetectedTech(
        name="Rust",
        confidence=0.99,
        detection_source="file_signal",
        technology_role="languages",
        belongs_to_artifact="api",
    )
    assign_missing_architectural_layers([tech], _classification())
    assert tech.architectural_layer is None


def test_unseeded_manifest_framework_stays_null_without_llm_layer():
    tech = DetectedTech(
        name="Unknown UI framework",
        confidence=0.72,
        detection_source="manifest",
        technology_role="frameworks",
        belongs_to_artifact="ui",
    )
    assign_missing_architectural_layers([tech], _classification())
    assert tech.architectural_layer is None


def test_library_without_defensible_layer_stays_null():
    tech = DetectedTech(
        name="Utility",
        confidence=0.4,
        detection_source="manifest_passthrough",
        technology_role="library",
        belongs_to_artifact="api",
    )
    assign_missing_architectural_layers([tech], _classification())
    assert tech.architectural_layer is None


def test_ai_inferred_cross_cutting_utility_cannot_default_to_backend():
    tech = DetectedTech(
        name="HPPC",
        confidence=0.8,
        detection_source="ai_inferred",
        technology_role="frameworks",
        belongs_to_artifact="api",
        architectural_layer={
            "primary": "backend",
            "assignment_method": "ai_inferred",
            "confidence": 0.8,
        },
    )
    assign_missing_architectural_layers([tech], _classification())
    assert tech.architectural_layer is None
    assert tech.layer_inference_status == "cross_cutting_null"


def test_non_utility_ai_framework_keeps_backend_assignment():
    tech = DetectedTech(
        name="FastAPI",
        confidence=0.8,
        detection_source="ai_inferred",
        technology_role="frameworks",
        belongs_to_artifact="api",
    )
    assign_missing_architectural_layers([tech], _classification())
    assert tech.architectural_layer.primary == "backend"
