"""
backend/routers/feedback.py

SoftwareType-level RLHF data collection.
Java equivalent: part of AnalysisController + UsageTrackingServiceImpl

Collects human preference signal on software_type classification correctness.
Signal flows to learning_service.py for pattern confidence updates.
Mirrors UsageTrackingEvent → IngestionEventListener in stacksniffer-learning.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from backend.services import storage_service as storage_service
from backend.services.learning_service import penalize_patterns, reward_patterns
from backend.routers.deps import resolve_repo_key

router = APIRouter(prefix="/api/feedback", tags=["feedback"])


class FeedbackRequest(BaseModel):
    software_type_correct: bool
    correct_software_type: Optional[str] = None
    approved_overlay_confirmed: bool = False
    confidence_felt: Optional[int] = None   # 1-5 user-perceived confidence
    techs_wrong: Optional[list[str]] = []
    techs_missing: Optional[list[str]] = []
    notes: Optional[str] = None


class FeedbackResponse(BaseModel):
    accepted: bool
    analysis_id: str
    repo_key: str
    patterns_updated: int
    message: str


@router.post("/{id:path}", response_model=FeedbackResponse)
async def submit_feedback(
    id: str,
    feedback: FeedbackRequest,
    repo_key: str = Depends(resolve_repo_key),
):
    """
    Submit software_type-level correctness signal.

    RLHF mechanism:
      software_type_correct=True  → reward: increase confidence of patterns that fired
      software_type_correct=False → penalty: decrease confidence + store correct label
      techs_wrong          → penalize specific false positive patterns
      techs_missing        → store for pattern discovery pipeline

    Java equivalent: UsageTrackingService.trackBatchUsages()
    triggered by UsageTrackingEvent in IngestionEventListener.
    """
    doc = await storage_service.get_repo(repo_key)
    if not doc:
        raise HTTPException(404, "no analysis for this repo")
    stack = doc["stack"]
    approved_overlay = None
    if feedback.approved_overlay_confirmed:
        approved_overlay = await storage_service.get_approved_software_type_correction(
            repo_key, stack.get("software_type"),
        )
        if (
            not approved_overlay
            or approved_overlay.get("corrected_value") != feedback.correct_software_type
        ):
            raise HTTPException(409, "approved software_type overlay no longer matches")
    if not feedback.software_type_correct:
        if not feedback.correct_software_type:
            raise HTTPException(422, "correct_software_type is required when software_type_correct is false")
        if not await storage_service.is_valid_software_type(feedback.correct_software_type):
            raise HTTPException(422, f"invalid software_type: {feedback.correct_software_type}")

    feedback_doc = {
        "software_type_correct":        feedback.software_type_correct,
        "correct_software_type":        feedback.correct_software_type,
        "approved_overlay_confirmed":   feedback.approved_overlay_confirmed,
        "confidence_felt":       feedback.confidence_felt,
        "techs_wrong":           feedback.techs_wrong or [],
        "techs_missing":         feedback.techs_missing or [],
        "notes":                 feedback.notes,
        "detected_software_type":       stack["software_type"],
        "ai_classification_used": stack["ai_classification_used"],
        "created_at":            datetime.utcnow().isoformat(),
    }
    await storage_service.store_feedback(
        repo_key=repo_key,
        commit_sha=doc["commit_sha"],
        pipeline_version=doc["pipeline_version"],
        rated_output=stack,
        rated_embedding=doc.get("stack_embedding"),
        feedback=feedback_doc,
    )
    if not feedback.software_type_correct and not feedback.approved_overlay_confirmed:
        assignment_method = "deterministic" if stack.get("layer0_prediction") else "ai_inferred"
        await storage_service.upsert_software_type_correction(
            pipeline_value=stack.get("software_type", "unknown"),
            corrected_value=feedback.correct_software_type,
            evidence={
                "repo": (doc.get("repo") or {}).get("full_name"),
                "repo_key": repo_key,
                "analysis_id": id,
                "source": "user",
                "seen_count": 1,
            },
            assignment_method=assignment_method,
            source="user",
        )

    pattern_matches = stack.get("pattern_matches", [])
    patterns_updated = 0

    if feedback.software_type_correct and not feedback.techs_wrong:
        patterns_updated = await reward_patterns(pattern_matches)
    elif not feedback.software_type_correct or feedback.techs_wrong:
        patterns_updated = await penalize_patterns(
            pattern_matches,
            wrong_techs=feedback.techs_wrong or []
        )

    return FeedbackResponse(
        accepted=True,
        analysis_id=id,
        repo_key=repo_key,
        patterns_updated=patterns_updated,
        message=(
            f"Feedback recorded. {patterns_updated} pattern confidence scores updated. "
            f"Contributes to RLHF training corpus."
        )
    )


@router.get("/stats")
async def feedback_stats():
    """
    Summary of collected RLHF data.
    Java equivalent: MonitoringStats DTO in HealthController.
    """
    all_feedback = await storage_service.get_all_feedback()

    if not all_feedback:
        return {
            "total": 0,
            "message": "No feedback collected yet. Use UI thumbs up/down on software_type classifications."
        }

    total   = len(all_feedback)
    correct = sum(1 for f in all_feedback if f.get("software_type_correct"))

    from collections import defaultdict
    software_type_stats = defaultdict(lambda: {"correct": 0, "total": 0})
    for f in all_feedback:
        detected = f.get("detected_software_type", "unknown")
        software_type_stats[detected]["total"] += 1
        if f.get("software_type_correct"):
            software_type_stats[detected]["correct"] += 1

    return {
        "total_feedback":    total,
        "software_type_accuracy":   f"{correct/total*100:.0f}%",
        "correct":           correct,
        "incorrect":         total - correct,
        "per_software_type": {
            software_type: {
                "accuracy": f"{s['correct']/s['total']*100:.0f}%",
                "samples":  s["total"]
            }
            for software_type, s in software_type_stats.items()
        },
        "training_ready":    total >= 10,
        "classifier_ready":  total >= 50,
        "message": (
            f"{total} feedback samples. "
            f"{'Ready for pattern update.' if total >= 10 else f'Need {10-total} more for pattern update.'} "
            f"{'Ready for classifier training.' if total >= 50 else f'Need {50-total} more for classifier.'}"
        )
    }
