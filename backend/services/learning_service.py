"""
backend/services/learning_service.py
Pattern confidence learning from human feedback.
This is the RLHF feedback loop — human signal updates pattern weights.
Also contains the classifier training pipeline.
"""
import json
import logging
import asyncio
from os import getenv
from pathlib import Path
from collections import defaultdict

logger = logging.getLogger(__name__)

PATTERNS_PATH = Path("backend/config/patterns.json")

# Learning rate for confidence updates
# Low = stable but slow to learn. High = fast but noisy.
REWARD_DELTA  = 0.02   # confidence += 0.02 when correct
PENALTY_DELTA = 0.05   # confidence -= 0.05 when wrong (penalize more than reward)
MIN_CONFIDENCE = 0.10
MAX_CONFIDENCE = 0.99

UNRELIABLE_FILES = (
    ".github/workflows",
    ".github/scripts",
    "docs/",
    "README",
    "CONTRIBUTING",
)


def _load_patterns() -> dict:
    with open(PATTERNS_PATH) as f:
        return json.load(f)


def _save_patterns(patterns: dict) -> None:
    with open(PATTERNS_PATH, "w") as f:
        json.dump(patterns, f, indent=2)


def _find_pattern_by_keyword(patterns: dict, keyword: str) -> tuple[str, dict] | tuple[None, None]:
    """Find a pattern entry by its keyword. Returns (category, pattern_dict)."""
    for category, entries in patterns.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if keyword in entry.get("keywords", []):
                return category, entry
    return None, None


def _find_pattern_by_tech(patterns: dict, tech_name: str) -> tuple[str, dict] | tuple[None, None]:
    """Find a pattern entry by tech name."""
    for category, entries in patterns.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if entry.get("name", "").lower() == tech_name.lower():
                return category, entry
    return None, None


def _pm_get(pm, key: str, default=""):
    if isinstance(pm, dict):
        return pm.get(key, default)
    return getattr(pm, key, default)


def _reliable_matches(pattern_matches: list) -> list:
    """Only learn from keyword matches in reliable evidence files."""
    reliable = []
    for pm in pattern_matches:
        matched_file = _pm_get(pm, "matched_file", "") or ""
        matched_keyword = _pm_get(pm, "matched_keyword", "") or ""
        if not matched_keyword:
            continue
        if any(pattern in matched_file for pattern in UNRELIABLE_FILES):
            continue
        reliable.append(pm)
    return reliable


async def reward_patterns(pattern_matches: list[dict]) -> int:
    """
    Increase confidence for patterns that fired correctly.
    Called when human confirms analysis was correct.
    Returns number of patterns updated.
    """
    patterns = _load_patterns()
    updated = 0

    for pm in _reliable_matches(pattern_matches):
        keyword = _pm_get(pm, "matched_keyword", "")
        _, entry = _find_pattern_by_keyword(patterns, keyword)
        if entry:
            old = entry["confidence"]
            entry["confidence"] = min(
                MAX_CONFIDENCE,
                round(old + REWARD_DELTA, 3)
            )
            if entry["confidence"] != old:
                updated += 1
                logger.debug(
                    "Rewarded pattern '%s' (%s → %s)",
                    entry["name"], old, entry["confidence"]
                )

    if updated > 0:
        _save_patterns(patterns)
        logger.info("Rewarded %d patterns", updated)

    return updated


async def penalize_patterns(
    pattern_matches: list[dict],
    wrong_techs: list[str] = None
) -> int:
    """
    Decrease confidence for patterns that fired incorrectly.
    Called when human says analysis was wrong.
    If wrong_techs specified: only penalize those specific techs.
    Otherwise: penalize all patterns that fired.
    Returns number of patterns updated.
    """
    patterns = _load_patterns()
    updated = 0
    wrong_techs_lower = [t.lower() for t in (wrong_techs or [])]

    for pm in _reliable_matches(pattern_matches):
        keyword = _pm_get(pm, "matched_keyword", "")
        tech = _pm_get(pm, "tech", "")

        # If specific wrong techs given, only penalize those
        if wrong_techs and tech.lower() not in wrong_techs_lower:
            continue

        _, entry = _find_pattern_by_keyword(patterns, keyword)
        if entry:
            old = entry["confidence"]
            entry["confidence"] = max(
                MIN_CONFIDENCE,
                round(old - PENALTY_DELTA, 3)
            )
            if entry["confidence"] != old:
                updated += 1
                logger.debug(
                    "Penalized pattern '%s' (%s → %s)",
                    entry["name"], old, entry["confidence"]
                )

    if updated > 0:
        _save_patterns(patterns)
        logger.info("Penalized %d patterns", updated)

    return updated


async def compute_pattern_accuracy_from_corpus() -> dict:
    """
    Aggregate pattern accuracy from all feedback in MongoDB.
    This is a batch learning step — run nightly or on demand.
    Returns per-keyword accuracy stats.
    """
    from backend.services import storage_service as storage_service_rag

    all_feedback = await storage_service_rag.get_all_feedback()
    if not all_feedback:
        return {}

    feedback_map = {f["analysis_id"]: f for f in all_feedback}
    analyses = await storage_service_rag.get_all_analyses()

    keyword_stats = defaultdict(lambda: {"fires": 0, "correct": 0, "tech": ""})

    for analysis in analyses:
        feedback = feedback_map.get(analysis.get("analysis_id"))
        if not feedback:
            continue

        wrong_techs = [t.lower() for t in feedback.get("techs_wrong", [])]
        domain_correct = feedback.get("domain_correct", True)

        for pm in _reliable_matches(analysis.get("stack", {}).get("pattern_matches", [])):
            keyword = _pm_get(pm, "matched_keyword", "")
            tech = _pm_get(pm, "tech", "")

            keyword_stats[keyword]["fires"] += 1
            keyword_stats[keyword]["tech"] = tech

            # Correct if: domain was right AND this tech wasn't flagged as wrong
            is_correct = domain_correct and tech.lower() not in wrong_techs
            if is_correct:
                keyword_stats[keyword]["correct"] += 1

    # Compute accuracy per keyword
    return {
        kw: {
            "tech": stats["tech"],
            "fires": stats["fires"],
            "accuracy": round(stats["correct"] / stats["fires"], 3),
            "correct": stats["correct"]
        }
        for kw, stats in keyword_stats.items()
        if stats["fires"] >= 3  # minimum sample size
    }


async def update_patterns_from_corpus(min_samples: int = 3) -> dict:
    """
    Batch update: recompute all pattern confidence scores from feedback corpus.
    Run this nightly. More data = more accurate confidence scores.
    This IS model training — fitting a statistical model to observed accuracy.
    """
    accuracy = await compute_pattern_accuracy_from_corpus()
    if not accuracy:
        return {"updated": 0, "message": "No feedback data available"}

    patterns = _load_patterns()
    updated = 0
    deltas = []

    for category, entries in patterns.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            for keyword in entry.get("keywords", []):
                if keyword in accuracy:
                    stats = accuracy[keyword]
                    if stats["fires"] >= min_samples:
                        old_conf = entry["confidence"]
                        new_conf = round(stats["accuracy"], 3)
                        entry["confidence"] = new_conf
                        if abs(old_conf - new_conf) > 0.01:
                            updated += 1
                            deltas.append({
                                "tech": entry["name"],
                                "keyword": keyword,
                                "old": old_conf,
                                "new": new_conf,
                                "delta": round(new_conf - old_conf, 3),
                                "samples": stats["fires"]
                            })

    if updated > 0:
        _save_patterns(patterns)

    # Sort by biggest change
    deltas.sort(key=lambda x: abs(x["delta"]), reverse=True)

    return {
        "updated": updated,
        "total_keywords_evaluated": len(accuracy),
        "significant_changes": deltas[:10],
        "message": f"Updated {updated} pattern confidence scores from {len(accuracy)} keyword samples"
    }


async def get_learning_stats() -> dict:
    """
    Returns current learning state — used by /api/learning/stats endpoint.
    Shows what the system has learned from feedback.
    """
    from backend.services import storage_service as storage_service_rag

    feedback = await storage_service_rag.get_all_feedback()
    analyses = await storage_service_rag.get_all_analyses()
    accuracy = await compute_pattern_accuracy_from_corpus()

    patterns = _load_patterns()
    total_patterns = sum(
        len(entries) for entries in patterns.values()
        if isinstance(entries, list)
    )

    # Find patterns that deviate most from default confidence
    # (evidence that learning has changed them)
    changed_patterns = []
    DEFAULT_CONFIDENCE = 0.90  # approximate baseline
    for category, entries in patterns.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            conf = entry.get("confidence", DEFAULT_CONFIDENCE)
            if abs(conf - DEFAULT_CONFIDENCE) > 0.05:
                changed_patterns.append({
                    "tech": entry["name"],
                    "category": category,
                    "confidence": conf,
                    "direction": "increased" if conf > DEFAULT_CONFIDENCE else "decreased"
                })

    return {
        "corpus_size": len(analyses),
        "feedback_collected": len(feedback),
        "patterns_total": total_patterns,
        "patterns_changed_by_learning": len(changed_patterns),
        "keyword_accuracy_computed": len(accuracy),
        "top_changed_patterns": sorted(
            changed_patterns,
            key=lambda x: abs(x["confidence"] - DEFAULT_CONFIDENCE),
            reverse=True
        )[:5],
        "training_pipeline": {
            "status": "active" if len(feedback) >= 5 else "collecting_data",
            "next_batch_update": "on demand via POST /api/learning/update",
            "min_samples_for_batch": 3,
            "current_feedback": len(feedback),
            "needed_for_classifier_training": max(0, 50 - len(feedback))
        }
    }


# ── Classifier training (Phase 3 — needs 50+ labeled samples) ────────────────

async def train_domain_classifier() -> dict:
    """
    Train a lightweight domain classifier from labeled corpus.
    Uses sklearn LogisticRegression on feature vectors extracted from analyses.
    Requires: pip install scikit-learn numpy
    
    Only meaningful with 50+ labeled samples.
    Returns training metrics.
    """
    from backend.services import storage_service as storage_service_rag

    labeled_data = await storage_service_rag.get_labeled_training_data()

    if len(labeled_data) < 10:
        return {
            "status": "insufficient_data",
            "samples": len(labeled_data),
            "needed": 50,
            "message": f"Need {50 - len(labeled_data)} more labeled samples. Submit feedback on more analyses."
        }

    try:
        import numpy as np
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import cross_val_score
        from sklearn.preprocessing import LabelEncoder
        import pickle

        # Feature extraction from stored analyses
        # Using embeddings if available, otherwise hand-crafted features
        X = []
        y = []

        for item in labeled_data:
            label = item.get("correct_domain", item.get("detected_domain", "unknown"))
            if label == "unknown":
                continue

            # Use embedding vector if available
            embedding = item.get("stack_embedding")
            if embedding and len(embedding) == 3072 and any(v != 0.0 for v in embedding):
                features = embedding
            else:
                # Fallback: hand-crafted features
                features = _extract_features(item)

            X.append(features)
            y.append(label)

        if len(X) < 10:
            return {
                "status": "insufficient_valid_data",
                "message": "Not enough samples with valid features"
            }

        X = np.array(X)
        le = LabelEncoder()
        y_encoded = le.fit_transform(y)

        # Train with cross-validation
        clf = LogisticRegression(max_iter=1000, C=1.0, multi_class="multinomial")
        cv_scores = cross_val_score(clf, X, y_encoded, cv=min(5, len(X)//2))
        clf.fit(X, y_encoded)

        # Save model
        model_path = Path("backend/models/domain_classifier.pkl")
        model_path.parent.mkdir(exist_ok=True)
        with open(model_path, "wb") as f:
            pickle.dump({"classifier": clf, "label_encoder": le}, f)

        logger.info("Trained domain classifier: %.2f accuracy", cv_scores.mean())

        return {
            "status": "trained",
            "samples": len(X),
            "classes": list(le.classes_),
            "cv_accuracy": round(float(cv_scores.mean()), 3),
            "cv_std": round(float(cv_scores.std()), 3),
            "model_path": str(model_path),
            "message": (
                f"Classifier trained on {len(X)} samples. "
                f"CV accuracy: {cv_scores.mean():.0%} ± {cv_scores.std():.0%}. "
                f"Deploy when accuracy > 80%."
            )
        }

    except ImportError:
        return {
            "status": "missing_dependencies",
            "message": "Run: pip install scikit-learn numpy"
        }
    except Exception as e:
        logger.error("Classifier training failed: %s", e)
        return {"status": "error", "message": str(e)}


def _extract_features(item: dict) -> list[float]:
    """
    Hand-crafted feature vector from analysis metadata.
    Used when embeddings aren't available.
    Binary features: [has_python, has_java, has_go, has_kafka, has_react, ...]
    """
    techs_detected = set()

    # Collect all detected tech names
    for cat in ["languages", "frameworks", "databases", "messaging", "ai_ml", "infra"]:
        for t in item.get(cat, []):
            if isinstance(t, dict):
                techs_detected.add(t.get("name", "").lower())
            else:
                techs_detected.add(str(t).lower())

    feature_map = [
        "python", "java", "go", "rust", "typescript", "javascript",
        "fastapi", "django", "flask", "spring boot", "express", "nestjs",
        "react", "next.js", "vue",
        "postgresql", "mongodb", "redis", "elasticsearch",
        "kafka", "rabbitmq",
        "langchain", "openai", "anthropic claude", "vertex ai", "huggingface",
        "docker", "kubernetes", "github actions",
        "pytest", "jest", "junit"
    ]

    return [1.0 if tech in techs_detected else 0.0 for tech in feature_map]


async def predict_domain(stack: dict) -> dict | None:
    """
    Use trained classifier to predict domain.
    Returns None if no model trained yet.
    Falls back gracefully — never raises.
    """
    model_path = Path("backend/models/domain_classifier.pkl")
    if not model_path.exists():
        return None

    try:
        import pickle
        import numpy as np

        with open(model_path, "rb") as f:
            saved = pickle.load(f)

        clf = saved["classifier"]
        le = saved["label_encoder"]

        # Try embedding first
        from backend.services.embedding_service import embed_stack
        embedding = await embed_stack(stack)

        if any(v != 0.0 for v in embedding):
            X = np.array([embedding])
        else:
            X = np.array([_extract_features(stack)])

        proba = clf.predict_proba(X)[0]
        best_idx = proba.argmax()
        domain = le.inverse_transform([best_idx])[0]
        confidence = float(proba[best_idx])

        return {
            "domain": domain,
            "confidence": confidence,
            "source": "trained_classifier",
            "all_probabilities": {
                le.inverse_transform([i])[0]: round(float(p), 3)
                for i, p in enumerate(proba)
            }
        }

    except Exception as e:
        logger.warning("Classifier prediction failed: %s", e)
        return None
