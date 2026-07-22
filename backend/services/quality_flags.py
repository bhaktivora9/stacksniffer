"""
backend/services/quality_flags.py

v2.0 — Flag layer fixes:
  1. OVERCALIBRATED_CONFIDENCE: >= 0.95 AND rag == 0
  2. LOW_FILE_COVERAGE: severity=warning not error (per spec)
  3. UNRELIABLE_SOURCE_FILE: deterministic — _SELF_EVIDENCING exempt from CI files
     CI techs (GitHub Actions from .github/workflows) are always exempt
  4. severity:error flags now demote — confidence penalty applied, excluded from insights
  5. confidence_breakdown spans ALL categories (was only pattern_match categories)
  6. missing_patterns: tech names only, never filenames
"""
from __future__ import annotations

_DOMAIN_REQUIRED_SIGNALS = {
    "ml_platform": {
        "categories": ["ai_ml"],
        "frameworks": [],
        "message":    "domain=ml_platform but no AI/ML technologies detected"
    },
    "data_pipeline": {
        "categories": ["messaging"],
        "frameworks": ["airflow", "dagster", "prefect", "spark", "flink", "dbt"],
        "message":    "domain=data_pipeline but no messaging or pipeline frameworks"
    },
    "web_api": {
        "categories": [],
        "frameworks": [
            "fastapi", "django", "flask", "spring boot", "express",
            "nestjs", "gin", "fiber", "echo", "actix"
        ],
        "message": "domain=web_api but no backend framework detected"
    },
    "web_app": {
        "categories": [],
        "frameworks": ["react", "vue", "next.js", "angular", "svelte"],
        "message":    "domain=web_app but no frontend framework detected"
    },
}

# These techs are CORRECTLY detected from their own config files
# GitHub Actions from .github/workflows IS self-evidencing
# Docker from Dockerfile IS self-evidencing
# Never flag these as UNRELIABLE_SOURCE_FILE
_SELF_EVIDENCING = {
    "GitHub Actions", "Docker", "Helm", "Kubernetes"
}

# Generic phrases that indicate low-quality insights
_GENERIC_PHRASES = [
    "works well together", "commonly used", "popular choice",
    "building applications", "modern applications",
    "these technologies", "this stack is used", "good choice",
    "well-suited", "widely used", "many companies",
]

_UNRELIABLE_FILE_PATTERNS = {
    ".github/workflows",
    ".github/scripts",
    ".pre-commit-config",
    "README",
    "CONTRIBUTING",
    "docs/",
}


def _pm_get(pm, key: str, default=""):
    """Safe getter for PatternMatch — handles Pydantic objects and dicts."""
    if isinstance(pm, dict):
        return pm.get(key, default)
    return getattr(pm, key, default)


def _is_unreliable_source(matched_file: str) -> bool:
    for pattern in _UNRELIABLE_FILE_PATTERNS:
        if pattern in matched_file:
            return True
    return False


def _get_framework_names(stack: dict) -> set[str]:
    return {
        (t.get("name", "") if isinstance(t, dict) else getattr(t, "name", "")).lower()
        for t in stack.get("frameworks", [])
    }


def _get_all_tech_sources(stack: dict) -> list[str]:
    sources = []
    for techs in stack.values():
        if not isinstance(techs, list):
            continue
        for t in techs:
            if isinstance(t, dict):
                source = t.get("detection_source")
            else:
                source = getattr(t, "detection_source", None)
            if source:
                sources.append(source)
    return sources


def compute_analysis_flags(
    stack: dict,
    pattern_matches: list,
    files_analyzed: int,
    rag_repos_retrieved: int = 0,
) -> list[dict]:
    """
    Compute quality flags. Returns list of flag dicts with code/severity/message.
    severity:error flags should trigger confidence demotion in analyze.py.
    """
    flags     = []
    domain    = stack.get("domain", "unknown")
    conf      = stack.get("domain_confidence", 0.0)
    frameworks = _get_framework_names(stack)

    # ── Flag 1: LOW_FILE_COVERAGE — warning (not error per spec) ─────────────
    if files_analyzed < 5:
        flags.append({
            "code":     "LOW_FILE_COVERAGE",
            "severity": "warning",   # spec: warning
            "message":  f"Only {files_analyzed} files analyzed — manifests may be missing.",
            "field":    "files_analyzed",
            "demote":   True,        # signals analyze.py to penalize confidence
        })
    elif files_analyzed < 8:
        flags.append({
            "code":     "SPARSE_FILE_COVERAGE",
            "severity": "warning",
            "message":  f"{files_analyzed} files analyzed — some dependencies may be missed.",
            "field":    "files_analyzed",
        })

    # ── Flag 2: UNKNOWN_DOMAIN ────────────────────────────────────────────────
    if domain == "unknown":
        flags.append({
            "code":     "UNKNOWN_DOMAIN",
            "severity": "warning",
            "message":  "Domain could not be classified.",
            "field":    "domain",
        })

    # ── Flag 3: LOW_DOMAIN_CONFIDENCE ────────────────────────────────────────
    if 0 < conf < 0.60 and domain != "unknown":
        flags.append({
            "code":     "LOW_DOMAIN_CONFIDENCE",
            "severity": "warning",
            "message":  f"Domain confidence {conf:.0%} — borderline.",
            "field":    "domain_confidence",
        })

    # ── Flag 4: OVERCALIBRATED_CONFIDENCE ────────────────────────────────────
    # Spec: >= 0.95 AND rag == 0
    if conf >= 0.95 and rag_repos_retrieved == 0:
        flags.append({
            "code":     "OVERCALIBRATED_CONFIDENCE",
            "severity": "info",
            "message":  "High confidence without corpus grounding — verify classification.",
            "field":    "domain_confidence",
        })

    # ── Flag 5: UNRELIABLE_SOURCE_FILE ───────────────────────────────────────
    # Deterministic: fires for every tech from unreliable files EXCEPT _SELF_EVIDENCING
    # CI techs (GitHub Actions from .github/workflows) are always exempt
    all_categories = {
        "frameworks", "ai_ml", "databases", "messaging",
        "infra", "languages", "testing"
    }
    for pm in pattern_matches:
        category     = _pm_get(pm, "category")
        matched_file = _pm_get(pm, "matched_file")
        tech         = _pm_get(pm, "tech")

        if category not in all_categories:
            continue
        if tech in _SELF_EVIDENCING:
            continue
        if _is_unreliable_source(matched_file):
            flags.append({
                "code":     "UNRELIABLE_SOURCE_FILE",
                "severity": "error",
                "message":  (
                    f"{tech} detected from '{matched_file}' — "
                    f"not a reliable dependency source."
                ),
                "field":    "pattern_matches",
                "tech":     tech,
                "file":     matched_file,
                "demote":   True,
            })

    # ── Flag 6: NO_PRIMARY_LANGUAGE ──────────────────────────────────────────
    if not stack.get("primary_language"):
        flags.append({
            "code":     "NO_PRIMARY_LANGUAGE",
            "severity": "error",
            "message":  "Primary language not detected — file tree may be truncated.",
            "field":    "primary_language",
            "demote":   True,
        })

    # ── Flag 7: ALL_AI_INFERRED ───────────────────────────────────────────────
    all_sources = _get_all_tech_sources(stack)
    if all_sources and all(s == "ai_inferred" for s in all_sources):
        flags.append({
            "code":     "ALL_AI_INFERRED",
            "severity": "warning",
            "message":  "All detections are AI inferences — no file-level evidence.",
            "field":    "detection_source",
        })

    # ── Flag 8: DOMAIN_CONTRADICTION ─────────────────────────────────────────
    if domain in _DOMAIN_REQUIRED_SIGNALS:
        rule = _DOMAIN_REQUIRED_SIGNALS[domain]
        required_cats = rule.get("categories", [])
        required_fws  = {f.lower() for f in rule.get("frameworks", [])}

        has_cat = any(len(stack.get(cat, [])) > 0 for cat in required_cats)
        has_fw  = bool(frameworks & required_fws)

        needs_cat = bool(required_cats)
        needs_fw  = bool(required_fws)

        if not ((not needs_cat or has_cat) or (not needs_fw or has_fw)):
            flags.append({
                "code":     "DOMAIN_CONTRADICTION",
                "severity": "error",
                "message":  rule["message"],
                "field":    "domain",
                "demote":   True,
            })

    # ── Flag 9: GENERIC_INSIGHTS ──────────────────────────────────────────────
    why = str(stack.get("why_this_stack") or "").lower()
    if any(phrase in why for phrase in _GENERIC_PHRASES):
        flags.append({
            "code":     "GENERIC_INSIGHTS",
            "severity": "info",
            "message":  "Insights may be generic — rate and improve via UI.",
            "field":    "why_this_stack",
        })

    # ── Flag 10: GENERIC_STACK_PATTERN ───────────────────────────────────────
    if stack.get("stack_pattern") in ("Custom", "MVC", "", None):
        flags.append({
            "code":     "GENERIC_STACK_PATTERN",
            "severity": "info",
            "message":  "Stack pattern defaulted to generic value.",
            "field":    "stack_pattern",
        })

    # ── Flag 11: NO_RAG_GROUNDING ─────────────────────────────────────────────
    if rag_repos_retrieved == 0 and stack.get("ai_classification_used"):
        flags.append({
            "code":     "NO_RAG_GROUNDING",
            "severity": "info",
            "message":  "Classification not grounded in corpus — zero-shot only.",
            "field":    "rag_repos_retrieved",
        })

    # ── Flag 12: TEST_FRAMEWORK_FROM_CI ──────────────────────────────────────
    for pm in pattern_matches:
        category     = _pm_get(pm, "category")
        matched_file = _pm_get(pm, "matched_file")
        tech         = _pm_get(pm, "tech")
        if category == "testing" and (
            "workflows" in matched_file or
            (".github" in matched_file and "scripts" in matched_file)
        ):
            flags.append({
                "code":     "TEST_FRAMEWORK_FROM_CI",
                "severity": "warning",
                "message":  (
                    f"{tech} detected from CI workflow '{matched_file}' — "
                    f"likely an artifact reference, not a test dependency."
                ),
                "field":    "testing",
                "tech":     tech,
            })

    # ── Flag 13: DEV_DEPENDENCY_AS_FRAMEWORK ─────────────────────────────────
    dev_suspects = {"flask", "django", "express"}
    for pm in pattern_matches:
        category     = _pm_get(pm, "category")
        matched_file = _pm_get(pm, "matched_file")
        tech         = (_pm_get(pm, "tech") or "").lower()
        scope        = _pm_get(pm, "scope", "required")
        if (category == "frameworks"
                and matched_file.endswith("pyproject.toml")
                and tech in dev_suspects
                and scope in ("test", "dev")):
            flags.append({
                "code":     "DEV_DEPENDENCY_AS_FRAMEWORK",
                "severity": "warning",
                "message":  (
                    f"{_pm_get(pm, 'tech')} is a {scope} dependency in pyproject.toml — "
                    f"likely testing infrastructure, not a real framework dependency."
                ),
                "field":    "frameworks",
                "tech":     _pm_get(pm, "tech"),
            })

    return flags


def apply_confidence_demotions(
    domain_confidence: float,
    flags: list[dict],
) -> float:
    """
    Apply confidence penalty for error-severity flags with demote=True.
    Called in analyze.py after compute_analysis_flags().

    Each demotion flag reduces confidence by 0.15, floored at 0.1.
    """
    demotion_flags = [f for f in flags if f.get("demote") and f.get("severity") == "error"]
    if not demotion_flags:
        return domain_confidence

    penalty = len(demotion_flags) * 0.15
    demoted = max(domain_confidence - penalty, 0.10)
    print(
        f"[quality_flags] Demoting confidence {domain_confidence:.2f} → {demoted:.2f} "
        f"({len(demotion_flags)} error flags)"
    )
    return round(demoted, 3)


def apply_demotions_to_detections(
    merged: dict[str, list],
    flags: list[dict],
) -> dict[str, list]:
    """
    Remove techs flagged as DEV_DEPENDENCY_AS_FRAMEWORK from final detections.
    The flag records the reason; this correction keeps the output itself honest.
    """
    flagged_techs = {
        f["tech"].lower()
        for f in flags
        if f.get("code") == "DEV_DEPENDENCY_AS_FRAMEWORK" and f.get("tech")
    }
    if not flagged_techs:
        return merged

    for category, techs in merged.items():
        merged[category] = [
            tech for tech in techs
            if getattr(tech, "name", "").lower() not in flagged_techs
        ]
    return merged
