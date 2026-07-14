"""
backend/routers/learning.py

Learning pipeline status and trigger endpoints.
Java equivalent: part of HealthController + PatternUpdateServiceImpl triggers.

Exposes the self-learning pipeline state:
  - Corpus size, feedback collected, pattern confidence changes
  - Manual trigger for batch pattern update
  - Manual trigger for classifier training
  - Per-keyword empirical accuracy from feedback

Java equivalent events:
  PatternConfigUpdatedEvent → published by DynamicPatternConfigService
  PatternsReloadedEvent → published when patterns.json reloaded
  DetectionServiceListener → consumes UsageTrackingEvent
"""
import asyncio

from fastapi import APIRouter, BackgroundTasks
from backend.services import learning_service
from backend.services import storage_service

router = APIRouter(prefix="/api/learning", tags=["learning"])


@router.get("/stats")
async def learning_stats():
    """
    Current state of the self-learning pipeline.

    Key fields:
      corpus_size              — total analyses in MongoDB
      feedback_collected       — RLHF signals submitted
      patterns_changed_by_learning — patterns whose confidence diverged from default
      training_pipeline.status — "active" (50+ feedback) or "collecting_data"

    Java equivalent: MonitoringStats DTO populated by UsageTrackingServiceImpl
    + PatternPerformanceMetrics from stacksniffer-learning.
    """
    return await learning_service.get_learning_stats()


@router.post("/update-patterns")
async def update_patterns(background_tasks: BackgroundTasks):
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
        "message": "Pattern confidence update running in background. Check /api/learning/stats."
    }


@router.post("/train-classifier")
async def train_classifier():
    """
    Train sklearn LogisticRegression domain classifier from labeled corpus.
    Requires 50+ feedback samples for meaningful accuracy.

    Once trained, activates Layer 0 in analyze.py — high-confidence analyses
    skip the Gemini domain classification call entirely.

    Expected accuracy by corpus size:
      50 samples:  ~60-70% CV accuracy (underfitting)
      200 samples: ~78-85% CV accuracy (viable for Layer 0 gating)
      500 samples: ~87-92% CV accuracy (production-viable)

    Java equivalent: FrequentPatternMiner + ML pipeline in stacksniffer-learning.
    Uses embeddings if available, falls back to hand-crafted feature vector.
    """
    return await learning_service.train_domain_classifier()

@router.post("/reembed-corpus")
async def reembed_corpus():
    """
    Re-embed all stored analyses with enriched post-AI fingerprints.
    Run once after deploying the enriched embedding change.
    Background task — returns immediately.
    """
    async def _reembed():
        from backend.services.embedding_service import embed_stack, is_valid_embedding
        analyses = await storage_service.get_all_analyses()
        updated = 0
        for a in analyses:
            try:
                stack = a.get("stack", {})
                # Skip if already has a rich embedding (domain not unknown)
                if stack.get("domain", "unknown") == "unknown":
                    continue
                enriched_stack = {
                    "languages":    stack.get("languages", []),
                    "frameworks":   stack.get("frameworks", []),
                    "databases":    stack.get("databases", []),
                    "messaging":    stack.get("messaging", []),
                    "ai_ml":        stack.get("ai_ml", []),
                    "infra":        stack.get("infra", []),
                    "testing":      stack.get("testing", []),
                    "domain":              stack.get("domain", "unknown"),
                    "architecture_style":  stack.get("architecture_style", "unknown"),
                    "stack_pattern":       stack.get("stack_pattern", ""),
                    "why_this_stack":      stack.get("why_this_stack", ""),
                    "ecosystem_context":   stack.get("ecosystem_context", ""),
                    "notable_combinations": stack.get("notable_combinations", []),
                }
                new_embedding = await embed_stack(enriched_stack)
                if is_valid_embedding(new_embedding):
                    await storage_service.store_analysis_with_embedding(
                        a["analysis_id"], a, new_embedding
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
