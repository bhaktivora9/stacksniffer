"""
backend/routers/analyze.py

4-Phase detection pipeline:

  Phase 0: File signals     — deterministic, <1ms, zero false positives
  Phase 1: Manifest extract — structural parsing, no classification
  Phase 2a: Dep classify    — Gemini classifies raw dep list (emergent technology_roles supported)
  Phase 2b + 3: SoftwareType + insights — Gemini software_type classification + stack insights

Emergent technology_roles (e.g. "bundler", "state_management") discovered by Phase 2a
are stored in MongoDB dep_technology_roles for human review via
POST /api/dep-technology_roles/{technology_role}/feedback (discard | merge | promote).
"""
import asyncio
import time
from copy import deepcopy
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException

import backend.services.github_service as github_service
from backend.models.schemas import (
    AnalyzeRequest,
    DetectedTech,
    StackAnalysis,
)
from backend.routers.deps import resolve_repo_key
from backend.services import storage_service
from backend.services.ai_pipeline import _json_model, run_full_ai_pipeline
from backend.services.artifact_classifier import (
    assign_artifact_ownership,
    classify_artifacts,
)
from backend.services.technology_role_registry import BUILTIN_TECHNOLOGY_ROLES, valid_technology_roles
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

router = APIRouter()

# Bump on ANY change to patterns.json, prompts, or the model.
# Later derive this from sha256(patterns.json + prompt templates + model name).
PIPELINE_VERSION = storage_service.PIPELINE_VERSION
DEP_CLASSIFICATION_TIMEOUT_SECONDS = 45.0

# Standard technology_roles — these map directly to StackAnalysis schema fields
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
        # Emergent technology_role — store in library for schema compatibility
        # emergent_technology_role field preserved for dep_technology_roles feedback loop
        try:
            tech.emergent_technology_role = emergent_technology_role or cat
        except Exception:
            pass
        tech.technology_role = "library"
        detections["library"].append(tech)


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
        return _to_response(doc, request_id, fresh=True, head_sha=head_sha)

    claimed = await storage_service.claim_refresh(repo_key, head_sha, PIPELINE_VERSION)

    if hard_refresh:
        if not claimed:
            doc = await _await_refresh(repo_key)
            if doc and storage_service.is_fresh(doc, head_sha, PIPELINE_VERSION):
                return _to_response(
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
        return _to_response(
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
        return _to_response(
            doc, request_id, fresh=False, refreshing=True, head_sha=head_sha
        )

    # COLD, and someone else owns the refresh. Do NOT run a second pipeline —
    # that was burning a duplicate ~50s Gemini run and racing on the same _id.
    # Wait for the owner to land a result.
    if not claimed:
        doc = await _await_refresh(repo_key)
        if doc and doc.get("stack"):
            return _to_response(doc, request_id, fresh=True, head_sha=head_sha)
        raise HTTPException(
            503,
            "Analysis in progress by another request and did not complete in time. Retry.",
        )

    # COLD and we own it: nothing to serve, so block.
    await _run_pipeline(repo_key, head_sha)
    doc = await storage_service.get_repo(repo_key)
    if not doc:
        raise HTTPException(500, "Analysis completed but no document was stored")
    return _to_response(doc, request_id, fresh=True, head_sha=head_sha)


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
        print(f"[analyze] background refresh failed for {repo_key}: {str(e)[:200]}")


def _to_response(
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
    kept until the frontend migrates — delete it once it reads request_id.
    """
    stack = doc.get("stack") or {}
    return {
        "request_id": request_id,
        "analysis_id": request_id,          # deprecated alias
        "repo_key": doc.get("repo_key") or doc.get("_id"),
        "repo": doc.get("repo") or {},
        "repository_classification": doc.get("repository_classification"),
        "stack": stack,
        "emergent_technology_roles": stack.get("emergent_technology_roles", []),
        "emergent_software_type": stack.get("emergent_software_type"),
        "commit_sha": doc.get("commit_sha"),
        "pipeline_version": doc.get("pipeline_version"),
        "fresh": fresh,
        "refreshing": refreshing,
        "hard_refresh": hard_refresh,
        "head_sha": head_sha or doc.get("commit_sha"),
        "created_at": doc.get("created_at"),
        "analyzed_at": doc.get("analyzed_at"),
    }

def _analysis_event_summary(stack: dict) -> dict:
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
        "stack_pattern": stack.get("stack_pattern"),
        "tech_names": tech_names,
        "quality_flags": stack.get("flags", []),
        "files_analyzed": stack.get("files_analyzed"),
        "ai_calls_made": stack.get("ai_calls_made"),
    }

async def _run_pipeline(repo_key: str, head_sha: str) -> dict:
    try:
        return await _run_pipeline_body(repo_key, head_sha)
    except Exception as e:
        await storage_service.fail_refresh(repo_key, str(e))
        raise


async def _run_pipeline_body(repo_key: str, head_sha: str) -> dict:
    start_ms = time.time() * 1000

    # GitHub ingestion
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

    repository_classification = classify_artifacts(repo)

    # ═══════════════════════════════════════════════════════════════════════════
    # PHASE 0: File signals — deterministic, <1ms, zero false positives
    # Dockerfile → Docker, go.mod → Go, .java files → Java
    # Each signal is a definitive filename — confidence 1.0
    # ═══════════════════════════════════════════════════════════════════════════
    file_signal_list = apply_file_signals(repo.file_tree)
    print(f"[analyze] Phase 0: {len(file_signal_list)} file signals "
          f"({[s['name'] for s in file_signal_list]})")

    # ═══════════════════════════════════════════════════════════════════════════
    # PHASE 1: Manifest extraction — structural parsing, no classification
    # Parses pyproject.toml, package.json, pom.xml, go.mod, Cargo.toml, etc.
    # Returns raw dep list: [{name, scope, origin, matched_file, version_spec}]
    # Self-references excluded at this stage.
    # ═══════════════════════════════════════════════════════════════════════════
    manifest_result = parse_manifest_dependencies(
        file_contents  = repo.file_contents,
        file_tree      = repo.file_tree,
        repo_full_name = repo.full_name,
    )
    raw_deps      = manifest_result["raw_deps"]
    project_names = manifest_result["project_names"]
    print(f"[analyze] Phase 1: {len(raw_deps)} raw deps from "
          f"{sum(1 for m in manifest_result['manifests_selected'] if m.get('parsed'))} "
          f"manifests")

# ═══════════════════════════════════════════════════════════════════════════
    # PHASE 2a: dependency classification — Gemini ENRICHES, never GATES
    #
    # The invariant: Phase 1 extracted N deps -> the output contains N deps.
    # Gemini can relabel a dep's technology_role and raise its confidence. It cannot
    # delete one. A dead Gemini call costs technology_role precision, not the stack.
    # (Three repos — fastapi, next.js, litellm — proved the old gate discards
    # everything on DEP_CLASSIFICATION_FAILED.)
    # ═══════════════════════════════════════════════════════════════════════════
    from backend.services.dep_fallback import (
        build_base_detections,
        collect_unresolved_tail,
        enrich_with_classifications,
        assert_deps_survived,
    )

    files_analyzed = len(repo.file_contents)

    # BASE first, unconditionally, no AI. This is what makes deps survive.
    # NOT gated on files_analyzed — raw_deps comes from manifest parsing, which
    # does not depend on file coverage.
    base_deps = build_base_detections(raw_deps)
    unresolved_raw_deps = collect_unresolved_tail(raw_deps, base_deps)

    dep_classifications: list[dict] = []
    dep_failed = False

    classifier_result = None
    if files_analyzed < 1:
        # Skip only the GEMINI call — the base is already built above.
        print(f"[analyze] Phase 2a (Gemini) skipped — only {files_analyzed} files fetched")
    elif unresolved_raw_deps:
        try:
            dep_classifications = await asyncio.wait_for(
                classify_dependencies(
                    raw_deps       = unresolved_raw_deps,
                    file_tree      = repo.file_tree,
                    repo_full_name = repo.full_name,
                    _json_model    = _json_model,
                ),
                timeout=DEP_CLASSIFICATION_TIMEOUT_SECONDS,
            )
            print(
                f"[analyze] Phase 2a: {len(dep_classifications)} of "
                f"{len(unresolved_raw_deps)} unresolved techs classified by Gemini"
            )
        except asyncio.TimeoutError:
            dep_failed = True
            print(
                f"[analyze] Phase 2a timed out after "
                f"{DEP_CLASSIFICATION_TIMEOUT_SECONDS:g}s - deterministic tiers only"
            )
        except Exception as e:
            dep_failed = True
            import traceback
            traceback.print_exc()   # you were losing the stack — this is why it fails
            print(f"[analyze] Phase 2a failed: {e} — deterministic tiers only")

    # Enrich the base with whatever Gemini returned. Empty list -> base passes
    # through untouched. This is the line that made the stack survivable.
    final_deps = enrich_with_classifications(base_deps, dep_classifications)

    print(f"[analyze] dep_failed={dep_failed}")
    #print(f"[analyze] dep_classifications={dep_classifications!r}")
    #print(f"[analyze] final_deps={final_deps!r}")

    # ── Build detections dict from Phase 0 + Phase 2a ────────────────────────
    valid = await valid_technology_roles()
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
                usage_scope      = "runtime",
                matched_file     = sig.get("matched_file"),
                file_count       = sig.get("file_count"),
            ),
            cat,
        )

    # Phase 2a: merged dep detections (base + Gemini enrichment).
    # Dedupe against Phase 0 — a tech found by BOTH a file signal and a manifest
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
            existing.architectural_layer = dep.get("architectural_layer")
            existing.layer_inference_status = dep.get("layer_inference_status")
            existing.usage_scope = dep.get("usage_scope")
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
                    architectural_layer = dep.get("architectural_layer"),
                    layer_inference_status = dep.get("layer_inference_status"),
                    usage_scope      = dep.get("usage_scope"),
                ),
                cat,
            )

    # ── The invariant, as a flag ─────────────────────────────────────────────
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
    }

    # ═══════════════════════════════════════════════════════════════════════════
    # PHASE 2b + 3: SoftwareType classification + stack insights
    # Input: Phase 0 + Phase 2a detections (clean, no false positives)
    # Suppressed when files_analyzed < 4 to prevent fabricated insights.
    # ═══════════════════════════════════════════════════════════════════════════
    if files_analyzed < 1:
        print(f"[analyze] Phase 2b suppressed — only {files_analyzed} files")
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
                print(
                    f"[analyze] Layer 0 classifier: {classifier_result['software_type']} "
                    f"({classifier_result['confidence']:.2f}) - advisory; Gemini still runs"
                )
        except Exception:
            pass

        # Phase 2b + 3: Gemini software_type + insights
        try:
            ai = await asyncio.wait_for(
                run_full_ai_pipeline(
                    detections,
                    repo.file_tree,
                    repo.name,
                    repo.description or "",
                    flags=[],
                ),
                timeout=90.0,
            )
        except asyncio.TimeoutError:
            print("[analyze] Phase 2b timed out after 90s")
            ai = deepcopy(_AI_FAILED_DEFAULTS)
        except Exception as e:
            print(f"[analyze] Phase 2b exception: {e}")
            ai = deepcopy(_AI_FAILED_DEFAULTS)

    gemini_prediction = {
        "software_type": ai.get("software_type", "unknown"),
        "confidence": ai.get("software_type_confidence", 0.0),
        "reasoning": ai.get("software_type_reasoning", ""),
    }

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
    if software_type_corrected_by_artifacts:
        print(
            "[analyze] Rejected repo-level library classification: "
            f"{ai['software_type_reasoning']}"
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
            print(
                "[analyze] Layer 0 disagreement: "
                f"{layer0_prediction['software_type']} -> {ai['software_type']}"
            )

    # ── Self-reference filter on Gemini ai_inferences ─────────────────────────
    # Gemini re-injects self-refs (HuggingFace for huggingface/transformers)
    # that Phase 1 manifest exclusion already caught. Strip them here.
    ai_inferences_clean, self_ref_excluded = filter_self_references_from_inferences(
        ai.get("ai_inferences", []),
        repo_full_name       = repo.full_name,
        parsed_project_names = project_names,
    )
    if self_ref_excluded:
        print(f"[analyze] Self-refs removed from ai_inferences: {self_ref_excluded}")
    ai["ai_inferences"]     = ai_inferences_clean
    ai["ai_inferred_techs"] = [
        t for t in ai.get("ai_inferred_techs", [])
        if t.get("name") not in self_ref_excluded
    ]

    # ── Merge Gemini ai_inferred_techs into detections ────────────────────────
    # Techs Gemini found from file tree / description / repo topics
    # that Phase 2a didn't find in manifests (no manifest declares Redis
    # but config files reference REDIS_URL → Gemini infers Redis).
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
                usage_scope      = "runtime",
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

    # Filter missing_patterns — remove techs already in output
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
    software_type_decisions = await storage_service.get_software_type_feedback_decisions()
    if emitted_software_type in software_type_decisions["merged"]:
        resolved_software_type = software_type_decisions["merged"][emitted_software_type]
    elif emitted_software_type in software_type_decisions["discarded"]:
        resolved_software_type = "unknown"
    else:
        resolved_software_type = emitted_software_type
    emergent_software_type = None
    if resolved_software_type not in await storage_service.get_valid_software_types():
        emergent_software_type = resolved_software_type
        await storage_service.record_emergent_software_type(resolved_software_type, repo.full_name)

    # ── Build StackAnalysis ───────────────────────────────────────────────────
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
        software_type_confidence     = ai.get("software_type_confidence", 0.0),
        software_type_reasoning      = ai.get("software_type_reasoning", ""),
        architecture_style    = ai.get("architecture_style", "unknown"),
        why_this_stack        = ai.get("why_this_stack", ""),
        ecosystem_context     = ai.get("ecosystem_context", ""),
        stack_pattern         = ai.get("stack_pattern", "Custom"),
        notable_combinations  = ai.get("notable_combinations", []),
        missing_patterns      = missing_patterns_clean,
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

    # ── Quality flags ─────────────────────────────────────────────────────────
    stack_dict = stack.model_dump()
    emergent_in_stack = [
        technology_role
        for technology_role, techs in merged.items()
        if technology_role not in BUILTIN_TECHNOLOGY_ROLES and techs
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
            "message":  "Gemini dep classification unavailable — using file signals only",
            "field":    "detections",
        })

    flags = [*flags, *manifest_flags]
    if flags:
        print(f"[analyze] {len(flags)} flags: {[f['code'] for f in flags]}")

    # ── Confidence demotions ──────────────────────────────────────────────────
    original_confidence = stack.software_type_confidence
    demoted_confidence  = apply_confidence_demotions(original_confidence, flags)
    if demoted_confidence != original_confidence:
        print(
            f"[analyze] Confidence demoted: "
            f"{original_confidence:.2f} → {demoted_confidence:.2f}"
        )
        stack.software_type_confidence = demoted_confidence
        try:
            stack.software_type_confidence_original = original_confidence
        except Exception:
            pass
        if demoted_confidence < 0.50:
            stack.ai_classification_used = False
            stack.software_type_reasoning = (
                f"[DEMOTED {original_confidence:.2f}→{demoted_confidence:.2f}] "
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
    except Exception:
        pass

    stack = stack.model_dump()
    for technology_role in emergent_in_stack:
        stack[technology_role] = [tech.model_dump() for tech in merged[technology_role]]
    stack["emergent_technology_roles"] = emergent_in_stack
    stack["emergent_software_type"] = emergent_software_type
    # Corrections BEFORE embedding: embedding the pre-correction stack makes the
    # vector encode the hallucination while the UI shows the correction.
    stack, touched = await storage_service.apply_corrections(repo_key, stack)
    embedding = await embed_stack(stack)
    await storage_service.complete_refresh(
        repo_key,
        stack,
        embedding,
        head_sha,
        PIPELINE_VERSION,
        # to_repo_metadata(), not `repo`. RepoData is a pydantic model and bson
        # cannot encode it — passing it raised InvalidDocument.
        repo_metadata=to_repo_metadata(repo),
        repository_classification=repository_classification.model_dump(mode="json"),
    )
    summary = _analysis_event_summary(stack)
    await storage_service.record_event(repo_key, head_sha, PIPELINE_VERSION, summary)
    return stack


# ── Read endpoints ────────────────────────────────────────────────────────────

@router.get("/analyse/{id}", include_in_schema=False)
@router.get("/analyze/{id}")
async def get_analysis(id: str, repo_key: str = Depends(resolve_repo_key)):
    data = await storage_service.get_repo(repo_key)
    if not data:
        raise HTTPException(404, "Analysis not found or expired")
    corrected_stack, _ = await storage_service.apply_corrections(
        repo_key, data.get("stack") or {}
    )
    data = {**data, "stack": corrected_stack}
    return _to_response(
        data,
        id,
        fresh=data.get("pipeline_version") == PIPELINE_VERSION,
        head_sha=data.get("commit_sha"),
    )


@router.get("/explain/{id}")
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


@router.get("/analyses/similar/{id}")
async def similar_analyses(
    id: str,
    limit: int = 5,
    repo_key: str = Depends(resolve_repo_key),
):
    # with_embedding=True: get_repo projects stack_embedding out by default.
    data = await storage_service.get_repo(repo_key, with_embedding=True)
    if not data:
        raise HTTPException(404, "Analysis not found")

    embedding = data.get("stack_embedding")
    if not embedding:
        software_type  = data.get("stack", {}).get("software_type", "unknown")
        similar = await storage_service.find_similar_by_software_type(software_type, limit)
        return {
            "request_id": id,
            "repo_key": repo_key,
            "similar":     similar,
            "method":      "software_type_fallback",
            "count":       len(similar),
            "note":        "No embedding — re-analyze to enable vector search.",
        }

    similar = await storage_service.find_similar(
        embedding,
        limit,
        exclude_repo_key=repo_key,
    )
    if not similar:
        software_type = data.get("stack", {}).get("software_type", "unknown")
        candidates = await storage_service.find_similar_by_software_type(software_type, limit + 1)
        similar = [
            item for item in candidates
            if item.get("repo_key") != repo_key and item.get("analysis_id") != repo_key
        ][:limit]
        return {
            "request_id": id,
            "repo_key": repo_key,
            "similar": similar,
            "method": "software_type_fallback",
            "count": len(similar),
            "note": "Vector search returned no matches; using software_type similarity.",
        }
    return {
        "request_id": id,
        "repo_key": repo_key,
        "similar":     similar,
        "method":      "vector_search",
        "count":       len(similar),
    }
