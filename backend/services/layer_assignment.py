"""Assign architectural layers after technology and artifact detection merge."""

from __future__ import annotations

from backend.models.schemas import (
    ArchitecturalLayer,
    ArtifactType,
    DetectedTech,
    RepositoryClassification,
)
from backend.services.cross_cutting_layer_fix import is_cross_cutting_utility

_TECHNOLOGY_ROLE_LAYERS = {
    "databases": "data",
    "messaging": "messaging",
    "ai_ml": "ai_ml",
    "infra": "infra",
    "testing": "testing",
}


def assign_missing_architectural_layers(
    technologies: list[DetectedTech],
    classification: RepositoryClassification,
) -> None:
    """Fill defensible non-seed layers without overwriting curated assignments."""
    artifacts = {artifact.name: artifact for artifact in classification.artifacts}

    for tech in technologies:
        # The broad AI inference path can rediscover a manifest utility under a
        # display name and give it a role that would otherwise imply backend.
        # Cross-cutting identity is stronger evidence: preserve the honest null.
        if tech.detection_source == "ai_inferred" and is_cross_cutting_utility(tech.name):
            tech.architectural_layer = None
            tech.layer_inference_status = "cross_cutting_null"
            continue
        if tech.architectural_layer is not None:
            continue

        # Manifest tail layers belong exclusively to the consolidated model
        # response. Preserve null, off-enum, omitted, and unreturned outcomes.
        if (tech.detection_source or "").startswith("manifest"):
            continue

        layer = _TECHNOLOGY_ROLE_LAYERS.get(tech.technology_role)
        if tech.technology_role == "frameworks":
            artifact = artifacts.get(tech.belongs_to_artifact or "")
            layer = (
                "frontend"
                if artifact and artifact.type == ArtifactType.WEB_APPLICATION
                else "backend"
            )
        if layer is None:
            continue

        source = tech.detection_source or ""
        deterministic = tech.technology_role == "testing" or source == "file_signal"
        method = (
            "emergent"
            if tech.emergent_technology_role
            else "deterministic" if deterministic
            else "ai_inferred"
        )
        tech.architectural_layer = ArchitecturalLayer(
            primary=layer,
            secondary=[],
            assignment_method=method,
            confidence=tech.confidence,
            disambiguation_pending=False,
        )
