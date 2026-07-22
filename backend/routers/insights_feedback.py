"""
backend/routers/insights_feedback.py

Collects quality signal for generate_stack_insights() output.
This is the data collection layer for future fine-tuning.

What it does:
  1. Stores star ratings + replacement text (gold labels) in MongoDB
  2. Stores user corrections outside the mutable analysis document
  3. Re-embeds the corrected analysis so RAG retrieves updated context
  4. Exports training datasets (supervised + DPO pairs)

Background jobs triggered on submission:
  - upsert_correction() — rewrites stack fields in MongoDB analyses
  - _reapply_and_reembed()        — re-generates embedding with corrected fields
    → similar repos panel reflects corrections on next load
    → future analyses of similar repos get correct few-shot context

Java equivalent: UsageTrackingServiceImpl in stacksniffer-learning
"""
import asyncio
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import backend.services.storage_service as storage_service
from backend.routers.deps import resolve_repo_key
from backend.services.embedding_service import embed_stack

router = APIRouter(prefix="/api/insights-feedback", tags=["insights-feedback"])


# ── Request / response models ─────────────────────────────────────────────────

class InsightsFeedbackRequest(BaseModel):
    """
    Quality signal for AI architectural insights.

    Explicit path:  user clicks star rating in UI
    Implicit path:  genREADME reports whether user kept the insights

    replacement_text IS the training label — what the user wrote instead
    is exactly what a fine-tuned model should produce.
    """
    quality_score:    Optional[int]  = None   # 1-5, None if implicit only
    accepted:         bool           = True   # False = user replaced content
    edited_fields:    list[str]      = []     # ["why_this_stack", "stack_pattern"]
    replacement_text: Optional[dict] = None   # {field: improved_text} — gold label
    source:           str            = "ui"   # "ui" | "genreadme" | "api"
    notes:            Optional[str]  = None


class QualityCriteria(BaseModel):
    field:         str
    good_signals:  list[str] = []
    bad_signals:   list[str] = []
    good_examples: list[str] = []
    bad_examples:  list[str] = []


# ── Submit feedback ───────────────────────────────────────────────────────────

@router.post("/{id}")
async def submit_insights_feedback(
    id: str,
    feedback: InsightsFeedbackRequest,
    repo_key: str = Depends(resolve_repo_key),
):
    """Submit quality rating for AI insights on an analysis."""
    doc = await storage_service.get_repo(repo_key)
    if not doc:
        raise HTTPException(404, "no analysis for this repo")

    stack = doc["stack"]

    implicit_score = None
    if feedback.quality_score is None:
        if not feedback.accepted and feedback.replacement_text:
            implicit_score = 1
        elif feedback.accepted and not feedback.edited_fields:
            implicit_score = 5
        elif feedback.accepted and len(feedback.edited_fields) == 1:
            implicit_score = 4
        elif feedback.accepted and len(feedback.edited_fields) >= 2:
            implicit_score = 3

    final_score = feedback.quality_score or implicit_score
    original_insights = {
        "why_this_stack": stack.get("why_this_stack", ""),
        "stack_pattern": stack.get("stack_pattern", ""),
        "ecosystem_context": stack.get("ecosystem_context", ""),
        "notable_combinations": stack.get("notable_combinations", []),
    }
    feedback_doc = {
        "quality_score": final_score,
        "explicit_score": feedback.quality_score,
        "implicit_score": implicit_score,
        "accepted": feedback.accepted,
        "edited_fields": feedback.edited_fields,
        "replacement_text": feedback.replacement_text,
        "source": feedback.source,
        "notes": feedback.notes,
        "original_insights": original_insights,
        "repo_key": repo_key,
        "domain": stack.get("domain", "unknown"),
        "created_at": datetime.utcnow().isoformat(),
        "training_label": feedback.replacement_text or (
            original_insights if final_score and final_score >= 4 else None
        ),
        "is_training_positive": final_score >= 4 if final_score else None,
        "is_training_negative": final_score <= 2 if final_score else None,
    }

    for field, value in (feedback.replacement_text or {}).items():
        await storage_service.upsert_correction(repo_key, field, value)

    await storage_service.store_feedback(
        repo_key=repo_key,
        commit_sha=doc["commit_sha"],
        pipeline_version=doc["pipeline_version"],
        rated_output=stack,
        rated_embedding=doc.get("stack_embedding"),
        feedback={"type": "insights_feedback", **feedback_doc},
    )
    await storage_service.store_insights_feedback(id, feedback_doc)

    analysis_updated = False
    if feedback.replacement_text:
        analysis_updated = True
        asyncio.create_task(_reapply_and_reembed(repo_key))

    return {
        "accepted": True,
        "request_id": id,
        "repo_key": repo_key,
        "final_score": final_score,
        "analysis_updated": analysis_updated,
        "training_value": (
            "gold_label" if feedback.replacement_text else
            "positive" if (final_score or 0) >= 4 else
            "negative" if (final_score or 0) <= 2 else
            "neutral"
        ),
        "message": (
            f"Insight quality recorded (score={final_score}). "
            f"{'Gold training label saved. Corrections queued.' if feedback.replacement_text else ''}"
        ),
    }


# ── Background re-embed ───────────────────────────────────────────────────────

async def _reapply_and_reembed(repo_key: str):
    doc = await storage_service.get_repo(repo_key)
    if not doc:
        return
    stack, touched = await storage_service.apply_corrections(repo_key, doc["stack"])
    if not touched:
        return
    emb = await embed_stack(stack)
    if not any(v != 0.0 for v in emb):
        return
    await storage_service.upsert_repo_analysis(
        repo_key,
        stack,
        emb,
        doc["commit_sha"],
        doc["pipeline_version"],
    )


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.get("/stats")
async def insights_feedback_stats():
    """
    Training dataset readiness summary.
    Shows how many high/low quality examples have been collected.
    """
    all_feedback = await storage_service.get_all_insights_feedback()

    if not all_feedback:
        return {
            "total":   0,
            "message": "No insights feedback yet. Rate AI insights in the UI.",
        }

    total    = len(all_feedback)
    positive = sum(1 for f in all_feedback if f.get("is_training_positive"))
    negative = sum(1 for f in all_feedback if f.get("is_training_negative"))
    gold     = sum(
        1 for f in all_feedback
        if f.get("training_label") and f.get("replacement_text")
    )

    scores    = [f["quality_score"] for f in all_feedback if f.get("quality_score")]
    avg_score = sum(scores) / len(scores) if scores else 0

    by_domain: dict[str, dict] = {}
    for f in all_feedback:
        domain = f.get("domain", "unknown")
        if domain not in by_domain:
            by_domain[domain] = {"positive": 0, "negative": 0, "total": 0}
        by_domain[domain]["total"] += 1
        if f.get("is_training_positive"):
            by_domain[domain]["positive"] += 1
        if f.get("is_training_negative"):
            by_domain[domain]["negative"] += 1

    need_more = max(0, 200 - positive)
    return {
        "total":             total,
        "positive_examples": positive,
        "negative_examples": negative,
        "gold_labels":       gold,
        "avg_quality_score": round(avg_score, 2),
        "by_domain":         by_domain,
        "finetuning_ready":  positive >= 200,
        "message": (
            f"{positive} positive, {negative} negative, {gold} gold labels. "
            f"{'Ready for fine-tuning!' if positive >= 200 else f'Need {need_more} more positive examples.'}"
        ),
    }


# ── Quality criteria ──────────────────────────────────────────────────────────

@router.get("/quality-criteria")
async def get_quality_criteria():
    """
    Returns quality criteria from MongoDB.
    Updated without code changes via PUT /api/insights-feedback/quality-criteria/{field}.
    Seed via: python seed_quality_criteria.py
    """
    criteria = await storage_service.get_quality_criteria()
    if not criteria:
        return {
            "message":  "No criteria seeded yet. Run: python seed_quality_criteria.py",
            "criteria": [],
        }
    return {"criteria": criteria, "total": len(criteria)}


@router.put("/quality-criteria/{field}")
async def update_quality_criterion(field: str, criterion: QualityCriteria):
    """
    Update quality criterion for a specific insights field.
    Takes effect on the next analysis — no restart needed.
    """
    await storage_service.update_quality_criterion(field, criterion.model_dump())
    return {"updated": True, "field": field}


# ── Training data export ──────────────────────────────────────────────────────

@router.post("/export-training-data")
async def export_training_data(min_quality: int = 4):
    """
    Export training datasets for fine-tuning generate_stack_insights().

    Returns two datasets:
      training_data — supervised examples (input → gold output)
                      requires replacement_text so each input has a known target
      dpo_pairs     — DPO pairs (rejected + chosen) for preference training
                      useful when user rated poorly AND provided improvement

    Low-score feedback WITHOUT replacement_text is skipped entirely —
    no positive label means it cannot be used for supervised fine-tuning.
    """
    all_feedback = await storage_service.get_all_insights_feedback()

    training_data:   list[dict] = []
    dpo_pairs:       list[dict] = []
    skipped_no_label = 0

    for fb in all_feedback:
        score       = fb.get("quality_score") or 0
        replacement = fb.get("replacement_text")
        original    = fb.get("original_insights")

        if not original:
            continue

        if score >= min_quality and replacement:
            # Gold supervised example — high quality + user improvement = best signal
            training_data.append({
                "input":   _build_input(fb),
                "output":  replacement,
                "score":   score,
                "source":  fb.get("source"),
                "domain":  fb.get("domain"),
                "is_gold": True,
            })
        elif score <= 2 and replacement:
            # DPO pair — rejected (Gemini original) + chosen (user improvement)
            dpo_pairs.append({
                "input":    _build_input(fb),
                "rejected": original,
                "chosen":   replacement,
                "score":    score,
                "source":   fb.get("source"),
                "domain":   fb.get("domain"),
            })
        elif score <= 2 and not replacement:
            # Negative signal without positive label — skip for supervised training
            skipped_no_label += 1

    return {
        "total":             len(training_data),
        "gold_labels":       len(training_data),
        "dpo_pairs_count":   len(dpo_pairs),
        "skipped_no_label":  skipped_no_label,
        "ready":             len(training_data) >= 200,
        "training_data":     training_data[:10],   # preview first 10
        "dpo_pairs":         dpo_pairs[:10],
        "message": (
            f"{len(training_data)} supervised gold examples, "
            f"{len(dpo_pairs)} DPO pairs, "
            f"{skipped_no_label} no-label negatives skipped. "
            f"{'Ready for fine-tuning.' if len(training_data) >= 200 else f'Need {200 - len(training_data)} more.'}"
        ),
    }


# ── Training input builder ────────────────────────────────────────────────────

def _build_input(feedback: dict) -> str:
    """
    Build the input prompt text for a training example.
    This is what the model receives — (repo context + domain) → insights.
    """
    return (
        f"Repository: {feedback.get('repo_name', '')}\n"
        f"Domain: {feedback.get('domain', 'unknown')}\n"
        f"Generate architectural insights.\n"
        f"Return JSON with why_this_stack, stack_pattern, "
        f"ecosystem_context, notable_combinations."
    )
