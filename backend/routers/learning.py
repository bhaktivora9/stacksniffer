"""
backend/routers/learning.py

Learning pipeline status and trigger endpoints.
Java equivalent: part of HealthController + PatternUpdateServiceImpl triggers.

Exposes the self-learning pipeline state:
  - Corpus size, feedback collected, and correction ledger events
  - Advisory Layer 0 classifier shadow-mode agreement metrics
  - Manual trigger for classifier retraining
  - Pattern update and per-keyword accuracy diagnostics

Java equivalent events:
  PatternConfigUpdatedEvent → published by DynamicPatternConfigService
  PatternsReloadedEvent → published when patterns.json reloaded
  DetectionServiceListener → consumes UsageTrackingEvent
"""
import asyncio

from fastapi import APIRouter, BackgroundTasks, Depends
from routers.admin_auth import require_admin
from services import learning_service
from services import storage_service

router = APIRouter(prefix="/api/learning", tags=["learning"])


@router.get("/stats")
async def learning_stats():
    """
    Current state of the self-learning pipeline.

    Key fields:
      corpus_size              — total stored analyses
      total_analyses           — storage-reported analysis count
      feedback_collected       — software_type feedback snapshots submitted
      layer0_agreement_rate    — advisory classifier agreement with primary path
      training_pipeline.status — "ready" or "collecting_data"

    Java equivalent: MonitoringStats DTO populated by UsageTrackingServiceImpl.
    """
    return await learning_service.get_learning_stats()


@router.get("/events")
async def learning_events(limit: int = 500):
    """Approved human corrections and observed system coercions only."""
    events = await storage_service.get_correction_events(limit=limit)
    feedback_counts = {
        "software_type": 0,
        "technology_role": 0,
        "architectural_layer": 0,
    }
    for event in events:
        field = event.get("correction_field")
        if event.get("actor_kind") == "user" and field in feedback_counts:
            feedback_counts[field] += 1
    ledger = [
        event for event in events
        if event.get("resolution") == "approved"
        or event.get("event_kind") == "system_coercion"
    ]
    return {
        "events": ledger,
        "count": len(ledger),
        "feedback_counts": feedback_counts,
        "feedback_count_total": sum(feedback_counts.values()),
        "scope": "approved_human_corrections_and_system_coercions",
    }


@router.post("/update-patterns")
async def update_patterns(
    background_tasks: BackgroundTasks,
    _auth: str = Depends(require_admin),
):
    """
    Batch recompute all pattern confidence scores from feedback corpus.
    Runs in background. Returns immediately.

    This IS model training — fitting a statistical model (pattern accuracy)
    to observed human preference data.

    Java equivalent: PatternUpdateServiceImpl.updatePatternConfig()
    triggered by PatternConfigUpdatedEvent.
    Writes to patterns.json (Python) vs YAML hot-reload (Java DynamicPatternConfigService).
    """
    background_tasks.add_task(learning_service.update_patterns_from_corpus)
    return {
        "status": "started",
        "message": "Pattern confidence update running in background. Check /api/learning/pattern-accuracy."
    }


@router.post("/train-classifier")
async def train_classifier(_auth: str = Depends(require_admin)):
    """
    Train sklearn LogisticRegression software_type classifier from labeled corpus.
    Requires 50+ feedback samples for meaningful accuracy.

    Once trained, activates advisory Layer 0 in analyze.py. The classifier
    prediction is recorded for diagnostics/disagreement tracking; Gemini still
    runs and remains the final software_type classifier path.

    Expected accuracy by corpus size:
      50 samples:  ~60-70% CV accuracy (underfitting)
      200 samples: ~78-85% CV accuracy (viable for Layer 0 gating)
      500 samples: ~87-92% CV accuracy (production-viable)

    Java equivalent: FrequentPatternMiner + ML pipeline in stacksniffer-learning.
    Uses embeddings if available, falls back to hand-crafted feature vector.
    """
    return await learning_service.train_software_type_classifier()

@router.post("/reembed-corpus")
async def reembed_corpus(_auth: str = Depends(require_admin)):
    """
    Re-embed all stored analyses with enriched post-AI fingerprints.
    Run once after deploying the enriched embedding change.
    Background task — returns immediately.
    """
    async def _reembed():
        from services.embedding_service import embed_stack, is_valid_embedding
        analyses = await storage_service.get_all_analyses()
        updated = 0
        for a in analyses:
            try:
                stack = a.get("stack", {})
                # Skip if already has a rich embedding (software_type not unknown)
                if stack.get("software_type", "unknown") == "unknown":
                    continue
                enriched_stack = {
                    "languages":    stack.get("languages", []),
                    "frameworks":   stack.get("frameworks", []),
                    "databases":    stack.get("databases", []),
                    "messaging":    stack.get("messaging", []),
                    "ai_ml":        stack.get("ai_ml", []),
                    "infra":        stack.get("infra", []),
                    "testing":      stack.get("testing", []),
                    "software_type":              stack.get("software_type", "unknown"),
                    "architecture_style":  stack.get("architecture_style", "unknown"),
                    "stack_pattern":       stack.get("stack_pattern", ""),
                    "why_this_stack":      stack.get("why_this_stack", ""),
                    "ecosystem_context":   stack.get("ecosystem_context", ""),
                    "notable_combinations": stack.get("notable_combinations", []),
                }
                new_embedding = await embed_stack(enriched_stack)
                if is_valid_embedding(new_embedding):
                    repo_key = a.get("repo_key") or a.get("analysis_id")
                    await storage_service.upsert_repo_analysis(
                        repo_key,
                        stack,
                        new_embedding,
                        a["commit_sha"],
                        a["pipeline_version"],
                    )
                    updated += 1
                    await asyncio.sleep(0.5)  # rate limit Gemini embeddings
            except Exception as e:
                print(f"[reembed] Failed {a.get('analysis_id')}: {e}")

        print(f"[reembed] Complete — {updated}/{len(analyses)} re-embedded")

    asyncio.create_task(_reembed())
    return {
        "status": "started",
        "message": "Re-embedding corpus in background. Check logs for progress."
    }

@router.get("/pattern-accuracy")
async def pattern_accuracy():
    """
    Per-keyword empirical accuracy computed from feedback corpus.
    Shows which detection patterns are reliable vs noisy.

    Java equivalent: PatternPerformanceMetrics + PatternValidationServiceImpl
    + PatternUsageStats in stacksniffer-learning.

    low_accuracy_patterns (<0.70): candidates for keyword refinement
    high_accuracy_patterns (>=0.90): reliable — safe to increase confidence
    """
    accuracy = await learning_service.compute_pattern_accuracy_from_corpus()
    if not accuracy:
        return {
            "message": "No feedback data yet. Submit feedback via UI or POST /api/feedback/{id}.",
            "total_keywords": 0,
            "low_accuracy_patterns": [],
            "high_accuracy_patterns": []
        }

    sorted_patterns = sorted(
        [{"keyword": kw, **stats} for kw, stats in accuracy.items()],
        key=lambda x: x["accuracy"]
    )
    return {
        "total_keywords":          len(accuracy),
        "low_accuracy_patterns":   [p for p in sorted_patterns if p["accuracy"] < 0.7],
        "high_accuracy_patterns":  [p for p in sorted_patterns if p["accuracy"] >= 0.9],
        "all_patterns":            sorted_patterns
    }
