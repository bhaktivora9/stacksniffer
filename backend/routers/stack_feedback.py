"""
backend/routers/stack_feedback.py

Per-technology RLHF feedback — more granular than domain-level feedback.
Java equivalent: UsageTrackingServiceImpl + PatternValidationServiceImpl
+ PatternPerformanceMetrics in stacksniffer-learning.

Domain feedback answers: "Was this repo classified correctly?"
Stack feedback answers: "Was THIS SPECIFIC TECHNOLOGY correctly detected?"

Verdict types:
  correct        → reward patterns that detected this tech
  false_positive → penalize patterns (tech shown but not actually used)
  false_negative → register for pattern discovery (tech used but not detected)
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from backend.services import storage_service as storage_service
from backend.services import stack_feedback_service
from backend.routers.deps import resolve_repo_key

router = APIRouter(prefix="/api/stack-feedback", tags=["stack-feedback"])


class TechEvaluation(BaseModel):
    tech_name: str
    category: str       # languages | frameworks | databases | messaging | ai_ml | infra | testing
    verdict: str        # "correct" | "false_positive" | "false_negative"
    reason: Optional[str] = None


class StackFeedbackRequest(BaseModel):
    tech_evaluations: list[TechEvaluation]
    missing_techs: Optional[list[dict]] = []
    # [{tech_name, category}]
    overall_stack_correct: Optional[bool] = None
    notes: Optional[str] = None


class PrimaryLanguageCorrection(BaseModel):
    primary_language: str


class StackFeedbackResponse(BaseModel):
    accepted: bool
    analysis_id: str
    repo_key: str
    evaluations_processed: int
    patterns_updated: int
    new_patterns_discovered: int
    summary: dict


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
        category  = missing.get("category", "infra")
        if tech_name:
            result = await stack_feedback_service.register_missing_tech(
                tech_name, category, repo_key
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


@router.post("/{id}/tech/{tech_name}/correct")
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


@router.post("/{id}/tech/{tech_name}/wrong")
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


@router.post("/{id}/tech/{tech_name}/missing")
async def mark_tech_missing(
    id: str,
    tech_name: str,
    category: str = "infra",
    repo_key: str = Depends(resolve_repo_key),
):
    """Report a technology present in repo but not detected."""
    doc = await storage_service.get_repo(repo_key)
    if not doc:
        raise HTTPException(404, "no analysis for this repo")
    result = await stack_feedback_service.register_missing_tech(
        tech_name, category, repo_key
    )
    feedback_doc = {
        "missing_techs": [{"tech_name": tech_name, "category": category}],
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


@router.post("/{id}/primary-language")
async def correct_primary_language(
    id: str,
    correction: PrimaryLanguageCorrection,
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
