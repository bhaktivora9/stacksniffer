"""
backend/routers/insights_feedback.py

Collects quality signal for generate_stack_insights() output.
This is the data collection layer for future fine-tuning.

Java equivalent: Would be part of UsageTrackingServiceImpl
in stacksniffer-learning, tracking insight quality as a
separate signal from domain correctness.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import asyncio
from datetime import datetime

from backend.services import storage_service as storage_service

router = APIRouter(prefix="/api/insights-feedback", tags=["insights-feedback"])


class InsightsFeedbackRequest(BaseModel):
    """
    Quality signal for AI architectural insights.

    Explicit path: user clicks star rating in UI
    Implicit path: genREADME reports whether user kept the insights
    """
    quality_score:    Optional[int] = None    # 1-5, None if implicit only
    accepted:         bool = True             # False = user replaced content
    edited_fields:    list[str] = []          # ["why_this_stack", "stack_pattern"]
    replacement_text: Optional[dict] = None  # {field: improved_text}
    # ^ this IS the training label — what good looks like
    source:           str = "ui"             # "ui" | "genreadme" | "api"
    notes:            Optional[str] = None


class QualityCriteria(BaseModel):
    field:       str
    good_signals: list[str] = []
    bad_signals:  list[str] = []
    good_examples: list[str] = []
    bad_examples:  list[str] = []


@router.post("/{analysis_id}")
async def submit_insights_feedback(
    analysis_id: str,
    feedback: InsightsFeedbackRequest
):
    """
    Submit quality rating for AI insights on an analysis.

    Called by:
      1. Frontend star rating component on AI insights card
      2. genREADME after user accepts or edits the architecture section

    Scoring logic:
      accepted=True,  edited_fields=[]         → implicit score 5
      accepted=True,  edited_fields has 1 item → implicit score 4
      accepted=True,  edited_fields has 2+     → implicit score 3
      accepted=False, replacement_text present → implicit score 1 (most valuable training signal)
      quality_score provided                   → use explicit score

    replacement_text is the gold label — what the user wrote instead
    is exactly what a fine-tuned model should produce.
    """
    analysis = await storage_service.get_analysis(analysis_id)
    if not analysis:
        raise HTTPException(404, "Analysis not found")

    # Compute implicit score if explicit not provided
    implicit_score = None
    if feedback.quality_score is None:
        if not feedback.accepted and feedback.replacement_text:
            implicit_score = 1   # user replaced it entirely — poor quality
        elif feedback.accepted and not feedback.edited_fields:
            implicit_score = 5   # kept unchanged — high quality
        elif feedback.accepted and len(feedback.edited_fields) == 1:
            implicit_score = 4
        elif feedback.accepted and len(feedback.edited_fields) >= 2:
            implicit_score = 3

    final_score = feedback.quality_score or implicit_score

    # Extract original insights for the training record
    stack = analysis.get("stack", {})
    original_insights = {
        "why_this_stack":       stack.get("why_this_stack", ""),
        "stack_pattern":        stack.get("stack_pattern", ""),
        "ecosystem_context":    stack.get("ecosystem_context", ""),
        "notable_combinations": stack.get("notable_combinations", []),
    }

    doc = {
        "analysis_id":      analysis_id,
        "quality_score":    final_score,
        "explicit_score":   feedback.quality_score,
        "implicit_score":   implicit_score,
        "accepted":         feedback.accepted,
        "edited_fields":    feedback.edited_fields,
        "replacement_text": feedback.replacement_text,
        "source":           feedback.source,
        "notes":            feedback.notes,
        "original_insights": original_insights,
        "repo_name":        analysis.get("repo", {}).get("full_name", ""),
        "domain":           stack.get("domain", "unknown"),
        "created_at":       datetime.utcnow().isoformat(),
        # Training label: if user replaced, that text is the gold label
        "training_label":   feedback.replacement_text or (
            original_insights if final_score and final_score >= 4 else None
        ),
        "is_training_positive": final_score >= 4 if final_score else None,
        "is_training_negative": final_score <= 2 if final_score else None,
    }

    await storage_service.store_insights_feedback(analysis_id, doc)
    analysis_updated = False
    if feedback.replacement_text:
        analysis_updated = await storage_service.update_analysis_insights(
            analysis_id,
            feedback.replacement_text,
        )
        if analysis_updated:
            print(f"[insights_feedback] Rewrote analysis {analysis_id} with corrected insights")
            asyncio.create_task(_reembed_analysis(analysis_id, feedback.replacement_text))

    return {
        "accepted":       True,
        "analysis_id":    analysis_id,
        "final_score":    final_score,
        "analysis_updated": analysis_updated,
        "training_value": (
            "gold_label"   if feedback.replacement_text else
            "positive"     if (final_score or 0) >= 4 else
            "negative"     if (final_score or 0) <= 2 else
            "neutral"
        ),
        "message": (
            f"Insight quality recorded (score={final_score}). "
            f"{'Gold training label saved.' if feedback.replacement_text else ''}"
        )
    }


@router.get("/stats")
async def insights_feedback_stats():
    """
    Training dataset readiness summary.
    Shows how many high/low quality examples have been collected.
    """
    all_feedback = await storage_service.get_all_insights_feedback()

    if not all_feedback:
        return {
            "total": 0,
            "message": "No insights feedback yet. Rate AI insights in the UI."
        }

    total    = len(all_feedback)
    positive = sum(1 for f in all_feedback if f.get("is_training_positive"))
    negative = sum(1 for f in all_feedback if f.get("is_training_negative"))
    gold     = sum(1 for f in all_feedback if f.get("training_label") and
                   f.get("replacement_text"))

    scores = [f["quality_score"] for f in all_feedback if f.get("quality_score")]
    avg_score = sum(scores) / len(scores) if scores else 0

    by_domain = {}
    for f in all_feedback:
        domain = f.get("domain", "unknown")
        if domain not in by_domain:
            by_domain[domain] = {"positive": 0, "negative": 0, "total": 0}
        by_domain[domain]["total"] += 1
        if f.get("is_training_positive"):
            by_domain[domain]["positive"] += 1
        if f.get("is_training_negative"):
            by_domain[domain]["negative"] += 1

    return {
        "total":              total,
        "positive_examples":  positive,
        "negative_examples":  negative,
        "gold_labels":        gold,
        "avg_quality_score":  round(avg_score, 2),
        "by_domain":          by_domain,
        "finetuning_ready":   positive >= 200,
        "message": (
            f"{positive} positive, {negative} negative, {gold} gold labels. "
            f"{'Ready for fine-tuning.' if positive >= 200 else f'Need {200 - positive} more positive examples.'}"
        )
    }


@router.get("/quality-criteria")
async def get_quality_criteria():
    """
    Returns quality criteria from MongoDB.
    Updated without code changes via direct MongoDB edit or admin endpoint.
    """
    criteria = await storage_service.get_quality_criteria()
    if not criteria:
        return {
            "message": "No criteria seeded yet. Run: python seed_quality_criteria.py",
            "criteria": []
        }
    return {"criteria": criteria, "total": len(criteria)}


@router.put("/quality-criteria/{field}")
async def update_quality_criterion(field: str, criterion: QualityCriteria):
    """
    Update quality criterion for a specific insights field.
    No code change or restart needed.
    """
    await storage_service.update_quality_criterion(field, criterion.model_dump())
    return {"updated": True, "field": field}


@router.post("/export-training-data")
async def export_training_data(min_quality: int = 4):
    """
    Export training datasets for tuning.

    Supervised examples require replacement_text so each input has a known
    desired output. Low-score feedback without a replacement is useful as a
    quality signal, but not as a supervised target.
    """
    all_feedback = await storage_service.get_all_insights_feedback()

    training_data = []
    dpo_pairs = []
    skipped_no_label = 0
    for fb in all_feedback:
        score = fb.get("quality_score") or 0
        replacement = fb.get("replacement_text")
        if not fb.get("original_insights"):
            continue

        if score >= min_quality and replacement:
            training_data.append({
                "input":   _build_input(fb),
                "output":  replacement,
                "score":   score,
                "source":  fb.get("source"),
                "domain":  fb.get("domain"),
                "is_gold": True,
            })
        elif score <= 2 and replacement:
            dpo_pairs.append({
                "input":    _build_input(fb),
                "rejected": fb.get("original_insights"),
                "chosen":   replacement,
                "score":    score,
                "source":   fb.get("source"),
                "domain":   fb.get("domain"),
            })
        elif score <= 2 and not replacement:
            skipped_no_label += 1

    return {
        "total":          len(training_data),
        "gold_labels":    len(training_data),
        "dpo_pairs_count": len(dpo_pairs),
        "skipped_no_label": skipped_no_label,
        "ready":          len(training_data) >= 200,
        "training_data":  training_data[:10],  # preview first 10
        "dpo_pairs":      dpo_pairs[:10],
        "message": (
            f"{len(training_data)} supervised gold examples, "
            f"{len(dpo_pairs)} DPO pairs, {skipped_no_label} no-label negatives skipped. "
            f"Download full dataset via /api/insights-feedback/export-training-data?min_quality=4"
        )
    }


def _build_input(feedback: dict) -> str:
    """Build the input prompt text for a training example."""
    insights = feedback.get("original_insights", {})
    return (
        f"Repository: {feedback.get('repo_name', '')}\n"
        f"Domain: {feedback.get('domain', 'unknown')}\n"
        f"Generate architectural insights.\n"
        f"Return JSON with why_this_stack, stack_pattern, "
        f"ecosystem_context, notable_combinations."
    )


async def _reembed_analysis(analysis_id: str, corrections: dict) -> None:
    """Re-embed a corrected analysis so vector search reflects gold labels."""
    try:
        analysis = await storage_service.get_analysis(analysis_id)
        if not analysis:
            return

        stack = analysis.get("stack", {})
        notable_combinations = corrections.get(
            "notable_combinations",
            stack.get("notable_combinations", []),
        )
        if isinstance(notable_combinations, str):
            notable_combinations = [
                item.strip()
                for item in notable_combinations.splitlines()
                if item.strip()
            ]
        enriched = {
            **{
                category: stack.get(category, [])
                for category in [
                    "languages",
                    "frameworks",
                    "databases",
                    "messaging",
                    "ai_ml",
                    "infra",
                    "testing",
                    "library",
                ]
            },
            "domain": stack.get("domain", "unknown"),
            "architecture_style": stack.get("architecture_style", "unknown"),
            "stack_pattern": corrections.get("stack_pattern", stack.get("stack_pattern", "")),
            "why_this_stack": corrections.get("why_this_stack", stack.get("why_this_stack", "")),
            "ecosystem_context": corrections.get("ecosystem_context", stack.get("ecosystem_context", "")),
            "notable_combinations": notable_combinations,
        }

        from backend.services.embedding_service import embed_stack, is_valid_embedding

        new_embedding = await embed_stack(enriched, task_type="retrieval_document")
        if is_valid_embedding(new_embedding):
            await storage_service.store_analysis_with_embedding(
                analysis_id,
                analysis,
                new_embedding,
            )
            print(f"[insights_feedback] Re-embedded {analysis_id} with corrections")
    except Exception as e:
        print(f"[insights_feedback] Re-embed failed: {e}")
