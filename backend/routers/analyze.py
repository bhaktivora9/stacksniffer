"""
backend/routers/analyze.py
"""
import asyncio
import time
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from backend.models.schemas import (
    AnalysisResult,
    AnalyzeRequest,
    DetectedTech,
    ExplainabilityReport,
    StackAnalysis,
)
import backend.services.storage_service as storage_service
from backend.services.ai_pipeline import run_full_ai_pipeline
from backend.services.github_service import (
    GitHubRateLimitError,
    RepoNotFoundError,
    fetch_repo,
)
from backend.services.stack_detector import detect_stack
from backend.services.quality_flags import (
    apply_confidence_demotions,
    apply_demotions_to_detections,
    compute_analysis_flags,
)
from backend.services.manifest_parser import filter_self_references_from_inferences

router = APIRouter()

_VALID_CATEGORIES = {
    "languages", "frameworks", "databases",
    "messaging", "ai_ml", "infra", "testing", "library"
}

_AI_FAILED_DEFAULTS = {
    "domain":               "unknown",
    "domain_confidence":    0.0,
    "domain_reasoning":     "AI classification timed out or was skipped",
    "architecture_style":   "unknown",
    "missing_patterns":     [],
    "ai_inferred_techs":    [],
    "why_this_stack":       "",
    "stack_pattern":        "Custom",
    "ecosystem_context":    "",
    "notable_combinations": [],
    "ai_inferences":        [],
    "ai_classification_used": False,
    "ai_calls_made":        0,
    "rag_repos_retrieved":  0,
    "embedding":            [],
}


@router.post("/analyse", response_model=AnalysisResult, include_in_schema=False)
@router.post("/analyze", response_model=AnalysisResult)
async def analyze_repository(request: AnalyzeRequest):
    start_ms = time.time() * 1000

    # ── Cache check ───────────────────────────────────────────────────────────
    try:
        full_name = request.repo_url.split("github.com/")[-1].rstrip("/")
        cached = await storage_service.find_cached_repo(full_name, max_age_hours=1)
        if cached:
            return AnalysisResult(**cached)
    except Exception:
        pass

    # ── GitHub ingestion ──────────────────────────────────────────────────────
    try:
        repo = await fetch_repo(request.repo_url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RepoNotFoundError as e:
        raise HTTPException(404, str(e))
    except GitHubRateLimitError as e:
        raise HTTPException(429, {
            "detail": "GitHub API rate limit exceeded",
            "retry_after_seconds": e.retry_after,
            "fix": (
                "Add GITHUB_TOKEN to .env. Unauthenticated limit is 60 req/hr; "
                "authenticated limit is 5000 req/hr."
            ),
        })
    except Exception as e:
        err = str(e)
        if "403" in err or "rate limit" in err.lower():
            raise HTTPException(429, "GitHub API rate limit. Add GITHUB_TOKEN to .env.")
        raise HTTPException(500, f"GitHub fetch failed: {err[:200]}")

    # ── Pass 1: Pattern detection ─────────────────────────────────────────────
    raw = detect_stack(repo.file_contents, repo.file_tree, repo.full_name, repo.topics)

    # ── Bug 1 fix: gate AI pipeline and classifier under one coverage check ───
    # All AI work is suppressed when files_analyzed < 4 to prevent
    # fabricated insights from polluting RLHF training data.
    if raw["files_analyzed"] < 4:
        print(f"[analyze] Suppressing AI — only {raw['files_analyzed']} files analyzed")
        ai = _AI_FAILED_DEFAULTS.copy()
        ai["domain_reasoning"] = (
            f"Suppressed: only {raw['files_analyzed']} files analyzed. "
            "Insights would be fabricated from insufficient evidence."
        )
    else:
        # ── Layer 0: Trained classifier (if available) ────────────────────────
        classifier_result = None
        try:
            from backend.services.learning_service import predict_domain
            classifier_result = await predict_domain(raw["detections"])
            if classifier_result and classifier_result["confidence"] >= 0.85:
                print(
                    f"[analyze] Layer 0 classifier: {classifier_result['domain']} "
                    f"({classifier_result['confidence']:.2f}) — skipping Gemini domain call"
                )
        except Exception:
            pass

        # ── Pass 2: AI pipeline (RAG + Gemini) ───────────────────────────────
        try:
            ai = await asyncio.wait_for(
                run_full_ai_pipeline(
                    raw["detections"],
                    repo.file_tree,
                    repo.name,
                    repo.description or "",
                    flags=[],  # pre-pass flags; quality flags computed after
                ),
                timeout=90.0,
            )
        except asyncio.TimeoutError:
            print("[analyze] AI pipeline timed out after 90s")
            ai = _AI_FAILED_DEFAULTS.copy()
        except Exception as e:
            print(f"[analyze] AI pipeline exception: {e}")
            ai = _AI_FAILED_DEFAULTS.copy()

        # Override domain with classifier if confident
        if classifier_result and classifier_result["confidence"] >= 0.85:
            ai["domain"]              = classifier_result["domain"]
            ai["domain_confidence"]   = classifier_result["confidence"]
            ai["domain_reasoning"]    = f"Trained classifier: {classifier_result['confidence']:.0%}"
            ai["ai_classification_used"] = True

    # ── Cap overcalibrated confidence ─────────────────────────────────────────
    ai["domain_confidence"] = min(ai.get("domain_confidence", 0.0), 0.97)

    # ── Bug 2 fix: filter self-references from Gemini ai_inferences ──────────
    # Gemini re-injects self-references (HuggingFace for huggingface/transformers)
    # that manifest_parser already excluded at the structural parse level.
    ai_inferences_clean, self_ref_excluded = filter_self_references_from_inferences(
        ai.get("ai_inferences", []),
        repo_full_name=repo.full_name,
    )
    if self_ref_excluded:
        print(f"[analyze] Self-references removed from ai_inferences: {self_ref_excluded}")
    ai["ai_inferences"] = ai_inferences_clean

    # Also remove from ai_inferred_techs (used in merge step below)
    ai["ai_inferred_techs"] = [
        t for t in ai.get("ai_inferred_techs", [])
        if t.get("name") not in self_ref_excluded
    ]

    # ── Merge pattern_match + ai_inferred detections ─────────────────────────
    merged: dict[str, list[DetectedTech]] = {
        cat: list(techs) for cat, techs in raw["detections"].items()
    }

    for inferred in ai.get("ai_inferred_techs", []):
        name = inferred.get("name", "")
        if not name:
            continue
        category = inferred.get("category", "infra")
        if category not in _VALID_CATEGORIES:
            category = "infra"
        existing = next(
            (t for t in merged[category] if t.name.lower() == name.lower()), None
        )
        if not existing:
            merged[category].append(DetectedTech(
                name=name,
                confidence=inferred.get("confidence", 0.5),
                detection_source="ai_inferred",
                category=category,
            ))

    # Reconcile detection_source: "both" = pattern fired AND AI independently inferred
    ai_inferred_names = {
        inf.get("tech", "").lower()
        for inf in ai["ai_inferences"]
        if inf.get("tech")
    }
    for techs in merged.values():
        for tech in techs:
            if tech.name.lower() in ai_inferred_names and tech.detection_source == "pattern_match":
                tech.detection_source = "both"

    # ── Bug 3 fix: filter missing_patterns against merged detections ──────────
    # Gemini lists techs as "missing" that were actually detected via ai_inferred.
    # After merge, anything already in the output array is NOT missing.
    all_detected_lower = {
        t.name.lower()
        for cat in merged
        for t in merged[cat]
    }
    missing_patterns_clean = [
        p for p in ai.get("missing_patterns", [])
        if p and p.lower() not in all_detected_lower
    ]

    processing_time_ms = int(time.time() * 1000 - start_ms)

    # ── Build StackAnalysis ───────────────────────────────────────────────────
    stack = StackAnalysis(
        languages=merged["languages"],
        frameworks=merged["frameworks"],
        databases=merged["databases"],
        messaging=merged["messaging"],
        ai_ml=merged["ai_ml"],
        infra=merged["infra"],
        testing=merged["testing"],
        library=merged.get("library", []),
        primary_language=raw["primary_language"],
        complexity_score=raw["complexity_score"],
        domain=ai.get("domain", "unknown"),
        domain_confidence=ai.get("domain_confidence", 0.0),
        domain_reasoning=ai.get("domain_reasoning", ""),
        architecture_style=ai.get("architecture_style", "unknown"),
        why_this_stack=ai.get("why_this_stack", ""),
        ecosystem_context=ai.get("ecosystem_context", ""),
        stack_pattern=ai.get("stack_pattern", "Custom"),
        notable_combinations=ai.get("notable_combinations", []),
        missing_patterns=missing_patterns_clean,        # Bug 3 fix
        ai_classification_used=ai.get("ai_classification_used", False),
        pattern_matches=raw["pattern_matches"],
        ai_inferences=ai["ai_inferences"],
        confidence_breakdown=raw["confidence_breakdown"],
        ai_calls_made=ai.get("ai_calls_made", 0),
        files_analyzed=raw["files_analyzed"],
        patterns_checked=raw["patterns_checked"],
        processing_time_ms=processing_time_ms,
        manifests_selected=raw.get("manifests_selected", []),
    )

    # ── Compute quality flags ─────────────────────────────────────────────────
    stack_dict = stack.model_dump()
    flags = compute_analysis_flags(
        stack=stack_dict,
        pattern_matches=raw["pattern_matches"],
        files_analyzed=raw["files_analyzed"],
        rag_repos_retrieved=ai.get("rag_repos_retrieved", 0),
    )
    manifest_flags = [
        {"code": code, "severity": "warning", "message": code, "field": "manifests_selected"}
        for code in raw.get("flags", [])
    ]
    flags = [*flags, *manifest_flags]
    if flags:
        print(f"[analyze] {len(flags)} quality flags: {[f['code'] for f in flags]}")

    # ── Bug 4 fix: apply_confidence_demotions ────────────────────────────────
    # Error-severity flags reduce domain_confidence by 0.15 per flag (floor 0.10).
    # This must happen BEFORE storing the result so the stored confidence reflects
    # actual evidence quality, not the raw Gemini output.
    original_confidence = stack.domain_confidence
    demoted_confidence  = apply_confidence_demotions(original_confidence, flags)
    if demoted_confidence != original_confidence:
        print(
            f"[analyze] Confidence demoted: {original_confidence:.2f} → "
            f"{demoted_confidence:.2f} ({len([f for f in flags if f.get('demote')])} error flags)"
        )
        stack.domain_confidence = demoted_confidence
        # Store original for audit trail
        try:
            stack.domain_confidence_original = original_confidence
        except Exception:
            pass
        # If confidence drops below 0.50 — classification is unreliable
        if demoted_confidence < 0.50:
            stack.ai_classification_used = False
            stack.domain_reasoning = (
                f"[DEMOTED {original_confidence:.2f}→{demoted_confidence:.2f}] "
                + stack.domain_reasoning
            )

    # Apply corrective demotions to tech arrays
    # DEV_DEPENDENCY_AS_FRAMEWORK flag removes flagged techs from output
    merged = apply_demotions_to_detections(merged, flags)
    stack.languages  = merged["languages"]
    stack.frameworks = merged["frameworks"]
    stack.databases  = merged["databases"]
    stack.messaging  = merged["messaging"]
    stack.ai_ml      = merged["ai_ml"]
    stack.infra      = merged["infra"]
    stack.testing    = merged["testing"]
    stack.library    = merged.get("library", [])

    # Attach flags
    try:
        stack.flags = flags
    except Exception:
        pass

    analysis_id = str(uuid4())
    result      = AnalysisResult(analysis_id=analysis_id, repo=repo, stack=stack)
    result_dict = result.model_dump()

    # ── Enriched post-AI embedding (non-blocking) ─────────────────────────────
    enriched_embedding = []
    try:
        from backend.services.embedding_service import embed_stack
        enriched_stack = {
            "languages":  [t.model_dump() for t in merged["languages"]],
            "frameworks": [t.model_dump() for t in merged["frameworks"]],
            "databases":  [t.model_dump() for t in merged["databases"]],
            "messaging":  [t.model_dump() for t in merged["messaging"]],
            "ai_ml":      [t.model_dump() for t in merged["ai_ml"]],
            "infra":      [t.model_dump() for t in merged["infra"]],
            "library":    [t.model_dump() for t in merged.get("library", [])],
            "domain":               ai.get("domain", "unknown"),
            "architecture_style":   ai.get("architecture_style", "unknown"),
            "stack_pattern":        ai.get("stack_pattern", ""),
            "why_this_stack":       ai.get("why_this_stack", ""),
            "ecosystem_context":    ai.get("ecosystem_context", ""),
            "notable_combinations": ai.get("notable_combinations", []),
        }
        enriched_embedding = await embed_stack(enriched_stack)
        non_zero = sum(1 for v in enriched_embedding if v != 0.0)
        print(f"[analyze] Enriched embedding: dim={len(enriched_embedding)} non_zero={non_zero}")
    except Exception as e:
        print(f"[analyze] Enriched embedding failed: {e}")
        enriched_embedding = ai.get("embedding", [])

    final_embedding = (
        enriched_embedding
        if enriched_embedding and any(v != 0.0 for v in enriched_embedding)
        else ai.get("embedding", [])
    )

    if final_embedding and any(v != 0.0 for v in final_embedding):
        asyncio.create_task(
            storage_service.store_analysis_with_embedding(
                analysis_id, result_dict, final_embedding
            )
        )
    else:
        await storage_service.store_analysis(analysis_id, result_dict)

    return result


@router.get("/analyse/{analysis_id}", response_model=AnalysisResult, include_in_schema=False)
@router.get("/analyze/{analysis_id}", response_model=AnalysisResult)
async def get_analysis(analysis_id: str):
    data = await storage_service.get_analysis(analysis_id)
    if not data:
        raise HTTPException(404, "Analysis not found or expired")
    return AnalysisResult(**data)


@router.get("/explain/{analysis_id}", response_model=ExplainabilityReport)
async def explain_analysis(analysis_id: str):
    data = await storage_service.get_analysis(analysis_id)
    if not data:
        raise HTTPException(404, "Analysis not found or expired")
    result = AnalysisResult(**data)
    s = result.stack
    return ExplainabilityReport(
        analysis_id=analysis_id,
        pattern_matches=s.pattern_matches,
        ai_inferences=s.ai_inferences,
        domain_reasoning=s.domain_reasoning,
        confidence_breakdown=s.confidence_breakdown,
        ai_calls_made=s.ai_calls_made,
        processing_time_ms=s.processing_time_ms,
        patterns_checked=s.patterns_checked,
        files_analyzed=s.files_analyzed,
    )


@router.get("/analyses/domain/{domain}")
async def analyses_by_domain(domain: str):
    results = await storage_service.find_by_domain(domain, limit=20)
    return {"domain": domain, "analyses": results, "count": len(results)}


@router.get("/analyses/pattern/{pattern}")
async def analyses_by_pattern(pattern: str):
    results = await storage_service.find_by_stack_pattern(pattern, limit=10)
    return {"pattern": pattern, "analyses": results}


@router.get("/analyses/similar/{analysis_id}")
async def similar_analyses(analysis_id: str, limit: int = 5):
    data = await storage_service.get_analysis(analysis_id)
    if not data:
        raise HTTPException(404, "Analysis not found")

    embedding = data.get("stack_embedding")
    if not embedding:
        domain  = data.get("stack", {}).get("domain", "unknown")
        similar = await storage_service.find_similar_by_domain(domain, limit)
        return {
            "analysis_id": analysis_id,
            "similar":     similar,
            "method":      "domain_fallback",
            "count":       len(similar),
            "note":        "No embedding stored. Re-analyze to enable vector search."
        }

    similar = await storage_service.find_similar(embedding, limit + 1)
    similar = [s for s in similar if s.get("analysis_id") != analysis_id][:limit]
    return {
        "analysis_id": analysis_id,
        "similar":     similar,
        "method":      "vector_search",
        "count":       len(similar)
    }