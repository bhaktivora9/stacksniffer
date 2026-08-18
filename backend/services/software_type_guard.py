"""
Corrected software_type_guard.py — fixes fastapi -> cli_tool.

ROOT CAUSE (two errors in the original):
1. derive_type_from_artifacts checked `if CLI_TOOL in types` — ANY CLI artifact
   triggered a cli_tool reclassification. Framework/library repos routinely ship
   CLI tooling (fastapi->typer, next.js->CLI, django->manage.py). A secondary CLI
   artifact is NOT evidence the repo IS a cli_tool.
2. The premise "non-deployable claim + any deployable artifact = wrong" is FALSE
   for frameworks: a framework legitimately produces deployable artifacts
   (example apps, a CLI, tooling). Their presence doesn't contradict "framework".

FIX:
- consider ONLY the PRIMARY artifact, never the set
- NEVER override framework/library -> cli_tool (frameworks ship CLIs)
- only override when the PRIMARY artifact is a genuine deployable PRODUCT
  (deployable_service / web_application / etc.) — the legitimate spring-boot case
"""
from __future__ import annotations

from backend.models.schemas import ArtifactType

_DEPLOYABLE_TYPES = {
    ArtifactType.DEPLOYABLE_SERVICE,
    ArtifactType.WEB_APPLICATION,
    ArtifactType.CLI_TOOL,
    ArtifactType.DESKTOP_APPLICATION,
    ArtifactType.MOBILE_APPLICATION,
    ArtifactType.DATA_PIPELINE,
}

_NON_DEPLOYABLE_SOFTWARE_TYPES = {"library", "framework", "sdk", "documentation", "template"}

_PRIMARY_ARTIFACT_TO_SOFTWARE_TYPE = {
    ArtifactType.WEB_APPLICATION: "web_application",
    ArtifactType.DATA_PIPELINE: "data_pipeline",
    ArtifactType.DESKTOP_APPLICATION: "desktop_application",
    ArtifactType.MOBILE_APPLICATION: "mobile_application",
    ArtifactType.DEPLOYABLE_SERVICE: "deployable_service",
    # CLI_TOOL deliberately absent — see guard below
}


def _artifact_type(artifact) -> ArtifactType | None:
    value = artifact.get("type") if isinstance(artifact, dict) else getattr(artifact, "type", None)
    try:
        return value if isinstance(value, ArtifactType) else ArtifactType(value)
    except (TypeError, ValueError):
        return None


def _is_primary(artifact) -> bool:
    return bool(artifact.get("primary") if isinstance(artifact, dict)
                else getattr(artifact, "primary", False))


def _primary_artifact(artifacts):
    for artifact in artifacts or []:
        if _is_primary(artifact):
            return artifact
    return None


def derive_type_from_primary(artifacts) -> str:
    primary = _primary_artifact(artifacts)
    if primary is None:
        return "unknown"
    return _PRIMARY_ARTIFACT_TO_SOFTWARE_TYPE.get(_artifact_type(primary), "unknown")


def guard_software_type(resolved_type: str, artifacts, *, on_correction=None):
    current = getattr(resolved_type, "value", resolved_type)
    if current not in _NON_DEPLOYABLE_SOFTWARE_TYPES:
        return current, None

    primary = _primary_artifact(artifacts)
    if primary is None:
        return current, None

    ptype = _artifact_type(primary)
    if ptype not in _DEPLOYABLE_TYPES:
        return current, None

    # KEY: never override framework/library -> cli_tool. Frameworks ship CLIs.
    if ptype == ArtifactType.CLI_TOOL:
        return current, None

    corrected = derive_type_from_primary(artifacts)
    if corrected in ("unknown", current):
        return current, None

    record = {
        "guard": "software_type_primary_artifact_contradiction",
        "overridden_from": current,
        "corrected_to": corrected,
        "primary_artifact_type": ptype.value,
        "retrain_signal": True,
    }
    if on_correction is not None:
        on_correction(record)
    return corrected, record


def guard_library_classification(software_type, confidence, reasoning,
                                 classification, repo_name="", description=""):
    corrected, record = guard_software_type(software_type, classification.artifacts)
    if record is None:
        return software_type, confidence, reasoning, False
    return (
        corrected,
        min(confidence, 0.90) if corrected != "unknown" else 0.0,
        f"Primary artifact ({record['primary_artifact_type']}) contradicts {software_type}",
        True,
    )