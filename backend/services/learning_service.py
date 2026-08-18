"""
backend/services/learning_service.py

Two distinct subsystems live here, and it's important not to confuse them:

  1. patterns.json confidence learning (reward/penalize/update_patterns_*)
     LEGACY. These operate on `pattern_matches`, which the AI-first pipeline
     no longer produces — it emits []. So every function here is a silent
     no-op today. They are kept for the eventual return of a keyword layer,
     but each now reports `active: False` instead of pretending to work, so a
     caller can't mistake "did nothing" for "succeeded". Delete this whole
     block if the keyword scanner is truly gone.

  2. SoftwareType classifier (train/predict) + regression tracking
     LIVE. Trains on frozen feedback snapshots (rated_embedding), never joins
     against mutable analyses_result.
"""
import json
import logging
from os import getenv
from pathlib import Path
from collections import Counter, defaultdict

logger = logging.getLogger(__name__)

PATTERNS_PATH = Path("backend/config/patterns.json")

REWARD_DELTA = 0.02
PENALTY_DELTA = 0.05
MIN_CONFIDENCE = 0.10
MAX_CONFIDENCE = 0.99

# Classifier training thresholds — one source of truth, not scattered literals.
MIN_LABELED_SAMPLES = 50      # below this, don't train at all
MIN_FEATURE_SAMPLES = 10      # below this, features too sparse to bother
MIN_CLASS_FOR_CV = 2          # cross_val_score needs >= 2 examples per class
MAX_CV_FOLDS = 5
MIN_CV_FOLDS = 2          # cross_val_score's floor

UNRELIABLE_FILES = (
    ".github/workflows",
    ".github/scripts",
    "docs/",
    "README",
    "CONTRIBUTING",
)

EMBEDDING_DIM = 3072


# ── patterns.json helpers (LEGACY) ───────────────────────────────────────


def _patterns_available() -> bool:
    return PATTERNS_PATH.exists()


def _load_patterns() -> dict:
    if not PATTERNS_PATH.exists():
        return {}
    with open(PATTERNS_PATH) as f:
        return json.load(f)


def _save_patterns(patterns: dict) -> None:
    with open(PATTERNS_PATH, "w") as f:
        json.dump(patterns, f, indent=2)


def _find_pattern_by_keyword(patterns: dict, keyword: str):
    for technology_role, entries in patterns.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if keyword in entry.get("keywords", []):
                return technology_role, entry
    return None, None


def _find_pattern_by_tech(patterns: dict, tech_name: str):
    for technology_role, entries in patterns.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if entry.get("name", "").lower() == tech_name.lower():
                return technology_role, entry
    return None, None


def _pm_get(pm, key: str, default=""):
    if isinstance(pm, dict):
        return pm.get(key, default)
    return getattr(pm, key, default)


def _reliable_matches(pattern_matches: list) -> list:
    reliable = []
    for pm in pattern_matches or []:
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
    LEGACY no-op in the AI-first pipeline: pattern_matches is always [].
    Guarded so a missing patterns.json can't crash the feedback route.
    """
    if not _patterns_available():
        return 0
    patterns = _load_patterns()
    updated = 0
    for pm in _reliable_matches(pattern_matches):
        keyword = _pm_get(pm, "matched_keyword", "")
        _, entry = _find_pattern_by_keyword(patterns, keyword)
        if entry:
            old = entry["confidence"]
            entry["confidence"] = min(MAX_CONFIDENCE, round(old + REWARD_DELTA, 3))
            if entry["confidence"] != old:
                updated += 1
    if updated:
        _save_patterns(patterns)
        logger.info("Rewarded %d patterns", updated)
    return updated


async def penalize_patterns(pattern_matches: list[dict], wrong_techs: list[str] = None) -> int:
    """LEGACY no-op in the AI-first pipeline: pattern_matches is always []."""
    if not _patterns_available():
        return 0
    patterns = _load_patterns()
    updated = 0
    wrong_techs_lower = [t.lower() for t in (wrong_techs or [])]
    for pm in _reliable_matches(pattern_matches):
        keyword = _pm_get(pm, "matched_keyword", "")
        tech = _pm_get(pm, "tech", "")
        if wrong_techs and tech.lower() not in wrong_techs_lower:
            continue
        _, entry = _find_pattern_by_keyword(patterns, keyword)
        if entry:
            old = entry["confidence"]
            entry["confidence"] = max(MIN_CONFIDENCE, round(old - PENALTY_DELTA, 3))
            if entry["confidence"] != old:
                updated += 1
    if updated:
        _save_patterns(patterns)
        logger.info("Penalized %d patterns", updated)
    return updated


async def compute_pattern_accuracy_from_corpus() -> dict:
    """
    Aggregate keyword accuracy from SELF-CONTAINED feedback snapshots only.

    Never opens analyses_result — that collection is mutable and overwritten on
    re-analysis, so joining against it pairs today's stack with last month's
    rating. Everything here reads feedback["rated_output"], which is frozen at
    the moment the human rated it.

    NOTE: also a de-facto no-op today, since rated_output["pattern_matches"] is
    [] in the AI-first pipeline. Returns {} rather than erroring.
    """
    from backend.services import storage_service

    all_feedback = await storage_service.get_all_feedback()
    if not all_feedback:
        return {}

    keyword_stats = defaultdict(lambda: {"fires": 0, "correct": 0, "tech": ""})

    for feedback in all_feedback:
        rated_output = feedback.get("rated_output") or {}
        if not rated_output:
            continue
        # feedback fields are promoted to the top level by store_feedback; fall
        # back to the nested blob for rows written before that promotion.
        fb = feedback.get("feedback") or feedback
        wrong_techs = [t.lower() for t in fb.get("techs_wrong", [])]
        software_type_correct = fb.get("software_type_correct", True)

        for pm in _reliable_matches(rated_output.get("pattern_matches", [])):
            keyword = _pm_get(pm, "matched_keyword", "")
            tech = _pm_get(pm, "tech", "")
            keyword_stats[keyword]["fires"] += 1
            keyword_stats[keyword]["tech"] = tech
            if software_type_correct and tech.lower() not in wrong_techs:
                keyword_stats[keyword]["correct"] += 1

    return {
        kw: {
            "tech": s["tech"],
            "fires": s["fires"],
            "accuracy": round(s["correct"] / s["fires"], 3),
            "correct": s["correct"],
        }
        for kw, s in keyword_stats.items()
        if s["fires"] >= 3
    }


async def update_patterns_from_corpus(min_samples: int = 3) -> dict:
    """LEGACY. No-op while pattern_matches is empty; returns a clear message."""
    if not _patterns_available():
        return {"updated": 0, "active": False, "message": "patterns.json not present"}

    accuracy = await compute_pattern_accuracy_from_corpus()
    if not accuracy:
        return {
            "updated": 0,
            "active": False,
            "message": (
                "No keyword accuracy data. The AI-first pipeline does not emit "
                "pattern_matches, so this legacy path has nothing to learn from."
            ),
        }

    patterns = _load_patterns()
    updated = 0
    deltas = []
    for technology_role, entries in patterns.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            for keyword in entry.get("keywords", []):
                if keyword in accuracy and accuracy[keyword]["fires"] >= min_samples:
                    old_conf = entry["confidence"]
                    new_conf = round(accuracy[keyword]["accuracy"], 3)
                    entry["confidence"] = new_conf
                    if abs(old_conf - new_conf) > 0.01:
                        updated += 1
                        deltas.append({
                            "tech": entry["name"], "keyword": keyword,
                            "old": old_conf, "new": new_conf,
                            "delta": round(new_conf - old_conf, 3),
                            "samples": accuracy[keyword]["fires"],
                        })
    if updated:
        _save_patterns(patterns)
    deltas.sort(key=lambda x: abs(x["delta"]), reverse=True)
    return {
        "updated": updated,
        "active": True,
        "total_keywords_evaluated": len(accuracy),
        "significant_changes": deltas[:10],
        "message": f"Updated {updated} pattern confidence scores from {len(accuracy)} keyword samples",
    }


# ── Learning stats (LIVE) ────────────────────────────────────────────────


async def get_learning_stats() -> dict:
    from backend.services import storage_service

    feedback = await storage_service.get_all_feedback()
    analyses = await storage_service.get_all_analyses()
    storage_stats = await storage_service.get_stats()
    events = await storage_service.get_analysis_events()
    disagreements = await storage_service.get_software_type_disagreements()
    # Count samples that could actually train a classifier — labeled AND with a
    # usable embedding. "50 feedback rows" of empty-embedding rows trains nothing.
    trainable = sum(
        1 for f in feedback
        if (f.get("rated_embedding") or f.get("stack_embedding"))
        and (f.get("correct_software_type") or (f.get("rated_output") or {}).get("software_type"))
    )
    layer0_analyses = [
        analysis for analysis in analyses
        if ((analysis.get("stack") or {}).get("layer0_prediction"))
    ]

    def _analysis_key(row: dict) -> tuple[str | None, str | None, str | None]:
        return (
            row.get("repo_key") or row.get("analysis_id") or row.get("_id"),
            row.get("commit_sha"),
            row.get("pipeline_version"),
        )

    def _confidence_band(confidence) -> str:
        if confidence is None:
            return "unknown"
        try:
            value = float(confidence)
        except (TypeError, ValueError):
            return "unknown"
        if value >= 0.8:
            return "high"
        if value >= 0.5:
            return "medium"
        return "low"

    layer0_by_key = {}
    for analysis in layer0_analyses:
        key = _analysis_key(analysis)
        if all(key):
            layer0_by_key[key] = analysis

    layer0_total = len(layer0_by_key)
    confidence_bands = {
        "high": {"label": "High confidence", "min_confidence": 0.8, "total": 0, "disagreements": 0},
        "medium": {"label": "Medium confidence", "min_confidence": 0.5, "max_confidence": 0.8, "total": 0, "disagreements": 0},
        "low": {"label": "Low confidence", "max_confidence": 0.5, "total": 0, "disagreements": 0},
        "unknown": {"label": "Unknown confidence", "total": 0, "disagreements": 0},
    }
    for analysis in layer0_by_key.values():
        prediction = (analysis.get("stack") or {}).get("layer0_prediction") or {}
        band = _confidence_band(prediction.get("confidence"))
        confidence_bands[band]["total"] += 1

    layer0_disagreement_rows = []
    for row in disagreements:
        key = _analysis_key(row)
        if row.get("layer0") and all(key) and key in layer0_by_key:
            layer0_disagreement_rows.append(row)
            prediction = (layer0_by_key[key].get("stack") or {}).get("layer0_prediction") or {}
            band = _confidence_band(prediction.get("confidence"))
            confidence_bands[band]["disagreements"] += 1

    layer0_disagreement_count = len(layer0_disagreement_rows)
    layer0_agreements = max(0, layer0_total - layer0_disagreement_count)
    layer0_agreement_rate = (
        layer0_agreements / layer0_total
        if layer0_total
        else None
    )
    for band in confidence_bands.values():
        band["agreements"] = max(0, band["total"] - band["disagreements"])
        band["agreement_rate"] = (
            band["agreements"] / band["total"]
            if band["total"]
            else None
        )

    disagreement_examples = []
    for row in sorted(layer0_disagreement_rows, key=lambda item: str(item.get("created_at") or ""), reverse=True)[:25]:
        key = _analysis_key(row)
        layer0 = row.get("layer0") or {}
        current_layer0 = ((layer0_by_key.get(key) or {}).get("stack") or {}).get("layer0_prediction") or {}
        gemini = row.get("gemini") or {}
        disagreement_examples.append({
            "repo_key": row.get("repo_key"),
            "commit_sha": row.get("commit_sha"),
            "pipeline_version": row.get("pipeline_version"),
            "layer0_software_type": layer0.get("software_type"),
            "layer0_confidence": current_layer0.get("confidence", layer0.get("confidence")),
            "layer0_confidence_band": _confidence_band(current_layer0.get("confidence", layer0.get("confidence"))),
            "primary_software_type": row.get("selected_software_type") or gemini.get("software_type"),
            "primary_confidence": row.get("selected_confidence") or gemini.get("confidence"),
            "guard_corrected": bool(row.get("guard_corrected")),
            "training_status": row.get("training_status"),
            "created_at": row.get("created_at"),
        })

    return {
        "corpus_size": len(analyses),
        "total_analyses": storage_stats.get("total_analyses", len(analyses)),
        "feedback_collected": len(feedback),
        "trainable_samples": trainable,
        "repos_with_feedback": len({f.get("repo_key") for f in feedback if f.get("repo_key")}),
        "corrections_count": await storage_service.count_corrections(),
        "layer0_predictions_total": layer0_total,
        "layer0_agreements": layer0_agreements,
        "layer0_disagreements": layer0_disagreement_count,
        "layer0_agreement_rate": layer0_agreement_rate,
        "layer0_confidence_bands": confidence_bands,
        "layer0_disagreement_examples": disagreement_examples,
        "layer0_pending_human_validation": sum(
            1
            for row in layer0_disagreement_rows
            if row.get("training_status") == "pending_human_validation"
        ),
        "layer0": {
            "mode": "advisory_shadow",
            "predictions_total": layer0_total,
            "agreements": layer0_agreements,
            "disagreements": layer0_disagreement_count,
            "agreement_rate": layer0_agreement_rate,
            "confidence_bands": confidence_bands,
            "disagreement_examples": disagreement_examples,
        },
        "pipeline_versions_seen": sorted(
            {e.get("pipeline_version") for e in events if e.get("pipeline_version")}
        ),
        "training_pipeline": {
            "status": "ready" if trainable >= MIN_LABELED_SAMPLES else "collecting_data",
            "current_feedback": len(feedback),
            "trainable_samples": trainable,
            "needed_for_classifier_training": max(0, MIN_LABELED_SAMPLES - trainable),
            "min_samples_for_batch": 3,
        },
    }


# ── SoftwareType classifier (LIVE) ─────────────────────────────────────────────


def _extract_features(item: dict) -> list[float]:
    """Binary tech-presence vector. Fallback when no embedding is available."""
    techs_detected = set()
    # rated_output holds the stack for feedback rows; item itself for raw stacks.
    source = item.get("rated_output") or item
    for techs in source.values():
        if not isinstance(techs, list):
            continue
        for t in techs:
            if not isinstance(t, dict) or not t.get("technology_role"):
                continue
            name = t.get("name", "") if isinstance(t, dict) else str(t)
            techs_detected.add(name.lower())

    feature_map = [
        "python", "java", "go", "rust", "typescript", "javascript",
        "fastapi", "django", "flask", "spring boot", "express", "nestjs",
        "react", "next.js", "vue",
        "postgresql", "mongodb", "redis", "elasticsearch",
        "kafka", "rabbitmq",
        "langchain", "openai", "anthropic claude", "vertex ai", "huggingface",
        "docker", "kubernetes", "github actions",
        "pytest", "jest", "junit",
    ]
    return [1.0 if tech in techs_detected else 0.0 for tech in feature_map]


def _valid_embedding(emb) -> bool:
    return bool(emb) and len(emb) == EMBEDDING_DIM and any(v != 0.0 for v in emb)


async def train_software_type_classifier() -> dict:
    """
    Train a software_type classifier from frozen feedback snapshots.

    Reads rated_embedding (the vector the human actually saw), never the live
    analyses_result embedding — so re-analysis can't shift the training target.
    """
    from backend.services import storage_service

    labeled_data = await storage_service.get_labeled_training_data()

    if len(labeled_data) < MIN_LABELED_SAMPLES:
        return {
            "status": "insufficient_data",
            "samples": len(labeled_data),
            "needed": MIN_LABELED_SAMPLES,
            "message": f"Need {MIN_LABELED_SAMPLES - len(labeled_data)} more labeled samples.",
        }

    try:
        import pickle

        import numpy as np
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import cross_val_score
        from sklearn.preprocessing import LabelEncoder
    except ImportError:
        return {"status": "missing_dependencies", "message": "Run: pip install scikit-learn numpy"}

    try:
        X, y = [], []
        for item in labeled_data:
            label = item.get("correct_software_type") or item.get("detected_software_type") or "unknown"
            if label == "unknown":
                continue
            emb = item.get("rated_embedding") or item.get("stack_embedding")
            features = emb if _valid_embedding(emb) else _extract_features(item)
            X.append(features)
            y.append(label)

        if len(X) < MIN_FEATURE_SAMPLES:
            return {
                "status": "insufficient_valid_data",
                "samples": len(X),
                "message": f"Only {len(X)} samples had a usable label/feature vector.",
            }

        # Feature vectors must be uniform length. Mixing 3072-dim embeddings
        # with 33-dim fallback vectors would make np.array ragged and sklearn
        # throw. Pick the majority representation and drop the rest.
        lengths = Counter(len(f) for f in X)
        target_len, _ = lengths.most_common(1)[0]
        filtered = [(f, lbl) for f, lbl in zip(X, y) if len(f) == target_len]
        X = [f for f, _ in filtered]
        y = [lbl for _, lbl in filtered]

        if len(X) < MIN_FEATURE_SAMPLES:
            return {
                "status": "insufficient_valid_data",
                "message": f"After length-normalising to {target_len}-dim, only {len(X)} samples remain.",
            }

        # A classifier needs at least two classes to classify. One software_type across
        # 55 samples is not a trained model — it's a constant function.
        distinct = set(y)
        if len(distinct) < 2:
            return {
                "status": "insufficient_class_diversity",
                "samples": len(X),
                "classes": sorted(distinct),
                "message": (
                    f"All {len(X)} labeled samples are software_type '{next(iter(distinct))}'. "
                    "Need feedback spanning at least 2 software_types to train."
                ),
            }

        X = np.array(X)
        le = LabelEncoder()
        y_encoded = le.fit_transform(y)

        clf = LogisticRegression(max_iter=1000, C=1.0)  # multi_class removed (sklearn 1.7)

        # Cross-validation guard. cross_val_score needs >= 2 folds AND >= 2
        # examples per class per fold. A rare software_type with 2 samples across 5
        # folds creates empty folds and either errors or reports noise.
        min_class = min(Counter(y_encoded).values())
        n = len(X)
        if min_class < MIN_CLASS_FOR_CV or n < 2 * MIN_CV_FOLDS:
            cv_mean = cv_std = None
            cv_note = (
                f"cross-validation skipped: smallest class has {min_class} "
                f"sample(s), need >= {MIN_CLASS_FOR_CV}. Model still fit on all data."
            )
        else:
            k = min(MAX_CV_FOLDS, min_class, n // 2)
            scores = cross_val_score(clf, X, y_encoded, cv=k)
            cv_mean = round(float(scores.mean()), 3)
            cv_std = round(float(scores.std()), 3)
            cv_note = f"{k}-fold CV"

        clf.fit(X, y_encoded)

        model_path = Path("backend/models/software_type_classifier.pkl")
        model_path.parent.mkdir(parents=True, exist_ok=True)
        with open(model_path, "wb") as f:
            pickle.dump(
                {"classifier": clf, "label_encoder": le, "feature_dim": target_len}, f
            )

        logger.info("Trained software_type classifier on %d samples (cv=%s)", n, cv_mean)
        return {
            "status": "trained",
            "samples": n,
            "classes": list(le.classes_),
            "feature_dim": target_len,
            "cv_accuracy": cv_mean,          # None when unvalidated — caller MUST check
            "cv_std": cv_std,
            "cv_note": cv_note,
            "model_path": str(model_path),
            "message": (
                f"Classifier trained on {n} samples. "
                + (f"CV accuracy: {cv_mean:.0%} ± {cv_std:.0%}." if cv_mean is not None
                   else "Not cross-validated (too few per-class samples).")
            ),
        }

    except Exception as e:
        logger.error("Classifier training failed: %s", e)
        return {"status": "error", "message": str(e)}


async def predict_software_type(stack: dict) -> dict | None:
    """
    Predict software_type with the trained classifier. Returns None if untrained.
    Never raises.

    Feature representation MUST match training. The saved model records its
    feature_dim; if the live embedding doesn't match it, fall back to the
    hand-crafted vector rather than handing sklearn a wrong-width array.
    """
    model_path = Path("backend/models/software_type_classifier.pkl")
    if not model_path.exists():
        return None

    try:
        import pickle

        import numpy as np

        with open(model_path, "rb") as f:
            saved = pickle.load(f)
        clf = saved["classifier"]
        le = saved["label_encoder"]
        trained_dim = saved.get("feature_dim", EMBEDDING_DIM)

        features = None
        if trained_dim == EMBEDDING_DIM:
            from backend.services.embedding_service import embed_stack
            emb = await embed_stack(stack)
            if _valid_embedding(emb):
                features = emb
        if features is None:
            fallback = _extract_features(stack)
            if len(fallback) != trained_dim:
                # Model was trained on embeddings; we only have the short vector.
                # Predicting would be a dimension mismatch — decline instead.
                logger.warning(
                    "predict_software_type: feature dim %d != trained %d; skipping",
                    len(fallback), trained_dim,
                )
                return None
            features = fallback

        X = np.array([features])
        proba = clf.predict_proba(X)[0]
        best_idx = int(proba.argmax())
        return {
            "software_type": le.inverse_transform([best_idx])[0],
            "confidence": float(proba[best_idx]),
            "source": "trained_classifier",
            "all_probabilities": {
                le.inverse_transform([i])[0]: round(float(p), 3)
                for i, p in enumerate(proba)
            },
        }
    except Exception as e:
        logger.warning("Classifier prediction failed: %s", e)
        return None


# ── Regression tracking (LIVE) ───────────────────────────────────────────


async def regression_report(repo_key: str) -> list[dict]:
    from backend.services import storage_service

    events = await storage_service.get_analysis_events(repo_key)
    return [
        {
            "repo_key": repo_key,
            "commit_sha": e.get("commit_sha"),
            "pipeline_version": e.get("pipeline_version"),
            "analyzed_at": e.get("analyzed_at") or e.get("created_at"),
            "software_type": e.get("summary", {}).get("software_type"),
            "software_type_confidence": e.get("summary", {}).get("software_type_confidence"),
            "stack_pattern": e.get("summary", {}).get("stack_pattern"),
            "tech_names": e.get("summary", {}).get("tech_names", []),
            "quality_flags": e.get("summary", {}).get("quality_flags", []),
            "files_analyzed": e.get("summary", {}).get("files_analyzed"),
            "ai_calls_made": e.get("summary", {}).get("ai_calls_made"),
        }
        for e in events
    ]


async def version_diff(repo_key: str, version_a: str, version_b: str) -> dict:
    report = await regression_report(repo_key)
    by_version = {r["pipeline_version"]: r for r in report}
    a, b = by_version.get(version_a), by_version.get(version_b)
    if not a or not b:
        return {
            "repo_key": repo_key, "version_a": version_a, "version_b": version_b,
            "error": "one or both versions not found",
            "versions_available": sorted(by_version.keys()),
        }

    techs_a, techs_b = set(a.get("tech_names", [])), set(b.get("tech_names", []))

    def _codes(flags):
        return {f.get("code", str(f)) if isinstance(f, dict) else str(f) for f in flags or []}

    flags_a, flags_b = _codes(a.get("quality_flags")), _codes(b.get("quality_flags"))
    return {
        "repo_key": repo_key, "version_a": version_a, "version_b": version_b,
        "techs_added": sorted(techs_b - techs_a),
        "techs_removed": sorted(techs_a - techs_b),
        "software_type_changed": a.get("software_type") != b.get("software_type"),
        "software_type_a": a.get("software_type"), "software_type_b": b.get("software_type"),
        "flags_changed": flags_a != flags_b,
        "flags_added": sorted(flags_b - flags_a),
        "flags_removed": sorted(flags_a - flags_b),
    }
