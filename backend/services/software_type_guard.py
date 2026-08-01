"""Reconcile semantic repository labels with deterministic artifact evidence."""

from __future__ import annotations

from backend.models.schemas import (
    ArtifactType,
    RepositoryClassification,
)

_DEPLOYABLE_TYPES = {
    ArtifactType.DEPLOYABLE_SERVICE,
    ArtifactType.WEB_APPLICATION,
    ArtifactType.CLI_TOOL,
    ArtifactType.DESKTOP_APPLICATION,
    ArtifactType.MOBILE_APPLICATION,
    ArtifactType.DATA_PIPELINE,
}
_NON_DEPLOYABLE_SOFTWARE_TYPES = {"library", "framework", "sdk", "documentation", "template"}


def _artifact_type(artifact) -> ArtifactType | None:
    value = artifact.get("type") if isinstance(artifact, dict) else getattr(artifact, "type", None)
    try:
        return value if isinstance(value, ArtifactType) else ArtifactType(value)
    except (TypeError, ValueError):
        return None


def derive_type_from_artifacts(artifacts) -> str:
    """Derive only values supported by the live software_type vocabulary."""
    types = {_artifact_type(artifact) for artifact in artifacts or []}
    if ArtifactType.WEB_APPLICATION in types:
        return "web_app"
    if ArtifactType.DATA_PIPELINE in types:
        return "data_pipeline"
    if ArtifactType.CLI_TOOL in types:
        return "cli_tool"
    if ArtifactType.DESKTOP_APPLICATION in types:
        return "desktop_app"
    if ArtifactType.MOBILE_APPLICATION in types:
        return "mobile_app"
    # A runnable service is not necessarily an HTTP API. Until the taxonomy
    # has a general service type, unknown is safer than inventing web_api.
    return "unknown"


def guard_software_type(
    resolved_type: str,
    artifacts,
    *,
    on_correction=None,
) -> tuple[str, dict | None]:
    """Reject non-deployable software_type claims contradicted by artifact evidence."""
    current = getattr(resolved_type, "value", resolved_type)
    if current not in _NON_DEPLOYABLE_SOFTWARE_TYPES:
        return current, None
    evidence = [
        artifact_type
        for artifact in artifacts or []
        if (artifact_type := _artifact_type(artifact)) in _DEPLOYABLE_TYPES
    ]
    if not evidence:
        return current, None

    corrected = derive_type_from_artifacts(artifacts)
    record = {
        "guard": "software_type_deployable_contradiction",
        "overridden_from": current,
        "corrected_to": corrected,
        "evidence": [artifact_type.value for artifact_type in evidence],
        "retrain_signal": True,
    }
    if on_correction is not None:
        on_correction(record)
    return corrected, record


def guard_library_classification(
    software_type: str,
    confidence: float,
    reasoning: str,
    classification: RepositoryClassification,
    repo_name: str = "",
    description: str = "",
) -> tuple[str, float, str, bool]:
    """Reject repo-level ``library`` when produced artifacts contradict it.

    Returns ``(software_type, confidence, reasoning, corrected)``. Ambiguous
    deployables become ``unknown`` rather than being forced into web_api.
    """
    corrected, record = guard_software_type(software_type, classification.artifacts)
    if record is None:
        return software_type, confidence, reasoning, False
    evidence = ", ".join(record["evidence"])
    return (
        corrected,
        min(confidence, 0.90) if corrected != "unknown" else 0.0,
        f"Artifact evidence contradicts {software_type}: {evidence}",
        True,
    )
