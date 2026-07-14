"""
backend/services/stack_feedback_service.py

Stack-level feedback — per-technology correctness signals.
More granular than domain feedback:
  - Was React correctly detected?
  - Was LangChain a false positive (commented out)?
  - Was Redis missed entirely?

This feeds the RLHF loop at the technology level, not just domain level.
"""
import json
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)
PATTERNS_PATH = Path("backend/config/patterns.json")

# Learning rates — asymmetric: penalize false positives harder than rewarding true positives
REWARD_DELTA   = 0.015   # confidence += per correct detection
PENALTY_DELTA  = 0.040   # confidence -= per false positive
MIN_CONF = 0.10
MAX_CONF = 0.99


# ── Internal helpers ──────────────────────────────────────────────────────────

def _load_patterns() -> dict:
    with open(PATTERNS_PATH) as f:
        return json.load(f)


def _save_patterns(patterns: dict) -> None:
    with open(PATTERNS_PATH, "w") as f:
        json.dump(patterns, f, indent=2)


def _find_entry(patterns: dict, tech_name: str) -> tuple[str | None, dict | None]:
    """Find pattern entry by tech name. Returns (category, entry)."""
    for cat, entries in patterns.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if entry.get("name", "").lower() == tech_name.lower():
                return cat, entry
    return None, None


def _find_entry_by_keyword(patterns: dict, keyword: str) -> tuple[str | None, dict | None]:
    for cat, entries in patterns.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if keyword in entry.get("keywords", []):
                return cat, entry
    return None, None


# ── Core update functions ─────────────────────────────────────────────────────

async def reward_tech(tech_name: str, pattern_matches: list[dict]) -> dict:
    """
    Human confirmed a technology was correctly detected.
    Increase confidence of every keyword that fired for this tech.
    """
    patterns = _load_patterns()
    updated = []

    for pm in pattern_matches:
        if pm.get("tech", "").lower() != tech_name.lower():
            continue
        keyword = pm.get("matched_keyword", "")
        if not keyword:
            continue
        _, entry = _find_entry_by_keyword(patterns, keyword)
        if entry:
            old = entry["confidence"]
            entry["confidence"] = min(MAX_CONF, round(old + REWARD_DELTA, 3))
            if entry["confidence"] != old:
                updated.append({
                    "tech": tech_name,
                    "keyword": keyword,
                    "old": old,
                    "new": entry["confidence"],
                    "delta": round(entry["confidence"] - old, 3)
                })

    if updated:
        _save_patterns(patterns)
    return {"tech": tech_name, "action": "rewarded", "keywords_updated": updated}


async def penalize_tech(tech_name: str, pattern_matches: list[dict], reason: str = "") -> dict:
    """
    Human flagged a technology as a false positive.
    Decrease confidence of every keyword that fired for this tech.
    """
    patterns = _load_patterns()
    updated = []

    for pm in pattern_matches:
        if pm.get("tech", "").lower() != tech_name.lower():
            continue
        keyword = pm.get("matched_keyword", "")
        if not keyword:
            continue
        _, entry = _find_entry_by_keyword(patterns, keyword)
        if entry:
            old = entry["confidence"]
            entry["confidence"] = max(MIN_CONF, round(old - PENALTY_DELTA, 3))
            if entry["confidence"] != old:
                updated.append({
                    "tech": tech_name,
                    "keyword": keyword,
                    "old": old,
                    "new": entry["confidence"],
                    "delta": round(entry["confidence"] - old, 3),
                    "reason": reason
                })
                logger.info(
                    "Penalized pattern '%s' keyword='%s': %.3f → %.3f (reason: %s)",
                    tech_name, keyword, old, entry["confidence"], reason
                )

    if updated:
        _save_patterns(patterns)
    return {"tech": tech_name, "action": "penalized", "keywords_updated": updated}


async def register_missing_tech(
    tech_name: str,
    category: str,
    analysis_id: str
) -> dict:
    """
    Human says a technology is present but was not detected.
    Two outcomes:
      A) If tech exists in patterns.json: flag it for investigation (detection failed despite pattern)
      B) If tech NOT in patterns.json: add a placeholder entry to discover new patterns

    This feeds pattern discovery — which keywords should we add?
    """
    patterns = _load_patterns()
    cat, entry = _find_entry(patterns, tech_name)

    if entry:
        # Tech exists in patterns but wasn't detected — detection failure
        # Don't penalize (pattern is correct), but flag for review
        logger.warning(
            "False negative: '%s' in patterns.json but not detected in analysis %s",
            tech_name, analysis_id
        )
        return {
            "tech": tech_name,
            "action": "false_negative_logged",
            "category": cat,
            "message": f"{tech_name} exists in patterns but wasn't detected. "
                       f"Check if the repo uses unconventional import/dependency names.",
            "investigation_needed": True
        }
    else:
        # Tech NOT in patterns — this is a discovery signal
        # Add a placeholder entry with low confidence pending keyword discovery
        if category not in patterns:
            patterns[category] = []

        new_entry = {
            "name": tech_name,
            "confidence": 0.50,  # low until we verify keywords
            "detection_files": [],
            "keywords": [],
            "_status": "discovered_via_feedback",
            "_discovered_in": analysis_id,
            "_discovered_at": datetime.utcnow().isoformat(),
            "_note": "Add keywords after inspecting the repo's dependency files"
        }
        patterns[category].append(new_entry)
        _save_patterns(patterns)
        logger.info("Discovered new tech '%s' in category '%s'", tech_name, category)
        return {
            "tech": tech_name,
            "action": "pattern_discovered",
            "category": category,
            "message": f"{tech_name} added to patterns.json with 0.50 confidence. "
                       f"Add keywords by inspecting the repo's dependency files."
        }


# ── Batch analysis for stack-level accuracy ───────────────────────────────────

async def compute_per_tech_accuracy() -> dict:
    """
    Aggregate per-technology accuracy from all stack-level feedback in MongoDB.
    Returns dict: {tech_name: {correct, total, accuracy, false_positives, false_negatives}}
    """
    from backend.services import storage_service as storage_service_rag

    all_feedback = await storage_service_rag.get_all_stack_feedback()
    if not all_feedback:
        return {}

    tech_stats = defaultdict(lambda: {
        "correct": 0,
        "false_positive": 0,
        "false_negative": 0,
        "total_evaluations": 0,
        "category": ""
    })

    for fb in all_feedback:
        for ev in fb.get("tech_evaluations", []):
            tech = ev.get("tech_name", "")
            verdict = ev.get("verdict", "")  # "correct" | "false_positive" | "false_negative"
            category = ev.get("category", "")

            tech_stats[tech]["total_evaluations"] += 1
            tech_stats[tech]["category"] = category

            if verdict == "correct":
                tech_stats[tech]["correct"] += 1
            elif verdict == "false_positive":
                tech_stats[tech]["false_positive"] += 1
            elif verdict == "false_negative":
                tech_stats[tech]["false_negative"] += 1

    return {
        tech: {
            **stats,
            "precision": round(
                stats["correct"] / max(stats["correct"] + stats["false_positive"], 1), 3
            ),
            "recall": round(
                stats["correct"] / max(stats["correct"] + stats["false_negative"], 1), 3
            ),
            "f1": round(
                2 * stats["correct"] / max(
                    2 * stats["correct"] + stats["false_positive"] + stats["false_negative"], 1
                ), 3
            )
        }
        for tech, stats in tech_stats.items()
        if stats["total_evaluations"] >= 2
    }


async def get_stack_learning_summary() -> dict:
    """
    Full stack-level learning summary.
    Shows precision/recall per technology, top false positives, top missed techs.
    """
    accuracy = await compute_per_tech_accuracy()
    if not accuracy:
        return {
            "status": "no_data",
            "message": "No stack-level feedback yet. Use the tech feedback UI to evaluate detections."
        }

    sorted_by_f1 = sorted(
        [{"tech": t, **s} for t, s in accuracy.items()],
        key=lambda x: x["f1"]
    )

    false_positives = [t for t in sorted_by_f1 if t["false_positive"] > 0]
    false_negatives = [t for t in sorted_by_f1 if t["false_negative"] > 0]
    high_precision  = [t for t in sorted_by_f1 if t["precision"] >= 0.9]

    return {
        "total_techs_evaluated": len(accuracy),
        "avg_precision": round(
            sum(t["precision"] for t in sorted_by_f1) / max(len(sorted_by_f1), 1), 3
        ),
        "avg_recall": round(
            sum(t["recall"] for t in sorted_by_f1) / max(len(sorted_by_f1), 1), 3
        ),
        "worst_precision": sorted_by_f1[:5],    # lowest F1 first
        "best_precision": sorted_by_f1[-5:],    # highest F1 first
        "top_false_positives": sorted(
            false_positives, key=lambda x: x["false_positive"], reverse=True
        )[:5],
        "top_missed_techs": sorted(
            false_negatives, key=lambda x: x["false_negative"], reverse=True
        )[:5],
        "high_precision_techs": high_precision,
        "patterns_needing_keywords": await _get_discovered_patterns_without_keywords()
    }


async def _get_discovered_patterns_without_keywords() -> list[dict]:
    """Return patterns discovered via feedback that still need keywords added."""
    patterns = _load_patterns()
    result = []
    for cat, entries in patterns.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if (entry.get("_status") == "discovered_via_feedback"
                    and not entry.get("keywords")):
                result.append({
                    "tech": entry["name"],
                    "category": cat,
                    "discovered_in": entry.get("_discovered_in", ""),
                    "note": entry.get("_note", "")
                })
    return result
