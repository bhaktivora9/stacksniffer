"""
backend/routers/feedback.py

Domain-level RLHF data collection.
Java equivalent: part of AnalysisController + UsageTrackingServiceImpl

Collects human preference signal on domain classification correctness.
Signal flows to learning_service.py for pattern confidence updates.
Mirrors UsageTrackingEvent → IngestionEventListener in stacksniffer-learning.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from backend.services import storage_service as storage_service
from backend.services.learning_service import penalize_patterns, reward_patterns

router = APIRouter(prefix="/api/feedback", tags=["feedback"])


class FeedbackRequest(BaseModel):
    domain_correct: bool
    correct_domain: Optional[str] = None
    confidence_felt: Optional[int] = None   # 1-5 user-perceived confidence
    techs_wrong: Optional[list[str]] = []
    techs_missing: Optional[list[str]] = []
    notes: Optional[str] = None


class FeedbackResponse(BaseModel):
    accepted: bool
    analysis_id: str
    patterns_updated: int
    message: str


@router.post("/{analysis_id}", response_model=FeedbackResponse)
async def submit_feedback(analysis_id: str, feedback: FeedbackRequest):
    """
    Submit domain-level correctness signal.

    RLHF mechanism:
      domain_correct=True  → reward: increase confidence of patterns that fired
      domain_correct=False → penalty: decrease confidence + store correct label
      techs_wrong          → penalize specific false positive patterns
      techs_missing        → store for pattern discovery pipeline

    Java equivalent: UsageTrackingService.trackBatchUsages()
    triggered by UsageTrackingEvent in IngestionEventListener.
    """
    analysis = await storage_service.get_analysis(analysis_id)
    if not analysis:
        raise HTTPException(404, "Analysis not found")

    await storage_service.store_feedback(analysis_id, {
        "domain_correct":        feedback.domain_correct,
        "correct_domain":        feedback.correct_domain,
        "confidence_felt":       feedback.confidence_felt,
        "techs_wrong":           feedback.techs_wrong or [],
        "techs_missing":         feedback.techs_missing or [],
        "notes":                 feedback.notes,
        "detected_domain":       analysis["stack"]["domain"],
        "ai_classification_used": analysis["stack"]["ai_classification_used"],
        "created_at":            datetime.utcnow().isoformat(),
    })

    pattern_matches = analysis["stack"].get("pattern_matches", [])
    patterns_updated = 0

    if feedback.domain_correct and not feedback.techs_wrong:
        patterns_updated = await reward_patterns(pattern_matches)
    elif not feedback.domain_correct or feedback.techs_wrong:
        patterns_updated = await penalize_patterns(
            pattern_matches,
            wrong_techs=feedback.techs_wrong or []
        )

    return FeedbackResponse(
        accepted=True,
        analysis_id=analysis_id,
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
            "message": "No feedback collected yet. Use UI thumbs up/down on domain classifications."
        }

    total   = len(all_feedback)
    correct = sum(1 for f in all_feedback if f.get("domain_correct"))

    from collections import defaultdict
    domain_stats = defaultdict(lambda: {"correct": 0, "total": 0})
    for f in all_feedback:
        detected = f.get("detected_domain", "unknown")
        domain_stats[detected]["total"] += 1
        if f.get("domain_correct"):
            domain_stats[detected]["correct"] += 1

    return {
        "total_feedback":    total,
        "domain_accuracy":   f"{correct/total*100:.0f}%",
        "correct":           correct,
        "incorrect":         total - correct,
        "per_domain": {
            domain: {
                "accuracy": f"{s['correct']/s['total']*100:.0f}%",
                "samples":  s["total"]
            }
            for domain, s in domain_stats.items()
        },
        "training_ready":    total >= 10,
        "classifier_ready":  total >= 50,
        "message": (
            f"{total} feedback samples. "
            f"{'Ready for pattern update.' if total >= 10 else f'Need {10-total} more for pattern update.'} "
            f"{'Ready for classifier training.' if total >= 50 else f'Need {50-total} more for classifier.'}"
        )
    }