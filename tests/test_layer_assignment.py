from backend.models.schemas import (
    DetectedTech,
    RepositoryClassification,
)
from backend.services.layer_assignment import assign_missing_architectural_layers


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
