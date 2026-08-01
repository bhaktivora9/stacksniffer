import pytest
from pydantic import ValidationError

from backend.models.schemas import (
    ArchitecturalLayer,
    Artifact,
    AssignmentMethod,
    DetectedTech,
    RepositoryClassification,
)


def test_single_artifact_contract():
    classification = RepositoryClassification(
        artifact_count="single",
        artifacts=[
            Artifact(
                name="api",
                type="deployable_service",
                path="/",
                primary=True,
            )
        ],
    )

    assert classification.model_dump(mode="json") == {
        "artifact_count": "single",
        "artifacts": [
            {
                "name": "api",
                "type": "deployable_service",
                "path": "/",
                "primary": True,
                "subordinate_to": None,
            }
        ],
    }


def test_multi_artifact_contract_supports_subordination():
    classification = RepositoryClassification(
        artifact_count="multi",
        artifacts=[
            {
                "name": "influxd",
                "type": "deployable_service",
                "path": "/",
                "primary": True,
            },
            {
                "name": "ui",
                "type": "web_application",
                "path": "/ui",
                "subordinate_to": "influxd",
            },
        ],
    )

    assert classification.artifacts[1].subordinate_to == "influxd"


@pytest.mark.parametrize(
    "payload",
    [
        {
            "artifact_count": "single",
            "artifacts": [
                {"name": "api", "type": "deployable_service", "path": "/", "primary": True},
                {"name": "ui", "type": "web_application", "path": "/ui"},
            ],
        },
        {
            "artifact_count": "single",
            "artifacts": [
                {"name": "api", "type": "deployable_service", "path": "/", "primary": False}
            ],
        },
        {
            "artifact_count": "multi",
            "artifacts": [
                {"name": "api", "type": "deployable_service", "path": "/", "primary": True},
                {
                    "name": "ui",
                    "type": "web_application",
                    "path": "/ui",
                    "subordinate_to": "missing",
                },
            ],
        },
    ],
)
def test_repository_classification_rejects_inconsistent_artifacts(payload):
    with pytest.raises(ValidationError):
        RepositoryClassification.model_validate(payload)


def test_architectural_layer_contract_is_distinct_from_ui_technology_role():
    tech = DetectedTech(
        name="React",
        confidence=0.98,
        detection_source="manifest",
        technology_role="frameworks",
        architectural_layer=ArchitecturalLayer(
            primary="frontend",
            assignment_method=AssignmentMethod.DETERMINISTIC,
            confidence=0.98,
        ),
        belongs_to_artifact="ui",
        usage_scope="runtime",
    )

    dumped = tech.model_dump(mode="json")
    assert dumped["technology_role"] == "frameworks"
    assert dumped["architectural_layer"]["primary"] == "frontend"
    assert dumped["architectural_layer"]["assignment_method"] == "deterministic"


def test_architectural_layer_rejects_invalid_confidence_and_duplicate_primary():
    with pytest.raises(ValidationError):
        ArchitecturalLayer(
            primary="cache",
            secondary=["cache"],
            assignment_method="ai_inferred",
            confidence=1.2,
        )

