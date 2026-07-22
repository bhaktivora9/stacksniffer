"""
backend/routers/discovery.py

Pattern discovery endpoints.
Java equivalent: PatternDiscoveryServiceImpl + AssociationRuleLearner
in stacksniffer-learning.

Exposes:
  POST /api/discovery/run          — mine corpus for new tech patterns
  POST /api/discovery/apply        — add approved patterns to patterns.json
  GET  /api/discovery/frequent-keywords — all frequent keywords in corpus
  GET  /api/discovery/associations — tech co-occurrence rules (used by UI dropdown)
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import json
from pathlib import Path

import backend.services.storage_service as storage_service

router = APIRouter(prefix="/api/discovery", tags=["discovery"])

_patterns_path = Path(__file__).parent.parent / "config" / "patterns.json"


class ApplyPatternRequest(BaseModel):
    tech_name: str
    category: str
    keywords: list[str]
    confidence: float = 0.75
    detection_files: list[str] = []


@router.get("/associations")
async def get_associations():
    """
    Tech co-occurrence rules from corpus analyses.
    Used by AiInsightsCard dropdown to show corpus-derived stack patterns.

    Returns: {rules: [{stack_pattern, domain, tech_combo, count}]}
    Java equivalent: AssociationRuleLearner.getFrequentRules()
    """
    try:
        analyses = await storage_service.get_all_analyses()
    except Exception:
        return {"rules": [], "total_analyses": 0}

    if not analyses:
        return {
            "rules": [],
            "total_analyses": 0,
            "message": "No analyses in corpus yet. Seed repos to populate.",
        }

    # Extract stack_pattern values and domains from corpus
    from collections import defaultdict, Counter

    pattern_counts = Counter()
    domain_patterns = defaultdict(Counter)
    tech_by_pattern = defaultdict(list)
    categories = await storage_service.get_valid_categories()

    for a in analyses:
        stack = a.get("stack", {})
        sp = stack.get("stack_pattern", "")
        domain = stack.get("domain", "unknown")

        if not sp or sp in ("Custom", "MVC", ""):
            continue

        pattern_counts[sp] += 1
        domain_patterns[domain][sp] += 1

        # Collect tech names for this pattern
        techs = []
        for cat in categories:
            for t in stack.get(cat, []):
                name = (
                    t.get("name") if isinstance(t, dict) else getattr(t, "name", None)
                )
                if name:
                    techs.append(name)
        if techs:
            tech_by_pattern[sp].append(techs[:3])  # top 3 techs

    # Build rules
    rules = []
    for pattern, count in pattern_counts.most_common(20):
        # Find most common domain for this pattern
        domains = domain_patterns.get(pattern, {})
        top_domain = max(domains, key=domains.get) if domains else "unknown"

        # Find most common tech combo
        all_techs = [t for combo in tech_by_pattern.get(pattern, []) for t in combo]
        tech_counter = Counter(all_techs)
        top_techs = [t for t, _ in tech_counter.most_common(3)]

        rules.append(
            {
                "stack_pattern": pattern,
                "domain": top_domain,
                "tech_combo": top_techs,
                "count": count,
            }
        )

    return {
        "rules": rules,
        "total_analyses": len(analyses),
        "unique_patterns": len(pattern_counts),
    }


@router.get("/frequent-keywords")
async def frequent_keywords(min_count: int = 2):
    """
    Keywords that appear frequently across corpus but may not be in patterns.json.
    Java equivalent: FrequentPatternMiner.mineFrequentKeywords()
    """
    try:
        analyses = await storage_service.get_all_analyses()
    except Exception:
        return {"keywords": []}

    from collections import Counter

    keyword_counts = Counter()

    for a in analyses:
        stack = a.get("stack", {})
        for pm in stack.get("pattern_matches", []):
            kw = (
                pm.get("matched_keyword")
                if isinstance(pm, dict)
                else getattr(pm, "matched_keyword", None)
            )
            if kw and len(kw) > 2:
                keyword_counts[kw] += 1

        # Also count ai_inferred tech names — these are candidates for new patterns
        for inf in stack.get("ai_inferences", []):
            name = (
                inf.get("tech") if isinstance(inf, dict) else getattr(inf, "tech", None)
            )
            if name:
                keyword_counts[f"[ai] {name}"] += 1

    frequent = [
        {"keyword": kw, "count": count}
        for kw, count in keyword_counts.most_common(50)
        if count >= min_count
    ]

    return {
        "keywords": frequent,
        "total_unique": len(keyword_counts),
        "min_count": min_count,
    }


@router.post("/run")
async def run_discovery():
    """
    Mine corpus for technology patterns not in patterns.json.
    Returns candidate patterns for human review before applying.
    Java equivalent: PatternDiscoveryService.discoverPatterns()
    """
    try:
        analyses = await storage_service.get_all_analyses()
    except Exception:
        return {"candidates": [], "error": "Storage unavailable"}

    if len(analyses) < 5:
        return {
            "candidates": [],
            "message": f"Need at least 5 analyses, have {len(analyses)}. Seed more repos.",
        }

    from collections import Counter

    # Load existing pattern tech names
    with _patterns_path.open() as f:
        patterns = json.load(f)

    known_techs = {
        entry.get("name", "").lower()
        for cat_entries in patterns.values()
        if isinstance(cat_entries, list)
        for entry in cat_entries
        if isinstance(entry, dict)
    }

    # Find ai_inferred techs that appear in multiple analyses
    inferred_counts = Counter()
    inferred_categories = {}

    for a in analyses:
        stack = a.get("stack", {})
        for inf in stack.get("ai_inferences", []):
            name = (
                inf.get("tech") if isinstance(inf, dict) else getattr(inf, "tech", None)
            )
            cat = (
                inf.get("category")
                if isinstance(inf, dict)
                else getattr(inf, "category", None)
            )
            if name and name.lower() not in known_techs:
                inferred_counts[name] += 1
                inferred_categories[name] = cat

    candidates = [
        {
            "tech_name": tech,
            "category": inferred_categories.get(tech, "infra"),
            "seen_in": count,
            "suggested_keyword": tech.lower().replace(" ", "-"),
            "action": "review",
        }
        for tech, count in inferred_counts.most_common(20)
        if count >= 2
    ]

    return {
        "candidates": candidates,
        "total_found": len(candidates),
        "corpus_size": len(analyses),
        "message": (
            f"Found {len(candidates)} technology candidates appearing in 2+ analyses. "
            f"Review and POST /api/discovery/apply to add to patterns.json."
        ),
    }


@router.post("/apply")
async def apply_pattern(request: ApplyPatternRequest):
    """
    Add an approved discovered pattern to patterns.json.
    Java equivalent: PatternUpdateService.addNewPattern()
    """
    if not request.tech_name or not request.category:
        raise HTTPException(400, "tech_name and category required")

    from backend.services.category_registry import valid_categories
    valid = await valid_categories()
    if request.category not in valid:
        raise HTTPException(400, f"category must be one of: {valid}")

    with _patterns_path.open() as f:
        patterns = json.load(f)

    # Check not already present
    existing = [
        e
        for e in patterns.get(request.category, [])
        if isinstance(e, dict)
        and e.get("name", "").lower() == request.tech_name.lower()
    ]
    if existing:
        raise HTTPException(
            409, f"{request.tech_name} already exists in {request.category}"
        )

    new_entry = {
        "name": request.tech_name,
        "confidence": request.confidence,
        "detection_files": request.detection_files,
        "keywords": request.keywords,
    }

    if request.category not in patterns:
        patterns[request.category] = []
    patterns[request.category].append(new_entry)

    with _patterns_path.open("w") as f:
        json.dump(patterns, f, indent=2)

    return {
        "applied": True,
        "tech_name": request.tech_name,
        "category": request.category,
        "entry": new_entry,
        "message": f"Added {request.tech_name} to patterns.json [{request.category}]. Restart not required — patterns reload on next analysis.",
    }


# In backend/routers/discovery.py
@router.get("/dep-categories")
async def get_dep_categories():
    """
    All known dependency categories — standard + emergent from Gemini.
    Used by frontend to render tech pills for non-standard categories
    like 'bundler', 'state_management', 'css_framework'.
    """
    return await storage_service.get_dep_categories()
