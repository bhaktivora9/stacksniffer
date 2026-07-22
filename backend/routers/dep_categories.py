"""
backend/routers/dep_categories.py

Feedback loop for emergent dependency categories discovered by Gemini.

Three feedback actions:
  discard  — category is noise/wrong, exclude from future classification
  merge    — category is valid but should map to an existing standard category
  promote  — category is valid and should become a first-class standard category

Gemini reads the feedback decisions on every classify_dependencies() call
via _build_category_feedback_context(), ensuring it doesn't re-emit
discarded categories or re-create merged ones.

MongoDB collections:
  dep_categories       — all known categories (standard + emergent)
  dep_category_feedback — human decisions on emergent categories

Java equivalent: DynamicPatternConfigService in stacksniffer-learning
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import backend.services.storage_service as storage_service

router = APIRouter(prefix="/api/dep-categories", tags=["dep-categories"])


# ── Request models ────────────────────────────────────────────────────────────

class CategoryFeedback(BaseModel):
    action:      str              # "discard" | "merge" | "promote"
    merge_into:  Optional[str] = None   # required when action == "merge"
    reason:      Optional[str] = None   # human-readable note
    source:      str = "ui"


class CategoryUpdate(BaseModel):
    display_name: Optional[str] = None
    description:  Optional[str] = None
    color:        Optional[str] = None  # hex color for UI pill


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
async def list_dep_categories():
    """
    All known dep categories — standard + emergent.
    Includes feedback status for each emergent category.
    Used by frontend to render tech pills and by dep_classifier prompt.
    """
    return await storage_service.get_dep_categories()


@router.get("/feedback-context")
async def get_feedback_context():
    """
    Returns the feedback decisions formatted for injection into
    the dep_classifier Gemini prompt. Called by dep_classifier.py
    _build_category_feedback_context() on each analysis.

    Format:
    {
      "discarded": ["bundler", "state_management"],
      "merged":    {"css_framework": "library", "task_runner": "infra"},
      "promoted":  ["observability", "auth"]
    }
    """
    decisions = await storage_service.get_category_feedback_decisions()
    return decisions


@router.post("/{category}/feedback")
async def submit_category_feedback(
    category: str,
    feedback: CategoryFeedback,
):
    """
    Submit a feedback decision for an emergent category.

    action=discard:
      Category is noise, wrong, or too granular.
      Gemini will be told NOT to emit this category.
      All techs currently tagged with it are re-tagged as "library".

    action=merge:
      Category is valid but maps to an existing standard.
      Requires merge_into: "library" | "infra" | "testing" | etc.
      Gemini will map this category to merge_into in future.
      Example: "css_framework" → merge_into="library"

    action=promote:
      Category is valid and should be a first-class standard.
      Adds to standard set in dep_categories collection.
      Gemini will continue emitting it and it renders as a proper panel.
      Example: "bundler" promoted → gets its own UI section.
    """
    VALID_ACTIONS = {"discard", "merge", "promote"}
    standard_categories = await storage_service.get_valid_categories()

    if feedback.action not in VALID_ACTIONS:
        raise HTTPException(400, f"action must be one of: {VALID_ACTIONS}")

    if feedback.action == "merge":
        if not feedback.merge_into:
            raise HTTPException(400, "merge_into required when action=merge")
        if feedback.merge_into not in standard_categories:
            raise HTTPException(
                400,
                f"merge_into must be a standard category: {standard_categories}"
            )

    if feedback.action == "promote" and category in standard_categories:
        raise HTTPException(400, f"{category} is already a standard category")

    result = await storage_service.store_category_feedback(
        category   = category,
        action     = feedback.action,
        merge_into = feedback.merge_into,
        reason     = feedback.reason,
        source     = feedback.source,
    )

    # Apply action immediately to existing techs in MongoDB
    if feedback.action == "discard":
        await storage_service.reclassify_category_techs(
            from_category = category,
            to_category   = None,   # None = remove from output
        )
    elif feedback.action == "merge":
        await storage_service.reclassify_category_techs(
            from_category = category,
            to_category   = feedback.merge_into,
        )
    elif feedback.action == "promote":
        await storage_service.promote_category(category)

    return {
        "category": category,
        "action":   feedback.action,
        "applied":  True,
        "message":  _feedback_message(category, feedback),
    }


@router.delete("/{category}/feedback")
async def undo_category_feedback(category: str):
    """
    Undo a feedback decision — restores category to pending state.
    Discarded categories re-appear as emergent.
    Merged categories revert to their own category.
    Promoted categories revert to emergent.
    """
    await storage_service.delete_category_feedback(category)
    return {"category": category, "status": "reverted to pending"}


@router.patch("/{category}")
async def update_category_metadata(category: str, update: CategoryUpdate):
    """
    Update display name, description, or UI color for a category.
    Used to give emergent categories human-readable labels before promoting.
    Example: category="bundler" → display_name="Build Bundler", color="#F97316"
    """
    await storage_service.update_category_metadata(
        category     = category,
        display_name = update.display_name,
        description  = update.description,
        color        = update.color,
    )
    return {"category": category, "updated": True}


@router.get("/stats")
async def category_stats():
    """
    Statistics on emergent categories to guide feedback decisions.
    Shows how many repos + techs each emergent category has.
    """
    categories = await storage_service.get_dep_categories()
    decisions  = await storage_service.get_category_feedback_decisions()

    discarded = set(decisions.get("discarded", []))
    merged    = decisions.get("merged", {})
    promoted  = set(decisions.get("promoted", []))

    emergent = [
        c for c in categories
        if not c.get("standard")
    ]

    return {
        "total_emergent":  len(emergent),
        "pending_review":  len([c for c in emergent
                                if c["category"] not in discarded
                                and c["category"] not in merged
                                and c["category"] not in promoted]),
        "discarded":       len(discarded),
        "merged":          len(merged),
        "promoted":        len(promoted),
        "categories":      [
            {
                **c,
                "status": (
                    "discarded" if c["category"] in discarded else
                    f"merged → {merged[c['category']]}" if c["category"] in merged else
                    "promoted" if c["category"] in promoted else
                    "pending"
                )
            }
            for c in emergent
        ],
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _feedback_message(category: str, feedback: CategoryFeedback) -> str:
    if feedback.action == "discard":
        return (
            f"'{category}' discarded. Gemini will not emit this category. "
            f"Existing techs tagged '{category}' have been removed from output."
        )
    elif feedback.action == "merge":
        return (
            f"'{category}' merged into '{feedback.merge_into}'. "
            f"Gemini will map '{category}' to '{feedback.merge_into}' in future analyses. "
            f"Existing techs re-tagged."
        )
    elif feedback.action == "promote":
        return (
            f"'{category}' promoted to standard. "
            f"It now appears as a first-class panel in the UI. "
            f"Gemini will continue emitting it."
        )
    return "Feedback recorded."


@router.get("/pending")
async def list_pending():
    """Emergent categories awaiting review, for the UI to render."""
    return {"pending": await storage_service.get_pending_categories()}
