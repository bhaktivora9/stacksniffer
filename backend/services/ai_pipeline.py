"""
backend/services/ai_pipeline.py

Phase 2b (domain classification) + Phase 3 (stack insights) of the detection pipeline.

Phase 2a (dependency classification) is in dep_classifier.py.
This file is now domain + insights only — it does NOT classify packages.

Changes from previous version:
  - run_full_ai_pipeline() accepts detections that already include Phase 0 + Phase 2a results
  - _filter_insights_techs() retains scope/origin awareness from manifest classification
  - _json_model exposed at module level for dep_classifier.py to import
  - model_id in every response
  - rejected: true path — short-circuits insights when classification implausible
  - DAG Scheduler + Pull-Based Scraping in _DOMAIN_PATTERN_MAP
  - Causal verb validation on why_this_stack
"""
import asyncio
from dotenv import load_dotenv
load_dotenv()

import google.generativeai as genai
from os import getenv
import json
import logging
from backend.models.schemas import AiInference
from backend.services.rag_filter import format_rag_context

logger = logging.getLogger(__name__)

_KEY        = getenv("GEMINI_API_KEY", "")
_MODEL      = getenv("GEMINI_ANALYSIS_MODEL", "gemini-2.5-flash")
_MAX_TOKENS = int(getenv("GEMINI_MAX_TOKENS_ANALYSIS", 8192))

print(f"[ai_pipeline] KEY:   {'YES — ' + _KEY[:12] + '...' if _KEY else 'NO — MISSING'}")
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

_DOMAIN_SAFE_DEFAULTS = {
    "domain":             "unknown",
    "domain_confidence":  0.0,
    "domain_reasoning":   "AI classification failed",
    "architecture_style": "unknown",
    "missing_patterns":   [],
    "ai_inferred_techs":  [],
    "rag_influenced":     False,
    "similar_repos_used": 0,
    "rejected":           False,
}

_INSIGHTS_SAFE_DEFAULTS = {
    "why_this_stack":       "",
    "stack_pattern":        "Custom",
    "ecosystem_context":    "",
    "notable_combinations": [],
}

# Causal verbs required in why_this_stack
_CAUSAL_VERBS = [
    "enables", "decouples", "avoids", "prevents", "allows",
    "reduces", "eliminates", "provides", "enforces", "ensures",
    "separates", "abstracts", "simplifies", "offloads", "replaces",
]

# Domain → allowed stack_pattern values
# Gemini prompt injects only the allowed set for the classified domain
_DOMAIN_PATTERN_MAP: dict[str, list[str]] = {
    "library":       [
        "Plugin Architecture", "Chain of Responsibility",
        "Fluent Interface", "Hexagonal", "MVC", "Custom"
    ],
    "ml_platform":   [
        "Plugin Architecture", "Chain of Responsibility",
        "Fluent Interface", "Hexagonal", "Custom"
    ],
    "data_pipeline": [
        "Event-Driven", "DAG Scheduler", "Lambda Architecture",
        "Event Sourcing", "Custom"
    ],
    "infra_tool":    [
        "Plugin Architecture", "Pull-Based Scraping",
        "Event-Driven", "Hexagonal", "Custom"
    ],
    "web_api":       [
        "MVC", "Hexagonal", "CQRS", "Event-Driven",
        "Microservices", "Serverless", "Custom"
    ],
    "web_app":       ["MVC", "JAMstack", "Hexagonal", "Custom"],
    "database":      ["Plugin Architecture", "Hexagonal", "Event-Driven", "Custom"],
    "cli_tool":      ["Plugin Architecture", "Hexagonal", "Custom"],
    "language":      ["Plugin Architecture", "Hexagonal", "Custom"],
    "mobile_app":    ["MVC", "Hexagonal", "Custom"],
    "desktop_app":   ["MVC", "Plugin Architecture", "Hexagonal", "Custom"],
    "unknown":       [
        "Plugin Architecture", "Chain of Responsibility", "Fluent Interface",
        "Hexagonal", "CQRS", "Event Sourcing", "Lambda Architecture",
        "JAMstack", "Microservices", "Event-Driven", "Serverless", "MVC",
        "DAG Scheduler", "Pull-Based Scraping", "Custom"
    ],
}
_ALL_PATTERNS = sorted({
    p for patterns in _DOMAIN_PATTERN_MAP.values() for p in patterns
})

# Classification rules — injected into classify_domain prompt
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
               spring-projects/spring-boot → ALL library.
               SDK repos, plugin repos, client libraries → library.

"database":    Other systems read/write data via a protocol.
               Repo IS the database/search/cache/vector-store engine.
               Examples: elasticsearch, redis, chroma, qdrant, influxdb
               NOTE: vector databases → database, NOT ml_platform.

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
               NOTE: Grafana → infra_tool not web_app (operators are consumer).

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
Q1: Repo name matches well-known framework? → library
Q2: Primary artifact is a storage engine? → database
Q3: Data moves through it between systems? → data_pipeline
Q4: AI/ML IS the product (not a feature)? → ml_platform
Q5: Operators use it to manage infrastructure? → infra_tool
Q6: End users see a browser UI? → web_app
Q7: Other services call HTTP endpoints? → web_api
Q8: Terminal binary? → cli_tool
"""

# Infra techs excluded from insights unless domain == infra_tool
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
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines   = cleaned.split("\n")
            inner   = lines[1:] if len(lines) > 1 else lines
            if inner and inner[-1].strip() == "```":
                inner = inner[:-1]
            cleaned = "\n".join(inner).strip()
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        print(f"[ai_pipeline] JSON parse failed: {e} — raw: {text[:300]}")
        return fallback


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
    t = text.lower()
    return any(v in t for v in _CAUSAL_VERBS)


def _filter_insights_techs(detections: dict, domain: str) -> dict:
    """
    Filter techs fed to generate_stack_insights().
    - Exclude infra noise unless domain == infra_tool
    - Exclude non-required/non-product manifest deps (dev tools, test scaffolding)
    """
    if domain == "infra_tool":
        return detections

    result = {}
    for cat, techs in detections.items():
        # Filter out dev/test-only manifest deps
        insight_techs = [
            t for t in techs
            if not (
                _tech_get(t, "detection_source") == "manifest"
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


# ── Dynamic domain taxonomy ───────────────────────────────────────────────────

async def _get_domain_options() -> str:
    try:
        import backend.services.storage_service as storage_service
        return " | ".join(domain["_id"] for domain in await storage_service.get_domains())
    except Exception as e:
        logger.debug("[ai_pipeline] Domain taxonomy fetch failed: %s", e)
    return "unknown"


# ── RAG context ───────────────────────────────────────────────────────────────

# ── Phase 2b: classify_domain ─────────────────────────────────────────────────

async def classify_domain(
    raw_detections: dict,
    file_tree: list[str],
    flags: list[dict] = None,
    similar_repos: list[dict] = None,
) -> dict:
    """
    Domain classification — blind to pattern_matches.
    Receives tech category summaries from Phase 0 + Phase 2a.

    flags: quality flags passed so Gemini can output rejected:true
           when detections are implausible rather than rationalising them.
    """
    print(
        f"[ai_pipeline] classify_domain — model={_MODEL} "
        f"rag={'YES (' + str(len(similar_repos)) + ')' if similar_repos else 'NO'}"
    )

    serializable   = _serialize(raw_detections)
    domain_options = await _get_domain_options()
    rag_section    = format_rag_context(similar_repos or [])

    # Error flags passed to Gemini — it can reject implausible detections
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

DETECTED TECHNOLOGIES (Phase 0 file signals + Phase 2a Gemini dep classification):
{json.dumps(serializable, indent=2)}

File tree sample (60 files): {json.dumps(file_tree[:60])}

{flags_str}

Return ONLY valid JSON:
{{
  "domain": "one of: {domain_options}",
  "domain_confidence": 0.0,
  "domain_reasoning": "under 120 chars — cite specific evidence",
  "architecture_style": "one of: monolith | microservices | serverless | event_driven | unknown",
  "missing_patterns": ["tech NAMES likely used but absent from detected stack — NOT filenames"],
  "ai_inferred_techs": [
    {{"name": "str", "category": "str", "confidence": 0.0, "reasoning": "under 80 chars"}}
  ],
  "rejected": false,
  "rejection_reason": "only if rejected=true — why detections are implausible",
  "rag_influenced": {json.dumps(bool(similar_repos))}
}}"""

    try:
        print(f"[ai_pipeline] → Gemini classify_domain...")
        response = await asyncio.to_thread(_json_model.generate_content, prompt)
        print(f"[ai_pipeline] ✓ classify_domain {len(response.text)} chars")
        result = _safe_json(response.text, _DOMAIN_SAFE_DEFAULTS.copy())
        import backend.services.storage_service as storage_service
        valid_domains = await storage_service.get_valid_domains()
        if result.get("domain") not in valid_domains:
            result["domain"] = "unknown"
            result["domain_confidence"] = 0.0
            result["domain_reasoning"] = "Model emitted a domain outside the active taxonomy"
        result["rag_influenced"]     = bool(similar_repos)
        result["similar_repos_used"] = len(similar_repos) if similar_repos else 0
        result.setdefault("rejected", False)
        return result
    except Exception as e:
        _log_error("classify_domain", str(e))
        return _DOMAIN_SAFE_DEFAULTS.copy()


# ── Phase 3: generate_stack_insights ─────────────────────────────────────────

async def generate_stack_insights(
    domain_result: dict,
    detections: dict,
    repo_name: str,
    repo_description: str,
    similar_repos: list[dict] = None,
) -> dict:
    """
    Generate architectural insights.
    - Infra noise filtered from input unless domain == infra_tool
    - stack_pattern constrained by domain (DOMAIN_PATTERN_MAP)
    - why_this_stack validated for causal verb
    - Microservices banned for library/ml_platform repos
    """
    print(
        f"[ai_pipeline] generate_stack_insights — "
        f"domain={domain_result.get('domain')} "
        f"rag={'YES' if similar_repos else 'NO'}"
    )

    domain   = domain_result.get("domain", "unknown")
    filtered = _filter_insights_techs(detections, domain)
    serial   = _serialize(filtered)
    rag      = format_rag_context(similar_repos or [])

    # Domain-specific pattern guidance
    allowed_patterns = _DOMAIN_PATTERN_MAP.get(domain, _ALL_PATTERNS)
    pattern_enum     = " | ".join(allowed_patterns)

    additional_context = ""
    if domain in ("library", "ml_platform"):
        additional_context = """
NOTE: This repo is a LIBRARY or FRAMEWORK (or ml_platform framework).
stack_pattern MUST describe its design architecture, NOT deployment topology.
Allowed: Plugin Architecture, Chain of Responsibility, Fluent Interface, Hexagonal
NOT allowed: Microservices, Serverless, Event-Driven (unless this IS a service)
"""
    elif domain == "data_pipeline":
        additional_context = """
For data pipeline repos:
DAG Scheduler (airflow/dagster), Event-Driven (kafka/flink),
Lambda Architecture (batch + stream), Event Sourcing.
"""
    elif domain == "infra_tool":
        additional_context = """
For infra tools:
Plugin Architecture (Grafana/Terraform), Pull-Based Scraping (Prometheus),
Event-Driven (Kubernetes controllers), Hexagonal.
"""

    banned = ""
    if domain not in ("web_api", "web_app"):
        banned = '\nDo NOT use the word "microservices" in any field.'

    prompt = f"""You are a principal engineer providing architectural analysis.
Base ALL insights on detected technologies only. Do not invent features.
why_this_stack MUST contain a causal verb:
  enables | decouples | avoids | prevents | allows | reduces | eliminates |
  provides | enforces | ensures | separates | abstracts | simplifies
Enumeration without causality will be rejected.

Repository: {repo_name}
Description: {repo_description or "no description"}
Domain: {domain} — {domain_result.get("domain_reasoning", "")}
Stack (infra noise filtered for non-infra domains):
{json.dumps(serial, indent=2)}

{rag}
{additional_context}
{banned}

Return ONLY valid JSON:
{{
  "why_this_stack": "under 130 chars — causal explanation with enables/decouples/etc",
  "stack_pattern": "one of: {pattern_enum}",
  "ecosystem_context": "under 160 chars — specific industry/adoption context",
  "notable_combinations": ["specific non-obvious tech relationship in this repo"]
}}"""

    try:
        print(f"[ai_pipeline] → Gemini generate_stack_insights...")
        response = await asyncio.to_thread(_json_model.generate_content, prompt)
        print(f"[ai_pipeline] ✓ generate_stack_insights {len(response.text)} chars")
        result = _safe_json(response.text, _INSIGHTS_SAFE_DEFAULTS.copy())

        # Validate causal verb
        why = result.get("why_this_stack", "")
        if why and not _has_causal_verb(why):
            result["_why_no_causal_verb"] = True
            logger.warning("[ai_pipeline] why_this_stack missing causal verb: %s", why[:100])

        return result
    except Exception as e:
        _log_error("generate_stack_insights", str(e))
        return _INSIGHTS_SAFE_DEFAULTS.copy()


# ── run_full_ai_pipeline ──────────────────────────────────────────────────────

async def run_full_ai_pipeline(
    raw_detections: dict,
    file_tree: list[str],
    repo_name: str,
    repo_description: str,
    flags: list[dict] = None,
) -> dict:
    """
    Phase 2b + Phase 3 of the detection pipeline.

    Input: detections already merged from Phase 0 (file signals) +
           Phase 2a (Gemini dep classification).
    Does NOT call classify_dependencies — that happens in analyze.py Phase 2a.

    Steps:
      1. Embed detections for RAG retrieval
      2. Find similar corpus repos (if corpus >= 5)
      3. classify_domain() — blind to pattern_matches, sees flags
      4. If rejected → return early
      5. generate_stack_insights() — domain-aware, infra-filtered
    """
    print(f"[ai_pipeline] ═══ PIPELINE START — repo={repo_name} model={_MODEL} ═══")

    similar_repos: list[dict] = []
    embedding:     list[float] = []

    # RAG: embed current detections + find similar corpus repos
    try:
        from backend.services.embedding_service import embed_stack
        import backend.services.storage_service as storage_service

        prelim_stack = {
            **{
                cat: [
                    t.model_dump() if hasattr(t, "model_dump") else t
                    for t in techs
                ]
                for cat, techs in raw_detections.items()
            },
            "domain": "unknown", "stack_pattern": "",
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
                    d = r.get("stack", {}).get("domain", "?")
                    s = r.get("score", 0.0)
                    print(f"  → {n} ({d}) sim={s:.3f}")
            else:
                print(f"[ai_pipeline] RAG skipped — corpus too small ({corpus_size}/5)")
        else:
            print("[ai_pipeline] Zero embedding — skipping RAG")

    except Exception as e:
        print(f"[ai_pipeline] Embedding/RAG failed: {e}")
        logger.warning("[ai_pipeline] Embedding/RAG: %s", str(e)[:200])

    # Phase 2b: domain classification
    result1 = await classify_domain(
        raw_detections, file_tree,
        flags         = flags or [],
        similar_repos = similar_repos,
    )

    # Rejected path — Gemini flagged detections as implausible
    if result1.get("rejected"):
        print(f"[ai_pipeline] REJECTED: {result1.get('rejection_reason', '')}")
        return {
            **result1,
            **_INSIGHTS_SAFE_DEFAULTS,
            "ai_inferences":          [],
            "ai_classification_used": False,
            "ai_calls_made":          1,
            "rag_repos_retrieved":    len(similar_repos),
            "embedding":              embedding,
            "model_id":               _MODEL,
        }

    # Phase 3: insights
    result2 = await generate_stack_insights(
        result1, raw_detections, repo_name, repo_description, similar_repos
    )

    # Build AiInference objects
    ai_inferences = []
    for t in result1.get("ai_inferred_techs", []):
        try:
            ai_inferences.append(AiInference(
                tech       = t.get("name", ""),
                category   = t.get("category", "infra"),
                reasoning  = t.get("reasoning", ""),
                confidence = float(t.get("confidence", 0.0)),
            ))
        except Exception:
            pass

    ai_used = (
        result1.get("domain", "unknown") != "unknown"
        and result1.get("domain_confidence", 0.0) > 0.0
        and not result1.get("rejected", False)
    )

    print(
        f"[ai_pipeline] ═══ COMPLETE ═══ "
        f"domain={result1.get('domain')} "
        f"conf={result1.get('domain_confidence', 0):.2f} "
        f"pattern={result2.get('stack_pattern')} "
        f"rejected={result1.get('rejected', False)}"
    )

    return {
        **result1,
        **result2,
        "ai_inferences":          [i.model_dump() for i in ai_inferences],
        "ai_classification_used": ai_used,
        "ai_calls_made":          2,
        "rag_repos_retrieved":    len(similar_repos),
        "embedding":              embedding,
        "model_id":               _MODEL,
    }
