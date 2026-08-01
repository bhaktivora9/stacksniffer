"""
backend/routers/dep_technology_roles.py

Feedback loop for emergent dependency technology_roles discovered by Gemini.

Three feedback actions:
  discard  — technology_role is noise/wrong, exclude from future classification
  merge    — technology_role is valid but should map to an existing standard technology_role
  promote  — technology_role is valid and should become a first-class standard technology_role

Gemini reads the feedback decisions on every classify_dependencies() call
via _build_technology_role_feedback_context(), ensuring it doesn't re-emit
discarded technology_roles or re-create merged ones.

MongoDB collections:
  dep_technology_roles       — all known technology_roles (standard + emergent)
  dep_technology_role_feedback — human decisions on emergent technology_roles

Java equivalent: DynamicPatternConfigService in stacksniffer-learning
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import backend.services.storage_service as storage_service

router = APIRouter(prefix="/api/dep-technology_roles", tags=["dep-technology_roles"])


# ── Request models ────────────────────────────────────────────────────────────

class TechnologyRoleFeedback(BaseModel):
    action:      str              # "discard" | "merge" | "promote"
    merge_into:  Optional[str] = None   # required when action == "merge"
    reason:      Optional[str] = None   # human-readable note
    source:      str = "ui"


class TechnologyRoleUpdate(BaseModel):
    display_name: Optional[str] = None
    description:  Optional[str] = None
    color:        Optional[str] = None  # hex color for UI pill


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
async def list_dep_technology_roles():
    """
    All known dep technology_roles — standard + emergent.
    Includes feedback status for each emergent technology_role.
    Used by frontend to render tech pills and by dep_classifier prompt.
    """
    return await storage_service.get_dep_technology_roles()


@router.get("/feedback-context")
async def get_feedback_context():
    """
    Returns the feedback decisions formatted for injection into
    the dep_classifier Gemini prompt. Called by dep_classifier.py
    _build_technology_role_feedback_context() on each analysis.

    Format:
    {
      "discarded": ["bundler", "state_management"],
      "merged":    {"css_framework": "library", "task_runner": "infra"},
      "promoted":  ["observability", "auth"]
    }
    """
    decisions = await storage_service.get_technology_role_feedback_decisions()
    return decisions


@router.post("/{technology_role}/feedback")
async def submit_technology_role_feedback(
    technology_role: str,
    feedback: TechnologyRoleFeedback,
):
    """
    Submit a feedback decision for an emergent technology_role.

    action=discard:
      TechnologyRole is noise, wrong, or too granular.
      Gemini will be told NOT to emit this technology_role.
      All techs currently tagged with it are re-tagged as "library".

    action=merge:
      TechnologyRole is valid but maps to an existing standard.
      Requires merge_into: "library" | "infra" | "testing" | etc.
      Gemini will map this technology_role to merge_into in future.
      Example: "css_framework" → merge_into="library"

    action=promote:
      TechnologyRole is valid and should be a first-class standard.
      Adds to standard set in dep_technology_roles collection.
      Gemini will continue emitting it and it renders as a proper panel.
      Example: "bundler" promoted → gets its own UI section.
    """
    VALID_ACTIONS = {"discard", "merge", "promote"}
    standard_technology_roles = await storage_service.get_valid_technology_roles()

    if feedback.action not in VALID_ACTIONS:
        raise HTTPException(400, f"action must be one of: {VALID_ACTIONS}")

    if feedback.action == "merge":
        if not feedback.merge_into:
            raise HTTPException(400, "merge_into required when action=merge")
        if feedback.merge_into not in standard_technology_roles:
            raise HTTPException(
                400,
                f"merge_into must be a standard technology_role: {standard_technology_roles}"
            )

    if feedback.action == "promote" and technology_role in standard_technology_roles:
        raise HTTPException(400, f"{technology_role} is already a standard technology_role")

    result = await storage_service.store_technology_role_feedback(
        technology_role   = technology_role,
        action     = feedback.action,
        merge_into = feedback.merge_into,
        reason     = feedback.reason,
        source     = feedback.source,
    )

    # Apply action immediately to existing techs in MongoDB
    if feedback.action == "discard":
        await storage_service.reclassify_technology_role_techs(
            from_technology_role = technology_role,
            to_technology_role   = None,   # None = remove from output
        )
    elif feedback.action == "merge":
        await storage_service.reclassify_technology_role_techs(
            from_technology_role = technology_role,
            to_technology_role   = feedback.merge_into,
        )
    elif feedback.action == "promote":
        await storage_service.promote_technology_role(technology_role)

    return {
        "technology_role": technology_role,
        "action":   feedback.action,
        "applied":  True,
        "message":  _feedback_message(technology_role, feedback),
    }


@router.delete("/{technology_role}/feedback")
async def undo_technology_role_feedback(technology_role: str):
    """
    Undo a feedback decision — restores technology_role to pending state.
    Discarded technology_roles re-appear as emergent.
    Merged technology_roles revert to their own technology_role.
    Promoted technology_roles revert to emergent.
    """
    await storage_service.delete_technology_role_feedback(technology_role)
    return {"technology_role": technology_role, "status": "reverted to pending"}


@router.patch("/{technology_role}")
async def update_technology_role_metadata(technology_role: str, update: TechnologyRoleUpdate):
    """
    Update display name, description, or UI color for a technology_role.
    Used to give emergent technology_roles human-readable labels before promoting.
    Example: technology_role="bundler" → display_name="Build Bundler", color="#F97316"
    """
    await storage_service.update_technology_role_metadata(
        technology_role     = technology_role,
        display_name = update.display_name,
        description  = update.description,
        color        = update.color,
    )
    return {"technology_role": technology_role, "updated": True}


@router.get("/stats")
async def technology_role_stats():
    """
    Statistics on emergent technology_roles to guide feedback decisions.
    Shows how many repos + techs each emergent technology_role has.
    """
    technology_roles = await storage_service.get_dep_technology_roles()
    decisions  = await storage_service.get_technology_role_feedback_decisions()

    discarded = set(decisions.get("discarded", []))
    merged    = decisions.get("merged", {})
    promoted  = set(decisions.get("promoted", []))

    emergent = [
        c for c in technology_roles
        if not c.get("standard")
    ]

    return {
        "total_emergent":  len(emergent),
        "pending_review":  len([c for c in emergent
                                if c["technology_role"] not in discarded
                                and c["technology_role"] not in merged
                                and c["technology_role"] not in promoted]),
        "discarded":       len(discarded),
        "merged":          len(merged),
        "promoted":        len(promoted),
        "technology_roles":      [
            {
                **c,
                "status": (
                    "discarded" if c["technology_role"] in discarded else
                    f"merged → {merged[c['technology_role']]}" if c["technology_role"] in merged else
                    "promoted" if c["technology_role"] in promoted else
                    "pending"
                )
            }
            for c in emergent
        ],
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _feedback_message(technology_role: str, feedback: TechnologyRoleFeedback) -> str:
    if feedback.action == "discard":
        return (
            f"'{technology_role}' discarded. Gemini will not emit this technology_role. "
            f"Existing techs tagged '{technology_role}' have been removed from output."
        )
    elif feedback.action == "merge":
        return (
            f"'{technology_role}' merged into '{feedback.merge_into}'. "
            f"Gemini will map '{technology_role}' to '{feedback.merge_into}' in future analyses. "
            f"Existing techs re-tagged."
        )
    elif feedback.action == "promote":
        return (
            f"'{technology_role}' promoted to standard. "
            f"It now appears as a first-class panel in the UI. "
            f"Gemini will continue emitting it."
        )
    return "Feedback recorded."


@router.get("/pending")
async def list_pending():
    """Emergent technology_roles awaiting review, for the UI to render."""
    return {"pending": await storage_service.get_pending_technology_roles()}
