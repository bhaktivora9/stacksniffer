"""
backend/routers/analyze.py

4-Phase detection pipeline:

  Phase 0: File signals     - deterministic, <1ms, zero false positives
  Phase 1: Manifest extract - structural parsing, no classification
  Phase 2a: Dep classify    - Gemini classifies raw dep list (emergent technology_roles supported)
  Phase 2b: SoftwareType - Gemini software_type classification

Emergent technology_roles (e.g. "bundler", "state_management") discovered by Phase 2a
are stored in MongoDB dep_technology_roles for human review via
POST /api/dep-technology_roles/{technology_role}/feedback (discard | merge | promote).
"""
import asyncio
import json
import time
from contextvars import ContextVar
from copy import deepcopy
from uuid import uuid4
import logging
from os import getenv
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

import backend.services.github_service as github_service
from backend.models.schemas import (
    AnalyzeRequest,
    ArchitecturalLayer,
    UsageScope,
    DetectedTech,
    StackAnalysis,
)
from backend.models.taxonomy import canonicalize_software_type
from backend.routers.deps import resolve_repo_key
from backend.services import storage_service
from backend.services.ai_pipeline import run_full_ai_pipeline
from backend.services.artifact_classifier import (
    assign_artifact_ownership,
    classify_artifacts,
)
from backend.services.technology_role_registry import valid_technology_roles
from backend.services.dep_classifier import (
    apply_file_signals,
    classify_dependencies,
)
from backend.services.embedding_service import embed_stack
from backend.services.github_service import (
    GitHubRateLimitError,
    RepoNotFoundError,
    fetch_repo,
    get_head_sha,
    to_repo_metadata,
)
from backend.services.layer_assignment import assign_missing_architectural_layers
from backend.services.technology_dedup import dedup_stack
from backend.services.manifest_parser import (
    filter_self_references_from_inferences,
    parse_manifest_dependencies,
)
from backend.services.quality_flags import (
    apply_confidence_demotions,
    apply_demotions_to_detections,
    compute_analysis_flags,
)
from backend.services.repo_key import (
    RepoKeyError,
    canonical_repo_key,
    repo_key_to_url,
)
from backend.services.software_type_guard import guard_library_classification
from backend.services.safe_json import get_repair_counters

router = APIRouter()
logger = logging.getLogger(__name__)

_progress_sink: ContextVar = ContextVar("analysis_progress_sink", default=None)
_stream_jobs: dict[str, asyncio.Task] = {}


async def _emit_progress(**event) -> None:
    sink = _progress_sink.get()
    if sink is not None:
        await sink(event)

# Bump on ANY change to patterns.json, prompts, or the model.
# Later derive this from sha256(patterns.json + prompt templates + model name).
PIPELINE_VERSION = storage_service.PIPELINE_VERSION

def _env_float(name: str, default: float) -> float:
    try:
        return float(getenv(name, str(default)))
    except (TypeError, ValueError):
        logger.warning("[analyze] invalid float for %s, using default %.1fs", name, default)
        return default

_DEP_CLASSIFICATION_TIMEOUT_SECONDS = _env_float(
    "GEMINI_DEP_CLASSIFICATION_TIMEOUT_SECONDS", 180.0
)
SOFTWARE_TYPE_TIMEOUT_SECONDS = _env_float(
    "GEMINI_SOFTWARE_TYPE_TIMEOUT_SECONDS", 90.0
)

# Standard technology_roles - these map directly to StackAnalysis schema fields
_AI_FAILED_DEFAULTS = {
    "software_type":                 "unknown",
    "software_type_confidence":      0.0,
    "software_type_reasoning":       "AI classification timed out or was skipped",
    "architecture_style":     "unknown",
    "missing_patterns":       [],
    "ai_inferred_techs":      [],
    "why_this_stack":         "",
    "stack_pattern":          "Custom",
    "ecosystem_context":      "",
    "notable_combinations":   [],
    "ai_inferences":          [],
    "ai_classification_used": False,
    "ai_calls_made":          0,
    "rag_repos_retrieved":    0,
    "embedding":              [],
}


def _empty_detections(valid: set[str]) -> dict[str, list[DetectedTech]]:
    return {technology_role: [] for technology_role in valid}


def _compute_complexity(detections: dict) -> int:
    high_conf = sum(
        1 for cat in detections
        if any(
            getattr(t, "confidence", 0) >= 0.80
            for t in detections.get(cat, [])
        )
    )
    return {0: 1, 1: 2, 2: 4, 3: 5, 4: 7, 5: 8, 6: 9}.get(high_conf, 10)


def _repair_counters_delta(before: dict[str, dict[str, int]], after: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for site, stages in after.items():
        before_stages = before.get(site, {})
        out[site] = {
            stage: int(count) - int(before_stages.get(stage, 0))
            for stage, count in stages.items()
        }
    return out


def _place_tech(
    detections: dict[str, list[DetectedTech]],
    tech: DetectedTech,
    cat: str,
    emergent_technology_role: str | None = None,
) -> None:
    """
    Place a DetectedTech into detections.
    Standard technology_roles go directly into their slot.
    Emergent technology_roles (bundler, state_management, etc.) go into "library"
    with emergent_technology_role preserved for storage + UI rendering.
    """
    if cat in detections:
        detections[cat].append(tech)
    else:
        # Emergent technology_role - store in library for schema compatibility
        # emergent_technology_role field preserved for dep_technology_roles feedback loop
        try:
            tech.emergent_technology_role = emergent_technology_role or cat
        except Exception:
            pass
        tech.technology_role = "library"
        detections["library"].append(tech)


def _inject_self_project_techs(
    detections: dict[str, list[DetectedTech]],
    project_names: set[str],
    valid_roles: set[str],
) -> None:
    """
    Add repo-owned package metadata as an explicit manifest-derived signal.

    Manifest parser intentionally removes self-references from raw_deps. Without this,
    package-name-only repos with strong manifest evidence can lose a product-level
    anchor signal. We reinsert as a high-confidence, source-annotated `library`.
    """
    if not project_names:
        return
    canonical = {
        name.strip().lower().replace("_", "-")
        for name in project_names
        if isinstance(name, str) and name.strip()
    }
    if not canonical:
        return

    for name in sorted(canonical):
        # If the same name is already detected in another role (e.g. inferred
        # as a framework), keep that placement and boost confidence.
        existing: DetectedTech | None = None
        for techs in detections.values():
            existing = next((t for t in techs if t.name.casefold() == name), None)
            if existing:
                break

        if existing is not None:
            if existing.confidence < 0.95:
                existing.confidence = max(existing.confidence, 0.95)
            existing.detection_source = "project_manifest"
            continue

        _place_tech(
            detections,
            DetectedTech(
                name                = name,
                confidence          = 0.98,
                detection_source     = "project_manifest",
                technology_role     = "library",
                scope               = "required",
                usage_scope         = _coerce_usage_scope("runtime"),
                matched_file        = None,
            ),
            "library" if "library" in valid_roles else next(iter(valid_roles), "library"),
        )


def _coerce_architectural_layer(raw_layer: dict | None) -> ArchitecturalLayer | None:
    if raw_layer is None:
        return None
    if isinstance(raw_layer, ArchitecturalLayer):
        return raw_layer
    if not isinstance(raw_layer, dict):
        return None
    try:
        return ArchitecturalLayer(**raw_layer)
    except Exception:
        return None


def _coerce_usage_scope(raw_scope: str | None) -> UsageScope | None:
    if raw_scope is None:
        return None
    if isinstance(raw_scope, UsageScope):
        return raw_scope
    try:
        return UsageScope(raw_scope)
    except Exception:
        return None


@router.get("/analyze/stream")
async def analyze_repository_stream(
    request: Request,
    repo: str = Query(..., min_length=1),
    job: str | None = Query(default=None),
):
    """Stream authoritative analysis progress using the frozen SSE event contract."""
    job_id = job or str(uuid4())

    async def events():
        queue: asyncio.Queue = asyncio.Queue()
        seq = 0
        totals = {"verified_total": 0, "inferred_total": 0}

        async def publish(event: dict | None = None, **fields) -> None:
            nonlocal seq
            # Pipeline progress reaches this sink as one dict via
            # `_emit_progress`, while the stream lifecycle publishes its own
            # boundary events with keyword arguments. Normalize both call
            # forms into the same frozen SSE event contract.
            event = {**(event or {}), **fields}
            seq += 1
            for key in totals:
                if key in event:
                    totals[key] = int(event[key])
            payload = {
                "seq": seq,
                "phase": event.get("phase", "ingest"),
                "level": event.get("level", "inf"),
                "message": event.get("message", ""),
                "provenance": event.get("provenance"),
                "field": event.get("field"),
                "value": event.get("value"),
                "confidence": event.get("confidence"),
                "progress": event.get("progress", 0.0),
                **totals,
                "analysis_id": event.get("analysis_id"),
                "error": event.get("error"),
            }
            await queue.put(payload)

        async def run() -> None:
            token = _progress_sink.set(publish)
            try:
                await publish(phase="ingest", level="inf", message="Initializing analysis engine", progress=0.03)
                result = await analyze_repository(AnalyzeRequest(repo_url=repo))
                analysis_id = result.get("request_id") or result.get("analysis_id") or result.get("repo_key")
                stack = result.get("stack") or {}
                detected = [tech for value in stack.values() if isinstance(value, list) for tech in value if isinstance(tech, dict) and tech.get("name")]
                verified = sum(1 for tech in detected if tech.get("detection_source") not in {"ai", "ai_inferred"})
                inferred = len(stack.get("ai_inferences") or [])
                if stack.get("software_type") not in {None, "", "unknown"}:
                    inferred = max(1, inferred)
                await publish(phase="done", level="ok", message="Analysis complete", progress=1.0, verified_total=verified, inferred_total=inferred, analysis_id=analysis_id)
            except asyncio.CancelledError:
                await publish(phase="done", level="error", message="Analysis cancelled", progress=0.0, error={"code": "cancelled", "detail": "Analysis was cancelled"})
                raise
            except Exception as exc:
                detail = getattr(exc, "detail", None) or str(exc)
                if not isinstance(detail, str):
                    detail = json.dumps(detail)
                await publish(phase="done", level="error", message=detail, progress=0.0, error={"code": "analysis_failed", "detail": detail})
            finally:
                _progress_sink.reset(token)
                await queue.put(None)

        task = asyncio.create_task(run(), name=f"analysis-stream:{job_id}")
        _stream_jobs[job_id] = task
        try:
            while True:
                if await request.is_disconnected():
                    task.cancel()
                    break
                item = await queue.get()
                if item is None:
                    break
                yield {"id": str(item["seq"]), "data": json.dumps(item)}
        finally:
            if not task.done():
                task.cancel()
            _stream_jobs.pop(job_id, None)

    return EventSourceResponse(events(), ping=15)


@router.post("/analyze/{job}/cancel")
async def cancel_streamed_analysis(job: str):
    task = _stream_jobs.get(job)
    if task is None or task.done():
        return {"job": job, "cancelled": False}
    task.cancel()
    return {"job": job, "cancelled": True}


@router.post("/analyse", include_in_schema=False)
@router.post("/analyze")
async def analyze_repository(request: AnalyzeRequest):
    try:
        repo_key = canonical_repo_key(request.repo_url)
    except RepoKeyError as e:
        raise HTTPException(400, str(e))

    request_id = str(uuid4())
    try:
        head_sha = await get_head_sha(repo_key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RepoNotFoundError as e:
        raise HTTPException(404, str(e))

    except GitHubRateLimitError as e:
        raise HTTPException(429, {
            "detail": "GitHub API rate limit exceeded",
            "retry_after_seconds": e.retry_after,
            "fix": (
                "Add GITHUB_TOKEN to .env. "
                "Unauthenticated: 60 req/hr. Authenticated: 5000 req/hr."
            ),
        })

    doc = await storage_service.get_repo(repo_key)
    fresh = storage_service.is_fresh(doc, head_sha, PIPELINE_VERSION)
    hard_refresh = bool(request.hard_refresh)

    await storage_service.log_request(
        request_id,
        repo_key,
        request.repo_url,
        head_sha,
        served=(
            "HARD_REFRESH"
            if hard_refresh
            else ("CACHED" if fresh else ("STALE" if doc else "ANALYZED"))
        ),
    )

    if fresh and not hard_refresh:
        return await _to_response(doc, request_id, fresh=True, head_sha=head_sha)

    claimed = await storage_service.claim_refresh(repo_key, head_sha, PIPELINE_VERSION)

    if hard_refresh:
        if not claimed:
            doc = await _await_refresh(repo_key)
            if doc and storage_service.is_fresh(doc, head_sha, PIPELINE_VERSION):
                return await _to_response(
                    doc,
                    request_id,
                    fresh=True,
                    head_sha=head_sha,
                    hard_refresh=True,
                )
            raise HTTPException(
                503,
                "Hard refresh is already running for this repo. Retry shortly.",
            )

        await _run_pipeline(repo_key, head_sha)
        doc = await storage_service.get_repo(repo_key)
        if not doc:
            raise HTTPException(500, "Hard refresh completed but no document was stored")
        return await _to_response(
            doc,
            request_id,
            fresh=True,
            head_sha=head_sha,
            hard_refresh=True,
        )

    # WARM: a previous complete result exists. Serve it now, refresh behind.
    if doc and doc.get("stack"):
        if claimed:
            asyncio.create_task(_run_pipeline_background(repo_key, head_sha))
        return await _to_response(
            doc, request_id, fresh=False, refreshing=True, head_sha=head_sha
        )

    # COLD, and someone else owns the refresh. Do NOT run a second pipeline -
    # that was burning a duplicate ~50s Gemini run and racing on the same _id.
    # Wait for the owner to land a result.
    if not claimed:
        doc = await _await_refresh(repo_key)
        if doc and doc.get("stack"):
            return await _to_response(doc, request_id, fresh=True, head_sha=head_sha)
        raise HTTPException(
            503,
            "Analysis in progress by another request and did not complete in time. Retry.",
        )

    # COLD and we own it: nothing to serve, so block.
    await _run_pipeline(repo_key, head_sha)
    doc = await storage_service.get_repo(repo_key)
    if not doc:
        raise HTTPException(500, "Analysis completed but no document was stored")
    return await _to_response(doc, request_id, fresh=True, head_sha=head_sha)


async def _await_refresh(repo_key: str, timeout_s: int = 120) -> dict | None:
    """Poll until whoever owns the refresh clears it (or the lease lapses)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        await asyncio.sleep(2)
        doc = await storage_service.get_repo(repo_key)
        if doc and doc.get("stack") and not (doc.get("refresh") or {}).get("status") == "RUNNING":
            return doc
    return await storage_service.get_repo(repo_key)


async def _run_pipeline_background(repo_key: str, head_sha: str) -> None:
    """
    create_task target. _run_pipeline raises HTTPException on GitHub errors,
    which is meaningless in a background task and surfaces as an unhandled
    task exception. The refresh lease is already released by fail_refresh.
    """
    try:
        await _run_pipeline(repo_key, head_sha)
    except Exception as e:
        logger.warning(
            "[analyze] background refresh failed for %s: %s",
            repo_key,
            str(e)[:200],
        )


async def _to_response(
    doc: dict,
    request_id: str,
    fresh: bool,
    refreshing: bool = False,
    head_sha: str | None = None,
    hard_refresh: bool = False,
) -> dict:
    """
    Always takes an analyses_result doc, never a bare stack.

    `repo` and `analysis_id` are what the frontend reads. The old doc had a
    repo sub-document (check_all.py does d["repo"]["full_name"]); dropping it
    is what blanked the results page. analysis_id is an alias for request_id,
    kept until the frontend migrates - delete it once it reads request_id.
    """
    stack = doc.get("stack") or {}
    repo_key = doc.get("repo_key") or doc.get("_id")
    corrected_stack, _ = await storage_service.apply_corrections(repo_key, stack)
    correction = await _overlay_software_type_corrections(
        repo_key, stack.get("software_type"),
    )
    if correction:
        corrected_stack = {**corrected_stack, "software_type_overlay": correction}
    return {
        "request_id": request_id,
        "analysis_id": request_id,          # deprecated alias
        "repo_key": doc.get("repo_key") or doc.get("_id"),
        "repo": doc.get("repo") or {},
        "repository_classification": doc.get("repository_classification"),
        "stack": corrected_stack,
        "software_type": {
            "pipeline": stack.get("software_type", "unknown"),
            "ai": {
                "value": stack.get("software_type_ai") or stack.get("software_type", "unknown"),
                "reasoning": stack.get("software_type_ai_reasoning") or stack.get("software_type_reasoning", ""),
            },
        },
        "emergent_technology_roles": stack.get("emergent_technology_roles", []),
        "emergent_software_type": stack.get("emergent_software_type"),
        "software_type_overlay": correction,
        "commit_sha": doc.get("commit_sha"),
        "pipeline_version": doc.get("pipeline_version"),
        "fresh": fresh,
        "refreshing": refreshing,
        "hard_refresh": hard_refresh,
        "head_sha": head_sha or doc.get("commit_sha"),
        "created_at": doc.get("created_at"),
        "analyzed_at": doc.get("analyzed_at"),
    }


async def _overlay_software_type_corrections(
    repo_key: str,
    pipeline_value: str | None,
) -> dict | None:
    return await storage_service.get_approved_software_type_correction(
        repo_key, pipeline_value,
    )

def _analysis_event_summary(stack: dict, repair_counters: dict | None = None) -> dict:
    repair_counters = repair_counters if repair_counters is not None else get_repair_counters()
    tech_names = []
    for techs in stack.values():
        if not isinstance(techs, list):
            continue
        for tech in techs:
            if isinstance(tech, dict) and tech.get("name"):
                tech_names.append(tech["name"])
    return {
        "software_type": stack.get("software_type"),
        "software_type_confidence": stack.get("software_type_confidence"),
        "repair_counters": repair_counters,
        "stack_pattern": stack.get("stack_pattern"),
        "tech_names": tech_names,
        "quality_flags": stack.get("flags", []),
        "files_analyzed": stack.get("files_analyzed"),
        "ai_calls_made": stack.get("ai_calls_made"),
    }

async def _run_pipeline(repo_key: str, head_sha: str) -> dict:
    try:
        return await _run_pipeline_body(repo_key, head_sha)
    except asyncio.CancelledError:
        await storage_service.fail_refresh(repo_key, "Analysis cancelled")
        raise
    except Exception as e:
        await storage_service.fail_refresh(repo_key, str(e))
        raise


async def _run_pipeline_body(repo_key: str, head_sha: str) -> dict:
    start_ms = time.time() * 1000

    # GitHub ingestion
    await _emit_progress(phase="ingest", level="run", message=f"Cloning {repo_key}", progress=0.08)
    try:
        repo = await fetch_repo(repo_key_to_url(repo_key))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RepoNotFoundError as e:
        raise HTTPException(404, str(e))
    except GitHubRateLimitError as e:
        raise HTTPException(429, {
            "detail":              "GitHub API rate limit exceeded",
            "retry_after_seconds": e.retry_after,
            "fix": (
                "Add GITHUB_TOKEN to .env. "
                "Unauthenticated: 60 req/hr. Authenticated: 5000 req/hr."
            ),
        })
    except Exception as e:
        err = str(e)
        if "403" in err or "rate limit" in err.lower():
            raise HTTPException(429, "GitHub API rate limit. Add GITHUB_TOKEN to .env.")
        raise HTTPException(500, f"GitHub fetch failed: {err[:200]}")

    await _emit_progress(phase="ingest", level="ok", message=f"Repository fetched · {len(repo.file_tree)} files indexed", progress=0.20)
    repository_classification = classify_artifacts(repo)

    # ---------------------------------------------------------------------------
    # PHASE 0: File signals - deterministic, <1ms, zero false positives
    # Dockerfile -> Docker, go.mod -> Go, .java files -> Java
    # Each signal is a definitive filename - confidence 1.0
    # ---------------------------------------------------------------------------
    file_signal_list = apply_file_signals(repo.file_tree)
    logger.info(
        "[analyze] Phase 0: %s file signals (%s)",
        len(file_signal_list),
        [s["name"] for s in file_signal_list],
    )

    # ---------------------------------------------------------------------------
    # PHASE 1: Manifest extraction - structural parsing, no classification
    # Parses pyproject.toml, package.json, pom.xml, go.mod, Cargo.toml, etc.
    # Returns raw dep list: [{name, scope, origin, matched_file, version_spec}]
    # Self-references excluded at this stage.
    # ---------------------------------------------------------------------------
    manifest_result = parse_manifest_dependencies(
        file_contents  = repo.file_contents,
        file_tree      = repo.file_tree,
        repo_full_name = repo.full_name,
    )
    raw_deps      = manifest_result["raw_deps"]
    project_names = manifest_result["project_names"]
    logger.info(
        "[analyze] Phase 1: %s raw deps from %s manifests",
        len(raw_deps),
        sum(1 for m in manifest_result["manifests_selected"] if m.get("parsed")),
    )
    verified_total = len(file_signal_list)
    await _emit_progress(phase="verify", level="run", message="Reading dependency manifests", progress=0.30, verified_total=verified_total)
    for signal in file_signal_list:
        await _emit_progress(
            phase="verify", level="map",
            message=f"{signal.get('matched_file') or 'file tree'} → {signal.get('name')}",
            provenance="map", field="technology", value=signal.get("name"),
            progress=0.38, verified_total=verified_total,
        )

# ---------------------------------------------------------------------------
    # PHASE 2a: dependency classification - Gemini ENRICHES, never GATES
    #
    # The invariant: Phase 1 extracted N deps -> the output contains N deps.
    # Gemini can relabel a dep's technology_role and raise its confidence. It cannot
    # delete one. A dead Gemini call costs technology_role precision, not the stack.
    # (Three repos - fastapi, next.js, litellm - proved the old gate discards
    # everything on DEP_CLASSIFICATION_FAILED.)
    # ---------------------------------------------------------------------------
    from backend.services.dep_fallback import (
        build_base_detections,
        collect_unresolved_tail,
        enrich_with_classifications,
        assert_deps_survived,
    )

    files_analyzed = len(repo.file_contents)
    repair_counters_start = get_repair_counters()

    # BASE first, unconditionally, no AI. This is what makes deps survive.
    # NOT gated on files_analyzed - raw_deps comes from manifest parsing, which
    # does not depend on file coverage.
    base_deps = build_base_detections(raw_deps)
    learned_mappings = await storage_service.get_learned_technology_mappings()
    for dependency in base_deps:
        learned = learned_mappings.get(str(dependency.get("name") or "").casefold())
        if not learned:
            continue
        if learned.get("technology_role"):
            dependency["technology_role"] = learned["technology_role"]
        if learned.get("architectural_layer"):
            dependency["architectural_layer"] = {
                "primary": learned["architectural_layer"],
                "secondary": [],
                "assignment_method": "maintainer_approved",
                "confidence": 1.0,
                "disambiguation_pending": False,
            }
        dependency["detection_source"] = "maintainer_learned"
        dependency["fallback_tier"] = "learned"
        dependency["learned_mapping"] = True
    unresolved_raw_deps = collect_unresolved_tail(raw_deps, base_deps)

    dep_classifications: list[dict] = []
    dep_failed = False
    dep_classification_fail_reason = "none"

    classifier_result = None
    if files_analyzed < 1:
        # Skip only the GEMINI call - the base is already built above.
        logger.info(
            "[analyze] Phase 2a (Gemini) skipped - only %s files fetched",
            files_analyzed,
        )
    elif unresolved_raw_deps:
        try:
            dep_classification_call = classify_dependencies(
                raw_deps       = unresolved_raw_deps,
                file_tree      = repo.file_tree,
                repo_full_name = repo.full_name,
            )
            if _DEP_CLASSIFICATION_TIMEOUT_SECONDS and _DEP_CLASSIFICATION_TIMEOUT_SECONDS > 0:
                dep_classifications = await asyncio.wait_for(
                    dep_classification_call,
                    timeout=_DEP_CLASSIFICATION_TIMEOUT_SECONDS,
                )
            else:
                dep_classifications = await dep_classification_call
            logger.info(
                "[analyze] Phase 2a: %s of %s unresolved techs classified by Gemini",
                len(dep_classifications),
                len(unresolved_raw_deps),
            )
        except asyncio.TimeoutError:
            dep_failed = True
            dep_classification_fail_reason = "timeout"
            logger.warning(
                "[analyze] Phase 2a timed out after %ss - deterministic tiers only",
                _DEP_CLASSIFICATION_TIMEOUT_SECONDS,
            )
        except Exception as e:
            dep_failed = True
            dep_classification_fail_reason = "exception"
            logger.exception("[analyze] Phase 2a dependency classification failed: %s", e)
            logger.warning(
                "[analyze] Phase 2a failed: %s - deterministic tiers only",
                e,
            )

    # Enrich the base with whatever Gemini returned. Empty list -> base passes
    # through untouched. This is the line that made the stack survivable.
    final_deps = enrich_with_classifications(base_deps, dep_classifications)
    verified_total = len(file_signal_list) + len(final_deps)
    await _emit_progress(
        phase="verify", level="ok",
        message=f"Manifest detection complete · {verified_total} verified signals",
        provenance="map", progress=0.62, verified_total=verified_total,
    )

    logger.info("[analyze] dep_failed=%s", dep_failed)
    #print(f"[analyze] dep_classifications={dep_classifications!r}")
    #print(f"[analyze] final_deps={final_deps!r}")

    # -- Build detections dict from Phase 0 + Phase 2a ------------------------
    valid = await valid_technology_roles()
    valid_role_set = set(valid)
    detections = _empty_detections(valid)

    # Phase 0: file signals (confidence 1.0, detection_source="file_signal")
    for sig in file_signal_list:
        cat = sig["technology_role"]
        _place_tech(
            detections,
            DetectedTech(
                name             = sig["name"],
                confidence       = sig["confidence"],
                detection_source = "file_signal",
                technology_role         = cat if cat in valid else "infra",
                scope            = "required",
                usage_scope      = _coerce_usage_scope("runtime"),
                matched_file     = sig.get("matched_file"),
                file_count       = sig.get("file_count"),
            ),
            cat,
        )

    # Phase 2a: merged dep detections (base + Gemini enrichment).
    # Dedupe against Phase 0 - a tech found by BOTH a file signal and a manifest
    # becomes detection_source="both" rather than appearing twice.
    for dep in final_deps:
        cat  = dep.get("technology_role", "library")
        name = dep.get("name", "")
        if not name:
            continue

        target_cat = cat if cat in valid else "library"
        existing = next(
            (t for t in detections[target_cat] if t.name.lower() == name.lower()),
            None,
        )
        if existing:
            if dep.get("confidence", 0) > existing.confidence:
                existing.confidence = dep["confidence"]
            existing.detection_source = "both"
            existing.multi_role = dep.get("multi_role", False)
            existing.secondary_roles = dep.get("secondary_roles", [])
            existing.architectural_layer = _coerce_architectural_layer(
                dep.get("architectural_layer"),
            )
            existing.layer_inference_status = dep.get("layer_inference_status")
            existing.usage_scope = _coerce_usage_scope(dep.get("usage_scope"))
        else:
            _place_tech(
                detections,
                DetectedTech(
                    name             = name,
                    confidence       = dep.get("confidence", 0.80),
                    detection_source = dep.get("detection_source", "manifest"),
                    technology_role         = target_cat,
                    scope            = dep.get("scope", "required"),
                    matched_file     = dep.get("matched_file"),
                    multi_role       = dep.get("multi_role", False),
                    secondary_roles  = dep.get("secondary_roles", []),
                    architectural_layer = _coerce_architectural_layer(
                        dep.get("architectural_layer"),
                    ),
                    layer_inference_status = dep.get("layer_inference_status"),
                    usage_scope      = _coerce_usage_scope(dep.get("usage_scope")),
                ),
                cat,
            )

    # -------------------------------------------------------------------------
    # PHASE 2a.5: Re-add repo-owned package name from manifest metadata.
    # project_names come from [project].name (pyproject.toml) / name (package.json)
    # and were excluded from raw_deps as self-refs. Re-inject as a high-confidence
    # product signal for software-type grounding.
    _inject_self_project_techs(
        detections=detections,
        project_names=project_names,
        valid_roles=valid_role_set,
    )

    # -- The invariant, as a flag ---------------------------------------------
    # Phase 1 found deps but none reached the output => the pipeline dropped
    # data. This is the check that would have caught the empty next.js stack at
    # analysis time instead of a human noticing it in the UI a day later.
    drop_flag = assert_deps_survived(raw_deps, final_deps)
    if drop_flag:
        manifest_result["flags"].append(drop_flag)

    complexity_score     = _compute_complexity(detections)
    confidence_breakdown = {
        cat: round(sum(t.confidence for t in techs) / len(techs), 3)
        for cat, techs in detections.items()
        if techs
    }

    raw = {
        "detections":           detections,
        "pattern_matches":      [],
        "files_analyzed":       files_analyzed,
        "patterns_checked":     0,
        # Recomputed after ai_inferred_techs are merged below.
        "primary_language":     "",
        "complexity_score":     complexity_score,
        "confidence_breakdown": confidence_breakdown,
        "manifests_selected":   manifest_result["manifests_selected"],
        "flags":                manifest_result["flags"],
        "dep_classification_failed": dep_failed,   # for the flag/telemetry layer
        "dep_classification_fail_reason": dep_classification_fail_reason,
    }

    # ---------------------------------------------------------------------------
    # PHASE 2b: SoftwareType classification (stack insights are disabled)
    # Input: Phase 0 + Phase 2a detections (clean, no false positives)
    # Suppressed when files_analyzed < 4 to prevent fabricated insights.
    # ---------------------------------------------------------------------------
    if files_analyzed < 1:
        logger.info("[analyze] Phase 2b suppressed - only %s files", files_analyzed)
        ai = deepcopy(_AI_FAILED_DEFAULTS)
        ai["software_type_reasoning"] = (
            f"Suppressed: only {files_analyzed} files analyzed. "
            "Insights would be fabricated from insufficient evidence."
        )
    else:
        # Layer 0: trained software_type classifier (active after 50+ feedback items)
        try:
            from backend.services.learning_service import predict_software_type
            classifier_result = await predict_software_type(detections)
            if classifier_result and classifier_result["confidence"] >= 0.85:
                logger.info(
                    "[analyze] Layer 0 classifier: %s (%.2f) - advisory; Gemini still runs",
                    classifier_result["software_type"],
                    classifier_result["confidence"],
                )
        except Exception as e:
            logger.exception("[analyze] Layer 0 classifier failed")

        # Phase 2b: Gemini software_type classification
        await _emit_progress(
            phase="infer", level="ai", message="Querying Gemini for repository classification",
            provenance="ai", progress=0.72, verified_total=verified_total,
        )
        try:
            ai = await asyncio.wait_for(
                run_full_ai_pipeline(
                    detections,
                    repo.file_tree,
                    repo.name,
                    repo.description or "",
                    flags=[],
                ),
                timeout=SOFTWARE_TYPE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("[analyze] Phase 2b timed out after %ss", SOFTWARE_TYPE_TIMEOUT_SECONDS)
            ai = deepcopy(_AI_FAILED_DEFAULTS)
        except Exception as e:
            logger.exception("[analyze] Phase 2b exception: %s", e)
            ai = deepcopy(_AI_FAILED_DEFAULTS)

    gemini_prediction = {
        "software_type": ai.get("software_type", "unknown"),
        "confidence": ai.get("software_type_confidence", 0.0),
        "reasoning": ai.get("software_type_reasoning", ""),
    }
    inferred_total = 0
    if ai.get("software_type") and ai.get("software_type") != "unknown":
        inferred_total = 1
        await _emit_progress(
            phase="infer", level="ai",
            message=f"software_type → {ai.get('software_type')}", provenance="ai",
            field="software_type", value=ai.get("software_type"),
            confidence=ai.get("software_type_confidence"), progress=0.88,
            verified_total=verified_total, inferred_total=inferred_total,
        )

    # Cap overcalibrated confidence (1.0 means Gemini has no uncertainty)
    ai["software_type_confidence"] = min(ai.get("software_type_confidence", 0.0), 0.97)

    (
        ai["software_type"],
        ai["software_type_confidence"],
        ai["software_type_reasoning"],
        software_type_corrected_by_artifacts,
    ) = guard_library_classification(
        software_type=ai.get("software_type", "unknown"),
        confidence=ai.get("software_type_confidence", 0.0),
        reasoning=ai.get("software_type_reasoning", ""),
        classification=repository_classification,
        repo_name=repo.name,
        description=repo.description or "",
    )
    canonical_software_type = canonicalize_software_type(ai["software_type"]).value
    if canonical_software_type != ai["software_type"]:
        logger.debug(
            "[analyze] software_type canonicalized: %r -> %r",
            ai["software_type"],
            canonical_software_type,
        )
        ai["software_type"] = canonical_software_type
    if software_type_corrected_by_artifacts:
        logger.warning(
            "[analyze] Rejected repo-level library classification: %s",
            ai["software_type_reasoning"],
        )

    coercions = []
    if ai.get("coerced_from") and ai.get("coerced_from") != ai.get("software_type"):
        coercions.append({
            "before": ai["coerced_from"],
            "after": ai.get("software_type", "unknown"),
            "reason": ai.get("coercion_reason") or ai.get("rejection_reason"),
            "source": "taxonomy_gate",
        })
    if software_type_corrected_by_artifacts:
        before = gemini_prediction.get("software_type", "unknown")
        after = ai.get("software_type", "unknown")
        if before != after:
            coercions.append({
                "before": before,
                "after": after,
                "reason": ai.get("software_type_reasoning"),
                "source": "artifact_guard",
            })
    for coercion in coercions:
        await storage_service.append_correction_event(
            correction_field="software_type",
            pipeline_value=coercion["before"],
            proposed_value=coercion["after"],
            evidence={
                "repo": repo.full_name,
                "repo_key": repo_key,
                "analysis_id": f"{repo_key}|{head_sha}|{PIPELINE_VERSION}",
                "coercion_source": coercion["source"],
                "reason": coercion["reason"],
            },
            assignment_method="system_guard",
            actor="system",
            actor_kind="system",
            resolution="system_coercion",
            event_kind="system_coercion",
        )

    layer0_prediction = None
    software_type_disagreement = None
    if classifier_result:
        layer0_prediction = {
            "software_type": classifier_result.get("software_type", "unknown"),
            "confidence": classifier_result.get("confidence", 0.0),
            "advisory": True,
        }
        if layer0_prediction["software_type"] != ai["software_type"]:
            software_type_disagreement = {
                "layer0": layer0_prediction,
                "gemini": gemini_prediction,
                "selected_software_type": ai["software_type"],
                "selected_confidence": ai["software_type_confidence"],
                "guard_corrected": software_type_corrected_by_artifacts,
                "artifact_types": [
                    artifact.type.value
                    for artifact in repository_classification.artifacts
                ],
                # This becomes a supervised label only after human feedback.
                "training_status": "pending_human_validation",
            }
            await storage_service.record_software_type_disagreement(
                repo_key,
                head_sha,
                PIPELINE_VERSION,
                software_type_disagreement,
            )
            logger.info(
                "[analyze] Layer 0 disagreement: %s -> %s",
                layer0_prediction["software_type"],
                ai["software_type"],
            )

    # -- Self-reference filter on Gemini ai_inferences -------------------------
    # Gemini re-injects self-refs (HuggingFace for huggingface/transformers)
    # that Phase 1 manifest exclusion already caught. Strip them here.
    ai_inferences_clean, self_ref_excluded = filter_self_references_from_inferences(
        ai.get("ai_inferences", []),
        repo_full_name       = repo.full_name,
        parsed_project_names = project_names,
    )
    if self_ref_excluded:
        logger.info(
            "[analyze] Self-refs removed from ai_inferences: %s",
            self_ref_excluded,
        )
    ai["ai_inferences"]     = ai_inferences_clean
    ai["ai_inferred_techs"] = [
        t for t in ai.get("ai_inferred_techs", [])
        if t.get("name") not in self_ref_excluded
    ]

    # -- Merge Gemini ai_inferred_techs into detections ------------------------
    # Techs Gemini found from file tree / description / repo topics
    # that Phase 2a didn't find in manifests (no manifest declares Redis
    # but config files reference REDIS_URL -> Gemini infers Redis).
    merged: dict[str, list[DetectedTech]] = {
        cat: list(techs) for cat, techs in detections.items()
    }

    for inferred in ai.get("ai_inferred_techs", []):
        name     = inferred.get("name", "")
        technology_role = inferred.get("technology_role", "infra")
        if not name:
            continue
        if technology_role not in valid:
            technology_role = "infra"
        existing = next(
            (t for t in merged[technology_role] if t.name.lower() == name.lower()), None
        )
        if not existing:
            merged[technology_role].append(DetectedTech(
                name             = name,
                confidence       = inferred.get("confidence", 0.5),
                detection_source = "ai_inferred",
                technology_role         = technology_role,
                usage_scope      = _coerce_usage_scope("runtime"),
            ))

    # Reconcile detection_source:
    # "both" = independently confirmed by manifest AND Gemini ai_inferences
    ai_inferred_names = {
        inf.get("tech", "").lower()
        for inf in ai["ai_inferences"]
        if inf.get("tech")
    }
    for techs in merged.values():
        for tech in techs:
            if (tech.name.lower() in ai_inferred_names
                    and tech.detection_source in ("manifest", "file_signal")):
                tech.detection_source = "both"

    # Twins can occupy different role buckets, so deduplicate the assembled
    # stack globally before ownership and layer assignment.
    dedup_merges: list[dict] = []
    merged = dedup_stack(merged, on_merge=dedup_merges.append)
    for merge in dedup_merges:
        logger.info(
            "[analyze] dedup kept %r; removed twin %r",
            merge["kept"],
            merge["removed"],
        )

    # Filter missing_patterns - remove techs already in output
    all_detected_lower = {
        t.name.lower()
        for cat in merged
        for t in merged[cat]
    }
    missing_patterns_clean = [
        p for p in ai.get("missing_patterns", [])
        if p and p.lower() not in all_detected_lower
    ]

    # Primary language + language list: GitHub Linguist is authoritative.
    raw_langs = await github_service.get_languages(repo_key)
    linguist_langs = github_service.canonicalize_languages(raw_langs)

    if linguist_langs:
        # Scoped replacement: every non-language technology_role remains untouched.
        merged["languages"] = [
            DetectedTech(
                name=language["name"],
                technology_role="languages",
                confidence=1.0,
                detection_source="github_linguist",
                byte_count=language["byte_count"],
                byte_share=language["byte_share"],
            )
            for language in linguist_langs
            if not language["below_noise_floor"]
        ]
        primary_language = next(
            (language["name"] for language in linguist_langs if language["is_primary"]),
            "",
        )
    else:
        languages = merged.get("languages", [])
        primary_language = ""
        if languages:
            primary_language = max(
                languages,
                key=lambda tech: (
                    getattr(tech, "byte_count", 0) or 0,
                    getattr(tech, "file_count", 0) or 0,
                    getattr(tech, "confidence", 0.0) or 0.0,
                ),
            ).name

    raw["primary_language"] = primary_language

    processing_time_ms = int(time.time() * 1000 - start_ms)

    # Artifact ownership is structural, not semantic. A single-artifact repo
    # owns every detected technology. In a multi-artifact repo the resolver
    # assigns only recognized manifest paths; source/config signals stay null.
    for technologies in merged.values():
        if not isinstance(technologies, list):
            continue
        assign_artifact_ownership(
            [tech for tech in technologies if isinstance(tech, DetectedTech)],
            repository_classification,
        )
        assign_missing_architectural_layers(
            [tech for tech in technologies if isinstance(tech, DetectedTech)],
            repository_classification,
        )

    emitted_software_type = ai.get("software_type", "unknown")
    assignment_method = "ai_inferred"
    if ai.get("software_type_is_new"):
        assignment_method = "ai_inferred"
    elif layer0_prediction:
        assignment_method = "deterministic"
    software_type_decisions = await storage_service.get_software_type_feedback_decisions()
    if emitted_software_type in software_type_decisions["merged"]:
        resolved_software_type = software_type_decisions["merged"][emitted_software_type]
    elif emitted_software_type in software_type_decisions["discarded"]:
        resolved_software_type = "unknown"
    else:
        resolved_software_type = emitted_software_type
    if resolved_software_type != emitted_software_type:
        await storage_service.append_correction_event(
            correction_field="software_type",
            pipeline_value=emitted_software_type,
            proposed_value=resolved_software_type,
            evidence={
                "repo": repo.full_name,
                "repo_key": repo_key,
                "analysis_id": f"{repo_key}|{head_sha}|{PIPELINE_VERSION}",
                "coercion_source": "taxonomy_decision",
            },
            assignment_method="system_guard",
            actor="system",
            actor_kind="system",
            resolution="system_coercion",
            event_kind="system_coercion",
        )
    emergent_software_type = ai.get("emergent_software_type")
    if (
        not emergent_software_type
        and resolved_software_type not in await storage_service.get_valid_software_types()
    ):
        emergent_software_type = resolved_software_type
        await storage_service.record_emergent_software_type(resolved_software_type, repo.full_name)

    # Queue non-enum and ai-inferred emergent software types for maintainer review.
    if ai.get("software_type_is_new") or (emergent_software_type and ai.get("rejected")):
        await storage_service.create_review_item(
            kind="emergent_type",
            pipeline_value=emitted_software_type,
            proposed_value=emergent_software_type,
            source="ai_pipeline",
            assignment_method=assignment_method,
            evidence={
                "repo": repo.full_name,
                "analysis_id": repo_key,
                "source": "ai_pipeline",
                "seen_count": 1,
            },
            repo_key=repo_key,
        )

    repair_counters_delta = _repair_counters_delta(
        repair_counters_start,
        get_repair_counters(),
    )
    if repair_counters_delta:
        for site, stages in repair_counters_delta.items():
            for stage, count in stages.items():
                if count:
                    logger.info(
                        "[analyze] repair counter: site=%s stage=%s +%d",
                        site,
                        stage,
                        count,
                    )

    # -- Build StackAnalysis ---------------------------------------------------
    stack = StackAnalysis(
        languages             = merged["languages"],
        frameworks            = merged["frameworks"],
        databases             = merged["databases"],
        messaging             = merged["messaging"],
        ai_ml                 = merged["ai_ml"],
        infra                 = merged["infra"],
        testing               = merged["testing"],
        library               = merged.get("library", []),
        primary_language      = primary_language,
        complexity_score      = raw["complexity_score"],
        software_type                = resolved_software_type,
        software_type_ai             = gemini_prediction.get("software_type", "unknown"),
        software_type_ai_reasoning   = gemini_prediction.get("reasoning", ""),
        software_type_confidence     = ai.get("software_type_confidence", 0.0),
        software_type_reasoning      = ai.get("software_type_reasoning", ""),
        specific_identity            = ai.get("specific_identity"),
        architecture_style    = ai.get("architecture_style", "unknown"),
        why_this_stack        = ai.get("why_this_stack", ""),
        ecosystem_context     = ai.get("ecosystem_context", ""),
        stack_pattern         = ai.get("stack_pattern", "Custom"),
        notable_combinations  = ai.get("notable_combinations", []),
        missing_patterns      = missing_patterns_clean,
        dep_classification_failed = raw.get("dep_classification_failed", False),
        dep_classification_fail_reason = raw.get(
            "dep_classification_fail_reason",
            "none",
        ),
        repair_counters       = repair_counters_delta,
        ai_classification_used= ai.get("ai_classification_used", False),
        layer0_prediction      = layer0_prediction,
        software_type_disagreement    = software_type_disagreement,
        pattern_matches       = [],
        ai_inferences         = ai["ai_inferences"],
        confidence_breakdown  = raw["confidence_breakdown"],
        ai_calls_made         = ai.get("ai_calls_made", 0) + (
            1 if dep_classifications else 0
        ),
        files_analyzed        = raw["files_analyzed"],
        patterns_checked      = 0,
        processing_time_ms    = processing_time_ms,
        manifests_selected    = raw.get("manifests_selected", []),
    )

    # -- Quality flags ---------------------------------------------------------
    stack_dict = stack.model_dump()
    emergent_in_stack = [
        technology_role
        for technology_role, techs in merged.items()
        if technology_role not in valid_role_set and techs
    ]
    for technology_role in emergent_in_stack:
        stack_dict[technology_role] = [tech.model_dump() for tech in merged[technology_role]]
    stack_dict["emergent_technology_roles"] = emergent_in_stack
    stack_dict["emergent_software_type"] = emergent_software_type
    flags = compute_analysis_flags(
        stack               = stack_dict,
        pattern_matches     = [],
        files_analyzed      = raw["files_analyzed"],
        rag_repos_retrieved = ai.get("rag_repos_retrieved", 0),
    )

    # Manifest parse warnings
    manifest_flags = [
        {"code": code, "severity": "warning", "message": code,
         "field": "manifests_selected"}
        for code in raw.get("flags", [])
        if not code.startswith("SELF_REFERENCE_EXCLUDED")
    ]

    # Phase 2a fallback
    if unresolved_raw_deps and not dep_classifications:
        manifest_flags.append({
            "code":     "DEP_CLASSIFICATION_FAILED",
            "severity": "warning",
            "message":  f"Gemini dep classification unavailable ({dep_classification_fail_reason}) - using file signals only",
            "field":    "detections",
        })

    flags = [*flags, *manifest_flags]
    if flags:
        logger.info(
            "[analyze] %s flags: %s",
            len(flags),
            [f["code"] for f in flags],
        )

    # -- Confidence demotions --------------------------------------------------
    original_confidence = stack.software_type_confidence
    demoted_confidence = apply_confidence_demotions(original_confidence, flags)
    if demoted_confidence != original_confidence:
        logger.warning(
            "[analyze] Confidence demoted: %.2f -> %.2f",
            original_confidence,
            demoted_confidence,
        )
        stack.software_type_confidence = demoted_confidence
        try:
            stack.software_type_confidence_original = original_confidence
        except (AttributeError, TypeError, ValueError):
            logger.exception("[analyze] Failed to set software_type_confidence_original")
        if demoted_confidence < 0.50:
            stack.ai_classification_used = False
            stack.software_type_reasoning = (
                f"[DEMOTED {original_confidence:.2f}->{demoted_confidence:.2f}] "
                + stack.software_type_reasoning
            )

    # Apply corrective demotions (DEV_DEPENDENCY_AS_FRAMEWORK etc.)
    merged = apply_demotions_to_detections(merged, flags)
    stack.languages  = merged["languages"]
    stack.frameworks = merged["frameworks"]
    stack.databases  = merged["databases"]
    stack.messaging  = merged["messaging"]
    stack.ai_ml      = merged["ai_ml"]
    stack.infra      = merged["infra"]
    stack.testing    = merged["testing"]
    stack.library    = merged.get("library", [])

    try:
        stack.flags = flags
    except (AttributeError, TypeError):
        logger.exception("[analyze] Failed to set stack.flags")

    stack = stack.model_dump()
    for technology_role in emergent_in_stack:
        stack[technology_role] = [tech.model_dump() for tech in merged[technology_role]]
    stack["emergent_technology_roles"] = emergent_in_stack
    stack["emergent_software_type"] = emergent_software_type
    embedding = await embed_stack(stack)
    await storage_service.complete_refresh(
        repo_key,
        stack,
        embedding,
        head_sha,
        PIPELINE_VERSION,
        # to_repo_metadata(), not `repo`. RepoData is a pydantic model and bson
        # cannot encode it - passing it raised InvalidDocument.
        repo_metadata=to_repo_metadata(repo),
        repository_classification=repository_classification.model_dump(mode="json"),
    )
    logger.info("[analyze] repair counters delta=%s", repair_counters_delta)
    summary = _analysis_event_summary(stack, repair_counters=repair_counters_delta)
    await storage_service.record_event(repo_key, head_sha, PIPELINE_VERSION, summary)
    return stack


# -- Read endpoints ------------------------------------------------------------

@router.get("/analyses")
async def list_analyses(
    limit: int = 100,
    sort_by: Literal["time", "name"] = "time",
    sort_order: Literal["asc", "desc"] = "desc",
):
    """Return a compact registry for the repository-history UI."""
    docs = await storage_service.get_all_analyses()
    feedback_counts: dict[str, int] = {}
    for feedback in await storage_service.get_all_feedback():
        feedback_repo_key = feedback.get("repo_key")
        if feedback_repo_key:
            feedback_counts[feedback_repo_key] = feedback_counts.get(feedback_repo_key, 0) + 1
    rows = []
    for doc in docs:
        stack = doc.get("stack") or {}
        repo = doc.get("repo_metadata") or doc.get("repo") or {}
        rows.append({
            "analysis_id": doc.get("analysis_id") or doc.get("_id") or doc.get("repo_key"),
            "repo_key": doc.get("repo_key") or doc.get("_id"),
            "repo": repo,
            "software_type": stack.get("software_type") or "unknown",
            "confidence": stack.get("software_type_confidence"),
            "commit_sha": doc.get("commit_sha"),
            "pipeline_version": doc.get("pipeline_version"),
            "analyzed_at": doc.get("updated_at") or doc.get("created_at") or doc.get("analyzed_at"),
            "feedback_count": feedback_counts.get(doc.get("repo_key") or doc.get("_id"), 0),
        })

    def sort_value(row: dict) -> str:
        if sort_by == "name":
            repo = row.get("repo") or {}
            return str(repo.get("full_name") or repo.get("name") or row.get("repo_key") or "").casefold()
        return str(row.get("analyzed_at") or "")

    rows.sort(key=sort_value, reverse=sort_order == "desc")
    return {"analyses": rows[:max(1, min(limit, 500))], "count": len(docs)}

@router.get("/analyse/{id:path}", include_in_schema=False)
@router.get("/analyze/{id:path}")
async def get_analysis(id: str, repo_key: str = Depends(resolve_repo_key)):
    data = await storage_service.get_repo(repo_key)
    if not data:
        raise HTTPException(404, "Analysis not found or expired")
    return await _to_response(
        data,
        id,
        fresh=data.get("pipeline_version") == PIPELINE_VERSION,
        head_sha=data.get("commit_sha"),
    )


@router.get("/explain/{id:path}")
async def explain_analysis(id: str, repo_key: str = Depends(resolve_repo_key)):
    data = await storage_service.get_repo(repo_key)
    if not data:
        raise HTTPException(404, "Analysis not found or expired")
    stack = data.get("stack", {})
    return {
        "request_id": id,
        "repo_key": repo_key,
        "pattern_matches": stack.get("pattern_matches", []),
        "ai_inferences": stack.get("ai_inferences", []),
        "software_type_reasoning": stack.get("software_type_reasoning", ""),
        "specific_identity": stack.get("specific_identity"),
        "confidence_breakdown": stack.get("confidence_breakdown", {}),
        "ai_calls_made": stack.get("ai_calls_made", 0),
        "processing_time_ms": stack.get("processing_time_ms", 0),
        "patterns_checked": stack.get("patterns_checked", 0),
        "files_analyzed": stack.get("files_analyzed", 0),
    }


@router.get("/analyses/software_type/{software_type}")
async def analyses_by_software_type(software_type: str):
    results = await storage_service.find_by_software_type(software_type, limit=20)
    return {"software_type": software_type, "analyses": results, "count": len(results)}


@router.get("/analyses/pattern/{pattern}")
async def analyses_by_pattern(pattern: str):
    results = await storage_service.find_by_stack_pattern(pattern, limit=10)
    return {"pattern": pattern, "analyses": results}


@router.get("/analyses/similar/{id:path}")
async def similar_analyses(
    id: str,
    limit: int = 5,
    repo_key: str = Depends(resolve_repo_key),
):
    # with_embedding=True: get_repo projects stack_embedding out by default.
    data = await storage_service.get_repo(repo_key, with_embedding=True)
    if not data:
        raise HTTPException(404, "Analysis not found")

    async def with_effective_types(items: list[dict]) -> list[dict]:
        effective = []
        for item in items:
            item = dict(item)
            candidate_key = item.get("repo_key") or item.get("analysis_id")
            candidate_stack = dict(item.get("stack") or {})
            raw_type = candidate_stack.get("software_type") or item.get("software_type")
            correction = await storage_service.get_approved_software_type_correction(
                candidate_key, raw_type,
            )
            if correction:
                candidate_stack["software_type"] = correction["corrected_value"]
                candidate_stack["software_type_overlay"] = correction
                item["stack"] = candidate_stack
            effective.append(item)
        return effective

    raw_software_type = data.get("stack", {}).get("software_type", "unknown")
    correction = await storage_service.get_approved_software_type_correction(
        repo_key, raw_software_type,
    )
    effective_software_type = (
        correction.get("corrected_value") if correction else raw_software_type
    )

    effective_specific_identity = data.get("stack", {}).get("specific_identity")
    similar = await storage_service.find_similar_by_specific_identity_and_software_type(
        effective_specific_identity,
        effective_software_type,
        limit,
        exclude_repo_key=repo_key,
    )
    similar = await with_effective_types(similar)
    return {
        "request_id": id,
        "repo_key": repo_key,
        "software_type": effective_software_type,
        "specific_identity": effective_specific_identity,
        "similar": similar,
        "method": "specific_identity_software_type",
        "count": len(similar),
    }
