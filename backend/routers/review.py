from fastapi import APIRouter, Depends, HTTPException, Query, Response
from models.schemas import ReviewActionRequest, ReviewItemKind
from routers.admin_auth import (
    AdminLoginRequest,
    clear_admin_session,
    create_admin_session,
    require_admin,
)
from services import storage_service

router = APIRouter(prefix="/api/review", tags=["review"])


@router.post("/login")
async def login_review_admin(
    response: Response,
    login: AdminLoginRequest,
):
    return create_admin_session(response, login.username, login.password)


@router.get("/session")
async def get_review_session(
    _auth: str = Depends(require_admin),
):
    return {"authenticated": True, "role": "admin"}


@router.delete("/session")
async def delete_review_session(response: Response):
    return clear_admin_session(response)


_COLLECTION_PURPOSES = [
    {
        "name": "analyses_result",
        "purpose": "Primary analysis output per repo key (stack, software_type, metadata). Refreshed on each run.",
    },
    {
        "name": "analyses_request",
        "purpose": "Per-request audit trail for analyze submissions (request metadata + served mode).",
    },
    {
        "name": "corrections",
        "purpose": "User software_type correction overlays keyed by repo/field.",
    },
    {
        "name": "feedback",
        "purpose": "Software_type feedback records (software_type_correct, replacement labels, etc.).",
    },
    {
        "name": "analysis_events",
        "purpose": "Sequential event logs for each repo analysis lifecycle and traceability.",
    },
    {
        "name": "stack_feedback",
        "purpose": "Per-technology role feedback events from stack feedback submissions.",
    },
    {
        "name": "insights_feedback",
        "purpose": "Insight/quality feedback about AI generated outputs.",
    },
    {
        "name": "quality_criteria",
        "purpose": "Quality rubric definitions for grading insight feedback.",
    },
    {
        "name": "software_types",
        "purpose": "Legacy/manual software_type registry for taxonomy-discovery approval flow.",
    },
    {
        "name": "dep_technology_roles",
        "purpose": "Technology role taxonomy with builtin/emergent roles and lifecycle status.",
    },
    {
        "name": "dep_technology_role_feedback",
        "purpose": "Human review actions for emergent roles/types (approve/merge/discard).",
    },
    {
        "name": "taxonomy_software_types",
        "purpose": "Canonical software_type taxonomy (builtin + emergent + status/active flags).",
    },
    {
        "name": "taxonomy_technology_roles",
        "purpose": "Canonical technology role taxonomy metadata and lifecycle flags.",
    },
    {
        "name": "software_type_disagreements",
        "purpose": "Derived registry of software_type disagreement evidence used by learning.",
    },
    {
        "name": "review_items",
        "purpose": "Maintainer review queue entries (corrections/emergent type/role).",
    },
    {
        "name": "correction_events",
        "purpose": "Append-only human-correction and system-coercion events with analysis/repo provenance.",
    },
    {
        "name": "software_type_corrections",
        "purpose": "Approved software_type correction overlay store for future analyses.",
    },
]


@router.get("/collections")
async def list_collections(
    _auth: str = Depends(require_admin),
):
    return {"collections": _COLLECTION_PURPOSES, "count": len(_COLLECTION_PURPOSES)}


@router.get("/stats")
async def get_review_stats(
    _auth: str = Depends(require_admin),
):
    return await storage_service.get_review_stats()


@router.get("/queue")
async def list_queue(
    kind: str | None = Query(default=None),
    type: str | None = Query(default=None),
    status: str = Query(default="pending"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
):
    if kind is None:
        kind = type
    if kind is not None and kind not in ReviewItemKind._value2member_map_:
        raise HTTPException(
            400,
            "kind must be correction|classifier_diagnosis|emergent_type|emergent_role",
        )
    if status not in {"pending", "approved", "rejected"}:
        raise HTTPException(400, "status must be pending|approved|rejected")

    normalized = kind or None
    skip = (page - 1) * limit
    items, total = await storage_service.get_review_queue(
        kind=normalized,
        status=status,
        limit=limit,
        skip=skip,
    )
    return {
        "items": items,
        "page": page,
        "limit": limit,
        "total": total,
        "status": status,
        "kind": normalized or "all",
        "type": normalized or "all",
    }


@router.get("/correction-events")
async def list_correction_events(
    _actor: str = Depends(require_admin),
    resolution: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
):
    if resolution and resolution not in {
        "pending_review", "approved", "rejected", "classifier_diagnosis", "taxonomy_pending",
        "system_coercion",
    }:
        raise HTTPException(400, "invalid correction-event resolution")
    events = await storage_service.get_correction_events(resolution=resolution, limit=limit)
    return {"events": events, "count": len(events), "resolution": resolution or "all"}


def _requested_merge_target(request: ReviewActionRequest) -> str | None:
    return (request.merge_target or request.merge_into or "").strip() or None


@router.post("/{item_id:path}/approve")
async def approve_item(
    item_id: str,
    request: ReviewActionRequest,
    actor: str = Depends(require_admin),
):
    item = await storage_service.get_review_item(item_id)
    if not item:
        raise HTTPException(404, "review item not found")
    if item.get("status") != "pending":
        raise HTTPException(409, f"item already {item.get('status')}")

    kind = item.get("kind")
    if kind not in ReviewItemKind._value2member_map_:
        raise HTTPException(400, f"invalid review item kind: {kind}")

    if kind in {ReviewItemKind.CORRECTION.value, ReviewItemKind.CLASSIFIER_DIAGNOSIS.value}:
        target = (request.target_value or item.get("proposed_value") or "").strip()
        if not target:
            raise HTTPException(400, "target_value is required for correction approvals")
        evidence = item.get("evidence") or {}
        repo_key = evidence.get("repo_key")
        if not repo_key and evidence.get("repo"):
            repo_key = f"github:{evidence['repo']}"
        if not repo_key:
            raise HTTPException(400, "correction evidence must identify a repository")
        correction_field = evidence.get("correction_field")
        if correction_field == "technology_role":
            tech_name = (evidence.get("tech_name") or "").strip()
            if not tech_name:
                raise HTTPException(400, "technology-role correction requires tech_name")
            await storage_service.upsert_technology_role_correction(
                repo_key, tech_name, target,
                source_event_ids=item.get("trigger_event_ids") or [],
            )
            await storage_service.upsert_learned_technology_mapping(
                tech_name,
                technology_role=target,
                approved_by=actor,
                source_event_ids=item.get("trigger_event_ids") or [],
            )
        elif correction_field == "architectural_layer":
            tech_name = (evidence.get("tech_name") or "").strip()
            if not tech_name:
                raise HTTPException(400, "architectural-layer correction requires tech_name")
            await storage_service.upsert_architectural_layer_correction(
                repo_key, tech_name, target,
                source_event_ids=item.get("trigger_event_ids") or [],
            )
            await storage_service.upsert_learned_technology_mapping(
                tech_name,
                architectural_layer=target,
                approved_by=actor,
                source_event_ids=item.get("trigger_event_ids") or [],
            )
        else:
            valid_types = await storage_service.get_valid_software_types()
            if target not in valid_types:
                raise HTTPException(400, "target_value must be a canonical software_type")
            await storage_service.approve_software_type_correction(
                item["pipeline_value"], target, repo_key=repo_key, evidence=evidence,
                assignment_method=item.get("assignment_method"),
                source_event_ids=item.get("trigger_event_ids") or [],
            )
        item["proposed_value"] = target
    elif kind == ReviewItemKind.EMERGENT_TYPE.value:
        merge_target = _requested_merge_target(request)
        if merge_target:
            valid = await storage_service.get_valid_software_types()
            if merge_target not in valid:
                raise HTTPException(400, "merge_into must be a canonical software_type")
            await storage_service.apply_taxonomy_action(
                kind="software_type",
                name=item["pipeline_value"],
                action="merge",
                merge_into=merge_target,
                source="maintainer_approved",
            )
        else:
            emergent_name = (item.get("proposed_value") or item["pipeline_value"]).strip()
            await storage_service.apply_taxonomy_action(
                kind="software_type",
                name=emergent_name,
                action="promote",
                source="maintainer_approved",
            )
    elif kind == ReviewItemKind.EMERGENT_ROLE.value:
        merge_target = _requested_merge_target(request)
        if merge_target:
            valid_roles = await storage_service.get_valid_technology_roles()
            if merge_target not in valid_roles:
                raise HTTPException(400, "merge_into must be a canonical technology_role")
            await storage_service.apply_taxonomy_action(
                kind="technology_role",
                name=item["pipeline_value"],
                action="merge",
                merge_into=merge_target,
                source="maintainer_approved",
            )
        else:
            await storage_service.apply_taxonomy_action(
                kind="technology_role",
                name=item["pipeline_value"],
                action="promote",
                source="maintainer_approved",
            )
    else:
        raise HTTPException(400, "unsupported review kind")

    await storage_service.set_review_item_status(
        item_id=item_id,
        status="approved",
        reviewed_by=actor,
        note=request.note,
    )
    await storage_service.resolve_correction_events(
        item.get("trigger_event_ids") or [], resolution="approved", actor=actor,
    )
    return {
        "item_id": item_id,
        "status": "approved",
        "kind": kind,
        "message": "review item approved",
    }


@router.post("/{item_id:path}/reject")
async def reject_item(
    item_id: str,
    request: ReviewActionRequest,
    actor: str = Depends(require_admin),
):
    item = await storage_service.get_review_item(item_id)
    if not item:
        raise HTTPException(404, "review item not found")
    if item.get("status") != "pending":
        raise HTTPException(409, f"item already {item.get('status')}")
    await storage_service.set_review_item_status(
        item_id=item_id,
        status="rejected",
        reviewed_by=actor,
        note=request.note,
    )
    await storage_service.resolve_correction_events(
        item.get("trigger_event_ids") or [], resolution="rejected", actor=actor,
    )
    return {
        "item_id": item_id,
        "status": "rejected",
        "kind": item.get("kind"),
        "message": "review item rejected",
    }


@router.post("/{item_id:path}/merge")
async def merge_item(
    item_id: str,
    request: ReviewActionRequest,
    actor: str = Depends(require_admin),
):
    merge_target = _requested_merge_target(request)
    if not merge_target:
        raise HTTPException(400, "merge_target is required")
    item = await storage_service.get_review_item(item_id)
    if not item:
        raise HTTPException(404, "review item not found")
    if item.get("status") != "pending":
        raise HTTPException(409, f"item already {item.get('status')}")

    kind = item.get("kind")
    if kind == ReviewItemKind.EMERGENT_TYPE.value:
        valid = await storage_service.get_valid_software_types()
        if merge_target not in valid:
            raise HTTPException(400, "merge_into must be a canonical software_type")
        await storage_service.apply_taxonomy_action(
            kind="software_type",
            name=item["pipeline_value"],
            action="merge",
            merge_into=merge_target,
            source="maintainer_approved",
        )
    elif kind == ReviewItemKind.EMERGENT_ROLE.value:
        valid_roles = await storage_service.get_valid_technology_roles()
        if merge_target not in valid_roles:
            raise HTTPException(400, "merge_into must be a canonical technology_role")
        await storage_service.apply_taxonomy_action(
            kind="technology_role",
            name=item["pipeline_value"],
            action="merge",
            merge_into=merge_target,
            source="maintainer_approved",
        )
    else:
        raise HTTPException(400, "merge endpoint is only for emergent items")

    await storage_service.set_review_item_status(
        item_id=item_id,
        status="approved",
        reviewed_by=actor,
        note=request.note,
    )
    return {
        "item_id": item_id,
        "status": "approved",
        "kind": kind,
        "message": f"merged into {merge_target}",
    }


@router.post("/{item_id:path}/discard")
async def discard_item(
    item_id: str,
    request: ReviewActionRequest,
    actor: str = Depends(require_admin),
):
    item = await storage_service.get_review_item(item_id)
    if not item:
        raise HTTPException(404, "review item not found")
    if item.get("status") != "pending":
        raise HTTPException(409, f"item already {item.get('status')}")

    await storage_service.set_review_item_status(
        item_id=item_id,
        status="rejected",
        reviewed_by=actor,
        note=request.note,
    )
    return {
        "item_id": item_id,
        "status": "rejected",
        "kind": item.get("kind"),
        "message": "review item discarded",
    }
