"""
backend/routers/stack_feedback.py

Per-technology RLHF feedback — more granular than software_type-level feedback.
Java equivalent: UsageTrackingServiceImpl + PatternValidationServiceImpl
+ PatternPerformanceMetrics in stacksniffer-learning.

SoftwareType feedback answers: "Was this repo classified correctly?"
Stack feedback answers: "Was THIS SPECIFIC TECHNOLOGY correctly detected?"

Verdict types:
  correct        → reward patterns that detected this tech
  false_positive → penalize patterns (tech shown but not actually used)
  false_negative → register for pattern discovery (tech used but not detected)
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import re

from services import storage_service as storage_service
from models.taxonomy import canonicalize_technology_role
from services import stack_feedback_service
from routers.admin_auth import require_admin
from routers.deps import resolve_repo_key

router = APIRouter(prefix="/api/stack-feedback", tags=["stack-feedback"])


class TechEvaluation(BaseModel):
    tech_name: str
    technology_role: str       # languages | frameworks | databases | messaging | ai_ml | infra | testing
    verdict: str        # "correct" | "false_positive" | "false_negative"
    reason: Optional[str] = None


class StackFeedbackRequest(BaseModel):
    tech_evaluations: list[TechEvaluation]
    missing_techs: Optional[list[dict]] = []
    # [{tech_name, technology_role}]
    overall_stack_correct: Optional[bool] = None
    notes: Optional[str] = None


class PrimaryLanguageCorrection(BaseModel):
    primary_language: str


class TechnologyRoleFeedback(BaseModel):
    current_role: str
    correct: bool
    corrected_role: Optional[str] = None
    reason: Optional[str] = None


class ArchitecturalLayerFeedback(BaseModel):
    current_layer: Optional[str] = None
    corrected_layer: str
    reason: Optional[str] = None


class StackFeedbackResponse(BaseModel):
    accepted: bool
    analysis_id: str
    repo_key: str
    evaluations_processed: int
    patterns_updated: int
    new_patterns_discovered: int
    summary: dict


@router.post("/by-id/{id:path}", response_model=StackFeedbackResponse)
@router.post("/{id}", response_model=StackFeedbackResponse)
async def submit_stack_feedback(
    id: str,
    feedback: StackFeedbackRequest,
    repo_key: str = Depends(resolve_repo_key),
):
    """
    Submit per-technology verdicts for an analysis.

    Java equivalent: UsageTrackingService.trackBatchUsages()
    + PatternValidationService.validatePatterns()
    triggered via UsageTrackingEvent → IngestionEventListener.

    Immediate effects:
      correct        → reward_tech() → confidence += 0.015 per keyword
      false_positive → penalize_tech() → confidence -= 0.040 per keyword
      false_negative → register_missing_tech() → adds to patterns.json with empty keywords
    """
    doc = await storage_service.get_repo(repo_key)
    if not doc:
        raise HTTPException(404, "no analysis for this repo")
    stack = doc["stack"]

    pattern_matches = stack.get("pattern_matches", [])
    patterns_updated = 0
    discovered = 0
    update_log = []

    for ev in feedback.tech_evaluations:
        tech    = ev.tech_name
        verdict = ev.verdict
        reason  = ev.reason or ""

        if verdict == "correct":
            result = await stack_feedback_service.reward_tech(tech, pattern_matches)
            patterns_updated += len(result["keywords_updated"])
            update_log.append({"tech": tech, "action": "rewarded",
                                "updates": len(result["keywords_updated"])})

        elif verdict == "false_positive":
            result = await stack_feedback_service.penalize_tech(tech, pattern_matches, reason)
            patterns_updated += len(result["keywords_updated"])
            update_log.append({"tech": tech, "action": "penalized",
                                "updates": len(result["keywords_updated"]),
                                "reason": reason})

        elif verdict == "false_negative":
            update_log.append({"tech": tech, "action": "false_negative_logged"})

    for missing in (feedback.missing_techs or []):
        tech_name = missing.get("tech_name", "")
        technology_role  = missing.get("technology_role", "infra")
        if tech_name:
            result = await stack_feedback_service.register_missing_tech(
                tech_name, technology_role, repo_key
            )
            if result["action"] == "pattern_discovered":
                discovered += 1
            update_log.append(result)

    feedback_doc = {
        "tech_evaluations":    [ev.model_dump() for ev in feedback.tech_evaluations],
        "missing_techs":       feedback.missing_techs or [],
        "overall_stack_correct": feedback.overall_stack_correct,
        "notes":               feedback.notes,
        "patterns_updated_count": patterns_updated,
        "new_patterns_discovered": discovered,
        "created_at":          datetime.utcnow().isoformat(),
    }
    await storage_service.store_feedback(
        repo_key=repo_key,
        commit_sha=doc["commit_sha"],
        pipeline_version=doc["pipeline_version"],
        rated_output=stack,
        rated_embedding=doc.get("stack_embedding"),
        feedback={"type": "stack_feedback", **feedback_doc},
    )
    await storage_service.store_stack_feedback(repo_key, feedback_doc)

    return StackFeedbackResponse(
        accepted=True,
        analysis_id=id,
        repo_key=repo_key,
        evaluations_processed=len(feedback.tech_evaluations),
        patterns_updated=patterns_updated,
        new_patterns_discovered=discovered,
        summary={
            "update_log": update_log,
            "message": (
                f"Processed {len(feedback.tech_evaluations)} tech evaluations. "
                f"Updated {patterns_updated} pattern confidence scores. "
                f"{f'Discovered {discovered} new patterns.' if discovered else ''}"
            )
        }
    )


@router.post("/{id:path}/tech/{tech_name}/correct")
async def mark_tech_correct(
    id: str,
    tech_name: str,
    repo_key: str = Depends(resolve_repo_key),
):
    """Quick endpoint — mark single tech as correctly detected."""
    doc = await storage_service.get_repo(repo_key)
    if not doc:
        raise HTTPException(404, "no analysis for this repo")
    stack = doc["stack"]
    result = await stack_feedback_service.reward_tech(
        tech_name, stack.get("pattern_matches", [])
    )
    feedback_doc = {
        "tech_evaluations": [{"tech_name": tech_name, "verdict": "correct"}],
        "quick_feedback": True,
        "created_at": datetime.utcnow().isoformat(),
    }
    await storage_service.store_feedback(
        repo_key=repo_key,
        commit_sha=doc["commit_sha"],
        pipeline_version=doc["pipeline_version"],
        rated_output=stack,
        rated_embedding=doc.get("stack_embedding"),
        feedback={"type": "stack_feedback", **feedback_doc},
    )
    await storage_service.store_stack_feedback(repo_key, feedback_doc)
    return {
        "accepted": True, "tech": tech_name,
        "patterns_updated": len(result["keywords_updated"]),
        "changes": result["keywords_updated"]
    }


@router.post("/{id:path}/tech/{tech_name}/wrong")
async def mark_tech_wrong(
    id: str,
    tech_name: str,
    reason: Optional[str] = None,
    repo_key: str = Depends(resolve_repo_key),
):
    """Quick endpoint — mark single tech as false positive."""
    doc = await storage_service.get_repo(repo_key)
    if not doc:
        raise HTTPException(404, "no analysis for this repo")
    stack = doc["stack"]
    result = await stack_feedback_service.penalize_tech(
        tech_name,
        stack.get("pattern_matches", []),
        reason or "marked wrong by user"
    )
    feedback_doc = {
        "tech_evaluations": [{"tech_name": tech_name, "verdict": "false_positive",
                               "reason": reason}],
        "quick_feedback": True,
        "created_at": datetime.utcnow().isoformat(),
    }
    await storage_service.store_feedback(
        repo_key=repo_key,
        commit_sha=doc["commit_sha"],
        pipeline_version=doc["pipeline_version"],
        rated_output=stack,
        rated_embedding=doc.get("stack_embedding"),
        feedback={"type": "stack_feedback", **feedback_doc},
    )
    await storage_service.store_stack_feedback(repo_key, feedback_doc)
    return {
        "accepted": True, "tech": tech_name,
        "patterns_penalized": len(result["keywords_updated"]),
        "changes": result["keywords_updated"]
    }


@router.post("/{id:path}/tech/{tech_name}/missing")
async def mark_tech_missing(
    id: str,
    tech_name: str,
    technology_role: str = "infra",
    repo_key: str = Depends(resolve_repo_key),
):
    """Report a technology present in repo but not detected."""
    doc = await storage_service.get_repo(repo_key)
    if not doc:
        raise HTTPException(404, "no analysis for this repo")
    result = await stack_feedback_service.register_missing_tech(
        tech_name, technology_role, repo_key
    )
    feedback_doc = {
        "missing_techs": [{"tech_name": tech_name, "technology_role": technology_role}],
        "quick_feedback": True,
        "created_at": datetime.utcnow().isoformat(),
    }
    await storage_service.store_feedback(
        repo_key=repo_key,
        commit_sha=doc["commit_sha"],
        pipeline_version=doc["pipeline_version"],
        rated_output=doc["stack"],
        rated_embedding=doc.get("stack_embedding"),
        feedback={"type": "stack_feedback", **feedback_doc},
    )
    await storage_service.store_stack_feedback(repo_key, feedback_doc)
    return {"accepted": True, **result}


@router.post("/{id:path}/primary-language")
async def correct_primary_language(
    id: str,
    correction: PrimaryLanguageCorrection,
    _auth: str = Depends(require_admin),
    repo_key: str = Depends(resolve_repo_key),
):
    """Save a corrected primary language as a durable analysis override."""
    language = correction.primary_language.strip()
    if not language:
        raise HTTPException(422, "primary_language must not be empty")

    doc = await storage_service.get_repo(repo_key)
    if not doc:
        raise HTTPException(404, "no analysis for this repo")

    previous = doc.get("stack", {}).get("primary_language", "")
    await storage_service.upsert_correction(repo_key, "primary_language", language)
    feedback_doc = {
        "primary_language_correction": {
            "previous": previous,
            "corrected": language,
        },
        "created_at": datetime.utcnow().isoformat(),
    }
    await storage_service.store_feedback(
        repo_key=repo_key,
        commit_sha=doc["commit_sha"],
        pipeline_version=doc["pipeline_version"],
        rated_output=doc["stack"],
        rated_embedding=doc.get("stack_embedding"),
        feedback={"type": "stack_feedback", **feedback_doc},
    )
    await storage_service.store_stack_feedback(repo_key, feedback_doc)
    return {
        "accepted": True,
        "previous_primary_language": previous,
        "primary_language": language,
    }


@router.post("/{id:path}/tech/{tech_name}/role")
async def submit_technology_role_feedback(
    id: str,
    tech_name: str,
    feedback: TechnologyRoleFeedback,
    request: Request,
    repo_key: str = Depends(resolve_repo_key),
):
    """Confirm or correct the role assigned to one detected technology."""
    doc = await storage_service.get_repo(repo_key)
    if not doc:
        raise HTTPException(404, "no analysis for this repo")

    stack = doc.get("stack", {})
    detected = [
        record
        for records in stack.values()
        if isinstance(records, list)
        for record in records
        if isinstance(record, dict)
        and (record.get("name") or "").casefold() == tech_name.casefold()
    ]
    if not detected:
        raise HTTPException(404, f"technology not found in analysis: {tech_name}")

    corrected_role = feedback.corrected_role
    review_status = "not_required"
    if feedback.correct:
        corrected_role = feedback.current_role
    else:
        require_admin(request)
        if not corrected_role:
            raise HTTPException(422, "corrected_role is required when correct=false")
        from services.technology_role_registry import valid_technology_roles
        valid = await valid_technology_roles()
        # Promoted emergent roles are canonical even when the locked v2 enum
        # predates them. Only apply enum alias normalization when the submitted
        # value is not already active in the live registry.
        if corrected_role not in valid:
            try:
                corrected_role = canonicalize_technology_role(corrected_role).value
            except ValueError:
                corrected_role = re.sub(r"[^a-z0-9]+", "_", corrected_role.strip().lower()).strip("_")
                if not corrected_role:
                    raise HTTPException(422, "corrected_role must contain letters or numbers")
        if corrected_role not in valid:
            # Preserve the taxonomy signal, while still creating a repo-scoped
            # correction that a maintainer can approve as an overlay.
            await storage_service.record_emergent_technology_role(
                corrected_role, tech_name, (doc.get("repo") or {}).get("full_name") or repo_key,
            )
        if corrected_role == feedback.current_role:
            raise HTTPException(422, "corrected_role must differ from current_role")
        assignment_method = detected[0].get("assignment_method") or (
            "deterministic"
            if detected[0].get("detection_source") in {"file_signal", "manifest_table", "project_manifest"}
            else "ai_inferred"
        )
        evidence = {
            "correction_field": "technology_role",
            "tech_name": tech_name,
            "repo": (doc.get("repo") or {}).get("full_name"),
            "repo_key": repo_key,
            "analysis_id": id,
            "source": "user",
            "seen_count": 1,
            "example": feedback.reason,
            "required_resolution": "maintainer_approval",
        }
        event_id = await storage_service.append_correction_event(
            correction_field="technology_role",
            pipeline_value=feedback.current_role,
            proposed_value=corrected_role,
            evidence=evidence,
            assignment_method=assignment_method,
            actor=None,
            actor_kind="user",
            resolution="pending",
        )
        await storage_service.create_review_item(
            kind="correction",
            pipeline_value=feedback.current_role,
            proposed_value=corrected_role,
            assignment_method=assignment_method,
            source="user",
            evidence=evidence,
            repo_key=repo_key,
            trigger_event_id=event_id,
        )
        review_status = "pending"

    feedback_doc = {
        "tech_evaluations": [{
            "tech_name": tech_name,
            "verdict": "role_correct" if feedback.correct else "role_corrected",
            "technology_role": feedback.current_role,
            "corrected_technology_role": corrected_role,
            "reason": feedback.reason,
        }],
        "quick_feedback": True,
        "created_at": datetime.utcnow().isoformat(),
    }
    await storage_service.store_feedback(
        repo_key=repo_key,
        commit_sha=doc["commit_sha"],
        pipeline_version=doc["pipeline_version"],
        rated_output=stack,
        rated_embedding=doc.get("stack_embedding"),
        feedback={"type": "technology_role_feedback", **feedback_doc},
    )
    await storage_service.store_stack_feedback(repo_key, feedback_doc)
    return {
        "accepted": True,
        "tech": tech_name,
        "current_role": feedback.current_role,
        "technology_role": corrected_role,
        "correct": feedback.correct,
        "review_status": review_status,
    }


@router.post("/{id:path}/tech/{tech_name}/layer")
async def submit_architectural_layer_feedback(
    id: str,
    tech_name: str,
    feedback: ArchitecturalLayerFeedback,
    _auth: str = Depends(require_admin),
    repo_key: str = Depends(resolve_repo_key),
):
    """Queue a technology layer correction for maintainer approval."""
    doc = await storage_service.get_repo(repo_key)
    if not doc:
        raise HTTPException(404, "no analysis for this repo")
    stack = doc.get("stack", {})
    detected = next((
        record
        for records in stack.values() if isinstance(records, list)
        for record in records
        if isinstance(record, dict)
        and (record.get("name") or "").casefold() == tech_name.casefold()
    ), None)
    if not detected:
        raise HTTPException(404, f"technology not found in analysis: {tech_name}")
    current = detected.get("architectural_layer") or {}
    current_layer = feedback.current_layer or current.get("primary")
    corrected_layer = re.sub(r"[^a-z0-9]+", "_", feedback.corrected_layer.strip().lower()).strip("_")
    if not corrected_layer:
        raise HTTPException(422, "corrected_layer must contain letters or numbers")
    if corrected_layer == current_layer:
        raise HTTPException(422, "corrected_layer must differ from current_layer")

    evidence = {
        "correction_field": "architectural_layer",
        "tech_name": tech_name,
        "repo": (doc.get("repo") or {}).get("full_name"),
        "repo_key": repo_key,
        "analysis_id": id,
        "source": "user",
        "seen_count": 1,
        "example": feedback.reason,
    }
    event_id = await storage_service.append_correction_event(
        correction_field="architectural_layer",
        pipeline_value=current_layer or "unassigned",
        proposed_value=corrected_layer,
        evidence=evidence,
        assignment_method=current.get("assignment_method") or "ai_inferred",
        actor=None,
        actor_kind="user",
    )
    await storage_service.create_review_item(
        kind="correction",
        pipeline_value=current_layer or "unassigned",
        proposed_value=corrected_layer,
        assignment_method=current.get("assignment_method") or "ai_inferred",
        source="user",
        evidence=evidence,
        repo_key=repo_key,
        trigger_event_id=event_id,
    )
    feedback_doc = {
        "type": "architectural_layer_feedback",
        "tech_name": tech_name,
        "current_layer": current_layer,
        "corrected_layer": corrected_layer,
        "reason": feedback.reason,
        "review_status": "pending",
        "created_at": datetime.utcnow().isoformat(),
    }
    await storage_service.store_feedback(
        repo_key=repo_key,
        commit_sha=doc["commit_sha"],
        pipeline_version=doc["pipeline_version"],
        rated_output=stack,
        rated_embedding=doc.get("stack_embedding"),
        feedback=feedback_doc,
    )
    return {"accepted": True, **feedback_doc}


@router.get("/accuracy")
async def stack_accuracy():
    """
    Per-technology precision/recall/F1 from accumulated feedback.
    Java equivalent: PatternPerformanceMetrics in stacksniffer-learning.
    """
    return await stack_feedback_service.get_stack_learning_summary()


@router.get("/patterns/discovered")
async def discovered_patterns():
    """
    Patterns added via feedback but with no detection keywords yet.
    Next step: inspect a repo that uses them and add keywords to patterns.json.
    Java equivalent: LearnedPattern with PatternStatus.PENDING_VALIDATION.
    """
    result = await stack_feedback_service._get_discovered_patterns_without_keywords()
    return {
        "count": len(result),
        "patterns": result,
        "message": (
            "These technologies were reported as missing but have no detection keywords. "
            "Inspect a repo that uses them and add keywords to patterns.json, "
            "or run POST /api/discovery/run to auto-discover keywords from corpus."
        ) if result else "No undiscovered patterns."
    }
