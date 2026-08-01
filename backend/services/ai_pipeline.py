"""
backend/services/ai_pipeline.py

Phase 2b (software_type classification) + Phase 3 (stack insights).
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
     for library/ml_platform that overrode the "suggestions, not exhaustive"
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
from copy import deepcopy
from dotenv import load_dotenv
from backend.services.embedding_service import embed_stack
import backend.services.storage_service as storage_service
import google.generativeai as genai
from os import getenv
import json
import logging
from backend.models.schemas import AiInference
from backend.services.rag_filter import format_rag_context

load_dotenv()

logger = logging.getLogger(__name__)

_KEY        = getenv("GEMINI_API_KEY", "")
_MODEL      = getenv("GEMINI_ANALYSIS_MODEL", "gemini-2.5-flash")
_MAX_TOKENS = int(getenv("GEMINI_MAX_TOKENS_ANALYSIS", 8192))

print(f"[ai_pipeline] KEY:   {'configured' if _KEY else 'missing'}")
print(f"[ai_pipeline] MODEL: {_MODEL}")

genai.configure(api_key=_KEY)

# Exported — imported by dep_classifier.py and analyze.py
_json_model = genai.GenerativeModel(
    _MODEL,
    generation_config=genai.types.GenerationConfig(
        response_mime_type="application/json",
        max_output_tokens=_MAX_TOKENS,
        temperature=0.2,
    ),
)

# ── Safe defaults ─────────────────────────────────────────────────────────────
# NOTE: always deepcopy these — they contain nested lists. A shallow .copy()
# shares the list objects across every caller, so one mutation leaks into the
# next analysis's defaults.

_SOFTWARE_TYPE_SAFE_DEFAULTS = {
    "software_type":             "unknown",
    "software_type_confidence":  0.0,
    "software_type_reasoning":   "AI classification failed",
    "architecture_style": "unknown",
    "missing_patterns":   [],
    "ai_inferred_techs":  [],
    "rag_influenced":     False,
    "similar_repos_used": 0,
    "rejected":           False,
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
        "Distributed Task Queue", "Decorator-Based Composition",
    ],
    "ml_platform": [
        "Plugin Architecture", "Chain of Responsibility", "Fluent Interface",
        "Hexagonal", "RAG Framework", "Multi-Agent Framework",
        "Inference Server", "Experiment Tracking Platform",
    ],
    "data_pipeline": [
        "Event-Driven", "DAG Scheduler", "Lambda Architecture",
        "Event Sourcing", "Streaming Dataflow", "Dynamic Workflow Engine",
        "Compiler / Templating Pipeline",
    ],
    "infra_tool": [
        "Plugin Architecture", "Pull-Based Scraping", "Event-Driven",
        "Hexagonal", "GitOps Controller", "Object Storage Server",
        "Package Manager / Resolver", "Runtime / VM",
    ],
    "web_api": [
        "MVC", "Hexagonal", "CQRS", "Event-Driven", "ASGI Framework",
        "ORM / Data Mapper", "Type-Safe RPC", "Middleware Pipeline",
        "Microservices", "Serverless",
    ],
    "web_app": [
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
    "language": ["Plugin Architecture", "Hexagonal", "Runtime / VM"],
    "mobile_app": ["MVC", "Hexagonal"],
    "desktop_app": ["MVC", "Plugin Architecture", "Hexagonal", "Native Webview Shell"],
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


_CLASSIFICATION_RULES = """
Classify this repository by answering:
"Who uses the final artifact of this codebase and how do they interact with it?"

RULES (strict priority order):

"language":    Repo IS a programming language, compiler, or runtime.
               Lexer/parser/AST, bytecode, runtime GC.
               Examples: python/cpython, rust-lang/rust

"library":     Developers import it into their own code.
               CRITICAL: ALL framework repos are library.
               fastapi/fastapi, nestjs/nest, gin-gonic/gin, vercel/next.js,
               spring-projects/spring-boot -> ALL library.
               SDK repos, plugin repos, client libraries -> library.

"database":    Other systems read/write data via a protocol.
               Repo IS the database/search/cache/vector-store engine.
               Examples: elasticsearch, redis, chroma, qdrant, influxdb
               NOTE: vector databases -> database, NOT ml_platform.

"data_pipeline": Data flows through it between systems.
               Stream processors, batch ETL, orchestrators, message brokers.
               Examples: kafka, airflow, flink, dagster, prefect, dbt

"ml_platform": AI/ML IS the primary product.
               LLM frameworks (LangChain/LlamaIndex/AutoGen), model serving
               (vLLM/Triton/Ollama), AI agent frameworks.
               NOT ml_platform: web apps with one AI feature, vector databases,
               infra that serves models, data pipelines producing ML features.

"infra_tool":  Operators deploy/monitor/manage other systems.
               IaC (Terraform/Helm), monitoring (Prometheus/Grafana), service mesh.
               NOTE: Grafana -> infra_tool not web_app (operators are consumer).

"web_api":     Other services call its HTTP endpoints.
               Backend framework present, no dominant frontend framework.

"web_app":     End users interact via browser.
               Frontend framework present (React/Vue/Next.js/Angular/Svelte).

"cli_tool":    End users interact via terminal.
               CLI parsing (argparse/click/cobra/clap), no HTTP server.

"mobile_app":  End users via iOS/Android.
"desktop_app": End users via native desktop (Electron/Tauri/Qt).
"unknown":     Genuinely ambiguous — insufficient signals.

DISAMBIGUATION:
Q1: Repo name matches well-known framework? -> library
Q2: Primary artifact is a storage engine? -> database
Q3: Data moves through it between systems? -> data_pipeline
Q4: AI/ML IS the product (not a feature)? -> ml_platform
Q5: Operators use it to manage infrastructure? -> infra_tool
Q6: End users see a browser UI? -> web_app
Q7: Other services call HTTP endpoints? -> web_api
Q8: Terminal binary? -> cli_tool
"""

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


def _safe_json(text: str, fallback: dict | list) -> dict | list:
    try:
        cleaned = (text or "").strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            inner = lines[1:] if len(lines) > 1 else lines
            if inner and inner[-1].strip() == "```":
                inner = inner[:-1]
            cleaned = "\n".join(inner).strip()
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        print(f"[ai_pipeline] JSON parse failed: {e} — raw: {(text or '')[:300]}")
        return deepcopy(fallback)


def _log_error(fn: str, err: str) -> None:
    if "429" in err or "quota" in err.lower() or "ResourceExhausted" in err:
        msg = "QUOTA EXCEEDED"
    elif "403" in err or "PERMISSION_DENIED" in err or "API_KEY_INVALID" in err:
        msg = "AUTH FAILED — check GEMINI_API_KEY"
    elif "404" in err or "not found" in err.lower() or "MODEL_NOT_FOUND" in err:
        msg = f"MODEL NOT FOUND — check GEMINI_ANALYSIS_MODEL={_MODEL}"
    else:
        msg = "FAILED"
    print(f"[ai_pipeline] {fn} {msg}: {err[:300]}")
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
    - Exclude infra noise unless software_type == infra_tool
    - Exclude non-required/non-product manifest deps (dev tools, test scaffolding)
    """
    if software_type == "infra_tool":
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
        import backend.services.storage_service as storage_service
        software_types = await storage_service.get_software_types()
        names = [d["_id"] if isinstance(d, dict) else d for d in software_types]
        if names:
            return " | ".join(names)
    except Exception as e:
        logger.debug("[ai_pipeline] SoftwareType taxonomy fetch failed: %s", e)
    return "unknown"


async def _get_valid_software_types() -> set[str]:
    try:
        import backend.services.storage_service as storage_service
        return set(await storage_service.get_valid_software_types())
    except Exception as e:
        logger.debug("[ai_pipeline] Valid-software_type fetch failed: %s", e)
        return set()   # empty => caller SKIPS validation rather than rejecting all


async def _build_software_type_feedback_context() -> str:
    try:
        import backend.services.storage_service as storage_service
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
        import backend.services.storage_service as storage_service
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
        import backend.services.storage_service as storage_service
        patterns = await storage_service.get_stack_patterns()
        names = [p["_id"] if isinstance(p, dict) else p for p in patterns]
        if names:
            return {_norm(n) for n in names}
    except Exception:
        pass
    return {_norm(p) for p in _ALL_PATTERNS}


async def _build_pattern_feedback_context() -> str:
    try:
        import backend.services.storage_service as storage_service
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
        import backend.services.storage_service as storage_service
        await storage_service.record_emergent_pattern(
            name=name, software_type=software_type, example_repo=repo, evidence=evidence,
        )
        print(f"[ai_pipeline] emergent pattern recorded: {name!r} ({software_type}) from {repo}")
    except Exception as e:
        logger.debug("[ai_pipeline] record_emergent_pattern unavailable: %s", e)


# ── TechnologyRoles (for ai_inferred_techs) ────────────────────────────────────────

async def _get_valid_technology_roles() -> set[str]:
    try:
        import backend.services.storage_service as storage_service
        cats = await storage_service.get_valid_technology_roles()
        if cats:
            return set(cats)
    except Exception as e:
        logger.debug("[ai_pipeline] TechnologyRole taxonomy fetch failed: %s", e)
    return set(_BUILTIN_TECHNOLOGY_ROLES)


async def _record_emergent_technology_role(name: str, tech: str, repo: str) -> None:
    try:
        import backend.services.storage_service as storage_service
        await storage_service.record_emergent_technology_role(name, tech, repo)
        print(f"[ai_pipeline] emergent technology_role recorded: {name!r} (tech={tech}) from {repo}")
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
    flags: list[dict] = None,
    similar_repos: list[dict] = None,
    repo_name: str = "",
) -> dict:
    """
    SoftwareType classification. Receives Phase 0 + Phase 2a detections.

    flags: quality flags passed so Gemini can output rejected:true when
           detections are implausible rather than rationalising them.
    """
    print(
        f"[ai_pipeline] classify_software_type — model={_MODEL} "
        f"rag={'YES (' + str(len(similar_repos)) + ')' if similar_repos else 'NO'}"
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

    technology_role_options = " | ".join(sorted(await _get_valid_technology_roles()))

    prompt = f"""{_CLASSIFICATION_RULES}

{rag_section}

{software_type_feedback}

DETECTED TECHNOLOGIES (Phase 0 file signals + Phase 2a dep classification):
{json.dumps(serializable, indent=2)}

File tree sample (60 files): {json.dumps(file_tree[:60])}

{flags_str}

SOFTWARE_TYPE
Known software_types: {software_type_options}
Choose the software_type that best fits. If NONE of them honestly describes this
repository, propose a specific snake_case software_type name and set
"software_type_is_new": true. A precise new software_type beats forcing a poor fit.
Do not invent a software_type when an existing one fits.

TECHNOLOGY_ROLES for ai_inferred_techs
Known technology_roles: {technology_role_options}
Use one of these. If a technology genuinely belongs to none of them, name a
specific snake_case technology_role — it will be reviewed before adoption.

Return ONLY valid JSON:
{{
  "software_type": "the software_type name",
  "software_type_is_new": false,
  "software_type_confidence": 0.0,
  "software_type_reasoning": "under 120 chars — cite specific evidence",
  "architecture_style": "one of: monolith | microservices | serverless | event_driven | unknown",
  "missing_patterns": ["tech NAMES likely used but absent from detected stack — NOT filenames"],
  "ai_inferred_techs": [
    {{"name": "str", "technology_role": "str", "confidence": 0.0, "reasoning": "under 80 chars"}}
  ],
  "rejected": false,
  "rejection_reason": "only if rejected=true — why detections are implausible",
  "rag_influenced": {json.dumps(bool(similar_repos))}
}}"""

    try:
        print("[ai_pipeline] → Gemini classify_software_type...")
        response = await asyncio.to_thread(_json_model.generate_content, prompt)
        print(f"[ai_pipeline] ✓ classify_software_type {len(response.text)} chars")
        result = _safe_json(response.text, deepcopy(_SOFTWARE_TYPE_SAFE_DEFAULTS))

        # ── SoftwareType validation: record-then-fallback, don't silently blank ──
        proposed = (result.get("software_type") or "").strip()
        valid_software_types = await _get_valid_software_types()
        if valid_software_types and proposed and proposed not in valid_software_types:
            # An out-of-vocabulary software_type is a SIGNAL, not noise. Record it as a
            # pending emergent software_type, then fall back so the analysis stays usable.
            try:
                import backend.services.storage_service as storage_service
                await storage_service.record_emergent_software_type(
                    name=proposed,
                    example_repo=repo_name or "unknown",
                    reasoning=result.get("software_type_reasoning", ""),
                )
                print(f"[ai_pipeline] emergent software_type recorded: {proposed!r} from {repo_name}")
            except Exception as e:
                logger.debug("[ai_pipeline] record_emergent_software_type unavailable: %s", e)

            result["proposed_software_type"] = proposed
            result["software_type_is_new"] = True
            result["software_type"] = "unknown"
            result["software_type_confidence"] = 0.0
            result["software_type_reasoning"] = (
                f"Proposed '{proposed}' is outside the active taxonomy — pending review"
            )

        # ── TechnologyRole validation on ai_inferred_techs ──────────────────────────
        valid_technology_roles = await _get_valid_technology_roles()
        cleaned_techs = []
        for t in result.get("ai_inferred_techs", []) or []:
            if not isinstance(t, dict):
                continue
            cat = (t.get("technology_role") or "").strip()
            if cat and cat not in valid_technology_roles:
                await _record_emergent_technology_role(cat, t.get("name", ""), repo_name or "unknown")
                t["proposed_technology_role"] = cat
                t["technology_role_is_new"] = True
                t["technology_role"] = "library"   # safe home until reviewed
            cleaned_techs.append(t)
        result["ai_inferred_techs"] = cleaned_techs

        result["rag_influenced"]     = bool(similar_repos)
        result["similar_repos_used"] = len(similar_repos) if similar_repos else 0
        result.setdefault("rejected", False)
        result.setdefault("software_type_is_new", False)
        return result
    except Exception as e:
        _log_error("classify_software_type", str(e))
        return deepcopy(_SOFTWARE_TYPE_SAFE_DEFAULTS)


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
    - Infra noise filtered unless software_type == infra_tool
    - stack_pattern SUGGESTED (not restricted) by software_type; new patterns allowed
      and recorded as emergent
    - why_this_stack validated for a causal verb
    """
    print(
        f"[ai_pipeline] generate_stack_insights — "
        f"software_type={software_type_result.get('software_type')} "
        f"rag={'YES' if similar_repos else 'NO'}"
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
    # This block previously said "Allowed: <four patterns>" for library/ml_platform,
    # which contradicted the "suggestions, not exhaustive" instruction below and
    # made correct answers like "Sync / Async Client" unreachable for httpx.
    # It now describes what KIND of pattern fits, without enumerating a closed set.
    additional_context = ""
    if software_type in ("library", "ml_platform"):
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
    elif software_type == "infra_tool":
        additional_context = """
NOTE: For infra tools, the pattern should describe the CONTROL model (reconcile
loop, pull-based scraping, plugin/provider extension, declarative apply).
"""

    banned = ""
    if software_type not in ("web_api", "web_app"):
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
        print("[ai_pipeline] → Gemini generate_stack_insights...")
        response = await asyncio.to_thread(_json_model.generate_content, prompt)
        print(f"[ai_pipeline] ✓ generate_stack_insights {len(response.text)} chars")
        result = _safe_json(response.text, deepcopy(_INSIGHTS_SAFE_DEFAULTS))

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
    flags: list[dict] = None,
) -> dict:
    """
    Phase 2b + Phase 3.

    Input: detections already merged from Phase 0 (file signals) + Phase 2a
    (dep classification). Does NOT call classify_dependencies.
    """
    print(f"[ai_pipeline] ═══ PIPELINE START — repo={repo_name} model={_MODEL} ═══")

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
        print(f"[ai_pipeline] Embedding: dim={len(embedding)} non_zero={non_zero}")

        if non_zero > 0:
            corpus_size = await storage_service.count_embedded_analyses()
            if corpus_size >= 5:
                similar_repos = await storage_service.find_similar(embedding, limit=3)
                print(f"[ai_pipeline] RAG: {len(similar_repos)} repos (corpus={corpus_size})")
                for r in similar_repos:
                    n = r.get("repo", {}).get("full_name", "?")
                    d = r.get("stack", {}).get("software_type", "?")
                    s = r.get("score", 0.0)
                    print(f"  → {n} ({d}) sim={s:.3f}")
            else:
                print(f"[ai_pipeline] RAG skipped — corpus too small ({corpus_size}/5)")
        else:
            print("[ai_pipeline] Zero embedding — skipping RAG")

    except Exception as e:
        print(f"[ai_pipeline] Embedding/RAG failed: {e}")
        logger.warning("[ai_pipeline] Embedding/RAG: %s", str(e)[:200])

    # Phase 2b
    result1 = await classify_software_type(
        raw_detections, file_tree,
        flags=flags or [],
        similar_repos=similar_repos,
        repo_name=repo_name,
    )

    if result1.get("rejected"):
        print(f"[ai_pipeline] REJECTED: {result1.get('rejection_reason', '')}")
        return {
            **result1,
            **deepcopy(_INSIGHTS_SAFE_DEFAULTS),
            "ai_inferences":          [],
            "ai_classification_used": False,
            "ai_calls_made":          1,
            "rag_repos_retrieved":    len(similar_repos),
            "embedding":              embedding,
            "model_id":               _MODEL,
        }

    # Phase 3
    result2 = await generate_stack_insights(
        result1, raw_detections, repo_name, repo_description, similar_repos
    )

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

    print(
        f"[ai_pipeline] ═══ COMPLETE ═══ "
        f"software_type={result1.get('software_type')} "
        f"conf={result1.get('software_type_confidence', 0):.2f} "
        f"pattern={result2.get('stack_pattern')!r} "
        f"pattern_new={result2.get('pattern_is_new')} "
        f"rejected={result1.get('rejected', False)}"
    )
    if any(emergent.values()):
        print(f"[ai_pipeline] EMERGENT: {emergent}")

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
