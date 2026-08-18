"""
backend/services/ai_pipeline.py

Phase 2b (software_type classification). Phase 3 (stack insights) is disabled.
Phase 2a (dependency classification) lives in dep_classifier.py.

THREE VOCABULARIES, ONE PATTERN
-------------------------------
software_types, stack_patterns, and technology_roles are now handled identically:

    _get_*_options()        -> storage-backed vocabulary (falls back to builtin)
    _build_*_feedback()     -> promote/merge/discard steering injected into prompt
    validate + record       -> out-of-vocabulary values are RECORDED as emergent,
                               not silently dropped

Previously only `software_type` was dynamic; patterns were a hardcoded map and
`ai_inferred_techs[].technology_role` was completely unvalidated (any string passed
through). Both are now consistent with the software_type path.

FIXED IN THIS REVISION
  1. CONTRADICTION: additional_context declared a hard "Allowed:" pattern list
     for library-like repositories that overrode the "suggestions, not exhaustive"
     instruction below it. httpx (software_type=library) therefore could not answer
     "Sync / Async Client" even though the map contained it. additional_context
     is now guidance, not a whitelist.
  2. _INSIGHTS_SAFE_DEFAULTS["stack_pattern"] was "Custom" while the prompt
     said "Do NOT answer Custom" — the fallback violated the instruction.
     Now "" (unknown), which is honest and scoreable.
  3. pattern_is_new was requested by the prompt but never recorded, so the
     emergent-pattern path could never fire. Now recorded.
  4. ai_inferred_techs[].technology_role was unvalidated — Gemini could emit any
     string and it flowed straight into detections. Now validated + recorded.
  5. .copy() on defaults containing nested lists is a SHALLOW copy: every
     caller shared the same list objects. Now deepcopy.
"""
import asyncio
import json
import logging
from copy import deepcopy
from os import getenv

import google.generativeai as genai
from dotenv import load_dotenv
from models.schemas import AiInference
from models.taxonomy import SOFTWARE_TYPE_DEFINITIONS, normalize_specific_identity

from services import storage_service
from services.embedding_service import embed_stack
from services.gemini_interactions import InteractionsModel
from services.rag_filter import format_rag_context
from services.safe_json import get_repair_counters, safe_parse_gemini_json

load_dotenv()

logger = logging.getLogger(__name__)

_KEY = getenv("GEMINI_API_KEY", "")

def _normalize_model_name(model_id: str | None, fallback: str) -> str:
    if not model_id:
        return fallback
    normalized = model_id.strip()
    normalized = normalized.removeprefix("models/")
    if normalized in {
        "gemini-2.0-flash",
        "gemini-2.0-flash-001",
        "gemini-2.0-flash-lite",
        "gemini-2.0-flash-lite-001",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
    }:
        return fallback
    return normalized


_MODEL_RAW = getenv("GEMINI_ANALYSIS_MODEL", "gemini-3.5-flash")
_DEP_CLASSIFICATION_MODEL_RAW = getenv(
    "GEMINI_DEP_CLASSIFICATION_MODEL", "gemini-3.5-flash-lite"
)
_MODEL = _normalize_model_name(_MODEL_RAW, "gemini-3.5-flash")
_DEP_CLASSIFICATION_MODEL = _normalize_model_name(
    _DEP_CLASSIFICATION_MODEL_RAW, "gemini-3.5-flash-lite"
)
_MAX_TOKENS = int(getenv("GEMINI_MAX_TOKENS_ANALYSIS", 32768))
_CLASSIFICATION_MAX_TOKENS = int(getenv("GEMINI_MAX_TOKENS_CLASSIFICATION", 32768))
_STACK_INFERENCE_MAX_TOKENS = int(getenv("GEMINI_MAX_TOKENS_STACK_INFERENCE", 8192))
def _env_float(name: str, default: float) -> float:
    try:
        return float(getenv(name, str(default)))
    except (TypeError, ValueError):
        logger.warning(
            "[ai_pipeline] invalid float for %s, using default %.1fs",
            name,
            default,
        )
        return default


_SOFTWARE_TYPE_TIMEOUT_SECONDS = _env_float("GEMINI_SOFTWARE_TYPE_TIMEOUT_SECONDS", 90.0)
_STACK_INFERENCE_TIMEOUT_SECONDS = _env_float("GEMINI_STACK_INFERENCE_TIMEOUT_SECONDS", 60.0)
_STACK_INSIGHTS_TIMEOUT_SECONDS = _env_float("GEMINI_STACK_INSIGHTS_TIMEOUT_SECONDS", 60.0)

logger.info("[ai_pipeline] KEY: %s", "configured" if _KEY else "missing")
if _MODEL_RAW != _MODEL:
    logger.warning(
        "[ai_pipeline] deprecated model override: GEMINI_ANALYSIS_MODEL=%r normalized to %r",
        _MODEL_RAW,
        _MODEL,
    )
if _DEP_CLASSIFICATION_MODEL_RAW != _DEP_CLASSIFICATION_MODEL:
    logger.warning(
        "[ai_pipeline] deprecated model override: GEMINI_DEP_CLASSIFICATION_MODEL=%r normalized to %r",
        _DEP_CLASSIFICATION_MODEL_RAW,
        _DEP_CLASSIFICATION_MODEL,
    )
logger.info("[ai_pipeline] MODEL: %s", _MODEL)
logger.info("[ai_pipeline] DEP_CLASSIFICATION_MODEL: %s", _DEP_CLASSIFICATION_MODEL)

genai.configure(api_key=_KEY)


def _use_deprecated_sampling(model_id: str) -> bool:
    # Gemini 3.x does not accept temperature/top_k/top_p in this form on some endpoints.
    return not model_id.startswith("gemini-3.")


def _build_generation_config(
    max_output_tokens: int,
    temperature: float = 0.2,
    *,
    response_mime_type: str = "application/json",
    model_id: str | None = None,
) -> genai.types.GenerationConfig:
    kwargs = {
        "response_mime_type": response_mime_type,
        "max_output_tokens": max_output_tokens,
    }
    if _use_deprecated_sampling(model_id or _MODEL):
        kwargs["temperature"] = temperature
    return genai.types.GenerationConfig(**kwargs)

# Exported — imported by dep_classifier.py and analyze.py
_legacy_json_model = genai.GenerativeModel(
    _MODEL,
    generation_config=_build_generation_config(_MAX_TOKENS, model_id=_MODEL),
)

_json_model = InteractionsModel(
    model=_MODEL,
    generation_config=_build_generation_config(_MAX_TOKENS, model_id=_MODEL),
    legacy_model_factory=lambda: _legacy_json_model,
)

_legacy_dep_json_model = genai.GenerativeModel(
    _DEP_CLASSIFICATION_MODEL,
    generation_config=_build_generation_config(
        _MAX_TOKENS,
        response_mime_type="application/json",
        model_id=_DEP_CLASSIFICATION_MODEL,
    ),
)

_dep_json_model = InteractionsModel(
    model=_DEP_CLASSIFICATION_MODEL,
    generation_config=_build_generation_config(
        _MAX_TOKENS,
        response_mime_type="application/json",
        model_id=_DEP_CLASSIFICATION_MODEL,
    ),
    legacy_model_factory=lambda: _legacy_dep_json_model,
)

# ── Safe defaults ─────────────────────────────────────────────────────────────
# NOTE: always deepcopy these — they contain nested lists. A shallow .copy()
# shares the list objects across every caller, so one mutation leaks into the
# next analysis's defaults.

_SOFTWARE_TYPE_SAFE_DEFAULTS = {
    "software_type":             "unknown",
    "software_type_confidence":  0.0,
    "software_type_reasoning":   "AI classification failed",
    "specific_identity":         None,
    "rag_influenced":     False,
    "similar_repos_used": 0,
    "rejected":           False,
}

_TECH_INFERENCE_SAFE_DEFAULTS = {
    "architecture_style": "unknown",
    "missing_patterns":   [],
    "ai_inferred_techs":  [],
}

_INSIGHTS_SAFE_DEFAULTS = {
    "why_this_stack":       "",
    # "" not "Custom": the prompt forbids answering "Custom", so the fallback
    # must not produce it either. Empty = honestly unknown, and scoreable.
    "stack_pattern":        "",
    "pattern_is_new":       False,
    "pattern_evidence":     "",
    "pattern_confidence":   0.0,
    "ecosystem_context":    "",
    "notable_combinations": [],
}

_CAUSAL_VERBS = [
    "enables", "decouples", "avoids", "prevents", "allows",
    "reduces", "eliminates", "provides", "enforces", "ensures",
    "separates", "abstracts", "simplifies", "offloads", "replaces",
]

# The 8 standard tech technology_roles. Mirrors dep_classifier; the live valid set is
# fetched from storage so promoted emergent technology_roles are accepted too.
_BUILTIN_TECHNOLOGY_ROLES = [
    "languages", "frameworks", "databases", "messaging",
    "ai_ml", "infra", "testing", "library",
]

# ── Stack pattern vocabulary (BUILTIN FALLBACK ONLY) ─────────────────────────
# This is no longer the source of truth — it is the fallback used when the
# storage-backed pattern taxonomy is unavailable. Patterns are SUGGESTIONS in
# the prompt; the model may propose new ones, which are recorded as emergent.

_SOFTWARE_TYPE_PATTERN_MAP: dict[str, list[str]] = {
    "library": [
        "Plugin Architecture", "Chain of Responsibility", "Fluent Interface",
        "Hexagonal", "MVC", "Sync / Async Client", "ORM / Data Mapper",
        "Type-Safe RPC", "API Client SDK", "Async Runtime",
        "Distributed Task Queue", "Decorator-Based Composition", "RAG Framework",
        "Multi-Agent Framework",
    ],
    "data_pipeline": [
        "Event-Driven", "DAG Scheduler", "Lambda Architecture",
        "Event Sourcing", "Streaming Dataflow", "Dynamic Workflow Engine",
        "Compiler / Templating Pipeline",
    ],
    "infrastructure_tool": [
        "Plugin Architecture", "Pull-Based Scraping", "Event-Driven",
        "Hexagonal", "GitOps Controller", "Object Storage Server",
        "Package Manager / Resolver", "Runtime / VM",
    ],
    "deployable_service": [
        "MVC", "Hexagonal", "CQRS", "Event-Driven", "ASGI Framework",
        "ORM / Data Mapper", "Type-Safe RPC", "Middleware Pipeline",
        "Microservices", "Serverless",
    ],
    "web_application": [
        "MVC", "JAMstack", "Hexagonal", "ASGI Framework",
        "Middleware Pipeline", "Full-Stack SSR Framework",
    ],
    "database": [
        "Plugin Architecture", "Hexagonal", "Event-Driven",
        "Vector Index Engine", "In-Memory Data Store", "Time-Series Engine",
    ],
    "cli_tool": [
        "Plugin Architecture", "Hexagonal", "Decorator-Based Composition",
        "Convention-Based Generator",
    ],
    "desktop_application": ["MVC", "Plugin Architecture", "Hexagonal", "Native Webview Shell"],
    "unknown": [],   # filled below with the union
}
_ALL_PATTERNS = sorted({
    p for patterns in _SOFTWARE_TYPE_PATTERN_MAP.values() for p in patterns
})
_SOFTWARE_TYPE_PATTERN_MAP["unknown"] = list(_ALL_PATTERNS)


def get_stack_pattern_taxonomy() -> dict:
    """
    JSON-safe view of the BUILTIN pattern vocabulary, for the UI dropdown's
    offline fallback. The live list should come from the taxonomy endpoint.
    """
    return {
        "patterns": list(_ALL_PATTERNS),
        "by_software_type": {d: list(p) for d, p in _SOFTWARE_TYPE_PATTERN_MAP.items()},
    }


def _build_classification_rules() -> str:
    rules = [
        "Classify this repository by answering:",
        '"Who uses the final artifact of this codebase and how do they interact with it?"',
        "",
        "RULES (strict priority order):",
    ]
    for definition in SOFTWARE_TYPE_DEFINITIONS:
        rules.extend((
            "",
            f'"{definition.software_type.value}": {definition.consumer}',
            f'  {definition.predicate}',
        ))
    rules.extend((
        "",
        "NOVELTY:",
        "Strong evidence that fits no canonical type must set software_type_is_new=true",
        "and name emergent_software_type. Never silently coerce novelty to",
        "application_platform or unknown. Imported AI libraries remain library.",
    ))
    return "\n".join(rules)


_CLASSIFICATION_RULES = _build_classification_rules()

_INFRA_NOISE = {
    "GitHub Actions", "Docker", "Helm", "Kubernetes",
    "Skaffold", "Terraform", "Ansible", "Pulumi",
}


# ── Utilities ─────────────────────────────────────────────────────────────────

def _serialize(raw: dict) -> dict:
    result = {}
    for cat, techs in raw.items():
        result[cat] = [
            t.model_dump() if hasattr(t, "model_dump") else t
            for t in techs
        ]
    return result


def _safe_json(
    text: str,
    fallback: dict | list,
    *,
    expect: str = "auto",
    site: str = "pipeline",
) -> dict | list:
    parsed = safe_parse_gemini_json(
        text,
        expect=expect,
        site=site,
        on_repair=lambda stage: (
            logger.warning(
                "[ai_pipeline] %s parse_repair stage=%s count=%s",
                site,
                stage,
                get_repair_counters().get(site, {}).get(stage, 0),
            )
        ),
    )
    if parsed is None:
        logger.warning("[ai_pipeline] JSON parse failed: raw: %r", (text or "")[:300])
        return deepcopy(fallback)
    return parsed


def _log_generation_status(site: str, response, max_output_tokens: int | None = None) -> None:
    candidate = None
    finish_reason = None
    try:
        candidates = getattr(response, "candidates", None)
        if candidates:
            candidate = candidates[0]
            finish_reason = getattr(candidate, "finish_reason", None)
    except Exception:
        pass

    if finish_reason is None:
        return
    reason = str(finish_reason).lower()
    if "max" in reason and "token" in reason:
        logger.warning(
            "[ai_pipeline] %s generation may have been truncated (finish_reason=%s)",
            site,
            finish_reason,
        )
        return

    usage = getattr(response, "usage_metadata", None)
    output_tokens = getattr(usage, "candidates_token_count", None)
    if max_output_tokens and output_tokens is not None and output_tokens >= max_output_tokens:
        logger.warning(
            "[ai_pipeline] %s output reached max_output_tokens=%s (observed=%s)",
            site,
            max_output_tokens,
            output_tokens,
        )


def _log_error(fn: str, err: str) -> None:
    if "429" in err or "quota" in err.lower() or "ResourceExhausted" in err:
        msg = "QUOTA EXCEEDED"
    elif "403" in err or "PERMISSION_DENIED" in err or "API_KEY_INVALID" in err:
        msg = "AUTH FAILED — check GEMINI_API_KEY"
    elif "404" in err or "not found" in err.lower() or "MODEL_NOT_FOUND" in err:
        msg = f"MODEL NOT FOUND — check GEMINI_ANALYSIS_MODEL={_MODEL}"
    else:
        msg = "FAILED"
    logger.error("[ai_pipeline] %s %s: %s", fn, msg, err[:300])


def _tech_get(tech, key: str, default=""):
    if isinstance(tech, dict):
        return tech.get(key, default)
    return getattr(tech, key, default)


def _has_causal_verb(text: str) -> bool:
    t = (text or "").lower()
    return any(v in t for v in _CAUSAL_VERBS)


def _norm(s: str) -> str:
    """Loose comparison key so 'Sync / Async Client' == 'sync/async client'."""
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _filter_insights_techs(detections: dict, software_type: str) -> dict:
    """
    Filter techs fed to generate_stack_insights().
    - Exclude infra noise unless software_type == infrastructure_tool
    - Exclude non-required/non-product manifest deps (dev tools, test scaffolding)
    """
    if software_type == "infrastructure_tool":
        return detections

    result = {}
    for cat, techs in detections.items():
        insight_techs = [
            t for t in techs
            if not (
                str(_tech_get(t, "detection_source", "")).startswith("manifest")
                and (
                    _tech_get(t, "scope") not in ("required", "optional")
                    or _tech_get(t, "origin") == "test"
                )
            )
        ]
        if cat == "infra":
            result[cat] = [
                t for t in insight_techs
                if _tech_get(t, "name") not in _INFRA_NOISE
            ]
        else:
            result[cat] = insight_techs
    return result


# ══════════════════════════════════════════════════════════════════════════════
# VOCABULARY LAYER — software_types, patterns, technology_roles: one shape each
#
# Every accessor degrades to a builtin fallback. A taxonomy fetch failure must
# never empty a vocabulary: an empty valid-set would reject every value the
# model emits, silently blanking the analysis.
# ══════════════════════════════════════════════════════════════════════════════

# ── SoftwareTypes ───────────────────────────────────────────────────────────────────

async def _get_software_type_options() -> str:
    try:
        from services import storage_service
        software_types = await storage_service.get_software_types()
        names = [d["_id"] if isinstance(d, dict) else d for d in software_types]
        if names:
            return " | ".join(names)
    except Exception as e:
        logger.debug("[ai_pipeline] SoftwareType taxonomy fetch failed: %s", e)
    return " | ".join(
        definition.software_type.value for definition in SOFTWARE_TYPE_DEFINITIONS
    )


async def _get_valid_software_types() -> set[str]:
    try:
        from services import storage_service
        return set(await storage_service.get_valid_software_types())
    except Exception as e:
        logger.debug("[ai_pipeline] Valid-software_type fetch failed: %s", e)
        return {
            definition.software_type.value for definition in SOFTWARE_TYPE_DEFINITIONS
        }


async def _build_software_type_feedback_context() -> str:
    try:
        from services import storage_service
        decisions = await storage_service.get_software_type_feedback_decisions()
    except Exception:
        return ""
    return _format_feedback("SOFTWARE_TYPE", "SOFTWARE_TYPES", decisions)


# ── Stack patterns ────────────────────────────────────────────────────────────

async def _get_pattern_options(software_type: str) -> list[str]:
    """
    Suggested patterns for a software_type. Storage-backed when available (so promoted
    emergent patterns appear), else the builtin map. Never returns [] — an empty
    suggestion list leaves the model with no anchor at all.
    """
    try:
        from services import storage_service
        patterns = await storage_service.get_stack_patterns(software_type=software_type)
        names = [p["_id"] if isinstance(p, dict) else p for p in patterns]
        if names:
            return names
    except Exception as e:
        logger.debug("[ai_pipeline] Pattern taxonomy fetch failed: %s", e)
    return _SOFTWARE_TYPE_PATTERN_MAP.get(software_type, _ALL_PATTERNS) or _ALL_PATTERNS


async def _get_known_patterns() -> set[str]:
    """Normalized set of every known pattern, for the is-this-new check."""
    try:
        from services import storage_service
        patterns = await storage_service.get_stack_patterns()
        names = [p["_id"] if isinstance(p, dict) else p for p in patterns]
        if names:
            return {_norm(n) for n in names}
    except Exception:
        pass
    return {_norm(p) for p in _ALL_PATTERNS}


async def _build_pattern_feedback_context() -> str:
    try:
        from services import storage_service
        decisions = await storage_service.get_pattern_feedback_decisions()
    except Exception:
        return ""
    return _format_feedback("STACK PATTERN", "STACK PATTERNS", decisions)


async def _record_emergent_pattern(name: str, software_type: str, repo: str, evidence: str) -> None:
    """
    A pattern the model proposed that isn't in the taxonomy -> pending review.
    Best-effort: recording must never break an analysis.
    """
    try:
        from services import storage_service
        await storage_service.record_emergent_pattern(
            name=name, software_type=software_type, example_repo=repo, evidence=evidence,
        )
        logger.info(
            "[ai_pipeline] emergent pattern recorded: %r (%s) from %s",
            name,
            software_type,
            repo,
        )
    except Exception as e:
        logger.debug("[ai_pipeline] record_emergent_pattern unavailable: %s", e)


# ── TechnologyRoles (for ai_inferred_techs) ────────────────────────────────────────

async def _get_valid_technology_roles() -> set[str]:
    try:
        import services.storage_service as storage_service
        cats = await storage_service.get_valid_technology_roles()
        if cats:
            return set(cats)
    except Exception as e:
        logger.debug("[ai_pipeline] TechnologyRole taxonomy fetch failed: %s", e)
    return set(_BUILTIN_TECHNOLOGY_ROLES)


async def _record_emergent_technology_role(name: str, tech: str, repo: str) -> None:
    try:
        import services.storage_service as storage_service
        await storage_service.record_emergent_technology_role(name, tech, repo)
        logger.info(
            "[ai_pipeline] emergent technology_role recorded: %r (tech=%s) from %s",
            name,
            tech,
            repo,
        )
    except Exception as e:
        logger.debug("[ai_pipeline] record_emergent_technology_role unavailable: %s", e)


def _format_feedback(label: str, plural: str, decisions: dict) -> str:
    """Shared renderer for promote/merge/discard steering — identical shape for
    software_types, patterns, and technology_roles so the model sees one consistent idiom."""
    if not decisions:
        return ""
    lines = []
    discarded = decisions.get("discarded") or []
    merged = decisions.get("merged") or {}
    promoted = decisions.get("promoted") or []
    if discarded:
        lines.append(f"DISCARDED {plural} (never emit): {discarded}")
    for name, target in merged.items():
        lines.append(f"MERGED {label}: emit '{target}' instead of '{name}'")
    if promoted:
        lines.append(f"PROMOTED {plural} (valid, emit normally): {promoted}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2b: classify_software_type
# ══════════════════════════════════════════════════════════════════════════════

async def classify_software_type(
    raw_detections: dict,
    file_tree: list[str],
    flags: list[dict] | None = None,
    similar_repos: list[dict] | None = None,
    repo_name: str = "",
) -> dict:
    """
    SoftwareType classification. Receives Phase 0 + Phase 2a detections.

    flags: quality flags passed so Gemini can output rejected:true when
           detections are implausible rather than rationalising them.
    """
    logger.info(
        "[ai_pipeline] classify_software_type — model=%s rag=%s",
        _MODEL,
        f"YES ({len(similar_repos)})" if similar_repos else "NO",
    )

    serializable    = _serialize(raw_detections)
    software_type_options  = await _get_software_type_options()
    software_type_feedback = await _build_software_type_feedback_context()
    rag_section     = format_rag_context(similar_repos or [])

    flags_str = ""
    if flags:
        error_flags = [f for f in flags if f.get("severity") == "error"]
        if error_flags:
            flags_str = f"""
QUALITY FLAGS (errors in detection layer):
{json.dumps([{"code": f["code"], "message": f["message"]} for f in error_flags], indent=2)}

If these flags indicate unreliable detections, set "rejected": true.
Do NOT rationalise false positives — reject instead.
"""

    prompt = f"""{_CLASSIFICATION_RULES}

{rag_section}

{software_type_feedback}

DETECTED TECHNOLOGIES (Phase 0 file signals + Phase 2a dep classification):
{json.dumps(serializable, indent=2)}

File tree sample (60 files): {json.dumps(file_tree[:60])}

{flags_str}

SOFTWARE_TYPE
software_type MUST be one of these established values:
{software_type_options}

If the repository genuinely represents a category NOT in this list, you may
propose a new one — but ONLY by setting software_type_is_new = true AND providing
the new name in emergent_software_type. Do NOT place a non-listed value in
software_type while claiming software_type_is_new = false — an "existing" type
must be from the list above.

Propose a new type ONLY when the DETECTED STACK genuinely doesn't fit any listed
category — not because you recognize the repository as a well-known project.
"This repo is X, known as a Y" is recognition, not analysis, and is not grounds
for a new type. Reserve high confidence for unambiguous stack evidence.

LANGUAGE RUNTIMES AND OPERATING SYSTEMS ARE is_new CASES:
If the repository IS a language implementation (compiler, interpreter, or runtime
— e.g. CPython, the Rust compiler, or a Wasm runtime) or an operating system /
kernel, set software_type_is_new=true and emergent_software_type accordingly
("language_runtime" or "operating_system"). Do NOT classify these as cli_tool or
build_tool merely because they ship a terminal binary or participate in builds. A
runtime EXECUTES code; it is not a CLI utility or a build step. The terminal binary
is how you invoke the runtime, not what the repository is.

software_type_is_new and specific_identity are MUTUALLY EXCLUSIVE. If you set
software_type_is_new=true, set specific_identity=null. language_runtime and
operating_system always use the is_new/emergent_software_type path and NEVER the
specific_identity path.

SPECIFIC_IDENTITY (observation — does NOT change software_type or software_type_is_new)
After choosing the best-fit software_type above, answer one further question:
"Is this repo's primary identity MORE SPECIFIC than the software_type I chose?"

- If a finer-grained taxonomy would give this repo its own category that the
  chosen software_type only loosely covers, name that category in
  specific_identity as a snake_case string. You are NOT changing software_type
  and NOT proposing novelty — you are recording the narrower identity you already
  perceived, so it can be reviewed later.
- If the chosen software_type already captures the repo precisely, set
  specific_identity to null. Do not invent granularity that isn't there.

It is EXPECTED and correct that many repos with software_type deployable_service,
build_tool, cli_tool, or library carry a non-null specific_identity. That is the
signal we want. Examples (software_type -> specific_identity):
  deployable_service whose purpose is model/LLM inference   -> "model_serving"
  deployable_service that is a durable workflow engine      -> "workflow_orchestration"
  build_tool that is a container-native CI/CD engine        -> "ci_cd_engine"
  library with no narrower identity                         -> null
  database (plain relational/vector/kv store)               -> null
  framework (plain web/app framework)                       -> null

specific_identity MUST NOT be one of the established software_type values — it is
strictly for SUB-canonical identity. If nothing narrower applies, use null.

Return ONLY valid JSON:
{{
  "software_type": "the software_type name",
  "software_type_is_new": false,
  "emergent_software_type": "new snake_case name only when software_type_is_new=true, otherwise null",
  "software_type_confidence": 0.0,
  "software_type_reasoning": "under 120 chars — cite specific evidence",
  "specific_identity": "snake_case narrower identity when more specific than software_type, otherwise null",
  "rejected": false,
  "rejection_reason": "only if rejected=true — why detections are implausible"
}}"""
    
    try:
        # Do not log the full prompt: detected repository text can contain
        # characters unsupported by the Windows cp1252 console used by Uvicorn.
        logger.info("[ai_pipeline] -> Gemini classify_software_type...")
        response = await asyncio.to_thread(
            _json_model.generate_content,
            prompt,
            request_options={"timeout": _SOFTWARE_TYPE_TIMEOUT_SECONDS},
            generation_config=_build_generation_config(
                _CLASSIFICATION_MAX_TOKENS,
                model_id=_MODEL,
            ),
        )
        _log_generation_status("software_type", response, _CLASSIFICATION_MAX_TOKENS)
        logger.info("[ai_pipeline] OK classify_software_type %s chars", len(response.text))
        result = _safe_json(
            response.text, deepcopy(_SOFTWARE_TYPE_SAFE_DEFAULTS), expect="object", site="software_type"
        )
        result["specific_identity"] = normalize_specific_identity(
            result.get("specific_identity")
        )
        logger.info(
            "[ai_pipeline] classify_software_type result: %s",
            json.dumps(result, ensure_ascii=True, default=str),
        )
        emitted = (result.get("software_type") or "").strip()
        emergent = (result.get("emergent_software_type") or "").strip()
        declared_new = result.get("software_type_is_new") is True
        conf = float(result.get("software_type_confidence", 0.0) or 0.0)
        valid_software_types = await _get_valid_software_types()

        if declared_new and emergent and emergent not in valid_software_types:
            try:
                import services.storage_service as storage_service
                await storage_service.record_emergent_software_type(
                    name=emergent,
                    example_repo=repo_name or "unknown",
                )
                logger.info(
                    "[ai_pipeline] emergent software_type recorded: %r from %s",
                    emergent,
                    repo_name,
                )
            except Exception as e:
                logger.debug("[ai_pipeline] record_emergent_software_type unavailable: %s", e)

            result["proposed_software_type"] = emergent
            result["emergent_software_type"] = emergent
            result["software_type_is_new"] = True
            result["specific_identity"] = None
            result["software_type"] = "unknown"
            result["coerced_from"] = emergent
            result["coercion_reason"] = "emergent_type_pending_review"
            result["software_type_confidence"] = min(conf, 0.5)
            result["software_type_reasoning"] = (
                f"Proposed '{emergent}' is outside the active taxonomy — pending review"
            )
        elif declared_new and not emergent:
            result["rejected"] = True
            result["rejection_reason"] = (
                "software_type_is_new=true requires emergent_software_type"
            )
            result["software_type"] = "unknown"
            result["coerced_from"] = emitted
            result["coercion_reason"] = "new_type_missing_emergent_name"
            result["software_type_confidence"] = min(conf, 0.5)
        elif valid_software_types and emitted not in valid_software_types:
            # Recognition-driven invention is not emergence. It must be explicitly
            # proposed through emergent_software_type to enter the review queue.
            result["rejected"] = True
            result["rejection_reason"] = (
                f"Non-listed software_type '{emitted}' claimed as established"
            )
            result["software_type"] = "unknown"
            result["coerced_from"] = emitted
            result["coercion_reason"] = "non_listed_type_claimed_as_established"
            result["software_type_confidence"] = min(conf, 0.3)
            result["software_type_is_new"] = False
            result["emergent_software_type"] = None
        else:
            result["software_type_is_new"] = False
            result["emergent_software_type"] = None

        result["rag_influenced"]     = bool(similar_repos)
        result["similar_repos_used"] = len(similar_repos) if similar_repos else 0
        result.setdefault("rejected", False)
        result.setdefault("software_type_is_new", False)
        return result
    except Exception as e:
        _log_error("classify_software_type", str(e))
        return deepcopy(_SOFTWARE_TYPE_SAFE_DEFAULTS)


async def infer_stack_technologies(
    raw_detections: dict,
    file_tree: list[str],
    repo_name: str = "",
) -> dict:
    """Infer stack gaps independently of the bounded software-type response."""
    serializable = _serialize(raw_detections)
    technology_role_options = " | ".join(sorted(await _get_valid_technology_roles()))
    prompt = f"""You are reviewing a detected software stack for likely omissions.

DETECTED TECHNOLOGIES:
{json.dumps(serializable, indent=2)}

File tree sample (60 files): {json.dumps(file_tree[:60])}

Known technology_roles: {technology_role_options}
Only infer a technology when the supplied evidence strongly implies it. Do not
repeat detected technologies. Keep each reasoning field under 80 characters.

Return ONLY valid JSON:
{{
  "architecture_style": "one of: monolith | microservices | serverless | event_driven | unknown",
  "missing_patterns": ["tech NAMES likely used but absent from the detected stack"],
  "ai_inferred_techs": [
    {{"name": "str", "technology_role": "str", "confidence": 0.0, "reasoning": "str"}}
  ]
}}"""

    try:
        logger.info("[ai_pipeline] -> Gemini infer_stack_technologies...")
        response = await asyncio.to_thread(
            _json_model.generate_content,
            prompt,
            request_options={"timeout": _STACK_INFERENCE_TIMEOUT_SECONDS},
            generation_config=_build_generation_config(
                _STACK_INFERENCE_MAX_TOKENS,
                model_id=_MODEL,
            ),
        )
        logger.info("[ai_pipeline] OK infer_stack_technologies %s chars", len(response.text))
        _log_generation_status("stack_inference", response, _STACK_INFERENCE_MAX_TOKENS)
        result = _safe_json(
            response.text, deepcopy(_TECH_INFERENCE_SAFE_DEFAULTS), expect="object", site="stack_inference"
        )

        valid_technology_roles = await _get_valid_technology_roles()
        cleaned_techs = []
        for tech in result.get("ai_inferred_techs", []) or []:
            if not isinstance(tech, dict):
                continue
            role = (tech.get("technology_role") or "").strip()
            if role and role not in valid_technology_roles:
                await _record_emergent_technology_role(
                    role, tech.get("name", ""), repo_name or "unknown"
                )
                tech["proposed_technology_role"] = role
                tech["technology_role_is_new"] = True
                tech["technology_role"] = "library"
            cleaned_techs.append(tech)
        result["ai_inferred_techs"] = cleaned_techs

        for key, default in _TECH_INFERENCE_SAFE_DEFAULTS.items():
            result.setdefault(key, deepcopy(default))
        return result
    except Exception as e:
        _log_error("infer_stack_technologies", str(e))
        return deepcopy(_TECH_INFERENCE_SAFE_DEFAULTS)


# ══════════════════════════════════════════════════════════════════════════════
# Phase 3: generate_stack_insights
# ══════════════════════════════════════════════════════════════════════════════

async def generate_stack_insights(
    software_type_result: dict,
    detections: dict,
    repo_name: str,
    repo_description: str,
    similar_repos: list[dict] = None,
) -> dict:
    """
    Architectural insights.
    - Infra noise filtered unless software_type == infrastructure_tool
    - stack_pattern SUGGESTED (not restricted) by software_type; new patterns allowed
      and recorded as emergent
    - why_this_stack validated for a causal verb
    """
    logger.info(
        "[ai_pipeline] generate_stack_insights - software_type=%s rag=%s",
        software_type_result.get("software_type"),
        "YES" if similar_repos else "NO",
    )

    software_type   = software_type_result.get("software_type", "unknown")
    filtered = _filter_insights_techs(detections, software_type)
    serial   = _serialize(filtered)
    rag      = format_rag_context(similar_repos or [])

    suggested        = await _get_pattern_options(software_type)
    pattern_enum     = " | ".join(suggested)
    pattern_feedback = await _build_pattern_feedback_context()

    # GUIDANCE, not a whitelist.
    #
    # This block previously used a closed pattern list for library-like repos,
    # which contradicted the "suggestions, not exhaustive" instruction below and
    # made correct answers like "Sync / Async Client" unreachable for httpx.
    # It now describes what KIND of pattern fits, without enumerating a closed set.
    additional_context = ""
    if software_type in ("library", "framework", "sdk"):
        additional_context = """
NOTE: This repo is a library or framework. The stack_pattern must describe its
INTERNAL DESIGN architecture (how the code is organised for its consumers), not
its deployment topology. Deployment patterns (Microservices, Serverless) are
wrong here unless the repo itself IS a deployed service.
"""
    elif software_type == "data_pipeline":
        additional_context = """
NOTE: For data pipelines, the pattern should describe how data MOVES and is
SCHEDULED (batch vs stream, DAG vs dynamic, push vs pull).
"""
    elif software_type == "infrastructure_tool":
        additional_context = """
NOTE: For infra tools, the pattern should describe the CONTROL model (reconcile
loop, pull-based scraping, plugin/provider extension, declarative apply).
"""

    banned = ""
    if software_type not in ("deployable_service", "web_application"):
        banned = '\nDo NOT use the word "microservices" in any field.'

    prompt = f"""You are a principal engineer providing architectural analysis.

Ground your analysis in the detected technologies and the repository's stated
purpose. Do NOT invent technologies that were not detected. You MAY infer
architectural structure that the detected stack implies.

why_this_stack MUST contain a causal verb:
  enables | decouples | avoids | prevents | allows | reduces | eliminates |
  provides | enforces | ensures | separates | abstracts | simplifies
Enumeration without causality will be rejected.

Repository: {repo_name}
Description: {repo_description or "no description"}
SoftwareType: {software_type} — {software_type_result.get("software_type_reasoning", "")}
Stack (infra noise filtered for non-infra software_types):
{json.dumps(serial, indent=2)}

{rag}
{additional_context}
{banned}

{pattern_feedback}

STACK PATTERN
Patterns commonly seen in this software_type: {pattern_enum}
These are SUGGESTIONS, not an exhaustive list. If none accurately describes this
repository's architecture, propose your own specific 2-4 word pattern name and
set "pattern_is_new": true. A precise new name beats a poor fit from the list.
Do NOT answer "Custom" or "Other" — name the actual pattern.
pattern_evidence must cite the specific detected technologies or structural
signals that justify the pattern. A pattern you cannot evidence is a guess —
lower pattern_confidence accordingly.

Return ONLY valid JSON:
{{
  "why_this_stack": "under 130 chars — causal explanation",
  "stack_pattern": "the pattern name",
  "pattern_is_new": false,
  "pattern_evidence": "under 100 chars — detected technologies or structure justifying this pattern",
  "pattern_confidence": 0.0,
  "ecosystem_context": "under 160 chars — specific industry/adoption context",
  "notable_combinations": ["specific non-obvious tech relationship in this repo"]
}}"""

    try:
        logger.info("[ai_pipeline] -> Gemini generate_stack_insights...")
        response = await asyncio.to_thread(
            _json_model.generate_content,
            prompt,
            request_options={"timeout": _STACK_INSIGHTS_TIMEOUT_SECONDS},
        )
        logger.info("[ai_pipeline] OK generate_stack_insights %s chars", len(response.text))
        result = _safe_json(
            response.text, deepcopy(_INSIGHTS_SAFE_DEFAULTS), site="stack_insights"
        )

        # Causal-verb validation
        why = result.get("why_this_stack", "")
        if why and not _has_causal_verb(why):
            result["_why_no_causal_verb"] = True
            logger.warning("[ai_pipeline] why_this_stack missing causal verb: %s", why[:100])

        # "Custom"/"Other" are forbidden by the prompt — treat as no answer so a
        # non-answer is never scored as a pattern.
        pattern = (result.get("stack_pattern") or "").strip()
        if _norm(pattern) in {"custom", "other", "none", "na"}:
            result["stack_pattern"] = ""
            result["pattern_confidence"] = 0.0
            pattern = ""

        # Emergent-pattern detection: trust the model's flag, but verify against
        # the taxonomy — a pattern can be new even when the model forgets to say so.
        if pattern:
            known = await _get_known_patterns()
            is_new = _norm(pattern) not in known
            result["pattern_is_new"] = bool(result.get("pattern_is_new")) or is_new
            if is_new:
                await _record_emergent_pattern(
                    name=pattern,
                    software_type=software_type,
                    repo=repo_name,
                    evidence=result.get("pattern_evidence", ""),
                )

        for key, default in _INSIGHTS_SAFE_DEFAULTS.items():
            result.setdefault(key, deepcopy(default))
        return result
    except Exception as e:
        _log_error("generate_stack_insights", str(e))
        return deepcopy(_INSIGHTS_SAFE_DEFAULTS)


# ══════════════════════════════════════════════════════════════════════════════
# run_full_ai_pipeline
# ══════════════════════════════════════════════════════════════════════════════

async def run_full_ai_pipeline(
    raw_detections: dict,
    file_tree: list[str],
    repo_name: str,
    repo_description: str,
    flags: list[dict] | None = None,
) -> dict:
    """
    Phase 2b software_type classification. Phase 3 stack insights is disabled.

    Input: detections already merged from Phase 0 (file signals) + Phase 2a
    (dep classification). Does NOT call classify_dependencies.
    """
    logger.info("[ai_pipeline] === PIPELINE START - repo=%s model=%s ===", repo_name, _MODEL)

    similar_repos: list[dict] = []
    embedding: list[float] = []

    try:

        prelim_stack = {
            **{
                cat: [t.model_dump() if hasattr(t, "model_dump") else t for t in techs]
                for cat, techs in raw_detections.items()
            },
            "software_type": "unknown", "stack_pattern": "",
            "why_this_stack": "", "ecosystem_context": "",
            "architecture_style": "unknown",
        }

        embedding = await embed_stack(prelim_stack)
        non_zero  = sum(1 for v in embedding if v != 0.0)
        logger.info("[ai_pipeline] Embedding: dim=%s non_zero=%s", len(embedding), non_zero)

        if non_zero > 0:
            corpus_size = await storage_service.count_embedded_analyses()
            if corpus_size >= 5:
                similar_repos = await storage_service.find_similar(embedding, limit=3)
                logger.info(
                    "[ai_pipeline] RAG: %s repos (corpus=%s)",
                    len(similar_repos),
                    corpus_size,
                )
                for r in similar_repos:
                    n = r.get("repo", {}).get("full_name", "?")
                    d = r.get("stack", {}).get("software_type", "?")
                    s = r.get("score", 0.0)
                    logger.info("[ai_pipeline]   -> %s (%s) sim=%.3f", n, d, s)
            else:
                logger.info("[ai_pipeline] RAG skipped - corpus too small (%s/5)", corpus_size)
        else:
            logger.info("[ai_pipeline] Zero embedding - skipping RAG")

    except (OSError, ValueError, TypeError, RuntimeError, asyncio.TimeoutError) as e:
        logger.warning("[ai_pipeline] Embedding/RAG failed: %s", e)
        logger.warning("[ai_pipeline] Embedding/RAG: %s", str(e)[:200])

    # Phase 2b
    result1 = await classify_software_type(
        raw_detections, file_tree,
        flags=flags or [],
        similar_repos=similar_repos,
        repo_name=repo_name,
    )

    if result1.get("rejected"):
        logger.warning("[ai_pipeline] REJECTED: %s", result1.get("rejection_reason", ""))
        return {
            **result1,
            **deepcopy(_TECH_INFERENCE_SAFE_DEFAULTS),
            **deepcopy(_INSIGHTS_SAFE_DEFAULTS),
            "ai_inferences":          [],
            "ai_classification_used": False,
            "ai_calls_made":          1,
            "rag_repos_retrieved":    len(similar_repos),
            "embedding":              embedding,
            "model_id":               _MODEL,
        }

    # Technology inference is deliberately isolated from software-type
    # classification so a large or truncated array cannot discard the label.
    tech_inference = await infer_stack_technologies(
        raw_detections,
        file_tree,
        repo_name=repo_name,
    )
    result1 = {**result1, **tech_inference}

    # Phase 3 (stack insights) is intentionally disabled. Keep the response
    # shape stable for stored analyses and API consumers without making the
    # second Gemini request.
    result2 = deepcopy(_INSIGHTS_SAFE_DEFAULTS)

    ai_inferences = []
    for t in result1.get("ai_inferred_techs", []):
        try:
            ai_inferences.append(AiInference(
                tech       = t.get("name", ""),
                technology_role   = t.get("technology_role", "library"),
                reasoning  = t.get("reasoning", ""),
                confidence = float(t.get("confidence", 0.0)),
            ))
        except Exception:
            pass

    # "did the model RUN" is not "was the model CONFIDENT". An honest "unknown"
    # must not be reported to the UI as "AI unavailable".
    ai_ran = (
        result1.get("software_type_reasoning") != "AI classification failed"
        and not result1.get("rejected", False)
    )
    software_type_confident = (
        ai_ran
        and result1.get("software_type", "unknown") != "unknown"
        and result1.get("software_type_confidence", 0.0) > 0.0
    )

    emergent = {
        "software_type":   result1.get("proposed_software_type") if result1.get("software_type_is_new") else None,
        "pattern":  result2.get("stack_pattern") if result2.get("pattern_is_new") else None,
        "technology_roles": sorted({
            t.get("proposed_technology_role")
            for t in result1.get("ai_inferred_techs", [])
            if t.get("technology_role_is_new") and t.get("proposed_technology_role")
        }),
    }

    logger.info(
        f"[ai_pipeline] ═══ COMPLETE ═══ "
        f"software_type={result1.get('software_type')} "
        f"conf={result1.get('software_type_confidence', 0):.2f} "
        f"pattern={result2.get('stack_pattern')!r} "
        f"pattern_new={result2.get('pattern_is_new')} "
        f"rejected={result1.get('rejected', False)}"
    )
    if any(emergent.values()):
        logger.info("[ai_pipeline] EMERGENT: %s", emergent)

    return {
        **result1,
        **result2,
        "ai_inferences":          [i.model_dump() for i in ai_inferences],
        "ai_classification_used": ai_ran,
        "software_type_confident":       software_type_confident,
        "emergent":               emergent,
        "ai_calls_made":          2,
        "rag_repos_retrieved":    len(similar_repos),
        "embedding":              embedding,
        "model_id":               _MODEL,
    }
